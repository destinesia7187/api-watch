"""Account lifecycle, revocable browser sessions and personal API tokens."""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api_watch.api_utils import (Principal, cursor_page, get_db, public_user, rate_limit,
                                 same_origin, session_principal, lock_live_session_user)
from api_watch.db import utcnow
from api_watch.contracts import CsrfRead, LoginRead, MessageRead, Page, TokenCreatedRead, TokenRead, UserRead
from api_watch.errors import APIError
from api_watch.models import ApiToken, EmailMessage, LoginSession, Monitor, PasswordReset, User
from api_watch.security import csrf_token, digest, encrypt_payload, password_hash, random_token, verify_password

router = APIRouter(prefix="/api/v1", tags=["Accounts"])
COOKIE = "api_watch_session"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize(cls, value):
        return str(value).strip().lower()

    @field_validator("password")
    @classmethod
    def password_size(cls, value):
        if len(value.encode()) > 512:
            raise ValueError("Password must not exceed 512 UTF-8 bytes")
        return value


class LoginCredentials(StrictModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=512)


class ForgotPassword(StrictModel):
    email: EmailStr


class ResetPassword(StrictModel):
    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=128)


class ChangePassword(StrictModel):
    current_password: str = Field(max_length=512)
    new_password: str = Field(min_length=12, max_length=128)


class DeleteAccount(StrictModel):
    password: str = Field(max_length=512)


class TokenCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["read", "read_write"] = "read"
    expires_days: int = Field(default=90, ge=1, le=365)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A nonempty token name without null characters is required")
        return value


def revoke_credentials(db, user_id):
    now = utcnow()
    db.execute(update(LoginSession).where(LoginSession.user_id == user_id).values(revoked_at=now))
    db.execute(update(ApiToken).where(ApiToken.user_id == user_id).values(revoked_at=now))


def clear_cookie(response):
    response.delete_cookie(COOKIE, path="/")


@router.post("/auth/register", status_code=201, response_model=UserRead)
def register(body: Credentials, request: Request, db: Session = Depends(get_db)):
    same_origin(request)
    settings = request.app.state.settings
    if not settings.registration_enabled:
        raise APIError(403, "REGISTRATION_DISABLED", "Registration is disabled on this deployment.")
    rate_limit(db, f"register:{request.client.host}", 5, 3600)
    user = User(email=str(body.email), password_hash=password_hash(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise APIError(409, "EMAIL_EXISTS", "An account with this email already exists.") from None
    return public_user(user)


@router.post("/auth/login", response_model=LoginRead)
def login(body: LoginCredentials, request: Request, response: Response, db: Session = Depends(get_db)):
    same_origin(request)
    email = body.email.strip().lower()
    if "\x00" in email:
        raise APIError(401, "INVALID_CREDENTIALS", "Invalid email or password.")
    rate_limit(db, f"login-ip:{request.client.host}", 10, 900)
    rate_limit(db, f"login-email:{email}", 10, 900)
    user = db.scalar(select(User).where(User.email == email).with_for_update(key_share=True))
    # A real Argon2 calculation is also performed for unknown accounts.
    encoded = user.password_hash if user else request.app.state.dummy_password_hash
    valid = verify_password(encoded, body.password)
    if not user or not valid:
        raise APIError(401, "INVALID_CREDENTIALS", "Invalid email or password.")
    raw = random_token()
    now = utcnow()
    old_cookie = request.cookies.get(COOKIE)
    if old_cookie:
        db.execute(update(LoginSession).where(LoginSession.token_hash == digest(old_cookie)).values(revoked_at=now))
    db.add(LoginSession(user_id=user.id, token_hash=digest(raw), expires_at=now + timedelta(days=7)))
    db.commit()
    response.set_cookie(COOKIE, raw, max_age=7 * 86400, httponly=True, samesite="lax", path="/",
                        secure=request.app.state.settings.app_env == "production")
    return {"user": public_user(user), "csrf_token": csrf_token(raw, request.app.state.settings)}


@router.get("/auth/me", response_model=UserRead)
def me(auth: Principal = Depends(session_principal)):
    return public_user(auth.user)


@router.get("/auth/csrf", response_model=CsrfRead)
def csrf(request: Request, auth: Principal = Depends(session_principal)):
    return {"csrf_token": csrf_token(auth.raw_session, request.app.state.settings)}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    same_origin(request)
    raw = request.cookies.get(COOKIE)
    if raw:
        row = db.scalar(select(LoginSession).where(LoginSession.token_hash == digest(raw)))
        if row and not row.revoked_at:
            import hmac
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf_token(raw, request.app.state.settings)):
                raise APIError(403, "CSRF_FAILED", "A valid CSRF token is required.")
            row.revoked_at = utcnow()
            db.commit()
    clear_cookie(response)


@router.post("/auth/password/forgot", status_code=202, response_model=MessageRead)
def forgot_password(body: ForgotPassword, request: Request, db: Session = Depends(get_db)):
    same_origin(request)
    email = str(body.email).lower()
    rate_limit(db, f"forgot-ip:{request.client.host}", 5, 3600)
    rate_limit(db, f"forgot-email:{email}", 3, 3600)
    user = db.scalar(select(User).where(User.email == email).with_for_update(key_share=True))
    if user:
        now = utcnow()
        db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None)).values(used_at=now))
        raw = random_token()
        db.add(PasswordReset(user_id=user.id, token_hash=digest(raw), expires_at=now + timedelta(minutes=30)))
        url = f"{request.app.state.settings.public_base_url}/#reset={raw}"
        payload = {"to": user.email, "subject": "Reset your API Watch password",
                   "text": f"Use this link within 30 minutes to reset your password:\n\n{url}\n\nIf you did not request this, ignore this message."}
        db.add(EmailMessage(user_id=user.id, encrypted_payload=encrypt_payload(payload, request.app.state.settings)))
        db.commit()
    return {"message": "If an account exists, a password reset message will be delivered."}


@router.post("/auth/password/reset", response_model=MessageRead)
def reset_password(body: ResetPassword, request: Request, response: Response, db: Session = Depends(get_db)):
    same_origin(request)
    if len(body.new_password.encode()) > 512:
        raise APIError(422, "VALIDATION_ERROR", "Password is too long.")
    rate_limit(db, f"reset:{request.client.host}", 10, 900)
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == digest(body.token)))
    if not row:
        raise APIError(422, "INVALID_RESET_TOKEN", "The reset link is invalid or expired.")
    user = db.scalar(select(User).where(User.id == row.user_id).with_for_update(key_share=True))
    if not user:
        raise APIError(422, "INVALID_RESET_TOKEN", "The reset link is invalid or expired.")
    db.refresh(row, with_for_update=True)
    if row.used_at or row.expires_at <= utcnow():
        raise APIError(422, "INVALID_RESET_TOKEN", "The reset link is invalid or expired.")
    user.password_hash = password_hash(body.new_password)
    row.used_at = utcnow()
    revoke_credentials(db, user.id)
    db.commit()
    clear_cookie(response)
    return {"message": "Password reset. Please sign in again."}


@router.post("/auth/password", response_model=MessageRead)
def change_password(body: ChangePassword, request: Request, response: Response,
                    auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    user = lock_live_session_user(db, auth)
    if not verify_password(user.password_hash, body.current_password):
        raise APIError(422, "INVALID_PASSWORD", "The current password is incorrect.")
    if len(body.new_password.encode()) > 512:
        raise APIError(422, "VALIDATION_ERROR", "Password is too long.")
    user.password_hash = password_hash(body.new_password)
    revoke_credentials(db, user.id)
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id).values(used_at=utcnow()))
    db.commit()
    clear_cookie(response)
    return {"message": "Password changed. Please sign in again."}


@router.delete("/auth/account", status_code=204)
def delete_account(body: DeleteAccount, request: Request, response: Response,
                   auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    user = lock_live_session_user(db, auth)
    if not verify_password(user.password_hash, body.password):
        raise APIError(422, "INVALID_PASSWORD", "The password is incorrect.")
    from api_watch.engine import cancel_monitor_runs
    monitors = db.scalars(select(Monitor).where(Monitor.owner_id == auth.user.id).with_for_update()).all()
    for monitor in monitors:
        cancel_monitor_runs(db, monitor, "monitor_deleted")
    db.execute(delete(User).where(User.id == auth.user.id))
    db.commit()
    clear_cookie(response)


def public_token(row):
    return {key: getattr(row, key) for key in (
        "id", "name", "prefix", "scope", "created_at", "expires_at", "revoked_at", "last_used_at")}


@router.get("/api-tokens", tags=["API tokens"], response_model=Page[TokenRead])
def list_tokens(request: Request, limit: int = 20, cursor: str | None = None,
                auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    rows, page = cursor_page(db, select(ApiToken).where(ApiToken.user_id == auth.user.id),
                             ApiToken, request, limit, cursor, {"owner": auth.user.id, "resource": "tokens"})
    return {"items": [public_token(row) for row in rows], **page}


@router.post("/api-tokens", status_code=201, tags=["API tokens"], response_model=TokenCreatedRead)
def create_token(body: TokenCreate, auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    lock_live_session_user(db, auth)
    count = db.scalar(select(func.count()).select_from(ApiToken).where(
        ApiToken.user_id == auth.user.id, ApiToken.revoked_at.is_(None), ApiToken.expires_at > utcnow()))
    if count >= 5:
        raise APIError(422, "TOKEN_LIMIT", "You can have at most five active API tokens.")
    raw = "aw_" + random_token()
    row = ApiToken(user_id=auth.user.id, name=body.name.strip(), prefix=raw[:10], token_hash=digest(raw),
                   scope=body.scope, expires_at=utcnow() + timedelta(days=body.expires_days))
    db.add(row)
    db.commit()
    return {**public_token(row), "token": raw}


@router.delete("/api-tokens/{token_id}", status_code=204, tags=["API tokens"])
def revoke_token(token_id: str, auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    row = db.scalar(select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == auth.user.id))
    if not row:
        raise APIError(404, "NOT_FOUND", "API token not found.")
    row.revoked_at = utcnow()
    db.commit()
