"""Phase 1 gate: the exact NQ prompt (system text + rendered task and facts) is unchanged.

Regenerate deliberately with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review the diff.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.desk.models import NoteKind
from core.desk.prompts import SYSTEM, user_message
from tests.desk.test_service import RELEASE, cpi_rows, make

SNAPSHOTS = Path(__file__).parents[1] / "fixtures" / "prompt_snapshots"


def check(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), f"prompt changed: {name}"


async def facts_for(kind: NoteKind) -> dict:
    """The facts the service hands the writer for one NQ note of ``kind``."""
    if kind in (NoteKind.PREMARKET, NoteKind.RELEASE):
        service, _, _ = make(RELEASE + timedelta(minutes=35), rows=cpi_rows())
        await service.tick()
    elif kind is NoteKind.MOVE:
        now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        nq = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                              value=30000.0 * (1 - 0.0002 * i)) for i in range(80)]
        service, _, _ = make(now, series={"NQ": nq}, rows=cpi_rows())
        await service.tick()
    else:
        service, _, _ = make(datetime(2026, 10, 20, 15, 0, tzinfo=UTC), rows=cpi_rows())
        await service.ask()
    return next(f for k, f in service.writer.calls if k is kind)


@pytest.mark.parametrize("kind", [NoteKind.PREMARKET, NoteKind.RELEASE,
                                  NoteKind.MOVE, NoteKind.ASK])
async def test_nq_prompt_snapshot(kind):
    facts = await facts_for(kind)
    check(f"nq_{kind.value}.txt", f"SYSTEM\n{SYSTEM}\n\nUSER\n{user_message(kind, facts)}\n")
