"""Shared authentication, authorization, persistent limits, and cursor pagination."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.security import APIKeyCookie, HTTPBearer
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from api_watch.db import utcnow
from api_watch.config import canonical_origin
from api_watch.errors import APIError
from api_watch.models import ApiToken, LoginSession, RateBucket, User
from api_watch.security import csrf_token, digest

cookie_scheme = APIKeyCookie(name="api_watch_session", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def get_db(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def same_origin(request):
    origin = request.headers.get("origin")
    if origin:
        try:
            matched = canonical_origin(origin) == request.app.state.settings.public_base_url
        except (ValueError, UnicodeError):
            matched = False
        if not matched:
            raise APIError(403, "ORIGIN_FORBIDDEN", "This request origin is not allowed.")


def rate_limit(db, key, limit, seconds, *, commit=True):
    now = utcnow()
    window = int(now.timestamp()) // seconds
    identifier = digest(f"{key}:{window}")
    expires = datetime.fromtimestamp((window + 1) * seconds, tz=now.tzinfo)
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(RateBucket).values(id=identifier, count=1, expires_at=expires)
    statement = statement.on_conflict_do_update(
        index_elements=[RateBucket.id], set_={"count": RateBucket.count + 1}
    ).returning(RateBucket.count)
    count = db.scalar(statement)
    if commit:
        db.commit()
    if count > limit:
        delay = max(1, int((expires - now).total_seconds()))
        raise APIError(429, "RATE_LIMITED", "Too many requests. Please try again later.",
                       headers={"Retry-After": str(delay)})


@dataclass
class Principal:
    user: User
    kind: str
    session_id: str | None = None
    raw_session: str | None = None


def principal(request: Request, db: Session = Depends(get_db),
              cookie: str | None = Depends(cookie_scheme), bearer=Depends(bearer_scheme)):
    now = utcnow()
    if cookie and bearer:
        raise APIError(400, "AMBIGUOUS_AUTHENTICATION", "Use either a session or an API token.")
    if bearer:
        token = db.scalar(select(ApiToken).where(ApiToken.token_hash == digest(bearer.credentials)))
        if not token or token.revoked_at or token.expires_at <= now:
            raise APIError(401, "UNAUTHENTICATED", "A valid API token is required.")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and token.scope != "read_write":
            raise APIError(403, "INSUFFICIENT_SCOPE", "This API token is read-only.")
        user = db.get(User, token.user_id)
        token.last_used_at = now
        result = Principal(user, "token")
    elif cookie:
        row = db.scalar(select(LoginSession).where(LoginSession.token_hash == digest(cookie)))
        if not row or row.revoked_at or row.expires_at <= now or row.last_seen_at <= now - timedelta(hours=24):
            raise APIError(401, "UNAUTHENTICATED", "Your session has expired. Please sign in.")
        user = db.get(User, row.user_id)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            same_origin(request)
            received = request.headers.get("X-CSRF-Token", "")
            expected = csrf_token(cookie, request.app.state.settings)
            if not hmac.compare_digest(received, expected):
                raise APIError(403, "CSRF_FAILED", "A valid CSRF token is required.")
        row.last_seen_at = now
        result = Principal(user, "session", row.id, cookie)
    else:
        raise APIError(401, "UNAUTHENTICATED", "Please sign in or provide an API token.")
    if user is None:
        raise APIError(401, "UNAUTHENTICATED", "The account no longer exists.")
    rate_limit(db, f"api:{user.id}", 120, 60)
    return result


def session_principal(auth: Principal = Depends(principal)):
    if auth.kind != "session":
        raise APIError(403, "SESSION_REQUIRED", "This operation requires a browser session.")
    return auth


def lock_live_session_user(db, auth):
    """Serialize credential changes and revalidate a session after acquiring the user lock."""
    user = db.scalar(select(User).where(User.id == auth.user.id).with_for_update(key_share=True)
                     .execution_options(populate_existing=True))
    if not user:
        raise APIError(401, "UNAUTHENTICATED", "The account no longer exists.")
    row = db.scalar(select(LoginSession).where(LoginSession.id == auth.session_id)
                    .execution_options(populate_existing=True))
    now = utcnow()
    if not row or row.revoked_at or row.expires_at <= now or row.last_seen_at <= now - timedelta(hours=24):
        raise APIError(401, "UNAUTHENTICATED", "Your session is no longer valid. Please sign in.")
    return user


def public_user(user):
    return {"id": user.id, "email": user.email, "created_at": user.created_at}


def exact_response(payload, status_code=200, headers=None):
    """Keep exact JSON numeric literals at the monitor configuration boundary."""
    from api_watch.schemas import json_dumps_exact

    def encode_dates(value):
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, dict):
            return {key: encode_dates(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode_dates(item) for item in value]
        return value
    return Response(json_dumps_exact(encode_dates(payload)), status_code=status_code,
                    media_type="application/json", headers=headers)


def cursor_page(db, statement, model, request, limit=20, cursor=None, filters=None):
    if not 1 <= limit <= 100:
        raise APIError(422, "VALIDATION_ERROR", "limit must be between 1 and 100.")
    fingerprint = digest(json.dumps(filters or {}, sort_keys=True, default=str))
    secret = request.app.state.settings.session_secret.encode()
    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), signature):
                raise ValueError("signature")
            data = json.loads(payload)
            if data["filter"] != fingerprint:
                raise ValueError("filters")
            created_at = datetime.fromisoformat(data["created_at"])
            statement = statement.where(or_(model.created_at < created_at,
                and_(model.created_at == created_at, model.id < data["id"])))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise APIError(400, "INVALID_CURSOR", "The pagination cursor is invalid.") from None
    rows = list(db.scalars(statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        payload = json.dumps({"id": last.id, "created_at": last.created_at.isoformat(),
                              "filter": fingerprint}, separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload + hmac.new(secret, payload, hashlib.sha256).digest()).decode().rstrip("=")
    return rows, {"next_cursor": next_cursor, "has_more": more}
