"""Notifications multi-canal (§4.14).

* trois canaux : Discord (webhook), Telegram (jeton + chat), e-mail (SMTP) ;
* secrets chiffrés par la clé maître, jamais renvoyés par l'API (``has_…``) ;
* matrice événement × canal, niveau par événement, heures silencieuses
  (Europe/Paris) qui laissent quand même passer les « critique » ;
* file d'attente en base (``notification_log``), trois tentatives, journal.

Aucun secret ni adresse de wallet complète ne doit sortir dans un message ou
dans le journal : les erreurs sont masquées avant écriture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai_config import get_setting, set_setting
from app.models import AppSetting, Metric, NotificationLog, NotificationState
from app.secrets import decrypt_secret, encrypt_secret

logger = logging.getLogger("fossor.notifications")

CONFIG_KEY = "notifications"
SECRETS_KEY = "notification_secrets"
MAX_ATTEMPTS = 3
LOOP_INTERVAL = 3.0

CHANNELS = ("discord", "telegram", "email")
LEVELS = ("info", "alerte", "critique")

EVENTS: dict[str, dict[str, str]] = {
    "machine_offline": {"label": "Machine hors ligne", "level": "alerte"},
    "machine_online": {"label": "Machine de retour en ligne", "level": "info"},
    "hashrate_low": {"label": "Hashrate sous 50 % de la moyenne 1 h", "level": "alerte"},
    "psu_overloaded": {"label": "Alimentation surchargée", "level": "critique"},
    "campaign_started": {"label": "Campagne démarrée", "level": "info"},
    "campaign_finished": {"label": "Campagne terminée", "level": "info"},
    "campaign_failed": {"label": "Campagne en échec", "level": "critique"},
    "default_miner_restored": {"label": "Retour au token par défaut (tentative)", "level": "info"},
    "default_miner_restore_failed": {"label": "Retour au token par défaut échoué", "level": "critique"},
    "pool_balance_threshold": {"label": "Solde de pool au-dessus du seuil de paiement", "level": "info"},
    "payment_received": {"label": "Paiement reçu", "level": "info"},
    "new_listing": {"label": "Nouveau prix / listing d'un token sans prix", "level": "info"},
    "update_available": {"label": "Mise à jour disponible", "level": "info"},
    "update_done": {"label": "Mise à jour terminée", "level": "info"},
    "update_rolled_back": {"label": "Mise à jour annulée (retour arrière)", "level": "critique"},
}

DEFAULT_QUIET = {"enabled": False, "start": "22:00", "end": "07:00", "tz": "Europe/Paris"}
ACTIVE_UPDATE_STATES = {"pulling", "backing_up", "switching", "health_check"}

# Test hook: httpx.MockTransport replaces the real network in the unit tests.
TRANSPORT: httpx.AsyncBaseTransport | None = None
SMTP_TIMEOUT = 15

_task: asyncio.Task | None = None


# ── Configuration ──────────────────────────────────────────────────────
def default_config() -> dict:
    return {
        "channels": {
            "discord": {"enabled": False},
            "telegram": {"enabled": False, "chat_id": ""},
            "email": {
                "enabled": False,
                "host": "",
                "port": 587,
                "security": "starttls",
                "username": "",
                "from": "",
                "to": "",
            },
        },
        "matrix": {event: [] for event in EVENTS},
        "quiet_hours": dict(DEFAULT_QUIET),
    }


def get_config(db: Session) -> dict:
    config = default_config()
    stored = get_setting(db, CONFIG_KEY, None)
    if isinstance(stored, dict):
        channels = stored.get("channels")
        if isinstance(channels, dict):
            for name in CHANNELS:
                if isinstance(channels.get(name), dict):
                    config["channels"][name].update(channels[name])
        matrix = stored.get("matrix")
        if isinstance(matrix, dict):
            for event in EVENTS:
                value = matrix.get(event)
                if isinstance(value, list):
                    config["matrix"][event] = [c for c in value if c in CHANNELS]
        quiet = stored.get("quiet_hours")
        if isinstance(quiet, dict):
            config["quiet_hours"].update({k: quiet[k] for k in DEFAULT_QUIET if k in quiet})
    return config


def _hhmm(value: str) -> str:
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except (TypeError, ValueError):
        raise ValueError(f"heure invalide : {value!r} (HH:MM attendu)")
    return parsed.strftime("%H:%M")


def save_config(db: Session, payload: dict) -> dict:
    """Validate and persist the non-secret part; secrets go through the API."""
    if not isinstance(payload, dict):
        raise ValueError("configuration invalide")
    config = get_config(db)
    channels = payload.get("channels")
    if channels is not None:
        if not isinstance(channels, dict):
            raise ValueError("« channels » doit être un objet")
        for name in CHANNELS:
            row = channels.get(name)
            if row is None:
                continue
            if not isinstance(row, dict):
                raise ValueError(f"canal {name} invalide")
            target = config["channels"][name]
            if "enabled" in row:
                target["enabled"] = bool(row["enabled"])
            if name == "telegram" and "chat_id" in row:
                target["chat_id"] = str(row["chat_id"] or "").strip()
            if name == "email":
                for field in ("host", "username", "from", "to"):
                    if field in row:
                        target[field] = str(row[field] or "").strip()
                if "port" in row:
                    try:
                        target["port"] = int(row["port"])
                    except (TypeError, ValueError):
                        raise ValueError("port SMTP invalide")
                    if not 1 <= target["port"] <= 65535:
                        raise ValueError("port SMTP hors plage")
                if "security" in row:
                    security = str(row["security"] or "starttls")
                    if security not in ("starttls", "ssl", "none"):
                        raise ValueError("sécurité SMTP invalide (starttls, ssl ou none)")
                    target["security"] = security
    matrix = payload.get("matrix")
    if matrix is not None:
        if not isinstance(matrix, dict):
            raise ValueError("« matrix » doit être un objet")
        for event, value in matrix.items():
            if event not in EVENTS:
                raise ValueError(f"événement inconnu : {event}")
            if not isinstance(value, list) or any(c not in CHANNELS for c in value):
                raise ValueError(f"canaux invalides pour {event}")
            config["matrix"][event] = list(dict.fromkeys(value))
    quiet = payload.get("quiet_hours")
    if quiet is not None:
        if not isinstance(quiet, dict):
            raise ValueError("« quiet_hours » doit être un objet")
        if "enabled" in quiet:
            config["quiet_hours"]["enabled"] = bool(quiet["enabled"])
        if "start" in quiet:
            config["quiet_hours"]["start"] = _hhmm(quiet["start"])
        if "end" in quiet:
            config["quiet_hours"]["end"] = _hhmm(quiet["end"])
        if "tz" in quiet:
            tz = str(quiet["tz"] or "Europe/Paris")
            try:
                ZoneInfo(tz)
            except Exception:
                raise ValueError(f"fuseau inconnu : {tz}")
            config["quiet_hours"]["tz"] = tz
    set_setting(db, CONFIG_KEY, config)
    db.commit()
    return config


def public_config(db: Session) -> dict:
    config = get_config(db)
    return {
        "channels": {
            "discord": {
                "enabled": bool(config["channels"]["discord"].get("enabled")),
                "has_webhook": has_secret(db, "discord_webhook"),
            },
            "telegram": {
                "enabled": bool(config["channels"]["telegram"].get("enabled")),
                "chat_id": config["channels"]["telegram"].get("chat_id", ""),
                "has_token": has_secret(db, "telegram_token"),
            },
            "email": {
                "enabled": bool(config["channels"]["email"].get("enabled")),
                "host": config["channels"]["email"].get("host", ""),
                "port": config["channels"]["email"].get("port", 587),
                "security": config["channels"]["email"].get("security", "starttls"),
                "username": config["channels"]["email"].get("username", ""),
                "from": config["channels"]["email"].get("from", ""),
                "to": config["channels"]["email"].get("to", ""),
                "has_password": has_secret(db, "email_password"),
            },
        },
        "matrix": config["matrix"],
        "quiet_hours": config["quiet_hours"],
        "events": [
            {"code": code, "label": meta["label"], "level": meta["level"]}
            for code, meta in EVENTS.items()
        ],
        "levels": list(LEVELS),
    }


# ── Secrets (chiffrés, jamais renvoyés) ────────────────────────────────
def _secret_refs(db: Session) -> dict:
    raw = get_setting(db, SECRETS_KEY, None)
    return raw if isinstance(raw, dict) else {}


def set_secret(db: Session, name: str, value: str) -> None:
    refs = _secret_refs(db)
    refs[name] = encrypt_secret(value)
    set_setting(db, SECRETS_KEY, refs)
    db.commit()


def clear_secret(db: Session, name: str) -> None:
    refs = _secret_refs(db)
    if name in refs:
        refs.pop(name)
        set_setting(db, SECRETS_KEY, refs)
        db.commit()


def has_secret(db: Session, name: str) -> bool:
    return name in _secret_refs(db)


def plain_secret(db: Session, name: str) -> str | None:
    ref = _secret_refs(db).get(name)
    if not ref:
        return None
    try:
        return decrypt_secret(ref)
    except Exception:
        # Master key rotated: the secret is unreadable, never crash the app.
        return None


def all_plain_secrets(db: Session) -> dict[str, str]:
    return {name: value for name in _secret_refs(db) if (value := plain_secret(db, name))}


def apply_secrets(db: Session, values: dict[str, str]) -> None:
    """Write already-plain secrets (import path), encrypted with the master key."""
    refs = _secret_refs(db)
    for name, value in values.items():
        refs[name] = encrypt_secret(value)
    set_setting(db, SECRETS_KEY, refs)
    db.commit()


def update_secrets_from_payload(db: Session, payload: dict) -> None:
    channels = payload.get("channels") or {}
    mapping = (
        ("discord", "webhook", "discord_webhook"),
        ("telegram", "token", "telegram_token"),
        ("email", "password", "email_password"),
    )
    for channel, field, name in mapping:
        value = (channels.get(channel) or {}).get(field)
        if isinstance(value, str) and value.strip():
            set_secret(db, name, value.strip())
    clear = payload.get("clear_secrets") or []
    if isinstance(clear, list):
        for channel in clear:
            for target, _, name in mapping:
                if channel == target:
                    clear_secret(db, name)


# ── Heures silencieuses ────────────────────────────────────────────────
def _minutes(value: str) -> int:
    hour, minute = str(value).split(":")[:2]
    return int(hour) * 60 + int(minute)


def in_quiet_hours(config: dict, now: datetime | None = None) -> bool:
    quiet = config.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    try:
        tz = ZoneInfo(quiet.get("tz") or "Europe/Paris")
        start = _minutes(quiet.get("start") or "22:00")
        end = _minutes(quiet.get("end") or "07:00")
    except Exception:
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(tz)
    minute = current.hour * 60 + current.minute
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    # Plage qui passe minuit : [start, 24 h[ ∪ [0, end[.
    return minute >= start or minute < end


# ── Message formatting ─────────────────────────────────────────────────
def mask_address(address: str | None) -> str:
    text = str(address or "")
    if len(text) <= 10:
        return text
    return f"{text[:6]}…{text[-4:]}"


def format_hashrate(value: float | None) -> str:
    if value is None:
        return "—"
    units = ("H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s")
    rate = float(value)
    index = 0
    while rate >= 1000 and index < len(units) - 1:
        rate /= 1000
        index += 1
    return f"{rate:.2f} {units[index]}"


def _sanitize(text: str, db: Session | None = None) -> str:
    cleaned = str(text)
    if db is not None:
        for value in all_plain_secrets(db).values():
            if value:
                cleaned = cleaned.replace(value, "***")
    return cleaned[:300]


# ── Envoi ──────────────────────────────────────────────────────────────
async def _post_json(url: str, payload: dict, headers: dict | None = None) -> int:
    async with httpx.AsyncClient(transport=TRANSPORT, timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    return response.status_code


async def send_discord(webhook: str, text: str) -> None:
    status = await _post_json(webhook, {"content": text[:1900]})
    if status >= 300:
        # Never echo the webhook: it is the credential itself.
        raise RuntimeError(f"Discord a répondu HTTP {status}")


async def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    status = await _post_json(url, {"chat_id": chat_id, "text": text[:4000]})
    if status >= 300:
        raise RuntimeError(f"Telegram a répondu HTTP {status}")


def _send_email_sync(config: dict, password: str, subject: str, text: str) -> None:
    message = EmailMessage()
    message["From"] = config.get("from") or config.get("username")
    message["To"] = config.get("to")
    message["Subject"] = subject
    message.set_content(text)
    host = config.get("host")
    port = int(config.get("port") or 587)
    security = config.get("security") or "starttls"
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    try:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if config.get("username"):
            server.login(config["username"], password or "")
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_email(config: dict, password: str, subject: str, text: str) -> None:
    await asyncio.to_thread(_send_email_sync, config, password, subject, text)


def channel_ready(db: Session, config: dict, channel: str) -> bool:
    if channel == "discord":
        return has_secret(db, "discord_webhook")
    if channel == "telegram":
        return has_secret(db, "telegram_token") and bool(config["channels"]["telegram"].get("chat_id"))
    if channel == "email":
        row = config["channels"]["email"]
        return has_secret(db, "email_password") and bool(row.get("host")) and bool(row.get("to"))
    return False


async def deliver(db: Session, row: NotificationLog, config: dict | None = None) -> bool:
    config = config or get_config(db)
    text = f"[Fossor] {row.subject}\n{row.body}"
    try:
        if row.channel == "discord":
            webhook = plain_secret(db, "discord_webhook")
            if not webhook:
                raise RuntimeError("webhook Discord non configuré")
            await send_discord(webhook, text)
        elif row.channel == "telegram":
            token = plain_secret(db, "telegram_token")
            chat_id = config["channels"]["telegram"].get("chat_id")
            if not token or not chat_id:
                raise RuntimeError("jeton ou chat Telegram non configuré")
            await send_telegram(token, chat_id, text)
        elif row.channel == "email":
            email = config["channels"]["email"]
            password = plain_secret(db, "email_password")
            if not email.get("host") or not email.get("to"):
                raise RuntimeError("SMTP non configuré")
            await send_email(email, password or "", row.subject, row.body)
        else:
            raise RuntimeError(f"canal inconnu : {row.channel}")
    except Exception as exc:
        row.attempts = int(row.attempts or 0) + 1
        row.error = _sanitize(str(exc) or exc.__class__.__name__, db)
        row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
        db.commit()
        logger.warning("notification %s (%s) en échec : %s", row.event, row.channel, row.error)
        return False
    row.attempts = int(row.attempts or 0) + 1
    row.error = None
    row.status = "sent"
    db.commit()
    return True


# ── File d'attente et journal ──────────────────────────────────────────
def enqueue(
    db: Session,
    event: str,
    message: str,
    level: str | None = None,
    channels: list[str] | None = None,
    subject: str | None = None,
) -> list[int]:
    """Queue one row per target channel; returns the created ``notification_log`` ids."""
    if event not in EVENTS:
        raise ValueError(f"événement inconnu : {event}")
    config = get_config(db)
    level = level or EVENTS[event]["level"]
    targets = [c for c in (channels if channels is not None else config["matrix"].get(event, []))]
    targets = [c for c in dict.fromkeys(targets) if c in CHANNELS]
    if not targets:
        return []
    quiet = in_quiet_hours(config)
    ids: list[int] = []
    for channel in targets:
        if quiet and level != "critique":
            row = NotificationLog(
                event=event, channel=channel, level=level,
                subject=subject or EVENTS[event]["label"], body=message[:2000],
                status="skipped", attempts=0, error="heures silencieuses",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
            continue
        row = NotificationLog(
            event=event, channel=channel, level=level,
            subject=subject or EVENTS[event]["label"], body=message[:2000],
            status="pending", attempts=0,
        )
        db.add(row)
        db.flush()
        ids.append(row.id)
    db.commit()
    wake()
    return ids


def list_log(db: Session, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    rows = db.query(NotificationLog).order_by(NotificationLog.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "ts": row.ts,
            "event": row.event,
            "event_label": EVENTS.get(row.event, {}).get("label", row.event),
            "channel": row.channel,
            "level": row.level,
            "subject": row.subject,
            "body": row.body,
            "status": row.status,
            "attempts": row.attempts,
            "error": row.error,
        }
        for row in rows
    ]


def pending_count(db: Session) -> int:
    return int(db.query(func.count(NotificationLog.id)).filter(NotificationLog.status == "pending").scalar() or 0)


async def process_queue(limit: int = 20) -> int:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(NotificationLog)
            .filter(NotificationLog.status == "pending", NotificationLog.attempts < MAX_ATTEMPTS)
            .order_by(NotificationLog.id)
            .limit(limit)
            .all()
        )
        if not rows:
            return 0
        config = get_config(db)
        sent = 0
        for row in rows:
            if await deliver(db, row, config):
                sent += 1
        return sent
    finally:
        db.close()


async def send_test(db: Session, channel: str) -> dict:
    if channel not in CHANNELS:
        raise ValueError(f"canal inconnu : {channel}")
    row = NotificationLog(
        event="test",
        channel=channel,
        level="info",
        subject="Test de notification",
        body="Message de test envoyé depuis Fossor. Si vous le lisez, le canal fonctionne.",
        status="pending",
        attempts=0,
    )
    db.add(row)
    db.commit()
    config = get_config(db)
    ok = await deliver(db, row, config)
    return {"ok": ok, "error": row.error, "status": row.status}


# ── Petit état persistant (anti-rebond, déduplication) ─────────────────
def get_state(db: Session, key: str) -> dict:
    row = db.query(NotificationState).filter(NotificationState.key == key).first()
    if row is None or not row.value:
        return {}
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def set_state(db: Session, key: str, value: dict) -> None:
    row = db.query(NotificationState).filter(NotificationState.key == key).first()
    if row is None:
        db.add(NotificationState(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)
        row.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()


# ── Événements branchés ────────────────────────────────────────────────
def _hourly_average(db: Session, machine_uuid: str) -> float | None:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = (
        db.query(Metric.ts, func.sum(Metric.hashrate_hs))
        .filter(Metric.machine_uuid == machine_uuid, Metric.ts >= since, Metric.hashrate_hs.isnot(None))
        .group_by(Metric.ts)
        .all()
    )
    values = [float(total) for _, total in rows if total]
    return sum(values) / len(values) if values else None


def machine_result(
    db: Session,
    machine,
    online: bool,
    error: str | None = None,
    hashrate_hs: float | None = None,
) -> list[int]:
    """Offline/online with 2-collection debounce, plus hashrate < 50 % of 1 h."""
    key = f"offline:{machine.uuid}"
    state = get_state(db, key)
    emitted: list[int] = []
    if online:
        if state.get("alerted"):
            emitted += enqueue(
                db, "machine_online", f"La machine {machine.name} est de retour en ligne."
            )
        set_state(db, key, {"count": 0, "alerted": False})
        if hashrate_hs and hashrate_hs > 0:
            average = _hourly_average(db, machine.uuid)
            if average and hashrate_hs < 0.5 * average:
                hour_key = f"hashrate:{machine.uuid}"
                last = get_state(db, hour_key).get("ts")
                now = datetime.now(timezone.utc)
                recent = False
                if last:
                    try:
                        recent = (now - datetime.fromisoformat(last)).total_seconds() < 3600
                    except ValueError:
                        recent = False
                if not recent:
                    emitted += enqueue(
                        db,
                        "hashrate_low",
                        f"Hashrate de {machine.name} à {format_hashrate(hashrate_hs)}, "
                        f"sous 50 % de sa moyenne 1 h ({format_hashrate(average)}).",
                    )
                    set_state(db, hour_key, {"ts": now.isoformat()})
        return emitted

    count = int(state.get("count") or 0) + 1
    if count >= 2 and not state.get("alerted"):
        emitted += enqueue(
            db,
            "machine_offline",
            f"La machine {machine.name} est hors ligne depuis 2 collectes "
            f"({str(error or 'erreur inconnue')[:160]}).",
        )
        set_state(db, key, {"count": count, "alerted": True})
    else:
        set_state(db, key, {"count": count, "alerted": bool(state.get("alerted"))})
    return emitted


def pool_sample(
    db: Session,
    wallet,
    previous_paid: float | None,
    previous_total: float | None,
    current: dict,
    total: float | None,
) -> list[int]:
    """Payment received (paid goes up) and balance above the payout threshold."""
    emitted: list[int] = []
    ticker = str(getattr(wallet, "coin_ticker", "") or "?")
    address = mask_address(getattr(wallet, "address", ""))
    paid = current.get("paid")
    if previous_paid is not None and paid is not None and paid > previous_paid + 1e-9:
        delta = paid - previous_paid
        emitted += enqueue(
            db,
            "payment_received",
            f"Paiement reçu sur {ticker} : +{delta:.8f} {ticker} (wallet {address}).",
        )
    threshold = current.get("threshold")
    if threshold and total is not None and total >= float(threshold):
        key = f"threshold:{wallet.id}"
        if not get_state(db, key).get("notified") or (previous_total is not None and previous_total < float(threshold)):
            emitted += enqueue(
                db,
                "pool_balance_threshold",
                f"Solde {ticker} au-dessus du seuil de paiement : {total:.8f} / {float(threshold):.8f} "
                f"{ticker} (wallet {address}).",
            )
            set_state(db, key, {"notified": True})
    return emitted


def new_price(db: Session, ticker: str, price_eur: float | None) -> list[int]:
    """First non-null price seen for a token that had none (listing / new price)."""
    key = f"price:{ticker}"
    state = get_state(db, key)
    if not state:
        set_state(db, key, {"eur": price_eur})
        return []
    previous = state.get("eur")
    if price_eur and previous in (None, 0):
        set_state(db, key, {"eur": price_eur})
        return enqueue(
            db, "new_listing", f"Nouveau prix pour {ticker} : {float(price_eur):.6f} EUR."
        )
    if price_eur:
        set_state(db, key, {"eur": price_eur})
    return []


def poll_sources(db: Session) -> list[int]:
    """Update-available and updater terminal states, without side effect on GETs."""
    emitted: list[int] = []
    try:
        from app import update_checker

        status = update_checker.status()
        latest = status.get("latest_version")
        if status.get("update_available") and latest:
            key = f"update:{latest}"
            if not get_state(db, key):
                emitted += enqueue(
                    db,
                    "update_available",
                    f"Nouvelle version disponible : {latest} "
                    f"(installée : {status.get('installed_version') or '?'}).",
                )
                set_state(db, key, {"notified": True})
    except Exception:  # pragma: no cover - status must never break the queue
        logger.warning("contrôle de version indisponible", exc_info=True)

    try:
        from app import updater_client

        status = updater_client.read_status() or {}
        current = status.get("state")
        last = get_state(db, "updater_state").get("state")
        if current in ("done", "rolled_back") and last in ACTIVE_UPDATE_STATES:
            if current == "done":
                emitted += enqueue(
                    db, "update_done", f"Mise à jour terminée : version {status.get('to') or '?'}."
                )
            else:
                emitted += enqueue(
                    db,
                    "update_rolled_back",
                    f"Mise à jour annulée, retour arrière vers {status.get('from') or '?'}.",
                )
        if current and current != last:
            set_state(db, "updater_state", {"state": current})
    except Exception:  # pragma: no cover - absent updater is normal
        pass
    return emitted


# ── Boucle de fond ─────────────────────────────────────────────────────
async def _loop() -> None:
    from app.database import SessionLocal

    while True:
        try:
            await process_queue()
            db = SessionLocal()
            try:
                poll_sources(db)
            finally:
                db.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("boucle de notifications en échec", exc_info=True)
        await asyncio.sleep(LOOP_INTERVAL)


def wake() -> None:
    """Best-effort immediate tick (the loop also polls every few seconds)."""
    if _task is None or _task.done():
        return
    try:
        loop = _task.get_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
    except RuntimeError:
        pass


def start() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.create_task(_loop())
    except RuntimeError:  # pragma: no cover - no running loop at import time
        _task = None


def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
