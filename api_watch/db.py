"""Database sessions shared by API and independent background processes."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event, select, func
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow():
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return value.astimezone(UTC)
        return None

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def database_now(session):
    if session.bind.dialect.name == "sqlite":
        # CURRENT_TIMESTAMP truncates to whole seconds and can repeat a resumed
        # schedule slot after a quick pause/resume in local development.
        value = session.scalar(select(func.strftime("%Y-%m-%dT%H:%M:%f+00:00", "now")))
        return datetime.fromisoformat(value)
    value = session.scalar(select(func.clock_timestamp()))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def make_database(settings):
    if settings.database_url.startswith("sqlite:///var/"):
        Path("var").mkdir(exist_ok=True)
    kwargs = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in settings.database_url:
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
    elif settings.database_url.startswith("postgresql") and settings.database_schema != "public":
        kwargs["connect_args"] = {"options": f"-csearch_path={settings.database_schema}"}
    engine = create_engine(settings.database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(engine, expire_on_commit=False)
