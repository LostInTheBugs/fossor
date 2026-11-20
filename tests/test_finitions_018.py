"""2026.11.018 — verdicts « retenu », TTY SRBMiner, finitions.

Aucune connexion réelle : les machines sont des objets, le runner de power
limit est injecté et les dates viennent de la fixture de veille.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import analyses as A

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mining-analyses-anon.json"


def _cand(name: str, verdict: str, **extra) -> dict:
    return A.normalize_candidate({"name": name, "verdict": verdict, **extra})


# ── 1. Verdicts : horodatages comparés en datetime ───────────────────
def test_parse_ts_formats_melanges():
    assert A.parse_ts("2026-11-17T05:50Z") == datetime(2026, 11, 17, 5, 50, tzinfo=timezone.utc)
    assert A.parse_ts("2026-10-27T17:02+0100").utcoffset().total_seconds() == 3600
    assert A.parse_ts("17/11 05:50", default_year=2026) == datetime(
        2026, 11, 17, 5, 50, tzinfo=timezone.utc
    )
    assert A.parse_ts("pas une date") is None


def test_date_jour_mois_recente_gagne_sur_iso_ancienne():
    """« 17/11 05:50 » se classait avant « 2026-11-09… » : le retenu disparaissait."""
    rows = A.token_rows(
        [
            {"ts": "2026-11-09T12:59Z", "run": "vieux", "candidates": [_cand("NVL", "surveillance")]},
            {"ts": "17/11 05:50", "run": "récent", "candidates": [_cand("NVL", "retenu")]},
        ]
    )
    assert len(rows) == 1
    assert rows[0]["verdict"] == "retenu"
    assert rows[0]["last_ts"] == "17/11 05:50"


def test_date_iso_plus_recente_gagne_sur_jour_mois():
    rows = A.token_rows(
        [
            {"ts": "17/11 05:50", "candidates": [_cand("NVL", "retenu")]},
            {"ts": "2026-11-19T23:11Z", "candidates": [_cand("NVL", "surveillance")]},
        ]
    )
    assert rows[0]["verdict"] == "surveillance"
    assert rows[0]["last_ts"] == "2026-11-19T23:11Z"


def test_split_ticker_name_deux_conventions():
    assert A.split_ticker_name("NEB (Neural Engine Byte)") == ("NEB", "Neural Engine Byte")
    assert A.split_ticker_name("NIVAL (NVL)") == ("NVL", "NIVAL")
    assert A.split_ticker_name("BigCoin (BIG)") == ("BIG", "BigCoin")


def _fixture_rows() -> dict[str, dict]:
    entries = A.parse_document(json.loads(FIXTURE.read_text()))
    return {row["key"]: row for row in A.token_rows(entries)}


def test_fixture_nival_retenu():
    row = _fixture_rows()["nival"]
    assert row["verdict"] == "retenu"
    assert row["last_ts"] == "2026-11-17T05:50Z"
    assert (row["ticker"], row["name"]) == ("NVL", "NIVAL")


def test_fixture_big_et_opacra_expliques_par_plus_recent():
    rows = _fixture_rows()
    big = rows["big"]
    assert big["verdict"] == "surveillance"
    assert big["last_ts"] == "2026-11-17T17:14Z"  # plus récent que le retenu du 16/11
    assert any(h["verdict"] == "retenu" for h in big["history"])
    opa = rows["opa"]
    assert opa["verdict"] == "surveillance"
    assert opa["last_ts"] == "2026-11-15T11:26Z"  # plus récent que le retenu du 12/11
    assert any(h["verdict"] == "retenu" for h in opa["history"])


# ── 2. TTY pour SRBMiner ─────────────────────────────────────────────
def test_tty_wrapper_desactive_par_defaut():
    from app import miners as M
    from tests.test_miners import profile

    p = M.validate_profile(profile())
    assert p["tty_wrapper"] is False
    argv = M.render_argv(p, M.build_values(p, worker="rig", gpus=[0], api_port=21373))
    unit = M.render_unit(p, argv, user="fossor")
    assert "/usr/bin/script" not in unit
    assert f"ExecStart={M.exec_start(argv)}" in unit


def test_tty_wrapper_encadre_la_commande_et_echappe_wallet():
    from app import miners as M
    from tests.test_miners import profile

    wallet = "prl1+abc/def=ghi"
    p = M.validate_profile({**profile(), "wallet": wallet, "tty_wrapper": True})
    argv = M.render_argv(p, M.build_values(p, worker="rig", gpus=[0], api_port=21373))
    inner = M.shlex.join(argv)
    unit = M.render_unit(p, argv, user="fossor")
    line = next(row for row in unit.splitlines() if row.startswith("ExecStart="))
    assert line == f"ExecStart={M.exec_start(['/usr/bin/script', '-qfec', inner, '/dev/null'])}"
    assert wallet in inner  # +, / et = traversent intacts
    assert "/usr/bin/script" in line and '"-qfec"' in line and '"/dev/null"' in line


def test_wrapped_argv_refuse_une_commande_vide():
    from app import miners as M

    with pytest.raises(M.ProfileError):
        M.wrapped_argv([])


async def test_script_absent_bloque_avant_installation(monkeypatch):
    from app import miner_remote as MR

    seen: list[str] = []

    async def fake_run(machine, step):
        seen.append(step.title)
        return {
            "status": 0,
            "stdout": "TOOL:tar\nTOOL:sha256sum\nTOOL:systemctl\nMISSING:script\n",
            "stderr": "",
        }

    monkeypatch.setattr(MR, "_run", fake_run)
    from tests.test_miners import profile as base

    profile = {**base(), "tty_wrapper": True, "sha256": "a" * 64, "binary_source": "url"}
    with pytest.raises(MR.MinerError, match="script"):
        await MR.start(object(), profile, ["/bin/true"], simulated=False, user="fossor")
    assert "empreinte de l'archive" not in seen


# ── 3. Finitions ─────────────────────────────────────────────────────
def test_migration_renomme_ryen_une_seule_fois():
    from app.database import SessionLocal, init_db
    from app.models import Machine

    with SessionLocal() as db:
        db.add(Machine(name="Ryen-5-3600", host="192.0.2.55"))
        db.add(Machine(name="Ryen-5-3600 bis", host="192.0.2.56"))
        db.commit()
    init_db()
    init_db()
    with SessionLocal() as db:
        names = sorted(row.name for row in db.query(Machine).all())
    assert names == ["Ryen-5-3600 bis", "Ryzen-5-3600"]


def test_colonne_tty_wrapper_creee():
    from app.database import engine

    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(miner_profiles)")}
    assert "tty_wrapper" in columns


def test_collecte_realigne_vm_host():
    from app import collector as C

    class FakeMachine:
        vm_host = False

    class FakeDB:
        def add(self, *_args):
            pass

        def commit(self):
            pass

    machine = FakeMachine()
    kvm = "### CPU\nx\n### VIRT\nKVM=1\nVIRSH=1\nKVM_DOMAIN=1\n### OS\ny\n"
    assert C.apply_virt(FakeDB(), machine, kvm) is True
    assert machine.vm_host is True
    assert C.apply_virt(FakeDB(), machine, kvm) is False  # rien ne change
    emule = "### VIRT\nKVM=1\nVIRSH=1\nKVM_DOMAIN=0\n"
    assert C.apply_virt(FakeDB(), machine, emule) is True
    assert machine.vm_host is False


async def test_restore_remet_le_power_limit_par_defaut(monkeypatch):
    from app import rule_runner as RR
    from app.database import SessionLocal
    from app.models import Device, EnergyRuleEvent, Machine

    with SessionLocal() as db:
        machine = Machine(name="Rig", host="192.0.2.10")
        db.add(machine)
        db.commit()
        db.refresh(machine)
        device = Device(machine_id=machine.id, uuid=str(uuid.uuid4()), kind="gpu", index=2, model="RTX")
        db.add(device)
        db.commit()
        device_uuid = device.uuid
        db.add(
            EnergyRuleEvent(
                ts="2026-11-20T12:00:00+00:00",
                kind="action",
                machine_id=machine.id,
                device_uuid=device_uuid,
                detail="power limit GPU 2 → 120 W",
                simulated=False,
            )
        )
        db.commit()

        calls: list[tuple[int, float]] = []

        async def fake_limits(_machine):
            return {2: {"min": 100, "max": 300, "default": 250}}

        async def fake_runner(_machine, index, watts):
            calls.append((index, watts))
            return {"ok": True}

        monkeypatch.setattr(RR, "read_limits", fake_limits)
        first = await RR.restore(db, runner=fake_runner)
        second = await RR.restore(db, runner=fake_runner)

    assert calls == [(2, 250.0)]
    assert first["restored"] == [
        {"machine_id": first["restored"][0]["machine_id"], "device_uuid": device_uuid, "watts": 250.0}
    ]
    assert second["restored"] == []


async def test_evaluation_au_prix_bas_appelle_restore(monkeypatch):
    from app import energy, energy_rules as ER, rule_runner as RR
    from app.database import SessionLocal

    monkeypatch.setattr(ER, "get_rule", lambda db: {"enabled": True, "mode": "stop"})
    monkeypatch.setattr(energy, "current_price", lambda db, moment: {"price_eur_kwh": 0.05})
    monkeypatch.setattr(
        energy, "build_report", lambda db, period, quotes=None, now=None: {"machines": []}
    )
    monkeypatch.setattr(
        ER,
        "decide",
        lambda rule, price_eur_kwh, machines: {"active": False, "machines": [], "simulated": False},
    )
    calls: list[dict] = []

    async def fake_restore(db, **kwargs):
        calls.append(kwargs)
        return {"restored": [{"machine_id": 7}]}

    monkeypatch.setattr(RR, "restore", fake_restore)
    with SessionLocal() as db:
        out = await RR.evaluate(db, simulate=False)
    assert calls
    assert out["restored"] == [{"machine_id": 7}]


def test_age_relatif_du_dernier_evenement():
    from app.parite import event_entry, relative_age

    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    assert relative_age("2026-11-17T09:00:00+00:00", now) == "il y a 3 j"
    assert relative_age("2026-11-20T10:00:00+00:00", now) == "il y a 2 h"
    assert relative_age("2026-11-20T11:55:00+00:00", now) == "il y a 5 min"
    entry = event_entry("2026-11-17T09:00:00+00:00", "BIG", "fin", "x", "fossor", now)
    assert entry["age"] == "il y a 3 j"
