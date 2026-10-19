"""AT-16: kill a real worker after a POST, fence its lease, and recover normally.

Requires TEST_DATABASE_URL on a local PostgreSQL server. All application tables
live in one unique disposable schema. Only worker subprocesses created here are
terminated. Real database time, HTTP, processes and a 60-second slot are used.
"""

import argparse
import asyncio
import base64
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from api_watch.config import Settings
from api_watch.db import database_now, make_database, utcnow
from api_watch.engine import expire_leases, finish_check, schedule_due
from api_watch.models import CheckRun, Incident, Monitor, MonitorConfig, Notification, User
from api_watch.schemas import MonitorCreate
from api_watch.security import encrypt_payload, random_token


def progress(message):
    print(message, flush=True)


def child_environment(settings):
    environment = os.environ.copy()
    environment.update({
        "APP_ENV": "test", "DATABASE_URL": settings.database_url, "DATABASE_SCHEMA": settings.database_schema,
        "PUBLIC_BASE_URL": settings.public_base_url, "SESSION_SECRET": settings.session_secret,
        "ENCRYPTION_KEY": settings.encryption_key, "ENCRYPTION_KEY_ID": settings.encryption_key_id,
        "CHECK_CONCURRENCY": "1", "TEST_ALLOWED_HOSTS": "127.0.0.1", "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_BOT_USERNAME": "", "SMTP_HOST": "", "REGISTRATION_ENABLED": "false",
        "PYTHONUNBUFFERED": "1", "LOG_LEVEL": "WARNING",
    })
    return environment


async def stop_owned(process, hard=False):
    if process.poll() is None:
        process.kill() if hard else process.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
    return process.returncode


async def migrate(environment):
    def run():
        return subprocess.run(
            [sys.executable, "-m", "api_watch", "migrate"], env=environment,
            capture_output=True, timeout=45, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    completed = await asyncio.to_thread(run)
    if completed.returncode:
        raise RuntimeError("The isolated schema migration subprocess failed")


async def verify(args):
    if not __debug__:
        raise RuntimeError("Run without Python optimization so all verification assertions remain enabled")
    raw_url = os.environ.get("TEST_DATABASE_URL", "")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("TEST_DATABASE_URL must reference a local disposable PostgreSQL test database")
    schema = "recovery_" + uuid4().hex
    if not re.fullmatch(r"recovery_[a-f0-9]{32}", schema):
        raise ValueError("Unexpected disposable schema name")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.parent / f"worker-recovery-{uuid4().hex}.log"
    report = {
        "recorded_at": utcnow().isoformat(), "status": "failed", "platform": platform.platform(),
        "python_version": platform.python_version(), "schema": schema,
        "scope": "Real killed CLI worker and controlled local POST; unique disposable PostgreSQL schema",
        "clock": "Actual PostgreSQL clock_timestamp; no simulated timestamps",
        "interval_seconds": 60, "target_timeout_ms": 1000, "worker_lease_seconds": 16,
        "verification": {}, "cleanup": {"owned_workers_stopped": False, "schema_removed": False},
    }
    started = time.monotonic()
    administration = create_engine(raw_url, pool_pre_ping=True)
    engine = None
    runner = None
    log_file = None
    owned = []
    schema_created = False
    received = []
    first_received, second_received = asyncio.Event(), asyncio.Event()

    async def target(request):
        body = await request.json()
        received.append({"method": request.method, "body": body, "received_at": utcnow().isoformat()})
        if len(received) == 1:
            first_received.set()
            await asyncio.sleep(2)
        else:
            second_received.set()
        return web.json_response({"status": "ok"})

    try:
        with administration.begin() as connection:
            report["postgresql_version"] = connection.scalar(text("SELECT version()"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        application = web.Application()
        application.router.add_post("/operation", target)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        settings = Settings(app_env="test", database_url=raw_url, database_schema=schema,
            public_base_url="http://worker-drill.invalid", test_allowed_hosts="127.0.0.1", check_concurrency=1,
            encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
            encryption_key_id="worker-drill-v1", session_secret=random_token(), _env_file=None)
        environment = child_environment(settings)
        progress("Applying committed migrations inside the disposable recovery schema.")
        await migrate(environment)
        engine, factory = make_database(settings)
        config = MonitorCreate(name="Worker crash POST fixture", url=f"http://127.0.0.1:{port}/operation",
            method="POST", side_effects_acknowledged=True, interval_seconds=60, timeout_ms=1000,
            body={"enabled": True, "json": {"operation": "crash-recovery-proof"}},
            json_assertions=[{"pointer": "/status", "operator": "equals", "expected": "ok"}]).model_dump()
        with factory.begin() as session:
            user = User(email="worker-recovery@example.com", password_hash="not-a-login-account")
            session.add(user)
            session.flush()
            anchor = database_now(session)
            monitor = Monitor(owner_id=user.id, name=config["name"], next_due_at=anchor)
            session.add(monitor)
            session.flush()
            monitor_id = monitor.id
            session.add(MonitorConfig(monitor_id=monitor_id, version=1,
                encrypted_payload=encrypt_payload(config, settings), key_id=settings.encryption_key_id,
                interval_seconds=60, timeout_ms=1000, failure_threshold=3, recovery_threshold=2))
        with factory.begin() as session:
            assert schedule_due(session, settings) == {"queued": 1, "skipped": 0, "missed": 0}
        with factory() as session:
            original_id = session.scalar(select(CheckRun.id))
        report["anchor_at"] = anchor.isoformat()
        next_slot = anchor + timedelta(seconds=60)
        log_file = log_path.open("wb")

        def spawn_worker():
            process = subprocess.Popen([sys.executable, "-m", "api_watch", "worker"], env=environment,
                stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            owned.append(process)
            return process

        worker = spawn_worker()
        progress("Started a real CLI worker; waiting for the fixture to receive the first POST.")
        await asyncio.wait_for(first_received.wait(), timeout=20)
        # Kill immediately after the target confirms receipt, before inspecting
        # state through another DB round trip or allowing a target timeout.
        report["killed_worker_exit_code"] = await stop_owned(worker, hard=True)
        progress("The target received the POST. The owned worker was hard-killed before completing it.")
        with factory() as session:
            original = session.get(CheckRun, original_id)
            assert original.status == "running" and original.outcome is None
            lease_token, lease_expiry = original.lease_token, original.lease_expires_at
            assert lease_token is not None
            report["actual_lease_duration_seconds"] = (lease_expiry - original.started_at).total_seconds()
        assert len(received) == 1 and received[0]["method"] == "POST"
        assert received[0]["body"] == {"operation": "crash-recovery-proof"}
        report["verification"]["target_received_exact_post_before_worker_kill"] = True
        report["verification"]["hard_kill_left_ambiguous_running_check"] = True
        report["first_post_received_at"] = received[0]["received_at"]
        report["lease_expires_at"] = lease_expiry.isoformat()
        wait_started = time.monotonic()
        next_progress = time.monotonic()
        while True:
            with factory() as session:
                now = database_now(session)
            if now >= lease_expiry:
                break
            if time.monotonic() >= next_progress:
                progress(f"Waiting for real lease expiry: {(lease_expiry - now).total_seconds():.1f}s remaining.")
                next_progress = time.monotonic() + 5
            await asyncio.sleep(min(.25, max(.01, (lease_expiry - now).total_seconds())))
        report["actual_lease_wait_seconds"] = round(time.monotonic() - wait_started, 3)
        with factory.begin() as session:
            assert expire_leases(session, settings) == 1
        with factory() as session:
            original = session.get(CheckRun, original_id)
            assert (original.status, original.outcome, original.error_code) == ("finished", "interrupted", "worker_lost")
            assert original.lease_token is None
            assert session.scalar(select(func.count()).select_from(CheckRun)) == 1
            report["original_terminal_state"] = {"status": original.status, "outcome": original.outcome,
                                                   "error_code": original.error_code}
        report["verification"]["real_expired_lease_becomes_worker_lost"] = True
        with factory.begin() as session:
            assert not finish_check(session, original_id, lease_token, {
                "outcome": "success", "http_status": 200, "response_time_ms": 1,
                "elapsed_ms": 1, "bytes_received": 15, "error_code": None,
                "error_message": None, "failed_assertions": [],
            }, settings)
        report["verification"]["late_completion_with_old_lease_is_rejected"] = True
        replacement = spawn_worker()
        progress("Expired check is interrupted and its late completion was fenced. Replacement worker is running.")
        await asyncio.sleep(2)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(CheckRun)) == 1
            assert session.scalar(select(CheckRun.id).where(CheckRun.status.in_(["queued", "running"]))) is None
        assert len(received) == 1
        report["verification"]["replacement_does_not_requeue_or_replay_original_post"] = True
        next_progress = time.monotonic()
        while True:
            with factory() as session:
                now = database_now(session)
            if now >= next_slot:
                break
            assert replacement.poll() is None
            if time.monotonic() >= next_progress:
                progress(f"Waiting for the next real anchored 60-second slot: {(next_slot - now).total_seconds():.1f}s remaining.")
                next_progress = time.monotonic() + 10
            await asyncio.sleep(min(.5, max(.01, (next_slot - now).total_seconds())))
        with factory.begin() as session:
            assert schedule_due(session, settings) == {"queued": 1, "skipped": 0, "missed": 0}
        progress("The next actual anchored slot is queued; waiting for the replacement worker's result.")
        await asyncio.wait_for(second_received.wait(), timeout=10)
        deadline = time.monotonic() + 10
        while True:
            with factory() as session:
                current = session.scalar(select(CheckRun).where(CheckRun.id != original_id))
                if current and current.status == "finished":
                    assert current.outcome == "success" and current.http_status == 200
                    assert current.scheduled_at == next_slot
                    assert session.scalar(select(func.count()).select_from(CheckRun)) == 2
                    original = session.get(CheckRun, original_id)
                    assert original.outcome == "interrupted" and original.error_code == "worker_lost"
                    assert session.scalar(select(func.count()).select_from(Incident)) == 0
                    assert session.scalar(select(func.count()).select_from(Notification)) == 0
                    monitor = session.get(Monitor, monitor_id)
                    assert monitor.health_status == "up"
                    report["next_slot_result"] = {"scheduled_at": current.scheduled_at.isoformat(),
                        "outcome": current.outcome, "http_status": current.http_status,
                        "response_time_ms": current.response_time_ms}
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError("The replacement worker did not finish the next slot")
            await asyncio.sleep(.1)
        assert len(received) == 2
        report["verification"]["next_real_anchored_slot_completes_successfully"] = True
        report["verification"]["original_check_stays_interrupted_after_recovery"] = True
        report["verification"]["interruption_does_not_create_false_incident_or_notification"] = True
        report["post_requests_received"] = len(received)
        report["check_rows"] = 2
        report["public_or_telegram_requests"] = 0
        report["status"] = "passed"
        progress("PASS: exactly two POSTs across two distinct scheduled slots; no replay of the killed operation.")
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        for process in owned:
            await stop_owned(process)
        report["cleanup"]["owned_workers_stopped"] = all(process.poll() is not None for process in owned)
        if log_file:
            log_file.close()
        if runner:
            await runner.cleanup()
        if engine:
            engine.dispose()
        if schema_created:
            with administration.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report["cleanup"]["schema_removed"] = True
        administration.dispose()
        if log_path.exists():
            log_path.unlink()
        report["total_seconds"] = round(time.monotonic() - started, 3)
        if not all(report["cleanup"].values()):
            report["status"] = "failed"
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="var/worker-recovery-report.json")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args)) else 1)


if __name__ == "__main__":
    main()
