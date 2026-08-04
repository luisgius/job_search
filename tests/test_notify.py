"""Tests for src/notify.py — getting an alert out of cron and to a human.

Two things are worth more attention than the rest:

  * **The `command` channel runs a subprocess**, and the message it carries
    is built from job titles and error strings — text from the open internet.
    It must be impossible for that text to become a command.
  * **A notifier must never break the run it was meant to warn you about.**
    Every channel failure is contained, and one channel dying does not stop
    the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.health import Alert, HealthReport
from src.notify import (
    ALERT_FILENAME,
    CHANNELS,
    SMTP_PASSWORD_ENV,
    channel_settings,
    clear_alert_file,
    enabled_channels,
    send,
)
from tests.conftest import write_config

BAD = HealthReport([
    Alert("no_jobs", "critical", "no postings fetched from any source",
          "Verify your slugs."),
])


#: Every channel off. Tests opt in explicitly, because `write_config` merges
#: onto the shipped defaults — where `console` and `file` are already on — and
#: a test that silently gets an extra channel is a test asserting the wrong
#: thing.
ALL_OFF = {"console": False, "file": False, "command": "", "email": {}}


def notify_config(tmp_path: Path, channels=None, **overrides):
    settings = {"enabled": True,
                "channels": {**ALL_OFF, **(channels if channels is not None
                                           else {"file": True})}}
    settings.update(overrides)
    return write_config(tmp_path, {"notify": settings,
                                   "output": {"dir": str(tmp_path / "output")}})


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Stand-in for `subprocess.run`, recording exactly what it was handed."""

    def __init__(self, returncode=0, error=None):
        self.returncode = returncode
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.error:
            raise self.error
        return FakeCompleted(returncode=self.returncode, stderr="boom")


class FakeSMTP:
    """Stand-in for `smtplib.SMTP`."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, *, fail_on=None):
        self.host, self.port = host, port
        self.fail_on = fail_on
        self.started_tls = False
        self.login_args = None
        self.sent: list[Any] = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self):
        if self.fail_on == "starttls":
            raise RuntimeError("tls failed")
        self.started_tls = True

    def login(self, username, password):
        if self.fail_on == "login":
            raise RuntimeError("bad credentials")
        self.login_args = (username, password)

    def send_message(self, message):
        if self.fail_on == "send":
            raise RuntimeError("relay refused")
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def _reset_smtp():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


def smtp_factory(**kwargs):
    return lambda host, port: FakeSMTP(host, port, **kwargs)


# ==========================================================================
# nothing is sent when the run was healthy
# ==========================================================================


def test_a_healthy_run_sends_nothing(tmp_path: Path):
    results = send(HealthReport(), notify_config(tmp_path), output_dir=tmp_path)
    assert results == []


def test_a_healthy_run_clears_a_stale_alert_file(tmp_path: Path):
    """So the presence of ALERT.txt always means "the last run had a problem"
    — a file that lingers after recovery is worse than no file."""
    (tmp_path / ALERT_FILENAME).write_text("yesterday's problem", encoding="utf-8")
    send(HealthReport(), notify_config(tmp_path), output_dir=tmp_path)
    assert not (tmp_path / ALERT_FILENAME).exists()


def test_notify_can_be_switched_off_entirely(tmp_path: Path):
    cfg = notify_config(tmp_path, enabled=False)
    assert send(BAD, cfg, output_dir=tmp_path) == []
    assert not (tmp_path / ALERT_FILENAME).exists()


# ==========================================================================
# channels
# ==========================================================================


def test_the_file_channel_writes_the_alert(tmp_path: Path):
    send(BAD, notify_config(tmp_path, {"file": True}), output_dir=tmp_path)
    text = (tmp_path / ALERT_FILENAME).read_text(encoding="utf-8")
    assert "no postings fetched" in text
    assert "Verify your slugs." in text


def test_the_file_channel_creates_the_directory(tmp_path: Path):
    target = tmp_path / "deep" / "nested"
    send(BAD, notify_config(tmp_path, {"file": True}), output_dir=target)
    assert (target / ALERT_FILENAME).exists()


def test_the_console_channel_writes_to_the_given_stream(tmp_path: Path):
    import io

    stream = io.StringIO()
    send(BAD, notify_config(tmp_path, {"console": True}), output_dir=tmp_path,
         stream=stream)
    assert "no postings fetched" in stream.getvalue()


def test_the_command_channel_runs_without_a_shell(tmp_path: Path):
    """The whole safety argument for this channel: job text reaches a
    subprocess, so it must never reach a shell."""
    runner = FakeRunner()
    cfg = notify_config(tmp_path, {"command": "notify-send 'Job Hunter'"})
    send(BAD, cfg, output_dir=tmp_path, runner=runner)

    call = runner.calls[0]
    assert call["shell"] is False
    assert call["argv"][0] == "notify-send"
    assert call["argv"][1] == "Job Hunter"       # shlex handled the quoting


def test_hostile_job_text_cannot_become_a_command(tmp_path: Path):
    """A posting titled "; rm -rf ~" must be an argument, never a command."""
    hostile = HealthReport([
        Alert("errors", "info", "1 error(s) during the run",
              'greenhouse/"; rm -rf ~; echo "pwned: HTTP 500'),
    ])
    runner = FakeRunner()
    send(hostile, notify_config(tmp_path, {"command": "notify-send"}),
         output_dir=tmp_path, runner=runner)

    call = runner.calls[0]
    assert call["shell"] is False
    assert call["argv"][0] == "notify-send"
    # The whole hostile string is one opaque argv entry, not parsed.
    assert any("rm -rf" in str(part) for part in call["argv"][1:])
    assert len(call["argv"]) == 2


def test_the_command_gets_the_message_three_ways(tmp_path: Path):
    """argv, stdin and the environment — so almost any notifier works with no
    wrapper script."""
    runner = FakeRunner()
    send(BAD, notify_config(tmp_path, {"command": "notify-send"}),
         output_dir=tmp_path, runner=runner)

    call = runner.calls[0]
    assert "no postings fetched" in call["argv"][-1]
    assert "no postings fetched" in call["input"]
    assert "no postings fetched" in call["env"]["JOBHUNTER_ALERT"]
    assert call["env"]["JOBHUNTER_ALERT_TITLE"]


def test_the_command_channel_has_a_timeout(tmp_path: Path):
    """A hung notifier must not hold the run open."""
    runner = FakeRunner()
    send(BAD, notify_config(tmp_path, {"command": "sleep"}), output_dir=tmp_path,
         runner=runner)
    assert runner.calls[0]["timeout"] > 0


@pytest.mark.parametrize(
    "channels",
    [{"command": ""}, {"command": "   "}, {"command": "notify-send 'unclosed"}],
)
def test_an_unusable_command_is_reported_not_run(tmp_path: Path, channels):
    runner = FakeRunner()
    results = send(BAD, notify_config(tmp_path, channels), output_dir=tmp_path,
                   runner=runner)
    assert runner.calls == []
    assert all(not r.sent for r in results if r.channel == "command")


def test_a_failing_command_is_reported_not_raised(tmp_path: Path):
    runner = FakeRunner(returncode=127)
    results = send(BAD, notify_config(tmp_path, {"command": "nope"}),
                   output_dir=tmp_path, runner=runner)
    result = next(r for r in results if r.channel == "command")
    assert result.sent is False
    assert "127" in result.detail


def test_a_command_that_cannot_start_is_reported_not_raised(tmp_path: Path):
    runner = FakeRunner(error=FileNotFoundError("no such binary"))
    results = send(BAD, notify_config(tmp_path, {"command": "nope"}),
                   output_dir=tmp_path, runner=runner)
    assert next(r for r in results if r.channel == "command").sent is False


# ==========================================================================
# email
# ==========================================================================


EMAIL = {"to": "ada@example.com", "from": "bot@example.com",
         "smtp_host": "smtp.example.com", "smtp_port": 587,
         "username": "bot@example.com", "starttls": True}


def test_the_email_channel_sends_one_message(tmp_path: Path):
    cfg = notify_config(tmp_path, {"email": dict(EMAIL)})
    results = send(BAD, cfg, output_dir=tmp_path, smtp_factory=smtp_factory(),
                   env={SMTP_PASSWORD_ENV: "hunter2"})

    assert next(r for r in results if r.channel == "email").sent is True
    client = FakeSMTP.instances[0]
    assert client.host == "smtp.example.com"
    assert client.started_tls is True
    assert client.login_args == ("bot@example.com", "hunter2")
    assert len(client.sent) == 1
    assert "no postings fetched" in client.sent[0]["Subject"]
    assert client.quit_called is True


def test_the_password_comes_from_the_environment_first(tmp_path: Path):
    """Same reason the Anthropic key does: a committed config must never be
    able to hold a live credential."""
    settings = dict(EMAIL, password="from-file")
    cfg = notify_config(tmp_path, {"email": settings})
    send(BAD, cfg, output_dir=tmp_path, smtp_factory=smtp_factory(),
         env={SMTP_PASSWORD_ENV: "from-env"})
    assert FakeSMTP.instances[0].login_args[1] == "from-env"


def test_email_without_a_recipient_or_host_is_reported(tmp_path: Path):
    for settings in ({"smtp_host": "smtp.example.com"}, {"to": "ada@example.com"}):
        cfg = notify_config(tmp_path, {"email": dict(settings, enabled=True)})
        results = send(BAD, cfg, output_dir=tmp_path, smtp_factory=smtp_factory())
        result = next(r for r in results if r.channel == "email")
        assert result.sent is False
        assert "smtp_host" in result.detail or "to" in result.detail


@pytest.mark.parametrize("stage", ["starttls", "login", "send"])
def test_an_smtp_failure_is_contained(tmp_path: Path, stage):
    cfg = notify_config(tmp_path, {"email": dict(EMAIL)})
    results = send(BAD, cfg, output_dir=tmp_path,
                   smtp_factory=smtp_factory(fail_on=stage), env={})
    assert next(r for r in results if r.channel == "email").sent is False


def test_starttls_can_be_turned_off(tmp_path: Path):
    cfg = notify_config(tmp_path, {"email": dict(EMAIL, starttls=False)})
    send(BAD, cfg, output_dir=tmp_path, smtp_factory=smtp_factory(), env={})
    assert FakeSMTP.instances[0].started_tls is False


# ==========================================================================
# isolation between channels
# ==========================================================================


def test_one_broken_channel_does_not_stop_the_others(tmp_path: Path):
    """The property that matters most: a notifier that breaks the run it was
    meant to warn about is worse than no notifier."""
    cfg = notify_config(tmp_path, {"command": "nope", "file": True})
    results = send(BAD, cfg, output_dir=tmp_path,
                   runner=FakeRunner(error=OSError("exec format error")))

    assert (tmp_path / ALERT_FILENAME).exists()
    assert {r.channel for r in results} == {"command", "file"}
    assert any(r.sent for r in results)


def test_send_never_raises_whatever_the_config_says(tmp_path: Path):
    for channels in ({"nonsense": True}, {"email": True}, {"command": True},
                     {"file": {"dir": "/proc/nope/nope"}}):
        cfg = notify_config(tmp_path, channels)
        assert isinstance(send(BAD, cfg, output_dir=tmp_path), list)


def test_send_survives_a_config_with_no_notify_block(tmp_path: Path):
    cfg = write_config(tmp_path, {"output": {"dir": str(tmp_path)}})
    assert isinstance(send(BAD, cfg, output_dir=tmp_path), list)


# ==========================================================================
# channel configuration shapes
# ==========================================================================


def test_enabled_channels_reads_the_shapes_people_write(tmp_path: Path):
    cfg = notify_config(tmp_path, {
        "console": True,
        "file": False,
        "command": "notify-send",          # a non-empty string means "on"
        "email": {"to": "ada@example.com"},
    })
    assert enabled_channels(cfg) == ["console", "command", "email"]


def test_an_empty_string_command_is_off(tmp_path: Path):
    assert "command" not in enabled_channels(notify_config(tmp_path, {"command": ""}))


def test_an_empty_email_block_is_off(tmp_path: Path):
    assert "email" not in enabled_channels(notify_config(tmp_path, {"email": {}}))


def test_channel_settings_normalises_a_bare_string(tmp_path: Path):
    cfg = notify_config(tmp_path, {"command": "notify-send"})
    assert channel_settings(cfg, "command")["command"] == "notify-send"


def test_channel_names_are_stable():
    # These are config keys; renaming one silently disables a user's alerts.
    assert CHANNELS == ("console", "file", "command", "email")


def test_clear_alert_file(tmp_path: Path):
    assert clear_alert_file(tmp_path) is False
    (tmp_path / ALERT_FILENAME).write_text("x", encoding="utf-8")
    assert clear_alert_file(tmp_path) is True
    assert not (tmp_path / ALERT_FILENAME).exists()
