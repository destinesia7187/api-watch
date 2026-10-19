"""Public OpenAPI contracts, separate from persistence and encrypted configuration.

Monitoring responses use ``exact_response`` in the routes so JSON numeric literals
in request bodies and assertions remain exact. These models describe that wire
format; they must not replace the lossless serializer with a float conversion.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    MISSING,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
)

HTTPMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
HealthStatus = Literal["unknown", "up", "down", "degraded", "recovering"]
CheckStatus = Literal["queued", "running", "finished"]
CheckOutcome = Literal["success", "failure", "blocked", "interrupted", "cancelled", "skipped"]
IncidentStatus = Literal["open", "resolved", "aborted"]
NotificationStatus = Literal["pending", "sending", "sent", "failed", "suppressed", "cancelled"]
NotificationEvent = Literal[
    "incident_opened", "incident_resolved", "incident_aborted", "test_notification"
]
TokenScope = Literal["read", "read_write"]
Bucket = Literal["1m", "5m", "1h", "1d"]
PositiveVersion = Annotated[StrictInt, Field(ge=1)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
HeaderName = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
]
HeaderValue = Annotated[str, Field(max_length=4096)]
T = TypeVar("T")


class Contract(BaseModel):
    """Only documented fields are accepted; nullable fields are explicit."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, from_attributes=True)


class Page(Contract, Generic[T]):
    """Stable descending cursor page; the cursor is bound to the supplied filters."""

    items: list[T]
    next_cursor: str | None = Field(default=None, description="Opaque cursor for the next page.")
    has_more: bool


class UserRead(Contract):
    """Account metadata. The email address is not represented as verified."""

    id: UUID
    email: EmailStr
    created_at: AwareDatetime


class PublicHeader(Contract):
    """An ordinary header, whose value is readable in monitor settings."""

    name: HeaderName
    value: HeaderValue


class QueryParameter(Contract):
    """Ordered query parameter; repeated names are allowed."""

    name: str = Field(max_length=1000)
    value: str = Field(max_length=4096)


class SecretHeaderRead(Contract):
    """Presence marker for an encrypted header; its value is never returned."""

    name: HeaderName
    configured: bool


class PublicAuth(Contract):
    """Authorization type and presence marker without target credentials."""

    type: Literal["none", "bearer"]
    configured: bool


class BodyRead(Contract):
    """Disabled means no body; enabled with JSON null means a literal null body."""

    enabled: bool
    json_value: Any = Field(
        default=None, alias="json",
        description="Arbitrary JSON value. Numeric literals retain their original precision.",
    )


class AssertionBase(Contract):
    pointer: str = Field(
        max_length=512,
        description="RFC 6901 JSON Pointer. Empty selects the root; ~0 escapes ~ and ~1 escapes /.",
        examples=["/data/status", "/items/0/id", ""],
    )

    @field_validator("pointer")
    @classmethod
    def pointer_syntax(cls, value):
        if (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
            raise ValueError("Use an RFC 6901 JSON Pointer with only ~0 and ~1 escapes")
        return value


class ExistsAssertion(AssertionBase):
    """The pointer must resolve; an existing JSON null satisfies this assertion."""

    operator: Literal["exists"]


class EqualsAssertion(AssertionBase):
    """The pointer must resolve and equal the supplied, type-sensitive JSON value."""

    operator: Literal["equals"]
    expected: Any = Field(
        ...,
        description="Required JSON value, including explicit null. Numbers retain exact precision.",
    )


Assertion = Annotated[ExistsAssertion | EqualsAssertion, Field(discriminator="operator")]


class MonitorRead(Contract):
    """Current settings and scheduled health, with write-only credentials removed."""

    id: UUID
    name: Name
    description: str
    url: str = Field(max_length=2048)
    method: HTTPMethod
    headers: list[PublicHeader]
    secret_headers: list[SecretHeaderRead]
    query_params: list[QueryParameter]
    body: BodyRead
    auth: PublicAuth
    side_effects_acknowledged: bool
    interval_seconds: int = Field(ge=60, le=86400)
    timeout_ms: int = Field(ge=1000, le=30000)
    expected_status: int = Field(ge=200, le=599)
    max_response_time_ms: int | None = Field(default=None, ge=1, le=30000)
    json_assertions: list[Assertion]
    failure_threshold: int = Field(ge=1, le=10)
    recovery_threshold: int = Field(ge=1, le=10)
    notifications_enabled: bool
    enabled: bool = Field(description="Read-only reflection of whether state is active.")
    state: Literal["active", "paused"]
    record_version: PositiveVersion
    config_version: PositiveVersion
    health_status: HealthStatus
    next_due_at: AwareDatetime | None = None
    last_checked_at: AwareDatetime | None = None
    last_response_time_ms: float | None = Field(default=None, ge=0)
    last_error_code: str | None = None
    failure_streak: int = Field(ge=0)
    success_streak: int = Field(ge=0)
    created_at: AwareDatetime
    open_incident_id: UUID | None = None
    busy: bool = Field(description="A check is queued or running for this monitor.")


class CheckRead(Contract):
    """A logical check. Queued and running checks have no terminal outcome."""

    id: UUID
    monitor_id: UUID
    config_version: PositiveVersion
    trigger: Literal["scheduled", "manual"]
    created_at: AwareDatetime
    scheduled_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    status: CheckStatus
    outcome: CheckOutcome | None = None
    http_status: int | None = Field(
        default=None, description="Observed target status; null if no HTTP response was received."
    )
    response_time_ms: float | None = Field(
        default=None, ge=0,
        description="Full HTTP exchange duration; null when the exchange did not complete.",
    )
    elapsed_ms: float | None = Field(default=None, ge=0)
    bytes_received: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    failed_assertions: list[int] = Field(description="Zero-based indices of failed JSON assertions.")


class IncidentRead(Contract):
    """Confirmed incident timeline; an aborted incident does not imply recovery."""

    id: UUID
    monitor_id: UUID
    monitor_name: str | None = None
    config_version: PositiveVersion
    status: IncidentStatus
    started_at: AwareDatetime
    detected_at: AwareDatetime
    recovery_started_at: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    first_check_id: UUID | None = None
    confirming_check_id: UUID | None = None
    last_check_id: UUID | None = None
    reason: str | None = None
    created_at: AwareDatetime
    duration_seconds: float = Field(ge=0)


class Metrics(Contract):
    """Scheduled observations only; unknown periods remain null, never synthetic uptime."""

    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    success_rate_percent: float | None = Field(default=None, ge=0, le=100)
    blocked_count: int = Field(ge=0)
    interrupted_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    non_evaluated_count: int = Field(ge=0)
    skipped_slot_count: int = Field(ge=0)
    latency_sample_count: int = Field(ge=0)
    avg_response_time_ms: float | None = Field(default=None, ge=0)
    min_response_time_ms: float | None = Field(default=None, ge=0)
    max_response_time_ms: float | None = Field(default=None, ge=0)
    p50_response_time_ms: float | None = Field(default=None, ge=0)
    p95_response_time_ms: float | None = Field(default=None, ge=0)


class StatsRead(Metrics):
    """Metrics over [from, to), with all contributing configuration versions."""

    from_time: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    config_versions: list[int]


class SeriesBucket(Metrics):
    """UTC-aligned bucket clipped to the requested half-open period."""

    bucket_start: AwareDatetime
    bucket_end: AwareDatetime


class SeriesRead(Contract):
    from_time: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    bucket: Bucket
    items: list[SeriesBucket] = Field(max_length=1000)
    config_versions: list[int]


class NotificationRead(Contract):
    """Delivery metadata; no Telegram chat identifier or message credentials."""

    id: UUID
    monitor_id: UUID | None = None
    incident_id: UUID | None = None
    event_type: NotificationEvent
    status: NotificationStatus
    attempts: int = Field(ge=0)
    next_attempt_at: AwareDatetime
    error_code: str | None = None
    error_message: str | None = None
    created_at: AwareDatetime
    sent_at: AwareDatetime | None = None


class TokenRead(Contract):
    """API token metadata. The full secret is not available from list operations."""

    id: UUID
    name: str
    prefix: str
    scope: TokenScope
    created_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    last_used_at: AwareDatetime | None = None


class TokenCreatedRead(TokenRead):
    """Token creation adds its secret exactly once; store it before closing the response."""

    token: str = Field(description="Complete API credential, returned only by the creation request.")


class ErrorDetail(Contract):
    code: str
    message: str
    details: list[dict[str, Any]] = Field(default_factory=list)
    request_id: UUID


class ErrorEnvelope(Contract):
    """Shared safe error format for validation, authorization, and dependency failures."""

    error: ErrorDetail


class VersionRequest(Contract):
    """Optimistic concurrency guard for pause and resume operations."""

    expected_version: PositiveVersion = Field(description="Current record_version returned by GET.")


class SecretHeaderSet(Contract):
    """Add or replace one encrypted header; omitted names preserve existing secrets."""

    name: HeaderName
    value: HeaderValue = Field(json_schema_extra={"writeOnly": True})


class SecretHeaderRemove(Contract):
    """Delete an encrypted header by its case-insensitive name."""

    name: HeaderName
    remove: Literal[True]


class NoAuthRequest(Contract):
    """Switching to none deletes the saved built-in Bearer token."""

    type: Literal["none"]


class BearerAuthPatch(Contract):
    """Omitting token preserves an existing Bearer token; enabling Bearer requires a value."""

    type: Literal["bearer"]
    token: str | MISSING = Field(
        default=MISSING, min_length=1, max_length=4096,
        json_schema_extra={"writeOnly": True},
        description="New target credential. Omit only when an existing Bearer token is configured.",
    )


class MonitorPatch(VersionRequest):
    """Partial atomic update. Omitted fields are preserved; only the latency limit accepts null.

    Request execution fields require an idle paused monitor. Public header,
    parameter, assertion, and body values replace their corresponding settings.
    Secret headers are operations, not an array replacement. MISSING keeps the
    omission/null distinction both at runtime and in the generated OpenAPI schema.
    """

    name: Name | MISSING = MISSING
    description: Annotated[str, Field(max_length=1000)] | MISSING = MISSING
    url: Annotated[str, Field(max_length=2048)] | MISSING = MISSING
    method: HTTPMethod | MISSING = MISSING
    headers: Annotated[list[PublicHeader], Field(max_length=20)] | MISSING = MISSING
    secret_headers: Annotated[
        list[SecretHeaderSet | SecretHeaderRemove], Field(max_length=20)
    ] | MISSING = MISSING
    query_params: Annotated[list[QueryParameter], Field(max_length=100)] | MISSING = MISSING
    body: BodyRead | MISSING = MISSING
    auth: Annotated[
        NoAuthRequest | BearerAuthPatch, Field(discriminator="type")
    ] | MISSING = MISSING
    side_effects_acknowledged: StrictBool | MISSING = MISSING
    interval_seconds: Annotated[StrictInt, Field(ge=60, le=86400)] | MISSING = MISSING
    timeout_ms: Annotated[StrictInt, Field(ge=1000, le=30000)] | MISSING = MISSING
    expected_status: Annotated[StrictInt, Field(ge=200, le=599)] | MISSING = MISSING
    max_response_time_ms: Annotated[StrictInt, Field(ge=1, le=30000)] | None | MISSING = MISSING
    json_assertions: Annotated[list[Assertion], Field(max_length=20)] | MISSING = MISSING
    failure_threshold: Annotated[StrictInt, Field(ge=1, le=10)] | MISSING = MISSING
    recovery_threshold: Annotated[StrictInt, Field(ge=1, le=10)] | MISSING = MISSING
    notifications_enabled: StrictBool | MISSING = MISSING


class LoginRead(Contract):
    user: UserRead
    csrf_token: str


class CsrfRead(Contract):
    csrf_token: str


class MessageRead(Contract):
    message: str


class TelegramRead(Contract):
    connected: bool
    configured: bool
    bot_username: str | None = None
    connected_at: AwareDatetime | None = None


class TelegramLinkRead(Contract):
    url: str = Field(json_schema_extra={"format": "uri"})
    expires_at: AwareDatetime


class ManualCheckRead(Contract):
    check_id: UUID
    trigger: Literal["manual"]
    status: CheckStatus


class TestNotificationRead(Contract):
    notification_id: UUID
    status: NotificationStatus


class HealthLiveRead(Contract):
    """Liveness checks only the API process and never contact monitored targets."""

    status: Literal["ok"]


class HealthReadyRead(Contract):
    """Readiness reports database/migration readiness separately from worker health."""

    status: Literal["ready", "not_ready"]


class ServiceProcessRead(Contract):
    """One authenticated background-process heartbeat without infrastructure secrets."""

    name: str
    last_seen_at: AwareDatetime
    healthy: bool = Field(description="The latest heartbeat was less than 60 seconds ago.")


class ServiceStatusRead(Contract):
    processes: list[ServiceProcessRead]
