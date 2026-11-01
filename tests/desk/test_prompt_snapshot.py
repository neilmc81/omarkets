"""Phase 1 gate: the exact NQ desk:v8 prompt (system text + rendered task and
facts) is unchanged. desk:v9's snapshots are in ``test_prompt_v9.py``.

Regenerate deliberately with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review the diff.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.desk.identity import NQ
from core.desk.models import NoteKind
from core.desk.prompts import V8, render
from tests.desk.test_service import RELEASE, FakeWriter, cpi_rows, make

SNAPSHOTS = Path(__file__).parents[1] / "fixtures" / "prompt_snapshots"


def check(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), f"prompt changed: {name}"


class VersionedWriter(FakeWriter):
    def __init__(self, version: str):
        super().__init__()
        self.prompt_version = version


async def facts_for(kind: NoteKind, version: str = V8) -> dict:
    """The facts the service hands a ``version`` writer for one NQ note of ``kind``."""
    writer = VersionedWriter(version)
    if kind in (NoteKind.PREMARKET, NoteKind.RELEASE):
        service, _, _ = make(RELEASE + timedelta(minutes=35), rows=cpi_rows(), writer=writer)
        await service.tick()
    elif kind is NoteKind.MOVE:
        now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        nq = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                              value=30000.0 * (1 - 0.0002 * i)) for i in range(80)]
        service, _, _ = make(now, series={"NQ": nq}, rows=cpi_rows(), writer=writer)
        await service.tick()
    else:
        service, _, _ = make(datetime(2026, 10, 20, 15, 0, tzinfo=UTC), rows=cpi_rows(),
                             writer=writer)
        await service.ask()
    return next(f for k, f in service.writer.calls if k is kind)


@pytest.mark.parametrize("kind", [NoteKind.PREMARKET, NoteKind.RELEASE,
                                  NoteKind.MOVE, NoteKind.ASK])
async def test_nq_prompt_snapshot(kind):
    facts = await facts_for(kind)
    # Release notes are desk-wide; they still use NQ's v8 text.
    prompt = render(kind, None if kind is NoteKind.RELEASE else NQ, facts, version=V8)
    check(f"nq_{kind.value}.txt", f"SYSTEM\n{prompt.system}\n\nUSER\n{prompt.user}\n")


async def test_nq_prompt_snapshot_with_market_state():
    """Slice 4's new blocks with real quotes: per-quote status, NQ vs ES and megacaps."""
    from core.domain.instruments import AssetClass, QuoteType
    from core.domain.models import DataStatus, FeedStatus
    from tests.desk.test_complete_inputs import inst

    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    service, _, _ = make(now, rows=cpi_rows(), writer=VersionedWriter(V8))
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62),
        inst("ES", 6810.25, change_pct=0.31),
        inst("US10Y", 4.012, change=0.031, change_pct=None, delay=0, feed=FeedStatus.LIVE,
             asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD),
        inst("US2Y", None, status=DataStatus.UNAVAILABLE, change=None, change_pct=None,
             at=None, delay=0, feed=FeedStatus.UNAVAILABLE,
             asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD),
        inst("VIX", 18.39, status=DataStatus.STALE, feed=FeedStatus.STALE, delay=0,
             asset=AssetClass.VOLATILITY_INDEX, quote=QuoteType.INDEX),
        inst("NVDA", 190.1, change_pct=1.2, asset=AssetClass.EQUITY),
        inst("MSFT", 520.4, change_pct=-0.4, asset=AssetClass.EQUITY),
        inst("AAPL", 260.0, status=DataStatus.STALE, asset=AssetClass.EQUITY),
    )})
    await service.ask()
    facts = next(f for k, f in service.writer.calls if k is NoteKind.ASK)
    prompt = render(NoteKind.ASK, NQ, facts, version=V8)
    check("nq_ask_market_state.txt", f"SYSTEM\n{prompt.system}\n\nUSER\n{prompt.user}\n")
