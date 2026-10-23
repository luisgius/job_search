"""LinkedIn guest search: card and detail parsing, the request budget, the
promising-cards-only detail fetch, and the stop-at-first-refusal rule.

The HTML below is trimmed to the classes the parser reads, in the shape the
live jobs-guest pages had on 2026-10-23.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import DEFAULTS, WATCHLIST_DEFAULTS
from src.explore import PUBLIC_SOURCES
from src.filters import SOURCE_RANK
from src.sources.linkedin_search import (
    DETAIL_URL, SEARCH_URL, fetch, parse_cards, parse_detail,
)
from tests.conftest import FakeResponse, FakeSession, write_config

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


def card(job_id, title, company="Acme", location="Kraków, Małopolskie, Poland",
         date="2026-10-23"):
    return f"""
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{job_id}">
  <a class="base-card__full-link absolute" href="https://pl.linkedin.com/jobs/view/x-{job_id}?refId=abc&amp;trk=t">
    <span class="sr-only">{title}</span></a>
  <div class="base-search-card__info">
    <h3 class="base-search-card__title">
      {title}
    </h3>
    <h4 class="base-search-card__subtitle"><a href="https://pl.linkedin.com/company/acme?trk=x">{company}</a></h4>
    <div class="base-search-card__metadata">
      <span class="job-search-card__location">{location}</span>
      <time class="job-search-card__listdate--new" datetime="{date}">2 hours ago</time>
    </div>
  </div>
</div></li>"""


def detail(body="<p>Build models in <strong>Python</strong>.</p>", closed=False,
           seniority="Entry level", employment="Full-time"):
    banner = ('<figure class="closed-job closed-job__flavor topcard__flavor-row">'
              '<figcaption>No longer accepting applications</figcaption></figure>'
              if closed else "")
    return f"""
<section class="top-card-layout"><h2 class="top-card-layout__title">Data Scientist</h2>
{banner}</section>
<div class="description__text description__text--rich">
  <div class="show-more-less-html__markup relative">
    {body}
    <div><ul><li>SQL</li><li>Spark</li></ul></div>
    <p>We offer a hybrid model.</p>
  </div>
</div>
<ul class="description__job-criteria-list">
  <li><h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">{seniority}</span></li>
  <li><h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">{employment}</span></li>
</ul>"""


def li_config(tmp_path, queries=None, **settings):
    watch = {"greenhouse": ["acme"], "linkedin_search": {
        **WATCHLIST_DEFAULTS["linkedin_search"],
        "queries": queries or [{"keywords": "data scientist", "location": "Poland"}],
        "delay_seconds": 0, **settings}}
    return write_config(
        tmp_path,
        {"sources": {"linkedin_search": True},
         "filters": {"title_include": ["data scientist", "machine learning engineer"],
                     "title_exclude": ["senior"]}},
        watch,
    )


def page(*cards):
    return FakeResponse(text="<ul>" + "".join(cards) + "</ul>")


def test_cards_parse_title_company_location_date_and_skip_a_card_without_title():
    html = card("4480031273", "Data Scientist &amp; ML", company="Rumos &amp; Co") \
        + '<li data-entity-urn="urn:li:jobPosting:4480031274"><h3>no title class</h3></li>'
    cards = parse_cards(html)
    assert cards == [{
        "id": "4480031273", "title": "Data Scientist & ML", "company": "Rumos & Co",
        "location": "Kraków, Małopolskie, Poland", "date": "2026-10-23", "salary": "",
    }]


def test_the_detail_description_keeps_nested_blocks_and_reads_the_criteria():
    parsed = parse_detail(detail())
    assert "Build models in Python" in parsed["description"]
    # A regex to the first </div> would stop before these.
    assert "Spark" in parsed["description"]
    assert "We offer a hybrid model." in parsed["description"]
    assert parsed["seniority"] == "Entry level"
    assert parsed["employment_type"] == "Full-time"
    assert parsed["closed"] is False


def test_closed_is_read_from_the_top_card_never_from_the_description():
    assert parse_detail(detail(closed=True))["closed"] is True
    quoted = detail(body="<p>Once filled we are no longer accepting applications.</p>")
    assert parse_detail(quoted)["closed"] is False


def test_fetch_searches_then_describes_only_the_cards_the_filters_would_keep(tmp_path):
    cfg = li_config(tmp_path, queries=[
        {"keywords": "data scientist", "location": "Poland"},
        {"keywords": "data scientist", "location": "European Union", "work_type": "remote"},
    ])
    session = FakeSession([
        ("jobPosting/1000001", FakeResponse(text=detail())),
        ("jobPosting/1000004", FakeResponse(text=detail())),
        (SEARCH_URL, lambda url, params: page(
            card("1000001", "Data Scientist", date="2026-10-21"),
            card("1000002", "Senior Data Scientist"),
            card("1000003", "Data Scientist", location="Austin, Texas, United States"),
        ) if params["location"] == "Poland" else page(
            card("1000001", "Data Scientist"),  # seen in query 1
            card("1000004", "Data Scientist", location="European Union"),
        )),
    ])
    sleeps = []
    jobs = fetch(cfg, session=session, sleep=sleeps.append, now=NOW)

    searches = session.params_for("seeMoreJobPostings")
    assert searches[0] == {"keywords": "data scientist", "location": "Poland",
                           "f_TPR": "r86400", "start": 0}
    assert searches[1]["f_WT"] == "2"
    # A page under ten cards is the last one: one search per query.
    assert len(searches) == 2
    # Only the two promising cards cost a detail page.
    details = [u for u in session.urls() if "jobPosting/" in u]
    assert details == [DETAIL_URL.format(job_id="1000001"),
                       DETAIL_URL.format(job_id="1000004")]
    by_id = {job.raw["linkedin_id"]: job for job in jobs}
    assert set(by_id) == {"1000001", "1000002", "1000003", "1000004"}
    first = by_id["1000001"]
    assert first.url == "https://www.linkedin.com/jobs/view/1000001"
    assert first.source == "linkedin_search" and "Python" in first.description
    assert first.raw["employment_type"] == "Full-time"
    # Dated two days back, but the 24-hour window says it is younger than that.
    assert first.posted_at == NOW - timedelta(hours=24)
    assert by_id["1000004"].remote is True
    assert by_id["1000002"].description == ""
    assert sleeps == []  # delay_seconds: 0


def test_a_closed_posting_and_an_undescribed_promising_card_are_held_back(tmp_path):
    cfg = li_config(tmp_path, max_descriptions=2)
    session = FakeSession([
        ("jobPosting/1000001", FakeResponse(text=detail(closed=True))),
        ("jobPosting/1000002", FakeResponse(text=detail())),
        (SEARCH_URL, page(card("1000001", "Data Scientist"),
                          card("1000002", "Data Scientist"),
                          card("1000003", "Data Scientist"))),
    ])
    jobs = fetch(cfg, session=session, now=NOW)
    # 1 closed, 3 past the description cap: neither may reach the scorer.
    assert [job.raw["linkedin_id"] for job in jobs] == ["1000002"]
    assert "jobPosting/1000003" not in " ".join(session.urls())


def test_paging_follows_full_pages_within_the_shared_request_cap(tmp_path):
    cfg = li_config(tmp_path, max_requests=3, max_descriptions=1)
    full = page(*(card(str(2000000 + n), "Senior Data Scientist") for n in range(10)))
    session = FakeSession([(SEARCH_URL, full)])
    sleeps = []
    cfg.watchlist["linkedin_search"]["delay_seconds"] = 2.0
    fetch(cfg, session=session, sleep=sleeps.append, now=NOW)
    starts = [p["start"] for p in session.params_for("seeMoreJobPostings")]
    assert starts == [0, 10, 20]
    assert len(sleeps) == 2 and all(2.0 <= s <= 3.0 for s in sleeps)


def test_the_first_refusal_stops_every_further_linkedin_request(tmp_path):
    cfg = li_config(tmp_path, queries=[
        {"keywords": "data scientist", "location": "Poland"},
        {"keywords": "machine learning engineer", "location": "Poland"},
    ])
    session = FakeSession([
        (SEARCH_URL, lambda url, params: page(card("1000001", "Data Scientist"))
         if params["keywords"] == "data scientist" else FakeResponse(status_code=429)),
    ])
    errors = []
    jobs = fetch(cfg, session=session, errors=errors, now=NOW)
    assert len(session.calls) == 2  # no retry, no detail page after the 429
    assert any("refused" in e and "429" in e for e in errors)
    # The promising card from query 1 was never described, so it is held back.
    assert jobs == []


def test_linkedins_999_counts_as_a_refusal(tmp_path):
    cfg = li_config(tmp_path)
    session = FakeSession([(SEARCH_URL, FakeResponse(status_code=999))])
    errors = []
    assert fetch(cfg, session=session, errors=errors, now=NOW) == []
    assert len(session.calls) == 1 and "999" in errors[0]


def test_the_source_ships_off_ranks_with_the_email_alerts_and_stays_out_of_explore():
    assert DEFAULTS["sources"]["linkedin_search"] is False
    assert SOURCE_RANK["linkedin_search"] == SOURCE_RANK["linkedin_email"]
    assert "linkedin_search" not in PUBLIC_SOURCES


def test_validation_names_a_query_without_keywords_and_a_bad_work_type(tmp_path):
    cfg = li_config(tmp_path, queries=[{"location": "Poland"},
                                       {"keywords": "x", "work_type": "office"}])
    problems = cfg.validate()
    assert any("queries[0] needs keywords" in p for p in problems)
    assert any("queries[1].work_type" in p for p in problems)


def test_the_shipped_config_turns_it_on_with_queries_that_validate():
    from src.config import Config

    cfg = Config.load()
    assert cfg.source_enabled("linkedin_search")
    assert not [p for p in cfg.validate() if "linkedin_search" in p]
    settings = cfg.watchlist["linkedin_search"]
    assert settings["max_requests"] <= 40 and settings["max_descriptions"] <= 80
