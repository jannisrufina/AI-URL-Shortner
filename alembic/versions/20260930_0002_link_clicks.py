"""Add append-only click events for successful redirects.

No foreign key to links is intentional: the benchmark seeder's confirmed
--reset truncates links, and a foreign key would block truncating links alone.
The seeder explicitly truncates this event table first.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260930_0002"
down_revision: str | None = "20260929_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "link_clicks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=7), nullable=False),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("code ~ '^[A-Za-z0-9]{7}$'", name="link_clicks_code_format"),
    )
    op.create_index(
        "link_clicks_code_clicked_at_idx",
        "link_clicks",
        ["code", "clicked_at"],
    )


def downgrade() -> None:
    op.drop_index("link_clicks_code_clicked_at_idx", table_name="link_clicks")
    op.drop_table("link_clicks")
