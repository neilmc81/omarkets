"""The desk scorecard: is the AI writer any good, and did a prompt change help?

Deterministic and model-free: it only counts what the store already holds (notes,
their ``why`` level, their "did it hold?" verdict, the reader's mark and the stored
cost). Every count is split by note kind and prompt version, and every cost by
model and prompt version: nothing is averaged across a prompt version or a model.

"Not available" is never folded into zero. A note with no ``why`` (written before
``desk:v10``) is counted as ``not_recorded``, a note whose follow-up has no verdict
as ``no_verdict``, a note with no stored cost is left out of the mean (and counted
apart as ``unpriced``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from core.desk import followup
from core.desk.models import DeskNote, NoteKind

#: The default period, and how far back the comparison period reaches.
DEFAULT_WINDOW = timedelta(days=7)
#: Kinds whose draft carries a ``why`` (desk:v10 and later).
WHY_KINDS = frozenset({NoteKind.MOVE, NoteKind.ASK})
UNKNOWN = "unknown"
VERDICTS = ("grew", "held", "faded", "reversed", "quiet")
_SEPARATOR = " · "


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WhyCounts(_Frozen):
    reported: int = Field(ge=0)
    likely: int = Field(ge=0)
    unknown: int = Field(ge=0)
    #: Written notes with no ``why`` (an older prompt version): not the same as unknown.
    not_recorded: int = Field(ge=0)


class VerdictCounts(_Frozen):
    grew: int = Field(ge=0)
    held: int = Field(ge=0)
    faded: int = Field(ge=0)
    reversed: int = Field(ge=0)
    quiet: int = Field(ge=0)
    #: Notes whose follow-up has no verdict (yet, or not available): not zero of any verdict.
    no_verdict: int = Field(ge=0)


class RatingCounts(_Frozen):
    useful: int = Field(ge=0)
    not_useful: int = Field(ge=0)
    unrated: int = Field(ge=0)


class ScorecardGroup(_Frozen):
    """One note kind written under one prompt version."""

    kind: NoteKind
    prompt_version: str
    written: int = Field(ge=0)
    failed: int = Field(ge=0)
    #: Only for move and ask notes; ``None`` for every other kind.
    why: WhyCounts | None = None
    #: Only for kinds that get a "did it hold?" follow-up; ``None`` for every other kind.
    verdicts: VerdictCounts | None = None
    ratings: RatingCounts


class KindCount(_Frozen):
    kind: NoteKind
    written: int = Field(ge=0)
    failed: int = Field(ge=0)


class VersionCount(_Frozen):
    prompt_version: str
    written: int = Field(ge=0)
    failed: int = Field(ge=0)


class CostGroup(_Frozen):
    """Stored cost of notes from one model under one prompt version."""

    model: str
    prompt_version: str
    notes: int = Field(ge=0)
    #: Notes with a stored cost; the mean is over these only.
    priced: int = Field(ge=0)
    unpriced: int = Field(ge=0)
    total_usd: float = Field(ge=0)
    #: ``None`` when no note has a stored cost: not available, not zero.
    mean_usd: float | None = None


class ScorecardPeriod(_Frozen):
    #: Half-open ``[start, end)``, UTC.
    start: AwareDatetime
    end: AwareDatetime
    written: int = Field(ge=0)
    failed: int = Field(ge=0)
    by_kind: list[KindCount]
    by_prompt_version: list[VersionCount]
    groups: list[ScorecardGroup]
    costs: list[CostGroup]


class Scorecard(_Frozen):
    generated_at: AwareDatetime
    current: ScorecardPeriod
    #: The period of the same length just before ``current``; ``None`` when not asked for.
    previous: ScorecardPeriod | None = None


def split_model(model: str | None) -> tuple[str, str]:
    """``("model", "prompt version")`` from a stored ``"model · desk:v11"``; both
    ``"unknown"`` when the note recorded none."""
    if not model:
        return UNKNOWN, UNKNOWN
    name, sep, version = model.rpartition(_SEPARATOR)
    return (name, version) if sep and name and version else (model, UNKNOWN)


def _group(kind: NoteKind, version: str, notes: Sequence[DeskNote], failed: int) -> ScorecardGroup:
    written = [n for n in notes if n.status == "ok"]
    why = None
    if kind in WHY_KINDS:
        levels = [n.draft.why.level if n.draft is not None and n.draft.why is not None else None
                  for n in written]
        why = WhyCounts(reported=levels.count("reported"), likely=levels.count("likely"),
                        unknown=levels.count("unknown"), not_recorded=levels.count(None))
    verdicts = None
    if kind in followup.FOLLOWUP_KINDS or kind is NoteKind.MOVE:
        seen = [n.follow_up.verdict if n.follow_up is not None else None for n in written]
        verdicts = VerdictCounts(**{v: seen.count(v) for v in VERDICTS},
                                 no_verdict=seen.count(None))
    useful = sum(1 for n in written if n.rating == "useful")
    not_useful = sum(1 for n in written if n.rating == "not_useful")
    return ScorecardGroup(
        kind=kind, prompt_version=version, written=len(written), failed=failed, why=why,
        verdicts=verdicts,
        ratings=RatingCounts(useful=useful, not_useful=not_useful,
                             unrated=len(written) - useful - not_useful),
    )


def period(notes: Sequence[DeskNote], start: datetime, end: datetime) -> ScorecardPeriod:
    """The scorecard of ``notes`` (those created in ``[start, end)``)."""
    inside = [n for n in notes if start <= n.created_at < end]
    buckets: dict[tuple[NoteKind, str], list[DeskNote]] = defaultdict(list)
    spend: dict[tuple[str, str], list[DeskNote]] = defaultdict(list)
    for note in inside:
        model, version = split_model(note.model)
        buckets[(note.kind, version)].append(note)
        spend[(model, version)].append(note)
    groups = [_group(kind, version, items, sum(1 for n in items if n.status == "failed"))
              for (kind, version), items in sorted(buckets.items(),
                                                   key=lambda kv: (kv[0][0].value, kv[0][1]))]
    kinds: dict[NoteKind, list[int]] = defaultdict(lambda: [0, 0])
    versions: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in groups:
        for tally in (kinds[g.kind], versions[g.prompt_version]):
            tally[0] += g.written
            tally[1] += g.failed
    costs = []
    for (model, version), items in sorted(spend.items()):
        priced = [n.cost_usd for n in items if n.cost_usd is not None]
        total = sum(priced)
        costs.append(CostGroup(
            model=model, prompt_version=version, notes=len(items), priced=len(priced),
            unpriced=len(items) - len(priced), total_usd=total,
            mean_usd=total / len(priced) if priced else None))
    return ScorecardPeriod(
        start=start, end=end,
        written=sum(g.written for g in groups), failed=sum(g.failed for g in groups),
        by_kind=[KindCount(kind=k, written=w, failed=f)
                 for k, (w, f) in sorted(kinds.items(), key=lambda kv: kv[0].value)],
        by_prompt_version=[VersionCount(prompt_version=v, written=w, failed=f)
                           for v, (w, f) in sorted(versions.items())],
        groups=groups, costs=costs,
    )


def build(notes: Sequence[DeskNote], *, now: datetime, start: datetime, end: datetime,
          compare: bool = True) -> Scorecard:
    """The scorecard for ``[start, end)`` and, with ``compare``, for the equally long
    period right before it. ``notes`` must cover both."""
    length = end - start
    return Scorecard(
        generated_at=now,
        current=period(notes, start, end),
        previous=period(notes, start - length, start) if compare else None,
    )


# --------------------------------------------------------------------------- text


def _cost(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def format_text(card: Scorecard) -> str:
    """The scorecard as plain text, the periods one after the other."""
    lines: list[str] = []
    for title, per in (("This period", card.current), ("Previous period", card.previous)):
        if per is None:
            continue
        lines.append(f"{title}: {per.start:%Y-%m-%d %H:%M} → {per.end:%Y-%m-%d %H:%M} UTC")
        lines.append(f"  notes written {per.written}, failed {per.failed}")
        if not per.groups:
            lines.append("  no notes in this period")
            lines.append("")
            continue
        lines.append("  by kind and prompt version")
        lines.append(f"    {'kind':<10} {'version':<9} {'written':>7} {'failed':>6}  "
                     f"{'useful':>6} {'not':>4} {'unrated':>7}  why (rep/lik/unk/n-a)  "
                     "did it hold (grew/held/faded/rev/quiet/n-a)")
        for g in per.groups:
            why = ("-" if g.why is None else
                   f"{g.why.reported}/{g.why.likely}/{g.why.unknown}/{g.why.not_recorded}")
            hold = ("-" if g.verdicts is None else
                    f"{g.verdicts.grew}/{g.verdicts.held}/{g.verdicts.faded}/"
                    f"{g.verdicts.reversed}/{g.verdicts.quiet}/{g.verdicts.no_verdict}")
            lines.append(f"    {g.kind.value:<10} {g.prompt_version:<9} {g.written:>7} "
                         f"{g.failed:>6}  {g.ratings.useful:>6} {g.ratings.not_useful:>4} "
                         f"{g.ratings.unrated:>7}  {why:<21}  {hold}")
        lines.append("  cost by model and prompt version (stored, USD)")
        lines.append(f"    {'model':<40} {'version':<9} {'notes':>5} {'total':>9} {'mean':>9}")
        for c in per.costs:
            lines.append(f"    {c.model:<40} {c.prompt_version:<9} {c.notes:>5} "
                         f"{c.total_usd:>9.4f} {_cost(c.mean_usd):>9}"
                         + (f"  ({c.unpriced} unpriced)" if c.unpriced else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CostGroup",
    "KindCount",
    "RatingCounts",
    "Scorecard",
    "ScorecardGroup",
    "ScorecardPeriod",
    "VerdictCounts",
    "VersionCount",
    "WhyCounts",
    "build",
    "format_text",
    "period",
    "split_model",
]
