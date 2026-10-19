"""Deletion seams and malformed request boundaries, without timing-dependent races."""

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_watch import monitors
from api_watch.config import Settings
from api_watch.errors import APIError
from api_watch.schemas import MonitorCreate
from api_watch.security import encrypt_payload


@pytest.fixture
def view_context():
    settings = Settings(app_env="test", session_secret="contract-review" * 4,
                        encryption_key=base64.urlsafe_b64encode(b"r" * 32).decode(), _env_file=None)
    owner_id = str(uuid4())
    now = datetime(2026, 10, 19, tzinfo=UTC)
    row = SimpleNamespace(id=str(uuid4()), owner_id=owner_id, name="Surviving monitor", description="",
        state="active", record_version=1, config_version=1, health_status="unknown",
        notifications_enabled=False, next_due_at=now, last_checked_at=None,
        last_response_time_ms=None, last_error_code=None, failure_streak=0, success_streak=0,
        created_at=now)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    auth = SimpleNamespace(user=SimpleNamespace(id=owner_id))
    config = MonitorCreate(name=row.name, url="https://example.com/health",
                           auth={"type": "bearer", "token": "target-secret"}).model_dump()
    encrypted = SimpleNamespace(encrypted_payload=encrypt_payload(config, settings))
    return SimpleNamespace(settings=settings, request=request, auth=auth, row=row, encrypted=encrypted)


@pytest.mark.parametrize("purged", [None, SimpleNamespace(encrypted_payload=None)])
def test_detail_returns_not_found_when_config_disappears_after_owner_selection(view_context, monkeypatch, purged):
    context = view_context
    db = SimpleNamespace(scalar=lambda _statement: context.row)
    monkeypatch.setattr(monitors, "configuration", lambda *_args: purged)
    with pytest.raises(APIError) as error:
        monitors.get_monitor(context.row.id, context.request, auth=context.auth, db=db)
    assert error.value.status == 404
    assert error.value.code == "NOT_FOUND"


@pytest.mark.parametrize("purged", [None, SimpleNamespace(encrypted_payload=None)])
def test_monitor_page_omits_deleted_config_and_preserves_cursor_position(view_context, monkeypatch, purged):
    context = view_context
    removed = SimpleNamespace(id=str(uuid4()))
    page = {"next_cursor": "immutable-position-after-deleted-row", "has_more": True}
    monkeypatch.setattr(monitors, "cursor_page", lambda *_args, **_kwargs: ([removed, context.row], page))
    monkeypatch.setattr(monitors, "configuration",
                        lambda _db, row: purged if row.id == removed.id else context.encrypted)
    db = SimpleNamespace(scalar=lambda _statement: None)
    result = monitors.list_monitors(context.request, auth=context.auth, db=db)
    payload = json.loads(result.body)
    assert [item["id"] for item in payload["items"]] == [context.row.id]
    assert payload["next_cursor"] == page["next_cursor"]
    assert payload["has_more"] is True
    assert "target-secret" not in result.body.decode()


def test_monitor_page_does_not_hide_non_deletion_errors(view_context, monkeypatch):
    context = view_context
    monkeypatch.setattr(monitors, "cursor_page", lambda *_args, **_kwargs: (
        [context.row], {"next_cursor": None, "has_more": False}))

    def unavailable(*_args):
        raise APIError(503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable.")

    monkeypatch.setattr(monitors, "configuration", unavailable)
    with pytest.raises(APIError) as error:
        monitors.list_monitors(context.request, auth=context.auth, db=SimpleNamespace())
    assert error.value.status == 503


def incident(monitor_id):
    now = datetime(2026, 10, 19, tzinfo=UTC)
    return SimpleNamespace(id=str(uuid4()), monitor_id=monitor_id, config_version=1, status="open",
        started_at=now, detected_at=now, recovery_started_at=None, resolved_at=None, ended_at=None,
        first_check_id=None, confirming_check_id=None, last_check_id=None,
        reason="unexpected_status", created_at=now)


@pytest.mark.parametrize("parent_state", ["missing", "deleted"])
def test_incident_detail_handles_parent_deleted_after_join_selection(view_context, parent_state):
    row = incident(view_context.row.id)
    parent = None if parent_state == "missing" else SimpleNamespace(state="deleted")
    db = SimpleNamespace(scalar=lambda _statement: row, get=lambda *_args: parent)
    with pytest.raises(APIError) as error:
        monitors.get_incident(row.id, auth=view_context.auth, db=db)
    assert error.value.status == 404


@pytest.mark.parametrize("parent_state", ["missing", "deleted"])
def test_incident_page_handles_cascade_after_page_selection(view_context, monkeypatch, parent_state):
    context = view_context
    removed = incident(str(uuid4()))
    survivor = incident(context.row.id)
    page = {"next_cursor": "after-selected-incidents", "has_more": True}
    monkeypatch.setattr(monitors, "cursor_page", lambda *_args, **_kwargs: ([removed, survivor], page))
    gone = None if parent_state == "missing" else SimpleNamespace(state="deleted")
    db = SimpleNamespace(get=lambda _model, identifier: context.row if identifier == context.row.id else gone)
    result = monitors.list_incidents(context.request, from_time=None, to=None, auth=context.auth, db=db)
    assert [item["id"] for item in result["items"]] == [survivor.id]
    assert result["next_cursor"] == page["next_cursor"]
    assert result["has_more"] is True


@pytest.mark.parametrize(("method", "endpoint", "payload"), [
    ("POST", "/auth/login", r'{"email":"owner@example.com","password":"\ud800"}'),
    ("POST", "/auth/password/reset", r'{"token":"unknown","new_password":"long password \udfff"}'),
    ("POST", "/auth/password", r'{"current_password":"\ud800","new_password":"another long password"}'),
    ("DELETE", "/auth/account", r'{"password":"\udfff"}'),
    ("POST", "/monitors", r'{"name":"Bad \ud800","url":"https://example.com/"}'),
    ("POST", "/monitors", r'{"name":"Bad description","description":"\udfff","url":"https://example.com/"}'),
    ("POST", "/monitors", r'{"name":"Bad key","url":"https://example.com/","json_assertions":[{"pointer":"","operator":"equals","expected":{"\ud800":true}}]}'),
    ("POST", "/monitors", r'{"name":"Bad assertion","url":"https://example.com/","json_assertions":[{"pointer":"/status","operator":"equals","expected":"\udfff"}]}'),
])
def test_unicode_request_boundary_rejects_unpaired_surrogates(client, method, endpoint, payload):
    result = client.request(method, "/api/v1" + endpoint, content=payload.encode("ascii"),
                            headers={"Content-Type": "application/json"})
    assert result.status_code == 400, result.text
    assert result.json()["error"]["code"] == "INVALID_JSON"
    assert "request_id" in result.json()["error"]


def test_valid_surrogate_pair_is_saved_as_astral_unicode(signed_in):
    payload = r'{"name":"Rocket \ud83d\ude80","url":"https://example.com/","enabled":false,"json_assertions":[{"pointer":"/symbol","operator":"equals","expected":"\ud83d\ude80"}]}'
    result = signed_in.post("/api/v1/monitors", content=payload.encode("ascii"),
                            headers={"Content-Type": "application/json"})
    assert result.status_code == 201, result.text
    assert result.json()["name"] == "Rocket \U0001F680"
    assert result.json()["json_assertions"][0]["expected"] == "\U0001F680"


def test_login_null_email_is_an_invalid_credential_not_a_database_error(client):
    result = client.post("/api/v1/auth/login", json={"email": "invalid\x00@example.com", "password": "wrong"})
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.parametrize(("endpoint", "payload"), [
    ("/monitors", {"name": "Bad\x00name", "url": "https://example.com/"}),
    ("/monitors", {"name": "Bad description", "description": "Bad\x00text", "url": "https://example.com/"}),
    ("/api-tokens", {"name": "Bad\x00token"}),
])
def test_database_text_fields_reject_null_characters_before_inserting(signed_in, endpoint, payload):
    result = signed_in.post("/api/v1" + endpoint, json=payload)
    assert result.status_code == 422, result.text
    assert result.json()["error"]["code"] == "VALIDATION_ERROR"


def test_encrypted_arbitrary_json_can_still_contain_a_null_character(signed_in):
    result = signed_in.post("/api/v1/monitors", json={"name": "Literal JSON null character",
        "url": "https://example.com/", "enabled": False,
        "json_assertions": [{"pointer": "/value", "operator": "equals", "expected": "\x00"}]})
    assert result.status_code == 201, result.text
    assert result.json()["json_assertions"][0]["expected"] == "\x00"
@pytest.mark.parametrize("number", ["1e999999999999999999999999999", "1e-999999999999999999999999999"])
def test_unrepresentable_json_exponents_return_input_error(client, number):
    response = client.post("/api/v1/auth/login", content='{"email":"fixture@example.com","password":' + number + '}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]
