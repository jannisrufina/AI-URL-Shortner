import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

import psycopg

from url_shortener.database import Database

_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_CODE_ATTEMPTS = 5
_INSERT_OR_REUSE = """
INSERT INTO links (code, url_digest, original_url, expires_at, created_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (url_digest) DO UPDATE
SET expires_at = CASE
    WHEN links.expires_at IS NULL OR EXCLUDED.expires_at IS NULL THEN NULL
    ELSE EXCLUDED.expires_at
END
RETURNING code, original_url, expires_at
"""
_SELECT_BY_CODE = """
SELECT code, original_url, expires_at
FROM links
WHERE code = %s
"""


class CodeGenerationExhaustedError(RuntimeError):
    """Raised after repeated code primary-key collisions."""


@dataclass(frozen=True, slots=True)
class Link:
    code: str
    original_url: str
    expires_at: datetime | None


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(7))


def create_or_reuse_link(
    database: Database,
    submitted_url: str,
    expires_at: datetime | None,
    created_at: datetime,
    code_generator: Callable[[], str] | None = None,
) -> Link:
    digest = sha256(submitted_url.encode("utf-8")).digest()
    generator = generate_code if code_generator is None else code_generator
    for _ in range(_CODE_ATTEMPTS):
        candidate = generator()
        try:
            with database.connection() as connection:
                row = connection.execute(
                    _INSERT_OR_REUSE,
                    (candidate, digest, submitted_url, expires_at, created_at),
                ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name != "links_pkey":
                raise
            continue

        if row is None:
            raise RuntimeError("Link insert did not return a row")
        return Link(code=row[0], original_url=row[1], expires_at=row[2])

    raise CodeGenerationExhaustedError("Could not allocate a unique short code")


def get_link_by_code(database: Database, code: str) -> Link | None:
    with database.connection() as connection:
        row = connection.execute(_SELECT_BY_CODE, (code,)).fetchone()
    if row is None:
        return None
    return Link(code=row[0], original_url=row[1], expires_at=row[2])
