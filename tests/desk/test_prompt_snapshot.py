"""Phase 1 gate: the exact NQ prompt (system text + rendered task and facts) is unchanged.

Regenerate deliberately with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review the diff.
"""

import os
from datetime import timedelta
from pathlib import Path

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


@pytest.mark.parametrize("kind", [NoteKind.PREMARKET, NoteKind.RELEASE])
async def test_nq_prompt_snapshot(kind):
    service, clock, _ = make(RELEASE + timedelta(minutes=35), rows=cpi_rows())
    await service.tick()
    facts = next(f for k, f in service.writer.calls if k is kind)
    check(f"nq_{kind.value}.txt", f"SYSTEM\n{SYSTEM}\n\nUSER\n{user_message(kind, facts)}\n")
