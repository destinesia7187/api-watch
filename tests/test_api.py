"""End-to-end HTTP contracts: ownership, auth, state transitions, and metrics."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from api_watch.db import utcnow
from api_watch.models import (CheckRun, EmailMessage, IdempotencyKey, Incident, LoginSession,
                              MonitorConfig, PasswordReset, User)
from api_watch.security import decrypt_payload, digest


def test_registration_login_logout_and_cookie_flags(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/register", json={"email": " Owner@Example.com ", "password": "long enough password"}).status_code == 201
    assert client.post("/api/v1/auth/register", json={"email": "owner@example.com", "password": "long enough password"}).status_code == 409
    bad = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "wrong"})
    assert bad.status_code == 401
    login = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "long enough password"})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").json()["email"] == "owner@example.com"
    assert client.post("/api/v1/auth/logout").status_code == 403
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_csrf_origin_and_safe_validation(signed_in):
    browser = signed_in
    assert browser.post("/api/v1/monitors", json={"name": "X", "url": "https://example.com"},
                        headers={"Origin": "https://evil.example"}).status_code == 403
    csrf = browser.headers.pop("X-CSRF-Token")
    assert browser.post("/api/v1/monitors", json={"name": "X", "url": "https://example.com"}).status_code == 403
    browser.headers["X-CSRF-Token"] = csrf
    result = browser.post("/api/v1/monitors", json={"name": "X", "url": "file:///etc/passwd", "auth": {"type": "bearer", "token": "super-secret"}})
    assert result.status_code == 422
    assert "super-secret" not in result.text
    assert "request_id" in result.json()["error"]
    assert browser.patch("/api/v1/monitors/missing", content="{invalid", headers={"Content-Type": "application/json"}).status_code == 400


def test_monitor_crud_versions_secret_patch_and_pause(signed_in, app):
    payload = {"name": "Private API", "url": "https://example.com", "auth": {"type": "bearer", "token": "original-secret"},
               "secret_headers": [{"name": "X-API-Key", "value": "header-secret"}]}
    response = signed_in.post("/api/v1/monitors", json=payload)
    assert response.status_code == 201, response.text
    row = response.json()
    mid = row["id"]
    assert "original-secret" not in response.text and "header-secret" not in response.text
    assert row["auth"]["configured"]
    assert signed_in.patch(f"/api/v1/monitors/{mid}", json={"expected_version": 1, "timeout_ms": 3000}).status_code == 409
    row = signed_in.post(f"/api/v1/monitors/{mid}/pause", json={"expected_version": 1}).json()
    assert row["state"] == "paused"
    result = signed_in.patch(f"/api/v1/monitors/{mid}", json={"expected_version": row["record_version"],
        "timeout_ms": 3000, "auth": {"type": "bearer"},
        "secret_headers": [{"name": "X-API-Key", "remove": True}, {"name": "X-Other", "value": "replacement"}]})
    assert result.status_code == 200, result.text
    row = result.json()
    assert row["config_version"] == 2
    with app.state.session_factory() as db:
        config = db.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == mid, MonitorConfig.version == 2))
        assert "original-secret" not in config.encrypted_payload
        stored = decrypt_payload(config.encrypted_payload, app.state.settings)
        assert stored["auth"]["token"] == "original-secret"
        assert stored["secret_headers"] == [{"name": "X-Other", "value": "replacement"}]
    assert signed_in.patch(f"/api/v1/monitors/{mid}", json={"expected_version": 1, "name": "stale"}).status_code == 409
    assert signed_in.patch(f"/api/v1/monitors/{mid}", json={"expected_version": row["record_version"], "enabled": True}).status_code == 422
    assert signed_in.delete(f"/api/v1/monitors/{mid}?expected_version={row['record_version']}").status_code == 204
    assert signed_in.get(f"/api/v1/monitors/{mid}").status_code == 404
    with app.state.session_factory() as db:
        assert all(c.encrypted_payload is None for c in db.scalars(select(MonitorConfig).where(MonitorConfig.monitor_id == mid)))


def test_ownership_all_resource_routes(signed_in, monitor, app):
    with app.state.session_factory.begin() as db:
        run = CheckRun(monitor_id=monitor["id"], config_version=1, status="finished", outcome="success")
        db.add(run)
        incident = Incident(monitor_id=monitor["id"], config_version=1, started_at=utcnow(), detected_at=utcnow())
        db.add(incident)
        db.flush()
        run_id, incident_id = run.id, incident.id
    with TestClient(app) as stranger:
        stranger.post("/api/v1/auth/register", json={"email": "other@example.com", "password": "another long password"})
        result = stranger.post("/api/v1/auth/login", json={"email": "other@example.com", "password": "another long password"})
        stranger.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        for path in [f"/monitors/{monitor['id']}", f"/monitors/{monitor['id']}/checks",
                     f"/monitors/{monitor['id']}/stats", f"/monitors/{monitor['id']}/stats/series",
                     f"/checks/{run_id}", f"/incidents/{incident_id}"]:
            assert stranger.get("/api/v1" + path).status_code == 404, path
        assert stranger.get("/api/v1/monitors").json()["items"] == []
        assert stranger.post(f"/api/v1/monitors/{monitor['id']}/pause", json={"expected_version": 1}).status_code == 404
        assert stranger.delete(f"/api/v1/monitors/{monitor['id']}?expected_version=1").status_code == 404


def test_api_tokens_scope_expiry_and_revocation(signed_in, app, monitor):
    response = signed_in.post("/api/v1/api-tokens", json={"name": "Read only", "scope": "read"})
    assert response.status_code == 201
    token = response.json()
    assert token["token"] not in signed_in.get("/api/v1/api-tokens").text
    with TestClient(app) as cli:
        cli.headers["Authorization"] = "Bearer " + token["token"]
        assert cli.get("/api/v1/monitors").status_code == 200
        assert cli.post("/api/v1/monitors", json={"name": "x", "url": "https://example.com"}).status_code == 403
        assert cli.get("/api/v1/api-tokens").status_code == 403
        signed_in.delete(f"/api/v1/api-tokens/{token['id']}")
        assert cli.get("/api/v1/monitors").status_code == 401
    signed_in.headers["Authorization"] = "Bearer " + token["token"]
    assert signed_in.get("/api/v1/monitors").status_code == 400


def test_manual_idempotency_survives_pause(signed_in, monitor, app):
    mid = monitor["id"]
    headers = {"Idempotency-Key": "a-unique-manual-key"}
    first = signed_in.post(f"/api/v1/monitors/{mid}/run-now", headers=headers)
    assert first.status_code == 202, first.text
    again = signed_in.post(f"/api/v1/monitors/{mid}/run-now", headers=headers)
    assert again.json()["check_id"] == first.json()["check_id"]
    signed_in.post(f"/api/v1/monitors/{mid}/pause", json={"expected_version": 1})
    replay = signed_in.post(f"/api/v1/monitors/{mid}/run-now", headers=headers)
    assert replay.status_code == 202 and replay.json()["check_id"] == first.json()["check_id"]
    with app.state.session_factory() as db:
        assert len(db.scalars(select(IdempotencyKey)).all()) == 1


def test_stats_empty_manual_and_latency(signed_in, monitor, app):
    mid = monitor["id"]
    empty = signed_in.get(f"/api/v1/monitors/{mid}/stats").json()
    assert empty["success_rate_percent"] is None and empty["p95_response_time_ms"] is None
    now = utcnow() - timedelta(minutes=1)
    with app.state.session_factory.begin() as db:
        for index, (outcome, latency, trigger) in enumerate([
            ("success", 10, "scheduled"), ("success", 30, "scheduled"),
            ("failure", None, "scheduled"), ("interrupted", None, "scheduled"),
            ("success", 9999, "manual")]):
            db.add(CheckRun(monitor_id=mid, config_version=1, status="finished", outcome=outcome,
                trigger=trigger, started_at=now + timedelta(seconds=index), response_time_ms=latency))
    result = signed_in.get(f"/api/v1/monitors/{mid}/stats").json()
    assert result["success_count"] == 2 and result["failure_count"] == 1
    assert result["interrupted_count"] == 1 and result["evaluated_count"] == 3
    assert result["avg_response_time_ms"] == 20 and result["p95_response_time_ms"] == 30
    series = signed_in.get(f"/api/v1/monitors/{mid}/stats/series?bucket=1h").json()
    assert sum(item["success_count"] for item in series["items"]) == 2
    assert any(item["success_rate_percent"] is None for item in series["items"])


def test_cursor_pagination_stable_and_bound_to_filters(signed_in):
    for index in range(3):
        assert signed_in.post("/api/v1/monitors", json={"name": f"Monitor {index}", "url": "https://example.com"}).status_code == 201
    first = signed_in.get("/api/v1/monitors?limit=2").json()
    assert len(first["items"]) == 2 and first["has_more"]
    second = signed_in.get("/api/v1/monitors", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    assert signed_in.get("/api/v1/monitors", params={"state": "paused", "cursor": first["next_cursor"]}).status_code == 400
    assert signed_in.get("/api/v1/monitors?cursor=broken").status_code == 400


def test_password_reset_delivery_revokes_sessions(signed_in, app):
    response = signed_in.post("/api/v1/auth/password/forgot", json={"email": "owner@example.com"})
    assert response.status_code == 202
    with app.state.session_factory() as db:
        mail = db.scalar(select(EmailMessage))
        text = decrypt_payload(mail.encrypted_payload, app.state.settings)["text"]
        token = text.split("#reset=", 1)[1].split()[0]
        reset = db.scalar(select(PasswordReset))
        assert reset.token_hash == digest(token) and token not in response.text
    result = signed_in.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "a different good password"})
    assert result.status_code == 200
    assert signed_in.get("/api/v1/auth/me").status_code == 401
    assert signed_in.post("/api/v1/auth/password/reset", json={"token": token, "new_password": "a different good password"}).status_code == 422
    assert signed_in.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "a different good password"}).status_code == 200


def test_account_delete_removes_secrets(signed_in, app, monitor):
    assert signed_in.request("DELETE", "/api/v1/auth/account", json={"password": "a long test password"}).status_code == 204
    assert signed_in.get("/api/v1/auth/me").status_code == 401
    with app.state.session_factory() as db:
        assert not db.scalars(select(User)).all()
        assert not db.scalars(select(MonitorConfig)).all()
        assert not db.scalars(select(LoginSession)).all()


def test_health_docs_static_and_body_limit(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/v1/monitors" in schema["paths"]
    assert "APIKeyCookie" in schema["components"]["securitySchemes"]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.post("/api/v1/auth/login", content="x" * 262145).status_code == 413
    assert client.post("/api/v1/auth/login", content="{oops", headers={"Content-Type": "application/json"}).status_code == 400


def test_exact_json_numbers_survive_encrypted_api_roundtrip(signed_in):
    literal = '1.00000000000000000000000000000001'
    payload = '{"name":"Exact","url":"https://example.com/","json_assertions":[{"pointer":"/value","operator":"equals","expected":' + literal + '}]}'
    result = signed_in.post("/api/v1/monitors", content=payload, headers={"Content-Type": "application/json"})
    assert result.status_code == 201, result.text
    assert literal in result.text
    mid = result.json()["id"]
    assert literal in signed_in.get(f"/api/v1/monitors/{mid}").text
    assert literal in signed_in.get("/api/v1/monitors").text


def test_noop_secret_header_patch_does_not_change_configuration(signed_in, monitor):
    result = signed_in.patch(f"/api/v1/monitors/{monitor['id']}", json={"expected_version": 1, "secret_headers": []})
    assert result.status_code == 200, result.text
    assert result.json()["config_version"] == 1
    assert signed_in.patch(f"/api/v1/monitors/{monitor['id']}", json={"expected_version": 2, "auth": {}}).status_code == 422


def test_patch_json_boolean_is_not_equal_to_number(signed_in):
    result = signed_in.post("/api/v1/monitors", json={"name": "Typed JSON", "url": "https://example.com",
        "enabled": False, "json_assertions": [{"pointer": "/value", "operator": "equals", "expected": 1}]})
    assert result.status_code == 201
    mid = result.json()["id"]
    changed = signed_in.patch(f"/api/v1/monitors/{mid}", json={"expected_version": 1,
        "json_assertions": [{"pointer": "/value", "operator": "equals", "expected": True}]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["config_version"] == 2
    assert changed.json()["json_assertions"][0]["expected"] is True


def test_finite_large_exponent_is_not_coerced_to_infinity_before_validation(signed_in):
    payload = '{"name":"Large exponent","url":"https://example.com/","json_assertions":[{"pointer":"/value","operator":"equals","expected":1e309}]}'
    result = signed_in.post("/api/v1/monitors", content=payload, headers={"Content-Type": "application/json"})
    assert result.status_code == 201, result.text
    assert '1E+309' in result.text


def test_fast_resume_never_reuses_a_cancelled_schedule_slot(signed_in, app, monitor, monkeypatch):
    from api_watch.engine import schedule_due
    from api_watch.models import Monitor

    slot = utcnow() - timedelta(seconds=1)
    mid = monitor["id"]
    with app.state.session_factory.begin() as db:
        db.get(Monitor, mid).next_due_at = slot
        assert schedule_due(db, app.state.settings, slot)["queued"] == 1
    paused = signed_in.post(f"/api/v1/monitors/{mid}/pause", json={"expected_version": 1})
    assert paused.status_code == 200
    monkeypatch.setattr("api_watch.monitors.database_now", lambda db: slot)
    resumed = signed_in.post(f"/api/v1/monitors/{mid}/resume",
                            json={"expected_version": paused.json()["record_version"]})
    assert resumed.status_code == 200
    with app.state.session_factory.begin() as db:
        due = db.get(Monitor, mid).next_due_at
        assert due > slot
        assert schedule_due(db, app.state.settings, due)["queued"] == 1
    with app.state.session_factory() as db:
        runs = db.scalars(select(CheckRun).where(CheckRun.monitor_id == mid)).all()
        assert len(runs) == 2
        assert {run.status for run in runs} == {"finished", "queued"}
        assert len({run.scheduled_at for run in runs}) == 2
