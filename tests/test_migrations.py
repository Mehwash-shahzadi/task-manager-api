"""B11: the test database is built by the migrations, so these tests check the migrations."""

from sqlalchemy import text

from tests.conftest import _test_url, alembic


def test_models_and_migrations_match(test_engine):
    # alembic check fails when a model changed and nobody wrote the migration.
    result = alembic(_test_url(), "check", check=False)

    assert result.returncode == 0, result.stderr[-2000:]
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_database_is_at_the_latest_migration(test_engine):
    head = alembic(_test_url(), "heads").stdout.split()[0]

    with test_engine.connect() as connection:
        current = connection.execute(text("SELECT version_num FROM alembic_version"))

    assert current.scalar_one() == head


def test_constraints_from_the_migrations_exist(test_engine):
    with test_engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'task'::regclass"
                )
            )
        }

    assert "ck_task_title_not_empty" in names
