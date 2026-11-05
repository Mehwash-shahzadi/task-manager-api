"""add server defaults

Revision ID: d3a7c1e90b42
Revises: 572ff24eb2f6
Create Date: 2026-11-04 12:00:00.000000

The database now fills these columns itself, so a raw INSERT that skips them works.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a7c1e90b42"
down_revision: str | Sequence[str] | None = "572ff24eb2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, default). func.now() is the current time on the database server.
DEFAULTS = [
    ("task", "created_at", sa.func.now()),
    ("task", "updated_at", sa.func.now()),
    ("task", "status", "pending"),
    ("outbox_event", "created_at", sa.func.now()),
    ("outbox_event", "updated_at", sa.func.now()),
    ("outbox_event", "next_attempt_at", sa.func.now()),
    ("outbox_event", "status", "pending"),
    ("outbox_event", "attempts", "0"),
    ("outbox_event", "bad_attempts", "0"),
]


def upgrade() -> None:
    for table, column, default in DEFAULTS:
        op.alter_column(table, column, server_default=default)


def downgrade() -> None:
    for table, column, _ in DEFAULTS:
        op.alter_column(table, column, server_default=None)
