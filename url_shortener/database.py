from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.errors import QueryCanceled
from psycopg_pool import ConnectionPool, PoolTimeout

from url_shortener.settings import Settings


class DatabaseAccessError(RuntimeError):
    """Base class for failures while acquiring or using a database connection."""


class DatabasePoolTimeoutError(DatabaseAccessError):
    """Raised when the pool cannot provide a connection within its wait limit."""


class DatabaseConnectionError(DatabaseAccessError):
    """Raised when PostgreSQL cannot establish or maintain a connection."""


class DatabaseStatementTimeoutError(DatabaseAccessError):
    """Raised when PostgreSQL cancels a statement at its configured timeout."""


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            kwargs={"connect_timeout": settings.connect_timeout_seconds},
            configure=self._configure_connection,
            open=False,
        )

    def _configure_connection(self, connection: Any) -> None:
        timeout = f"{self._settings.statement_timeout_ms}ms"
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)", (timeout,)
        )
        connection.commit()

    def open(self) -> None:
        wait_seconds = (
            self._settings.connect_timeout_seconds + self._settings.pool_wait_ms / 1000
        )
        try:
            self._pool.open(wait=True, timeout=wait_seconds)
        except PoolTimeout as error:
            self._pool.close()
            raise DatabasePoolTimeoutError(
                "Timed out while opening the PostgreSQL pool"
            ) from error
        except psycopg.OperationalError as error:
            self._pool.close()
            raise DatabaseConnectionError(
                "Could not establish a PostgreSQL connection"
            ) from error

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        try:
            with self._pool.connection(
                timeout=self._settings.pool_wait_ms / 1000
            ) as connection:
                try:
                    yield connection
                except QueryCanceled as error:
                    raise DatabaseStatementTimeoutError(
                        "PostgreSQL statement exceeded its timeout"
                    ) from error
        except PoolTimeout as error:
            raise DatabasePoolTimeoutError(
                "Timed out waiting for a PostgreSQL pool connection"
            ) from error
        except psycopg.OperationalError as error:
            raise DatabaseConnectionError("PostgreSQL connection failed") from error
