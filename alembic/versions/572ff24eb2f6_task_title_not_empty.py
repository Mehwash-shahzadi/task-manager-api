"""task title not empty

Revision ID: 572ff24eb2f6
Revises: a1f580401946

Alembic does not detect check constraints, so this migration is written by hand.
With the naming convention the real name is ck_task_title_not_empty.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "572ff24eb2f6"
down_revision: str | Sequence[str] | None = "a1f580401946"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_check_constraint("title_not_empty", "task", "length(title) > 0")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("title_not_empty", "task", type_="check")
