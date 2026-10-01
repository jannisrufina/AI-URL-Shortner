"""Open-loop local HTTP load profiles for the URL shortener."""

import argparse
import asyncio
import ipaddress
import json
import math
import os
import platform
import re
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import psycopg

MAX_RATE = 5_000
MAX_TOTAL_REQUESTS = 300_000
MAX_CREATES_PER_SOURCE = 9
CREATE_WINDOW_SECONDS = 60.0
MIXED_REDIRECTS_PER_CREATE = 100
DEFAULT_SOURCE_ADDRESSES = ("127.0.0.1",)
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{7}$")
_PROFILE_DEFAULTS: dict[str, tuple[float, float]] = {
    "redirect-average": (100.0, 60.0),
    "redirect-peak": (1_000.0, 30.0),
    "create": (1.0, 240.0),
    "mixed": (100.0, 60.0),
    "overload": (100.0, 60.0),
}


@dataclass(frozen=True, slots=True)
class ScheduledRequest:
    offset_seconds: float
    method: Literal["GET", "POST"]
    path: str
    source_address: str
    expected_status: int
    kind: Literal["redirect", "create"]


@dataclass(frozen=True, slots=True)
class RequestResult:
    status: int | str
    latency_ms: float | None
    generator_lag_ms: float
    error: bool


def percentile(values: Sequence[float], percentile_value: float) -> float | None:
    if not values:
        return None
    if not 0 <= percentile_value <= 100:
        raise ValueError("Percentile must be between 0 and 100.")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_value / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values) if values else None,
    }


def timing_metrics(
    scheduled_at: float, actual_send_at: float, completed_at: float
) -> tuple[float, float]:
    return (
        max(0.0, (completed_at - scheduled_at) * 1_000),
        max(0.0, (actual_send_at - scheduled_at) * 1_000),
    )


def validate_target_url(base_url: str, allow_remote: bool = False) -> str:
    parsed = urlsplit(base_url)
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("Target URL is malformed.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Target must be an HTTP(S) origin without credentials, path, or query."
        )

    normalized_host = host.casefold().rstrip(".")
    try:
        parsed_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        parsed_address = None
    is_loopback = (
        normalized_host == "localhost"
        or parsed_address == ipaddress.IPv4Address("127.0.0.1")
    )
    if not is_loopback and not allow_remote:
        raise ValueError("Remote targets require --allow-remote.")

    safe_netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit((parsed.scheme, safe_netloc, "", "", ""))


def validate_source_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    if not addresses:
        raise ValueError("At least one source address is required.")
    normalized: list[str] = []
    for address in addresses:
        try:
            normalized.append(str(ipaddress.ip_address(address.strip())))
        except ValueError as error:
            raise ValueError("Source addresses must be IP address literals.") from error
    if len(normalized) != len(set(normalized)):
        raise ValueError("Source addresses must be unique.")
    return tuple(normalized)


def effective_rate(profile: str, requested_rate: float, source_count: int) -> float:
    if requested_rate <= 0:
        raise ValueError("Rate must be positive.")
    if requested_rate > MAX_RATE:
        raise ValueError("Rates above 5,000 requests per second are refused.")
    if source_count < 1:
        raise ValueError("At least one source address is required.")

    create_capacity = MAX_CREATES_PER_SOURCE * source_count / CREATE_WINDOW_SECONDS
    if profile == "create":
        return min(requested_rate, create_capacity)
    if profile == "mixed":
        return min(requested_rate, create_capacity * (MIXED_REDIRECTS_PER_CREATE + 1))
    return requested_rate


def assign_create_sources(
    scheduled_times: Sequence[float], source_addresses: Sequence[str]
) -> list[str | None]:
    """Assign create arrivals without exceeding 9 attempts per source per window."""
    requests_by_source = {address: deque[float]() for address in source_addresses}
    assignments: list[str | None] = []
    next_address_index = 0

    for scheduled_at in scheduled_times:
        selected: str | None = None
        for offset in range(len(source_addresses)):
            address_index = (next_address_index + offset) % len(source_addresses)
            address = source_addresses[address_index]
            timestamps = requests_by_source[address]
            while timestamps and timestamps[0] <= scheduled_at - CREATE_WINDOW_SECONDS:
                timestamps.popleft()
            if len(timestamps) < MAX_CREATES_PER_SOURCE:
                timestamps.append(scheduled_at)
                selected = address
                next_address_index = (address_index + 1) % len(source_addresses)
                break
        assignments.append(selected)
    return assignments


def build_schedule(
    profile: str,
    rate: float,
    duration_seconds: float,
    codes: Sequence[str],
    source_addresses: Sequence[str],
) -> tuple[list[ScheduledRequest], float, float]:
    if profile not in _PROFILE_DEFAULTS:
        raise ValueError("Unknown load profile.")
    if duration_seconds <= 0:
        raise ValueError("Duration must be positive.")
    if not codes and profile in {
        "redirect-average",
        "redirect-peak",
        "mixed",
        "overload",
    }:
        raise ValueError("Redirect profiles require a sample-codes file.")
    if any(_CODE_PATTERN.fullmatch(code) is None for code in codes):
        raise ValueError("Sample-codes file contains a malformed code.")

    applied_rate = effective_rate(profile, rate, len(source_addresses))
    request_count = math.ceil(applied_rate * duration_seconds)
    if request_count > MAX_TOTAL_REQUESTS:
        raise ValueError("This run exceeds the 300,000-request safety cap.")

    kinds: list[Literal["redirect", "create"]] = []
    for index in range(request_count):
        if profile == "create" or (
            profile == "mixed"
            and index % (MIXED_REDIRECTS_PER_CREATE + 1) == MIXED_REDIRECTS_PER_CREATE
        ):
            kinds.append("create")
        else:
            kinds.append("redirect")

    create_offsets = [
        index / applied_rate for index, kind in enumerate(kinds) if kind == "create"
    ]
    assigned_create_sources = iter(
        assign_create_sources(create_offsets, source_addresses)
    )
    requests: list[ScheduledRequest] = []
    for index, kind in enumerate(kinds):
        source = (
            next(assigned_create_sources)
            if kind == "create"
            else source_addresses[index % len(source_addresses)]
        )
        if source is None:
            raise ValueError("The planned creates exceed the per-source limiter.")
        if kind == "create":
            method: Literal["GET", "POST"] = "POST"
            path = "/api/links"
            expected_status = 200
        else:
            method = "GET"
            path = f"/{codes[index % len(codes)]}"
            expected_status = 302
        requests.append(
            ScheduledRequest(
                offset_seconds=index / applied_rate,
                method=method,
                path=path,
                source_address=source,
                expected_status=expected_status,
                kind=kind,
            )
        )
    return requests, applied_rate, rate


def status_distribution(results: Sequence[RequestResult]) -> dict[str, int]:
    return dict(sorted(Counter(str(result.status) for result in results).items()))


def error_count(results: Sequence[RequestResult]) -> int:
    return sum(result.error for result in results)


def machine_info() -> dict[str, str | int | None]:
    return {
        "os_name": platform.system(),
        "os_version": platform.version(),
        "cpu_model": platform.processor() or None,
        "logical_cores": os.cpu_count(),
        "python_version": platform.python_version(),
    }


def create_report(
    *,
    profile: str,
    target_base_url: str,
    dataset_rows: int,
    requested_rate: float,
    offered_rate: float,
    duration_seconds: float,
    results: Sequence[RequestResult],
    source_count: int,
    pool_max: int,
    pool_wait_ms: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    latencies = [
        result.latency_ms for result in results if result.latency_ms is not None
    ]
    lags = [result.generator_lag_ms for result in results]
    interval_ms = 1_000 / offered_rate if offered_rate > 0 else 0.0
    generator_limited = bool(lags) and max(lags) >= interval_ms
    latency_by_status: dict[str, dict[str, float | None]] = {}
    for status in sorted({str(result.status) for result in results}):
        latency_by_status[status] = latency_summary(
            [
                result.latency_ms
                for result in results
                if str(result.status) == status and result.latency_ms is not None
            ]
        )

    profile_latency = latency_summary(latencies)
    p99 = profile_latency["p99_ms"]
    failures = error_count(results)
    all_expected = all(not result.error for result in results)
    verdict = "REPORTED_ONLY"
    target_ms: int | None = None
    if profile == "redirect-average":
        target_ms = 200
        verdict = (
            "PASS"
            if p99 is not None
            and p99 < target_ms
            and all_expected
            and not generator_limited
            else "FAIL"
        )
    elif profile == "create":
        target_ms = 300
        verdict = (
            "PASS"
            if p99 is not None
            and p99 < target_ms
            and all_expected
            and not generator_limited
            else "FAIL"
        )

    weak_sample = profile in {"create", "mixed"} and source_count == 1
    return {
        "date_utc": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "profile": profile,
        "machine": machine_info(),
        "target_base_url": target_base_url,
        "dataset_rows": dataset_rows,
        "requested_rate_per_second": requested_rate,
        "offered_rate_per_second": offered_rate,
        "achieved_rate_per_second": len(results) / duration_seconds,
        "duration_seconds": duration_seconds,
        "scheduled_request_count": math.ceil(offered_rate * duration_seconds),
        "request_count": len(results),
        "status_distribution": status_distribution(results),
        "error_count": failures,
        "latency_ms": profile_latency,
        "latency_by_status_ms": latency_by_status,
        "generator_lag_ms": latency_summary(lags),
        "generator_limited": generator_limited,
        "generator_limited_rule": "max lag >= one arrival interval",
        "statistically_weak": weak_sample,
        "statistical_note": (
            "Single source limits the create sample; p99 is statistically weak."
            if weak_sample
            else None
        ),
        "pool_settings": {"pool_max": pool_max, "pool_wait_ms": pool_wait_ms},
        "verdict": verdict,
        "target_p99_ms": target_ms,
        "target_note": (
            "NFR-3 pass/fail uses redirect-average only."
            if profile in {"redirect-peak", "overload"}
            else None
        ),
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    machine = report["machine"]
    latency = report["latency_ms"]
    lag = report["generator_lag_ms"]
    lines = [
        f"# Load test: {report['profile']}",
        "",
        f"- Date (UTC): {report['date_utc']}",
        f"- Target base URL: {report['target_base_url']}",
        f"- Dataset rows: {report['dataset_rows']}",
        f"- Offered rate: {report['offered_rate_per_second']:.3f} requests/second",
        f"- Requested rate: {report['requested_rate_per_second']:.3f} requests/second",
        f"- Achieved rate: {report['achieved_rate_per_second']:.3f} requests/second",
        f"- Duration: {report['duration_seconds']:.3f} seconds",
        f"- Scheduled requests: {report['scheduled_request_count']}",
        f"- Requests sent: {report['request_count']}",
        f"- Error count: {report['error_count']}",
        "- Status distribution: "
        f"`{json.dumps(report['status_distribution'], sort_keys=True)}`",
        f"- Latency p50/p95/p99/max: "
        f"{latency['p50_ms']}/{latency['p95_ms']}/"
        f"{latency['p99_ms']}/{latency['max_ms']} ms",
        f"- Generator lag p50/p95/p99/max: "
        f"{lag['p50_ms']}/{lag['p95_ms']}/{lag['p99_ms']}/{lag['max_ms']} ms",
        f"- Generator-limited: {report['generator_limited']}",
        f"- Pool settings: max={report['pool_settings']['pool_max']}, "
        f"wait={report['pool_settings']['pool_wait_ms']} ms",
        f"- Verdict: {report['verdict']}",
        "",
        "## Latency by status",
        "",
    ]
    for status, summary in report["latency_by_status_ms"].items():
        lines.append(
            f"- {status}: p50/p95/p99/max "
            f"{summary['p50_ms']}/{summary['p95_ms']}/"
            f"{summary['p99_ms']}/{summary['max_ms']} ms"
        )
    if report["statistical_note"]:
        lines.extend(("", f"Statistical note: {report['statistical_note']}"))
    if report["target_note"]:
        lines.extend(("", report["target_note"]))
    lines.extend(
        (
            "",
            "## Machine",
            "",
            f"- OS: {machine['os_name']} {machine['os_version']}",
            f"- CPU: {machine['cpu_model'] or 'unavailable'}",
            f"- Logical cores: {machine['logical_cores']}",
            f"- Python: {machine['python_version']}",
        )
    )
    return "\n".join(lines) + "\n"


async def execute_schedule(
    schedule: Sequence[ScheduledRequest],
    base_url: str,
    *,
    transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleeper: Callable[[float], Any] = asyncio.sleep,
    timeout_seconds: float = 5.0,
) -> list[RequestResult]:
    addresses = sorted({request.source_address for request in schedule})
    clients: dict[str, httpx.AsyncClient] = {}
    for address in addresses:
        transport = (
            httpx.AsyncHTTPTransport(local_address=address, retries=0)
            if transport_factory is None
            else transport_factory(address)
        )
        clients[address] = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=10_000, max_keepalive_connections=1_000
            ),
        )

    run_started_at = clock()

    async def execute_one(request: ScheduledRequest) -> RequestResult:
        scheduled_at = run_started_at + request.offset_seconds
        wait_seconds = scheduled_at - clock()
        if wait_seconds > 0:
            await sleeper(wait_seconds)
        create_payload = (
            {"url": f"https://loadtest.example/{uuid4().hex}"}
            if request.kind == "create"
            else None
        )
        actual_send_at = clock()
        client = clients[request.source_address]
        try:
            if request.kind == "create":
                response = await client.post(
                    request.path,
                    json=create_payload,
                )
            else:
                response = await client.get(request.path)
            completed_at = clock()
            latency, lag = timing_metrics(scheduled_at, actual_send_at, completed_at)
            return RequestResult(
                status=response.status_code,
                latency_ms=latency,
                generator_lag_ms=lag,
                error=response.status_code != request.expected_status,
            )
        except httpx.TimeoutException:
            completed_at = clock()
            latency, lag = timing_metrics(scheduled_at, actual_send_at, completed_at)
            return RequestResult("timeout", latency, lag, True)
        except httpx.HTTPError:
            completed_at = clock()
            latency, lag = timing_metrics(scheduled_at, actual_send_at, completed_at)
            return RequestResult("http_error", latency, lag, True)

    try:
        return list(
            await asyncio.gather(*(execute_one(request) for request in schedule))
        )
    finally:
        await asyncio.gather(*(client.aclose() for client in clients.values()))


def read_sample_codes(path: Path) -> list[str]:
    with path.open(encoding="ascii") as sample_file:
        codes = [line.strip() for line in sample_file if line.strip()]
    if not codes or any(_CODE_PATTERN.fullmatch(code) is None for code in codes):
        raise ValueError("Sample-codes file is empty or malformed.")
    return codes


def count_dataset_rows(database_url: str) -> int:
    with psycopg.connect(database_url) as connection:
        row = connection.execute("SELECT count(*) FROM links").fetchone()
    if row is None:
        raise RuntimeError("Database returned no dataset count.")
    return int(row[0])


def write_report_files(
    report: dict[str, Any], output_directory: Path
) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{report['profile']}_{timestamp}"
    json_path = output_directory / f"{stem}.json"
    markdown_path = output_directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return json_path, markdown_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(_PROFILE_DEFAULTS), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--sample-codes-file", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--dataset-rows", type=int)
    parser.add_argument("--source-addresses", default="127.0.0.1")
    parser.add_argument("--rate", type=float)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--pool-max", type=int, default=10)
    parser.add_argument("--pool-wait-ms", type=int, default=250)
    parser.add_argument("--allow-remote", action="store_true")
    return parser


async def _run_profile(
    *,
    profile: str,
    target_base_url: str,
    rate: float,
    duration_seconds: float,
    codes: Sequence[str],
    source_addresses: Sequence[str],
    dataset_rows: int,
    pool_max: int,
    pool_wait_ms: int,
    transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
) -> dict[str, Any]:
    schedule, offered_rate, requested_rate = build_schedule(
        profile, rate, duration_seconds, codes, source_addresses
    )
    results = await execute_schedule(
        schedule, target_base_url, transport_factory=transport_factory
    )
    return create_report(
        profile=profile,
        target_base_url=target_base_url,
        dataset_rows=dataset_rows,
        requested_rate=requested_rate,
        offered_rate=offered_rate,
        duration_seconds=duration_seconds,
        results=results,
        source_count=len(source_addresses),
        pool_max=pool_max,
        pool_wait_ms=pool_wait_ms,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        target_base_url = validate_target_url(args.base_url, args.allow_remote)
        source_addresses = validate_source_addresses(args.source_addresses.split(","))
        default_rate, default_duration = _PROFILE_DEFAULTS[args.profile]
        rate = default_rate if args.rate is None else args.rate
        duration = default_duration if args.duration is None else args.duration
        if rate > MAX_RATE:
            raise ValueError("Rates above 5,000 requests per second are refused.")
        if duration <= 0:
            raise ValueError("Duration must be positive.")
        if args.profile == "create" and duration < 240:
            raise ValueError(
                "The create profile duration must be at least 240 seconds."
            )
        if args.pool_max < 1 or args.pool_wait_ms < 1:
            raise ValueError("Pool settings must be positive.")
        codes = (
            read_sample_codes(args.sample_codes_file)
            if args.sample_codes_file is not None
            else []
        )
        if (
            args.profile in {"redirect-average", "redirect-peak", "mixed", "overload"}
            and not codes
        ):
            raise ValueError("This profile requires --sample-codes-file.")
        if args.database_url:
            dataset_rows = count_dataset_rows(args.database_url)
        elif args.dataset_rows is not None and args.dataset_rows >= 0:
            dataset_rows = args.dataset_rows
        else:
            raise ValueError("Pass --database-url or a non-negative --dataset-rows.")
        applied_rate = effective_rate(args.profile, rate, len(source_addresses))
        if math.ceil(applied_rate * duration) > MAX_TOTAL_REQUESTS:
            raise ValueError("This run exceeds the 300,000-request safety cap.")
    except (OSError, psycopg.Error, ValueError) as error:
        print(
            f"Invalid load-test configuration ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(
        _run_profile(
            profile=args.profile,
            target_base_url=target_base_url,
            rate=rate,
            duration_seconds=duration,
            codes=codes,
            source_addresses=source_addresses,
            dataset_rows=dataset_rows,
            pool_max=args.pool_max,
            pool_wait_ms=args.pool_wait_ms,
        )
    )
    write_report_files(report, Path("bench-results"))
    print(render_markdown_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
