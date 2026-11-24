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
from urllib.parse import urljoin, urlparse, urlsplit

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


class IceRiverSessionError(AsicError):
    """Panneau IceRiver à session : l'étape qui a échoué est connue.

    Distincte d'``AsicError`` pour que la sonde **ne retombe pas** sur l'ancien
    mode ``post=4`` : sur un firmware pbv081, une session refusée doit se lire
    telle quelle, pas se déguiser en panne d'un autre firmware.
    """


class IceRiverLegacyFirmware(AsicError):
    """Le panneau ne sert pas ``/overview`` : firmware ICM168, repli ``post=4``."""


class AsicUnsupported(AsicError):
    """Action non prise en charge par le pilote : notification seule."""


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


async def certificate_der(host: str, port: int = 443, timeout: float = TIMEOUT_S) -> bytes:
    """Certificat DER présenté par ``host:port`` (poignée de main TLS seule)."""
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
    return der


def fingerprint(der: bytes) -> str:
    """Empreinte SHA-256 d'un certificat DER, en hexadécimal séparé par ``:``."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


async def certificate_sha256(host: str, port: int = 443, timeout: float = TIMEOUT_S) -> str:
    """Empreinte SHA-256 du certificat présenté, en hexadécimal séparé par ``:``.

    La vérification de chaîne est volontairement désactivée : les firmwares
    ASIC utilisent un certificat auto-signé (« mkcert development certificate »).
    C'est l'empreinte, épinglée à l'ajout, qui sert d'ancre de confiance.
    """
    return fingerprint(await certificate_der(host, port, timeout))


def _pinned_context(der: bytes):
    """Contexte TLS qui **ne fait confiance qu'au certificat épinglé**.

    ``cadata`` = le certificat observé et validé contre l'empreinte épinglée :
    la poignée de main de la connexion qui porte la requête échoue si le
    serveur présente un autre certificat, avant tout octet HTTP (donc avant
    tout identifiant).  Pas de vérification de nom : les ASIC sont joints par
    adresse IP et leur certificat est auto-signé.
    """
    import ssl

    context = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _normalized(value: str) -> str:
    return str(value or "").replace(":", "").replace(" ", "").upper()


class _Session:
    """Client HTTP d'un ASIC : redirections suivies à la main, hôte unique."""

    def __init__(self, timeout: float = TIMEOUT_S, cert_sha256: str | None = None) -> None:
        self.timeout = timeout
        #: Empreinte épinglée.  Elle n'est **jamais** vérifiée sur une connexion
        #: séparée de celle qui porte la requête : elle sert à valider le
        #: certificat qui construit le contexte TLS de la connexion elle-même.
        self.pin = cert_sha256
        self.https_seen = False
        self.certificates: dict[str, str] = {}
        self.clients: dict[str, Any] = {}
        self._plain_client = None
        self.client = None

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc_info) -> None:
        for client in list(self.clients.values()):
            await client.aclose()
        if self._plain_client is not None:
            await self._plain_client.aclose()

    def _plain(self):
        import httpx

        if self._plain_client is None:
            self._plain_client = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            )
        return self._plain_client

    async def _https(self, url: str):
        """Client HTTPS dont le contexte ne fait confiance qu'au certificat épinglé."""
        import httpx

        parts = urlsplit(url)
        hostname = parts.hostname or ""
        port = parts.port or 443
        origin = f"{parts.scheme}://{hostname}:{port}"
        if origin in self.clients:
            return self.clients[origin]
        # Le certificat est lu une fois pour construire l'ancre de confiance ;
        # l'empreinte épinglée est vérifiée **avant** toute requête, et la
        # connexion qui enverra les identifiants n'accepte que ce certificat.
        der = await certificate_der(hostname, port, self.timeout)
        observed = fingerprint(der)
        _verify_pin(observed, self.pin)
        self.certificates[f"{hostname}:{port}"] = observed
        self.https_seen = True
        self.clients[origin] = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            verify=_pinned_context(der),
        )
        return self.clients[origin]

    def certificate_for(self, hostname: str, port: int | None = None) -> str | None:
        return self.certificates.get(f"{hostname}:{port or 443}")

    async def client_for(self, url: str):
        """Client de l'URL : HTTPS épinglé, HTTP simple ; cookie jar par origine."""
        client = await self._https(url) if url.startswith("https://") else self._plain()
        self.client = client
        return client

    async def request(self, method: str, url: str, **kwargs: Any):
        """Requête + redirections (301/302/303/307/308), même hôte, 3 sauts."""
        import httpx

        body = kwargs.pop("data", None)
        current = url
        current_method = method
        hops = 0
        while True:
            client = await self.client_for(current)
            try:
                response = await client.request(
                    current_method,
                    current,
                    data=body if current_method == method else None,
                    **kwargs,
                )
            except (httpx.HTTPError, OSError) as exc:
                # HTTPError couvre ConnectError/Timeout, mais aussi
                # RemoteProtocolError, ReadError, WriteTimeout et PoolTimeout :
                # sans cette capture, l'exception sortait brute de la sonde.
                raise AsicError(
                    f"{_origin_text(url)} injoignable : {type(exc).__name__}: {exc}"
                ) from exc
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


LOGIN_REFUSAL_MARKERS = ("/user/loginpage", "/notfound")


def _login_target_refused(location: str) -> bool:
    """Redirection de login qui ramène à la page de connexion : refus."""
    lowered = (location or "").lower()
    return any(marker in lowered for marker in LOGIN_REFUSAL_MARKERS)


# ── Login « comme le navigateur » : en-têtes et trace ─────────────────
def _login_headers(base: str) -> dict[str, str]:
    """En-têtes du ``$.ajax`` du panneau IceRiver (mêmes valeurs que le navigateur)."""
    parsed = urlparse(base)
    host = parsed.netloc or parsed.path
    scheme = parsed.scheme or "https"
    return {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{scheme}://{host}/user/loginpage",
        "Origin": f"{scheme}://{host}",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


def _cookie_presence(session: _Session) -> str:
    """Présence du cookie ``esp-app`` — jamais sa valeur."""
    try:
        holder = getattr(session, "client", None) or session
        jar = getattr(getattr(holder, "cookies", None), "jar", None)
        names = [getattr(cookie, "name", "") for cookie in (jar or [])]
    except Exception:  # pragma: no cover - bocal exotique
        names = []
    return "présent" if "esp-app" in names else "absent"


def _login_trace(method: str, url: str, headers: dict[str, str], session: _Session) -> str:
    """Ce que Fossor a envoyé, sans aucune valeur sensible."""
    parts = [f"envoyé : {method} {url}", f"cookie esp-app={_cookie_presence(session)}"]
    for name in ("X-Requested-With", "Accept", "Referer", "Origin", "Content-Type"):
        parts.append(f"{name}={headers.get(name, '')}")
    return " ; ".join(parts)


def _body_says_refused(body: str) -> bool:
    """Le corps d'un 302 dit-il explicitement que les identifiants sont refusés ?"""
    text = (body or "")[:4000]
    lowered = text.lower()
    if '"error"' in lowered or "error:1" in lowered:
        return True
    return any(marker in text for marker in LOGIN_MARKERS)


async def _login(session: _Session, base: str, username: str, password: str) -> str:
    """``POST /user/login`` une fois ; rend la ligne d'étape, ou lève.

    Le POST part comme celui du navigateur (``$.ajax`` du panneau) : mêmes
    en-têtes, même ``Referer``.  La trace dit ce qui a été envoyé — jamais le
    mot de passe : cookie ``esp-app`` présent ou non, schéma, URL, en-têtes.

    Un 302 vers ``/notfound`` n'est un refus d'identifiants que si le corps le
    dit ; sinon la session est refusée après un login pourtant accepté, et la
    trace le montre (le firmware pbv081 répond 200 ``error:1`` à de mauvais
    identifiants, mais 302 vers ``/notfound`` à de bons identifiants dans
    certaines configurations).
    """
    url = base + "/user/login"
    data = {"username": username, "password": password}
    headers = _login_headers(base)
    trace = _login_trace("POST", url, headers, session)
    response = await _single(session, "POST", url, data=data, headers=headers)
    # Panneau en clair : tout part en ``301 https://`` — la montée est suivie
    # une fois et le POST rejoué tel quel (même chemin, même corps).
    for _hop in range(MAX_REDIRECTS):
        location = response.headers.get("location") or ""
        if response.status_code in (301, 307, 308) and location.startswith("https://"):
            target = urljoin(url, location)
            if not _same_host(url, target):
                raise AsicError(f"redirection vers un autre hôte refusée : {target}")
            url = target
            trace = _login_trace("POST", url, headers, session)
            response = await _single(session, "POST", url, data=data, headers=headers)
            continue
        break
    payload = _json_or_none(response)
    if payload is not None:
        error = payload.get("error")
        if error not in (None, 0, "0", False, ""):
            message = str(payload.get("message") or "").strip()
            raise AsicAuthError(
                f"identifiants refusés : login : error={error}"
                + (f", message={message}" if message else "")
                + f" ; {trace}"
            )
        return f"login : error=0 ; {trace}"
    location = response.headers.get("location") or ""
    if response.status_code in (302, 303) and location:
        step = f"login : {response.status_code} → {location}"
        if _login_target_refused(location):
            if "loginpage" in location.lower() or _body_says_refused(response.text or ""):
                raise AsicAuthError(f"identifiants refusés : {step} ; {trace}")
            raise AsicAuthError(
                f"session refusée après login ({response.status_code} → {location})"
                " — voir la trace : " + trace
            )
        # ``302 → /`` : succès, la session vient du Set-Cookie de la réponse.
        return f"{step} ; {trace}"
    if _looks_like_login(response):
        raise AsicAuthError(
            f"identifiants refusés : le panneau réaffiche la page de connexion ; {trace}"
        )
    raise AsicAuthError(
        f"login : réponse non JSON ({response.status_code}"
        + (f" → {location}" if location else "") + f") ; {trace}"
    )


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


LOGIN_REQUIRED_TEXT = (
    "identifiants requis : le panneau IceRiver est servi en HTTPS et exige une connexion"
)
OVERVIEW_PATH = "/overview"
_UA = {"User-Agent": "Fossor/0.1"}


async def _single(session: _Session, method: str, url: str, **kwargs: Any):
    """Une requête, redirections **non** suivies : on veut constater l'étape."""
    import httpx

    client = await session.client_for(url)
    try:
        return await client.request(method, url, **kwargs)
    except (httpx.HTTPError, OSError) as exc:
        raise AsicError(
            f"{_origin_text(url)} injoignable : {type(exc).__name__}: {exc}"
        ) from exc


def _cookie_count(session: _Session) -> int:
    """Nombre de cookies reçus — jamais leur valeur, jamais leur nom en clair."""
    try:
        if session.client is None:
            return 0
        return len(list(session.client.cookies.jar))
    except Exception:
        return 0


def _step_line(status: int, location: str, cookie: bool) -> str:
    target = f" → {location}" if location else ""
    return f"étape GET / : {status}{target}, cookie reçu {'oui' if cookie else 'non'}"


async def _root(session: _Session, base: str):
    """``GET /`` puis la seule montée ``http -> https`` du même hôte."""
    url = base + "/"
    response = await _single(session, "GET", url, headers=_UA)
    for _hop in range(MAX_REDIRECTS):
        location = response.headers.get("location") or ""
        if response.status_code in (301, 302, 303, 307, 308) and location.startswith("https://"):
            target = urljoin(url, location)
            if not _same_host(url, target):
                raise AsicError(f"redirection vers un autre hôte refusée : {target}")
            url = target
            response = await _single(session, "GET", url, headers=_UA)
            continue
        break
    return response, url


async def _open_panel(
    session: _Session, base: str, username: str, password: str
) -> tuple[list[str], str]:
    """``GET /`` (session) puis ``POST /user/login`` ; rend (trace, base https)."""
    response, used = await _root(session, base)
    # Une fois la montée ``http -> https`` faite, tout le reste s'adresse à
    # l'origine HTTPS : le port 80 du firmware ne fait que rediriger.
    panel_base = used.rstrip("/")
    location = response.headers.get("location") or ""
    cookie = _cookie_count(session) > 0
    step = _step_line(response.status_code, location, cookie)
    if response.status_code >= 400:
        raise AsicError(step)
    if not cookie:
        raise AsicError(step + " — aucune session posée par le panneau")
    login_step = await _login(session, panel_base, username, password)
    return [step, login_step], panel_base


async def _read_overview(session: _Session, base: str) -> tuple[dict[str, Any] | None, str]:
    """``GET /overview`` ; rend (charge JSON, échec lisible)."""
    url = base + OVERVIEW_PATH
    response = await _single(session, "GET", url, headers=_UA)
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("location") or ""
        target = urljoin(url, location)
        if location.startswith("https://") and _same_host(url, target):
            url = target
            response = await _single(session, "GET", url, headers=_UA)
    location = response.headers.get("location") or ""
    if response.status_code in (301, 302, 303, 307, 308):
        return None, f"/overview : {response.status_code}" + (f" → {location}" if location else "")
    if response.status_code != 200:
        return None, f"/overview : {response.status_code}"
    payload = _json_or_none(response)
    if payload is None:
        return None, f"/overview : réponse non JSON ({response.status_code})"
    return payload, ""


async def _overview_flow(
    session: _Session, base: str, username: str, password: str
) -> dict[str, Any]:
    """GET / → login → /overview ; **un seul** nouvel essai si la session tombe."""
    try:
        steps, panel_base = await _open_panel(session, base, username, password)
    except (AsicAuthError, IceRiverSessionError):
        raise
    except AsicError as exc:
        # ``GET /`` n'a pas ouvert de session : ``/overview`` tranche entre
        # session absente (panneau réel) et firmware qui ignore ``GET``.
        _payload, failure = await _read_overview(session, base)
        if _payload is not None:
            return {"payload": _payload, "steps": [str(exc)]}
        if failure.startswith("/overview : 404"):
            # Panneau sans /overview (ICM168) : l'appelant tente ``post=4``.
            raise IceRiverLegacyFirmware(f"{exc} ; {failure}") from exc
        if "loginpage" in str(exc) or "notfound" in str(exc):
            # Page de connexion servie : panneau IceRiver, session absente.
            raise IceRiverSessionError(f"{exc} ; {failure}") from exc
        # 405/404 sur ``GET /`` : GET ignoré par l'ancien firmware.
        raise IceRiverLegacyFirmware(f"{exc} ; {failure}") from exc
    payload, failure = await _read_overview(session, panel_base)
    if payload is not None:
        return {"payload": payload, "steps": steps}
    if failure.startswith("/overview : 404"):
        # Firmware sans /overview : le panneau ``post=4`` reste la source.
        raise IceRiverLegacyFirmware(f"{' ; '.join(steps)} ; {failure}")
    steps.append(failure)
    more, panel_base = await _open_panel(session, base, username, password)
    steps.extend(more)
    payload, failure = await _read_overview(session, panel_base)
    if payload is not None:
        return {"payload": payload, "steps": steps}
    steps.append(failure)
    raise AsicAuthError(
        f"session refusée : {failure} après un nouveau login (GET / + login)"
    )


def _observed_certificate(session: _Session, host: str) -> str | None:
    hostname, port = split_host(host)
    certificate = session.certificate_for(hostname, port) if session.https_seen else None
    if certificate is None:
        observed = list(session.certificates.values())
        certificate = observed[0] if observed else None
    return certificate


def _verify_pin(certificate: str | None, cert_sha256: str | None) -> None:
    if cert_sha256 and certificate and _normalized(certificate) != _normalized(cert_sha256):
        raise AsicCertError(
            f"{CERT_CHANGED} : épinglé {cert_sha256}, présenté {certificate} — "
            "ré-épinglez le certificat après vérification"
        )


def iceriver_state(found: dict[str, Any]) -> dict[str, Any]:
    """État normalisé d'une sonde IceRiver, selon le firmware rencontré."""
    payload = found.get("payload")
    if payload is None:
        return _login_state(found.get("cert_sha256"), found.get("error") or LOGIN_REQUIRED_TEXT)
    if found.get("mode") == "overview" or asic.iceriver_overview_like(payload):
        return asic.parse_iceriver_overview(payload)
    return asic.parse_iceriver(payload)


async def iceriver_identity(host: str, timeout: float = TIMEOUT_S) -> bool:
    """``GET /`` se présente-t-il comme un panneau IceRiver (``用户界面``) ?"""
    base = _url(host, "")
    async with _Session(timeout) as session:
        response, _used = await _root(session, base)
        location = (response.headers.get("location") or "").lower()
        if response.status_code in (301, 302, 303, 307, 308) and "loginpage" in location:
            return True
        text = (response.text or "")[:4000]
        return any(marker in text for marker in LOGIN_MARKERS)


async def iceriver_probe(
    host: str,
    username: str = "",
    password: str = "",
    cert_sha256: str | None = None,
    timeout: float = TIMEOUT_S,
) -> dict[str, Any]:
    """Sonde IceRiver : ``/overview`` (pbv081) ou panneau ``post=4`` (ICM168).

    Le firmware pbv081 exige une session : ``GET /`` (cookie) ->
    ``POST /user/login`` -> ``GET /overview``.  Un panneau qui ne pose pas de
    cookie ou n'a pas ``/overview`` fait retomber sur l'ancien mode, conservé
    pour l'ancien firmware.  Chaque échec porte l'étape et l'état observé,
    jamais le mot de passe ni le cookie.
    """
    base = _url(host, "")
    async with _Session(timeout, cert_sha256) as session:
        steps: list[str] = []
        if username and password:
            try:
                opened = await _overview_flow(session, base, username, password)
            except (AsicAuthError, IceRiverSessionError):
                raise
            except IceRiverLegacyFirmware as exc:
                # Firmware sans /overview : repli ``post=4`` ci-dessous.
                steps.append(str(exc))
            except AsicError:
                # Pas de session posée : ancien firmware ci-dessous.
                pass
            else:
                certificate = _observed_certificate(session, host)
                _verify_pin(certificate, cert_sha256)
                return {
                    "payload": opened["payload"],
                    "mode": "overview",
                    "https": session.https_seen,
                    "login_required": True,
                    "cert_sha256": certificate,
                    "steps": opened["steps"],
                }
        response = await session.request("POST", base + "/user/userpanel", data={"post": "4"})
        https = session.https_seen
        certificate = _observed_certificate(session, host)
        _verify_pin(certificate, cert_sha256)
        payload = _json_or_none(response)
        if payload is not None:
            return {
                "payload": payload,
                "mode": "panel",
                "https": https,
                "login_required": False,
                "cert_sha256": certificate,
                "steps": steps,
            }
        if not _looks_like_login(response):
            raise AsicError(
                f"{base} : réponse inattendue ({response.status_code}) sur /user/userpanel"
            )
        if not (username and password):
            return {
                "payload": None,
                "mode": "panel",
                "https": https,
                "login_required": True,
                "cert_sha256": certificate,
                "error": LOGIN_REQUIRED_TEXT,
                "steps": steps,
            }
        panel = await _login_then_panel(session, base, username, password)
        return {
            "payload": panel,
            "mode": "panel",
            "https": True,
            "login_required": True,
            "cert_sha256": certificate,
            "steps": steps,
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


async def restart_axeos(host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """``POST /api/system/restart`` — AxeOS.

    Source : ESP-Miner **v2.12.2**, ``main/http_server/http_server.c``
    lignes 1318-1323 (``.uri = "/api/system/restart", .method = HTTP_POST,
    .handler = POST_restart``) ; le handler répond « System will restart
    shortly. » puis appelle ``esp_restart()``.
    """
    url = _url(host, "/api/system/restart")
    async with _Session(timeout) as session:
        response = await session.request("POST", url, headers={"User-Agent": "Fossor/0.1"})
        try:
            response.raise_for_status()
        except Exception as exc:
            raise AsicError(f"{url} : {exc}") from exc
    return {"ok": True, "status": response.status_code, "path": "/api/system/restart"}


async def restart_asic(driver: str, host: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Redémarre un ASIC par le pilote de la machine.

    AxeOS : ``POST /api/system/restart`` (ESP-Miner v2.12.2).  IceRiver et
    cgminer n'ont aucun endpoint documenté : :class:`AsicUnsupported`, que le
    watchdog traduit en simple notification.
    """
    if (driver or "").strip().lower() != "axeos":
        raise AsicUnsupported(
            f"pilote {driver or 'inconnu'} : redémarrage non pris en charge, "
            "aucun endpoint documenté"
        )
    return await restart_axeos(host, timeout=timeout)


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

    IceRiver est reconnu **avant** AxeOS : l'identité ``GET /`` d'abord, puis
    le panneau ``POST /user/userpanel`` (``post=4``) de l'ancien firmware, qui
    répond 405 à tout ``GET``.  Un panneau reconnu ne bascule jamais sur AxeOS ;
    l'échec d'un autre pilote passe au suivant.
    """
    errors: list[str] = []
    try:
        is_iceriver = await iceriver_identity(host, timeout)
    except AsicError as exc:
        errors.append(str(exc))
        is_iceriver = False
    def _iceriver_result(found: dict[str, Any]) -> dict[str, Any]:
        return {
            "driver": "iceriver",
            "state": iceriver_state(found),
            "payload": found["payload"],
            "https": found["https"],
            "login_required": found["login_required"],
            "cert_sha256": found.get("cert_sha256"),
            "errors": errors,
        }

    if is_iceriver:
        # Un panneau IceRiver n'expose pas /api/system/info : **aucun** repli
        # AxeOS, même quand la lecture échoue — l'erreur dit où ça coince.
        found = await iceriver_probe(host, username, password, cert_sha256, timeout)
        return _iceriver_result(found)
    # ``GET /`` n'a pas répondu comme un panneau à session.  L'ancien firmware
    # ICM168 ne sert que ``POST /user/userpanel`` (``post=4``) et renvoie 405
    # sur tout GET : c'est sa réponse JSON qui l'identifie, **avant** AxeOS.
    try:
        legacy = await iceriver_probe(host, username, password, cert_sha256, timeout)
    except (AsicAuthError, IceRiverSessionError):
        # Page de connexion / session refusée : c'est un panneau, pas un AxeOS.
        raise
    except AsicError as exc:
        errors.append(str(exc))
    else:
        return _iceriver_result(legacy)
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
        found = await iceriver_probe(
            host, username=username, password=password, cert_sha256=cert_sha256
        )
        if found["payload"] is None:
            raise AsicAuthError(LOGIN_REQUIRED_TEXT)
        return iceriver_state(found)
    if driver == "cgminer":
        payload = await fetch_cgminer(host, int(port or CGMINER_PORT))
        return asic.parse_cgminer(payload, payload, payload)
    raise AsicError(f"pilote inconnu : {driver!r}")
