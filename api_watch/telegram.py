"""Telegram linking and durable notification delivery through the fixed Bot API.

Only safe event snapshots enter the outbox. Chat identifiers remain encrypted;
link codes are one-use hashes. No bot URL, response description or credentials
are ever copied into an error log or user-facing delivery error.
"""

import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from api_watch.db import database_now
from api_watch.models import (
    Monitor, MonitorConfig, Notification, PollState, TelegramConnection, TelegramLink, User,
)
from api_watch.security import decrypt_payload, digest, encrypt_payload

TERMINAL = ("sent", "failed", "suppressed", "cancelled")
_TOKEN = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{20,}$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _now(session, now):
    return now if now is not None else database_now(session)


def configured(settings):
    return bool(_TOKEN.fullmatch(settings.telegram_bot_token))


def _domain(session, monitor, settings):
    config = session.scalar(select(MonitorConfig).where(
        MonitorConfig.monitor_id == monitor.id, MonitorConfig.version == monitor.config_version))
    if not config or not config.encrypted_payload:
        return "unavailable"
    try:
        payload = decrypt_payload(config.encrypted_payload, settings)
        return urlsplit(payload["url"]).hostname or "unavailable"
    except Exception:
        return "unavailable"


def enqueue_event(session, monitor, incident, event_type, settings, now=None):
    """Stage one incident event in the caller's incident transaction."""
    if not monitor.notifications_enabled:
        return None
    now = _now(session, now)
    existing = session.scalar(select(Notification).where(
        Notification.incident_id == incident.id, Notification.event_type == event_type))
    if existing:
        return existing
    connection = session.scalar(select(TelegramConnection).where(
        TelegramConnection.user_id == monitor.owner_id,
        TelegramConnection.status == "connected"))
    # A strict monotonic per-monitor event timestamp also orders simultaneous transitions.
    latest = session.scalar(select(func.max(Notification.created_at)).where(
        Notification.monitor_id == monitor.id))
    created_at = max(now, latest + timedelta(microseconds=1)) if latest else now
    payload = {
        "monitor_name": monitor.name[:100], "domain": _domain(session, monitor, settings),
        "incident_id": incident.id, "event": event_type, "time_utc": now.isoformat(),
        "reason": incident.reason or "", "duration_seconds": None,
    }
    end = incident.resolved_at or incident.ended_at
    if end:
        payload["duration_seconds"] = max(0, int((end - incident.started_at).total_seconds()))
    row = Notification(
        owner_id=monitor.owner_id, monitor_id=monitor.id, incident_id=incident.id,
        event_type=event_type, generation=connection.generation if connection else None,
        encrypted_chat_id=connection.encrypted_chat_id if connection else None,
        payload=payload, status="pending" if connection else "suppressed",
        error_code=None if connection else "integration_not_connected",
        error_message=None if connection else "Telegram is not connected.",
        attempts=0, next_attempt_at=now, created_at=created_at)
    session.add(row)
    return row


async def _telegram_request(method, payload, settings):
    """Narrow fixed-origin adapter; never expose Telegram's raw error text."""
    if method not in {"sendMessage", "getUpdates"} or not configured(settings):
        return 0, {"ok": False}
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=15)) as client:
            async with client.post(url, json=payload, allow_redirects=False) as response:
                raw = await response.content.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    return 502, {"ok": False}
                import json
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = {"ok": False}
                return response.status, body if isinstance(body, dict) else {"ok": False}
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return 0, {"ok": False}


def _render(notification):
    payload = notification.payload or {}
    labels = {
        "incident_opened": "API Watch: incident opened",
        "incident_resolved": "API Watch: service recovered",
        "incident_aborted": "API Watch: observation ended without confirmed recovery",
        "test_notification": "API Watch: test notification",
    }
    lines = [labels.get(notification.event_type, "API Watch notification")]
    for key, label in (("monitor_name", "Monitor"), ("domain", "Host"),
                       ("incident_id", "Incident"), ("time_utc", "Time (UTC)"), ("reason", "Reason")):
        if payload.get(key):
            # No parse_mode: text cannot become Telegram markup or executable links.
            value = str(payload[key]).replace("\r", " ").replace("\n", " ")[:300]
            lines.append(f"{label}: {value}")
    if payload.get("duration_seconds") is not None:
        lines.append(f"Observed incident duration: {payload['duration_seconds']} seconds")
    return "\n".join(lines)[:3500]


def _cancel(row, code, now):
    row.status, row.error_code, row.error_message = "cancelled", code, "Delivery was cancelled."
    row.lease_token = row.lease_expires_at = None
    row.next_attempt_at = now


def _claim(session, now):
    # A prior terminal event unlocks the next one, including failures/suppression.
    predecessor = aliased(Notification)
    earlier = select(predecessor.id).where(
        predecessor.monitor_id == Notification.monitor_id,
        or_(predecessor.created_at < Notification.created_at,
            and_(predecessor.created_at == Notification.created_at, predecessor.id < Notification.id)),
        ~predecessor.status.in_(TERMINAL)).exists()
    rows = session.scalars(select(Notification).where(
        or_(and_(Notification.status == "pending", Notification.next_attempt_at <= now),
            and_(Notification.status == "sending", Notification.lease_expires_at <= now)),
        ~earlier).order_by(Notification.created_at, Notification.id)
        .limit(20).with_for_update(skip_locked=True)).all()
    for row in rows:
        connection = session.scalar(select(TelegramConnection).where(
            TelegramConnection.user_id == row.owner_id,
            TelegramConnection.status == "connected"))
        monitor = session.get(Monitor, row.monitor_id) if row.monitor_id else None
        if not connection or connection.generation != row.generation:
            _cancel(row, "connection_changed", now)
            continue
        if monitor and not monitor.notifications_enabled and row.event_type != "test_notification":
            _cancel(row, "notifications_disabled", now)
            continue
        if row.attempts >= 6:
            row.status, row.error_code = "failed", "delivery_attempts_exhausted"
            row.error_message = "The maximum number of delivery attempts was reached."
            row.lease_token = row.lease_expires_at = None
            row.next_attempt_at = now
            continue
        row.status, row.lease_token = "sending", str(uuid4())
        row.lease_expires_at = now + timedelta(seconds=45)
        row.attempts += 1
        session.flush()
        return row
    return None


def _delivery_result(session, notification_id, lease_token, status, body, now):
    row = session.scalar(select(Notification).where(Notification.id == notification_id).with_for_update())
    if (not row or row.status != "sending" or row.lease_token != lease_token
            or row.lease_expires_at <= now):
        return False
    connection = session.scalar(select(TelegramConnection).where(
        TelegramConnection.user_id == row.owner_id, TelegramConnection.status == "connected"))
    if not connection or connection.generation != row.generation:
        _cancel(row, "connection_changed", now)
        return False
    row.lease_token = row.lease_expires_at = None
    row.next_attempt_at = now
    error_status = body.get("error_code", status)
    if status == 200 and body.get("ok") is True:
        row.status, row.sent_at = "sent", now
        row.error_code = row.error_message = None
    elif error_status == 429 or status == 0 or status >= 500:
        row.error_code = "telegram_rate_limited" if error_status == 429 else "telegram_unavailable"
        row.error_message = "Telegram rate limited delivery." if error_status == 429 else "Telegram is unavailable."
        if row.attempts >= 6:
            row.status = "failed"
        else:
            row.status = "pending"
            delay = 30 * 2 ** (row.attempts - 1)
            delay *= 0.9 + secrets.randbelow(201) / 1000
            parameters = body.get("parameters")
            retry_after = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
            if isinstance(retry_after, (int, float)) and 0 < retry_after <= 365 * 86400:
                delay = max(delay, retry_after)
            row.next_attempt_at = now + timedelta(seconds=delay)
    else:
        row.status, row.error_code = "failed", "telegram_forbidden" if error_status == 403 else "telegram_rejected"
        row.error_message = "The bot cannot send to this chat." if error_status == 403 else "Telegram rejected delivery."
    return True


async def dispatch_once(session_factory, settings):
    if not configured(settings):
        return False
    with session_factory.begin() as session:
        row = _claim(session, database_now(session))
        if not row:
            return False
        notification_id, token = row.id, row.lease_token
        try:
            chat = decrypt_payload(row.encrypted_chat_id, settings)
            chat_id = chat.get("chat_id") if isinstance(chat, dict) else chat
            if not isinstance(chat_id, (str, int)):
                raise ValueError("Invalid encrypted chat identifier")
        except Exception:
            row.status, row.error_code, row.error_message = "failed", "invalid_chat", "Chat data cannot be decrypted."
            row.lease_token = row.lease_expires_at = None
            return False
        payload = {"chat_id": chat_id, "text": _render(row), "link_preview_options": {"is_disabled": True}}
    status, body = await _telegram_request("sendMessage", payload, settings)
    with session_factory.begin() as session:
        _delivery_result(session, notification_id, token, status, body, database_now(session))
    return True


def _apply_update(session, update, settings, now):
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    chat, sender = message.get("chat", {}), message.get("from", {})
    if (chat.get("type") != "private" or not isinstance(chat.get("id"), int)
            or chat["id"] <= 0 or sender.get("id") != chat["id"]):
        return False
    value = message.get("text", "")
    if not isinstance(value, str):
        return False
    parts = value.split()
    if len(parts) != 2 or parts[0].split("@")[0] != "/start" or not _CODE.fullmatch(parts[1]):
        return False
    candidate = session.scalar(select(TelegramLink).where(
        TelegramLink.token_hash == digest(parts[1]), TelegramLink.used_at.is_(None),
        TelegramLink.expires_at > now))
    if not candidate:
        return False
    # Match API link/disconnect lock order. NO KEY UPDATE serializes account
    # operations while allowing unrelated foreign-key KEY SHARE checks.
    user = session.scalar(select(User).where(User.id == candidate.user_id)
                          .with_for_update(key_share=True))
    if not user:
        return False
    link = session.scalar(select(TelegramLink).where(
        TelegramLink.id == candidate.id, TelegramLink.used_at.is_(None),
        TelegramLink.expires_at > now).with_for_update().execution_options(populate_existing=True))
    if not link:
        return False
    # A confirmed private chat belongs to one account at a time. The poller lock
    # serializes this check; chat identifiers never need a searchable plaintext column.
    for existing in session.scalars(select(TelegramConnection).where(
            TelegramConnection.status == "connected", TelegramConnection.user_id != link.user_id)).all():
        try:
            stored = decrypt_payload(existing.encrypted_chat_id, settings)
            stored_id = stored.get("chat_id") if isinstance(stored, dict) else stored
            if str(stored_id) == str(chat["id"]):
                return False
        except Exception:
            continue
    connection = session.scalar(select(TelegramConnection).where(
        TelegramConnection.user_id == link.user_id).with_for_update())
    if not connection:
        connection = TelegramConnection(user_id=link.user_id)
        session.add(connection)
    connection.encrypted_chat_id = encrypt_payload({"chat_id": chat["id"]}, settings)
    connection.generation, connection.status, connection.created_at = str(uuid4()), "connected", now
    link.used_at = now
    # Reissuing or consuming a code revokes all other outstanding link codes.
    for other in session.scalars(select(TelegramLink).where(
            TelegramLink.user_id == link.user_id, TelegramLink.used_at.is_(None))).all():
        other.used_at = now
    for row in session.scalars(select(Notification).where(
            Notification.owner_id == link.user_id, Notification.status.in_(("pending", "sending")))).all():
        _cancel(row, "connection_changed", now)
    return True


async def poll_once(session_factory, settings):
    if not configured(settings):
        return False
    from api_watch.engine import service_lock

    async with service_lock(session_factory, "telegram-poller") as acquired:
        if not acquired:
            return False
        with session_factory.begin() as session:
            state = session.get(PollState, 1)
            if not state:
                state = PollState(id=1, offset=0)
                session.add(state)
            offset = state.offset
        status, body = await _telegram_request("getUpdates", {
            "offset": offset, "timeout": 10, "limit": 100, "allowed_updates": ["message"]}, settings)
        if status != 200 or body.get("ok") is not True or not isinstance(body.get("result"), list):
            return False
        updates = [item for item in body["result"] if isinstance(item, dict)
                   and isinstance(item.get("update_id"), int)]
        with session_factory.begin() as session:
            state = session.scalar(select(PollState).where(PollState.id == 1).with_for_update())
            now = database_now(session)
            for update in sorted(updates, key=lambda item: item["update_id"]):
                if update["update_id"] < state.offset:
                    continue
                _apply_update(session, update, settings, now)
                state.offset = update["update_id"] + 1
        return bool(updates)
