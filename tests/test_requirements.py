"""Pinned requirement files."""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def pinned_lines(filename):
    """Every package line of a requirements file (the "-r other-file" line is skipped)."""
    lines = (PROJECT_ROOT / filename).read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("-")]


# ---------- B9: requirements are pinned ----------


@pytest.mark.parametrize(
    "line", pinned_lines("requirements.txt") + pinned_lines("requirements-dev.txt")
)
def test_every_requirement_has_a_version(line):
    assert "==" in line, f"{line} has no pinned version"


# ---------- C9: the dev tools are pinned too ----------


def test_dev_requirements_pin_every_tool_to_the_installed_version():
    from importlib.metadata import version

    text = (PROJECT_ROOT / "requirements-dev.txt").read_text()

    for package in ("pytest", "httpx2", "ruff", "mypy", "pip-audit"):
        assert f"{package}=={version(package)}" in text
