from datetime import UTC, datetime, timedelta

import pytest

from url_shortener.expiry import ExpiryValidationError, parse_expiry

CREATED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def test_no_expiry_is_none() -> None:
    assert parse_expiry(None, CREATED_AT) is None


def test_accepts_exactly_365_days() -> None:
    value = (CREATED_AT + timedelta(days=365)).isoformat()

    assert parse_expiry(value, CREATED_AT) == CREATED_AT + timedelta(days=365)


def test_rejects_more_than_365_days() -> None:
    value = (CREATED_AT + timedelta(days=365, microseconds=1)).isoformat()

    with pytest.raises(ExpiryValidationError):
        parse_expiry(value, CREATED_AT)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-30T11:59:59Z",
        "2026-09-30T12:00:00Z",
        "2026-10-01T12:00:00",
        1_798_747_200,
        1.5,
        [],
        {},
        True,
        "not-a-timestamp",
        "2026-02-30T12:00:00Z",
    ],
)
def test_rejects_invalid_expiry_values(value: object) -> None:
    with pytest.raises(ExpiryValidationError) as caught:
        parse_expiry(value, CREATED_AT)

    assert caught.value.code == "invalid_expiry"


def test_parses_rfc3339_offsets_to_utc() -> None:
    expiry = parse_expiry("2026-10-01T15:00:00+03:00", CREATED_AT)

    assert expiry == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
