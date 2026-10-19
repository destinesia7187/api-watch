"""User-confirmed Telegram connections and delivery visibility."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from api_watch.api_utils import Principal, cursor_page, get_db, rate_limit, session_principal
from api_watch.db import utcnow
from api_watch.contracts import NotificationRead, Page, TelegramLinkRead, TelegramRead, TestNotificationRead
from api_watch.errors import APIError
from api_watch.models import Notification, TelegramConnection, TelegramLink, User
from api_watch.security import digest, random_token

router = APIRouter(prefix="/api/v1", tags=["Telegram"])


@router.get("/integrations/telegram", response_model=TelegramRead)
def get_telegram(request: Request, auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    row = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == auth.user.id))
    settings = request.app.state.settings
    return {"connected": bool(row and row.status == "connected"),
            "configured": bool(settings.telegram_bot_token and settings.telegram_bot_username),
            "bot_username": settings.telegram_bot_username or None,
            "connected_at": row.created_at if row else None}


@router.post("/integrations/telegram/link", status_code=201, response_model=TelegramLinkRead)
def telegram_link(request: Request, auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if not settings.telegram_bot_token or not settings.telegram_bot_username:
        raise APIError(503, "TELEGRAM_NOT_CONFIGURED", "Telegram is not configured on this deployment.")
    rate_limit(db, f"tg-link:{auth.user.id}", 5, 600)
    db.scalar(select(User).where(User.id == auth.user.id).with_for_update(key_share=True))
    db.execute(delete(TelegramLink).where(TelegramLink.user_id == auth.user.id))
    raw = random_token()
    expires = utcnow() + timedelta(minutes=10)
    db.add(TelegramLink(user_id=auth.user.id, token_hash=digest(raw), expires_at=expires))
    db.commit()
    return {"url": f"https://t.me/{settings.telegram_bot_username}?start={raw}", "expires_at": expires}


@router.delete("/integrations/telegram", status_code=204)
def disconnect_telegram(auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    db.scalar(select(User).where(User.id == auth.user.id).with_for_update(key_share=True))
    db.execute(delete(TelegramConnection).where(TelegramConnection.user_id == auth.user.id))
    db.execute(delete(TelegramLink).where(TelegramLink.user_id == auth.user.id))
    db.execute(update(Notification).where(Notification.owner_id == auth.user.id,
        Notification.status.in_(["pending", "sending"])).values(status="cancelled", lease_token=None,
            lease_expires_at=None, next_attempt_at=utcnow()))
    db.commit()


@router.post("/integrations/telegram/test", status_code=202, response_model=TestNotificationRead)
def test_telegram(request: Request, response: Response,
                  auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    rate_limit(db, f"tg-test:{auth.user.id}", 3, 600)
    connection = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == auth.user.id))
    if not connection or connection.status != "connected":
        raise APIError(409, "TELEGRAM_NOT_CONNECTED", "Connect your Telegram account first.")
    row = Notification(owner_id=auth.user.id, event_type="test_notification", generation=connection.generation,
        encrypted_chat_id=connection.encrypted_chat_id,
        payload={"text": "API Watch test notification. Your alert channel is connected."})
    db.add(row)
    db.commit()
    response.headers["Location"] = f"/api/v1/notifications/{row.id}"
    return {"notification_id": row.id, "status": row.status}


def notification_view(row):
    return {key: getattr(row, key) for key in (
        "id", "monitor_id", "incident_id", "event_type", "status", "attempts", "next_attempt_at",
        "error_code", "error_message", "created_at", "sent_at")}


@router.get("/notifications", response_model=Page[NotificationRead])
def list_notifications(request: Request, limit: int = 20, cursor: str | None = None,
                       auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    rows, page = cursor_page(db, select(Notification).where(Notification.owner_id == auth.user.id),
        Notification, request, limit, cursor, {"owner": auth.user.id, "resource": "notifications"})
    return {"items": [notification_view(row) for row in rows], **page}


@router.get("/notifications/{notification_id}", response_model=NotificationRead)
def get_notification(notification_id: str, auth: Principal = Depends(session_principal), db: Session = Depends(get_db)):
    row = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.owner_id == auth.user.id))
    if not row:
        raise APIError(404, "NOT_FOUND", "Notification not found.")
    return notification_view(row)
