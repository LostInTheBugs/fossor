"""Coffre des wallets — dérivation, scellement, verrouillage, non-fuite.

Aucune valeur ici n'est une vraie seed, clé ou adresse : les adresses sont
fabriquées de zéro (charge utile factice) et les secrets portent un jeton
aléatoire propre à chaque test pour être cherchés dans toutes les sorties.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PASS = "coffre-test-phrase-2026"
NEW_PASS = "coffre-test-phrase-2027"


@pytest.fixture(autouse=True)
def _reset_vault_session():
    from app import vault

    vault.lock()
    yield
    vault.lock()


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _secret(prefix: str = "FAKE-SEED") -> str:
    return f"{prefix}-{uuid.uuid4().hex}-DO-NOT-LEAK"


# ── Adresses factices, fabriquées de zéro ───────────────────────────────────
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _convertbits(data: bytes) -> list[int]:
    acc = 0
    bits = 0
    out = []
    for value in data:
        acc = (acc << 8) | value
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def _fake_bech32(hrp: str = "bc") -> str:
    data = [0] + _convertbits(bytes([7] * 20))
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = _polymod(expanded + data + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_base58() -> str:
    payload = b"\x00" + bytes([9] * 20)
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + checksum
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


# ── Aides HTTP ──────────────────────────────────────────────────────────────
async def _init(client: AsyncClient, passphrase: str = PASS, lock_minutes: int = 10):
    response = await client.post(
        "/api/vault/init",
        json={"passphrase": passphrase, "confirm": passphrase, "lock_minutes": lock_minutes},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _add(client: AsyncClient, **overrides):
    payload = {
        "coin": "BTC",
        "network": "mainnet",
        "address": _fake_bech32(),
        "machine": "rig-test",
        "wallet_path": "/tmp/fake-wallet.dat",
        "note": "wallet de test",
        "secrets": {"seed": _secret(), "password": _secret("FAKE-PASS")},
    }
    payload.update(overrides)
    response = await client.post("/api/vault/wallets", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _token(db) -> str:
    from app.ai_config import new_token

    return new_token(db)["token"]


def _seal(public_key_b64: str, plaintext: str) -> str:
    from nacl.public import PublicKey, SealedBox

    box = SealedBox(PublicKey(base64.b64decode(public_key_b64)))
    return base64.b64encode(box.encrypt(plaintext.encode())).decode()


def _client(ip: str = "127.0.0.1") -> AsyncClient:
    from app.main import app as application

    return AsyncClient(
        transport=ASGITransport(app=application, client=(ip, 1234)),
        base_url="http://test",
    )


# ── Dérivation, chiffrement, verrouillage ──────────────────────────────────
@pytest.mark.asyncio
async def test_init_unlock_lock_roundtrip(client):
    state = await _init(client)
    assert state["initialized"] is True
    assert state["locked"] is False
    assert state["kdf"]["algorithm"] == "argon2id"
    assert state["kdf"]["memory_kib"] == 65536

    locked = await client.post("/api/vault/lock")
    assert locked.status_code == 200
    assert locked.json()["locked"] is True

    wrong = await client.post("/api/vault/unlock", json={"passphrase": "mauvaise-phrase-000"})
    assert wrong.status_code == 401
    right = await client.post("/api/vault/unlock", json={"passphrase": PASS})
    assert right.status_code == 200
    assert right.json()["locked"] is False
    assert right.json()["unlock_remaining"] > 0


@pytest.mark.asyncio
async def test_second_init_refused_and_short_passphrase(client):
    await _init(client)
    again = await client.post(
        "/api/vault/init", json={"passphrase": PASS, "confirm": PASS}
    )
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_passphrase_and_confirmation_must_match(client):
    response = await client.post(
        "/api/vault/init", json={"passphrase": PASS, "confirm": PASS + "x"}
    )
    assert response.status_code == 400
    short = await client.post(
        "/api/vault/init", json={"passphrase": "court", "confirm": "court"}
    )
    assert short.status_code == 400


@pytest.mark.asyncio
async def test_session_expires_after_delay(client, monkeypatch):
    monkeypatch.setenv("FOSSOR_VAULT_LOCK_SECONDS", "1")
    await _init(client)
    assert (await client.get("/api/vault/status")).json()["locked"] is False
    time.sleep(1.15)
    state = (await client.get("/api/vault/status")).json()
    assert state["locked"] is True
    assert state["unlock_remaining"] == 0


@pytest.mark.asyncio
async def test_five_failures_block_fifteen_minutes_and_audit(client, db):
    await _init(client)
    await client.post("/api/vault/lock")
    codes = []
    for _ in range(5):
        response = await client.post("/api/vault/unlock", json={"passphrase": "fausse-phrase-000"})
        codes.append(response.status_code)
    assert codes == [401, 401, 401, 401, 423]
    blocked = (await client.get("/api/vault/status")).json()
    assert blocked["blocked_until"] is not None
    assert blocked["locked"] is True
    # Même la bonne phrase est refusée pendant le blocage.
    refused = await client.post("/api/vault/unlock", json={"passphrase": PASS})
    assert refused.status_code == 423
    from app.models import AuditLog

    rows = db.query(AuditLog).filter(AuditLog.action == "vault-unlock-failed").all()
    assert len(rows) == 5
    assert all("fausse-phrase" not in (row.output or "") for row in rows)


@pytest.mark.asyncio
async def test_vault_ignores_trusted_network_bypass(db):
    from app import auth_settings

    auth_settings.write_settings(
        db, {"auth_enabled": False, "trusted_networks": ["127.0.0.1"]}
    )
    async with _client("127.0.0.1") as ac:
        machines = await ac.get("/api/machines")
        vault_state = await ac.get("/api/vault/status")
    assert machines.status_code == 200  # le contournement s'applique au reste
    assert vault_state.status_code == 401
    assert "réseau de confiance" in vault_state.json()["detail"]

    async with _client("127.0.0.1") as ac:
        login = await ac.post("/api/login", data={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        after = await ac.get("/api/vault/status")
    assert after.status_code == 200


# ── Dépôt scellé Hermes : écrire sans relire ───────────────────────────────
@pytest.mark.asyncio
async def test_hermes_deposit_is_sealed_and_never_readable(client, db):
    await _init(client)
    token = _token(db)
    headers = {"Authorization": f"Bearer {token}"}
    pub = await client.get("/api/hermes/wallets/pubkey", headers=headers)
    assert pub.status_code == 200
    assert pub.json()["algorithm"] == "x25519"
    public_key = pub.json()["public_key"]

    seed = _secret("HERMES-SEED")
    sealed = _seal(public_key, seed)
    deposit = await client.post(
        "/api/hermes/wallets",
        headers=headers,
        json={
            "coin": "BTC",
            "network": "mainnet",
            "address": _fake_bech32(),
            "machine": "rig-hermes",
            "wallet_path": "/tmp/hermes.dat",
            "note": "déposé par Hermes",
            "secrets": {"seed": sealed},
        },
    )
    assert deposit.status_code == 201, deposit.text
    body = deposit.json()
    assert body["readable_by_api"] is False
    assert "value" not in body and "seed" not in body
    assert seed not in deposit.text
    assert sealed not in deposit.text

    # Aucune route de lecture Hermes.
    listing = await client.get("/api/hermes/wallets", headers=headers)
    assert listing.status_code in (404, 405)
    assert seed not in listing.text

    # Le coffre déverrouillé, lui, ouvre l'entrée avec la phrase.
    wallets = (await client.get("/api/vault/wallets")).json()["wallets"]
    assert wallets[0]["has_seed"] is True
    reveal = await client.post(
        f"/api/vault/wallets/{wallets[0]['id']}/reveal",
        json={"field": "seed", "passphrase": PASS},
    )
    assert reveal.status_code == 200
    assert reveal.json()["value"] == seed

    # Déverrouillé mais phrase fausse : refus, et rien dans le message.
    bad = await client.post(
        f"/api/vault/wallets/{wallets[0]['id']}/reveal",
        json={"field": "seed", "passphrase": "autre-phrase-fausse"},
    )
    assert bad.status_code == 401
    assert seed not in bad.text


@pytest.mark.asyncio
async def test_hermes_deposit_requires_token_and_flags_bad_address(client, db):
    await _init(client)
    token = _token(db)
    headers = {"Authorization": f"Bearer {token}"}
    public_key = (
        await client.get("/api/hermes/wallets/pubkey", headers=headers)
    ).json()["public_key"]
    sealed = _seal(public_key, _secret("HERMES-SEED"))

    anonymous = await client.post("/api/hermes/wallets", json={"coin": "BTC", "address": "x"})
    assert anonymous.status_code == 401

    bad = await client.post(
        "/api/hermes/wallets",
        headers=headers,
        json={
            "coin": "BTC",
            "address": "bc1qcetteadresseestfausse0000000000",
            "sealed_secret": sealed,
            "secret_kind": "seed",
        },
    )
    assert bad.status_code == 201
    assert bad.json()["address_status"] == "invalid"
    assert bad.json()["warning"]


@pytest.mark.asyncio
async def test_hermes_deposit_requires_initialized_vault(client, db):
    token = _token(db)
    response = await client.post(
        "/api/hermes/wallets",
        headers={"Authorization": f"Bearer {token}"},
        json={"coin": "BTC", "address": _fake_bech32(), "sealed_secret": "A" * 80,
              "secret_kind": "seed"},
    )
    assert response.status_code == 409


# ── Absence de secret partout ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_secret_in_api_export_backup_logs_audit_or_database(client, db, caplog):
    caplog.set_level(logging.DEBUG)
    await _init(client)
    seed = _secret("LEAK-SEED")
    password = _secret("LEAK-PASS")
    added = await _add(client, secrets={"seed": seed, "private_key": _secret("LEAK-PRIV"),
                                        "password": password})
    assert added["has_seed"] and added["has_private_key"] and added["has_password"]

    from app import config_io, observability

    # Réponses d'API (hors « Révéler »).
    for path in ("/api/vault/status", "/api/vault/wallets"):
        response = await client.get(path)
        assert seed not in response.text
        assert password not in response.text
    state = await client.get("/api/vault/status")
    assert seed not in state.text

    # Export de configuration, même en incluant ses secrets chiffrés.
    exported = json.dumps(
        config_io.build_export(db, include_secrets=True, passphrase="phrase-export-test"),
        ensure_ascii=False,
    )
    assert seed not in exported and password not in exported
    assert "vault" not in exported.lower()

    # Audit.
    from app.models import AuditLog

    for row in db.query(AuditLog).all():
        blob = " ".join(str(x or "") for x in (row.action, row.object_ref, row.result, row.output))
        assert seed not in blob and password not in blob

    # Journal applicatif.
    assert seed not in caplog.text and password not in caplog.text

    # Base et sauvegarde : le chiffré seulement.
    import app.database as database

    db_path = Path(database.engine.url.database)
    db_bytes = db_path.read_bytes()
    assert seed.encode() not in db_bytes and password.encode() not in db_bytes
    result = observability.run_backup()
    backup_dir = Path(os.environ["FOSSOR_DATA_DIR"]) / "backups"
    assert result.get("ok") is not False
    for backup in backup_dir.rglob("*"):
        if backup.is_file():
            blob = backup.read_bytes()
            assert seed.encode() not in blob and password.encode() not in blob


# ── Révélation temporaire, altération, cycle de vie ────────────────────────
@pytest.mark.asyncio
async def test_reveal_requires_unlock_and_fresh_passphrase(client, monkeypatch):
    monkeypatch.setenv("FOSSOR_VAULT_REVEAL_SECONDS", "1")
    await _init(client)
    seed = _secret("REVEAL-SEED")
    wallet = await _add(client, secrets={"seed": seed})
    await client.post("/api/vault/lock")

    locked = await client.post(
        f"/api/vault/wallets/{wallet['id']}/reveal",
        json={"field": "seed", "passphrase": PASS},
    )
    assert locked.status_code == 423

    await client.post("/api/vault/unlock", json={"passphrase": PASS})
    wrong = await client.post(
        f"/api/vault/wallets/{wallet['id']}/reveal",
        json={"field": "seed", "passphrase": "pas-la-bonne-phrase"},
    )
    assert wrong.status_code == 401
    assert seed not in wrong.text

    good = await client.post(
        f"/api/vault/wallets/{wallet['id']}/reveal",
        json={"field": "seed", "passphrase": PASS},
    )
    assert good.status_code == 200
    assert good.json()["value"] == seed
    assert good.json()["expires_in"] == 1


@pytest.mark.asyncio
async def test_tampered_ciphertext_is_refused(client, db):
    await _init(client)
    seed = _secret("TAMPER-SEED")
    wallet = await _add(client, secrets={"seed": seed})
    from app.models import VaultWallet

    row = db.get(VaultWallet, wallet["id"])
    sealed = row.sealed_seed
    changed = ("B" if sealed[10] != "B" else "C")
    row.sealed_seed = sealed[:10] + changed + sealed[11:]
    db.commit()

    response = await client.post(
        f"/api/vault/wallets/{wallet['id']}/reveal",
        json={"field": "seed", "passphrase": PASS},
    )
    assert response.status_code == 400
    assert "illisible" in response.json()["detail"].lower()
    assert seed not in response.text


@pytest.mark.asyncio
async def test_update_and_delete_wallet(client):
    await _init(client)
    wallet = await _add(client)
    updated = await client.put(
        f"/api/vault/wallets/{wallet['id']}",
        json={"note": "note modifiée", "secrets": {"password": _secret("NEW-PASS")}},
    )
    assert updated.status_code == 200
    assert updated.json()["note"] == "note modifiée"
    assert updated.json()["has_password"] is True
    deleted = await client.delete(f"/api/vault/wallets/{wallet['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/vault/wallets")).json()["wallets"] == []


@pytest.mark.asyncio
async def test_wallets_require_unlock_to_add(client):
    await _init(client)
    await client.post("/api/vault/lock")
    response = await client.post(
        "/api/vault/wallets", json={"coin": "BTC", "address": _fake_bech32()}
    )
    assert response.status_code == 423


# ── Changement de phrase : re-chiffrement complet ──────────────────────────
@pytest.mark.asyncio
async def test_change_passphrase_reencrypts_everything(client, db):
    await _init(client)
    seed = _secret("CHANGE-SEED")
    wallet = await _add(client, secrets={"seed": seed})
    from app.models import VaultWallet

    before = db.get(VaultWallet, wallet["id"]).sealed_seed
    pub_before = (await client.get("/api/vault/status")).json()

    changed = await client.post(
        "/api/vault/passphrase",
        json={"current": PASS, "new": NEW_PASS, "confirm": NEW_PASS},
    )
    assert changed.status_code == 200, changed.text

    db.expire_all()
    after = db.get(VaultWallet, wallet["id"]).sealed_seed
    assert after != before

    # Verrouillage : l'ancienne phrase ne rouvre plus, la nouvelle oui.
    await client.post("/api/vault/lock")
    assert (await client.post("/api/vault/unlock", json={"passphrase": PASS})).status_code == 401
    assert (
        await client.post("/api/vault/unlock", json={"passphrase": NEW_PASS})
    ).status_code == 200
    reveal = await client.post(
        f"/api/vault/wallets/{wallet['id']}/reveal",
        json={"field": "seed", "passphrase": NEW_PASS},
    )
    assert reveal.status_code == 200
    assert reveal.json()["value"] == seed
    assert pub_before["kdf"]["algorithm"] == "argon2id"


# ── Export/import ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_export_is_encrypted_and_requires_unlocked_vault(client):
    await _init(client)
    seed = _secret("EXPORT-SEED")
    await _add(client, secrets={"seed": seed})
    await client.post("/api/vault/lock")
    assert (await client.post("/api/vault/export", json={"passphrase": PASS})).status_code == 423

    await client.post("/api/vault/unlock", json={"passphrase": PASS})
    bad = await client.post("/api/vault/export", json={"passphrase": "fausse-phrase-export"})
    assert bad.status_code == 401
    assert seed not in bad.text

    good = await client.post("/api/vault/export", json={"passphrase": PASS})
    assert good.status_code == 200
    assert seed.encode() not in good.content
    assert b'"seed"' not in good.content
    from app import vault

    document = vault.decrypt_export(good.content, PASS)
    assert document["entries"][0]["secrets"]["seed"] == seed


@pytest.mark.asyncio
async def test_registre_import_keeps_metadata_only(client, db):
    await _init(client)
    marker = _secret("REGISTRE-PASSWORD")
    text = (
        "| Date | Coin | Réseau | Adresse | Logiciel | Chemin | Mot de passe |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| 2026-11-04 | MWC | mainnet | mwwFAKEaddress0001 | midwimble | /home/x/w.mww | {marker} |\n"
    )
    preview = await client.post(
        "/api/vault/import/registre", json={"text": text, "dry_run": True}
    )
    assert preview.status_code == 200
    assert preview.json()["count"] == 1
    assert preview.json()["ignored_secret_fields"] >= 1
    assert marker not in preview.text

    applied = await client.post(
        "/api/vault/import/registre", json={"text": text, "dry_run": False}
    )
    assert applied.status_code == 200
    assert applied.json()["count"] == 1
    assert marker not in applied.text

    from app.models import VaultWallet

    row = db.query(VaultWallet).one()
    assert row.coin == "MWC"
    assert row.address == "mwwFAKEaddress0001"
    assert row.wallet_path == "/home/x/w.mww"
    assert row.origin == "import"
    assert row.sealed_seed is None
    assert row.sealed_private_key is None
    assert row.sealed_password is None
    assert marker not in (row.note or "")


# ── Adresses ───────────────────────────────────────────────────────────────
def test_address_checksums():
    from app import vault

    assert vault.verify_address("BTC", _fake_bech32())[0] == "ok"
    assert vault.verify_address("BTC", _fake_base58())[0] == "ok"
    assert vault.verify_address("BTC", _fake_bech32()[:-1] + "q")[0] == "invalid"
    assert vault.verify_address("BTC", "1BoatZZZinvalide")[0] == "invalid"
    assert vault.verify_address("XMR", "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A")[0] == "unverified"


@pytest.mark.asyncio
async def test_invalid_address_is_visible_in_list(client):
    await _init(client)
    wallet = await _add(client, address="bc1qcetteadresseestfausse0000000000")
    assert wallet["address_status"] == "invalid"
    assert wallet["address_note"]
    listing = await client.get("/api/vault/wallets")
    assert listing.json()["wallets"][0]["address_status"] == "invalid"
