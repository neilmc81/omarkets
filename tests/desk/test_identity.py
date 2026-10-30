"""Phase 1 slice 3: note identity and instrument isolation."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import identity as ID
from core.desk.models import NoteKind
from core.desk.service import DeskService
from core.desk.store import DeskStore
from tests.desk.test_note_facts import AT, note
from tests.desk.test_service import RELEASE, cpi_rows, make

MOVE_AT = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def falling(symbol_value, now=MOVE_AT, step=0.0002):
    return [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=symbol_value * (1 - step * i)) for i in range(80)]


def test_nq_keeps_the_legacy_ids_and_others_carry_the_symbol():
    assert ID.note_id(NoteKind.PREMARKET, "2026-10-29", ID.NQ) == "premarket:2026-10-29"
    assert ID.note_id(NoteKind.MOVE, "20261029T10", "ES") == "move:ES:20261029T10"
    assert ID.note_id(NoteKind.ASK, "20261029T100000", "ES") == "ask:ES:20261029T100000"
    with pytest.raises(ValueError):
        ID.note_id(NoteKind.PREMARKET, "2026-10-29")
    with pytest.raises(ValueError):
        ID.note_id(NoteKind.MOVE, "x", "es")


@pytest.mark.parametrize("kind", sorted(set(NoteKind) - ID.INSTRUMENT_KINDS))
def test_event_ids_never_carry_a_symbol(kind):
    assert ID.note_id(kind, "key") == f"{kind.value}:key"
    with pytest.raises(ValueError):
        ID.note_id(kind, "key", "ES")
    with pytest.raises(ValueError):
        ID.note_id(kind, "key", ID.NQ)


@pytest.mark.parametrize(("value", "ref"), [
    ("premarket:2026-10-29", ID.NoteRef("premarket", "NQ", "2026-10-29")),
    ("move:20261029T10", ID.NoteRef("move", "NQ", "20261029T10")),
    ("ask:20261029T100000", ID.NoteRef("ask", "NQ", "20261029T100000")),
    ("premarket:ES:2026-10-29", ID.NoteRef("premarket", "ES", "2026-10-29")),
    ("release:2026-10-20T12:30:00+00:00", ID.NoteRef("release", None,
                                                    "2026-10-20T12:30:00+00:00")),
    ("earnings:AAPL:2026-10-29", ID.NoteRef("earnings", None, "AAPL:2026-10-29")),
    ("auction:2026-10-29:912797XX1", ID.NoteRef("auction", None, "2026-10-29:912797XX1")),
])
def test_parser_reads_legacy_and_scoped_ids(value, ref):
    assert ID.parse_note_id(value) == ref
    if ref.instrument is not None:
        assert ID.note_id(NoteKind(ref.kind), ref.rest, ref.instrument) == value


def test_production_writes_for_nq_only():
    assert ID.PRODUCTION_INSTRUMENTS == ("NQ",)
    service, _, _ = make(RELEASE)
    assert [i.symbol for i in service.instruments] == ["NQ"]


async def test_nq_and_es_premarkets_carry_their_own_facts_and_ids():
    service, _, _ = make(RELEASE + timedelta(minutes=35), rows=cpi_rows())
    service.instruments = (ID.NQ, ID.Instrument.of("ES"))
    service._location = lambda now, inst: {f"{inst.symbol.lower()}_price_location":
                                           {"of": inst.symbol}}
    await service.tick()
    pre = {n.id: n for n in service.store.recent(include_failed=True)
           if n.kind is NoteKind.PREMARKET}
    assert set(pre) == {"premarket:2026-10-20", "premarket:ES:2026-10-20"}
    assert pre["premarket:2026-10-20"].instrument == "NQ"
    assert pre["premarket:ES:2026-10-20"].instrument == "ES"
    assert pre["premarket:ES:2026-10-20"].title.startswith("ES pre-market")
    nq_facts, nq_env = service.store.facts("premarket:2026-10-20")
    es_facts, es_env = service.store.facts("premarket:ES:2026-10-20")
    assert nq_facts["nq_price_location"] == {"of": "NQ"} and "es_price_location" not in nq_facts
    assert es_facts["es_price_location"] == {"of": "ES"} and "nq_price_location" not in es_facts
    assert (nq_env["instrument"], es_env["instrument"]) == ("NQ", "ES")
    [release] = [n for n in service.store.recent() if n.kind is NoteKind.RELEASE]
    assert release.instrument is None and ID.instrument_of(release.id) is None


async def test_nq_move_fires_at_the_same_threshold_with_its_legacy_id():
    service, _, _ = make(MOVE_AT, series={"NQ": falling(30000.0)})
    [nq] = await service.tick()
    assert nq.id == "move:20261020T10" and nq.instrument == "NQ"
    # A move just under the 0.8% threshold still writes nothing.
    small, _, _ = make(MOVE_AT, series={"NQ": falling(30000.0, step=0.00009)})
    assert NoteKind.MOVE not in [j[1] for j in await small.due_jobs(MOVE_AT)]


async def test_move_cooldown_and_trigger_are_per_instrument():
    service, clock, _ = make(MOVE_AT, series={"NQ": falling(30000.0)})
    service.instruments = (ID.NQ, ID.Instrument.of("ES"))
    assert [n.id for n in await service.tick()] == ["move:20261020T10"]
    # ES moves an hour later: NQ's cooldown does not block it.
    later = MOVE_AT + timedelta(hours=1)
    service.sources = service.sources.__class__(
        **{**vars(service.sources), "history": lambda symbol, *, start=None, end=None,
           limit=500: [o for o in (falling(30000.0, later) if symbol == "NQ"
                                   else falling(6000.0, later) if symbol == "ES" else [])
                       if (start is None or o.timestamp >= start)
                       and (end is None or o.timestamp <= end)]})
    clock["now"] = later
    produced = await service.tick()
    assert [n.id for n in produced] == ["move:ES:20261020T11"]
    assert produced[0].title.startswith("ES down")
    assert {k[0] for k in service._trigger} <= {"NQ", "ES"}


async def test_ask_quota_is_global_and_checked_inside_the_write_lock():
    service, _, _ = make(MOVE_AT)
    service.ask_daily_limit = 1
    es = ID.Instrument.of("ES")
    service.instruments = (ID.NQ, es)
    results = await asyncio.gather(service.ask(), service.ask(es), return_exceptions=True)
    assert sum(isinstance(r, RuntimeError) for r in results) == 1
    assert service.store.count_since("ask", "2026-10-20T00:00:00+00:00") == 1


async def test_ask_reuse_is_per_instrument():
    service, _, _ = make(MOVE_AT)
    service.instruments = (ID.NQ, ID.Instrument.of("ES"))
    nq = await service.ask()
    assert (await service.ask()).id == nq.id
    es = await service.ask(ID.Instrument.of("ES"))
    assert es.id.startswith("ask:ES:") and es.instrument == "ES"


def test_old_database_is_back_filled_and_old_links_resolve(tmp_path):
    path = tmp_path / "desk.db"
    store = DeskStore(path)
    for nid, kind in [("premarket:2026-10-20", NoteKind.PREMARKET),
                      ("move:20261020T10", NoteKind.MOVE), ("ask:20261020T150000", NoteKind.ASK),
                      ("release:2026-10-20T12:30:00+00:00", NoteKind.RELEASE)]:
        store.save(note(nid).model_copy(update={"kind": kind}))
    store.close()
    # Simulate a database written before the column existed.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE desk_notes DROP COLUMN instrument")
    db.commit()
    db.close()
    store = DeskStore(path)
    assert store.get("premarket:2026-10-20").instrument == "NQ"
    assert store.get("move:20261020T10").instrument == "NQ"
    assert store.get("ask:20261020T150000").instrument == "NQ"
    assert store.get("release:2026-10-20T12:30:00+00:00").instrument is None
    assert store.count_since("move", AT.isoformat(), instrument="NQ") == 1
    assert store.count_since("move", AT.isoformat(), instrument="ES") == 0


def test_instrument_stays_out_of_the_payload_for_older_readers():
    store = DeskStore(":memory:")
    store.save(note("move:ES:20261020T10").model_copy(
        update={"kind": NoteKind.MOVE, "instrument": "ES"}))
    payload, column = store._db.execute(
        "SELECT payload, instrument FROM desk_notes").fetchone()
    assert '"instrument"' not in payload and column == "ES"


def test_service_default_matches_production():
    assert DeskService.__init__.__kwdefaults__["instruments"] == ID.PRODUCTION_INSTRUMENTS
