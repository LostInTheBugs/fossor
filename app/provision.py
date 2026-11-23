"""Automatic preparation of a dedicated account (§4.1 bis, mode 3).

Rules enforced here:

* the temporary login/password only exist in memory for the duration of the
  request — never in the database, the audit log, the logs, or an exception;
* every remote command is a fixed template whose only variables are the
  **validated** account name (``^[a-z_][a-z0-9_-]{0,31}$``) and Fossor's
  **validated** public key (OpenSSH ed25519, identical to ours);
* ``sudo -S`` receives the temporary password on stdin, never in the command
  line; ``visudo -cf`` validates the sudoers drop-in before ``install -m 0440``;
* a failure at any step rolls back what was created and audits
  « préparation échouée à l'étape X ».
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import textwrap
from typing import Any

import asyncssh

logger = logging.getLogger("fossor.provision")

ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUDOERS_PATH = "/etc/sudoers.d/fossor"
STEP_TIMEOUT = 30

STEP_TEMP_CONNECTION = "connexion temporaire"
STEP_PREFLIGHT = "préflight (sudo / useradd / visudo)"
STEP_ACCOUNT = "création du compte"
STEP_KEY = "pose de la clé publique"
STEP_SUDOERS = "fichier sudoers"
STEP_GPU = "script de limite de puissance"
STEP_VERIFY = "vérification par clé"
GPU_PL_PATH = "/usr/local/sbin/fossor-gpu-pl"
GPU_PL_SUDOERS_PATH = "/etc/sudoers.d/fossor-gpu-pl"


class ProvisionError(ValueError):
    """Invalid request data — surfaced as HTTP 400."""


# ── Validation ────────────────────────────────────────────────────────
def validate_account(name: str) -> str:
    if not isinstance(name, str) or not ACCOUNT_RE.fullmatch(name):
        raise ProvisionError(
            "Nom de compte invalide : 1 à 32 caractères, minuscules, chiffres, "
            "« _ » ou « - », commençant par une lettre ou « _ »."
        )
    return name


def validate_public_key(line: str, expected: str | None = None) -> str:
    """Refuse anything that is not our own OpenSSH ed25519 public key."""
    if not isinstance(line, str):
        raise ProvisionError("Clé publique invalide")
    parts = line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ProvisionError("Clé publique invalide : format OpenSSH ed25519 attendu")
    try:
        base64.b64decode(parts[1], validate=True)
        asyncssh.import_public_key(" ".join(parts[:2]))
    except Exception:
        raise ProvisionError("Clé publique invalide : contenu illisible")
    if expected is not None and " ".join(parts[:2]) != " ".join(expected.split()[:2]):
        raise ProvisionError("Clé publique inattendue : ce n'est pas la clé de Fossor")
    return line.strip()


# ── Remote scripts (fixed templates, validated variables only) ────────
_PREFLIGHT = textwrap.dedent(
    """\
    command -v sudo >/dev/null 2>&1 || { echo MISSING:sudo; exit 20; }
    command -v useradd >/dev/null 2>&1 || { echo MISSING:useradd; exit 21; }
    command -v visudo >/dev/null 2>&1 || { echo MISSING:visudo; exit 22; }
    command -v install >/dev/null 2>&1 || { echo MISSING:install; exit 23; }
    command -v getent >/dev/null 2>&1 || { echo MISSING:getent; exit 24; }
    command -v awk >/dev/null 2>&1 || { echo MISSING:awk; exit 25; }
    echo PREFLIGHT_OK
    """
).strip()

_CREATE_ACCOUNT = (
    'if id -u "$1" >/dev/null 2>&1; then echo ACCOUNT_EXISTS; '
    'else useradd -m -s /bin/bash "$1" >/dev/null && echo ACCOUNT_CREATED; fi'
)

_INSTALL_KEY = textwrap.dedent(
    """\
    set -e
    H=$(getent passwd "$1" | cut -d: -f6)
    if [ -z "$H" ] || [ ! -d "$H" ]; then echo NO_HOME; exit 30; fi
    install -d -m 700 -o "$1" "$H/.ssh"
    touch "$H/.ssh/authorized_keys"
    chown "$1" "$H/.ssh/authorized_keys"
    chmod 600 "$H/.ssh/authorized_keys"
    if grep -qxF "$2" "$H/.ssh/authorized_keys"; then
      echo KEY_PRESENT
    else
      printf '%s\\n' "$2" >> "$H/.ssh/authorized_keys"
      echo KEY_INSTALLED
    fi
    """
).strip()

_INSTALL_SUDOERS = textwrap.dedent(
    """\
    set -e
    T=$(mktemp)
    printf '%s ALL=(ALL) NOPASSWD:ALL\\n' "$1" > "$T"
    visudo -cf "$T" >/dev/null
    install -m 0440 -o root -g root "$T" /etc/sudoers.d/fossor
    rm -f "$T"
    echo SUDOERS_INSTALLED
    """
).strip()

#: Script enveloppe root : deux entiers seulement, bornes relues ici.
GPU_PL_WRAPPER = textwrap.dedent(r"""#!/bin/sh
# Applique une limite de puissance GPU. Seuls deux entiers sont acceptes ;
# les bornes sont relues ici, jamais fournies par l'appelant.
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: fossor-gpu-pl <index> <watts>" >&2
  exit 2
fi
case "$1" in ''|*[!0-9]*) echo "index invalide" >&2; exit 2 ;; esac
case "$2" in ''|*[!0-9]*) echo "watts invalide" >&2; exit 2 ;; esac
INDEX=$1
WATTS=$2
NVSMI=$(command -v nvidia-smi 2>/dev/null || true)
if [ -z "$NVSMI" ]; then
  echo "nvidia-smi absent" >&2
  exit 3
fi
BOUNDS=$("$NVSMI" -i "$INDEX" --query-gpu=power.min_limit,power.max_limit --format=csv,noheader,nounits 2>/dev/null | head -n 1)
if [ -z "$BOUNDS" ]; then
  echo "bornes illisibles pour GPU $INDEX" >&2
  exit 4
fi
MIN=$(printf '%s' "$BOUNDS" | cut -d, -f1 | tr -d ' ')
MAX=$(printf '%s' "$BOUNDS" | cut -d, -f2 | tr -d ' ')
case "$MIN" in ''|*[!0-9.]*) echo "borne minimale illisible" >&2; exit 4 ;; esac
case "$MAX" in ''|*[!0-9.]*) echo "borne maximale illisible" >&2; exit 4 ;; esac
if ! awk -v w="$WATTS" -v lo="$MIN" -v hi="$MAX" 'BEGIN { exit !(w + 0 >= lo + 0 && w + 0 <= hi + 0) }'; then
  echo "limite $WATTS W hors bornes [$MIN, $MAX] pour GPU $INDEX" >&2
  exit 5
fi
exec "$NVSMI" -i "$INDEX" -pl "$WATTS"
""").strip()

_INSTALL_GPU_WRAPPER = textwrap.dedent(
    """    set -e
    T=$(mktemp)
    printf '%s' "$1" > "$T"
    sh -n "$T"
    install -m 0755 -o root -g root "$T" /usr/local/sbin/fossor-gpu-pl
    S=$(mktemp)
    printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/fossor-gpu-pl\\n' "$2" > "$S"
    visudo -cf "$S" >/dev/null
    install -m 0440 -o root -g root "$S" /etc/sudoers.d/fossor-gpu-pl
    rm -f "$T" "$S"
    echo GPU_WRAPPER_INSTALLED
    """
).strip()

_VERIFY_SUDO = (
    "sudo -n -l /usr/local/sbin/fossor-gpu-pl >/dev/null 2>&1"
    " && echo SUDO_OK || echo SUDO_FAIL"
)

_REMOVE_SUDOERS = "rm -f /etc/sudoers.d/fossor && echo SUDOERS_REMOVED"

_REMOVE_GPU_WRAPPER = (
    "rm -f /usr/local/sbin/fossor-gpu-pl /etc/sudoers.d/fossor-gpu-pl"
    " && echo GPU_WRAPPER_REMOVED"
)

_REMOVE_KEY = textwrap.dedent(
    """\
    H=$(getent passwd "$1" | cut -d: -f6)
    if [ -z "$H" ] || [ ! -f "$H/.ssh/authorized_keys" ]; then
      echo KEY_ABSENT
    else
      F="$H/.ssh/authorized_keys"
      grep -vxF "$2" "$F" > "$F.fossor-tmp" || true
      cat "$F.fossor-tmp" > "$F"
      rm -f "$F.fossor-tmp"
      echo KEY_REMOVED
    fi
    """
).strip()

_ROLLBACK_ACCOUNT = 'if id -u "$1" >/dev/null 2>&1; then userdel -r "$1" >/dev/null 2>&1 && echo ACCOUNT_DELETED; fi'


def _sudo(inner: str, *args: str, no_password: bool = False) -> str:
    """Wrap a fixed inner script; only validated values are passed as args."""
    flag = "-n" if no_password else "-S -p ''"
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    return f"sudo {flag} sh -c {shlex.quote(inner)} sh {quoted_args}".strip()


def _script_create_account(account: str) -> str:
    return _sudo(_CREATE_ACCOUNT, account)


def _script_install_key(account: str, public_key: str) -> str:
    return _sudo(_INSTALL_KEY, account, public_key)


def _script_install_sudoers(account: str) -> str:
    return _sudo(_INSTALL_SUDOERS, account)


def _script_install_gpu_wrapper(account: str) -> str:
    return _sudo(_INSTALL_GPU_WRAPPER, GPU_PL_WRAPPER, account)


def _script_remove_gpu_wrapper() -> str:
    return _sudo(_REMOVE_GPU_WRAPPER, no_password=True)


def _script_rollback_account(account: str) -> str:
    return _sudo(_ROLLBACK_ACCOUNT, account)


def _script_remove_sudoers() -> str:
    return _sudo(_REMOVE_SUDOERS, no_password=True)


def _script_remove_key(account: str, public_key: str) -> str:
    return _sudo(_REMOVE_KEY, account, public_key, no_password=True)


def _script_deprovision(account: str, public_key: str, delete_account: bool) -> str:
    """One privileged shell: sudoers first, then the key, then the account."""
    inner = textwrap.dedent(
        f"""\
        set -e
        rm -f /usr/local/sbin/fossor-gpu-pl /etc/sudoers.d/fossor-gpu-pl
        echo GPU_WRAPPER_REMOVED
        rm -f {SUDOERS_PATH}
        echo SUDOERS_REMOVED
        H=$(getent passwd "$1" | cut -d: -f6)
        if [ -n "$H" ] && [ -f "$H/.ssh/authorized_keys" ]; then
          F="$H/.ssh/authorized_keys"
          grep -vxF "$2" "$F" > "$F.fossor-tmp" || true
          cat "$F.fossor-tmp" > "$F"
          rm -f "$F.fossor-tmp"
          echo KEY_REMOVED
        else
          echo KEY_ABSENT
        fi
        if [ "$3" = "1" ]; then
          if userdel -r "$1" >/dev/null 2>&1; then echo ACCOUNT_DELETED; else echo ACCOUNT_DELETE_FAILED; fi
        else
          echo ACCOUNT_KEPT
        fi
        """
    ).strip()
    return _sudo(inner, account, public_key, "1" if delete_account else "0", no_password=True)


# ── Plumbing ──────────────────────────────────────────────────────────
def _redact(text: Any, password: str) -> str:
    value = "" if text is None else str(text)
    if password:
        value = value.replace(password, "***")
    return value


def _detail(stdout: str, stderr: str) -> str:
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return " / ".join(lines[-3:])[:300] or "aucune sortie"


def _missing_tool_message(output: str) -> str:
    if "MISSING:" not in output:
        return f"préflight refusé ({output or 'sortie vide'})"
    tool = output.split("MISSING:", 1)[1].split()[0]
    return (
        f"« {tool} » est absent sur cette machine : préparation automatique "
        "impossible (utilisez la clé publique posée à la main)"
    )


async def _exec(conn, script: str, password: str | None = None) -> tuple[int, str, str]:
    """Run one fixed script; the optional password goes to stdin only."""
    proc = await asyncio.wait_for(conn.create_process(script), timeout=STEP_TIMEOUT)
    if password:
        proc.stdin.write(password + "\n")
    proc.stdin.write_eof()
    stdout, stderr, completed = await asyncio.gather(
        proc.stdout.read(), proc.stderr.read(), proc.wait()
    )
    # asyncssh 2.24: wait() returns an SSHCompletedProcess, not an exit code.
    status = getattr(completed, "exit_status", completed)
    return int(status or 0), _redact(stdout, password or ""), _redact(stderr, password or "")


def _audit(machine_id: int | None, result: str, output: str,
           actor: str = "system") -> None:
    """Audit entry — never contains the temporary password."""
    from app.database import SessionLocal
    from app.models import AuditLog

    db = SessionLocal()
    try:
        db.add(
            AuditLog(
                actor=actor or "system",
                action="provision",
                machine_id=machine_id,
                result=result,
                output=output[:500],
            )
        )
        db.commit()
    finally:
        db.close()


def _french_connection_error(error: str) -> str:
    """Name the network failure in French instead of leaking errno text."""
    text = str(error or "")
    lowered = text.lower()
    if "name or service not known" in lowered or "no address associated" in lowered:
        return "hôte introuvable : vérifiez l'adresse IP ou le nom DNS de la machine"
    if "connection refused" in lowered or "connect failed" in lowered:
        return "connexion refusée par la machine : vérifiez l'adresse et le port SSH"
    if "timed out" in lowered or "timeout" in lowered:
        return "délai dépassé : la machine ne répond pas"
    return text




def _failed(machine_id: int | None, step: str, detail: str, steps: list[str],
            rollback: str = "", actor: str = "system") -> dict[str, Any]:
    message = f"préparation échouée à l'étape « {step} » — {detail}"
    if rollback:
        message += f" ; {rollback}"
    _audit(machine_id, "fail", message, actor=actor)
    return {
        "ok": False,
        "step": step,
        "error": message,
        "steps_done": steps,
    }


async def _rollback(conn, machine_id: int | None, state: dict[str, bool],
                    account: str, public_key: str, password: str) -> str:
    """Undo what was created, in reverse order. Returns a note for the user."""
    notes: list[str] = []
    try:
        if state.get("gpu_wrapper"):
            status, _out, err = await _exec(conn, _script_remove_gpu_wrapper(), password)
            notes.append("script GPU retiré" if status == 0 else f"script GPU non retiré ({_detail('', err)})")
        if state.get("sudoers"):
            status, _out, err = await _exec(conn, _script_remove_sudoers(), password)
            notes.append("sudoers retiré" if status == 0 else f"sudoers non retiré ({_detail('', err)})")
        if state.get("key_installed"):
            status, _out, err = await _exec(conn, _script_remove_key(account, public_key), password)
            notes.append("clé retirée" if status == 0 else f"clé non retirée ({_detail('', err)})")
        if state.get("account_created"):
            status, out, err = await _exec(conn, _script_rollback_account(account), password)
            notes.append("compte supprimé" if "ACCOUNT_DELETED" in out else f"compte non supprimé ({_detail(out, err)})")
    except Exception as exc:  # pragma: no cover - defensive
        notes.append(f"retour arrière incomplet ({_redact(exc, password)})")
    return "retour arrière : " + ", ".join(notes) if notes else "rien à annuler"


def _apply_success(machine_id: int, account: str) -> None:
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine is None:
            return
        machine.auth_type = "fossor_key"
        machine.ssh_user = account
        machine.sudo = "sudo without password"
        machine.secret_ref = None  # the temporary secret is forgotten
        machine.updated_at = datetime.now(timezone.utc).isoformat()
        db.commit()
    finally:
        db.close()


# ── Public API ────────────────────────────────────────────────────────
async def provision_machine(
    machine: Any,
    temp_user: str,
    temp_password: str,
    account: str = "fossor",
    actor: str = "system",
) -> dict[str, Any]:
    """Prepare the dedicated account through one temporary SSH session."""
    from app import fossor_keys
    from app.ssh_client import open_pinned_connection

    account = validate_account(account)
    if not isinstance(temp_user, str) or not temp_user.strip():
        raise ProvisionError("Login temporaire requis")
    if not isinstance(temp_password, str) or not temp_password:
        raise ProvisionError("Mot de passe temporaire requis")
    temp_user = temp_user.strip()
    public_key = validate_public_key(fossor_keys.public_key_text())

    steps: list[str] = []
    state = {"account_created": False, "key_installed": False,
             "gpu_wrapper": False, "sudoers": False}

    conn, fingerprint, failure = await open_pinned_connection(
        host=machine.host,
        port=machine.ssh_port,
        username=temp_user,
        auth_type="password",
        secret=temp_password,
        pinned_fp=(machine.host_key or "").strip() or None,
        pinned_public_key=(machine.host_key_public or "").strip() or None,
        machine_id=machine.id,
        audit_kind="provision",
        actor=actor,
    )
    if failure is not None:
        return _failed(
            machine.id,
            STEP_TEMP_CONNECTION,
            _french_connection_error(_redact(failure.get("error", ""), temp_password)),
            steps,
            actor=actor,
        )

    try:
        status, out, err = await _exec(conn, _PREFLIGHT)
        if status != 0 or "PREFLIGHT_OK" not in out:
            return _failed(machine.id, STEP_PREFLIGHT, _missing_tool_message(out or err), steps, actor=actor)
        steps.append("préflight")

        status, out, err = await _exec(conn, _script_create_account(account), temp_password)
        if status != 0 or ("ACCOUNT_CREATED" not in out and "ACCOUNT_EXISTS" not in out):
            note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
            return _failed(machine.id, STEP_ACCOUNT, _detail(out, err), steps, note, actor=actor)
        state["account_created"] = "ACCOUNT_CREATED" in out
        steps.append("compte")

        status, out, err = await _exec(conn, _script_install_key(account, public_key), temp_password)
        if status != 0 or ("KEY_INSTALLED" not in out and "KEY_PRESENT" not in out):
            note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
            return _failed(machine.id, STEP_KEY, _detail(out, err), steps, note, actor=actor)
        state["key_installed"] = "KEY_INSTALLED" in out
        steps.append("clé")

        status, out, err = await _exec(conn, _script_install_gpu_wrapper(account), temp_password)
        if status != 0 or "GPU_WRAPPER_INSTALLED" not in out:
            note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
            return _failed(machine.id, STEP_GPU, _detail(out, err), steps, note, actor=actor)
        state["gpu_wrapper"] = True
        steps.append("limite GPU")

        status, out, err = await _exec(conn, _script_install_sudoers(account), temp_password)
        if status != 0 or "SUDOERS_INSTALLED" not in out:
            note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
            return _failed(machine.id, STEP_SUDOERS, _detail(out, err), steps, note, actor=actor)
        state["sudoers"] = True
        steps.append("sudoers")
    except asyncio.TimeoutError:
        note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
        return _failed(machine.id, STEP_ACCOUNT, f"délai dépassé ({STEP_TIMEOUT} s)", steps, note, actor=actor)
    except (asyncssh.Error, OSError) as exc:
        note = await _rollback(conn, machine.id, state, account, public_key, temp_password)
        return _failed(machine.id, STEP_ACCOUNT, _redact(exc, temp_password), steps, note, actor=actor)
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass

    # ── Verification with a second session, using the key only ────────
    verify_conn, verify_fp, verify_failure = await open_pinned_connection(
        host=machine.host,
        port=machine.ssh_port,
        username=account,
        auth_type="fossor_key",
        secret="",
        pinned_fp=(machine.host_key or "").strip() or None,
        pinned_public_key=(machine.host_key_public or "").strip() or None,
        machine_id=machine.id,
        audit_kind="provision",
        actor=actor,
    )
    verified = False
    verify_error = ""
    if verify_failure is not None:
        verify_error = _redact(verify_failure.get("error", ""), temp_password)
    else:
        try:
            status, out, err = await _exec(verify_conn, _VERIFY_SUDO)
            verified = status == 0 and "SUDO_OK" in out
            if not verified:
                verify_error = _detail(out, err)
        except Exception as exc:
            verify_error = _redact(exc, temp_password)
        finally:
            verify_conn.close()
            try:
                await verify_conn.wait_closed()
            except Exception:
                pass

    if not verified:
        # Roll back through a fresh temporary session: credentials are still valid.
        rollback_conn, _fp, _failure = await open_pinned_connection(
            host=machine.host,
            port=machine.ssh_port,
            username=temp_user,
            auth_type="password",
            secret=temp_password,
            pinned_fp=(machine.host_key or "").strip() or None,
            pinned_public_key=(machine.host_key_public or "").strip() or None,
            machine_id=machine.id,
            audit_kind="provision",
        actor=actor,
        )
        note = "rien à annuler"
        if rollback_conn is not None:
            note = await _rollback(rollback_conn, machine.id, state, account, public_key, temp_password)
            rollback_conn.close()
            try:
                await rollback_conn.wait_closed()
            except Exception:
                pass
        return _failed(machine.id, STEP_VERIFY, verify_error or "sudo -n impossible", steps, note, actor=actor)

    steps.append("vérification")
    _apply_success(machine.id, account)
    _audit(
        machine.id,
        "ok",
        f"préparation par accès temporaire du compte {account} "
        "(clé Fossor posée, sudo sans mot de passe) ; accès temporaire oublié",
    )
    return {
        "ok": True,
        "account": account,
        "host_key_fingerprint": verify_fp or fingerprint,
        "steps_done": steps,
        "message": (
            f"Machine préparée : connexion par la clé de Fossor avec le compte "
            f"{account} (sudo sans mot de passe). L'accès temporaire n'a pas été conservé."
        ),
    }


async def update_preparation(
    machine: Any,
    temp_user: str,
    temp_password: str,
    actor: str = "system",
) -> dict[str, Any]:
    """Réinstalle le script enveloppe et sa règle sudoers sur une machine préparée.

    Une seule session avec les identifiants admin ; ils ne sont ni stockés,
    ni journalisés, ni audités.
    """
    from app.ssh_client import open_pinned_connection

    account = validate_account(machine.ssh_user or "")
    if not isinstance(temp_user, str) or not temp_user.strip():
        raise ProvisionError("Login admin requis")
    if not isinstance(temp_password, str) or not temp_password:
        raise ProvisionError("Mot de passe admin requis")
    temp_user = temp_user.strip()

    conn, _fingerprint, failure = await open_pinned_connection(
        host=machine.host,
        port=machine.ssh_port,
        username=temp_user,
        auth_type="password",
        secret=temp_password,
        pinned_fp=(machine.host_key or "").strip() or None,
        pinned_public_key=(machine.host_key_public or "").strip() or None,
        machine_id=machine.id,
        audit_kind="provision-update",
        actor=actor,
    )
    if failure is not None:
        message = (
            "connexion admin impossible — "
            + _french_connection_error(_redact(failure.get("error", ""), temp_password))
        )
        _audit(machine.id, "fail", message, actor=actor)
        return {"ok": False, "error": message}

    steps: list[str] = []
    try:
        status, out, err = await _exec(conn, _PREFLIGHT)
        if status != 0 or "PREFLIGHT_OK" not in out:
            message = f"mise à jour impossible — {_missing_tool_message(out or err)}"
            _audit(machine.id, "fail", message, actor=actor)
            return {"ok": False, "error": message, "steps_done": steps}
        steps.append("préflight")

        status, out, err = await _exec(
            conn, _script_install_gpu_wrapper(account), temp_password
        )
        if status != 0 or "GPU_WRAPPER_INSTALLED" not in out:
            message = (
                f"mise à jour échouée à l'étape « {STEP_GPU} » — {_detail(out, err)}"
            )
            _audit(machine.id, "fail", message, actor=actor)
            return {"ok": False, "step": STEP_GPU, "error": message,
                    "steps_done": steps}
        steps.append("limite GPU")
    except asyncio.TimeoutError:
        message = f"mise à jour échouée — délai dépassé ({STEP_TIMEOUT} s)"
        _audit(machine.id, "fail", message, actor=actor)
        return {"ok": False, "error": message, "steps_done": steps}
    except (asyncssh.Error, OSError) as exc:
        message = f"mise à jour échouée — {_redact(exc, temp_password)}"
        _audit(machine.id, "fail", message, actor=actor)
        return {"ok": False, "error": message, "steps_done": steps}
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass

    _audit(
        machine.id,
        "ok",
        f"préparation mise à jour pour {account} : script {GPU_PL_PATH} et règle "
        f"sudoers étroite ; identifiants admin oubliés",
        actor=actor,
    )
    return {
        "ok": True,
        "steps_done": steps,
        "message": (
            "Préparation mise à jour : le script fossor-gpu-pl et sa règle sudoers "
            "étroite sont installés. Les identifiants admin n'ont pas été conservés."
        ),
    }


async def deprovision_machine(machine: Any, delete_account: bool = False) -> dict[str, Any]:
    """Remove Fossor's access: sudoers drop-in, key line, optionally the account."""
    from app import fossor_keys
    from app.ssh_client import open_pinned_connection

    if machine.auth_type != "fossor_key":
        raise ProvisionError("Cette machine n'utilise pas la clé de Fossor")
    account = validate_account(machine.ssh_user or "")
    public_key = validate_public_key(fossor_keys.public_key_text())

    # An account cannot be deleted while it is the account of the running session.
    can_delete = bool(delete_account) and account != (machine.ssh_user or "")

    conn, fingerprint, failure = await open_pinned_connection(
        host=machine.host,
        port=machine.ssh_port,
        username=machine.ssh_user,
        auth_type="fossor_key",
        secret="",
        pinned_fp=(machine.host_key or "").strip() or None,
        pinned_public_key=(machine.host_key_public or "").strip() or None,
        machine_id=machine.id,
        audit_kind="deprovision",
    )
    if failure is not None:
        message = f"retrait impossible — {failure.get('error', '')}"
        _audit(machine.id, "fail", message)
        return {"ok": False, "error": message}

    try:
        status, out, err = await _exec(
            conn, _script_deprovision(account, public_key, can_delete)
        )
    except Exception as exc:
        message = f"retrait impossible — {exc}"
        _audit(machine.id, "fail", message)
        return {"ok": False, "error": message}
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass

    if status != 0 or "SUDOERS_REMOVED" not in out:
        message = f"retrait échoué — {_detail(out, err)}"
        _audit(machine.id, "fail", message)
        return {"ok": False, "error": message}

    deleted = "ACCOUNT_DELETED" in out
    message = "Accès Fossor retiré : fichier sudoers et clé publique supprimés."
    if delete_account and not deleted:
        if can_delete:
            message += f" La suppression du compte {account} a échoué — à faire à la main."
        else:
            message += (
                f" Le compte {account} est celui de la session en cours : "
                f"supprimez-le à la main (sudo userdel -r {account}) une fois la clé retirée."
            )
    _audit(machine.id, "ok", f"accès Fossor retiré (compte {account})")
    return {
        "ok": True,
        "account": account,
        "account_deleted": deleted,
        "host_key_fingerprint": fingerprint,
        "message": message,
    }
