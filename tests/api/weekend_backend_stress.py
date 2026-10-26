"""Bounded offline full-core replay with independent loopback HTTP readers.

Run: .venv/bin/python tests/api/weekend_backend_stress.py --source RELEASE --output JSON
Only NEW temporary databases are used. No real providers or paid model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import socket
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


def stats(values):
    ordered = sorted(values)
    return (
        {
            "count": len(values),
            "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
            "max_ms": round(max(ordered), 2),
        }
        if ordered
        else {"count": 0}
    )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash(source):
    return hashlib.sha256(
        b"".join(
            path.relative_to(source).as_posix().encode() + path.read_bytes()
            for path in sorted((source / "core").rglob("*.py"))
        )
    ).hexdigest()


async def run(args):
    source = await asyncio.to_thread(Path(args.source).resolve)
    sys.path.insert(0, str(source))
    import core.main as main
    import core.services.economics as economics_module

    assert await asyncio.to_thread(Path(main.__file__).resolve) == source / "core/main.py"
    assert (
        await asyncio.to_thread(Path(economics_module.__file__).resolve)
        == source / "core/services/economics.py"
    )
    import httpx
    import uvicorn
    from core.domain.briefings import Briefing, BriefingType
    from core.services.technical import TechnicalService

    from core.api.app import create_app
    from core.config.settings import Settings
    from core.domain.economics import (
        EconomicEvent,
        MacroObservation,
        MacroProvenance,
        MacroRegistry,
    )
    from core.domain.macro_registry import MACRO_DEFINITIONS
    from core.providers.economic_base import EconomicBatch
    from core.providers.fake import FakeMarketDataProvider
    from core.services.ai_budget import AICostGuard
    from core.services.economics import EconomicService
    from core.services.market_state import MarketStateService
    from core.storage.duckdb_repo import DuckDBObservationRepository
    from core.storage.economics import SQLiteEconomicRepository

    started = time.monotonic()
    base = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)  # Friday 10:00 New York

    def clock():
        return base + timedelta(seconds=time.monotonic() - started)

    metrics = defaultdict(list)
    counters = Counter()
    errors = Counter()
    paths = [
        "/health",
        "/api/v1/shell/status",
        "/api/v1/state",
        "/api/v1/briefings?limit=100",
        "/api/v1/analyses/recent?limit=100",
        "/api/v1/calendar/week",
        "/api/v1/macro/state",
        "/api/v1/technical/NQ",
        "/api/v1/analysts/status",
    ]
    # Deny external networking even if an accidental provider is constructed.
    connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError("external networking forbidden in offline stress harness")
        return connect(sock, address)

    with (
        tempfile.TemporaryDirectory(prefix="omarkets-weekend-stress-") as folder,
        patch("core.domain.models.utc_now", clock),
        patch("socket.socket.connect", local_connect),
    ):
        data = Path(folder)
        settings = Settings(
            _env_file=None,
            data_dir=data,
            data_provider="fake",
            economic_mode="live",
            analyst_enabled=True,
            analyst_mode="demo",
            analyst_model="fake-analyst-v1",
            decision_enabled=False,
            decision_mode="demo",
            news_enabled=False,
            workflow_enabled=True,
            workflow_tick_seconds=5,
            workflow_analysis_wait_seconds=2,
            serve_frontend=False,
        )
        data_root = await asyncio.to_thread(data.resolve)
        analyst_path = await asyncio.to_thread(settings.resolved_analyst_db_path.resolve)
        workflow_path = await asyncio.to_thread(settings.resolved_workflow_db_path.resolve)
        assert analyst_path.is_relative_to(data_root)
        assert workflow_path.is_relative_to(data_root)
        guard = AICostGuard(data / "budget.sqlite", clock=clock)
        provider = FakeMarketDataProvider(clock=clock, update_interval=10000)
        market = MarketStateService(
            provider=provider,
            repository=DuckDBObservationRepository(data / "market.duckdb"),
            clock=clock,
        )
        technical = TechnicalService(market=market, clock=clock)
        economics = EconomicService(
            repository=SQLiteEconomicRepository(data / "economics.sqlite", "live"),
            registry=MacroRegistry(definitions=MACRO_DEFINITIONS),
            mode="live",
            clock=clock,
        )
        analysts = main.build_analyst_service(
            settings, market=market, technical=technical, economics=economics
        )
        assert analysts.provider.name == "fake_analyst"
        analysts.clock = clock
        analysts.provider._cost_guard = (
            guard  # Exercise real budget health-read seam, never reserve.
        )
        workflow = main.build_briefing_service(
            settings,
            market=market,
            technical=technical,
            economics=economics,
            analyst_service=analysts,
        )
        workflow.clock = clock
        app = create_app(
            service=market,
            economic_service=economics,
            technical_service=technical,
            analyst_service=analysts,
            briefing_service=workflow,
            ai_budget=guard,
        )
        # Bind an ephemeral loopback socket ourselves; never use production port 8765.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        assert port != 8765
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="error", lifespan="on")
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(1500):
            if server.started or server_task.done():
                break
            await asyncio.sleep(0.02)
        else:
            server.should_exit = True
            await server_task
            listener.close()
            raise RuntimeError("offline server startup exceeded 30 seconds")
        if server_task.done():
            await server_task
            raise RuntimeError("offline server failed startup")
        try:
            # Warm history: 180 minutes x 18 instruments, matching core retention inputs.
            snapshot = await provider.fetch_snapshot()
            history = [
                row.model_copy(
                    update={
                        "timestamp": base - timedelta(minutes=180 - minute),
                        "received_at": base - timedelta(minutes=180 - minute),
                    }
                )
                for minute in range(180)
                for row in snapshot
            ]
            await market._handle_batch(history)
            await technical.ingest_async(history)
            for definition in MACRO_DEFINITIONS:
                observations = [
                    MacroObservation(
                        id=definition.id,
                        period=f"{2024 + i // 12:04d}-{i % 12 + 1:02d}",
                        value=float(i) / 10,
                        unit=definition.unit,
                        provider=definition.provider,
                        source_series_id=definition.source_series_id,
                        received_at=clock(),
                        metadata=MacroProvenance(
                            source_url="https://api.bls.gov/publicAPI/v2/timeseries/data/"
                        ),
                        notes=["Synthetic offline stress fixture"],
                    )
                    for i in range(30)
                ]
                economics.accept_batch(
                    EconomicBatch(
                        provider=definition.provider,
                        checked_at=clock(),
                        status="healthy",
                        observations=observations,
                    )
                )
            events = [
                EconomicEvent(
                    id=f"offline-calendar-{i}",
                    event_code="CPI",
                    name="Synthetic CPI fixture",
                    category="inflation",
                    importance="critical",
                    scheduled_at=base + timedelta(days=i + 1),
                    unit="percent",
                    frequency="monthly",
                    status="scheduled",
                    provider="bls",
                    source_event_id=f"fixture-{i}",
                    received_at=clock(),
                    mode="live",
                    notes=["Synthetic offline stress fixture"],
                )
                for i in range(250)
            ]
            economics.accept_batch(
                EconomicBatch(provider="bls", checked_at=clock(), status="healthy", events=events)
            )
            for i in range(120):
                for revision in range(1, 4):
                    workflow.repository.store_briefing(
                        Briefing(
                            briefing_id=f"offline-brief-{i}",
                            briefing_type=BriefingType.POST_RELEASE,
                            version=revision,
                            generated_at=clock(),
                            effective_at=clock(),
                            title="Synthetic stress fixture",
                            summary="Synthetic evidence. " * 60,
                        )
                    )
            counters["seed_market_observations"] = len(history)
            counters["seed_macro_observations"] = 30 * len(MACRO_DEFINITIONS)
            counters["seed_calendar_events"] = len(events)
            counters["seed_briefing_versions"] = 360
            measuring = True
            recompute = technical._safe_recompute

            async def measured_recompute():
                t = time.perf_counter()
                try:
                    await recompute()
                finally:
                    metrics["technical_recompute"].append((time.perf_counter() - t) * 1000)
                    counters["technical_recomputations"] += 1

            technical._safe_recompute = measured_recompute
            # Collect only function names and code paths while a heartbeat is late.
            # No locals, frame contents, source payloads or provider bodies are inspected.
            heartbeat = [time.monotonic()]
            loop_thread = threading.get_ident()
            stall_stacks = Counter()
            sampler_stop = threading.Event()

            def sample_stacks():
                while not sampler_stop.wait(0.02):
                    if time.monotonic() - heartbeat[0] <= 0.1:
                        continue
                    frame = sys._current_frames().get(loop_thread)
                    stack = []
                    while frame is not None:
                        path = Path(frame.f_code.co_filename)
                        try:
                            relative = path.relative_to(source)
                        except ValueError:
                            frame = frame.f_back
                            continue
                        stack.append(str(relative) + ":" + frame.f_code.co_name)
                        frame = frame.f_back
                    if stack:
                        stall_stacks[" <- ".join(stack[:6])] += 1

            sampler = threading.Thread(target=sample_stacks, daemon=True)
            sampler.start()

            async def lag_sampler():
                while measuring:
                    deadline = time.monotonic() + 0.02
                    await asyncio.sleep(0.02)
                    metrics["event_loop_lag"].append(max(0, time.monotonic() - deadline) * 1000)
                    heartbeat[0] = time.monotonic()

            async def background_market():
                while measuring:
                    t = time.perf_counter()
                    await market._handle_batch(await provider.fetch_snapshot())
                    metrics["market_batch"].append((time.perf_counter() - t) * 1000)
                    counters["market_batches"] += 1
                    await asyncio.sleep(0.25)

            async def background_economics():
                revision = 0
                while measuring:
                    revision += 1
                    moment = clock()
                    released = EconomicEvent(
                        id=f"offline-release-{revision}",
                        event_code="CPI",
                        name="Synthetic released CPI",
                        category="inflation",
                        importance="critical",
                        scheduled_at=moment - timedelta(minutes=2),
                        released_at=moment - timedelta(minutes=2),
                        actual=0.2,
                        actual_basis="source_release",
                        period="2026-09",
                        unit="percent",
                        frequency="monthly",
                        status="released",
                        provider="bls",
                        source_event_id=f"fixture-release-{revision}",
                        source_url="https://www.bls.gov/cpi/",
                        received_at=moment,
                        mode="live",
                        notes=["Synthetic offline stress fixture"],
                    )
                    definition = MACRO_DEFINITIONS[0]
                    obs = MacroObservation(
                        id=definition.id,
                        period="2026-09",
                        revision=revision,
                        value=None if revision % 4 == 0 else float(revision),
                        unit=definition.unit,
                        provider=definition.provider,
                        source_series_id=definition.source_series_id,
                        received_at=moment,
                        notes=["Synthetic offline stress fixture"],
                    )
                    t = time.perf_counter()
                    economics.accept_batch(
                        EconomicBatch(
                            provider=definition.provider,
                            checked_at=moment,
                            status="healthy",
                            observations=[obs],
                        )
                    )
                    economics.accept_batch(
                        EconomicBatch(
                            provider="bls", checked_at=moment, status="healthy", events=[released]
                        )
                    )
                    metrics["economic_batch"].append((time.perf_counter() - t) * 1000)
                    counters["economic_batches"] += 1
                    await asyncio.sleep(2)

            async def background_workflow():
                while measuring:
                    t = time.perf_counter()
                    await workflow.release_results_refresh()
                    await workflow.tick()
                    metrics["workflow_pass"].append((time.perf_counter() - t) * 1000)
                    counters["workflow_passes"] += 1
                    await asyncio.sleep(1)

            tasks = [
                asyncio.create_task(fn())
                for fn in (
                    lag_sampler,
                    background_market,
                    background_economics,
                    background_workflow,
                )
            ]
            # HTTP workers use their own OS threads: event-loop stalls remain visible in latency.
            measure_started = time.monotonic()

            def reader(index):
                result = []
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
                ) as client:
                    iteration = 0
                    while time.monotonic() < measure_started + args.seconds:
                        path = paths[(index + iteration) % len(paths)]
                        t = time.perf_counter()
                        try:
                            response = client.get(path)
                            result.append(
                                (path, (time.perf_counter() - t) * 1000, str(response.status_code))
                            )
                        except Exception as exc:
                            result.append(
                                (path, (time.perf_counter() - t) * 1000, type(exc).__name__)
                            )
                        iteration += 1
                        time.sleep(0.1)
                return result

            def readers():
                with ThreadPoolExecutor(max_workers=args.readers) as pool:
                    return list(pool.map(reader, range(args.readers)))

            results = await asyncio.to_thread(readers)
            measuring = False
            await asyncio.gather(*tasks)
            sampler_stop.set()
            sampler.join(timeout=1)
            for result in results:
                for path, elapsed, status in result:
                    metrics[path].append(elapsed)
                    if status != "200":
                        errors[f"{path}:{status}"] += 1
            counters["fake_analyst_calls"] = len(analysts.provider.calls)
            analyses = analysts.recent(limit=500)
            briefings = workflow.repository.recent_briefings(limit=500)
            counters["analysis_records"] = len(analyses)
            for status, count in Counter(item.status.value for item in analyses).items():
                counters["analysis_status_" + status] = count
            counters["briefing_identities"] = len(briefings)
            completed_ids = {
                item.analysis_id for item in analyses if item.status.value == "completed"
            }
            counters["briefings_linked_to_completed_analysis"] = sum(
                bool(completed_ids.intersection(item.analysis_refs)) for item in briefings
            )
            counters["budget_charge_rows"] = guard._db.execute(
                "SELECT COUNT(*) FROM ai_charges"
            ).fetchone()[0]
            assert counters["budget_charge_rows"] == 0
            report = {
                "source": str(source),
                "source_core_sha256": await asyncio.to_thread(source_hash, source),
                "harness_sha256": await asyncio.to_thread(file_hash, Path(__file__)),
                "measurement_seconds": round(time.monotonic() - measure_started, 2),
                "reader_threads": args.readers,
                "clock_anchor_utc": base.isoformat(),
                "clock_anchor_new_york": "2026-10-02 10:00 EDT",
                "safety": {
                    "temporary_databases": True,
                    "loopback_only": True,
                    "fake_ai_only": True,
                    "production_port_excluded": True,
                    "external_socket_connect_denied": True,
                },
                "counters": dict(counters),
                "errors": dict(errors),
                "late_heartbeat_stack_samples": dict(stall_stacks.most_common(12)),
                "metrics": {key: stats(value) for key, value in metrics.items()},
                "limitations": [
                    "Bounded synthetic load; does not establish sustained 24-hour acceptance.",
                    "No external HTTP decoding, raw provider bodies, live ingestion "
                    "or paid provider execution.",
                    "Active Friday replay during weekend; production session "
                    "and budget settings unchanged.",
                ],
            }
            await asyncio.to_thread(
                Path(args.output).write_text, json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report, indent=2))
        finally:
            server.should_exit = True
            await server_task
            listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--readers", type=int, default=12)
    arguments = parser.parse_args()
    if not 5 <= arguments.seconds <= 300 or not 1 <= arguments.readers <= 32:
        parser.error("bounded run requires seconds 5..300 and readers 1..32")
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(run(arguments))
