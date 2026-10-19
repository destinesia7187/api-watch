"""Password-reset delivery via SMTP or an ignored local development mailbox."""

import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage as SMTPMessage
from pathlib import Path

from sqlalchemy import select

from api_watch.db import utcnow
from api_watch.models import EmailMessage
from api_watch.security import decrypt_payload


def dispatch_mail(factory, settings):
    with factory() as db:
        row = db.scalar(select(EmailMessage).where(EmailMessage.status == "pending",
            EmailMessage.next_attempt_at <= utcnow()).order_by(EmailMessage.created_at).with_for_update(skip_locked=True))
        if not row:
            return False
        row.attempts += 1
        try:
            payload = decrypt_payload(row.encrypted_payload, settings)
            if settings.smtp_host:
                message = SMTPMessage()
                message["From"] = settings.smtp_from
                message["To"] = payload["to"]
                message["Subject"] = payload["subject"]
                message.set_content(payload["text"])
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                    if settings.smtp_starttls:
                        smtp.starttls(context=ssl.create_default_context())
                    if settings.smtp_username:
                        smtp.login(settings.smtp_username, settings.smtp_password)
                    smtp.send_message(message)
            elif settings.app_env != "production":
                directory = Path("var/mail")
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{row.id}.txt"
                path.write_text(f"To: {payload['to']}\nSubject: {payload['subject']}\n\n{payload['text']}", encoding="utf-8")
                path.chmod(0o600)
            else:
                raise RuntimeError("SMTP is required")
            row.status = "sent"
            row.encrypted_payload = ""
        except Exception:
            if row.attempts >= 6:
                row.status = "failed"
                row.encrypted_payload = ""
            else:
                row.next_attempt_at = utcnow() + timedelta(seconds=min(480, 30 * 2 ** (row.attempts - 1)))
        db.commit()
        return True
