"""Macro trends reach the desk notes that put data in context, and only those."""

from __future__ import annotations

from types import SimpleNamespace

from core.desk.models import NoteKind
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from tests.desk.test_macro import NOW, series
from tests.desk.test_service import FakeWriter


def desk(macro_series):
    writer = FakeWriter()
    service = DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments={}),
                            history=lambda *a, **k: [], calendar_events=lambda s, e: [],
                            news_events=lambda: [], macro_series=macro_series),
        store=DeskStore(":memory:"), writer=writer, clock=lambda: NOW)
    return service, writer


async def test_premarket_and_primer_get_macro_trends():
    cpi = series("CPI_YOY", [2.4, 2.5, 2.6, 3.0, 3.2, 3.7])
    service, writer = desk(lambda: [cpi])
    await service.build_premarket("premarket:t", NOW)
    await service.build_primer("primer:t", NOW)
    for kind, facts in writer.calls:
        assert kind in (NoteKind.PREMARKET, NoteKind.PRIMER)
        assert facts["macro_trends"]["inflation"][0]["latest"] == "3.7% (Sep 2026)"


async def test_no_source_no_series_or_failure_leaves_macro_trends_out():
    def broken():
        raise RuntimeError("economics down")

    for source in (None, lambda: [], broken):
        service, writer = desk(source)
        await service.build_premarket("premarket:t", NOW)
        [(_, facts)] = writer.calls
        assert "macro_trends" not in facts


async def test_on_demand_notes_do_not_get_macro_trends():
    cpi = series("CPI_YOY", [2.4, 2.5, 2.6, 3.0, 3.2, 3.7])
    service, writer = desk(lambda: [cpi])
    await service.ask()
    [(kind, facts)] = writer.calls
    assert kind is NoteKind.ASK and "macro_trends" not in facts
