"""Verdicts justifiés — raison obligatoire, grille, cohérence, import rétrocompatible.

Cas de référence : SHEAR (19/11 « mainnet 21 déc, pas encore lancé » puis 20/11
« mainnet actif 16/11 », trois arguments « pour » d'exploitation, aucun
« contre » et aucune raison). Le verdict « surveillance » doit être signalé.
"""

from __future__ import annotations

from app import analyses as A
from app.ai_config import new_token
from app.database import SessionLocal


def _candidate(**extra):
    candidate = {"ticker": "SHEAR", "name": "SHEAR", "algo": "x"}
    candidate.update(extra)
    return candidate


def _payload(*candidates):
    return {
        "ts": "2026-11-21T10:00:00Z",
        "run": "veille",
        "action": "rien",
        "detail": "Test",
        "candidates": list(candidates),
    }


def _analysis(ts: str, detail: str, **candidate):
    return {
        "ts": ts,
        "run": "veille",
        "action": "rien",
        "detail": detail,
        "source": "hermes",
        "candidates": [_candidate(**candidate)],
    }


def _token() -> str:
    with SessionLocal() as db:
        return new_token(db)["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_reason_is_mandatory_for_hermes(unauth_client):
    token = _token()
    response = await unauth_client.post(
        "/api/hermes/analyses",
        json=_payload(_candidate(verdict="surveillance")),
        headers=_headers(token),
    )
    assert response.status_code == 422
    assert "reason obligatoire" in response.json()["detail"]


async def test_criteria_mandatory_for_retenu_and_rejete(unauth_client):
    token = _token()
    for verdict in ("retenu", "rejete"):
        response = await unauth_client.post(
            "/api/hermes/analyses",
            json=_payload(_candidate(verdict=verdict, reason="Verdict expliqué.")),
            headers=_headers(token),
        )
        assert response.status_code == 422, response.text
        assert "criteria obligatoire" in response.json()["detail"]

    accepted = await unauth_client.post(
        "/api/hermes/analyses",
        json=_payload(
            _candidate(
                verdict="retenu",
                reason="Réseau actif et device libre : retenu.",
                criteria=[{"key": "fenetre", "verdict": "ok", "note": "sorti cette nuit"}],
            )
        ),
        headers=_headers(token),
    )
    assert accepted.status_code == 200, accepted.text
    stored = accepted.json()["analysis"]["candidates"][0]
    assert stored["reason"] == "Réseau actif et device libre : retenu."
    assert stored["criteria"][0]["key"] == "fenetre"


def test_shear_case_is_flagged():
    rows = A.token_rows(
        [
            _analysis(
                "2026-11-19T17:43:00Z",
                "mainnet 21 déc, pas encore lancé",
                verdict="surveillance",
                pour=["ANN active"],
                contre=[],
            ),
            _analysis(
                "2026-11-20T17:43:00Z",
                "mainnet actif 16/11",
                verdict="surveillance",
                pour=["cron jobs eca6896264bc/aa8e2aa870c6 vérifiés armés"],
                contre=[],
            ),
        ]
    )
    row = rows[0]
    assert row["verdict"] == "surveillance"
    assert any("sans aucun argument « contre » ni raison" in warning for warning in row["warnings"])
    assert any("contredit l'évaluation précédente" in warning for warning in row["warnings"])


def test_retenu_with_ko_criterion_is_flagged():
    rows = A.token_rows(
        [
            _analysis(
                "2026-11-21T10:00:00Z",
                "grille",
                verdict="retenu",
                reason="Réseau actif mais VRAM limite.",
                contre=["VRAM insuffisante"],
                criteria=[{"key": "gpu", "verdict": "ko", "note": "VRAM insuffisante"}],
            )
        ]
    )
    assert any("« retenu »" in warning and "ko" in warning for warning in rows[0]["warnings"])


def test_import_without_reason_is_backward_compatible():
    entries = A.parse_document(
        {
            "analyses": [
                {
                    "ts": "2026-11-19T09:00:00Z",
                    "run": "ancien tick",
                    "candidates": [{"name": "SHEAR", "verdict": "surveillance"}],
                }
            ]
        }
    )
    assert entries[0]["candidates"][0]["reason"] == ""
    rows = A.token_rows([{**entries[0], "source": "import"}])
    assert rows[0]["history"][0]["reason"] == "raison non renseignée (import)"
