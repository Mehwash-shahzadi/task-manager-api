"""outbox cleanup index

The hourly cleanup looks for old sent rows (status = 'sent' and sent_at before a date).
A partial index on sent_at holds only sent rows, so the cleanup no longer reads every row.
The single-column status index is dropped: the (status, next_attempt_at) index already
starts with status, so it serves the same questions.

Revision ID: c0b7f00732ee
Revises: 5bdc93f21193
Create Date: 2026-11-05 03:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c0b7f00732ee"
down_revision: str | Sequence[str] | None = "5bdc93f21193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_outbox_event_status"), table_name="outbox_event")
    op.create_index(
        "ix_outbox_event_sent_at_sent",
        "outbox_event",
        ["sent_at"],
        unique=False,
        postgresql_where=sa.text("status = 'sent'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_event_sent_at_sent",
        table_name="outbox_event",
        postgresql_where=sa.text("status = 'sent'"),
    )
    op.create_index(
        op.f("ix_outbox_event_status"), "outbox_event", ["status"], unique=False
    )
