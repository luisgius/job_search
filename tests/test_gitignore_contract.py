"""The public repo's .gitignore is a privacy control: pin what it must cover.

The repo is public and the pipeline writes secrets, a tracker database and
CV exports next to the code. A `.gitignore` edit that drops a rule fails
silently, at the moment someone runs `git add -A`. These tests ask git itself
(`check-ignore --no-index`, so nothing needs to exist on disk) and pin three
things:

- every MUST_IGNORE path is ignored *by the repo's own .gitignore*: a rule in
  a developer's global excludes file does not protect a fresh clone, so the
  matching rule's source is checked, not just the exit code;
- every MUST_TRACK path stays committable (template CVs, shipped config);
- no tracked file is matched by an ignore rule, which would hide its next
  edit from `git status` until someone forces it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Representative names, not files that exist: `--no-index` matches the path
# against the rules without consulting the index or the disk.
MUST_IGNORE = [
    # credentials and tokens
    ".env",
    ".env.local",
    ".env.production",
    "config.local.yaml",
    "gmail_credentials.json",
    "gmail_token.json",
    "token.json",
    "credentials.json",
    "client_secret_1234.apps.googleusercontent.com.json",
    # tracker databases and backups outside output/
    "tracker.sqlite3",
    "tracker.sqlite3-journal",
    "data/tracker.sqlite3-wal",
    "data/tracker.db",
    "data/tracker.db-journal",
    "output/tracker.sqlite3",
    "config.yaml.bak",
    # CVs and their exports
    "cv/my_cv.md",
    "cv/cv.pdf",
    "cv/cv.docx",
    "Luis_Gimenez_CV.pdf",
    # agent-local state (the shared .claude/ config stays committable)
    ".claude/settings.local.json",
    ".claude/scheduled_tasks.lock",
    ".claude/projects/session.jsonl",
    ".claude/worktrees/agent-1/README.md",
    # personal documents read by `python -m src.expand`
    "documents/old_cv.pdf",
    "documents/linkedin/Profile.csv",
]

MUST_TRACK = [
    ".env.example",
    "cv/base_cv.md",
    "cv/base_cv_ml.md",
    "cv/base_cv_product.md",
    "output/.gitkeep",
    "config.yaml",
    "watchlist.yaml",
    # `.claude/` is not ignored wholesale: shared settings and skills can ship.
    ".claude/settings.json",
    ".claude/skills/example/SKILL.md",
    "documents/README.md",
]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # core.excludesFile=<null> keeps a personal global ignore file from
    # deciding the result; the contract is about this repo's .gitignore.
    return subprocess.run(
        ["git", "-c", f"core.excludesFile={os.devnull}", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def _require_git_work_tree() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    top = _git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        pytest.skip("not running from a git work tree")
    if Path(top.stdout.strip()).resolve() != ROOT:
        pytest.skip("the enclosing work tree is not this repository")


@pytest.mark.parametrize("path", MUST_IGNORE)
def test_sensitive_path_is_ignored_by_the_repo_gitignore(path: str) -> None:
    res = _git("check-ignore", "-v", "--no-index", "--", path)
    assert res.returncode == 0, f"{path} is committable: .gitignore has no rule for it"
    # `-v` prints `<source>:<line>:<pattern>\t<path>`; a `!pattern` is a
    # negation, which -v reports with exit 0 even though the path is kept.
    source, _line, pattern = res.stdout.split("\t", 1)[0].split(":", 2)
    assert source == ".gitignore", f"{path} is ignored only by {source}, not the repo"
    assert not pattern.startswith("!"), f"{path} is re-included by {pattern!r}"


@pytest.mark.parametrize("path", MUST_TRACK)
def test_shipped_path_stays_committable(path: str) -> None:
    if path == ".env.example" and not (ROOT / path).exists():
        pytest.skip(".env.example is not part of this checkout")
    res = _git("check-ignore", "-q", "--no-index", "--", path)
    assert res.returncode == 1, f"{path} is ignored (git check-ignore exit {res.returncode})"


def test_no_tracked_file_matches_an_ignore_rule() -> None:
    res = _git("ls-files", "-ci", "--exclude-standard")
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [], (
        "tracked files match an ignore rule; add a narrow `!negation`"
    )
