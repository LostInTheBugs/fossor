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

Trois garanties ajoutées par le correctif ``-c5`` :

* **nonce mémorisé sur disque** (``status.json`` : ``handled_nonce``) : une
  demande traitée ne repart jamais, même après un redémarrage du compagnon ;
* **battement indépendant** : un fil dédié réécrit ``heartbeat_at`` toutes les
  ``FOSSOR_UPDATER_HEARTBEAT`` secondes, y compris pendant un ``docker pull``
  long, et chaque appel Docker a son propre délai maximal ;
* **auto-mise à jour** : après une bascule réussie, si l'image
  ``fossor-updater:<cible>`` existe, le tag est écrit dans le ``.env`` et un
  conteneur d'amorçage détaché recrée le service ``fossor-updater`` — l'ancien
  compagnon reste en place si cette étape échoue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket as socket_module
import sqlite3
import subprocess
import threading
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
#: États pendant lesquels une mise à jour est en cours.
ACTIVE_STATES = ("pulling", "backing_up", "switching", "health_check")

#: Clé du `.env` réécrite pour l'application, puis pour le compagnon.
TAG_KEY = "FOSSOR_IMAGE_TAG"
UPDATER_TAG_KEY = "FOSSOR_UPDATER_TAG"

DEFAULT_IMAGE = "ghcr.io/lostinthebugs/fossor"
DEFAULT_UPDATER_IMAGE = "ghcr.io/lostinthebugs/fossor-updater"

#: Délais maximaux des appels Docker : un démon muet ne fige plus la boucle.
PULL_TIMEOUT = 300
UP_TIMEOUT = 120
INSPECT_TIMEOUT = 30

#: Battement de cœur par défaut (secondes).
HEARTBEAT_INTERVAL = 10


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, message: str) -> None:
    """Trace sur la sortie standard — c'est ce que ``docker logs`` montre."""
    print(f"{now()} {event} — {message}", flush=True)


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


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _resolve_version() -> str:
    """Version rapportée du compagnon : variable d'environnement connue."""
    for name in ("FOSSOR_UPDATER_VERSION", "FOSSOR_UPDATER_TAG"):
        value = (os.environ.get(name) or "").strip()
        if value and value != "latest":
            return value
    return "1.0.0"


UPDATER_VERSION = _resolve_version()


@dataclass
class Config:
    update_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_DIR", "/data/update")))
    compose_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_COMPOSE_DIR", "/compose")))
    compose_file: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_COMPOSE_FILE", "docker-compose.yml"))
    project: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_PROJECT", "fossor"))
    service: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_SERVICE", "fossor"))
    image: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_EXPECTED_IMAGE", DEFAULT_IMAGE))
    backup_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_BACKUP_DIR", "/data/backups")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOSSOR_UPDATER_DATA_DIR", "/data")))
    health_url: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_HEALTH_URL", "http://fossor:8008/api/version"))
    health_timeout: int = field(default_factory=lambda: _env_int("FOSSOR_UPDATER_HEALTH_TIMEOUT", 120))
    poll: int = field(default_factory=lambda: _env_int("FOSSOR_UPDATER_POLL", 10))
    health_poll: float = field(default_factory=lambda: _env_float("FOSSOR_UPDATER_HEALTH_POLL", 2.0))
    heartbeat_interval: float = field(default_factory=lambda: _env_float("FOSSOR_UPDATER_HEARTBEAT", HEARTBEAT_INTERVAL))
    local_images: bool = field(default_factory=lambda: _env_bool("FOSSOR_UPDATER_LOCAL_IMAGES", False))
    project_dir: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_PROJECT_DIR", ""))
    #: Recréation du compagnon lui-même après une bascule réussie.
    self_update: bool = field(default_factory=lambda: _env_bool("FOSSOR_UPDATER_SELF_UPDATE", True))
    updater_service: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_SELF_SERVICE", "fossor-updater"))
    updater_image: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_IMAGE", DEFAULT_UPDATER_IMAGE))
    docker_socket: str = field(default_factory=lambda: os.environ.get("FOSSOR_UPDATER_DOCKER_SOCKET", "/var/run/docker.sock"))
    tag_key: str = TAG_KEY
    updater_tag_key: str = UPDATER_TAG_KEY

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


def read_env_tag(text: str, key: str = TAG_KEY) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def set_env_tag(text: str, tag: str, key: str = TAG_KEY) -> str:
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
    """Les commandes autorisées, injectables pour les tests.

    ``docker compose`` tourne *dans* ce conteneur mais parle au démon de
    l'hôte : les chemins relatifs du compose doivent donc être résolus vers le
    dossier du projet **sur l'hôte**.  Ce dossier est lu dans l'étiquette
    ``com.docker.compose.project.working_dir`` du conteneur applicatif (ou
    fourni par ``FOSSOR_UPDATER_PROJECT_DIR``) ; le compose et le ``.env`` sont
    lus depuis le montage local ``/compose``.

    Chaque appel porte un délai maximal (``PULL_TIMEOUT`` 300 s,
    ``UP_TIMEOUT`` 120 s, ``INSPECT_TIMEOUT`` 30 s) : un démon Docker muet
    rend la main au lieu de figer la boucle pour toujours.
    """

    def __init__(self, config: Config):
        self.config = config

    def _run(self, argv: list[str], env_extra: dict | None = None,
             timeout: int = INSPECT_TIMEOUT) -> subprocess.CompletedProcess:
        env = {**os.environ, **(env_extra or {})}
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            message = f"délai dépassé ({timeout} s) : {' '.join(argv[:3])}…"
            log("timeout", message)
            return subprocess.CompletedProcess(argv, 124, "", message)
        except OSError as exc:  # docker absent, socket inaccessible
            message = f"{type(exc).__name__}: {exc}"
            log("erreur", f"{' '.join(argv[:3])}… : {message}")
            return subprocess.CompletedProcess(argv, 127, "", message)

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
        done = self._run(["docker", "pull", reference], timeout=PULL_TIMEOUT)
        detail = (done.stdout or done.stderr or "").strip().splitlines()
        return done.returncode == 0, (detail[-1] if detail else "")

    def image_present(self, reference: str) -> bool:
        return self._run(["docker", "image", "inspect", reference], timeout=INSPECT_TIMEOUT).returncode == 0

    def image_tag(self, reference: str) -> str | None:
        """Tag d'une référence ``repo:tag`` (``None`` si absent)."""
        done = self._run([
            "docker", "image", "inspect", "-f", "{{index .RepoTags 0}}", reference,
        ], timeout=INSPECT_TIMEOUT)
        tag = (done.stdout or "").strip()
        return tag or None

    def remove_image(self, reference: str) -> tuple[bool, str]:
        """Supprime une image locale (« oublier la version précédente »)."""
        done = self._run(["docker", "rmi", reference], timeout=PULL_TIMEOUT)
        detail = (done.stdout or done.stderr or "").strip().splitlines()
        text = detail[-1] if detail else ""
        if done.returncode == 0 or "No such image" in text:
            return True, text or "image absente"
        return False, text or "docker rmi a échoué"

    def compose_up(self, restore: str | None = None) -> tuple[bool, str]:
        argv = self._compose("up", "-d")
        if restore:
            argv.append("--force-recreate")
        argv.append(self.config.service)
        env_extra = {"FOSSOR_RESTORE_BACKUP": restore} if restore else None
        done = self._run(argv, env_extra=env_extra, timeout=UP_TIMEOUT)
        output = (done.stdout or "") + (done.stderr or "")
        return done.returncode == 0, output.strip().splitlines()[-1] if output.strip() else ""

    def container_running(self) -> bool:
        ids = self._run(self._compose("ps", "-q", self.config.service), timeout=INSPECT_TIMEOUT)
        containers = (ids.stdout or "").strip().splitlines()
        if not containers:
            return False
        state = self._run(["docker", "inspect", "-f", "{{.State.Running}}", containers[0]],
                          timeout=INSPECT_TIMEOUT)
        return (state.stdout or "").strip() == "true"

    def self_image(self) -> str | None:
        """Image de ce conteneur (lue au démon), pour rapporter sa version."""
        inspected = self._run([
            "docker", "inspect", "-f", "{{.Config.Image}}", socket_module.gethostname(),
        ], timeout=INSPECT_TIMEOUT)
        value = (inspected.stdout or "").strip()
        return value or None

    def start_helper(self, argv: list[str]) -> tuple[bool, str]:
        """Lance un conteneur détaché qui survit à l'arrêt de celui-ci."""
        done = self._run(argv, timeout=INSPECT_TIMEOUT)
        detail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()
        return done.returncode == 0, (detail[-1] if detail else "")

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
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
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
            "handled_nonce": None,
            "updater_version": UPDATER_VERSION,
            "updater_to": None,
        }
        self._handled_nonce: str | None = None
        self._load()
        self.status["updater_version"] = self._detect_version()

    # ── Version du compagnon ───────────────────────────────────────────
    def _detect_version(self) -> str:
        """Tag de l'image du compagnon (env, sinon inspect du démon)."""
        if UPDATER_VERSION != "1.0.0":
            return UPDATER_VERSION
        probe = getattr(self.runner, "self_image", None)
        if callable(probe):
            try:
                reference = probe()
            except Exception:  # noqa: BLE001 - jamais bloquant pour l'updater
                reference = None
            if reference:
                tag = reference.rsplit(":", 1)[-1].strip()
                if tag and tag != "latest":
                    return tag
        return str(self.status.get("updater_version") or UPDATER_VERSION)

    # ── Fichiers ───────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            payload = json.loads(self.config.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(payload, dict):
            self.status.update({key: value for key, value in payload.items() if key in self.status})
            self._handled_nonce = payload.get("handled_nonce") or payload.get("request_nonce")

    def _write_status(self) -> None:
        with self._lock:
            self.status["heartbeat_at"] = now()
            path = self.config.status_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.status, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def _history(self, event: str, **extra) -> None:
        line = {"at": now(), "event": event, **extra}
        self.config.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.config.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def heartbeat(self) -> None:
        """Battement de cœur sans changer d'état (preuve de vie pour l'UI)."""
        self._write_status()

    def start_heartbeat(self) -> threading.Thread:
        """Fil indépendant : le battement continue pendant une opération longue."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        interval = max(0.05, float(self.config.heartbeat_interval))

        def loop() -> None:  # pragma: no cover - fil de fond, testé par son effet
            while not self._stop.wait(interval):
                try:
                    self.heartbeat()
                except Exception as exc:  # noqa: BLE001 - le fil ne meurt jamais
                    log("erreur", f"battement impossible : {type(exc).__name__}: {exc}")

        self._thread = threading.Thread(target=loop, name="fossor-updater-heartbeat", daemon=True)
        self._thread.start()
        return self._thread

    def stop_heartbeat(self) -> None:
        self._stop.set()

    def _transition(self, state: str, step: str, message: str, **extra) -> None:
        assert state in STATES, state
        self.status.update({"state": state, "step": step, "message": message,
                            "updated_at": now(), **extra})
        self._write_status()
        self._history(state, step=step, message=message,
                      **{key: value for key, value in extra.items() if key in ("from", "to")})
        log(state, f"[{step}] {message}")

    # ── Nonce traité, mémorisé sur disque ──────────────────────────────
    def is_handled(self, nonce) -> bool:
        """Cette demande a-t-elle déjà été prise en charge ?"""
        if not isinstance(nonce, str) or not nonce:
            return False
        return nonce in {
            self._handled_nonce,
            self.status.get("handled_nonce"),
            self.status.get("request_nonce"),
        }

    def _remember_nonce(self, nonce: str) -> None:
        """Écrit le nonce **avant** tout travail : un redémarrage ne rejoue rien."""
        self._handled_nonce = nonce
        self.status.update({"handled_nonce": nonce, "request_nonce": nonce})
        self._write_status()

    # ── Demande ────────────────────────────────────────────────────────
    def read_request(self) -> dict | None:
        try:
            payload = json.loads(self.config.request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _reject(self, request, reason: str) -> str:
        nonce = (request or {}).get("nonce") if isinstance(request, dict) else None
        if isinstance(nonce, str) and nonce:
            self._handled_nonce = nonce
        self.status.update({"state": "failed", "step": "refus", "message": reason,
                            "request_nonce": nonce, "handled_nonce": nonce,
                            "refused": True, "to": None, "updated_at": now()})
        self._write_status()
        self._history("refused", reason=reason, request=request, nonce=nonce)
        log("refus", reason)
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
        if self.is_handled(nonce):
            # Demande déjà traitée : on garde le battement, on ne rejoue rien.
            self.heartbeat()
            log("ignoré", f"demande {nonce[:8]}… déjà traitée")
            return "ignored"

        action = str(request.get("action") or "update")
        if action in ("rollback", "forget_previous"):
            self._remember_nonce(request["nonce"])
            self._history("request", request=request)
            if action == "rollback":
                return self._manual_rollback(request)
            return self._forget_previous(request)

        target = request["target"].strip()
        image = self.config.image
        reference = f"{image}:{target}"
        old_text = ""
        try:
            old_text = self.config.env_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""
        previous = read_env_tag(old_text, self.config.tag_key) or "latest"

        self._remember_nonce(nonce)
        self.status.update({"from": previous, "to": target, "started_at": now(), "refused": False})
        self._history("request", request=request)
        log("demande", f"cible {target} (installée : {previous}), nonce {nonce[:8]}…")
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
            log("pull", f"{reference} récupérée ({detail})")

        self._transition("backing_up", "backup", "vérification de la sauvegarde préalable")
        backup = find_backup(self.config.backup_dir, target)
        if backup is None:
            self._transition("failed", "backup",
                             f"aucune sauvegarde pre-{target}-* trouvée : bascule refusée")
            return "failed"
        log("backup", f"sauvegarde {backup}")

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
        log("bascule", f"service {self.config.service} recréé sur {target} ({detail})")

        self._transition("health_check", "health", f"attente de {target} sur /api/version")
        healthy, detail = self.wait_healthy(target)
        if healthy:
            self._record_previous(previous, image)
            self._transition("done", "health", f"{target} répond ({detail})")
            self._self_update(target)
            self._history("finished", target=target)
            log("fin", f"mise à jour {previous} → {target} terminée")
            return "done"
        return self._rollback(previous, old_text, backup, f"contrôle de santé en échec : {detail}")

    # ── Auto-mise à jour du compagnon ──────────────────────────────────
    def self_update_argv(self, reference: str) -> list[str]:
        """Commande d'amorçage : un conteneur détaché recrée ``fossor-updater``.

        Le lancement se fait depuis un conteneur **frère** de l'image cible :
        si la recréation arrêtait ce processus, l'amorçage continuerait et le
        service serait recréé avec le nouveau tag (et remis en place par
        ``restart: unless-stopped`` sinon).
        """
        host_dir = str(self.runner.host_project_dir()) if hasattr(self.runner, "host_project_dir") else str(self.config.compose_dir)
        argv = [
            "docker", "run", "-d", "--rm",
            "-v", f"{self.config.docker_socket}:{self.config.docker_socket}",
            "-v", f"{host_dir}:{self.config.compose_dir}",
            "--entrypoint", "docker",
        ]
        try:
            gid = os.stat(self.config.docker_socket).st_gid
        except OSError:
            gid = None
        if gid is not None:
            argv += ["--group-add", str(gid)]
        argv += [
            reference,
            "compose", "-p", self.config.project,
            "--project-directory", host_dir,
            "--env-file", str(self.config.env_path),
            "-f", str(self.config.compose_dir / self.config.compose_file),
            "up", "-d", "--no-deps", self.config.updater_service,
        ]
        return argv

    def _self_update(self, target: str) -> None:
        """Bascule le compagnon sur ``target`` si son image existe déjà."""
        if not self.config.self_update:
            log("auto-maj", "désactivée (FOSSOR_UPDATER_SELF_UPDATE=0)")
            return
        reference = f"{self.config.updater_image}:{target}"
        # 2026.11.003-c8 : le compagnon **tire** son image cible.  L'ancien code
        # ne vérifiait que sa présence locale, donc une image publiée par le CI
        # n'était jamais récupérée : le compagnon restait en arrière.
        puller = getattr(self.runner, "pull", None)
        pulled = False
        detail = "runner sans pull"
        if self.config.local_images:
            pulled, detail = True, "image locale (FOSSOR_UPDATER_LOCAL_IMAGES=1)"
        elif callable(puller):
            log("auto-maj", f"récupération du compagnon {reference}")
            result = puller(reference)
            if isinstance(result, tuple) and len(result) == 2:
                pulled, detail = bool(result[0]), str(result[1])
        if not pulled:
            message = (
                f"pull de {reference} en échec ({detail}) : "
                "compagnon laissé en place"
            )
            log("auto-maj", message)
            self._history("self_update_skipped", reference=reference, reason=message)
            return
        log("auto-maj", f"image {reference} récupérée ({detail})")
        # Trace durable : le rapport de mise à jour doit prouver que le
        # compagnon a bien tiré son image, pas seulement qu'il l'a vue.
        self._history("self_pull", reference=reference, detail=detail)
        try:
            text = self.config.env_path.read_text(encoding="utf-8")
        except OSError as exc:
            log("auto-maj", f"lecture du .env impossible : {exc}")
            self._history("self_update_failed", reference=reference, reason=str(exc))
            return
        previous = read_env_tag(text, self.config.updater_tag_key) or "latest"
        try:
            updated = set_env_tag(text, target, self.config.updater_tag_key)
            tmp = self.config.env_path.with_suffix(".env.tmp")
            tmp.write_text(updated, encoding="utf-8")
            os.replace(tmp, self.config.env_path)
        except (OSError, ValueError) as exc:
            log("auto-maj", f"écriture de {self.config.updater_tag_key} impossible : {exc}")
            self._history("self_update_failed", reference=reference, reason=str(exc))
            return

        argv = self.self_update_argv(reference)
        launch = getattr(self.runner, "start_helper", None)
        if not callable(launch):
            log("auto-maj", "runner sans amorçage détaché : compagnon laissé en place")
            self._history("self_update_skipped", reference=reference, reason="runner sans amorçage")
            return
        ok, detail = launch(argv)
        if not ok:
            # L'ancien compagnon doit rester utilisable : on remet le tag d'avant.
            try:
                tmp = self.config.env_path.with_suffix(".env.tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.config.env_path)
            except OSError:
                pass
            log("auto-maj", f"amorçage refusé ({detail}) : {previous} conservé")
            self._history("self_update_failed", reference=reference, reason=detail)
            return
        self.status["updater_to"] = target
        self._write_status()
        self._history("self_update", reference=reference, **{"from": previous, "to": target})
        log("auto-maj", f"compagnon en cours de bascule {previous} → {target} ({detail})")

    def _record_previous(self, previous: str, image: str) -> None:
        """Garde la version qu'on vient de quitter (retour arrière manuel possible)."""
        if not previous or previous in ("latest", "") or previous == self.status.get("to"):
            return
        self.status["precedente"] = {
            "version": previous,
            "image": self.runner.image_present(f"{image}:{previous}"),
        }

    def _manual_rollback(self, request: dict) -> str:
        """Retour manuel vers l'image conservée, avec la sauvegarde de base d'alors.

        La sauvegarde restaurée est celle prise **avant** l'installation de la
        version qu'on quitte (``pre-<installée>-*``) : c'est l'état d'alors.
        """
        previous = self.status.get("precedente") or {}
        target = str(request.get("target") or "").strip()
        if not previous or previous.get("version") != target:
            return self._reject(request, "aucune version précédente conservée ne correspond à la cible")
        reference = f"{self.config.image}:{target}"
        if not self.runner.image_present(reference):
            return self._reject(request, f"image conservée absente : {reference}")

        old_text = ""
        try:
            old_text = self.config.env_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""
        current = read_env_tag(old_text, self.config.tag_key) or "latest"
        backup = find_backup(self.config.backup_dir, current)
        if backup is None:
            self._transition("failed", "rollback",
                             f"aucune sauvegarde pre-{current}-* : retour refusé")
            return "failed"

        self.status.update({"from": current, "to": target, "started_at": now(), "refused": False})
        self._transition("switching", "rollback", f"retour manuel de {current} vers {target}")
        try:
            new_text = set_env_tag(old_text, target, self.config.tag_key)
            tmp = self.config.env_path.with_suffix(".env.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, self.config.env_path)
        except OSError as exc:
            self._transition("failed", "rollback", f"écriture du .env impossible : {exc}")
            return "failed"

        up, detail = self.runner.compose_up(restore=str(backup))
        if not up:
            self._transition("failed", "rollback", f"recréation impossible : {detail}")
            return "failed"
        self._transition("health_check", "rollback", f"attente de {target} sur /api/version")
        healthy, detail = self.wait_healthy(target)
        if not healthy:
            self._transition("failed", "rollback", f"{target} ne répond pas ({detail})")
            return "failed"
        self.status["precedente"] = {
            "version": current,
            "image": self.runner.image_present(f"{self.config.image}:{current}"),
        }
        self._transition("rolled_back", "rollback",
                         f"retour manuel à {target} vérifié ({detail}), base restaurée", restore=True)
        self._history("finished", target=target, action="rollback")
        log("retour", f"{current} → {target} terminé")
        return "rolled_back"

    def _forget_previous(self, request: dict) -> str:
        """Supprime l'image conservée de la version précédente."""
        previous = self.status.get("precedente") or {}
        target = str(request.get("target") or "").strip()
        if not previous or previous.get("version") != target:
            return self._reject(request, "aucune version précédente conservée")
        reference = f"{self.config.image}:{target}"
        ok, detail = self.runner.remove_image(reference)
        if not ok:
            self._transition("failed", "forget", f"suppression de {reference} impossible : {detail}")
            return "failed"
        self.status["precedente"] = None
        self._transition("done", "forget", f"image {reference} supprimée")
        self._history("forgotten", target=target, reference=reference)
        log("oubli", f"image {reference} supprimée")
        return "done"

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
    def recover(self) -> None:
        """Un état actif au démarrage = mise à jour interrompue par un arrêt."""
        if self.status.get("state") in ACTIVE_STATES:
            self._transition(
                "failed", "recover",
                "mise à jour interrompue par un redémarrage du compagnon : "
                "état repris depuis status.json, aucune demande rejouée",
            )

    def step_once(self) -> str | None:
        request = self.read_request()
        if request is None:
            self.heartbeat()
            return None
        nonce = request.get("nonce") if isinstance(request, dict) else None
        if self.is_handled(nonce):
            self.heartbeat()
            return "ignored"
        return self.handle(request)

    def run_forever(self) -> None:  # pragma: no cover - boucle infinie
        self.status.setdefault("state", "idle")
        self.recover()
        self.start_heartbeat()
        self._write_status()
        log("démarrage", f"compagnon {self.status.get('updater_version')} — "
                         f"poll {self.config.poll} s, battement {self.config.heartbeat_interval} s, "
                         f"demande déjà traitée : {self.status.get('handled_nonce')}")
        while True:
            try:
                self.step_once()
            except Exception as exc:  # la boucle ne meurt jamais
                log("erreur", f"{type(exc).__name__}: {exc}")
                self.status.update({"state": "failed", "step": "loop",
                                    "message": f"{type(exc).__name__}: {exc}",
                                    "updated_at": now()})
                self._write_status()
            time.sleep(max(1, self.config.poll))


def main() -> None:  # pragma: no cover - point d'entrée
    Updater().run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
