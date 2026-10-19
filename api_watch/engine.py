"""Durable scheduling, fenced check execution, incident transitions and retention.

PostgreSQL is the production coordinator: monitor row locks serialize state,
advisory locks serialize deployment-wide admission, and expiring leases fence
late results. SQLite supports a single local background/worker deployment.
"""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, text

from api_watch.db import database_now
from api_watch.models import (
    ApiToken, CheckRun, DestinationLease, EmailMessage, Heartbeat, IdempotencyKey, Incident, LoginSession, Monitor,
    MonitorConfig, Notification, PasswordReset, RateBucket, ScheduleGap, TelegramLink,
)
from api_watch.security import decrypt_payload

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running")
TERMINAL_NOTIFICATIONS = ("sent", "failed", "suppressed", "cancelled")
_local_locks = {}


def _now(session, now):
    return now if now is not None else database_now(session)


def _lock_monitor(session, monitor_id):
    return session.scalar(select(Monitor).where(Monitor.id == monitor_id).with_for_update()
                          .execution_options(populate_existing=True))


def _config(session, monitor):
    return session.scalar(select(MonitorConfig).where(
        MonitorConfig.monitor_id == monitor.id, MonitorConfig.version == monitor.config_version))


def _reset_streaks(monitor):
    monitor.failure_streak = monitor.success_streak = 0
    monitor.streak_started_at = monitor.recovery_started_at = None


def _unknown(monitor, scheduled_at, session=None):
    if scheduled_at is not None and (
        monitor.last_applied_scheduled_at is None or scheduled_at > monitor.last_applied_scheduled_at
    ):
        monitor.last_applied_scheduled_at = scheduled_at
        monitor.health_status = "unknown"
        _reset_streaks(monitor)
        if session is not None:
            incident = session.scalar(select(Incident).where(
                Incident.monitor_id == monitor.id, Incident.status == "open"))
            if incident:
                incident.recovery_started_at = None


def _terminal(run, outcome, code, now):
    run.status, run.outcome, run.error_code = "finished", outcome, code
    run.finished_at = now
    run.error_message = {
        "monitor_busy": "The monitor already had an active check at this scheduled slot.",
        "queue_expired": "The check was not started before its queue deadline.",
        "worker_lost": "The worker lease expired; this request will not be retried.",
        "monitor_paused": "The monitor was paused.",
        "monitor_deleted": "The monitor was deleted.",
        "configuration_changed": "The monitor configuration changed.",
    }.get(code, "The check was cancelled by a management operation.")
    run.lease_token = run.lease_expires_at = None


def cancel_monitor_runs(session, monitor, reason, now=None):
    """Caller holds the monitor lock; invalidate all queued/running leases."""
    now = _now(session, now)
    runs = session.scalars(select(CheckRun).where(
        CheckRun.monitor_id == monitor.id, CheckRun.status.in_(ACTIVE)).with_for_update()).all()
    for run in runs:
        _terminal(run, "cancelled", reason, now)
    _reset_streaks(monitor)
    monitor.health_status = "unknown"
    incident = session.scalar(select(Incident).where(
        Incident.monitor_id == monitor.id, Incident.status == "open"))
    if incident:
        incident.recovery_started_at = None
    return len(runs)


def abort_incident(session, monitor, reason, settings, now=None):
    """Abort observation without falsely reporting recovery."""
    from api_watch.telegram import enqueue_event

    now = _now(session, now)
    incident = session.scalar(select(Incident).where(
        Incident.monitor_id == monitor.id, Incident.status == "open").with_for_update())
    if incident:
        incident.status, incident.reason, incident.ended_at = "aborted", reason, now
        enqueue_event(session, monitor, incident, "incident_aborted", settings, now)
    _reset_streaks(monitor)
    monitor.health_status = "unknown"
    return incident


def schedule_due(session, settings, now=None):
    """Advance anchored slots and enqueue only the newest due slot. Caller commits."""
    now = _now(session, now)
    counts = {"queued": 0, "skipped": 0, "missed": 0}
    monitors = session.scalars(select(Monitor).where(
        Monitor.state == "active", Monitor.next_due_at <= now
    ).order_by(Monitor.next_due_at, Monitor.id).limit(500).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)).all()
    for monitor in monitors:
        config = _config(session, monitor)
        if not config or not config.encrypted_payload:
            continue
        interval = timedelta(seconds=config.interval_seconds)
        first = monitor.next_due_at
        missing = int((now - first).total_seconds() // config.interval_seconds)
        slot = first + missing * interval
        if missing:
            last_missed = slot - interval
            session.add(ScheduleGap(
                monitor_id=monitor.id, config_version=monitor.config_version,
                first_slot_at=first, last_slot_at=last_missed, slot_count=missing,
                interval_seconds=config.interval_seconds, reason="schedule_missed", created_at=now))
            _unknown(monitor, last_missed, session)
            counts["missed"] += missing
        monitor.next_due_at = slot + interval
        active = session.scalar(select(CheckRun.id).where(
            CheckRun.monitor_id == monitor.id, CheckRun.status.in_(ACTIVE)).limit(1))
        run = CheckRun(monitor_id=monitor.id, config_version=monitor.config_version,
                       trigger="scheduled", scheduled_at=slot, created_at=now,
                       status="queued", failed_assertions=[])
        if active:
            _terminal(run, "skipped", "monitor_busy", now)
            _unknown(monitor, slot, session)
            counts["skipped"] += 1
        else:
            counts["queued"] += 1
        session.add(run)
    return counts


def _admission_lock(session):
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(7147064001)"))


def claim_check(session, settings, now=None):
    """Atomically admit one request under the deployment-wide concurrency cap."""
    _admission_lock(session)
    now = _now(session, now)
    running = session.scalar(select(func.count()).select_from(CheckRun).where(
        CheckRun.status == "running", CheckRun.lease_expires_at > now))
    if running >= settings.check_concurrency:
        return None
    # Lock monitor before run, consistently with management and completion paths.
    monitor = session.scalar(select(Monitor).where(
        Monitor.id.in_(select(CheckRun.monitor_id).where(CheckRun.status == "queued"))
    ).order_by(Monitor.id).with_for_update(skip_locked=True).limit(1)
        .execution_options(populate_existing=True))
    if not monitor:
        return None
    run = session.scalar(select(CheckRun).where(
        CheckRun.monitor_id == monitor.id, CheckRun.status == "queued").with_for_update())
    if not run:
        return None
    config = _config(session, monitor)
    if monitor.state != "active" or run.config_version != monitor.config_version or not config:
        _terminal(run, "cancelled", "monitor_paused" if monitor.state == "paused"
                  else "configuration_changed", now)
        return None
    deadline = (run.scheduled_at + timedelta(seconds=config.interval_seconds)
                if run.trigger == "scheduled" else run.created_at + timedelta(seconds=60))
    if now >= deadline:
        _terminal(run, "skipped", "queue_expired", now)
        if run.trigger == "scheduled":
            _unknown(monitor, run.scheduled_at, session)
        return None
    run.status, run.started_at = "running", now
    run.lease_token = str(uuid4())
    run.lease_expires_at = now + timedelta(milliseconds=config.timeout_ms, seconds=15)
    session.flush()
    return run


def _apply_result(session, monitor, config, run, settings, now):
    from api_watch.telegram import enqueue_event

    if run.trigger != "scheduled" or run.scheduled_at is None:
        return
    if monitor.last_applied_scheduled_at and run.scheduled_at <= monitor.last_applied_scheduled_at:
        return
    monitor.last_applied_scheduled_at = run.scheduled_at
    monitor.last_checked_at = now
    monitor.last_response_time_ms, monitor.last_error_code = run.response_time_ms, run.error_code
    incident = session.scalar(select(Incident).where(
        Incident.monitor_id == monitor.id, Incident.status == "open").with_for_update())
    if run.outcome not in ("success", "failure"):
        _reset_streaks(monitor)
        monitor.health_status = "unknown"
        if incident:
            incident.recovery_started_at = None
        return
    if run.outcome == "failure":
        monitor.success_streak, monitor.recovery_started_at = 0, None
        if not monitor.failure_streak:
            monitor.streak_started_at = now
        monitor.failure_streak += 1
        monitor.health_status = "down" if incident else "degraded"
        if incident:
            incident.last_check_id, incident.recovery_started_at = run.id, None
        elif monitor.failure_streak >= config.failure_threshold:
            first_check = session.scalar(select(CheckRun.id).where(
                CheckRun.monitor_id == monitor.id, CheckRun.config_version == run.config_version,
                CheckRun.trigger == "scheduled", CheckRun.outcome == "failure",
                CheckRun.finished_at >= monitor.streak_started_at
            ).order_by(CheckRun.scheduled_at).limit(1))
            incident = Incident(monitor_id=monitor.id, config_version=run.config_version,
                                status="open", started_at=monitor.streak_started_at,
                                detected_at=now, first_check_id=first_check or run.id,
                                confirming_check_id=run.id, last_check_id=run.id,
                                reason=run.error_code or "check_failed", created_at=now)
            session.add(incident)
            session.flush()
            monitor.health_status = "down"
            enqueue_event(session, monitor, incident, "incident_opened", settings, now)
    else:
        monitor.failure_streak, monitor.streak_started_at = 0, None
        if not monitor.success_streak:
            monitor.recovery_started_at = now
        monitor.success_streak += 1
        if not incident:
            monitor.health_status = "up"
        else:
            incident.last_check_id = run.id
            incident.recovery_started_at = monitor.recovery_started_at
            monitor.health_status = "recovering"
            if monitor.success_streak >= config.recovery_threshold:
                incident.status = "resolved"
                incident.resolved_at = incident.ended_at = now
                monitor.health_status = "up"
                enqueue_event(session, monitor, incident, "incident_resolved", settings, now)


def finish_check(session, run_id, lease_token, result, settings, now=None):
    """Fence late/cancelled completions and apply state/outbox in one transaction."""
    snapshot = session.get(CheckRun, run_id)
    if not snapshot:
        return False
    monitor = _lock_monitor(session, snapshot.monitor_id)
    run = session.scalar(select(CheckRun).where(CheckRun.id == run_id).with_for_update()
                         .execution_options(populate_existing=True))
    now = _now(session, now)
    if (not monitor or not run or run.status != "running" or run.lease_token != lease_token
            or run.lease_expires_at is None or run.lease_expires_at <= now
            or monitor.state != "active" or monitor.config_version != run.config_version):
        return False
    config = _config(session, monitor)
    if not config:
        return False
    run.status, run.finished_at = "finished", now
    run.outcome = result.get("outcome", "interrupted")
    if run.outcome not in {"success", "failure", "blocked", "interrupted"}:
        run.outcome = "interrupted"
    # Only allow checker-owned, explicitly safe values into retained history.
    for key in ("http_status", "response_time_ms", "elapsed_ms", "bytes_received", "error_code",
                "error_message", "failed_assertions"):
        if key in result:
            setattr(run, key, result[key])
    run.lease_token = run.lease_expires_at = None
    session.flush()
    _apply_result(session, monitor, config, run, settings, now)
    return True


def expire_leases(session, settings, now=None):
    now = _now(session, now)
    count = 0
    # Select expired candidates before the batch limit. Valid long-interval
    # queues must not permanently crowd a lost worker out of the first page.
    queue_due = or_(
        and_(CheckRun.trigger == "manual", CheckRun.created_at <= now - timedelta(seconds=60)),
        and_(CheckRun.trigger == "scheduled",
             func.extract("epoch", CheckRun.scheduled_at) + func.coalesce(MonitorConfig.interval_seconds, 60)
             <= func.extract("epoch", now)),
    )
    monitor_ids = select(CheckRun.monitor_id).outerjoin(MonitorConfig, and_(
        MonitorConfig.monitor_id == CheckRun.monitor_id,
        MonitorConfig.version == CheckRun.config_version)).where(or_(
            and_(CheckRun.status == "running", CheckRun.lease_expires_at <= now),
            and_(CheckRun.status == "queued", queue_due),
        ))
    monitors = session.scalars(select(Monitor).where(Monitor.id.in_(monitor_ids)).order_by(
    Monitor.id).limit(500).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)).all()
    for monitor in monitors:
        config = _config(session, monitor)
        for run in session.scalars(select(CheckRun).where(
                CheckRun.monitor_id == monitor.id, CheckRun.status.in_(ACTIVE)).with_for_update()).all():
            code = None
            if run.status == "running" and run.lease_expires_at <= now:
                code = "worker_lost"
            elif run.status == "queued":
                deadline = (run.scheduled_at + timedelta(seconds=config.interval_seconds)
                            if run.trigger == "scheduled" and config
                            else run.created_at + timedelta(seconds=60))
                if deadline <= now:
                    code = "queue_expired"
            if code:
                _terminal(run, "interrupted" if code == "worker_lost" else "skipped", code, now)
                if run.trigger == "scheduled" and run.config_version == monitor.config_version:
                    _unknown(monitor, run.scheduled_at, session)
                count += 1
    return count


def cleanup(session, settings, now=None):
    """Small bounded retention batches; call repeatedly until old backlog is gone."""
    now = _now(session, now)
    counts = {}

    def remove(model, condition, label):
        ids = select(model.id).where(condition).limit(1000)
        result = session.execute(delete(model).where(model.id.in_(ids)))
        counts[label] = result.rowcount

    cutoff = now - timedelta(days=settings.history_days)
    remove(CheckRun, and_(CheckRun.status == "finished", CheckRun.created_at < cutoff), "checks")
    remove(ScheduleGap, ScheduleGap.created_at < cutoff, "gaps")
    remove(Incident, and_(Incident.status != "open", Incident.ended_at < now - timedelta(days=180)),
           "incidents")
    remove(Notification, and_(Notification.status.in_(TERMINAL_NOTIFICATIONS),
           func.coalesce(Notification.sent_at, Notification.next_attempt_at) < cutoff), "notifications")
    remove(EmailMessage, and_(EmailMessage.status.in_(("sent", "failed")),
                             EmailMessage.created_at < cutoff), "mail")
    remove(IdempotencyKey, IdempotencyKey.expires_at < now, "idempotency")
    remove(TelegramLink, TelegramLink.expires_at < now, "links")
    remove(PasswordReset, PasswordReset.expires_at < now, "password_resets")
    remove(LoginSession, or_(LoginSession.expires_at < now, LoginSession.revoked_at < cutoff), "sessions")
    remove(ApiToken, or_(ApiToken.expires_at < cutoff, ApiToken.revoked_at < cutoff), "api_tokens")
    remove(RateBucket, RateBucket.expires_at < now, "rate_buckets")
    deleted = select(Monitor.id).where(Monitor.state == "deleted")
    session.query(MonitorConfig).filter(MonitorConfig.monitor_id.in_(deleted),
        MonitorConfig.encrypted_payload.is_not(None)).update(
            {MonitorConfig.encrypted_payload: None, MonitorConfig.secrets_purged_at: now},
            synchronize_session=False)
    remove(Monitor, and_(Monitor.state == "deleted", Monitor.deleted_at < now - timedelta(days=180)),
           "deleted_monitors")
    # Keep version metadata while either retained checks or incidents reference it.
    needed_check = select(CheckRun.id).where(CheckRun.monitor_id == MonitorConfig.monitor_id,
        CheckRun.config_version == MonitorConfig.version).exists()
    needed_incident = select(Incident.id).where(Incident.monitor_id == MonitorConfig.monitor_id,
        Incident.config_version == MonitorConfig.version).exists()
    current = select(Monitor.id).where(Monitor.id == MonitorConfig.monitor_id,
        Monitor.config_version == MonitorConfig.version).exists()
    remove(MonitorConfig, and_(~current, ~needed_check, ~needed_incident), "old_configs")
    return counts


def _lock_key(value):
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


@asynccontextmanager
async def service_lock(session_factory, name, slots=1, wait=False):
    """Session advisory locks survive commits, and are explicitly released before pooling."""
    engine = session_factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        key = (id(engine), name, id(asyncio.get_running_loop()))
        semaphore = _local_locks.setdefault(key, asyncio.Semaphore(slots))
        if not wait and semaphore.locked():
            yield False
            return
        await semaphore.acquire()
        try:
            yield True
        finally:
            semaphore.release()
        return
    connection = None
    acquired = None
    try:
        while acquired is None:
            connection = engine.connect()
            for index in range(slots):
                key = _lock_key(f"api-watch:{name}:{index}")
                if connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                    acquired = key
                    break
            connection.commit()
            if acquired is not None or not wait:
                break
            connection.close()
            connection = None
            await asyncio.sleep(0.05)
        yield acquired is not None
    finally:
        if connection is not None:
            if acquired is not None:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": acquired})
                connection.commit()
            connection.close()


def destination_guard(session_factory, run_id, lease_token):
    """Short transactions enforce two live sockets per IP across all workers.

    Capacity expires with the owning check lease. No pooled database connection
    or transaction remains open during an HTTP exchange.
    """
    @asynccontextmanager
    async def guard(ip):
        acquired = False
        try:
            while not acquired:
                with session_factory.begin() as session:
                    if session.bind.dialect.name == "postgresql":
                        session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                        {"key": _lock_key(f"destination:{ip}")})
                    now = database_now(session)
                    run = session.get(CheckRun, run_id)
                    if (not run or run.status != "running" or run.lease_token != lease_token
                            or run.lease_expires_at <= now):
                        raise RuntimeError("Check lease is no longer valid")
                    session.execute(delete(DestinationLease).where(DestinationLease.expires_at <= now))
                    busy = session.scalar(select(func.count()).select_from(DestinationLease).where(
                        DestinationLease.ip == ip, DestinationLease.expires_at > now))
                    if busy < 2:
                        session.add(DestinationLease(run_id=run_id, ip=ip, lease_token=lease_token,
                                                     expires_at=run.lease_expires_at))
                        acquired = True
                if not acquired:
                    await asyncio.sleep(0.05)
            yield True
        finally:
            if acquired:
                with session_factory.begin() as session:
                    session.execute(delete(DestinationLease).where(DestinationLease.run_id == run_id,
                                                                   DestinationLease.lease_token == lease_token))
    return guard


def heartbeat(session_factory, name, details=None):
    with session_factory.begin() as session:
        current = session.get(Heartbeat, name)
        if not current:
            current = Heartbeat(id=name)
            session.add(current)
        current.last_seen_at, current.details = database_now(session), details or {}


async def _sleep(stop_event, seconds):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_worker(session_factory, settings, stop_event=None):
    from api_watch.checker import execute_check

    stop_event = stop_event or asyncio.Event()
    tasks = set()

    async def execute(run_id, lease_token, payload, timeout_ms):
        check_task = watcher = None

        async def cancellation_watch():
            while True:
                await asyncio.sleep(0.5)
                with session_factory() as session:
                    run = session.get(CheckRun, run_id)
                    if not run or run.status != "running" or run.lease_token != lease_token:
                        return

        try:
            async with asyncio.timeout(timeout_ms / 1000 + 15):
                check_task = asyncio.create_task(execute_check(payload, settings,
                    destination_guard=destination_guard(session_factory, run_id, lease_token)))
                watcher = asyncio.create_task(cancellation_watch())
                done, _ = await asyncio.wait((check_task, watcher), return_when=asyncio.FIRST_COMPLETED)
                if check_task in done:
                    result = check_task.result()
                else:
                    # Pause/delete invalidated the database lease. Stop the local
                    # exchange promptly; the remote server may already have received it.
                    result = {"outcome": "interrupted", "error_code": "worker_lost",
                              "error_message": "The running check was cancelled."}
        except asyncio.CancelledError:
            result = {"outcome": "interrupted", "error_code": "worker_lost",
                      "error_message": "Worker execution was interrupted."}
            raise
        except Exception:
            result = {"outcome": "interrupted", "error_code": "internal_error",
                      "error_message": "API Watch could not complete this check."}
            logger.warning("Check execution failed internally", extra={"check_id": run_id})
        finally:
            all_tasks = [task for task in (check_task, watcher) if task]
            pending = [task for task in all_tasks if not task.done()]
            for task in pending:
                task.cancel()
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            if "result" in locals():
                try:
                    with session_factory.begin() as session:
                        finish_check(session, run_id, lease_token, result, settings)
                except Exception:
                    logger.warning("Could not persist check result; lease recovery will fence it",
                                   extra={"check_id": run_id})

    while not stop_event.is_set():
        try:
            heartbeat(session_factory, "worker", {"active": len(tasks), "limit": settings.check_concurrency})
            while len(tasks) < settings.check_concurrency and not stop_event.is_set():
                with session_factory.begin() as session:
                    run = claim_check(session, settings)
                    if not run:
                        break
                    config = session.scalar(select(MonitorConfig).where(
                        MonitorConfig.monitor_id == run.monitor_id,
                        MonitorConfig.version == run.config_version))
                    payload = decrypt_payload(config.encrypted_payload, settings)
                    task_data = (run.id, run.lease_token, payload, config.timeout_ms)
                task = asyncio.create_task(execute(*task_data))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception:
            logger.warning("Worker cannot claim checks; no external request started")
        try:
            await _sleep(stop_event, 0.5)
        except asyncio.CancelledError:
            stop_event.set()
            break
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_background(session_factory, settings, stop_event=None):
    from api_watch.telegram import dispatch_once, poll_once

    stop_event = stop_event or asyncio.Event()

    async def scheduler():
        rounds = 0
        while not stop_event.is_set():
            try:
                with session_factory.begin() as session:
                    expire_leases(session, settings)
                    schedule_due(session, settings)
                    if rounds % 60 == 0:
                        cleanup(session, settings)
                heartbeat(session_factory, "scheduler")
            except Exception:
                logger.warning("Scheduler iteration failed; transaction rolled back")
            rounds += 1
            await _sleep(stop_event, 1)

    async def notifier():
        while not stop_event.is_set():
            try:
                await dispatch_once(session_factory, settings)
                heartbeat(session_factory, "notifier", {"configured": bool(settings.telegram_bot_token)})
            except Exception:
                logger.warning("Notification dispatch iteration failed")
            await _sleep(stop_event, 1)

    async def poller():
        while not stop_event.is_set():
            try:
                await poll_once(session_factory, settings)
                heartbeat(session_factory, "telegram_poller", {"configured": bool(settings.telegram_bot_token)})
            except Exception:
                logger.warning("Telegram polling iteration failed")
            await _sleep(stop_event, 1)

    await asyncio.gather(scheduler(), notifier(), poller())
