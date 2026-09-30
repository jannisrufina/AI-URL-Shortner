from typing import Any

import psycopg
import pytest

from url_shortener.database import Database, DatabaseConnectionError
from url_shortener.settings import Settings


class FailingConnectionContext:
    def __enter__(self) -> Any:
        raise psycopg.OperationalError("simulated connection timeout")

    def __exit__(self, *args: object) -> None:
        return None


class FailingPool:
    def connection(self, *, timeout: float) -> FailingConnectionContext:
        return FailingConnectionContext()


def test_connection_timeout_uses_database_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://user:password@localhost/test_db",
            "PUBLIC_BASE_URL": "https://jrb.sh",
        }
    )
    database = Database(settings)
    monkeypatch.setattr(database, "_pool", FailingPool())

    with pytest.raises(DatabaseConnectionError), database.connection():
        pass
