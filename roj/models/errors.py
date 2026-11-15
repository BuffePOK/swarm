"""Gateway error classes. The class decides the reaction, not the message text."""

from __future__ import annotations

from typing import Any

from ..util import truncate


class ModelError(Exception):
    retryable = False

    def __init__(self, model: str, status: int | None, detail: str = ""):
        self.model, self.status, self.detail = model, status, truncate(detail, 500)
        super().__init__(f"{type(self).__name__}({model}, status={status}): {self.detail}")

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "error": type(self).__name__, "status": self.status, "detail": self.detail}


class Transient(ModelError):
    """5xx, connection reset: retry with backoff, then fall back."""

    retryable = True


class Timeout(Transient):
    pass


class RateLimited(ModelError):
    """429: wait Retry-After (bounded), then fall back."""

    retryable = True

    def __init__(self, model: str, status: int | None, detail: str = "", retry_after: float | None = None):
        super().__init__(model, status, detail)
        self.retry_after = retry_after


class Blocked(ModelError):
    """403: access refused (ToS, disabled account). Never retried; model cooled down for hours."""


class AuthError(ModelError):
    """401: bad or missing key. Never retried."""


class BadRequest(ModelError):
    """400/413/422: our request is wrong. Raised to the caller, no fallback hides the bug."""


class ModelNotFound(ModelError):
    """404: model gone from the gateway. Fall back and tell the Architect."""


class Unpriced(ModelError):
    """strict pricing on and the model has no price: skip it."""


class AllModelsFailed(Exception):
    def __init__(self, errors: list[ModelError]):
        self.errors = errors
        super().__init__("all candidate models failed: " + "; ".join(str(e) for e in errors))

    def summary(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.errors]


def classify(model: str, status: int, body: str, headers: dict[str, str] | None = None) -> ModelError:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if status == 429:
        ra = headers.get("retry-after")
        try:
            retry_after = float(ra) if ra is not None else None
        except ValueError:
            retry_after = None
        return RateLimited(model, status, body, retry_after)
    if status == 403:
        return Blocked(model, status, body)
    if status == 401:
        return AuthError(model, status, body)
    if status == 404:
        return ModelNotFound(model, status, body)
    if status in (400, 413, 422):
        return BadRequest(model, status, body)
    if status == 408 or status >= 500:
        return Transient(model, status, body)
    return BadRequest(model, status, body)
