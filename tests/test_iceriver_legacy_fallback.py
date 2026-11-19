"""Detection IceRiver : repli ``POST /user/userpanel`` ``post=4`` (ancien firmware).

Deux serveurs locaux, aucune adresse d'appareil :
- ``old`` : ICM168, tout ``GET`` en 405, seule reponse ``POST /user/userpanel``
  (``post=4``) — fixture ``iceriver-userpanel-post4.json`` ;
- ``pbv081`` : ``GET /`` -> ``302 /user/loginpage`` + cookie, ``/overview``
  servi avec la fixture reelle, ``POST /user/userpanel`` -> ``302 /notfound``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import asic_client

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OLD_PANEL = json.loads((FIXTURES / "iceriver-userpanel-post4.json").read_text())
PBV081 = json.loads((FIXTURES / "iceriver-ks0ultra-pbv081-overview.json").read_text())


class _Panel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "old"
    seen: list = []

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append(("GET", self.path, ""))
        if self.mode == "pbv081" and self.path == "/":
            self._send(
                302,
                b"",
                {"Location": "/user/loginpage", "Set-Cookie": "esp-app=1; Path=/"},
            )
            return
        if self.mode == "pbv081" and self.path == "/overview":
            self._send(200, json.dumps(PBV081).encode(), {"Content-Type": "application/json"})
            return
        self._send(405)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        type(self).seen.append(("POST", self.path, body))
        if self.mode == "pbv081":
            if self.path == "/user/login":
                self._send(200, b'{"error": 0}', {"Content-Type": "application/json"})
                return
            self._send(302, b"", {"Location": "/notfound"})
            return
        if self.path == "/user/userpanel":
            payload = json.dumps(OLD_PANEL).encode()
            self._send(200, payload, {"Content-Type": "application/json"})
            return
        self._send(404)


@contextmanager
def _panel(mode: str):
    handler = type("_Panel_" + mode, (_Panel,), {"mode": mode, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1:%d" % server.server_address[1], handler.seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _axeos(seen):
    return [row for row in seen if row[1] == "/api/system/info"]


def test_ancien_firmware_tout_get_405_reconnu_sans_identifiants():
    with _panel("old") as (host, seen):
        result = asyncio.run(asic_client.detect(host))
    assert result["driver"] == "iceriver"
    assert result["state"]["model"] == "KS0 Ultra"
    assert result["state"]["hashrate_hs"] == pytest.approx(271e9)
    assert ("POST", "/user/userpanel", "post=4") in seen
    assert _axeos(seen) == []


def test_ancien_firmware_tout_get_405_reconnu_avec_identifiants():
    with _panel("old") as (host, seen):
        result = asyncio.run(asic_client.detect(host, username="admin", password="mdp"))
    assert result["driver"] == "iceriver"
    assert result["state"]["model"] == "KS0 Ultra"
    assert ("POST", "/user/userpanel", "post=4") in seen
    assert _axeos(seen) == []


def test_pbv081_jamais_de_bascule_axeos():
    with _panel("pbv081") as (host, seen):
        result = asyncio.run(asic_client.detect(host))
    assert result["driver"] == "iceriver"
    assert result["login_required"] is True
    assert result["state"]["requires_login"] is True
    assert _axeos(seen) == []


def test_pbv081_overview_servi_apres_login():
    with _panel("pbv081") as (host, seen):
        found = asyncio.run(asic_client.iceriver_probe(host, "admin", "mdp"))
    assert found["mode"] == "overview"
    assert asic_client.iceriver_state(found)["driver"] == "iceriver"
    assert [row for row in seen if row[1] == "/user/login"]
    assert not [row for row in seen if row[1] == "/user/userpanel"]


def test_ancien_firmware_panneau_post4_lu_sans_overview():
    with _panel("old") as (host, seen):
        found = asyncio.run(asic_client.iceriver_probe(host, "admin", "mdp"))
    assert found["mode"] == "panel"
    assert found["payload"] is not None
    assert asic_client.iceriver_state(found)["hashrate_hs"] == pytest.approx(271e9)
