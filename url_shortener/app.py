from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from url_shortener.database import Database
from url_shortener.rate_limiter import (
    CreateRateLimitExceeded,
    SlidingWindowRateLimiter,
    create_rate_limit_exception_handler,
)
from url_shortener.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings if settings is not None else Settings.from_env()
        database = Database(configured)
        database.open()
        application.state.settings = configured
        application.state.database = database
        try:
            yield
        finally:
            database.close()

    application = FastAPI(lifespan=lifespan)
    application.state.create_rate_limiter = SlidingWindowRateLimiter()
    application.add_exception_handler(
        CreateRateLimitExceeded, create_rate_limit_exception_handler
    )
    return application


app = create_app()
