"""Relational model and constraints. No plaintext monitoring credentials."""

import uuid

from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text

from api_watch.db import Base, UTCDateTime, utcnow


def identifier():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=identifier)
    email = Column(String(320), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class LoginSession(Base):
    __tablename__ = "sessions"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    last_seen_at = Column(UTCDateTime, default=utcnow, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    revoked_at = Column(UTCDateTime)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    prefix = Column(String(16), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    scope = Column(String(16), nullable=False, default="read")
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    revoked_at = Column(UTCDateTime)
    last_used_at = Column(UTCDateTime)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    used_at = Column(UTCDateTime)


class Monitor(Base):
    __tablename__ = "monitors"
    id = Column(String(36), primary_key=True, default=identifier)
    owner_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="", nullable=False)
    state = Column(String(16), default="active", nullable=False)
    record_version = Column(Integer, default=1, nullable=False)
    config_version = Column(Integer, default=1, nullable=False)
    notifications_enabled = Column(Boolean, default=False, nullable=False)
    next_due_at = Column(UTCDateTime, index=True)
    health_status = Column(String(16), default="unknown", nullable=False)
    failure_streak = Column(Integer, default=0, nullable=False)
    success_streak = Column(Integer, default=0, nullable=False)
    streak_started_at = Column(UTCDateTime)
    recovery_started_at = Column(UTCDateTime)
    last_applied_scheduled_at = Column(UTCDateTime)
    last_checked_at = Column(UTCDateTime)
    last_response_time_ms = Column(Float)
    last_error_code = Column(String(64))
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    deleted_at = Column(UTCDateTime)


class MonitorConfig(Base):
    __tablename__ = "monitor_configs"
    __table_args__ = (UniqueConstraint("monitor_id", "version"),)
    id = Column(String(36), primary_key=True, default=identifier)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    encrypted_payload = Column(Text)
    interval_seconds = Column(Integer, nullable=False, default=60)
    timeout_ms = Column(Integer, nullable=False, default=10000)
    failure_threshold = Column(Integer, nullable=False, default=3)
    recovery_threshold = Column(Integer, nullable=False, default=2)
    key_id = Column(String(100), nullable=False, default="local-v1")
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    secrets_purged_at = Column(UTCDateTime)


class CheckRun(Base):
    __tablename__ = "check_runs"
    __table_args__ = (
        UniqueConstraint("monitor_id", "scheduled_at"),
        Index("ix_one_active_check", "monitor_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"),
              sqlite_where=text("status IN ('queued', 'running')")),
        Index("ix_check_history", "monitor_id", "created_at", "id"),
        Index("ix_check_queue", "status", "scheduled_at"),
    )
    id = Column(String(36), primary_key=True, default=identifier)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False)
    config_version = Column(Integer, nullable=False)
    trigger = Column(String(16), nullable=False, default="scheduled")
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    scheduled_at = Column(UTCDateTime)
    started_at = Column(UTCDateTime)
    finished_at = Column(UTCDateTime)
    status = Column(String(16), default="queued", nullable=False)
    outcome = Column(String(16))
    http_status = Column(Integer)
    response_time_ms = Column(Float)
    elapsed_ms = Column(Float)
    bytes_received = Column(Integer, default=0)
    error_code = Column(String(64))
    error_message = Column(Text)
    failed_assertions = Column(JSON, default=list, nullable=False)
    lease_token = Column(String(36))
    lease_expires_at = Column(UTCDateTime, index=True)


class ScheduleGap(Base):
    __tablename__ = "schedule_gaps"
    id = Column(String(36), primary_key=True, default=identifier)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True)
    config_version = Column(Integer, nullable=False)
    first_slot_at = Column(UTCDateTime, nullable=False)
    last_slot_at = Column(UTCDateTime, nullable=False)
    slot_count = Column(Integer, nullable=False)
    interval_seconds = Column(Integer, nullable=False)
    reason = Column(String(64), default="schedule_missed", nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class DestinationLease(Base):
    __tablename__ = "destination_leases"
    run_id = Column(ForeignKey("check_runs.id", ondelete="CASCADE"), primary_key=True)
    ip = Column(String(45), nullable=False, index=True)
    lease_token = Column(String(36), nullable=False)
    expires_at = Column(UTCDateTime, nullable=False, index=True)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_one_open_incident", "monitor_id", unique=True,
                            postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),)
    id = Column(String(36), primary_key=True, default=identifier)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True)
    config_version = Column(Integer, nullable=False)
    status = Column(String(16), default="open", nullable=False)
    started_at = Column(UTCDateTime, nullable=False)
    detected_at = Column(UTCDateTime, nullable=False)
    recovery_started_at = Column(UTCDateTime)
    resolved_at = Column(UTCDateTime)
    ended_at = Column(UTCDateTime)
    first_check_id = Column(String(36))
    confirming_check_id = Column(String(36))
    last_check_id = Column(String(36))
    reason = Column(String(100))
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class TelegramConnection(Base):
    __tablename__ = "telegram_connections"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    encrypted_chat_id = Column(Text, nullable=False)
    generation = Column(String(36), default=identifier, nullable=False)
    status = Column(String(16), default="connected", nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    used_at = Column(UTCDateTime)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class PollState(Base):
    __tablename__ = "telegram_poll_state"
    id = Column(Integer, primary_key=True, default=1)
    offset = Column(Integer, default=0, nullable=False)


class Notification(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("incident_id", "event_type"),
                     Index("ix_outbox_due", "status", "next_attempt_at"))
    id = Column(String(36), primary_key=True, default=identifier)
    owner_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    incident_id = Column(ForeignKey("incidents.id", ondelete="SET NULL"))
    event_type = Column(String(64), nullable=False)
    generation = Column(String(36))
    encrypted_chat_id = Column(Text)
    payload = Column(JSON, default=dict, nullable=False)
    status = Column(String(16), default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(UTCDateTime, default=utcnow, nullable=False)
    lease_token = Column(String(36))
    lease_expires_at = Column(UTCDateTime)
    error_code = Column(String(64))
    error_message = Column(Text)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
    sent_at = Column(UTCDateTime)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "monitor_id", "key_hash"),)
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    monitor_id = Column(ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False)
    key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    check_id = Column(String(36), nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)


class Heartbeat(Base):
    __tablename__ = "service_heartbeats"
    id = Column(String(100), primary_key=True)
    last_seen_at = Column(UTCDateTime, default=utcnow, nullable=False)
    details = Column(JSON, default=dict, nullable=False)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    id = Column(String(100), primary_key=True)
    count = Column(Integer, default=0, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)


class EmailMessage(Base):
    __tablename__ = "email_outbox"
    id = Column(String(36), primary_key=True, default=identifier)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    encrypted_payload = Column(Text, nullable=False)
    status = Column(String(16), default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(UTCDateTime, default=utcnow, nullable=False)
    created_at = Column(UTCDateTime, default=utcnow, nullable=False)
