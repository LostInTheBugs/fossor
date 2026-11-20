"""Critères de Fred, regroupement des tokens, bandeau et binaires adoptés.

Les analyses de ``tests/fixtures/mining-analyses-anon.json`` viennent de la
vraie histoire de veille, **adresses retirées** (les IP sont fictives).  Le
garde-fou ``test_no_real_wallets`` reste la référence sur ce point.
"""

from __future__ import annotations

import hashlib
import json
import uuid as uuid_lib
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app import analyses as A
from app import criteria as CRIT
from app import installations as I
from app import rules as R
from app.database import SessionLocal
from app.ai_config import new_token
from app.models import Device, Machine, MinerProfile

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mining-analyses-anon.json"


def _cand(name: str, verdict: str = "surveillance", **extra) -> dict:
    payload = {"name": name, "verdict": verdict, **extra}
    return A.normalize_candidate(payload)


def _analyses(*groups):
    return [
        {"ts": f"2026-11-{index + 1:02d}T06:00:00+00:00", "run": f"tick {index + 1}", "candidates": candidates}
        for index, candidates in enumerate(groups)
    ]


def _names(rows) -> list[str]:
    return sorted(row["name"].lower() for row in rows)


# ── 1. Regroupement ───────────────────────────────────────────────────
def test_bigcoin_three_labels_one_token():
    rows = A.token_rows(
        _analyses(
            [_cand("BIG (BigCoin)", "retenu")],
            [_cand("BIGCOIN", "surveillance")],
            [_cand("bigcoin big", "rejete")],
        )
    )
    assert len(rows) == 1
    assert rows[0]["evaluations"] == 3
    assert rows[0]["verdict"] == "rejete"  # la plus récente


def test_grox_case_and_parens_one_token():
    rows = A.token_rows(_analyses([_cand("GROX")], [_cand("Grox (GROX)")], [_cand("grox")]))
    assert len(rows) == 1
    assert rows[0]["evaluations"] == 3


def test_nex_chain_ticker_and_paren_variants_one_token():
    rows = A.token_rows(
        _analyses(
            [_cand("NEX Chain (NEXC)", "rejeté")],
            [_cand("NEXC", "surveillance")],
            [_cand("NexChain (NEXC)", "retenu")],
        )
    )
    assert len(rows) == 1
    assert rows[0]["verdict"] == "retenu"
    assert rows[0]["evaluations"] == 3


def test_ksh_and_korsh_one_token():
    rows = A.token_rows(
        _analyses([_cand("KORSH", "surveillance")], [_cand("Korsh (KSH)", "retenu")], [_cand("KSH")])
    )
    assert len(rows) == 1
    assert rows[0]["evaluations"] == 3


def test_accented_rejected_counts_as_rejete():
    rows = A.token_rows(_analyses([_cand("ZED", "rejeté")], [_cand("ZED", "REJETÉ")]))
    assert len(rows) == 1
    assert rows[0]["verdict"] == "rejete"
    assert rows[0]["verdict_label"] == "rejeté"


def test_manual_alias_merge_and_split():
    analyses = _analyses([_cand("ALPHA"), _cand("BETA")])
    assert len(A.token_rows(analyses)) == 2
    merged = A.token_rows(analyses, [{"canonical": "alpha", "member": "beta", "mode": "merge"}])
    assert len(merged) == 1
    # L'heuristique rapproche le ticker BIG du nom « bigcoin big » ; un split
    # bloque ce lien direct et rend deux lignes.
    linked = _analyses([_cand("bigcoin big"), _cand("BIG", ticker="BIG")])
    assert len(A.token_rows(linked)) == 1
    split = A.token_rows(linked, [{"canonical": "big", "member": "bigcoinbig", "mode": "split"}])
    assert len(split) == 2


def test_anonymized_history_gets_near_mining_html():
    """334 candidats, 111 lignes dans mining.html : le regroupement doit y tomber."""
    document = json.loads(FIXTURE.read_text())
    entries = [A.normalize_analysis(item) for item in document["analyses"]]
    rows = A.token_rows(entries)
    evaluations = sum(row["evaluations"] for row in rows)
    assert evaluations == 334
    print(f"GROUPES={len(rows)} EVALUATIONS={evaluations}")
    # 93 < 111 : l'heuristique fusionne les vrais couples ticker ↔ nom que
    # mining.html comptait séparément (BIG/BigCoin, NEXC/NEX Chain, KSH/KORSH…).
    assert 90 <= len(rows) <= 110, len(rows)


# ── 2. Critères ───────────────────────────────────────────────────────
def test_criteria_rule_defaults_carry_freds_text():
    with SessionLocal() as db:
        R.seed_rules(db)
        rule = db.get(R.Rule, "criteria")
        assert rule is not None
        assert rule.scope == "selection"
        assert rule.enabled is True
        params = rule.params
        assert len(params["criteria"]) == 5
        assert params["criteria"][0]["key"] == "fenetre"
        assert params["criteria"][0]["linked_rule"] == "freshness"
        assert "Coin sorti depuis quelques heures" in params["criteria"][0]["text"]
        assert params["criteria"][4]["computed"] == "gpu_inventory"
        assert len(params["nuances"]) == 6
        assert len(params["bonuses"]) == 2
        assert len(params["pour"]) == 6
        assert len(params["contre"]) == 7


def test_gpu_criterion_uses_real_inventory():
    with SessionLocal() as db:
        assert CRIT.gpu_criterion(db, "RandomX")["verdict"] == "ko"
        machine = Machine(name="rig-gpu", host="10.0.0.9", ssh_port=22, ssh_user="tester", auth_type="fossor_key")
        db.add(machine)
        db.commit()
        db.refresh(machine)
        db.add(Device(uuid=str(uuid_lib.uuid4()), machine_id=machine.id, kind="gpu",
                      model="RTX 4080", vram_mb=16384, present=True))
        db.add(Device(uuid=str(uuid_lib.uuid4()), machine_id=machine.id, kind="gpu",
                      model="RTX 3070", vram_mb=8192, present=True))
        db.commit()
        inventory = CRIT.gpu_inventory(db)
        assert [entry["model"] for entry in inventory] == ["RTX 4080", "RTX 3070"]
        assert CRIT.gpu_criterion(db, "Ethash")["verdict"] == "ok"
        assert CRIT.gpu_criterion(db, "RandomX")["verdict"] == "na"
        assert CRIT.gpu_criterion(db, "AlgoMaison")["verdict"] == "warn"


async def test_context_serves_criteria_and_inventory(unauth_client):
    with SessionLocal() as db:
        token = new_token(db)["token"]
    response = await unauth_client.get("/api/hermes/context", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["criteria"]["rule_id"] == "criteria"
    assert [item["key"] for item in data["criteria"]["criteria"]][0] == "fenetre"
    gpu = [item for item in data["criteria"]["criteria"] if item["key"] == "gpu"][0]
    assert "inventory" in gpu and isinstance(gpu["evaluation"], dict)
    assert len(data["criteria"]["nuances"]) == 6


async def test_criteria_grid_is_stored_then_rendered(unauth_client, client):
    with SessionLocal() as db:
        token = new_token(db)["token"]
    payload = {
        "run": "veille-criteres",
        "action": "rien",
        "detail": "grille",
        "candidates": [
            {
                "ticker": "GRID",
                "name": "GridCoin",
                "algo": "Ethash",
                "verdict": "surveillance",
                "criteria": [
                    {"key": "fenetre", "verdict": "ok", "note": "sorti il y a 8 h"},
                    {"key": "gpu", "verdict": "warn", "note": "à confirmer"},
                    {"key": "utilite", "verdict": "pas-un-verdict", "note": ""},
                ],
            }
        ],
    }
    created = await unauth_client.post(
        "/api/hermes/analyses", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert created.status_code == 200, created.text
    stored = created.json()["analysis"]["candidates"][0]["criteria"]
    assert [item["verdict"] for item in stored] == ["ok", "warn", "na"]
    table = (await client.get("/api/analyses/tokens")).json()
    row = [item for item in table["tokens"] if (item["ticker"] or "").upper() == "GRID"][0]
    assert row["history"][0]["criteria"][0]["key"] == "fenetre"
    assert row["history"][0]["criteria"][0]["note"] == "sorti il y a 8 h"


def test_old_analyses_have_no_invented_criteria():
    rows = A.token_rows(_analyses([_cand("OLD", "retenu")]))
    assert rows[0]["history"][0]["criteria"] == []


# ── 3. Bandeau et auto_launch ─────────────────────────────────────────
async def test_banner_settings_reflect_armed_machine(client):
    with SessionLocal() as db:
        idle = Machine(name="simulee", host="10.0.0.8", ssh_port=22, ssh_user="tester", auth_type="fossor_key")
        armed = Machine(name="athlon-3000g", host="10.0.0.7", ssh_port=22, ssh_user="tester",
                        auth_type="fossor_key", simulation_disabled=True)
        db.add_all([idle, armed])
        db.commit()
    data = (await client.get("/api/miner/settings")).json()
    assert data["pilotage"]["armed"] == ["athlon-3000g"]
    assert data["pilotage"]["simulated"] == ["simulee"]
    assert data["pilotage"]["all_simulated"] is False
    assert data["pilotage"]["all_armed"] is False


async def test_banner_settings_all_simulated(client):
    with SessionLocal() as db:
        db.add(Machine(name="simulee", host="10.0.0.8", ssh_port=22, ssh_user="tester", auth_type="fossor_key"))
        db.commit()
    data = (await client.get("/api/miner/settings")).json()
    assert data["pilotage"]["all_simulated"] is True
    assert data["pilotage"]["armed"] == []


def test_auto_launch_is_gpu_and_cpu_but_disabled():
    with SessionLocal() as db:
        R.seed_rules(db)
        rule = db.get(R.Rule, "auto_launch")
        assert rule.enabled is False
        assert rule.params == {"gpu": True, "cpu": True}
        assert "GPU ET CPU" in rule.description


# ── 4. Binaires déjà installés ────────────────────────────────────────
GROWTH = b"\x7fELF" + b"fossor-binary" * 20
DIGEST = hashlib.sha256(GROWTH).hexdigest()


def _machine(db) -> Machine:
    machine = Machine(name=f"athlon-{uuid_lib.uuid4().hex[:4]}", host="10.0.0.7", ssh_port=22,
                      ssh_user="tester", auth_type="fossor_key")
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def _profile(db, sha: str = DIGEST) -> MinerProfile:
    profile = MinerProfile(
        uuid=str(uuid_lib.uuid4()), name=f"Profil {uuid_lib.uuid4().hex[:6]}", coin="TEST",
        wallet="big1qtestaddressfossor000000000000000", pool="stratum+tcp://pool.test:3333",
        software="custom", binary_url="", sha256=sha, binary_path="mineur", version="1.0.0",
        cmdline="mineur --wallet {wallet}", execution_mode="host", binary_source="fossor",
        binary_kind="elf",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


async def test_adopt_installed_binary_sha_ok(monkeypatch):
    calls = {}

    async def fake_sha(machine, path, actor="system"):
        calls["path"] = path
        return {"ok": True, "stdout": f"{DIGEST}  {path}\n"}

    monkeypatch.setattr(I, "ssh_binary_sha256", fake_sha)
    with SessionLocal() as db:
        machine = _machine(db)
        profile = _profile(db)
        result = await I.adopt(db, profile, machine)
        assert result["adopted"] is True
        assert result["sha256"] == DIGEST
        assert calls["path"] == "/opt/fossor/miners/custom/1.0.0/mineur"
        rows = I.installations(db, profile.id)
        assert rows and rows[0]["machine"] == machine.name


async def test_adopt_refuses_wrong_sha(monkeypatch):
    async def fake_sha(machine, path, actor="system"):
        return {"ok": True, "stdout": f"{'0' * 64}  {path}\n"}

    monkeypatch.setattr(I, "ssh_binary_sha256", fake_sha)
    with SessionLocal() as db:
        machine = _machine(db)
        profile = _profile(db)
        with pytest.raises(I.InstallError, match="SHA256 différent"):
            await I.adopt(db, profile, machine)
        assert I.installations(db, profile.id) == []


async def test_import_binary_from_machine_sha_ok(monkeypatch):
    async def fake_get(machine, remote_path, local_path):
        Path(local_path).write_bytes(GROWTH)
        return {"ok": True}

    monkeypatch.setattr(I, "ssh_get_file", fake_get)
    with SessionLocal() as db:
        machine = _machine(db)
        profile = _profile(db)
        result = await I.import_from_machine(db, profile, machine)
        assert result["imported"] is True
        assert result["kind"] == "elf"
        assert result["sha256"] == DIGEST
        db.refresh(profile)
        assert profile.binary_stored
        assert Path(profile.binary_stored).is_file()
        assert Path(profile.binary_stored).read_bytes() == GROWTH


async def test_import_binary_refuses_wrong_sha(monkeypatch):
    async def fake_get(machine, remote_path, local_path):
        Path(local_path).write_bytes(b"\x7fELF" + b"autre" * 30)
        return {"ok": True}

    monkeypatch.setattr(I, "ssh_get_file", fake_get)
    with SessionLocal() as db:
        machine = _machine(db)
        profile = _profile(db)
        with pytest.raises(I.InstallError, match="SHA256 différent"):
            await I.import_from_machine(db, profile, machine)
        db.refresh(profile)
        assert profile.binary_stored == ""
