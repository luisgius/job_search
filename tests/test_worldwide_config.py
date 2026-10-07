"""The shipped configuration admits worldwide jobs through existing ATS parsing."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.config import Config
from src.filters import apply_filters
from src.scoring import _candidate_rules, build_prompt
from src.sources.ats_boards import _parse_lever_posting

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)


def configured():
    return Config.load(ROOT / 'config.yaml', ROOT / 'watchlist.yaml', root=ROOT, env={})


def posting(location, days_old=1):
    return _parse_lever_posting({
        'id': 'worldwide-regression', 'text': 'Data Scientist',
        'hostedUrl': 'https://jobs.lever.co/example/worldwide-regression',
        'categories': {'location': location}, 'workplaceType': 'remote',
        'createdAt': int((NOW - timedelta(days=days_old)).timestamp() * 1000),
        'descriptionPlain': 'Build Python and SQL models for product analytics, '
                            'experimentation and forecasting using machine learning. '
                            'Collaborate with the team to evaluate experiments and '
                            'communicate findings. Working language is English.',
    }, 'example', 'Example')


@pytest.mark.parametrize('location, expected', [
    ('Remote - Worldwide', True), ('Remote - Anywhere', True),
    ('Krakow, Poland', True), ('Remote - US only', False), ('Remote', False),
])
def test_configured_search_accepts_worldwide_without_disabling_europe(location, expected):
    job = posting(location)
    assert job is not None
    result = apply_filters([job], configured(), now=NOW)
    assert bool(result.kept) is expected, result.rejected


def test_worldwide_does_not_bypass_freshness():
    result = apply_filters([posting('Remote - Worldwide', days_old=10)], configured(), now=NOW)
    assert not result.kept
    assert any('old' in reason or 'stale' in reason for _, reason in result.rejected)


def test_worldwide_preference_reaches_prompt_without_inventing_authorization():
    conf = configured()
    assert conf.get('filters.allow_remote_worldwide') is True
    prompt = build_prompt(posting('Remote - Worldwide'), 'Candidate CV', {}, rules=_candidate_rules(conf))
    assert 'worldwide' in prompt
    assert 'not proof of' in prompt
    assert 'unstated eligibility requirements unknown' in prompt
    assert Config().get('filters.allow_remote_worldwide') is False
