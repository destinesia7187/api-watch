"""Explicit migrations and supervised process entry points."""

import argparse
import asyncio
import base64
import json
import logging
import secrets
import signal
from pathlib import Path


def migrate(settings):
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = settings.database_url
    command.upgrade(cfg, "head")


def init_env():
    path = Path(".env")
    if path.exists():
        raise SystemExit(".env already exists; it has not been changed.")
    session_path, encryption_path = Path("var/session.key"), Path("var/encryption.key")
    session_key = session_path.read_text().strip() if session_path.exists() else secrets.token_urlsafe(48)
    encryption_key = encryption_path.read_text().strip() if encryption_path.exists() else base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    path.write_text(
        "APP_ENV=development\nPUBLIC_BASE_URL=http://127.0.0.1:8000\n"
        "DATABASE_URL=sqlite:///var/api-watch.db\n"
        f"SESSION_SECRET={session_key}\n"
        f"ENCRYPTION_KEY={encryption_key}\n"
        "ENCRYPTION_KEY_ID=local-v1\n"
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}\n"
        "REGISTRATION_ENABLED=true\nCHECK_CONCURRENCY=10\n"
        "TELEGRAM_BOT_TOKEN=\nTELEGRAM_BOT_USERNAME=\n"
        "SMTP_HOST=\nSMTP_PORT=587\nSMTP_USERNAME=\nSMTP_PASSWORD=\n"
        "SMTP_FROM=API Watch <noreply@example.com>\nSMTP_STARTTLS=true\n",
        encoding="utf-8")
    path.chmod(0o600)
    print("Created .env with random local secrets. Keep it out of version control.")


async def background_with_mail(factory, settings, stop_event):
    from api_watch.engine import run_background
    from api_watch.mail import dispatch_mail

    async def mail_loop():
        while not stop_event.is_set():
            await asyncio.to_thread(dispatch_mail, factory, settings)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2)
            except TimeoutError:
                pass
    await asyncio.gather(run_background(factory, settings, stop_event), mail_loop())


async def supervise(factory, settings, command):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
    if command == "worker":
        from api_watch.engine import run_worker
        await run_worker(factory, settings, stop)
    else:
        await background_with_mail(factory, settings, stop)


def main():
    parser = argparse.ArgumentParser(description="API Watch process management")
    parser.add_argument("command", choices=["init-env", "migrate", "web", "worker", "background", "cleanup", "status", "export-openapi"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    if args.command == "init-env":
        return init_env()
    from api_watch.config import Settings
    from api_watch.db import make_database
    settings = Settings().ensure_local_secrets()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP clients must never log the Bot API URL, which contains a credential.
    logging.getLogger("aiohttp.client").setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "migrate":
        migrate(settings)
        print("Database migrations applied.")
        return
    if args.command == "web":
        import uvicorn
        uvicorn.run("api_watch.app:app", host=args.host, port=args.port, access_log=False)
        return
    if args.command == "export-openapi":
        from api_watch.app import create_app
        Path("docs").mkdir(exist_ok=True)
        Path("docs/openapi.json").write_text(
            json.dumps(create_app(settings).openapi(), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print("Exported docs/openapi.json")
        return
    engine, factory = make_database(settings)
    try:
        if args.command == "worker":
            asyncio.run(supervise(factory, settings, "worker"))
        elif args.command == "background":
            asyncio.run(supervise(factory, settings, "background"))
        elif args.command == "cleanup":
            from api_watch.engine import cleanup
            with factory() as db:
                print(cleanup(db, settings))
                db.commit()
        elif args.command == "status":
            from sqlalchemy import select
            from api_watch.models import Heartbeat
            with factory() as db:
                for row in db.scalars(select(Heartbeat)):
                    print(f"{row.id}: {row.last_seen_at.isoformat()}")
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
