"""task status stored as lowercase values

Revision ID: a1f580401946
Revises: cbb83e54110c

The API uses "pending", but the database kept the Python names ("PENDING").
Renaming the enum labels keeps every existing row; no data is copied.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f580401946"
down_revision: str | Sequence[str] | None = "cbb83e54110c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LABELS = [("PENDING", "pending"), ("IN_PROGRESS", "in_progress"), ("DONE", "done")]


def upgrade() -> None:
    """Upgrade schema."""
    for old, new in LABELS:
        op.execute(f"ALTER TYPE taskstatus RENAME VALUE '{old}' TO '{new}'")


def downgrade() -> None:
    """Downgrade schema."""
    for old, new in LABELS:
        op.execute(f"ALTER TYPE taskstatus RENAME VALUE '{new}' TO '{old}'")
