"""No Telegram credentials or live Telegram traffic are needed for these tests."""

import base64
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from api_watch import telegram
from api_watch.config import Settings
from api_watch.db import Base, database_now, make_database
from api_watch.models import (
    Incident, Monitor, MonitorConfig, Notification, PollState, TelegramConnection,
    TelegramLink, User,
)
from api_watch.security import decrypt_payload, digest, encrypt_payload, random_token


@pytest.fixture
def telegram_database():
    settings = Settings(app_env="test", database_url="sqlite:///:memory:",
        encryption_key=base64.urlsafe_b64encode(b"t" * 32).decode(), session_secret="test" * 12,
        telegram_bot_token="123456:" + "t" * 32, telegram_bot_username="api_watch_test_bot", _env_file=None)
    engine, factory = make_database(settings)
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        now = database_now(session)
        user = User(email=f"{uuid4()}@example.com", password_hash="fixture")
        session.add(user)
        session.flush()
        monitor = Monitor(owner_id=user.id, name="Unsafe <b>markup</b>", notifications_enabled=True)
        session.add(monitor)
        session.flush()
        session.add(MonitorConfig(monitor_id=monitor.id, version=1,
            encrypted_payload=encrypt_payload({"url": "https://example.com/private?token=SECRET"}, settings)))
        incident = Incident(monitor_id=monitor.id, config_version=1, status="open",
                            started_at=now, detected_at=now, reason="timeout")
        session.add(incident)
        session.flush()
        ids = user.id, monitor.id, incident.id
    yield factory, settings, ids
    engine.dispose()


def connect(factory, settings, user_id):
    with factory.begin() as session:
        row = TelegramConnection(user_id=user_id,
                                 encrypted_chat_id=encrypt_payload({"chat_id": 12345}, settings))
        session.add(row)
        session.flush()
        return row.generation


def enqueue(factory, settings, ids, event="incident_opened"):
    with factory.begin() as session:
        row = telegram.enqueue_event(session, session.get(Monitor, ids[1]), session.get(Incident, ids[2]),
                                     event, settings)
        session.flush()
        return row.id


def test_outbox_suppression_and_safe_snapshot(telegram_database):
    factory, settings, ids = telegram_database
    row_id = enqueue(factory, settings, ids)
    assert enqueue(factory, settings, ids) == row_id
    with factory() as session:
        row = session.get(Notification, row_id)
        assert row.status == "suppressed" and row.error_code == "integration_not_connected"
        assert row.payload["domain"] == "example.com"
        assert "SECRET" not in str(row.payload) and "private" not in str(row.payload)
    connect(factory, settings, ids[0])
    # Connecting later does not revive old events.
    with factory() as session:
        assert session.get(Notification, row_id).status == "suppressed"


async def test_dispatch_preserves_plain_text_and_sent_state(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    connect(factory, settings, ids[0])
    row_id = enqueue(factory, settings, ids)
    sent = []

    async def adapter(method, payload, _settings):
        assert method == "sendMessage"
        sent.append(payload)
        return 200, {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(telegram, "_telegram_request", adapter)
    assert await telegram.dispatch_once(factory, settings)
    assert not await telegram.dispatch_once(factory, settings)
    assert len(sent) == 1 and "parse_mode" not in sent[0]
    assert sent[0]["chat_id"] == 12345 and "<b>markup</b>" in sent[0]["text"]
    assert "SECRET" not in sent[0]["text"]
    with factory() as session:
        row = session.get(Notification, row_id)
        assert row.status == "sent" and row.attempts == 1 and row.sent_at
        assert not row.lease_token


async def test_retry_after_six_attempt_bound_and_no_raw_errors(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    connect(factory, settings, ids[0])
    row_id = enqueue(factory, settings, ids)
    responses = [(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1000},
                         "description": "RAW_SECRET"})] + [(503, {"ok": False})] * 5

    async def adapter(*args):
        return responses.pop(0)

    monkeypatch.setattr(telegram, "_telegram_request", adapter)
    for attempt in range(1, 7):
        assert await telegram.dispatch_once(factory, settings)
        with factory.begin() as session:
            now = database_now(session)
            row = session.get(Notification, row_id)
            assert row.attempts == attempt
            assert "RAW_SECRET" not in (row.error_message or "")
            if attempt == 1:
                assert row.next_attempt_at >= now + timedelta(seconds=999)
            if attempt < 6:
                assert row.status == "pending"
                row.next_attempt_at = now - timedelta(seconds=1)
            else:
                assert row.status == "failed"
    assert not await telegram.dispatch_once(factory, settings)


async def test_terminal_predecessors_unlock_order_including_permanent_failure(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    connect(factory, settings, ids[0])
    opened = enqueue(factory, settings, ids)
    resolved = enqueue(factory, settings, ids, "incident_resolved")
    with factory.begin() as session:
        row = session.get(Notification, opened)
        row.next_attempt_at = database_now(session) + timedelta(hours=1)
    calls = []

    async def adapter(method, payload, _settings):
        calls.append(payload["text"])
        return (403, {"ok": False, "error_code": 403}) if len(calls) == 1 else (200, {"ok": True})

    monkeypatch.setattr(telegram, "_telegram_request", adapter)
    assert not await telegram.dispatch_once(factory, settings)
    with factory.begin() as session:
        session.get(Notification, opened).next_attempt_at = database_now(session)
    assert await telegram.dispatch_once(factory, settings)
    assert await telegram.dispatch_once(factory, settings)
    assert "incident opened" in calls[0] and "service recovered" in calls[1]
    with factory() as session:
        assert session.get(Notification, opened).status == "failed"
        assert session.get(Notification, resolved).status == "sent"


async def test_generation_changes_cancel_old_recipient_even_during_send(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    connect(factory, settings, ids[0])
    row_id = enqueue(factory, settings, ids)

    async def adapter(*args):
        with factory.begin() as session:
            connection = session.scalar(select(TelegramConnection))
            connection.generation = str(uuid4())
        return 200, {"ok": True}

    monkeypatch.setattr(telegram, "_telegram_request", adapter)
    assert await telegram.dispatch_once(factory, settings)
    with factory() as session:
        assert session.get(Notification, row_id).status == "cancelled"


def test_expired_delivery_lease_can_retry_but_late_ack_is_fenced(telegram_database):
    factory, settings, ids = telegram_database
    connect(factory, settings, ids[0])
    row_id = enqueue(factory, settings, ids)
    with factory.begin() as session:
        now = database_now(session)
        row = telegram._claim(session, now)
        old_token = row.lease_token
    with factory.begin() as session:
        row = telegram._claim(session, now + timedelta(seconds=46))
        new_token = row.lease_token
        assert old_token != new_token and row.attempts == 2
        assert not telegram._delivery_result(session, row_id, old_token, 200, {"ok": True},
                                             now + timedelta(seconds=47))
        assert telegram._delivery_result(session, row_id, new_token, 200, {"ok": True},
                                         now + timedelta(seconds=47))


def add_link(factory, user_id, expires_delta=600):
    token = random_token()
    with factory.begin() as session:
        session.add(TelegramLink(user_id=user_id, token_hash=digest(token),
                                expires_at=database_now(session) + timedelta(seconds=expires_delta)))
    return token


def update(token, update_id=1, chat_type="private", chat_id=12345):
    return {"update_id": update_id, "message": {
        "text": f"/start {token}", "chat": {"id": chat_id, "type": chat_type}, "from": {"id": chat_id}}}


def test_link_is_private_one_use_expiring_and_encrypts_chat(telegram_database):
    factory, settings, ids = telegram_database
    token = add_link(factory, ids[0])
    expired = add_link(factory, ids[0], -1)
    with factory.begin() as session:
        now = database_now(session)
        assert not telegram._apply_update(session, update(token, chat_type="group"), settings, now)
        assert not telegram._apply_update(session, update(expired), settings, now)
        assert telegram._apply_update(session, update(token), settings, now)
        session.flush()
        assert not telegram._apply_update(session, update(token), settings, now)
    with factory() as session:
        connection = session.scalar(select(TelegramConnection))
        assert "12345" not in connection.encrypted_chat_id
        assert decrypt_payload(connection.encrypted_chat_id, settings) == {"chat_id": 12345}
        assert session.scalar(select(TelegramLink).where(TelegramLink.token_hash == digest(token))).used_at


async def test_poller_persists_offset_and_replayed_update_does_not_relink(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    token = add_link(factory, ids[0])
    offsets = []

    async def adapter(method, payload, _settings):
        assert method == "getUpdates"
        offsets.append(payload["offset"])
        return 200, {"ok": True, "result": [update(token, 101)]}

    monkeypatch.setattr(telegram, "_telegram_request", adapter)
    assert await telegram.poll_once(factory, settings)
    with factory() as session:
        generation = session.scalar(select(TelegramConnection.generation))
    assert await telegram.poll_once(factory, settings)
    with factory() as session:
        assert session.get(PollState, 1).offset == 102
        assert session.scalar(select(TelegramConnection.generation)) == generation
    assert offsets == [0, 102]


async def test_missing_bot_configuration_is_explicitly_inactive(telegram_database, monkeypatch):
    factory, settings, ids = telegram_database
    settings.telegram_bot_token = ""

    async def no_network(*args):
        raise AssertionError("Disabled integration must not contact Telegram")

    monkeypatch.setattr(telegram, "_telegram_request", no_network)
    assert not await telegram.dispatch_once(factory, settings)
    assert not await telegram.poll_once(factory, settings)
