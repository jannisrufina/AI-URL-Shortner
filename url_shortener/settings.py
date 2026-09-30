import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """Raised when required environment settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    public_base_url: str
    pool_min_size: int = 1
    pool_max_size: int = 10
    pool_wait_ms: int = 250
    connect_timeout_seconds: int = 2
    statement_timeout_ms: int = 1000

    def __post_init__(self) -> None:
        if not self.database_url.strip():
            raise SettingsError("DATABASE_URL must not be empty")

        parsed = urlsplit(self.public_base_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise SettingsError("PUBLIC_BASE_URL has an invalid port") from error
        if not parsed.scheme or not parsed.netloc or not parsed.hostname:
            raise SettingsError("PUBLIC_BASE_URL must be an absolute URL")
        if port is not None and not 1 <= port <= 65535:
            raise SettingsError("PUBLIC_BASE_URL port must be between 1 and 65535")

        if self.pool_min_size < 1:
            raise SettingsError("DB_POOL_MIN_SIZE must be at least 1")
        if self.pool_max_size < self.pool_min_size:
            raise SettingsError(
                "DB_POOL_MAX_SIZE must be greater than or equal to DB_POOL_MIN_SIZE"
            )
        if self.pool_wait_ms < 1:
            raise SettingsError("DB_POOL_WAIT_MS must be positive")
        if self.connect_timeout_seconds < 1:
            raise SettingsError("DB_CONNECT_TIMEOUT_SECONDS must be positive")
        if self.statement_timeout_ms < 1:
            raise SettingsError("DB_STATEMENT_TIMEOUT_MS must be positive")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        database_url = source.get("DATABASE_URL", "").strip()
        public_base_url = source.get("PUBLIC_BASE_URL", "").strip()
        if not database_url:
            raise SettingsError("DATABASE_URL is required")
        if not public_base_url:
            raise SettingsError("PUBLIC_BASE_URL is required")

        return cls(
            database_url=database_url,
            public_base_url=public_base_url,
            pool_min_size=_integer(source, "DB_POOL_MIN_SIZE", 1),
            pool_max_size=_integer(source, "DB_POOL_MAX_SIZE", 10),
            pool_wait_ms=_integer(source, "DB_POOL_WAIT_MS", 250),
            connect_timeout_seconds=_integer(source, "DB_CONNECT_TIMEOUT_SECONDS", 2),
            statement_timeout_ms=_integer(source, "DB_STATEMENT_TIMEOUT_MS", 1000),
        )


def _integer(source: Mapping[str, str], name: str, default: int) -> int:
    value = source.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise SettingsError(f"{name} must be an integer") from error
