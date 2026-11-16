"""Real network probe against Hermes (§4.12) — the only real call of step 1b-4.

Credentials come from the environment and never from the repository:

    FOSSOR_TEST_HERMES_KEY=<api key of the Hermes API server>
    FOSSOR_TEST_HERMES_BASE_URL=http://<host>:8642/v1   (optional, default below)

Run it alone:

    docker run --rm --user $(id -u):$(id -g) -v $PWD:/src:ro -w /src \
      -e FOSSOR_TEST_HERMES_KEY=... -e FOSSOR_TEST_HERMES_BASE_URL=... \
      fossor-app:test python -m pytest tests/test_ai_network.py -m network -v -p no:cacheprovider
"""

import json
import os

import pytest

DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"

pytestmark = pytest.mark.network


async def test_hermes_probe_real_network():
    api_key = os.environ.get("FOSSOR_TEST_HERMES_KEY")
    base_url = os.environ.get("FOSSOR_TEST_HERMES_BASE_URL") or DEFAULT_BASE_URL
    if not api_key:
        pytest.skip("FOSSOR_TEST_HERMES_KEY absent : test réseau réel non lancé")

    from app.ai_client import probe_provider

    provider = {
        "base_url": base_url,
        "model": "hermes-agent",
        "timeout_s": 180,
        "reasoning_effort": "",
        "extra_body": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "require_model_lock": True,
        },
    }
    result = await probe_provider(provider, api_key)
    print("probe:", json.dumps(result, ensure_ascii=False))

    assert result["ok"] is True, result
    assert result["model"], result
    assert api_key not in json.dumps(result)
    assert 0 <= result["latency_ms"] < 180_000
