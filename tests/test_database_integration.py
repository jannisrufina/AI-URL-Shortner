import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient

from alembic import command
from url_shortener.app import create_app
from url_shortener.database import (
    Database,
    DatabasePoolTimeoutError,
    DatabaseStatementTimeoutError,
)
from url_shortener.links import Link, create_or_reuse_link
from url_shortener.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.environ.get("REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}:
            pytest.fail("REQUIRE_DB is set but TEST_DATABASE_URL is missing")
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


def test_exact_url_reuses_code_but_case_and_slash_remain_distinct(
    database_url: str,
) -> None:
    database = Database(_base_settings(database_url))
    database.open()
    base_url = f"https://example.test/exact/{uuid4().hex}"
    created_at = datetime.now(UTC)
    try:
        first = create_or_reuse_link(database, base_url, None, created_at)
        repeated = create_or_reuse_link(database, base_url, None, created_at)
        different_case = create_or_reuse_link(
            database, base_url.upper(), None, created_at
        )
        trailing_slash = create_or_reuse_link(
            database, f"{base_url}/", None, created_at
        )
    finally:
        database.close()

    assert repeated.code == first.code
    assert different_case.code != first.code
    assert trailing_slash.code != first.code


def test_concurrent_same_url_creates_arbitrate_to_one_row(
    database_url: str,
) -> None:
    database = Database(
        _base_settings(database_url, DB_POOL_MAX_SIZE="10", DB_POOL_WAIT_MS="2000")
    )
    database.open()
    submitted_url = f"https://example.test/concurrent/{uuid4().hex}"
    created_at = datetime.now(UTC)
    start = threading.Barrier(20)

    def create(_: int) -> Link:
        start.wait(timeout=10)
        return create_or_reuse_link(database, submitted_url, None, created_at)

    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(create, range(20)))
    finally:
        database.close()

    codes = {result.code for result in results}
    digest = sha256(submitted_url.encode("utf-8")).digest()
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM links WHERE url_digest = %s", (digest,)
        ).fetchone()

    assert len(codes) == 1
    assert count == (1,)


def test_code_primary_key_collision_retries_with_new_candidate(
    database_url: str,
) -> None:
    occupied_code = _code()
    retry_code = _code()
    with psycopg.connect(database_url) as connection:
        _insert(connection, occupied_code, _digest())

    database = Database(_base_settings(database_url))
    database.open()
    candidates = iter((occupied_code, retry_code))
    generated: list[str] = []

    def code_generator() -> str:
        code = next(candidates)
        generated.append(code)
        return code

    submitted_url = f"https://example.test/collision/{uuid4().hex}"
    try:
        result = create_or_reuse_link(
            database,
            submitted_url,
            None,
            datetime.now(UTC),
            code_generator,
        )
    finally:
        database.close()

    assert result.code == retry_code
    assert generated == [occupied_code, retry_code]


def test_expired_rows_are_retained_and_repeat_create_revives_same_code(
    database_url: str,
) -> None:
    database = Database(_base_settings(database_url))
    database.open()
    submitted_url = f"https://example.test/revive/{uuid4().hex}"
    created_at = datetime.now(UTC)
    expired_at = created_at - timedelta(seconds=1)
    revived_expiry = created_at + timedelta(days=30)
    try:
        expired = create_or_reuse_link(database, submitted_url, expired_at, created_at)
        retained = create_or_reuse_link(database, submitted_url, expired_at, created_at)
        revived = create_or_reuse_link(
            database, submitted_url, revived_expiry, created_at
        )
    finally:
        database.close()

    assert retained.code == expired.code
    assert revived.code == expired.code
    assert revived.expires_at == revived_expiry


@pytest.mark.parametrize(
    ("existing_expiry", "new_expiry", "expected_expiry"),
    [
        (None, datetime(2026, 10, 1, tzinfo=UTC), None),
        (datetime(2026, 10, 1, tzinfo=UTC), None, None),
        (
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
        ),
    ],
)
def test_repeat_expiry_rules_are_applied_atomically(
    database_url: str,
    existing_expiry: datetime | None,
    new_expiry: datetime | None,
    expected_expiry: datetime | None,
) -> None:
    database = Database(_base_settings(database_url))
    database.open()
    submitted_url = f"https://example.test/expiry/{uuid4().hex}"
    created_at = datetime.now(UTC)
    try:
        first = create_or_reuse_link(
            database, submitted_url, existing_expiry, created_at
        )
        repeated = create_or_reuse_link(database, submitted_url, new_expiry, created_at)
    finally:
        database.close()

    assert repeated.code == first.code
    assert repeated.expires_at == expected_expiry


def test_create_api_persists_and_reuses_link(database_url: str) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/api/{uuid4().hex}"
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/links", json={"url": submitted_url})
        repeated = client.post("/api/links", json={"url": submitted_url})

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["code"] == repeated.json()["code"]
    assert first.json()["short_url"] == f"https://jrb.sh/{first.json()['code']}"
    assert first.json()["expires_at"] is None
