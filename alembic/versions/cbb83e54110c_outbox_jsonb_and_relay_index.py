"""outbox jsonb and relay index

Revision ID: cbb83e54110c
Revises: 38e75fd2d3f8
Create Date: 2026-11-04 01:46:52.787421

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cbb83e54110c"
down_revision: str | Sequence[str] | None = "38e75fd2d3f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "outbox_event",
        "payload",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="payload::jsonb",
    )
    op.create_index(
        "ix_outbox_event_status_next_attempt",
        "outbox_event",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outbox_event_status_next_attempt", table_name="outbox_event")
    op.alter_column(
        "outbox_event",
        "payload",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="payload::json",
    )
