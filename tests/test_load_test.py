import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from starlette.types import Receive, Scope, Send

import scripts.load_test as load_test


def test_percentiles_cover_known_values_and_empty_input() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert load_test.percentile(values, 50) == 3.0
    assert load_test.percentile(values, 95) == 5.0
    assert load_test.percentile([], 99) is None
    assert load_test.latency_summary([]) == {
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
    }


def test_p99_of_one_hundred_uses_nearest_rank() -> None:
    assert load_test.percentile(list(range(1, 101)), 99) == 99


def test_open_loop_schedule_count_spacing_and_scheduled_time_latency() -> None:
    schedule, offered_rate, requested_rate = load_test.build_schedule(
        "redirect-average",
        rate=4,
        duration_seconds=1,
        codes=["AbC1234"],
        source_addresses=["127.0.0.1"],
    )

    assert offered_rate == requested_rate == 4
    assert len(schedule) == 4
    assert [request.offset_seconds for request in schedule] == [0, 0.25, 0.5, 0.75]

    latency_ms, generator_lag_ms = load_test.timing_metrics(
        scheduled_at=12.0,
        actual_send_at=12.025,
        completed_at=12.1,
    )
    assert latency_ms == pytest.approx(100)
    assert generator_lag_ms == pytest.approx(25)


def test_create_source_assignment_never_exceeds_nine_in_a_rolling_minute() -> None:
    addresses = ["127.0.0.2", "127.0.0.3"]
    scheduled_times = [index * 60 / 17 for index in range(50)]
    assigned = load_test.assign_create_sources(scheduled_times, addresses)

    for address in addresses:
        times = [
            scheduled_time
            for scheduled_time, assigned_address in zip(
                scheduled_times, assigned, strict=True
            )
            if assigned_address == address
        ]
        for window_end in times:
            count = sum(
                window_end - load_test.CREATE_WINDOW_SECONDS < value <= window_end
                for value in times
            )
            assert count <= load_test.MAX_CREATES_PER_SOURCE
    assert all(address is not None for address in assigned)


def test_create_rate_is_reduced_for_one_source_and_small_sample_is_marked() -> None:
    schedule, offered_rate, _ = load_test.build_schedule(
        "create", 1.0, 240.0, [], ["127.0.0.1"]
    )
    report = load_test.create_report(
        profile="create",
        target_base_url="http://127.0.0.1:8000",
        dataset_rows=1_000,
        requested_rate=1.0,
        offered_rate=offered_rate,
        duration_seconds=240.0,
        results=[],
        source_count=1,
        pool_max=10,
        pool_wait_ms=250,
    )

    assert len(schedule) == 36
    assert offered_rate == pytest.approx(0.15)
    assert report["statistically_weak"] is True
    assert "statistically weak" in report["statistical_note"]


def test_mixed_profile_maintains_one_create_per_hundred_redirects() -> None:
    addresses = [f"127.0.0.{index}" for index in range(1, 8)]
    schedule, offered_rate, _ = load_test.build_schedule(
        "mixed", 100, 1.01, ["AbC1234"], addresses
    )

    assert offered_rate == 100
    assert sum(request.kind == "redirect" for request in schedule) == 100
    assert sum(request.kind == "create" for request in schedule) == 1


def test_status_distribution_counts_redirects_limits_failures_and_timeouts() -> None:
    results = [
        load_test.RequestResult(302, 10.0, 0.0, False),
        load_test.RequestResult(302, 12.0, 0.1, False),
        load_test.RequestResult(429, 14.0, 0.0, True),
        load_test.RequestResult(503, 18.0, 0.2, True),
        load_test.RequestResult("timeout", 20.0, 0.0, True),
    ]

    assert load_test.status_distribution(results) == {
        "302": 2,
        "429": 1,
        "503": 1,
        "timeout": 1,
    }
    assert load_test.error_count(results) == 3


def test_report_has_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        load_test,
        "machine_info",
        lambda: {
            "os_name": "TestOS",
            "os_version": "1",
            "cpu_model": "Test CPU",
            "logical_cores": 4,
            "python_version": "3.11",
        },
    )
    results = [load_test.RequestResult(302, 12.5, 0.25, False)]
    report = load_test.create_report(
        profile="redirect-average",
        target_base_url="http://127.0.0.1:8000",
        dataset_rows=10_000,
        requested_rate=100,
        offered_rate=100,
        duration_seconds=60,
        results=results,
        source_count=1,
        pool_max=10,
        pool_wait_ms=250,
        now=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert {
        "date_utc",
        "machine",
        "target_base_url",
        "dataset_rows",
        "offered_rate_per_second",
        "achieved_rate_per_second",
        "duration_seconds",
        "request_count",
        "status_distribution",
        "error_count",
        "latency_ms",
        "generator_lag_ms",
        "pool_settings",
        "verdict",
    } <= report.keys()
    assert report["verdict"] == "PASS"
    assert report["status_distribution"] == {"302": 1}
    assert load_test.render_markdown_report(report).startswith(
        "# Load test: redirect-average"
    )


def test_target_and_rate_safety_refusals() -> None:
    with pytest.raises(ValueError, match="Remote targets"):
        load_test.validate_target_url("http://SENTINEL_HOSTNAME:8000")
    with pytest.raises(ValueError, match="5,000"):
        load_test.effective_rate("redirect-average", 5_001, 1)
    with pytest.raises(ValueError, match="5,000"):
        load_test.build_schedule(
            "redirect-average", 5_001, 1, ["AbC1234"], ["127.0.0.1"]
        )


def test_cli_requires_full_create_duration_without_starting_network_or_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = load_test.main(
        ["--profile", "create", "--duration", "2", "--dataset-rows", "10"]
    )

    output = capsys.readouterr()
    assert result == 2
    assert "configuration" in output.err
    assert "https://loadtest.example" not in output.out + output.err


def test_total_request_cap_is_enforced() -> None:
    with pytest.raises(ValueError, match="300,000-request safety cap"):
        load_test.build_schedule(
            "redirect-peak",
            rate=5_000,
            duration_seconds=61,
            codes=["AbC1234"],
            source_addresses=["127.0.0.1"],
        )


def test_two_second_smoke_run_uses_in_process_asgi_transport() -> None:
    async def fake_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await receive()
        await send({"type": "http.response.start", "status": 302, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    report = asyncio.run(
        load_test._run_profile(
            profile="redirect-average",
            target_base_url="http://127.0.0.1:8000",
            rate=2,
            duration_seconds=2,
            codes=["AbC1234"],
            source_addresses=["127.0.0.1"],
            dataset_rows=50,
            pool_max=10,
            pool_wait_ms=250,
            transport_factory=lambda _address: httpx.ASGITransport(app=fake_app),
        )
    )

    assert report["request_count"] == 4
    assert report["status_distribution"] == {"302": 4}
    assert report["latency_ms"]["p99_ms"] is not None
    assert report["dataset_rows"] == 50


@pytest.mark.parametrize(
    "target",
    [
        "http://user:SENTINEL_PASSWORD@127.0.0.1:8000",
        "http://127.0.0.1:8000/SENTINEL_PATH",
        "http://127.0.0.1:8000/?q=SENTINEL_FULL_URL",
    ],
)
def test_target_url_with_credentials_path_or_query_is_refused(target: str) -> None:
    with pytest.raises(ValueError) as caught:
        load_test.validate_target_url(target)
    assert "SENTINEL" not in str(caught.value)


def _report(
    profile: str, results: list[load_test.RequestResult], rate: float = 100
) -> dict[str, Any]:
    return load_test.create_report(
        profile=profile,
        target_base_url="http://127.0.0.1:8000",
        dataset_rows=1,
        requested_rate=rate,
        offered_rate=rate,
        duration_seconds=1.0,
        results=results,
        source_count=1,
        pool_max=10,
        pool_wait_ms=250,
    )


def test_redirect_average_verdict_fails_on_slow_p99() -> None:
    slow = [load_test.RequestResult(302, 250.0, 0.0, False)]
    assert _report("redirect-average", slow)["verdict"] == "FAIL"


def test_redirect_average_verdict_fails_on_unexpected_status() -> None:
    bad = [load_test.RequestResult(404, 5.0, 0.0, True)]
    assert _report("redirect-average", bad)["verdict"] == "FAIL"


def test_generator_limited_run_cannot_pass() -> None:
    laggy = [load_test.RequestResult(302, 5.0, 50.0, False)]
    report = _report("redirect-average", laggy)
    assert report["generator_limited"] is True
    assert report["verdict"] == "FAIL"


def test_peak_and_overload_never_carry_a_pass_fail_verdict() -> None:
    fast = [load_test.RequestResult(302, 5.0, 0.0, False)]
    assert _report("redirect-peak", fast, 1000)["verdict"] == "REPORTED_ONLY"
    assert _report("overload", fast)["verdict"] == "REPORTED_ONLY"


def test_harness_never_sends_forwarded_headers() -> None:
    seen: list[dict[str, str]] = []

    async def recording_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await receive()
        seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await send({"type": "http.response.start", "status": 302, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(
        load_test._run_profile(
            profile="redirect-average",
            target_base_url="http://127.0.0.1:8000",
            rate=2,
            duration_seconds=1,
            codes=["AbC1234"],
            source_addresses=["127.0.0.1"],
            dataset_rows=1,
            pool_max=10,
            pool_wait_ms=250,
            transport_factory=lambda _a: httpx.ASGITransport(app=recording_app),
        )
    )

    assert seen
    for headers in seen:
        assert "x-forwarded-for" not in headers
        assert "forwarded" not in headers
        assert "x-real-ip" not in headers
