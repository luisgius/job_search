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
from itertools import permutations

import pytest

from src.filters import (
    FUTURE_TOLERANCE_HOURS,
    REASON_CATEGORIES,
    SOURCE_RANK,
    TRACKED_APPLIED,
    TRACKED_HANDLED,
    TRACKED_NONE,
    TRACKED_QUEUED,
    VARIANT_MIN_BODY_CHARS,
    apply_filters,
    collapse_variants,
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
    assert "Globex" not in kept    # older than freshness.max_age_hours
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
# collapse_variants — one posting listed once per city
# ==========================================================================
#
# No Fluff Jobs publishes one posting per city and rotates the city list, so
# every copy has a different first city, a different `dedupe_key` and — with
# no ATS id — a different `Job.key`. A burst of fourteen copies of one ad
# used to take fourteen of the run's scoring slots.

HOME = {"applicant": {"location": "Kraków, Poland"}}

#: A full ad body comfortably over the tier B floor.
BODY = " ".join(f"Responsibility {i}: build and run batch data pipelines." for i in range(20))


def city_copy(location, n, **overrides):
    """One No Fluff Jobs-shaped copy: no ATS, a synthesized snippet body."""
    fields = dict(source="nofluffjobs", company="Northwind", title="Data Engineer",
                  ats=None, ats_job_id=f"nfj-{n}", location=location,
                  url=f"https://nofluffjobs.com/job/northwind-data-engineer-{n}",
                  description="Main technology: Python. Seniority: Mid.",
                  raw={"snippet_only": True})
    fields.update(overrides)
    return make_job(**fields)


def test_rotated_city_lists_are_one_posting():
    copies = [city_copy("Warsaw, Cracow, Gdańsk", 1),
              city_copy("Gdańsk, Warsaw, Cracow", 2),
              city_copy("Cracow, Gdańsk, Warsaw", 3)]
    assert len({job.dedupe_key for job in copies}) == 3      # dedupe cannot see it

    result = collapse_variants(copies, HOME)

    assert result.kept == [copies[2]]
    assert [job for job, _ in result.rejected] == copies[:2]
    assert all(reason.startswith("same_posting_other_city:") and copies[2].key[:8] in reason
               for _, reason in result.rejected)
    assert result.counts == {"same_posting_other_city": 2}
    assert copies[2].raw["also_in"] == ["Warsaw", "Gdańsk"]


def test_the_profile_city_copy_is_kept_over_a_richer_earlier_one():
    """"Cracow" on the board, "Kraków" in the profile: one city."""
    richer = city_copy("Warsaw, Cracow", 1, description=BODY, raw={})
    home = city_copy("Cracow, Warsaw", 2)
    assert collapse_variants([richer, home], HOME).kept == [home]
    assert home.raw["also_in"] == ["Warsaw"]


def test_without_a_home_copy_remote_then_richness_decides():
    office = city_copy("Berlin, Paris", 1, description=BODY, raw={})
    remote = city_copy("Paris, Berlin", 2, remote=True)
    assert collapse_variants([office, remote], HOME).kept == [remote]

    teaser = city_copy("Berlin, Paris", 3)
    richer = city_copy("Paris, Berlin", 4, description=BODY, raw={})
    assert collapse_variants([teaser, richer], HOME).kept == [richer]


def test_one_full_body_listed_for_two_cities_is_one_posting():
    """Tier B: separate requisitions per city, the same ad word for word."""
    berlin = city_copy("Berlin, Germany", 1, source="greenhouse", ats="greenhouse",
                       description=BODY, raw={})
    barcelona = city_copy("Barcelona, Spain", 2, source="greenhouse", ats="greenhouse",
                          description=BODY, raw={})
    result = collapse_variants([berlin, barcelona], HOME)
    assert result.kept == [berlin]                # a full tie keeps the first sighting
    assert berlin.raw["also_in"] == ["Barcelona"]


def test_different_city_sets_with_different_bodies_stay_apart():
    a = city_copy("Warsaw, Cracow", 1, description=BODY, raw={})
    b = city_copy("Berlin, Munich", 2, description=BODY.replace("batch", "streaming"),
                  raw={})
    other_title = city_copy("Cracow, Warsaw", 3, title="Data Analyst")
    result = collapse_variants([a, b, other_title], HOME)
    assert result.kept == [a, b, other_title]
    assert result.rejected == [] and result.counts == {}
    assert "also_in" not in a.raw


@pytest.mark.parametrize("snippet", [True, False])
def test_a_shared_teaser_is_never_identity(snippet):
    """Snippets are synthesized from structured fields, so unrelated jobs share
    them; a long one still never merges. A *full* body that long does."""
    assert len(BODY) >= VARIANT_MIN_BODY_CHARS
    raw = {"snippet_only": True} if snippet else {}
    a = city_copy("Warsaw", 1, description=BODY, raw=dict(raw))
    b = city_copy("Gdańsk", 2, description=BODY, raw=dict(raw))
    assert len(collapse_variants([a, b], HOME).kept) == (2 if snippet else 1)


def test_a_short_shared_full_body_is_not_identity():
    a = city_copy("Warsaw", 1, description="We use Python and SQL.", raw={})
    b = city_copy("Gdańsk", 2, description="We use Python and SQL.", raw={})
    assert collapse_variants([a, b], HOME).kept == [a, b]


def test_groups_come_out_in_first_seen_order():
    first = city_copy("Warsaw, Cracow", 1)
    other = city_copy("Berlin, Munich", 2, company="Contoso")
    home = city_copy("Cracow, Warsaw", 3)
    assert collapse_variants([first, other, home], HOME).kept == [home, other]


def ats_copy(location, n, **overrides):
    """One Greenhouse requisition per city, the same full ad (tier B)."""
    return city_copy(location, n, source="greenhouse", ats="greenhouse", ats_job_id=str(n),
                     url=f"https://boards.greenhouse.io/northwind/jobs/{n}",
                     description=BODY, raw={}, **overrides)


def standing(**tiers):
    """`known=` as the pipeline passes it: a tier per key, NONE otherwise."""
    return lambda job: tiers.get(job.key, TRACKED_NONE)


def test_a_copy_already_applied_to_stands_for_its_posting():
    """The home-city copy would win on rank, but its Berlin sibling was
    applied to: keeping the new copy is a second application to one ad."""
    applied, home = ats_copy("Berlin, Germany", 1), ats_copy("Kraków, Poland", 2)
    result = collapse_variants([applied, home], HOME,
                               known=standing(**{applied.key: TRACKED_APPLIED}))
    assert result.kept == [applied]
    assert result.rejected == [(home, "same_posting_other_city: the copy listed for "
                                      f"Berlin ({applied.key[:8]}) was already applied to")]


def test_a_context_copy_the_tracker_holds_rejects_every_candidate():
    """Stale today, so the filters dropped it — but applied (or queued) all
    the same. Nothing of the posting may be kept beside it."""
    for tier, outcome in ((TRACKED_APPLIED, "was already applied to"),
                          (TRACKED_QUEUED, "is already tracked")):
        held, home = ats_copy("Berlin, Germany", 1), ats_copy("Kraków, Poland", 2)
        result = collapse_variants([home], HOME, known=standing(**{held.key: tier}),
                                   context=[held])
        assert result.kept == [] and [job for job, _ in result.rejected] == [home]
        assert result.rejected[0][1].endswith(f"({held.key[:8]}) {outcome}")
        assert result.counts == {"same_posting_other_city": 1}


def test_context_the_tracker_does_not_hold_never_displaces_a_candidate():
    held, home = ats_copy("Kraków, Poland", 1), ats_copy("Berlin, Germany", 2)
    stranger = ats_copy("Gdańsk, Poland", 3, company="Contoso")
    result = collapse_variants([home], HOME, known=standing(), context=[held, stranger])
    assert result.kept == [home] and result.rejected == []


def test_among_held_copies_the_stronger_hold_wins():
    """Handled beats queued: a copy already shown stops one still waiting."""
    queued, shown = ats_copy("Kraków, Poland", 1), ats_copy("Berlin, Germany", 2)
    result = collapse_variants([queued], HOME, context=[shown], known=standing(
        **{queued.key: TRACKED_QUEUED, shown.key: TRACKED_HANDLED}))
    assert result.kept == [] and result.rejected[0][0] is queued


def test_the_ats_record_beats_an_aggregators_remote_copy():
    """Only the ATS's own record can be auto-applied to; a "Remote" freehire
    copy of the same ad used to win on remote-ness and leave nothing to act on."""
    ats = ats_copy("Warsaw, Poland", 77)
    agg = city_copy("Remote", 78, source="freehire", ats=None, remote=True,
                    url="https://freehire.me/jobs/acme-ml", description=BODY, raw={})
    assert collapse_variants([agg, ats], HOME).kept == [ats]


def test_a_direct_record_outranks_the_home_city_of_an_indirect_copy():
    """Every copy passed the location filters; home city decides among
    direct records, and among indirect ones, but not across the two."""
    ats = ats_copy("Warsaw, Poland", 1)
    home = city_copy("Kraków, Poland", 2, source="freehire", ats=None,
                     description=BODY, raw={})
    assert collapse_variants([home, ats], HOME).kept == [ats]
    other_ats = ats_copy("Kraków, Poland", 3)
    assert collapse_variants([ats, home, other_ats], HOME).kept == [other_ats]


def test_collapse_city_variants_false_keeps_every_copy():
    copies = [city_copy("Warsaw, Cracow", 1), city_copy("Cracow, Warsaw", 2)]
    off = {**HOME, "filters": {"collapse_city_variants": False}}
    result = collapse_variants(copies, off)
    assert result.kept == copies and result.rejected == []
    assert "same_posting_other_city" in REASON_CATEGORIES


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


@pytest.mark.parametrize("same_url", [False, True])
def test_dedupe_fresh_snippet_survives_before_freshness_filter(tmp_path, same_url):
    old = make_job(source="nofluffjobs", ats=None, ats_job_id="old",
                   hours_old=15 * 24, country="DE",
                   description="Main technology: Python. Seniority: mid. Category: data.",
                   raw={"snippet_only": True})
    fresh = make_job(source="nofluffjobs", ats=None, ats_job_id="fresh",
                     hours_old=20, country="DE",
                     description="Seniority: mid. Category: data.",
                     raw={"snippet_only": True})
    if same_url:
        fresh.url = old.url + "?utm_source=refresh"
    config = write_config(tmp_path, {
        "filters": {"title_include": [], "title_exclude": [], "countries": ["DE"]},
        "freshness": {"max_age_hours": 72, "skip_undated": True},
    })
    original_dates = old.posted_at, fresh.posted_at
    for batch in ([old, fresh], [fresh, old]):
        assert dedupe(batch) == [fresh]
        assert apply_filters(dedupe(batch), config, now=NOW).kept == [fresh]
    assert (old.posted_at, fresh.posted_at) == original_dates


@pytest.mark.parametrize("full_source", ["greenhouse", "nofluffjobs"])
def test_dedupe_full_description_beats_newer_longer_snippet(full_source):
    full = make_job(source=full_source, ats=None, ats_job_id="full",
                    hours_old=100, description="Build forecasting models with Python.")
    snippet = make_job(source="nofluffjobs", ats=None, ats_job_id="snippet",
                       hours_old=1, description="Required skills: Python. " * 20,
                       raw={"snippet_only": True})
    for batch in ([full, snippet], [snippet, full]):
        assert dedupe(batch) == [full]


def test_dedupe_snippets_use_source_rank_then_recency_in_every_order():
    older = make_job(source="adzuna", ats=None, ats_job_id="older",
                     hours_old=100, description="Older long teaser. " * 20,
                     raw={"snippet_only": True})
    newer = make_job(source="nofluffjobs", ats=None, ats_job_id="newer",
                     hours_old=20, description="Python", raw={"snippet_only": True})
    email = make_job(source="linkedin_email", ats=None, ats_job_id="email",
                     hours_old=1, description="Newest alert teaser. " * 30,
                     raw={"snippet_only": True})
    # A single transitive key must give the same winner in all six orders.
    for batch in permutations([older, newer, email]):
        assert dedupe(list(batch)) == [newer]


def test_dedupe_dated_snippet_still_beats_undated_full_description():
    undated = make_job(source="greenhouse", ats_job_id="undated", hours_old=None,
                       description="Full employer description. " * 50)
    dated = make_job(source="nofluffjobs", ats=None, ats_job_id="dated",
                     hours_old=10, description="Python", raw={"snippet_only": True})
    for batch in ([undated, dated], [dated, undated]):
        assert dedupe(batch) == [dated]
    assert undated.posted_at is None


@pytest.mark.parametrize("hours_old", [None, 2])
def test_dedupe_snippet_ties_ignore_length_and_keep_first_group_order(hours_old):
    first = make_job(source="nofluffjobs", ats=None, ats_job_id="first",
                     hours_old=hours_old, description="Python", raw={"snippet_only": True})
    second = make_job(source="nofluffjobs", ats=None, ats_job_id="second",
                      hours_old=hours_old, description="Much longer metadata. " * 10,
                      raw={"snippet_only": True})
    other = make_job(company="Different employer", ats_job_id="other")
    assert dedupe([first, other, second]) == [first, other]
    assert dedupe([second, other, first]) == [second, other]


def test_dedupe_new_email_receipt_does_not_replace_dated_ats_body():
    ats = make_job(source="greenhouse", ats_job_id="ats", hours_old=100,
                   description="Full employer description with requirements.")
    email = make_job(source="linkedin_email", ats=None, ats_job_id=None,
                     hours_old=1, description="", url="https://www.linkedin.com/jobs/view/1")
    assert dedupe([email, ats]) == [ats]


def test_dedupe_keeps_genuinely_different_jobs():
    # Distinct URLs, because distinct jobs have them: dedupe's first pass now
    # (correctly) treats one canonical URL as one posting, whatever the rest
    # of the record claims.
    a = make_job(company="Acme", title="Backend Engineer", ats_job_id="1",
                 url="https://boards.greenhouse.io/acme/jobs/1")
    b = make_job(company="Acme", title="Data Engineer", ats_job_id="2",
                 url="https://boards.greenhouse.io/acme/jobs/2")
    c = make_job(company="Globex", title="Backend Engineer", ats_job_id="3",
                 url="https://boards.greenhouse.io/globex/jobs/3")
    assert len(dedupe([a, b, c])) == 3


def test_dedupe_is_order_stable_and_deterministic():
    batch = [
        make_job(company=f"C{i}", ats_job_id=str(i),
                 url=f"https://boards.greenhouse.io/c{i}/jobs/{i}")
        for i in range(5)
    ]
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


# ==========================================================================
# first_title_match — the public matcher cv.variants selection rides on
# ==========================================================================

from src.filters import first_title_match  # noqa: E402


def test_first_title_match_is_whole_word_and_accent_folded():
    assert first_title_match("ML Engineer", ["ml"]) == "ml"
    assert first_title_match("HTML Developer", ["ml"]) is None
    assert first_title_match("Ingeniería de Analytics", ["analytics"]) == "analytics"


def test_first_title_match_returns_the_first_hit_in_term_order():
    assert first_title_match("ML Product Engineer", ["product", "ml"]) == "product"


def test_first_title_match_tolerates_junk():
    assert first_title_match(None, ["ml"]) is None
    assert first_title_match("ML Engineer", None) is None


# ==========================================================================
# Phase 4 — junior stamp, sponsorship exception, language gate, URL dedupe
# ==========================================================================

from src.filters import dedupe  # noqa: E402

_DS_FILTERS = {
    "countries": ["PL", "DE", "NL"],
    "title_include": ["data scientist", "machine learning engineer"],
    "title_exclude": ["senior"],
    "title_junior_markers": ["junior", "graduate"],
}


def test_a_junior_marker_stamps_metadata_without_gating():
    cfg = {"filters": dict(_DS_FILTERS)}
    job = make_job(title="Junior Data Scientist", location="Berlin, Germany")
    result = apply_filters([job], cfg, now=NOW)
    assert result.kept == [job]
    assert job.raw["level"] == "junior"


def test_a_plain_title_is_not_junior():
    """Target level is mid — junior is acceptable, never the default."""
    cfg = {"filters": dict(_DS_FILTERS)}
    job = make_job(title="Data Scientist", location="Berlin, Germany")
    apply_filters([job], cfg, now=NOW)
    assert "level" not in job.raw


def test_the_exclude_list_still_wins_over_the_stamp():
    cfg = {"filters": dict(_DS_FILTERS)}
    job = make_job(title="Senior Data Scientist", location="Berlin, Germany")
    result = apply_filters([job], cfg, now=NOW)
    assert result.kept == []
    assert "level" not in job.raw


def test_a_sponsorship_country_needs_the_posting_to_say_so():
    cfg = {"filters": {"countries": ["PL"], "countries_if_sponsorship": ["GB"]}}
    silent = make_job(title="Data Scientist", location="London, UK",
                      description="Great team, hybrid working.")
    result = apply_filters([silent], cfg, now=NOW)
    assert result.kept == []
    assert "visa sponsorship" in result.rejected[0][1]

    offered = make_job(title="Data Scientist", location="London, UK",
                       description="Visa sponsorship is available for this role.")
    result = apply_filters([offered], cfg, now=NOW)
    assert result.kept == [offered]
    assert offered.country == "GB"


def test_demanding_a_visa_is_not_offering_one():
    """"Must have a valid work visa" is the opposite sentence — matching it
    would wave through exactly the postings the exception exists to drop."""
    cfg = {"filters": {"countries": ["PL"], "countries_if_sponsorship": ["GB"]}}
    job = make_job(title="Data Scientist", location="London, UK",
                   description="Applicants must have a valid work visa already.")
    assert apply_filters([job], cfg, now=NOW).kept == []


@pytest.mark.parametrize("refusal", [
    "Unfortunately we cannot sponsor visas at this time.",
    "We are unable to sponsor work visas.",
    "We do not sponsor visas for this position.",
    "We don't sponsor visas.",
    "Please note: no visa sponsorship is available.",
    "Visa sponsorship is not available for this role.",
    "This position does not offer visa sponsorship.",
    "Relocation and visa support is not provided.",
])
def test_refusing_sponsorship_is_not_offering_it(refusal):
    """Every refusal contains an offer-shaped substring ("…cannot SPONSOR
    VISAS…"), and refusals are the most common sponsorship sentence in GB
    ads — rescuing them would defeat the exception entirely."""
    cfg = {"filters": {"countries": ["PL"], "countries_if_sponsorship": ["GB"]}}
    job = make_job(title="Data Scientist", location="London, UK",
                   description=refusal)
    assert apply_filters([job], cfg, now=NOW).kept == []


def test_an_unrelated_negative_clause_does_not_cancel_the_offer():
    """The refusal screen is clause-bounded: "No agencies." in one sentence
    must not turn "Visa sponsorship is available." in the next into a no."""
    cfg = {"filters": {"countries": ["PL"], "countries_if_sponsorship": ["GB"]}}
    job = make_job(title="Data Scientist", location="London, UK",
                   description="No agencies please. Visa sponsorship is available.")
    assert apply_filters([job], cfg, now=NOW).kept == [job]


GERMAN_AD = (
    "Wir suchen einen Data Scientist für unser Team in Berlin. Du entwickelst "
    "Prognosemodelle, arbeitest eng mit dem Produktteam zusammen und "
    "präsentierst deine Ergebnisse den Stakeholdern. Sehr gute Deutschkenntnisse "
    "sind erforderlich, ebenso Erfahrung mit Python und SQL im Produktivbetrieb."
)


def test_explicit_german_requirement_conflicts_with_known_a2():
    cfg = {"filters": {"language_levels": {"de": "A2"}}}
    job = make_job(title="Data Scientist", description=GERMAN_AD)
    result = apply_filters([job], cfg, now=NOW)
    assert result.kept == []
    assert result.counts.get("language_requirement") == 1
    assert "Deutschkenntnisse" in result.rejected[0][1]


def test_an_english_description_passes_the_language_gate():
    cfg = {"filters": {"languages": ["en"]}}
    job = make_job(title="Data Scientist", description=(
        "We are looking for a data scientist to build forecasting models, "
        "work closely with product teams and present results to stakeholders. "
        "Strong Python and SQL experience in production is required."
    ))
    assert apply_filters([job], cfg, now=NOW).kept == [job]


def test_a_short_description_is_kept_in_doubt():
    """Two sources ship synthesized skill snippets; judging language on them
    is guesswork, and the safe direction is always "show the job"."""
    cfg = {"filters": {"languages": ["en"]}}
    job = make_job(title="Data Scientist", description="Prognosemodelle. SQL.")
    assert apply_filters([job], cfg, now=NOW).kept == [job]


def test_an_empty_language_list_disables_the_gate():
    job = make_job(title="Data Scientist", description=GERMAN_AD)
    assert apply_filters([job], {"filters": {}}, now=NOW).kept == [job]


def test_dedupe_merges_on_the_canonical_url_first():
    """The same posting arrives as the ATS record and as an aggregator copy
    whose URL only differs by tracking clutter — one job, and the ATS copy
    (richer source rank) is the survivor."""
    ats = make_job(source="greenhouse", company="Acme GmbH",
                   title="Data Scientist",
                   url="https://boards.greenhouse.io/acme/jobs/123",
                   description="Long ATS description with all the details.")
    copy = make_job(source="adzuna", company="ACME",
                    title="Data Scientist (m/f/d)",
                    url="https://boards.greenhouse.io/acme/jobs/123?utm_source=adzuna",
                    description="Short snippet.")
    survivors = dedupe([copy, ats])
    assert survivors == [ats]


def test_meaningful_query_parameters_keep_jobs_apart():
    """`?gh_jid=` IS the job identity on Greenhouse embed boards — stripping
    it would merge every posting a company has."""
    a = make_job(url="https://acme.example/careers?gh_jid=1", ats_job_id="1")
    b = make_job(url="https://acme.example/careers?gh_jid=2", ats_job_id="2",
                 title="Machine Learning Engineer")
    assert len(dedupe([a, b])) == 2


def test_jobs_without_urls_are_never_merged_by_the_url_pass():
    a = make_job(url="", ats_job_id="1", title="Data Scientist", company="A")
    b = make_job(url="", ats_job_id="2", title="ML Engineer", company="B")
    assert len(dedupe([a, b])) == 2
