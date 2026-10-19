"""Environment configuration. Production fails closed on unsafe defaults."""

from pathlib import Path
import re
import base64
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def canonical_origin(value):
    """Compare origins as browsers serialize them: lowercase host and no default port."""
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        raise ValueError("An origin cannot contain whitespace or backslashes")
    public = urlsplit(value)
    if (public.scheme not in {"http", "https"} or not public.hostname or public.username
            or public.password or public.path not in {"", "/"} or public.query or public.fragment):
        raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP/HTTPS origin without a path")
    host = public.hostname.encode("idna").decode().lower()
    authority = f"[{host}]" if ":" in host else host
    if public.port and public.port != (443 if public.scheme == "https" else 80):
        authority += f":{public.port}"
    return f"{public.scheme}://{authority}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///var/api-watch.db"
    database_schema: str = "public"
    public_base_url: str = "http://127.0.0.1:8000"
    session_secret: str = ""
    encryption_key: str = ""
    encryption_key_id: str = "local-v1"
    registration_enabled: bool = True
    check_concurrency: int = 10
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    log_level: str = "INFO"
    test_allowed_hosts: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "API Watch <noreply@example.com>"
    smtp_starttls: bool = True
    max_monitors: int = 20
    history_days: int = 30

    @model_validator(mode="after")
    def secure_environment(self):
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test, or production")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.database_schema):
            raise ValueError("DATABASE_SCHEMA must be a lowercase SQL identifier")
        self.public_base_url = canonical_origin(self.public_base_url)
        if self.telegram_bot_token and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", self.telegram_bot_token):
            raise ValueError("TELEGRAM_BOT_TOKEN must have the BotFather token format")
        username = self.telegram_bot_username.strip()
        if username.startswith("@"):
            username = username[1:]
            if not username:
                raise ValueError("TELEGRAM_BOT_USERNAME must contain a bot username after @")
        if username and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            raise ValueError("TELEGRAM_BOT_USERNAME must be a 5 to 32 character bot username, not a URL")
        self.telegram_bot_username = username.lower()
        if self.encryption_key:
            try:
                key = base64.b64decode(self.encryption_key, altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                raise ValueError("ENCRYPTION_KEY must be URL-safe base64 for exactly 32 bytes") from None
            if len(key) != 32:
                raise ValueError("ENCRYPTION_KEY must encode exactly 32 bytes")
        if not self.encryption_key_id or len(self.encryption_key_id) > 100:
            raise ValueError("ENCRYPTION_KEY_ID must contain 1 to 100 characters")
        if self.test_allowed_hosts and self.app_env != "test":
            raise ValueError("TEST_ALLOWED_HOSTS is available only in APP_ENV=test")
        if self.app_env == "production":
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Production PUBLIC_BASE_URL must use HTTPS")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if len(self.session_secret) < 32 or len(self.encryption_key) < 40:
                raise ValueError("Production requires generated session and encryption keys")
            if not self.smtp_host:
                raise ValueError("Production requires SMTP for password recovery")
        if not 1 <= self.check_concurrency <= 100:
            raise ValueError("CHECK_CONCURRENCY must be between 1 and 100")
        return self

    def ensure_local_secrets(self):
        """Persist development keys, shared by web and worker processes."""
        import base64
        import secrets

        if self.app_env == "production":
            return self
        Path("var").mkdir(exist_ok=True)
        for field, filename, size in [
            ("session_secret", "var/session.key", 48),
            ("encryption_key", "var/encryption.key", 32),
        ]:
            if getattr(self, field):
                continue
            path = Path(filename)
            try:
                with path.open("x", encoding="ascii") as handle:
                    handle.write(base64.urlsafe_b64encode(secrets.token_bytes(size)).decode())
                path.chmod(0o600)
            except FileExistsError:
                pass
            setattr(self, field, path.read_text(encoding="ascii").strip())
        return self
