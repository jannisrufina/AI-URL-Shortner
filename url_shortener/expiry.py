import re
from datetime import UTC, datetime, timedelta


class ExpiryValidationError(ValueError):
    def __init__(self) -> None:
        self.code = "invalid_expiry"
        self.message = "Expiry must be an RFC 3339 timestamp within the next 365 days."
        super().__init__(self.message)


_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def parse_expiry(value: object, created_at: datetime) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _RFC3339_TIMESTAMP.fullmatch(value):
        raise ExpiryValidationError

    normalized = value.replace("t", "T")
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        expiry = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ExpiryValidationError from error

    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ExpiryValidationError
    if expiry <= created_at or expiry > created_at + timedelta(days=365):
        raise ExpiryValidationError
    return expiry.astimezone(UTC)
