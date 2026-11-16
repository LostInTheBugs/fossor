#!/usr/bin/env python3
"""Faux mineur (étape 3a) — cible jetable, aucun calcul, aucun réseau sortant.

Instantané dans une archive .tar.gz servie par un conteneur HTTP local, il
écrit son PID, expose un hashrate fictif sur ``/`` et s'arrête sur SIGTERM.
Il ne télécharge rien et ne contacte aucun pool : son unique but est de prouver
le cycle installation → SHA256 → exécution → statut → arrêt de Fossor.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTED = time.time()
STATE = {"hashrate_hs": 0.0}
ARGS = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - API de http.server
        payload = {
            "fake": True,
            "worker": ARGS.worker,
            "hashrate": STATE["hashrate_hs"],
            "gpus": [g for g in (ARGS.gpus or "").split(",") if g],
            "uptime_s": int(time.time() - STARTED),
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(description="faux mineur Fossor")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--worker", default="fossor")
    parser.add_argument("--gpus", default="")
    parser.add_argument("--api-port", type=int, default=0)
    ARGS = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    STATE["hashrate_hs"] = 1_234_000.0 + 1000.0 * len([g for g in ARGS.gpus.split(",") if g])

    if ARGS.api_port:
        server = ThreadingHTTPServer(("127.0.0.1", ARGS.api_port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
