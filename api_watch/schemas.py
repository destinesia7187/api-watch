"""Strict request configuration and safe, secret-free representations."""

import copy
import ipaddress
import json
import math
import re
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$")
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PROHIBITED_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding", "forwarded", "cookie",
    "accept-encoding", "content-type", "te", "trailer", "upgrade", "proxy-connection",
}


def normalize_url(value: str) -> str:
    """One URL parser used at save time and immediately before connecting.

    Port and destination policy is checked by checker.validate_target, where the
    isolated test environment can explicitly allow a fixture's ephemeral port.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("URL must contain between 1 and 2048 characters")
    if any(ord(c) <= 32 or ord(c) == 127 or c.isspace() for c in value) or "\\" in value:
        raise ValueError("URL contains whitespace, control characters, or a backslash")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not host:
        raise ValueError("URL must be an absolute HTTP or HTTPS address")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("URL credentials are not allowed")
    authority_pattern = r"\[[^\]]+\](?::[0-9]{1,5})?" if parsed.netloc.startswith("[") else r"[^:\[\]]+(?::[0-9]{1,5})?"
    if not re.fullmatch(authority_pattern, parsed.netloc):
        raise ValueError("URL authority is malformed")
    if "?" in value or "#" in value:
        raise ValueError("Use query_params instead of a URL query; fragments are not allowed")
    if "%" in host or host.endswith(".") or host.startswith("."):
        raise ValueError("Encoded, scoped, or ambiguous hostnames are not allowed")
    try:
        address = ipaddress.ip_address(host)
        host = address.compressed
        authority = f"[{host}]" if address.version == 6 else host
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Hostname is not valid IDNA") from exc
        if len(host) > 253 or any(not LABEL.fullmatch(label) for label in host.split(".")):
            raise ValueError("Hostname is malformed")
        # Decimal integers, octal/hex, short dotted IPv4, and mixed IPv4 forms
        # must never be handed to a system resolver that may reinterpret them.
        last_label = host.rsplit(".", 1)[-1]
        if last_label.isdecimal() or last_label.startswith("0x"):
            raise ValueError("Noncanonical IP addresses are not allowed")
        authority = host
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        if port < 1:
            raise ValueError("URL port is invalid")
        authority += f":{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", "", ""))


def validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > 64 or depth == 64 and isinstance(value, (dict, list)):
        raise ValueError("JSON depth exceeds 64 levels")
    if value is None or type(value) in {str, bool, int}:
        return
    if isinstance(value, (float, Decimal)):
        if not (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            validate_json_value(item, depth + 1)
        return
    raise ValueError("Value must be valid JSON")


def json_dumps_exact(value: Any) -> str:
    """Serialize Decimal as a JSON number, without a lossy float round trip.

    The standard JSON encoder has no Decimal number hook. A small recursive
    encoder keeps its trusted string escaping while emitting exact numeric text.
    Use this at the encrypted configuration and API boundaries as well as for
    target request bodies. Objects are restricted to ordinary JSON values.
    """
    validate_json_value(value)

    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, list):
            return "[" + ",".join(encode(child) for child in item) + "]"
        if isinstance(item, dict):
            return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + encode(child)
                                  for key, child in item.items()) + "}"
        return json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    return encode(value)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pair(StrictModel):
    name: str = Field(max_length=1000)
    value: str = Field(max_length=4096)

    @model_validator(mode="after")
    def bounded_pair(self):
        if len(self.name.encode()) > 1000 or len(self.value.encode()) > 4096:
            raise ValueError("Parameter name or value exceeds its byte limit")
        return self


class HeaderPair(Pair):
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(json_schema_extra={"writeOnly": True})

    @model_validator(mode="after")
    def valid_header(self):
        if not HEADER_NAME.fullmatch(self.name):
            raise ValueError("Header name must be an ASCII HTTP token")
        lower = self.name.lower()
        if lower in PROHIBITED_HEADERS or lower.startswith(("proxy-", "x-forwarded-")):
            raise ValueError("This header is managed by the service and cannot be set")
        if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in self.value):
            raise ValueError("Header values cannot contain control characters")
        # aiohttp serializes request headers using UTF-8; the byte bound applies
        # before transport construction and cannot be bypassed with Unicode.
        if len(self.value.encode()) > 4096:
            raise ValueError("Header value exceeds 4096 bytes")
        return self


class RequestBody(StrictModel):
    enabled: StrictBool = False
    json_value: Any = Field(default=None, alias="json")

    @model_validator(mode="after")
    def valid_body(self):
        if self.enabled and "json_value" not in self.model_fields_set:
            raise ValueError("An enabled body requires an explicit json value, including null")
        validate_json_value(self.json_value)
        try:
            encoded = json_dumps_exact(self.json_value)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("Request body must be serializable JSON") from exc
        if len(encoded.encode()) > 65536:
            raise ValueError("JSON request body exceeds 64 KiB")
        return self


class TargetAuth(StrictModel):
    type: Literal["none", "bearer"] = "none"
    token: str | None = Field(default=None, max_length=4096, json_schema_extra={"writeOnly": True})

    @model_validator(mode="after")
    def valid_auth(self):
        if self.type == "none":
            if self.token is not None:
                raise ValueError("A token is allowed only for bearer authentication")
        elif not self.token or self.token.strip() != self.token or any(c.isspace() for c in self.token):
            raise ValueError("Bearer authentication requires a nonempty token without whitespace")
        elif not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", self.token):
            raise ValueError("Bearer token contains invalid characters")
        return self


class JSONAssertion(StrictModel):
    pointer: str = Field(max_length=512)
    operator: Literal["exists", "equals"]
    expected: Any = None

    @model_validator(mode="after")
    def valid_assertion(self):
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("JSON Pointer must be empty or begin with /")
        if re.search(r"~(?![01])", self.pointer):
            raise ValueError("JSON Pointer escape must be ~0 or ~1")
        present = "expected" in self.model_fields_set
        if self.operator == "equals" and not present:
            raise ValueError("equals requires expected, including explicit null")
        if self.operator == "exists" and present:
            raise ValueError("exists does not accept expected")
        validate_json_value(self.expected)
        return self


class MonitorCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    url: str = Field(max_length=2048)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] = "GET"
    headers: list[HeaderPair] = Field(default_factory=list, max_length=20)
    secret_headers: list[HeaderPair] = Field(default_factory=list, max_length=20)
    query_params: list[Pair] = Field(default_factory=list, max_length=100)
    body: RequestBody = Field(default_factory=RequestBody)
    auth: TargetAuth = Field(default_factory=TargetAuth)
    side_effects_acknowledged: StrictBool = False
    interval_seconds: StrictInt = Field(default=60, ge=60, le=86400)
    timeout_ms: StrictInt = Field(default=10000, ge=1000, le=30000)
    expected_status: StrictInt = Field(default=200, ge=200, le=599)
    max_response_time_ms: StrictInt | None = Field(default=None, ge=1, le=30000)
    json_assertions: list[JSONAssertion] = Field(default_factory=list, max_length=20)
    failure_threshold: StrictInt = Field(default=3, ge=1, le=10)
    recovery_threshold: StrictInt = Field(default=2, ge=1, le=10)
    notifications_enabled: StrictBool = False
    enabled: StrictBool = True

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", "description")
    @classmethod
    def database_text(cls, value):
        if "\x00" in value:
            raise ValueError("Text fields cannot contain a null character")
        return value

    @field_validator("url")
    @classmethod
    def canonical_url(cls, value):
        return normalize_url(value)

    @model_validator(mode="after")
    def valid_configuration(self):
        if self.timeout_ms >= self.interval_seconds * 1000:
            raise ValueError("Timeout must be less than the check interval")
        if self.max_response_time_ms is not None and self.max_response_time_ms > self.timeout_ms:
            raise ValueError("Response-time threshold cannot exceed the timeout")
        if self.method in {"POST", "PUT", "PATCH", "DELETE"} and not self.side_effects_acknowledged:
            raise ValueError("This method requires side_effects_acknowledged=true")
        if self.method in {"GET", "HEAD", "OPTIONS"} and self.body.enabled:
            raise ValueError("This method cannot have a request body")
        if self.method == "HEAD" and self.json_assertions:
            raise ValueError("HEAD cannot have JSON assertions")
        all_headers = self.headers + self.secret_headers
        if len(all_headers) > 20:
            raise ValueError("At most 20 headers are allowed")
        names = [header.name.lower() for header in all_headers]
        if len(names) != len(set(names)):
            raise ValueError("Header names must be unique, ignoring case")
        if sum(len(h.name.encode()) + len(h.value.encode()) + 4 for h in all_headers) > 16384:
            raise ValueError("Total header size exceeds 16 KiB")
        if any(h.name.lower() == "authorization" for h in self.headers):
            raise ValueError("Authorization must be a secret header or bearer authentication")
        if self.auth.type == "bearer" and "authorization" in names:
            raise ValueError("Bearer authentication cannot be combined with an Authorization header")
        if (self.secret_headers or self.auth.type == "bearer") and not self.url.startswith("https://"):
            raise ValueError("Secrets can only be sent to HTTPS targets")
        query_size = sum(len(p.name.encode()) + len(p.value.encode()) for p in self.query_params)
        if query_size > 16384:
            raise ValueError("Total query parameters exceed 16 KiB")
        return self

    def model_dump(self, *args, **kwargs):
        """Keep the JSON wire field and the exists/equals distinction on disk."""
        kwargs.setdefault("by_alias", True)
        result = super().model_dump(*args, **kwargs)
        for assertion in result.get("json_assertions", []):
            if assertion.get("operator") == "exists":
                assertion.pop("expected", None)
        if result.get("auth", {}).get("type") == "none":
            result["auth"].pop("token", None)
        return result


def public_config(config: dict) -> dict:
    """Return a deep copy: no target credential is ever returned after creation."""
    result = copy.deepcopy(config)
    auth = result.get("auth", {"type": "none"})
    result["auth"] = {"type": auth.get("type", "none"), "configured": bool(auth.get("token"))}
    result["secret_headers"] = [
        {"name": item["name"], "configured": True} for item in result.get("secret_headers", [])
    ]
    return result
