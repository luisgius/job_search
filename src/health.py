"""Did this run actually work?

The failure this module exists for is the quiet one. A morning where every
board returned 404, or where cron never fired because the laptop was shut,
produces exactly what a genuinely quiet Tuesday produces: a digest with
nothing in it, or no digest at all. Both look like "no jobs today", and a
user who reads that for a week concludes the market is dead rather than that
their watchlist rotted.

So `assess()` compares this run against the *history* in the tracker's `runs`
table and returns explicit alerts. It is pure apart from reading that table:
same run, same history, same alerts.

Deliberately NOT alerted on:
  * a low job count — that is a real quiet day and crying wolf trains you to
    ignore the alert that matters;
  * a single scoring failure — the job still reaches the digest, which says so;
  * the first ever run, which has no baseline to be suspicious about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import RunStats, ensure_utc, utcnow
from .util import get_logger

logger = get_logger(__name__)

#: Alert kinds, in the order they are reported. These strings are config
#: values (`notify.on`) and appear in the digest, so they are part of the
#: contract: add freely, never rename.
ALERT_KINDS: tuple[str, ...] = (
    "no_digest",
    "missed_run",
    "no_jobs",
    "source_zero",
    "all_sources_failed",
    "errors",
)

#: How many previous runs form the baseline for "this source used to work".
BASELINE_RUNS = 5

#: A source has to have produced at least this many jobs, on average, before
#: its silence today counts as news. One flaky posting a week is not a signal.
BASELINE_MIN_AVERAGE = 3.0

#: Hours after which a missing run is worth mentioning. 36 rather than 24 so a
#: weekday cron that slips a few hours does not nag, but a skipped day does.
MISSED_RUN_HOURS = 36.0

#: Extra grace per weekend day inside the gap. A weekday cron is *supposed*
#: to be silent on Saturday and Sunday, so Friday -> Monday (72h) must stay
#: quiet — while Saturday -> Tuesday (also 72h, but only one weekend day in
#: between) really did miss Monday and should not.
WEEKEND_GRACE_PER_DAY_HOURS = 24.0

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Alert:
    """One thing worth telling the user about this run."""

    kind: str
    severity: str          # "critical" | "warning" | "info"
    message: str           # one line, reads on a phone notification
    detail: str = ""       # optional second line with the specifics

    def __str__(self) -> str:
        return f"{self.message}\n{self.detail}" if self.detail else self.message


@dataclass
class HealthReport:
    """The verdict on one run."""

    alerts: list[Alert] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.alerts

    @property
    def worst(self) -> str:
        if not self.alerts:
            return "ok"
        return min((a.severity for a in self.alerts),
                   key=lambda s: SEVERITY_ORDER.get(s, 9))

    def kinds(self) -> list[str]:
        return [a.kind for a in self.alerts]

    def summary(self) -> str:
        """The notification body: one line per alert, worst first."""
        ordered = sorted(self.alerts,
                         key=lambda a: SEVERITY_ORDER.get(a.severity, 9))
        return "\n".join(str(a) for a in ordered)

    def title(self) -> str:
        """The notification subject line."""
        if self.ok:
            return "Job Hunter: run OK"
        first = sorted(self.alerts, key=lambda a: SEVERITY_ORDER.get(a.severity, 9))[0]
        extra = f" (+{len(self.alerts) - 1} more)" if len(self.alerts) > 1 else ""
        return f"Job Hunter: {first.message}{extra}"


# --------------------------------------------------------------------------
# baselines from the runs table
# --------------------------------------------------------------------------


def _run_stats(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("stats_json") or "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def source_baselines(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Average jobs per source across previous *completed* runs.

    Unfinished runs are skipped: a run that crashed mid-fetch would otherwise
    drag every baseline toward zero and permanently silence the alert.
    """
    totals: dict[str, float] = {}
    counted = 0
    for row in rows:
        if not row.get("finished_at"):
            continue
        stats = _run_stats(row)
        counts = stats.get("source_counts")
        if not isinstance(counts, Mapping):
            continue
        counted += 1
        for name, value in counts.items():
            try:
                totals[str(name)] = totals.get(str(name), 0.0) + float(value)
            except (TypeError, ValueError):
                continue
    if not counted:
        return {}
    return {name: total / counted for name, total in totals.items()}


def last_finished_run(rows: Iterable[Mapping[str, Any]]) -> datetime | None:
    """When a run last completed, ignoring the one in progress."""
    for row in rows:
        if not row.get("finished_at"):
            continue
        try:
            return ensure_utc(datetime.fromisoformat(str(row["finished_at"])))
        except (TypeError, ValueError):
            continue
    return None


def weekend_days_between(previous: datetime, now: datetime) -> int:
    """Whole Saturdays/Sundays strictly between two moments.

    "Strictly between" is what makes the heuristic discriminate: a Friday ->
    Monday gap contains both weekend days, while a Saturday -> Tuesday gap of
    the same length contains only Sunday, because Saturday is the day the run
    happened rather than a day it was excused from.
    """
    count = 0
    day = (previous + timedelta(days=1)).date()
    end = now.date()
    while day < end:
        if day.weekday() >= 5:
            count += 1
        day += timedelta(days=1)
    return count


def _gap_allows(previous: datetime, now: datetime) -> float:
    """Hours we tolerate before calling a gap a missed run."""
    excused = weekend_days_between(previous, now) * WEEKEND_GRACE_PER_DAY_HOURS
    return MISSED_RUN_HOURS + excused


# --------------------------------------------------------------------------
# the assessment
# --------------------------------------------------------------------------


def assess(
    stats: RunStats | Any,
    *,
    previous_runs: Sequence[Mapping[str, Any]] | None = None,
    digest_path: str | None = None,
    now: datetime | None = None,
    active_sources: Iterable[str] | None = None,
) -> HealthReport:
    """Decide what, if anything, is worth telling the user about this run.

    `previous_runs` is `Tracker.recent_runs()` — newest first, excluding this
    run. Pass `[]` (or nothing) on a first run and only the alerts that need
    no history will fire.
    """
    moment = ensure_utc(now) or utcnow()
    history = list(previous_runs or [])
    report = HealthReport()

    fetched = int(getattr(stats, "fetched", 0) or 0)
    errors = list(getattr(stats, "errors", None) or [])
    source_counts = dict(getattr(stats, "source_counts", None) or {})
    active = {str(s) for s in (active_sources or source_counts.keys())}

    # -- the run produced nothing to read ---------------------------------
    if not digest_path:
        report.alerts.append(Alert(
            "no_digest", "critical",
            "the run finished without writing a digest",
            "Nothing to read this morning. Check output/run.log for the traceback.",
        ))

    # -- the previous run never happened ----------------------------------
    previous = last_finished_run(history)
    if previous is not None:
        gap_hours = (moment - previous).total_seconds() / 3600.0
        allowed = _gap_allows(previous, moment)
        if gap_hours > allowed:
            report.alerts.append(Alert(
                "missed_run", "warning",
                f"no run completed for {gap_hours / 24:.1f} days",
                f"Last successful run was {previous:%Y-%m-%d %H:%M UTC}. "
                "If this is a laptop, cron only fires while it is awake — "
                "consider launchd (macOS) or a systemd timer.",
            ))

    # -- every source failed ----------------------------------------------
    failed_sources = [e for e in errors if "source failed" in e.lower()]
    if active and len(failed_sources) >= len(active):
        report.alerts.append(Alert(
            "all_sources_failed", "critical",
            "every source failed this run",
            "; ".join(failed_sources[:3]),
        ))
    elif fetched == 0 and active:
        # Zero postings from every board at once is not a quiet day; it is a
        # broken watchlist or an API that changed shape.
        report.alerts.append(Alert(
            "no_jobs", "critical",
            "no postings fetched from any source",
            "Verify your slugs: python -m src.sources.ats_boards --check-all",
        ))

    # -- a source that used to work went silent ---------------------------
    baselines = source_baselines(history[:BASELINE_RUNS])
    silent: list[str] = []
    for name, average in sorted(baselines.items()):
        if average < BASELINE_MIN_AVERAGE:
            continue                      # never produced enough to be missed
        if name not in active:
            continue                      # switched off on purpose
        if int(source_counts.get(name, 0) or 0) == 0:
            silent.append(f"{name} (averaged {average:.0f}/run)")
    if silent and "no_jobs" not in report.kinds():
        report.alerts.append(Alert(
            "source_zero", "warning",
            f"{len(silent)} source(s) returned nothing today",
            "; ".join(silent) + ". A renamed board looks exactly like an empty one.",
        ))

    # -- anything else the run logged -------------------------------------
    if errors and "all_sources_failed" not in report.kinds():
        report.alerts.append(Alert(
            "errors", "info",
            f"{len(errors)} error(s) during the run",
            "; ".join(errors[:3]) + ("; ..." if len(errors) > 3 else ""),
        ))

    return report


def filter_alerts(report: HealthReport, wanted: Iterable[str] | None) -> HealthReport:
    """Keep only the alert kinds the user asked for in `notify.on`.

    `None` means "all of them"; an empty list means the user switched every
    alert off, which is their call.
    """
    if wanted is None:
        return report
    keep = {str(kind).strip().lower() for kind in wanted}
    return HealthReport([a for a in report.alerts if a.kind in keep])
