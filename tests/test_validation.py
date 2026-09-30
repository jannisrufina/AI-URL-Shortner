import ast
import socket
from pathlib import Path

import pytest

from url_shortener.validation import URLValidationError, validate_url

SELF_ROOT = "jrb.sh"


def _assert_rejected(url: str, expected_code: str) -> URLValidationError:
    with pytest.raises(URLValidationError) as caught:
        validate_url(url, SELF_ROOT)

    error = caught.value
    assert error.code == expected_code
    assert url not in str(error)
    assert url not in error.message
    return error


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com:8443/a/b?x=1&y=two#fragment",
        "https://93.184.216.34:443/path?query=yes",
    ],
)
def test_accepts_valid_http_urls_unchanged(url: str) -> None:
    assert validate_url(url, SELF_ROOT) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "file:///tmp/example",
        "https:///missing-host",
        "https://",
        "http://example.com:abc/path",
        "http://example.com:99999/path",
        "http://example.com:/path",
        "http://[::1/path",
    ],
)
def test_rejects_invalid_absolute_urls(url: str) -> None:
    _assert_rejected(url, "invalid_url")


@pytest.mark.parametrize("character", [" ", "\t", "\n", "\x00"])
def test_rejects_raw_whitespace_and_control_characters(character: str) -> None:
    url = f"https://example.com/a{character}b"
    _assert_rejected(url, "invalid_url")


def test_rejects_overlength_url_before_parsing() -> None:
    url = "https://example.com/" + ("a" * 2029)
    assert len(url) == 2049
    _assert_rejected(url, "url_too_long")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/path",
        "https://user@example.com/path",
        "https://:secret@example.com/path",
    ],
)
def test_rejects_embedded_credentials(url: str) -> None:
    _assert_rejected(url, "credentials_not_allowed")


@pytest.mark.parametrize(
    "url",
    [
        "https://jrb.sh",
        "https://jrb.sh:443/path",
        "http://api.jrb.sh/path",
        "https://JRB.SH./path?x=1",
        "https://Api.JrB.Sh.:8443/path",
        "http://jrb.sh./with/trailing/dot",
    ],
)
def test_rejects_self_reference(url: str) -> None:
    _assert_rejected(url, "self_reference")


def test_self_reference_uses_supplied_hostname_root() -> None:
    url = "https://api.short.example:9443/path"
    with pytest.raises(URLValidationError) as caught:
        validate_url(url, "short.example.")
    assert caught.value.code == "self_reference"
    assert url not in str(caught.value)


def test_self_reference_lookalike_is_allowed() -> None:
    url = "https://notjrb.sh/path"
    assert validate_url(url, SELF_ROOT) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.10.20/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "http://foo.localhost/",
        "http://LOCALHOST./",
    ],
)
def test_rejects_private_loopback_and_local_hosts(url: str) -> None:
    _assert_rejected(url, "private_host")


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
    ],
)
def test_legacy_alternate_ipv4_forms_remain_accepted(url: str) -> None:
    # L-5: legacy decimal, hexadecimal, and octal-like IPv4 forms are not normalized.
    assert validate_url(url, SELF_ROOT) == url


def test_validation_does_not_resolve_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> None:
        pytest.fail("URL validation must not perform DNS lookups")

    monkeypatch.setattr(socket, "getaddrinfo", fail_if_called)
    monkeypatch.setattr(socket, "gethostbyname", fail_if_called)

    for url in (
        "https://example.com/path",
        "http://public-looking.internal/",
        "https://subdomain.example.net:8443/",
    ):
        assert validate_url(url, SELF_ROOT) == url


def test_validation_module_does_not_import_database() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "url_shortener" / "validation.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    imports_database = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "url_shortener.database"
        or isinstance(node, ast.Import)
        and any(alias.name == "url_shortener.database" for alias in node.names)
        for node in ast.walk(module)
    )
    assert not imports_database
