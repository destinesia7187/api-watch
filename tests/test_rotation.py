"""Offline rotation preserves encrypted records and rolls back partial failures."""

import base64

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select

from api_watch.models import EmailMessage, MonitorConfig, Notification, TelegramConnection, User
from api_watch.security import decrypt_payload, encrypt_payload
from scripts import rotate_key


def rotation_fixture(app, monitor, monkeypatch):
    settings = app.state.settings
    with app.state.session_factory.begin() as db:
        owner = db.scalar(select(User))
        db.add(TelegramConnection(user_id=owner.id, encrypted_chat_id=encrypt_payload({"chat_id": "123"}, settings)))
        db.add(Notification(owner_id=owner.id, monitor_id=monitor["id"], event_type="test",
                            encrypted_chat_id=encrypt_payload({"chat_id": "123"}, settings)))
        db.add(EmailMessage(user_id=owner.id, encrypted_payload=encrypt_payload({"text": "reset fixture"}, settings)))
    monkeypatch.setattr(rotate_key, "Settings", lambda: settings)
    new_key = base64.urlsafe_b64encode(b"r" * 32).decode()
    monkeypatch.setenv("NEW_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("NEW_ENCRYPTION_KEY_ID", "rotated-v2")
    replacement = settings.model_copy(update={"encryption_key": new_key, "encryption_key_id": "rotated-v2"})
    return settings, replacement


FIELDS = [(MonitorConfig, "encrypted_payload"), (TelegramConnection, "encrypted_chat_id"),
          (Notification, "encrypted_chat_id"), (EmailMessage, "encrypted_payload")]


def snapshots(app):
    with app.state.session_factory() as db:
        return {(model.__tablename__, row.id): getattr(row, field)
                for model, field in FIELDS for row in db.scalars(select(model))}


def test_rotation_reencrypts_all_record_types(app, monitor, monkeypatch):
    current, replacement = rotation_fixture(app, monitor, monkeypatch)
    before = snapshots(app)
    rotate_key.main()
    after = snapshots(app)
    assert before.keys() == after.keys() and len(after) == 4
    for key, old in before.items():
        assert after[key] != old
        assert decrypt_payload(after[key], replacement) == decrypt_payload(old, current)
        with pytest.raises(InvalidTag):
            decrypt_payload(after[key], current)
    with app.state.session_factory() as db:
        assert db.scalar(select(MonitorConfig)).key_id == "rotated-v2"


def test_rotation_failure_rolls_back_previously_updated_records(app, monitor, monkeypatch):
    rotation_fixture(app, monitor, monkeypatch)
    with app.state.session_factory.begin() as db:
        db.scalar(select(EmailMessage)).encrypted_payload = base64.urlsafe_b64encode(b"bad" * 20).decode()
    before = snapshots(app)
    with pytest.raises(InvalidTag):
        rotate_key.main()
    assert snapshots(app) == before
    with app.state.session_factory() as db:
        assert db.scalar(select(MonitorConfig)).key_id == "local-v1"


def test_invalid_replacement_key_is_rejected_before_writes(app, monitor, monkeypatch):
    rotation_fixture(app, monitor, monkeypatch)
    before = snapshots(app)
    monkeypatch.setenv("NEW_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(ValueError):
        rotate_key.main()
    assert snapshots(app) == before


def test_invalid_replacement_key_id_cannot_make_restart_configuration_unusable(app, monitor, monkeypatch):
    rotation_fixture(app, monitor, monkeypatch)
    before = snapshots(app)
    monkeypatch.setenv("NEW_ENCRYPTION_KEY_ID", "x" * 101)
    with pytest.raises(ValueError):
        rotate_key.main()
    assert snapshots(app) == before
