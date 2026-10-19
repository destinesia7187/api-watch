"""Boundary and revocation guarantees for browser sessions and credentials."""

from contextlib import ExitStack
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api_watch.db import utcnow
from api_watch.models import ApiToken, EmailMessage, LoginSession, Notification, PasswordReset, User
from api_watch.security import decrypt_payload, digest

PASSWORD = "a long test password"
NEW_PASSWORD = "a different lifecycle test password"


def delivered_reset_token(browser, app):
    requested = browser.post("/api/v1/auth/password/forgot", json={"email": "owner@example.com"})
    assert requested.status_code == 202, requested.text
    with app.state.session_factory() as session:
        message = session.scalar(select(EmailMessage).order_by(EmailMessage.created_at.desc(), EmailMessage.id.desc()))
        payload = decrypt_payload(message.encrypted_payload, app.state.settings)
    token = payload["text"].split("#reset=", 1)[1].split()[0]
    assert token not in requested.text
    return token


@pytest.mark.parametrize("boundary", ["absolute", "idle"])
def test_session_absolute_and_idle_expiry_boundaries(signed_in, app, monkeypatch, boundary):
    """The exact expiration instant fails; one microsecond inside remains valid."""
    now = utcnow()
    monkeypatch.setattr("api_watch.api_utils.utcnow", lambda: now)
    session_hash = digest(signed_in.cookies.get("api_watch_session"))

    def set_boundary(inside):
        epsilon = timedelta(microseconds=1) if inside else timedelta()
        with app.state.session_factory.begin() as session:
            row = session.scalar(select(LoginSession).where(LoginSession.token_hash == session_hash))
            row.created_at = now - timedelta(days=7)
            row.expires_at = now + (epsilon if boundary == "absolute" else timedelta(days=1))
            row.last_seen_at = now - timedelta(hours=24) + epsilon if boundary == "idle" else now

    set_boundary(inside=True)
    assert signed_in.get("/api/v1/auth/me").status_code == 200
    set_boundary(inside=False)
    rejected = signed_in.get("/api/v1/auth/me")
    assert rejected.status_code == 401, rejected.text
    assert rejected.json()["error"]["code"] == "UNAUTHENTICATED"


def test_api_token_exact_expiry_boundary(signed_in, app, monkeypatch):
    created = signed_in.post("/api/v1/api-tokens", json={"name": "Expiry boundary"})
    assert created.status_code == 201, created.text
    token = created.json()
    now = utcnow()
    monkeypatch.setattr("api_watch.api_utils.utcnow", lambda: now)
    with TestClient(app) as api_client:
        api_client.headers["Authorization"] = "Bearer " + token["token"]
        with app.state.session_factory.begin() as session:
            session.get(ApiToken, token["id"]).expires_at = now + timedelta(microseconds=1)
        assert api_client.get("/api/v1/monitors").status_code == 200
        with app.state.session_factory.begin() as session:
            session.get(ApiToken, token["id"]).expires_at = now
        rejected = api_client.get("/api/v1/monitors")
        assert rejected.status_code == 401, rejected.text
        assert rejected.json()["error"]["code"] == "UNAUTHENTICATED"


def test_login_sets_secure_cookie_in_production(signed_in, app, monkeypatch):
    # Configuration validation is covered separately. This verifies the actual
    # login response switches its cookie policy in the production environment.
    monkeypatch.setattr(app.state.settings, "app_env", "production")
    response = signed_in.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"].lower()
    assert "; secure" in cookie
    assert "; httponly" in cookie
    assert "; samesite=lax" in cookie


def test_foreign_notification_and_token_ids_return_404(signed_in, app):
    owner_id = signed_in.get("/api/v1/auth/me").json()["id"]
    created = signed_in.post("/api/v1/api-tokens", json={"name": "Owner token"})
    assert created.status_code == 201, created.text
    token = created.json()
    with app.state.session_factory.begin() as session:
        notification = Notification(owner_id=owner_id, event_type="test_notification", status="suppressed")
        session.add(notification)
        session.flush()
        notification_id = notification.id
    with TestClient(app) as stranger:
        credentials = {"email": "stranger@example.com", "password": "another long test password"}
        assert stranger.post("/api/v1/auth/register", json=credentials).status_code == 201
        logged_in = stranger.post("/api/v1/auth/login", json=credentials)
        assert logged_in.status_code == 200, logged_in.text
        stranger.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]
        for identifier in [notification_id, str(uuid4())]:
            denied = stranger.get(f"/api/v1/notifications/{identifier}")
            assert denied.status_code == 404, denied.text
            assert denied.json()["error"]["code"] == "NOT_FOUND"
        assert stranger.get("/api/v1/notifications").json()["items"] == []
        for identifier in [token["id"], str(uuid4())]:
            denied = stranger.delete(f"/api/v1/api-tokens/{identifier}")
            assert denied.status_code == 404, denied.text
            assert denied.json()["error"]["code"] == "NOT_FOUND"
        assert stranger.get("/api/v1/api-tokens").json()["items"] == []

    assert signed_in.get(f"/api/v1/notifications/{notification_id}").status_code == 200
    with app.state.session_factory() as session:
        assert session.get(ApiToken, token["id"]).revoked_at is None
        assert session.get(Notification, notification_id).owner_id == owner_id


def test_expired_password_reset_cannot_change_password_or_revoke_session(signed_in, app, monkeypatch):
    token = delivered_reset_token(signed_in, app)
    now = utcnow()
    monkeypatch.setattr("api_watch.auth.utcnow", lambda: now)
    with app.state.session_factory.begin() as session:
        reset = session.scalar(select(PasswordReset).where(PasswordReset.token_hash == digest(token)))
        reset.expires_at = now
        user_id = reset.user_id
        original_hash = session.get(User, user_id).password_hash
    rejected = signed_in.post("/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "INVALID_RESET_TOKEN"
    assert signed_in.get("/api/v1/auth/me").status_code == 200
    with app.state.session_factory() as session:
        reset = session.scalar(select(PasswordReset).where(PasswordReset.token_hash == digest(token)))
        assert reset.used_at is None
        assert session.get(User, user_id).password_hash == original_hash
        assert session.scalar(select(LoginSession).where(LoginSession.user_id == user_id)).revoked_at is None


@pytest.mark.parametrize("operation", ["change", "reset"])
def test_password_rotation_revokes_all_sessions_and_api_tokens(signed_in, app, operation):
    """Exercise three real logins and both API token scopes before rotation."""
    with ExitStack() as stack:
        browsers = [signed_in]
        for _ in range(2):
            browser = stack.enter_context(TestClient(app))
            response = browser.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
            assert response.status_code == 200, response.text
            browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            browsers.append(browser)
        cookies = [browser.cookies.get("api_watch_session") for browser in browsers]
        assert len(set(cookies)) == 3
        tokens = []
        for scope in ["read", "read_write"]:
            created = signed_in.post("/api/v1/api-tokens", json={"name": scope, "scope": scope})
            assert created.status_code == 201, created.text
            tokens.append(created.json()["token"])

        if operation == "change":
            response = signed_in.post("/api/v1/auth/password", json={
                "current_password": PASSWORD, "new_password": NEW_PASSWORD,
            })
        else:
            token = delivered_reset_token(signed_in, app)
            response = signed_in.post("/api/v1/auth/password/reset", json={
                "token": token, "new_password": NEW_PASSWORD,
            })
        assert response.status_code == 200, response.text
        assert "max-age=0" in response.headers["set-cookie"].lower()

        probe = stack.enter_context(TestClient(app))
        for cookie in cookies:
            denied = probe.get("/api/v1/auth/me", headers={"Cookie": f"api_watch_session={cookie}"})
            assert denied.status_code == 401, denied.text
            assert denied.json()["error"]["code"] == "UNAUTHENTICATED"
        for token in tokens:
            denied = probe.get("/api/v1/monitors", headers={"Authorization": "Bearer " + token})
            assert denied.status_code == 401, denied.text
            assert denied.json()["error"]["code"] == "UNAUTHENTICATED"
        with app.state.session_factory() as session:
            sessions = session.scalars(select(LoginSession)).all()
            api_tokens = session.scalars(select(ApiToken)).all()
            assert len(sessions) == 3 and all(item.revoked_at is not None for item in sessions)
            assert len(api_tokens) == 2 and all(item.revoked_at is not None for item in api_tokens)

        assert probe.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).status_code == 401
        signed_back_in = probe.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": NEW_PASSWORD})
        assert signed_back_in.status_code == 200, signed_back_in.text
        assert probe.get("/api/v1/auth/me").status_code == 200
