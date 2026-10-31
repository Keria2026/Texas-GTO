import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from texas_analysis.app import create_app

ROOT = Path(__file__).resolve().parents[3]


def test_health_matches_the_shared_contract_and_fixture():
    with TestClient(create_app()) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    schema = json.loads(
        (ROOT / "packages/protocol/schemas/health-response.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(response.json())
    expected = json.loads((ROOT / "fixtures/protocol/health-response.json").read_text())
    assert response.json() == expected


def test_only_explicit_browser_origins_are_allowed():
    with TestClient(create_app(("http://127.0.0.1:5173",))) as client:
        allowed = client.get("/v1/health", headers={"origin": "http://127.0.0.1:5173"})
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        assert (
            client.get("/v1/health", headers={"origin": "https://example.com"}).status_code == 403
        )
        assert client.get("/v1/health", headers={"origin": "null"}).status_code == 403


@pytest.mark.parametrize("origin", ["*", "chrome-extension://*", "null"])
def test_wildcard_origins_cannot_be_configured(origin):
    with pytest.raises(ValueError):
        create_app((origin,))


def test_analysis_is_not_exposed_before_implementation():
    with TestClient(create_app()) as client:
        assert client.post("/v1/analyze", json={}).status_code == 404
