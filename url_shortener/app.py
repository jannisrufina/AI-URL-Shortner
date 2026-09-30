from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from url_shortener.database import Database
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

    return FastAPI(lifespan=lifespan)


app = create_app()
