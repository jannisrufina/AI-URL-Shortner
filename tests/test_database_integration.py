import logging
import os
import threading
from collections.abc import Callable
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
from scripts.seed_links import code_for_index, seed_links
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


def _run_alembic(
    database_url: str,
    migration: Callable[[Config, str], None],
    revision: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        migration(config, revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


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


def test_create_then_redirect_uses_stored_url(database_url: str) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/path/{uuid4().hex}?q=value#part"
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/links", json={"url": submitted_url})
        redirected = client.get(f"/{created.json()['code']}", follow_redirects=False)

    assert created.status_code == 200
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url


def test_expired_redirect_is_404_and_row_remains_stored(database_url: str) -> None:
    settings = _base_settings(database_url)
    database = Database(settings)
    database.open()
    submitted_url = f"https://example.test/expired/{uuid4().hex}"
    created_at = datetime.now(UTC)
    try:
        expired = create_or_reuse_link(
            database,
            submitted_url,
            created_at - timedelta(seconds=1),
            created_at,
        )
    finally:
        database.close()

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/{expired.code}", follow_redirects=False)

    with psycopg.connect(database_url) as connection:
        retained = connection.execute(
            "SELECT code FROM links WHERE code = %s", (expired.code,)
        ).fetchone()

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert retained == (expired.code,)


def test_repeat_create_revives_expired_redirect(database_url: str) -> None:
    settings = _base_settings(database_url)
    database = Database(settings)
    database.open()
    submitted_url = f"https://example.test/revive-redirect/{uuid4().hex}"
    created_at = datetime.now(UTC)
    try:
        expired = create_or_reuse_link(
            database,
            submitted_url,
            created_at - timedelta(seconds=1),
            created_at,
        )
    finally:
        database.close()

    with TestClient(create_app(settings)) as client:
        before_revival = client.get(f"/{expired.code}", follow_redirects=False)
        recreated = client.post("/api/links", json={"url": submitted_url})
        after_revival = client.get(f"/{expired.code}", follow_redirects=False)

    assert before_revival.status_code == 404
    assert recreated.status_code == 200
    assert recreated.json()["code"] == expired.code
    assert after_revival.status_code == 302
    assert after_revival.headers["location"] == submitted_url


def test_form_and_json_api_reuse_the_same_persisted_code(
    database_url: str,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/shared-create/{uuid4().hex}"
    with TestClient(create_app(settings)) as client:
        api_response = client.post("/api/links", json={"url": submitted_url})
        form_response = client.post("/", data={"url": submitted_url})

    assert api_response.status_code == 200
    assert form_response.status_code == 200
    assert api_response.json()["code"] in form_response.text
    assert api_response.json()["short_url"] in form_response.text


def test_seed_links_accepts_real_schema_and_rolls_back(database_url: str) -> None:
    class RollbackSeed(Exception):
        pass

    codes = [code_for_index(index) for index in range(1000)]
    with psycopg.connect(database_url) as connection:
        before = connection.execute(
            "SELECT count(*) FROM links WHERE code = ANY(%s)", (codes,)
        ).fetchone()

        with pytest.raises(RollbackSeed), connection.transaction():
            inserted = seed_links(connection, count=1000, seed=314159, batch_size=173)
            data = connection.execute(
                """
                    SELECT count(*),
                           count(DISTINCT code),
                           count(DISTINCT url_digest),
                           count(*) FILTER (WHERE code !~ '^[A-Za-z0-9]{7}$'),
                           count(*) FILTER (WHERE octet_length(url_digest) <> 32)
                    FROM links
                    WHERE code = ANY(%s)
                    """,
                (codes,),
            ).fetchone()
            constraints = {
                row[0]
                for row in connection.execute(
                    """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid = 'public.links'::regclass
                        """
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = 'public' AND tablename = 'links'
                        """
                ).fetchall()
            }

            assert inserted == 1000
            assert data == (1000, 1000, 1000, 0, 0)
            assert {"links_code_format", "links_url_digest_length"} <= constraints
            assert {"links_pkey", "links_url_digest_uq"} <= indexes
            raise RollbackSeed

        remaining = connection.execute(
            "SELECT count(*) FROM links WHERE code = ANY(%s)", (codes,)
        ).fetchone()

    assert before == (0,)
    assert remaining == (0,)


def test_end_to_end_api_create_redirect_and_form_reuse_logs_no_url(
    database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/task9/{uuid4().hex}?query=value#anchor"
    with caplog.at_level(logging.DEBUG), TestClient(create_app(settings)) as client:
        created = client.post("/api/links", json={"url": submitted_url})
        short_url = created.json()["short_url"]
        redirected = client.get(f"/{created.json()['code']}", follow_redirects=False)
        form_result = client.post("/", data={"url": submitted_url})

    assert created.status_code == 200
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url
    assert form_result.status_code == 200
    assert short_url in form_result.text
    assert f'href="{short_url}"' in form_result.text
    assert submitted_url not in caplog.text


def test_expiry_set_through_api_redirects_before_expiration(
    database_url: str,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/task9-expiry/{uuid4().hex}"
    first_expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    replacement_expiry = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    permanent_url = f"https://example.test/task9-permanent/{uuid4().hex}"
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/links",
            json={"url": submitted_url, "expires_at": first_expiry},
        )
        replaced = client.post(
            "/api/links",
            json={"url": submitted_url, "expires_at": replacement_expiry},
        )
        made_permanent = client.post("/api/links", json={"url": submitted_url})
        redirected = client.get(f"/{created.json()['code']}", follow_redirects=False)
        permanent = client.post("/api/links", json={"url": permanent_url})
        expiry_does_not_change_permanent = client.post(
            "/api/links",
            json={"url": permanent_url, "expires_at": replacement_expiry},
        )

    assert created.status_code == 200
    assert replaced.status_code == 200
    assert replaced.json()["code"] == created.json()["code"]
    assert replaced.json()["expires_at"].startswith(replacement_expiry[:19])
    assert made_permanent.json()["expires_at"] is None
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url
    assert permanent.status_code == 200
    assert expiry_does_not_change_permanent.status_code == 200
    assert expiry_does_not_change_permanent.json()["code"] == permanent.json()["code"]
    assert expiry_does_not_change_permanent.json()["expires_at"] is None


def test_http_rate_limit_ignores_forwarded_headers_and_not_redirects(
    database_url: str,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/task9-limiter/{uuid4().hex}"
    forwarded_headers = [
        {"X-Forwarded-For": f"198.51.100.{index + 1}"} for index in range(11)
    ]
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/links", json={"url": submitted_url}, headers=forwarded_headers[0]
        )
        allowed = [
            client.post(
                "/api/links",
                json={"url": submitted_url},
                headers=forwarded_headers[index],
            )
            for index in range(1, 10)
        ]
        limited = client.post(
            "/api/links",
            json={"url": submitted_url},
            headers=forwarded_headers[10],
        )
        redirected = client.get(f"/{created.json()['code']}", follow_redirects=False)

    assert created.status_code == 200
    assert all(response.status_code == 200 for response in allowed)
    assert limited.status_code == 429
    assert limited.headers["content-type"].startswith("application/json")
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url


def test_exact_url_identity_through_create_api(database_url: str) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/Case/{uuid4().hex}"
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/links", json={"url": submitted_url})
        repeated = client.post("/api/links", json={"url": submitted_url})
        different_case = client.post(
            "/api/links", json={"url": submitted_url.replace("Case", "case")}
        )
        trailing_slash = client.post("/api/links", json={"url": f"{submitted_url}/"})

    assert first.status_code == 200
    assert repeated.json()["code"] == first.json()["code"]
    assert different_case.status_code == 200
    assert different_case.json()["code"] != first.json()["code"]
    assert trailing_slash.status_code == 200
    assert trailing_slash.json()["code"] != first.json()["code"]


@pytest.mark.parametrize(
    ("submitted_url", "expected_code"),
    [
        ("ftp://example.test/path", "invalid_url"),
        ("https://user:pass@example.test/path", "credentials_not_allowed"),
        ("https://api.jrb.sh/path", "self_reference"),
        ("http://127.0.0.1/path", "private_host"),
    ],
)
def test_create_api_url_policy_rejections_through_real_app(
    database_url: str,
    submitted_url: str,
    expected_code: str,
) -> None:
    settings = _base_settings(database_url)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/links", json={"url": submitted_url})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


def test_missing_and_malformed_redirects_through_real_app(
    database_url: str,
) -> None:
    settings = _base_settings(database_url)
    with psycopg.connect(database_url) as connection:
        missing_code = _code()
        while connection.execute(
            "SELECT 1 FROM links WHERE code = %s", (missing_code,)
        ).fetchone():
            missing_code = _code()

    with TestClient(create_app(settings)) as client:
        missing = client.get(f"/{missing_code}", follow_redirects=False)
        malformed = client.get("/bad-code!", follow_redirects=False)

    assert missing.status_code == 404
    assert malformed.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert malformed.json()["error"]["code"] == "not_found"


def test_form_escapes_url_through_real_postgres(
    database_url: str,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = (
        f"https://example.test/task9-escape/{uuid4().hex}/<script>alert(1)</script>"
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/", data={"url": submitted_url})

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "https://jrb.sh/" in response.text


def test_successful_redirect_records_exactly_one_click(database_url: str) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/click-event/{uuid4().hex}"
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/links", json={"url": submitted_url})
        code = created.json()["code"]
        redirected = client.get(f"/{code}", follow_redirects=False)

    with psycopg.connect(database_url) as connection:
        events = connection.execute(
            "SELECT code, clicked_at FROM link_clicks WHERE code = %s", (code,)
        ).fetchall()

    assert created.status_code == 200
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url
    assert len(events) == 1
    assert events[0][0] == code
    assert events[0][1].tzinfo is not None


def test_redirect_404_cases_do_not_record_clicks(database_url: str) -> None:
    settings = _base_settings(database_url)
    database = Database(settings)
    database.open()
    submitted_url = f"https://example.test/click-expired/{uuid4().hex}"
    created_at = datetime.now(UTC)
    try:
        expired = create_or_reuse_link(
            database,
            submitted_url,
            created_at - timedelta(seconds=1),
            created_at,
        )
    finally:
        database.close()

    missing_code = _code()
    with psycopg.connect(database_url) as connection:
        while connection.execute(
            "SELECT 1 FROM links WHERE code = %s", (missing_code,)
        ).fetchone():
            missing_code = _code()
        before = connection.execute("SELECT count(*) FROM link_clicks").fetchone()

    with TestClient(create_app(settings)) as client:
        malformed = client.get("/bad-code!", follow_redirects=False)
        missing = client.get(f"/{missing_code}", follow_redirects=False)
        expired_response = client.get(f"/{expired.code}", follow_redirects=False)

    with psycopg.connect(database_url) as connection:
        after = connection.execute("SELECT count(*) FROM link_clicks").fetchone()

    assert malformed.status_code == 404
    assert missing.status_code == 404
    assert expired_response.status_code == 404
    assert after == before


def test_missing_clicks_table_does_not_change_redirect(
    database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _base_settings(database_url)
    submitted_url = f"https://example.test/click-missing-table/{uuid4().hex}"
    hidden_table_name = "link_clicks_hidden_for_test"

    with caplog.at_level(logging.WARNING), TestClient(create_app(settings)) as client:
        created = client.post("/api/links", json={"url": submitted_url})
        code = created.json()["code"]
        with psycopg.connect(database_url) as connection:
            connection.execute(
                f'ALTER TABLE link_clicks RENAME TO "{hidden_table_name}"'
            )
        try:
            redirected = client.get(f"/{code}", follow_redirects=False)
        finally:
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    f'ALTER TABLE "{hidden_table_name}" RENAME TO link_clicks'
                )

    with psycopg.connect(database_url) as connection:
        click_rows = connection.execute(
            "SELECT count(*) FROM link_clicks WHERE code = %s", (code,)
        ).fetchone()

    assert created.status_code == 200
    assert redirected.status_code == 302
    assert redirected.headers["location"] == submitted_url
    assert code in caplog.text
    assert submitted_url not in caplog.text
    assert click_rows == (0,)


def test_click_migration_downgrade_preserves_links(database_url: str) -> None:
    code = _code()
    with psycopg.connect(database_url) as connection:
        _insert(connection, code, _digest())

    _run_alembic(database_url, command.downgrade, "20260929_0001")
    try:
        with psycopg.connect(database_url) as connection:
            relations = connection.execute(
                "SELECT to_regclass('public.links'), to_regclass('public.link_clicks')"
            ).fetchone()
            retained_link = connection.execute(
                "SELECT code FROM links WHERE code = %s", (code,)
            ).fetchone()
        assert relations == ("links", None)
        assert retained_link == (code,)
    finally:
        _run_alembic(database_url, command.upgrade, "head")

    with psycopg.connect(database_url) as connection:
        click_table = connection.execute(
            "SELECT to_regclass('public.link_clicks')"
        ).fetchone()
    assert click_table == ("link_clicks",)
