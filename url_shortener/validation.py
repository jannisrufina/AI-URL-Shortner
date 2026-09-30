import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit


class URLValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def validate_url(submitted_url: str, self_reference_root: str) -> str:
    """Validate an absolute HTTP(S) URL without resolving or fetching its host."""
    if len(submitted_url) > 2048:
        raise URLValidationError("url_too_long", "URL must be at most 2048 characters.")

    if any(
        character == "\\"
        or character.isspace()
        or unicodedata.category(character) == "Cc"
        for character in submitted_url
    ):
        raise URLValidationError(
            "invalid_url",
            "URL must not contain backslashes, whitespace, or control characters.",
        )

    try:
        parsed = urlsplit(submitted_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise URLValidationError("invalid_url", "URL is malformed.") from error

    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise URLValidationError(
            "invalid_url", "URL must be an absolute HTTP or HTTPS URL with a hostname."
        )

    if not parsed.netloc.isascii():
        raise URLValidationError("invalid_url", "URL hostname is malformed.")

    authority = parsed.netloc.rsplit("@", maxsplit=1)[-1]
    if authority.endswith(":") and port is None:
        raise URLValidationError("invalid_url", "URL contains a malformed port.")

    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError(
            "credentials_not_allowed", "URLs must not contain embedded credentials."
        )

    normalized_hostname = hostname.rstrip(".").casefold()
    normalized_root = self_reference_root.rstrip(".").casefold()
    if normalized_hostname == normalized_root or normalized_hostname.endswith(
        f".{normalized_root}"
    ):
        raise URLValidationError(
            "self_reference", "URLs hosted by the shortener are not allowed."
        )

    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        raise URLValidationError(
            "private_host", "Private or local hosts are not allowed."
        )

    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", normalized_hostname):
            raise URLValidationError(
                "invalid_url", "URL hostname is malformed."
            ) from None
        return submitted_url

    addresses = [address]
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        addresses.append(address.ipv4_mapped)

    if any(
        candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_unspecified
        for candidate in addresses
    ):
        raise URLValidationError(
            "private_host", "Private or local hosts are not allowed."
        )

    return submitted_url
