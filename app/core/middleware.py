"""Pure-ASGI middleware: request id + access log, body-size cap, security headers."""
from __future__ import annotations

import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

access_log = logging.getLogger("app.access")


class RequestContextMiddleware:
    """Assigns a request id, caps request bodies, adds security headers, writes the access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.s = get_settings()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status_holder = {"code": 500}
        max_body = self.s.max_body_bytes

        # Reject oversize bodies early by header, and again while streaming.
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > max_body:
                    await self._reject(scope, receive, send, rid, 413)
                    return
            except ValueError:
                await self._reject(scope, receive, send, rid, 400)
                return

        received = 0
        too_large = False
        replaced = False

        async def limited_receive() -> Message:
            # Chunked bodies have no Content-Length: stop feeding the app once the cap is hit
            # and answer 413 instead of whatever it would have produced from the truncated body.
            nonlocal received, too_large
            if too_large:
                return {"type": "http.request", "body": b"", "more_body": False}
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > max_body:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def send_wrapper(message: Message) -> None:
            nonlocal replaced
            if too_large:
                if not replaced:
                    replaced = True
                    status_holder["code"] = 413
                    await self._reject(scope, receive, send, rid, 413)
                return
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                hdrs = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                hdrs.extend(self._security_headers(scope, rid))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        finally:
            ms = (time.perf_counter() - start) * 1000
            access_log.info(
                "%s %s -> %s %.1fms rid=%s",
                scope.get("method"), scope.get("path"), status_holder["code"], ms, rid,
            )

    def _security_headers(self, scope: Scope, rid: str) -> list[tuple[bytes, bytes]]:
        path = scope.get("path", "")
        h = {
            "x-request-id": rid,
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "same-origin",
            "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-resource-policy": "same-origin",
            "content-security-policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
        }
        if not path.startswith("/static/"):
            h["cache-control"] = "no-store"  # authenticated pages must never sit in shared caches
        if self.s.is_production:
            h["strict-transport-security"] = "max-age=31536000; includeSubDomains"
        return [(k.encode(), v.encode()) for k, v in h.items()]

    async def _reject(self, scope: Scope, receive: Receive, send: Send, rid: str, status: int) -> None:
        body = b"Request too large" if status == 413 else b"Bad request"
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close"), *self._security_headers(scope, rid)]})
        await send({"type": "http.response.body", "body": body})

