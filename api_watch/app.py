"""Same-origin web application and versioned REST API."""

import logging
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException

from api_watch import __version__
from api_watch.config import Settings
from api_watch.contracts import ErrorEnvelope, HealthLiveRead, HealthReadyRead, ServiceStatusRead
from api_watch.db import Base, make_database, utcnow
from api_watch.errors import APIError
from api_watch.models import CheckRun, Heartbeat
from api_watch.security import password_hash, random_token


def create_app(settings=None, initialize=False):
    settings = (settings or Settings()).ensure_local_secrets()
    engine, factory = make_database(settings)
    if initialize:
        Base.metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(application):
        yield
        engine.dispose()

    app = FastAPI(title="API Watch", version=__version__, description=(
        "Secure, observation-based website and REST API monitoring. "
        "Browser sessions use CSRF protection; personal API tokens use Bearer authentication. "
        "External target errors are check outcomes, not errors of this API."), lifespan=lifespan,
        responses={code: {"model": ErrorEnvelope} for code in [400, 401, 403, 404, 409, 413, 422, 429, 500, 503]})
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.dummy_password_hash = password_hash(random_token())
    app.state.initialized_for_tests = initialize

    def error_response(request, status, code, message, details=None, headers=None):
        response = JSONResponse(status_code=status, content=jsonable_encoder({"error": {
            "code": code, "message": message, "details": details or [],
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4()))}}), headers=headers)
        return response_headers(request, response)

    def response_headers(request, response):
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.middleware("http")
    async def request_policy(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > 262144 or int(length) < 0
            except ValueError:
                return error_response(request, 400, "INVALID_CONTENT_LENGTH", "Invalid Content-Length.")
            if too_large:
                return error_response(request, 413, "REQUEST_TOO_LARGE", "The request body limit is 256 KiB.")
        # Enforce the limit even for a chunked request without Content-Length.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            collected = bytearray()
            async for chunk in request.stream():
                collected.extend(chunk)
                if len(collected) > 262144:
                    return error_response(request, 413, "REQUEST_TOO_LARGE", "The request body limit is 256 KiB.")
            request._body = bytes(collected)
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if collected and (not media_type or media_type == "application/json" or media_type.endswith("+json")):
                from api_watch.checker import strict_json_loads
                try:
                    strict_json_loads(bytes(collected), exact_integers=False)
                except (ValueError, UnicodeError, RecursionError):
                    return error_response(request, 400, "INVALID_JSON",
                        "The request body must be unambiguous bounded UTF-8 JSON.")
        response = await call_next(request)
        return response_headers(request, response)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            return error_response(request, 400, "INVALID_JSON", "The request body is not valid JSON.")
        details = [{"field": ".".join(map(str, e["loc"])), "code": e["type"], "message": "Invalid value."}
                   for e in exc.errors()]
        return error_response(request, 422, "VALIDATION_ERROR", "The request did not pass validation.", details)

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(request, exc):
        return error_response(request, 400, "INVALID_JSON", "The request body is not valid JSON.")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, "HTTP_ERROR", str(exc.detail), headers=exc.headers)

    @app.exception_handler(IntegrityError)
    async def conflict_error(request, exc):
        return error_response(request, 409, "CONCURRENT_CHANGE", "The resource changed concurrently. Reload and retry.")

    @app.exception_handler(OperationalError)
    async def dependency_error(request, exc):
        return error_response(request, 503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable.")

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("api_watch").error("internal_error request_id=%s type=%s",
                                            request.state.request_id, type(exc).__name__)
        return error_response(request, 500, "INTERNAL_ERROR", "An internal error occurred.")

    from api_watch import auth, integrations, monitors, statistics
    app.include_router(auth.router)
    app.include_router(monitors.router)
    app.include_router(statistics.router)
    app.include_router(integrations.router)

    @app.get("/health/live", tags=["Health"], response_model=HealthLiveRead)
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"], response_model=HealthReadyRead,
             responses={503: {"model": HealthReadyRead, "description": "Database or migration is not ready."}})
    def ready():
        try:
            with factory() as db:
                if not initialize:
                    revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                    if revision != "0001":
                        return JSONResponse({"status": "not_ready"}, 503)
                db.execute(select(CheckRun.id).limit(1)).first()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, 503)

    from fastapi import Depends
    from api_watch.api_utils import principal, get_db

    @app.get("/api/v1/service-status", tags=["Health"], response_model=ServiceStatusRead)
    def authenticated_status(auth=Depends(principal), db=Depends(get_db)):
        now = utcnow()
        return {"processes": [{"name": row.id, "last_seen_at": row.last_seen_at,
                               "healthy": (now - row.last_seen_at).total_seconds() < 60}
                              for row in db.scalars(select(Heartbeat))]}

    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app


app = create_app()
