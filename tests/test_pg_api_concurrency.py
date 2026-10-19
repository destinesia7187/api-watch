"""Exercise API admission and credential races against real PostgreSQL locks.

Each test owns a newly generated schema. No scheduler, public network service,
wall-clock sleep, or shared application tables are involved. Set
TEST_DATABASE_URL to a disposable PostgreSQL database to enable this suite.
"""

import base64
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from api_watch import auth, monitors
from api_watch.app import create_app
from api_watch.config import Settings
from api_watch.db import utcnow
from api_watch.models import ApiToken, CheckRun, IdempotencyKey, LoginSession, Monitor, RateBucket, User
from api_watch.security import digest, password_hash

PASSWORD = "a long concurrency test password"
WAIT_SECONDS = 15


@dataclass
class PostgreSQLAccount:
    app: object
    user_id: str
    cookie: str
    csrf: str

    def browser(self, stack):
        """A separate ASGI portal per concurrent request prevents loop sharing."""
        client = stack.enter_context(TestClient(self.app))
        client.cookies.set("api_watch_session", self.cookie)
        client.headers["X-CSRF-Token"] = self.csrf
        return client


@pytest.fixture
def pg_account(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL enables the real PostgreSQL API concurrency suite")

    async def fixture_dns(host, port):
        assert host == "example.com"
        return ("127.0.0.1",)

    monkeypatch.setattr("api_watch.checker._lookup", fixture_dns)
    schema = f"api_race_{uuid4().hex}"
    # Only this locally generated identifier is ever interpolated into DDL.
    assert re.fullmatch(r"api_race_[0-9a-f]{32}", schema)
    admin = create_engine(url, pool_pre_ping=True)
    application = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        settings = Settings(
            app_env="test", database_url=url, database_schema=schema,
            public_base_url="http://testserver", session_secret="race-test" * 8,
            encryption_key=base64.urlsafe_b64encode(b"r" * 32).decode(),
            test_allowed_hosts="example.com", _env_file=None,
        )
        application = create_app(settings, initialize=True)

        @event.listens_for(application.state.engine, "connect")
        def bound_database_waits(connection, _):
            # A broken lock order should fail the test instead of hanging CI.
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SET statement_timeout = '25s'")
            connection.commit()

        application.state.engine.dispose()
        with TestClient(application) as client:
            credentials = {"email": "concurrency@example.com", "password": PASSWORD}
            registered = client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201, registered.text
            signed_in = client.post("/api/v1/auth/login", json=credentials)
            assert signed_in.status_code == 200, signed_in.text
            account = PostgreSQLAccount(
                application, signed_in.json()["user"]["id"],
                client.cookies.get("api_watch_session"), signed_in.json()["csrf_token"],
            )
        yield account
    finally:
        if application is not None:
            application.state.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def create_monitor(client):
    response = client.post("/api/v1/monitors", json={
        "name": "Concurrent monitor", "url": "https://example.com/health",
    })
    assert response.status_code == 201, response.text
    return response.json()


def concurrently(*operations):
    """Surface request exceptions, retaining bounded joins for debugging."""
    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(operation) for operation in operations]
        return [future.result(timeout=WAIT_SECONDS * 3) for future in futures]


def test_overlapping_same_key_run_now_returns_one_check_without_consuming_retry_limits(pg_account, monkeypatch):
    """Both fast-path lookups miss before either request may acquire its lock."""
    with ExitStack() as stack:
        first, second = [pg_account.browser(stack) for _ in range(2)]
        monitor = create_monitor(first)
        misses = Barrier(2, timeout=WAIT_SECONDS)
        original = Session.scalar
        seen_sessions = set()
        guard = Lock()

        def scalar_after_both_initial_misses(session, statement, *args, **kwargs):
            result = original(session, statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", ())
            if descriptions and descriptions[0].get("entity") is IdempotencyKey:
                with guard:
                    initial = session not in seen_sessions
                    seen_sessions.add(session)
                if initial:
                    assert result is None
                    misses.wait()
            return result

        monkeypatch.setattr(Session, "scalar", scalar_after_both_initial_misses)
        url = f"/api/v1/monitors/{monitor['id']}/run-now"
        headers = {"Idempotency-Key": str(uuid4())}
        admission_started = utcnow()
        responses = concurrently(
            lambda: first.post(url, headers=headers),
            lambda: second.post(url, headers=headers),
        )
        admission_finished = utcnow()
        monkeypatch.setattr(Session, "scalar", original)
        assert [response.status_code for response in responses] == [202, 202], [r.text for r in responses]
        payloads = [response.json() for response in responses]
        assert payloads[0] == payloads[1]
        assert payloads[0]["status"] == "queued"
        assert len(seen_sessions) == 2
        assert responses[0].headers["Location"] == responses[1].headers["Location"]

        with pg_account.app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(CheckRun)) == 1
            assert session.scalar(select(func.count()).select_from(IdempotencyKey)) == 1
            row = session.scalar(select(IdempotencyKey))
            assert row.check_id == payloads[0]["check_id"]
            for key, seconds in [
                (f"manual-user:{pg_account.user_id}", 60),
                (f"manual-monitor:{monitor['id']}", 10),
            ]:
                windows = range(int(admission_started.timestamp()) // seconds,
                                int(admission_finished.timestamp()) // seconds + 1)
                identifiers = [digest(f"{key}:{window}") for window in windows]
                consumed = session.scalar(select(func.sum(RateBucket.count)).where(RateBucket.id.in_(identifiers)))
                assert consumed == 1, "An idempotent retry must not consume another admission allowance"


def test_concurrent_patch_accepts_only_one_matching_expected_version(pg_account, monkeypatch):
    """Both writers reach the locking boundary with the same visible version."""
    with ExitStack() as stack:
        first, second = [pg_account.browser(stack) for _ in range(2)]
        monitor = create_monitor(first)
        writers = Barrier(2, timeout=WAIT_SECONDS)
        original = monitors.owned_monitor

        def synchronize_lock_boundary(db, monitor_id, owner_id, lock=False):
            if lock:
                writers.wait()
            return original(db, monitor_id, owner_id, lock=lock)

        monkeypatch.setattr(monitors, "owned_monitor", synchronize_lock_boundary)
        url = f"/api/v1/monitors/{monitor['id']}"
        responses = concurrently(
            lambda: first.patch(url, json={"name": "Writer A", "expected_version": monitor["record_version"]}),
            lambda: second.patch(url, json={"name": "Writer B", "expected_version": monitor["record_version"]}),
        )
        assert sorted(response.status_code for response in responses) == [200, 409], [r.text for r in responses]
        accepted = next(response.json() for response in responses if response.status_code == 200)
        rejected = next(response.json() for response in responses if response.status_code == 409)
        assert rejected["error"]["code"] == "VERSION_CONFLICT"
        assert accepted["record_version"] == monitor["record_version"] + 1
        with pg_account.app.state.session_factory() as session:
            persisted = session.get(Monitor, monitor["id"])
            assert persisted.name == accepted["name"]
            assert persisted.record_version == monitor["record_version"] + 1
            assert persisted.config_version == monitor["config_version"]


def test_session_revoked_while_waiting_for_credential_lock_cannot_create_token(pg_account, monkeypatch):
    """A request authenticated before password rotation must refresh its session."""
    reached_credential_lock = Event()
    original = auth.lock_live_session_user

    def observe_credential_lock(db, principal):
        reached_credential_lock.set()
        return original(db, principal)

    monkeypatch.setattr(auth, "lock_live_session_user", observe_credential_lock)
    with ExitStack() as stack:
        browser = pg_account.browser(stack)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pg_account.app.state.session_factory.begin() as session:
                user = session.scalar(select(User).where(User.id == pg_account.user_id)
                                      .with_for_update(key_share=True))
                future = executor.submit(browser.post, "/api/v1/api-tokens", json={"name": "Too late"})
                assert reached_credential_lock.wait(WAIT_SECONDS), "The API request never reached the user lock"
                # This is the atomic state change made by password rotation. The
                # request already passed principal(), and now waits on our lock.
                user.password_hash = password_hash("the replacement test password")
                auth.revoke_credentials(session, user.id)
            response = future.result(timeout=WAIT_SECONDS * 2)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"
        with pg_account.app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ApiToken)) == 0
            login = session.scalar(select(LoginSession).where(LoginSession.user_id == pg_account.user_id))
            assert login.revoked_at is not None


def test_concurrent_token_creation_cannot_exceed_five_active_tokens(pg_account, monkeypatch):
    with ExitStack() as stack:
        first, second = [pg_account.browser(stack) for _ in range(2)]
        for index in range(4):
            created = first.post("/api/v1/api-tokens", json={"name": f"Existing {index}"})
            assert created.status_code == 201, created.text
        contenders = Barrier(2, timeout=WAIT_SECONDS)
        original = auth.lock_live_session_user

        def synchronize_credential_lock(db, principal):
            contenders.wait()
            return original(db, principal)

        monkeypatch.setattr(auth, "lock_live_session_user", synchronize_credential_lock)
        responses = concurrently(
            lambda: first.post("/api/v1/api-tokens", json={"name": "Contender A"}),
            lambda: second.post("/api/v1/api-tokens", json={"name": "Contender B"}),
        )
        assert sorted(response.status_code for response in responses) == [201, 422], [r.text for r in responses]
        rejected = next(response for response in responses if response.status_code == 422)
        assert rejected.json()["error"]["code"] == "TOKEN_LIMIT"
        with pg_account.app.state.session_factory() as session:
            active = session.scalar(select(func.count()).select_from(ApiToken).where(
                ApiToken.user_id == pg_account.user_id, ApiToken.revoked_at.is_(None),
                ApiToken.expires_at > utcnow(),
            ))
            assert active == 5
