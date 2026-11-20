"""E2E test fixtures — start a real uvicorn server for Playwright."""

import os
import subprocess
import time
from pathlib import Path

import pytest


E2E_PORT = 18765
E2E_DATA_DIR = "/tmp/fossor_e2e_data"
E2E_URL = f"http://127.0.0.1:{E2E_PORT}"


@pytest.fixture(scope="session")
def e2e_server():
    """Start a real uvicorn server on E2E_PORT for browser tests."""
    sources_dir = Path(__file__).resolve().parent.parent.parent
    env = {
        **os.environ,
        "FOSSOR_DATA_DIR": E2E_DATA_DIR,
        "DATABASE_URL": f"sqlite:///{E2E_DATA_DIR}/fossor.db",
        "FOSSOR_ADMIN_PASSWORD": "adminpass",
        "FOSSOR_MASTER_KEY": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v=",
        # Pas de tick automatique pendant la suite : le test 4b déclenche le sien.
        "FOSSOR_VEILLE_SCHEDULER": "0",
    }
    # Clean slate
    import shutil
    shutil.rmtree(E2E_DATA_DIR, ignore_errors=True)
    os.makedirs(E2E_DATA_DIR, exist_ok=True)

    log_path = Path(E2E_DATA_DIR) / "server.log"
    log_file = open(log_path, "wb", buffering=0)
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(E2E_PORT)],
        cwd=str(sources_dir),
        env=env,
        # Fichier, jamais un PIPE : un PIPE que personne ne vide se remplit
        # (~64 Kio d'access log) et l'écriture synchrone du log fige la boucle
        # asyncio du serveur au milieu de la suite.
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    # Wait for server to be ready
    for _ in range(40):
        time.sleep(0.3)
        if proc.poll() is not None:
            out = log_path.read_text(errors="replace") if log_path.exists() else ""
            raise RuntimeError(f"Server failed to start:\n{out}")
        try:
            import urllib.request
            urllib.request.urlopen(f"{E2E_URL}/health", timeout=1)
            break
        except Exception:
            pass
    else:
        proc.kill()
        raise RuntimeError("Server did not become ready in time")

    yield E2E_URL

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log_file.close()
