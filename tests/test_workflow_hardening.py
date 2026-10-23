"""Pin the GitHub Actions hardening that a later workflow edit could drop.

- Every `uses:` is pinned to a full 40-hex commit SHA with a `# vX.Y.Z`
  comment. A tag can be moved to different code after review; a SHA cannot.
  Dependabot (.github/dependabot.yml) keeps the pins current.
- Every `actions/checkout` sets `persist-credentials: false`, so the job token
  is not left in `.git/config` for every later step to read.
- Every workflow declares top-level `permissions:` with no write scope; a job
  that needs more must ask for it itself.
- No attacker-controllable expression (`github.event.*`, `inputs.*`,
  `github.head_ref`) is expanded inside a `run:` script, where it would be
  pasted into the shell as code. Pass it through `env:` instead.

Adapted from ai-job-search (https://github.com/ai-job-search-dev/ai-job-search),
tests/test_workflow_hardening.py, MIT License, © 2026 Mads Lorentzen.
Rewritten to parse the workflows with PyYAML instead of scanning lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

GITHUB = Path(__file__).resolve().parents[1] / ".github"
WORKFLOWS = sorted([*GITHUB.glob("workflows/*.yml"), *GITHUB.glob("workflows/*.yaml")])

PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
PINNED_LINE = re.compile(r"\buses:\s*['\"]?[^@\s'\"]+@[0-9a-f]{40}['\"]?\s+#\s*v\d")
UNTRUSTED_IN_SCRIPT = re.compile(
    r"\$\{\{[^}]*\b(github\.event\b|inputs\.|github\.head_ref\b)"
)


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a workflow mapping"
    return data


def _steps(workflow: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, step


def _uses(workflow: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Every `uses:` reference: step actions and job-level reusable workflows."""
    for job_id, job in (workflow.get("jobs") or {}).items():
        if "uses" in job:
            yield job_id, str(job["uses"])
    for job_id, step in _steps(workflow):
        if "uses" in step:
            yield job_id, str(step["uses"])


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def test_workflows_exist() -> None:
    assert WORKFLOWS, "no workflow under .github/workflows; CI would never run"


@pytest.mark.parametrize("path", WORKFLOWS, ids=_ids(WORKFLOWS))
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    for job_id, ref in _uses(_load(path)):
        if ref.startswith("./"):  # an action in this repo, versioned with it
            continue
        assert PINNED.match(ref), f"{path.name} job {job_id}: {ref} is not pinned to a SHA"
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.split("#", 1)[0]
        if re.search(r"\buses:\s*['\"]?\./", stripped) or "uses:" not in stripped:
            continue
        assert PINNED_LINE.search(line), (
            f"{path.name}:{n}: a SHA pin needs a `# vX.Y.Z` comment saying what it is"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=_ids(WORKFLOWS))
def test_every_checkout_drops_its_credentials(path: Path) -> None:
    for job_id, step in _steps(_load(path)):
        if not str(step.get("uses", "")).startswith("actions/checkout@"):
            continue
        persist = (step.get("with") or {}).get("persist-credentials")
        assert persist in (False, "false"), (
            f"{path.name} job {job_id}: checkout must set persist-credentials: false"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=_ids(WORKFLOWS))
def test_top_level_token_is_read_only(path: Path) -> None:
    workflow = _load(path)
    assert "permissions" in workflow, (
        f"{path.name}: no top-level permissions; the token gets the repo default"
    )
    permissions = workflow["permissions"]
    if isinstance(permissions, str):
        assert permissions == "read-all", f"{path.name}: permissions: {permissions}"
    else:
        writes = {k: v for k, v in (permissions or {}).items() if v == "write"}
        assert not writes, f"{path.name}: top-level write scopes {writes}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=_ids(WORKFLOWS))
def test_untrusted_expressions_stay_out_of_run_scripts(path: Path) -> None:
    for job_id, step in _steps(_load(path)):
        script = step.get("run")
        if script is None:
            continue
        hit = UNTRUSTED_IN_SCRIPT.search(str(script))
        assert hit is None, (
            f"{path.name} job {job_id}: `{hit.group(0)}...` expanded inside run:; "
            "pass it through env: and quote the variable"
        )


def test_dependabot_bumps_the_pinned_actions() -> None:
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {u.get("package-ecosystem") for u in config.get("updates") or []}
    assert "github-actions" in ecosystems, "SHA pins rot without a bumper"
