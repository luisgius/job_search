"""Record what happened after the click — the part no job board tells the tracker.

Run ``python -m src.track <command>``:

  mark HANDLE STAGE [--on YYYY-MM-DD] [--note TEXT] [--force]
  add --company C --title T --location L --url U [--on YYYY-MM-DD] [--note TEXT]
  list [--open]
  report
  export [--out PATH]

HANDLE is a full job key, a unique key prefix (6+ characters) or the posting
URL (tracking parameters such as ?gh_src= ignored); the digest prints the
command with the first eight. A stage that means an application went out also
marks the job applied, so the pipeline never resurfaces it, never re-scores it
and never auto-applies to it or to a repost with the same company, title and
city.

`add` is for applications made outside the pipeline. A Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, Personio or Teamtailor URL is recorded under
the id that board's own copy is keyed by, so that copy is blocked however the
company or city were typed. Any other URL blocks auto-apply at that URL, and
other copies only when their company, title and city read as typed.

Exit codes: 0 done; 1 refused (config problem, no tracker yet, or an
application already closed — rerun with --force); 2 usage (bad arguments,
an unknown stage, a bad or future date, a handle matching zero or several jobs).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from . import digest
from .config import Config, ConfigError
from .db import (
    LABEL_STAGES, MIN_KEY_PREFIX, OUTCOME_STAGES, TERMINAL_APPLY_STATUSES,
    NON_SUBMITTING_STAGES, OutcomeClosedError, Tracker, normalize_stage,
)
from .models import ApplyStatus, Job, ensure_utc, utcnow

COMMANDS = ("mark", "add", "list", "report", "export")

#: One row per event, so a spreadsheet can pivot on stage or source.
EXPORT_COLUMNS: tuple[str, ...] = (
    "key", "company", "title", "location", "url", "source", "status",
    "stage", "occurred_on", "note", "recorded_at",
)

#: Where each ATS adapter (`src/sources/ats_boards.py`) finds the id it keys a
#: posting by in that posting's URL: (ats, host pattern, path pattern). With
#: it, `add` builds the key the board's own copy gets (`Job.key` hashes `ats`
#: and `ats_job_id`); without it, a company/title/location key no board makes.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ATS_URL_IDS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$"), re.compile(r"/jobs/(\d+)(?:/|$)")),
    ("lever", re.compile(r"(^|\.)jobs\.(?:eu\.)?lever\.co$"),
     re.compile(rf"^/[^/]+/({_UUID})(?:/|$)", re.IGNORECASE)),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$"),
     re.compile(rf"^/[^/]+/({_UUID})(?:/|$)", re.IGNORECASE)),
    ("workable", re.compile(r"^apply\.workable\.com$"), re.compile(r"/j/([A-Za-z0-9]+)(?:/|$)")),
    ("smartrecruiters", re.compile(r"^jobs\.smartrecruiters\.com$"),
     re.compile(r"^/[^/]+/(\d+)(?:-[^/]*)?(?:/|$)")),
    ("personio", re.compile(r"\.jobs\.personio\.(?:de|com)$"), re.compile(r"^/job/(\d+)(?:/|$)")),
    ("teamtailor", re.compile(r"\.teamtailor\.com$"), re.compile(r"^/jobs/(\d+)")),
)

#: A cell starting with one of these is a formula to Excel, Numbers and
#: LibreOffice — and company names and notes are untrusted text.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class _UsageError(Exception):
    """Exit 2: the command line, not the tracker, is the problem."""


def _day(raw: str) -> date:
    """argparse type: a real calendar day written YYYY-MM-DD."""
    text = str(raw).strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a YYYY-MM-DD date") from None


def _plain(value: Any) -> str:
    """Untrusted job text for a terminal: control characters become spaces,
    so a hostile title cannot smuggle escape sequences into the shell."""
    return "".join(" " if unicodedata.category(ch).startswith("C") else ch
                   for ch in str(value or ""))


def _cell(value: Any) -> str:
    """One CSV cell, defused: a leading apostrophe stops formula evaluation."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def ats_identity(url: str) -> tuple[str | None, str | None]:
    """`(ats, ats_job_id)` the board adapter gives the posting at `url`, or
    `(None, None)` when the URL does not carry one.

    `gh_jid=` is Greenhouse's job id on an employer's own careers page, and
    `token=` on its embedded form; both are the id the board API returns.
    """
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower().strip(".")
    query = dict(parse_qsl(parts.query))
    if query.get("gh_jid", "").isdigit():
        return "greenhouse", query["gh_jid"]
    for ats, host_re, path_re in _ATS_URL_IDS:
        if not host_re.search(host):
            continue
        match = path_re.search(parts.path)
        if match:
            ident = match.group(1)
            return ats, ident.lower() if ats in ("lever", "ashby") else ident
        if ats == "greenhouse" and query.get("token", "").isdigit():
            return ats, query["token"]
        break
    return None, None


def _job_line(row: Mapping[str, Any]) -> str:
    return (f"{row['key']}  {_plain(row.get('company'))} - {_plain(row.get('title'))}"
            f"  ({_plain(row.get('source'))})  {_plain(row.get('url'))}")


def _resolve(tracker: Tracker, handle: str) -> dict[str, Any]:
    """Exactly one job for `handle`, or a `_UsageError` listing the candidates."""
    matches = tracker.jobs_matching(handle)
    if len(matches) == 1:
        return matches[0]
    shown = _plain(handle)
    if not matches:
        short = len(str(handle).strip()) < MIN_KEY_PREFIX and "://" not in str(handle)
        hint = f" (a key prefix needs at least {MIN_KEY_PREFIX} characters)" if short else ""
        raise _UsageError(
            f"no job matches {shown!r}{hint}. `python -m src.track list` shows tracked "
            "jobs; `add` records an application the pipeline never saw."
        )
    lines = [f"{shown!r} matches {len(matches)} jobs; nothing was written. "
             "Use the full key:"]
    lines.extend(f"  {_job_line(row)}" for row in matches[:20])
    raise _UsageError("\n".join(lines))


def _record(tracker: Tracker, key: str, stage: str, args: argparse.Namespace,
            on: date, now: datetime, *, promise: str = "") -> None:
    """Write one event and say what it did, including what it implied.
    `promise` replaces the "marked applied" line, for a job the pipeline
    would recognise differently from one it fetched itself."""
    before = tracker.get_status(key)
    tracker.record_event(key, stage, on=on, note=args.note, force=args.force, now=now)
    unconfirmed = before == ApplyStatus.SUBMITTED_UNCONFIRMED.value
    if stage == "applied" and before in TERMINAL_APPLY_STATUSES and not unconfirmed:
        print("already recorded as applied; nothing new written")
        return
    if stage != "applied" or not unconfirmed:
        print(f"recorded {stage} on {on.isoformat()}")
    if stage in NON_SUBMITTING_STAGES:
        return
    if unconfirmed:
        print("receipt confirmed: status is now applied")
    elif before not in TERMINAL_APPLY_STATUSES:
        print(promise or "marked applied: the pipeline will not resurface, score or "
              "auto-apply to it, or to a repost with the same company, title and city")


def _mark(tracker: Tracker, args: argparse.Namespace, on: date, now: datetime) -> int:
    if not args.handle or not args.stage:
        raise _UsageError("mark needs a HANDLE and a STAGE, e.g. "
                          "`python -m src.track mark 3f9c2a1b interview`")
    try:
        stage = normalize_stage(args.stage)
    except ValueError as exc:
        raise _UsageError(str(exc)) from None
    job = _resolve(tracker, args.handle)
    print(f"{_plain(job['company'])} - {_plain(job['title'])} [{job['key']}]")
    _record(tracker, job["key"], stage, args, on, now)
    return 0


def _add(tracker: Tracker, args: argparse.Namespace, on: date, now: datetime) -> int:
    given = {"--company": args.company, "--title": args.title,
             "--location": args.location, "--url": args.url}
    missing = [flag for flag, value in given.items() if not str(value or "").strip()]
    if missing:
        raise _UsageError(f"add needs {', '.join(missing)}")
    url = args.url.strip()
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise _UsageError(f"--url must be an http(s) link, got {_plain(url)!r}")
    # source="manual" keeps it off every board's repost arithmetic, and its
    # dedupe_key (company + title + city) is what lets `has_applied_similar`
    # block the same role arriving from a board. With an ATS id from the URL
    # its key is the board copy's own, which no spelling can miss.
    ats, ats_job_id = ats_identity(url)
    job = Job(source="manual", company=args.company, title=args.title,
              url=url, location=args.location, ats=ats, ats_job_id=ats_job_id)
    if ats:
        found = tracker.get_job(job.key)
        existing = [found] if found is not None else []
    else:
        existing = tracker.jobs_matching(url)
        if len(existing) > 1:
            _resolve(tracker, url)  # raises, listing the candidates
    promise = ""
    if existing:
        key = existing[0]["key"]
        print(f"already tracked: {_job_line(existing[0])}")
    else:
        key = job.key
        print(f"{_plain(job.company)} - {_plain(job.title)} [{key}]")
        if not tracker.has_job(key):
            tracker.record_job(job, now=now)
        promise = (
            f"marked applied as {ats} job {_plain(ats_job_id)}: the pipeline will not "
            "resurface, score or auto-apply to that posting, or auto-apply to anything "
            "at this URL" if ats else
            "marked applied: the pipeline will not auto-apply to anything at this URL"
        ) + ("; another copy or a repost is recognised only when its company, title "
             "and city read as typed here")
    _record(tracker, key, "applied", args, on, now, promise=promise)
    return 0


def _list(tracker: Tracker, args: argparse.Namespace) -> int:
    rows = tracker.open_applications() if args.open else tracker.tracked_applications()
    if not rows:
        print("no open applications" if args.open else "nothing tracked yet")
        return 0
    print(f"{'key':<8}  {'stage':<14}  {'last':<10}  company - title (source)")
    for row in rows:
        label = f"  [{row['label']}]" if row.get("label") else ""
        print(f"{row['key'][:8]:<8}  {row['stage'] or '-':<14}  "
              f"{row['last_activity'] or '-':<10}  {_plain(row['company'])} - "
              f"{_plain(row['title'])} ({_plain(row['source'])}){label}")
    return 0


def funnel_lines(funnel: Mapping[str, Any] | None) -> list[str]:
    """`digest.application_funnel` as text — the digest's numbers, verbatim."""
    if funnel is None:
        return ["Applications to date: none recorded yet."]
    lines = [
        "Applications to date: " + " -> ".join(
            f"{step['label']} {step['value']}" for step in funnel["steps"]),
        f"Rejected or no response: {funnel['rejection_rate']}"
        + (" final outcomes" if funnel["final"] else ""),
    ]
    if funnel["by_source"]:
        lines.append("By source:")
        lines.extend(
            f"  {_plain(row['source'])}: applied {row['applied']}, interview "
            f"{row['interview']}, offer {row['offer']}, hired {row['hired']}"
            for row in funnel["by_source"]
        )
    lines.extend(f"unrecognised: {_plain(stage)}" for stage in funnel["unrecognised"])
    return lines


def _export(tracker: Tracker, args: argparse.Namespace, config: Config) -> int:
    path = Path(args.out) if args.out else config.output_dir / "applications.csv"
    rows = tracker.outcome_events()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            writer.writerow([_cell(row.get(column)) for column in EXPORT_COLUMNS])
    print(f"wrote {len(rows)} event(s) to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.track", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("handle", nargs="?", help="mark: job key, 6+ character key prefix, or posting URL")
    parser.add_argument("stage", nargs="?", help="mark: one of " + ", ".join(OUTCOME_STAGES + LABEL_STAGES))
    parser.add_argument("--on", type=_day, metavar="YYYY-MM-DD", help="when it happened (default: today)")
    parser.add_argument("--note", default="", help="free text kept with the event")
    parser.add_argument("--force", action="store_true", help="record even after a final stage")
    parser.add_argument("--company", help="add: employer name")
    parser.add_argument("--title", help="add: job title as advertised")
    parser.add_argument("--location", help="add: city, country — the city is what matches a repost")
    parser.add_argument("--url", help="add: the posting you applied through")
    parser.add_argument("--open", action="store_true", help="list: only applications still waiting on an outcome")
    parser.add_argument("--out", help="export: CSV path (default: <output.dir>/applications.csv)")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml (default: %(default)s)")
    parser.add_argument("--watchlist", default="watchlist.yaml", help="path to watchlist.yaml (default: %(default)s)")
    return parser


def main(argv: list[str] | None = None, *, today: date | None = None,
         now: datetime | None = None) -> int:
    """CLI entry point. `today` / `now` are the injectable clocks: `--on`
    defaults to and may not pass today's *local* date, the one Luis means."""
    args = build_parser().parse_args(argv)
    day = today or date.today()
    moment = ensure_utc(now) or utcnow()
    try:
        if args.on is not None and args.on > day:
            raise _UsageError(f"--on {args.on.isoformat()} is in the future (today is {day.isoformat()})")
        config = Config.load(args.config, args.watchlist)
        if args.command != "add" and not config.db_path.exists():
            print(f"no tracker at {config.db_path} yet — run the pipeline once, "
                  "or pass --config", file=sys.stderr)
            return 1
        with Tracker(config.db_path) as tracker:
            on = args.on or day
            if args.command == "mark":
                return _mark(tracker, args, on, moment)
            if args.command == "add":
                return _add(tracker, args, on, moment)
            if args.command == "list":
                return _list(tracker, args)
            if args.command == "report":
                print("\n".join(funnel_lines(digest.application_funnel(tracker.outcome_events()))))
                return 0
            return _export(tracker, args, config)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OutcomeClosedError as exc:
        print(f"refused: {exc} (rerun with --force)", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
