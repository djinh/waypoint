"""The version: one source, and a new number whenever the installed code changes.

`pip install --upgrade` skips a version it already has, so a change to src/ that keeps the version
would never reach an installation. Compared with origin/main when this tree is a git checkout of
its own that has that ref (a branch under review); skipped otherwise, e.g. in an unpacked tree,
also one that happens to sit inside another git repository.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import waypoint

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'^__version__ = "([^"]+)"', re.M)


def base_comparison(root: Path) -> tuple[list[str], str] | str:
    """(files changed under src/ since the merge base with origin/main, the version at that base),
    or the reason the comparison can't be made. Every git call is in here, under one guard."""

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
                              check=True).stdout

    try:
        top = Path(git("rev-parse", "--show-toplevel").strip()).resolve()
        if top != root.resolve():
            return "not a git checkout of its own (inside another repository?)"
        base = git("merge-base", "HEAD", "origin/main").strip()
        changed = git("diff", "--name-only", base, "--", "src/").split()
        old = VERSION.search(git("show", f"{base}:src/waypoint/__init__.py"))
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "no git checkout with origin/main"
    if not old:
        return "no version at the base"
    return changed, old.group(1)


def test_one_source():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text and 'path = "src/waypoint/__init__.py"' in text
    assert not re.search(r'^version\s*=', text, re.M)
    # X.Y.Z, or a pre-release of one while a major version is built (2.0.0a1), as PEP 440 writes it
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", waypoint.__version__)


def test_code_changes_bump_the_version():
    got = base_comparison(ROOT)
    if isinstance(got, str):
        pytest.skip(got)
    changed, old = got
    if changed:
        assert old != waypoint.__version__, (
            f"src/ changed ({', '.join(changed[:3])}) but the version is still {waypoint.__version__}: "
            "bump __version__ in src/waypoint/__init__.py")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_an_unpacked_tree_inside_another_repository_is_skipped(tmp_path):
    """A plain copy of the tree inside an unrelated repository that has origin/main: git -C finds the
    enclosing repository, which must not be compared (it has no src/waypoint at its base)."""
    outer = tmp_path / "outer"
    tree = outer / "vendor" / "waypoint"
    (tree / "src" / "waypoint").mkdir(parents=True)
    (tree / "src" / "waypoint" / "__init__.py").write_text('__version__ = "9.9.9"\n')

    def git(*args):
        subprocess.run(["git", "-C", str(outer), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                       check=True, capture_output=True, timeout=30)

    git("init", "-q")
    (outer / "README").write_text("an unrelated repository\n")
    git("add", "README")
    git("commit", "-q", "-m", "start")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert base_comparison(tree) == "not a git checkout of its own (inside another repository?)"
    assert isinstance(base_comparison(tmp_path), str)  # not in any repository at all
