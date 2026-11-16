"""§4.14 — notifications: matrix, quiet hours, debounce, fake servers only.

No real Discord, Telegram or SMTP server is contacted: ``notifications.TRANSPORT``
is replaced by an ``httpx.MockTransport`` and the SMTP test uses a local
``aiosmtpd`` fixture.
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

WEBHOOK = "https://discord.example/api/webhooks/123/SECRET-WEBHOOK-TOKEN"
TELEGRAM_TOKEN = "123456:SECRET-TELEGRAM-TOKEN"
SMTP_PASSWORD = "SECRET-SMTP-PASSWORD"


def _db():
    from app.database import SessionLocal

    return SessionLocal()


# Documentation-style fake address: never a real wallet, never a real prefix.
FAKE_WALLET_ADDRESS = "prl1demo" + "0000abcdefghijklmnopqrstuvwxyz"
FAKE_WALLET_MASKED = "prl1de…wxyz"


class FakeWallet:
    id = 7
    coin_ticker = "PRL"
    address = FAKE_WALLET_ADDRESS


def test_quiet_hours_cross_midnight():
    from app import notifications

    config = notifications.default_config()
    config["quiet_hours"].update(
        {"enabled": True, "start": "22:00", "end": "07:00", "tz": "Europe/Paris"}
    )
    paris = ZoneInfo("Europe/Paris")

    def at(hour, minute):
        return datetime(2026, 11, 16, hour, minute, tzinfo=paris).astimezone(timezone.utc)

    assert notifications.in_quiet_hours(config, at(23, 30)) is True
    assert notifications.in_quiet_hours(config, at(0, 0)) is True
    assert notifications.in_quiet_hours(config, at(2, 0)) is True
    assert notifications.in_quiet_hours(config, at(6, 59)) is True
    assert notifications.in_quiet_hours(config, at(7, 0)) is False
    assert notifications.in_quiet_hours(config, at(12, 0)) is False
    assert notifications.in_quiet_hours(config, at(21, 59)) is False
    config["quiet_hours"]["enabled"] = False
    assert notifications.in_quiet_hours(config, at(23, 30)) is False


async def test_secrets_never_returned(client):
    r = await client.put(
        "/api/notifications",
        json={
            "channels": {
                "discord": {"enabled": True, "webhook": WEBHOOK},
                "telegram": {"enabled": True, "token": TELEGRAM_TOKEN, "chat_id": "42"},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channels"]["discord"]["has_webhook"] is True
    assert body["channels"]["telegram"]["has_token"] is True
    assert WEBHOOK not in json.dumps(body)
    assert TELEGRAM_TOKEN not in json.dumps(body)
    again = await client.get("/api/notifications")
    assert WEBHOOK not in json.dumps(again.json())
    assert "has_webhook" in again.json()["channels"]["discord"]


async def test_discord_test_message_hits_the_fake_server(client, monkeypatch):
    from app import notifications

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(notifications, "TRANSPORT", httpx.MockTransport(handler))
    await client.put(
        "/api/notifications",
        json={"channels": {"discord": {"enabled": True, "webhook": WEBHOOK}}},
    )
    r = await client.post("/api/notifications/test/discord")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == WEBHOOK
    payload = json.loads(request.content)
    assert "Test de notification" in payload["content"]
    assert "SECRET-WEBHOOK-TOKEN" not in payload["content"]

    entries = (await client.get("/api/notifications/log")).json()["entries"]
    assert entries[0]["channel"] == "discord"
    assert entries[0]["status"] == "sent"
    assert "SECRET-WEBHOOK-TOKEN" not in json.dumps(entries)


async def test_telegram_test_message_hits_the_fake_server(client, monkeypatch):
    from app import notifications

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(notifications, "TRANSPORT", httpx.MockTransport(handler))
    await client.put(
        "/api/notifications",
        json={
            "channels": {
                "telegram": {"enabled": True, "token": TELEGRAM_TOKEN, "chat_id": "4242"}
            }
        },
    )
    r = await client.post("/api/notifications/test/telegram")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["chat_id"] == "4242"
    entries = (await client.get("/api/notifications/log")).json()["entries"]
    assert entries[0]["status"] == "sent"
    assert "SECRET-TELEGRAM-TOKEN" not in json.dumps(entries)


async def test_email_test_message_with_local_smtp(client):
    from aiosmtpd.controller import Controller

    received: list[str] = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope.content.decode())
            return "250 Message accepted for delivery"

    controller = Controller(Handler(), hostname="127.0.0.1", port=18025)
    controller.start()
    try:
        r = await client.put(
            "/api/notifications",
            json={
                "channels": {
                    "email": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 18025,
                        "security": "none",
                        "username": "",
                        "from": "fossor@example.test",
                        "to": "ops@example.test",
                        "password": SMTP_PASSWORD,
                    }
                }
            },
        )
        assert r.status_code == 200, r.text
        test = await client.post("/api/notifications/test/email")
    finally:
        controller.stop()

    assert test.status_code == 200, test.text
    assert test.json()["ok"] is True, test.json()
    assert len(received) == 1
    assert "Test de notification" in received[0]
    assert "ops@example.test" in received[0]
    entries = (await client.get("/api/notifications/log")).json()["entries"]
    assert entries[0]["status"] == "sent"
    assert SMTP_PASSWORD not in json.dumps(entries)


async def test_offline_debounce_two_collections_then_recovery(client):
    from app import notifications
    from app.models import Machine

    await client.put(
        "/api/notifications",
        json={
            "matrix": {
                "machine_offline": ["discord"],
                "machine_online": ["discord"],
            }
        },
    )
    db = _db()
    machine = Machine(name="rig-test", host="192.0.2.10")
    db.add(machine)
    db.commit()
    try:
        # First failed collection: nothing yet (anti-rebond).
        assert notifications.machine_result(db, machine, online=False, error="timeout") == []
        # Second failed collection: exactly one alert.
        first = notifications.machine_result(db, machine, online=False, error="timeout")
        assert len(first) == 1
        # Third: no repeat.
        assert notifications.machine_result(db, machine, online=False, error="timeout") == []
        # Back online: exactly one recovery.
        back = notifications.machine_result(db, machine, online=True)
        assert len(back) == 1
    finally:
        db.close()

    entries = (await client.get("/api/notifications/log")).json()["entries"]
    events = [entry["event"] for entry in entries]
    assert events.count("machine_offline") == 1
    assert events.count("machine_online") == 1


async def test_quiet_hours_skip_info_but_pass_critical(client, monkeypatch):
    from app import notifications

    await client.put(
        "/api/notifications",
        json={
            "matrix": {
                "machine_offline": ["discord"],
                "update_rolled_back": ["discord"],
            },
            "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"},
        },
    )
    monkeypatch.setattr(notifications, "in_quiet_hours", lambda config, now=None: True)
    db = _db()
    try:
        notifications.enqueue(db, "machine_offline", "test quiet", channels=["discord"])
        notifications.enqueue(db, "update_rolled_back", "test critical", channels=["discord"])
        rows = {
            entry["event"]: entry["status"]
            for entry in notifications.list_log(db, limit=10)
        }
    finally:
        db.close()
    assert rows["machine_offline"] == "skipped"
    assert rows["update_rolled_back"] == "pending"


async def test_payment_and_threshold_events_mask_the_wallet(client):
    from app import notifications

    await client.put(
        "/api/notifications",
        json={
            "matrix": {
                "payment_received": ["discord"],
                "pool_balance_threshold": ["discord"],
            }
        },
    )
    db = _db()
    try:
        ids = notifications.pool_sample(
            db,
            FakeWallet(),
            previous_paid=1.0,
            previous_total=4.0,
            current={"paid": 2.5, "threshold": 5.0},
            total=6.5,
        )
        assert len(ids) == 2
        entries = notifications.list_log(db, limit=10)
        dumped = json.dumps(entries, ensure_ascii=False)
        assert FAKE_WALLET_ADDRESS not in dumped
        assert FAKE_WALLET_MASKED in dumped
        # No threshold duplicate while the balance stays above it.
        assert notifications.pool_sample(
            db, FakeWallet(), 2.5, 6.6, {"paid": 2.5, "threshold": 5.0}, 6.7
        ) == []
    finally:
        db.close()


async def test_new_price_emits_once_when_first_seen(client):
    from app import notifications

    await client.put("/api/notifications", json={"matrix": {"new_listing": ["discord"]}})
    db = _db()
    try:
        # First observation only records the baseline (no spam on upgrade).
        assert notifications.new_price(db, "QTC", None) == []
        emitted = notifications.new_price(db, "QTC", 0.42)
        assert len(emitted) == 1
        assert notifications.new_price(db, "QTC", 0.43) == []
    finally:
        db.close()
    entries = (await client.get("/api/notifications/log")).json()["entries"]
    assert [e["event"] for e in entries].count("new_listing") == 1
    assert "0.42" in entries[0]["body"]
