"""AT-34: dump and restore a disposable PostgreSQL fixture with real pg tools.

Run from the repository root:
  TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/apiwatch_test
  python -m scripts.verify_backup_restore --pg-bin .tools/pgsql/bin

The URL supplies connection parameters only. Its named database is never read
or modified. Two new random databases are created and removed by this script.
Fixture encryption material stays in memory and is never written to the report.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url

from api_watch.app import create_app
from api_watch.config import Settings
from api_watch.db import make_database, utcnow
from api_watch.engine import claim_check, expire_leases, finish_check, schedule_due
from api_watch.models import (
    ApiToken, CheckRun, Incident, Monitor, MonitorConfig, Notification, TelegramConnection, User,
)
from api_watch.schemas import MonitorCreate, public_config
from api_watch.security import decrypt_payload, digest, encrypt_payload, password_hash, random_token


@contextmanager
def isolated_environment(settings):
    values = {
        "APP_ENV": "test", "DATABASE_URL": settings.database_url, "DATABASE_SCHEMA": "public",
        "PUBLIC_BASE_URL": settings.public_base_url, "SESSION_SECRET": settings.session_secret,
        "ENCRYPTION_KEY": settings.encryption_key, "ENCRYPTION_KEY_ID": settings.encryption_key_id,
        "TEST_ALLOWED_HOSTS": "",
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def migrate(settings):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    with isolated_environment(settings):
        command.upgrade(config, "head")
    return ScriptDirectory.from_config(config).get_current_head()


def command_binary(pg_bin, name):
    if pg_bin:
        executable = Path(pg_bin).resolve() / (name + (".exe" if os.name == "nt" else ""))
        if not executable.is_file():
            raise ValueError(f"{name} was not found under --pg-bin")
        return str(executable)
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"Install PostgreSQL client tools or provide --pg-bin for {name}")
    return executable


def execute_tool(executable, arguments, environment, label):
    started = time.monotonic()
    completed = subprocess.run(
        [executable, *arguments], env=environment, capture_output=True, check=False, timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        # Do not publish raw stderr or connection strings into a reusable report.
        raise RuntimeError(f"{label} failed with exit status {completed.returncode}")
    return round(time.monotonic() - started, 3)


def database_snapshot(engine):
    """Compare every restored table, without writing fixture data to a report."""
    metadata = MetaData()
    metadata.reflect(bind=engine)
    fingerprint = hashlib.sha256()
    counts = {}
    with engine.connect() as connection:
        for table_name in sorted(metadata.tables):
            table = metadata.tables[table_name]
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
            canonical = sorted(json.dumps(row, default=str, ensure_ascii=True, sort_keys=True) for row in rows)
            counts[table_name] = len(canonical)
            fingerprint.update(table_name.encode())
            for row in canonical:
                fingerprint.update(row.encode())
    return {"table_row_counts": counts, "sha256": fingerprint.hexdigest()}


def seed_fixture(settings):
    engine, factory = make_database(settings)
    password = random_token() + "-Strong9!"
    target_token, header_secret, access_token = random_token(), random_token(), "aw_" + random_token()
    initial = utcnow() - timedelta(minutes=10)
    raw_config = MonitorCreate(
        name="Backup verification target", description="Disposable recovery fixture",
        url="https://fixture.invalid/health", auth={"type": "bearer", "token": target_token},
        secret_headers=[{"name": "X-API-Key", "value": header_secret}],
        json_assertions=[{"pointer": "/exact", "operator": "equals",
                          "expected": Decimal("1.000000000000000000001")}],
        failure_threshold=3, recovery_threshold=2, notifications_enabled=True,
    ).model_dump()
    with factory.begin() as session:
        user = User(email="backup-verification@example.com", password_hash=password_hash(password))
        session.add(user)
        session.flush()
        monitor = Monitor(owner_id=user.id, name=raw_config["name"], description=raw_config["description"],
                          notifications_enabled=True, next_due_at=initial)
        session.add(monitor)
        session.flush()
        session.add(MonitorConfig(monitor_id=monitor.id, version=1,
            encrypted_payload=encrypt_payload(raw_config, settings), key_id=settings.encryption_key_id,
            interval_seconds=60, timeout_ms=10000, failure_threshold=3, recovery_threshold=2))
        session.add(TelegramConnection(user_id=user.id,
            encrypted_chat_id=encrypt_payload({"chat_id": "123456789"}, settings)))
        session.add(ApiToken(user_id=user.id, name="Recovery read access", prefix=access_token[:12],
            token_hash=digest(access_token), scope="read", expires_at=utcnow() + timedelta(days=1)))
        user_id, monitor_id = user.id, monitor.id
    for offset, outcome in enumerate(["failure"] * 3 + ["success"] * 2 + ["failure"] * 3):
        instant = initial + timedelta(minutes=offset)
        with factory.begin() as session:
            scheduled = schedule_due(session, settings, instant)
            assert scheduled == {"queued": 1, "skipped": 0, "missed": 0}
        with factory.begin() as session:
            run = claim_check(session, settings, instant + timedelta(milliseconds=10))
            assert run is not None
            run_id, lease_token = run.id, run.lease_token
        with factory.begin() as session:
            assert finish_check(session, run_id, lease_token, {
                "outcome": outcome, "http_status": 503 if outcome == "failure" else 200,
                "response_time_ms": 25.5, "elapsed_ms": 25.5, "bytes_received": 15,
                "error_code": "unexpected_status" if outcome == "failure" else None,
                "error_message": "Unexpected HTTP status." if outcome == "failure" else None,
                "failed_assertions": [],
            }, settings, instant + timedelta(milliseconds=40))
    # Capture an ambiguous HTTP operation as running. Recovery must terminate
    # this expired lease rather than replay an operation whose outcome is unknown.
    instant = initial + timedelta(minutes=8)
    with factory.begin() as session:
        schedule_due(session, settings, instant)
    with factory.begin() as session:
        running = claim_check(session, settings, instant + timedelta(milliseconds=10))
        assert running is not None
        running_id = running.id
    with factory() as session:
        assert sorted(row.status for row in session.scalars(select(Incident))) == ["open", "resolved"]
        assert len(list(session.scalars(select(Notification)))) == 3
    return engine, factory, {
        "user_id": user_id, "monitor_id": monitor_id, "running_id": running_id,
        "password": password, "api_token": access_token, "config": raw_config,
        "target_token": target_token, "header_secret": header_secret,
    }


def assert_restored_state(settings, fixture, report):
    engine, factory = make_database(settings)
    try:
        with factory() as session:
            config = session.scalar(select(MonitorConfig).where(MonitorConfig.monitor_id == fixture["monitor_id"]))
            assert decrypt_payload(config.encrypted_payload, settings) == fixture["config"]
            assert config.key_id == settings.encryption_key_id
            report["verification"]["matching_key_decrypts_exact_configuration"] = True
            wrong_key = settings.model_copy(update={"encryption_key": base64.urlsafe_b64encode(os.urandom(32)).decode()})
            try:
                decrypt_payload(config.encrypted_payload, wrong_key)
            except InvalidTag:
                report["verification"]["wrong_key_is_rejected"] = True
            else:
                raise AssertionError("Wrong encryption key was unexpectedly accepted")
            connection = session.scalar(select(TelegramConnection))
            assert decrypt_payload(connection.encrypted_chat_id, settings) == {"chat_id": "123456789"}
            incidents = list(session.scalars(select(Incident)))
            checks = {row.id: row for row in session.scalars(select(CheckRun))}
            assert len(incidents) == 2 and len(checks) == 9
            for incident in incidents:
                for check_id in [incident.first_check_id, incident.confirming_check_id, incident.last_check_id]:
                    assert check_id in checks and checks[check_id].monitor_id == incident.monitor_id
            events = list(session.scalars(select(Notification)))
            assert len(events) == 3 and all(row.status == "pending" for row in events)
            assert len({(row.incident_id, row.event_type) for row in events}) == 3
            for event in events:
                incident = session.get(Incident, event.incident_id)
                assert incident and incident.monitor_id == event.monitor_id
                assert event.owner_id == fixture["user_id"]
                assert decrypt_payload(event.encrypted_chat_id, settings) == {"chat_id": "123456789"}
            report["verification"]["history_incident_and_outbox_references_consistent"] = True
            report["verification"]["encrypted_telegram_recipient_snapshots_decrypt"] = True
        with factory.begin() as session:
            assert expire_leases(session, settings) == 1
        with factory() as session:
            running = session.get(CheckRun, fixture["running_id"])
            assert (running.status, running.outcome, running.error_code) == ("finished", "interrupted", "worker_lost")
            assert session.scalar(select(CheckRun.id).where(CheckRun.status.in_(["queued", "running"]))) is None
            assert len(list(session.scalars(select(Notification)))) == 3
            report["verification"]["expired_running_lease_interrupted_without_replay"] = True
        application = create_app(settings)
        try:
            with TestClient(application) as client:
                ready = client.get("/health/ready")
                assert ready.status_code == 200 and ready.json()["status"] == "ready"
                report["verification"]["restored_application_ready"] = True
                login = client.post("/api/v1/auth/login", json={
                    "email": "backup-verification@example.com", "password": fixture["password"]})
                assert login.status_code == 200
                report["verification"]["restored_user_can_sign_in"] = True
                view = client.get(f"/api/v1/monitors/{fixture['monitor_id']}")
                assert view.status_code == 200
                public = public_config(fixture["config"])
                assert view.json()["auth"] == public["auth"]
                assert view.json()["secret_headers"] == public["secret_headers"]
                assert fixture["target_token"] not in view.text and fixture["header_secret"] not in view.text
                assert '1.000000000000000000001' in view.text
                report["verification"]["restored_api_redacts_secrets_and_preserves_exact_json"] = True
                client.cookies.clear()
                bearer = client.get(f"/api/v1/monitors/{fixture['monitor_id']}",
                    headers={"Authorization": "Bearer " + fixture["api_token"]})
                assert bearer.status_code == 200
                report["verification"]["restored_api_token_authenticates"] = True
        finally:
            application.state.engine.dispose()
    finally:
        engine.dispose()


def verify(args):
    if not __debug__:
        raise RuntimeError("Run this verification without Python optimization so assertions remain enabled")
    url = make_url(os.environ.get("TEST_DATABASE_URL", ""))
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("TEST_DATABASE_URL must reference an explicitly local disposable PostgreSQL server")
    if not url.username:
        raise ValueError("TEST_DATABASE_URL must contain an explicit database user")
    pg_dump = command_binary(args.pg_bin, "pg_dump")
    pg_restore = command_binary(args.pg_bin, "pg_restore")
    suffix = uuid4().hex[:12]
    source_name, restored_name = f"apiwatch_backup_source_{suffix}", f"apiwatch_backup_restore_{suffix}"
    created = []
    engines = []
    started = time.monotonic()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    archive = output.parent / f"backup-drill-{suffix}.dump"
    report = {"recorded_at": utcnow().isoformat(), "status": "failed", "database": "PostgreSQL",
        "scope": "Two unique disposable databases; the TEST_DATABASE_URL database is not modified",
        "source_database": source_name, "restored_database": restored_name,
        "verification": {}, "cleanup": {"disposable_databases_removed": False, "dump_removed": False}}
    administrator = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username,
        password=url.password, dbname="postgres", autocommit=True, connect_timeout=10)
    try:
        report["postgresql_version"] = administrator.execute("SELECT version()").fetchone()[0]
        for name in [source_name, restored_name]:
            if not re.fullmatch(r"apiwatch_backup_(source|restore)_[a-f0-9]{12}", name):
                raise ValueError("Unsafe disposable database name")
            administrator.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
            created.append(name)
        source_url = url.set(database=source_name).render_as_string(hide_password=False)
        restore_url = url.set(database=restored_name).render_as_string(hide_password=False)
        settings = Settings(app_env="test", database_url=source_url, public_base_url="http://backup-drill.invalid",
            database_schema="public", encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
            encryption_key_id="backup-drill-v1", session_secret=random_token(),
            telegram_bot_token="unused-fixture-bot-token", _env_file=None)
        report["migration_revision"] = migrate(settings)
        source_engine, _factory, fixture = seed_fixture(settings)
        engines.append(source_engine)
        source_snapshot = database_snapshot(source_engine)
        report["fixture_row_counts"] = source_snapshot["table_row_counts"]
        report["source_snapshot_sha256"] = source_snapshot["sha256"]
        environment = os.environ.copy()
        for key in ["PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"]:
            environment.pop(key, None)
        environment.update(PGAPPNAME="api-watch-backup-drill", PGCONNECT_TIMEOUT="10")
        if url.password is not None:
            environment["PGPASSWORD"] = url.password
        common = ["--host", url.host, "--port", str(url.port or 5432), "--username", url.username]
        report["dump_seconds"] = execute_tool(pg_dump,
            [*common, "--dbname", source_name, "--format=custom", "--no-owner", "--no-acl", "--file", str(archive)],
            environment, "pg_dump")
        archive_bytes = archive.read_bytes()
        report["dump_bytes"] = len(archive_bytes)
        report["dump_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
        report["restore_seconds"] = execute_tool(pg_restore,
            [*common, "--dbname", restored_name, "--exit-on-error", "--no-owner", "--no-acl", str(archive)],
            environment, "pg_restore")
        # Reconstruct settings from a separate in-memory key backup. The SQL dump
        # contains ciphertext and key IDs, but cannot supply its own AES key.
        key_backup = json.loads(json.dumps({name: getattr(settings, name) for name in
            ["encryption_key", "encryption_key_id", "session_secret"]}))
        restored_settings = settings.model_copy(update={"database_url": restore_url, **key_backup})
        restored_engine = create_engine(restore_url)
        engines.append(restored_engine)
        restored_snapshot = database_snapshot(restored_engine)
        assert restored_snapshot == source_snapshot
        report["restored_snapshot_sha256"] = restored_snapshot["sha256"]
        report["verification"]["all_tables_match_dump_snapshot"] = True
        report["verification"]["migration_revision_restored"] = migrate(restored_settings) == report["migration_revision"]
        with restored_engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == report["migration_revision"]
        assert_restored_state(restored_settings, fixture, report)
        report["verification"]["no_external_http_or_telegram_requests_issued"] = True
        report["status"] = "passed"
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        for engine in engines:
            engine.dispose()
        cleanup_errors = []
        for name in reversed(created):
            try:
                administrator.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        administrator.close()
        report["cleanup"]["disposable_databases_removed"] = not cleanup_errors
        if cleanup_errors:
            report["cleanup"]["error_types"] = cleanup_errors
            report["status"] = "failed"
        if archive.exists():
            archive.unlink()
        report["cleanup"]["dump_removed"] = not archive.exists()
        report["total_seconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", default="", help="Directory containing pg_dump and pg_restore")
    parser.add_argument("--output", default="var/backup-report.json")
    args = parser.parse_args()
    raise SystemExit(0 if verify(args) else 1)


if __name__ == "__main__":
    main()
