import logging
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import url_shortener.app as app_module
from url_shortener.database import (
    DatabaseConnectionError,
    DatabasePoolTimeoutError,
    DatabaseStatementTimeoutError,
)
from url_shortener.links import Link
from url_shortener.settings import Settings


class FakeDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    public_base_url: str = "https://short.example/base/",
) -> TestClient:
    settings = Settings(
        database_url="postgresql://user:password@localhost/test_db",
        public_base_url=public_base_url,
    )
    monkeypatch.setattr(app_module, "Database", FakeDatabase)
    return TestClient(app_module.create_app(settings))


def _successful_create(
    _database: Any,
    submitted_url: str,
    expires_at: datetime | None,
    _created_at: datetime,
) -> Link:
    return Link("AbC1234", submitted_url, expires_at)


def test_new_and_reused_create_return_200_with_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _successful_create)
    with _client(monkeypatch) as client:
        for _ in range(2):
            response = client.post("/api/links", json={"url": "https://example.test/a"})
            assert response.status_code == 200
            assert response.json() == {
                "code": "AbC1234",
                "short_url": "https://short.example/base/AbC1234",
                "expires_at": None,
            }


def test_invalid_url_returns_validator_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post("/api/links", json={"url": "ftp://private-input.test"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_url"
    assert "ftp://private-input.test" not in response.text


@pytest.mark.parametrize(
    ("body", "raw", "secret"),
    [
        (
            None,
            b'{"url": "https://secret.example/path"',
            "https://secret.example/path",
        ),
        ({"other": "https://secret.example/path"}, None, "https://secret.example/path"),
        (
            {"url": 17, "other": "https://secret.example/path"},
            None,
            "https://secret.example/path",
        ),
    ],
)
def test_invalid_request_body_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object] | None,
    raw: bytes | None,
    secret: str | None,
) -> None:
    with _client(monkeypatch) as client:
        response = (
            client.post(
                "/api/links",
                content=raw,
                headers={"content-type": "application/json"},
            )
            if raw is not None
            else client.post("/api/links", json=body)
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    if secret is not None:
        assert secret not in response.text


def test_non_string_expiry_returns_invalid_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/links",
            json={"url": "https://example.test/", "expires_at": 1_798_747_200},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_expiry"


def test_configured_public_hostname_is_used_for_self_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/links", json={"url": "https://api.short.example/path"}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "self_reference"


def test_invalid_request_consumes_rate_limit_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _successful_create)
    with _client(monkeypatch) as client:
        for _ in range(9):
            assert (
                client.post(
                    "/api/links", json={"url": "https://example.test/"}
                ).status_code
                == 200
            )
        assert (
            client.post("/api/links", json={"url": "ftp://invalid.test"}).status_code
            == 422
        )
        limited = client.post("/api/links", json={"url": "https://example.test/"})

    assert limited.status_code == 429
    assert limited.json() == {
        "error": {
            "code": "rate_limited",
            "message": "Create rate limit exceeded. Try again later.",
        }
    }


def test_eleventh_request_returns_standard_429_through_create_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _successful_create)
    with _client(monkeypatch) as client:
        for _ in range(10):
            assert (
                client.post(
                    "/api/links", json={"url": "https://example.test/"}
                ).status_code
                == 200
            )
        response = client.post("/api/links", json={"url": "https://example.test/"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize(
    "error_type",
    [
        DatabasePoolTimeoutError,
        DatabaseConnectionError,
        DatabaseStatementTimeoutError,
    ],
)
def test_database_timeouts_return_sanitized_503(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    def fail_create(*args: Any) -> Link:
        raise error_type("database details must not leak")

    monkeypatch.setattr(app_module, "create_or_reuse_link", fail_create)
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/links", json={"url": "https://secret.example/private"}
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "The service is temporarily unavailable.",
        }
    }
    assert "secret.example" not in response.text
    assert "database details" not in response.text


def test_failing_create_does_not_log_submitted_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    submitted_url = "ftp://sensitive.example/private"
    with _client(monkeypatch) as client, caplog.at_level(logging.DEBUG):
        response = client.post("/api/links", json={"url": submitted_url})

    assert response.status_code == 422
    assert submitted_url not in caplog.text


def test_lone_surrogate_in_url_returns_standard_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/links",
            content=b'{"url": "https://example.com/\\ud800"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_url"
    assert "ud800" not in response.text


def test_deeply_nested_json_returns_standard_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/api/links",
            content=b"[" * 100000,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
