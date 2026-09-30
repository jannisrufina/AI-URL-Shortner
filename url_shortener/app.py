import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from url_shortener.database import Database, DatabaseAccessError
from url_shortener.expiry import ExpiryValidationError, parse_expiry
from url_shortener.links import (
    CodeGenerationExhaustedError,
    Link,
    create_or_reuse_link,
    get_link_by_code,
)
from url_shortener.rate_limiter import (
    CreateRateLimitExceeded,
    SlidingWindowRateLimiter,
    create_rate_limit_exception_handler,
    enforce_create_rate_limit,
)
from url_shortener.settings import Settings
from url_shortener.validation import URLValidationError, validate_url

_SHORT_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{7}")


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def request_validation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request, error
    return _error_response(422, "invalid_request", "Request body is invalid.")


async def url_validation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request
    validation_error = cast(URLValidationError, error)
    return _error_response(422, validation_error.code, validation_error.message)


async def expiry_validation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request
    validation_error = cast(ExpiryValidationError, error)
    return _error_response(422, validation_error.code, validation_error.message)


async def database_access_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request, error
    return _error_response(
        503, "service_unavailable", "The service is temporarily unavailable."
    )


async def code_generation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    del request, error
    return _error_response(
        503, "service_unavailable", "The service is temporarily unavailable."
    )


def _not_found_response() -> JSONResponse:
    return _error_response(404, "not_found", "Short link was not found.")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def record_click(link: Link) -> None:
    del link


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
        CreateRateLimitExceeded, create_rate_limit_exception_handler
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
        submitted_url = payload.get("url")
        if not isinstance(submitted_url, str):
            raise RequestValidationError([])

        validated_url = validate_url(
            submitted_url, request.app.state.self_reference_root
        )
        created_at = datetime.now(UTC)
        expiry_value = payload.get("expires_at")
        expires_at = parse_expiry(expiry_value, created_at)
        link = await run_in_threadpool(
            create_or_reuse_link,
            request.app.state.database,
            validated_url,
            expires_at,
            created_at,
        )
        public_base_url = request.app.state.settings.public_base_url.rstrip("/")
        return {
            "code": link.code,
            "short_url": f"{public_base_url}/{link.code}",
            "expires_at": link.expires_at,
        }

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
