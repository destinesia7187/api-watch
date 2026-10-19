"""User-owned monitor management and read-only check/incident history."""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api_watch.api_utils import Principal, cursor_page, exact_response, get_db, principal, rate_limit
from api_watch.checker import TargetBlocked, TargetDNSError, json_equal, strict_json_loads, validate_target
from api_watch.db import database_now, utcnow
from api_watch.contracts import CheckRead, IncidentRead, ManualCheckRead, MonitorPatch, MonitorRead, Page, VersionRequest
from api_watch.errors import APIError
from api_watch.exact_json import ExactJSONRoute
from api_watch.models import CheckRun, IdempotencyKey, Incident, Monitor, MonitorConfig, Notification, User
from api_watch.schemas import HEADER_NAME, MonitorCreate, public_config
from api_watch.security import decrypt_payload, digest, encrypt_payload

router = APIRouter(prefix="/api/v1", tags=["Monitors"], route_class=ExactJSONRoute)


def owned_monitor(db, monitor_id, owner_id, lock=False):
    stmt = select(Monitor).where(Monitor.id == monitor_id, Monitor.owner_id == owner_id,
                                 Monitor.state != "deleted")
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(stmt)
    if not row:
        raise APIError(404, "NOT_FOUND", "Monitor not found.")
    return row


def configuration(db, monitor):
    return db.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == monitor.id,
                                                MonitorConfig.version == monitor.config_version))


def monitor_view(db, monitor, settings):
    config = configuration(db, monitor)
    if not config or not config.encrypted_payload:
        # A deletion can commit between the monitor and configuration reads.
        raise APIError(404, "NOT_FOUND", "Monitor not found.")
    public = public_config(decrypt_payload(config.encrypted_payload, settings))
    public.update({key: getattr(monitor, key) for key in (
        "id", "name", "description", "state", "record_version", "config_version", "health_status",
        "notifications_enabled", "next_due_at", "last_checked_at", "last_response_time_ms",
        "last_error_code", "failure_streak", "success_streak", "created_at")})
    public["enabled"] = monitor.state == "active"
    incident = db.scalar(select(Incident).where(Incident.monitor_id == monitor.id, Incident.status == "open"))
    public["open_incident_id"] = incident.id if incident else None
    public["busy"] = bool(db.scalar(select(CheckRun.id).where(
        CheckRun.monitor_id == monitor.id, CheckRun.status.in_(["queued", "running"]))))
    return public


async def preflight(config, settings):
    try:
        async with asyncio.timeout(5):
            await validate_target(config["url"], settings)
    except TargetBlocked:
        raise APIError(422, "TARGET_BLOCKED", "The target is not allowed by the outbound security policy.") from None
    except (TargetDNSError, TimeoutError):
        # A temporarily missing DNS record is an observable check failure, not invalid saved syntax.
        pass


async def raw_body(request):
    try:
        return strict_json_loads(await request.body(), exact_integers=False)
    except (ValueError, UnicodeError, RecursionError):
        raise APIError(400, "INVALID_JSON", "The request body must be unambiguous bounded UTF-8 JSON.") from None


def validate_config(raw):
    try:
        return MonitorCreate.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise APIError(422, "VALIDATION_ERROR", "Invalid monitor settings.",
            [{"field": ".".join(map(str, e["loc"])), "code": e["type"], "message": "Invalid value."} for e in exc.errors()]) from None


def save_config(db, monitor, raw, settings):
    row = MonitorConfig(monitor_id=monitor.id, version=monitor.config_version,
        encrypted_payload=encrypt_payload(raw, settings), key_id=settings.encryption_key_id,
        interval_seconds=raw["interval_seconds"], timeout_ms=raw["timeout_ms"],
        failure_threshold=raw["failure_threshold"], recovery_threshold=raw["recovery_threshold"])
    db.add(row)
    return row


def version_check(monitor, expected):
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != monitor.record_version:
        raise APIError(409, "VERSION_CONFLICT", "The monitor has changed. Reload it and try again.")


@router.get("/monitors", response_model=Page[MonitorRead])
def list_monitors(request: Request, limit: int = 20, cursor: str | None = None,
                  state: str | None = None, auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    statement = select(Monitor).where(Monitor.owner_id == auth.user.id, Monitor.state != "deleted")
    if state:
        if state not in {"active", "paused"}:
            raise APIError(422, "VALIDATION_ERROR", "state must be active or paused.")
        statement = statement.where(Monitor.state == state)
    rows, page = cursor_page(db, statement, Monitor, request, limit, cursor,
                             {"owner": auth.user.id, "resource": "monitors", "state": state})
    items = []
    for row in rows:
        try:
            items.append(monitor_view(db, row, request.app.state.settings))
        except APIError as exc:
            if exc.status != 404:
                raise
            # Omit an entry deleted after this page's selection. Its immutable
            # cursor position still advances, so later entries are not repeated.
    return exact_response({"items": items, **page})


@router.post("/monitors", status_code=201, response_model=MonitorRead)
async def create_monitor(body: MonitorCreate, request: Request, response: Response,
                         auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    raw = validate_config(await raw_body(request))
    await preflight(raw, settings)
    db.scalar(select(User).where(User.id == auth.user.id).with_for_update(key_share=True))
    count = db.scalar(select(func.count()).select_from(Monitor).where(
        Monitor.owner_id == auth.user.id, Monitor.state != "deleted"))
    if count >= settings.max_monitors:
        raise APIError(422, "MONITOR_LIMIT", f"The maximum is {settings.max_monitors} monitors per account.")
    monitor = Monitor(owner_id=auth.user.id, name=raw["name"], description=raw["description"],
        state="active" if raw["enabled"] else "paused", notifications_enabled=raw["notifications_enabled"],
        next_due_at=database_now(db) if raw["enabled"] else None)
    db.add(monitor)
    db.flush()
    save_config(db, monitor, raw, settings)
    db.commit()
    response.headers["Location"] = f"/api/v1/monitors/{monitor.id}"
    return exact_response(monitor_view(db, monitor, settings), 201, dict(response.headers))


@router.get("/monitors/{monitor_id}", response_model=MonitorRead)
def get_monitor(monitor_id: str, request: Request,
                auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    return exact_response(monitor_view(db, owned_monitor(db, monitor_id, auth.user.id), request.app.state.settings))


@router.patch("/monitors/{monitor_id}", response_model=MonitorRead)
async def patch_monitor(monitor_id: str, request: Request, body_model: MonitorPatch,
                        auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    from api_watch.engine import abort_incident

    body = await raw_body(request)
    if not isinstance(body, dict):
        raise APIError(422, "VALIDATION_ERROR", "A JSON object is required.")
    body = dict(body)
    expected = body.pop("expected_version", None)
    if "enabled" in body:
        raise APIError(422, "VALIDATION_ERROR", "Use the pause or resume endpoint to change enabled state.")
    monitor = owned_monitor(db, monitor_id, auth.user.id, lock=True)
    version_check(monitor, expected)
    settings = request.app.state.settings
    old = decrypt_payload(configuration(db, monitor).encrypted_payload, settings)
    merged = dict(old)
    if "secret_headers" in body:
        secrets = {entry["name"].lower(): entry for entry in old.get("secret_headers", [])}
        changes = body.pop("secret_headers")
        if not isinstance(changes, list):
            raise APIError(422, "VALIDATION_ERROR", "secret_headers must be an array.")
        seen = set()
        for change in changes:
            if (not isinstance(change, dict) or not isinstance(change.get("name"), str)
                    or not HEADER_NAME.fullmatch(change["name"])):
                raise APIError(422, "VALIDATION_ERROR", "A secret header operation needs a name.")
            key = change["name"].lower()
            if key in seen or set(change) - {"name", "value", "remove"}:
                raise APIError(422, "VALIDATION_ERROR", "Duplicate or unknown secret header operation.")
            seen.add(key)
            if change.get("remove") is True and "value" not in change:
                secrets.pop(key, None)
            elif "value" in change and "remove" not in change:
                secrets[key] = {"name": change["name"], "value": change["value"]}
            else:
                raise APIError(422, "VALIDATION_ERROR", "Use either value or remove=true for a secret header.")
        merged["secret_headers"] = list(secrets.values())
    if "auth" in body:
        change = body.pop("auth")
        if not isinstance(change, dict) or "type" not in change or set(change) - {"type", "token"}:
            raise APIError(422, "VALIDATION_ERROR", "Invalid auth settings.")
        if change.get("type") == "bearer" and "token" not in change:
            if old.get("auth", {}).get("type") != "bearer":
                raise APIError(422, "VALIDATION_ERROR", "A token is required when enabling Bearer authorization.")
            change = {**change, "token": old["auth"]["token"]}
        merged["auth"] = change
    merged.update(body)
    merged["name"] = body.get("name", monitor.name)
    merged["description"] = body.get("description", monitor.description)
    merged["notifications_enabled"] = body.get("notifications_enabled", monitor.notifications_enabled)
    validated = validate_config(merged)
    execution_keys = set(validated) - {"name", "description", "notifications_enabled", "enabled"}
    execution_change = any(not json_equal(validated.get(key), old.get(key)) for key in execution_keys)
    if execution_change:
        busy = db.scalar(select(CheckRun.id).where(CheckRun.monitor_id == monitor.id,
                                                  CheckRun.status.in_(["queued", "running"])))
        if monitor.state != "paused" or busy:
            raise APIError(409, "MONITOR_MUST_BE_IDLE_AND_PAUSED", "Pause the monitor before changing request settings.")
    if execution_change:
        await preflight(validated, settings)
        abort_incident(db, monitor, "configuration_changed", settings)
        monitor.config_version += 1
        monitor.failure_streak = monitor.success_streak = 0
        monitor.streak_started_at = monitor.recovery_started_at = None
        monitor.last_applied_scheduled_at = None
        monitor.health_status = "unknown"
        save_config(db, monitor, validated, settings)
    monitor.name = validated["name"]
    monitor.description = validated["description"]
    if monitor.notifications_enabled and not validated["notifications_enabled"]:
        for notification in db.scalars(select(Notification).where(Notification.monitor_id == monitor.id,
                Notification.status.in_(["pending", "sending"]))):
            notification.status = "cancelled"
            notification.lease_token = None
            notification.lease_expires_at = None
            notification.next_attempt_at = database_now(db)
    monitor.notifications_enabled = validated["notifications_enabled"]
    monitor.record_version += 1
    db.commit()
    return exact_response(monitor_view(db, monitor, settings))


@router.post("/monitors/{monitor_id}/pause", response_model=MonitorRead)
async def pause_monitor(monitor_id: str, request: Request, body_model: VersionRequest,
                        auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    from api_watch.engine import cancel_monitor_runs
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"expected_version"}:
        raise APIError(422, "VALIDATION_ERROR", "Only expected_version is required.")
    monitor = owned_monitor(db, monitor_id, auth.user.id, lock=True)
    version_check(monitor, body["expected_version"])
    if monitor.state != "paused":
        monitor.state = "paused"
        monitor.next_due_at = None
        cancel_monitor_runs(db, monitor, "monitor_paused")
        monitor.failure_streak = monitor.success_streak = 0
        monitor.health_status = "unknown"
        monitor.record_version += 1
        db.commit()
    return exact_response(monitor_view(db, monitor, request.app.state.settings))


@router.post("/monitors/{monitor_id}/resume", response_model=MonitorRead)
async def resume_monitor(monitor_id: str, request: Request, body_model: VersionRequest,
                         auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"expected_version"}:
        raise APIError(422, "VALIDATION_ERROR", "Only expected_version is required.")
    monitor = owned_monitor(db, monitor_id, auth.user.id, lock=True)
    version_check(monitor, body["expected_version"])
    if monitor.state != "active":
        monitor.state = "active"
        monitor.next_due_at = database_now(db)
        previous_slot = db.scalar(select(func.max(CheckRun.scheduled_at)).where(CheckRun.monitor_id == monitor.id))
        if previous_slot and previous_slot >= monitor.next_due_at:
            monitor.next_due_at = previous_slot + timedelta(microseconds=1)
        monitor.failure_streak = monitor.success_streak = 0
        monitor.streak_started_at = monitor.recovery_started_at = None
        monitor.health_status = "unknown"
        monitor.record_version += 1
        db.commit()
    return exact_response(monitor_view(db, monitor, request.app.state.settings))


@router.delete("/monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: str, request: Request, expected_version: int,
                   auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    from api_watch.engine import abort_incident, cancel_monitor_runs
    monitor = owned_monitor(db, monitor_id, auth.user.id, lock=True)
    version_check(monitor, expected_version)
    cancel_monitor_runs(db, monitor, "monitor_deleted")
    abort_incident(db, monitor, "monitor_deleted", request.app.state.settings)
    monitor.state = "deleted"
    monitor.deleted_at = utcnow()
    monitor.next_due_at = None
    monitor.record_version += 1
    for config in db.scalars(select(MonitorConfig).where(MonitorConfig.monitor_id == monitor.id)):
        config.encrypted_payload = None
        config.secrets_purged_at = utcnow()
    db.commit()


@router.post("/monitors/{monitor_id}/run-now", status_code=202, response_model=ManualCheckRead)
async def run_now(monitor_id: str, request: Request, response: Response,
                  idempotency_key: str = Header(min_length=8, max_length=128),
                  auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    if await request.body() not in {b"", b"{}"}:
        raise APIError(422, "VALIDATION_ERROR", "This endpoint does not accept a request body.")
    owned_monitor(db, monitor_id, auth.user.id)
    key_hash = digest(idempotency_key)
    now = database_now(db)
    previous = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == auth.user.id,
        IdempotencyKey.monitor_id == monitor_id, IdempotencyKey.key_hash == key_hash,
        IdempotencyKey.expires_at > now))
    if previous:
        response.headers["Location"] = f"/api/v1/checks/{previous.check_id}"
        run = db.get(CheckRun, previous.check_id)
        return {"check_id": previous.check_id, "trigger": "manual", "status": run.status if run else "finished"}
    monitor = owned_monitor(db, monitor_id, auth.user.id, lock=True)
    # Re-check after the monitor lock to serialize concurrent idempotent requests.
    previous = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == auth.user.id,
        IdempotencyKey.monitor_id == monitor_id, IdempotencyKey.key_hash == key_hash))
    if previous and previous.expires_at > now:
        response.headers["Location"] = f"/api/v1/checks/{previous.check_id}"
        run = db.get(CheckRun, previous.check_id)
        return {"check_id": previous.check_id, "trigger": "manual", "status": run.status if run else "finished"}
    # Keep admission and the idempotency recheck in the same monitor transaction.
    # A concurrent retry must return its original check without consuming limits.
    rate_limit(db, f"manual-user:{auth.user.id}", 5, 60, commit=False)
    rate_limit(db, f"manual-monitor:{monitor_id}", 1, 10, commit=False)
    if previous:
        db.delete(previous)
        db.flush()
    if monitor.state != "active":
        raise APIError(409, "MONITOR_PAUSED", "Resume the monitor before running a diagnostic check.")
    if db.scalar(select(CheckRun.id).where(CheckRun.monitor_id == monitor_id,
                                          CheckRun.status.in_(["queued", "running"]))):
        raise APIError(409, "MONITOR_BUSY", "A check is already queued or running.")
    run = CheckRun(monitor_id=monitor_id, config_version=monitor.config_version, trigger="manual")
    db.add(run)
    db.flush()
    db.add(IdempotencyKey(user_id=auth.user.id, monitor_id=monitor_id, key_hash=key_hash,
        request_hash=digest("{}"), check_id=run.id, expires_at=now + timedelta(hours=24)))
    db.commit()
    response.headers["Location"] = f"/api/v1/checks/{run.id}"
    return {"check_id": run.id, "trigger": "manual", "status": run.status}


def check_view(row):
    return {key: getattr(row, key) for key in (
        "id", "monitor_id", "config_version", "trigger", "created_at", "scheduled_at", "started_at",
        "finished_at", "status", "outcome", "http_status", "response_time_ms", "elapsed_ms", "bytes_received",
        "error_code", "error_message", "failed_assertions")}


@router.get("/monitors/{monitor_id}/checks", tags=["Checks"], response_model=Page[CheckRead])
def list_checks(monitor_id: str, request: Request, limit: int = 20, cursor: str | None = None,
                trigger: str | None = None, outcome: str | None = None, config_version: int | None = None,
                from_time: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    owned_monitor(db, monitor_id, auth.user.id)
    stmt = select(CheckRun).where(CheckRun.monitor_id == monitor_id)
    if trigger:
        if trigger not in {"manual", "scheduled"}:
            raise APIError(422, "VALIDATION_ERROR", "Invalid trigger filter.")
        stmt = stmt.where(CheckRun.trigger == trigger)
    if outcome:
        if outcome not in {"success", "failure", "blocked", "interrupted", "cancelled", "skipped"}:
            raise APIError(422, "VALIDATION_ERROR", "Invalid outcome filter.")
        stmt = stmt.where(CheckRun.outcome == outcome)
    if config_version is not None:
        stmt = stmt.where(CheckRun.config_version == config_version)
    event_time = func.coalesce(CheckRun.started_at, CheckRun.scheduled_at, CheckRun.created_at)
    for value in [from_time, to]:
        if value and value.tzinfo is None:
            raise APIError(422, "VALIDATION_ERROR", "Dates must include a timezone.")
    if from_time:
        stmt = stmt.where(event_time >= from_time)
    if to:
        stmt = stmt.where(event_time < to)
    filters = {"owner": auth.user.id, "monitor": monitor_id, "trigger": trigger, "outcome": outcome,
               "config_version": config_version, "from": from_time, "to": to}
    rows, page = cursor_page(db, stmt, CheckRun, request, limit, cursor, filters)
    return {"items": [check_view(row) for row in rows], **page}


@router.get("/checks/{check_id}", tags=["Checks"], response_model=CheckRead)
def get_check(check_id: str, auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    row = db.scalar(select(CheckRun).join(Monitor).where(CheckRun.id == check_id,
        Monitor.owner_id == auth.user.id, Monitor.state != "deleted"))
    if not row:
        raise APIError(404, "NOT_FOUND", "Check not found.")
    return check_view(row)


def incident_view(row, monitor_name=None):
    result = {key: getattr(row, key) for key in (
        "id", "monitor_id", "config_version", "status", "started_at", "detected_at", "recovery_started_at",
        "resolved_at", "ended_at", "first_check_id", "confirming_check_id", "last_check_id", "reason", "created_at")}
    result["monitor_name"] = monitor_name
    end = row.resolved_at or row.ended_at or utcnow()
    result["duration_seconds"] = max(0, (end - row.started_at).total_seconds())
    return result


@router.get("/incidents", tags=["Incidents"], response_model=Page[IncidentRead])
def list_incidents(request: Request, limit: int = 20, cursor: str | None = None,
                   monitor_id: str | None = None, status: str | None = None,
                   from_time: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                   auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    stmt = select(Incident).join(Monitor).where(Monitor.owner_id == auth.user.id, Monitor.state != "deleted")
    if monitor_id:
        owned_monitor(db, monitor_id, auth.user.id)
        stmt = stmt.where(Incident.monitor_id == monitor_id)
    if status:
        if status not in {"open", "resolved", "aborted"}:
            raise APIError(422, "VALIDATION_ERROR", "Invalid incident status.")
        stmt = stmt.where(Incident.status == status)
    for value in [from_time, to]:
        if value and value.tzinfo is None:
            raise APIError(422, "VALIDATION_ERROR", "Dates must include a timezone.")
    if from_time:
        stmt = stmt.where((func.coalesce(Incident.resolved_at, Incident.ended_at) > from_time) |
                          (Incident.status == "open"))
    if to:
        stmt = stmt.where(Incident.started_at < to)
    rows, page = cursor_page(db, stmt, Incident, request, limit, cursor,
        {"owner": auth.user.id, "monitor": monitor_id, "status": status, "from": from_time, "to": to})
    items = []
    for row in rows:
        monitor = db.get(Monitor, row.monitor_id)
        if monitor is not None and monitor.state != "deleted":
            items.append(incident_view(row, monitor.name))
    return {"items": items, **page}


@router.get("/incidents/{incident_id}", tags=["Incidents"], response_model=IncidentRead)
def get_incident(incident_id: str, auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    row = db.scalar(select(Incident).join(Monitor).where(Incident.id == incident_id,
        Monitor.owner_id == auth.user.id, Monitor.state != "deleted"))
    if not row:
        raise APIError(404, "NOT_FOUND", "Incident not found.")
    monitor = db.get(Monitor, row.monitor_id)
    if monitor is None or monitor.state == "deleted":
        raise APIError(404, "NOT_FOUND", "Incident not found.")
    return incident_view(row, monitor.name)
