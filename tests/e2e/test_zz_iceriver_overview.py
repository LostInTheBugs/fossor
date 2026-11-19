"""E2E : IceRiver pbv081 (HTTPS à session) ajouté par le panneau Fossor.

Faux firmware **local** en HTTPS : ``GET /`` pose le cookie ``esp-app`` puis
redirige vers ``/user/loginpage``, ``/user/userpanel`` répond toujours
``302 /notfound``, ``POST /user/login`` rend un JSON et ``GET /overview`` sert
la fixture réelle du KS0 Ultra.  Le modèle ne mesure pas sa puissance : la
fiche porte 230 W **saisis**.
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
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OVERVIEW = FIXTURES / "iceriver-ks0ultra-pbv081-overview.json"
USER = "fossor-test"
PASSWORD = "motdepasse-factice-0000"
LOGIN_HTML = (
    "<html><head><title>\u7528\u6237\u754c\u9762</title></head><body>login</body></html>"
)


def _write_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fossor-e2e")])
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
    return cert_path, key_path


def _token(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "esp-app":
            return value
    return ""


class _Panel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    records: list[dict] = []
    sessions: set[str] = set()

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

    def do_GET(self) -> None:  # noqa: N802
        type(self).records.append({"method": "GET", "path": self.path})
        if self.path == "/":
            token = secrets.token_hex(8)
            type(self).sessions.add(token)
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
            if token and token in type(self).sessions:
                self._send(200, OVERVIEW.read_bytes())
                return
            self._send(302, extra=(("Location", "/user/loginpage"),))
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        form = parse_qs(body.decode("utf-8", "replace"))
        type(self).records.append({"method": "POST", "path": self.path})
        if self.path == "/user/login":
            ok = (
                (form.get("username") or [""])[0] == USER
                and (form.get("password") or [""])[0] == PASSWORD
            )
            if ok:
                token = secrets.token_hex(8)
                type(self).sessions.add(token)
                self._send(
                    200,
                    json.dumps({"error": 0, "message": "ok"}).encode(),
                    extra=(("Set-Cookie", f"esp-app={token}; httponly; Path=/"),),
                )
            else:
                self._send(
                    200,
                    json.dumps({"error": 1, "message": "invalid user or password"}).encode(),
                )
            return
        if self.path == "/user/userpanel":
            self._send(302, extra=(("Location", "/notfound"),))
            return
        self._send(404, b"{}")


class _Redirect(BaseHTTPRequestHandler):
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


@pytest.fixture()
def fake_panel(tmp_path):
    _Panel.records = []
    _Panel.sessions = set()
    cert_path, key_path = _write_cert(tmp_path)
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
        yield f"127.0.0.1:{redirect.server_address[1]}"
    finally:
        for running, worker in ((redirect, http), (server, https)):
            running.shutdown()
            running.server_close()
            worker.join(timeout=5)


async def _login(page) -> None:
    await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
    await page.fill("#username", "admin")
    await page.fill("#password", ADMIN_PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url("**/dashboard", timeout=15000)


@pytest.mark.e2e
async def test_iceriver_pbv081_added_with_credentials(e2e_server, fake_panel):
    console_errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page = await context.new_page()
        await _login(page)

        await page.goto(E2E_URL + "/machines", wait_until="domcontentloaded")
        await page.fill("#aName", "ks0-ultra-pbv081")
        await page.fill("#aHost", fake_panel)
        await page.select_option("#aDriver", "auto")
        await page.fill("#aUser", USER)
        await page.fill("#aPass", PASSWORD)
        await page.fill("#aPower", "230")
        async with page.expect_response(
            lambda r: r.url.endswith("/api/asics") and r.request.method == "POST"
        ) as caught:
            await page.click("#asicForm button[type=submit]")
        response = await caught.value
        print("ASIC_POST", response.status, (await response.text())[:400])
        assert response.status == 201, await response.text()

        row = page.locator("[data-asic]", has_text="ks0-ultra-pbv081")
        await row.wait_for(timeout=20000)
        text = await row.inner_text()
        assert "KS0ULTRA" in text
        assert "pbv081_ks0ultra_miner" in text
        assert "481" in text and "GH/s" in text
        assert "230 W (saisie)" in text

        asics = await page.evaluate(
            "async () => (await (await fetch('/api/asics')).json()).asics"
        )
        entry = next(item for item in asics if item["name"] == "ks0-ultra-pbv081")
        state = entry["asic"]
        assert entry["driver"] == "iceriver"
        assert entry["power_source"] == "saisie"
        assert entry["power_w"] == pytest.approx(230.0)
        assert state["model"] == "KS0ULTRA"
        assert state["firmware"] == "pbv081_ks0ultra_miner"
        assert state["hashrate_hs"] == pytest.approx(481e9)
        assert state["power_w"] is None
        assert state["power_measured"] is False
        assert entry["health"]["status"] == "green"
        dumped = json.dumps(entry, ensure_ascii=False)
        assert PASSWORD not in dumped
        assert "REDACTED-WALLET" not in dumped

        await browser.close()

    assert console_errors == [], console_errors
    routes = [(item["method"], item["path"]) for item in _Panel.records]
    assert ("POST", "/user/login") in routes
    assert ("GET", "/overview") in routes
    assert not [item for item in routes if item[1] == "/api/system/info"]
