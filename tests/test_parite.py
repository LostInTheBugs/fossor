"""Parité avec ``mining.html`` — agrégation des tokens, imports, tableau de bord.

Aucun test ici ne contacte un rig : l'import des campagnes travaille sur les
fixtures anonymisées, et la lecture du solde solo (``getbalance``) est validée
par le parseur et la commande en liste blanche, pas par un SSH réel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import analyses as A
from app import legacy_import as L
from app import parite as P
from app import rules as R

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _analyses() -> list[dict]:
    return A.parse_document(_load("parite-analyses.json"))


# ── 1. Agrégation par token ──────────────────────────────────────────────
def test_token_rows_aggregate_by_token():
    rows = A.token_rows(_analyses())
    assert [row["ticker"] for row in rows] == ["NOVA", "BRK"] or {row["ticker"] for row in rows} == {"NOVA", "BRK"}
    nova = next(row for row in rows if row["ticker"] == "NOVA")
    assert nova["evaluations"] == 2
    assert nova["verdict"] == "retenu"
    assert nova["first_ts"] == "2026-11-01T06:00Z"
    assert nova["last_ts"] == "2026-11-03T18:00Z"
    assert nova["history"][0]["ts"] == "2026-11-03T18:00Z"  # le plus récent d'abord
    assert nova["pour"] == ["p3", "p4"]
    assert nova["contre"] == ["c3"]
    assert nova["age"] == "~4j"
    assert nova["links"] == ["https://example.invalid/ann"]
    brk = next(row for row in rows if row["ticker"] == "BRK")
    assert brk["algo"] == "Yespower"
    assert len(brk["links"]) == 2


def test_token_import_accepts_both_key_forms():
    entries = _analyses()
    first = entries[0]["candidates"]
    nova = next(item for item in first if item["ticker"] == "NOVA")
    brk = next(item for item in first if item["ticker"] == "BRK")
    # forme française : nom/lien
    assert nova["name"] == "Nova"
    assert nova["links"] == ["https://example.invalid/ann"]
    # forme anglaise : name/links
    assert brk["name"] == "Brique"
    assert "https://github.com/example/brique" in brk["links"]


def test_verdict_catalog_and_filters():
    rows = A.token_rows(_analyses())
    catalog = {entry["value"]: entry for entry in A.verdict_catalog(rows)}
    assert catalog["retenu"]["count"] == 1
    assert catalog["surveillance"]["count"] == 1
    assert catalog["rejete"]["count"] == 0

    only_ret = A.filter_token_rows(rows, ["retenu"], "")
    assert [row["ticker"] for row in only_ret] == ["NOVA"]
    both = A.filter_token_rows(rows, ["retenu", "surveillance"], "")
    assert {row["ticker"] for row in both} == {"NOVA", "BRK"}
    by_name = A.filter_token_rows(rows, [], "bri")
    assert [row["ticker"] for row in by_name] == ["BRK"]
    by_ticker = A.filter_token_rows(rows, [], "nova")
    assert [row["ticker"] for row in by_ticker] == ["NOVA"]
    assert A.filter_token_rows(rows, [], "introuvable") == []


# ── 2. Import des campagnes, idempotent ──────────────────────────────────
def test_campaign_import_is_idempotent(db):
    status = _load("parite-status.json")
    plan = L.build_plan(status, {}, source="legacy")
    assert plan["counts"]["campaigns_active"] == 2
    big = next(item for item in plan["campaigns"] if item["coin_ticker"] == "BIG")
    assert big["state"] == "active"
    assert big["events"], "la prolongation doit être rattachée à BigCoin"

    first = L.apply_plan(db, plan)
    assert first["campaigns_created"] == len(plan["campaigns"])
    second = L.apply_plan(db, L.build_plan(status, {}, source="legacy"))
    assert second["campaigns_created"] == 0
    assert second["campaigns_updated"] == 0


def test_overview_mode_last_event_and_solo(db):
    L.apply_plan(db, L.build_plan(_load("parite-status.json"), {}, source="legacy"))
    data = P.overview(db)
    assert data["mode"]["kind"] == "spec"
    assert data["mode"]["label"] == "spec BIG"
    assert data["last_event"] is not None
    assert "BigCoin" in (data["last_event"]["text"] + data["last_event"]["detail"])
    big = next(row for row in data["campaigns"] if row["ticker"] == "BIG")
    assert big["mode"] == "solo"
    assert big["solo"] is True
    assert big["extension_count"] == 1
    assert big["end_at"] == "2026-11-23T12:00:00Z"
    wam = next(row for row in data["campaigns"] if row["ticker"] == "WAM")
    assert wam["unlimited"] is True
    assert wam["end_at"] is None


def test_parse_getbalance_reads_last_numeric_line():
    assert P.parse_getbalance("15351.00213900\n") == 15351.00213900
    assert P.parse_getbalance("### BALANCE\n\n12,5\n") == 12.5
    assert P.parse_getbalance("error: wallet locked") is None


# ── 3. Lancement automatique GPU / CPU ───────────────────────────────────
def test_auto_launch_disabled_never_launches(db):
    R.seed_rules(db)
    rule = db.get(R.Rule, "auto_launch")
    rule.enabled = False
    rule.params = {"gpu": True, "cpu": True}
    db.commit()
    for kind in ("gpu", "cpu"):
        plan = R.plan_auto_launch(db, kind, simulated=False)
        assert plan["allowed"] is False
        assert plan["launch"] is False


def test_auto_launch_gpu_and_cpu_are_separate(db):
    R.seed_rules(db)
    rule = db.get(R.Rule, "auto_launch")
    rule.enabled = True
    rule.params = {"gpu": True, "cpu": False}
    db.commit()
    assert R.plan_auto_launch(db, "gpu", simulated=False)["launch"] is True
    assert R.plan_auto_launch(db, "cpu", simulated=False)["launch"] is False

    rule.params = {"gpu": False, "cpu": True}
    db.commit()
    assert R.plan_auto_launch(db, "gpu", simulated=False)["launch"] is False
    assert R.plan_auto_launch(db, "cpu", simulated=False)["launch"] is True


def test_auto_launch_enabled_with_simulation_is_simulated(db):
    R.seed_rules(db)
    rule = db.get(R.Rule, "auto_launch")
    rule.enabled = True
    rule.params = {"gpu": True, "cpu": True}
    db.commit()
    plan = R.plan_auto_launch(db, "gpu", simulated=True)
    assert plan["allowed"] is True
    assert plan["simulated"] is True
    assert plan["launch"] is False
