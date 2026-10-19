"""Real HTTP/PostgreSQL load verification in a disposable isolated schema.

Requires TEST_DATABASE_URL, never operates on existing application tables.
Default: 100 monitors, 30-day history, 2-minute warmup, 30 measured minutes.
"""

import argparse
import asyncio
import base64
import json
import math
import os
import platform
import socket
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from aiohttp import web
from sqlalchemy import create_engine, func, select, text

from api_watch.app import create_app
from api_watch.config import Settings
from api_watch.db import utcnow
from api_watch.engine import run_background, run_worker
from api_watch.models import ApiToken, CheckRun, DestinationLease, Incident, Monitor, MonitorConfig, Notification, User
from api_watch.schemas import MonitorCreate
from api_watch.security import digest, encrypt_payload, random_token


def summary(values):
    values = sorted(values)
    if not values:
        return {"samples": 0, "p95_ms": None, "avg_ms": None, "max_ms": None}
    return {"samples": len(values), "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3),
            "median_ms": round(statistics.median(values), 3),
            "avg_ms": round(statistics.fmean(values), 3), "max_ms": round(values[-1], 3)}


async def benchmark(args):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        raise SystemExit("Set TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.")
    schema = "bench_" + uuid4().hex
    administration = create_engine(url)
    with administration.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    runner = None
    api_server = None
    tasks = []
    app = None
    stop = asyncio.Event()
    try:
        target = web.Application()
        counter = 0
        target_attempts = Counter()

        async def health(request):
            nonlocal counter
            counter += 1
            name = request.query.get("monitor", "unknown")
            target_attempts[name] += 1
            await asyncio.sleep(.05 + (counter % 10) * .08)
            # Every monitor experiences exactly one three-failure/two-success
            # confirmation cycle during the sustained run, using real HTTP.
            failed = 3 <= target_attempts[name] <= 5
            return web.json_response({"status": "unavailable" if failed else "ok"}, status=503 if failed else 200)

        target.router.add_get("/health", health)
        runner = web.AppRunner(target)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        fixture_port = site._server.sockets[0].getsockname()[1]
        settings = Settings(app_env="test", database_url=url, database_schema=schema,
            public_base_url="http://apiwatch.invalid", test_allowed_hosts="127.0.0.1", check_concurrency=10,
            encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(), session_secret=random_token(), _env_file=None)
        app = create_app(settings, initialize=True)
        factory = app.state.session_factory
        now = utcnow()
        tokens, mids = [], []
        with factory.begin() as db:
            for owner in range(5):
                user = User(email=f"load-{owner}@example.com", password_hash="not-a-login-account")
                db.add(user)
                db.flush()
                raw = "aw_" + random_token()
                db.add(ApiToken(user_id=user.id, name="Load test", prefix=raw[:10], token_hash=digest(raw),
                    scope="read", expires_at=now + timedelta(days=1)))
                tokens.append(raw)
                owner_mids = []
                for index in range(20):
                    number = owner * 20 + index
                    config = MonitorCreate(name=f"Load target {number}",
                        url=f"http://127.0.0.1:{fixture_port}/health",
                        query_params=[{"name": "monitor", "value": str(number)}]).model_dump()
                    monitor = Monitor(owner_id=user.id, name=config["name"], config_version=2, notifications_enabled=True,
                                      next_due_at=now + timedelta(seconds=number * .6))
                    db.add(monitor)
                    db.flush()
                    owner_mids.append(monitor.id)
                    for version in [1, 2]:
                        db.add(MonitorConfig(monitor_id=monitor.id, version=version, interval_seconds=60,
                            timeout_ms=10000, failure_threshold=3, recovery_threshold=2,
                            encrypted_payload=encrypt_payload(config, settings)))
                mids.append(owner_mids)
        print(f"Created isolated schema {schema}; seeding {args.history_days} days across 100 monitors.", flush=True)
        history_rows = args.history_days * 1440
        # PostgreSQL generates the representative history server-side, in bounded per-monitor transactions.
        if history_rows:
            for group in mids:
                for mid in group:
                    with factory.begin() as db:
                        db.execute(text("""
                            INSERT INTO check_runs
                            (id,monitor_id,config_version,trigger,created_at,scheduled_at,started_at,finished_at,
                             status,outcome,http_status,response_time_ms,elapsed_ms,bytes_received,failed_assertions)
                            SELECT gen_random_uuid()::text,:mid,CASE WHEN n > 1440 THEN 1 ELSE 2 END,'scheduled',
                                   :end - n * interval '1 minute', :end - n * interval '1 minute',
                                   :end - n * interval '1 minute', :end - n * interval '1 minute' + interval '100 milliseconds',
                                   'finished',CASE WHEN n % 97 = 0 THEN 'skipped' WHEN n % 43 = 0 THEN 'interrupted'
                                                   WHEN n % 17 = 0 THEN 'failure' ELSE 'success' END,
                                   CASE WHEN n % 17 = 0 THEN 503 ELSE 200 END,
                                   50 + n % 700,50 + n % 700,15,'[]'::json
                            FROM generate_series(1,:rows) AS n
                        """), {"mid": mid, "end": now - timedelta(minutes=1), "rows": history_rows})
        print(f"Seeded {history_rows * 100:,} rows. Starting actual HTTP API, worker and scheduler.", flush=True)
        api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        api_socket.bind(("127.0.0.1", 0))
        api_socket.listen(128)
        api_socket.setblocking(False)
        api_port = api_socket.getsockname()[1]
        api_server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        tasks.append(asyncio.create_task(api_server.serve(sockets=[api_socket])))
        tasks.append(asyncio.create_task(run_worker(factory, settings, stop)))
        tasks.append(asyncio.create_task(run_background(factory, settings, stop)))
        while not api_server.started:
            if tasks[0].done():
                await tasks[0]
            await asyncio.sleep(.01)
        started = time.monotonic()
        measurement_at = utcnow() + timedelta(seconds=args.warmup)
        duration = args.warmup + args.seconds
        samples = defaultdict(list)
        codes = Counter()
        max_running = max_per_ip = duplicate_active = 0

        async def reader(owner):
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}",
                headers={"Authorization": "Bearer " + tokens[owner]}, timeout=20) as client:
                index = 0
                next_request = time.monotonic()
                while time.monotonic() - started < duration:
                    await asyncio.sleep(max(0, next_request - time.monotonic()))
                    mid = mids[owner][index % 20]
                    to = utcnow()
                    phase = index % 12
                    label = "list"
                    path = "/api/v1/monitors?limit=20"
                    params = None
                    if phase in {1, 5, 9}:
                        label, path = "history", f"/api/v1/monitors/{mid}/checks"
                    elif phase in {3, 7, 11}:
                        label = {3: "stats_24h", 7: "stats_30d", 11: "series_24h"}[phase]
                        path = f"/api/v1/monitors/{mid}/stats" + ("/series" if phase == 11 else "")
                        period = timedelta(days=30, seconds=-30) if phase == 7 else timedelta(hours=24)
                        params = {"from": (to - period).isoformat(), "to": to.isoformat()}
                    tick = time.monotonic()
                    try:
                        result = await client.get(path, params=params)
                        code = result.status_code
                    except httpx.HTTPError:
                        code = "transport_error"
                    if tick - started >= args.warmup:
                        samples[label].append((time.monotonic() - tick) * 1000)
                        codes[str(code)] += 1
                    index += 1
                    next_request = max(next_request + 1, time.monotonic())

        readers = [asyncio.create_task(reader(owner)) for owner in range(5)]
        while any(not task.done() for task in readers):
            elapsed = time.monotonic() - started
            with factory() as db:
                max_running = max(max_running, db.scalar(select(func.count()).select_from(CheckRun)
                    .where(CheckRun.status == "running")))
                per_ip = db.execute(select(DestinationLease.ip, func.count()).group_by(DestinationLease.ip)).all()
                max_per_ip = max(max_per_ip, max((row[1] for row in per_ip), default=0))
                duplicate_active = max(duplicate_active, len(db.execute(select(CheckRun.monitor_id, func.count())
                    .where(CheckRun.status.in_(["queued", "running"]))
                    .group_by(CheckRun.monitor_id).having(func.count() > 1)).all()))
            stats_p95 = summary(samples["stats_30d"])["p95_ms"]
            print(f"Load progress: {elapsed:.0f}/{duration}s; measured API requests={sum(codes.values())}; "
                  f"30-day stats p95={stats_p95}ms", flush=True)
            await asyncio.sleep(min(30, max(1, duration - elapsed)))
        await asyncio.gather(*readers)
        stop.set()
        api_server.should_exit = True
        await asyncio.gather(*tasks)
        tasks.clear()
        with factory() as db:
            runs = db.scalars(select(CheckRun).where(CheckRun.trigger == "scheduled",
                CheckRun.started_at >= measurement_at, CheckRun.scheduled_at.is_not(None))).all()
            delay = [(run.started_at - run.scheduled_at).total_seconds() * 1000 for run in runs]
            outcomes = Counter(run.outcome for run in runs)
            incident_counts = dict(db.execute(select(Incident.status, func.count()).group_by(Incident.status)).all())
            event_counts = dict(db.execute(select(Notification.event_type, func.count()).group_by(Notification.event_type)).all())
            postgres_version = db.scalar(text("SHOW server_version"))
        report = {
            "recorded_at": utcnow().isoformat(), "platform": platform.platform(), "logical_cpus": os.cpu_count(),
            "database": "PostgreSQL", "api_transport": "real loopback HTTP through Uvicorn",
            "python": sys.version.split()[0], "postgresql_version": postgres_version,
            "application_version": "1.0.0", "process_layout": "one Python process with concurrent API/worker/background tasks",
            "fixture_destinations": 1, "historical_outcomes": "success, failure, interrupted, skipped; configurations 1 and 2",
            "monitors": 100, "users": 5, "interval_seconds": 60, "global_concurrency": 10,
            "per_ip_concurrency": 2, "seed_history_rows": history_rows * 100,
            "warmup_seconds": args.warmup, "measured_seconds": args.seconds,
            "target_delay_seconds": [.05, .77], "api_statuses": dict(codes),
            "api_latency": {name: summary(values) for name, values in samples.items()},
            "schedule_delay": summary(delay), "live_check_outcomes": dict(outcomes),
            "incident_statuses": incident_counts, "notification_events": event_counts,
            "observed_max_running": max_running, "observed_max_per_ip": max_per_ip,
            "duplicate_active_checks": duplicate_active,
        }
        report["acceptance"] = {
            "duration_30_minutes": args.seconds >= 1800,
            "representative_30_day_history": args.history_days >= 30,
            "api_errors_zero": set(codes) <= {"200"},
            "no_duplicate_active_checks": duplicate_active == 0,
            "concurrency_limits_observed": max_running <= 10 and max_per_ip <= 2,
            "expected_incident_transitions": incident_counts == {"resolved": 100}
                and event_counts == {"incident_opened": 100, "incident_resolved": 100},
            "schedule_p95_under_5s": bool(delay) and report["schedule_delay"]["p95_ms"] <= 5000,
            "list_history_p95_under_500ms": all(summary(samples[name])["p95_ms"] is not None
                and summary(samples[name])["p95_ms"] <= 500 for name in ["list", "history"]),
            "stats_24h_p95_under_1s": bool(samples["stats_24h"]) and summary(samples["stats_24h"])["p95_ms"] <= 1000,
            "stats_30d_p95_under_2s": bool(samples["stats_30d"]) and summary(samples["stats_30d"])["p95_ms"] <= 2000,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        return all(report["acceptance"].values())
    finally:
        stop.set()
        if api_server:
            api_server.should_exit = True
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if runner:
            await runner.cleanup()
        if app:
            app.state.engine.dispose()
        with administration.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=120)
    parser.add_argument("--history-days", type=int, default=30)
    parser.add_argument("--output", default="var/load-report.json")
    args = parser.parse_args()
    if args.seconds < 1 or args.warmup < 0 or not 0 <= args.history_days <= 30:
        parser.error("Use seconds>=1, warmup>=0, and history-days between 0 and 30")
    raise SystemExit(0 if asyncio.run(benchmark(args)) else 1)
