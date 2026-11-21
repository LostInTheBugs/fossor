"""Durcissement 020 — exposition réseau, cookie de session, dépôt (§5)."""

from __future__ import annotations

from pathlib import Path

from app import auth

ROOT = Path(__file__).resolve().parent.parent


def test_cookie_secure_desactive_par_defaut(monkeypatch):
    monkeypatch.delenv("FOSSOR_COOKIE_SECURE", raising=False)
    assert auth.cookie_secure() is False
    assert auth.session_cookie_options()["secure"] is False


def test_cookie_secure_active_derriere_un_proxy_https(monkeypatch):
    for value in ("1", "true", "TRUE", "on", "yes"):
        monkeypatch.setenv("FOSSOR_COOKIE_SECURE", value)
        assert auth.cookie_secure() is True
        options = auth.session_cookie_options()
        assert options["secure"] is True
        assert options["httponly"] is True
        assert options["samesite"] == "strict"
    monkeypatch.setenv("FOSSOR_COOKIE_SECURE", "false")
    assert auth.session_cookie_options()["secure"] is False


def test_compose_bind_et_cookie_secure():
    text = (ROOT / "docker-compose.yml").read_text()
    assert '"${FOSSOR_BIND:-0.0.0.0}:${FOSSOR_PORT:-8008}:8008"' in text
    assert "FOSSOR_COOKIE_SECURE=${FOSSOR_COOKIE_SECURE:-false}" in text


def test_socket_proxy_option_desactivee_par_defaut():
    base = (ROOT / "docker-compose.yml").read_text()
    assert "docker-socket-proxy" not in base
    option = (ROOT / "docker-compose.socket-proxy.yml").read_text()
    assert "docker-socket-proxy:0.3.0" in option
    assert "DOCKER_HOST=tcp://docker-socket-proxy:2375" in option
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in option
    assert "volumes: !override" in option
    for closed in ('EXEC: "0"', 'BUILD: "0"', 'SWARM: "0"', 'SYSTEM: "0"'):
        assert closed in option
    # Le compagnon ne monte plus le socket brut quand l'option est utilisée.
    updater = option.split("fossor-updater:", 1)[1]
    assert "/var/run/docker.sock:/var/run/docker.sock" not in updater


def test_licence_mit_et_version_de_main():
    licence = (ROOT / "LICENSE").read_text()
    assert licence.startswith("MIT License")
    assert "Copyright (c) 2026 LostInTheBugs" in licence
    assert (ROOT / "VERSION").read_text().strip() == "2026.11.020-dev"


def test_readme_arborescence_et_securite():
    text = (ROOT / "README.md").read_text()
    tree = text.split("## État du dépôt", 1)[1]
    assert "sources/" not in tree
    assert "updater/" in tree and "static/" in tree
    assert "FOSSOR_BIND" in text
    assert "FOSSOR_COOKIE_SECURE" in text
    assert "sans HTTPS" in text
