"""Public schemas track real wire responses and preserve secret/omission boundaries."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from api_watch.contracts import (
    BodyRead,
    CheckRead,
    EqualsAssertion,
    ErrorEnvelope,
    IncidentRead,
    MonitorPatch,
    MonitorRead,
    NotificationRead,
    Page,
    PublicHeader,
    SeriesRead,
    StatsRead,
    TelegramRead,
    TokenCreatedRead,
    TokenRead,
    UserRead,
    VersionRequest,
)
from api_watch.db import utcnow
from api_watch.models import Incident, Monitor, Notification


@pytest.fixture(autouse=True)
def isolate_contracts_from_public_dns(monkeypatch):
    """Wire-format tests do not need a real resolver; transport has its own suite."""

    async def allow_fixture_target(*_args, **_kwargs):
        return None

    monkeypatch.setattr("api_watch.monitors.validate_target", allow_fixture_target)


def test_monitor_response_contract_matches_current_api(signed_in, monitor):
    result = signed_in.get(f"/api/v1/monitors/{monitor['id']}")
    assert result.status_code == 200
    parsed = MonitorRead.model_validate(result.json())
    assert str(parsed.id) == monitor["id"]
    assert parsed.auth.configured is False
    assert parsed.last_response_time_ms is None
    page = Page[MonitorRead].model_validate(signed_in.get("/api/v1/monitors").json())
    assert len(page.items) == 1


def test_account_and_token_response_contracts(signed_in):
    account = UserRead.model_validate(signed_in.get("/api/v1/auth/me").json())
    assert account.email == "owner@example.com"
    created = signed_in.post("/api/v1/api-tokens", json={"name": "Contract test", "scope": "read"})
    assert created.status_code == 201
    token = TokenCreatedRead.model_validate(created.json())
    assert token.token.startswith("aw_")
    listed = Page[TokenRead].model_validate(signed_in.get("/api/v1/api-tokens").json())
    assert len(listed.items) == 1
    assert "token" not in listed.items[0].model_dump()
    TelegramRead.model_validate(signed_in.get("/api/v1/integrations/telegram").json())


def test_check_statistics_and_series_response_contracts(signed_in, monitor):
    prefix = f"/api/v1/monitors/{monitor['id']}"
    queued = signed_in.post(prefix + "/run-now", headers={"Idempotency-Key": "contract-check-run"})
    assert queued.status_code == 202
    check = CheckRead.model_validate(signed_in.get("/api/v1/checks/" + queued.json()["check_id"]).json())
    assert check.outcome is None
    assert check.http_status is None
    Page[CheckRead].model_validate(signed_in.get(prefix + "/checks").json())
    stats = StatsRead.model_validate(signed_in.get(prefix + "/stats").json())
    assert stats.success_rate_percent is None
    assert stats.p95_response_time_ms is None
    series = SeriesRead.model_validate(signed_in.get(prefix + "/stats/series").json())
    assert series.items
    assert all(bucket.success_rate_percent is None for bucket in series.items)


def test_incident_and_notification_contracts_from_persisted_metadata(signed_in, monitor, app):
    with app.state.session_factory.begin() as db:
        owner_id = db.get(Monitor, monitor["id"]).owner_id
        incident = Incident(monitor_id=monitor["id"], config_version=1,
                            started_at=utcnow(), detected_at=utcnow())
        db.add(incident)
        db.flush()
        notification = Notification(owner_id=owner_id, monitor_id=monitor["id"],
            incident_id=incident.id, event_type="incident_opened", status="suppressed",
            error_code="integration_not_connected")
        db.add(notification)
        db.flush()
        incident_id, notification_id = incident.id, notification.id
    incident_result = IncidentRead.model_validate(signed_in.get("/api/v1/incidents/" + incident_id).json())
    assert incident_result.status == "open"
    assert incident_result.resolved_at is None
    notification_result = NotificationRead.model_validate(
        signed_in.get("/api/v1/notifications/" + notification_id).json()
    )
    assert notification_result.status == "suppressed"
    assert notification_result.sent_at is None
    Page[NotificationRead].model_validate(signed_in.get("/api/v1/notifications").json())


def test_patch_omission_and_explicit_null_have_distinct_schema_and_validation():
    assert MonitorPatch(expected_version=1).model_dump() == {"expected_version": 1}
    assert MonitorPatch(expected_version=1, max_response_time_ms=None).model_dump() == {
        "expected_version": 1, "max_response_time_ms": None,
    }
    schema = MonitorPatch.model_json_schema()
    assert schema["required"] == ["expected_version"]
    assert schema["properties"]["name"]["type"] == "string"
    assert {entry.get("type") for entry in schema["properties"]["max_response_time_ms"]["anyOf"]} == {
        "integer", "null",
    }
    for patch in [{"name": None}, {"headers": None}, {"enabled": True}, {"owner_id": "foreign"}]:
        with pytest.raises(ValidationError):
            MonitorPatch.model_validate({"expected_version": 1, **patch})
    for value in [True, "1", 0]:
        with pytest.raises(ValidationError):
            VersionRequest(expected_version=value)


def test_patch_secret_operations_and_readable_public_header_schema():
    patch = MonitorPatch.model_validate({"expected_version": 1, "auth": {"type": "bearer"},
        "secret_headers": [{"name": "X-Key", "value": "replacement"},
                           {"name": "X-Old", "remove": True}]})
    dumped = patch.model_dump()
    assert dumped["auth"] == {"type": "bearer"}
    assert dumped["secret_headers"][1] == {"name": "X-Old", "remove": True}
    schema = MonitorPatch.model_json_schema()
    assert schema["$defs"]["SecretHeaderSet"]["properties"]["value"]["writeOnly"] is True
    assert "writeOnly" not in PublicHeader.model_json_schema()["properties"]["value"]
    for secret in [{"name": "X-Key", "value": "secret", "remove": True},
                   {"name": "X-Key", "remove": False}]:
        with pytest.raises(ValidationError):
            MonitorPatch.model_validate({"expected_version": 1, "secret_headers": [secret]})
    with pytest.raises(ValidationError):
        MonitorPatch.model_validate({"expected_version": 1, "auth": {"type": "bearer", "configured": True}})


def test_body_and_assertion_any_values_keep_decimal_precision():
    precise = Decimal("1.00000000000000000000000000001")
    body = BodyRead.model_validate({"enabled": True, "json": {"precise": precise}})
    assertion = EqualsAssertion.model_validate({"pointer": "/precise", "operator": "equals", "expected": precise})
    assert body.json_value["precise"] is precise
    assert assertion.expected is precise
    assert EqualsAssertion(pointer="", operator="equals", expected=None).expected is None
    with pytest.raises(ValidationError):
        EqualsAssertion(pointer="/missing", operator="equals")


def test_error_envelope_accepts_field_and_dependency_detail_objects(signed_in):
    result = signed_in.get("/api/v1/monitors/not-owned")
    assert result.status_code == 404
    error = ErrorEnvelope.model_validate(result.json())
    assert error.error.code == "NOT_FOUND"
    payload = result.json()
    payload["error"]["details"] = [{"field": "name", "code": "REQUIRED"},
                                    {"available_from": "2026-10-01T00:00:00Z", "recommended_bucket": "1h"}]
    assert len(ErrorEnvelope.model_validate(payload).error.details) == 2
