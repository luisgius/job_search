"""The scheduling pieces in scripts/ — checked, not just shipped.

A broken plist or a wrapper with a syntax error fails at 08:00 on a machine
nobody is watching, which is precisely the failure mode this whole setup
exists to close. So the suite proves what it can offline: both shell scripts
parse, the plist template is real XML with the weekday-08:00 schedule once
the path is substituted, the installer and template agree on names, and
`.env.example` is structurally incapable of carrying a secret.

The second half actually RUNS the wrapper and the installer in a sandbox —
python and curl stubbed, launchctl faked, no network — because the two
failure modes that matter most are behavioral, not syntactic: a lock left by
a SIGKILLed run must not block every future morning, and a repo path with a
space (or an ampersand) must survive the installer's sed and land in the
plist intact.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TEMPLATE = SCRIPTS / "com.job-hunter.daily.plist.template"


@pytest.mark.parametrize("name", ["run_daily.sh", "install_launchd.sh"])
def test_the_shell_scripts_parse_and_are_executable(name):
    path = SCRIPTS / name
    assert os.access(path, os.X_OK), f"{name} lost its executable bit"
    try:
        subprocess.run(["bash", "-n", str(path)], check=True, timeout=30,
                       capture_output=True)
    except FileNotFoundError:
        pytest.skip("no bash on this machine")


def test_the_plist_template_is_valid_and_says_weekdays_at_eight():
    filled = TEMPLATE.read_text(encoding="utf-8").replace("__REPO__", "/x/y")
    assert "__REPO__" not in filled
    root = ET.fromstring(filled)

    top = root.find("dict")
    keys = [el.text for el in top if el.tag == "key"]
    assert "StartCalendarInterval" in keys and "ProgramArguments" in keys

    args = [el.text for el in top.findall("array")[0].findall("string")]
    assert args[-1].endswith("scripts/run_daily.sh")

    schedule = top.findall("array")[1].findall("dict")
    assert len(schedule) == 5, "five weekdays, no weekends"
    for day in schedule:
        values = {k.text: v.text for k, v in zip(day.findall("key"),
                                                 day[1::2])}
        assert values["Hour"] == "8" and values["Minute"] == "0"
    assert {d[1::2][0].text for d in schedule} == {"1", "2", "3", "4", "5"}


def test_the_installer_and_the_template_agree_on_the_label():
    installer = (SCRIPTS / "install_launchd.sh").read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")
    assert 'LABEL="com.job-hunter.daily"' in installer
    assert "<string>com.job-hunter.daily</string>" in template
    # The installer builds the template's filename from the label.
    assert "$LABEL.plist.template" in installer


def test_the_wrapper_loads_env_takes_a_lock_and_never_opens_a_browser():
    wrapper = (SCRIPTS / "run_daily.sh").read_text(encoding="utf-8")
    # Sourcing is GUARDED: an unquoted space in .env is a command-not-found
    # under set -e, and it must cost a log line, never the silent death of
    # the whole run (the exact failure a scheduler hides best).
    assert 'if ! . "$REPO/.env"' in wrapper
    assert "set -a" in wrapper
    assert 'mkdir "$LOCK"' in wrapper, "overlap protection is load-bearing"
    assert "--no-browser" in wrapper, "a scheduler must not pop windows"
    # The breadcrumb precedes the first thing that can fail, so "the wrapper
    # ran at all" is always answerable from the daily log.
    assert wrapper.index("wrapper invoked") < wrapper.index('. "$REPO/.env"')
    # Monitoring is best-effort: every healthcheck ping must swallow failure.
    assert wrapper.count("|| true") >= 2


def test_env_example_cannot_carry_a_value():
    """Tracked file, so it must be structurally secret-proof: every line is a
    comment, blank, or `KEY=` with nothing after the equals sign — an empty
    quoted string allowed, since it doubles as the quote-your-spaces hint."""
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert re.fullmatch(r'[A-Z0-9_]+=(""|\'\')?', stripped), (
            f".env.example line carries a value: {line!r}"
        )


# ---------------------------------------------------------------------------
# Behavioral: the wrapper and the installer, actually executed in a sandbox.
# ---------------------------------------------------------------------------

def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _sandbox(tmp_path: Path, name: str = "repo with space") -> Path:
    """A copy of the scheduler scripts in a repo whose path has a space, with
    a stub venv python (records argv to called.txt, exits $FAKE_PY_EXIT) and
    nothing else the real pipeline needs — the wrapper is the thing under
    test, not the run."""
    repo = tmp_path / name
    (repo / "scripts").mkdir(parents=True)
    for script in ("run_daily.sh", "install_launchd.sh",
                   "com.job-hunter.daily.plist.template"):
        shutil.copy(SCRIPTS / script, repo / "scripts" / script)
    (repo / "output").mkdir()
    py = repo / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    # cwd is $REPO by the time the wrapper runs python, so bare filenames land
    # in the sandbox repo root.
    _stub(py, 'printf \'%s\\n\' "$@" >>called.txt\nexit "${FAKE_PY_EXIT:-0}"\n')
    return repo


def _run_wrapper(repo: Path, tmp_path: Path, **extra_env: str):
    """Run run_daily.sh with curl stubbed out (the last argument — the URL —
    is appended to pings.txt; nothing touches the network)."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    _stub(stubs / "curl",
          'for a in "$@"; do :; done\nprintf \'%s\\n\' "$a" >>pings.txt\n')
    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra_env)
    return subprocess.run(["bash", str(repo / "scripts" / "run_daily.sh")],
                          capture_output=True, text=True, timeout=60, env=env)


def _daily_log(repo: Path) -> str:
    logs = list((repo / "output" / "logs").glob("daily-*.log"))
    assert len(logs) == 1, f"expected exactly one daily log, got {logs}"
    return logs[0].read_text(encoding="utf-8")


def test_the_wrapper_runs_from_a_path_with_a_space_and_cleans_its_lock(tmp_path):
    repo = _sandbox(tmp_path)
    res = _run_wrapper(repo, tmp_path)
    assert res.returncode == 0, res.stderr
    assert (repo / "called.txt").read_text().split() == \
        ["-m", "src.main", "--no-browser"]
    assert "finished ok" in _daily_log(repo)
    assert not (repo / "output" / ".daily-run.lock").exists(), \
        "a clean run must release its lock"


def test_the_wrapper_passes_the_pipelines_exit_code_through(tmp_path):
    repo = _sandbox(tmp_path)
    res = _run_wrapper(repo, tmp_path, FAKE_PY_EXIT="3")
    assert res.returncode == 3, "launchctl must see the run's own exit code"
    assert "FAILED with exit 3" in _daily_log(repo)
    assert not (repo / "output" / ".daily-run.lock").exists(), \
        "the EXIT trap must fire on the failure path too"


def test_a_trailing_slash_in_the_healthchecks_url_is_stripped(tmp_path):
    """hc-ping routes are <uuid>, <uuid>/start, <uuid>/fail — a trailing
    slash pasted into .env must not 404 every ping as <uuid>//start."""
    repo = _sandbox(tmp_path)
    res = _run_wrapper(repo, tmp_path,
                       HEALTHCHECKS_URL="https://hc-ping.invalid/uuid/")
    assert res.returncode == 0, res.stderr
    pings = (repo / "pings.txt").read_text().splitlines()
    assert pings[0] == "https://hc-ping.invalid/uuid/start"
    assert pings[-1] == "https://hc-ping.invalid/uuid"
    assert not any("uuid//" in p for p in pings)


def test_a_fresh_lock_means_skip_and_the_lock_is_left_for_its_owner(tmp_path):
    repo = _sandbox(tmp_path)
    lock = repo / "output" / ".daily-run.lock"
    lock.mkdir()
    res = _run_wrapper(repo, tmp_path)
    assert res.returncode == 0, "an overlap skip is not a failure"
    assert not (repo / "called.txt").exists(), "the pipeline must not run"
    assert lock.is_dir(), "the loser must never remove the winner's lock"
    assert "skipping" in _daily_log(repo)


def test_a_stale_lock_is_reclaimed_instead_of_blocking_forever(tmp_path):
    """THE deadlock case: SIGKILL and power loss skip the EXIT trap, so the
    lock outlives its run. Six hours later it is a corpse, and the next
    morning must run — not skip with exit 0 forever."""
    repo = _sandbox(tmp_path)
    lock = repo / "output" / ".daily-run.lock"
    lock.mkdir()
    corpse = time.time() - 7 * 3600
    os.utime(lock, (corpse, corpse))
    res = _run_wrapper(repo, tmp_path)
    assert res.returncode == 0, res.stderr
    assert (repo / "called.txt").exists(), "the run must actually happen"
    assert not lock.exists(), "the stale lock must be gone afterwards"
    assert "stale lock" in _daily_log(repo)


def test_the_installer_fills_the_plist_from_a_path_with_space_and_ampersand(tmp_path):
    """Runs the real install_launchd.sh (uname and launchctl faked) from a
    repo path carrying the two characters that break naive substitution: a
    space (shell quoting) and an ampersand (special on sed's replacement
    side AND in XML). The installed plist must parse and round-trip the
    exact path."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine")
    repo = _sandbox(tmp_path, name="repo with space & co")
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _stub(fakes / "uname", "echo Darwin\n")
    _stub(fakes / "launchctl",
          'printf \'%s\\n\' "$*" >>"${LAUNCHCTL_LOG:-/dev/null}"\n')
    home = tmp_path / "home with space"
    home.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{fakes}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(home)
    env["LAUNCHCTL_LOG"] = str(tmp_path / "launchctl.log")
    res = subprocess.run(["bash", str(repo / "scripts" / "install_launchd.sh")],
                         capture_output=True, text=True, timeout=60, env=env)
    assert res.returncode == 0, res.stderr

    plist = home / "Library" / "LaunchAgents" / "com.job-hunter.daily.plist"
    with plist.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["ProgramArguments"] == \
        ["/bin/bash", f"{repo}/scripts/run_daily.sh"]
    assert data["WorkingDirectory"] == str(repo)
    assert data["StandardOutPath"] == f"{repo}/output/logs/launchd.log"
    assert (repo / "output" / "logs").is_dir(), \
        "launchd never creates log directories, so the installer must"

    calls = (tmp_path / "launchctl.log").read_text().splitlines()
    assert any(c.startswith("bootout gui/") for c in calls)
    assert any(c.startswith("bootstrap gui/") and c.endswith(".plist")
               for c in calls), "the agent must actually get bootstrapped"
