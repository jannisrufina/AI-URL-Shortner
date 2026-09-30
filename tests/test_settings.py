import pytest

from url_shortener.settings import Settings, SettingsError

BASE_ENV = {
    "DATABASE_URL": "postgresql://user:password@localhost/test_db",
    "PUBLIC_BASE_URL": "https://jrb.sh",
}


def test_settings_use_bounded_database_defaults() -> None:
    settings = Settings.from_env(BASE_ENV)

    assert settings.pool_min_size == 1
    assert settings.pool_max_size == 10
    assert settings.pool_wait_ms == 250
    assert settings.connect_timeout_seconds == 2
    assert settings.statement_timeout_ms == 1000


def test_settings_read_database_pool_overrides() -> None:
    environment = {
        **BASE_ENV,
        "DB_POOL_MIN_SIZE": "2",
        "DB_POOL_MAX_SIZE": "4",
        "DB_POOL_WAIT_MS": "125",
        "DB_CONNECT_TIMEOUT_SECONDS": "3",
        "DB_STATEMENT_TIMEOUT_MS": "750",
    }

    settings = Settings.from_env(environment)

    assert settings.pool_min_size == 2
    assert settings.pool_max_size == 4
    assert settings.pool_wait_ms == 125
    assert settings.connect_timeout_seconds == 3
    assert settings.statement_timeout_ms == 750


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PUBLIC_BASE_URL", "jrb.sh"),
        ("PUBLIC_BASE_URL", "https://"),
        ("PUBLIC_BASE_URL", "https://jrb.sh:99999"),
        ("DB_POOL_MIN_SIZE", "0"),
        ("DB_POOL_MAX_SIZE", "0"),
        ("DB_POOL_WAIT_MS", "0"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("DB_STATEMENT_TIMEOUT_MS", "0"),
        ("DB_POOL_MAX_SIZE", "not-an-integer"),
    ],
)
def test_settings_reject_invalid_values(name: str, value: str) -> None:
    with pytest.raises(SettingsError):
        Settings.from_env({**BASE_ENV, name: value})


def test_settings_require_database_url() -> None:
    with pytest.raises(SettingsError, match="DATABASE_URL is required"):
        Settings.from_env({"PUBLIC_BASE_URL": "https://jrb.sh"})
