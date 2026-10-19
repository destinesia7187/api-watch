"""Clean-clone secrets setup is repeatable without overwriting existing keys."""

import base64
from pathlib import Path

import pytest
from dotenv import dotenv_values

from api_watch.cli import init_env


def test_init_env_generates_random_secrets_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_env()
    content = Path(".env").read_bytes()
    values = dotenv_values(".env")
    assert len(base64.urlsafe_b64decode(values["ENCRYPTION_KEY"])) == 32
    assert len(values["SESSION_SECRET"]) >= 48
    assert len(values["POSTGRES_PASSWORD"]) >= 24
    assert values["DATABASE_URL"] == "sqlite:///var/api-watch.db"
    with pytest.raises(SystemExit, match="already exists"):
        init_env()
    assert Path(".env").read_bytes() == content


def test_init_env_preserves_earlier_local_database_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("var").mkdir()
    encryption_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    Path("var/encryption.key").write_text(encryption_key)
    Path("var/session.key").write_text("existing" * 8)
    init_env()
    values = dotenv_values(".env")
    assert values["ENCRYPTION_KEY"] == encryption_key
    assert values["SESSION_SECRET"] == "existing" * 8
