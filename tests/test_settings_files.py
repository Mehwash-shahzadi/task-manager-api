"""Every setting in config.py must be listed in .env.example (config.py has no defaults)."""

import re
from pathlib import Path

from app.config import Settings, settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def keys_in(filename):
    text = (PROJECT_ROOT / filename).read_text()
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


def test_every_setting_is_listed_in_env_example():
    missing = set(Settings.model_fields) - keys_in(".env.example")

    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_no_setting_has_a_default_in_config_py():
    with_default = [
        name for name, field in Settings.model_fields.items() if not field.is_required()
    ]

    assert with_default == []


def _readme_setting_names():
    import re

    text = (PROJECT_ROOT / "README.md").read_text()
    section = text[text.index("## Settings") : text.index("### Two Redis servers")]
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", section, re.M))


def _env_example_names():
    import re

    text = (PROJECT_ROOT / ".env.example").read_text()
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


# ---------- I18: the database login is written once ----------


def test_database_url_is_built_from_the_postgres_parts(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("POSTGRES_USER", "me")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word/1")
    monkeypatch.setenv("POSTGRES_HOST", "dbhost")
    monkeypatch.setenv("POSTGRES_PORT", "5555")
    monkeypatch.setenv("POSTGRES_DB", "mydb")

    url = Settings().DATABASE_URL  # type: ignore[call-arg]

    # The special characters of the password are escaped, so the address stays valid.
    assert url == "postgresql+psycopg://me:p%40ss%3Aword%2F1@dbhost:5555/mydb"


def test_database_url_is_not_a_setting_any_more():
    assert "DATABASE_URL" not in type(settings).model_fields
    assert {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST"} <= set(
        type(settings).model_fields
    )


def test_the_login_is_not_repeated_in_env_example_or_compose():
    import re

    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()

    assert not re.search(r"^DATABASE_URL=", env_example, re.M)
    assert "DATABASE_URL" not in compose
    assert re.search(r"^POSTGRES_HOST=", env_example, re.M)


# ---------- F2: the settings are documented in one table ----------


def test_readme_lists_every_setting_and_every_env_variable():
    documented = _readme_setting_names()

    assert set(type(settings).model_fields) <= documented
    assert _env_example_names() <= documented


def test_readme_lists_no_setting_that_does_not_exist():
    known = _env_example_names() | set(type(settings).model_fields)

    assert _readme_setting_names() <= known
