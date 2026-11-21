"""ASIC (§4.20) — IceRiver en HTTPS avec connexion (2026.11.008).

Faux serveur **local** : certificat auto-signé généré dans le test, redirections
``http -> https`` puis ``/user/loginpage``, formulaire ``#login``, cookie
``esp-app``, ``POST /user/userpanel`` sans session -> ``302 /notfound``.  Pour
``post=4`` il renvoie exactement la fixture réelle du KS0 Ultra
(``iceriver-userpanel-post4.json``, firmware ``ICM168_3_2_14``).

Aucun ASIC de Fred n'est contacté ; aucun mot de passe réel n'apparaît.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from app import asic, asic_client

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ICERIVER = json.loads((FIXTURES / "iceriver-userpanel-post4.json").read_text())
AXEOS = json.loads((FIXTURES / "axeos-system-info.json").read_text())

USER = "fossor-test"
PASSWORD = "motdepasse-factice-0000"
LOGIN_HTML = (
    "<html><head><title>\u7528\u6237\u754c\u9762</title></head>"
    '<body><form id="login"><input name="username"><input name="password"></form></body></html>'
)


def _write_cert(directory: Path) -> tuple[Path, Path, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fossor-test")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "key.pem"
    cert_path = directory / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    digest = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return cert_path, key_path, ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _cookie_token(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "esp-app":
            return value
    return ""


class _PanelHandler(BaseHTTPRequestHandler):
    """Faux firmware ICM168 : login, cookie, panneau, redirects."""

    protocol_version = "HTTP/1.1"
    records: list[dict] = []
    sessions: set[str] = set()
    drop_next_panel = False
    serve_axeos = False
    panel_enabled = True
    #: L'ancien firmware sert le panneau sans session ; le nouveau exige le cookie.
    require_session = True

    def log_message(self, *args) -> None:  # bruit des tests
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json",
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _record(self, method: str, form: dict[str, list[str]]) -> None:
        entry: dict = {"method": method, "path": self.path}
        if form:
            entry["post"] = (form.get("post") or [""])[0]
            entry["fields"] = sorted(form)
            if "password" in form:
                # Le journal de preuve ne porte jamais le mot de passe : sa
                # longueur et le verdict suffisent.
                entry["password_len"] = len(form["password"][0])
                entry["password_accepted"] = form["password"][0] == PASSWORD
        type(self).records.append(entry)

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET", {})
        if self.path == "/":
            self._send(302, extra=(("Location", "/user/loginpage"),))
            return
        if self.path == "/user/loginpage":
            self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/notfound":
            self._send(200, b"<html>not found</html>", "text/html; charset=utf-8")
            return
        if self.path == "/api/system/info" and type(self).serve_axeos:
            self._send(200, json.dumps(AXEOS).encode())
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        form = parse_qs(body.decode("utf-8", "replace"))
        self._record("POST", form)
        if self.path == "/user/login":
            user = (form.get("username") or [""])[0]
            password = (form.get("password") or [""])[0]
            if user == USER and password == PASSWORD:
                token = secrets.token_hex(8)
                type(self).sessions.add(token)
                self._send(200, json.dumps({"error": 0, "message": "ok"}).encode(),
                           extra=(("Set-Cookie", f"esp-app={token}; secure; httponly; Path=/"),))
            else:
                self._send(200, json.dumps({"error": 1, "message": "invalid user or password"}).encode())
            return
        if self.path == "/user/userpanel":
            if not type(self).panel_enabled:
                self._send(404, b"{}")
                return
            if (form.get("post") or [""])[0] != "4":
                self._send(400, b"{}")
                return
            if not type(self).require_session:
                # Firmware d'avant le 18/11 : panneau nu, sans authentification.
                self._send(200, (FIXTURES / "iceriver-userpanel-post4.json").read_bytes())
                return
            token = _cookie_token(self.headers.get("Cookie") or "")
            if type(self).drop_next_panel and token in type(self).sessions:
                type(self).drop_next_panel = False
                type(self).sessions.discard(token)
                self._send(302, extra=(("Location", "/notfound"),))
                return
            if token and token in type(self).sessions:
                self._send(200, (FIXTURES / "iceriver-userpanel-post4.json").read_bytes())
                return
            self._send(302, extra=(("Location", "/notfound"),))
            return
        self._send(404, b"{}")


class _RedirectHandler(BaseHTTPRequestHandler):
    """Ancien port 80 du firmware : tout part en 301 vers HTTPS."""

    protocol_version = "HTTP/1.1"
    target = ""

    def log_message(self, *args) -> None:
        pass

    def _redirect(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(301)
        self.send_header("Location", type(self).target + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _redirect  # noqa: N815
    do_POST = _redirect  # noqa: N815


def _reset() -> None:
    _PanelHandler.records = []
    _PanelHandler.sessions = set()
    _PanelHandler.drop_next_panel = False
    _PanelHandler.serve_axeos = False
    _PanelHandler.panel_enabled = True
    _PanelHandler.require_session = True


def _serve(server: ThreadingHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture()
def https_panel(tmp_path):
    _reset()
    cert_path, key_path, fingerprint = _write_cert(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PanelHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = _serve(server)
    port = server.server_address[1]
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    _RedirectHandler.target = f"https://127.0.0.1:{port}"
    rthread = _serve(redirect)
    try:
        yield {
            "host": f"127.0.0.1:{redirect.server_address[1]}",
            "fingerprint": fingerprint,
        }
    finally:
        for running, worker in ((redirect, rthread), (server, thread)):
            running.shutdown()
            running.server_close()
            worker.join(timeout=5)


@pytest.fixture()
def plain_panel():
    _reset()
    _PanelHandler.require_session = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PanelHandler)
    thread = _serve(server)
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_https_login_pins_certificate_and_reads_the_real_fixture(https_panel):
    found = await asic_client.detect(https_panel["host"])
    assert found["driver"] == "iceriver"
    assert found["https"] is True
    assert found["login_required"] is True
    assert found["cert_sha256"] == https_panel["fingerprint"]
    assert found["state"]["requires_login"] is True
    verdict = asic.health(found["state"])
    assert verdict["status"] == "orange"
    assert "identifiants requis" in verdict["reason"]

    state = await asic_client.poll(
        "iceriver",
        https_panel["host"],
        username=USER,
        password=PASSWORD,
        cert_sha256=found["cert_sha256"],
    )
    expected = asic.parse_iceriver(ICERIVER)
    assert state["driver"] == "iceriver"
    assert state["hashrate_hs"] == pytest.approx(expected["hashrate_hs"])
    assert state["nominal_pct"] == pytest.approx(expected["nominal_pct"])

    sent = json.dumps(_PanelHandler.records)
    assert PASSWORD not in sent
    assert any(
        row["path"] == "/user/userpanel" and row.get("post") == "4"
        for row in _PanelHandler.records
    )
    login_rows = [row for row in _PanelHandler.records if row["path"] == "/user/login"]
    assert login_rows and login_rows[0]["password_len"] == len(PASSWORD)


async def test_wrong_password_is_refused_and_host_down_is_distinct(https_panel):
    with pytest.raises(asic_client.AsicAuthError) as refused:
        await asic_client.poll(
            "iceriver", https_panel["host"], username=USER, password="mauvais-mot-de-passe"
        )
    assert "identifiants refusés" in str(refused.value)

    with pytest.raises(asic_client.AsicError) as unreachable:
        await asic_client.poll("iceriver", "127.0.0.1:1", username=USER, password=PASSWORD)
    assert not isinstance(unreachable.value, asic_client.AsicAuthError)
    assert "injoignable" in str(unreachable.value)


async def test_changed_certificate_is_refused(https_panel):
    with pytest.raises(asic_client.AsicCertError) as changed:
        await asic_client.poll(
            "iceriver",
            https_panel["host"],
            username=USER,
            password=PASSWORD,
            cert_sha256="00:11:22:33",
        )
    assert "certificat changé" in str(changed.value)


async def test_other_certificate_refused_before_any_credential(https_panel, tmp_path):
    """Un autre certificat que l'épinglé : refus avant tout envoi d'identifiant.

    Le faux serveur journalise chaque requête HTTP reçue : son journal doit
    rester vide, ce qui prouve que le mot de passe n'est jamais parti.
    """
    autre = tmp_path / "autre"
    autre.mkdir()
    _cert, _key, autre_empreinte = _write_cert(autre)
    _PanelHandler.records = []
    with pytest.raises(asic_client.AsicCertError) as changed:
        await asic_client.poll(
            "iceriver",
            https_panel["host"],
            username=USER,
            password=PASSWORD,
            cert_sha256=autre_empreinte,
        )
    assert "certificat changé" in str(changed.value)
    assert _PanelHandler.records == [], _PanelHandler.records
    assert PASSWORD not in json.dumps(_PanelHandler.records)


async def test_expired_session_does_one_new_login(https_panel):
    _PanelHandler.drop_next_panel = True
    state = await asic_client.poll(
        "iceriver", https_panel["host"], username=USER, password=PASSWORD
    )
    assert state["online"] is True
    logins = [row for row in _PanelHandler.records if row["path"] == "/user/login"]
    assert len(logins) == 2, _PanelHandler.records


async def test_old_firmware_http_needs_no_login(plain_panel):
    # Le panneau répond aussi en JSON sur /api/system/info : IceRiver doit être
    # reconnu **avant** AxeOS.
    _PanelHandler.serve_axeos = True
    found = await asic_client.detect(plain_panel)
    assert found["driver"] == "iceriver"
    assert found["login_required"] is False
    assert found["cert_sha256"] is None
    expected = asic.parse_iceriver(ICERIVER)
    assert found["state"]["hashrate_hs"] == pytest.approx(expected["hashrate_hs"])

    state = await asic_client.poll("iceriver", plain_panel)
    assert state["power_w"] is None or isinstance(state["power_w"], float)
    assert not [row for row in _PanelHandler.records if row["path"] == "/user/login"]


async def test_detect_never_falls_back_to_axeos_on_an_iceriver(plain_panel):
    # Panneau cassé, mais il se présente comme un IceRiver (GET / -> loginpage) :
    # aucun repli AxeOS, même si /api/system/info répondrait.
    _PanelHandler.panel_enabled = False
    _PanelHandler.serve_axeos = True
    with pytest.raises(asic_client.AsicError) as failed:
        await asic_client.detect(plain_panel)
    assert "pilote non reconnu" not in str(failed.value)
    assert not [row for row in _PanelHandler.records if row["path"] == "/api/system/info"]


def test_health_without_credentials_says_so():
    verdict = asic.health({"online": True, "requires_login": True})
    assert verdict["status"] == "orange"
    assert verdict["reason"] == "identifiants requis pour lire le panneau"
