import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from url_shortener.database import (
    Database,
    DatabasePoolTimeoutError,
    DatabaseStatementTimeoutError,
)
from url_shortener.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = value
    try:
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
    return value


def _base_settings(database_url: str, **overrides: str) -> Settings:
    environment = {
        "DATABASE_URL": database_url,
        "PUBLIC_BASE_URL": "https://jrb.sh",
        **overrides,
    }
    return Settings.from_env(environment)


def _insert(
    connection: psycopg.Connection,
    code: str,
    digest: bytes,
) -> None:
    connection.execute(
        """
        INSERT INTO links (code, url_digest, original_url, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            code,
            digest,
            "https://example.test/",
            None,
            datetime.now(UTC),
        ),
    )


def _code() -> str:
    return uuid4().hex[:7]


def _digest() -> bytes:
    return uuid4().bytes + uuid4().bytes


def test_migration_applies_and_is_repeatable(database_url: str) -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
        command.upgrade(config, "head")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    with psycopg.connect(database_url) as connection:
        tables = connection.execute("SELECT to_regclass('public.links')").fetchone()
    assert tables is not None and tables[0] == "links"


def test_schema_has_both_unique_indexes(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        indexes: dict[str, str] = dict(
            connection.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'links'
                """
            ).fetchall()
        )

    assert "links_pkey" in indexes
    assert "UNIQUE" in indexes["links_pkey"]
    assert "links_url_digest_uq" in indexes
    assert "UNIQUE" in indexes["links_url_digest_uq"]
    assert "url_digest" in indexes["links_url_digest_uq"]


def test_schema_constraints_reject_invalid_rows(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            _insert(connection, "bad-cod", _digest())

        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            _insert(connection, _code(), bytes(31))

        duplicate_digest = _digest()
        with connection.transaction():
            _insert(connection, _code(), duplicate_digest)
        with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
            _insert(connection, _code(), duplicate_digest)

        duplicate_code = _code()
        with connection.transaction():
            _insert(connection, duplicate_code, _digest())
        with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
            _insert(connection, duplicate_code, _digest())


def test_pool_exhaustion_has_database_error_type(database_url: str) -> None:
    settings = _base_settings(
        database_url,
        DB_POOL_MIN_SIZE="1",
        DB_POOL_MAX_SIZE="1",
        DB_POOL_WAIT_MS="50",
    )
    database = Database(settings)
    database.open()
    try:
        with (
            database.connection(),
            pytest.raises(DatabasePoolTimeoutError),
            database.connection(),
        ):
            pass
    finally:
        database.close()


def test_statement_timeout_has_database_error_type(database_url: str) -> None:
    settings = _base_settings(
        database_url,
        DB_POOL_MIN_SIZE="1",
        DB_POOL_MAX_SIZE="1",
        DB_STATEMENT_TIMEOUT_MS="25",
    )
    database = Database(settings)
    database.open()
    try:
        with (
            pytest.raises(DatabaseStatementTimeoutError),
            database.connection() as connection,
        ):
            connection.execute("SELECT pg_sleep(0.2)")
    finally:
        database.close()
