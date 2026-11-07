"""Tally integration boundary.

The Tally API is not available yet. Approved orders are written to the ``tally_outbox`` table in the
same transaction as the approval, so nothing is lost. When ``TALLY_API_URL`` is set, the background
worker (app/services/outbox.py) delivers each payload through ``HttpTallyGateway``.

When the real API is known, only ``HttpTallyGateway.send`` needs to change (e.g. render the payload
as Tally XML). The idempotency key is sent as a header so a retried request can be de-duplicated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import get_settings


class TallyError(Exception):
    """Delivery failed; the outbox will retry with backoff."""


@dataclass(slots=True)
class TallyResult:
    reference: str | None = None


class TallyGateway(Protocol):
    @property
    def configured(self) -> bool: ...

    def send(self, payload: dict[str, Any], idempotency_key: str) -> TallyResult: ...


class HttpTallyGateway:
    def __init__(self, url: str, token: str, timeout: float = 20.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def send(self, payload: dict[str, Any], idempotency_key: str) -> TallyResult:
        headers = {"Idempotency-Key": idempotency_key, "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            # follow_redirects=False: never forward the token to a redirect target.
            resp = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TallyError(f"Could not reach Tally: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise TallyError(f"Tally responded with HTTP {resp.status_code}")
        ref = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                ref = str(body.get("reference") or body.get("id") or "")[:100] or None
        except ValueError:
            pass
        return TallyResult(reference=ref)


def get_gateway() -> TallyGateway:
    s = get_settings()
    return HttpTallyGateway(s.tally_api_url, s.tally_api_token)
