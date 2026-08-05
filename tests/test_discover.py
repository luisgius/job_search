"""Tests for `--discover`: company name -> the board and slug to paste.

This is the one feature in the tool that makes **deliberate unsolicited
requests** to third parties. Everything else fetches boards the user chose;
discovery guesses, and six vendors times several spellings times N companies is
a lot of 404s from one IP. Traffic that looks like a scanner gets the user
blocked from the boards the daily run actually needs.

So the tests here are about two things in roughly equal measure:

  * **the bounds** — a per-company candidate cap and a total request cap, each
    of which must be *said out loud* when it bites, because a sweep that stops
    quietly looks exactly like a company that is on no board;
  * **not sounding more certain than the evidence** — a wrong slug is worse than
    no slug. It goes into `watchlist.yaml`, and from the next morning on it
    produces an empty board that reads as a quiet market rather than as a
    mistake. Two boards answering is reported as two boards answering. Zero
    postings, a 404 and a timeout are three different answers and are never
    collapsed into one.

Everything runs through the `session=` seam. The CLI tests reuse
`test_ats_boards.py`'s `stub_boards` fixture, for the reason documented there:
`main()` takes no session.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from src.sources import ats_boards
from src.sources.ats_boards import (
    DISCOVER_MAX_REQUESTS,
    DISCOVER_MAX_SLUGS_PER_COMPANY,
    PROBE_ABSENT,
    PROBE_EMPTY,
    PROBE_ERROR,
    PROBE_FOUND,
    PROBE_UNREACHABLE,
    RequestBudget,
    discover,
    discover_company,
    format_discovery,
    main,
    slug_candidates,
)
from src.util import HttpError
from tests.conftest import FakeResponse, FakeSession, json_response, xml_response
from tests.test_ats_boards import stub_boards  # noqa: F401  (fixture re-use)

# ==========================================================================
# board fakes
#
# One minimal, parseable payload per vendor. They are deliberately the *bare*
# shape — discovery only ever asks "does this board answer, and how big is it",
# so a fixture-faithful payload would be testing the parsers a second time.
# ==========================================================================

_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "personio": "https://{slug}.jobs.personio.de/xml",
}


def board_url(board: str, slug: str) -> str:
    """The URL a probe of `(board, slug)` hits."""
    return _URLS[board].format(slug=slug)


def board_route(board: str, slug: str) -> re.Pattern[str]:
    """An **exactly**-this-URL route key.

    `FakeSession`'s substring matching is not good enough for this file and the
    difference is not cosmetic: `.../accounts/factorial` is a substring of
    `.../accounts/factorial-hr`, so a substring route registered for one
    spelling answers for every longer one — and a test for "the first candidate
    404s and the second one hits" would pass no matter which candidate the code
    actually asked about.
    """
    return re.compile(r"^" + re.escape(board_url(board, slug)) + r"$")


def board_payload(
    board: str, count: int, *, company: str, total: int | None = None
) -> FakeResponse:
    """A response carrying exactly `count` parseable postings.

    `total` is SmartRecruiters' `totalFound`, and it is a separate knob for a
    reason: it is what tells the fetcher there are more pages to walk. A payload
    whose `totalFound` equals its page length stops the offset loop after one
    request no matter what the caller asked for, which would make a test for
    "one probe is one request" pass against a fetcher that pages forever.
    """
    if board == "greenhouse":
        return json_response({"jobs": [
            {"id": i, "title": "Backend Engineer",
             "absolute_url": f"https://boards.greenhouse.io/x/jobs/{i}"}
            for i in range(count)
        ]})
    if board == "lever":
        return json_response([
            {"id": str(i), "text": "Backend Engineer",
             "hostedUrl": f"https://jobs.lever.co/x/{i}"}
            for i in range(count)
        ])
    if board == "workable":
        return json_response({
            "name": company,
            "jobs": [{"title": "Backend Engineer", "shortcode": f"AB{i}"}
                     for i in range(count)],
        })
    if board == "ashby":
        return json_response({"apiVersion": "1", "jobs": [
            {"id": str(i), "title": "Backend Engineer",
             "jobUrl": f"https://jobs.ashbyhq.com/x/{i}"}
            for i in range(count)
        ]})
    if board == "smartrecruiters":
        return json_response({
            "totalFound": count if total is None else total,
            "content": [
                {"id": str(i), "name": "Backend Engineer",
                 "company": {"identifier": "x", "name": company}}
                for i in range(count)
            ],
        })
    if board == "personio":
        positions = "".join(
            f"<position><id>{i}</id><name>Backend Engineer</name></position>"
            for i in range(count)
        )
        return xml_response(f"<workzag-jobs>{positions}</workzag-jobs>")
    raise AssertionError(f"unknown board {board!r}")


def answers(board: str, slug: str, count: int = 3, *, company: str | None = None,
            total: int | None = None):
    """A route: this board answers for this exact slug with `count` postings.

    `company` defaults to the name Workable and SmartRecruiters would really
    publish for that slug, so the collision check has nothing to complain about
    unless a test deliberately gives it something to complain about.
    """
    if company is None:
        company = ats_boards.company_from_slug(slug)
    return (board_route(board, slug),
            board_payload(board, count, company=company, total=total))


def rejects(board: str, slug: str, status: int = 403):
    """A route: this board answers this exact slug with a status, not a board."""
    return (board_route(board, slug), FakeResponse(status_code=status))


def raises(board: str, slug: str, exc: Exception):
    """A route: this board never answers for this exact slug."""
    return (board_route(board, slug), exc)


def discovery_session(*routes, default=None) -> FakeSession:
    """A session where every unrouted URL is a 404 — the honest default.

    A slug nobody has is a 404 from every board, and `util.http_get` does not
    retry a 404, so the default costs exactly one request per probe.
    """
    return FakeSession(
        list(routes), default=default or FakeResponse(status_code=404)
    )


def statuses(result) -> dict[tuple[str, str], str]:
    return {(p.board, p.slug): p.status for p in result.probes}


def paste_block(report: str) -> str:
    """Just the YAML the user is meant to copy, without the evidence above it."""
    lines = report.splitlines()
    start = max(i for i, line in enumerate(lines) if line.startswith("# ---")) + 1
    end = next(i for i, line in enumerate(lines) if re.match(r"^\d+ probe\(s\)", line))
    return "\n".join(lines[start:end])


# ==========================================================================
# slug derivation
#
# `models.normalize_company` and friends do the work: the tracker already has
# to decide that "Spotify AB" and "spotify" are one company, which is the same
# question, so discovery reuses that answer rather than growing a second
# normaliser that can drift away from it.
# ==========================================================================


@pytest.mark.parametrize(
    "name,expected",
    [
        # The legal-suffix case, and why `collapse_initialisms` runs first:
        # "N.V." is one token to a reader and two to a punctuation-stripper.
        ("Adyen N.V.", ["adyen"]),
        ("Adyen NV", ["adyen"]),
        ("Zalando SE", ["zalando"]),
        ("Sopra Steria SA", ["soprasteria", "sopra-steria", "sopra"]),
        # Spaces: three plausible spellings, in the order a board is likeliest
        # to have used.
        ("Factorial HR", ["factorialhr", "factorial-hr", "factorial"]),
        # Nothing to do.
        ("Glovo", ["glovo"]),
        ("TravelPerk", ["travelperk"]),
        ("Glovoapp", ["glovoapp"]),
    ],
)
def test_slug_candidates_for_the_ordinary_shapes(name, expected):
    assert slug_candidates(name, limit=None) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        # NFKD strips the combining accent...
        ("Bücher", ["bucher", "buecher"]),
        ("Zürich Insurance", ["zurichinsurance", "zurich-insurance", "zurich",
                              "zuerichinsurance", "zuerich-insurance", "zuerich"]),
        # ...but "Æ" is a distinct letter, not an accented one, and only
        # `models._TRANSLITERATE` knows it spells "ae".
        ("Æther", ["aether"]),
        ("Malmö Ångpanneförening", ["malmoangpanneforening", "malmo-angpanneforening",
                                    "malmo", "malmoeangpannefoerening",
                                    "malmoe-angpannefoerening", "malmoe"]),
    ],
)
def test_non_ascii_names_survive_derivation(name, expected):
    """A name the pipeline cannot even spell is a company discovery cannot look
    for. German expands its umlauts in URLs at least as often as it drops them
    ("buecher"), and NFKD alone only ever produces the dropped form."""
    assert slug_candidates(name, limit=None) == expected


def test_an_ampersand_gets_both_spellings():
    """"&" is punctuation to the normaliser, so "H&M" already joins to "hm" —
    but "h-and-m" is a real slug shape and is not derivable from the folded
    tokens alone."""
    assert slug_candidates("H&M", limit=None) == ["hm", "h-m", "handm", "h-and-m"]


def test_a_one_character_candidate_is_never_tried():
    """The bare first token of "H&M" is "h". It is never a real slug, and a
    candidate costs a whole round of six probes to disprove."""
    assert "h" not in slug_candidates("H&M", limit=None)


def test_derivation_never_returns_duplicates():
    """A name whose spellings coincide must not pay for the same probe twice."""
    for name in ("glovo", "Glovo", "H&M", "Bucher", "adyen nv"):
        candidates = slug_candidates(name, cased=True, limit=None)
        assert len(candidates) == len(set(candidates)), name


@pytest.mark.parametrize("name", ["", "   ", None, "!!!", "-"])
def test_a_nameless_name_derives_nothing_rather_than_probing(name):
    assert slug_candidates(name, limit=None) == []


# ==========================================================================
# SmartRecruiters is case-sensitive
# ==========================================================================


def test_smartrecruiters_candidates_keep_the_companys_own_capitals():
    """Documented in `watchlist.yaml`: on this one vendor the slug is spelled
    exactly as it appears in the jobs.smartrecruiters.com URL, capitals and all.
    Lowercasing it away is how the one board a European company is actually on
    gets reported as a 404."""
    assert slug_candidates("Factorial HR", cased=True, limit=None) == [
        "FactorialHR", "factorialhr", "Factorial", "factorial",
        "Factorial-HR", "factorial-hr",
    ]


def test_the_cased_and_folded_spellings_are_both_tried():
    """Real SmartRecruiters tenants exist under both conventions, so neither
    spelling may be the only one asked about."""
    candidates = slug_candidates("Glovo", cased=True, limit=None)
    assert candidates == ["Glovo", "glovo"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Bücher GmbH", ["Bucher", "bucher", "Buecher", "buecher"]),
        ("Æther", ["AEther", "aether"]),
        ("H&M", ["HM", "hm", "H-M", "h-m", "HandM", "handm", "H-and-M", "h-and-m"]),
    ],
)
def test_the_cased_spellings_survive_folding_and_expansion(name, expected):
    """The capitals have to come off the *same* tokenisation as the folded
    spelling, or they are lost on exactly the names that need them most.

    A cased list built by splitting the raw string instead looks right for
    "Factorial HR" and quietly collapses to lower case for every name carrying
    an accent, a ligature or an ampersand — which on the one case-sensitive
    board means never asking about "Buecher" or "HM" at all. The alignment check
    in `_company_tokens` hides that failure by falling back to the folded list,
    so it has to be asserted here rather than inferred from the ASCII cases.
    """
    assert slug_candidates(name, cased=True, limit=None) == expected


def test_a_lowercase_name_costs_smartrecruiters_nothing_extra():
    """When the user types the name in lower case the two spellings coincide,
    and dedupe must collapse them — a duplicate candidate is a wasted round of
    six probes against boards that already said no."""
    assert slug_candidates("glovo", cased=True, limit=None) == ["glovo"]


def test_only_smartrecruiters_gets_the_cased_candidates():
    """Every other board is case-insensitive, so asking twice would double the
    request count for nothing."""
    session = discovery_session(answers("smartrecruiters", "FactorialHR", 9))
    result = discover_company("Factorial HR", session=session)

    assert result.candidates["smartrecruiters"][0] == "FactorialHR"
    for board in ("greenhouse", "lever", "workable", "ashby", "personio"):
        assert result.candidates[board] == ["factorialhr", "factorial-hr", "factorial"]


def test_the_case_sensitive_slug_is_what_gets_suggested():
    """The suggestion has to be pasteable verbatim; a folded copy of a
    case-sensitive slug is a 404 tomorrow morning."""
    session = discovery_session(answers("smartrecruiters", "FactorialHR", 9))
    result = discover_company("Factorial HR", session=session)

    assert result.suggestion.board == "smartrecruiters"
    assert result.suggestion.slug == "FactorialHR"
    assert "FactorialHR" in format_discovery([result], RequestBudget())


# ==========================================================================
# the sweep
# ==========================================================================


def test_a_company_on_exactly_one_board():
    session = discovery_session(answers("workable", "glovo", 34, company="Glovo"))
    result = discover_company("Glovo", session=session)

    assert result.confidence == "high"
    assert result.ambiguous is False
    assert result.suggestion.board == "workable"
    assert result.suggestion.slug == "glovo"
    assert result.suggestion.count == 34
    assert result.pasteable is True
    assert [p.board for p in result.matches] == ["workable"]


def test_the_other_five_boards_are_still_asked_and_reported():
    """The round is the unit of work: every board is asked about the best
    spelling before any of them is asked about the second-best. Stopping
    mid-round would make the answer depend on the order of `BOARDS`, which is
    arbitrary, and would hide the two-boards-answered case entirely."""
    session = discovery_session(answers("workable", "glovo", 34))
    result = discover_company("Glovo", session=session)

    assert statuses(result) == {
        ("greenhouse", "glovo"): PROBE_ABSENT,
        ("lever", "glovo"): PROBE_ABSENT,
        ("workable", "glovo"): PROBE_FOUND,
        ("ashby", "glovo"): PROBE_ABSENT,
        # SmartRecruiters is asked about its own spelling first; the folded one
        # is round two's business, and round two never happened.
        ("smartrecruiters", "Glovo"): PROBE_ABSENT,
        ("personio", "glovo"): PROBE_ABSENT,
    }


def test_a_company_found_nowhere():
    session = discovery_session()
    result = discover_company("TravelPerk", session=session)

    assert result.confidence == "none"
    assert result.suggestion is None
    assert result.pasteable is False
    assert result.matches == []
    assert all(p.status == PROBE_ABSENT for p in result.probes)


def test_a_company_found_nowhere_says_what_to_do_instead():
    """"Nothing found" has to send the reader somewhere, or the tool has simply
    handed the manual trawl back to them without saying so."""
    session = discovery_session()
    results, budget = discover(["TravelPerk"], session=session)
    report = format_discovery(results, budget)

    assert "no board answered" in report
    assert "travelperk" in report
    assert "--check" in report


def test_a_hit_stops_the_sweep_before_the_later_spellings():
    """The economy that matters: a company found on the first spelling costs one
    round, not four. `stopped_early` is what the report prints, so it is a fact
    rather than an inference."""
    session = discovery_session(answers("greenhouse", "factorialhr", 12))
    result = discover_company("Factorial HR", session=session)

    assert result.stopped_early is True
    assert {p.slug for p in result.probes} == {"factorialhr", "FactorialHR"}
    assert "factorial-hr" not in session.urls()[-1]
    assert not any("factorial-hr" in url for url in session.urls())


def test_the_spellings_a_hit_skipped_are_named_in_the_report():
    """"We stopped looking" and "there was nothing to find" are different
    claims, and only one of them is true here."""
    session = discovery_session(answers("greenhouse", "factorialhr", 12))
    results, budget = discover(["Factorial HR"], session=session)
    report = format_discovery(results, budget)

    assert "stopped here" in report
    assert "factorial-hr" in report


def test_a_slug_that_404s_on_one_candidate_and_hits_on_the_next():
    """The whole reason more than one spelling is derived. Round one is a clean
    sweep of 404s; round two finds the company on "factorial"."""
    session = discovery_session(answers("ashby", "factorial", 7))
    result = discover_company("Factorial HR", session=session)

    assert result.suggestion.board == "ashby"
    assert result.suggestion.slug == "factorial"
    assert statuses(result)[("ashby", "factorialhr")] == PROBE_ABSENT
    assert statuses(result)[("ashby", "factorial-hr")] == PROBE_ABSENT
    assert statuses(result)[("ashby", "factorial")] == PROBE_FOUND
    # Round four — SmartRecruiters' last cased spelling — was never reached.
    assert result.stopped_early is True


def test_a_404_is_the_answer_and_is_not_retried():
    """`util.http_get` already refuses to retry a 4xx. Asserted here because a
    discovery sweep is the one place where retrying a "no" would multiply the
    request count by three against boards we were never invited to ask."""
    session = discovery_session()
    discover_company("Glovo", session=session)

    # One request per board, plus Personio's documented .de -> .com fallback,
    # plus SmartRecruiters' second (case-folded) spelling.
    assert len(session.calls) == 8
    assert sum(1 for url in session.urls() if "greenhouse" in url) == 1


def test_one_board_is_never_asked_about_a_slug_meant_for_another():
    """A sanity check on the fake as much as on the code: routing is by
    substring, and a test whose routes overlap proves nothing."""
    session = discovery_session(answers("lever", "glovo", 4))
    discover_company("Glovo", session=session)

    for call in session.calls:
        assert call["url"].count("glovo") + call["url"].count("Glovo") == 1


# ==========================================================================
# ambiguity — the finding, not a tie to be broken
# ==========================================================================


def test_two_boards_answering_is_reported_as_two_boards_answering():
    """Slug collisions across vendors are ordinary: `acme` on Lever is not the
    same company as `acme` on Greenhouse. Picking one of the two would be the
    tool inventing exactly the confidence it was asked not to have."""
    session = discovery_session(
        answers("greenhouse", "acme", 12),
        answers("lever", "acme", 3),
    )
    result = discover_company("Acme", session=session)

    assert result.ambiguous is True
    assert result.confidence == "low"
    assert {p.board for p in result.matches} == {"greenhouse", "lever"}
    assert result.suggestion is None
    assert result.pasteable is False


def test_an_ambiguous_result_is_printed_commented_out():
    """Pasting the block must never be able to install a guess."""
    session = discovery_session(
        answers("greenhouse", "acme", 12),
        answers("lever", "acme", 3),
    )
    results, budget = discover(["Acme"], session=session)
    report = format_discovery(results, budget)

    assert "AMBIGUOUS" in report
    # Both are named...
    assert "greenhouse" in report and "lever" in report
    # ...and neither is pasteable YAML: every line of the paste block is
    # commented out, so copying the whole thing installs nothing.
    for line in paste_block(report).splitlines():
        assert not line.strip() or line.lstrip().startswith("#"), line


def test_two_boards_answering_still_names_both_counts():
    session = discovery_session(
        answers("greenhouse", "acme", 12),
        answers("lever", "acme", 3),
    )
    results, budget = discover(["Acme"], session=session)
    report = format_discovery(results, budget)

    assert "12 posting" in report
    assert "3 posting" in report


def test_a_board_that_calls_itself_something_else_is_not_trusted():
    """Workable and SmartRecruiters publish the employer's own name, which is
    the only direct evidence available that the slug belongs to the company
    asked for. "Fabrikam" answering to "glovo" is a collision, not a find."""
    session = discovery_session(
        answers("workable", "glovo", 20, company="Fabrikam Insurance"),
    )
    result = discover_company("Glovo", session=session)

    assert result.matches[0].name_mismatch is True
    assert result.confidence == "low"
    assert result.pasteable is False
    assert "Fabrikam Insurance" in " ".join(result.notes)


def test_a_longer_published_name_is_not_a_mismatch():
    """The lenient half of the rule. "Glovo" and "Glovo Spain SL" are one
    company, and a false mismatch would downgrade a correct answer — which
    silently sends the user back to the careers page they came from."""
    session = discovery_session(
        answers("workable", "glovo", 20, company="Glovo Spain SL"),
    )
    result = discover_company("Glovo", session=session)

    assert result.matches[0].name_mismatch is False
    assert result.confidence == "high"


def test_the_four_boards_that_invent_a_name_never_produce_a_mismatch():
    """Greenhouse, Lever, Ashby and Personio have no company field, so
    `company_from_slug` fills it in — comparing that back against the name we
    derived the slug from would compare a string to itself and always agree.
    An assertion that cannot fail is worse than none, so it is not made."""
    session = discovery_session(answers("greenhouse", "glovo", 20))
    result = discover_company("Glovo", session=session)

    assert result.matches[0].company_name == ""
    assert result.matches[0].name_mismatch is False


# ==========================================================================
# zero postings is not a 404, and neither is a timeout
# ==========================================================================


def test_a_board_with_zero_postings_is_not_a_board_that_404s():
    """Three different facts about the world: this company exists here and is
    not hiring; this slug does not exist; we never got an answer. Collapsing any
    two of them is how a wrong slug gets recommended."""
    session = discovery_session(answers("ashby", "glovo", 0))
    result = discover_company("Glovo", session=session)

    assert statuses(result)[("ashby", "glovo")] == PROBE_EMPTY
    assert statuses(result)[("lever", "glovo")] == PROBE_ABSENT
    assert result.empties[0].count == 0
    assert result.matches == []


def test_an_empty_board_is_reported_but_not_recommended():
    """A board with nothing on it is consistent with the right slug on a quiet
    week and with a wrong slug that happens to exist. It is offered, commented
    out, with the reason."""
    session = discovery_session(answers("ashby", "glovo", 0))
    results, budget = discover(["Glovo"], session=session)
    result = results[0]

    assert result.confidence == "low"
    assert result.pasteable is False
    assert result.suggestion.board == "ashby"
    report = format_discovery(results, budget)
    assert "no open postings" in report
    assert "#   ashby: [glovo]" in report


def test_an_empty_board_does_not_stop_the_sweep():
    """Only real postings end it. An empty board on the first spelling must not
    prevent the second spelling from finding the company's actual board."""
    session = discovery_session(
        answers("workable", "factorialhr", 0),
        answers("workable", "factorial", 40),
    )
    result = discover_company("Factorial HR", session=session)

    assert result.suggestion.slug == "factorial"
    assert result.suggestion.count == 40
    # ...and the empty one is still on the record, and still qualifies the
    # answer rather than being quietly dropped.
    assert [p.slug for p in result.empties] == ["factorialhr"]
    assert result.confidence == "medium"


def test_a_board_that_answers_with_something_that_is_not_a_board():
    """A parked domain or a login wall answers 200 with HTML. Personio's feed is
    XML, so this arrives as a parse failure — an *answer*, and it must not be
    filed under "no answer", which would blame the network."""
    session = discovery_session(
        (board_route("personio", "glovo"),
         FakeResponse(status_code=200, text="<html>a login wall</html>")),
    )
    result = discover_company("Glovo", session=session)

    assert statuses(result)[("personio", "glovo")] == PROBE_ERROR
    assert result.confidence == "none"


def test_a_refusal_is_not_an_absence():
    """A 403 says the board exists and would not talk to us. It is not evidence
    that the company is not there, and the report must not read as though it
    were."""
    session = discovery_session(rejects("ashby", "glovo", 403))
    result = discover_company("Glovo", session=session)

    assert statuses(result)[("ashby", "glovo")] == PROBE_ERROR
    assert "neither ruled in nor out" in " ".join(result.notes)


@pytest.mark.parametrize("status,expected", [
    (404, PROBE_ABSENT), (410, PROBE_ABSENT),
    (401, PROBE_ERROR), (403, PROBE_ERROR),
])
def test_every_status_lands_in_the_right_bucket(status, expected):
    session = discovery_session(rejects("lever", "glovo", status))
    result = discover_company("Glovo", session=session)
    assert statuses(result)[("lever", "glovo")] == expected


def test_a_board_that_times_out_does_not_cost_the_other_five():
    """One dead board must cost that board and nothing else — the rule the whole
    module is built on. The timeout is reported as a timeout, which is neither a
    404 nor an empty board, and the company is still found on Workable.

    Slow on purpose: a transport failure spends `util.http_get`'s real retry
    budget, which is the policy discovery reuses rather than replacing.
    """
    session = discovery_session(
        raises("ashby", "glovo", TimeoutError("read timed out")),
        answers("workable", "glovo", 11),
    )
    result = discover_company("Glovo", session=session)

    assert statuses(result)[("ashby", "glovo")] == PROBE_UNREACHABLE
    assert result.suggestion.board == "workable"
    assert result.suggestion.count == 11
    # It cost confidence, though — an unasked board is not a ruled-out board.
    assert result.confidence == "medium"
    assert "no answer either way" in " ".join(result.notes)


def test_a_timeout_is_told_apart_from_a_404_in_the_report():
    session = discovery_session(
        raises("ashby", "glovo", TimeoutError("read timed out")),
        answers("workable", "glovo", 11),
    )
    results, budget = discover(["Glovo"], session=session)
    report = format_discovery(results, budget)

    assert "no answer" in report
    assert "no such slug" in report


# ==========================================================================
# the bounds
# ==========================================================================


def test_the_per_company_cap_bounds_the_candidates():
    session = discovery_session()
    result = discover_company("Zürich Insurance", session=session, max_slugs=2)

    assert result.candidates["greenhouse"] == ["zurichinsurance", "zurich-insurance"]
    assert result.dropped_candidates["greenhouse"] == [
        "zurich", "zuerichinsurance", "zuerich-insurance", "zuerich",
    ]
    assert not any("zuerich" in url for url in session.urls())


def test_the_per_company_cap_says_what_it_dropped(caplog):
    """`SMARTRECRUITERS_MAX_PAGES` sets the precedent: a cap that bites
    silently turns "we stopped guessing" into "this company is on no board"."""
    with caplog.at_level(logging.INFO, logger="src.sources.ats_boards"):
        discover_company("Zürich Insurance", session=discovery_session(), max_slugs=2)

    text = caplog.text
    assert "DISCOVER_MAX_SLUGS_PER_COMPANY" in text
    assert "zuerichinsurance" in text
    assert "--check" in text


def test_the_shipped_per_company_cap_covers_the_ordinary_shapes():
    """The cap is only defensible if it does not routinely cut a real spelling.
    Every derivation an ordinary name produces has to fit inside it."""
    for name in ("Glovo", "Factorial HR", "Adyen N.V.", "TravelPerk", "H&M", "Bücher"):
        assert len(slug_candidates(name, limit=None)) <= DISCOVER_MAX_SLUGS_PER_COMPANY


def test_the_request_cap_stops_the_sweep():
    session = discovery_session()
    results, budget = discover(
        ["Glovo", "Factorial HR", "TravelPerk"], session=session, max_requests=3
    )

    assert budget.spent == 3
    assert len(session.calls) <= 4  # 3 probes; Personio's fallback is 2 requests
    assert budget.skipped
    assert results[2].probes == []


def test_the_request_cap_is_said_out_loud_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="src.sources.ats_boards"):
        discover(["Glovo", "TravelPerk"], session=discovery_session(), max_requests=2)

    text = caplog.text
    assert "DISCOVER_MAX_REQUESTS" in text
    assert "NOT made" in text
    assert "TravelPerk" in text


def test_the_request_cap_is_said_out_loud_in_the_report():
    """Loudly enough that the reader cannot mistake an unfinished sweep for an
    exhaustive one — which is the whole failure mode of a silent cap."""
    results, budget = discover(
        ["Glovo", "TravelPerk"], session=discovery_session(), max_requests=2
    )
    report = format_discovery(results, budget)

    assert "REQUEST CAP HIT" in report
    assert "TravelPerk" in report
    assert "not asked" in report


def test_a_capped_company_is_not_reported_as_a_company_with_no_board():
    """The dangerous reading. A company the cap never asked about must not come
    out looking like a company that was asked and said no — and the place that
    matters most is the paste block, which is the part people read."""
    results, budget = discover(
        ["Glovo", "TravelPerk"], session=discovery_session(), max_requests=2
    )
    unfinished = results[1]

    assert unfinished.capped is True
    assert unfinished.confidence == "none"
    assert "request cap" in " ".join(unfinished.notes)

    block = paste_block(format_discovery(results, budget))
    travelperk = block.split("# TravelPerk")[1]
    assert "UNFINISHED" in travelperk
    assert "no board answered" not in travelperk
    assert "not on one of the six public boards" not in travelperk


def test_a_capped_hit_is_downgraded_rather_than_trusted():
    """A company found *while* the cap was biting was only half-swept: the
    boards below it were never asked, so a second one could have answered."""
    session = discovery_session(answers("greenhouse", "glovo", 30))
    results, _budget = discover(["Glovo"], session=session, max_requests=2)

    assert results[0].capped is True
    assert results[0].matches[0].board == "greenhouse"
    assert results[0].confidence == "low"
    assert results[0].pasteable is False


def test_the_budget_is_shared_across_companies():
    session = discovery_session()
    _results, budget = discover(
        ["Glovo", "TravelPerk"], session=session, max_requests=100
    )
    # Seven probes each (six boards, plus SmartRecruiters' folded spelling).
    assert budget.spent == 14


def test_the_shipped_request_cap_is_a_bound_a_real_run_stays_under():
    """Six boards times four spellings is the worst case for one company, so the
    shipped cap has to be worth several of those or it fires on ordinary use."""
    worst_case_per_company = len(ats_boards.BOARDS) * DISCOVER_MAX_SLUGS_PER_COMPANY
    assert DISCOVER_MAX_REQUESTS >= 4 * worst_case_per_company


def test_a_probe_against_a_huge_smartrecruiters_tenant_costs_one_page():
    """SmartRecruiters is offset-paginated and `fetch_smartrecruiters` follows
    the offsets to the end — correct for the daily run, ruinous for a budget
    that thinks one probe is one request. A full first page of a board that says
    it has 250 roles must not turn one probe into three requests, or twenty.

    The probe therefore under-reports the size of a big board (100, not 250),
    which is the right trade: discovery only has to establish that the board is
    real, and the daily fetch is what needs every posting.
    """
    session = discovery_session(
        answers("smartrecruiters", "Glovo", 100, total=250)
    )
    result = discover_company("Glovo", session=session)

    assert sum(1 for url in session.urls() if "smartrecruiters" in url) == 1
    assert result.matches[0].count == 100


def test_discovery_never_asks_for_the_expensive_half_of_a_fetch():
    """A probe only needs to know a board answers. Descriptions are the
    expensive half of every fetch and are pure waste here."""
    session = discovery_session(
        answers("greenhouse", "glovo", 3), answers("workable", "glovo", 3),
    )
    discover_company("Glovo", session=session)

    for call in session.calls:
        assert call["params"].get("content") != "true"
        assert call["params"].get("details") != "true"


# ==========================================================================
# the report
# ==========================================================================


def test_the_report_is_pasteable_yaml():
    import yaml

    session = discovery_session(answers("workable", "glovo", 34))
    results, budget = discover(["Glovo"], session=session)

    parsed = yaml.safe_load(paste_block(format_discovery(results, budget)))
    assert parsed == {"workable": ["glovo"]}


def test_the_paste_block_carries_the_evidence_as_a_comment():
    session = discovery_session(answers("workable", "glovo", 34))
    results, budget = discover(["Glovo"], session=session)
    report = format_discovery(results, budget)

    assert "# Glovo — 34 postings (high confidence)" in report


def test_the_report_shows_every_board_that_was_asked():
    session = discovery_session(answers("workable", "glovo", 34))
    results, budget = discover(["Glovo"], session=session)
    report = format_discovery(results, budget)

    for board in ats_boards.BOARDS:
        assert board in report


def test_the_report_states_the_cap_even_when_it_did_not_bite():
    """"Bounded" is only a property the user can rely on if they can see the
    bound without reading the source."""
    results, budget = discover(["Glovo"], session=discovery_session())
    report = format_discovery(results, budget)

    assert f"DISCOVER_MAX_REQUESTS={DISCOVER_MAX_REQUESTS}" in report
    assert "7 probe(s)" in report


def test_confidence_is_reported_rather_than_a_verdict():
    session = discovery_session(answers("workable", "glovo", 34))
    results, budget = discover(["Glovo"], session=session)

    assert "confidence: HIGH" in format_discovery(results, budget)


# ==========================================================================
# CLI
# ==========================================================================


@pytest.fixture
def stub_discovery(monkeypatch):
    """Intercept the network for CLI tests, which cannot pass a session.

    The same seam and the same reasoning as `stub_boards` in
    `test_ats_boards.py`: `main()` takes no `session=`, so `_fetch_board` is the
    only interception point that does not reach past a public boundary.
    """
    calls: list[tuple[str, str]] = []
    found: dict[tuple[str, str], int] = {}

    def _fake(board, slug, *, session=None, **kwargs):
        calls.append((board, slug))
        if (board, slug) in found:
            return [object()] * found[(board, slug)]
        raise HttpError(f"https://x/{slug} -> HTTP 404")

    monkeypatch.setattr(ats_boards, "_fetch_board", _fake)
    _fake.calls = calls        # type: ignore[attr-defined]
    _fake.found = found        # type: ignore[attr-defined]
    return _fake


def test_cli_discover_prints_what_to_paste(stub_discovery, capsys):
    stub_discovery.found[("workable", "glovo")] = 34
    assert main(["--discover", "Glovo"]) == 0

    out = capsys.readouterr().out
    assert "workable:" in out
    assert "- glovo" in out
    assert "34 postings" in out


def test_cli_discover_takes_several_companies(stub_discovery, capsys):
    stub_discovery.found[("workable", "glovo")] = 34
    stub_discovery.found[("greenhouse", "factorialhr")] = 12
    main(["--discover", "Glovo", "Factorial HR", "TravelPerk"])

    out = capsys.readouterr().out
    assert "Glovo — confidence: HIGH" in out
    assert "Factorial HR — confidence: HIGH" in out
    assert "TravelPerk — confidence: NONE" in out


def test_cli_discover_exits_nonzero_when_a_company_needs_a_human(stub_discovery):
    """Ambiguous, low-confidence and not-found all mean "you have to look at
    this", and a green exit code on a report that says so is a lie."""
    stub_discovery.found[("greenhouse", "acme")] = 12
    stub_discovery.found[("lever", "acme")] = 3
    assert main(["--discover", "Acme"]) == 1


def test_cli_discover_exit_code_is_zero_only_when_everything_is_pasteable(
    stub_discovery,
):
    stub_discovery.found[("workable", "glovo")] = 34
    assert main(["--discover", "Glovo"]) == 0
    stub_discovery.found[("greenhouse", "glovo")] = 1
    assert main(["--discover", "Glovo"]) == 1


def test_cli_discover_json_is_machine_readable(stub_discovery, capsys):
    stub_discovery.found[("workable", "glovo")] = 34
    assert main(["--discover", "Glovo", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["companies"] == 1
    assert payload["capped"] is False
    assert payload["request_cap"] == DISCOVER_MAX_REQUESTS
    result = payload["results"][0]
    assert result["suggestion"] == {"board": "workable", "slug": "glovo", "count": 34}
    assert result["confidence"] == "high"
    assert result["ambiguous"] is False
    assert {p["board"] for p in result["probes"]} == set(ats_boards.BOARDS)


def test_cli_discover_json_reports_ambiguity_as_data(stub_discovery, capsys):
    stub_discovery.found[("greenhouse", "acme")] = 12
    stub_discovery.found[("lever", "acme")] = 3
    main(["--discover", "Acme", "--json"])

    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["ambiguous"] is True
    assert result["suggestion"] is None
    assert {m["board"] for m in result["matches"]} == {"greenhouse", "lever"}


def test_cli_discover_json_says_when_the_cap_bit(stub_discovery, capsys):
    main(["--discover", "Glovo", "TravelPerk", "--max-requests", "2", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["capped"] is True
    assert payload["ok"] is False
    assert payload["requests"] == 2
    assert payload["skipped_probes"]
    assert {s["company"] for s in payload["skipped_probes"]} == {"Glovo", "TravelPerk"}


def test_cli_max_requests_is_honoured(stub_discovery):
    main(["--discover", "Glovo", "--max-requests", "3"])
    assert len(stub_discovery.calls) == 3


def test_cli_discover_never_writes_the_watchlist(stub_discovery, tmp_path: Path):
    """The file the user curates by hand is never edited. A tool that rewrites
    it will eventually eat something they wrote — and the point of printing is
    that a wrong guess stays on the terminal instead of in the config."""
    watchlist = tmp_path / "watchlist.yaml"
    original = "greenhouse:\n  - spotify   # do not touch\n"
    watchlist.write_text(original, encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    stub_discovery.found[("workable", "glovo")] = 34
    main(["--discover", "Glovo", "--watchlist", str(watchlist)])

    assert watchlist.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_cli_discover_does_not_need_a_config_file(stub_discovery, tmp_path: Path):
    """Discovery answers "who should be in the watchlist" and so must work
    before there is one — including with the shipped default paths pointing at
    files that may not exist."""
    stub_discovery.found[("workable", "glovo")] = 34
    assert main(["--discover", "Glovo",
                 "--config", str(tmp_path / "nope.yaml"),
                 "--watchlist", str(tmp_path / "nope.yaml")]) == 0


def test_cli_check_still_works(stub_boards, capsys):  # noqa: F811
    """`--discover` shares `main()` and `_probe` with `--check`; the older
    command must be untouched by that."""
    assert main(["--check", "greenhouse", "spotify"]) == 0
    assert "OK greenhouse/spotify — 3 postings" in capsys.readouterr().out


def test_cli_discover_and_check_do_not_collide(stub_boards, capsys):  # noqa: F811
    """`--discover` wins and `--check` is not silently run as well: two
    different questions, and answering both would double the request count
    without saying so."""
    main(["--discover", "Glovo", "--check", "greenhouse", "spotify"])
    assert ("greenhouse", "spotify") not in stub_boards.calls
