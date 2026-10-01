from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

import scripts.seed_links as seeder


def test_codes_are_base62_and_unique_over_100k_rows() -> None:
    seen: set[str] = set()

    for code, *_ in seeder.iter_rows(100_001, seed=23):
        assert len(code) == 7
        assert all(character in seeder.BASE62_ALPHABET for character in code)
        assert code not in seen
        seen.add(code)

    assert len(seen) == 100_001


def test_generated_digests_are_exact_and_unique() -> None:
    seen: set[bytes] = set()

    for _, digest, original_url, _, _ in seeder.iter_rows(10_000, seed=7):
        assert len(digest) == 32
        assert digest == sha256(original_url.encode("utf-8")).digest()
        assert digest not in seen
        seen.add(digest)


def test_seeded_rows_are_repeatable_and_seed_changes_random_fields() -> None:
    first = list(seeder.iter_rows(100, seed=10))
    repeated = list(seeder.iter_rows(100, seed=10))
    other_seed = list(seeder.iter_rows(100, seed=11))

    assert first == repeated
    assert first != other_seed
    assert [(row[0], row[1], row[2]) for row in first] == [
        (row[0], row[1], row[2]) for row in other_seed
    ]
    assert any(
        (row[3], row[4]) != (other[3], other[4])
        for row, other in zip(first, other_seed, strict=True)
    )
    assert all(row[4].tzinfo is UTC for row in first)


def test_expiry_rate_is_five_percent_and_expiries_are_30_to_365_days_later() -> None:
    rows = list(seeder.iter_rows(20_000, seed=2026))
    expired_count = sum(expires_at is not None for _, _, _, expires_at, _ in rows)

    assert 900 <= expired_count <= 1_100
    assert all(
        expires_at is None
        or timedelta(days=30) <= expires_at - created_at <= timedelta(days=365)
        for _, _, _, expires_at, created_at in rows
    )


def test_created_at_is_deterministic_recent_and_fixed_utc() -> None:
    first = list(seeder.iter_rows(2, seed=1))
    repeated = list(seeder.iter_rows(2, seed=1))
    second = list(seeder.iter_rows(2, seed=2))
    created_at = first[0][4]
    anchor = datetime(2026, 9, 1, tzinfo=UTC)

    assert created_at == repeated[0][4]
    assert second[0][4] != created_at
    assert created_at.tzinfo is UTC
    assert anchor <= created_at < anchor + timedelta(days=1)


def test_sample_codes_are_deterministic_and_contain_no_urls(tmp_path: Path) -> None:
    sample_path = tmp_path / "codes.txt"

    written = seeder.write_sample_codes(sample_path, count=20, sample_count=5)

    lines = sample_path.read_text(encoding="ascii").splitlines()
    assert written == 5
    assert lines == [seeder.code_for_index(index * 20 // 5) for index in range(5)]
    assert all("https://" not in line for line in lines)


def test_sample_codes_count_larger_than_dataset_writes_all_codes(
    tmp_path: Path,
) -> None:
    sample_path = tmp_path / "all-codes.txt"

    written = seeder.write_sample_codes(sample_path, count=3, sample_count=10)

    assert written == 3
    assert sample_path.read_text(encoding="ascii").splitlines() == [
        seeder.code_for_index(index) for index in range(3)
    ]


def test_database_name_must_be_explicit() -> None:
    assert (
        seeder.database_name("postgresql://bench:secret@localhost/url_shortener_bench")
        == "url_shortener_bench"
    )
    with pytest.raises(ValueError):
        seeder.database_name("postgresql://bench:secret@localhost/")


def test_database_name_guard_requires_benchmark_suffix() -> None:
    with pytest.raises(ValueError, match="end in '_bench'"):
        seeder.validate_cli_options(
            "url_shortener",
            count=1,
            batch_size=1,
            sample_count=0,
            allow_large=False,
            reset=False,
            confirm_database=None,
        )


def test_reset_requires_exact_database_confirmation() -> None:
    options: dict[str, Any] = {
        "count": 1,
        "batch_size": 1,
        "sample_count": 0,
        "allow_large": False,
        "reset": True,
        "confirm_database": None,
    }
    with pytest.raises(ValueError, match="--confirm-database"):
        seeder.validate_cli_options("url_shortener_bench", **options)

    options["confirm_database"] = "other_bench"
    with pytest.raises(ValueError, match="--confirm-database"):
        seeder.validate_cli_options("url_shortener_bench", **options)

    options["confirm_database"] = "url_shortener_bench"
    seeder.validate_cli_options("url_shortener_bench", **options)


def test_large_count_requires_explicit_allow_flag() -> None:
    with pytest.raises(ValueError, match="--allow-large"):
        seeder.validate_cli_options(
            "url_shortener_bench",
            count=2_000_001,
            batch_size=10,
            sample_count=0,
            allow_large=False,
            reset=False,
            confirm_database=None,
        )
    seeder.validate_cli_options(
        "url_shortener_bench",
        count=2_000_001,
        batch_size=10,
        sample_count=0,
        allow_large=True,
        reset=False,
        confirm_database=None,
    )


def test_cli_never_reads_database_url_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("BENCH_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://main:secret@localhost/url_shortener"
    )

    result = seeder.main([])

    output = capsys.readouterr()
    assert result == 2
    assert "BENCH_DATABASE_URL" in output.err
    assert "secret" not in output.err
    assert "postgresql://" not in output.err


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.query = ""
        self.result: tuple[Any, ...] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str) -> "FakeCursor":
        self.query = query
        self.connection.queries.append(query)
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.result

    def copy(self, query: str) -> "FakeCopy":
        self.connection.queries.append(query)
        return FakeCopy(self.connection)


class FakeCopy:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeCopy":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def write_row(self, row: seeder.SeedRow) -> None:
        self.connection.rows.append(row)


class FakeTransaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, *, has_rows: bool = False) -> None:
        self.has_rows = has_rows
        self.rows: list[seeder.SeedRow] = []
        self.queries: list[str] = []
        self.transaction_calls = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        cursor.result = (self.has_rows,)
        return cursor

    def execute(self, query: str) -> FakeCursor:
        cursor = FakeCursor(self)
        cursor.execute(query)
        if "SELECT EXISTS" in query:
            cursor.result = (self.has_rows,)
        elif "pg_relation_size" in query:
            cursor.result = (100, 25, 130)
        return cursor

    def transaction(self) -> FakeTransaction:
        self.transaction_calls += 1
        return FakeTransaction()


def test_seed_links_uses_one_transaction_per_copy_batch() -> None:
    connection = FakeConnection()

    inserted = seeder.seed_links(
        cast(psycopg.Connection[Any], connection), count=5, seed=3, batch_size=2
    )

    assert inserted == 5
    assert len(connection.rows) == 5
    assert connection.transaction_calls == 3


def test_nonempty_table_refusal_is_cli_only_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection(has_rows=True)
    monkeypatch.setattr(psycopg, "connect", lambda _url: connection)
    database_url = "postgresql://bench:secret-password@localhost/url_shortener_bench"

    result = seeder.main(["--database-url", database_url])

    output = capsys.readouterr()
    assert result == 1
    assert "not empty" in output.err
    assert "secret-password" not in output.out + output.err + caplog.text
    assert "postgresql://" not in output.out + output.err + caplog.text
    assert not connection.rows


def test_confirmed_reset_truncates_links_before_copy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = FakeConnection(has_rows=True)
    monkeypatch.setattr(psycopg, "connect", lambda _url: connection)

    result = seeder.main(
        [
            "--database-url",
            "postgresql://bench:secret@localhost/url_shortener_bench",
            "--count",
            "1",
            "--reset",
            "--confirm-database",
            "url_shortener_bench",
        ]
    )

    output = capsys.readouterr()
    assert result == 0
    assert "TRUNCATE links" in connection.queries
    assert len(connection.rows) == 1
    assert "secret" not in output.out + output.err


def test_cli_refuses_unsafe_database_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connect_calls = 0

    def connect(_url: str) -> FakeConnection:
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection()

    monkeypatch.setattr(psycopg, "connect", connect)
    result = seeder.main(
        ["--database-url", "postgresql://bench:password@localhost/main_db"]
    )

    output = capsys.readouterr()
    assert result == 2
    assert connect_calls == 0
    assert "main_db" not in output.out + output.err
    assert "password" not in output.out + output.err


def test_cli_reports_only_safe_metadata_and_writes_code_sample(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    connection = FakeConnection()
    database_url = "postgresql://bench:secret-password@localhost/url_shortener_bench"
    monkeypatch.setattr(psycopg, "connect", lambda _url: connection)
    sample_path = tmp_path / "codes.txt"

    result = seeder.main(
        [
            "--database-url",
            database_url,
            "--count",
            "3",
            "--batch-size",
            "2",
            "--sample-codes-file",
            str(sample_path),
            "--sample-codes-count",
            "2",
        ]
    )

    output = capsys.readouterr()
    combined_output = output.out + output.err + caplog.text
    assert result == 0
    assert "Database: url_shortener_bench" in output.out
    assert "Rows inserted: 3" in output.out
    assert "Index bytes per row" in output.out
    assert "Other-overhead bytes per row" in output.out
    assert "10M index projection" in output.out
    assert "10M other-overhead projection" in output.out
    assert "secret-password" not in combined_output
    assert "postgresql://" not in combined_output
    assert "https://bench.example" not in combined_output
    assert len(sample_path.read_text(encoding="ascii").splitlines()) == 2
