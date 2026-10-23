"""Tests for src/main.py — the whole pipeline, end to end, offline.

`run_pipeline` takes four seams (`tracker=`, `llm_client=`, `now=`,
`sources=`), and with those supplied the entire run — fetch, dedupe, filter,
tracker gate, score, tailor, PDF, apply, digest — executes with no network,
no API key and no browser. That is what makes an integration test of this
shape possible at all, so the first thing tested is that it really is
hermetic.

The properties that matter here are the ones no unit test can see:

  * the stages compose in the right order and each degrades independently —
    a dead source, a broken filter or a failed digest must not abort the run;
  * the funnel adds up, because those numbers are the only way a user can
    tell a quiet day from a broken pipeline;
  * running twice does not re-surface yesterday's jobs, which is the whole
    reason the tracker exists.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from src import main as main_module
from src.config import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_REPOST_MIN_GAP_DAYS,
    DEFAULTS,
    ConfigError,
)
from src.db import Tracker
from src.main import (
    apply_cli_overrides,
    build_parser,
    format_summary,
    main,
    run_pipeline,
)
from src.models import ApplyStatus
from tests.conftest import (
    BASE_CV,
    NOW,
    FakeSession,
    json_response,
    llm_client,
    load_json_fixture,
    make_job,
    write_config,
)

GREENHOUSE = load_json_fixture("greenhouse_jobs.json")

SCORE_JSON = json.dumps({
    "score": 88, "verdict": "Strong fit",
    "reasons": ["CV shows 8y Python, posting asks 5+"],
    "strengths": ["Python"], "gaps": ["No Kafka"],
})
TAILORED = "# Ada Lovelace\n\n## Summary\nSenior backend engineer.\n"
COVER = "Dear team,\n\nI cut p99 latency 840ms to 210ms.\n\nAda Lovelace\n"

# score, cv, cover — repeated for each job; the last entry repeats forever.
LLM_SCRIPT = [SCORE_JSON, TAILORED, COVER, SCORE_JSON]


def pipeline_config(tmp_path: Path, **overrides):
    base = {
        "sources": {"greenhouse": True, "lever": False,
                    "adzuna": False, "linkedin_email": False},
        "output": {"dir": str(tmp_path / "output"), "open_browser": False},
        "db": {"path": str(tmp_path / "output" / "tracker.sqlite3"),
               "skip_seen_days": 30},
        "scoring": {"threshold": 65, "concurrency": 1},
        "tailoring": {"enabled": True, "max_per_run": 10},
        "apply": {"enabled": False},     # no browser in the default test run
        "freshness": {"max_age_hours": 24},
    }
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return write_config(tmp_path, base, watchlist={"greenhouse": ["acme"]})


@pytest.fixture
def stub_sources(monkeypatch):
    """Replace the network-facing source fetchers.

    `run_pipeline` has no `session=` seam (sources read it from config), so
    this is the one place the suite patches module attributes — and it patches
    the *public* fetchers, exactly as a caller would see them.
    """
    calls: list[str] = []
    results: dict[str, object] = {}

    def make(name):
        def _fetch(config, **kwargs):
            calls.append(name)
            outcome = results.get(name, [])
            if isinstance(outcome, Exception):
                raise outcome
            return list(outcome)
        return _fetch

    monkeypatch.setattr(main_module.ats_boards, "fetch", make("ats_boards"))
    monkeypatch.setattr(main_module.adzuna, "fetch", make("adzuna"))
    monkeypatch.setattr(main_module.linkedin_email, "fetch", make("linkedin_email"))
    make.calls = calls          # type: ignore[attr-defined]
    make.results = results      # type: ignore[attr-defined]
    return make


def fresh_jobs(n=3):
    # Distinct URLs, as distinct jobs have: dedupe's first pass treats one
    # canonical URL as one posting, whatever the rest of the record says.
    return [make_job(company=f"Company{i}", title="Backend Engineer",
                     location="Berlin, Germany", hours_old=2, ats_job_id=str(i),
                     url=f"https://boards.greenhouse.io/company{i}/jobs/{i}")
            for i in range(n)]


# ==========================================================================
# the happy path
# ==========================================================================


def test_a_full_run_produces_a_digest(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = pipeline_config(tmp_path)

    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))

    assert len(scored) == 2
    assert stats.fetched == 2
    assert stats.matches == 1
    assert stats.scoring_pending == 1
    digest = Path(stats.digest_path)
    assert digest.exists()
    assert "Company0" in digest.read_text(encoding="utf-8")


def test_the_run_is_hermetic(tmp_path: Path, stub_sources, memory_tracker):
    """No network, no key, no browser — asserted by the absence of any real
    session, and by the run completing with a fake LLM client."""
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path, keys={"anthropic": "", "openrouter": ""})
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))
    assert len(scored) == 1


def test_tailored_documents_land_next_to_each_other(tmp_path: Path, stub_sources,
                                                    memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path)
    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                             llm_client=llm_client(LLM_SCRIPT))

    directory = Path(scored[0].artifacts.dir)
    assert (directory / "cv.md").exists()
    assert (directory / "cover_letter.md").exists()
    assert (directory / "job.json").exists()


def test_the_funnel_adds_up(tmp_path: Path, stub_sources, memory_tracker):
    """These numbers are the only way to tell a quiet day from a broken run,
    so they have to be internally consistent."""
    jobs = fresh_jobs(2) + [
        make_job(company="Stale", location="Berlin, Germany", hours_old=99,
                 ats_job_id="s"),
        make_job(company="US", location="San Francisco, CA", hours_old=1,
                 ats_job_id="u"),
    ]
    stub_sources.results["ats_boards"] = jobs
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))

    assert stats.fetched == 4
    assert stats.after_dedupe == 4
    assert stats.after_filters == 2
    assert stats.scoring_attempted == 2
    assert stats.scored == stats.scoring_completed == 1
    assert stats.matches == 1
    assert stats.scoring_pending == 1
    assert stats.filter_counts == {"stale": 1, "location_outside_eu": 1}
    assert stats.to_dict()["filter_counts"] == stats.filter_counts
    assert stats.matches == stats.auto_applied + stats.dry_run + stats.digest_items


def test_a_relisted_job_is_flagged_on_the_page_and_not_dropped_from_it(
        tmp_path: Path, stub_sources, memory_tracker):
    """The wiring behind the ghost-job flag: `run_pipeline` hands the tracker
    to the digest, which is the only way the page can know a role was already
    listed under a different job id. And it fires **through the real pipeline
    at the shipped threshold** — which is the bar the deleted age flag could
    never clear, since anything old enough to trip it had been dropped by the
    freshness filter weeks earlier.

    Both halves are asserted, and the second is the important one. A flag that
    also removed the job would be worse than no flag at all.

    That second half has to be a *cross-check*, not a repetition. Asserting
    `after_filters == 2` and `matches == 2` proves nothing about the digest:
    both counters are set at pipeline steps 3 and 6, before `write_digest` is
    ever called, so no change to the digest stage can move them. Mutating
    `build_context` to delete every flagged job left both passing. What bites
    is the funnel number against the cards actually on the page.
    """
    earlier = make_job(company="Company0", title="Backend Engineer",
                       location="Berlin, Germany", ats_job_id="old-req",
                       posted_at=NOW - timedelta(days=120))
    memory_tracker.record_job(earlier, now=NOW - timedelta(days=120))

    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = pipeline_config(tmp_path)
    assert cfg.get("freshness.repost_min_gap_days") == DEFAULT_REPOST_MIN_GAP_DAYS
    scored, stats = run_pipeline(cfg, tracker=memory_tracker,
                                 now=NOW, llm_client=llm_client(LLM_SCRIPT))

    html = Path(stats.digest_path).read_text(encoding="utf-8")
    assert "On the market 120 days or more" in html      # the flag fired ...
    # ... and the page carries one card per job the run produced. The funnel is
    # counted at step 6, the cards are built at step 11; dropping a flagged job
    # breaks the equality between them. Every job here is above the threshold,
    # so `matches` is the whole set and the count is unambiguous.
    assert len(scored) == 2
    assert stats.matches == 1 and stats.scoring_pending == 1
    assert html.count('<article class="card') == stats.matches + stats.scoring_pending
    assert "Company0" in html and "Company1" in html


def test_source_counts_are_recorded(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(3)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert sum(stats.source_counts.values()) == 3


# ==========================================================================
# the cost ceiling spends itself on the freshest postings
# ==========================================================================


def shipped_defaults_config(tmp_path: Path, freshness=None, **overrides):
    """A pipeline config that really does use the shipped freshness window and
    the shipped cost ceiling, not the tighter ones the other tests pick."""
    window = {"max_age_hours": DEFAULT_MAX_AGE_HOURS}
    window.update(freshness or {})
    cfg = pipeline_config(tmp_path, freshness=window, **overrides)
    assert cfg.get("freshness.max_age_hours") == DEFAULT_MAX_AGE_HOURS == 72
    assert cfg.get("scoring.max_jobs") == DEFAULTS["scoring"]["max_jobs"] == 40
    return cfg


def test_the_freshest_postings_are_never_the_ones_the_cap_drops(
        tmp_path: Path, stub_sources, memory_tracker):
    """The regression the 24h -> 72h widening introduced, end to end.

    `scoring.max_jobs` slices `batch[:max_jobs]`, and until the pipeline
    sorted, that slice was in **fetch order**: `_fetch_all` extends in source
    order and `apply_filters` and `_gate_on_tracker` both append in input
    order. So board order decided who got scored. Forty postings 48 hours old
    ahead of five posted two hours ago, at the shipped ceiling of 40:

        parent (24h window)   after_filters 5    cards 5   2h-old shown 5 of 5
        72h window, unsorted  after_filters 45   cards 40  2h-old shown 0 of 5

    Every one of the five freshest postings in the run was cut. The harm is a
    one-run delay rather than a permanent loss — nothing truncated before
    scoring gets an `applications` row, so `should_surface` brings it back
    tomorrow — but it is systematic in the worst direction: on any given
    morning you were least likely to see the postings you most wanted.

    Widening the window was right; spending the ceiling in board order was the
    bug. Recency now decides.
    """
    old = [make_job(company=f"Old{i}", title="Backend Engineer",
                    location="Berlin, Germany", hours_old=48, ats_job_id=f"o{i}")
           for i in range(40)]
    newest = [make_job(company=f"New{i}", title="Backend Engineer",
                       location="Berlin, Germany", hours_old=2, ats_job_id=f"n{i}")
              for i in range(5)]
    stub_sources.results["ats_boards"] = old + newest      # board order: old first

    scored, stats = run_pipeline(shipped_defaults_config(tmp_path),
                                 tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))

    assert stats.after_filters == 45      # all 45 are inside the 72h window
    assert len(scored) == 40              # and 40 is the ceiling, as before
    shown = {item.job.company for item in scored}
    assert {f"New{i}" for i in range(5)} <= shown, (
        "the five freshest postings in the run were cut by the cost ceiling"
    )


def test_the_limit_flag_also_keeps_the_freshest(tmp_path: Path, stub_sources,
                                                memory_tracker):
    """`--limit` truncates in the same place and had the same bug. It is the
    flag a user reaches for to try the tool cheaply, so handing them the five
    oldest postings on the board is a bad first impression on top of a bug."""
    old = [make_job(company=f"Old{i}", title="Backend Engineer",
                    location="Berlin, Germany", hours_old=60, ats_job_id=f"o{i}")
           for i in range(6)]
    newest = make_job(company="Newest", title="Backend Engineer",
                      location="Berlin, Germany", hours_old=1, ats_job_id="n")
    stub_sources.results["ats_boards"] = old + [newest]

    scored, _ = run_pipeline(shipped_defaults_config(tmp_path), limit=2,
                             tracker=memory_tracker, now=NOW,
                             llm_client=llm_client(LLM_SCRIPT))
    assert "Newest" in {item.job.company for item in scored}


def test_an_undated_posting_queues_behind_every_dated_one(
        tmp_path: Path, stub_sources, memory_tracker):
    """Undated postings need a defined position in that order, and this is it:
    **last, in fetch order**.

    `skip_undated` ships true, so an undated posting only gets this far when
    the user has turned it off and, in that setting's own words, accepted some
    staleness. Ranking it above a posting we can prove is two hours old would
    contradict `filters.is_fresh`, which holds that an undated posting cannot
    be proven fresh at all. And the alternative starves the other side: ATS
    endpoints return every open requisition rather than the recent ones, so
    `skip_undated: false` can admit hundreds of undated postings in one run
    and putting them first would evict every dated posting from the ceiling.

    The cost, stated rather than hidden: a board that never dates anything is
    scored last, and on a busy morning not at all that day. It is still better
    off than under the shipped default, which drops it outright.
    """
    undated = [make_job(company=f"Undated{i}", title="Backend Engineer",
                        location="Berlin, Germany", hours_old=None,
                        ats_job_id=f"u{i}")
               for i in range(3)]
    dated = make_job(company="Dated", title="Backend Engineer",
                     location="Berlin, Germany", hours_old=48, ats_job_id="d")
    stub_sources.results["ats_boards"] = undated + [dated]   # undated arrive first

    cfg = shipped_defaults_config(tmp_path, freshness={"skip_undated": False})
    scored, stats = run_pipeline(cfg, limit=1, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))

    assert stats.after_filters == 4         # skip_undated is off, so all four
    assert [item.job.company for item in scored] == ["Dated"]


# ==========================================================================
# the tracker gate
# ==========================================================================


def test_a_second_run_does_not_resurface_the_same_jobs(tmp_path: Path, stub_sources,
                                                       memory_tracker):
    """The reason the tracker exists. Without it every morning's digest is
    yesterday's digest plus one."""
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = pipeline_config(tmp_path)

    first, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client([SCORE_JSON, SCORE_JSON, TAILORED, COVER]))
    # Two hours later, not a day: the postings must still be *fresh*, so that
    # the tracker gate is demonstrably what drops them rather than the
    # freshness filter quietly doing it for the wrong reason.
    second, stats = run_pipeline(cfg, tracker=memory_tracker,
                                 now=NOW + timedelta(hours=2),
                                 llm_client=llm_client(LLM_SCRIPT))

    assert len(first) == 2
    assert second == []
    assert stats.after_filters == 2      # they survived every hard filter ...
    assert stats.already_seen == 2       # ... and the tracker is what stopped them


def test_jobs_resurface_once_the_window_expires(tmp_path: Path, stub_sources,
                                                memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path, db={"skip_seen_days": 7})

    run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=llm_client(LLM_SCRIPT))
    # The posting has to still be fresh, so move its date forward too.
    stub_sources.results["ats_boards"] = [
        make_job(company="Company0", title="Backend Engineer",
                 location="Berlin, Germany", ats_job_id="0",
                 posted_at=NOW + timedelta(days=8))
    ]
    later, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(days=8, hours=2),
                            llm_client=llm_client(LLM_SCRIPT))
    assert len(later) == 1


def test_filtered_jobs_are_recorded_so_they_are_not_re_evaluated(
        tmp_path: Path, stub_sources, memory_tracker):
    """A stale posting only ever gets staler. Without a row, every morning
    re-fetches and re-judges the identical job, forever."""
    stale = make_job(company="Stale", location="Berlin, Germany", hours_old=99,
                     ats_job_id="s")
    stub_sources.results["ats_boards"] = [stale]
    run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker, now=NOW,
                 llm_client=llm_client(LLM_SCRIPT))
    assert memory_tracker.get_status(stale.key) == ApplyStatus.FILTERED.value


class CountingScorer:
    """Answers every scoring call with a match and counts them. The runs that
    use it switch tailoring off, so every call it sees is a scoring call."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        return {"score": 85, "reasons": ["Python matches"], "strengths": [],
                "gaps": [], "verdict": "good fit"}


def test_a_cross_source_copy_of_yesterdays_digest_is_not_scored_again(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    """Day one the Greenhouse record is scored and lands in the digest. Day
    two the Arbeitnow copy of the same job carries a longer description, so it
    wins in-run dedupe — under a key the tracker has never seen. That copy was
    scored and shown a second time; now its handled twin stops it."""
    aggregator: list = []
    monkeypatch.setattr(main_module.arbeitnow, "fetch",
                        lambda config, **kwargs: list(aggregator))
    cfg = pipeline_config(tmp_path, sources={"arbeitnow": True},
                          tailoring={"enabled": False})
    ats = make_job(company="Celonis", title="Data Engineer", location="Munich, Germany",
                   hours_old=2, ats_job_id="4711")
    stub_sources.results["ats_boards"] = [ats]
    first = CountingScorer()
    day_one, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=first)
    assert [item.key for item in day_one] == [ats.key] and first.calls == 1
    assert memory_tracker.get_status(ats.key) == ApplyStatus.DIGEST.value

    copy = make_job(source="arbeitnow", company="Celonis", title="Data Engineer",
                    location="Munich, Germany", hours_old=2, ats=None, ats_job_id=None,
                    url="https://www.arbeitnow.com/jobs/celonis/data-engineer-munich",
                    description=ats.description + " Benefits, team and interview steps.")
    assert copy.dedupe_key == ats.dedupe_key and copy.key != ats.key
    aggregator.append(copy)
    second = CountingScorer()
    day_two, stats = run_pipeline(cfg, tracker=memory_tracker,
                                  now=NOW + timedelta(hours=20), llm_client=second)

    assert second.calls == 0 and day_two == []
    assert stats.after_dedupe == 1 and stats.after_filters == 1
    assert stats.already_seen == 1 and stats.filter_counts == {}
    row = memory_tracker.get_application(copy.key)
    assert row["status"] == ApplyStatus.FILTERED.value
    assert row["detail"] == (f"cross-source duplicate of {ats.key[:8]} "
                             f"(greenhouse, digest {NOW.date().isoformat()})")
    assert memory_tracker.get_status(ats.key) == ApplyStatus.DIGEST.value


def test_a_queued_copy_whose_twin_was_handled_stops_without_a_model_call(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    """The backlog path: the aggregator copy was queued behind the cap while
    its Greenhouse twin was scored. When it is next observed it stops on the
    existing protected path, not with a second evaluation."""
    aggregator: list = []
    monkeypatch.setattr(main_module.arbeitnow, "fetch",
                        lambda config, **kwargs: list(aggregator))
    cfg = pipeline_config(tmp_path, sources={"arbeitnow": True},
                          tailoring={"enabled": False})
    ats = make_job(company="Celonis", title="Data Engineer", location="Munich, Germany",
                   hours_old=2, ats_job_id="4711")
    copy = make_job(source="arbeitnow", company="Celonis", title="Data Engineer",
                    location="Munich, Germany", hours_old=2, ats=None, ats_job_id=None,
                    url="https://www.arbeitnow.com/jobs/celonis/data-engineer-munich")
    memory_tracker.enqueue_scoring(copy, now=NOW - timedelta(hours=1))
    memory_tracker.record_job(ats, now=NOW - timedelta(hours=1))
    memory_tracker.record_status(ats.key, ApplyStatus.DIGEST, now=NOW - timedelta(hours=1))
    aggregator.append(copy)
    client = CountingScorer()
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=client)

    assert client.calls == 0 and scored == []
    row = memory_tracker.get_scoring(copy.key)
    assert row["state"] == "protected"
    assert row["detail"].startswith(f"cross-source duplicate of {ats.key[:8]} (greenhouse")
    assert stats.already_seen == 1


def test_a_same_board_repost_is_still_scored_and_flagged(
        tmp_path: Path, stub_sources, memory_tracker):
    """The twin check ignores same-board rows on purpose: a new id on the
    same board is a re-listing, and the digest's repost flag is the place
    that says so — on a card that therefore has to exist."""
    earlier = make_job(company="Company0", title="Backend Engineer",
                       location="Berlin, Germany", ats_job_id="old-req",
                       posted_at=NOW - timedelta(days=20))
    memory_tracker.record_job(earlier, now=NOW - timedelta(days=20))
    memory_tracker.record_status(earlier.key, ApplyStatus.DIGEST,
                                 now=NOW - timedelta(days=20))
    repost = make_job(company="Company0", title="Backend Engineer",
                      location="Berlin, Germany", ats_job_id="new-req", hours_old=2)
    stub_sources.results["ats_boards"] = [repost]
    client = CountingScorer()
    scored, stats = run_pipeline(pipeline_config(tmp_path, tailoring={"enabled": False}),
                                 tracker=memory_tracker, now=NOW, llm_client=client)

    assert client.calls == 1 and [item.key for item in scored] == [repost.key]
    assert stats.already_seen == 0
    assert "On the market 20 days or more" in Path(stats.digest_path).read_text(encoding="utf-8")


def nofluff_copy(location, n):
    """A No Fluff Jobs-shaped copy of one posting, listed for one city."""
    return make_job(source="nofluffjobs", company="Northwind", title="Data Engineer",
                    location=location, hours_old=2, ats=None, ats_job_id=f"nfj-{n}",
                    url=f"https://nofluffjobs.com/job/northwind-data-engineer-{n}",
                    description="Main technology: Python. Seniority: Mid.",
                    raw={"snippet_only": True})


def test_one_posting_listed_per_city_is_scored_once_as_the_home_copy(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    copies = [nofluff_copy("Warsaw, Cracow, Gdańsk", 1),
              nofluff_copy("Cracow, Gdańsk, Warsaw", 2),
              nofluff_copy("Gdańsk, Warsaw, Cracow", 3)]
    monkeypatch.setattr(main_module.nofluffjobs, "fetch",
                        lambda config, **kwargs: list(copies))
    cfg = pipeline_config(tmp_path, sources={"greenhouse": False, "nofluffjobs": True},
                          applicant={"location": "Kraków, Poland"},
                          tailoring={"enabled": False})
    client = CountingScorer()
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=client)

    assert client.calls == 1 and [item.key for item in scored] == [copies[1].key]
    assert scored[0].job.raw["also_in"] == ["Warsaw", "Gdańsk"]
    assert stats.after_dedupe == 3 and stats.after_filters == 1
    assert stats.filter_counts == {"same_posting_other_city": 2}
    assert stats.to_dict()["filter_counts"] == stats.filter_counts
    for loser in (copies[0], copies[2]):
        row = memory_tracker.get_application(loser.key)
        assert row["status"] == ApplyStatus.FILTERED.value
        assert row["detail"].startswith("same_posting_other_city:")


def test_collapsing_city_copies_leaves_never_apply_twice_alone(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    """Only merging, never unblocking: an applied home copy stays blocked, and
    an applied copy recorded as a city variant keeps its `applied` row."""
    home, other = nofluff_copy("Cracow, Warsaw", 1), nofluff_copy("Warsaw, Cracow", 2)
    for job in (home, other):
        memory_tracker.record_job(job, now=NOW - timedelta(days=60))
        memory_tracker.record_status(job.key, ApplyStatus.APPLIED,
                                     now=NOW - timedelta(days=60))
    monkeypatch.setattr(main_module.nofluffjobs, "fetch",
                        lambda config, **kwargs: [other, home])
    cfg = pipeline_config(tmp_path, sources={"greenhouse": False, "nofluffjobs": True},
                          applicant={"location": "Kraków, Poland"},
                          tailoring={"enabled": False})
    client = CountingScorer()
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=client)

    assert client.calls == 0 and scored == []
    assert stats.filter_counts == {"same_posting_other_city": 1}
    assert memory_tracker.has_applied(home.key) and memory_tracker.has_applied(other.key)


# --------------------------------------------------------------------------
# city copies against what the tracker already holds
# --------------------------------------------------------------------------

#: One ad, word for word, published as a separate Greenhouse req per city.
VARIANT_AD = "We are hiring a data engineer to build pipelines in Python and SQL. " * 15


def city_req(req, location, **overrides):
    fields = dict(company="Acme", title="Data Engineer", location=location,
                  description=VARIANT_AD, ats_job_id=str(req), hours_old=3,
                  url=f"https://boards.greenhouse.io/acme/jobs/{req}")
    fields.update(overrides)
    return make_job(**fields)


def warsaw_config(tmp_path: Path):
    return pipeline_config(tmp_path, applicant={"location": "Warsaw, Poland"},
                           tailoring={"enabled": False})


def test_a_new_city_copy_of_an_ad_already_applied_to_is_never_scored(
        tmp_path: Path, stub_sources, memory_tracker):
    """Berlin req applied to six days ago — stale today, so the filters drop
    it — and today the same ad as a Warsaw req, the profile's city. Collapse
    kept the Warsaw copy and every gate keys on it, so it was scored and
    could be auto-applied: a second application to one posting. Neither on
    the day, nor after the reminder window while Berlin is no longer listed."""
    applied = city_req(111, "Berlin, Germany", hours_old=24 * 6)
    memory_tracker.record_job(applied, now=NOW - timedelta(days=5))
    memory_tracker.record_status(applied.key, ApplyStatus.APPLIED, method="greenhouse",
                                 now=NOW - timedelta(days=5))
    home = city_req(222, "Warsaw, Poland")
    stub_sources.results["ats_boards"] = [applied, home]
    client = CountingScorer()
    scored, stats = run_pipeline(warsaw_config(tmp_path), tracker=memory_tracker, now=NOW,
                                 llm_client=client)

    assert client.calls == 0 and scored == []
    assert stats.after_filters == 0 and stats.filter_counts["same_posting_other_city"] == 1
    row = memory_tracker.get_application(home.key)
    assert (row["status"], row["detail"]) == (
        "filtered", f"same_posting_other_city: the copy listed for Berlin "
                    f"({applied.key[:8]}) was already applied to")
    assert memory_tracker.get_scoring(home.key) is None

    later = NOW + timedelta(days=31)
    stub_sources.results["ats_boards"] = [city_req(222, "Warsaw, Poland", posted_at=later)]
    scored, stats = run_pipeline(warsaw_config(tmp_path), tracker=memory_tracker, now=later,
                                 llm_client=client)
    assert client.calls == 0 and scored == [] and stats.already_seen == 1
    assert memory_tracker.get_application(home.key)["updated_at"] == later.isoformat()


def test_a_city_copy_of_queued_work_is_held_back_not_scored_beside_it(
        tmp_path: Path, stub_sources, memory_tracker):
    """Queued work skips the filters and the collapse, so yesterday's queued
    Warsaw copy and today's Berlin copy were both scored."""
    queued = city_req(1, "Warsaw, Poland", hours_old=26)
    memory_tracker.enqueue_scoring(queued, now=NOW - timedelta(days=1))
    sibling = city_req(2, "Berlin, Germany")
    stub_sources.results["ats_boards"] = [queued, sibling]
    client = CountingScorer()
    scored, stats = run_pipeline(warsaw_config(tmp_path), tracker=memory_tracker, now=NOW,
                                 llm_client=client)

    assert client.calls == 1 and [item.key for item in scored] == [queued.key]
    row = memory_tracker.get_application(sibling.key)
    assert (row["status"], row["detail"]) == (
        "filtered", f"same_posting_other_city: the copy listed for Warsaw "
                    f"({queued.key[:8]}) is already tracked")
    assert stats.filter_counts == {"same_posting_other_city": 1}


def test_two_queued_copies_of_one_ad_are_scored_once(
        tmp_path: Path, stub_sources, memory_tracker):
    """Both copies were admitted on days the other was not listed; today both
    are ready. The combined list is collapsed once more, the loser stops on
    the protected path, and the counts say where it went."""
    berlin, warsaw = city_req(1, "Berlin, Germany", hours_old=50), city_req(2, "Warsaw, Poland")
    memory_tracker.enqueue_scoring(berlin, now=NOW - timedelta(days=2))
    memory_tracker.enqueue_scoring(warsaw, now=NOW - timedelta(days=1))
    stub_sources.results["ats_boards"] = [berlin, warsaw]
    client = CountingScorer()
    scored, stats = run_pipeline(warsaw_config(tmp_path), tracker=memory_tracker, now=NOW,
                                 llm_client=client)

    assert client.calls == 1 and [item.key for item in scored] == [warsaw.key]
    row = memory_tracker.get_scoring(berlin.key)
    assert (row["state"], row["detail"]) == (
        "protected", f"same_posting_other_city: the copy listed for Warsaw "
                     f"({warsaw.key[:8]}) was kept")
    assert stats.after_filters == 1 and stats.source_after_filters["greenhouse"] == 1
    assert stats.filter_counts == {"same_posting_other_city": 1}
    assert stats.scoring_pending == 0


def test_queued_work_stops_when_a_city_copy_was_applied_to(
        tmp_path: Path, stub_sources, memory_tracker):
    applied = city_req(1, "Berlin, Germany", hours_old=24 * 6)
    memory_tracker.record_job(applied, now=NOW - timedelta(days=5))
    memory_tracker.record_status(applied.key, ApplyStatus.APPLIED, now=NOW - timedelta(days=5))
    queued = city_req(2, "Warsaw, Poland", hours_old=26)
    memory_tracker.enqueue_scoring(queued, now=NOW - timedelta(days=1))
    stub_sources.results["ats_boards"] = [applied, queued]
    client = CountingScorer()
    scored, _ = run_pipeline(warsaw_config(tmp_path), tracker=memory_tracker, now=NOW,
                             llm_client=client)

    assert client.calls == 0 and scored == []
    row = memory_tracker.get_scoring(queued.key)
    assert row["state"] == "protected"
    assert row["detail"].endswith(f"({applied.key[:8]}) was already applied to")


def test_a_held_copy_stands_in_when_its_representative_is_no_longer_listed(
        tmp_path: Path, stub_sources, memory_tracker):
    """Day one the Warsaw copy is queued and Berlin held back. Day two only
    Berlin is listed: it used to stay hidden for the whole reminder window
    while Warsaw waited to be seen again — long enough to go stale. Now it
    is scored, and Warsaw's queued work is held back for it instead."""
    warsaw, berlin = city_req(1, "Warsaw, Poland"), city_req(2, "Berlin, Germany")
    stub_sources.results["ats_boards"] = [warsaw, berlin]
    cfg = warsaw_config(tmp_path)
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, skip_llm=True)
    assert memory_tracker.get_scoring(warsaw.key)["state"] == "pending"
    assert memory_tracker.get_status(berlin.key) == ApplyStatus.FILTERED.value

    stub_sources.results["ats_boards"] = [berlin]
    client = CountingScorer()
    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(hours=18),
                             llm_client=client)
    assert client.calls == 1 and [item.key for item in scored] == [berlin.key]
    row = memory_tracker.get_scoring(warsaw.key)
    assert (row["state"], row["detail"]) == (
        "protected", f"same_posting_other_city: the copy listed for Berlin "
                     f"({berlin.key[:8]}) stands in for it")

    stub_sources.results["ats_boards"] = [warsaw, berlin]
    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(hours=20),
                             llm_client=client)
    assert client.calls == 1 and scored == []


def test_copies_held_back_for_work_that_expires_unscored_are_released(
        tmp_path: Path, stub_sources, memory_tracker):
    warsaw, berlin = city_req(1, "Warsaw, Poland"), city_req(2, "Berlin, Germany")
    stub_sources.results["ats_boards"] = [warsaw, berlin]
    cfg = warsaw_config(tmp_path)
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, skip_llm=True)
    assert memory_tracker.get_status(berlin.key) == ApplyStatus.FILTERED.value

    stub_sources.results["ats_boards"] = []
    _, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(days=8),
                            skip_llm=True)
    assert memory_tracker.get_scoring(warsaw.key)["state"] == "expired"
    assert memory_tracker.get_application(berlin.key) is None
    assert any(warsaw.key in error and "1 copy/copies of the same posting" in error
               for error in stats.errors)


class BelowScorer(CountingScorer):
    def complete_json(self, **kwargs):
        self.calls += 1
        return {"score": 40, "reasons": ["thin match"], "strengths": [],
                "gaps": [], "verdict": "weak"}


def test_a_teaser_scored_below_does_not_hide_the_full_ad_from_another_board(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    """Yesterday a snippet-only board copy scored below the threshold; today
    the Greenhouse record of the same role arrives with the full ad. It was
    filtered as a cross-source duplicate and never read."""
    board: list = []
    monkeypatch.setattr(main_module.arbeitnow, "fetch", lambda config, **kwargs: list(board))
    cfg = pipeline_config(tmp_path, sources={"arbeitnow": True}, tailoring={"enabled": False})
    teaser = make_job(source="arbeitnow", company="Acme", title="Data Engineer",
                      location="Warsaw, Poland", hours_old=2, ats=None, ats_job_id=None,
                      url="https://www.arbeitnow.com/jobs/acme/data-engineer",
                      description="Data Engineer at Acme. Python, SQL.",
                      raw={"snippet_only": True})
    board.append(teaser)
    first = BelowScorer()
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=first)
    assert first.calls == 1
    assert memory_tracker.get_status(teaser.key) == ApplyStatus.SCORED_BELOW.value

    full = make_job(company="Acme", title="Data Engineer", location="Warsaw, Poland",
                    hours_old=2, ats_job_id="42", description="The full ad. " * 200)
    assert full.dedupe_key == teaser.dedupe_key
    board.clear()
    stub_sources.results["ats_boards"] = [full]
    second = CountingScorer()
    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(hours=20),
                             llm_client=second)
    assert second.calls == 1 and [item.key for item in scored] == [full.key]


def test_a_full_ad_scored_below_still_hides_its_copy_on_another_board(
        tmp_path: Path, stub_sources, memory_tracker, monkeypatch):
    board: list = []
    monkeypatch.setattr(main_module.arbeitnow, "fetch", lambda config, **kwargs: list(board))
    cfg = pipeline_config(tmp_path, sources={"arbeitnow": True}, tailoring={"enabled": False})
    full = make_job(company="Acme", title="Data Engineer", location="Warsaw, Poland",
                    hours_old=2, ats_job_id="42", description="The full ad. " * 200)
    stub_sources.results["ats_boards"] = [full]
    first = BelowScorer()
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=first)
    assert memory_tracker.get_application(full.key)["full_text"] == 1

    stub_sources.results["ats_boards"] = []
    board.append(make_job(source="arbeitnow", company="Acme", title="Data Engineer",
                          location="Warsaw, Poland", hours_old=2, ats=None, ats_job_id=None,
                          url="https://www.arbeitnow.com/jobs/acme/data-engineer",
                          description="The full ad. " * 210))
    second = CountingScorer()
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(hours=20),
                                 llm_client=second)
    assert second.calls == 0 and scored == [] and stats.already_seen == 1


def test_a_manual_application_stops_its_scoring_backlog(tmp_path: Path, stub_sources,
                                                        memory_tracker):
    """Applied by hand (`python -m src.track mark K applied`) while the job sat
    in the scoring queue: the next run must neither score it nor apply to it."""
    job = fresh_jobs(1)[0]
    stub_sources.results["ats_boards"] = [job]
    cfg = pipeline_config(tmp_path)
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, skip_llm=True)
    assert memory_tracker.get_scoring(job.key)["state"] == "pending"

    memory_tracker.record_event(job.key, "applied", on=NOW.date(), now=NOW)

    class Exploding:
        def complete(self, **kw):
            raise AssertionError("a manually applied job was scored")

        complete_json = complete

    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(hours=2),
                             llm_client=Exploding())
    assert scored == []
    row = memory_tracker.get_scoring(job.key)
    assert (row["state"], row["detail"]) == (
        "protected", "application or submit attempt already recorded")
    assert memory_tracker.get_status(job.key) == ApplyStatus.APPLIED.value


def test_final_statuses_are_persisted(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    scored, _ = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                             now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert memory_tracker.get_status(scored[0].key) == ApplyStatus.DIGEST.value


def test_the_pipeline_runs_without_a_tracker_at_all(tmp_path: Path, stub_sources):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    scored, _ = run_pipeline(pipeline_config(tmp_path), tracker=None, now=NOW,
                             llm_client=llm_client(LLM_SCRIPT))
    assert len(scored) == 1


# ==========================================================================
# degradation — one broken stage must not abort the run
# ==========================================================================


def test_a_dead_source_costs_that_source_and_nothing_else(tmp_path: Path,
                                                          stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = RuntimeError("greenhouse is down")
    cfg = pipeline_config(tmp_path)
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))
    assert scored == []
    assert any("greenhouse is down" in e for e in stats.errors)
    assert Path(stats.digest_path).exists()      # the digest still gets written


def test_a_scoring_outage_still_produces_a_digest(tmp_path: Path, stub_sources,
                                                  memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    scored, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                                 now=NOW, llm_client=llm_client(["not json at all"]))
    assert len(scored) == 2
    assert all(s.status is ApplyStatus.SCORING_PENDING for s in scored)
    assert Path(stats.digest_path).exists()


def test_no_sources_active_is_reported_not_silent(tmp_path: Path, stub_sources,
                                                  memory_tracker):
    cfg = pipeline_config(tmp_path, sources={"greenhouse": False})
    _, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client(LLM_SCRIPT))
    assert any("no sources" in e for e in stats.errors)


def test_a_missing_cv_is_fatal_before_any_api_spend(tmp_path: Path, stub_sources,
                                                    memory_tracker):
    """Scoring compares against the CV and tailoring rewrites it. Without one
    the run would still "work" and hand back a page of confident nonsense."""
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True},
                                  "output": {"dir": str(tmp_path / "output")}},
                       watchlist={"greenhouse": ["acme"]}, cv=None)
    with pytest.raises(ConfigError, match="cannot read the CV"):
        run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                     llm_client=llm_client(LLM_SCRIPT))


def test_a_stub_cv_is_rejected_too(tmp_path: Path, stub_sources, memory_tracker):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True},
                                  "output": {"dir": str(tmp_path / "output")}},
                       watchlist={"greenhouse": ["acme"]}, cv="# TODO write my CV")
    with pytest.raises(ConfigError, match="that is not"):
        run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                     llm_client=llm_client(LLM_SCRIPT))


# ==========================================================================
# source selection and limits
# ==========================================================================


def test_only_enabled_sources_are_fetched(tmp_path: Path, stub_sources, memory_tracker):
    run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker, now=NOW,
                 llm_client=llm_client(LLM_SCRIPT))
    assert stub_sources.calls == ["ats_boards"]


def test_sources_argument_narrows_further(tmp_path: Path, stub_sources, memory_tracker):
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": True},
                          keys={"adzuna_app_id": "x", "adzuna_app_key": "y"})
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, sources=["adzuna"],
                 llm_client=llm_client(LLM_SCRIPT))
    assert stub_sources.calls == ["adzuna"]


def test_limit_caps_what_reaches_scoring(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(5)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, limit=2, llm_client=llm_client(LLM_SCRIPT))
    assert stats.scoring_attempted == 2
    assert stats.scored == stats.scoring_completed == 1


def test_dedupe_collapses_the_same_role_seen_through_two_sources(
        tmp_path: Path, stub_sources, memory_tracker):
    """The cross-source case dedupe exists for: one posting reached us from
    both an ATS board and an aggregator."""
    stub_sources.results["ats_boards"] = [
        make_job(company="Acme", title="Backend Engineer", source="greenhouse",
                 location="Berlin, Germany", hours_old=2, ats=None, ats_job_id=None)
    ]
    stub_sources.results["adzuna"] = [
        make_job(company="Acme", title="Backend Engineer", source="adzuna",
                 location="Berlin, Germany", hours_old=2, ats=None,
                 ats_job_id=None, url="https://adzuna/1")
    ]
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": True},
                          keys={"adzuna_app_id": "x", "adzuna_app_key": "y"})
    _, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client(LLM_SCRIPT))
    assert stats.fetched == 2
    assert stats.after_dedupe == 1
    assert stats.after_filters == 1


def test_a_board_fetch_returning_a_foreign_source_is_discarded(
        tmp_path: Path, stub_sources, memory_tracker):
    """`ats_boards.fetch` serves greenhouse and lever in one call, so its
    output is filtered by source afterwards. A job tagged anything else came
    from somewhere unexpected and is dropped rather than trusted."""
    stub_sources.results["ats_boards"] = [
        make_job(company="Acme", source="greenhouse", location="Berlin, Germany",
                 hours_old=2, ats_job_id="1"),
        make_job(company="Sneaky", source="linkedin_email", location="Berlin, Germany",
                 hours_old=2, ats=None, ats_job_id=None),
    ]
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert stats.fetched == 1
    assert stats.source_counts == {"greenhouse": 1}


# ==========================================================================
# CLI
# ==========================================================================


def test_parser_accepts_the_documented_flags():
    args = build_parser().parse_args([
        "--no-browser", "--dry-run", "--skip-apply", "--limit", "5",
        "--source", "greenhouse", "--source", "lever", "--verbose",
    ])
    assert args.no_browser is True
    assert args.dry_run is True
    assert args.skip_apply is True
    assert args.limit == 5
    assert args.sources == ["greenhouse", "lever"]


@pytest.mark.parametrize(
    "name",
    ["greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
     "adzuna", "allegro", "linkedin_email"],
)
def test_every_source_can_be_named_on_the_cli(name):
    """`--source` is how you prove one board in isolation. A source missing
    from `SOURCE_NAMES` is rejected by argparse, so the flag that exists to
    debug it cannot be used on it."""
    from src.main import SOURCE_NAMES

    assert name in SOURCE_NAMES
    assert build_parser().parse_args(["--source", name]).sources == [name]


def test_the_board_sources_are_exactly_the_ones_ats_boards_serves():
    """`_fetch_all` calls `ats_boards.fetch` once and then keeps only the jobs
    whose `source` is in `BOARD_SOURCES`. A vendor missing from that set is
    fetched over the network and then silently thrown away."""
    from src.main import BOARD_SOURCES
    from src.sources.ats_boards import BOARDS

    assert BOARD_SOURCES == frozenset(BOARDS)


def test_dry_run_and_no_dry_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--dry-run", "--no-dry-run"])


def test_dry_run_defaults_to_whatever_the_config_says():
    assert build_parser().parse_args([]).dry_run is None


def test_cli_overrides_are_written_into_the_config(tmp_path: Path):
    """Downstream stages read the config and nothing else, so a flag that is
    not reflected here is a flag half the pipeline cannot see."""
    cfg = pipeline_config(tmp_path, apply={"enabled": True, "dry_run": True})
    args = build_parser().parse_args(["--no-dry-run", "--skip-apply", "--no-browser"])
    apply_cli_overrides(cfg, args)

    assert cfg.get("apply.dry_run") is False
    assert cfg.get("apply.enabled") is False
    assert cfg.get("output.open_browser") is False


def test_source_flag_narrows_but_never_enables(tmp_path: Path):
    """`--source adzuna` must not quietly switch on a paid API the user
    turned off on purpose."""
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": False})
    apply_cli_overrides(cfg, build_parser().parse_args(["--source", "adzuna"]))
    assert cfg.get("sources.adzuna") is False
    assert cfg.get("sources.greenhouse") is False


def test_validate_only_reports_and_exits(tmp_path: Path, capsys):
    write_config(tmp_path)
    code = main(["--validate-only", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0
    assert "config OK" in capsys.readouterr().out


def test_an_invalid_config_exits_1_and_lists_every_problem(tmp_path: Path, capsys):
    write_config(tmp_path, {"applicant": {"name": "", "email": ""},
                            "keys": {"openrouter": ""}}, cv=None)
    code = main(["--validate-only", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 1
    err = capsys.readouterr().err
    assert "problem(s)" in err
    assert "applicant.name" in err
    assert "openrouter" in err


def test_a_missing_config_file_still_validates_the_defaults(tmp_path: Path, capsys):
    code = main(["--validate-only", "--config", str(tmp_path / "nope.yaml"),
                 "--watchlist", str(tmp_path / "nope2.yaml")])
    assert code == 1                       # no applicant, no CV, no key


def test_main_returns_2_on_an_unexpected_failure(tmp_path: Path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(main_module, "_run_cli",
                        lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--config", str(tmp_path / "config.yaml")]) == 2


def test_main_returns_130_on_interrupt(tmp_path: Path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(main_module, "_run_cli",
                        lambda args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["--config", str(tmp_path / "config.yaml")]) == 130


def test_the_epilog_documents_the_canonical_invocations():
    help_text = build_parser().format_help()
    assert "python -m src.main" in help_text
    assert "--no-browser" in help_text
    assert "cron" in help_text.lower()


# ==========================================================================
# summary
# ==========================================================================


def test_format_summary_reads_like_the_documented_output(tmp_path: Path,
                                                         stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(3)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    summary = format_summary(stats)
    for token in ("fetched", "deduped", "eligible / retries", "attempted", "completed", "failed", "pending", "matched",
                  "auto-applied", "dry-run", "needs your click", "digest:"):
        assert token in summary


def test_format_summary_says_when_no_digest_was_written():
    from src.models import RunStats

    assert "not written" in format_summary(RunStats())


def test_format_summary_counts_errors(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = RuntimeError("down")
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert "error(s) this run" in format_summary(stats)


# ==========================================================================
# failure notification
# ==========================================================================


def alerting_config(tmp_path: Path, **notify_overrides):
    settings = {"enabled": True,
                "channels": {"console": False, "file": True,
                             "command": "", "email": {}}}
    settings.update(notify_overrides)
    return write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": False},
         "output": {"dir": str(tmp_path / "output"), "open_browser": False},
         "db": {"path": str(tmp_path / "output" / "tracker.sqlite3")},
         "apply": {"enabled": False}, "tailoring": {"enabled": False},
         "notify": settings},
        watchlist={"greenhouse": ["acme"]},
    )


def test_a_run_that_fetches_nothing_writes_an_alert(tmp_path: Path, monkeypatch,
                                                    capsys):
    """The whole point: an empty digest because every board 404'd must not
    look like a genuinely quiet Tuesday."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    alerting_config(tmp_path)

    code = main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0                       # exit_nonzero is off by default
    alert = (tmp_path / "output" / ALERT_FILENAME)
    assert alert.exists()
    assert "no postings fetched" in alert.read_text(encoding="utf-8")


def test_a_healthy_run_clears_yesterdays_alert(tmp_path: Path, monkeypatch):
    """A stale ALERT.txt after recovery is worse than none: it trains you to
    ignore the file."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch",
                        lambda config, **kw: fresh_jobs(2))
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))
    alerting_config(tmp_path)
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / ALERT_FILENAME).write_text("old", encoding="utf-8")

    main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
          "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()


def test_exit_nonzero_is_opt_in(tmp_path: Path, monkeypatch):
    """It changes the documented exit codes, so it must never turn itself on."""
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])

    alerting_config(tmp_path)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0

    alerting_config(tmp_path, exit_nonzero=True)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 4


def test_notify_on_filters_which_alerts_are_delivered(tmp_path: Path, monkeypatch):
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    alerting_config(tmp_path, on=["missed_run"])     # deliberately not no_jobs

    main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
          "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()


def test_a_broken_notifier_does_not_break_the_run(tmp_path: Path, monkeypatch,
                                                  capsys):
    """A notifier that takes down the run it was meant to warn about is
    strictly worse than no notifier."""
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    monkeypatch.setattr(main_module.notify, "send",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    alerting_config(tmp_path)

    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0
    assert "digest:" in capsys.readouterr().out


def test_a_broken_health_check_does_not_break_the_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    monkeypatch.setattr(main_module.health, "assess",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    alerting_config(tmp_path)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0


def test_the_baseline_comes_from_runs_before_this_one(tmp_path: Path, monkeypatch):
    """`assess` must not compare the run against itself — reading the history
    after inserting this run's row would make every source look normal."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch",
                        lambda config, **kw: fresh_jobs(40))
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))
    alerting_config(tmp_path)
    argv = ["--no-browser", "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]

    main(argv)                                  # establishes a baseline
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    main(argv)                                  # the board went silent
    assert (tmp_path / "output" / ALERT_FILENAME).exists()


# ==========================================================================
# a real end-to-end run through the CLI
# ==========================================================================


def test_the_cli_runs_the_whole_pipeline_against_a_fake_board(
        tmp_path: Path, monkeypatch, capsys):
    """The closest thing to `python -m src.main` this suite can do offline:
    a real Tracker on disk, real filters, a fake HTTP session behind the
    Greenhouse fetcher and a fake LLM behind scoring."""
    # Only two things are faked: the socket behind the Greenhouse fetcher, and
    # the model behind scoring. Filters, dedupe, the tracker, the digest and
    # the CLI itself are all the real implementations.
    session = FakeSession([("boards-api.greenhouse.io", json_response(GREENHOUSE))])
    real_fetch = main_module.ats_boards.fetch
    monkeypatch.setattr(
        main_module.ats_boards, "fetch",
        lambda config, **kw: real_fetch(config, session=session, errors=kw.get("errors")),
    )
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))

    write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": False},
         "output": {"dir": str(tmp_path / "output"), "open_browser": False},
         "db": {"path": str(tmp_path / "output" / "tracker.sqlite3")},
         "apply": {"enabled": False},
         "tailoring": {"enabled": False},
         "freshness": {"max_age_hours": 24, "skip_undated": True}},
        watchlist={"greenhouse": ["acme"]},
    )

    code = main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    out = capsys.readouterr().out
    assert code == 0
    assert "digest:" in out
    assert list((tmp_path / "output").glob("digest_*.html"))
    # The tracker is a real file on disk and has recorded the run.
    with Tracker(tmp_path / "output" / "tracker.sqlite3") as tracker:
        assert tracker.recent_runs()
        assert tracker.counts_by_status()


def _scored(jobs):
    """Deterministic stand-in for the scoring stage in the CLI test."""
    from src.models import Score, ScoredJob

    return [
        ScoredJob(job=job, status=ApplyStatus.DIGEST,
                  score=Score(value=90, verdict="fits", model="test"))
        for job in jobs
    ]


# ==========================================================================
# --no-llm: fetch and filter only
# ==========================================================================


def no_llm_config(tmp_path: Path, **overrides):
    """Deliberately no API key and NO CV — the point of the mode."""
    base = {
        "sources": {"greenhouse": True, "lever": False},
        "output": {"dir": str(tmp_path / "output"), "open_browser": False},
        "db": {"path": str(tmp_path / "output" / "tracker.sqlite3")},
        "keys": {"anthropic": "", "openrouter": ""},
    }
    base.update(overrides or {})
    return write_config(tmp_path, base, watchlist={"greenhouse": ["acme"]}, cv=None)


def test_no_llm_needs_neither_a_key_nor_a_cv(tmp_path: Path, stub_sources,
                                             memory_tracker):
    """The whole point: prove the sources and tune the filters before paying
    for anything, and before writing your CV."""
    stub_sources.results["ats_boards"] = fresh_jobs(3)
    scored, stats = run_pipeline(no_llm_config(tmp_path), tracker=memory_tracker,
                                 now=NOW, skip_llm=True)
    assert len(scored) == 3
    assert Path(stats.digest_path).exists()


def test_no_llm_makes_no_model_calls(tmp_path: Path, stub_sources, memory_tracker):
    class Exploding:
        def complete(self, **kw):
            raise AssertionError("--no-llm called the model")

        def complete_json(self, **kw):
            raise AssertionError("--no-llm called the model")

    stub_sources.results["ats_boards"] = fresh_jobs(2)
    run_pipeline(no_llm_config(tmp_path), tracker=memory_tracker, now=NOW,
                 skip_llm=True, llm_client=Exploding())


def test_no_llm_leaves_jobs_unscored_rather_than_scored_zero(tmp_path: Path,
                                                             stub_sources,
                                                             memory_tracker):
    """Rendering these as 0 would tell the reader "terrible fit" when the
    truth is "nobody looked" — the opposite instruction."""
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    scored, _ = run_pipeline(no_llm_config(tmp_path), tracker=memory_tracker,
                             now=NOW, skip_llm=True)
    assert scored[0].score is None
    assert scored[0].status is ApplyStatus.DIGEST
    assert "--no-llm" in scored[0].status_detail


def test_the_digest_shows_an_unscored_job_as_a_dash(tmp_path: Path, stub_sources,
                                                    memory_tracker):
    from src.digest import build_context, render_html

    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = no_llm_config(tmp_path)
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW, skip_llm=True)

    item = build_context(scored, stats, cfg, now=NOW)["pending"][0]
    assert item["unscored"] is True
    assert item["score_label"] == "—"
    assert item["score_class"] == "score-unscored"
    assert "score-unscored" in render_html(build_context(scored, stats, cfg, now=NOW))


def test_no_llm_reports_an_honest_funnel(tmp_path: Path, stub_sources,
                                         memory_tracker):
    """`scored` must read 0: nothing was scored, however many jobs the digest
    ends up showing."""
    stub_sources.results["ats_boards"] = fresh_jobs(4)
    _, stats = run_pipeline(no_llm_config(tmp_path), tracker=memory_tracker,
                            now=NOW, skip_llm=True)
    assert stats.after_filters == 4
    assert stats.scored == 0
    assert stats.llm_skipped is True


def test_no_llm_still_applies_the_hard_filters(tmp_path: Path, stub_sources,
                                               memory_tracker):
    """Otherwise it would not be a filter-tuning tool, which is its job."""
    stub_sources.results["ats_boards"] = fresh_jobs(2) + [
        make_job(company="US", location="San Francisco, CA", hours_old=1,
                 ats_job_id="u"),
        make_job(company="Stale", location="Berlin, Germany", hours_old=99,
                 ats_job_id="s"),
    ]
    scored, stats = run_pipeline(no_llm_config(tmp_path), tracker=memory_tracker,
                                 now=NOW, skip_llm=True)
    assert len(scored) == 2
    assert stats.filter_counts == {"stale": 1, "location_outside_eu": 1}


def test_allegro_source_respects_selection_and_reports_partial_results(tmp_path, monkeypatch):
    calls = []
    job = make_job(source="allegro", company="Allegro", location="Warsaw, Poland",
                   ats="allegro", ats_job_id="sap:123", hours_old=1)

    def fetch(config, *, errors):
        calls.append("allegro")
        errors.append("allegro: detail cap reached; partial descriptions")
        return [job]

    monkeypatch.setattr(main_module.allegro, "fetch", fetch)
    cfg = no_llm_config(tmp_path, sources={"greenhouse": False, "lever": False,
                                         "allegro": True})
    scored, stats = run_pipeline(cfg, now=NOW, sources=["allegro"], skip_llm=True,
                                skip_apply=True)
    assert calls == ["allegro"]
    assert [item.key for item in scored] == [job.key]
    assert stats.source_counts == {"allegro": 1}
    assert stats.scored == stats.auto_applied == 0
    assert any("partial descriptions" in error for error in stats.errors)
    calls.clear()
    run_pipeline(cfg, now=NOW, sources=["greenhouse"], skip_llm=True, skip_apply=True)
    assert calls == []


def test_no_llm_never_applies(tmp_path: Path, stub_sources, memory_tracker):
    """There is no score and no PDF, so there is nothing to submit."""
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = no_llm_config(tmp_path, apply={"enabled": True, "dry_run": True})
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW, skip_llm=True)
    assert all(s.status is ApplyStatus.DIGEST for s in scored)
    assert stats.auto_applied == 0 and stats.dry_run == 0


def test_the_cli_accepts_no_llm_without_a_key_or_a_cv(tmp_path: Path, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(main_module.ats_boards, "fetch",
                        lambda config, **kw: fresh_jobs(2))
    no_llm_config(tmp_path)
    argv = ["--no-llm", "--no-browser", "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]

    assert main(argv + ["--validate-only"]) == 0
    assert main(argv) == 0
    assert "digest:" in capsys.readouterr().out


def test_without_no_llm_the_key_and_cv_are_still_required(tmp_path: Path, capsys):
    """The mode relaxes validation; it must not relax it for everyone else."""
    no_llm_config(tmp_path)
    code = main(["--validate-only", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 1
    err = capsys.readouterr().err
    assert "API key" in err
    assert "CV not found" in err


def test_summary_separates_run_failures_from_backlog_totals():
    from src.models import RunStats

    summary = format_summary(RunStats(
        fetched=5185, after_dedupe=4592, after_filters=3,
        scoring_attempted=3, scoring_completed=1, scoring_failed=2,
        scoring_pending=7, scoring_blocked=1, matches=0,
    ))
    lines = summary.splitlines()
    run = next(line for line in lines if line.startswith("Evaluation outcomes this run:"))
    backlog = next(line for line in lines if line.startswith("Backlog totals across runs:"))
    for value in ("attempted 3", "completed 1", "failed 2", "matched 0"):
        assert value in run
    assert "pending" not in run and "paused" not in run
    assert "pending 7" in backlog and "paused at retry/age limits 1" in backlog
    assert "new 3" not in summary
