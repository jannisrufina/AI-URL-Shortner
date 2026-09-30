import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from url_shortener.rate_limiter import (
    CreateRateLimitExceeded,
    SlidingWindowRateLimiter,
    create_rate_limit_exception_handler,
    enforce_create_rate_limit,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _test_app(limiter: SlidingWindowRateLimiter) -> FastAPI:
    app = FastAPI()
    app.state.create_rate_limiter = limiter
    app.add_exception_handler(
        CreateRateLimitExceeded, create_rate_limit_exception_handler
    )
    return app


def _request(
    app: FastAPI,
    client_host: str | None = "192.0.2.10",
    headers: dict[str, str] | None = None,
) -> Request:
    encoded_headers = [
        (name.lower().encode("ascii"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/links",
            "raw_path": b"/api/links",
            "query_string": b"url=https%3A%2F%2Fsecret.example%2Fprivate",
            "headers": encoded_headers,
            "client": None if client_host is None else (client_host, 50000),
            "server": ("testserver", 80),
            "app": app,
        }
    )


def _allowed(request: Request) -> bool:
    try:
        asyncio.run(enforce_create_rate_limit(request))
    except CreateRateLimitExceeded:
        return False
    return True


def test_admits_ten_requests_and_returns_standard_429_body() -> None:
    app = _test_app(SlidingWindowRateLimiter())
    request = _request(app)

    for _ in range(10):
        assert _allowed(request)

    with pytest.raises(CreateRateLimitExceeded) as caught:
        asyncio.run(enforce_create_rate_limit(request))

    response = asyncio.run(create_rate_limit_exception_handler(request, caught.value))
    rendered_body = bytes(response.body).decode("utf-8")
    body = json.loads(rendered_body)
    assert response.status_code == 429
    assert body == {
        "error": {
            "code": "rate_limited",
            "message": "Create rate limit exceeded. Try again later.",
        }
    }
    assert "192.0.2.10" not in rendered_body
    assert "secret.example" not in rendered_body
    assert "https://" not in rendered_body


def test_window_slides_at_sixty_seconds() -> None:
    clock = FakeClock()
    app = _test_app(SlidingWindowRateLimiter(clock=clock))
    request = _request(app)

    for _ in range(10):
        assert _allowed(request)

    clock.now = 59.0
    assert not _allowed(request)
    clock.now = 60.0
    assert _allowed(request)


def test_rejected_requests_do_not_extend_the_window() -> None:
    clock = FakeClock()
    app = _test_app(SlidingWindowRateLimiter(clock=clock))
    request = _request(app)

    for _ in range(10):
        assert _allowed(request)

    clock.now = 30.0
    for _ in range(10):
        assert not _allowed(request)

    clock.now = 60.0
    assert _allowed(request)


def test_each_direct_peer_ip_has_an_independent_bucket() -> None:
    app = _test_app(SlidingWindowRateLimiter())
    first_peer = _request(app, client_host="192.0.2.10")
    second_peer = _request(app, client_host="192.0.2.11")

    for _ in range(10):
        assert _allowed(first_peer)
    assert not _allowed(first_peer)
    assert _allowed(second_peer)


def test_forwarded_headers_do_not_change_the_rate_limit_key() -> None:
    app = _test_app(SlidingWindowRateLimiter())
    for index in range(10):
        request = _request(
            app,
            headers={
                "X-Forwarded-For": f"198.51.100.{index + 1}",
                "Forwarded": f"for=198.51.100.{index + 1}",
                "X-Real-IP": f"203.0.113.{index + 1}",
            },
        )
        assert _allowed(request)

    spoofed_request = _request(
        app,
        headers={
            "X-Forwarded-For": "203.0.113.250",
            "Forwarded": "for=203.0.113.250",
            "X-Real-IP": "198.51.100.250",
        },
    )
    assert not _allowed(spoofed_request)


def test_periodic_cleanup_removes_idle_ips_and_keeps_active_ips() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock=clock, cleanup_interval=1)
    app = _test_app(limiter)

    assert _allowed(_request(app, client_host="192.0.2.20"))
    assert _allowed(_request(app, client_host="192.0.2.21"))
    assert len(limiter._requests_by_ip) == 2

    clock.now = 60.0
    assert _allowed(_request(app, client_host="192.0.2.21"))

    assert "192.0.2.20" not in limiter._requests_by_ip
    assert "192.0.2.21" in limiter._requests_by_ip


def test_missing_client_is_handled_with_shared_unknown_bucket() -> None:
    app = _test_app(SlidingWindowRateLimiter())
    request = _request(app, client_host=None)

    for _ in range(10):
        assert _allowed(request)
    assert not _allowed(request)


def test_concurrent_requests_admit_exactly_ten_for_one_ip() -> None:
    limiter = SlidingWindowRateLimiter()
    request_count = 100
    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(limiter.allow, ["192.0.2.30"] * request_count))

    assert sum(results) == 10
