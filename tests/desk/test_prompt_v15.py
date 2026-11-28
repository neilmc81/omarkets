"""desk:v15: the pre-market and weekly review must cover real yields, with the past
week's split of the 10-year yield worked out in Python. Everything else is desk:v14's."""

from datetime import date

import pytest

from core.config.settings import Settings
from core.desk import facts as F
from core.desk import prompts as P
from core.desk.models import NoteKind
from tests.desk.test_prompt_v14 import ROWS, WED, day, v14_facts

# ------------------------------------------------------------------ the split


def split(real_then, nominal_then, real_now, nominal_now):
    return F.week_split(day(date(2026, 11, 25), real_now, nominal_now),
                        day(date(2026, 11, 18), real_then, nominal_then))


def test_the_split_adds_up_and_names_the_main_part():
    assert split(2.88, 5.07, 2.73, 4.85) == (
        "10-year yield -22.0bp over the past week = real yield -15.0bp + breakeven "
        "inflation -7.0bp: mostly the real yield")
    assert split(2.00, 4.00, 2.02, 4.20).endswith("mostly inflation expectations (breakeven)")
    assert split(2.00, 4.00, 2.08, 4.15).endswith(": both parts")
    assert split(2.00, 4.00, 2.10, 4.05).endswith("opposite directions")
    assert split(2.00, 4.00, 2.01, 4.02).endswith("little changed")


def test_no_week_ago_or_no_nominal_gives_unavailable():
    last = day(date(2026, 11, 25), 2.73, 4.85)
    assert F.week_split(last, None) == "unavailable"
    assert F.week_split(day(date(2026, 11, 25), 2.73), day(date(2026, 11, 18), 2.8, 5.0)) \
        == "unavailable"


def test_real_yields_carries_the_split_only_when_asked():
    rows = [day(date(2026, 11, 18), 2.88, 5.07), day(date(2026, 11, 25), 2.73, 4.85)]
    assert "past_week_split" not in F.real_yields(rows, now=WED)
    assert F.real_yields(rows, now=WED, split=True)["past_week_split"].endswith(
        "mostly the real yield")
    assert "past_week_split" not in F.real_yields([], now=WED, split=True)


async def test_v15_notes_get_the_split_and_v14_notes_do_not():
    v15, _ = await v14_facts(P.V15, ROWS)
    v14, _ = await v14_facts(P.V14, ROWS)
    assert v15["real_yields"]["past_week_split"] == "unavailable"  # ROWS span 3 days
    assert "past_week_split" not in v14["real_yields"]


# ------------------------------------------------------------------ the prompt


def test_v15_is_selectable_layered_on_v14_and_not_the_default():
    assert P.PROMPT_VERSION == P.V9 and P.REAL_YIELD_SPLIT_VERSIONS == {P.V15}
    assert Settings(_env_file=None, desk_prompt_version=P.V15).desk_prompt_version == P.V15
    for versions in (P.WHY_VERSIONS, P.EVENT_VERSIONS, P.FRESH_VERSIONS, P.HEDGE_VERSIONS,
                     P.RECENT_LESSONS_VERSIONS, P.CHIP_VERSIONS, P.REAL_YIELD_VERSIONS):
        assert P.V15 in versions


@pytest.mark.parametrize("symbol", sorted(P.SPECS))
def test_v15_changes_only_the_premarket_and_review_tasks(symbol):
    v14, v15 = P.PROMPTS[P.V14][symbol], P.PROMPTS[P.V15][symbol]
    assert (v15.system, v15.stated_facts, v15.event_task, v15.hedge_likely) == (
        v14.system, v14.stated_facts, v14.event_task, v14.hedge_likely)
    assert set(v15.tasks) == set(v14.tasks)
    extra = P._V15_REAL_YIELD_TASK.format(symbol=symbol)
    for kind, text in v15.tasks.items():
        if kind in (NoteKind.PREMARKET, NoteKind.REVIEW):
            assert text == v14.tasks[kind] + extra and "{" not in text
        else:
            assert text == v14.tasks[kind]


def test_nq_has_both_required_tasks():
    tasks = P.PROMPTS[P.V15]["NQ"].tasks
    assert "past_week_split" in tasks[NoteKind.PREMARKET]
    assert "past_week_split" in tasks[NoteKind.REVIEW]


def test_the_writer_drops_the_split_below_v15():
    facts = {"real_yields": {"status": "available", "past_week_split": "x"}, "now": "n"}
    assert P.facts_at(P.V14, facts) == {"real_yields": {"status": "available"}, "now": "n"}
    assert P.facts_at(P.V15, facts) == facts
    assert facts["real_yields"]["past_week_split"] == "x"  # the input is not changed
    assert set(P.facts_at(P.V13, facts)) == {"now"}
