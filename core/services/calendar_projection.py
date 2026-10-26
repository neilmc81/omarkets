"""Non-destructive occurrence projection; stored source identities remain evidence."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent, EventStatus
from core.providers.economic_calendars import summary_period

NY = ZoneInfo("America/New_York")


def _bea_key(event: EconomicEvent) -> tuple[str, str, str] | None:
    if event.provider not in {"bea_calendar", "bea_releases"}:
        return None
    period = event.period or summary_period(event.name)
    moment = event.scheduled_at or event.released_at
    if period is None or moment is None or event.status == EventStatus.CANCELLED:
        return None
    title = re.sub(r"[^a-z0-9]+", " ", event.name.lower()).strip()
    # Issuer titles differ only in the US prefix for the joint trade report.
    title = re.sub(r"^(?:u s |us |united states )", "", title)
    return title, period, moment.astimezone(NY).date().isoformat()


def project_calendar(
    events: list[EconomicEvent], *, now: datetime | None = None
) -> list[EconomicEvent]:
    events = [
        EconomicEvent.model_validate(source.model_dump())
        for item in events
        for source in (item.occurrence_sources or [item])
        if now is None or source.received_at <= now
    ]
    groups: dict[tuple, list[EconomicEvent]] = {}
    for event in events:
        moment = event.scheduled_at
        key: tuple = ("source", event.id)
        if event.provider == "consensus" and "adp" in event.name.casefold() and moment:
            # Same title/day with a minute correction is a schedule revision;
            # monthly and weekly titles remain separate. No cross-day guess.
            key = (
                "adp",
                event.country,
                event.name.casefold().strip(),
                event.period,
                moment.astimezone(NY).date().isoformat(),
                event.unit,
                event.mode,
            )
        if (
            event.provider == "ism_newswire"
            and event.event_code in {"ISM_MFG", "ISM_SERVICES"}
            and event.period is not None
            and event.released_at is not None
            and event.unit == "index points"
            and event.actual is not None
        ):
            # The macro-derived release and issuer event describe the same verified table.
            key = (
                "ism",
                event.event_code,
                event.period,
                event.released_at.isoformat(),
                event.country,
                event.mode,
            )
        bea = _bea_key(event)
        if bea:
            key = ("bea", *bea, event.country, event.mode)
        groups.setdefault(key, []).append(event)
    result = []
    for key, members in groups.items():
        if key[0] == "source":
            result.extend(members)
            continue
        if key[0] == "adp":
            times = [item.scheduled_at for item in members]
            if (max(times) - min(times)).total_seconds() > 300:
                # Ambiguous distinct same-day announcements: leave independent.
                result.extend(members)
                continue
        if key[0] == "ism" and (len({item.actual for item in members}) > 1
                                or len({item.source_url for item in members}) > 1):
            result.extend(members)
            continue
        schedules = [item for item in members if item.scheduled_at is not None]
        notices = [item for item in members if item.provider == "bea_releases"]
        # Only one independently scheduled BEA occurrence can consume a notice.
        if key[0] == "bea" and len({item.id for item in schedules}) > 1:
            result.extend(members)
            continue
        primary = max(
            schedules or members,
            key=lambda item: (
                not item.id.startswith("observed:"),
                item.received_at,
                item.revision,
                item.id,
            ),
        )
        payload = primary.model_dump()
        if key[0] == "adp":
            payload.update(event_code="ADP", category="employment", importance="medium")
        aliases = sorted(
            {item.id for item in members}
            | {alias for item in members for alias in item.occurrence_aliases}
        )
        if key[0] == "bea" and not schedules:
            # Until a schedule matches, notice IDs remain their durable source IDs.
            result.extend(members)
            continue
        identity = (("bea_schedule", primary.provider, primary.source_event_id, primary.mode)
                    if key[0] == "bea" else key)
        canonical = "occurrence:" + hashlib.sha256(repr(identity).encode()).hexdigest()[:32]
        payload.update(
            id=canonical,
            occurrence_aliases=aliases,
            revision=sum(item.revision for item in members),
        )
        if key[0] == "bea" and "international trade" in primary.name.lower():
            payload["event_code"] = "TRADE"
        if notices and schedules:
            notice = max(notices, key=lambda item: (item.received_at, item.revision, item.id))
            payload.update(status=EventStatus.RELEASED, released_at=notice.released_at)
            payload["notes"] = list(
                dict.fromkeys(
                    [
                        *primary.notes,
                        "Official publication notice matched title, "
                        "reference period and release date; "
                        "numeric actual remains unavailable unless separately verified.",
                    ]
                )
            )
        payload["occurrence_sources"] = [
            item.model_dump(exclude={"occurrence_sources", "occurrence_aliases"})
            for item in sorted(members, key=lambda item: (item.received_at, item.id))
        ]
        result.append(EconomicEvent.model_validate(payload))
    return _current_issuer_actuals(_nar_actuals(_adp_actuals(result, now), now), now)


def _adp_actuals(events: list[EconomicEvent], now: datetime | None) -> list[EconomicEvent]:
    """Reconcile only a unique issuer/consensus pair in the same publication slot."""
    issuers = [
        item
        for item in events
        if item.provider == "adp_releases"
        and item.actual is not None
        and item.released_at is not None
        and item.released_at <= item.received_at
        and (now is None or item.released_at <= now and item.received_at <= now)
    ]
    schedules = [
        item
        for item in events
        if item.provider == "consensus"
        and item.event_code == "ADP"
        and item.scheduled_at is not None
        and item.status != EventStatus.CANCELLED
        and item.unit in {"value", "jobs", "jobs/week (four-week average, seasonally adjusted)"}
        and (now is None or item.scheduled_at <= now and item.received_at <= now)
    ]

    def matches(schedule: EconomicEvent, issuer: EconomicEvent) -> bool:
        weekly = "weekly" in schedule.name.casefold()
        return (
            issuer.frequency == ("weekly" if weekly else "monthly")
            and schedule.country == issuer.country
            and schedule.mode == issuer.mode
            and (schedule.period is None or schedule.period == issuer.period)
            and schedule.scheduled_at.astimezone(NY).date()
            == issuer.released_at.astimezone(NY).date()
            and abs((schedule.scheduled_at - issuer.released_at).total_seconds()) <= 300
        )

    replacements, consumed = {}, set()
    for schedule in schedules:
        candidates = [issuer for issuer in issuers if matches(schedule, issuer)]
        if len(candidates) != 1:
            continue
        issuer = candidates[0]
        if sum(matches(other, issuer) for other in schedules) != 1:
            continue
        if schedule.actual is not None and schedule.actual != issuer.actual:
            continue
        source_records = [
            *(schedule.occurrence_sources or [schedule]),
            *(issuer.occurrence_sources or [issuer]),
        ]
        payload = schedule.model_dump()
        payload.update(
            actual=issuer.actual,
            actual_basis="source_release",
            status=EventStatus.RELEASED,
            period=issuer.period,
            unit=issuer.unit,
            frequency=issuer.frequency,
            released_at=issuer.released_at,
            received_at=max(schedule.received_at, issuer.received_at),
            revised_previous=issuer.revised_previous,
            # Keep the revision's own baseline: the issuer's first-reported prior.
            **({"previous": issuer.previous}
               if issuer.revised_previous is not None and issuer.previous is not None else {}),
            raw_surprise=None,
            occurrence_aliases=sorted(set(schedule.occurrence_aliases + [schedule.id, issuer.id])),
            occurrence_sources=[
                row.model_dump(exclude={"occurrence_sources", "occurrence_aliases"})
                for row in source_records
            ],
            revision=schedule.revision + issuer.revision,
            notes=[
                *schedule.notes,
                *issuer.notes,
                f"Actual verified against ADP issuer release: {issuer.source_url}",
                "Issuer frequency, publication date and calendar time matched uniquely.",
            ],
        )
        replacements[schedule.id] = EconomicEvent.model_validate(payload)
        consumed.add(issuer.id)
    return [replacements.get(item.id, item) for item in events if item.id not in consumed]


def _nar_actuals(events: list[EconomicEvent], now: datetime | None) -> list[EconomicEvent]:
    """A unique national SAAR issuer release may enrich a same-day consensus slot."""
    issuers = [e for e in events if e.provider == "nar_releases"
               and e.actual_basis == "source_release" and e.actual is not None
               and e.released_at is not None and e.released_at <= e.received_at
               and e.period is not None and e.frequency == "monthly"
               and e.unit == "homes/year (seasonally adjusted annual rate)"
               and (now is None or e.received_at <= now and e.released_at <= now)]
    schedules = [e for e in events if e.provider == "consensus"
                 and e.event_code == "EXISTING_HOME_SALES" and e.scheduled_at is not None
                 and e.status != EventStatus.CANCELLED and e.unit == "value"
                 and e.frequency == "release"
                 and (now is None or e.received_at <= now and e.scheduled_at <= now)]

    def matches(schedule, issuer):
        return (schedule.country == issuer.country and schedule.mode == issuer.mode
                and (schedule.period is None or schedule.period == issuer.period)
                and schedule.scheduled_at.astimezone(NY).date()
                == issuer.released_at.astimezone(NY).date()
                and abs((schedule.scheduled_at - issuer.released_at).total_seconds()) <= 300
                and (schedule.actual is None or schedule.actual == issuer.actual))

    replacements, consumed = {}, set()
    for schedule in schedules:
        candidates = [issuer for issuer in issuers if matches(schedule, issuer)]
        if len(candidates) != 1:
            continue
        issuer = candidates[0]
        if sum(matches(other, issuer) for other in schedules) != 1:
            continue
        sources = [*(schedule.occurrence_sources or [schedule]),
                   *(issuer.occurrence_sources or [issuer])]
        payload = schedule.model_dump()
        payload.update(actual=issuer.actual, actual_basis="source_release",
                       period=issuer.period, unit=issuer.unit, frequency=issuer.frequency,
                       status=EventStatus.RELEASED, released_at=issuer.released_at,
                       received_at=max(schedule.received_at, issuer.received_at),
                       revision=schedule.revision + issuer.revision, raw_surprise=None,
                       occurrence_aliases=sorted(set(schedule.occurrence_aliases
                                                     + [schedule.id, issuer.id])),
                       occurrence_sources=[s.model_dump(exclude={"occurrence_sources",
                                                                  "occurrence_aliases"})
                                           for s in sources],
                       notes=[*schedule.notes, *issuer.notes,
                              "National NAR SAAR matched unique calendar slot: "
                              f"{issuer.source_url}"])
        replacements[schedule.id] = EconomicEvent.model_validate(payload)
        consumed.add(issuer.id)
    return [replacements.get(e.id, e) for e in events if e.id not in consumed]


def _current_issuer_actuals(
    events: list[EconomicEvent], now: datetime | None
) -> list[EconomicEvent]:
    """Only two audited current issuer metrics; require one-to-one source/slot pairing."""
    specifications = {
        "census_retail": ("RETAIL_SALES", "retail sales m/m", (8, 30, 0)),
        "fed_g17": ("INDUSTRIAL_PRODUCTION", "industrial production m/m", (9, 15, 0)),
    }
    issuers = [
        e
        for e in events
        if e.provider in specifications
        and e.event_code == specifications[e.provider][0]
        and e.actual is not None
        and e.actual_basis == "source_release"
        and e.period is not None
        and e.unit == "percent"
        and e.frequency == "monthly"
        and (now is None or e.received_at <= now)
    ]
    schedules = [
        e
        for e in events
        if e.provider == "consensus"
        and e.scheduled_at is not None
        and e.status != EventStatus.CANCELLED
        and e.unit in {"percent", "value"}
        and (e.unit == "percent" or e.consensus is None and e.previous is None)
        and (now is None or e.received_at <= now and e.scheduled_at <= now)
    ]

    def matches(schedule: EconomicEvent, issuer: EconomicEvent) -> bool:
        code, title, time = specifications[issuer.provider]
        local = schedule.scheduled_at.astimezone(NY)
        if (
            schedule.event_code != code
            or schedule.name.casefold().strip() != title
            or schedule.country != issuer.country
            or schedule.mode != issuer.mode
            or (schedule.period is not None and schedule.period != issuer.period)
            or (local.hour, local.minute, local.second) != time
            or issuer.received_at < schedule.scheduled_at
            or schedule.actual is not None
            and schedule.actual != issuer.actual
        ):
            return False
        if issuer.provider == "census_retail":
            # The Census API has no publication time. Keep released_at null; never
            # turn the calendar's scheduled time into a publication claim. Retail
            # sales for month M are published in month M+1, so a slot only takes
            # the month that release covers (a delayed release fails closed).
            year, month = (int(part) for part in issuer.period.split("-"))
            covered = f"{year + month // 12:04d}-{month % 12 + 1:02d}"
            return (
                issuer.source_event_id == f"{issuer.period}:RETAIL_SALES"
                and f"{local.year:04d}-{local.month:02d}" == covered
            )
        return (
            issuer.released_at == schedule.scheduled_at and issuer.released_at <= issuer.received_at
        )

    replacements, consumed = {}, set()
    for schedule in schedules:
        candidates = [e for e in issuers if matches(schedule, e)]
        if len(candidates) != 1:
            continue
        issuer = candidates[0]
        if sum(matches(other, issuer) for other in schedules) != 1:
            continue
        sources = [
            *(schedule.occurrence_sources or [schedule]),
            *(issuer.occurrence_sources or [issuer]),
        ]
        payload = schedule.model_dump()
        payload.update(
            actual=issuer.actual,
            actual_basis="source_release",
            raw_surprise=None,
            status=EventStatus.RELEASED,
            period=issuer.period,
            unit=issuer.unit,
            frequency=issuer.frequency,
            released_at=issuer.released_at,
            received_at=max(schedule.received_at, issuer.received_at),
            revision=schedule.revision + issuer.revision,
            occurrence_aliases=sorted(set(schedule.occurrence_aliases + [schedule.id, issuer.id])),
            occurrence_sources=[
                e.model_dump(exclude={"occurrence_sources", "occurrence_aliases"}) for e in sources
            ],
            notes=list(
                dict.fromkeys(
                    [
                        *schedule.notes,
                        *issuer.notes,
                        f"Actual source: {issuer.source_url}; "
                        "unique metric, month and publication-day match.",
                    ]
                )
            ),
        )
        replacements[schedule.id] = EconomicEvent.model_validate(payload)
        consumed.add(issuer.id)
    return [replacements.get(e.id, e) for e in events if e.id not in consumed]
