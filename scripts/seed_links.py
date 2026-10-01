"""Deterministically bulk-load synthetic links into a dedicated benchmark DB."""

import argparse
import hashlib
import itertools
import os
import random
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

BASE62_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
CODE_SPACE = 62**7
CODE_MULTIPLIER = 37
DEFAULT_COUNT = 10_000
DEFAULT_BATCH_SIZE = 50_000
DEFAULT_SAMPLE_COUNT = 10_000
LARGE_COUNT_LIMIT = 2_000_000
TEN_MILLION_ROWS = 10_000_000
FIVE_GB_BYTES = 5_000_000_000
_CREATED_AT_ANCHOR = datetime(2026, 9, 1, tzinfo=UTC)
_CREATED_AT_PERIOD_SECONDS = 24 * 60 * 60
_MIN_EXPIRY_SECONDS = 30 * 24 * 60 * 60
_MAX_EXPIRY_SECONDS = 365 * 24 * 60 * 60
_COPY_LINKS_SQL = """
COPY links (code, url_digest, original_url, expires_at, created_at)
FROM STDIN
"""

SeedRow = tuple[str, bytes, str, datetime | None, datetime]


class SeederRefusedError(RuntimeError):
    """Raised for a safe operational refusal with a fixed message."""


@dataclass(frozen=True, slots=True)
class StorageMetrics:
    table_bytes: int
    index_bytes: int
    total_bytes: int


def code_for_index(index: int) -> str:
    # 37 is odd and not divisible by 31, so gcd(37, 62**7) is 1 and
    # multiplication modulo the code space permutes every possible code.
    value = (index * CODE_MULTIPLIER) % CODE_SPACE
    digits: list[str] = []
    for _ in range(7):
        value, digit = divmod(value, len(BASE62_ALPHABET))
        digits.append(BASE62_ALPHABET[digit])
    return "".join(reversed(digits))


def iter_rows(count: int, seed: int) -> Iterator[SeedRow]:
    generator = random.Random(seed)
    created_at = _CREATED_AT_ANCHOR + timedelta(
        seconds=seed % _CREATED_AT_PERIOD_SECONDS
    )

    for index in range(count):
        original_url = f"https://bench.example/links/{index:010d}"
        url_digest = hashlib.sha256(original_url.encode("utf-8")).digest()
        expires_at = None
        if generator.randrange(20) == 0:
            expires_at = created_at + timedelta(
                seconds=generator.randint(_MIN_EXPIRY_SECONDS, _MAX_EXPIRY_SECONDS)
            )
        yield (
            code_for_index(index),
            url_digest,
            original_url,
            expires_at,
            created_at,
        )


def seed_links(
    connection: psycopg.Connection[Any],
    count: int,
    seed: int,
    batch_size: int,
) -> int:
    rows = iter_rows(count, seed)
    inserted = 0
    while batch := list(itertools.islice(rows, batch_size)):
        with (
            connection.transaction(),
            connection.cursor() as cursor,
            cursor.copy(_COPY_LINKS_SQL) as copy,
        ):
            for row in batch:
                copy.write_row(row)
        inserted += len(batch)
    return inserted


def write_sample_codes(path: Path, count: int, sample_count: int) -> int:
    sample_size = min(count, sample_count)
    with path.open("w", encoding="ascii", newline="\n") as sample_file:
        for sample_index in range(sample_size):
            index = sample_index * count // sample_size
            sample_file.write(f"{code_for_index(index)}\n")
    return sample_size


def database_name(database_url: str) -> str:
    parsed = conninfo_to_dict(database_url)
    name = parsed.get("dbname") or parsed.get("database")
    if not isinstance(name, str) or not name:
        raise ValueError("The benchmark connection must specify a database name.")
    return name


def validate_cli_options(
    target_name: str,
    *,
    count: int,
    batch_size: int,
    sample_count: int,
    allow_large: bool,
    reset: bool,
    confirm_database: str | None,
) -> None:
    if not target_name.endswith("_bench"):
        raise ValueError("The target database name must end in '_bench'.")
    if count < 0:
        raise ValueError("Count must not be negative.")
    if count > CODE_SPACE:
        raise ValueError("Count exceeds the available seven-character codes.")
    if count > LARGE_COUNT_LIMIT and not allow_large:
        raise ValueError("Counts above 2,000,000 require --allow-large.")
    if batch_size < 1:
        raise ValueError("Batch size must be positive.")
    if sample_count < 0:
        raise ValueError("Sample-code count must not be negative.")
    if reset and confirm_database != target_name:
        raise ValueError(
            "--reset requires --confirm-database matching the target name."
        )
    if not reset and confirm_database is not None:
        raise ValueError("--confirm-database may only be used with --reset.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--database-url")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--allow-large", action="store_true")
    parser.add_argument("--sample-codes-file", type=Path)
    parser.add_argument("--sample-codes-count", type=int, default=DEFAULT_SAMPLE_COUNT)
    return parser


def _read_storage_metrics(connection: psycopg.Connection[Any]) -> StorageMetrics:
    row = connection.execute(
        """
        SELECT pg_relation_size('links'),
               pg_indexes_size('links'),
               pg_total_relation_size('links')
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL returned no storage metrics.")
    return StorageMetrics(table_bytes=row[0], index_bytes=row[1], total_bytes=row[2])


def _print_report(
    database: str,
    count: int,
    elapsed_seconds: float,
    metrics: StorageMetrics,
) -> None:
    rows_per_second = count / elapsed_seconds if elapsed_seconds > 0 else 0.0
    overhead_bytes = metrics.total_bytes - metrics.table_bytes - metrics.index_bytes
    if count:
        table_per_row = metrics.table_bytes / count
        index_per_row = metrics.index_bytes / count
        overhead_per_row = overhead_bytes / count
        total_per_row = metrics.total_bytes / count
        projected_table = round(table_per_row * TEN_MILLION_ROWS)
        projected_indexes = round(metrics.index_bytes / count * TEN_MILLION_ROWS)
        projected_overhead = round(overhead_bytes / count * TEN_MILLION_ROWS)
    else:
        table_per_row = 0.0
        index_per_row = 0.0
        overhead_per_row = 0.0
        total_per_row = 0.0
        projected_table = 0
        projected_indexes = 0
        projected_overhead = 0

    print(f"Database: {database}")
    print(f"Rows inserted: {count}")
    print(f"Elapsed: {elapsed_seconds:.3f} seconds")
    print(f"Rows per second: {rows_per_second:.1f}")
    print(f"Table size: {metrics.table_bytes} bytes")
    print(f"Index size: {metrics.index_bytes} bytes")
    print(f"Total size: {metrics.total_bytes} bytes")
    print(f"Table bytes per row: {table_per_row:.2f}")
    print(f"Index bytes per row: {index_per_row:.2f}")
    print(f"Other-overhead bytes per row: {overhead_per_row:.2f}")
    print(f"Total bytes per row: {total_per_row:.2f}")
    print(f"10M table projection: {projected_table} bytes")
    print(f"10M index projection: {projected_indexes} bytes")
    print(f"10M other-overhead projection: {projected_overhead} bytes")
    print(f"A-8 record-only estimate: {FIVE_GB_BYTES} bytes")
    print(f"10M table projection vs A-8: {projected_table - FIVE_GB_BYTES} bytes")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    target_url = args.database_url or os.environ.get("BENCH_DATABASE_URL")
    if not target_url:
        print("Set BENCH_DATABASE_URL or pass --database-url.", file=sys.stderr)
        return 2

    try:
        target_name = database_name(target_url)
        validate_cli_options(
            target_name,
            count=args.count,
            batch_size=args.batch_size,
            sample_count=args.sample_codes_count,
            allow_large=args.allow_large,
            reset=args.reset,
            confirm_database=args.confirm_database,
        )
    except (psycopg.Error, ValueError) as error:
        print(
            f"Invalid seeder configuration ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2

    try:
        with psycopg.connect(target_url) as connection:
            if args.reset:
                with connection.transaction():
                    connection.execute("TRUNCATE link_clicks")
                    connection.execute("TRUNCATE links")
            else:
                exists = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM links LIMIT 1)"
                ).fetchone()
                if exists is not None and exists[0]:
                    raise SeederRefusedError(
                        "The links table is not empty; use --reset."
                    )

            start = time.perf_counter()
            inserted = seed_links(connection, args.count, args.seed, args.batch_size)
            elapsed = time.perf_counter() - start
            with connection.transaction():
                connection.execute("ANALYZE links")
            metrics = _read_storage_metrics(connection)

        if args.sample_codes_file is not None:
            write_sample_codes(
                args.sample_codes_file, inserted, args.sample_codes_count
            )
    except SeederRefusedError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Seeding failed ({type(error).__name__}).", file=sys.stderr)
        return 1

    _print_report(target_name, inserted, elapsed, metrics)
    if args.sample_codes_file is not None:
        print(f"Sample codes written: {min(inserted, args.sample_codes_count)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
