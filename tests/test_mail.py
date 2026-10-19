from pathlib import Path

from sqlalchemy import select

from api_watch.mail import dispatch_mail
from api_watch.models import EmailMessage


def test_local_password_reset_mail_is_delivered_without_logging_secret(signed_in, app, tmp_path, monkeypatch):
    signed_in.post("/api/v1/auth/password/forgot", json={"email": "owner@example.com"})
    monkeypatch.chdir(tmp_path)
    assert dispatch_mail(app.state.session_factory, app.state.settings)
    files = list(Path("var/mail").glob("*.txt"))
    assert len(files) == 1 and "#reset=" in files[0].read_text()
    with app.state.session_factory() as db:
        row = db.scalar(select(EmailMessage))
        assert row.status == "sent" and row.encrypted_payload == ""


def test_smtp_transient_failure_is_durable(signed_in, app, monkeypatch):
    signed_in.post("/api/v1/auth/password/forgot", json={"email": "owner@example.com"})
    settings = app.state.settings.model_copy(update={"smtp_host": "smtp.example.com"})

    def unavailable(*args, **kwargs):
        raise OSError("simulated unreachable SMTP; no real network")

    monkeypatch.setattr("api_watch.mail.smtplib.SMTP", unavailable)
    assert dispatch_mail(app.state.session_factory, settings)
    with app.state.session_factory() as db:
        row = db.scalar(select(EmailMessage))
        assert row.status == "pending" and row.attempts == 1
        assert row.encrypted_payload


def test_smtp_uses_tls_and_message_payload(signed_in, app, monkeypatch):
    signed_in.post("/api/v1/auth/password/forgot", json={"email": "owner@example.com"})
    settings = app.state.settings.model_copy(update={"smtp_host": "smtp.example.com", "smtp_username": "test-user",
                                                    "smtp_password": "test-password"})
    calls = []

    class SMTPFixture:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context):
            calls.append(("tls",))

        def login(self, username, password):
            assert username == "test-user" and password == "test-password"
            calls.append(("login",))

        def send_message(self, message):
            assert message["To"] == "owner@example.com"
            assert "#reset=" in message.get_content()
            calls.append(("send",))

    monkeypatch.setattr("api_watch.mail.smtplib.SMTP", SMTPFixture)
    assert dispatch_mail(app.state.session_factory, settings)
    assert [call[0] for call in calls] == ["connect", "tls", "login", "send"]
