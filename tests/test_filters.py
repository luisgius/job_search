"""Tests for src/filters.py — the free, deterministic stage that does most of
the real work.

Every job this stage lets through costs an LLM call, and every job it wrongly
drops is invisible forever. So the tests below care about both directions,
and about the *reason* attached to a rejection: the digest groups on those
slugs, and they are the only way a user can tell "no jobs today" apart from
"my filters are wrong".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.filters import (
    FUTURE_TOLERANCE_HOURS,
    REASON_CATEGORIES,
    SOURCE_RANK,
    apply_filters,
    dedupe,
    is_fresh,
    passes_keywords,
    passes_location,
    passes_title,
)
from tests.conftest import NOW, make_job, write_config


def cfg(tmp_path, **filters):
    """A config with the filter block overridden and everything else default."""
    return write_config(tmp_path, {"filters": filters} if filters else None)


# ==========================================================================
# title
# ==========================================================================


@pytest.mark.parametrize(
    "title",
    ["Intern - Backend", "Backend Intern", "Software Engineering Intern (m/f/d)",
     "Internship, Data Science", "Working Student Backend", "Werkstudent Data",
     "Praktikum Softwareentwicklung", "Graduate Program 2026"],
)
def test_title_exclusions_reject_the_things_they_are_for(tmp_path, title):
    ok, reason = passes_title(make_job(title=title), cfg(tmp_path))
    assert ok is False
    assert reason


@pytest.mark.parametrize(
    "title",
    ["International Sales Manager", "Internal Tools Engineer",
     "Internationalization Engineer", "Backend Engineer",
     "Senior Interface Designer", "Data Internals Engineer"],
)
def test_title_exclusions_match_whole_words_only(tmp_path, title):
    """"intern" inside "International" is the classic substring bug: it would
    silently delete a whole category of real jobs."""
    ok, _ = passes_title(make_job(title=title), cfg(tmp_path))
    assert ok is True


def test_empty_include_list_allows_everything(tmp_path):
    ok, _ = passes_title(make_job(title="Underwater Basket Weaver"),
                         cfg(tmp_path, title_include=[], title_exclude=[]))
    assert ok is True


def test_include_list_rejects_anything_unlisted(tmp_path):
    conf = cfg(tmp_path, title_include=["engineer", "developer"], title_exclude=[])
    assert passes_title(make_job(title="Backend Engineer"), conf)[0] is True
    assert passes_title(make_job(title="Senior Developer"), conf)[0] is True
    assert passes_title(make_job(title="Product Manager"), conf)[0] is False


def test_title_matching_is_case_and_accent_insensitive(tmp_path):
    conf = cfg(tmp_path, title_include=["ingenieur"], title_exclude=[])
    assert passes_title(make_job(title="Ingénieur Backend"), conf)[0] is True
    assert passes_title(make_job(title="INGENIEUR BACKEND"), conf)[0] is True


def test_multiword_exclusion_matches_as_a_phrase(tmp_path):
    conf = cfg(tmp_path, title_exclude=["working student"], title_include=[])
    assert passes_title(make_job(title="Working Student Backend"), conf)[0] is False
    # "working" and "student" apart is not the phrase.
    assert passes_title(make_job(title="Student of Working Systems"), conf)[0] is True


# ==========================================================================
# location
# ==========================================================================


def test_eu_location_passes_and_stamps_the_country(tmp_path):
    job = make_job(location="Berlin, Germany")
    assert passes_location(job, cfg(tmp_path))[0] is True


def test_us_location_is_rejected(tmp_path):
    ok, reason = passes_location(make_job(location="San Francisco, CA"), cfg(tmp_path))
    assert ok is False
    assert reason


def test_country_not_on_the_list_is_rejected(tmp_path):
    conf = cfg(tmp_path, countries=["DE", "NL"])
    assert passes_location(make_job(location="Madrid, Spain"), conf)[0] is False
    assert passes_location(make_job(location="Berlin, Germany"), conf)[0] is True


def test_remote_with_an_eu_hint_passes(tmp_path):
    conf = cfg(tmp_path, allow_remote=True, remote_requires_eu_hint=True)
    assert passes_location(make_job(location="Remote - Europe", remote=True), conf)[0] is True
    assert passes_location(make_job(location="Remote (EMEA)", remote=True), conf)[0] is True


def test_bare_remote_is_rejected_when_an_eu_hint_is_required(tmp_path):
    """"Remote" with nothing European anywhere is usually a US role — the most
    common way an EU-only search fills up with jobs you cannot take."""
    conf = cfg(tmp_path, allow_remote=True, remote_requires_eu_hint=True)
    job = make_job(location="Remote", remote=True,
                   description="Work from anywhere in the United States.")
    assert passes_location(job, conf)[0] is False


def test_bare_remote_passes_when_the_hint_is_not_required(tmp_path):
    conf = cfg(tmp_path, allow_remote=True, remote_requires_eu_hint=False)
    assert passes_location(make_job(location="Remote", remote=True), conf)[0] is True


def test_remote_can_be_disallowed_entirely(tmp_path):
    conf = cfg(tmp_path, allow_remote=False)
    assert passes_location(make_job(location="Remote - Europe", remote=True), conf)[0] is False
    assert passes_location(make_job(location="Berlin, Germany"), conf)[0] is True


def test_allow_remote_false_still_keeps_a_job_in_an_allowed_country(tmp_path):
    """A deliberate asymmetry, pinned here because it is easy to read as a bug.

    `allow_remote: false` rejects postings that are *only* remote. A posting
    that names an allowed country is kept even if it also offers remote work,
    because "Berlin, Germany (Remote)" overwhelmingly means a Berlin role with
    flexibility — rejecting it would throw away most hybrid postings.
    """
    conf = cfg(tmp_path, allow_remote=False)
    assert passes_location(
        make_job(location="Berlin, Germany (Remote)", remote=True), conf
    )[0] is True


def test_location_rejection_says_which_country_it_resolved_to(tmp_path):
    """The digest has to distinguish "wrong country" from "unparseable" —
    they call for completely different fixes."""
    conf = cfg(tmp_path, countries=["DE"])
    _, wrong_country = passes_location(make_job(location="Madrid, Spain"), conf)
    _, unparseable = passes_location(make_job(location="Somewhereville"), conf)
    assert "Spain" in wrong_country and "ES" in wrong_country
    assert "could not be resolved" in unparseable


def test_location_check_stamps_remote_when_the_source_left_it_unknown(tmp_path):
    job = make_job(location="Remote - Europe", remote=None)
    passes_location(job, cfg(tmp_path))
    assert job.remote is True


def test_unresolvable_location_is_rejected_with_a_clear_reason(tmp_path):
    ok, reason = passes_location(make_job(location="Somewhereville"), cfg(tmp_path))
    assert ok is False
    assert "somewhereville" in reason.lower() or "could not" in reason.lower()


# ==========================================================================
# freshness
# ==========================================================================


def test_fresh_job_passes():
    ok, _ = is_fresh(make_job(hours_old=3), 24, now=NOW)
    assert ok is True


def test_stale_job_is_rejected():
    ok, reason = is_fresh(make_job(hours_old=50), 24, now=NOW)
    assert ok is False
    assert "50" in reason or "old" in reason.lower()


def test_exactly_at_the_boundary_is_still_fresh():
    ok, _ = is_fresh(make_job(hours_old=24), 24, now=NOW)
    assert ok is True


def test_undated_is_skipped_by_default():
    """`skip_undated: true` is the honest default: a posting we cannot date
    cannot be proven fresh."""
    ok, reason = is_fresh(make_job(hours_old=None), 24, now=NOW)
    assert ok is False
    assert "date" in reason.lower()


def test_undated_can_be_kept_deliberately():
    ok, _ = is_fresh(make_job(hours_old=None), 24, skip_undated=False, now=NOW)
    assert ok is True


def test_a_slightly_future_date_is_accepted_silently():
    """Minutes of drift is timezone rounding, not news."""
    job = make_job(posted_at=NOW + timedelta(hours=FUTURE_TOLERANCE_HOURS - 0.5))
    ok, reason = is_fresh(job, 24, now=NOW)
    assert ok is True
    assert reason == ""


def test_a_wildly_future_date_is_accepted_but_reported():
    """A board hours or days ahead is a real signal that its dates cannot be
    trusted for freshness — worth saying out loud, not worth dropping the job."""
    job = make_job(posted_at=NOW + timedelta(days=30))
    ok, reason = is_fresh(job, 24, now=NOW)
    assert ok is True
    assert "future" in reason.lower()


# ==========================================================================
# keywords
# ==========================================================================


def test_require_keywords_any_needs_one_hit(tmp_path):
    conf = cfg(tmp_path, require_keywords_any=["python", "golang"])
    assert passes_keywords(make_job(description="We use Python and Django."), conf)[0] is True
    assert passes_keywords(make_job(description="We use Rust."), conf)[0] is False


def test_require_keywords_any_also_reads_the_title(tmp_path):
    conf = cfg(tmp_path, require_keywords_any=["python"])
    job = make_job(title="Python Engineer", description="Backend work.")
    assert passes_keywords(job, conf)[0] is True


def test_empty_require_keywords_allows_everything(tmp_path):
    conf = cfg(tmp_path, require_keywords_any=[])
    assert passes_keywords(make_job(description="anything"), conf)[0] is True


def test_description_exclude_rejects(tmp_path):
    conf = cfg(tmp_path, description_exclude=["security clearance"])
    job = make_job(description="Applicants must hold an active security clearance.")
    ok, reason = passes_keywords(job, conf)
    assert ok is False
    assert "security clearance" in reason.lower()


def test_description_exclude_is_whole_word(tmp_path):
    conf = cfg(tmp_path, description_exclude=["ts/sci"])
    assert passes_keywords(make_job(description="We use TypeScript."), conf)[0] is True


# ==========================================================================
# apply_filters
# ==========================================================================


def test_apply_filters_partitions_the_batch(tmp_path, jobs):
    result = apply_filters(jobs, cfg(tmp_path), now=NOW)
    kept = {j.company for j in result.kept}
    assert "Acme" in kept          # fresh, Berlin
    assert "Umbrella" in kept      # fresh, remote with an EU hint
    assert "Globex" not in kept    # 50h old
    assert "Initech" not in kept   # undated
    assert "Hooli" not in kept     # San Francisco
    assert "Soylent" not in kept   # remote, US-flavoured
    assert "Vandelay" not in kept  # internship
    assert len(result.kept) + len(result.rejected) == len(jobs)


def test_every_rejection_has_a_reason_and_a_category(tmp_path, jobs):
    result = apply_filters(jobs, cfg(tmp_path), now=NOW)
    assert all(reason for _job, reason in result.rejected)
    assert sum(result.counts.values()) == len(result.rejected)
    for category in result.counts:
        assert category in REASON_CATEGORIES, category


def test_filters_run_cheapest_first_so_one_reason_is_reported(tmp_path):
    """A stale US internship should be reported as an internship — the first
    thing the user can act on — not as three separate problems."""
    job = make_job(title="Backend Intern", location="San Francisco, CA", hours_old=99)
    result = apply_filters([job], cfg(tmp_path), now=NOW)
    assert list(result.counts) == ["title_excluded"]


def test_apply_filters_stamps_the_country(tmp_path):
    job = make_job(location="Amsterdam, Netherlands", country=None)
    apply_filters([job], cfg(tmp_path), now=NOW)
    assert job.country == "NL"


def test_apply_filters_on_an_empty_batch(tmp_path):
    result = apply_filters([], cfg(tmp_path), now=NOW)
    assert result.kept == [] and result.rejected == [] and result.counts == {}


def test_apply_filters_survives_a_pathological_job(tmp_path):
    """One malformed posting must cost one posting, not the run."""
    broken = make_job(title="Engineer", location="Berlin, Germany")
    broken.description = None  # type: ignore[assignment]
    good = make_job(company="Fine", location="Berlin, Germany", ats_job_id="99")
    result = apply_filters([broken, good], cfg(tmp_path, min_description_chars=10),
                           now=NOW)
    assert good in result.kept
    assert len(result.kept) + len(result.rejected) == 2


def test_min_description_chars(tmp_path):
    conf = cfg(tmp_path, min_description_chars=100)
    short = make_job(description="Too short.", ats_job_id="1")
    long = make_job(description="x " * 200, ats_job_id="2")
    result = apply_filters([short, long], conf, now=NOW)
    assert result.kept == [long]
    assert result.counts == {"description_too_short": 1}


def test_result_summary_reads_like_a_log_line(tmp_path, jobs):
    summary = apply_filters(jobs, cfg(tmp_path), now=NOW).summary()
    assert "kept" in summary and "dropped" in summary


# ==========================================================================
# dedupe
# ==========================================================================


def test_dedupe_collapses_the_same_role_from_two_sources():
    ats = make_job(source="greenhouse", ats="greenhouse", ats_job_id="1",
                   country="DE", description="x" * 500)
    email = make_job(source="linkedin_email", ats=None, ats_job_id=None,
                     country="DE", description="short",
                     url="https://www.linkedin.com/jobs/view/1")
    out = dedupe([ats, email])
    assert len(out) == 1
    assert out[0].source == "greenhouse"


def test_dedupe_prefers_a_dated_record_over_an_undated_one():
    """The date decides whether the job survives the freshness filter at all,
    so it outranks description length."""
    undated = make_job(source="greenhouse", hours_old=None, description="x" * 900,
                       ats=None, ats_job_id=None, country="DE")
    dated = make_job(source="linkedin_email", hours_old=2, description="short",
                     ats=None, ats_job_id=None, country="DE")
    assert dedupe([undated, dated])[0] is dated


def test_dedupe_prefers_the_longer_description_when_both_are_dated():
    thin = make_job(source="adzuna", description="snippet…", ats=None,
                    ats_job_id=None, country="DE")
    full = make_job(source="adzuna", description="x" * 2000, ats=None,
                    ats_job_id=None, country="DE")
    assert dedupe([thin, full])[0] is full


def test_dedupe_prefers_an_ats_source_on_a_tie():
    aggregator = make_job(source="adzuna", description="same", ats=None,
                          ats_job_id=None, country="DE")
    ats = make_job(source="lever", description="same", ats=None,
                   ats_job_id=None, country="DE")
    assert dedupe([aggregator, ats])[0].source == "lever"


def test_dedupe_keeps_genuinely_different_jobs():
    a = make_job(company="Acme", title="Backend Engineer", ats_job_id="1")
    b = make_job(company="Acme", title="Data Engineer", ats_job_id="2")
    c = make_job(company="Globex", title="Backend Engineer", ats_job_id="3")
    assert len(dedupe([a, b, c])) == 3


def test_dedupe_is_order_stable_and_deterministic():
    batch = [make_job(company=f"C{i}", ats_job_id=str(i)) for i in range(5)]
    assert [j.company for j in dedupe(batch)] == [j.company for j in batch]
    assert dedupe(batch) == dedupe(batch)


def test_dedupe_ties_keep_the_first_sighting():
    first = make_job(source="greenhouse", description="same", ats=None,
                     ats_job_id=None, country="DE", url="https://a")
    second = make_job(source="greenhouse", description="same", ats=None,
                      ats_job_id=None, country="DE", url="https://b")
    assert dedupe([first, second])[0] is first


def test_dedupe_on_empty_input():
    assert dedupe([]) == []


def test_source_rank_puts_ats_above_aggregators_above_email():
    assert SOURCE_RANK["greenhouse"] > SOURCE_RANK["adzuna"] > SOURCE_RANK["linkedin_email"]
