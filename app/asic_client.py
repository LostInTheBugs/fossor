"""Lecture réseau des ASIC (§4.20) — HTTP et TCP 4028, **sans aucun pilotage**.

Toutes les fonctions sont en lecture seule : ``GET /api/system/info`` (AxeOS),
``GET /api/overview`` (IceRiver) et les commandes ``summary``/``stats``/``pools``
de l'API cgminer.  Aucune commande d'écriture n'est construite ici.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from typing import Any

from app import asic

TIMEOUT_S = 6.0
CGMINER_PORT = 4028

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")


class AsicError(RuntimeError):
    """Erreur de lecture d'un ASIC, message destiné à l'UI."""


def clean_host(value: str) -> str:
    """Nettoie et valide une cible : IP (v4/v6) ou nom d'hôte, port optionnel.

    ``http://192.0.2.10:8080/api`` -> ``192.0.2.10:8080``.  Aucun schéma, aucun
    chemin : une adresse seulement, pour qu'aucune URL arbitraire ne soit
    construite depuis l'API.
    """
    text = str(value or "").strip()
    if not text:
        raise AsicError("adresse vide")
    text = re.sub(r"^[a-zA-Z]+://", "", text).strip("/")
    if "/" in text:
        text = text.split("/", 1)[0]
    if not _HOST_RE.match(text) or " " in text:
        raise AsicError(f"adresse invalide : {value!r}")
    port: int | None = None
    host = text
    if text.count(":") == 1:
        head, tail = text.split(":")
        if tail.isdigit():
            host, port = head, int(tail)
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$", host):
            raise AsicError(f"adresse invalide : {value!r}")
    if port is not None and not 0 < port < 65536:
        raise AsicError(f"port invalide : {port}")
    return f"{host}:{port}" if port else host


def _url(host: str, path: str) -> str:
    host = clean_host(host)
    if host.count(":") > 1 and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}{path}"


async def _get_json(url: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={"User-Agent": "Fossor/0.1"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # réseau, HTTP, JSON
        raise AsicError(f"{url} : {exc}") from exc
    if not isinstance(payload, dict):
        raise AsicError(f"{url} : réponse inattendue")
    return payload


async def fetch_axeos(host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """``GET /api/system/info`` — Bitaxe / NerdQaxe."""
    return await _get_json(_url(host, "/api/system/info"), timeout)


async def fetch_iceriver(host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """``GET /api/overview`` — firmware IceRiver (KS0 & co)."""
    return await _get_json(_url(host, "/api/overview"), timeout)


async def _cgminer_command(reader, writer, command: str, timeout: float) -> dict[str, Any]:
    writer.write(json.dumps({"command": command}).encode() + b"\n")
    await writer.drain()
    chunks = bytearray()
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
        if not chunk:
            break
        chunks.extend(chunk)
        if b"\x00" in chunk:
            break
    text = chunks.decode("utf-8", "replace").replace("\x00", "").strip()
    if not text:
        return {}
    start = text.find("{")
    try:
        payload = json.loads(text[start:] if start >= 0 else text)
    except ValueError as exc:
        raise AsicError(f"réponse 4028 illisible ({command}) : {exc}") from exc
    return payload if isinstance(payload, dict) else {}


async def fetch_cgminer(
    host: str,
    port: int = CGMINER_PORT,
    commands: tuple[str, ...] = ("summary", "stats", "pools"),
    timeout: float = TIMEOUT_S,
) -> dict[str, Any]:
    """API TCP 4028 : ``summary`` + ``stats`` + ``pools`` fusionnés.

    **Format non vérifié** : écrit d'après la documentation publique de
    cgminer/bmminer/BOSminer, aucune sortie réelle n'a été fournie.
    """
    target = clean_host(host)
    if not 0 < int(port) < 65536:
        raise AsicError(f"port invalide : {port}")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target, int(port)), timeout=timeout
        )
    except Exception as exc:
        raise AsicError(f"{target}:{port} injoignable : {exc}") from exc
    merged: dict[str, Any] = {}
    try:
        for command in commands:
            try:
                payload = await _cgminer_command(reader, writer, command, timeout)
            except (AsicError, asyncio.TimeoutError) as exc:
                merged.setdefault("__errors__", []).append(f"{command}: {exc}")
                continue
            for key, value in payload.items():
                merged[key] = value
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    if not merged:
        raise AsicError(f"{target}:{port} : aucune réponse exploitable")
    return merged


async def detect(host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Sonde en lecture seule : reconnaît le pilote d'un ASIC."""
    errors: list[str] = []
    for driver, fetcher in (("axeos", fetch_axeos), ("iceriver", fetch_iceriver)):
        try:
            payload = await fetcher(host, timeout)
        except AsicError as exc:
            errors.append(str(exc))
            continue
        state = asic.state_from_payload(payload)
        if state:
            return {"driver": driver, "state": state, "payload": payload, "errors": errors}
        errors.append(f"{driver} : réponse non reconnue")
    try:
        payload = await fetch_cgminer(host, CGMINER_PORT, timeout=timeout)
        state = asic.parse_cgminer(payload, payload, payload)
        return {"driver": "cgminer", "state": state, "payload": payload, "errors": errors}
    except AsicError as exc:
        errors.append(str(exc))
    raise AsicError("pilote non reconnu — " + " | ".join(errors[-2:]))


async def poll(driver: str, host: str, port: int | None = None) -> dict[str, Any]:
    """Interroge un ASIC selon son pilote et rend l'état normalisé."""
    if driver == "axeos":
        return asic.parse_axeos(await fetch_axeos(host))
    if driver == "iceriver":
        return asic.parse_iceriver(await fetch_iceriver(host))
    if driver == "cgminer":
        payload = await fetch_cgminer(host, int(port or CGMINER_PORT))
        return asic.parse_cgminer(payload, payload, payload)
    raise AsicError(f"pilote inconnu : {driver!r}")
