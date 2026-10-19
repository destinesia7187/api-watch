"""Password hashing, credential digests, and authenticated payload encryption."""

import base64
import hashlib
import hmac
import json
import secrets
from decimal import Decimal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def random_token():
    return secrets.token_urlsafe(32)


def csrf_token(raw_session, settings):
    return hmac.new(settings.session_secret.encode(), raw_session.encode(), hashlib.sha256).hexdigest()


def password_hash(password):
    return PASSWORDS.hash(password)


def verify_password(encoded, password):
    try:
        return PASSWORDS.verify(encoded, password)
    except (VerificationError, InvalidHashError, UnicodeError):
        return False


def encrypt_payload(payload, settings):
    from api_watch.schemas import json_dumps_exact
    key = base64.urlsafe_b64decode(settings.encryption_key)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, json_dumps_exact(payload).encode(),
                                     settings.encryption_key_id.encode())
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_payload(ciphertext, settings):
    raw = base64.urlsafe_b64decode(ciphertext)
    key = base64.urlsafe_b64decode(settings.encryption_key)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], settings.encryption_key_id.encode()), parse_float=Decimal)
