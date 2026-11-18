"""Lecture réseau des ASIC (§4.20) — HTTP/HTTPS et TCP 4028, **sans pilotage**.

Toutes les fonctions sont en lecture seule : ``GET /api/system/info`` (AxeOS),
``POST /user/userpanel`` avec ``post=4`` (IceRiver) et les commandes
``summary``/``stats``/``pools`` de l'API cgminer.  Aucune commande d'écriture
n'est construite ici ; aucun identifiant de pool n'est lu.

Depuis la 2026.11.008, le firmware IceRiver ``ICM168_3_2_14`` sert son panneau
en **HTTPS** avec un certificat auto-signé et exige une connexion :

* ``http://<ip>/`` répond ``301`` vers ``https`` ;
* ``GET https://<ip>/`` répond ``302 /user/loginpage`` + cookie ``esp-app`` ;
* ``POST /user/login`` (form-urlencoded) rend un JSON ``error``/``message`` ;
* ``POST /user/userpanel`` sans session répond ``302 /notfound``.

L'empreinte SHA-256 du certificat est **épinglée** à l'ajout, comme une clé
d'hôte SSH : elle est affichée, puis refusée si elle change.  Les redirections
sont suivies à la main (3 au plus, même hôte) pour qu'un ``http -> https`` ne
passe pas pour une panne.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from app import asic

TIMEOUT_S = 6.0
CGMINER_PORT = 4028
MAX_REDIRECTS = 3
LOGIN_MARKERS = ("\u7528\u6237\u754c\u9762", "/user/loginpage")
CERT_CHANGED = "certificat changé"

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")


class AsicError(RuntimeError):
    """Erreur de lecture d'un ASIC, message destiné à l'UI."""


class AsicAuthError(AsicError):
    """Le panneau refuse la connexion : identifiants absents ou refusés."""


class AsicCertError(AsicError):
    """Le certificat HTTPS de l'ASIC n'est plus celui qui a été épinglé."""


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


def split_host(host: str) -> tuple[str, int | None]:
    """``192.0.2.10:8443`` -> ``("192.0.2.10", 8443)`` (port absent -> ``None``)."""
    text = clean_host(host)
    if text.count(":") == 1:
        head, tail = text.split(":")
        if tail.isdigit():
            return head, int(tail)
    return text.strip("[]"), None


def _url(host: str, path: str) -> str:
    host = clean_host(host)
    if host.count(":") > 1 and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}{path}"


def _same_host(first: str, second: str) -> bool:
    """Même hôte pour une redirection : le nom seul, jamais un saut de domaine."""
    return (urlsplit(first).hostname or "").lower() == (urlsplit(second).hostname or "").lower()


def _origin_text(url: str) -> str:
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


async def certificate_sha256(host: str, port: int = 443, timeout: float = TIMEOUT_S) -> str:
    """Empreinte SHA-256 du certificat présenté, en hexadécimal séparé par ``:``.

    La vérification de chaîne est volontairement désactivée : les firmwares
    ASIC utilisent un certificat auto-signé (« mkcert development certificate »).
    C'est l'empreinte, épinglée à l'ajout, qui sert d'ancre de confiance.
    """
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port), ssl=context, server_hostname=host),
            timeout=timeout,
        )
    except Exception as exc:
        raise AsicError(f"certificat illisible sur {host}:{port} : {exc}") from exc
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    if not der:
        raise AsicError(f"certificat illisible sur {host}:{port} : aucun certificat présenté")
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def _normalized(value: str) -> str:
    return str(value or "").replace(":", "").replace(" ", "").upper()


class _Session:
    """Client HTTP d'un ASIC : redirections suivies à la main, hôte unique."""

    def __init__(self, timeout: float = TIMEOUT_S) -> None:
        import httpx

        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=False)
        self.https_seen = False
        self.certificates: dict[str, str] = {}

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.client.aclose()

    async def _note_certificate(self, url: str) -> None:
        """Calcule l'empreinte du certificat au premier passage en HTTPS."""
        self.https_seen = True
        parts = urlsplit(url)
        origin = f"{parts.hostname}:{parts.port or 443}"
        if origin in self.certificates:
            return
        hostname = parts.hostname or ""
        self.certificates[origin] = await certificate_sha256(
            hostname, parts.port or 443, self.timeout
        )

    def certificate_for(self, hostname: str, port: int | None = None) -> str | None:
        return self.certificates.get(f"{hostname}:{port or 443}")

    async def request(self, method: str, url: str, **kwargs: Any):
        """Requête + redirections (301/302/303/307/308), même hôte, 3 sauts."""
        import httpx

        body = kwargs.pop("data", None)
        current = url
        current_method = method
        hops = 0
        while True:
            if current.startswith("https://"):
                await self._note_certificate(current)
            try:
                response = await self.client.request(
                    current_method,
                    current,
                    data=body if current_method == method else None,
                    **kwargs,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError) as exc:
                raise AsicError(f"{_origin_text(url)} injoignable : {exc}") from exc
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location") or ""
            if not location or hops >= MAX_REDIRECTS:
                return response
            target = urljoin(current, location)
            if not _same_host(current, target):
                raise AsicError(f"redirection vers un autre hôte refusée : {target}")
            hops += 1
            # ``http -> https`` sur le **même chemin** : c'est une montée de
            # schéma, pas un déplacement de ressource — le POST ``post=4`` doit
            # être rejoué tel quel (le firmware ICM168 répond 302 /notfound au
            # panneau, ce qui prouve que la requête arrive bien en POST).
            scheme_only = (
                urlsplit(current).scheme != urlsplit(target).scheme
                and urlsplit(current).path == urlsplit(target).path
            )
            if response.status_code in (301, 302, 303) and not scheme_only:
                current_method = "GET"
                body = None
            current = target


def _json_or_none(response) -> dict[str, Any] | None:
    """JSON exploitable, ou ``None`` (page HTML, corps vide, 3xx/4xx)."""
    if response.status_code >= 300:
        return None
    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        text = (response.text or "").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
    return payload if isinstance(payload, dict) else None


def _looks_like_login(response) -> bool:
    """Page de connexion IceRiver : ``302 /user/loginpage`` ou titre ``用户界面``."""
    location = (response.headers.get("location") or "").lower()
    # ``/user/loginpage`` (page de connexion) ou ``/notfound`` : c'est ainsi que
    # le firmware ICM168 répond à ``post=4`` quand la session manque.
    if response.status_code in (301, 302, 303, 307, 308) and (
        "loginpage" in location or "notfound" in location
    ):
        return True
    if "/notfound" in str(getattr(response, "url", "")):
        # Le firmware redirige vers /notfound quand la session (cookie
        # ``esp-app``) manque : c'est un refus de session, pas une panne.
        return True
    text = (response.text or "")[:4000]
    return any(marker in text for marker in LOGIN_MARKERS)


async def _login(session: _Session, base: str, username: str, password: str) -> None:
    response = await session.request(
        "POST", base + "/user/login", data={"username": username, "password": password}
    )
    payload = _json_or_none(response)
    if payload is not None:
        error = payload.get("error")
        if error not in (None, 0, "0", False, ""):
            message = str(payload.get("message") or "").strip()
            raise AsicAuthError(
                "identifiants refusés" + (f" : {message}" if message else "")
            )
        return
    if _looks_like_login(response):
        raise AsicAuthError("identifiants refusés : le panneau réaffiche la page de connexion")


async def _login_then_panel(
    session: _Session, base: str, username: str, password: str
) -> dict[str, Any]:
    """Connexion puis panneau ; une seule reconnexion si la session est perdue."""
    await _login(session, base, username, password)
    for attempt in range(2):
        response = await session.request("POST", base + "/user/userpanel", data={"post": "4"})
        payload = _json_or_none(response)
        if payload is not None:
            return payload
        if attempt == 0:
            # 302 vers /user/loginpage ou /notfound : le plugin a perdu la session.
            await _login(session, base, username, password)
    raise AsicAuthError(
        "session refusée : le panneau renvoie la page de connexion après un nouveau login"
    )


async def iceriver_probe(
    host: str,
    username: str = "",
    password: str = "",
    cert_sha256: str | None = None,
    timeout: float = TIMEOUT_S,
) -> dict[str, Any]:
    """Sonde IceRiver : panneau sans login (ancien firmware) ou HTTPS + login.

    Rend ``payload`` (``None`` si la connexion est requise mais absente),
    ``https``, ``login_required`` et ``cert_sha256`` (empreinte observée).
    """
    base = _url(host, "")
    async with _Session(timeout) as session:
        response = await session.request("POST", base + "/user/userpanel", data={"post": "4"})
        https = session.https_seen
        hostname, port = split_host(host)
        certificate = session.certificate_for(hostname, port) if https else None
        if certificate is None:
            observed = list(session.certificates.values())
            certificate = observed[0] if observed else None
        if https and cert_sha256 and certificate and _normalized(certificate) != _normalized(cert_sha256):
            raise AsicCertError(
                f"{CERT_CHANGED} : épinglé {cert_sha256}, présenté {certificate} — "
                "ré-épinglez le certificat après vérification"
            )
        payload = _json_or_none(response)
        if payload is not None:
            return {
                "payload": payload,
                "https": https,
                "login_required": False,
                "cert_sha256": certificate,
            }
        if not _looks_like_login(response):
            raise AsicError(
                f"{base} : réponse inattendue ({response.status_code}) sur /user/userpanel"
            )
        if not (username and password):
            return {
                "payload": None,
                "https": https,
                "login_required": True,
                "cert_sha256": certificate,
            }
        panel = await _login_then_panel(session, base, username, password)
        return {
            "payload": panel,
            "https": True,
            "login_required": True,
            "cert_sha256": certificate,
        }


async def fetch_axeos(host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """``GET /api/system/info`` — Bitaxe / NerdQaxe (301 http -> https suivi)."""
    url = _url(host, "/api/system/info")
    async with _Session(timeout) as session:
        response = await session.request("GET", url, headers={"User-Agent": "Fossor/0.1"})
        try:
            response.raise_for_status()
        except Exception as exc:
            raise AsicError(f"{url} : {exc}") from exc
        payload = response.json()
    if not isinstance(payload, dict):
        raise AsicError(f"{url} : réponse inattendue")
    return payload


async def fetch_iceriver(
    host: str,
    timeout: float = TIMEOUT_S,
    *,
    username: str = "",
    password: str = "",
    cert_sha256: str | None = None,
) -> dict[str, Any]:
    """``POST /user/userpanel`` avec ``post=4`` — firmware IceRiver ICM168."""
    found = await iceriver_probe(host, username, password, cert_sha256, timeout)
    if found["payload"] is None:
        raise AsicAuthError(
            "identifiants requis : le panneau IceRiver est servi en HTTPS et exige une connexion"
        )
    return found["payload"]


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
            asyncio.open_connection(*split_host(target)[:1], int(port)), timeout=timeout
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


def _login_state(cert_sha256: str | None, error: str) -> dict[str, Any]:
    """État minimal d'un IceRiver reconnu dont le panneau exige une connexion."""
    return {
        "driver": "iceriver",
        "online": True,
        "requires_login": True,
        "model": "",
        "firmware": "",
        "board_version": "",
        "asic_model": "",
        "hashrate_hs": None,
        "expected_hashrate_hs": None,
        "nominal_pct": None,
        "power_w": None,
        "temp_c": None,
        "fan_rpm": None,
        "fans_zero": False,
        "pool_connected": None,
        "shares_accepted": None,
        "shares_rejected": None,
        "reject_pct": None,
        "uptime_s": None,
        "domains_total": 0,
        "domains_inactive": 0,
        "algorithm": "kHeavyHash",
        "coin": "KAS",
        "read_only": True,
        "format_verified": True,
        "login_required": True,
        "cert_sha256": cert_sha256 or "",
        "error": error,
    }


async def detect(
    host: str,
    timeout: float = TIMEOUT_S,
    *,
    username: str = "",
    password: str = "",
    cert_sha256: str | None = None,
) -> dict[str, Any]:
    """Sonde en lecture seule : reconnaît le pilote d'un ASIC.

    IceRiver est reconnu **avant** AxeOS (le firmware ICM168 en HTTPS répond à
    ``/user/userpanel`` par la page de connexion, pas par du JSON).  L'échec
    d'un pilote passe au suivant ; aucun ne fait échouer la détection entière.
    """
    errors: list[str] = []
    try:
        found = await iceriver_probe(host, username, password, cert_sha256, timeout)
        state = (
            asic.parse_iceriver(found["payload"])
            if found["payload"] is not None
            else _login_state(
                found.get("cert_sha256"),
                "identifiants requis : le panneau IceRiver est servi en HTTPS et exige une connexion",
            )
        )
        return {
            "driver": "iceriver",
            "state": state,
            "payload": found["payload"],
            "https": found["https"],
            "login_required": found["login_required"],
            "cert_sha256": found.get("cert_sha256"),
            "errors": errors,
        }
    except (AsicAuthError, AsicCertError):
        raise
    except AsicError as exc:
        errors.append(str(exc))
    try:
        payload = await fetch_axeos(host, timeout)
        state = asic.parse_axeos(payload)
        return {
            "driver": "axeos",
            "state": state,
            "payload": payload,
            "https": False,
            "login_required": False,
            "cert_sha256": None,
            "errors": errors,
        }
    except AsicError as exc:
        errors.append(str(exc))
    try:
        payload = await fetch_cgminer(host, CGMINER_PORT, timeout=timeout)
        state = asic.parse_cgminer(payload, payload, payload)
        return {
            "driver": "cgminer",
            "state": state,
            "payload": payload,
            "https": False,
            "login_required": False,
            "cert_sha256": None,
            "errors": errors,
        }
    except AsicError as exc:
        errors.append(str(exc))
    raise AsicError("pilote non reconnu — " + " | ".join(errors[-3:]))


async def poll(
    driver: str,
    host: str,
    port: int | None = None,
    *,
    username: str = "",
    password: str = "",
    cert_sha256: str | None = None,
) -> dict[str, Any]:
    """Interroge un ASIC selon son pilote et rend l'état normalisé."""
    if driver == "axeos":
        return asic.parse_axeos(await fetch_axeos(host))
    if driver == "iceriver":
        payload = await fetch_iceriver(
            host, username=username, password=password, cert_sha256=cert_sha256
        )
        return asic.parse_iceriver(payload)
    if driver == "cgminer":
        payload = await fetch_cgminer(host, int(port or CGMINER_PORT))
        return asic.parse_cgminer(payload, payload, payload)
    raise AsicError(f"pilote inconnu : {driver!r}")
