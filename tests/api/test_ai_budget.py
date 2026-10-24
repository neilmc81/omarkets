from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.ai_budget import router
from core.services.ai_budget import AICostGuard


def test_budget_contract_does_not_require_enabled_paid_services(tmp_path):
    app = FastAPI()
    app.include_router(router)
    guard = AICostGuard(tmp_path / "budget.sqlite3")
    guard.block("openrouter", "Insufficient credits; explicit recovery required.")
    app.state.ai_budget = guard
    with TestClient(app) as client:
        response = client.get("/api/v1/ai/budget")
    assert response.status_code == 200
    data = response.json()
    assert data["monthly_limit_usd"] == 5
    assert data["providers"][0]["blocked"]
    assert data["providers"][0]["reason"].startswith("Insufficient credits")
    assert "api_key" not in response.text


def test_budget_missing_is_explicit_unavailable():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/v1/ai/budget").status_code == 503
