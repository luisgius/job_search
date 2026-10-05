"""Confirmed October failures and CV-backed boundary cases; no live services."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src import filters, main
from src.config import Config
from src.db import Tracker
from src.digest import build_context, render_html
from src.llm import LLMClient, LLMError, ModelChain, UsageMeter, chain_from_config
from src.models import ApplyStatus, Score, ScoredJob, RunStats
from src.scoring import RESPONSE_KEYS, RESPONSE_SCHEMA, score_job
from src.sources import ats_boards, justjoin_it, landing_jobs
from tests.conftest import make_job, NOW, write_config

GOOD = {"score": 82, "verdict": "Compatible", "reasons": ["SQL and experiments"],
        "strengths": ["Causal inference"], "gaps": ["Salary unknown"]}


class Result:
    def __init__(self, value):
        self.value = value
        self.calls = 0
    def complete_json(self, **kwargs):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return deepcopy(self.value)


@pytest.mark.parametrize("invalid", [{"score": 99}, {**GOOD, "score": True},
                                     {**GOOD, "score": float('nan')},
                                     {**GOOD, "score": 1000}, {**GOOD, "score": -5},
                                     {**GOOD, "score": "not a number"},
                                     {**GOOD, "reasons": "invented string"}])
def test_invalid_score_objects_advance_to_fallback(invalid):
    bad, good = Result(invalid), Result(GOOD)
    chain = ModelChain([(lambda: bad, "bad"), (lambda: good, "good")])
    score = score_job(make_job(), "CV", {"scoring": {"model": "bad"}}, client=chain)
    assert score.ok and score.model == "good" and score.value == 82
    assert bad.calls == good.calls == 1


def test_chain_retains_all_failure_causes_and_thread_local_attribution():
    bad = Result(LLMError("HTTP 429")); empty = Result(LLMError("empty final response"))
    chain = ModelChain([(lambda: bad, "remote"), (lambda: empty, "local")])
    with pytest.raises(LLMError, match="remote: HTTP 429.*local: empty final response"):
        chain.complete_json()
    barrier = Barrier(2)
    def task(model):
        chain.last_model = model
        barrier.wait()
        return chain.last_model
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(task, ['one', 'two'])) == ['one', 'two']


def test_length_exhaustion_cannot_consume_reasoning_as_score(monkeypatch):
    from src import util
    calls = []
    def response(url, **kw):
        calls.append(kw)
        return {"choices": [{"finish_reason": "length", "message": {
            "content": "", "reasoning": json.dumps(GOOD)}}],
            "usage": {"completion_tokens": 1500}}
    monkeypatch.setattr(util, 'http_post_json', response)
    meter = UsageMeter()
    client = LLMClient('', base_url='http://localhost:11434/v1', meter=meter, max_retries=2)
    with pytest.raises(LLMError, match="generation limit reached.*completion_tokens=1500"):
        client.complete_json(model='qwen', system='s', prompt='p', max_tokens=1500)
    assert len(calls) == 1  # repeating the same exhausted budget cannot recover
    assert meter.snapshot()['output_tokens'] == 1500


def test_local_entry_controls_are_applied_without_changing_the_model(monkeypatch):
    from src import util
    calls = []
    def response(url, **kw):
        calls.append((url, kw['json']))
        return {"choices": [{"finish_reason": "stop", "message": {'content': json.dumps(GOOD)}}]}
    monkeypatch.setattr(util, 'http_post_json', response)
    cfg = {'llm': {'provider': 'openrouter'}, 'keys': {}, 'scoring': {'model': 'qwen3.8:27b', 'fallback_models': [
        {'model': 'qwen3.8:27b', 'base_url': 'http://localhost:11434/v1',
         'reasoning_effort': 'none', 'max_tokens': 1800, 'max_retries': 0}]}}
    chain = chain_from_config(cfg, 'scoring')
    result = chain.complete_json(model='qwen3.8:27b', system='s', prompt='p', max_tokens=1500,
                                 require_keys=RESPONSE_KEYS, schema=RESPONSE_SCHEMA)
    assert result == GOOD
    assert calls[0][0] == 'http://localhost:11434/v1/chat/completions'
    assert calls[0][1]['reasoning_effort'] == 'none'
    assert calls[0][1]['max_tokens'] == 1800


def test_transient_empty_response_retries_within_existing_limit(monkeypatch):
    from src import util
    outputs = iter(['', json.dumps(GOOD)])
    monkeypatch.setattr(util, 'http_post_json', lambda *a, **kw: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': next(outputs)}}]})
    client = LLMClient('', base_url='http://localhost:11434/v1', max_retries=1, sleep=lambda _: None)
    assert client.complete_json(model='qwen', system='', prompt='', max_tokens=1500,
                                schema=RESPONSE_SCHEMA, require_keys=RESPONSE_KEYS) == GOOD


def profile_config():
    return Config.load(Path(__file__).resolve().parents[1] / 'config.yaml', env={})


@pytest.mark.parametrize('title,description,keep', [
    ('Product Analyst', 'Use SQL and Python to design A/B tests and measure product retention. 2 years experience.', True),
    ('Experimentation Scientist', 'Statistical experimental design and causal inference with Python.', True),
    ('Decision Science Analyst', 'SQL and causal inference for marketplace decisions.', True),
    ('Causal Inference Scientist', 'Python and causal inference, experiment analysis.', True),
    ('Decision Scientist', 'Python and marketplace forecasting.', True),
    ('Senior Product Analyst', 'SQL and A/B testing.', False),
    ('Product Analyst', 'SQL and A/B tests; requires 5+ years experience.', False),
    ('Product Analyst', 'SQL and A/B tests; 2–5 years experience.', True),
    ('Product Analyst', 'SQL and A/B tests; 5 years experience preferred.', True),
    ('Product Analyst', 'Maintain product catalogue and supplier inventory records.', False),
    ('Marketing Manager', 'SQL and experimentation.', False),
    ('Data Analyst', 'Reporting and spreadsheet maintenance.', False),
])
def test_cv_backed_adjacent_roles_require_functions_and_compatible_level(title, description, keep):
    job = make_job(title=title, description=description)
    assert bool(filters.apply_filters([job], profile_config(), now=NOW).kept) == keep


@pytest.mark.parametrize('description,keep,status', [
    ('Wir entwickeln Prognosemodelle mit Python und SQL. Die Arbeit ist international.', True, 'unknown'),
    ('English is required. German is a plus.', True, 'explicit'),
    ('Fluent in German and English required.', False, 'explicit'),
    ('German C1 required.', False, 'explicit'),
    ('Sehr gute Deutschkenntnisse sind erforderlich.', False, 'explicit'),
    ('Fluent Polish required.', False, 'explicit'),
    ('No German required. English is our working language.', True, 'unknown'),
    ('German is not required.', True, 'unknown'),
    ('German or English fluency required.', True, 'unknown'),
    ('German required; level not specified.', True, 'explicit'),
    ('Professional French required.', True, 'explicit'),  # candidate French level unknown
    ('Fluent Spanish required.', True, 'explicit'),
    ('Basic German required.', True, 'explicit'),
])
def test_advertisement_language_is_not_work_language(description, keep, status):
    job = make_job(title='Data Scientist', description=description)
    result = filters.apply_filters([job], profile_config(), now=NOW)
    assert bool(result.kept) == keep
    assert job.raw['work_language_status'] == status


def test_adjacent_source_prefilters_do_not_hide_product_experimentation():
    for title in ['Product Analyst', 'Experimentation Scientist', 'Causal Inference Scientist']:
        assert landing_jobs.DS_TITLE_RE.search(title)
        assert justjoin_it.DS_RE.search(title)


def test_updates_and_first_detection_never_replace_publication(tmp_path):
    job = ats_boards._parse_greenhouse_posting({'id': 1, 'title': 'Data Scientist',
        'updated_at': NOW.isoformat(), 'location': {'name': 'Berlin'}}, 'acme', 'Acme')
    assert job.posted_at is None
    assert filters.apply_filters([job], profile_config(), now=NOW).counts == {'undated': 1}
    with Tracker() as tracker:
        tracker.record_job(job, now=NOW)
        item = ScoredJob(job, Score(80), status=ApplyStatus.DIGEST)
        ctx = build_context([item], RunStats(), profile_config(), now=NOW, tracker=tracker)
        card = ctx['needs_click'][0]
        assert card['posted_label'] == 'no posting date'
        assert card['source_updated_at'] == NOW.isoformat()
        assert card['first_seen_at'] != 'unknown'


def test_all_failed_run_is_pending_recoverable_and_never_a_match(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, {'sources': {'greenhouse': True},
                       'filters': {'title_include': [], 'title_exclude': []},
                       'tailoring': {'enabled': True}, 'apply': {'enabled': True, 'min_score': 0},
                       'scoring': {'threshold': 0, 'max_jobs': 2}})
    job = make_job(description='Complete retained job evidence')
    monkeypatch.setattr(main, '_fetch_all', lambda *a: [deepcopy(job)])
    monkeypatch.setattr(main, '_read_cv', lambda _: 'CV')
    monkeypatch.setattr(main.autoapply, 'run', lambda items, *a, **kw: items)
    with Tracker(tmp_path/'retry.sqlite3') as tracker:
        items, stats = main.run_pipeline(cfg, tracker=tracker, now=NOW,
                                         llm_client=Result(LLMError('all failed')), skip_apply=True)
        assert (stats.scoring_attempted, stats.scoring_completed, stats.scoring_failed) == (1, 0, 1)
        assert stats.matches == stats.digest_items == stats.tailored == stats.auto_applied == 0
        assert stats.scoring_pending == 1
        assert tracker.get_application(job.key) is None
        assert 'Complete retained job evidence' in tracker.get_scoring(job.key)['job_json']
        html = Path(stats.digest_path).read_text()
        assert 'Evaluation failed — no opportunities could be selected' in html
        assert 'Pending evaluation — not validated matches' in html
        assert build_context(items, stats, cfg, now=NOW)['needs_click'] == []
        cfg.data['tailoring']['enabled'] = False
        items, stats = main.run_pipeline(cfg, tracker=tracker, now=NOW+timedelta(hours=2),
                                         llm_client=Result(GOOD), skip_apply=True)
        assert stats.scoring_completed == stats.matches == 1
        assert tracker.get_scoring(job.key) is None


def test_legacy_error_card_and_fallback_html_preserve_uncertain_submissions(tmp_path):
    from src.digest import _fallback_html
    error = ScoredJob(make_job(), Score(0, error='failed'), status=ApplyStatus.DIGEST)
    uncertain = ScoredJob(make_job(ats_job_id='uncertain'), Score(85), status=ApplyStatus.SUBMITTED_UNCONFIRMED)
    ctx = build_context([error, uncertain], RunStats(scoring_attempted=1), profile_config(), now=NOW)
    assert len(ctx['pending']) == len(ctx['unconfirmed']) == 1 and not ctx['needs_click']
    fallback = _fallback_html(ctx, RuntimeError('template failed'))
    assert 'pending (1)' in fallback and 'unconfirmed (1)' in fallback


@pytest.mark.parametrize("status", [ApplyStatus.SCORING_PENDING, ApplyStatus.SUBMITTED_UNCONFIRMED])
def test_autoapply_preserves_pending_and_uncertain_statuses(status):
    from src.apply import autoapply
    item = ScoredJob(make_job(), Score(0, error="provider down"), status=status,
                     status_detail="Retained retry or uncertain confirmation evidence")
    class ForbiddenBrowser:
        def new_page(self):
            pytest.fail("pending/uncertain job must never open an application")
    autoapply.run([item], {"apply": {"enabled": True, "min_score": 0}}, browser=ForbiddenBrowser())
    assert item.status is status
    assert item.status_detail == "Retained retry or uncertain confirmation evidence"


def test_mixed_run_counts_valid_below_threshold_separately_from_failures(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, {'sources': {'greenhouse': True},
        'filters': {'title_include': [], 'title_exclude': []},
        'tailoring': {'enabled': False}, 'apply': {'enabled': False},
        'scoring': {'threshold': 65, 'concurrency': 1}})
    jobs = [make_job(ats_job_id=str(i), company=f'Example {i}') for i in range(3)]
    monkeypatch.setattr(main, '_fetch_all', lambda *a: jobs)
    monkeypatch.setattr(main, '_read_cv', lambda _: 'CV')
    class Mixed:
        values = iter([{**GOOD, 'score': 10}, LLMError('empty final'), LLMError('invalid JSON')])
        def complete_json(self, **kwargs):
            value = next(self.values)
            if isinstance(value, Exception):
                raise value
            return value
    with Tracker(tmp_path/'mixed.sqlite3') as tracker:
        items, stats = main.run_pipeline(cfg, tracker=tracker, now=NOW, llm_client=Mixed(), skip_apply=True)
        assert (stats.scoring_attempted, stats.scoring_completed, stats.scoring_failed) == (3, 1, 2)
        assert stats.scored == 1 and stats.scoring_pending == 2
        assert stats.matches == stats.digest_items == stats.auto_applied == stats.tailored == 0
        ctx = build_context(items, stats, cfg, now=NOW)
        assert not ctx['evaluation_failed']  # one completed, below threshold
        assert not ctx['needs_click'] and len(ctx['pending']) == 2
