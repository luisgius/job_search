"""Edge cases the FETCH + FILTER leg meets in a real European job week.

`test_filters.py`, `test_geo.py`, `test_ats_boards.py`, `test_adzuna.py` and
`test_linkedin_email.py` already cover the shapes the modules were designed
against. This file covers the shapes a *real* week produces and nobody
designed against: a French internship, a Greenhouse posting with five
offices, an alert email in German, an "EMEA (excluding UK)" location field,
and the same requisition opened in Berlin *and* Munich.

Three kinds of test live here, and the docstrings say which is which:

  * **good news** — the case is handled; the test stops a future change from
    breaking it;
  * **pinned trade-off** — the behaviour is surprising but deliberate and
    documented (`docs/EVALUATION.md`, or a comment in `src/`), so the test
    records the real behaviour and the reason;
  * **`xfail(strict=True)`** — the code genuinely mishandles a realistic
    case. Strict, so the day someone fixes it the test turns red and gets
    deleted rather than rotting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import geo
from src.config import DEFAULT_MAX_AGE_HOURS
from src.filters import (
    apply_filters,
    dedupe,
    is_fresh,
    passes_keywords,
    passes_location,
    passes_title,
)
from src.models import Job, normalize_company
from src.sources.adzuna import fetch as adzuna_fetch
from src.sources.adzuna import parse_result
from src.sources.ats_boards import fetch, fetch_greenhouse, fetch_lever
from src.sources.linkedin_email import extract_jobs_from_html
from tests.conftest import (
    NOW,
    FakeResponse,
    FakeSession,
    hours_ago,
    json_response,
    make_job,
    ms_epoch,
    write_config,
)

UTC = timezone.utc
RECEIVED = datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# local helpers
# --------------------------------------------------------------------------


def cfg(tmp_path, **filters):
    """A config with only the `filters` block overridden."""
    return write_config(tmp_path, {"filters": filters} if filters else None)


def fresh_cfg(tmp_path, **filters):
    """Same, but with the freshness gate wide open.

    Several tests below are about title/location/keywords and would otherwise
    be answered by `freshness.max_age_hours` before reaching the stage under
    test — whatever that window happens to be set to.
    """
    return write_config(
        tmp_path,
        {"filters": filters, "freshness": {"max_age_hours": 24 * 365}},
    )


def gh_session(payload):
    return FakeSession([("boards-api.greenhouse.io", json_response(payload))])


def lever_session(payload):
    return FakeSession([("api.lever.co", json_response(payload))])


def gh_posting(**overrides):
    """One structurally complete Greenhouse posting."""
    posting = {
        "id": 1,
        "title": "Backend Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "Berlin, Germany"},
        "content": "&lt;p&gt;Python and PostgreSQL.&lt;/p&gt;",
        "first_published": "2026-08-04T07:00:00Z",
    }
    posting.update(overrides)
    return posting


def lever_posting(**overrides):
    """One structurally complete Lever posting."""
    posting = {
        "id": "a1",
        "text": "Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/globex/a1",
        "categories": {"location": "Berlin, Germany", "commitment": "Full-time"},
        "createdAt": ms_epoch(hours_ago(2)),
        "descriptionPlain": "Python and PostgreSQL.",
    }
    posting.update(overrides)
    return posting


def adzuna_result(**overrides):
    result = {
        "id": "5012345678",
        "title": "Backend Engineer",
        "redirect_url": "https://www.adzuna.de/land/ad/5012345678",
        "created": "2026-08-04T07:00:00Z",
        "company": {"display_name": "Northwind GmbH"},
        "location": {"display_name": "Berlin, Berlin", "area": ["Germany", "Berlin"]},
        "description": "Python backend engineering with PostgreSQL. " * 4,
    }
    result.update(overrides)
    return result


# ==========================================================================
# multilingual titles — an EU search is not an English-language search
# ==========================================================================


@pytest.mark.parametrize(
    "title",
    [
        "Werkstudent (m/w/d) Backend Development",
        "Praktikum im Bereich Softwareentwicklung (m/w/d)",
        "Working Student (f/m/x) Data Engineering",
        "Werkstudent:in Softwareentwicklung",
    ],
)
def test_the_german_terms_the_defaults_do_list_survive_their_decorations(tmp_path, title):
    """German ads never write a bare "Werkstudent" — they write
    "Werkstudent (m/w/d)", "Werkstudent:in", "Praktikum im Bereich …". The
    gendering suffix must not stop the shipped exclusion from firing."""
    ok, reason = passes_title(make_job(title=title), cfg(tmp_path))
    assert ok is False
    assert reason


def test_the_shipped_exclusions_cover_the_other_eu_languages(tmp_path):
    """One week of a Berlin/Paris/Madrid/Amsterdam/Warsaw search returns all
    of these, and every one of them is an internship, an apprenticeship or a
    dual-study place that a senior engineer cannot take. Each costs a paid
    LLM call and a slot in the digest."""
    conf = fresh_cfg(tmp_path)
    missed = [
        title
        for title in (
            "Stage - Developpeur Backend (H/F)",          # FR internship
            "Alternance Ingenieur Logiciel (H/F)",        # FR apprenticeship
            "Becario Desarrollo Backend",                 # ES intern
            "Practicas en Desarrollo de Software",        # ES internship
            "Stagiair Software Engineer",                 # NL intern
            "Praktyki Programista Backend",               # PL internship
            "Staz Programista Python",                    # PL internship
            "Ausbildung zum Fachinformatiker",            # DE apprenticeship
            "Duales Studium Informatik",                  # DE dual study
        )
        if passes_title(make_job(title=title), conf)[0]
    ]
    assert missed == []


def test_plural_and_british_variants_of_a_listed_exclusion_are_caught(tmp_path):
    """`title_exclude` already lists "intern", "internship", "apprentice" and
    "graduate program". A board writing the plural, the -ship form or the
    British spelling walks straight past all three — and boards do."""
    conf = fresh_cfg(tmp_path)
    missed = [
        title
        for title in (
            "Summer Internships 2026 - Backend",
            "Software Engineering Apprenticeship",
            "Graduate Programme 2026 - Engineering",
        )
        if passes_title(make_job(title=title), conf)[0]
    ]
    assert missed == []


def test_the_exclusion_machinery_itself_handles_every_language(tmp_path):
    """Good news, and the reason the two xfails above are config gaps rather
    than parser gaps: once the terms are in `filters.title_exclude`, the
    accent-folded whole-word matcher rejects all of them, accents and all."""
    conf = fresh_cfg(
        tmp_path,
        title_exclude=["stage", "alternance", "becario", "practicas", "stagiair",
                       "praktyki", "staz", "ausbildung", "duales studium"],
    )
    for title in ("Stage - Développeur Backend", "Alternance Ingénieur",
                  "Becario Desarrollo", "Prácticas en Desarrollo",
                  "Stagiair Software Engineer", "Praktyki Programista",
                  "Staż Programista", "Ausbildung zum Fachinformatiker",
                  "Duales Studium Informatik"):
        assert passes_title(make_job(title=title), conf)[0] is False, title


def test_the_word_stage_does_not_eat_backstage_or_staging(tmp_path):
    """The cost of adding "stage" to the exclusion list, checked before
    recommending it: "stage" is also an English word. Whole-word matching
    means only the standalone French sense is hit."""
    conf = fresh_cfg(tmp_path, title_exclude=["stage"])
    assert passes_title(make_job(title="Backstage Platform Engineer"), conf)[0] is True
    assert passes_title(make_job(title="Staging Environments Engineer"), conf)[0] is True
    assert passes_title(make_job(title="Stage Développeur"), conf)[0] is False


@pytest.mark.parametrize(
    "title",
    ["🚀 Backend Engineer", "Backend\xa0Engineer", "Backend Engineer ⚡",
     "Backend Engineer (m/w/d)", "Backend Engineer [Berlin]"],
)
def test_emoji_nbsp_and_bracket_decoration_do_not_hide_a_title(tmp_path, title):
    """Boards decorate titles with emoji, non-breaking spaces and bracketed
    tails. None of it may stop `title_include` from recognising the role."""
    conf = fresh_cfg(tmp_path, title_include=["backend"], title_exclude=[])
    assert passes_title(make_job(title=title), conf)[0] is True


def test_an_invisible_character_inside_a_word_does_not_split_it(tmp_path):
    """German boards emit soft hyphens for hyphenation ("Software\xadentwickler")
    and rich-text editors leak zero-width spaces. The title still *reads*
    "Backend Engineer" to a human, but the include list stops matching it and
    the job vanishes with a "matches none of filters.title_include" reason
    the user cannot act on."""
    conf = fresh_cfg(tmp_path, title_include=["backend"], title_exclude=[])
    assert passes_title(make_job(title="Back​end Engineer"), conf)[0] is True
    assert passes_title(make_job(title="Back­end Engineer"), conf)[0] is True


# ==========================================================================
# seniority and role-family traps
# ==========================================================================


@pytest.mark.parametrize(
    "title",
    ["Recruiter, Engineering", "Technical Recruiter - Engineering",
     "Head of Engineering", "Engineering Manager", "Director of Engineering",
     "Solutions Architect (Pre-Sales)", "Technical Account Manager"],
)
def test_an_engineer_include_list_does_not_admit_the_engineering_adjacent(tmp_path, title):
    """Good news: "Recruiter, Engineering" is the trap an `in` test would fall
    into, because "Engineering" contains "Engineer". Whole-word matching means
    an IC hunting for "engineer" is not handed the recruiter who hires them."""
    conf = fresh_cfg(tmp_path, title_include=["engineer", "developer"])
    assert passes_title(make_job(title=title), conf)[0] is False


def test_a_sales_engineer_passes_an_engineer_include_list(tmp_path):
    """Pinned trade-off. "Sales Engineer" really is an engineer by title, and
    `filters.py` is deliberately lexical — role-family judgement is the LLM
    scorer's job (ARCHITECTURE.md: filters are the cheap gate in front of
    scoring). The escape hatch is `title_exclude`, checked here too."""
    conf = fresh_cfg(tmp_path, title_include=["engineer"])
    assert passes_title(make_job(title="Sales Engineer"), conf)[0] is True

    stricter = fresh_cfg(tmp_path, title_include=["engineer"],
                         title_exclude=["sales", "pre sales"])
    assert passes_title(make_job(title="Sales Engineer"), stricter)[0] is False


def test_the_shipped_defaults_send_management_titles_to_the_scorer(tmp_path):
    """Pinned deliberate behaviour: `filters.title_include` ships empty, and
    an empty include list means "any title is fine" rather than "nothing
    matches" (`passes_title` docstring). So on a default config an IC's run
    pays to score "Head of Engineering". That is the documented cost of the
    default, and the fix is configuration, not code."""
    conf = fresh_cfg(tmp_path)
    assert passes_title(make_job(title="Head of Engineering"), conf)[0] is True
    assert passes_title(make_job(title="VP of Engineering"), conf)[0] is True


# ==========================================================================
# contract shapes
# ==========================================================================


def test_structured_employment_type_is_filtered_not_just_recorded(tmp_path):
    """Lever states the employment type as structured data; the title often
    does not. A posting titled plainly "Software Engineer" with
    `commitment: Internship` — or an Adzuna result with
    `contract_type: contract` — is exactly what someone hunting a permanent
    senior role must not be shown, and the data to spot it was already
    fetched."""
    conf = fresh_cfg(tmp_path)

    internship = fetch_lever(
        "globex",
        session=lever_session([lever_posting(
            text="Software Engineer",
            categories={"location": "Berlin, Germany", "commitment": "Internship"},
        )]),
    )[0]
    assert internship.raw["commitment"] == "Internship"

    contractor = parse_result(
        adzuna_result(title="Backend Engineer", contract_type="contract"), "de"
    )
    assert contractor.raw["contract_type"] == "contract"

    # The shipped default drops the internship, whose title gives nothing away.
    assert apply_filters([internship], conf, now=NOW).kept == []

    # It deliberately does NOT drop contract work — see the companion test —
    # but the mechanism is there for anyone who only wants permanent roles.
    permanent_only = fresh_cfg(
        tmp_path, employment_type_exclude=["internship", "contract"]
    )
    assert apply_filters([contractor], permanent_only, now=NOW).kept == []


def test_the_shipped_default_does_not_delete_contract_work(tmp_path):
    """A default that deletes jobs is the wrong kind of opinionated. Plenty of
    real EU tech work is contract or B2B — Poland and the Netherlands
    especially — and a dropped posting is invisible forever, while a wrong
    card costs a glance. `contract` and `freelance` are therefore left out of
    the shipped `employment_type_exclude` and commented in config.yaml for
    anyone who wants them."""
    conf = fresh_cfg(tmp_path)
    contractor = parse_result(
        adzuna_result(title="Backend Engineer", contract_type="contract"), "de"
    )
    assert apply_filters([contractor], conf, now=NOW).kept == [contractor]


def test_freelance_and_fixed_term_titles_need_an_explicit_exclusion(tmp_path):
    """Pinned trade-off, with the remedy in the same test. B2B/freelance,
    interim and fixed-term contracts dominate some EU markets (Poland and the
    Netherlands especially); the shipped defaults let them through, and one
    `title_exclude` line stops all of them."""
    conf = fresh_cfg(tmp_path)
    shapes = ["Freelance Backend Developer (B2B)",
              "Interim Head of Data",
              "Backend Engineer - 6 month FTC",
              "Backend Engineer (Werkvertrag)"]
    assert all(passes_title(make_job(title=t), conf)[0] for t in shapes)

    stricter = fresh_cfg(
        tmp_path,
        title_exclude=["freelance", "b2b", "interim", "ftc", "werkvertrag"],
    )
    assert not any(passes_title(make_job(title=t), stricter)[0] for t in shapes)


# ==========================================================================
# location reality
# ==========================================================================


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Berlin or Munich or Remote", "DE"),
        ("Hybrid 3 days/week Amsterdam", "NL"),
        ("Dublin (relocation required)", "IE"),
        ("Remote - Poland only", "PL"),
        ("Warsaw, Poland (hybrid, 2 days in office)", "PL"),
        ("Barcelona or Madrid", "ES"),
    ],
)
def test_the_location_strings_recruiters_actually_type_resolve(location, expected):
    """Good news. None of these is the tidy "City, Country" the parser was
    written for, and all of them appear on real boards in a single week."""
    assert geo.country_of(location) == expected


def test_a_timezone_only_location_is_kept_as_remote_with_a_european_hint():
    """"Anywhere in CET ±2" names no country at all. It must not resolve to
    one, but CET is enough of a European hint to survive
    `remote_requires_eu_hint` — the alternative is losing every
    timezone-scoped remote role."""
    assert geo.country_of("Anywhere in CET ±2") is None
    assert geo.is_remote("Anywhere in CET ±2") is True
    assert geo.mentions_eu("Anywhere in CET ±2") is True


def test_a_location_that_names_a_country_in_order_to_exclude_it(tmp_path):
    """A pan-European posting that says "EMEA (excluding UK)" is a job an EU
    applicant can take. It resolves to the United Kingdom, so it is either
    filed under the wrong country or — for anyone whose `filters.countries`
    leaves GB out, which is the common post-Brexit setup — rejected outright
    with a reason that reads like a parser bug."""
    assert geo.country_of("EMEA (excluding UK)") != "GB"

    conf = fresh_cfg(tmp_path, countries=["DE", "NL", "ES"])
    job = make_job(location="Remote - EMEA (excluding UK)", remote=True)
    assert passes_location(job, conf)[0] is True


def test_a_remote_role_open_in_several_countries_is_not_pinned_to_the_last(tmp_path):
    """"Remote (Portugal, Spain, Poland)" is one job you may take from any of
    three countries. It resolves to PL only, so an applicant based in Lisbon
    whose `filters.countries` is [PT, ES] never sees it."""
    conf = fresh_cfg(tmp_path, countries=["PT", "ES"])
    job = make_job(location="Remote (Portugal, Spain, Poland)", remote=True)
    ok, reason = passes_location(job, conf)
    assert ok is True, reason


def test_a_us_only_remote_role_is_not_rescued_by_a_european_office(tmp_path):
    """Half of all US company descriptions mention their European offices.
    The location field here says "Remote (US)" in as many words, which is the
    single most authoritative statement in the posting, and it is never
    consulted once the job is known to be remote. This is the expensive
    mistake `geo.py` exists to prevent, arriving through `filters.py`."""
    conf = fresh_cfg(tmp_path)
    job = make_job(
        location="Remote (US)",
        remote=True,
        description="Work from anywhere in the US. We also have an engineering "
                    "office in Berlin and one in Dublin.",
    )
    assert passes_location(job, conf)[0] is False


def test_a_country_the_source_already_knows_is_believed(tmp_path):
    """A result from `api.adzuna.com/.../jobs/de/...` is a German posting —
    the index is the country, and `adzuna.parse_result` records that on
    `Job.country`. When the result also lacks a `location` node (Adzuna does
    emit those), the location gate throws that knowledge away and rejects the
    job as "could not be resolved"."""
    job = parse_result(
        {"id": "1", "title": "Backend Engineer",
         "redirect_url": "https://www.adzuna.de/land/ad/1",
         "created": "2026-08-04T07:00:00Z",
         "description": "Python and PostgreSQL backend engineering. " * 4},
        "de",
    )
    assert job.country == "DE"
    ok, reason = passes_location(job, fresh_cfg(tmp_path))
    assert ok is True, reason


def test_a_multiple_locations_placeholder_is_rejected_honestly(tmp_path):
    """Greenhouse boards use "Multiple Locations" as a location. It is not a
    place, so the job is dropped — but the reason has to say "could not be
    resolved" rather than naming a country, because the two call for
    completely different fixes."""
    ok, reason = passes_location(make_job(location="Multiple Locations"),
                                 fresh_cfg(tmp_path))
    assert ok is False
    assert "could not be resolved" in reason


def test_a_uk_role_is_rejected_by_name_when_gb_is_off_the_list(tmp_path):
    """The commonest post-Brexit configuration. The rejection must name the
    United Kingdom so the user can tell "I excluded this" from "the parser
    failed"."""
    conf = fresh_cfg(tmp_path, countries=["DE", "NL", "IE"])
    ok, reason = passes_location(make_job(location="London, UK"), conf)
    assert ok is False
    assert "United Kingdom" in reason and "GB" in reason


def test_switzerland_and_norway_ship_inside_the_default_country_list(tmp_path):
    """Pinned deliberately, because it surprises people: the defaults are the
    European *job market*, not the EU. Zürich and Oslo are kept out of the box
    even though neither country is in the EU — which matters, since a Swiss
    role usually needs a permit an EU passport does not grant."""
    conf = fresh_cfg(tmp_path)
    assert passes_location(make_job(location="Zürich, Switzerland"), conf)[0] is True
    assert passes_location(make_job(location="Oslo, Norway"), conf)[0] is True
    assert "CH" in geo.EU_COUNTRIES and "NO" in geo.EU_COUNTRIES


def test_an_empty_countries_list_switches_the_location_gate_off_entirely(tmp_path):
    """Pinned deliberate behaviour with a sharp edge (`_check_location`: "no
    country list configured -> no location gate"). Emptying
    `filters.countries` to "search everywhere in Europe" actually admits San
    Francisco, because there is then nothing left to compare against."""
    conf = fresh_cfg(tmp_path, countries=[])
    assert passes_location(make_job(location="San Francisco, CA"), conf)[0] is True
    assert passes_location(make_job(location="Remote (US)"), conf)[0] is True


def test_countries_written_as_a_bare_yaml_string_still_gates(tmp_path):
    """`countries: DE` is what someone hunting in one country writes, and YAML
    hands it over as a string rather than a list. Reading it character by
    character would produce the country set {"D", "E"} and drop everything."""
    conf = fresh_cfg(tmp_path, countries="DE")
    assert passes_location(make_job(location="Berlin, Germany"), conf)[0] is True
    assert passes_location(make_job(location="Madrid, Spain"), conf)[0] is False


def test_a_hybrid_location_beats_remote_boilerplate_in_the_description():
    """Pinned deliberate precedence (`geo.is_remote`): the structured field
    states the arrangement, the prose is company marketing. "Hybrid — 3 days
    in Amsterdam" plus a description bragging about a remote-first culture is
    a commute, and calling it remote would send it through the remote branch
    of the location gate instead of the Dutch one."""
    assert geo.is_remote(
        "Hybrid - Amsterdam",
        description="We are a fully remote, work from home first company.",
    ) is False


# ==========================================================================
# freshness reality
# ==========================================================================


def test_a_lever_board_emitting_seconds_dates_the_same_as_milliseconds():
    """`createdAt` is documented as a millisecond epoch, and a board that
    hands over plain seconds must not land in 1970 (undated, silently
    dropped) or the year 57000 (permanently fresh)."""
    posted = hours_ago(3)
    in_ms = fetch_lever("globex", session=lever_session(
        [lever_posting(createdAt=ms_epoch(posted))]))[0]
    in_seconds = fetch_lever("globex", session=lever_session(
        [lever_posting(createdAt=int(posted.timestamp()))]))[0]
    as_string = fetch_lever("globex", session=lever_session(
        [lever_posting(createdAt=str(ms_epoch(posted)))]))[0]

    assert in_ms.posted_at == in_seconds.posted_at == as_string.posted_at
    assert is_fresh(in_seconds, 24, now=NOW)[0] is True


def test_a_zero_epoch_dates_a_posting_to_1970_instead_of_leaving_it_undated():
    """Pinned, because the digest's funnel counts depend on it: a Lever board
    emitting `createdAt: 0` produces a *stale* rejection, not an *undated*
    one. Both drop the job, but only "undated" would prompt the user to try
    `freshness.skip_undated: false` — which would not help here."""
    job = fetch_lever("globex", session=lever_session([lever_posting(createdAt=0)]))[0]
    assert job.posted_at is not None and job.posted_at.year == 1970
    ok, _ = is_fresh(job, 24, now=NOW)
    assert ok is False


def test_a_corrupt_first_published_falls_back_to_updated_at():
    """A date the parser cannot read must not poison the posting: Greenhouse's
    `first_published` is preferred, but when it is junk the job has to fall
    back to `updated_at` rather than becoming undated and being dropped."""
    job = fetch_greenhouse("acme", session=gh_session({"jobs": [gh_posting(
        first_published="0000-00-00", updated_at="2026-08-04T07:00:00Z")]}))[0]
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_a_summer_offset_is_converted_rather_than_read_as_utc():
    """Central European Summer Time is UTC+2, so a posting stamped
    "2026-08-03T10:00:00+02:00" is 08:00 UTC — 25h before this run and
    therefore stale. Reading the local wall clock as UTC would make it 23h old
    and let a day-old posting through every morning during DST."""
    job = fetch_greenhouse("acme", session=gh_session({"jobs": [gh_posting(
        first_published="2026-08-03T10:00:00+02:00")]}))[0]
    assert job.posted_at == datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    ok, reason = is_fresh(job, 24, now=NOW)
    assert ok is False
    assert "25.0h" in reason


def test_a_posting_past_the_configured_window_is_invisible(tmp_path):
    """Pinned known limitation (docs/EVALUATION.md §9.4, "a --since /
    catch-up flag"): whatever `freshness.max_age_hours` says, the hour after
    it is a cliff. A posting that falls off it is absent from that morning's
    run with no trace anywhere, so a missed run really does mean a missed day.

    Written against the configured window rather than against a number of
    hours, because the number has moved once already — 24 to 72, see
    `config.DEFAULT_MAX_AGE_HOURS` — and the cliff is the part that matters.
    Both sides are asserted: a test that only shows the drop cannot tell a
    working boundary from a filter that rejects everything.
    """
    window = DEFAULT_MAX_AGE_HOURS
    inside = make_job(hours_old=window - 1, ats_job_id="inside")
    outside = make_job(hours_old=window + 1, ats_job_id="outside")
    result = apply_filters([inside, outside], cfg(tmp_path), now=NOW)
    assert [job.ats_job_id for job in result.kept] == ["inside"]
    assert result.counts == {"stale": 1}


def test_a_repost_dated_slightly_ahead_of_the_run_is_still_fresh():
    """Boards run on their own clocks and a posting timestamped a few minutes
    into the future is ordinary skew. Treating a negative age as "very old"
    would drop the freshest jobs on the board."""
    job = make_job(posted_at=NOW + timedelta(minutes=20))
    ok, reason = is_fresh(job, 24, now=NOW)
    assert ok is True
    assert reason == ""


# ==========================================================================
# payload reality
# ==========================================================================


def test_a_board_with_eight_hundred_postings_is_parsed_whole():
    """Big employers really do publish this many open reqs. There is no page
    size on the Greenhouse board endpoint, so a silent cap here would quietly
    hide half a company forever."""
    payload = {"jobs": [gh_posting(id=i, title=f"Backend Engineer {i}",
                                   absolute_url=f"https://x/{i}")
                        for i in range(800)]}
    jobs = fetch_greenhouse("acme", session=gh_session(payload))
    assert len(jobs) == 800
    assert len({j.ats_job_id for j in jobs}) == 800


def test_a_four_hundred_kilobyte_description_reaches_the_filters_intact():
    """Some ads paste an entire benefits handbook into `content`. Truncating
    at the source would hide the requirements from the keyword filter and the
    scorer; the length budget belongs to the prompt builder, not here."""
    body = "Python and PostgreSQL and Kafka. " * 12_000
    job = fetch_greenhouse("acme", session=gh_session(
        {"jobs": [gh_posting(content=body)]}))[0]
    assert len(job.description) > 300_000
    assert job.description.startswith("Python and PostgreSQL")


def test_a_description_that_is_only_an_embedded_image_is_dropped_on_length(tmp_path):
    """Design-led companies publish the ad as one big base64 image. After the
    HTML is flattened there is no text at all, so the scorer would be judging
    a title alone — `min_description_chars` is what catches it, and the
    rejection has to say so."""
    job = fetch_greenhouse("acme", session=gh_session({"jobs": [gh_posting(
        content="&lt;p&gt;&lt;img src=\"data:image/png;base64,iVBORw0KGgoAAAANSUhEUg\"/"
                "&gt;&lt;/p&gt;")]}))[0]
    assert job.description == ""
    result = apply_filters([job], cfg(tmp_path, min_description_chars=200), now=NOW)
    assert result.counts == {"description_too_short": 1}


def test_unclosed_tags_and_a_stray_angle_bracket_flatten_to_readable_text():
    """Greenhouse `content` is entity-escaped HTML pasted by humans: tags go
    unclosed and "&gt;" is used as a comparison operator. Neither may swallow
    the surrounding sentence — the keyword filter reads this text."""
    job = fetch_greenhouse("acme", session=gh_session({"jobs": [gh_posting(
        content="&lt;p&gt;We pay &amp;gt; 80k&lt;p&gt;&lt;b&gt;Python&lt;/p&gt; "
                "latency &amp;lt; 5ms")]}))[0]
    assert "We pay > 80k" in job.description
    assert "Python" in job.description
    assert "latency < 5ms" in job.description
    assert "<" not in job.description.replace("latency < 5ms", "")


def test_a_whitespace_only_title_is_skipped_like_a_missing_one():
    """A posting whose title is a non-breaking space is unusable — but it must
    cost that posting only, not the board."""
    payload = {"jobs": [gh_posting(id=1, title="  \xa0  "),
                        gh_posting(id=2, absolute_url="https://x/2")]}
    jobs = fetch_greenhouse("acme", session=gh_session(payload))
    assert [j.ats_job_id for j in jobs] == ["2"]


def test_a_board_answering_with_an_html_error_page_costs_only_that_board(tmp_path):
    """During an incident these endpoints return 200 with an HTML status page
    rather than a 5xx. That has to be reported as a per-slug failure, not
    parsed into zero jobs and silently believed."""
    conf = write_config(tmp_path, {"sources": {"greenhouse": True, "lever": False}},
                        watchlist={"greenhouse": ["broken", "acme"]})
    session = FakeSession([
        ("boards/broken/", FakeResponse(status_code=200,
                                        text="<html>We'll be right back</html>")),
        ("boards/acme/", json_response({"jobs": [gh_posting()]})),
    ])
    errors: list[str] = []
    jobs = fetch(conf, session=session, errors=errors)
    assert len(jobs) == 1
    assert len(errors) == 1 and "greenhouse/broken" in errors[0]


@pytest.mark.parametrize(
    "name,normalised",
    [("Marks & Spencer", "marks spencer"),
     ("Foo/Bar GmbH", "foo bar"),
     ("Booking.com", "booking com"),
     ("N26 GmbH", "n26")],
)
def test_ampersands_and_slashes_in_a_company_name_normalise_stably(name, normalised):
    """`Job.key` and `Job.dedupe_key` hash the normalised company, so a name
    with punctuation must normalise to the same thing on every run — otherwise
    the tracker cannot recognise a job it has already seen."""
    assert normalize_company(name) == normalised
    assert normalize_company(name) == normalize_company(name.upper())


def test_the_only_european_office_survives_a_long_office_list():
    """US companies list their offices home-first: SF, NYC, Austin, Seattle,
    then Berlin. Everything past the fourth is cut, and what is left looks
    unambiguously American — so the one posting in the batch that was
    genuinely open in Berlin is rejected as a US role. Both boards do it."""
    greenhouse = fetch_greenhouse("acme", session=gh_session({"jobs": [gh_posting(
        location=None,
        offices=[{"name": "SF", "location": "San Francisco, CA"},
                 {"name": "NYC", "location": "New York, NY"},
                 {"name": "ATX", "location": "Austin, TX"},
                 {"name": "SEA", "location": "Seattle, WA"},
                 {"name": "BER", "location": "Berlin, Germany"}])]}))[0]

    lever = fetch_lever("globex", session=lever_session([lever_posting(
        categories={"location": "San Francisco, CA",
                    "allLocations": ["New York, NY", "Austin, TX", "Seattle, WA",
                                     "Berlin, Germany"]})]))[0]

    assert geo.country_of(greenhouse.location) == "DE"
    assert geo.country_of(lever.location) == "DE"


# ==========================================================================
# LinkedIn alert-email reality
# ==========================================================================


def test_a_german_alert_still_yields_its_jobs():
    """Good news first: a German-language alert is structurally identical, so
    titles, links and the company/location blocks come out fine."""
    html = """
    <div><a href="https://www.linkedin.com/comm/jobs/view/2001/?trk=eml">
    Backend Entwickler (m/w/d)</a></div>
    <div>Globex SE</div><div>M&uuml;nchen, Deutschland</div><div>vor 5 Stunden</div>
    """
    job = extract_jobs_from_html(html, received_at=RECEIVED)[0]
    assert job.title == "Backend Entwickler (m/w/d)"
    assert job.company == "Globex SE"
    assert job.location == "München, Deutschland"


def test_an_english_promoted_badge_is_not_mistaken_for_the_employer():
    """"Promoted" sits between the title link and the company on sponsored
    cards, which are mixed in with the organic results in every digest."""
    html = """
    <a href="https://www.linkedin.com/comm/jobs/view/1112/?trk=eml">Senior Backend Engineer</a>
    <span>Promoted</span><span>Northwind</span><span>Berlin, Germany</span>
    <span>3 hours ago</span>
    """
    job = extract_jobs_from_html(html, received_at=RECEIVED)[0]
    assert job.company == "Northwind"
    assert job.location == "Berlin, Germany"


def test_a_localised_alert_does_not_file_its_chrome_as_the_employer():
    """A German alert says "Anzeige" where an English one says "Promoted", and
    "Alle Jobs anzeigen" where an English one says "See all jobs". Both slip
    through and become the company — which is not merely ugly: `Job.key`
    hashes the company for any posting with no ATS id, so the tracker cannot
    recognise the same job tomorrow and the digest re-offers it."""
    promoted = """
    <a href="https://www.linkedin.com/comm/jobs/view/1111/?trk=eml">Senior Backend Entwickler</a>
    <span>Anzeige</span><span>Northwind GmbH</span><span>Berlin, Deutschland</span>
    """
    with_footer = """
    <div><a href="https://www.linkedin.com/comm/jobs/view/2002/">Data Engineer</a></div>
    <div><a href="https://www.linkedin.com/jobs/search">Alle Jobs anzeigen</a></div>
    <div><a href="https://www.linkedin.com/unsub">Abmelden</a></div>
    """
    assert extract_jobs_from_html(promoted, received_at=RECEIVED)[0].company \
        == "Northwind GmbH"
    assert extract_jobs_from_html(with_footer, received_at=RECEIVED)[0].company == ""


def test_a_click_tracking_redirector_still_yields_the_job():
    """LinkedIn routes some alert links through `click.linkedin.com/?url=…`.
    The href still contains the posting URL, but the tracking parameters that
    follow it run into the id, and a percent-encoded variant hides it
    entirely. Either way the card produces nothing — and because the whole
    email uses one link style, the failure is all-or-nothing and silent."""
    plain = ("<a href=\"https://click.linkedin.com/r/?url=https://www.linkedin.com"
             "/jobs/view/3001&trk=eml-alert\">Platform Engineer</a><div>Initech</div>")
    encoded = ("<a href=\"https://click.linkedin.com/r/?url=https%3A%2F%2Fwww.linkedin"
               ".com%2Fjobs%2Fview%2F3002&trk=x\">Platform Engineer</a><div>Initech</div>")
    assert [j.raw["linkedin_job_id"]
            for j in extract_jobs_from_html(plain, received_at=RECEIVED)] == ["3001"]
    assert [j.raw["linkedin_job_id"]
            for j in extract_jobs_from_html(encoded, received_at=RECEIVED)] == ["3002"]


def test_a_slug_style_job_url_still_yields_the_numeric_id():
    """LinkedIn writes both `/jobs/view/3987654321` and
    `/jobs/view/senior-backend-engineer-at-acme-3987654321`. The id is the
    dedupe key across alerts, so the slug form must not become its own job."""
    html = ("<a href=\"https://www.linkedin.com/comm/jobs/view/"
            "senior-backend-engineer-at-acme-3987654321?trk=eml\">Senior Backend "
            "Engineer</a><div>Acme</div><div>Berlin, Germany</div>")
    job = extract_jobs_from_html(html, received_at=RECEIVED)[0]
    assert job.raw["linkedin_job_id"] == "3987654321"
    assert job.url == "https://www.linkedin.com/jobs/view/3987654321"


def test_one_job_matching_two_saved_searches_is_emitted_once():
    """A single alert email covers several saved searches, and a broad role
    matches more than one of them. Without id-level de-duplication the digest
    shows the same posting once per search you set up."""
    html = """
    <h2>Your job alert for backend engineer</h2>
    <a href="https://www.linkedin.com/comm/jobs/view/4001/?trk=a">Backend Engineer</a>
    <div>Acme</div><div>Berlin, Germany</div>
    <h2>Your job alert for python</h2>
    <a href="https://www.linkedin.com/comm/jobs/view/4001/?trk=b">Backend Engineer</a>
    <div>Acme</div><div>Berlin, Germany</div>
    """
    jobs = extract_jobs_from_html(html, received_at=RECEIVED)
    assert len(jobs) == 1
    assert jobs[0].raw["linkedin_job_id"] == "4001"


# ==========================================================================
# Adzuna reality
# ==========================================================================


def test_a_confidential_listing_keeps_its_placeholder_employer(tmp_path):
    """Agencies post under "Confidential" or under their own name rather than
    the employer's. Dropping those would lose a large slice of the aggregator
    feed, so they are kept — the scorer and the human read the description."""
    job = parse_result(
        adzuna_result(company={"display_name": "Confidential"}), "de")
    assert job.company == "Confidential"
    assert apply_filters([job], fresh_cfg(tmp_path), now=NOW).kept == [job]


@pytest.mark.parametrize(
    "country,symbol",
    [("de", "€"), ("at", "€"), ("nl", "€"), ("gb", "£"), ("ch", "CHF"), ("pl", "zł")],
)
def test_a_salary_is_printed_in_the_currency_of_the_index(country, symbol):
    """Adzuna's country index *is* the currency: a Swiss posting quotes CHF
    and a Polish one złoty. Printing "€120,000" for a 120k CHF Zürich salary
    would be a materially misleading number in the digest."""
    job = parse_result(adzuna_result(salary_min=120000, salary_max=140000), country)
    assert symbol in job.salary


def test_a_predicted_salary_and_an_open_ended_range_are_labelled():
    """Half of Adzuna's salary fields are modelled, and plenty carry only a
    floor. Both have to read as what they are rather than as a quoted offer."""
    predicted = parse_result(
        adzuna_result(salary_min=70000, salary_is_predicted="1"), "de")
    assert predicted.salary.startswith("from")
    assert "estimated" in predicted.salary


def test_a_page_longer_than_results_per_page_is_hard_capped(tmp_path):
    """Adzuna occasionally returns more rows than asked for. The per-page cap
    is the only ceiling on how many jobs one broad query can push into a run,
    so it has to be enforced on the response, not just on the request."""
    conf = write_config(
        tmp_path,
        {"sources": {"greenhouse": False, "adzuna": True},
         "keys": {"adzuna_app_id": "app-id", "adzuna_app_key": "app-key"}},
        watchlist={"adzuna": {"countries": ["de"], "queries": ["python"],
                              "results_per_page": 5}},
    )
    payload = {"count": 40,
               "results": [adzuna_result(id=str(i), redirect_url=f"https://a/{i}")
                           for i in range(20)]}
    session = FakeSession([("api.adzuna.com", json_response(payload))])
    assert len(adzuna_fetch(conf, session=session)) == 5


# ==========================================================================
# dedupe reality
# ==========================================================================


def test_the_same_role_seen_as_berlin_and_as_berlin_germany_is_one_job():
    """The case `dedupe` stamps the country for: an ATS says "Berlin, Germany"
    and a LinkedIn alert says "Berlin". Without the stamp the fallback key is
    the raw location and the two never meet."""
    ats = make_job(source="greenhouse", location="Berlin, Germany",
                   description="x" * 800, country=None)
    email = make_job(source="linkedin_email", location="Berlin", description="",
                     ats=None, ats_job_id=None, country=None,
                     url="https://www.linkedin.com/jobs/view/1")
    out = dedupe([ats, email])
    assert len(out) == 1
    assert out[0].source == "greenhouse"
    assert out[0].country == "DE"


def test_a_legal_suffix_merges_but_a_longer_trading_name_does_not():
    """"Acme" and "Acme GmbH" are one employer and must collapse. "Acme" and
    "Acme Technologies GmbH" are left apart on purpose — merging on a prefix
    would fold genuinely different companies (Deutsche Bank / Deutsche Post)
    into one, which is the more expensive mistake."""
    plain = make_job(company="Acme", country="DE", ats=None, ats_job_id=None)
    suffixed = make_job(company="Acme GmbH", country="DE", ats=None, ats_job_id=None)
    longer = make_job(company="Acme Technologies GmbH", country="DE",
                      ats=None, ats_job_id=None)
    assert len(dedupe([plain, suffixed])) == 1
    assert len(dedupe([plain, longer])) == 2


def test_the_same_title_opened_in_berlin_and_munich_is_two_jobs():
    """A company opening one role per office publishes two requisitions with
    two ATS ids, two apply links and two hiring managers. Both resolve to DE,
    and `dedupe_key` is company + normalised title + country — so one of them
    is deleted before it is ever filtered, scored or shown, and nothing in the
    funnel counts says a job was dropped."""
    berlin = make_job(location="Berlin, Germany", ats_job_id="1",
                      url="https://boards.greenhouse.io/acme/jobs/1")
    munich = make_job(location="Munich, Germany", ats_job_id="2",
                      url="https://boards.greenhouse.io/acme/jobs/2")
    assert len(dedupe([berlin, munich])) == 2


def test_a_repost_does_not_lose_the_fresh_copy_to_the_stale_one():
    """Boards accumulate: the same role sits there as a two-day-old req and as
    today's repost with a new id. The two tie on date-present, description
    length and source, so the first one in the payload wins — and if that is
    the old one, freshness then drops it and the fresh copy is already gone.
    The job disappears with a "stale" reason for a posting made this morning."""
    old = make_job(ats_job_id="1", url="https://x/1", hours_old=40,
                   description="D" * 300)
    new = make_job(ats_job_id="2", url="https://x/2", hours_old=2,
                   description="D" * 300)
    assert dedupe([old, new])[0] is new


def test_dedupe_never_merges_across_countries():
    """The counterweight to the two tests above: whatever else `dedupe` does,
    the same title in Berlin and in Warsaw must stay two jobs, because the
    country is part of the key."""
    berlin = make_job(location="Berlin, Germany", ats_job_id="1", url="https://x/1")
    warsaw = make_job(location="Warsaw, Poland", ats_job_id="2", url="https://x/2")
    assert len(dedupe([berlin, warsaw])) == 2


# ==========================================================================
# keyword filters
# ==========================================================================


def test_a_citizenship_requirement_is_caught_however_it_is_punctuated(tmp_path):
    """US defence and government contractors write "U.S. citizen" far more
    often than "US citizen", and this exclusion exists precisely to drop
    postings an EU applicant is legally barred from. It fires on one spelling
    and not the other."""
    conf = fresh_cfg(tmp_path)
    escaped = [
        text
        for text in ("Applicants must be a US citizen.",
                     "Applicants must be a U.S. citizen.",
                     "You must be a U.S. Citizen or green card holder.")
        if passes_keywords(make_job(description=text), conf)[0]
    ]
    assert escaped == []


def test_short_and_punctuated_keywords_match_as_tokens(tmp_path):
    """Pinned deliberate behaviour (`passes_keywords` docstring). "go" cannot
    match "going", and "c++" is compared as the bare token "c" — which is why
    a keyword list of ["go", "c++"] is a blunt instrument and the docstring
    says so. Worth knowing before blaming the filter."""
    conf = fresh_cfg(tmp_path, require_keywords_any=["go"])
    assert passes_keywords(make_job(description="We write Go services."), conf)[0] is True
    assert passes_keywords(make_job(description="Going to production daily."),
                           conf)[0] is False

    plus = fresh_cfg(tmp_path, require_keywords_any=["c++"])
    assert passes_keywords(make_job(description="Low latency C++ engine."),
                           plus)[0] is True
    assert passes_keywords(make_job(description="Graded C by the auditor."),
                           plus)[0] is True   # the token is just "c"


def test_a_rejection_reason_is_produced_for_every_stage(tmp_path):
    """The digest groups rejections on the category slug, so a batch that
    exercises every stage must come back fully accounted for: one reason per
    job, every category a known slug, and the counts adding up."""
    conf = write_config(tmp_path, {"filters": {"countries": ["DE"],
                                               "min_description_chars": 50,
                                               "require_keywords_any": ["python"]}})
    batch = [
        make_job(title="Backend Intern", ats_job_id="1"),
        make_job(location="San Francisco, CA", ats_job_id="2"),
        make_job(hours_old=99, ats_job_id="3"),
        make_job(hours_old=None, ats_job_id="4"),
        make_job(description="We use Rust and nothing else at all, ever, here.",
                 ats_job_id="5"),
        make_job(description="Python.", ats_job_id="6"),
    ]
    result = apply_filters(batch, conf, now=NOW)
    assert result.kept == []
    assert set(result.counts) == {"title_excluded", "location_outside_eu", "stale",
                                  "undated", "missing_keyword", "description_too_short"}
    assert sum(result.counts.values()) == 6


def test_one_pathological_posting_never_costs_the_batch(tmp_path):
    """A source can hand over anything. A job whose title is None and whose
    location is a dict must be rejected as a filter error while every healthy
    job in the same batch survives — a single bad posting taking down the
    morning's run is the failure this whole layer exists to prevent."""
    broken = make_job(location="Berlin, Germany", ats_job_id="1")
    broken.title = None  # type: ignore[assignment]
    broken.location = {"name": "Berlin"}  # type: ignore[assignment]
    good = make_job(company="Fine", location="Berlin, Germany", ats_job_id="2")
    result = apply_filters([broken, good], cfg(tmp_path), now=NOW)
    assert good in result.kept
    assert len(result.kept) + len(result.rejected) == 2


def test_a_us_only_role_declared_in_the_title_is_not_rescued(tmp_path):
    """The US veto read only `location` while the EU rescue read location,
    title and description — half a check. A posting whose location is a bare
    "Remote" and whose *title* says "(Remote - US)" was kept by any European
    city its description happened to mention."""
    conf = fresh_cfg(tmp_path)
    for title in ("Backend Engineer (Remote - US)",
                  "Backend Engineer — US Only",
                  "Backend Engineer (United States)"):
        job = make_job(location="Remote", title=title,
                       description="Our engineering team sits in Berlin and Madrid.")
        ok, reason = passes_location(job, conf)
        assert ok is False, f"{title!r} was kept: {reason}"


def test_a_remote_eu_role_is_still_kept(tmp_path):
    """The neighbouring case — widening the veto must not start dropping the
    remote European roles this whole tool exists to find."""
    conf = fresh_cfg(tmp_path)
    job = make_job(location="Remote", title="Backend Engineer (Remote - EU)",
                   description="Our engineering team sits in Berlin and Madrid.")
    assert passes_location(job, conf)[0] is True
