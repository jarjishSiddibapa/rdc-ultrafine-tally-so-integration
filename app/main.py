from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings
from app.core.database import get_engine, session_factory
from app.core.middleware import RequestContextMiddleware
from app.deps import LoginRequired, PasswordChangeRequired, csrf_protect
from app.routers import admin, auth, masters, notifications, orders, pages, reports
from app.services import auth as auth_service
from app.services import emailing, notify, outbox
from app.web import render

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("app")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout, force=True,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("uvicorn.access").disabled = True  # we write our own access log (no query strings)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _housekeeping(stop: asyncio.Event) -> None:
    while not stop.is_set():
        def purge() -> None:
            db = session_factory()()
            try:
                n = auth_service.purge_expired_sessions(db)
                db.commit()
                if n:
                    log.info("purged %s expired sessions", n)
                sent = notify.send_pending_reminders(db)       # once per approver per day (deduplicated)
                db.commit()
                if sent:
                    log.info("sent %s pending-order reminders", sent)
                gone = notify.purge_old(db) + emailing.purge_old(db)
                db.commit()
                if gone:
                    log.info("removed %s old notifications / emails", gone)
            finally:
                db.close()
        try:
            await asyncio.to_thread(purge)
        except Exception:
            log.exception("housekeeping failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


async def _mail_worker(stop: asyncio.Event, interval: float = 10.0) -> None:
    def tick() -> None:
        db = session_factory()()
        try:
            emailing.deliver_pending(db)
        finally:
            db.close()
    log.info("mail worker started (%s)", "configured" if get_settings().mail_configured else "email not configured: messages wait in the queue")
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("mail tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    get_engine()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(outbox.worker_loop(stop)), asyncio.create_task(_housekeeping(stop)), asyncio.create_task(_mail_worker(stop))]
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(*, run_background: bool = True) -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        lifespan=lifespan if run_background else None,
        docs_url=None if s.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_production else "/openapi.json",
        dependencies=[Depends(csrf_protect)],
    )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=s.allowed_host_list + (["testserver"] if s.app_env == "test" else []))
    app.add_middleware(RequestContextMiddleware)  # outermost: wraps everything, including host rejections

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    for r in (pages.router, auth.router, orders.router, notifications.router, reports.router, masters.router, admin.router):
        app.include_router(r)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        from urllib.parse import quote

        target = "/login" + (f"?next={quote(exc.next_url, safe='/')}" if exc.next_url and exc.next_url != "/" else "")
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _pw_required(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        titles = {403: "You do not have access", 404: "Page not found", 405: "Method not allowed", 413: "Request too large", 429: "Too many requests"}
        detail = exc.detail if isinstance(exc.detail, str) else ""
        return render(request, "error.html", {"code": exc.status_code, "title": titles.get(exc.status_code, "Something went wrong"),
                                              "detail": detail if exc.status_code in (403, 404) else ""}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        return render(request, "error.html", {"code": 422, "title": "That request was not valid",
                                              "detail": "Check the values you entered and try again."}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
        return render(request, "error.html", {"code": 500, "title": "Something went wrong on our side",
                                              "detail": f"Quote this reference to your administrator: {rid}"}, status_code=500)

    return app


app = create_app()
