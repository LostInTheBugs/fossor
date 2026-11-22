"""Metrics collector — SSH snapshot every N seconds, stored for the dashboard.

Design
------
* One whitelisted command per machine (``collect_stats``), never sudo.
* Machines are collected in parallel (``asyncio``, 5 at a time) with a
  per-machine timeout; a failure marks the machine offline with its error and
  never aborts the round.
* Machines with ``usage == "none"`` (or disabled) are never contacted.
* The SRBMiner HTTP API is queried from Fossor only when ``ps`` revealed an
  ``--api-port``; a closed port is a silent no-op.
* ``metrics`` keeps one row per device per round; ``machine_status`` keeps the
  last known machine-level state.  Rows older than the retention window are
  purged once a day.
"""

from __future__ import annotations

import asyncio
import json as json_lib
import logging
import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.hw_parser import norm_bus
from app.stats_parser import (
    merge_srbminer,
    parse_collect_stats,
    parse_srbminer_api,
)

logger = logging.getLogger("fossor.collector")

MAX_PARALLEL = 5
MACHINE_TIMEOUT = 45  # host-key read + connect + command, with margin
SRBMINER_TIMEOUT = 5.0

_task: asyncio.Task | None = None
_last_purge_date: str | None = None


# ── Settings (read per call so tests can set them) ────────────────────
def collect_interval() -> int:
    try:
        return int(os.environ.get("FOSSOR_COLLECT_INTERVAL", "60"))
    except ValueError:
        return 60


def retention_days() -> int:
    try:
        return int(os.environ.get("FOSSOR_METRICS_RETENTION_DAYS", "7"))
    except ValueError:
        return 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SRBMiner HTTP API ─────────────────────────────────────────────────
async def fetch_srbminer(host: str, port: int) -> dict[str, Any] | None:
    """GET ``http://host:port/`` for 5 s; any failure returns ``None`` silently."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=SRBMINER_TIMEOUT) as client:
            response = await client.get(f"http://{host}:{port}/")
        if response.status_code != 200:
            return None
        return parse_srbminer_api(response.text)
    except Exception:
        return None


# ── Settings read per call (patterns, coin pool hosts) ────────────────
def _patterns(db) -> list[dict[str, str]]:
    from app.external_miners import get_patterns

    return get_patterns(db)


def _coin_hosts(db) -> dict[str, str]:
    """Pool host substring → ticker, from the coin catalogue."""
    from app.models import Coin

    hosts: dict[str, str] = {}
    for coin in db.query(Coin).all():
        for host in (coin.pool_hosts or []):
            key = str(host or "").strip().lower()
            if key:
                hosts[key] = coin.ticker
    return hosts


def _coin_catalogue(db) -> dict[str, Any]:
    """Everything coin inference needs: pool hosts, tickers, names, aliases."""
    from app.ai_config import get_setting
    from app.coins import builtin_hosts, builtin_names, builtin_tickers
    from app.models import Coin
    from app.external_miners import COIN_ALIASES

    hosts: dict[str, str] = builtin_hosts()
    names: dict[str, str] = builtin_names()
    tickers: list[str] = builtin_tickers()
    for coin in db.query(Coin).all():
        ticker = str(coin.ticker or "").strip().upper()
        if not ticker:
            continue
        tickers.append(ticker)
        names[ticker.lower()] = ticker
        if coin.name:
            names[str(coin.name).strip().lower()] = ticker
        for host in (coin.pool_hosts or []):
            key = str(host or "").strip().lower()
            if key:
                hosts[key] = ticker
    stored = get_setting(db, "coin_aliases", None)
    aliases = dict(COIN_ALIASES)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if str(key).strip() and str(value).strip():
                aliases[str(key).strip().upper()] = str(value).strip().upper()
    return {"hosts": hosts, "tickers": tickers, "names": names, "aliases": aliases}


def _virt_section(stdout: str) -> str:
    """Bloc ``### VIRT`` d'une collecte (KVM / KVM_DOMAIN), sinon ``""``."""
    keep = False
    lines: list[str] = []
    for line in (stdout or "").splitlines():
        if line.startswith("### "):
            keep = line[4:].strip() == "VIRT"
            continue
        if keep:
            lines.append(line)
    return "\n".join(lines).strip()


def apply_virt(db, machine: Any, stdout: str) -> bool:
    """Réaligne ``machines.vm_host`` sur la sonde de **chaque** collecte.

    La détection matérielle n'écrit ``vm_host`` qu'au clic « Détecter » : une
    SVM activée au BIOS après coup laissait la fiche à ``vm_host=false`` alors
    que ``/dev/kvm`` existait. La collecte lit déjà ``KVM=1`` et
    ``KVM_DOMAIN=…``, on s'en sert à chaque tour. Rend ``True`` si la valeur a
    changé.
    """
    section = _virt_section(stdout)
    if not section:
        return False
    from app.hw_parser import parse_virt

    virt = parse_virt(section)
    if "kvm" not in virt:
        return False
    capable = bool(virt.get("vm_capable", virt.get("kvm")))
    if bool(machine.vm_host) == capable:
        return False
    machine.vm_host = capable
    db.add(machine)
    db.commit()
    return True


def _store_raw_collection(db, machine, stdout: str) -> None:
    """Keep the last raw collection, masked (admin diagnostic screen)."""
    from app.external_miners import mask_raw_collect
    from app.models import RawCollection

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())
    row = (
        db.query(RawCollection)
        .filter(RawCollection.machine_uuid == machine.uuid)
        .first()
    )
    if row is None:
        row = RawCollection(machine_uuid=machine.uuid)
        db.add(row)
    row.ts = _now()
    row.stdout = mask_raw_collect(stdout or "")


def launched_profile(db, machine: Any, stdout: str = "") -> Any:
    """Profil Fossor dont l'unité ``fossor-miner-*`` tourne sur la machine.

    Une unité lancée par Fossor porte le binaire installé dans son argv ; le
    profil reste la seule source de vérité de son API (``api_port``,
    ``api_kind``). ``ps`` peut perdre ces options (troncature, lanceur) : on ne
    s'y fie plus pour une unité Fossor.
    """
    from app import miners as M
    from app.models import MinerProfile

    profile_id = getattr(machine, "default_miner_profile_id", None)
    if not profile_id:
        return None
    profile = db.query(MinerProfile).filter(MinerProfile.id == profile_id).first()
    if profile is None or not profile.enabled:
        return None
    binary = M.binary_path({
        "software": profile.software,
        "version": profile.version,
        "binary_path": profile.binary_path,
    })
    marker = f"fossor-miner-{M.slug(profile.name)}"
    text = stdout or ""
    if binary in text or M.unit_name({"name": profile.name}) in text or marker in text:
        return profile
    return None


def profile_unit_seen(profile: Any, stdout: str = "") -> bool:
    """L'unité ``fossor-miner-*`` du profil est-elle visible dans ``ps`` ?"""
    from app import miners as M

    binary = M.binary_path({
        "software": profile.software,
        "version": profile.version,
        "binary_path": profile.binary_path,
    })
    marker = f"fossor-miner-{M.slug(profile.name)}"
    text = stdout or ""
    # Le chemin du binaire reste un signal (watchdog : unité vue) ; c'est
    # ``active_profiles`` qui écarte un profil dont les devices vivent ailleurs
    # (2026.11.023).
    return bool(
        (binary and binary in text)
        or M.unit_name({"name": profile.name}) in text
        or marker in text
    )


def active_profiles(db, machine: Any, stdout: str = "") -> list[Any]:
    """Profils actifs d'une machine : sélection, tokens par device, unités vues.

    L'athlon (postmigration 2026.11) n'a **aucun** profil sélectionné au niveau
    de la machine : chaque device porte son ``default_token_id`` (Pearl sur les
    GPU, KSH sur le CPU).  Chercher seulement ``selected_profile_id`` laissait
    l'API SRBMiner et le journal CPU invisibles.
    """
    from app.models import Device, MinerProfile

    found: list[Any] = []
    seen: set[int] = set()

    def take(profile: Any) -> None:
        if profile is not None and profile.enabled and profile.id not in seen:
            seen.add(profile.id)
            found.append(profile)

    if getattr(machine, "default_miner_profile_id", None):
        take(db.get(MinerProfile, machine.default_miner_profile_id))
    for device in db.query(Device).filter(
        Device.machine_id == machine.id, Device.present == True  # noqa: E712
    ).all():
        token = getattr(device, "default_token_id", None)
        if token and device.enabled_for_mining:
            take(db.get(MinerProfile, int(token)))
    for profile in db.query(MinerProfile).filter(MinerProfile.enabled == True).all():  # noqa: E712
        if profile.id in seen or not profile_unit_seen(profile, stdout):
            continue
        # 2026.11.023 : une unité vue ne suffit pas — un profil dont les devices
        # vivent sur une autre machine (Athlon Pearl vu dans le ``ps`` du Ryzen,
        # même binaire SRBMiner) n'a rien à faire ici.
        elsewhere = (
            db.query(Device)
            .filter(
                Device.default_token_id == profile.id,
                Device.machine_id != machine.id,
                Device.present == True,  # noqa: E712
            )
            .count()
        )
        if not elsewhere:
            take(profile)
    return found


def profile_devices(db, machine: Any, profile: Any) -> list[Any]:
    """Devices rattachés au profil par leur token par défaut."""
    from app.models import Device

    rows = db.query(Device).filter(
        Device.machine_id == machine.id, Device.present == True  # noqa: E712
    ).all()
    owned = [d for d in rows if getattr(d, "default_token_id", None) == profile.id]
    if owned:
        return owned
    if getattr(machine, "default_miner_profile_id", None) == profile.id:
        return rows
    return []


def _merge_profile_stats(stats: dict[str, Any], profile: Any, api: dict[str, Any] | None,
                         devices: list[Any]) -> dict[str, Any]:
    """Attribue le relevé d'une API aux devices **du** profil (multi-profils).

    Le hashrate d'un GPU se rattache par bus PCI dans le sous-ensemble du
    profil ; celui du CPU vient de ``cpu_hashrate_hs`` (ou du total quand aucun
    GPU du profil n'a répondu, cas d'un mineur CPU seul).
    """
    from app import miners as M

    busses = {norm_bus(d.pci_bus) for d in devices if d.kind == "gpu" and d.pci_bus}
    cpu_owned = any(d.kind == "cpu" for d in devices)
    software = (profile.software or "").strip().lower()
    port = int(profile.api_port or 0)
    # Le profil dont on lit l'API nomme le mineur (``ps`` peut citer le CPU
    # en dernier) ; sinon la première valeur connue suffit.
    if software and (not stats.get("miner") or port):
        stats["miner"] = software
    if port and not stats.get("api_port"):
        stats["api_port"] = port
    entry: dict[str, Any] = {
        "profile_id": profile.id,
        "name": profile.name,
        "unit": M.unit_name({"name": profile.name}),
        "coin": profile.coin or "",
        "miner": software,
        "api_port": port or None,
        "hashrate_hs": None,
        "accepted": None,
        "rejected": None,
        "api_error": None,
    }
    if not api:
        return entry
    gpus = [d for d in stats.get("devices") or [] if d.get("kind") == "gpu"]
    mine = [d for d in gpus if norm_bus(d.get("pci_bus")) in busses] if busses else gpus
    api_gpus = [gpu for gpu in (api.get("gpus") or []) if gpu.get("pci_bus")]
    if api_gpus:
        by_bus = {norm_bus(gpu["pci_bus"]): gpu for gpu in api_gpus}
        for gpu in mine:
            source = by_bus.get(norm_bus(gpu.get("pci_bus")))
            if source and gpu.get("hashrate_hs") is None:
                gpu["hashrate_hs"] = source.get("hashrate_hs")
    else:
        for gpu, value in zip(mine, api.get("device_hashrates_hs") or []):
            if gpu.get("hashrate_hs") is None:
                gpu["hashrate_hs"] = value
    if cpu_owned:
        cpu = next((d for d in stats.get("devices") or [] if d.get("kind") == "cpu"), None)
        value = api.get("cpu_hashrate_hs")
        if value is None and not any(gpu.get("hashrate_hs") for gpu in mine):
            value = api.get("total_hashrate_hs")
        if cpu is not None and cpu.get("hashrate_hs") is None and value:
            cpu["hashrate_hs"] = value
    values = [d.get("hashrate_hs") for d in mine if d.get("hashrate_hs")]
    entry["hashrate_hs"] = sum(values) if values else api.get("total_hashrate_hs")
    entry["accepted"] = api.get("accepted")
    entry["rejected"] = api.get("rejected")
    return entry


def _apply_profile_api(stats: dict[str, Any], profile: Any) -> dict[str, Any]:
    """L'API déclarée par le profil fait foi pour une unité lancée par Fossor."""
    software = (getattr(profile, "software", "") or "").strip().lower()
    if software:
        stats["miner"] = software
    stats["api_kind"] = (getattr(profile, "api_kind", "") or "").strip().lower()
    port = int(getattr(profile, "api_port", 0) or 0)
    if not port:
        return stats
    stats["api_port"] = port
    # 2026.11.026 : le port du profil ne va qu'au processus lancé par Fossor.
    # Un nœud solo (bitcoind) n'expose jamais l'API du mineur : lui prêter ce
    # port faisait entrer le pool et les algorithmes Pearl dans l'entrée BigCoin.
    for miner in stats.get("miners") or []:
        if miner.get("api_port") or str(miner.get("kind") or "") == "solo":
            continue
        if software:
            text = " ".join(
                str(miner.get(key) or "")
                for key in ("name", "miner_name", "label", "binary", "cmd")
            ).lower()
            if software not in text:
                continue
            miner["name"] = software
            miner["miner_name"] = software
            miner["label"] = software
        miner["api_port"] = port
        break
    return stats


def _parse_api_payload(payload: str, api_kind: str = "") -> dict[str, Any] | None:
    """Corps d'API → relevé, selon le type d'API déclaré par le profil."""
    kind = (api_kind or "").strip().lower()
    if kind == "cpuminer":
        from app.spec_vm import parse_cpuminer_summary

        value = parse_cpuminer_summary(payload or "")
        if value is None:
            return None
        return {"total_hashrate_hs": value, "miner": "cpuminer"}
    return parse_srbminer_api(payload)


def _api_hashrate(api: dict[str, Any] | None) -> bool:
    """An API answer is only usable when it actually carries a hashrate."""
    return isinstance(api, dict) and api.get("total_hashrate_hs") is not None


async def _fetch_miner_api(
    machine, port: int, api_kind: str = ""
) -> tuple[dict[str, Any] | None, str | None]:
    """Direct GET, then the SSH relay (SRBMiner bound to 127.0.0.1).

    A 200 answer without a hashrate (web page, foreign JSON) is **not** a
    success: the relay is still tried, and a miner that keeps a port but no
    hashrate ends up with an ``api_error`` instead of a silent « — »
    (2026.11.003-c4).
    """
    direct = await fetch_srbminer(machine.host, port)
    if _api_hashrate(direct):
        return direct, None
    direct_note = "réponse sans hashrate" if direct else "port fermé"

    try:
        from app.ssh_client import ssh_miner_api

        raw = await ssh_miner_api(machine, port)
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"ok": False, "error": str(exc)}

    relay: dict[str, Any] | None = None
    if raw and raw.get("ok"):
        relay = _parse_api_payload(raw.get("stdout") or "", api_kind)
    if _api_hashrate(relay):
        return relay, None

    if (raw or {}).get("ok"):
        relay_note = "réponse sans hashrate" if relay else "réponse illisible"
        message = (
            f"API du mineur sans hashrate (direct : {direct_note} ; SSH : {relay_note})"
        )
        return (relay if relay else None), message
    relay_note = str((raw or {}).get("error") or "relais SSH indisponible")
    message = f"API du mineur injoignable (direct : {direct_note} ; SSH : {relay_note})"
    return None, message


# ── Storage ───────────────────────────────────────────────────────────
def _numeric(value: float | None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


async def _ensure_devices(db, machine) -> bool:
    """Detect a machine once when it has no device row yet (§4.2).

    A machine that was never « detected » stores its GPU metrics with
    ``device_uuid = NULL`` and nothing shows per card.  The first successful
    collection therefore runs one whitelisted ``detect_hw`` (audited, actor
    ``scheduler``); the metrics of the same round then attach by PCI bus.
    """
    from app.devices import apply_detection
    from app.models import AuditLog, Device
    from app.ssh_client import ssh_detect_hw

    present = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
        .count()
    )
    if present:
        return False

    try:
        raw = await ssh_detect_hw(machine, actor="system")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("détection automatique de %s en échec: %s", machine.name, exc)
        return False

    if not raw.get("ok"):
        logger.info("détection automatique de %s impossible: %s", machine.name, raw.get("error"))
        db.add(AuditLog(
            actor="system",
            action="detect",
            machine_id=machine.id,
            object_ref=machine.name,
            result="fail",
            output=str(raw.get("error") or "")[:500],
        ))
        db.commit()
        return False

    # ssh_detect_hw already returns the parsed detection; older shapes carry stdout.
    detected = raw
    if "cpu" not in raw and "gpus" not in raw and raw.get("stdout"):
        from app.hw_parser import parse_detect_hw

        detected = parse_detect_hw(raw.get("stdout", ""))

    external_miners = raw.get("gpu_compute", [])
    summary = apply_detection(db, machine, detected, gpu_compute=external_miners)
    db.add(AuditLog(
        actor="system",
        action="detect",
        machine_id=machine.id,
        object_ref=machine.name,
        result="ok",
        output=("détection automatique (1re collecte) " + str(summary))[:500],
    ))
    db.commit()
    return True


async def _enrich_cpu_state(db, machine, raw_stdout: str, stats: dict[str, Any]) -> str:
    """Hashrate CPU et état du CPU (2026.11.003-c7).

    One extra read-only probe per round: the miner APIs and logs found in the
    command lines, the CPU counters (``/proc/stat`` twice), the hwmon sensors
    and two RAPL readings.  Every CPU miner ends up with a hashrate **or** an
    explicit reason; the ``cpu`` device gets charge, temperature, power and the
    summed CPU hashrate.
    """
    from app.cpu_stats import (
        attach_cpu_hashrates,
        cpu_state_with,
        parse_cpu_report,
        plan_cpu_probes,
    )
    from app.ssh_client import ssh_collect_cpu
    from app.stats_parser import split_sections

    sections = split_sections(raw_stdout)
    probes = plan_cpu_probes(sections["PS_MINERS"])
    report: dict[str, Any] = {"state": {}, "logs": {}, "apis": {}, "nodes": {}}
    probe_stdout = ""
    # 2026.11.003-c8 : la sonde tourne **toujours**, même sans mineur détecté.
    # La fréquence, la charge et la puissance du CPU ne dépendent d'aucun mineur,
    # et c'est leur absence qui laissait `cpu MHz` vide sur les trois machines.
    try:
        raw = await ssh_collect_cpu(machine, probes)
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("sonde CPU de %s en échec: %s", machine.name, exc)
        raw = {"ok": False, "error": str(exc)}
    if raw.get("ok"):
        probe_stdout = raw.get("stdout") or ""
        report = parse_cpu_report(probe_stdout)

    miners = stats.get("miners") or []
    attach_cpu_hashrates(miners, probes, report)
    # 025 : un hashrate sans source CPU (nœud BigCoin, total du GPU) n'est pas
    # un hashrate CPU.  Il reste inconnu plutôt que d'être attribué au CPU.
    for miner in miners:
        if str(miner.get("hashrate_note") or "").startswith("hashrate CPU inconnu"):
            miner["hashrate_hs"] = None
    cpu_miners = [m for m in miners if str(m.get("kind") or "") in ("cpu", "solo")]

    cputemp = _hive_cputemp(sections["HIVE_STAT"])
    cpu = cpu_state_with(
        report, cputemp, stats.get("cpu_package_power_w"), cpu_miners,
        typed_power_w=machine.cpu_power_w,
    )
    stats["cpu"] = cpu

    for item in stats.get("devices") or []:
        if item.get("kind") != "cpu":
            continue
        item["load_pct"] = cpu.get("load_pct")
        item["temp_c"] = _first_number(item.get("temp_c"), cpu.get("temp_c"))
        item["power_w"] = _first_number(item.get("power_w"), cpu.get("power_w"))
        item["hashrate_hs"] = cpu.get("hashrate_hs")
    return probe_stdout


def _first_number(*values: Any) -> float | None:
    for value in values:
        # §4.2 bis : 0 W renvoyé par une API veut dire « non mesuré ».
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    return None


def _hive_cputemp(hive_stat: str) -> float | None:
    """``cputemp`` of ``last_stat.json`` (HiveOS) — first value only."""
    import json

    try:
        data = json.loads(hive_stat or "{}")
    except (TypeError, ValueError):
        return None
    temps = data.get("temp") if isinstance(data, dict) else None
    cputemp = data.get("cputemp") if isinstance(data, dict) else None
    for value in (cputemp if isinstance(cputemp, list) else [], temps if isinstance(temps, list) else []):
        for entry in value:
            if isinstance(entry, (int, float)) and entry > 0:
                return float(entry)
    return None


def _store_metrics(db, machine, stats: dict[str, Any], ts: str) -> int:
    """Insert one metric row per parsed device, then upsert ``machine_status``."""
    from app.models import Device, MachineStatus, Metric

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())

    devices = (
        db.query(Device)
        .filter(Device.machine_id == machine.id, Device.present == True)  # noqa: E712
        .all()
    )
    cpu = next((d for d in devices if d.kind == "cpu"), None)
    gpus_by_bus = {
        norm_bus(d.pci_bus): d for d in devices if d.kind == "gpu" and d.pci_bus
    }

    rows = 0
    for item in stats.get("devices", []):
        if item.get("kind") == "cpu":
            device = cpu
        else:
            device = gpus_by_bus.get(item.get("pci_bus") or "")
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=device.uuid if device else None,
            ts=ts,
            hashrate_hs=_numeric(item.get("hashrate_hs")),
            temp_c=_numeric(item.get("temp_c")),
            fan_pct=_numeric(item.get("fan_pct")),
            power_w=_numeric(item.get("power_w")),
            load_pct=_numeric(item.get("load_pct")),
        ))
        rows += 1
    if rows == 0:
        # Machine-level row only (no device matched or none reported).
        db.add(Metric(
            machine_uuid=machine.uuid,
            device_uuid=None,
            ts=ts,
            hashrate_hs=_numeric(stats.get("total_hashrate_hs")),
            power_w=_numeric(stats.get("total_power_w")),
        ))
        rows = 1

    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    if status is None:
        status = MachineStatus(machine_uuid=machine.uuid)
        db.add(status)
    status.ts = ts
    status.online = True
    status.error = None
    status.miner = stats.get("miner") or ""
    status.algo = stats.get("algo") or ""
    status.coin = stats.get("coin") or ""
    status.uptime_s = stats.get("uptime_s")
    status.accepted = stats.get("accepted")
    status.rejected = stats.get("rejected")
    status.api_port = stats.get("api_port")
    status.total_hashrate_hs = _numeric(stats.get("total_hashrate_hs"))
    status.total_power_w = _numeric(stats.get("total_power_w"))
    # §10.7 : puissance à la prise par machine, à chaque collecte.
    try:
        from app import history

        history.record_plug_sample(db, machine, status.total_power_w)
    except Exception:  # pragma: no cover - l'historisation ne casse pas la collecte
        logger.warning("historisation de la puissance à la prise en échec", exc_info=True)
    status.api_error = stats.get("api_error")
    status.profiles_json = (
        json_lib.dumps(stats.get("profiles"), ensure_ascii=False)
        if stats.get("profiles") else None
    )
    # 2026.11.003-c7: état du CPU + hashrate par mineur CPU.
    status.cpu_json = (
        json_lib.dumps(stats.get("cpu"), ensure_ascii=False) if stats.get("cpu") else None
    )
    try:
        from app import notifications

        notifications.machine_result(
            db, machine, online=True, hashrate_hs=status.total_hashrate_hs
        )
    except Exception:  # pragma: no cover - notifications never break collection
        logger.warning("notification « en ligne » en échec", exc_info=True)
    db.commit()
    return rows


def _store_offline(db, machine, error: str, ts: str) -> None:
    from app.models import MachineStatus

    if not machine.uuid:
        machine.uuid = str(uuid_lib.uuid4())
    status = (
        db.query(MachineStatus)
        .filter(MachineStatus.machine_uuid == machine.uuid)
        .first()
    )
    if status is None:
        status = MachineStatus(machine_uuid=machine.uuid)
        db.add(status)
    status.ts = ts
    status.online = False
    status.error = (error or "collecte impossible")[:500]
    for field in ("miner", "algo", "coin", "uptime_s", "accepted", "rejected",
                  "api_port", "api_error", "total_hashrate_hs", "total_power_w"):
        setattr(status, field, None if field not in ("miner", "algo", "coin") else "")
    try:
        from app import notifications

        notifications.machine_result(db, machine, online=False, error=status.error)
    except Exception:  # pragma: no cover - notifications never break collection
        logger.warning("notification « hors ligne » en échec", exc_info=True)
    db.commit()


def _mark_offline(machine_id: int, error: str) -> None:
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine:
            _store_offline(db, machine, error, _now())
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not mark machine %s offline", machine_id)
        db.rollback()
    finally:
        db.close()


def purge_old_metrics() -> int:
    """Delete metrics older than the retention window (default 7 days)."""
    from app.database import SessionLocal
    from app.models import Metric

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days())).isoformat()
    db = SessionLocal()
    try:
        deleted = db.query(Metric).filter(Metric.ts < cutoff).delete()
        db.commit()
        return int(deleted or 0)
    finally:
        db.close()


def _purge_once_a_day() -> None:
    global _last_purge_date
    today = datetime.now(timezone.utc).date().isoformat()
    if _last_purge_date == today:
        return
    try:
        # §10.7 : agrégation horaire/journalière puis purge bornée, journalisées.
        from app import history

        history.run_maintenance()
        _last_purge_date = today
    except Exception:  # pragma: no cover - defensive
        logger.warning("maintenance de l'historique en échec", exc_info=True)


# ── One machine ───────────────────────────────────────────────────────
async def collect_machine(machine_id: int) -> dict[str, Any]:
    """Collect, parse and store one machine. Never raises."""
    from app.database import SessionLocal
    from app.models import Machine
    from app.ssh_client import ssh_collect_stats

    db = SessionLocal()
    try:
        machine = db.query(Machine).filter(Machine.id == machine_id).first()
        if machine is None:
            return {"machine_id": machine_id, "online": False, "error": "machine inconnue"}
        if not machine.enabled or str(machine.usage) == "none":
            return {"machine_id": machine_id, "skipped": True}
        if str(machine.auth_type) == "provision_pending":
            # §2.2: never attempt a connection before the account exists.
            return {
                "machine_id": machine_id,
                "name": machine.name,
                "skipped": True,
                "error": "machine pas encore préparée (provision_pending)",
            }

        raw = await ssh_collect_stats(machine, actor="system")
        ts = _now()
        if not raw.get("ok"):
            error = raw.get("error", "collecte impossible")
            _store_offline(db, machine, error, ts)
            logger.info("machine %s hors ligne: %s", machine.name, error)
            try:
                from app import watchdog

                await watchdog.evaluate_machine(db, machine, offline=error)
            except Exception:  # le watchdog ne casse jamais une collecte
                logger.warning("watchdog : évaluation hors ligne en échec", exc_info=True)
            return {"machine_id": machine_id, "name": machine.name, "online": False, "error": error}

        catalogue = _coin_catalogue(db)
        stats = parse_collect_stats(
            raw.get("stdout", ""),
            patterns=_patterns(db),
            coin_hosts=catalogue["hosts"],
            coin_tickers=catalogue["tickers"],
            coin_names=catalogue["names"],
            coin_aliases=catalogue["aliases"],
        )
        # Profils actifs : sélection machine, tokens par device et unités Fossor
        # vues.  L'athlon n'a aucun profil sélectionné : Pearl (GPU) et KSH (CPU)
        # sont rattachés par ``devices.default_token_id``.
        profiles = active_profiles(db, machine, raw.get("stdout", ""))
        applied = launched_profile(db, machine, raw.get("stdout", ""))
        multi = len(profiles) > 1
        profile_rows: list[dict[str, Any]] = []
        if not multi:
            chosen = applied or (profiles[0] if profiles else None)
            if chosen is not None and (
                chosen is applied or bool(profile_devices(db, machine, chosen))
            ):
                applied = chosen
                stats = _apply_profile_api(stats, applied)
            else:
                applied = None
        from app.devices import mark_external_devices, set_external_miners

        miners = stats.get("miners") or []
        set_external_miners(machine, miners, db=db)
        db.commit()
        # §4.2: first collection of a machine without devices → detect once,
        # so this round's GPU metrics attach by PCI bus instead of NULL.
        await _ensure_devices(db, machine)
        # Occupancy is refreshed every round, not only on the first detect.
        mark_external_devices(db, machine, miners)
        db.commit()
        if multi:
            total_accepted: int | None = None
            total_rejected: int | None = None
            for profile in profiles:
                api = None
                api_error = None
                owned = profile_devices(db, machine, profile)
                if owned and profile.api_port:
                    api, api_error = await _fetch_miner_api(
                        machine, int(profile.api_port), profile.api_kind or ""
                    )
                entry = _merge_profile_stats(stats, profile, api, owned)
                entry["api_error"] = api_error if api is None else None
                profile_rows.append(entry)
                if entry.get("accepted") is not None:
                    total_accepted = (total_accepted or 0) + int(entry["accepted"])
                if entry.get("rejected") is not None:
                    total_rejected = (total_rejected or 0) + int(entry["rejected"])
            stats["accepted"] = total_accepted
            stats["rejected"] = total_rejected
            stats["api_error"] = next(
                (row["api_error"] for row in profile_rows if row.get("api_error")), None
            )
        elif stats.get("api_port"):
            api, api_error = await _fetch_miner_api(
                machine, int(stats["api_port"]), stats.get("api_kind") or ""
            )
            if api:
                stats = merge_srbminer(stats, api)
            from app.external_miners import normalize_coin
            from app.srbminer_api import distinct_coins, expand_miner_algos

            aliases = catalogue["aliases"]
            stats["coin"] = normalize_coin(
                stats.get("coin", ""), aliases
            )
            # One hashrate per miner: it belongs to the process that answered.
            total = stats.get("total_hashrate_hs")
            for miner in stats.get("miners") or []:
                if miner.get("api_port") and total is not None:
                    miner["hashrate_hs"] = total
            # A miner with several algorithms is one line per algorithm, each
            # with its own coin and hashrate (SRBMiner on the rig, 003-c6).
            if stats.get("srbminer_algos"):
                stats["miners"] = expand_miner_algos(
                    stats.get("miners") or [], stats.pop("srbminer_algos"), aliases
                )
            stats["coins"] = distinct_coins(stats.get("miners") or [])
            # A port that yields no hashrate must be visible, never a silent « — ».
            if stats.get("total_hashrate_hs") is None:
                stats["api_error"] = api_error or (
                    "Hashrate absent : l'API du mineur n'a rien renvoyé"
                )
            else:
                stats["api_error"] = None

        # Miners as displayed (coin + per-miner hashrate included).
        cpu_probe_raw = await _enrich_cpu_state(db, machine, raw.get("stdout", ""), stats)
        if multi:
            # Le hashrate CPU vient du journal (``_enrich_cpu_state``) : la
            # répartition par profil se fait après la sonde, sur les devices.
            for entry, profile in zip(profile_rows, profiles):
                owned = profile_devices(db, machine, profile)
                busses = {norm_bus(d.pci_bus) for d in owned if d.kind == "gpu" and d.pci_bus}
                cpu_owned = any(d.kind == "cpu" for d in owned)
                values = [
                    item.get("hashrate_hs")
                    for item in stats.get("devices") or []
                    if item.get("hashrate_hs")
                    and (
                        (item.get("kind") == "gpu" and norm_bus(item.get("pci_bus")) in busses)
                        or (item.get("kind") == "cpu" and cpu_owned)
                    )
                ]
                if values:
                    entry["hashrate_hs"] = sum(values)
            device_sum = sum(
                item.get("hashrate_hs")
                for item in stats.get("devices") or []
                if item.get("hashrate_hs")
            )
            if device_sum:
                stats["total_hashrate_hs"] = device_sum
        elif applied is not None:
            from app import miners as M

            if stats.get("total_hashrate_hs") is None:
                stats["total_hashrate_hs"] = sum(
                    item.get("hashrate_hs")
                    for item in stats.get("devices") or []
                    if item.get("hashrate_hs")
                ) or None
            profile_rows.append({
                "profile_id": applied.id,
                "name": applied.name,
                "unit": M.unit_name({"name": applied.name}),
                "coin": applied.coin or "",
                "miner": stats.get("miner") or "",
                "api_port": stats.get("api_port"),
                "hashrate_hs": stats.get("total_hashrate_hs"),
                "accepted": stats.get("accepted"),
                "rejected": stats.get("rejected"),
                "api_error": stats.get("api_error"),
            })
        # 2026.11.023 — un seul verdict pour la santé, comme pour un profil
        # unique : l'API d'un mineur ne compte que si la collecte n'a **aucun**
        # hashrate.  Un GPU qui remonte par ses logs n'est pas « mineur
        # injoignable » parce qu'un profil étranger a un port fermé.
        if stats.get("total_hashrate_hs") is not None:
            stats["api_error"] = None
        stats["profiles"] = profile_rows
        set_external_miners(machine, stats.get("miners") or [], db=db)
        # Diagnostic 2026.11.003-c8 : la collecte brute porte aussi la sonde CPU
        # (masquée), en tête pour survivre à la troncature de mask_raw_collect.
        combined = raw.get("stdout", "")
        if cpu_probe_raw:
            combined = "### CPU_PROBE\n" + cpu_probe_raw + "\n" + combined
        apply_virt(db, machine, raw.get("stdout", ""))
        _store_raw_collection(db, machine, combined)
        rows = _store_metrics(db, machine, stats, ts)
        try:
            from app import watchdog

            await watchdog.evaluate_machine(
                db, machine, stats=stats, raw_stdout=raw.get("stdout", "")
            )
        except Exception:  # le watchdog ne casse jamais une collecte
            logger.warning("watchdog : évaluation en échec", exc_info=True)
        return {
            "machine_id": machine_id,
            "name": machine.name,
            "online": True,
            "miner": stats.get("miner") or "",
            "api_port": stats.get("api_port"),
            "devices": len(stats.get("devices", [])),
            "rows": rows,
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("collecte machine %s en échec: %s", machine_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return {"machine_id": machine_id, "online": False, "error": str(exc)}
    finally:
        db.close()


async def _guarded(machine_id: int, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        try:
            return await asyncio.wait_for(collect_machine(machine_id), timeout=MACHINE_TIMEOUT)
        except asyncio.TimeoutError:
            error = f"Collecte expirée ({MACHINE_TIMEOUT} s)"
            _mark_offline(machine_id, error)
            return {"machine_id": machine_id, "online": False, "error": error}
        except Exception as exc:  # pragma: no cover - defensive
            _mark_offline(machine_id, f"Erreur de collecte: {exc}")
            return {"machine_id": machine_id, "online": False, "error": str(exc)}


async def _collect_asic(machine_id: int) -> dict[str, Any]:
    """§4.20 : un ASIC se lit par HTTP/TCP, jamais en SSH."""
    from app.database import SessionLocal
    from app.models import Machine
    from app.routers.asic import probe_machine

    db = SessionLocal()
    try:
        machine = db.get(Machine, machine_id)
        if machine is None:
            return {"machine_id": machine_id, "online": False, "error": "machine absente"}
        state = await probe_machine(machine, db)
        db.commit()
        return {"machine_id": machine_id, "online": bool(state.get("online")), "asic": True}
    except Exception as exc:  # pragma: no cover - défensif
        db.rollback()
        _mark_offline(machine_id, f"ASIC : {exc}")
        return {"machine_id": machine_id, "online": False, "error": str(exc)}
    finally:
        db.close()


async def collect_all() -> list[dict[str, Any]]:
    """One round over every enabled machine whose usage is not ``none``."""
    from app.database import SessionLocal
    from app.models import Machine

    db = SessionLocal()
    try:
        machine_ids = [
            row.id
            for row in db.query(Machine)
            .filter(Machine.enabled == True, Machine.usage != "none")  # noqa: E712
            .all()
        ]
        asic_ids = [
            row.id
            for row in db.query(Machine)
            .filter(
                Machine.enabled == True,  # noqa: E712
                Machine.machine_kind == "asic",
            )
            .all()
        ]
    finally:
        db.close()

    _purge_once_a_day()
    semaphore = asyncio.Semaphore(MAX_PARALLEL)
    results = await asyncio.gather(
        *(_guarded(machine_id, semaphore) for machine_id in machine_ids),
        *(_collect_asic(machine_id) for machine_id in asic_ids),
        return_exceptions=True,
    )
    order = machine_ids + asic_ids
    out: list[dict[str, Any]] = []
    for machine_id, result in zip(order, results):
        if isinstance(result, BaseException):
            out.append({"machine_id": machine_id, "online": False, "error": str(result)})
        else:
            out.append(result)
    return out


# ── Scheduler ─────────────────────────────────────────────────────────
async def _loop() -> None:
    while True:
        interval = collect_interval()
        try:
            results = await collect_all()
            failed = [row for row in results if not row.get("online")]
            from app import observability
            from app.database import SessionLocal

            db = SessionLocal()
            try:
                observability.mark_routine(
                    db,
                    "collect",
                    not failed,
                    f"{len(results) - len(failed)}/{len(results)} machines jointes"
                    + (f" ; en échec : {', '.join(str(r.get('machine_id')) for r in failed[:5])}" if failed else ""),
                    interval_s=interval,
                )
            finally:
                db.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("collecte globale en échec", exc_info=True)
            try:
                from app import observability
                from app.database import SessionLocal

                db = SessionLocal()
                try:
                    observability.mark_routine(db, "collect", False, "collecte en échec", interval_s=interval)
                finally:
                    db.close()
            except Exception:
                pass
        await asyncio.sleep(interval if interval > 0 else 60)


def start() -> None:
    """Start the background loop (no-op when the interval is 0)."""
    global _task
    if collect_interval() <= 0:
        return
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
