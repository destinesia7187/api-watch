"""Isolated HTTP fixtures: the suite never calls a public external service."""

import asyncio
import gzip
import ssl
import zlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from api_watch import checker
from api_watch.config import Settings
from api_watch.schemas import MonitorCreate


async def test_local_capacity_wait_is_not_target_timeout_or_latency(fixture_server):
    base, _ = fixture_server
    settings = Settings(app_env="test", test_allowed_hosts="127.0.0.1", _env_file=None)

    @asynccontextmanager
    async def admission(_ip):
        await asyncio.sleep(1.1)
        yield

    result = await checker.execute_check(MonitorCreate(name="Capacity", url=base + "/slow",
        timeout_ms=1000).model_dump(), settings, destination_guard=admission)
    assert result["outcome"] == "success"
    assert result["elapsed_ms"] >= 1100
    assert result["response_time_ms"] < 800


async def test_lost_local_admission_is_an_interruption(fixture_server):
    base, _ = fixture_server
    settings = Settings(app_env="test", test_allowed_hosts="127.0.0.1", _env_file=None)

    @asynccontextmanager
    async def unavailable(_ip):
        raise RuntimeError("A local worker lease expired")
        yield

    result = await checker.execute_check(MonitorCreate(name="Capacity", url=base).model_dump(),
        settings, destination_guard=unavailable)
    assert result["outcome"] == "interrupted" and result["error_code"] == "internal_error"


@pytest_asyncio.fixture
async def fixture_server():
    seen = []

    async def respond(request):
        raw = await request.read()
        seen.append({"method": request.method, "headers": dict(request.headers),
                     "query": list(request.query.items()), "body": raw, "path": request.path})
        if request.path == "/redirect":
            return web.Response(status=302, headers={"Location": "/forbidden"})
        if request.path == "/slow":
            await asyncio.sleep(0.04)
        if request.path == "/timeout":
            await asyncio.sleep(1.2)
        if request.path == "/status":
            return web.Response(status=503, body=b"not json")
        bodies = {
            "/duplicate": b'{"key": 1, "key": 2}', "/nan": b'{"key": NaN}',
            "/infinity": b'{"key": Infinity}', "/invalid": b"not json",
            "/utf8": b'{"key":"\xff"}', "/deep": b"[" * 65 + b"0" + b"]" * 65,
            "/big": b"x" * (checker.MAX_RESPONSE_BYTES + 1),
        }
        if request.path in bodies:
            return web.Response(body=bodies[request.path])
        if request.path == "/gzip":
            return web.Response(body=gzip.compress(b'{"status":"ok"}'), headers={"Content-Encoding": "gzip"})
        if request.path == "/deflate":
            return web.Response(body=zlib.compress(b'{"status":"ok"}'), headers={"Content-Encoding": "deflate"})
        if request.path == "/bomb":
            return web.Response(body=gzip.compress(b"x" * (checker.MAX_RESPONSE_BYTES + 1)),
                                headers={"Content-Encoding": "gzip"})
        if request.path == "/truncated":
            return web.Response(body=gzip.compress(b"body")[:-3], headers={"Content-Encoding": "gzip"})
        if request.path == "/unsupported":
            return web.Response(body=b"hello", headers={"Content-Encoding": "br"})
        if request.path == "/cookie":
            return web.Response(text="ok", headers={"Set-Cookie": "secret=do-not-resend"})
        if request.path == "/disconnect":
            request.transport.close()
            return web.Response()
        return web.Response(body=b'{"status":"ok","value":null,"array":[true,1,"1"],'
                                 b'"a/b":{"~key":1.000000000000000000001},"huge":9007199254740993}',
                            content_type="text/plain")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", seen
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def tls_fixture(tmp_path):
    """A private test CA verifies the real TLS hostname and SNI code path."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example")])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example")]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "server.crt", tmp_path / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    sni, seen = [], []
    server_context.set_servername_callback(lambda _socket, hostname, _context: sni.append(hostname))

    async def receive(request):
        seen.append(dict(request.headers))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port, cert_path, sni, seen
    finally:
        await runner.cleanup()


def config(url, **changes):
    return MonitorCreate.model_validate({"name": "Fixture", "url": url, **changes}).model_dump()


def fixture_settings():
    return Settings(app_env="test", test_allowed_hosts="127.0.0.1")


@pytest.mark.parametrize("address", [
    "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.100.100.200", "100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "224.0.0.1",
    "255.255.255.255", "168.63.129.16", "::1", "::", "ff02::1", "fe80::1", "fc00::1",
    "2001:db8::1", "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:0808:0808::1",
])
def test_nonpublic_and_transition_ips_blocked(address):
    assert not checker.public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "2606:4700:4700::1111"])
def test_public_ips_allowed(address):
    assert checker.public_address(address)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://[::1]/", "https://localhost/", "http://169.254.169.254/",
    "http://8.8.8.8:8080/", "https://metadata.google.internal/", "https://service.internal/",
])
async def test_target_policy_blocks_before_request(url):
    with pytest.raises(checker.TargetBlocked):
        await checker.validate_target(url, Settings(app_env="test"))


async def test_dns_all_answers_checked_and_not_just_first(monkeypatch):
    async def answers(*args):
        return ("8.8.8.8", "127.0.0.1")
    monkeypatch.setattr(checker, "_lookup", answers)
    with pytest.raises(checker.TargetBlocked):
        await checker.validate_target("http://public.example/", Settings(app_env="test"))


async def test_dns_error_has_safe_failure(monkeypatch):
    async def missing(*args):
        raise checker.TargetDNSError()
    monkeypatch.setattr(checker, "_lookup", missing)
    result = await checker.execute_check(config("https://public.example"), Settings(app_env="test"))
    assert result["error_code"] == "dns_error"
    assert result["http_status"] is None
    assert result["response_time_ms"] is None


async def test_production_never_uses_test_allowlist():
    class UnsafeSettings:
        app_env = "production"
        test_allowed_hosts = "127.0.0.1"
        public_base_url = "https://8.8.8.8"
    with pytest.raises(checker.TargetBlocked):
        await checker.validate_target("http://127.0.0.1/", UnsafeSettings())


async def test_allowlist_is_exact():
    with pytest.raises(checker.TargetBlocked):
        await checker.validate_target("http://127.0.0.2:8080/", fixture_settings())


async def test_success_pinned_dns_and_host(monkeypatch, fixture_server):
    base, seen = fixture_server
    port = int(base.rsplit(":", 1)[1])
    calls = []
    async def vetted(host, resolved_port):
        calls.append((host, resolved_port))
        return ("127.0.0.1",)
    monkeypatch.setattr(checker, "_lookup", vetted)
    result = await checker.execute_check(config(f"http://fixture.example:{port}/"),
                                         Settings(app_env="test", test_allowed_hosts="fixture.example"))
    assert result["outcome"] == "success"
    assert calls == [("fixture.example", port)]
    assert seen[0]["headers"]["Host"] == f"fixture.example:{port}"
    assert result["response_time_ms"] is not None
    assert result["bytes_received"] > 0


async def test_dns_rebinding_cannot_trigger_second_lookup(monkeypatch, fixture_server):
    base, seen = fixture_server
    port = int(base.rsplit(":", 1)[1])
    calls = 0
    async def once(host, resolved_port):
        nonlocal calls
        calls += 1
        if calls > 1:
            return ("127.0.0.2",)
        return ("127.0.0.1",)
    monkeypatch.setattr(checker, "_lookup", once)
    result = await checker.execute_check(config(f"http://fixture.example:{port}/"),
                                         Settings(app_env="test", test_allowed_hosts="fixture.example"))
    assert result["outcome"] == "success"
    assert len(seen) == calls == 1


async def test_redirect_is_observed_and_never_followed(fixture_server):
    base, seen = fixture_server
    result = await checker.execute_check(config(base + "/redirect", expected_status=302), fixture_settings())
    assert result["outcome"] == "success"
    assert [r["path"] for r in seen] == ["/redirect"]


async def test_query_pairs_and_null_json_body(fixture_server):
    base, seen = fixture_server
    result = await checker.execute_check(config(base, method="POST", side_effects_acknowledged=True,
        query_params=[{"name": "x", "value": "a&b"}, {"name": "x", "value": "two words"}],
        body={"enabled": True, "json": None}), fixture_settings())
    assert result["outcome"] == "success"
    assert seen[0]["query"] == [("x", "a&b"), ("x", "two words")]
    assert seen[0]["body"] == b"null"
    assert seen[0]["headers"]["Content-Type"] == "application/json"
    assert seen[0]["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_all_methods_sent_once(method, fixture_server):
    base, seen = fixture_server
    result = await checker.execute_check(config(base, method=method, side_effects_acknowledged=True), fixture_settings())
    assert result["outcome"] == "success"
    assert len(seen) == 1
    assert seen[0]["method"] == method
    assert seen[0]["body"] == b""
    if method == "HEAD":
        assert result["bytes_received"] == 0


@pytest.mark.parametrize(("path", "code"), [
    ("/status", "unexpected_status"), ("/big", "response_too_large"),
    ("/bomb", "response_too_large"), ("/truncated", "invalid_http_response"),
    ("/unsupported", "invalid_http_response"),
])
async def test_http_limits_and_error_priority(path, code, fixture_server):
    base, _ = fixture_server
    result = await checker.execute_check(config(base + path,
        json_assertions=[{"pointer": "", "operator": "exists"}]), fixture_settings())
    assert result["error_code"] == code
    assert result["outcome"] == "failure"
    assert result["failed_assertions"] == []


@pytest.mark.parametrize("path", ["/duplicate", "/nan", "/infinity", "/invalid", "/utf8", "/deep"])
async def test_invalid_json_is_not_accepted(path, fixture_server):
    base, _ = fixture_server
    result = await checker.execute_check(config(base + path,
        json_assertions=[{"pointer": "", "operator": "exists"}]), fixture_settings())
    assert result["error_code"] == "invalid_json"
    assert result["response_time_ms"] is not None


async def test_body_not_parsed_without_assertions(fixture_server):
    base, _ = fixture_server
    result = await checker.execute_check(config(base + "/invalid"), fixture_settings())
    assert result["outcome"] == "success"


@pytest.mark.parametrize("path", ["/gzip", "/deflate"])
async def test_supported_compression_is_bounded_and_decoded(path, fixture_server):
    base, _ = fixture_server
    result = await checker.execute_check(config(base + path,
        json_assertions=[{"pointer": "/status", "operator": "equals", "expected": "ok"}]), fixture_settings())
    assert result["outcome"] == "success"


async def test_timeout_differs_from_completed_slow_response(fixture_server):
    base, _ = fixture_server
    slow = await checker.execute_check(config(base + "/slow", max_response_time_ms=1), fixture_settings())
    timeout = await checker.execute_check(config(base + "/timeout", timeout_ms=1000), fixture_settings())
    assert slow["error_code"] == "response_too_slow"
    assert slow["response_time_ms"] is not None
    assert timeout["error_code"] == "timeout"
    assert timeout["response_time_ms"] is None
    assert timeout["http_status"] is None


async def test_dns_time_included_in_absolute_deadline(monkeypatch):
    async def slow_dns(*args):
        await asyncio.sleep(2)
        return ("8.8.8.8",)
    monkeypatch.setattr(checker, "_lookup", slow_dns)
    result = await checker.execute_check(config("https://fixture.example", timeout_ms=1000), fixture_settings())
    assert result["error_code"] == "timeout"
    assert 950 <= result["elapsed_ms"] < 1500


async def test_json_pointer_types_null_arrays_and_large_integers(fixture_server):
    base, _ = fixture_server
    assertions = [
        {"pointer": "/value", "operator": "exists"},
        {"pointer": "/value", "operator": "equals", "expected": None},
        {"pointer": "/array/0", "operator": "equals", "expected": True},
        {"pointer": "/array/1", "operator": "equals", "expected": 1.0},
        {"pointer": "/array/2", "operator": "equals", "expected": "1"},
        {"pointer": "/huge", "operator": "equals", "expected": 9007199254740993},
    ]
    result = await checker.execute_check(config(base, json_assertions=assertions), fixture_settings())
    assert result["outcome"] == "success"
    assertions += [{"pointer": "/missing", "operator": "exists"},
                   {"pointer": "/array/0", "operator": "equals", "expected": 1},
                   {"pointer": "/array/01", "operator": "exists"}]
    result = await checker.execute_check(config(base, json_assertions=assertions), fixture_settings())
    assert result["failed_assertions"] == [6, 7, 8]
    assert result["error_code"] == "assertion_failed"
    assert "status" not in result["error_message"]


def test_json_exact_fraction_and_escaped_pointer():
    document = checker.strict_json_loads(b'{"a/b":{"~key":1.000000000000000000001}}')
    exists, actual = checker.json_pointer(document, "/a~1b/~0key")
    assert exists
    assert checker.json_equal(actual, Decimal("1.000000000000000000001"))
    assert not checker.json_equal(actual, 1)
    assert checker.json_equal({"x": [Decimal(1), True]}, {"x": [1.0, True]})
    assert not checker.json_equal({"x": [1, True]}, {"x": [True, 1]})
    assert checker.json_pointer(None, "") == (True, None)


async def test_destination_guard_receives_pinned_ip(fixture_server):
    base, _ = fixture_server
    events = []
    @asynccontextmanager
    async def guard(ip):
        events.append(("enter", ip))
        yield
        events.append(("exit", ip))
    result = await checker.execute_check(config(base), fixture_settings(), destination_guard=guard)
    assert result["outcome"] == "success"
    assert events == [("enter", "127.0.0.1"), ("exit", "127.0.0.1")]


async def test_ambient_proxy_cookie_and_auth_are_not_sent(monkeypatch, fixture_server):
    base, seen = fixture_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.2:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.2:1")
    monkeypatch.setenv("NETRC", "not-used-by-this-transport")
    await checker.execute_check(config(base + "/cookie"), fixture_settings())
    result = await checker.execute_check(config(base), fixture_settings())
    assert result["outcome"] == "success"
    for item in seen:
        assert "Cookie" not in item["headers"]
        assert "Authorization" not in item["headers"]


async def test_cancellation_propagates(monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(checker, "validate_target", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await checker.execute_check(config("https://example.com"), fixture_settings())


async def test_dropped_connection_is_not_automatically_retried(fixture_server):
    base, seen = fixture_server
    result = await checker.execute_check(config(base + "/disconnect"), fixture_settings())
    assert result["error_code"] == "connection_error"
    assert result["http_status"] is None
    assert len(seen) == 1


async def test_tls_untrusted_certificate_is_never_accepted(monkeypatch, tls_fixture):
    port, _cert_path, sni, seen = tls_fixture

    async def lookup(*args):
        return ("127.0.0.1",)

    monkeypatch.setattr(checker, "_lookup", lookup)
    settings = Settings(app_env="test", test_allowed_hosts="fixture.example")
    result = await checker.execute_check(config(f"https://fixture.example:{port}"), settings)
    assert result["error_code"] == "tls_error"
    assert result["http_status"] is None
    assert seen == []
    assert sni == ["fixture.example"]


async def test_verified_tls_preserves_sni_and_sends_secrets_only_to_target(monkeypatch, tls_fixture):
    port, cert_path, sni, seen = tls_fixture
    trusted = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", trusted)

    async def lookup(*args):
        return ("127.0.0.1",)

    monkeypatch.setattr(checker, "_lookup", lookup)
    settings = Settings(app_env="test", test_allowed_hosts="fixture.example")
    result = await checker.execute_check(config(f"https://fixture.example:{port}",
        auth={"type": "bearer", "token": "private-token"},
        secret_headers=[{"name": "X-Key", "value": "private-header"}]), settings)
    assert result["outcome"] == "success"
    assert sni == ["fixture.example"]
    assert seen[0]["Authorization"] == "Bearer private-token"
    assert seen[0]["X-Key"] == "private-header"
    assert "private-token" not in repr(result)
    assert "private-header" not in repr(result)


async def test_trusted_certificate_still_requires_matching_hostname(monkeypatch, tls_fixture):
    port, cert_path, sni, seen = tls_fixture
    trusted = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", trusted)

    async def lookup(*args):
        return ("127.0.0.1",)

    monkeypatch.setattr(checker, "_lookup", lookup)
    settings = Settings(app_env="test", test_allowed_hosts="different.example")
    result = await checker.execute_check(config(f"https://different.example:{port}"), settings)
    assert result["error_code"] == "tls_error"
    assert sni == ["different.example"]
    assert seen == []
