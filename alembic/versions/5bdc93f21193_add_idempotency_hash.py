"""add idempotency hash

A retry with the same Idempotency-Key must send the same body. The hash of the first
request is stored so a different body can be refused (409).

Revision ID: 5bdc93f21193
Revises: 7b2e5a9c41d3
Create Date: 2026-11-05 02:39:22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5bdc93f21193"
down_revision: str | Sequence[str] | None = "7b2e5a9c41d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task", sa.Column("idempotency_hash", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("task", "idempotency_hash")
