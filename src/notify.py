"""Getting an alert out of a cron job and in front of a human.

Four channels, all stdlib, none required:

  console  stderr — free, and the only one cron itself can act on
  file     output/ALERT.txt, deleted when the run is healthy
  command  an arbitrary shell command; this is the escape hatch that makes
           every notifier in the world work (osascript, notify-send, ntfy.sh,
           a Telegram curl) without this project taking a dependency
  email    SMTP via stdlib smtplib

`send()` never raises and never lets one channel's failure stop another: a
notifier that breaks the run it was meant to warn you about is worse than no
notifier at all.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .health import HealthReport
from .util import get_logger

logger = get_logger(__name__)

CHANNELS: tuple[str, ...] = ("console", "file", "command", "email")

ALERT_FILENAME = "ALERT.txt"

#: A notifier is a side-show; it must not hold the run open.
COMMAND_TIMEOUT_SECONDS = 30

#: Environment variable consulted for the SMTP password, so it never has to
#: live in config.yaml.
SMTP_PASSWORD_ENV = "JOBHUNTER_SMTP_PASSWORD"


@dataclass
class DeliveryResult:
    """What each channel did. Returned so `main` can log it and tests can see it."""

    channel: str
    sent: bool
    detail: str = ""


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    return getter(dotted, default) if callable(getter) else default


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------


def _send_console(title: str, body: str, settings: Mapping[str, Any],
                  *, stream: Any = None) -> DeliveryResult:
    """Print to stderr. In cron this is what lands in run.log — and, on most
    systems, in the mail cron sends when a job writes to stderr."""
    import sys

    target = stream if stream is not None else sys.stderr
    print(f"\n!! {title}\n{body}\n", file=target)
    return DeliveryResult("console", True)


def _send_file(title: str, body: str, settings: Mapping[str, Any],
               *, output_dir: Path | None = None) -> DeliveryResult:
    """Write `output/ALERT.txt`.

    A file is the one channel that survives a closed laptop, a missed
    notification and a cleared terminal — it is still there tomorrow.
    """
    directory = Path(settings.get("dir") or output_dir or "output")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ALERT_FILENAME
        path.write_text(f"{title}\n\n{body}\n", encoding="utf-8")
        return DeliveryResult("file", True, str(path))
    except OSError as exc:
        return DeliveryResult("file", False, str(exc))


def _send_command(title: str, body: str, settings: Mapping[str, Any],
                  *, runner: Callable[..., Any] | None = None) -> DeliveryResult:
    """Run a user-configured command, with the alert in its environment.

    The command is split with `shlex` and run WITHOUT a shell, so a job title
    containing backticks or a semicolon cannot become a command. Job text is
    attacker-controllable, and this is the one place in the project that runs
    a subprocess.

    The message reaches the command three ways, so almost any notifier works
    with no wrapper: appended as the final argv entry, on stdin, and as
    $JOBHUNTER_ALERT / $JOBHUNTER_ALERT_TITLE.
    """
    template = str(settings.get("command") or settings.get("value") or "").strip()
    if not template:
        return DeliveryResult("command", False, "no notify.channels.command configured")

    try:
        argv = shlex.split(template)
    except ValueError as exc:
        return DeliveryResult("command", False, f"could not parse the command: {exc}")
    if not argv:
        return DeliveryResult("command", False, "empty command")

    message = f"{title}\n\n{body}"
    env = dict(os.environ)
    env["JOBHUNTER_ALERT"] = message
    env["JOBHUNTER_ALERT_TITLE"] = title
    env["JOBHUNTER_ALERT_BODY"] = body

    run = runner or subprocess.run
    try:
        completed = run(
            argv + [message],
            input=message,
            env=env,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except Exception as exc:
        return DeliveryResult("command", False, f"{argv[0]}: {exc}")

    code = getattr(completed, "returncode", 0) or 0
    if code != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()[:200]
        return DeliveryResult("command", False, f"exit {code}: {stderr}")
    return DeliveryResult("command", True, argv[0])


def _send_email(title: str, body: str, settings: Mapping[str, Any],
                *, smtp_factory: Callable[..., Any] | None = None,
                env: Mapping[str, str] | None = None) -> DeliveryResult:
    """Send one plain-text email over SMTP.

    The password comes from `JOBHUNTER_SMTP_PASSWORD` in preference to the
    config file, for the same reason the Anthropic key does.
    """
    from email.message import EmailMessage

    environment = os.environ if env is None else env
    to = str(settings.get("to") or "").strip()
    host = str(settings.get("smtp_host") or "").strip()
    if not to or not host:
        return DeliveryResult("email", False,
                              "notify.channels.email needs at least `to` and `smtp_host`")

    sender = str(settings.get("from") or to).strip()
    port = int(settings.get("smtp_port") or 587)
    username = str(settings.get("username") or "").strip()
    password = str(environment.get(SMTP_PASSWORD_ENV)
                   or settings.get("password") or "")
    starttls = bool(settings.get("starttls", True))

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = to
    message.set_content(body)

    try:
        if smtp_factory is not None:
            client = smtp_factory(host, port)
        else:
            import smtplib

            client = smtplib.SMTP(host, port, timeout=30)
        try:
            if starttls:
                client.starttls()
            if username:
                client.login(username, password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # a failed quit is not a failed send
                pass
    except Exception as exc:
        return DeliveryResult("email", False, f"{host}:{port}: {exc}")
    return DeliveryResult("email", True, to)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def enabled_channels(config: Any) -> list[str]:
    """Channel names switched on in `notify.channels`, in a stable order.

    A channel is on when its value is `true` or a non-empty mapping/string —
    so `command: "notify-send Job Hunter"` needs no separate enable flag.
    """
    settings = _cfg(config, "notify.channels", {}) or {}
    if not isinstance(settings, Mapping):
        return []
    active: list[str] = []
    for name in CHANNELS:
        value = settings.get(name)
        if value is True:
            active.append(name)
        elif isinstance(value, str) and value.strip():
            active.append(name)
        elif isinstance(value, Mapping) and value.get("enabled", True) and value:
            active.append(name)
    return active


def channel_settings(config: Any, name: str) -> dict[str, Any]:
    """Normalise one channel's config into a mapping.

    `command: "notify-send"` and `command: {command: "notify-send"}` are the
    same thing; people write both.
    """
    value = (_cfg(config, "notify.channels", {}) or {}).get(name) \
        if isinstance(_cfg(config, "notify.channels", {}), Mapping) else None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return {"command": value, "value": value}
    return {}


def send(
    report: HealthReport,
    config: Any,
    *,
    output_dir: Path | None = None,
    stream: Any = None,
    runner: Callable[..., Any] | None = None,
    smtp_factory: Callable[..., Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[DeliveryResult]:
    """Deliver `report` on every enabled channel. Never raises.

    A healthy report sends nothing and clears any stale `ALERT.txt`, so the
    presence of that file always means "the last run had a problem".
    """
    if not bool(_cfg(config, "notify.enabled", True)):
        logger.debug("notify.enabled is false — not sending %d alert(s)",
                     len(report.alerts))
        return []

    directory = Path(output_dir) if output_dir is not None else Path(
        str(_cfg(config, "output.dir", "output"))
    )

    if report.ok:
        clear_alert_file(directory)
        return []

    title = report.title()
    body = report.summary()
    results: list[DeliveryResult] = []

    for name in enabled_channels(config):
        settings = channel_settings(config, name)
        try:
            if name == "console":
                result = _send_console(title, body, settings, stream=stream)
            elif name == "file":
                result = _send_file(title, body, settings, output_dir=directory)
            elif name == "command":
                result = _send_command(title, body, settings, runner=runner)
            elif name == "email":
                result = _send_email(title, body, settings,
                                     smtp_factory=smtp_factory, env=env)
            else:
                result = DeliveryResult(name, False, "unknown channel")
        except Exception as exc:  # a broken notifier must not break the run
            result = DeliveryResult(name, False, f"unexpected: {exc}")
        if not result.sent:
            logger.warning("notify: %s failed — %s", result.channel, result.detail)
        results.append(result)

    if results and not any(r.sent for r in results):
        logger.error("notify: every channel failed; the alert was: %s", title)
    return results


def clear_alert_file(output_dir: Path | str) -> bool:
    """Remove a stale `ALERT.txt`. True when one was actually there."""
    path = Path(output_dir) / ALERT_FILENAME
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        logger.debug("could not clear %s: %s", path, exc)
    return False
