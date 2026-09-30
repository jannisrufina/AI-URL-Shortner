"""Create the links table and its lookup indexes."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260929_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "links",
        sa.Column("code", sa.String(length=7), nullable=False),
        sa.Column("url_digest", sa.LargeBinary(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("code ~ '^[A-Za-z0-9]{7}$'", name="links_code_format"),
        sa.CheckConstraint(
            "octet_length(url_digest) = 32", name="links_url_digest_length"
        ),
        sa.PrimaryKeyConstraint("code", name="links_pkey"),
    )
    op.create_index("links_url_digest_uq", "links", ["url_digest"], unique=True)


def downgrade() -> None:
    op.drop_index("links_url_digest_uq", table_name="links")
    op.drop_table("links")
