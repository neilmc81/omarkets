"""Explainer library API: the fixed plain-English hover texts.

* ``GET /api/v1/explainers`` — every entry, keyed ``<kind>:<name>``.

Read-only and static: the entries are written once in
``core/desk/explainers.py``; nothing is generated.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from core.desk import explainers as X

router = APIRouter(prefix="/api/v1", tags=["explainers"])


class ExplainerLibrary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[X.Explainer]


_LIBRARY = ExplainerLibrary(entries=list(X.ENTRIES))


@router.get("/explainers", response_model=ExplainerLibrary)
async def explainers() -> ExplainerLibrary:
    """Every explainer entry, instruments first."""
    return _LIBRARY
