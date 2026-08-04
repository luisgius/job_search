"""CLI entry point — the only module that knows the whole pipeline.

Every other module is a stage that minds its own business; this one orders
them, carries a single `RunStats` through, and makes sure a failure anywhere
degrades the run instead of ending it. The shape is deliberately flat:

    fetch -> dedupe -> filter -> tracker gate -> score -> tailor -> PDF
          -> auto-apply -> persist -> digest

Two rules shape the code below.

**One clock.** `run_pipeline` computes `now` once and every time-dependent
call downstream receives it, so freshness, the tracker window, the digest
filename and the recorded timestamps all agree even if the run takes an hour.

**Every stage is guarded.** A dead job board, an API outage or a template bug
costs its stage and nothing else — the errors land in `stats.errors` and the
digest reports them. The only fatal condition is a config problem (exit 1),
because running without a CV or an API key produces garbage, not partial
results.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import digest, filters, pdf, scoring, tailor
from .apply import autoapply
from .config import Config, ConfigError
from .db import Tracker
from .models import ApplyStatus, Job, RunStats, ScoredJob, ensure_utc, utcnow
from .sources import adzuna, ats_boards, linkedin_email
from .util import get_logger, open_in_browser, setup_logging

logger = get_logger(__name__)

#: Every source name the config knows about, in fetch order.
SOURCE_NAMES: tuple[str, ...] = ("greenhouse", "lever", "adzuna", "linkedin_email")

#: The two sources `ats_boards.fetch` serves in a single call.
BOARD_SOURCES: frozenset[str] = frozenset({"greenhouse", "lever"})

#: A CV shorter than this cannot produce a meaningful score or a tailored
#: document. Treated as a config error rather than an empty run, because the
#: failure is silent otherwise: every job scores badly for the wrong reason.
MIN_CV_CHARS = 200

#: Statuses the apply stage produces, i.e. "we touched a form for this one".
_APPLY_STATUSES: frozenset[ApplyStatus] = frozenset(
    {ApplyStatus.APPLIED, ApplyStatus.DRY_RUN, ApplyStatus.APPLY_FAILED}
)

EPILOG = """\
examples:
  python -m src.main                  the normal daily run: fetch, score,
                                      tailor, apply, then open the digest
  python -m src.main --no-browser     the same run with nothing to click —
                                      what cron should call

cron (weekdays at 08:00 — `crontab -e`):
  0 8 * * 1-5 cd /path/to/job_search && .venv/bin/python -m src.main \\
      --no-browser >> output/cron.log 2>&1

first run, before spending anything:
  python -m src.main --validate-only
  python -m src.main --limit 3 --skip-apply
"""


# --------------------------------------------------------------------------
# config access (tolerates a Config or a plain nested dict)
# --------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a `Config` *or* a bare nested dict."""
    getter = getattr(config, "get", None)
    if callable(getter) and not isinstance(config, dict):
        return getter(dotted, default)
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _int(value: Any, default: int) -> int:
    """Coerce a config value to int, falling back rather than raising."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source_enabled(config: Any, name: str) -> bool:
    checker = getattr(config, "source_enabled", None)
    if callable(checker):
        return bool(checker(name))
    return bool(_cfg(config, f"sources.{name}", False))


def _status_of(scored: ScoredJob) -> str:
    """`ScoredJob.status` as its string value, tolerating a raw string."""
    status = getattr(scored, "status", None)
    return status.value if isinstance(status, ApplyStatus) else str(status or "")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _job_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return value


def build_parser() -> argparse.ArgumentParser:
    """The `python -m src.main` command line.

    `--dry-run` / `--no-dry-run` share one destination that defaults to
    `None`, so "the user said nothing" stays distinguishable from "the user
    said false" — only then can the config value survive.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description=(
            "Daily EU job pipeline: fetch fresh postings, filter them, "
            "LLM-score them against your CV, tailor a CV + cover letter per "
            "match, optionally fill simple Greenhouse/Lever forms, and write "
            "an HTML digest."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="path to config.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--watchlist", default="watchlist.yaml",
        help="path to watchlist.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="never open the digest (use this in cron)",
    )

    dry = parser.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="fill application forms and screenshot them, but never submit "
             "(overrides apply.dry_run)",
    )
    dry.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false", default=None,
        help="really submit applications (overrides apply.dry_run — read the "
             "README section on auto-apply first)",
    )

    parser.add_argument(
        "--skip-apply", action="store_true",
        help="skip the auto-apply stage entirely; everything lands in the digest",
    )
    parser.add_argument(
        "--source", dest="sources", action="append", metavar="NAME",
        choices=list(SOURCE_NAMES),
        help="restrict the run to this source; repeatable. Narrows what the "
             "config already enables, never enables anything new. "
             f"one of: {', '.join(SOURCE_NAMES)}",
    )
    parser.add_argument(
        "--limit", type=_job_limit, metavar="N",
        help="score at most N jobs this run — the cheap smoke test",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="check the config, print every problem, and exit without "
             "fetching or spending anything",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG logging",
    )
    return parser


def apply_cli_overrides(config: Config, args: argparse.Namespace) -> None:
    """Write the CLI flags into `config.data`, in place.

    Downstream stages read the config and nothing else, so a flag that is not
    reflected here is a flag half of the pipeline cannot see — `--no-browser`
    would be honoured but the digest's own "auto-apply: on" summary would
    still lie about `--skip-apply`, for example.

    `--source` narrows: it disables everything not named, and never enables a
    source the config has switched off. That keeps `--source adzuna` from
    quietly turning on a paid API the user disabled on purpose.
    """
    data = config.data

    if args.dry_run is not None:
        data.setdefault("apply", {})["dry_run"] = bool(args.dry_run)
    if getattr(args, "skip_apply", False):
        # Off for this run only (nothing writes config.yaml back to disk).
        data.setdefault("apply", {})["enabled"] = False
    if getattr(args, "no_browser", False):
        data.setdefault("output", {})["open_browser"] = False
    if getattr(args, "verbose", False):
        data.setdefault("logging", {})["level"] = "DEBUG"

    wanted = {str(name).strip().lower() for name in (args.sources or [])}
    if wanted:
        sources = data.setdefault("sources", {})
        for name in SOURCE_NAMES:
            if name not in wanted:
                sources[name] = False


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------


def _active_sources(config: Any, sources: Iterable[str] | None) -> set[str]:
    """Source names that are both enabled in config and requested on the CLI."""
    wanted = (
        None if sources is None
        else {str(name).strip().lower() for name in sources if str(name).strip()}
    )
    return {
        name for name in SOURCE_NAMES
        if _source_enabled(config, name) and (wanted is None or name in wanted)
    }


def _fetch_all(config: Any, active: set[str], stats: RunStats) -> list[Job]:
    """Run every active source, isolating each one's failures.

    Sources already promise not to raise, but they are the part of this system
    most exposed to the internet, so they get a belt-and-braces try/except
    too — one board's HTML surprise must not cost the other three sources.
    """
    jobs: list[Job] = []

    # `ats_boards.fetch` serves greenhouse *and* lever in one call and checks
    # `config.source_enabled` itself, so it is called once and its output is
    # filtered afterwards. The CLI path also narrows `sources.*` in the config
    # (see `apply_cli_overrides`), so `--source greenhouse` skips the Lever
    # request entirely rather than fetching and discarding it.
    boards = active & BOARD_SOURCES
    if boards:
        found = _safe_fetch("greenhouse/lever", ats_boards.fetch, config, stats)
        jobs.extend(job for job in found if (job.source or "").lower() in boards)

    if "adzuna" in active:
        jobs.extend(_safe_fetch("adzuna", adzuna.fetch, config, stats))

    if "linkedin_email" in active:
        jobs.extend(_safe_fetch("linkedin_email", linkedin_email.fetch, config, stats))

    # Seed a zero for every active source so the digest can tell "this board
    # had nothing today" apart from "this board was never asked".
    for name in sorted(active):
        stats.source_counts.setdefault(name, 0)
    for job in jobs:
        name = (job.source or "unknown").lower()
        stats.source_counts[name] = stats.source_counts.get(name, 0) + 1

    stats.fetched = len(jobs)
    logger.info("fetched %d postings from %s", len(jobs), ", ".join(sorted(active)) or "nothing")
    return jobs


def _safe_fetch(label: str, fn: Any, config: Any, stats: RunStats) -> list[Job]:
    """Call one source's `fetch(config, errors=)`, absorbing anything it throws."""
    try:
        result = fn(config, errors=stats.errors)
    except Exception as exc:
        message = f"{label} source failed: {exc}"
        logger.warning("%s", message)
        stats.errors.append(message)
        return []
    return list(result or [])


def _gate_on_tracker(
    kept: list[Job],
    rejected: list[tuple[Job, str]],
    tracker: Any,
    config: Any,
    stats: RunStats,
    now: datetime,
) -> list[Job]:
    """Record what we saw and drop what the user has already been shown.

    Rejected jobs get an explicit `FILTERED` row — *including* the ones
    rejected for being stale. It looks wrong (a stale posting could be
    re-posted tomorrow) but it is not: a re-post gets a new ATS id and
    therefore a new `Job.key`, while the *same* posting only ever gets older,
    so without the row we would re-fetch and re-evaluate the identical stale
    job every single morning, forever. The row costs one INSERT and saves that
    loop.

    `record_job` has to come first in both branches: `applications.key` is a
    foreign key onto `jobs.key`.
    """
    if tracker is None:
        return list(kept)

    skip_days = _int(_cfg(config, "db.skip_seen_days", 30), 30)
    surviving: list[Job] = []

    for job in kept:
        try:
            tracker.record_job(job, now=now)
            if tracker.should_surface(job.key, within_days=skip_days, now=now):
                surviving.append(job)
            else:
                stats.already_seen += 1
        except Exception as exc:  # one bad row must not cost the whole batch
            logger.warning("tracker failed on %s: %s", job.label, exc)
            surviving.append(job)  # when in doubt, show it rather than lose it

    for job, reason in rejected:
        try:
            tracker.record_job(job, now=now)
            tracker.record_status(
                job.key, ApplyStatus.FILTERED, detail=reason, now=now
            )
        except Exception as exc:
            logger.debug("could not record filtered %s: %s", job.key, exc)

    logger.info(
        "tracker: %d new, %d already handled within %d days",
        len(surviving), stats.already_seen, skip_days,
    )
    return surviving


def _read_cv(config: Any) -> str:
    """Load the base CV, or raise `ConfigError`.

    Fatal on purpose: scoring compares a job against this text and tailoring
    rewrites it. Without it the run would still "work" and hand back a page of
    confidently wrong scores.
    """
    resolver = getattr(config, "cv_path", None)
    path = Path(resolver) if resolver is not None else Path(
        str(_cfg(config, "cv.path", "cv/base_cv.md"))
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the CV at {path}: {exc}") from exc
    if len(text.strip()) < MIN_CV_CHARS:
        raise ConfigError(
            f"the CV at {path} is {len(text.strip())} characters — that is not "
            f"a CV. Paste yours in (at least {MIN_CV_CHARS} characters) before "
            "spending any API budget."
        )
    return text


def _render_pdfs(scored_jobs: list[ScoredJob], stats: RunStats) -> int:
    """Turn tailored markdown into PDFs, when the user's hook exists.

    No hook is a supported state, not an error, so it is reported once per run
    rather than once per job — and `apply.require_pdf` then routes every match
    to the digest instead of auto-applying with no CV attached.
    """
    candidates = [
        item for item in scored_jobs
        if item.artifacts and item.artifacts.dir and (item.tailored_cv_md or "").strip()
    ]
    if not candidates:
        return 0
    if not pdf.available():
        logger.info(
            "no PDF hook (%s) — %d tailored CV(s) stay as markdown and every "
            "match goes to the digest", pdf.HOOK_PATH, len(candidates),
        )
        return 0

    rendered = 0
    for item in candidates:
        directory = Path(item.artifacts.dir)
        cv_pdf = pdf.render_if_available(item.tailored_cv_md or "", directory / "cv.pdf")
        if cv_pdf:
            item.artifacts.cv_pdf = cv_pdf
            rendered += 1
        else:
            stats.errors.append(f"PDF render failed for {item.job.label}")
        cover_md = (item.cover_letter_md or "").strip()
        if cover_md:
            # A missing cover-letter PDF is cosmetic — nothing gates on it, so
            # it is not worth an entry in the run's error list.
            cover_pdf = pdf.render_if_available(cover_md, directory / "cover_letter.pdf")
            if cover_pdf:
                item.artifacts.cover_pdf = cover_pdf
    logger.info("rendered %d of %d CV PDFs", rendered, len(candidates))
    return rendered


def _count_outcomes(scored_jobs: list[ScoredJob], stats: RunStats) -> None:
    """Refresh the terminal counters from the jobs' final statuses."""
    stats.auto_applied = sum(
        1 for s in scored_jobs if _status_of(s) == ApplyStatus.APPLIED.value
    )
    stats.dry_run = sum(
        1 for s in scored_jobs if _status_of(s) == ApplyStatus.DRY_RUN.value
    )
    stats.apply_failed = sum(
        1 for s in scored_jobs if _status_of(s) == ApplyStatus.APPLY_FAILED.value
    )
    stats.digest_items = sum(
        1 for s in scored_jobs if _status_of(s) == ApplyStatus.DIGEST.value
    )
    stats.tailored = sum(
        1 for s in scored_jobs if s.artifacts and s.artifacts.cv_md
    )


def _persist(scored_jobs: list[ScoredJob], tracker: Any, now: datetime) -> None:
    """Write each job's final status to the tracker.

    `method` is deliberately left empty: the apply stage has already written
    the specific one ("greenhouse", "lever"), and `Tracker.record_status`
    keeps an existing method when the new one is blank. An `applied` row is
    likewise never downgraded, so re-recording here is safe.
    """
    if tracker is None:
        return
    for item in scored_jobs:
        try:
            tracker.record_job(item.job, now=now)
            tracker.record_status(
                item.key,
                item.status,
                detail=item.status_detail or "",
                score=item.score_value,
                artifacts_dir=(item.artifacts.dir if item.artifacts else None),
                now=now,
            )
        except Exception as exc:
            logger.warning("could not record %s for %s: %s",
                           _status_of(item), item.job.label, exc)


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------


def run_pipeline(
    config: Config,
    *,
    tracker: Any = None,
    now: datetime | None = None,
    llm_client: Any = None,
    sources: Iterable[str] | None = None,
    limit: int | None = None,
    skip_apply: bool = False,
) -> tuple[list[ScoredJob], RunStats]:
    """Run every stage and return `(scored_jobs, stats)`.

    Injectable seams, all of them optional: `tracker=` (an in-memory
    `Tracker` works), `llm_client=` (threaded into scoring *and* tailoring as
    `client=`), `now=` and `sources=`. With those four supplied the whole
    pipeline runs offline, which is exactly how it is tested.

    Two counters live on `stats` as dynamic attributes rather than
    `RunStats` fields, because `RunStats` is a foundation file this module may
    not edit:

      * `stats.filter_counts` — reason -> n from `filters.apply_filters`.
        `digest.build_context` already looks for it via `getattr`, so it
        reaches the page without touching `RunStats.to_dict()`.
      * `stats.digest_path` — where the digest was written, for `main` to print.

    Only a config problem (`ConfigError`, raised by the CV check) leaves this
    function. Everything else is caught, counted in `stats.errors` and
    rendered in the digest.
    """
    moment = ensure_utc(now) or utcnow()
    stats = RunStats()
    # Set before anything can fail, so the digest never meets a missing attribute.
    stats.filter_counts = {}       # type: ignore[attr-defined]
    stats.digest_path = None       # type: ignore[attr-defined]

    # -- 1. fetch ---------------------------------------------------------
    active = _active_sources(config, sources)
    if not active:
        message = "no sources are active — nothing to fetch"
        logger.warning("%s", message)
        stats.errors.append(message)
    jobs = _fetch_all(config, active, stats)

    # -- 2. dedupe --------------------------------------------------------
    try:
        jobs = filters.dedupe(jobs)
    except Exception as exc:
        logger.warning("dedupe failed (%s) — continuing with the raw list", exc)
        stats.errors.append(f"dedupe failed: {exc}")
    stats.after_dedupe = len(jobs)

    # -- 3. hard filters --------------------------------------------------
    kept: list[Job] = jobs
    rejected: list[tuple[Job, str]] = []
    try:
        result = filters.apply_filters(jobs, config, now=moment)
        kept, rejected = result.kept, result.rejected
        stats.filter_counts = dict(result.counts)  # type: ignore[attr-defined]
    except Exception as exc:
        # Filters are pure, so this means a bug rather than bad weather. Keep
        # everything: an unfiltered digest is recoverable, a silent empty one
        # is not.
        logger.warning("filtering failed (%s) — keeping every job", exc)
        stats.errors.append(f"filtering failed: {exc}")
    stats.after_filters = len(kept)

    # -- 4. tracker gate --------------------------------------------------
    fresh = _gate_on_tracker(kept, rejected, tracker, config, stats, moment)

    # -- 5. the CV (fatal when missing) -----------------------------------
    cv_markdown = _read_cv(config)

    # -- 6. scoring -------------------------------------------------------
    if limit is not None and 0 <= limit < len(fresh):
        logger.info("--limit %d: scoring %d of %d jobs", limit, limit, len(fresh))
        fresh = fresh[:limit]

    scored_jobs: list[ScoredJob] = []
    try:
        scored_jobs = scoring.score_jobs(
            fresh, cv_markdown, config, client=llm_client, errors=stats.errors
        )
    except Exception as exc:
        logger.warning("scoring failed: %s", exc)
        stats.errors.append(f"scoring failed: {exc}")
    stats.scored = len(scored_jobs)
    # A "match" is anything the human now has to deal with: at or above the
    # threshold, plus anything the scorer could not judge (those reach the
    # digest unscored rather than being dropped). The apply stage only ever
    # moves jobs *within* this set, so `matches` stays the sum of the
    # auto-applied / dry-run / needs-a-click counters below.
    stats.matches = sum(
        1 for s in scored_jobs if _status_of(s) == ApplyStatus.DIGEST.value
    )

    # -- 7. tailoring -----------------------------------------------------
    if scored_jobs:
        try:
            scored_jobs = tailor.tailor_jobs(
                scored_jobs, cv_markdown, config, client=llm_client, errors=stats.errors
            )
        except Exception as exc:
            logger.warning("tailoring failed: %s", exc)
            stats.errors.append(f"tailoring failed: {exc}")

    # -- 8. PDFs ----------------------------------------------------------
    try:
        _render_pdfs(scored_jobs, stats)
    except Exception as exc:
        logger.warning("PDF rendering failed: %s", exc)
        stats.errors.append(f"PDF rendering failed: {exc}")

    # -- 9. auto-apply ----------------------------------------------------
    if skip_apply:
        logger.info("--skip-apply: every match goes to the digest")
    elif not bool(_cfg(config, "apply.enabled", True)):
        logger.info("apply.enabled is false — every match goes to the digest")
    elif scored_jobs:
        try:
            scored_jobs = autoapply.run(scored_jobs, config, tracker=tracker)
        except Exception as exc:
            logger.warning("auto-apply failed: %s", exc)
            stats.errors.append(f"auto-apply failed: {exc}")

    # -- 10. persist ------------------------------------------------------
    _count_outcomes(scored_jobs, stats)
    _persist(scored_jobs, tracker, moment)

    # -- 11. digest -------------------------------------------------------
    try:
        path = digest.write_digest(scored_jobs, stats, config, now=moment)
        stats.digest_path = str(path)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("could not write the digest: %s", exc)
        stats.errors.append(f"digest failed: {exc}")

    return scored_jobs, stats


# --------------------------------------------------------------------------
# summary printing
# --------------------------------------------------------------------------


def _arrow() -> str:
    """"→" where the terminal can encode it, "->" where it cannot."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "→".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return "->"
    return "→"


def format_summary(stats: RunStats) -> str:
    """The three-line human summary `main` prints when a run finishes."""
    glyph = _arrow()
    arrow = f" {glyph} "
    dot = " · " if glyph == "→" else " | "
    new = max(0, stats.after_filters - stats.already_seen)
    funnel = arrow.join([
        f"fetched {stats.fetched}",
        f"deduped {stats.after_dedupe}",
        f"filtered {stats.after_filters}",
        f"new {new}",
        f"scored {stats.scored}",
        f"matched {stats.matches}",
    ])
    outcomes = dot.join([
        f"auto-applied {stats.auto_applied}",
        f"dry-run {stats.dry_run}",
        f"needs your click {stats.digest_items}",
    ])
    if stats.apply_failed:
        outcomes += f"{dot}apply failed {stats.apply_failed}"

    lines = [funnel, outcomes]
    path = getattr(stats, "digest_path", None)
    lines.append(f"digest: {path}" if path else "digest: not written — see the errors above")
    if stats.errors:
        lines.append(f"{len(stats.errors)} error(s) this run — details in the digest")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def _print_problems(problems: Sequence[str], config_path: str) -> None:
    print(f"{len(problems)} problem(s) in {config_path}:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)


def _run_cli(args: argparse.Namespace) -> int:
    """The body of `main`, minus the top-level exception handling."""
    config = Config.load(args.config, args.watchlist)
    apply_cli_overrides(config, args)
    # Re-apply now that the file (and -v) have had their say.
    setup_logging(str(_cfg(config, "logging.level", "INFO") or "INFO"))

    problems = config.validate(require_llm=True)
    if problems:
        _print_problems(problems, str(args.config))
        return 1
    if args.validate_only:
        print(f"config OK — {args.config} and {args.watchlist} are usable")
        return 0

    with Tracker(config.db_path) as tracker:
        now = utcnow()
        run_id = tracker.start_run(now=now)
        scored_jobs, stats = run_pipeline(
            config,
            tracker=tracker,
            now=now,
            sources=args.sources,
            limit=args.limit,
            skip_apply=bool(args.skip_apply),
        )
        tracker.finish_run(run_id, stats.to_dict(), now=now)

    print(format_summary(stats))

    path = getattr(stats, "digest_path", None)
    if path and not args.no_browser and bool(_cfg(config, "output.open_browser", True)):
        open_in_browser(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. 0 ok, 1 config invalid, 2 unexpected, 130 interrupted."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # Logging has to exist before the config is read, or a broken config.yaml
    # fails in silence. `setup_logging` is idempotent and gets called again
    # once the file's own level is known.
    setup_logging("DEBUG" if args.verbose else "INFO")

    try:
        return _run_cli(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("the run failed unexpectedly")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
