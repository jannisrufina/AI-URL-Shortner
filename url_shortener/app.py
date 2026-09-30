import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader
from starlette.concurrency import run_in_threadpool
from starlette.templating import Jinja2Templates

from url_shortener.database import Database, DatabaseAccessError
from url_shortener.expiry import ExpiryValidationError, parse_expiry
from url_shortener.links import (
    CodeGenerationExhaustedError,
    Link,
    create_or_reuse_link,
    get_link_by_code,
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
    del link


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

    application = FastAPI(lifespan=lifespan)
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

    @application.post("/api/links", dependencies=[Depends(enforce_create_rate_limit)])
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

    @application.get("/")
    async def create_form_page(request: Request) -> Response:
        return _render_form(request)

    @application.post("/", dependencies=[Depends(enforce_create_rate_limit)])
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

    # Register any future fixed GET paths before this dynamic route.
    @application.get("/{code:path}", response_model=None)
    async def redirect_link(code: str, request: Request) -> Response:
        if _SHORT_CODE_PATTERN.fullmatch(code) is None:
            return _not_found_response()

        now = _utc_now()
        link = await run_in_threadpool(
            get_link_by_code, request.app.state.database, code
        )
        if link is None or (link.expires_at is not None and link.expires_at <= now):
            return _not_found_response()

        record_click(link)
        return RedirectResponse(url=link.original_url, status_code=302)

    return application


app = create_app()
