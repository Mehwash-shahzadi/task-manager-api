"""set updated_at with a database trigger

SQLAlchemy sets updated_at only for updates that go through it. A raw SQL update or a
bulk update skipped it. The trigger sets the time inside Postgres for every update.

Revision ID: 7b2e5a9c41d3
Revises: ed6957865fc8
Create Date: 2026-11-05 10:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7b2e5a9c41d3"
down_revision: str | Sequence[str] | None = "ed6957865fc8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("task", "outbox_event")


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_set_updated_at ON {table}")
    op.execute("DROP FUNCTION set_updated_at()")
