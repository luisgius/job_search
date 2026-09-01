"""The tracked tree never carries a credential — enforced, not hoped.

`keys:` in config.yaml is an *editable* section of a public repository, one
careless paste away from publishing a live key; `.gitignore` covers tokens
and `output/`, but nothing covered the files git happily publishes. This is
the mechanical half of that defence: the suite goes red the moment a tracked
file contains anything shaped like a known secret, or the shipped config
carries a non-empty key — or a phone number, which stays out of the public
history as a standing decision.

Every pattern below is assembled by concatenation so this file never matches
itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

#: (label, compiled pattern). Longish tails keep prose like "sk-ant-" in a
#: comment from tripping the check — a warning that cries wolf gets deleted.
SECRET_PATTERNS = [
    ("Anthropic API key", re.compile("sk-" + "ant-" + r"[A-Za-z0-9_-]{16,}")),
    ("OpenRouter API key", re.compile("sk-" + "or-" + r"v1-[0-9a-f]{16,}")),
    ("Google API key", re.compile("AI" + "za" + r"[0-9A-Za-z_-]{35}")),
    ("GitHub token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{20,}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{12,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:" + "AA" + r"[A-Za-z0-9_-]{30,}")),
    ("private key block", re.compile("-----BEGIN" + r" [A-Z ]*PRIVATE KEY-----")),
]

#: Content nobody can read secrets out of anyway, or that legitimately holds
#: high-entropy strings (none tracked today; the list is here for the day
#: one is).
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".sqlite3",
                 ".woff", ".woff2", ".zip"}


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
            check=True, timeout=30,
        ).stdout
    except Exception:
        pytest.skip("not a git checkout — nothing is 'tracked' here")
    return [REPO / name for name in out.decode("utf-8", "replace").split("\0") if name]


def test_no_tracked_file_carries_a_secret():
    offenders: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            found = pattern.search(text)
            if found:
                offenders.append(
                    f"{path.relative_to(REPO)}: looks like a {label} "
                    f"({found.group(0)[:12]}…)"
                )
    assert not offenders, (
        "secret-shaped content in tracked files — rotate the credential "
        "FIRST (history keeps it), then remove it:\n  " + "\n  ".join(offenders)
    )


def test_the_shipped_config_carries_no_key_and_no_phone():
    config = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    keys = config.get("keys") or {}
    populated = {name: value for name, value in keys.items()
                 if str(value or "").strip()}
    assert not populated, (
        f"config.yaml ships non-empty keys {sorted(populated)} — keys travel "
        "via environment variables (env-wins), never via the tracked file"
    )
    phone = str((config.get("applicant") or {}).get("phone") or "").strip()
    assert not phone, (
        "config.yaml ships a phone number — the standing decision is that it "
        "stays out of the public history (export it via env for live runs)"
    )
