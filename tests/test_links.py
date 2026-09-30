from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from url_shortener.database import Database
from url_shortener.links import Link, create_or_reuse_link, generate_code


class FakeConnection:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: tuple[Any, ...] = ()

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> "FakeResult":
        self.statement = statement
        self.parameters = parameters
        return FakeResult(("AbC1234", parameters[2], parameters[3]))


class FakeResult:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self.row


class FakeDatabase:
    def __init__(self) -> None:
        self.connection_value = FakeConnection()
        self.connection_calls = 0

    def connection(self) -> "FakeDatabase":
        self.connection_calls += 1
        return self

    def __enter__(self) -> FakeConnection:
        return self.connection_value

    def __exit__(self, *args: object) -> None:
        return None


def test_create_uses_exact_url_digest_and_one_parameterized_statement() -> None:
    database = FakeDatabase()
    created_at = datetime(2026, 9, 30, tzinfo=UTC)
    submitted_url = "https://example.test/Exact/"

    link = create_or_reuse_link(
        cast(Database, database),
        submitted_url,
        None,
        created_at,
        lambda: "AbC1234",
    )

    assert link == Link("AbC1234", submitted_url, None)
    assert database.connection_calls == 1
    assert "ON CONFLICT (url_digest) DO UPDATE" in database.connection_value.statement
    assert (
        "RETURNING code, original_url, expires_at"
        in database.connection_value.statement
    )
    assert database.connection_value.parameters == (
        "AbC1234",
        sha256(submitted_url.encode("utf-8")).digest(),
        submitted_url,
        None,
        created_at,
    )


def test_generated_code_is_seven_base62_characters() -> None:
    code = generate_code()

    assert len(code) == 7
    assert code.isascii() and code.isalnum()
