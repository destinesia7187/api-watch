"""The durable scheduler is tested without live HTTP or wall-clock sleeps."""

import asyncio
import base64
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from api_watch.config import Settings
from api_watch.db import Base, database_now, make_database
from api_watch.engine import (
    abort_incident, cancel_monitor_runs, claim_check, cleanup, destination_guard,
    expire_leases, finish_check, schedule_due,
)
from api_watch.models import (
    CheckRun, DestinationLease, Incident, Monitor, MonitorConfig, Notification,
    ScheduleGap, TelegramConnection, User,
)
from api_watch.security import encrypt_payload

T0 = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgresql"])
def engine_database(request):
    url = "sqlite:///:memory:"
    if request.param == "postgresql":
        url = os.getenv("TEST_DATABASE_URL", "")
        if not url.startswith("postgresql"):
            pytest.skip("TEST_DATABASE_URL enables the real PostgreSQL concurrency suite")
    settings = Settings(app_env="test", database_url=url, check_concurrency=2,
                        encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
                        session_secret="test" * 12, _env_file=None)
    engine, factory = make_database(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        user = User(email=f"{uuid4()}@example.com", password_hash="fixture")
        session.add(user)
        session.flush()
        monitor = Monitor(owner_id=user.id, name="Health", next_due_at=T0,
                          notifications_enabled=True)
        session.add(monitor)
        session.flush()
        session.add(MonitorConfig(monitor_id=monitor.id, version=1, interval_seconds=60,
            timeout_ms=10000, failure_threshold=3, recovery_threshold=2,
            encrypted_payload=encrypt_payload({"url": "https://example.com/health?secret=hidden"}, settings)))
        session.add(TelegramConnection(user_id=user.id,
            encrypted_chat_id=encrypt_payload({"chat_id": 123}, settings)))
        user_id, monitor_id = user.id, monitor.id
    yield factory, settings, monitor_id
    with factory.begin() as session:
        user = session.get(User, user_id)
        if user:
            session.delete(user)
    engine.dispose()


def complete(factory, settings, monitor_id, slot, outcome="failure", trigger="scheduled", code=None):
    with factory.begin() as session:
        run = CheckRun(monitor_id=monitor_id, config_version=1, trigger=trigger, scheduled_at=slot,
                       created_at=slot, failed_assertions=[])
        session.add(run)
        session.flush()
        run_id = run.id
        claimed = claim_check(session, settings, slot)
        assert claimed and claimed.id == run_id
        token = claimed.lease_token
    with factory.begin() as session:
        assert finish_check(session, run_id, token, {
            "outcome": outcome, "error_code": code or ("unexpected_status" if outcome == "failure" else None),
            "http_status": 503 if outcome == "failure" else 200,
            "response_time_ms": 17.2, "elapsed_ms": 18.0,
        }, settings, slot + timedelta(seconds=1))
    return run_id


def test_scheduler_anchors_grid_and_aggregates_missed_slots(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        counts = schedule_due(session, settings, T0 + timedelta(seconds=181))
        assert counts == {"queued": 1, "skipped": 0, "missed": 3}
    with factory() as session:
        gap = session.scalar(select(ScheduleGap).where(ScheduleGap.monitor_id == monitor_id))
        assert (gap.slot_count, gap.first_slot_at, gap.last_slot_at) == (3, T0, T0 + timedelta(seconds=120))
        run = session.scalar(select(CheckRun).where(CheckRun.monitor_id == monitor_id))
        assert run.scheduled_at == T0 + timedelta(seconds=180)
        assert session.get(Monitor, monitor_id).next_due_at == T0 + timedelta(seconds=240)
    with factory.begin() as session:
        assert schedule_due(session, settings, T0 + timedelta(seconds=181))["queued"] == 0


def test_busy_slot_does_not_move_grid_and_fences_older_health(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        config = session.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == monitor_id))
        config.timeout_ms = 120000
        schedule_due(session, settings, T0)
        session.flush()
        run = claim_check(session, settings, T0)
        run_id, token = run.id, run.lease_token
    with factory.begin() as session:
        assert schedule_due(session, settings, T0 + timedelta(seconds=60))["skipped"] == 1
    with factory.begin() as session:
        assert finish_check(session, run_id, token, {"outcome": "failure"}, settings,
                            T0 + timedelta(seconds=61))
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.health_status == "unknown" and monitor.failure_streak == 0
        assert monitor.last_applied_scheduled_at == T0 + timedelta(seconds=60)
        assert monitor.next_due_at == T0 + timedelta(seconds=120)


def test_incident_thresholds_recovery_resets_and_exact_events(engine_database):
    factory, settings, monitor_id = engine_database
    ids = [complete(factory, settings, monitor_id, T0 + timedelta(seconds=60 * i)) for i in range(3)]
    with factory() as session:
        incident = session.scalar(select(Incident).where(Incident.monitor_id == monitor_id))
        assert incident.first_check_id == ids[0] and incident.confirming_check_id == ids[2]
        assert incident.started_at == T0 + timedelta(seconds=1)
        assert incident.detected_at == T0 + timedelta(seconds=121)
        assert session.get(Monitor, monitor_id).health_status == "down"
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=180), "success")
    with factory() as session:
        assert session.get(Monitor, monitor_id).health_status == "recovering"
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=240), "failure")
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=300), "success")
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=360), "success")
    with factory() as session:
        incident = session.scalar(select(Incident).where(Incident.monitor_id == monitor_id))
        assert incident.status == "resolved"
        assert incident.recovery_started_at == T0 + timedelta(seconds=301)
        assert incident.resolved_at == T0 + timedelta(seconds=361)
        events = session.scalars(select(Notification).where(Notification.monitor_id == monitor_id)).all()
        assert {item.event_type for item in events} == {"incident_opened", "incident_resolved"}
        assert all("hidden" not in str(item.payload) for item in events)


def test_manual_does_not_mutate_health_or_break_failure_series(engine_database):
    factory, settings, monitor_id = engine_database
    complete(factory, settings, monitor_id, T0)
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=10), "success", "manual")
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.failure_streak == 1 and monitor.health_status == "degraded"
        assert monitor.last_checked_at == T0 + timedelta(seconds=1)
        assert monitor.next_due_at == T0


def test_non_evaluable_outcome_breaks_series_without_resolving(engine_database):
    factory, settings, monitor_id = engine_database
    complete(factory, settings, monitor_id, T0)
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=60), "blocked", code="target_blocked")
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=120))
    with factory() as session:
        assert session.get(Monitor, monitor_id).failure_streak == 1
        assert not session.scalar(select(Incident.id).where(Incident.monitor_id == monitor_id))


def test_missed_slot_clears_pending_recovery_on_open_incident(engine_database):
    factory, settings, monitor_id = engine_database
    for i in range(3):
        complete(factory, settings, monitor_id, T0 + timedelta(seconds=60 * i))
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=180), "success")
    with factory.begin() as session:
        incident = session.scalar(select(Incident).where(Incident.monitor_id == monitor_id))
        assert incident.recovery_started_at == T0 + timedelta(seconds=181)
        monitor = session.get(Monitor, monitor_id)
        monitor.next_due_at = T0 + timedelta(seconds=240)
        schedule_due(session, settings, T0 + timedelta(seconds=300))
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        incident = session.scalar(select(Incident).where(Incident.monitor_id == monitor_id))
        assert monitor.health_status == "unknown" and monitor.success_streak == 0
        assert incident.status == "open" and incident.recovery_started_at is None


def test_expired_result_rejected_before_reaper_and_no_retry(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        schedule_due(session, settings, T0)
        session.flush()
        run = claim_check(session, settings, T0)
        run_id, token, deadline = run.id, run.lease_token, run.lease_expires_at
    with factory.begin() as session:
        assert not finish_check(session, run_id, token, {"outcome": "success"}, settings, deadline)
        assert expire_leases(session, settings, deadline) == 1
    with factory.begin() as session:
        assert claim_check(session, settings, deadline + timedelta(seconds=1)) is None
        assert not finish_check(session, run_id, token, {"outcome": "success"}, settings, deadline)
    with factory() as session:
        run = session.get(CheckRun, run_id)
        assert run.status == "finished" and run.outcome == "interrupted" and run.error_code == "worker_lost"


def test_unexpired_queues_cannot_starve_the_expired_lease_reaper(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        owner = session.get(Monitor, monitor_id).owner_id
        for index in range(501):
            name = f"early-{index:04}"
            session.add(Monitor(id=name, owner_id=owner, name=name))
            session.flush()
            session.add(MonitorConfig(monitor_id=name, version=1, interval_seconds=86400,
                                     encrypted_payload="fixture"))
            session.add(CheckRun(monitor_id=name, config_version=1, trigger="scheduled",
                                 scheduled_at=T0, created_at=T0))
        session.add(Monitor(id="zzzz-expired", owner_id=owner, name="Lost worker"))
        session.flush()
        expired = CheckRun(monitor_id="zzzz-expired", config_version=1, status="running",
                           trigger="manual", lease_token=str(uuid4()),
                           lease_expires_at=T0 + timedelta(seconds=10), created_at=T0)
        session.add(expired)
        session.flush()
        run_id = expired.id
    with factory.begin() as session:
        assert expire_leases(session, settings, T0 + timedelta(seconds=61)) == 1
    with factory() as session:
        assert session.get(CheckRun, run_id).outcome == "interrupted"


def test_pause_cancels_fences_and_keeps_open_incident(engine_database):
    factory, settings, monitor_id = engine_database
    for i in range(3):
        complete(factory, settings, monitor_id, T0 + timedelta(seconds=60 * i))
    with factory.begin() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.next_due_at = T0 + timedelta(seconds=180)
        schedule_due(session, settings, monitor.next_due_at)
        session.flush()
        run = claim_check(session, settings, monitor.next_due_at - timedelta(seconds=60))
        run_id, token = run.id, run.lease_token
        cancel_monitor_runs(session, monitor, "monitor_paused", T0 + timedelta(seconds=181))
        monitor.state = "paused"
    with factory.begin() as session:
        assert not finish_check(session, run_id, token, {"outcome": "success"}, settings,
                                T0 + timedelta(seconds=182))
        assert session.scalar(select(Incident).where(Incident.monitor_id == monitor_id)).status == "open"
        monitor = session.get(Monitor, monitor_id)
        abort_incident(session, monitor, "configuration_changed", settings, T0 + timedelta(seconds=183))
    with factory() as session:
        incident = session.scalar(select(Incident).where(Incident.monitor_id == monitor_id))
        assert incident.status == "aborted" and incident.resolved_at is None


def test_queue_deadline_and_result_idempotency(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        schedule_due(session, settings, T0)
    with factory.begin() as session:
        assert claim_check(session, settings, T0 + timedelta(seconds=60)) is None
    with factory() as session:
        row = session.scalar(select(CheckRun).where(CheckRun.monitor_id == monitor_id))
        assert row.outcome == "skipped" and row.error_code == "queue_expired"
    with factory.begin() as session:
        schedule_due(session, settings, T0 + timedelta(seconds=60))
        session.flush()
        run = claim_check(session, settings, T0 + timedelta(seconds=60))
        run_id, token = run.id, run.lease_token
        assert finish_check(session, run_id, token, {"outcome": "success"}, settings,
                            T0 + timedelta(seconds=61))
        assert not finish_check(session, run_id, token, {"outcome": "failure"}, settings,
                                T0 + timedelta(seconds=62))


def test_completion_refreshes_cached_monitor_after_another_session_pauses(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        schedule_due(session, settings, T0)
        session.flush()
        run = claim_check(session, settings, T0)
        run_id, token = run.id, run.lease_token
    with factory() as stale_session:
        cached_monitor = stale_session.get(Monitor, monitor_id)
        assert cached_monitor.state == "active"
        with factory.begin() as concurrent:
            concurrent.get(Monitor, monitor_id).state = "paused"
        assert cached_monitor.state == "active"
        assert not finish_check(stale_session, run_id, token, {"outcome": "success"}, settings,
                                T0 + timedelta(seconds=1))


def test_cleanup_preserves_open_incident_and_immediately_purges_deleted_secrets(engine_database):
    factory, settings, monitor_id = engine_database
    for i in range(3):
        complete(factory, settings, monitor_id, T0 + timedelta(seconds=60 * i))
    with factory.begin() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.state, monitor.deleted_at = "deleted", T0 + timedelta(days=31)
        result = cleanup(session, settings, T0 + timedelta(days=31))
        assert result["checks"] >= 3
    with factory() as session:
        assert session.scalar(select(Incident).where(Incident.monitor_id == monitor_id)).status == "open"
        config = session.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == monitor_id))
        assert config.encrypted_payload is None and config.secrets_purged_at
        assert session.get(Monitor, monitor_id)


async def test_destination_limit_uses_releasable_database_leases(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        now = database_now(session)
        ids = []
        for i in range(3):
            monitor = session.get(Monitor, monitor_id)
            other = Monitor(owner_id=monitor.owner_id, name=f"Other {i}")
            session.add(other)
            session.flush()
            run = CheckRun(monitor_id=other.id, config_version=1, trigger="manual", status="running",
                           lease_token=str(uuid4()), lease_expires_at=now + timedelta(minutes=1))
            session.add(run)
            session.flush()
            ids.append((run.id, run.lease_token))
    entered = asyncio.Event()

    async def third():
        async with destination_guard(factory, *ids[2])("93.184.216.34"):
            entered.set()

    async with destination_guard(factory, *ids[0])("93.184.216.34"):
        async with destination_guard(factory, *ids[1])("93.184.216.34"):
            task = asyncio.create_task(third())
            await asyncio.sleep(0.08)
            assert not entered.is_set()
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(DestinationLease)) == 2
        await asyncio.wait_for(task, 2)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DestinationLease)) == 0


def test_postgresql_concurrent_admission_is_deployment_global(engine_database):
    factory, settings, monitor_id = engine_database
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Cross-process coordination requires PostgreSQL")
    with factory.begin() as session:
        owner_id = session.get(Monitor, monitor_id).owner_id
        for i in range(8):
            monitor = Monitor(owner_id=owner_id, name=f"Concurrent {i}")
            session.add(monitor)
            session.flush()
            session.add(MonitorConfig(monitor_id=monitor.id, version=1, interval_seconds=60,
                                     encrypted_payload="fixture"))
            session.add(CheckRun(monitor_id=monitor.id, config_version=1, trigger="manual",
                                 created_at=T0))

    def claim(_):
        with factory.begin() as session:
            result = claim_check(session, settings, T0)
            return result.id if result else None

    with ThreadPoolExecutor(max_workers=8) as workers:
        claims = list(workers.map(claim, range(8)))
    assert len([run for run in claims if run]) == settings.check_concurrency
    assert len(set(run for run in claims if run)) == settings.check_concurrency


def test_result_incident_and_outbox_roll_back_together(engine_database):
    factory, settings, monitor_id = engine_database
    complete(factory, settings, monitor_id, T0)
    complete(factory, settings, monitor_id, T0 + timedelta(seconds=60))
    slot = T0 + timedelta(seconds=120)
    with factory.begin() as session:
        run = CheckRun(monitor_id=monitor_id, config_version=1, trigger="scheduled",
                       scheduled_at=slot, created_at=slot)
        session.add(run)
        session.flush()
        claimed = claim_check(session, settings, slot)
        run_id, token = claimed.id, claimed.lease_token
    with pytest.raises(RuntimeError, match="Injected transaction failure"):
        with factory.begin() as session:
            assert finish_check(session, run_id, token, {"outcome": "failure", "error_code": "timeout"},
                                settings, slot + timedelta(seconds=1))
            session.flush()
            raise RuntimeError("Injected transaction failure")
    with factory() as session:
        assert session.get(CheckRun, run_id).status == "running"
        assert session.get(Monitor, monitor_id).failure_streak == 2
        assert not session.scalar(select(Incident.id).where(Incident.monitor_id == monitor_id))
        assert not session.scalar(select(Notification.id).where(Notification.monitor_id == monitor_id))
    with factory.begin() as session:
        assert finish_check(session, run_id, token, {"outcome": "failure", "error_code": "timeout"},
                            settings, slot + timedelta(seconds=2))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Incident).where(
            Incident.monitor_id == monitor_id)) == 1
        assert session.scalar(select(func.count()).select_from(Notification).where(
            Notification.monitor_id == monitor_id)) == 1


def test_postgresql_concurrent_schedulers_enqueue_only_one_slot(engine_database):
    factory, settings, monitor_id = engine_database
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Concurrent row locking requires PostgreSQL")

    def schedule(_):
        with factory.begin() as session:
            return schedule_due(session, settings, T0)["queued"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        counts = list(workers.map(schedule, range(2)))
    assert sum(counts) == 1
    with factory() as session:
        runs = session.scalars(select(CheckRun).where(CheckRun.monitor_id == monitor_id)).all()
        assert len(runs) == 1 and runs[0].scheduled_at == T0


def test_postgresql_concurrent_completions_apply_one_incident(engine_database):
    factory, settings, monitor_id = engine_database
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Concurrent row locking requires PostgreSQL")
    with factory.begin() as session:
        session.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == monitor_id)).failure_threshold = 1
        schedule_due(session, settings, T0)
        session.flush()
        run = claim_check(session, settings, T0)
        run_id, token = run.id, run.lease_token

    def finish(_):
        with factory.begin() as session:
            return finish_check(session, run_id, token, {"outcome": "failure", "error_code": "timeout"},
                                settings, T0 + timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(finish, range(2)))
    assert sorted(results) == [False, True]
    with factory() as session:
        assert session.get(Monitor, monitor_id).failure_streak == 1
        assert session.scalar(select(func.count()).select_from(Incident).where(
            Incident.monitor_id == monitor_id)) == 1
        assert session.scalar(select(func.count()).select_from(Notification).where(
            Notification.monitor_id == monitor_id)) == 1
def test_database_timestamps_are_returned_in_utc(engine_database):
    factory, settings, monitor_id = engine_database
    with factory.begin() as session:
        schedule_due(session, settings, T0)
    with factory() as session:
        run = session.scalar(select(CheckRun).where(CheckRun.monitor_id == monitor_id))
        assert run.scheduled_at == T0
        assert run.scheduled_at.utcoffset() == timedelta(0)
