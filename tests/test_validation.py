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
        r"http://jrb.sh\path",
        r"http://localhost\foo",
        r"http://127.0.0.1\x",
        "http://jrb%2Esh/",
        "http://127%2E0%2E0%2E1/",
        "http://\uff4a\uff52\uff42.\uff53\uff48/",
        "http://exam%70le.com/",
        "http://\u212a.example.com/",
        "http://[example.com]/",
        r"http://example.com\@jrb.sh",
    ],
)
def test_rejects_invalid_absolute_urls(url: str) -> None:
    _assert_rejected(url, "invalid_url")


@pytest.mark.parametrize(
    "url",
    [
        " https://example.com/",
        "https://example.com/ ",
    ],
)
def test_rejects_leading_and_trailing_whitespace(url: str) -> None:
    _assert_rejected(url, "invalid_url")


@pytest.mark.parametrize(
    "character", [" ", "\t", "\n", "\r", "\x00", "\x7f", "\u00a0", "\u2028"]
)
def test_rejects_raw_whitespace_and_control_characters(character: str) -> None:
    url = f"https://example.com/a{character}b"
    _assert_rejected(url, "invalid_url")


def test_rejects_overlength_url_before_parsing() -> None:
    url = "https://example.com/" + ("a" * 2029)
    assert len(url) == 2049
    _assert_rejected(url, "url_too_long")


def test_accepts_url_at_maximum_length() -> None:
    prefix = "https://example.com/"
    url = prefix + ("a" * (2048 - len(prefix)))

    assert len(url) == 2048
    assert validate_url(url, SELF_ROOT) == url


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
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://172.31.255.255/",
    ],
)
def test_rejects_additional_nonpublic_literal_addresses(url: str) -> None:
    _assert_rejected(url, "private_host")


def test_accepts_address_just_outside_private_172_range() -> None:
    url = "http://172.32.0.1/"
    assert validate_url(url, SELF_ROOT) == url


def test_accepts_punycode_hostname() -> None:
    url = "http://xn--e1afmkfd.xn--p1ai/"
    assert validate_url(url, SELF_ROOT) == url


@pytest.mark.parametrize("address", ["100.64.0.1", "224.0.0.1"])
def test_cidr_classification_gaps_are_characterized(address: str) -> None:
    # These ranges remain accepted under the explicit ipaddress flag policy.
    url = f"http://{address}/"
    assert validate_url(url, SELF_ROOT) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://0/",
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
    assert not _imports_database(module)


@pytest.mark.parametrize(
    "source",
    [
        "import url_shortener.database",
        "from url_shortener.database import Database",
        "from url_shortener import database",
        "from .database import Database",
        "from . import database",
        "from ..url_shortener import database",
    ],
)
def test_database_import_detector_catches_absolute_and_relative_imports(
    source: str,
) -> None:
    assert _imports_database(ast.parse(source))


def _imports_database(module: ast.Module) -> bool:
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "url_shortener.database"
                or alias.name.startswith("url_shortener.database.")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "url_shortener.database":
                return True
            if node.module == "database" and node.level > 0:
                return True
            if node.module == "url_shortener" and any(
                alias.name == "database" for alias in node.names
            ):
                return True
            if (
                node.level > 0
                and node.module is None
                and any(alias.name == "database" for alias in node.names)
            ):
                return True
    return False
