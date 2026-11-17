"""``fossor-updater`` — le seul conteneur qui voit le socket Docker (§4.18).

L'application Fossor n'a jamais le socket : elle dépose une demande dans
``/data/update/request.json`` et lit ``status.json``.  Ce compagnon scrute ce
fichier toutes les 10 s et n'exécute **que** quatre choses :

1. ``docker pull <image>:<tag>`` (sauf ``FOSSOR_UPDATER_LOCAL_IMAGES=1``, le
   mode des tests d'intégration où les images sont construites localement) ;
2. la réécriture de ``FOSSOR_IMAGE_TAG`` dans le ``.env`` du compose ;
3. ``docker compose up -d fossor`` (recréation du seul service applicatif) ;
4. la lecture de l'état — ``GET http://fossor:8008/api/version``.

Tout le reste est refusé et journalisé : tag hors ``^20\\d\\d\\.\\d\\d\\.\\d{3}(-c\\d+)?$``,
image différente de celle configurée, demande sans sauvegarde préalable.

En cas d'échec du contrôle de santé : retour à l'ancien tag, et si la version
fautive a fait migrer le schéma de la base, la recréation passe
``FOSSOR_RESTORE_BACKUP=<sauvegarde>`` à l'application, qui restaure elle-même
sa base au démarrage (le compagnon, lui, monte ``/data`` en lecture seule).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TAG_RE = re.compile(r"^20\d\d\.\d\d\.\d{3}(-c\d+)?$")
STATES = (
    "idle", "pulling", "backing_up", "switching", "health_check",
    "done", "rolled_back", "failed",
)
UPDATER_VERSION = (os.environ.get("FOSSOR_UPDATER_VERSION") or "1.0.0").strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Config:
    update_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_DIR", "/data/update")))
    compose_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_COMPOSE_DIR", "/compose")))
    compose_file: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_COMPOSE_FILE", "docker-compose.yml"))
    project: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_PROJECT", "fossor"))
    service: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_SERVICE", "fossor"))
    image: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_EXPECTED_IMAGE", "ghcr.io/lostinthebugs/fossor"))
    backup_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_BACKUP_DIR", "/data/backups")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_DATA_DIR", "/data")))
    health_url: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_HEALTH_URL", "http://fossor:8008/api/version"))
    health_timeout: int = field(default_factory=lambda: _env_int("FOSSOR_UPDATER_HEALTH_TIMEOUT", 120))
    poll: int = field(default_factory=lambda: _env_int("FOSSOR_UPDATER_POLL", 10))
    health_poll: float = field(default_factory=lambda: float(_env_int("FOSSOR_UPDATER_HEALTH_POLL", 2)))
    local_images: bool = field(default_factory=lambda: _env_bool("FOSSOR_UPDATER_LOCAL_IMAGES", False))
    project_dir: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_PROJECT_DIR", ""))
    tag_key: str = "FOSSOR_IMAGE_TAG"

    @property
    def env_path(self) -> Path:
        return self.compose_dir / ".env"

    @property
    def status_path(self) -> Path:
        return self.update_dir / "status.json"

    @property
    def request_path(self) -> Path:
        return self.update_dir / "request.json"

    @property
    def history_path(self) -> Path:
        return self.update_dir / "history.jsonl"


# ── Fonctions pures ────────────────────────────────────────────────────
def valid_tag(value) -> bool:
    return isinstance(value, str) and bool(TAG_RE.match(value.strip()))


def validate_request(request, expected_image: str) -> tuple[bool, str]:
    """Accepte uniquement ``{target, image, requested_at, nonce}`` cohérents."""
    if not isinstance(request, dict):
        return False, "demande illisible (objet JSON attendu)"
    target = request.get("target")
    if not valid_tag(target):
        return False, f"tag refusé : {target!r} (motif attendu ANNEE.MOIS.NNN[-cN])"
    image = request.get("image")
    if image != expected_image:
        return False, f"image refusée : {image!r} (configurée : {expected_image!r})"
    nonce = request.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 8:
        return False, "nonce manquant : demande refusée"
    return True, ""


def read_env_tag(text: str, key: str = "FOSSOR_IMAGE_TAG") -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def set_env_tag(text: str, tag: str, key: str = "FOSSOR_IMAGE_TAG") -> str:
    """Réécrit **une seule** ligne du ``.env``, le reste est conservé tel quel."""
    if "\n" in tag or "\r" in tag:  # jamais de nouvelle ligne dans un .env
        raise ValueError("tag invalide")
    lines = text.splitlines()
    found = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name == key:
            lines[index] = f"{key}={tag}"
            found = True
            break
    if not found:
        lines.append(f"{key}={tag}")
    return "\n".join(lines) + "\n"


def schema_signature(database: Path) -> str | None:
    """Empreinte du schéma ; ``None`` si la base est illisible."""
    if not database.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select type, name, sql from sqlite_master "
                "where name not like 'sqlite_%' order by name"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def find_backup(backup_dir: Path, target: str) -> Path | None:
    """Dernière sauvegarde ``pre-<cible>-<horodatage>`` contenant une base."""
    if not backup_dir.is_dir():
        return None
    candidates = sorted(
        (path for path in backup_dir.iterdir()
         if path.is_dir() and path.name.startswith(f"pre-{target}-") and (path / "fossor.db").is_file()),
        key=lambda path: path.name,
    )
    return candidates[-1] if candidates else None


def schema_migrated(live: Path, backup: Path) -> bool:
    """La version fautive a-t-elle touché au schéma ?  (base illisible = oui)"""
    live_sig = schema_signature(live)
    backup_sig = schema_signature(backup)
    if backup_sig is None:
        return False
    if live_sig is None:
        return True
    return live_sig != backup_sig


# ── Exécution ──────────────────────────────────────────────────────────
class DockerRunner:
    """Les quatre commandes autorisées, injectables pour les tests.

    ``docker compose`` tourne *dans* ce conteneur mais parle au démon de
    l'hôte : les chemins relatifs du compose doivent donc être résolus vers le
    dossier du projet **sur l'hôte**.  Ce dossier est lu dans l'étiquette
    ``com.docker.compose.project.working_dir`` du conteneur applicatif (ou
    fourni par ``FOSSOR_UPDATER_PROJECT_DIR``) ; le compose et le ``.env`` sont
    lus depuis le montage local ``/compose``.
    """

    def __init__(self, config: Config):
        self.config = config

    def _run(self, argv: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(argv, capture_output=True, text=True, timeout=900, env=env)

    def _compose(self, *args: str) -> list[str]:
        return [
            "docker", "compose",
            "-p", self.config.project,
            "--project-directory", self.host_project_dir(),
            "--env-file", str(self.config.env_path),
            "-f", str(self.config.compose_dir / self.config.compose_file),
            *args,
        ]

    def host_project_dir(self) -> str:
        """Chemin hôte du projet compose (les montages relatifs en dépendent)."""
        if self.config.project_dir:
            return self.config.project_dir
        # Pas de `_compose()` ici : elle résout justement le dossier du projet.
        listed = self._run([
            "docker", "compose",
            "-p", self.config.project,
            "--project-directory", str(self.config.compose_dir),
            "--env-file", str(self.config.env_path),
            "-f", str(self.config.compose_dir / self.config.compose_file),
            "ps", "-q", self.config.service,
        ])
        containers = (listed.stdout or "").strip().splitlines()
        if not containers:
            return str(self.config.compose_dir)
        inspected = self._run([
            "docker", "inspect", "-f",
            '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}',
            containers[0],
        ])
        value = (inspected.stdout or "").strip()
        if not value or value.startswith("<"):
            return str(self.config.compose_dir)
        return value

    def pull(self, reference: str) -> tuple[bool, str]:
        done = self._run(["docker", "pull", reference])
        detail = (done.stdout or done.stderr or "").strip().splitlines()
        return done.returncode == 0, (detail[-1] if detail else "")

    def image_present(self, reference: str) -> bool:
        return self._run(["docker", "image", "inspect", reference]).returncode == 0

    def compose_up(self, restore: str | None = None) -> tuple[bool, str]:
        argv = self._compose("up", "-d")
        if restore:
            argv.append("--force-recreate")
        argv.append(self.config.service)
        env_extra = {"FOSSOR_RESTORE_BACKUP": restore} if restore else None
        done = self._run(argv, env_extra=env_extra)
        output = (done.stdout or "") + (done.stderr or "")
        return done.returncode == 0, output.strip().splitlines()[-1] if output.strip() else ""

    def container_running(self) -> bool:
        ids = self._run(self._compose("ps", "-q", self.config.service))
        containers = (ids.stdout or "").strip().splitlines()
        if not containers:
            return False
        state = self._run(["docker", "inspect", "-f", "{{.State.Running}}", containers[0]])
        return (state.stdout or "").strip() == "true"

    def health_once(self, expected: str) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(self.config.health_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        version = str((payload or {}).get("version") or "").strip()
        if version == expected:
            return True, version
        return False, f"version annoncée {version!r} au lieu de {expected!r}"


class Updater:
    def __init__(self, config: Config | None = None, runner=None):
        self.config = config or Config()
        self.runner = runner or DockerRunner(self.config)
        self.status: dict = {
            "state": "idle",
            "from": None,
            "to": None,
            "step": "",
            "message": "En attente",
            "started_at": None,
            "updated_at": now(),
            "heartbeat_at": now(),
            "request_nonce": None,
            "updater_version": UPDATER_VERSION,
        }
        self._handled_nonce: str | None = None
        self._load()

    # ── Fichiers ───────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            payload = json.loads(self.config.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(payload, dict):
            self.status.update({key: value for key, value in payload.items() if key in self.status})
            self._handled_nonce = payload.get("request_nonce")

    def _write_status(self) -> None:
        self.status["heartbeat_at"] = now()
        path = self.config.status_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _history(self, event: str, **extra) -> None:
        line = {"at": now(), "event": event, **extra}
        self.config.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def heartbeat(self) -> None:
        """Battement de cœur sans changer d'état (preuve de vie pour l'UI)."""
        self._write_status()

    def _transition(self, state: str, step: str, message: str, **extra) -> None:
        assert state in STATES, state
        self.status.update({"state": state, "step": step, "message": message,
                            "updated_at": now(), **extra})
        self._write_status()
        self._history(state, step=step, message=message,
                      **{key: value for key, value in extra.items() if key in ("from", "to")})

    # ── Demande ────────────────────────────────────────────────────────
    def read_request(self) -> dict | None:
        try:
            payload = json.loads(self.config.request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _reject(self, request, reason: str) -> str:
        nonce = (request or {}).get("nonce") if isinstance(request, dict) else None
        self._handled_nonce = nonce
        self.status.update({"state": "failed", "step": "refus", "message": reason,
                            "request_nonce": nonce, "refused": True,
                            "to": None, "updated_at": now()})
        self._write_status()
        self._history("refused", reason=reason, request=request, nonce=nonce)
        target = (request or {}).get("target") if isinstance(request, dict) else None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rejected = self.config.update_dir / f"request.refused-{stamp}.json"
        try:
            shutil.move(str(self.config.request_path), str(rejected))
        except OSError:
            pass
        return "failed"

    def handle(self, request: dict) -> str:
        ok, reason = validate_request(request, self.config.image)
        if not ok:
            return self._reject(request, reason)
        nonce = request["nonce"]
        if self._handled_nonce == nonce or self.status.get("request_nonce") == nonce:
            return "ignored"

        target = request["target"].strip()
        image = self.config.image
        reference = f"{image}:{target}"
        old_text = ""
        try:
            old_text = self.config.env_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""
        previous = read_env_tag(old_text, self.config.tag_key) or "latest"

        self.status.update({"from": previous, "to": target, "request_nonce": nonce,
                            "started_at": now(), "refused": False})
        self._history("request", request=request)
        self._transition("pulling", "pull", f"récupération de {reference}")
        if self.config.local_images:
            self._history("pull_skipped", reference=reference,
                          reason="FOSSOR_UPDATER_LOCAL_IMAGES=1")
            if not self.runner.image_present(reference):
                self._transition("failed", "pull", f"image locale absente : {reference}")
                return "failed"
        else:
            pulled, detail = self.runner.pull(reference)
            if not pulled:
                self._transition("failed", "pull", f"docker pull a échoué : {detail}")
                return "failed"

        self._transition("backing_up", "backup", "vérification de la sauvegarde préalable")
        backup = find_backup(self.config.backup_dir, target)
        if backup is None:
            self._transition("failed", "backup",
                             f"aucune sauvegarde pre-{target}-* trouvée : bascule refusée")
            return "failed"

        self._transition("switching", "switch", f"bascule de {previous} vers {target}")
        try:
            new_text = set_env_tag(old_text, target, self.config.tag_key)
            tmp = self.config.env_path.with_suffix(".env.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, self.config.env_path)
        except OSError as exc:
            self._transition("failed", "switch", f"écriture du .env impossible : {exc}")
            return "failed"
        up, detail = self.runner.compose_up()
        if not up:
            return self._rollback(previous, old_text, backup, f"recréation impossible : {detail}")

        self._transition("health_check", "health", f"attente de {target} sur /api/version")
        healthy, detail = self.wait_healthy(target)
        if healthy:
            self._transition("done", "health", f"{target} répond ({detail})")
            return "done"
        return self._rollback(previous, old_text, backup, f"contrôle de santé en échec : {detail}")

    def wait_healthy(self, expected: str) -> tuple[bool, str]:
        deadline = time.monotonic() + max(1, self.config.health_timeout)
        stopped = 0
        detail = "pas de réponse"
        while time.monotonic() < deadline:
            ok, detail = self.runner.health_once(expected)
            if ok:
                return True, detail
            if self.runner.container_running():
                stopped = 0
            else:
                stopped += 1
                if stopped >= 3:
                    return False, f"conteneur arrêté ({detail})"
            time.sleep(max(0.0, self.config.health_poll))
        return False, detail

    def _rollback(self, previous: str, old_text: str, backup: Path, reason: str) -> str:
        self._transition("switching", "rollback", f"{reason} → retour à {previous}")
        try:
            restored = set_env_tag(old_text, previous, self.config.tag_key)
            tmp = self.config.env_path.with_suffix(".env.tmp")
            tmp.write_text(restored, encoding="utf-8")
            os.replace(tmp, self.config.env_path)
        except OSError as exc:
            self._transition("failed", "rollback", f"écriture du .env impossible : {exc}")
            return "failed"

        live = self.config.data_dir / "fossor.db"
        restore = None
        if schema_migrated(live, backup / "fossor.db"):
            restore = str(backup)
            self._history("restore", backup=restore, reason="schéma migré")
        up, detail = self.runner.compose_up(restore=restore)
        if not up:
            self._transition("failed", "rollback", f"recréation impossible : {detail}")
            return "failed"
        self._transition("health_check", "rollback", f"attente de {previous} sur /api/version")
        healthy, detail = self.wait_healthy(previous)
        if healthy:
            self._transition("rolled_back", "rollback",
                             f"retour à {previous} vérifié ({detail})", restore=bool(restore))
            return "rolled_back"
        self._transition("failed", "rollback", f"{previous} ne répond pas ({detail})")
        return "failed"

    # ── Boucle ─────────────────────────────────────────────────────────
    def step_once(self) -> str | None:
        request = self.read_request()
        if request is None:
            self.heartbeat()
            return None
        if self._handled_nonce and request.get("nonce") == self._handled_nonce:
            self.heartbeat()
            return "ignored"
        return self.handle(request)

    def run_forever(self) -> None:  # pragma: no cover - boucle infinie
        self.status.setdefault("state", "idle")
        self._write_status()
        while True:
            try:
                self.step_once()
            except Exception as exc:  # la boucle ne meurt jamais
                self.status.update({"state": "failed", "step": "loop",
                                    "message": f"{type(exc).__name__}: {exc}",
                                    "updated_at": now()})
                self._write_status()
            time.sleep(max(2, self.config.poll))


def main() -> None:  # pragma: no cover - point d'entrée
    Updater().run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
