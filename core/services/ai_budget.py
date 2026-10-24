"""Persistent, atomic paid-inference admission. Unknown charges stay reserved.

Money uses integer micro-dollars. Every actual provider attempt must reserve
its full maximum liability before network I/O, including retries and fallbacks.
No credential, prompt or market payload is stored here.
"""
from __future__ import annotations

import asyncio
import calendar
import math
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from core.notifications.notifier import ALERT_ICON

#: Paid AI providers Omarkets admits. The desk writer uses OpenRouter.
PROVIDERS = ("openrouter",)
ALL_PAUSED = "All paid AI providers are paused; see provider reasons below."
WEEKLY_WARNING = "80% of the weekly Omarkets AI budget is spent."


class BudgetPaused(RuntimeError):
    pass


class ProviderBudgetStatus(BaseModel):
    provider: str
    blocked: bool
    reason: str | None = None


class AIBudgetStatus(BaseModel):
    generated_at: datetime
    monthly_limit_usd: float
    month_spent_usd: float
    month_reserved_usd: float
    month_remaining_usd: float
    daily_limit_usd: float
    day_spent_usd: float
    day_reserved_usd: float
    paused: bool
    reason: str | None
    resumes_at: datetime | None
    providers: list[ProviderBudgetStatus]
    notification_delivery: str
    spending_limits_enabled: bool = True
    budget_period: Literal["monthly", "weekly"] = "monthly"
    budget_timezone: str = "America/New_York"
    weekly_limit_usd: float = 0
    week_spent_usd: float = 0
    week_reserved_usd: float = 0
    week_remaining_usd: float = 0
    week_resets_at: datetime | None = None
    allowance_started_at: datetime | None = None


def _money(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid cost")
    return math.ceil(value * 1_000_000)


class AICostGuard:
    def __init__(self, path: Path | str, *, monthly_limit_usd: float = 5,
                 daily_limit_usd: float = .20,
                 weekly_limit_usd: float = 0,
                 openrouter_daily_request_limit: int | None = None,
                 openrouter_background_request_limit: int | None = None,
                 clock: Callable | None = None,
                 timezone: str = "America/New_York", notifier=None):
        self.path = Path(path)
        self.monthly = _money(monthly_limit_usd)
        self.daily = _money(daily_limit_usd)
        self.weekly = _money(weekly_limit_usd)
        self.openrouter_daily_request_limit = (
            (0 if self.weekly else 48) if openrouter_daily_request_limit is None
            else openrouter_daily_request_limit)
        self.openrouter_background_request_limit = (
            (0 if self.weekly else 32) if openrouter_background_request_limit is None
            else openrouter_background_request_limit)
        for cap in (self.openrouter_daily_request_limit, self.openrouter_background_request_limit):
            if not isinstance(cap, int) or cap < 0:
                raise ValueError("request caps must be nonnegative integers")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.zone = ZoneInfo(timezone)
        self.notifier = notifier
        self._lock = threading.RLock()
        self._db = None
        self._task = None
        self._status_reads: set[asyncio.Task] = set()
        self._stopping = False

    @property
    def spending_limits_enabled(self) -> bool:
        return self.weekly > 0 or self.monthly > 0 or self.daily > 0

    def _request_cap(self, provider: str) -> int:
        return self.openrouter_daily_request_limit

    def initialize(self):
        with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=2)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_charges(
                  id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                  day TEXT NOT NULL, month TEXT NOT NULL, reserved INTEGER NOT NULL,
                  actual INTEGER, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ai_charges_period ON ai_charges(month,day);
                CREATE TABLE IF NOT EXISTS ai_blocks(
                  provider TEXT PRIMARY KEY, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_alerts(
                  key TEXT PRIMARY KEY, reason TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                  attempted_at TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS ai_pauses(
                  day TEXT NOT NULL, provider TEXT NOT NULL, reason TEXT NOT NULL,
                  PRIMARY KEY(day,provider));
                CREATE TABLE IF NOT EXISTS ai_daily_overrides(
                  day TEXT PRIMARY KEY, limit_microusd INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_unsubmitted(
                  id TEXT PRIMARY KEY REFERENCES ai_charges(id));
                CREATE TABLE IF NOT EXISTS ai_priority_requests(
                  id TEXT PRIMARY KEY REFERENCES ai_charges(id));
                CREATE TABLE IF NOT EXISTS ai_allowance_resets(
                  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_allowance_baselines(
                  reset_id TEXT NOT NULL, charge_id TEXT NOT NULL,
                  PRIMARY KEY(reset_id,charge_id));
            """)
            self._db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(ai_charges)")}
            if "settled_at" not in columns:
                self._db.execute("ALTER TABLE ai_charges ADD COLUMN settled_at TEXT")
                self._db.execute(
                    "UPDATE ai_charges SET settled_at=created_at WHERE actual IS NOT NULL")
            self._db.commit()

    def _weekly_totals(self, now=None):
        now = (now or self.clock()).astimezone(self.zone)
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        reset = start + timedelta(days=7)
        epoch_row = self._db.execute(
            "SELECT started_at,id FROM ai_allowance_resets ORDER BY rowid DESC LIMIT 1").fetchone()
        epoch = datetime.fromisoformat(epoch_row[0]) if epoch_row else None
        cutoff = max(start.astimezone(UTC), epoch) if epoch else start.astimezone(UTC)
        excluded = {row[0] for row in self._db.execute(
            "SELECT charge_id FROM ai_allowance_baselines WHERE reset_id=?",
            (epoch_row[1],)).fetchall()} if epoch_row else set()
        rows = self._db.execute("SELECT id,reserved,actual,settled_at FROM ai_charges").fetchall()
        spent = sum(actual for identity, _, actual, settled in rows if actual is not None
                    and identity not in excluded
                    and datetime.fromisoformat(settled).astimezone(UTC) >= cutoff)
        reserved = sum(reserved for _, reserved, actual, _ in rows if actual is None)
        return spent, reserved, reset.astimezone(UTC), epoch

    def activate_weekly_allowance(self, identity: str, *, reason: str):
        """Auditable, idempotent operator reset; outstanding liabilities are retained."""
        if not self.weekly or not identity.strip() or not reason.strip():
            raise ValueError("weekly allowance, reset identity and reason are required")
        self.initialize()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                inserted = self._db.execute(
                    "INSERT OR IGNORE INTO ai_allowance_resets VALUES (?,?,?)",
                                 (identity, self.clock().astimezone(UTC).isoformat(), reason))
                if inserted.rowcount:
                    self._db.execute(
                        "INSERT INTO ai_allowance_baselines SELECT ?,id FROM ai_charges "
                        "WHERE actual IS NOT NULL", (identity,))
                self._db.execute("DELETE FROM ai_pauses WHERE reason IN (?,?,?,?,?,?)", (
                    "Monthly Omarkets AI budget cannot cover the next request.",
                    "Daily paced Omarkets AI budget cannot cover the next request.",
                    "Monthly Omarkets AI cost limit reached; paid interpretation is paused.",
                    "Daily paced Omarkets AI cost limit reached; paid interpretation is paused.",
                    "Weekly Omarkets AI budget cannot cover the next request.",
                    "Weekly Omarkets AI cost limit reached; paid interpretation is paused."))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _period(self):
        now = self.clock().astimezone(self.zone)
        return now, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _totals(self, day, month):
        rows = self._db.execute(
            "SELECT day,reserved,actual FROM ai_charges WHERE month=?", (month,)
        ).fetchall()
        spent = sum(r[2] or 0 for r in rows)
        reserved = sum(r[1] for r in rows if r[2] is None)
        daily_spent = sum(r[2] or 0 for r in rows if r[0] == day)
        daily_reserved = sum(r[1] for r in rows if r[0] == day and r[2] is None)
        return spent, reserved, daily_spent, daily_reserved

    def _limits(self):
        now, day, month = self._period()
        totals = self._totals(day, month)
        if self.weekly:
            spent, reserved, reset, _ = self._weekly_totals(now)
            reason = ("Weekly Omarkets AI cost limit reached; paid interpretation is paused."
                      if spent + reserved >= self.weekly else None)
            return now, day, month, totals, 0, reason, reset if reason else None
        prior = totals[0] + totals[1] - totals[2] - totals[3]
        remaining_days = calendar.monthrange(now.year, now.month)[1] - now.day + 1
        paced_daily = self.daily
        if self.monthly > 0 and self.daily > 0:
            paced_daily = min(self.daily, max(0, (self.monthly - prior) // remaining_days))
        override = self._db.execute(
            "SELECT limit_microusd FROM ai_daily_overrides WHERE day=?", (day,)
        ).fetchone()
        if override and self.daily > 0:
            paced_daily = override[0]
        reason, reset = None, None
        if self.monthly > 0 and totals[0] + totals[1] >= self.monthly:
            reason = "Monthly Omarkets AI cost limit reached; paid interpretation is paused."
            year, month_number = now.year + (now.month == 12), now.month % 12 + 1
            reset = datetime(year, month_number, 1, tzinfo=self.zone)
        elif self.daily > 0 and totals[2] + totals[3] >= paced_daily:
            reason = "Daily paced Omarkets AI cost limit reached; paid interpretation is paused."
            reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return now, day, month, totals, paced_daily, reason, reset

    def _alert(self, key, reason):
        self._db.execute("INSERT OR IGNORE INTO ai_alerts(key,reason) VALUES (?,?)", (key, reason))

    def _obsolete_cost_pause(self, provider, pause_reason, day, totals, daily):
        if self.weekly and pause_reason in {
            "Weekly Omarkets AI budget cannot cover the next request.",
            "Weekly Omarkets AI cost limit reached; paid interpretation is paused.",
        }:
            spent, reserved, _, _ = self._weekly_totals()
            return spent + reserved < self.weekly
        if pause_reason not in {
            "Monthly Omarkets AI budget cannot cover the next request.",
            "Daily paced Omarkets AI budget cannot cover the next request.",
            "Monthly Omarkets AI cost limit reached; paid interpretation is paused.",
            "Daily paced Omarkets AI cost limit reached; paid interpretation is paused.",
        }:
            return False
        if self.weekly:
            # Historical month/day pauses cannot constrain the new weekly policy.
            return True
        # A verified settlement must exist today. Unknown charges still count
        # in totals. Use the largest recorded provider reservation conservatively;
        # reserve() always checks the actual proposed maximum again.
        row = self._db.execute(
            "SELECT MAX(reserved), COUNT(actual) FROM ai_charges WHERE day=? AND provider=? "
            "AND model!='prior-usage' AND id NOT IN (SELECT id FROM ai_unsubmitted)",
            (day, provider),
        ).fetchone()
        if not row[0] or not row[1]:
            return False
        return ((self.monthly <= 0 or totals[0] + totals[1] + row[0] <= self.monthly)
                and (self.daily <= 0 or totals[2] + totals[3] + row[0] <= daily))

    def reserve(self, provider: str, model: str, max_cost_usd: float,
                *, priority: bool = False) -> str:
        maximum = _money(max_cost_usd)
        if maximum <= 0:
            raise ValueError("request maximum must be positive")
        self.initialize()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                now, day, month, totals, daily, reason, _ = self._limits()
                block = self._db.execute(
                    "SELECT reason FROM ai_blocks WHERE provider=?", (provider,)
                ).fetchone()
                if block:
                    reason = block[0]
                count = self._db.execute(
                    "SELECT COUNT(*) FROM ai_charges WHERE day=? AND provider=? "
                    "AND model!='prior-usage' AND id NOT IN (SELECT id FROM ai_unsubmitted)",
                    (day, provider),
                ).fetchone()[0]
                cap = self._request_cap(provider)
                pause = self._db.execute(
                    "SELECT reason FROM ai_pauses WHERE day=? AND provider=?", (day, provider)
                ).fetchone()
                if self.spending_limits_enabled and pause and pause[0] == (
                    f"Daily {provider} request limit reached; interpretation is paused."
                ) and (cap == 0 or count < cap):
                    self._db.execute("DELETE FROM ai_pauses WHERE day=? AND provider=?",
                                     (day, provider))
                    pause = None
                if pause and not block and self.spending_limits_enabled:
                    if self._obsolete_cost_pause(provider, pause[0], day, totals, daily):
                        self._db.execute("DELETE FROM ai_pauses WHERE day=? AND provider=?",
                                         (day, provider))
                    else:
                        reason = pause[0]
                if not reason and self.weekly:
                    spent, reserved, _, _ = self._weekly_totals(now)
                    if spent + reserved + maximum > self.weekly:
                        reason = "Weekly Omarkets AI budget cannot cover the next request."
                if (not reason and not self.weekly and self.monthly > 0
                        and totals[0] + totals[1] + maximum > self.monthly):
                    reason = "Monthly Omarkets AI budget cannot cover the next request."
                elif (not reason and not self.weekly and self.daily > 0
                      and totals[2] + totals[3] + maximum > daily):
                    reason = "Daily paced Omarkets AI budget cannot cover the next request."
                if not reason and self.spending_limits_enabled and cap > 0 and count >= cap:
                    reason = f"Daily {provider} request limit reached; interpretation is paused."
                if (not reason and self.spending_limits_enabled
                        and provider == "openrouter" and not priority):
                    background = self._db.execute(
                        "SELECT COUNT(*) FROM ai_charges WHERE day=? AND provider=? "
                        "AND model!='prior-usage' AND id NOT IN (SELECT id FROM ai_unsubmitted) "
                        "AND id NOT IN (SELECT id FROM ai_priority_requests)", (day, provider)
                    ).fetchone()[0]
                    if (self.openrouter_background_request_limit > 0
                            and background >= self.openrouter_background_request_limit):
                        self._db.commit()
                        raise BudgetPaused("Background AI allowance used; slots held for releases.")
                if reason:
                    if not block:
                        self._db.execute("INSERT OR REPLACE INTO ai_pauses VALUES (?,?,?)",
                                         (day, provider, reason))
                    self._alert(f"{month}:{day}:{provider}:{reason}", reason)
                    self._db.commit()
                    raise BudgetPaused(reason)
                token = str(uuid4())
                self._db.execute("INSERT INTO ai_charges "
                                 "(id,provider,model,day,month,reserved,actual,created_at) "
                                 "VALUES (?,?,?,?,?,?,NULL,?)",
                                 (token, provider, model, day, month, maximum, now.isoformat()))
                if priority:
                    self._db.execute("INSERT INTO ai_priority_requests VALUES (?)", (token,))
                self._db.commit()
                return token
            except BaseException:
                self._db.rollback()
                raise

    def cancel_before_submission(self, token: str):
        """Known setup failure only: retain audit record but release its request slot."""
        self.initialize()
        with self._lock, self._db:
            self._db.execute("UPDATE ai_charges SET actual=0,settled_at=? "
                             "WHERE id=? AND actual IS NULL",
                             (self.clock().astimezone(UTC).isoformat(), token))
            row = self._db.execute("SELECT actual FROM ai_charges WHERE id=?", (token,)).fetchone()
            if row and row[0] == 0:
                self._db.execute("INSERT OR IGNORE INTO ai_unsubmitted VALUES (?)", (token,))

    def settle(self, token: str, actual_cost_usd: float | None):
        # Unknown/timeout/crash retains the maximum liability indefinitely,
        # including across restarts; it is never silently refunded.
        if actual_cost_usd is None:
            return
        actual = _money(actual_cost_usd)
        self.initialize()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT reserved,actual FROM ai_charges WHERE id=?", (token,)
                ).fetchone()
                if not row:
                    raise ValueError("unknown cost reservation")
                if row[1] is not None:
                    # First verified settlement is immutable, including duplicate callbacks.
                    self._db.commit()
                    return
                self._db.execute("UPDATE ai_charges SET actual=?,settled_at=? WHERE id=?",
                                 (actual, self.clock().astimezone(UTC).isoformat(), token))
                if actual > row[0]:
                    detail = "Cost exceeded the verified reservation; manual review required."
                    for provider in PROVIDERS:
                        self._db.execute("INSERT OR REPLACE INTO ai_blocks VALUES (?,?)",
                                         (provider, detail))
                        self._alert(f"blocked:{provider}:{detail}", f"{provider}: {detail}")
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def seed(self, identity: str, *, provider: str, cost_usd: float, at: datetime):
        """Idempotently import prior aggregate spend without rewriting old records."""
        self.initialize()
        local = at.astimezone(self.zone)
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO ai_charges VALUES (?,?,?,?,?,?,?,?,?)",
                             (identity, provider, "prior-usage", local.strftime("%Y-%m-%d"),
                              local.strftime("%Y-%m"), 0, _money(cost_usd),
                              at.isoformat(), at.isoformat()))

    def block(self, provider: str, reason: str):
        self.initialize()
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO ai_blocks VALUES (?,?)", (provider, reason))
            self._alert(f"blocked:{provider}:{reason}", f"{provider}: {reason}")

    def reset_provider(self, provider: str):
        """Explicit operator recovery only; never adjusts charged usage."""
        self.initialize()
        with self._lock, self._db:
            self._db.execute("DELETE FROM ai_blocks WHERE provider=?", (provider,))

    def resume_today(self, extra_budget_usd: float = .20):
        """Explicit operator recovery; never refunds spend or lifts monthly/auth caps."""
        if self.weekly:
            raise ValueError("daily recovery is unavailable under weekly policy")
        extra = _money(extra_budget_usd)
        if extra <= 0 or extra > self.daily:
            raise ValueError("extra allowance must be positive and at most the daily ceiling")
        self.initialize()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                _, day, _, totals, _, _, _ = self._limits()
                remaining = self.monthly - totals[0] - totals[1]
                if remaining <= 0:
                    raise BudgetPaused(
                        "Monthly budget exhausted; daily recovery cannot override it."
                    )
                limit = totals[2] + totals[3] + min(extra, remaining)
                self._db.execute("INSERT OR REPLACE INTO ai_daily_overrides VALUES (?,?)",
                                 (day, limit))
                self._db.execute("DELETE FROM ai_pauses WHERE day=?", (day,))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    async def status_async(self) -> AIBudgetStatus:
        """Read accounting without blocking the API loop on SQLite or its lock.

        The connection allows cross-thread access; status retains the same lock
        as admission and settlement, so no accounting snapshot is cached.
        """
        if self._stopping:
            raise RuntimeError("AI budget guard is stopping")
        read = asyncio.create_task(asyncio.to_thread(self.status))
        self._status_reads.add(read)

        def completed(task):
            self._status_reads.discard(task)
            # A cancelled caller may no longer observe a failed worker. Retrieve
            # its exception while shield still propagates it to active callers.
            if not task.cancelled():
                task.exception()

        read.add_done_callback(completed)
        # Cancelling an API request cannot stop a SQLite operation already in
        # its worker thread. Keep that operation tracked until shutdown drains it.
        return await asyncio.shield(read)

    def status(self) -> AIBudgetStatus:
        """Consistent read snapshot, including concurrent operator connections."""
        self.initialize()
        with self._lock:
            owns_transaction = not self._db.in_transaction
            if owns_transaction:
                self._db.execute("BEGIN")
            try:
                result = self._status_snapshot()
                if owns_transaction:
                    self._db.commit()
                return result
            except BaseException:
                if owns_transaction:
                    self._db.rollback()
                raise

    def _status_snapshot(self) -> AIBudgetStatus:
        self.initialize()
        with self._lock:
            now, day, _, totals, daily, reason, reset = self._limits()
            blocks = dict(self._db.execute("SELECT provider,reason FROM ai_blocks"))
            pauses = dict(self._db.execute(
                "SELECT provider,reason FROM ai_pauses WHERE day=?", (day,)
            )) if self.spending_limits_enabled else {}
            for provider, pause_reason in list(pauses.items()):
                if self._obsolete_cost_pause(provider, pause_reason, day, totals, daily):
                    del pauses[provider]
                    continue
                if pause_reason != (
                    f"Daily {provider} request limit reached; interpretation is paused."
                ):
                    continue
                count = self._db.execute(
                    "SELECT COUNT(*) FROM ai_charges WHERE day=? AND provider=? "
                    "AND model!='prior-usage' AND id NOT IN (SELECT id FROM ai_unsubmitted)",
                    (day, provider),
                ).fetchone()[0]
                cap = self._request_cap(provider)
                if cap == 0 or count < cap:
                    del pauses[provider]
            if pauses and not reset:
                reset = (now + timedelta(days=1)).replace(hour=0, minute=0,
                                                        second=0, microsecond=0)
            if not reason and all(p in blocks or p in pauses for p in PROVIDERS):
                reason = ALL_PAUSED
            delivery = self._db.execute(
                "SELECT delivered,error FROM ai_alerts "
                + ("WHERE key LIKE 'blocked:%' " if not self.spending_limits_enabled else "")
                + "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            week_spent, week_reserved, week_reset, epoch = self._weekly_totals(now)
            if self.weekly and any("Weekly Omarkets" in r for r in pauses.values()):
                reset = week_reset
            return AIBudgetStatus(
                budget_period="weekly" if self.weekly else "monthly",
                budget_timezone=self.zone.key, weekly_limit_usd=self.weekly / 1e6,
                week_spent_usd=week_spent / 1e6, week_reserved_usd=week_reserved / 1e6,
                week_remaining_usd=max(0, self.weekly - week_spent - week_reserved) / 1e6,
                week_resets_at=week_reset, allowance_started_at=epoch,
                generated_at=now.astimezone(UTC),
                monthly_limit_usd=0 if self.weekly else self.monthly / 1e6,
                month_spent_usd=totals[0] / 1e6, month_reserved_usd=totals[1] / 1e6,
                month_remaining_usd=(0 if self.weekly else
                                     max(0, self.monthly - totals[0] - totals[1]) / 1e6),
                daily_limit_usd=daily / 1e6, day_spent_usd=totals[2] / 1e6,
                day_reserved_usd=totals[3] / 1e6, paused=reason is not None,
                reason=reason, resumes_at=reset.astimezone(UTC) if reset else None,
                providers=[ProviderBudgetStatus(provider=p, blocked=p in blocks or p in pauses,
                                                 reason=blocks.get(p) or pauses.get(p))
                           for p in PROVIDERS],
                notification_delivery=("none" if not delivery else "delivered" if delivery[0]
                                       else "failed" if delivery[1] else "pending"),
                spending_limits_enabled=self.spending_limits_enabled,
            )

    async def deliver_alerts(self):
        """Durable dedupe and retry; failed desktop delivery stays visible in API."""
        self.initialize()
        with self._lock, self._db:
            status = self.status()
            # Each provider block/pause already alerts; the summary would repeat it.
            if status.reason and status.reason != ALL_PAUSED:
                self._alert(f"pause:{self._period()[1]}:{status.reason}", status.reason)
            week = None
            if self.weekly:
                week = (status.week_resets_at - timedelta(days=7)).astimezone(
                    self.zone).date().isoformat()
                # Settled spend only: an in-flight reservation is not money spent yet.
                if status.week_spent_usd * 1e6 * 5 >= self.weekly * 4:
                    self._alert(f"weekly-80:{week}", WEEKLY_WARNING)
            cutoff = (self.clock() - timedelta(minutes=5)).isoformat()
            rows = self._db.execute(
                "SELECT key,reason FROM ai_alerts WHERE delivered=0 AND "
                + ("key LIKE 'blocked:%' AND " if not self.spending_limits_enabled else "")
                +
                "(attempted_at IS NULL OR attempted_at<?)", (cutoff,)
            ).fetchall()
        for key, reason in rows:
            if self.weekly and reason.startswith(("Monthly Omarkets", "Daily paced Omarkets")):
                continue  # Retain obsolete alerts as audit evidence, without redelivery.
            if self.weekly and (key.startswith("weekly-80:")
                                or reason.startswith("Weekly Omarkets")):
                resets = status.week_resets_at.astimezone(self.zone).strftime("%a %d %b %H:%M")
                spent = (f"Spent ${status.week_spent_usd:.2f} of "
                         f"${status.weekly_limit_usd:.2f} this week")
                if key.startswith("weekly-80:"):
                    summary, urgency = "Omarkets AI budget 80% used", "normal"
                    body = (f"{spent}; ${status.week_remaining_usd:.2f} left. New desk notes "
                            f"pause when it runs out. The budget resets {resets} your time.")
                else:
                    urgency = "critical"
                    # One "used up" alert per week, however many pauses repeat it.
                    used_up = f"weekly-100:{week}"
                    with self._lock, self._db:
                        done = self._db.execute(
                            "SELECT 1 FROM ai_alerts WHERE key=? AND delivered=1",
                            (used_up,)).fetchone()
                        if done:
                            self._db.execute(
                                "UPDATE ai_alerts SET delivered=1,attempted_at=?,error=NULL "
                                "WHERE key=?", (self.clock().isoformat(), key))
                            continue
                    summary = "Omarkets AI budget used up"
                    body = (f"{spent}, not enough for another note. New desk notes are "
                            f"paused until {resets} your time. Prices, the calendar and "
                            "existing notes keep working.")
                delivered, error = (
                    await self.notifier.deliver_text(summary, body, urgency=urgency,
                                                     icon=ALERT_ICON)
                    if self.notifier else (False, "desktop notifier unavailable"))
                with self._lock, self._db:
                    self._db.execute("UPDATE ai_alerts SET delivered=?,attempted_at=?,error=? "
                                     "WHERE key=?",
                                     (int(delivered), self.clock().isoformat(), error, key))
                    if delivered and summary == "Omarkets AI budget used up":
                        self._db.execute(
                            "INSERT OR REPLACE INTO ai_alerts(key,reason,delivered,attempted_at) "
                            "VALUES (?,?,1,?)", (used_up, reason, self.clock().isoformat()))
                continue
            body = (f"{reason} Monthly limit: ${status.monthly_limit_usd:.2f}; "
                    f"remaining: ${status.month_remaining_usd:.2f}. "
                    "Quotes, calendar, stored history and the API continue running. ")
            if self.weekly:
                body = (f"{reason} Weekly limit: ${status.weekly_limit_usd:.2f}; "
                        f"remaining: ${status.week_remaining_usd:.2f}. ")
            if not self.spending_limits_enabled:
                body = (f"{reason} Omarkets spending limits are disabled; "
                        "this is a provider or request-integrity block, not a budget pause. "
                        "Quotes, calendar, stored history and the API continue running. ")
            body += (f"Budget reset: {status.resumes_at.astimezone(self.zone).isoformat()}."
                     if status.resumes_at else "")
            body += " Credit/authentication blocks require explicit recovery, not a budget reset."
            delivered, error = (
                await self.notifier.deliver_text("Omarkets paid AI paused", body,
                                                 urgency="critical", icon=ALERT_ICON)
                if self.notifier else (False, "desktop notifier unavailable"))
            with self._lock, self._db:
                self._db.execute("UPDATE ai_alerts SET delivered=?,attempted_at=?,error=? "
                                 "WHERE key=?",
                                 (int(delivered), self.clock().isoformat(), error, key))

    async def start(self):
        self._stopping = False
        self.initialize()
        await self.deliver_alerts()
        async def monitor():
            while True:
                await asyncio.sleep(10)
                await self.deliver_alerts()
        self._task = asyncio.create_task(monitor(), name="omarkets-ai-cost-alerts")

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._status_reads:
            await asyncio.shield(asyncio.gather(*self._status_reads, return_exceptions=True))
        await asyncio.to_thread(self._close)

    def _close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect AI spending or explicitly recover a provider"
    )
    parser.add_argument("command",
                        choices=["status", "reset-provider", "resume-today", "activate-weekly"])
    parser.add_argument("--provider", choices=list(PROVIDERS))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--reset-id")
    parser.add_argument("--reason")
    args = parser.parse_args()
    from core.config.settings import Settings
    settings = Settings()
    guard = AICostGuard(args.db or settings.resolved_ai_budget_db_path,
                        monthly_limit_usd=settings.ai_monthly_budget_usd,
                        daily_limit_usd=settings.ai_daily_budget_usd,
                        weekly_limit_usd=settings.ai_weekly_budget_usd,
                        timezone=settings.ai_budget_timezone,
                        openrouter_daily_request_limit=settings.ai_openrouter_daily_request_limit,
                        openrouter_background_request_limit=settings.ai_openrouter_background_request_limit)
    if args.command == "activate-weekly":
        if not args.reset_id or not args.reason:
            parser.error("activate-weekly requires --reset-id and --reason")
        guard.activate_weekly_allowance(args.reset_id, reason=args.reason)
    if args.command == "reset-provider":
        if not args.provider:
            parser.error("reset-provider requires --provider, after credit/authentication repair")
        guard.reset_provider(args.provider)
    elif args.command == "resume-today":
        guard.resume_today()
    print(guard.status().model_dump_json(indent=2))
