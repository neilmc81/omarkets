import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from core.notifications.notifier import FakeNotifier
from core.services.ai_budget import AICostGuard, BudgetPaused


def guard(tmp_path, **kwargs):
    return AICostGuard(tmp_path / "budget.sqlite3",
                       clock=lambda: datetime(2026, 10, 31, 1, tzinfo=UTC), **kwargs)


@pytest.mark.asyncio
async def test_status_read_does_not_block_loop_on_accounting_lock(tmp_path):
    g = guard(tmp_path)
    g.initialize()
    held = threading.Event()
    release = threading.Event()

    def hold_accounting_lock():
        with g._lock:
            held.set()
            release.wait(timeout=1)

    worker = threading.Thread(target=hold_accounting_lock)
    worker.start()
    assert held.wait(timeout=1)
    task = asyncio.create_task(g.status_async())
    try:
        await asyncio.sleep(.02)
        assert not task.done(), "accounting lock wait blocked the event loop"
    finally:
        release.set()
        worker.join(timeout=1)
    status = await task
    assert status == g.status()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_reader", [False, True])
async def test_shutdown_drains_paused_status_reader_before_closing(
    tmp_path, monkeypatch, cancel_reader
):
    g = guard(tmp_path)
    g.initialize()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    original_status = g.status
    worker_results = []

    def paused_status():
        with g._lock:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2)
            worker_results.append(original_status())
            return worker_results[-1]

    monkeypatch.setattr(g, "status", paused_status)
    reader = asyncio.create_task(g.status_async())
    await asyncio.wait_for(entered.wait(), timeout=2)
    if cancel_reader:
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
    stopping = asyncio.create_task(g.stop())
    try:
        await asyncio.sleep(.02)
        assert not stopping.done()
        assert g._db is not None
        with pytest.raises(RuntimeError, match="stopping"):
            await g.status_async()
    finally:
        release.set()
    await stopping
    if not cancel_reader:
        assert await reader == worker_results[0]
    assert len(worker_results) == 1
    assert g._db is None
    assert not g._status_reads


def test_reserves_before_work_and_retains_unknown_on_restart(tmp_path):
    g = guard(tmp_path)
    token = g.reserve("openrouter", "model", .1)
    g.settle(token, None)
    other = guard(tmp_path)
    assert other.status().month_reserved_usd == .1
    with pytest.raises(BudgetPaused):
        other.reserve("openrouter", "model", .101)
    assert other.status().providers[0].blocked
    with pytest.raises(BudgetPaused):
        other.reserve("openrouter", "model", .001)


def test_explicit_daily_resume_preserves_spend_and_monthly_cap(tmp_path):
    g = guard(tmp_path)
    g.seed("old", provider="openrouter", cost_usd=1.94, at=g.clock())
    g.resume_today()
    other = guard(tmp_path)
    assert not other.status().paused
    assert other.status().month_spent_usd == 1.94
    other.reserve("openrouter", "model", .19)
    with pytest.raises(BudgetPaused):
        other.reserve("openrouter", "model", .02)
    g.seed("rest", provider="openrouter", cost_usd=3.06, at=g.clock())
    with pytest.raises(BudgetPaused, match="Monthly"):
        g.resume_today()


def test_daily_resume_expires_next_local_day(tmp_path):
    now = datetime(2026, 10, 1, 10, tzinfo=UTC)
    g = AICostGuard(tmp_path / "budget.sqlite3", clock=lambda: now)
    g.seed("old", provider="openrouter", cost_usd=1, at=now)
    g.resume_today()
    assert g.status().daily_limit_usd == 1.2
    now += timedelta(days=1)
    assert g.status().daily_limit_usd < .20


def test_unsubmitted_setup_fault_does_not_consume_request_slots(tmp_path):
    g = guard(tmp_path)
    for _ in range(15):
        token = g.reserve("openrouter", "model", .001)
        g.cancel_before_submission(token)
    assert g.status().month_spent_usd == 0
    assert g.status().month_reserved_usd == 0
    assert g.reserve("openrouter", "model", .001)


def test_atomic_concurrent_admission(tmp_path):
    g = guard(tmp_path)
    def reserve(_):
        try:
            return g.reserve("openrouter", "model", .03)
        except BudgetPaused:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(reserve, range(20)))
    assert sum(t is not None for t in tokens) == 6
    assert g.status().month_reserved_usd == .18


def test_actual_settlement_releases_only_verified_unused_liability(tmp_path):
    g = guard(tmp_path)
    token = g.reserve("openrouter", "model", .1)
    g.settle(token, .01)
    g.settle(token, 0)
    assert g.status().month_spent_usd == .01
    assert g.status().month_reserved_usd == 0


def test_account_breaker_survives_restart_and_reset_does_not_refund(tmp_path):
    g = guard(tmp_path)
    g.seed("existing", provider="openrouter", cost_usd=1, at=g.clock())
    g.block("openrouter", "Insufficient credits; explicit recovery required.")
    other = guard(tmp_path)
    with pytest.raises(BudgetPaused, match="credits"):
        other.reserve("openrouter", "model", .001)
    other.reset_provider("openrouter")
    assert other.status().month_spent_usd == 1
    assert not other.status().providers[0].blocked


def test_seed_idempotent_and_existing_month_spend_counts(tmp_path):
    g = guard(tmp_path)
    for _ in range(2):
        g.seed("existing", provider="openrouter", cost_usd=5, at=g.clock())
    assert g.status().month_spent_usd == 5
    assert g.status().paused
    assert "Monthly" in g.status().reason
    with pytest.raises(BudgetPaused):
        g.reserve("openrouter", "model", .001)


def test_daily_reset_not_monthly_refund(tmp_path):
    now = [datetime(2026, 10, 29, 1, tzinfo=UTC)]
    g = AICostGuard(tmp_path / "budget.sqlite3", clock=lambda: now[0])
    g.seed("today", provider="openrouter", cost_usd=.2, at=now[0])
    assert g.status().paused
    now[0] += timedelta(days=1)
    assert not g.status().paused
    assert g.status().month_spent_usd == .2


def test_month_reset_and_timezone_boundary(tmp_path):
    now = [datetime(2026, 10, 31, 23, 59, tzinfo=UTC)]
    g = AICostGuard(tmp_path / "budget.sqlite3", timezone="Europe/London", clock=lambda: now[0])
    g.seed("month", provider="openrouter", cost_usd=5, at=now[0])
    assert g.status().paused
    now[0] += timedelta(minutes=1)
    assert not g.status().paused
    assert g.status().month_spent_usd == 0


@pytest.mark.asyncio
async def test_clear_notification_delivered_once_even_after_restart(tmp_path):
    notifier = FakeNotifier()
    g = guard(tmp_path, notifier=notifier)
    g.seed("exhausted", provider="openrouter", cost_usd=5, at=g.clock())
    await g.deliver_alerts()
    await guard(tmp_path, notifier=notifier).deliver_alerts()
    assert len(notifier.delivered) == 1
    summary, body, urgency = notifier.delivered[0]
    assert "paused" in summary and urgency == "critical"
    assert "$5.00" in body and "continue running" in body
    assert "Budget reset" in body
    assert g.status().notification_delivery == "delivered"


@pytest.mark.asyncio
async def test_failed_notification_is_visible_and_retried(tmp_path):
    now = [datetime(2026, 10, 31, 1, tzinfo=UTC)]
    notifier = FakeNotifier()
    g = AICostGuard(tmp_path / "budget.sqlite3", clock=lambda: now[0])
    g.block("openrouter", "Insufficient credits")
    await g.deliver_alerts()
    assert g.status().notification_delivery == "failed"
    now[0] += timedelta(minutes=6)
    g.notifier = notifier
    await g.deliver_alerts()
    assert len(notifier.delivered) == 1


def test_request_count_includes_settled_zero_cost_and_manual_work(tmp_path):
    g = guard(tmp_path)
    for _ in range(48):
        g.settle(g.reserve("openrouter", "model", .001, priority=True), 0)
    with pytest.raises(BudgetPaused, match="request limit"):
        g.reserve("openrouter", "model", .001, priority=True)


def test_background_cannot_use_reserved_release_slots(tmp_path):
    g = guard(tmp_path)
    for _ in range(32):
        g.settle(g.reserve("openrouter", "model", .001), 0)
    with pytest.raises(BudgetPaused, match="held for releases"):
        g.reserve("openrouter", "model", .001)
    assert not g.status().providers[0].blocked
    assert g.reserve("openrouter", "model", .001, priority=True)


def test_previous_lower_request_cap_does_not_keep_provider_paused(tmp_path):
    g = guard(tmp_path)
    g.initialize()
    with g._db:
        g._db.execute("INSERT INTO ai_pauses VALUES (?,?,?)", (
            "2026-10-31", "openrouter",
            "Daily openrouter request limit reached; interpretation is paused.",
        ))
    assert not g.status().providers[0].blocked
    assert g.reserve("openrouter", "model", .001)


def test_reservation_overrun_blocks_all_paid_providers(tmp_path):
    g = guard(tmp_path)
    g.settle(g.reserve("openrouter", "model", .001), .002)
    assert all(p.blocked for p in g.status().providers)


def test_zero_limits_ignore_old_budget_pauses_and_request_counts_but_keep_audit(tmp_path):
    limited = guard(tmp_path)
    limited.seed("old", provider="openrouter", cost_usd=10, at=limited.clock())
    with pytest.raises(BudgetPaused):
        limited.reserve("openrouter", "model", .01)
    unlimited = guard(tmp_path, monthly_limit_usd=0, daily_limit_usd=0)
    for provider, count in (("openrouter", 265),):
        for _ in range(count):
            unlimited.reserve(provider, "model", .001)
    status = unlimited.status()
    assert status.spending_limits_enabled is False
    assert status.monthly_limit_usd == status.daily_limit_usd == 0
    assert status.month_spent_usd == 10
    assert status.month_reserved_usd == .265
    assert not status.paused
    assert status.resumes_at is None
    assert all(not provider.blocked for provider in status.providers)


def test_zero_limits_do_not_remove_provider_blocks_or_request_integrity(tmp_path):
    unlimited = guard(tmp_path, monthly_limit_usd=0, daily_limit_usd=0)
    with pytest.raises(ValueError, match="maximum"):
        unlimited.reserve("openrouter", "model", 0)
    unlimited.block("openrouter", "Provider authentication failed")
    with pytest.raises(BudgetPaused, match="authentication"):
        unlimited.reserve("openrouter", "model", .001)
    assert unlimited.status().providers[0].blocked


@pytest.mark.parametrize("monthly,daily", [(0, .20), (5, 0)])
def test_single_zero_disables_only_that_cost_limit(tmp_path, monthly, daily):
    g = guard(tmp_path, monthly_limit_usd=monthly, daily_limit_usd=daily)
    assert g.status().spending_limits_enabled
    assert g.reserve("openrouter", "model", .1)
    if daily:
        with pytest.raises(BudgetPaused, match="Daily"):
            g.reserve("openrouter", "model", .11)
    else:
        assert g.reserve("openrouter", "model", 4)
        with pytest.raises(BudgetPaused, match="Monthly"):
            g.reserve("openrouter", "model", 1)


@pytest.mark.asyncio
async def test_unlimited_skips_old_budget_notifications_but_delivers_provider_blocks(tmp_path):
    notifier = FakeNotifier()
    limited = guard(tmp_path)
    limited.seed("old", provider="openrouter", cost_usd=10, at=limited.clock())
    with pytest.raises(BudgetPaused):
        limited.reserve("openrouter", "model", .01)
    unlimited = guard(tmp_path, monthly_limit_usd=0, daily_limit_usd=0, notifier=notifier)
    await unlimited.deliver_alerts()
    assert notifier.delivered == []
    assert unlimited.status().notification_delivery == "none"
    unlimited.block("openrouter", "Provider credits unavailable")
    await unlimited.deliver_alerts()
    assert len(notifier.delivered) == 1
    assert "spending limits are disabled" in notifier.delivered[0][1]


@pytest.mark.parametrize("monthly,daily", [(-1, 0), (0, -1)])
def test_negative_limits_remain_invalid(tmp_path, monthly, daily):
    from core.config.settings import Settings

    with pytest.raises(ValueError):
        guard(tmp_path, monthly_limit_usd=monthly, daily_limit_usd=daily)
    with pytest.raises(ValueError):
        Settings(_env_file=None, ai_monthly_budget_usd=monthly, ai_daily_budget_usd=daily)


def test_settings_accept_zero_limits():
    from core.config.settings import Settings

    settings = Settings(_env_file=None, ai_monthly_budget_usd=0, ai_daily_budget_usd=0)
    assert settings.ai_monthly_budget_usd == settings.ai_daily_budget_usd == 0


def test_verified_settlement_unblocks_obsolete_cost_pause_but_rechecks_new_maximum(tmp_path):
    g = guard(tmp_path, daily_limit_usd=.10, monthly_limit_usd=5)
    token = g.reserve('openrouter', 'fixture', .06, priority=True)
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'fixture', .06, priority=True)
    assert next(p for p in g.status().providers if p.provider == 'openrouter').blocked
    g.settle(token, .01)
    assert not next(p for p in g.status().providers if p.provider == 'openrouter').blocked
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'fixture', .10, priority=True)
    assert g.status().month_spent_usd == .01
    assert g.status().month_reserved_usd == 0
    g.reserve('openrouter', 'fixture', .06, priority=True)


def test_unknown_liability_and_auth_block_are_not_cleared_by_cost_recovery(tmp_path):
    g = guard(tmp_path, daily_limit_usd=.10, monthly_limit_usd=5)
    token = g.reserve('openrouter', 'fixture', .06, priority=True)
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'fixture', .06, priority=True)
    g.settle(token, None)
    assert next(p for p in g.status().providers if p.provider == 'openrouter').blocked
    g.block('openrouter', 'provider returned HTTP 401')
    g.settle(token, .01)
    with pytest.raises(BudgetPaused, match='401'):
        g.reserve('openrouter', 'fixture', .06, priority=True)


def test_weekly_activation_retains_liability_history_and_provider_blocks(tmp_path):
    now = [datetime(2026, 10, 8, 3, tzinfo=UTC)]
    path = tmp_path / 'weekly.sqlite3'
    g = AICostGuard(path, weekly_limit_usd=10, clock=lambda: now[0])
    g.seed('history', provider='openrouter', cost_usd=8, at=now[0] - timedelta(hours=1))
    token = g.reserve('openrouter', 'model', 1)
    g.block('openrouter', 'authentication rejected')
    g.activate_weekly_allowance('operator-20261008', reason='user authorized fresh $10 weekly')
    status = g.status()
    assert status.week_spent_usd == 0
    assert status.week_reserved_usd == 1
    assert status.week_remaining_usd == 9
    assert status.providers[0].blocked
    now[0] += timedelta(seconds=1)
    g.settle(token, .4)
    assert g.status().week_spent_usd == .4
    g._close()
    g = AICostGuard(path, weekly_limit_usd=10, clock=lambda: now[0])
    g.activate_weekly_allowance('operator-20261008', reason='same command retried')
    assert g.status().week_spent_usd == .4
    assert g._db.execute('SELECT COUNT(*) FROM ai_charges').fetchone()[0] == 2


def test_weekly_monday_boundary_and_late_settlement(tmp_path):
    now = [datetime(2026, 10, 11, 22, 59, 59, tzinfo=UTC)]
    g = AICostGuard(
        tmp_path / 'w.sqlite3', weekly_limit_usd=10, timezone="Europe/London",
        clock=lambda: now[0],
    )
    g.seed('last-week', provider='openrouter', cost_usd=8, at=now[0])
    token = g.reserve('openrouter', 'model', 2)
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'model', .01)
    now[0] += timedelta(seconds=1)  # Monday midnight London (BST)
    assert g.status().week_spent_usd == 0
    assert g.status().week_reserved_usd == 2
    g.reserve('openrouter', 'model', 7)
    g.settle(token, 1.5)  # Late billing counts in the settlement week.
    assert g.status().week_spent_usd == 1.5
    assert g.status().week_remaining_usd == 1.5


@pytest.mark.parametrize('at,reset', [
    (datetime(2026, 3, 2, 5, tzinfo=UTC), datetime(2026, 3, 9, 4, tzinfo=UTC)),
    (datetime(2026, 10, 26, 4, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC)),
])
def test_weekly_reset_uses_local_monday_across_dst(tmp_path, at, reset):
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=10,
                    timezone='America/New_York', clock=lambda: at)
    assert g.status().week_resets_at == reset


def test_weekly_ignores_month_day_pacing_and_default_openrouter_caps(tmp_path):
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=10,
                    clock=lambda: datetime(2026, 10, 8, tzinfo=UTC))
    for _ in range(60):
        token = g.reserve('openrouter', 'model', .1)
        g.settle(token, .1)
    assert g.status().week_spent_usd == 6
    assert not g.status().paused
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'model', 4.01)


def test_weekly_atomic_admission_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'w.sqlite3'
    first = AICostGuard(path, weekly_limit_usd=10)
    second = AICostGuard(path, weekly_limit_usd=10)
    first.initialize()
    second.initialize()
    def attempt(g):
        try:
            return g.reserve('openrouter', 'model', 6)
        except BudgetPaused:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(attempt, (first, second)))
    assert sum(t is not None for t in tokens) == 1
    assert first.status().week_reserved_usd == 6


def test_reset_baseline_excludes_settled_records_with_equal_timestamp(tmp_path):
    now = datetime(2026, 10, 8, tzinfo=UTC)
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=10, clock=lambda: now)
    g.seed('settled-before-reset', provider='openrouter', cost_usd=9, at=now)
    g.activate_weekly_allowance('reset', reason='authorized reset')
    assert g.status().week_spent_usd == 0
    token = g.reserve('openrouter', 'model', 2)
    g.settle(token, 1)
    assert g.status().week_spent_usd == 1
    g.settle(token, 100)  # First settlement remains immutable and creates no false block.
    assert g.status().week_spent_usd == 1
    assert not any(p.blocked for p in g.status().providers)


def test_weekly_preserves_integrity_block_on_reset(tmp_path):
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=10)
    token = g.reserve('openrouter', 'model', 1)
    g.settle(token, 2)
    g.activate_weekly_allowance('reset', reason='authorized reset')
    assert g.status().week_spent_usd == 0
    with pytest.raises(BudgetPaused, match='manual review'):
        g.reserve('openrouter', 'model', 1)


def test_weekly_request_cap_remains_configurable(tmp_path):
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=10,
                    openrouter_daily_request_limit=1)
    token = g.reserve('openrouter', 'model', .01)
    g.settle(token, .01)
    with pytest.raises(BudgetPaused, match='request limit'):
        g.reserve('openrouter', 'model', .01)


@pytest.mark.asyncio
async def test_weekly_80_percent_warning_once_per_week(tmp_path):
    now = [datetime(2026, 10, 20, 3, tzinfo=UTC)]  # Tuesday, Ho Chi Minh
    notifier = FakeNotifier()
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=2.5,
                    clock=lambda: now[0], notifier=notifier)
    g.seed('a', provider='openrouter', cost_usd=1.99, at=now[0])
    await g.deliver_alerts()
    assert notifier.delivered == []
    token = g.reserve('openrouter', 'model', .3)  # In flight: not spent yet.
    await g.deliver_alerts()
    assert notifier.delivered == []
    g.settle(token, .01)
    await g.deliver_alerts()
    await AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=2.5, clock=lambda: now[0],
                      notifier=notifier).deliver_alerts()
    assert len(notifier.delivered) == 1
    summary, body, urgency = notifier.delivered[0]
    assert summary == 'Omarkets AI budget 80% used' and urgency == 'normal'
    assert '$2.00 of $2.50' in body and '$0.50 left' in body
    assert 'Mon 26 Oct 00:00' in body
    now[0] += timedelta(days=7)
    g.seed('b', provider='openrouter', cost_usd=2.1, at=now[0])
    await g.deliver_alerts()
    assert len(notifier.delivered) == 2


@pytest.mark.asyncio
async def test_weekly_used_up_alert_once_per_week(tmp_path):
    now = [datetime(2026, 10, 20, 3, tzinfo=UTC)]
    notifier = FakeNotifier()
    g = AICostGuard(tmp_path / 'w.sqlite3', weekly_limit_usd=2.5,
                    clock=lambda: now[0], notifier=notifier)
    g.seed('a', provider='openrouter', cost_usd=2.3, at=now[0])
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'model', .3)
    await g.deliver_alerts()
    summaries = [d[0] for d in notifier.delivered]
    assert summaries.count('Omarkets AI budget used up') == 1
    assert summaries.count('Omarkets AI budget 80% used') == 1
    used_up = next(d for d in notifier.delivered if d[0] == 'Omarkets AI budget used up')
    assert used_up[2] == 'critical' and 'paused until Mon 26 Oct 00:00' in used_up[1]
    now[0] += timedelta(days=1)  # Another day's pause in the same week.
    with pytest.raises(BudgetPaused):
        g.reserve('openrouter', 'model', .3)
    await g.deliver_alerts()
    assert len(notifier.delivered) == 2
    assert g.status().notification_delivery == 'delivered'
