"""Bounded HTTP checks with DNS pinning and no ambient credentials or proxies.

All exception messages exposed to a user are constants. In particular aiohttp
exceptions, request bodies and remote response content are never persisted.
"""

import asyncio
import ipaddress
import json
import socket
import time
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from api_watch.schemas import MonitorCreate, json_dumps_exact, normalize_url

MAX_RESPONSE_BYTES = 1024 * 1024
DENIED_IPS = {ipaddress.ip_address("168.63.129.16")}
DENIED_HOSTS = {"metadata.google.internal", "metadata.goog", "instance-data.ec2.internal"}
DENIED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan", ".localdomain")
IPV6_TRANSLATION = tuple(ipaddress.ip_network(net) for net in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
))

ERROR_MESSAGES = {
    "target_blocked": "The target is prohibited by the outbound network policy.",
    "dns_error": "The target hostname could not be resolved.",
    "connection_error": "A connection to the target could not be completed.",
    "tls_error": "The target TLS certificate or handshake could not be verified.",
    "timeout": "The complete HTTP exchange exceeded its timeout.",
    "response_too_large": "The response exceeded the 1 MiB transfer or decompression limit.",
    "invalid_http_response": "The target returned an invalid or unsupported HTTP response.",
    "unexpected_status": "The HTTP status did not match the configured status.",
    "response_too_slow": "The completed response exceeded the configured response-time threshold.",
    "invalid_json": "The response is not valid bounded UTF-8 JSON.",
    "assertion_failed": "One or more configured JSON assertions did not match.",
    "internal_error": "The check could not be completed because of a service error.",
}


class TargetBlocked(Exception):
    def __init__(self):
        super().__init__(ERROR_MESSAGES["target_blocked"])


class TargetDNSError(Exception):
    def __init__(self):
        super().__init__(ERROR_MESSAGES["dns_error"])


class ResponseTooLarge(Exception):
    pass


class InvalidHTTPResponse(Exception):
    pass


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def public_address(raw: str) -> bool:
    """Require global unicast, including for mapped and translation addresses."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address in DENIED_IPS):
        return False
    if address.version == 6:
        if address.ipv4_mapped is not None or any(address in net for net in IPV6_TRANSLATION):
            return False
    return True


async def _lookup(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, OSError, UnicodeError) as exc:
        raise TargetDNSError() from exc
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise TargetDNSError()
    return addresses


async def validate_target(url: str, settings) -> ValidatedTarget:
    """Validate *all* A/AAAA answers and return addresses safe to pin.

    Callers may accept TargetDNSError while saving a monitor, since a temporary
    DNS outage is a check failure. TargetBlocked always rejects configuration.
    The execution path always repeats the complete validation immediately before
    it opens a connection. The exact test allowlist is never user configurable.
    """
    try:
        canonical = normalize_url(url)
        parsed = urlsplit(canonical)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError) as exc:
        raise TargetBlocked() from exc
    allowed = {
        value.strip().lower() for value in getattr(settings, "test_allowed_hosts", "").split(",")
        if value.strip()
    } if getattr(settings, "app_env", None) == "test" else set()
    test_fixture = host in allowed
    if not test_fixture:
        if port != (443 if parsed.scheme == "https" else 80):
            raise TargetBlocked()
        if host in DENIED_HOSTS or host == "localhost" or host.endswith(DENIED_SUFFIXES):
            raise TargetBlocked()
        own_host = urlsplit(getattr(settings, "public_base_url", "")).hostname
        if own_host and host == own_host.lower():
            raise TargetBlocked()
    try:
        address = ipaddress.ip_address(host)
        addresses = (str(address),)
    except ValueError:
        addresses = await _lookup(host, port)
    if not test_fixture and not all(public_address(address) for address in addresses):
        raise TargetBlocked()
    # An alias of the application's own public host must not reach the service.
    # In production DNS is resolved again here; no preflight result is trusted.
    if not test_fixture:
        own_host = urlsplit(getattr(settings, "public_base_url", "")).hostname
        if own_host:
            try:
                own_addresses = (str(ipaddress.ip_address(own_host)),)
            except ValueError:
                try:
                    own_addresses = await _lookup(own_host, 443)
                except TargetDNSError:
                    # Fail closed until the deployment's own address is known.
                    raise TargetBlocked() from None
            if set(addresses).intersection(own_addresses):
                raise TargetBlocked()
    return ValidatedTarget(canonical, host, port, addresses)


class PinnedResolver(AbstractResolver):
    """aiohttp receives only the vetted address; it never repeats target DNS."""

    def __init__(self, hostname: str, address: str):
        self.hostname = hostname
        self.address = address

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host.lower() != self.hostname.lower():
            raise TargetBlocked()
        version = ipaddress.ip_address(self.address).version
        return [{
            "hostname": host, "host": self.address, "port": port,
            "family": socket.AF_INET6 if version == 6 else socket.AF_INET,
            "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST,
        }]

    async def close(self):
        return None


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON constant")


def strict_json_loads(raw: bytes, *, exact_integers: bool = True):
    """Bound depth before recursion and retain exact JSON numeric values."""
    text = raw.decode("utf-8", errors="strict")
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON depth exceeds 64")
        elif char in "]}":
            depth -= 1
    try:
        document = json.loads(text, parse_int=Decimal if exact_integers else int,
                              parse_float=Decimal, parse_constant=_reject_constant,
                              object_pairs_hook=_pairs_without_duplicates)
    except (InvalidOperation, OverflowError):
        raise ValueError("JSON number exceeds the supported numeric range") from None
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            # JSON escapes may decode to an unpaired surrogate even when the
            # wire bytes themselves are valid UTF-8. Reject those at the edge.
            value.encode("utf-8")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return document


def json_equal(actual, expected) -> bool:
    """JSON equality: bool differs from number; numeric representations do not."""
    numeric = (int, float, Decimal)
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual == expected
    if isinstance(actual, numeric) or isinstance(expected, numeric):
        if not isinstance(actual, numeric) or not isinstance(expected, numeric):
            return False
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except (ValueError, InvalidOperation):
            return False
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[k], expected[k]) for k in actual)
    return actual == expected


def json_pointer(document, pointer: str):
    """Return (exists, value), preserving explicit null at every level."""
    if pointer == "":
        return True, document
    value = document
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and (token == "0" or token.isascii() and token.isdecimal()
                                         and not token.startswith("0")):
            # Bounding the string avoids int conversion limits and huge indexes.
            if len(token) > 16 or int(token) >= len(value):
                return False, None
            value = value[int(token)]
        else:
            return False, None
    return True, value


@asynccontextmanager
async def _no_guard(_address):
    yield


async def _read_response(response, result: dict) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise InvalidHTTPResponse()
    decoder = None
    if encoding in {"gzip", "deflate"}:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    output = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        result["bytes_received"] += len(chunk)
        if result["bytes_received"] > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge()
        if decoder:
            try:
                decoded = decoder.decompress(chunk, MAX_RESPONSE_BYTES - len(output) + 1)
            except zlib.error as exc:
                raise InvalidHTTPResponse() from exc
        else:
            decoded = chunk
        output.extend(decoded)
        if len(output) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge()
        if decoder and decoder.unconsumed_tail:
            raise ResponseTooLarge()
    if decoder and (not decoder.eof or decoder.unused_data):
        # Concatenated gzip streams and trailing garbage are deliberately not
        # accepted. Never call unbounded decompressor.flush() on remote data.
        raise InvalidHTTPResponse()
    return bytes(output)


async def execute_check(config_dict: dict, settings, destination_guard=None) -> dict:
    """Execute one request. Local capacity waiting is not target response time.

    DNS/connect/read share the target budget. The worker's separate bounded lease
    limits local admission waiting; an expired service lease is an interruption.
    """
    started = time.monotonic()
    result = {
        "outcome": "success", "http_status": None, "response_time_ms": None,
        "elapsed_ms": 0, "bytes_received": 0, "error_code": None,
        "error_message": None, "failed_assertions": [],
    }

    def fail(code, outcome="failure"):
        result.update(outcome=outcome, error_code=code, error_message=ERROR_MESSAGES[code])

    try:
        # Configurations are versioned and validated at save time, but a corrupt
        # or legacy DB record must never bypass the current outbound policy.
        config = MonitorCreate.model_validate(config_dict).model_dump()
        async with asyncio.timeout(config["timeout_ms"] / 1000) as budget:
            target = await validate_target(config["url"], settings)
            chosen_address = target.addresses[0]
            guard = destination_guard or _no_guard
            deadline = budget.when()
            queue_started = time.monotonic()
            # Admission is API Watch's own queue, not evidence about the target.
            # The outer worker lease still bounds this phase independently.
            budget.reschedule(None)
            async with guard(chosen_address):
                capacity_wait = time.monotonic() - queue_started
                budget.reschedule(deadline + capacity_wait)
                resolver = PinnedResolver(target.hostname, chosen_address)
                connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, force_close=True,
                                                 limit=1, ssl=True)
                headers = [(h["name"], h["value"]) for h in config["headers"] + config["secret_headers"]]
                headers.append(("Accept-Encoding", "identity"))
                if not any(name.lower() == "user-agent" for name, _ in headers):
                    headers.append(("User-Agent", "API-Watch/1.0"))
                if config["auth"]["type"] == "bearer":
                    headers.append(("Authorization", "Bearer " + config["auth"]["token"]))
                data = None
                if config["body"]["enabled"]:
                    data = json_dumps_exact(config["body"]["json"]).encode()
                    headers.append(("Content-Type", "application/json"))
                async with aiohttp.ClientSession(
                    connector=connector, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None),
                    skip_auto_headers={"Accept-Encoding", "Content-Type", "User-Agent"},
                    max_line_size=8192, max_field_size=8192, read_bufsize=16384,
                ) as session:
                    # aiohttp retries some idempotent methods on a dropped
                    # connection. A logical check must issue exactly one request.
                    session._retry_connection = False
                    async with session.request(
                        config["method"], target.url, headers=headers,
                        params=[(p["name"], p["value"]) for p in config["query_params"]],
                        data=data, allow_redirects=False,
                    ) as response:
                        result["http_status"] = response.status
                        if config["method"] == "HEAD":
                            raw = b""
                        else:
                            raw = await _read_response(response, result)
                        result["response_time_ms"] = round((time.monotonic() - started - capacity_wait) * 1000, 3)
        if result["http_status"] != config["expected_status"]:
            fail("unexpected_status")
        elif config["max_response_time_ms"] is not None and (
            result["response_time_ms"] > config["max_response_time_ms"]
        ):
            fail("response_too_slow")
        elif config["json_assertions"]:
            try:
                document = strict_json_loads(raw)
            except (ValueError, UnicodeError, RecursionError, InvalidOperation):
                fail("invalid_json")
            else:
                for index, assertion in enumerate(config["json_assertions"]):
                    exists, actual = json_pointer(document, assertion["pointer"])
                    if not exists or assertion["operator"] == "equals" and not json_equal(
                        actual, assertion["expected"]
                    ):
                        result["failed_assertions"].append(index)
                if result["failed_assertions"]:
                    fail("assertion_failed")
    except TargetBlocked:
        fail("target_blocked", "blocked")
    except TargetDNSError:
        fail("dns_error")
    except (TimeoutError, aiohttp.ServerTimeoutError):
        result["http_status"] = None
        fail("timeout")
    except (aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError):
        result["http_status"] = None
        fail("tls_error")
    except aiohttp.ClientConnectorDNSError:
        result["http_status"] = None
        fail("dns_error")
    except ResponseTooLarge:
        fail("response_too_large")
    except (InvalidHTTPResponse, aiohttp.ClientPayloadError, aiohttp.ClientResponseError):
        fail("invalid_http_response")
    except (aiohttp.ClientConnectionError, OSError):
        result["http_status"] = None
        fail("connection_error")
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never stringify errors that may embed a URL, header, or response.
        fail("internal_error", "interrupted")
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result
