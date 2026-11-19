"""Pilote IceRiver « pbv081 » — panneau HTTPS avec session et ``GET /overview``.

Le faux firmware est **local** (certificat auto-signé généré dans le test) :
``GET /`` pose le cookie ``esp-app`` puis redirige vers ``/user/loginpage``,
``/user/userpanel`` répond **toujours** ``302 /notfound`` (l'adresse a disparu
de ce firmware), ``POST /user/login`` rend un JSON et ``/overview`` sert la
fixture réelle du KS0 Ultra (IP et wallet masqués) une fois la session posée.

Aucun ASIC réel n'est contacté ; aucun mot de passe réel n'apparaît.
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
from app.routers.asic import _probe_for_add

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OVERVIEW = json.loads((FIXTURES / "iceriver-ks0ultra-pbv081-overview.json").read_text())
POWERDRAW = json.loads(
    (FIXTURES / "iceriver-ks0ultra-pbv081-timeseries-powerdraw.json").read_text()
)
OLD_PANEL = json.loads((FIXTURES / "iceriver-userpanel-post4.json").read_text())
WALLET = OVERVIEW["data"]["pools"][0]["user"]

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


def _token(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "esp-app":
            return value
    return ""


class _Panel(BaseHTTPRequestHandler):
    """Faux firmware pbv081 : cookie sur ``GET /``, login JSON, ``/overview``."""

    protocol_version = "HTTP/1.1"
    records: list[dict] = []
    sessions: set[str] = set()
    tokens: list[str] = []
    set_cookie_on_root = True
    drop_overview = 0
    # pbv081 réel : le bon mot de passe répond 302 -> / (session posée), pas du JSON.
    success_via_redirect = False

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

    def _record(self, method: str, form: dict[str, list[str]] | None = None) -> None:
        entry: dict = {"method": method, "path": self.path}
        if form is not None:
            entry["fields"] = sorted(form)
            if "password" in form:
                # Jamais le mot de passe dans le journal de preuve.
                entry["password_len"] = len(form["password"][0])
                entry["password_accepted"] = form["password"][0] == PASSWORD
        type(self).records.append(entry)

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET")
        if self.path == "/":
            if not type(self).set_cookie_on_root:
                self._send(302, extra=(("Location", "/user/loginpage"),))
                return
            token = secrets.token_hex(8)
            type(self).sessions.add(token)
            type(self).tokens.append(token)
            self._send(
                302,
                extra=(
                    ("Location", "/user/loginpage"),
                    ("Set-Cookie", f"esp-app={token}; httponly; Path=/"),
                ),
            )
            return
        if self.path == "/user/loginpage":
            self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/overview":
            token = _token(self.headers.get("Cookie") or "")
            if type(self).drop_overview > 0 and token in type(self).sessions:
                type(self).drop_overview -= 1
                type(self).sessions.discard(token)
                self._send(302, extra=(("Location", "/notfound"),))
                return
            if token and token in type(self).sessions:
                self._send(200, (FIXTURES / "iceriver-ks0ultra-pbv081-overview.json").read_bytes())
                return
            self._send(302, extra=(("Location", "/user/loginpage"),))
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
                type(self).tokens.append(token)
                if type(self).success_via_redirect:
                    # Réponse réelle du pbv081 : 302 vers l'accueil + Set-Cookie.
                    self._send(
                        302,
                        extra=(
                            ("Location", "/"),
                            ("Set-Cookie", f"esp-app={token}; httponly; Path=/"),
                        ),
                    )
                    return
                self._send(
                    200,
                    json.dumps({"error": 0, "message": "ok"}).encode(),
                    extra=(("Set-Cookie", f"esp-app={token}; httponly; Path=/"),),
                )
            elif type(self).success_via_redirect:
                self._send(302, extra=(("Location", "/user/loginpage"),))
            else:
                self._send(
                    200,
                    json.dumps({"error": 1, "message": "invalid user or password"}).encode(),
                )
            return
        if self.path == "/user/userpanel":
            # L'adresse a disparu de ce firmware : toujours /notfound.
            self._send(302, extra=(("Location", "/notfound"),))
            return
        self._send(404, b"{}")


class _Redirect(BaseHTTPRequestHandler):
    """Port 80 du firmware : tout part en 301 vers HTTPS."""

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
    _Panel.records = []
    _Panel.sessions = set()
    _Panel.tokens = []
    _Panel.set_cookie_on_root = True
    _Panel.drop_overview = 0
    _Panel.success_via_redirect = False


@pytest.fixture()
def panel(tmp_path):
    _reset()
    cert_path, key_path, fingerprint = _write_cert(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Panel)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    https = threading.Thread(target=server.serve_forever, daemon=True)
    https.start()
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    _Redirect.target = f"https://127.0.0.1:{server.server_address[1]}"
    http = threading.Thread(target=redirect.serve_forever, daemon=True)
    http.start()
    try:
        yield {"host": f"127.0.0.1:{redirect.server_address[1]}", "fingerprint": fingerprint}
    finally:
        for running, worker in ((redirect, http), (server, https)):
            running.shutdown()
            running.server_close()
            worker.join(timeout=5)


def test_overview_fixture_is_parsed():
    state = asic.parse_iceriver_overview(OVERVIEW)
    assert state["model"] == "KS0ULTRA"
    assert state["firmware"] == "pbv081_ks0ultra_miner"
    assert state["hashrate_hs"] == pytest.approx(481e9)
    assert state["hashrate_1h_hs"] == pytest.approx(488e9)
    assert state["fans_active"] == 2
    assert state["fan_rpm"] == 6079
    assert state["temp_c"] == pytest.approx(57.0)
    assert state["intemp_c"] == pytest.approx(35.0)
    assert state["outtemp_c"] == pytest.approx(51.0)
    assert state["shares_accepted"] == 37
    assert state["shares_rejected"] == 0
    assert state["reject_pct"] == pytest.approx(0.047687)
    assert state["uptime_s"] == 57733
    assert state["coin"] == "KAS" and state["algorithm"] == "kHeavyHash"
    assert state["pool_connected"] is True
    assert state["state_faults"] == []
    assert asic.health(state)["status"] == "green"
    # Le wallet n'est rendu que tronqué : jamais l'adresse complète.
    assert state["pool_user_masked"] == f"{WALLET[:6]}\u2026{WALLET[-4:]}"
    dumped = json.dumps(state, ensure_ascii=False)
    assert WALLET not in dumped
    assert "REDACTED-WALLET" not in dumped


def test_overview_and_old_panel_are_told_apart():
    assert asic.iceriver_overview_like(OVERVIEW) is True
    assert asic.iceriver_overview_like(OLD_PANEL) is False
    assert asic.state_from_payload(OVERVIEW)["source"] == "overview"
    assert "source" not in asic.parse_iceriver(OLD_PANEL)


def test_units_are_converted_to_hs():
    assert asic.parse_rate("481G", "G") == pytest.approx(481e9)
    assert asic.parse_rate("1.2T", "G") == pytest.approx(1.2e12)
    assert asic.parse_rate("750M", "G") == pytest.approx(750e6)
    assert asic.parse_rate("900K", "G") == pytest.approx(900e3)
    assert asic.parse_rate("1500", "M") == pytest.approx(1.5e9)
    unit = dict(OVERVIEW)
    unit["data"] = dict(OVERVIEW["data"], unit="T", rtpow="0.481", avgpow="0.488")
    assert asic.parse_iceriver_overview(unit)["hashrate_hs"] == pytest.approx(481e9)


def test_power_is_never_zero_watts():
    state = asic.parse_iceriver_overview(OVERVIEW)
    assert state["power_w"] is None
    assert state["power_measured"] is False
    assert asic.power_label(state, None) == "non mesurée"
    assert asic.power_label(state, 230.0) == "saisie"
    assert asic.power_label({"power_w": 410.0}, None) == "mesurée par l'ASIC"

    # La courbe réelle du KS0 Ultra ne contient que des zéros -> non mesurée.
    empty = asic.parse_iceriver_overview(POWERDRAW)
    assert empty["power_w"] is None
    zeros = dict(OVERVIEW)
    zeros["data"] = dict(OVERVIEW["data"], powerdraw=POWERDRAW["series"])
    assert asic.parse_iceriver_overview(zeros)["power_w"] is None
    measured = dict(OVERVIEW)
    measured["data"] = dict(OVERVIEW["data"], powerdraw=[0, 0, 0, 410])
    assert asic.parse_iceriver_overview(measured)["power_w"] == pytest.approx(410.0)


async def test_login_sequence_reads_overview(panel):
    found = await asic_client.iceriver_probe(panel["host"], USER, PASSWORD)
    assert found["mode"] == "overview"
    assert found["steps"][0] == "étape GET / : 302 → /user/loginpage, cookie reçu oui"
    assert found["steps"][1] == "login : error=0"
    state = asic_client.iceriver_state(found)
    assert state["hashrate_hs"] == pytest.approx(481e9)
    assert asic.health(state)["status"] == "green"

    routes = [(row["method"], row["path"]) for row in _Panel.records]
    assert routes[0] == ("GET", "/")
    assert routes[1] == ("POST", "/user/login")
    assert routes[2] == ("GET", "/overview")
    assert not [row for row in routes if row[1] == "/user/userpanel"]


async def test_missing_cookie_names_the_failing_step(panel):
    _Panel.set_cookie_on_root = False
    with pytest.raises(asic_client.AsicError) as failed:
        await asic_client.iceriver_probe(panel["host"], USER, PASSWORD)
    text = str(failed.value)
    assert "étape GET / : 302 → /user/loginpage, cookie reçu non" in text
    assert "/overview : 302 → /user/loginpage" in text


async def test_wrong_password_reports_the_login_error(panel):
    with pytest.raises(asic_client.AsicAuthError) as refused:
        await asic_client.iceriver_probe(panel["host"], USER, "mauvais-mot-de-passe")
    text = str(refused.value)
    assert text == "identifiants refusés : login : error=1, message=invalid user or password"
    assert PASSWORD not in text
    assert "esp-app=" not in text
    assert all(token not in text for token in _Panel.tokens)


async def test_lost_session_does_one_new_login(panel):
    _Panel.drop_overview = 1
    found = await asic_client.iceriver_probe(panel["host"], USER, PASSWORD)
    assert found["mode"] == "overview"
    logins = [row for row in _Panel.records if row["path"] == "/user/login"]
    assert len(logins) == 2, _Panel.records


async def test_session_lost_twice_gives_a_clear_error(panel):
    _Panel.drop_overview = 2
    with pytest.raises(asic_client.AsicAuthError) as refused:
        # Deux pertes de suite : le second passage ne réessaie pas indéfiniment.
        await asic_client.iceriver_probe(panel["host"], USER, PASSWORD)
    assert "session refusée" in str(refused.value)
    assert "/overview : 302 → /notfound" in str(refused.value)


async def test_detect_identifies_iceriver_and_never_tries_axeos(panel):
    found = await asic_client.detect(
        panel["host"], username=USER, password=PASSWORD
    )
    assert found["driver"] == "iceriver"
    assert found["state"]["source"] == "overview"
    assert found["state"]["model"] == "KS0ULTRA"
    assert found["cert_sha256"] == panel["fingerprint"]
    assert not [row for row in _Panel.records if row["path"] == "/api/system/info"]


async def test_manual_axeos_on_an_iceriver_is_refused(panel):
    with pytest.raises(asic_client.AsicError) as failed:
        await _probe_for_add("axeos", panel["host"], "", "")
    assert "ce n'est pas un AxeOS (pas de /api/system/info)" in str(failed.value)


def test_health_reports_the_firmware_states():
    broken = json.loads(json.dumps(OVERVIEW))
    broken["data"]["netstate"] = False
    state = asic.parse_iceriver_overview(broken)
    assert state["state_faults"] == ["réseau"]
    verdict = asic.health(state)
    assert verdict["status"] == "orange"
    assert "réseau" in verdict["reason"]


async def test_login_success_via_redirect_is_accepted(panel):
    """pbv081 : bons identifiants -> 302 Location: / + Set-Cookie, puis /overview."""
    _Panel.success_via_redirect = True
    found = await asic_client.iceriver_probe(panel["host"], USER, PASSWORD)
    assert found["mode"] == "overview"
    assert found["steps"][1] == "login : 302 → /"
    state = asic_client.iceriver_state(found)
    assert state["hashrate_hs"] == pytest.approx(481e9)
    assert asic.health(state)["status"] == "green"

    logins = [row for row in _Panel.records if row["path"] == "/user/login"]
    assert len(logins) == 1
    assert logins[0]["password_accepted"] is True
    assert logins[0]["fields"] == ["password", "username"]
    assert [row["path"] for row in _Panel.records] == ["/", "/user/login", "/overview"]
    assert PASSWORD not in " ".join(found["steps"])
    assert all(token not in " ".join(found["steps"]) for token in _Panel.tokens)


async def test_login_redirect_to_loginpage_is_a_refusal(panel):
    """302 vers /user/loginpage : refus, jamais un succès."""
    _Panel.success_via_redirect = True
    with pytest.raises(asic_client.AsicAuthError) as refused:
        await asic_client.iceriver_probe(panel["host"], USER, "mauvais-mot-de-passe")
    assert str(refused.value) == "identifiants refusés : login : 302 → /user/loginpage"
    assert PASSWORD not in str(refused.value)
    assert all(token not in str(refused.value) for token in _Panel.tokens)
