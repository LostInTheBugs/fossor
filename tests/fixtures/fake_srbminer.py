#!/usr/bin/env python3
"""Faux SRBMiner-MULTI — preuve d'intégration 2026.11.003-c4.

Servi sous le nom ``srbminer_compute`` (binaire renommé, comme sur
l'athlon-3000g), écoute **uniquement** sur 127.0.0.1: le conteneur Fossor ne
peut donc pas l'atteindre en direct, seul le relais SSH le lit.

Aucun minage : le processus ne fait que répondre une charge JSON plausible et
dormir.  Il ne connaît ni wallet ni pool réels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HASHRATE = 1.25e9


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
        body = json.dumps({
            "mining": {
                "miner": "SRBMiner-Multi",
                "algorithm": "pearlhash",
                "coin": "PEARL",
                "uptime": 3800,
                "shares": {"accepted": 12, "rejected": 0},
                "hashrate": {"total": HASHRATE},
            }
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence
        return


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--api-port", type=int, default=21550)
    parser.add_argument("--api-enable", action="store_true")
    args, _unknown = parser.parse_known_args()
    try:
        HTTPServer(("127.0.0.1", args.api_port), Handler).serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:  # pragma: no cover - port occupé
        print(f"fake srbminer: {exc}", file=sys.stderr)
        return 1
    time.sleep(0)  # jamais atteint
    return 0


if __name__ == "__main__":
    sys.exit(main())
