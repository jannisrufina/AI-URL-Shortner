from datetime import UTC, datetime, timedelta
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

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FakeDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def connection(self) -> None:
        raise AssertionError("Malformed codes must not access the database")


def _client(
    monkeypatch: pytest.MonkeyPatch,
    lookup: Any | None = None,
) -> TestClient:
    settings = Settings(
        database_url="postgresql://user:password@localhost/test_db",
        public_base_url="https://jrb.sh",
    )
    monkeypatch.setattr(app_module, "Database", FakeDatabase)
    if lookup is not None:
        monkeypatch.setattr(app_module, "get_link_by_code", lookup)
    return TestClient(app_module.create_app(settings))


def test_dynamic_get_route_does_not_shadow_fastapi_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        docs = client.get("/docs")
        openapi = client.get("/openapi.json")

    assert docs.status_code == 200
    assert openapi.status_code == 200


@pytest.mark.parametrize(
    ("original_url", "expected_location"),
    [
        (
            "https://example.test/path",
            "https://example.test/path",
        ),
        (
            "https://example.test/path?one=two#section",
            "https://example.test/path?one=two#section",
        ),
        (
            "https://example.test/caf\u00e9?q=caf\u00e9",
            "https://example.test/caf%C3%A9?q=caf%C3%A9",
        ),
    ],
)
def test_active_links_redirect_with_valid_location(
    monkeypatch: pytest.MonkeyPatch,
    original_url: str,
    expected_location: str,
) -> None:
    link = Link("AbC1234", original_url, None)
    calls: list[str] = []

    def lookup(_database: Any, code: str) -> Link:
        calls.append(code)
        return link

    with _client(monkeypatch, lookup) as client:
        response = client.get("/AbC1234", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == expected_location
    assert calls == ["AbC1234"]


@pytest.mark.parametrize("path", ["/abc123", "/abc$234", "/..%2F"])
def test_malformed_codes_return_404_without_database_access(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    with _client(monkeypatch) as client:
        response = client.get(path, follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Short link was not found."}
    }


def test_missing_code_returns_standard_404(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def lookup(_database: Any, code: str) -> None:
        calls.append(code)
        return None

    with _client(monkeypatch, lookup) as client:
        response = client.get("/AbC1234", follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Short link was not found."}
    }
    assert calls == ["AbC1234"]


@pytest.mark.parametrize(
    ("expires_at", "expected_status"),
    [
        (NOW, 404),
        (NOW + timedelta(seconds=1), 302),
    ],
)
def test_expiry_boundary_uses_one_request_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    expires_at: datetime,
    expected_status: int,
) -> None:
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    link = Link("AbC1234", "https://example.test/path", expires_at)

    with _client(monkeypatch, lambda _database, _code: link) as client:
        response = client.get("/AbC1234", follow_redirects=False)

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("path", "lookup_result", "expected_hook_calls"),
    [
        ("/AbC1234", Link("AbC1234", "https://example.test/", None), 1),
        ("/AbC1234", None, 0),
        (
            "/AbC1234",
            Link("AbC1234", "https://example.test/", NOW),
            0,
        ),
        ("/bad", Link("AbC1234", "https://example.test/", None), 0),
    ],
)
def test_click_hook_runs_only_after_active_lookup(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    lookup_result: Link | None,
    expected_hook_calls: int,
) -> None:
    monkeypatch.setattr(app_module, "_utc_now", lambda: NOW)
    hook_calls: list[Link] = []
    monkeypatch.setattr(app_module, "record_click", hook_calls.append)

    def lookup(_database: Any, _code: str) -> Link | None:
        return lookup_result

    with _client(monkeypatch, lookup) as client:
        response = client.get(path, follow_redirects=False)

    assert len(hook_calls) == expected_hook_calls
    assert response.status_code == (302 if expected_hook_calls else 404)


@pytest.mark.parametrize(
    "database_error",
    [
        DatabasePoolTimeoutError("pool unavailable"),
        DatabaseConnectionError("connection unavailable"),
        DatabaseStatementTimeoutError("statement timed out"),
    ],
)
def test_database_failures_return_sanitized_503(
    monkeypatch: pytest.MonkeyPatch, database_error: Exception
) -> None:
    def fail_lookup(_database: Any, _code: str) -> None:
        raise database_error

    with _client(monkeypatch, fail_lookup) as client:
        response = client.get("/AbC1234", follow_redirects=False)

    assert response.status_code == 503
    assert response.status_code != 404
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "The service is temporarily unavailable.",
        }
    }
    assert str(database_error) not in response.text


def test_click_write_failure_keeps_redirect_and_logs_only_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    submitted_url = "https://example.test/private/path"

    def fail_click_write(_database: Any, _code: str) -> None:
        raise DatabasePoolTimeoutError("private URL must not enter the log")

    monkeypatch.setattr(app_module, "record_link_click", fail_click_write)
    link = Link("AbC1234", submitted_url, None)
    with _client(monkeypatch, lambda _database, _code: link) as client:
        response = client.get("/AbC1234", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == submitted_url
    assert "AbC1234" in caplog.text
    assert submitted_url not in caplog.text
    assert "private URL must not enter the log" not in caplog.text
