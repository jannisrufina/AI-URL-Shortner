from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import url_shortener.app as app_module
from url_shortener.database import DatabasePoolTimeoutError
from url_shortener.links import Link
from url_shortener.settings import Settings


class FakeDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(
        database_url="postgresql://user:password@localhost/test_db",
        public_base_url="https://short.example/base/",
    )
    monkeypatch.setattr(app_module, "Database", FakeDatabase)
    return TestClient(app_module.create_app(settings))


def _success(
    _database: Any,
    submitted_url: str,
    expires_at: datetime | None,
    _created_at: datetime,
) -> Link:
    return Link("AbC1234", submitted_url, expires_at)


def _assert_html_headers(response: Any) -> None:
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def test_get_root_renders_form_with_security_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.get("/")

    _assert_html_headers(response)
    assert response.status_code == 200
    assert '<form action="/" method="post">' in response.text
    assert 'name="url" type="text"' in response.text
    assert 'name="expires_at"' in response.text
    assert "RFC 3339 with timezone" in response.text
    assert "2026-10-01T12:00:00Z" in response.text
    assert "datetime-local" not in response.text


def test_form_and_json_api_share_create_service_and_short_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _success)
    submitted_url = "https://example.test/page"
    with _client(monkeypatch) as client:
        api_response = client.post("/api/links", json={"url": submitted_url})
        form_response = client.post("/", data={"url": submitted_url})
        repeated_form_response = client.post("/", data={"url": submitted_url})

    assert api_response.status_code == 200
    assert form_response.status_code == 200
    assert repeated_form_response.status_code == 200
    assert api_response.json()["code"] == "AbC1234"
    assert 'href="https://short.example/base/AbC1234"' in form_response.text
    assert "https://short.example/base/AbC1234" in form_response.text
    assert 'href="https://short.example/base/AbC1234"' in repeated_form_response.text
    _assert_html_headers(form_response)


def test_form_submits_optional_rfc3339_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_expiries: list[datetime | None] = []

    def save(
        _database: Any,
        submitted_url: str,
        expires_at: datetime | None,
        _created_at: datetime,
    ) -> Link:
        saved_expiries.append(expires_at)
        return Link("AbC1234", submitted_url, expires_at)

    monkeypatch.setattr(app_module, "create_or_reuse_link", save)
    expiry_value = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with _client(monkeypatch) as client:
        response = client.post(
            "/", data={"url": "https://example.test/page", "expires_at": expiry_value}
        )

    assert response.status_code == 200
    assert saved_expiries[0] is not None
    assert saved_expiries[0].isoformat() == expiry_value


@pytest.mark.parametrize(
    ("form_data", "expected_message", "submitted_value"),
    [
        (
            {"url": "ftp://secret.example/path"},
            "URL must be an absolute HTTP or HTTPS URL with a hostname.",
            "ftp://secret.example/path",
        ),
        (
            {"url": "https://example.test/", "expires_at": "not a timestamp"},
            "Expiry must be an RFC 3339 timestamp within the next 365 days.",
            "not a timestamp",
        ),
        (
            {"expires_at": "secret-expiry"},
            "Request body is invalid.",
            "secret-expiry",
        ),
    ],
)
def test_form_validation_errors_render_fixed_html(
    monkeypatch: pytest.MonkeyPatch,
    form_data: dict[str, str],
    expected_message: str,
    submitted_value: str,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post("/", data=form_data)

    _assert_html_headers(response)
    assert response.status_code == 422
    assert expected_message in response.text
    assert (
        submitted_value
        not in response.text.split('role="alert">', 1)[1].split("</p>", 1)[0]
    )


def test_malformed_form_body_renders_sanitized_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/",
            content=(
                b'--broken\r\nContent-Disposition: form-data; name="url"\r\n\r\nsecret'
            ),
            headers={"content-type": "multipart/form-data; boundary=broken"},
        )

    _assert_html_headers(response)
    assert response.status_code == 422
    assert "Request body is invalid." in response.text
    message = response.text.split('role="alert">', 1)[1].split("</p>", 1)[0]
    assert "secret" not in message


@pytest.mark.parametrize("xss_field", ["url", "expires_at"])
def test_success_and_error_pages_escape_submitted_markup(
    monkeypatch: pytest.MonkeyPatch, xss_field: str
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _success)
    attack = '"><script>alert(1)</script>'
    valid_attack_url = f"https://example.test/{attack}"
    with _client(monkeypatch) as client:
        if xss_field == "url":
            response = client.post("/", data={"url": valid_attack_url})
            assert response.status_code == 200
        else:
            response = client.post(
                "/",
                data={"url": valid_attack_url, "expires_at": attack},
            )
            assert response.status_code == 422

    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_invalid_form_request_consumes_rate_limit_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _success)
    with _client(monkeypatch) as client:
        invalid = client.post("/", data={"url": "ftp://invalid.test"})
        accepted = [
            client.post("/", data={"url": "https://example.test/"}) for _ in range(9)
        ]
        limited = client.post("/", data={"url": "https://example.test/"})

    assert invalid.status_code == 422
    assert all(response.status_code == 200 for response in accepted)
    _assert_html_headers(limited)
    assert limited.status_code == 429
    assert "Create rate limit exceeded. Try again later." in limited.text


def test_eleventh_valid_form_post_returns_html_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "create_or_reuse_link", _success)
    with _client(monkeypatch) as client:
        responses = [
            client.post("/", data={"url": "https://example.test/"}) for _ in range(11)
        ]

    _assert_html_headers(responses[-1])
    assert [response.status_code for response in responses] == [200] * 10 + [429]


def test_database_failure_renders_sanitized_html_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_create(*_args: Any) -> Link:
        raise DatabasePoolTimeoutError("private database details")

    monkeypatch.setattr(app_module, "create_or_reuse_link", fail_create)
    with _client(monkeypatch) as client:
        response = client.post("/", data={"url": "https://secret.example/private"})

    _assert_html_headers(response)
    assert response.status_code == 503
    assert "The service is temporarily unavailable." in response.text
    assert "private database details" not in response.text
    assert "secret.example" in response.text
    assert (
        "secret.example"
        not in response.text.split('role="alert">', 1)[1].split("</p>", 1)[0]
    )
