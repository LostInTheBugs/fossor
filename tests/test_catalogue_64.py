"""Étape 6.4 — catalogue des mineurs standard, identification et profils types.

Aucune donnée réelle : machines et devices fictifs (192.0.2.x), wallets de test.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest

from app import miner_catalogue as MC
from app import spec_vm as V
from app.analyses import normalize_candidate, token_rows
from app.rules import free_capacity


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _machine(db, name: str) -> int:
    from app.models import Machine

    row = Machine(
        name=name,
        host="192.0.2.10",
        ssh_port=22,
        ssh_user="fossor",
        auth_type="fossor_key",
        machine_kind="host",
        os_kind="ubuntu",
        enabled=True,
        default_miner_profile_id=None,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _device(db, machine_id: int, kind: str, index: int, *, occupied_by: str | None = None,
            role: str = "default_and_spec", enabled: bool = True, integrated: bool = False) -> str:
    from app.models import Device

    value = str(uuid.uuid4())
    db.add(
        Device(
            uuid=value,
            machine_id=machine_id,
            kind=kind,
            index=index,
            vendor="amd",
            model=f"{kind} fictif",
            present=True,
            enabled_for_mining=enabled,
            integrated=integrated,
            role=role,
            occupied_by=occupied_by,
            occupied_external=occupied_by == "external",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    db.commit()
    return value


# ── catalogue ────────────────────────────────────────────────────────
def test_catalogue_charge_sources_et_aucun_doublon():
    cat = MC.catalogue()
    assert cat["version"] == MC.CATALOGUE_VERSION
    assert cat["softwares"]
    software_ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for software in cat["softwares"]:
        assert software["id"] not in software_ids, software["id"]
        software_ids.add(software["id"])
        assert software["source"].startswith("http"), software["id"]
        assert software["release_url"].startswith("https://github.com/"), software["id"]
        assert software["cmdline"].count("{algo}") == 1, software["id"]
        for algo in software["algos"]:
            assert algo["name"].strip()
            assert algo["kind"] in ("cpu", "gpu", "both")
            pair = (software["id"], algo["name"].lower())
            assert pair not in pairs, pair
            pairs.add(pair)
    assert MC.algorithm_count() == len(pairs)
    assert len(MC.profile_templates()) == len(pairs)
    assert len({row["id"] for row in MC.profile_templates()}) == len(pairs)


def test_identification_randomx_yespower_etchash():
    rx = MC.identify("RandomX")
    assert rx["classification"] == "standard"
    assert "xmrig" in {item["software"] for item in rx["miners"]}
    assert rx["template"]["software"] == "xmrig"
    assert rx["template"]["algo"] == "rx/0"

    yespower = MC.identify("yespower")
    assert yespower["classification"] == "standard"
    assert "cpuminer-opt" in {item["software"] for item in yespower["miners"]}
    assert yespower["template"]["software"] == "cpuminer-opt"

    etchash = MC.identify("Etchash")
    assert etchash["classification"] == "standard"
    gpu = {item["software"] for item in etchash["miners"]}
    assert {"lolminer", "t-rex", "rigel"} <= gpu
    assert all(item["kind"] == "gpu" for item in etchash["miners"])


def test_identification_variante_et_proprietaire():
    # Famille RandomX mais nom non documenté + mineur du projet → variante.
    variant = MC.identify("rx/rcpu", own_miner=True)
    assert variant["classification"] == "variante"
    assert variant["miners_compatible"] is False

    # Nom de famille seulement + mineur du projet : rx/0 non prouvé.
    family_only = MC.identify("RandomX", own_miner=True)
    assert family_only["classification"] == "variante"

    # Algorithme inconnu + mineur du projet → propriétaire.
    owned = MC.identify("shadownet-hash", own_miner=True)
    assert owned["classification"] == "proprietaire"
    assert owned["miners"] == []

    # Sans mineur du projet, un nom inconnu reste une variante non documentée.
    assert MC.identify("shadownet-hash")["classification"] == "variante"


def test_profil_type_instancie_wallet_pool_et_binaire_officiel():
    template = MC.template_for("rx/0")
    assert template is not None
    profile = MC.instantiate(
        template,
        wallet="W" * 20,
        pool="stratum+tcp://pool.exemple.tld:3333",
        worker="rig1",
        binary="/opt/fossor/miners/xmrig/6.22.2/xmrig",
        version="6.22.2",
        coin="XMR",
    )
    assert profile["software"] == "xmrig"
    assert "stratum+tcp://pool.exemple.tld:3333" in profile["cmdline"]
    assert "W" * 20 in profile["cmdline"]
    assert "rig1" in profile["cmdline"]
    assert profile["binary_url"] == "https://github.com/xmrig/xmrig/releases"
    assert "{" not in profile["cmdline"] and "}" not in profile["cmdline"]
    assert profile["api_port"] == template["api_port"]


def test_contradiction_standard_et_aucun_profil():
    candidate = {
        "ticker": "NXC",
        "name": "NXC",
        "algo": "yespower",
        "verdict": "retenu",
        "reason": "yespower standard, mineur connu.",
        "criteria": [{"key": "fenetre", "verdict": "ok", "note": "1 j"}],
        "pour": ["yespower standard (cpuminer-opt)"],
        "contre": ["Aucun profil yespower dans Fossor"],
        "links": ["https://exemple.tld/nxc"],
    }
    normalised = normalize_candidate(candidate, strict=False)
    assert normalised["catalogue"]["classification"] == "standard"
    warnings = normalised["catalogue"]["warnings"]
    assert warnings and "contradiction" in warnings[0]

    rows = token_rows([
        {
            "ts": _now(),
            "run": "test",
            "action": "rien",
            "detail": "",
            "delivered": False,
            "source": "hermes",
            "candidates": [normalised],
        }
    ])
    assert rows[0]["catalogue"]["classification"] == "standard"
    assert any("contradiction" in warning for warning in rows[0]["warnings"])


def test_contre_legitime_ne_declenche_aucun_avertissement():
    candidate = {
        "ticker": "PEER",
        "name": "PEER",
        "algo": "RandomX",
        "verdict": "surveillance",
        "reason": "RandomX standard mais aucun pool connu.",
        "pour": ["RandomX standard (xmrig)"],
        "contre": ["Pas de pool au lancement", "CPU uniquement"],
        "links": [],
    }
    normalised = normalize_candidate(candidate, strict=False)
    assert normalised["catalogue"]["classification"] == "standard"
    assert normalised["catalogue"]["warnings"] == []


# ── capacité libérable ───────────────────────────────────────────────
def test_capacite_compte_les_devices_releasables(db):
    athlon = _machine(db, "athlon-fictif")
    ryzen = _machine(db, "ryzen-fictif")
    rig = _machine(db, "rig-fictif")

    _device(db, athlon, "cpu", 0, occupied_by="fossor_default")   # libérable
    _device(db, athlon, "cpu", 1, occupied_by="external")         # bloqué
    _device(db, ryzen, "cpu", 0)                                  # libre
    _device(db, ryzen, "cpu", 1, enabled=False)                   # exclu
    _device(db, rig, "gpu", 0, occupied_by="hiveos")              # libérable
    _device(db, rig, "gpu", 1, occupied_by="external")            # bloqué
    _device(db, rig, "gpu", 2, role="none")                       # exclu du spec

    capacity = free_capacity(db)
    assert capacity["counts"] == {"cpu": 2, "gpu": 1}
    assert capacity["counts_releasable"] == {"cpu": 1, "gpu": 1}
    assert capacity["counts_free"] == {"cpu": 1, "gpu": 0}
    assert capacity["counts_blocked"] == {"cpu": 1, "gpu": 1}
    states = {(row["kind"], row["state"]) for row in capacity["cpu"] + capacity["gpu"]}
    assert states == {("cpu", "free"), ("cpu", "releasable"), ("gpu", "releasable")}


# ── mode déduit du catalogue ─────────────────────────────────────────
def test_mode_deduit_du_catalogue_et_forcage_manuel():
    assert V.DEFAULT_SPEC_MODE == "auto"
    assert MC.mode_for("rx/0")["mode"] == "hybrid"
    assert MC.mode_for("yespower")["mode"] == "hybrid"
    assert MC.mode_for("shadownet-hash", software="custom")["mode"] == "vm"
    assert MC.mode_for("rx/rcpu", software="custom")["mode"] == "vm"
    assert V.normalize_mode("vm", "auto") == "vm"
    assert V.normalize_mode("hybrid", "auto") == "hybrid"
    assert V.normalize_mode("auto", "vm") == "auto"


def test_catalogue_edite_refuse_un_doublon():
    data = copy.deepcopy(MC.catalogue())
    data["softwares"].append(copy.deepcopy(data["softwares"][0]))
    with pytest.raises(ValueError, match="doublon"):
        MC.validate_catalogue(data)
