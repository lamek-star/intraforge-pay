from __future__ import annotations


class DomainError(Exception):
    """A refusal the caller can be told about. `code` is stable for clients and tests; `message` is for people."""

    status = 400

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if status is not None:
            self.status = status


class NotFound(DomainError):
    status = 404


class Forbidden(DomainError):
    status = 403


class Conflict(DomainError):
    status = 409
