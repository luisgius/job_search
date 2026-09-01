"""The scheduling pieces in scripts/ — checked, not just shipped.

A broken plist or a wrapper with a syntax error fails at 08:00 on a machine
nobody is watching, which is precisely the failure mode this whole setup
exists to close. So the suite proves what it can offline: both shell scripts
parse, the plist template is real XML with the weekday-08:00 schedule once
the path is substituted, the installer and template agree on names, and
`.env.example` is structurally incapable of carrying a secret.
"""

from __future__ import annotations

import os
import re
import subprocess
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
