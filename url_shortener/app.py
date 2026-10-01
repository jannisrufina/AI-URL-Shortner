import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Path as FastAPIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.templating import Jinja2Templates

from url_shortener.database import Database, DatabaseAccessError
from url_shortener.expiry import ExpiryValidationError, parse_expiry
from url_shortener.links import (
    CodeGenerationExhaustedError,
    Link,
    create_or_reuse_link,
    get_link_by_code,
    record_link_click,
)
from url_shortener.rate_limiter import (
    RATE_LIMIT_MESSAGE,
    CreateRateLimitExceeded,
    SlidingWindowRateLimiter,
    create_rate_limit_exception_handler,
    enforce_create_rate_limit,
)
from url_shortener.settings import Settings
from url_shortener.validation import URLValidationError, validate_url

_SHORT_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{7}")
_CLICK_DATABASE: ContextVar[Database | None] = ContextVar(
    "click_database", default=None
)
_LOGGER = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str


class Error(BaseModel):
    error: ErrorDetail


class CreateLinkResponse(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{7}$")
    short_url: str
    expires_at: datetime | None


_JSON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": Error, "description": "Invalid request data."},
    429: {"model": Error, "description": "Create rate limit exceeded."},
    503: {"model": Error, "description": "Service temporarily unavailable."},
}
_HTML_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "Invalid form data.",
        "content": {"text/html": {"schema": {"type": "string"}}},
    },
    429: {
        "description": "Create rate limit exceeded.",
        "content": {"text/html": {"schema": {"type": "string"}}},
    },
    503: {
        "description": "Service temporarily unavailable.",
        "content": {"text/html": {"schema": {"type": "string"}}},
    },
}
_HTML_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}
_TEMPLATES = Jinja2Templates(
    env=Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=True,
    )
)


@dataclass(frozen=True, slots=True)
class CreatedLink:
    code: str
    short_url: str
    expires_at: datetime | None


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _is_form_submission(request: Request) -> bool:
    return request.method == "POST" and request.url.path == "/"


def _render_form(
    request: Request,
    *,
    status_code: int = 200,
    short_url: str | None = None,
    error_message: str | None = None,
) -> Response:
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "url": getattr(request.state, "form_url", ""),
            "expires_at": getattr(request.state, "form_expires_at", ""),
            "short_url": short_url,
            "error_message": error_message,
        },
        status_code=status_code,
        headers=_HTML_HEADERS,
    )


def _route_error_response(
    request: Request, status_code: int, code: str, message: str
) -> Response:
    if _is_form_submission(request):
        return _render_form(request, status_code=status_code, error_message=message)
    return _error_response(status_code, code, message)


async def request_validation_exception_handler(
    request: Request, error: Exception
) -> Response:
    del error
    return _route_error_response(
        request, 422, "invalid_request", "Request body is invalid."
    )


async def url_validation_exception_handler(
    request: Request, error: Exception
) -> Response:
    validation_error = cast(URLValidationError, error)
    return _route_error_response(
        request, 422, validation_error.code, validation_error.message
    )


async def expiry_validation_exception_handler(
    request: Request, error: Exception
) -> Response:
    validation_error = cast(ExpiryValidationError, error)
    return _route_error_response(
        request, 422, validation_error.code, validation_error.message
    )


async def database_access_exception_handler(
    request: Request, error: Exception
) -> Response:
    del error
    return _route_error_response(
        request,
        503,
        "service_unavailable",
        "The service is temporarily unavailable.",
    )


async def code_generation_exception_handler(
    request: Request, error: Exception
) -> Response:
    del error
    return _route_error_response(
        request,
        503,
        "service_unavailable",
        "The service is temporarily unavailable.",
    )


async def app_rate_limit_exception_handler(
    request: Request, error: Exception
) -> Response:
    if _is_form_submission(request):
        return _render_form(request, status_code=429, error_message=RATE_LIMIT_MESSAGE)
    return await create_rate_limit_exception_handler(request, error)


def _not_found_response() -> JSONResponse:
    return _error_response(404, "not_found", "Short link was not found.")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def record_click(link: Link) -> None:
    database = _CLICK_DATABASE.get()
    if database is None:
        raise RuntimeError("No database available for click recording.")
    record_link_click(database, link.code)


async def create_short_link(
    request: Request, submitted_url: object, expiry_value: object
) -> CreatedLink:
    if not isinstance(submitted_url, str):
        raise RequestValidationError([])

    validated_url = validate_url(submitted_url, request.app.state.self_reference_root)
    created_at = _utc_now()
    expires_at = parse_expiry(expiry_value, created_at)
    link = await run_in_threadpool(
        create_or_reuse_link,
        request.app.state.database,
        validated_url,
        expires_at,
        created_at,
    )
    public_base_url = request.app.state.settings.public_base_url.rstrip("/")
    return CreatedLink(
        code=link.code,
        short_url=f"{public_base_url}/{link.code}",
        expires_at=link.expires_at,
    )


def _readyz_query(database: Database) -> None:
    with database.connection() as connection:
        connection.execute("SELECT 1 FROM links LIMIT 1").fetchone()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings if settings is not None else Settings.from_env()
        database = Database(configured)
        database.open()
        application.state.settings = configured
        application.state.database = database
        application.state.self_reference_root = urlsplit(
            configured.public_base_url
        ).hostname
        try:
            yield
        finally:
            database.close()

    application = FastAPI(
        title="AI URL Shortener API",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.create_rate_limiter = SlidingWindowRateLimiter()
    application.add_exception_handler(
        CreateRateLimitExceeded, app_rate_limit_exception_handler
    )
    application.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    application.add_exception_handler(
        URLValidationError, url_validation_exception_handler
    )
    application.add_exception_handler(
        ExpiryValidationError, expiry_validation_exception_handler
    )
    application.add_exception_handler(
        DatabaseAccessError, database_access_exception_handler
    )
    application.add_exception_handler(
        CodeGenerationExhaustedError, code_generation_exception_handler
    )

    @application.post(
        "/api/links",
        dependencies=[Depends(enforce_create_rate_limit)],
        summary="Create or reuse a short link",
        responses={
            200: {
                "model": CreateLinkResponse,
                "description": "HTTP 200 for both new and reused links.",
            },
            **_JSON_ERROR_RESPONSES,
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["url"],
                            "properties": {
                                "url": {"type": "string"},
                                "expires_at": {
                                    "type": ["string", "null"],
                                    "format": "date-time",
                                    "description": (
                                        "Optional RFC 3339 timestamp with an explicit "
                                        "timezone."
                                    ),
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def create_link(request: Request) -> dict[str, str | datetime | None]:
        try:
            payload = await request.json()
        except (ValueError, RecursionError):
            raise RequestValidationError([]) from None

        if not isinstance(payload, dict):
            raise RequestValidationError([])
        result = await create_short_link(
            request, payload.get("url"), payload.get("expires_at")
        )
        return {
            "code": result.code,
            "short_url": result.short_url,
            "expires_at": result.expires_at,
        }

    @application.get(
        "/",
        response_class=HTMLResponse,
        summary="Show the short-link form",
        responses={
            200: {
                "description": "HTML create form.",
                "content": {"text/html": {"schema": {"type": "string"}}},
            }
        },
    )
    async def create_form_page(request: Request) -> Response:
        return _render_form(request)

    @application.post(
        "/",
        dependencies=[Depends(enforce_create_rate_limit)],
        response_class=HTMLResponse,
        summary="Create or reuse a short link from the form",
        responses={
            200: {
                "description": "HTML form with the created or reused short URL.",
                "content": {"text/html": {"schema": {"type": "string"}}},
            },
            **_HTML_ERROR_RESPONSES,
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/x-www-form-urlencoded": {
                        "schema": {
                            "type": "object",
                            "required": ["url"],
                            "properties": {
                                "url": {"type": "string"},
                                "expires_at": {
                                    "type": "string",
                                    "format": "date-time",
                                    "description": (
                                        "Optional RFC 3339 timestamp with an explicit "
                                        "timezone."
                                    ),
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def create_form(request: Request) -> Response:
        try:
            form = await request.form()
        except HTTPException:
            return _render_form(
                request,
                status_code=422,
                error_message="Form data is invalid.",
            )

        submitted_url = form.get("url")
        expiry_value = form.get("expires_at")
        request.state.form_url = submitted_url if isinstance(submitted_url, str) else ""
        request.state.form_expires_at = (
            expiry_value if isinstance(expiry_value, str) else ""
        )
        if expiry_value == "":
            expiry_value = None

        result = await create_short_link(request, submitted_url, expiry_value)
        return _render_form(request, short_url=result.short_url)

    @application.get(
        "/livez",
        summary="Liveness check",
        responses={
            200: {
                "description": "Service is running.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        }
                    }
                },
            },
            503: {"model": Error, "description": "Service temporarily unavailable."},
        },
    )
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @application.get(
        "/readyz",
        summary="Readiness check",
        responses={
            200: {
                "description": "Service can run a query against links.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        }
                    }
                },
            },
            503: {"model": Error, "description": "Service temporarily unavailable."},
        },
    )
    async def readyz(request: Request) -> JSONResponse:
        try:
            await run_in_threadpool(_readyz_query, request.app.state.database)
        except Exception as exc:  # pragma: no cover - exercised by tests.
            _LOGGER.warning("Readiness check failed (%s)", exc.__class__.__name__)
            response = _error_response(
                503,
                "service_unavailable",
                "The service is temporarily unavailable.",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    # Register any future fixed GET paths before this dynamic route.
    @application.get(
        "/{code:path}",
        response_model=None,
        response_class=RedirectResponse,
        status_code=302,
        summary="Redirect a short code",
        responses={
            302: {
                "description": "Redirect to the stored original URL.",
                "headers": {
                    "Location": {
                        "description": "Stored original URL.",
                        "schema": {"type": "string", "format": "uri-reference"},
                    }
                },
            },
            404: {
                "model": Error,
                "description": "Malformed, missing, or expired code.",
            },
            503: {"model": Error, "description": "Service temporarily unavailable."},
        },
    )
    async def redirect_link(
        request: Request,
        code: str = FastAPIPath(
            description="Seven-character Base62 short code.",
            json_schema_extra={"pattern": "^[A-Za-z0-9]{7}$"},
        ),
    ) -> Response:
        if _SHORT_CODE_PATTERN.fullmatch(code) is None:
            return _not_found_response()

        now = _utc_now()
        link = await run_in_threadpool(
            get_link_by_code, request.app.state.database, code
        )
        if link is None or (link.expires_at is not None and link.expires_at <= now):
            return _not_found_response()

        click_database_token = _CLICK_DATABASE.set(request.app.state.database)
        try:
            await run_in_threadpool(record_click, link)
        except Exception:
            _LOGGER.warning("Click recording failed for short code %s", link.code)
        finally:
            _CLICK_DATABASE.reset(click_database_token)
        return RedirectResponse(url=link.original_url, status_code=302)

    return application


app = create_app()
