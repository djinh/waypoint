"""docs/CONFIGURATION.md stays complete: every key of the example configurations and every
environment variable the code reads is documented there."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")


def keys(table: dict, prefix: str = "") -> set[str]:
    out = set()
    for k, v in table.items():
        if isinstance(v, dict):
            out |= keys(v, f"{prefix}{k}.")
        else:
            out.add(f"{prefix}{k}")
    return out


def test_daemon_keys_documented():
    for key in keys(tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))):
        assert f"`{key}`" in DOC, key


def test_deliverer_keys_documented():
    data = tomllib.loads((ROOT / "deliver.example.toml").read_text(encoding="utf-8"))
    for key in keys(data["waypoint"]):
        assert f"`{key}`" in DOC, key
    for rc in data["recipient"].values():
        for key in rc:
            assert f"`{key}`" in DOC, key


def test_environment_documented():
    names = set()
    for path in (ROOT / "src" / "waypoint").glob("*.py"):
        names |= set(re.findall(r'environ\.get\("(WAYPOINT_[A-Z_]+)"', path.read_text(encoding="utf-8")))
    assert names
    for name in names:
        assert f"`{name}`" in DOC, name



LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_relative_links_resolve():
    """Every relative link in a Markdown file points to a file or folder that exists, so a copy of
    the tree with files left out fails here if anything still points to them."""
    for md in ROOT.rglob("*.md"):
        if any(part.startswith(".") or part in ("node_modules", "build", "dist") for part in md.relative_to(ROOT).parts):
            continue
        for target in LINK.findall(md.read_text(encoding="utf-8")):
            if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            path = (md.parent / target.split("#", 1)[0]).resolve()
            assert path.exists(), f"{md.relative_to(ROOT)}: {target}"
