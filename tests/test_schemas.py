"""Configuration boundary tests, including ambiguity and write-only secrets."""

import copy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from api_watch.schemas import MonitorCreate, json_dumps_exact, normalize_url, public_config


def monitor(**changes):
    return MonitorCreate.model_validate({"name": "  Example API  ", "url": "https://example.com", **changes})


def test_defaults_and_round_trip():
    config = monitor(json_assertions=[{"pointer": "/value", "operator": "exists"}]).model_dump()
    assert config["name"] == "Example API"
    assert config["url"] == "https://example.com/"
    assert config["body"] == {"enabled": False, "json": None}
    assert config["json_assertions"] == [{"pointer": "/value", "operator": "exists"}]
    assert monitor(**config).model_dump() == config


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com", "https://user:pass@example.com", "https://@example.com",
    "http://example.com/?token=secret", "http://example.com/?", "http://example.com/#", "//example.com",
    "http://example.com\\@127.0.0.1", "http://example.com\n", "http://127.1", "http://2130706433",
    "http://0177.0.0.1", "http://0x7f000001", "http://0x7f.0x0.0x0.0x1", "http://127.0.0.01",
    "http://[fe80::1%25eth0]", "http://[::1]evil", "http://example.com:", "http://example.com:65536",
    "http://example.com.", "http://%31%32%37.0.0.1", "http://bad_name.example", "http://example.com:80:90",
])
def test_ambiguous_url_rejected(url):
    with pytest.raises(ValidationError):
        monitor(url=url)


def test_url_idna_ipv6_and_case_are_canonical():
    assert normalize_url("HTTPS://BÜCHER.example:443/x") == "https://xn--bcher-kva.example/x"
    assert normalize_url("http://[2001:4860:4860:0:0:0:0:8888]:80/") == "http://[2001:4860:4860::8888]/"


@pytest.mark.parametrize("changes", [
    {"owner_id": "untrusted"}, {"interval_seconds": 59}, {"timeout_ms": 999}, {"failure_threshold": 0},
    {"enabled": "false"}, {"interval_seconds": True}, {"name": "   "}, {"expected_status": 600},
    {"max_response_time_ms": 10001}, {"headers": [{"name": "Host", "value": "127.0.0.1"}]},
    {"headers": [{"name": "Authorization", "value": "secret"}]},
    {"headers": [{"name": "X-Key", "value": "line\r\ninjected: value"}]},
    {"headers": [{"name": "x", "value": "a"}, {"name": "X", "value": "b"}]},
    {"headers": [{"name": "X-Forwarded-For", "value": "127.0.0.1"}]},
    {"headers": [{"name": "Proxy-Authorization", "value": "secret"}]},
    {"headers": [{"name": "Cookie", "value": "session=secret"}]},
    {"headers": [{"name": "Content-Type", "value": "application/json"}]},
    {"headers": [{"name": "Accept-Encoding", "value": "gzip"}]},
    {"headers": [{"name": "X-Key", "value": "é" * 2049}]},
    {"headers": [{"name": "X-a", "value": "x" * 4096}, {"name": "X-b", "value": "x" * 4096},
                 {"name": "X-c", "value": "x" * 4096}, {"name": "X-d", "value": "x" * 4096}]},
    {"headers": [{"name": "x", "value": "x"}], "secret_headers": [{"name": "X", "value": "secret"}]},
    {"method": "POST"}, {"method": "GET", "body": {"enabled": True, "json": {}}},
    {"method": "OPTIONS", "body": {"enabled": True, "json": None}},
    {"method": "HEAD", "json_assertions": [{"pointer": "", "operator": "exists"}]},
    {"method": "POST", "side_effects_acknowledged": True, "body": {"enabled": True}},
    {"json_assertions": [{"pointer": "status", "operator": "exists"}]},
    {"json_assertions": [{"pointer": "/bad~2escape", "operator": "exists"}]},
    {"json_assertions": [{"pointer": "", "operator": "exists", "expected": None}]},
    {"json_assertions": [{"pointer": "", "operator": "equals"}]},
    {"json_assertions": [{"pointer": "", "operator": "equals", "expected": float("nan")}]},
    {"url": "http://example.com", "secret_headers": [{"name": "X-Key", "value": "secret"}]},
    {"url": "http://example.com", "auth": {"type": "bearer", "token": "secret"}},
    {"auth": {"type": "bearer"}}, {"auth": {"type": "bearer", "token": "***"}},
    {"auth": {"type": "none", "token": "secret"}},
    {"auth": {"type": "bearer", "token": "secret"},
     "secret_headers": [{"name": "Authorization", "value": "Basic abc"}]},
])
def test_invalid_configuration_rejected(changes):
    with pytest.raises(ValidationError):
        monitor(**changes)


def test_null_body_and_assertion_are_preserved():
    config = monitor(method="POST", side_effects_acknowledged=True,
                     body={"enabled": True, "json": None},
                     json_assertions=[{"pointer": "", "operator": "equals", "expected": None}]).model_dump()
    assert config["body"]["json"] is None
    assert config["body"]["enabled"]
    assert config["json_assertions"][0]["expected"] is None
    assert MonitorCreate.model_validate(config).model_dump() == config


def test_headers_total_count_and_body_limit():
    with pytest.raises(ValidationError):
        monitor(headers=[{"name": f"X-{n}", "value": "x"} for n in range(11)],
                secret_headers=[{"name": f"Secret-{n}", "value": "s"} for n in range(10)])
    with pytest.raises(ValidationError):
        monitor(method="POST", side_effects_acknowledged=True, body={"enabled": True, "json": "x" * 65536})


def test_public_configuration_is_redacted_without_mutation():
    config = monitor(auth={"type": "bearer", "token": "private-token"},
                     secret_headers=[{"name": "X-Key", "value": "private-header"}]).model_dump()
    original = copy.deepcopy(config)
    public = public_config(config)
    assert public["auth"] == {"type": "bearer", "configured": True}
    assert public["secret_headers"] == [{"name": "X-Key", "configured": True}]
    assert config == original
    assert "private-token" not in repr(public)
    assert "private-header" not in repr(public)


def test_exact_decimal_config_roundtrip():
    from api_watch.checker import strict_json_loads

    raw = b'{"name":"Exact","url":"https://example.com","method":"POST",'
    raw += b'"side_effects_acknowledged":true,"body":{"enabled":true,"json":1.000000000000000000001},'
    raw += b'"json_assertions":[{"pointer":"","operator":"equals","expected":1.000000000000000000001}]}'
    parsed = strict_json_loads(raw, exact_integers=False)
    config = MonitorCreate.model_validate(parsed).model_dump()
    assert config["body"]["json"] == Decimal("1.000000000000000000001")
    assert config["json_assertions"][0]["expected"] == Decimal("1.000000000000000000001")
    encoded = json_dumps_exact(config).encode()
    assert b'"expected":1.000000000000000000001' in encoded
    assert MonitorCreate.model_validate(strict_json_loads(encoded, exact_integers=False)).model_dump() == config
