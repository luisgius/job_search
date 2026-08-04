"""Tests for src/models.py — normalisation and job identity.

Identity is the load-bearing part: `Job.key` is the primary key of the
tracker, so if it is unstable the "never apply twice" guarantee is a lie, and
if it collides two different jobs share one application record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import (
    ApplyStatus,
    Artifacts,
    Job,
    RunStats,
    Score,
    ScoredJob,
    ensure_utc,
    normalize_company,
    normalize_text,
    normalize_title,
    utcnow,
)
from tests.conftest import NOW, make_job


# ==========================================================================
# normalisation
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Berlin, Germany", "berlin germany"),
        ("  Senior   Engineer  ", "senior engineer"),
        ("Zürich", "zurich"),
        ("Kraków", "krakow"),
        ("Málaga", "malaga"),
        ("København", "kobenhavn"),
        ("C++ Developer", "c developer"),
        ("Data/ML Engineer", "data ml engineer"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Spotify AB", "spotify"),
        ("Spotify", "spotify"),
        ("Zalando SE", "zalando"),
        ("Northwind GmbH", "northwind"),
        ("Initech, Inc.", "initech"),
        ("Adyen N.V.", "adyen"),
        ("Booking.com B.V.", "booking com"),
        ("Acme Ltd", "acme"),
    ],
)
def test_normalize_company_strips_legal_suffixes(raw, expected):
    assert normalize_company(raw) == expected


def test_normalize_company_never_empties_a_name():
    # A company literally named out of suffix words must survive.
    assert normalize_company("The Group") != ""
    assert normalize_company("Holding") != ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        # decoration -> dropped
        ("Backend Engineer (m/f/d)", "backend engineer"),
        ("Backend Engineer (w/m/d)", "backend engineer"),
        ("Senior Engineer (Remote, EU)", "senior engineer"),
        ("Engineer (Full-time)", "engineer"),
        ("Engineer (80%)", "engineer"),
        ("Backend Engineer", "backend engineer"),
        # identity -> kept
        ("Software Engineer (Payments)", "software engineer payments"),
        ("Data Scientist [Berlin]", "data scientist berlin"),
    ],
)
def test_normalize_title_drops_only_decorative_parentheticals(raw, expected):
    """"(m/f/d)" is noise on one posting; "(Payments)" and "(Machine Learning)"
    are two different requisitions. Stripping both alike made `dedupe` delete
    one of the two before it was ever scored."""
    assert normalize_title(raw) == expected


def test_two_teams_at_one_company_are_two_jobs():
    a = make_job(title="Software Engineer (Payments)", ats=None, ats_job_id=None)
    b = make_job(title="Software Engineer (Machine Learning)", ats=None,
                 ats_job_id=None)
    assert a.key != b.key
    assert a.dedupe_key != b.dedupe_key


def test_the_ats_id_alone_carries_identity():
    """The display name is derived from the board slug or overridden in the
    watchlist, so mixing it into the key meant the documented `company:`
    override re-keyed every requisition on that board — and re-keying is how
    an already-applied job becomes eligible again."""
    a = make_job(company="Spotify", ats="greenhouse", ats_job_id="4012345")
    b = make_job(company="Spotify Technology S.A.", ats="greenhouse",
                 ats_job_id="4012345")
    assert a.key == b.key


def test_the_same_title_in_two_cities_is_two_jobs():
    a = make_job(title="Software Engineer", location="Berlin, Germany",
                 country="DE", ats=None, ats_job_id=None)
    b = make_job(title="Software Engineer", location="Munich, Germany",
                 country="DE", ats=None, ats_job_id=None)
    assert a.dedupe_key != b.dedupe_key


def test_city_verbosity_still_collapses():
    a = make_job(location="Berlin, Germany", country="DE", ats=None, ats_job_id=None)
    b = make_job(location="Berlin", country="DE", ats=None, ats_job_id=None)
    c = make_job(location="Berlin (Remote)", country="DE", ats=None, ats_job_id=None)
    assert a.dedupe_key == b.dedupe_key == c.dedupe_key


# ==========================================================================
# Job identity
# ==========================================================================


def test_key_is_stable_across_constructions():
    a = make_job()
    b = make_job()
    assert a.key == b.key


def test_key_uses_ats_id_when_present():
    a = make_job(ats="greenhouse", ats_job_id="1", title="Backend Engineer")
    b = make_job(ats="greenhouse", ats_job_id="1", title="Backend Engineer II")
    # Same requisition, retitled overnight -> still the same job.
    assert a.key == b.key


def test_key_differs_across_ats_ids():
    a = make_job(ats_job_id="1")
    b = make_job(ats_job_id="2")
    assert a.key != b.key


def test_key_without_ats_falls_back_to_company_title_location():
    a = make_job(ats=None, ats_job_id=None)
    b = make_job(ats=None, ats_job_id=None, url="https://example.com/other")
    # URL is deliberately not part of the identity: tracking params change it.
    assert a.key == b.key


def test_key_survives_legal_suffix_and_case_drift():
    a = make_job(ats=None, ats_job_id=None, company="Spotify AB")
    b = make_job(ats=None, ats_job_id=None, company="spotify")
    assert a.key == b.key


def test_key_distinguishes_different_companies():
    a = make_job(ats=None, ats_job_id=None, company="Acme")
    b = make_job(ats=None, ats_job_id=None, company="Globex")
    assert a.key != b.key


def test_dedupe_key_collapses_the_same_role_across_sources():
    ats = make_job(source="greenhouse", ats="greenhouse", ats_job_id="1",
                   location="Berlin, Germany", country="DE")
    email = make_job(source="linkedin_email", ats=None, ats_job_id=None,
                     location="Berlin, Germany", country="DE",
                     url="https://www.linkedin.com/jobs/view/1")
    assert ats.key != email.key          # different provenance, different rows
    assert ats.dedupe_key == email.dedupe_key   # ... but the same actual job


def test_dedupe_key_tolerates_location_verbosity():
    a = make_job(ats=None, ats_job_id=None, location="Berlin, Germany", country="DE")
    b = make_job(ats=None, ats_job_id=None, location="Berlin", country="DE")
    assert a.dedupe_key == b.dedupe_key


def test_dedupe_key_separates_cities_when_country_unknown():
    a = make_job(ats=None, ats_job_id=None, location="Berlin, Germany", country=None)
    b = make_job(ats=None, ats_job_id=None, location="Madrid, Spain", country=None)
    assert a.dedupe_key != b.dedupe_key


def test_keys_are_short_and_hex():
    key = make_job().key
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


# ==========================================================================
# Job normalisation on construction
# ==========================================================================


def test_post_init_strips_and_normalises():
    job = Job(source="greenhouse", company="  Acme  ", title=" Engineer\n",
              url=" https://x/1 ", location=" Berlin ", country="de")
    assert job.company == "Acme"
    assert job.title == "Engineer"
    assert job.url == "https://x/1"
    assert job.location == "Berlin"
    assert job.country == "DE"


def test_naive_posted_at_is_coerced_to_utc():
    job = Job(source="x", company="c", title="t", url="u",
              posted_at=datetime(2026, 8, 4, 9, 0, 0))
    assert job.posted_at.tzinfo is not None
    assert job.posted_at == datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def test_aware_posted_at_is_converted_not_relabelled():
    berlin = timezone(timedelta(hours=2))
    job = Job(source="x", company="c", title="t", url="u",
              posted_at=datetime(2026, 8, 4, 11, 0, tzinfo=berlin))
    assert job.posted_at == datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def test_age_hours_at_uses_the_injected_clock():
    job = make_job(hours_old=6)
    assert job.age_hours_at(NOW) == pytest.approx(6.0, abs=0.01)


def test_age_hours_at_is_none_for_undated_jobs():
    assert make_job(hours_old=None).age_hours_at(NOW) is None


def test_label_and_to_dict():
    job = make_job(company="Acme", title="Backend Engineer")
    assert job.label == "Acme — Backend Engineer"
    payload = job.to_dict()
    assert payload["key"] == job.key
    assert payload["posted_at"].endswith("+00:00")
    # to_dict must be JSON-serialisable: it is written to job.json per artifact.
    import json

    json.loads(json.dumps(payload))


# ==========================================================================
# helpers
# ==========================================================================


def test_utcnow_is_aware():
    assert utcnow().tzinfo is not None


def test_ensure_utc_passthrough_and_none():
    assert ensure_utc(None) is None
    assert ensure_utc(NOW) == NOW


# ==========================================================================
# Score / ScoredJob / RunStats
# ==========================================================================


def test_score_ok_reflects_error():
    assert Score(value=80).ok is True
    assert Score(value=0, error="boom").ok is False


def test_scored_job_defaults():
    scored = ScoredJob(job=make_job())
    assert scored.score_value == 0
    assert scored.status is ApplyStatus.NEW
    assert isinstance(scored.artifacts, Artifacts)
    assert scored.key == scored.job.key


def test_apply_status_values_are_the_persisted_strings():
    # These strings live in the SQLite file; changing one is a migration.
    assert ApplyStatus.APPLIED.value == "applied"
    assert ApplyStatus.DRY_RUN.value == "dry_run"
    assert ApplyStatus.DIGEST.value == "digest"
    assert ApplyStatus.APPLY_FAILED.value == "apply_failed"
    assert ApplyStatus.SCORED_BELOW.value == "scored_below"


def test_run_stats_to_dict_round_trips():
    stats = RunStats(fetched=10, matches=2)
    stats.errors.append("boom")
    stats.source_counts["greenhouse"] = 10
    payload = stats.to_dict()
    assert payload["fetched"] == 10
    assert payload["errors"] == ["boom"]
    assert payload["source_counts"] == {"greenhouse": 10}
    # Mutating the dict must not reach back into the stats object.
    payload["errors"].append("other")
    assert stats.errors == ["boom"]
