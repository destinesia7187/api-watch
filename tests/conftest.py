"""Application-level fixtures, independent from outbound network services."""

import base64

import pytest
from fastapi.testclient import TestClient

from api_watch.app import create_app
from api_watch.config import Settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    async def fixture_dns(host, port):
        if host == "example.com":
            return ("127.0.0.1",)
        from api_watch.checker import TargetDNSError
        raise TargetDNSError()

    monkeypatch.setattr("api_watch.checker._lookup", fixture_dns)
    settings = Settings(app_env="test", database_url=f"sqlite:///{tmp_path / 'app.db'}",
        public_base_url="http://testserver", session_secret="s" * 48,
        encryption_key=base64.urlsafe_b64encode(b"k" * 32).decode(),
        test_allowed_hosts="example.com", _env_file=None)
    application = create_app(settings, initialize=True)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as browser:
        yield browser


@pytest.fixture
def signed_in(client):
    response = client.post("/api/v1/auth/register", json={"email": "owner@example.com", "password": "a long test password"})
    assert response.status_code == 201, response.text
    response = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "a long test password"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.fixture
def monitor(signed_in):
    response = signed_in.post("/api/v1/monitors", json={"name": "Example", "url": "https://example.com/health"})
    assert response.status_code == 201, response.text
    return response.json()
