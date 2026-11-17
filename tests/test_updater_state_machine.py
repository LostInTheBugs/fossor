"""Machine à états de ``fossor-updater`` (§4.18) — Docker entièrement simulé.

Aucun test ne touche au démon Docker ni au réseau : le runner est un faux qui
enregistre les quatre opérations autorisées, et le contrôle de santé répond
selon la version demandée.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from updater.updater import (
    Config,
    Updater,
    DockerRunner,
    find_backup,
    read_env_tag,
    schema_migrated,
    set_env_tag,
    valid_tag,
    validate_request,
)

IMAGE = "ghcr.io/lostinthebugs/fossor"
TARGET = "2026.11.991"
OLD = "2026.11.990"


class FakeRunner:
    """Les opérations autorisées, sans Docker."""

    def __init__(self, healthy=(), images=(), running=True):
        self.healthy = set(healthy)
        self.images = set(images)
        self.running = running
        self.pulled: list[str] = []
        self.compose_calls: list[str | None] = []
        self.health_calls: list[str] = []
        self.helpers: list[list[str]] = []

    def pull(self, reference):
        self.pulled.append(reference)
        return True, "pulled"

    def image_present(self, reference):
        return reference in self.images

    def compose_up(self, restore=None):
        self.compose_calls.append(restore)
        return True, "recreated"

    def container_running(self):
        return self.running

    def start_helper(self, argv):
        self.helpers.append(argv)
        return True, "amorce"

    def health_once(self, expected):
        self.health_calls.append(expected)
        return (expected in self.healthy), f"version {expected}"


def make_config(tmp_path: Path, **overrides) -> Config:
    compose = tmp_path / "compose"
    compose.mkdir(exist_ok=True)
    (compose / ".env").write_text(
        "# commentaire conservé\n"
        "FOSSOR_MASTER_KEY=abc\n"
        f"FOSSOR_IMAGE_TAG={OLD}\n"
        "FOSSOR_PORT=8008\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True, exist_ok=True)
    (data / "backups").mkdir(parents=True, exist_ok=True)
    defaults = dict(
        update_dir=data / "update",
        compose_dir=compose,
        project="fossor-test",
        image=IMAGE,
        backup_dir=data / "backups",
        data_dir=data,
        health_timeout=1,
        health_poll=0.01,
        poll=2,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_backup(config: Config, target: str = TARGET) -> Path:
    path = config.backup_dir / f"pre-{target}-20261116T030000Z"
    path.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path / "fossor.db")
    con.execute("create table machines (id integer primary key, name text)")
    con.execute("insert into machines (name) values ('rig-1')")
    con.commit()
    con.close()
    return path


def request(target: str = TARGET, image: str = IMAGE, nonce: str = "abc123456789"):
    return {"target": target, "image": image, "requested_at": "2026-11-16T03:00:00+00:00",
            "nonce": nonce}


def env_tag(config: Config) -> str | None:
    return read_env_tag(config.env_path.read_text(encoding="utf-8"))


# ── Validation ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("tag", ["2026.11.004", "2026.11.990", "2026.11.004-c2", "2099.01.999"])
def test_valid_tags_accepted(tag):
    assert valid_tag(tag)
    ok, reason = validate_request(request(tag), IMAGE)
    assert ok, reason


@pytest.mark.parametrize(
    "tag",
    ["latest", "latest; rm -rf /", "2026.11.4", "26.11.004", "2026.11.004-c", "2026.11.004\nrm -rf /", "", None],
)
def test_invalid_tags_refused(tag):
    assert not valid_tag(tag)
    ok, reason = validate_request(request(tag), IMAGE)
    assert not ok
    assert "tag refusé" in reason


def test_foreign_image_refused():
    ok, reason = validate_request(request(image="docker.io/evil/fossor"), IMAGE)
    assert not ok
    assert "image refusée" in reason


def test_missing_nonce_refused():
    payload = request()
    payload.pop("nonce")
    ok, reason = validate_request(payload, IMAGE)
    assert not ok and "nonce" in reason


def test_env_tag_rewrite_keeps_everything_else():
    text = "# commentaire\nMASTER=abc\nFOSSOR_IMAGE_TAG=2026.11.990\nPORT=8008\n"
    out = set_env_tag(text, "2026.11.991")
    assert "FOSSOR_IMAGE_TAG=2026.11.991" in out
    assert "MASTER=abc" in out and "PORT=8008" in out and "# commentaire" in out
    assert out.count("FOSSOR_IMAGE_TAG") == 1
    with pytest.raises(ValueError):
        set_env_tag(text, "991\nrm -rf /")


# ── Machine à états ────────────────────────────────────────────────────
def test_nominal_update_reaches_done(tmp_path):
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)

    assert updater.handle(request()) == "done"
    assert env_tag(config) == TARGET
    # 2026.11.003-c8 : l'application **puis** le compagnon sont tirés.
    assert runner.pulled[0] == f"{IMAGE}:{TARGET}"
    assert f"{updater.config.updater_image}:{TARGET}" in runner.pulled
    assert runner.compose_calls == [None]
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "done"
    assert status["from"] == OLD and status["to"] == TARGET
    events = [json.loads(line)["event"] for line in config.history_path.read_text().splitlines()]
    assert events[:6] == ["request", "pulling", "backing_up", "switching", "health_check", "done"]
    # puis le compagnon tire son image et se remplace, puis la fin explicite.
    assert events[-3:] == ["self_pull", "self_update", "finished"]
    assert status["handled_nonce"] == "abc123456789"


def test_health_failure_rolls_back_and_restores_migrated_schema(tmp_path):
    config = make_config(tmp_path)
    backup = make_backup(config)
    # La version fautive a migré le schéma : la base vive diffère de la sauvegarde.
    live = config.data_dir / "fossor.db"
    con = sqlite3.connect(live)
    con.execute("create table machines (id integer primary key, name text, gpu_count integer)")
    con.commit()
    con.close()
    assert schema_migrated(live, backup / "fossor.db")

    runner = FakeRunner(healthy={OLD}, running=True)
    updater = Updater(config, runner)

    assert updater.handle(request()) == "rolled_back"
    assert env_tag(config) == OLD
    # pull puis bascule puis retour arrière avec la sauvegarde en argument
    assert runner.compose_calls == [None, str(backup)]
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "rolled_back"
    assert status["restore"] is True
    history = [json.loads(line) for line in config.history_path.read_text().splitlines()]
    assert any(item["event"] == "restore" and item["backup"] == str(backup) for item in history)


def test_rollback_without_schema_change_does_not_restore(tmp_path):
    config = make_config(tmp_path)
    backup = make_backup(config)
    # Même schéma des deux côtés : rien à restaurer.
    live = config.data_dir / "fossor.db"
    con = sqlite3.connect(live)
    con.execute("create table machines (id integer primary key, name text)")
    con.commit()
    con.close()
    assert not schema_migrated(live, backup / "fossor.db")

    runner = FakeRunner(healthy={OLD})
    updater = Updater(config, runner)
    assert updater.handle(request()) == "rolled_back"
    assert runner.compose_calls == [None, None]
    assert json.loads(config.status_path.read_text())["restore"] is False


def test_missing_backup_refuses_to_switch(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)

    assert updater.handle(request()) == "failed"
    assert env_tag(config) == OLD
    assert runner.compose_calls == []
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "failed" and "sauvegarde" in status["message"]


def test_invalid_tag_is_logged_and_never_pulled(tmp_path):
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy={TARGET})
    config.request_path.write_text(json.dumps(request("latest; rm -rf /")), encoding="utf-8")
    updater = Updater(config, runner)

    assert updater.step_once() == "failed"
    assert runner.pulled == [] and runner.compose_calls == []
    assert env_tag(config) == OLD
    history = [json.loads(line) for line in config.history_path.read_text().splitlines()]
    assert history[0]["event"] == "refused"
    assert "tag refusé" in history[0]["reason"]
    assert not (config.update_dir / "request.json").exists()
    assert list(config.update_dir.glob("request.refused-*.json"))


def test_local_mode_skips_pull_but_requires_the_image(tmp_path):
    config = make_config(tmp_path, local_images=True)
    make_backup(config)
    missing = FakeRunner(healthy={TARGET}, images=set())
    assert Updater(config, missing).handle(request()) == "failed"
    assert missing.pulled == []

    present = FakeRunner(healthy={TARGET}, images={f"{IMAGE}:{TARGET}"})
    assert Updater(config, present).handle(request(nonce="abc123456780")) == "done"
    assert present.pulled == []


def test_container_stopped_fails_fast(tmp_path):
    """Conteneur arrêté : on n'attend pas les 120 s du contrôle de santé."""
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy=set(), running=False)
    updater = Updater(config, runner)
    assert updater.handle(request()) == "failed"
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "failed"
    assert "ne répond pas" in status["message"] or "conteneur arrêté" in status["message"]
    # trois sondes par contrôle de santé au lieu d'une boucle de 120 s
    assert len(runner.health_calls) <= 12


def test_same_nonce_is_ignored(tmp_path):
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)
    assert updater.handle(request()) == "done"
    assert updater.handle(request()) == "ignored"
    assert len(runner.compose_calls) == 1


def test_request_file_drives_one_step_and_heartbeat(tmp_path):
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)

    assert updater.step_once() is None
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "idle" and status["heartbeat_at"]

    config.request_path.write_text(json.dumps(request()), encoding="utf-8")
    assert updater.step_once() == "done"
    assert updater.step_once() == "ignored"


def test_find_backup_picks_latest(tmp_path):
    config = make_config(tmp_path)
    first = make_backup(config)
    second = config.backup_dir / f"pre-{TARGET}-20261117T030000Z"
    second.mkdir()
    (second / "fossor.db").write_bytes(b"")
    assert find_backup(config.backup_dir, TARGET) == second
    assert find_backup(config.backup_dir, "2026.11.999") is None
    assert first.is_dir()


def test_docker_runner_only_uses_allowed_commands(tmp_path, monkeypatch):
    """Le compagnon n'exécute que pull / compose / inspect : aucun shell libre."""
    config = make_config(tmp_path, project_dir="/srv/fossor")
    runner = DockerRunner(config)
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: (seen.append(argv), Done())[1])
    runner.pull(f"{IMAGE}:{TARGET}")
    runner.compose_up()
    runner.compose_up(restore="/data/backups/pre-x")
    runner.container_running()

    assert seen[0] == ["docker", "pull", f"{IMAGE}:{TARGET}"]
    assert all(call[0] == "docker" and call[1] in ("pull", "compose", "inspect") for call in seen)
    ups = [call for call in seen if call[1] == "compose" and "up" in call]
    assert len(ups) == 2
    assert ups[0][-1] == "fossor" and "--force-recreate" not in ups[0]
    assert "--force-recreate" in ups[1]
    # le projet compose est celui de l'installation, avec son .env monté
    assert "-p" in ups[0] and "fossor-test" in ups[0]
    assert "--project-directory" in ups[0] and "/srv/fossor" in ups[0]
    assert "--env-file" in ups[0] and str(config.env_path) in ups[0]


def test_host_project_dir_comes_from_compose_label(tmp_path, monkeypatch):
    """Sans chemin fourni, le dossier du projet est lu dans l'étiquette compose."""
    config = make_config(tmp_path)
    runner = DockerRunner(config)

    class Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake(argv, **kwargs):
        if argv[1] == "inspect" and "project.working_dir" in argv[-2]:
            return Done("/home/cpl/fossor\n")
        if argv[1] == "inspect":
            return Done("true\n")
        return Done("deadbeef\n")

    monkeypatch.setattr("subprocess.run", fake)
    assert runner.host_project_dir() == "/home/cpl/fossor"


# ── Correctif 003-c5 : nonce persistant, battement, délais, auto-mise à jour ──
def test_nonce_is_on_disk_and_never_replayed_after_a_restart(tmp_path):
    """Le nonce traité vit dans status.json : un redémarrage ne rejoue rien."""
    config = make_config(tmp_path)
    make_backup(config)
    first = FakeRunner(healthy={TARGET})
    updater = Updater(config, first)
    config.request_path.write_text(json.dumps(request()), encoding="utf-8")
    assert updater.step_once() == "done"
    status = json.loads(config.status_path.read_text())
    assert status["handled_nonce"] == "abc123456789"
    assert status["request_nonce"] == "abc123456789"

    # Redémarrage : même demande toujours sur le disque, nouveau processus.
    restarted_runner = FakeRunner(healthy={TARGET})
    restarted = Updater(config, restarted_runner)
    assert restarted.is_handled("abc123456789")
    assert restarted.step_once() == "ignored"
    assert restarted_runner.pulled == [] and restarted_runner.compose_calls == []


def test_heartbeat_advances_after_done_instead_of_freezing(tmp_path):
    """Régression 003-c1 : après « done », le nonce revenait par handle() sans battement."""
    config = make_config(tmp_path)
    make_backup(config)
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)
    config.request_path.write_text(json.dumps(request()), encoding="utf-8")
    assert updater.step_once() == "done"

    beats = []
    for _ in range(3):
        time.sleep(0.01)
        assert updater.step_once() == "ignored"
        beats.append(json.loads(config.status_path.read_text())["heartbeat_at"])
    assert len(set(beats)) == 3, beats
    assert runner.pulled == [
        f"{IMAGE}:{TARGET}",
        f"{updater.config.updater_image}:{TARGET}",
    ]  # l'app puis le compagnon, une seule fois chacun


def test_heartbeat_thread_beats_during_a_long_docker_call(tmp_path):
    """Un fil dédié écrit le battement pendant un pull qui bloque."""
    config = make_config(tmp_path, heartbeat_interval=0.05)
    make_backup(config)
    release = threading.Event()
    inside: list[str] = []

    class SlowRunner(FakeRunner):
        def pull(self, reference):
            inside.append(json.loads(config.status_path.read_text())["heartbeat_at"])
            release.wait(3.0)  # simule un docker pull long
            inside.append(json.loads(config.status_path.read_text())["heartbeat_at"])
            return True, "pulled"

    runner = SlowRunner(healthy={TARGET})
    updater = Updater(config, runner)
    updater.start_heartbeat()
    worker = threading.Thread(target=updater.handle, args=(request(),))
    try:
        worker.start()
        time.sleep(0.3)
        assert runner.pulled == []  # le pull n'est pas terminé
        during = json.loads(config.status_path.read_text())
        assert during["state"] == "pulling" and during["step"] == "pull"
        first = during["heartbeat_at"]
        time.sleep(0.3)
        second = json.loads(config.status_path.read_text())["heartbeat_at"]
        assert second > first  # le battement avance pendant l'opération
        release.set()
        worker.join(5)
    finally:
        updater.stop_heartbeat()
    assert inside[0] != inside[1]


def test_every_docker_call_has_a_bounded_timeout(tmp_path, monkeypatch):
    config = make_config(tmp_path, project_dir="/srv/fossor")
    runner = DockerRunner(config)
    seen: list[tuple[list[str], int | None]] = []

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kwargs: (seen.append((argv[1:3], kwargs.get("timeout"))), Done())[1],
    )
    runner.pull(f"{IMAGE}:{TARGET}")
    runner.compose_up()
    runner.container_running()
    runner.image_present(f"{IMAGE}:{TARGET}")

    assert seen[0] == (["pull", f"{IMAGE}:{TARGET}"], 300)
    assert seen[1][1] == 120  # docker compose up
    assert [timeout for _, timeout in seen[2:]] == [30, 30, 30, 30][: len(seen) - 2]
    assert all(timeout is not None for _, timeout in seen)


def test_docker_timeout_fails_instead_of_hanging(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    runner = DockerRunner(config)

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 1)

    monkeypatch.setattr("subprocess.run", boom)
    ok, detail = runner.pull(f"{IMAGE}:{TARGET}")
    assert ok is False and "délai dépassé" in detail

    make_backup(config)

    class TimeoutRunner(FakeRunner):
        def pull(self, reference):
            return False, "délai dépassé (300 s)"

    updater = Updater(config, TimeoutRunner(healthy={TARGET}))
    assert updater.handle(request()) == "failed"
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "failed" and "délai dépassé" in status["message"]


def test_self_update_recreates_the_companion_after_a_success(tmp_path):
    config = make_config(tmp_path)
    (config.env_path).write_text(
        config.env_path.read_text(encoding="utf-8") + f"FOSSOR_UPDATER_TAG={OLD}\n",
        encoding="utf-8",
    )
    make_backup(config)
    reference = f"{config.updater_image}:{TARGET}"
    runner = FakeRunner(healthy={TARGET}, images={reference})
    updater = Updater(config, runner)

    assert updater.handle(request()) == "done"
    env = config.env_path.read_text(encoding="utf-8")
    assert read_env_tag(env, "FOSSOR_IMAGE_TAG") == TARGET
    assert read_env_tag(env, "FOSSOR_UPDATER_TAG") == TARGET

    assert len(runner.helpers) == 1
    argv = runner.helpers[0]
    assert argv[:3] == ["docker", "run", "-d"]
    assert argv[argv.index("--entrypoint") + 1] == "docker"
    assert reference in argv
    assert argv[-4:] == ["up", "-d", "--no-deps", "fossor-updater"]
    assert "--project-directory" in argv
    events = [json.loads(line)["event"] for line in config.history_path.read_text().splitlines()]
    assert "self_update" in events
    assert json.loads(config.status_path.read_text())["updater_to"] == TARGET


def test_self_update_failure_keeps_the_current_companion_usable(tmp_path):
    config = make_config(tmp_path)
    (config.env_path).write_text(
        config.env_path.read_text(encoding="utf-8") + "FOSSOR_UPDATER_TAG=2026.11.003-c1\n",
        encoding="utf-8",
    )
    make_backup(config)

    class FailingHelper(FakeRunner):
        def start_helper(self, argv):
            self.helpers.append(argv)
            return False, "amorçage refusé"

    runner = FailingHelper(healthy={TARGET}, images={f"{config.updater_image}:{TARGET}"})
    updater = Updater(config, runner)

    assert updater.handle(request()) == "done"
    # le .env est remis dans l'état d'avant : l'ancien compagnon reste en place
    assert read_env_tag(config.env_path.read_text(encoding="utf-8"), "FOSSOR_UPDATER_TAG") == "2026.11.003-c1"
    events = [json.loads(line)["event"] for line in config.history_path.read_text().splitlines()]
    assert "self_update_failed" in events


def test_self_update_is_skipped_only_when_the_pull_fails(tmp_path):
    config = make_config(tmp_path)
    make_backup(config)

    class FailingUpdaterPull(FakeRunner):
        def pull(self, reference):
            self.pulled.append(reference)
            if "fossor-updater" in reference:
                return False, "registry injoignable"
            return True, "pulled"

    runner = FailingUpdaterPull(healthy={TARGET}, images=set())
    updater = Updater(config, runner)
    assert updater.handle(request()) == "done"
    assert runner.helpers == []
    assert read_env_tag(config.env_path.read_text(encoding="utf-8"), "FOSSOR_UPDATER_TAG") is None
    events = [json.loads(line)["event"] for line in config.history_path.read_text().splitlines()]
    assert "self_update_skipped" in events


def test_interrupted_update_is_reported_and_never_replayed(tmp_path):
    """État actif au démarrage = arrêt pendant l'opération : on le dit, on ne rejoue pas."""
    config = make_config(tmp_path)
    make_backup(config)
    config.status_path.write_text(
        json.dumps({"state": "switching", "step": "switch", "from": OLD, "to": TARGET,
                    "request_nonce": "abc123456789", "handled_nonce": "abc123456789"}),
        encoding="utf-8",
    )
    config.request_path.write_text(json.dumps(request()), encoding="utf-8")
    runner = FakeRunner(healthy={TARGET})
    updater = Updater(config, runner)

    updater.recover()
    status = json.loads(config.status_path.read_text())
    assert status["state"] == "failed" and status["step"] == "recover"
    assert "interrompue" in status["message"]

    assert updater.step_once() == "ignored"
    assert runner.pulled == [] and runner.compose_calls == []


def test_logs_go_to_stdout(tmp_path, capsys):
    """Chaque étape écrit sur stdout : ``docker logs`` doit être utile."""
    config = make_config(tmp_path)
    make_backup(config)
    updater = Updater(config, FakeRunner(healthy={TARGET}))
    assert updater.handle(request()) == "done"
    out = capsys.readouterr().out
    for marker in ("demande", "pull", "bascule", "done", "fin"):
        assert marker in out, marker

