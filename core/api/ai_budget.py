"""Read-only cost control status; no provider calls or implicit recovery."""

from fastapi import APIRouter, HTTPException, Request

from core.services.ai_budget import AIBudgetStatus

router = APIRouter(prefix="/api/v1", tags=["ai-cost-control"])


@router.get("/ai/budget", response_model=AIBudgetStatus)
def budget_status(request: Request) -> AIBudgetStatus:
    guard = getattr(request.app.state, "ai_budget", None)
    if guard is None:
        raise HTTPException(status_code=503, detail="AI cost controls unavailable")
    return guard.status()
