"""Consistent, safe public API errors."""


class APIError(Exception):
    def __init__(self, status, code, message, details=None, headers=None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}
