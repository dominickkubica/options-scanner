"""MAP.md has to know about everything that exists.

A wiring map that depends on someone remembering to update it is out of date the first
time nobody does. So the inventory half is enforced here: every module under
`src/optscan`, every table the migrations create, every API route and every frontend
view must be named in MAP.md, and the suite -- which the pre-commit hook runs -- fails
until a new one is listed.

What this cannot check is whether the arrows are still true: a module can be listed and
its description wrong. That half stays a discipline, stated at the top of the map.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from optscan.config import REPO_ROOT, Settings
from optscan.storage import db

MAP = REPO_ROOT / "MAP.md"
SRC = REPO_ROOT / "src" / "optscan"
VIEWS = REPO_ROOT / "frontend" / "src" / "views"


@pytest.fixture(scope="module")
def text() -> str:
    assert MAP.exists(), "MAP.md is missing from the repo root"
    return MAP.read_text(encoding="utf-8")


def modules() -> list[str]:
    """Every module, as the path the map writes it: `analytics/gex.py`, `cli.py`."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        out.append(path.relative_to(SRC).as_posix())
    return out


def _named(text: str, module: str) -> bool:
    """A module counts as named if its path appears, or if it appears under its package.

    The index groups modules by package -- `**api/** ... routers/ideas.py ...` -- so a
    module may be written relative to any enclosing package section. Each split of the
    path is tried: `api/routers/ideas.py` matches `routers/ideas.py` in the **api/**
    section or `ideas.py` in an **api/routers/** one.
    """
    if module in text:
        return True
    parts = module.split("/")
    if len(parts) == 1:
        return f"`{module}`" in text
    for cut in range(1, len(parts)):
        package = "/".join(parts[:cut])
        rest = "/".join(parts[cut:])
        section = text.find(f"**{package}/**")
        if section == -1:
            continue
        following = text.find("\n\n", section)
        if f"`{rest}`" in text[section : following if following != -1 else None]:
            return True
    return False


@pytest.mark.parametrize("module", modules())
def test_every_module_is_on_the_map(text: str, module: str) -> None:
    assert _named(text, module), (
        f"{module} is not in MAP.md. Add it to the module index (section 8), and to "
        "whatever flow it takes part in."
    )


def test_every_table_is_on_the_map(text: str, tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    settings.ensure_dirs()
    with db.session(settings.sqlite_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
    missing = [table for table in tables if f"`{table}`" not in text]
    assert not missing, (
        f"Tables missing from MAP.md section 3: {missing}. Name the writer, the readers "
        "and what each holds."
    )


def test_every_route_is_on_the_map(text: str, tmp_path: Path) -> None:
    from optscan.api.app import create_app

    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    routes = [path.removeprefix("/api") for path in app.openapi()["paths"]]
    missing = [route for route in routes if f"`{route}`" not in text]
    assert not missing, f"Routes missing from MAP.md section 6: {missing}"


def test_every_view_is_on_the_map(text: str) -> None:
    views = sorted(path.stem for path in VIEWS.glob("*.jsx"))
    missing = [view for view in views if f"`{view}`" not in text]
    assert not missing, f"Frontend views missing from MAP.md section 7: {missing}"


def test_the_map_says_how_to_keep_it_current(text: str) -> None:
    """The rule has to live in the file it governs, where the next editor will see it."""
    assert "Keep it current" in text
    assert "tests/test_map.py" in text
