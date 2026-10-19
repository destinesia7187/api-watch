"""Reproduce a real HTTP outage/recovery with a disposable database and virtual slots.

No public target, production database, or real Telegram account is contacted.
The HTTP exchange is real; only scheduled timestamps are advanced for a fast demo.
"""

import asyncio
import base64
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from sqlalchemy import select

from api_watch.checker import execute_check
from api_watch.config import Settings
from api_watch.db import Base, make_database, utcnow
from api_watch.engine import claim_check, finish_check, schedule_due
from api_watch.models import Incident, Monitor, MonitorConfig, Notification, User
from api_watch.schemas import MonitorCreate
from api_watch.security import encrypt_payload, password_hash


async def demo():
    target = {"status": 200}

    async def health(request):
        return web.json_response({"status": "ok" if target["status"] == 200 else "unavailable"}, status=target["status"])

    application = web.Application()
    application.router.add_get("/health", health)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    folder = Path("var/demo") / uuid4().hex
    folder.mkdir(parents=True)
    settings = Settings(app_env="test", database_url=f"sqlite:///{folder / 'demo.db'}",
        public_base_url="http://apiwatch.invalid", test_allowed_hosts="127.0.0.1",
        encryption_key=base64.urlsafe_b64encode(b"d" * 32).decode(), session_secret="d" * 48, _env_file=None)
    engine, factory = make_database(settings)
    Base.metadata.create_all(engine)
    config = MonitorCreate(name="Controlled demo API", url=f"http://127.0.0.1:{port}/health",
        notifications_enabled=True,
        json_assertions=[{"pointer": "/status", "operator": "equals", "expected": "ok"}]).model_dump()
    now = utcnow()
    with factory.begin() as db:
        user = User(email="demo@example.com", password_hash=password_hash("Disposable demo password"))
        db.add(user)
        db.flush()
        monitor = Monitor(owner_id=user.id, name=config["name"], notifications_enabled=True, next_due_at=now)
        db.add(monitor)
        db.flush()
        mid = monitor.id
        db.add(MonitorConfig(monitor_id=mid, version=1, encrypted_payload=encrypt_payload(config, settings),
            interval_seconds=60, timeout_ms=10000, failure_threshold=3, recovery_threshold=2))
    try:
        print("Controlled target: real local HTTP; virtual scheduler advances one minute per step.")
        for step, status in enumerate([200, 503, 503, 503, 200, 200]):
            target["status"] = status
            tick = now + timedelta(minutes=step)
            with factory.begin() as db:
                schedule_due(db, settings, tick)
                run = claim_check(db, settings, tick)
                run_id, lease = run.id, run.lease_token
            result = await execute_check(config, settings)
            with factory.begin() as db:
                assert finish_check(db, run_id, lease, result, settings, tick + timedelta(seconds=1))
                current = db.get(Monitor, mid)
                incidents = db.scalars(select(Incident).where(Incident.monitor_id == mid)).all()
                print(f"{step + 1}. HTTP {status} -> {result['outcome']:7s} health={current.health_status:10s} incidents={len(incidents)}")
        with factory() as db:
            incident = db.scalar(select(Incident).where(Incident.monitor_id == mid))
            events = db.scalars(select(Notification).where(Notification.monitor_id == mid)).all()
            assert incident.status == "resolved"
            assert {event.event_type for event in events} == {"incident_opened", "incident_resolved"}
            print("PASS: one incident opened after three failures and resolved after two successes.")
            print("Two durable Telegram events were recorded as suppressed because no chat was connected.")
            print("Real Bot API delivery is covered by the isolated adapter tests and enabled by your own bot configuration.")
    finally:
        await runner.cleanup()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(demo())
