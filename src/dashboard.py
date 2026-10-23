"""The search in hindsight: one HTML page built from weeks of tracker history.

The digest answers "what came in today". The questions that need history —
are applications turning into interviews, where do scores land against the
threshold, which boards earn their fetch, which missing skill keeps costing
jobs, who is owed a follow-up — were four terminal commands. This puts them
on one page Luis can open from disk.

Adapted from ai-job-search's `/html-report` (MIT, Mads Lorentzen, e16acee),
without its model: every number is read from the tracker and computed by the
code that already prints it — `digest.application_funnel` (`src.track
report`), `digest.followups`, `skill_gaps.gap_report` (`src.track gaps`) — so
the page and the terminal can never disagree.

It keeps the digest's rules (see `src/digest.py`):

  * **The template is dumb.** Every string and every bar's geometry is
    computed here; the template only places them.
  * **Autoescaping is not optional.** Company, title and URL are text a
    stranger wrote, and this page opens as file://. Company and title also
    pass through `posting_guard.clean_field`, so a bidi override cannot make
    a row read differently from what the tracker holds, and a posting URL
    becomes a link only when it is plain http(s).
  * **Self-contained.** Inline CSS and inline SVG; no script, no font, no
    CDN. Hover text is SVG `<title>`, which needs no script.

Run it with `python -m src.track dashboard [--out PATH] [--days N]`.
"""

from __future__ import annotations

import json
import statistics
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader

from . import digest, posting_guard, skill_gaps
from .models import ensure_utc

TEMPLATE_NAME = "dashboard.html.j2"

#: The look-back for scores and skill gaps; `src.track gaps` defaults to the same.
DEFAULT_DAYS = 90
#: Runs read for source yield: a month of daily runs.
RECENT_RUNS = 30
#: Gap rows shown. The tail is single jobs, and the terminal has all of it.
TOP_GAPS = 15

# Score histogram geometry, in viewBox units. A 10-point bucket per column.
_HIST_W, _HIST_H = 640, 240
_HIST_LEFT, _HIST_RIGHT, _HIST_TOP, _HIST_BOTTOM = 12, 12, 40, 30
_COLUMN_W = 28
# Funnel geometry: one row per step, labels in a column to the left.
_FUNNEL_W, _FUNNEL_LABEL_W, _FUNNEL_ROW, _FUNNEL_BAR_H = 640, 150, 36, 22

_env: Environment | None = None


def _text(value: Any, limit: int = 160) -> str:
    """A stranger's single-line field, made safe to look at: invisible and
    bidi characters gone, one line, capped. Escaping is the template's job."""
    return posting_guard.clean_field(value, limit)


def _link(url: Any) -> dict[str, str]:
    """A posting URL as `{"href", "text"}`; `href` is empty unless the URL is
    plain http(s).

    Autoescape keeps a URL inside its attribute; it does nothing about a
    `javascript:` or `data:` scheme. A URL carrying whitespace or a control
    character is refused outright: browsers strip those before reading the
    scheme, so "java\\tscript:" would run. The link text is the host — the
    destination is readable before the click — and a refused URL is printed
    as text, so the row still shows what the tracker holds.
    """
    raw = str(url or "").strip()
    if not raw:
        return {"href": "", "text": ""}
    shown = {"href": "", "text": _text(raw, 120)}
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in raw):
        return shown
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
    except ValueError:
        return shown
    if parts.scheme.lower() not in ("http", "https") or not host:
        return shown
    return {"href": raw, "text": host}


def _n(value: float) -> str:
    """A coordinate, short: 12.0 -> "12", 12.25 -> "12.2"."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _column_path(x: float, y: float, w: float, h: float) -> str:
    """A vertical bar, rounded at the data end and square on the baseline."""
    r = min(4.0, w / 2, h)
    return (f"M{_n(x)} {_n(y + h)}V{_n(y + r)}Q{_n(x)} {_n(y)} {_n(x + r)} {_n(y)}"
            f"H{_n(x + w - r)}Q{_n(x + w)} {_n(y)} {_n(x + w)} {_n(y + r)}V{_n(y + h)}Z")


def _bar_path(x: float, y: float, w: float, h: float) -> str:
    """A horizontal bar, rounded at the data end and square at the axis."""
    r = min(4.0, h / 2, w)
    return (f"M{_n(x)} {_n(y)}H{_n(x + w - r)}Q{_n(x + w)} {_n(y)} {_n(x + w)} {_n(y + r)}"
            f"V{_n(y + h - r)}Q{_n(x + w)} {_n(y + h)} {_n(x + w - r)} {_n(y + h)}"
            f"H{_n(x)}Z")


# --------------------------------------------------------------------------
# sections — each returns plain dicts, or None/[] when there is nothing to show
# --------------------------------------------------------------------------


def funnel_section(events: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """`digest.application_funnel`, plus the bars that draw it."""
    funnel = digest.application_funnel(events)
    if funnel is None:
        return None
    top = max((step["value"] for step in funnel["steps"]), default=0) or 1
    span = _FUNNEL_W - _FUNNEL_LABEL_W - 56
    bars = []
    for index, step in enumerate(funnel["steps"]):
        y = 6 + index * _FUNNEL_ROW
        length = span * step["value"] / top
        bars.append({
            "label": step["label"], "value": step["value"],
            "path": _bar_path(_FUNNEL_LABEL_W, y, length, _FUNNEL_BAR_H) if step["value"] else "",
            "label_x": _n(_FUNNEL_LABEL_W - 10), "value_x": _n(_FUNNEL_LABEL_W + length + 8),
            "text_y": _n(y + _FUNNEL_BAR_H / 2 + 4),
        })
    return {
        **funnel,
        "by_source": [{**row, "source": _text(row["source"], 60)} for row in funnel["by_source"]],
        "unrecognised": [_text(stage, 60) for stage in funnel["unrecognised"]],
        "chart": {
            "width": _FUNNEL_W, "height": 6 + len(bars) * _FUNNEL_ROW, "bars": bars,
            "label": "Applications: " + ", ".join(
                f"{bar['label']} {bar['value']}" for bar in bars),
        },
    }


def applications_section(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """`Tracker.tracked_applications()`, newest activity first."""
    table = []
    for row in rows:
        table.append({
            "handle": str(row.get("key") or "")[:8],
            "company": _text(row.get("company")) or "Unknown company",
            "title": _text(row.get("title")) or "Untitled role",
            "source": _text(row.get("source"), 60),
            "stage": _text(row.get("stage"), 40),
            "label": _text(row.get("label"), 40).removeprefix("label_"),
            "closed": bool(row.get("closed")),
            "last_activity": _text(row.get("last_activity"), 10),
            "link": _link(row.get("url")),
        })
    table.sort(key=lambda item: (item["last_activity"], item["handle"]), reverse=True)
    return table


def score_section(rows: Iterable[Mapping[str, Any]], *, threshold: int,
                  days: int) -> dict[str, Any] | None:
    """Scores in 10-point buckets, with the threshold drawn where it falls.

    A score of 100 joins the 90s bucket rather than getting a column of its
    own; anything outside 0-100 is clamped, not dropped, so the bars add up
    to the count printed above them.
    """
    scores = [row["score"] for row in rows
              if isinstance(row.get("score"), int) and not isinstance(row.get("score"), bool)]
    if not scores:
        return None
    counts = [0] * 10
    for score in scores:
        counts[min(9, max(0, score // 10))] += 1
    plot_w = _HIST_W - _HIST_LEFT - _HIST_RIGHT
    plot_h = _HIST_H - _HIST_TOP - _HIST_BOTTOM
    band = plot_w / 10
    baseline = _HIST_H - _HIST_BOTTOM
    tallest = max(counts)
    columns = []
    for index, count in enumerate(counts):
        label = f"{index * 10}–{index * 10 + 9}" if index < 9 else "90–100"
        centre = _HIST_LEFT + band * (index + 0.5)
        height = max(2.0, plot_h * count / tallest) if count else 0.0
        columns.append({
            "label": label, "count": count,
            "path": _column_path(centre - _COLUMN_W / 2, baseline - height,
                                 _COLUMN_W, height) if count else "",
            "x": _n(centre), "count_y": _n(baseline - height - 6),
            # The whole band answers hover, so an empty bucket says "0" too.
            "hit_x": _n(_HIST_LEFT + band * index), "hit_w": _n(band),
        })
    marker = _HIST_LEFT + plot_w * min(100, max(0, threshold)) / 100
    passed = sum(1 for score in scores if score >= threshold)
    return {
        "days": days, "scored": len(scores), "threshold": threshold,
        "passed": passed, "median": round(statistics.median(scores)),
        "chart": {
            "width": _HIST_W, "height": _HIST_H, "columns": columns,
            "baseline": _n(baseline), "left": _n(_HIST_LEFT), "top": _n(_HIST_TOP),
            "hit_h": _n(plot_h), "right": _n(_HIST_W - _HIST_RIGHT),
            "axis_y": _n(_HIST_H - 10),
            # Label on the top line, above the tallest bar's count.
            "marker_x": _n(marker), "marker_top": _n(16), "marker_label_y": _n(12),
            "marker_label_x": _n(marker + 5 if threshold < 80 else marker - 5),
            "marker_anchor": "start" if threshold < 80 else "end",
            "label": (f"Score distribution of {len(scores)} jobs, threshold {threshold}: "
                      + ", ".join(f"{c['label']}: {c['count']}" for c in columns)),
        },
    }


def _run_stats(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """`(fetched, passed)` per source from one `runs` row; None when the run
    never finished or its stats are unreadable — one bad row costs that row."""
    try:
        data = json.loads(str(row.get("stats_json") or "{}"))
    except ValueError:
        return None
    if not isinstance(data, Mapping) or not isinstance(data.get("source_counts"), Mapping):
        return None
    after = data.get("source_after_filters")
    return dict(data["source_counts"]), dict(after) if isinstance(after, Mapping) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def source_yield(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Per source over recent runs: how often it reported, what it fetched,
    and what survived the filters. `runs` newest first, as `recent_runs`
    returns them; the newest readable one is "last run"."""
    totals: dict[str, dict[str, Any]] = {}
    read = 0
    last_stamp = first_stamp = ""
    for row in runs:
        stats = _run_stats(row)
        if stats is None:
            continue
        fetched, passed = stats
        stamp = str(row.get("finished_at") or row.get("started_at") or "")[:16].replace("T", " ")
        last_stamp = last_stamp or stamp
        first_stamp = stamp
        newest = read == 0
        read += 1
        for name, count in fetched.items():
            entry = totals.setdefault(str(name), {
                "name": _text(name, 60), "runs": 0, "fetched": 0, "passed": 0,
                "last_fetched": None, "last_passed": None,
            })
            entry["runs"] += 1
            entry["fetched"] += _count(count)
            entry["passed"] += _count(passed.get(name))
            if newest:
                entry["last_fetched"] = _count(count)
                entry["last_passed"] = _count(passed.get(name))
    if not totals:
        return None
    rows = sorted(totals.values(), key=lambda entry: (-entry["passed"], -entry["fetched"],
                                                      entry["name"]))
    for entry in rows:
        entry["rate"] = _rate(entry["passed"], entry["fetched"])
    return {"runs": read, "since": first_stamp, "last": last_stamp, "rows": rows}


def _rate(passed: int, fetched: int) -> str:
    """Passed over fetched as a percentage. Under 1% prints "<1%": a board
    that fetches 60,000 to pass 7 has a yield, and "0%" would deny it."""
    if not fetched:
        return "—"
    percent = 100 * passed / fetched
    return "<1%" if passed and percent < 1 else f"{round(percent)}%"


def gaps_section(report: skill_gaps.GapReport, *, top: int = TOP_GAPS) -> dict[str, Any]:
    """`skill_gaps.gap_report` as rows, each with the best-scored jobs behind it."""
    rows = []
    for gap in report.gaps[:top]:
        jobs = sorted(gap.jobs, key=lambda job: -job["score"])[:2]
        rows.append({
            "name": gap.name, "kind": gap.kind, "minor": gap.minor,
            "jobs": len(gap.jobs), "near_misses": gap.near_misses, "average": gap.average,
            "examples": [f"{_text(job['company'], 60)} – {_text(job['title'], 80)} "
                         f"({job['score']})" for job in jobs],
        })
    return {
        "scored": report.scored, "threshold": report.threshold,
        "near_low": report.threshold - skill_gaps.NEAR_BAND,
        "unrecognised": report.unrecognised, "total": len(report.gaps),
        "rows": rows,
    }


def _tracking(config: Any) -> dict[str, int]:
    """`tracking.*` as `digest.followups` takes it; the digest's defaults."""
    values = {}
    for name, default in digest.TRACKING_DEFAULTS.items():
        value = config.get(f"tracking.{name}", default)
        values[name] = value if isinstance(value, int) and not isinstance(value, bool) \
            and value >= 0 else default
    return values


def followups_section(rows: Iterable[Mapping[str, Any]], *, now: datetime,
                      tracking: Mapping[str, int]) -> list[dict[str, Any]]:
    """`digest.followups`, its stranger-written fields cleaned."""
    return [{**item, "company": _text(item["company"]), "title": _text(item["title"]),
             "source": _text(item["source"], 60), "link": _link(item["url"])}
            for item in digest.followups(rows, now, **tracking)]


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------


def build_context(tracker: Any, config: Any, *, now: datetime,
                  days: int = DEFAULT_DAYS, runs: int = RECENT_RUNS) -> dict[str, Any]:
    """Everything the template prints. Reads the tracker and the CV; writes nothing."""
    moment = ensure_utc(now) or now
    since = moment - timedelta(days=days)
    threshold = int(config.get("scoring.threshold", 65) or 65)
    tracking = _tracking(config)
    funnel = funnel_section(tracker.outcome_events())
    scores = score_section(tracker.scored_applications(since.isoformat()),
                           threshold=threshold, days=days)
    followups = followups_section(tracker.open_applications(), now=moment, tracking=tracking)
    steps = {step["label"]: step["value"] for step in (funnel or {}).get("steps", [])}
    return {
        "title": f"Job Hunter — dashboard {moment.date().isoformat()}",
        "generated": moment.strftime("%Y-%m-%d %H:%M UTC"),
        "days": days,
        "since": since.date().isoformat(),
        "tiles": [
            {"label": "applied", "value": steps.get("applied", 0)},
            {"label": "reached interview", "value": steps.get("reached interview", 0)},
            {"label": "reached offer", "value": steps.get("reached offer", 0)},
            {"label": "follow-ups due", "value": len(followups)},
            {"label": f"scored, {days} days", "value": scores["scored"] if scores else 0},
            {"label": f"at or above {threshold}", "value": scores["passed"] if scores else 0},
        ],
        "funnel": funnel,
        "applications": applications_section(tracker.tracked_applications()),
        "scores": scores,
        "threshold": threshold,
        "sources": source_yield(tracker.recent_runs(limit=runs)),
        "runs_read": runs,
        "gaps": gaps_section(skill_gaps.gap_report(tracker, config, days=days, now=moment)),
        "followups": followups,
        "tracking": tracking,
    }


def _environment() -> Environment:
    """The digest's template folder, autoescaping unconditionally — the same
    reasoning as `digest._environment`."""
    global _env
    if _env is None:
        _env = Environment(loader=FileSystemLoader(str(digest.TEMPLATE_DIR)),
                           autoescape=True, trim_blocks=True, lstrip_blocks=True,
                           keep_trailing_newline=True)
    return _env


def render_html(context: Mapping[str, Any]) -> str:
    return _environment().get_template(TEMPLATE_NAME).render(**context)


def build_dashboard(tracker: Any, config: Any, *, now: datetime,
                    days: int = DEFAULT_DAYS) -> str:
    """The dashboard page as a string."""
    return render_html(build_context(tracker, config, now=now, days=days))


def write_dashboard(tracker: Any, config: Any, path: str | Path, *, now: datetime,
                    days: int = DEFAULT_DAYS) -> Path:
    """Write the page to `path`, replacing any earlier copy, and return the path.
    Overwriting is the point: the page is a view, the tracker is the record."""
    target = Path(path)
    page = build_dashboard(tracker, config, now=now, days=days)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target
