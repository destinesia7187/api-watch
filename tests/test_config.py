import base64

import pytest
from pydantic import ValidationError

from api_watch.config import Settings
from api_watch.security import decrypt_payload, encrypt_payload


def test_production_fails_closed_on_unsafe_configuration():
    with pytest.raises(ValidationError):
        Settings(app_env="production", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(app_env="development", test_allowed_hosts="127.0.0.1", _env_file=None)


def test_encryption_is_authenticated_and_bound_to_key_id():
    settings = Settings(app_env="test", session_secret="t" * 48,
        encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(), _env_file=None)
    ciphertext = encrypt_payload({"token": "private"}, settings)
    assert "private" not in ciphertext
    assert decrypt_payload(ciphertext, settings) == {"token": "private"}
    with pytest.raises(Exception):
        decrypt_payload(ciphertext, settings.model_copy(update={"encryption_key_id": "wrong"}))


@pytest.mark.parametrize("raw,expected", [
    ("https://Example.com:443/", "https://example.com"),
    ("http://Example.com:80", "http://example.com"),
    ("http://[::1]:8000/", "http://[::1]:8000"),
])
def test_public_origin_matches_browser_serialization(raw, expected):
    assert Settings(public_base_url=raw, _env_file=None).public_base_url == expected


@pytest.mark.parametrize("token", ["bad", "1234:short", "1234:" + "x" * 20 + "/path",
                                    "1234:" + "x" * 20 + "?q=1", " 1234:" + "x" * 20])
def test_telegram_configuration_rejects_malformed_nonempty_tokens(token):
    with pytest.raises(ValidationError, match="BotFather token format"):
        Settings(telegram_bot_token=token, _env_file=None)


@pytest.mark.parametrize("username", ["https://t.me/test_bot", "test_bot?start=secret",
                                       "test_bot/path", "@@test_bot", "@", "bad name", "a" * 33, "a\x00bot"])
def test_telegram_username_is_a_safe_name_not_a_link(username):
    with pytest.raises(ValidationError, match="bot username"):
        Settings(telegram_bot_username=username, _env_file=None)


def test_telegram_username_normalizes_at_prefix_case_and_surrounding_space():
    settings = Settings(telegram_bot_token="123456:" + "t" * 32,
                        telegram_bot_username=" @API_Watch_Test_Bot ", _env_file=None)
    assert settings.telegram_bot_username == "api_watch_test_bot"


def test_unconfigured_telegram_remains_optional():
    assert Settings(telegram_bot_token="", telegram_bot_username="", _env_file=None).telegram_bot_token == ""
