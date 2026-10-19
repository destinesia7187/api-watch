"""Offline authenticated-encryption key rotation. Stop all application processes first.

Use current keys in .env and NEW_ENCRYPTION_KEY / NEW_ENCRYPTION_KEY_ID in the
environment. Secrets are never accepted as command arguments or printed.
"""

import os

from sqlalchemy import select

from api_watch.config import Settings
from api_watch.db import make_database
from api_watch.models import EmailMessage, MonitorConfig, Notification, TelegramConnection
from api_watch.security import decrypt_payload, encrypt_payload


def main():
    current = Settings().ensure_local_secrets()
    key = os.environ.get("NEW_ENCRYPTION_KEY", "")
    key_id = os.environ.get("NEW_ENCRYPTION_KEY_ID", "")
    if not key or not key_id or key_id == current.encryption_key_id:
        raise SystemExit("Set a generated NEW_ENCRYPTION_KEY and a distinct NEW_ENCRYPTION_KEY_ID first.")
    replacement = type(current)(**{**current.model_dump(), "encryption_key": key,
                                   "encryption_key_id": key_id}, _env_file=None)
    # Validate the complete restart configuration before any database changes.
    decrypt_payload(encrypt_payload({"rotation": "probe"}, replacement), replacement)
    engine, factory = make_database(current)
    count = 0
    try:
        with factory.begin() as db:
            for model, field in [(MonitorConfig, "encrypted_payload"), (TelegramConnection, "encrypted_chat_id"),
                                 (Notification, "encrypted_chat_id"), (EmailMessage, "encrypted_payload")]:
                for row in db.scalars(select(model).where(getattr(model, field).is_not(None)).with_for_update()):
                    value = getattr(row, field)
                    if not value:
                        continue
                    setattr(row, field, encrypt_payload(decrypt_payload(value, current), replacement))
                    if model is MonitorConfig:
                        row.key_id = key_id
                    count += 1
        print(f"Rotated {count} encrypted records in one transaction.")
        print("Update ENCRYPTION_KEY and ENCRYPTION_KEY_ID in your secret store before restarting any process.")
        print("Keep the old key with old backups; do not commit either key.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
