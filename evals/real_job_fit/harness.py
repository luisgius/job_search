"""Bounded real score_job evaluation, offline by default; no pipeline writes."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from src import scoring
from src.filters import passes_title
from src.llm import LLMClient, LLMError
from src.models import Job

ROOT = Path(__file__).resolve().parents[2]
DATASET = Path(__file__).parent / 'dataset' / 'cohort.json'


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


SOURCE_FILES = ('src/scoring.py', 'src/llm.py', 'src/filters.py', 'evals/real_job_fit/harness.py')


def code_fingerprints():
    return {p: digest((ROOT / p).read_bytes()) for p in SOURCE_FILES}


# Capture disk sources when this module is imported, not only after a long run.
# This is source-file provenance, not a claim about monkeypatched function code.
IMPORTED_CODE_SHA256 = code_fingerprints()


def load_dataset(path=DATASET):
    data = json.loads(Path(path).read_text())
    rows = data['jobs']
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('duplicate case id')
    for field in ('employer_group', 'dedup_group', 'time_group'):
        seen = {}
        for row in rows:
            if row['split'] not in ('tuning', 'heldout'):
                raise ValueError('invalid split')
            prior = seen.setdefault(row[field], row['split'])
            if prior != row['split']:
                raise ValueError(f'{field} crosses splits')
    for row in rows:
        if row['proposed_label']['decision'] not in ('pursue', 'decline', 'unknown'):
            raise ValueError('invalid proposed label')
        label = row.get('human_label')
        if label and (label.get('decision') not in ('pursue', 'decline', 'unknown')
                      or not label.get('reviewer') or not label.get('reviewed_at')
                      or not label.get('evidence')):
            raise ValueError('human labels require reviewer, date and actual review evidence')
    return data


def make_job(row, full_inputs=None):
    payload = copy.deepcopy(row['job'])
    context = 'selected_excerpt'
    if full_inputs is not None:
        item = full_inputs[row['id']]
        payload = copy.deepcopy(item['job'])
        if digest(payload['description'].encode()) != row['provenance']['description_sha256']:
            raise ValueError(f"description hash mismatch: {row['id']}")
        if digest(payload) != row['provenance']['job_payload_sha256']:
            raise ValueError(f"job payload hash mismatch: {row['id']}")
        context = row['provenance']['original_description_completeness']
    if payload.get('posted_at'):
        payload['posted_at'] = datetime.fromisoformat(payload['posted_at'])
    return Job(**payload), context


class OfflineClient(LLMClient):
    """Only generation is fake; score_job, prompt, parser and schema are real."""
    def __init__(self, reply=None, error=False):
        self.provider = 'openrouter'
        self.last_model = 'offline-contract'
        self.reply = reply if reply is not None else json.dumps({
            'score': 50, 'reasons': ['Contract fixture, not a fit judgment'],
            'strengths': [], 'gaps': [], 'verdict': 'Contract fixture'})
        self.error = error
        self.calls = 0
        self.request = None

    def complete(self, **kwargs):
        self.calls += 1
        self.request = kwargs
        if self.error:
            raise LLMError('scripted provider failure')
        return self.reply


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_endpoint(endpoint):
    p = urlsplit(endpoint)
    if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', '::1', 'localhost')
            or p.username or p.password or p.query or p.fragment
            or p.path.rstrip('/') != '/v1' or not p.port):
        raise ValueError('endpoint must be explicit loopback http://127.0.0.1:PORT/v1')
    # Resolve localhost to a literal; never trust DNS or proxy environment.
    host = '[::1]' if p.hostname == '::1' else '127.0.0.1'
    return f'http://{host}:{p.port}/v1'


@contextmanager
def deadline(seconds):
    """Wall-clock deadline, including response-body reads, on CLI main thread."""
    def expired(signum, frame):
        raise TimeoutError('evaluation request deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class LocalClient(LLMClient):
    """Thin, single-attempt HTTP adapter; inherit the production JSON guards."""
    def __init__(self, *, endpoint, model, max_requests, max_tokens, timeout,
                 run_seconds=600, max_input_bytes=64000):
        self.endpoint = validate_endpoint(endpoint)
        if not model or not 1 <= max_requests <= 16 or not 1 <= max_tokens <= 4096:
            raise ValueError('model and bounded requests/tokens required')
        if not 0 < timeout <= 600 or not 0 < run_seconds <= 3600:
            raise ValueError('timeout/deadline outside bounds')
        if not 1 <= max_input_bytes <= 262144:
            raise ValueError('input byte limit outside bounds')
        self.provider = 'openrouter'
        self.last_model = model
        self.model = model
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.expires = time.monotonic() + run_seconds
        self.max_input_bytes = max_input_bytes
        self.calls = 0
        self.telemetry = []
        self.request = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def complete(self, **kwargs):
        self.request = kwargs
        remaining = self.expires - time.monotonic()
        if self.calls >= self.max_requests or remaining <= 0:
            raise LLMError('evaluation budget exhausted')
        if kwargs['model'] != self.model or kwargs['max_tokens'] > self.max_tokens:
            raise LLMError('evaluation model/token limit mismatch')
        if len((kwargs['system'] + kwargs['prompt']).encode()) > self.max_input_bytes:
            raise LLMError('evaluation input byte budget exceeded')
        body = {'model': self.model, 'messages': [
            {'role': 'system', 'content': kwargs['system']},
            {'role': 'user', 'content': kwargs['prompt']}],
            'max_tokens': self.max_tokens, 'temperature': kwargs['temperature'],
            'reasoning_effort': 'none', 'stream': False}
        request = urllib.request.Request(self.endpoint + '/chat/completions',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        self.calls += 1  # includes failures; no retries, redirects, auth or remote fallback
        record = {'request': self.calls, 'input_tokens': None, 'output_tokens': None,
                  'cost': None, 'error': None, 'served_model': None}
        started = time.monotonic()
        try:
            with deadline(min(self.timeout, remaining)):
                with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError('response size limit')
                    payload = json.loads(raw)
            served = payload.get('model')
            record['served_model'] = served if isinstance(served, str) and len(served) <= 256 else None
            usage = payload.get('usage') or {}
            for target, source in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
                value = usage.get(source)
                record[target] = value if isinstance(value, int) and value >= 0 else None
            choice = payload['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ValueError('generation token limit')
            content = choice['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError('missing final text')
            return content
        except Exception as exc:
            record['error'] = type(exc).__name__
            # Do not put server replies, CV text, prompts or contacts in reports/logs.
            raise LLMError(f'local transport/response failure: {type(exc).__name__}') from None
        finally:
            record['latency_seconds'] = time.monotonic() - started
            self.telemetry.append(record)


def metrics(rows, label_field, threshold):
    labeled = [r for r in rows if r[label_field] in ('pursue', 'decline')]
    successful = [r for r in labeled if not r['error']]
    ranked = sorted(successful, key=lambda r: (-r['score'], r['id']))
    k = min(10, len(ranked))
    positives = [r for r in successful if r[label_field] == 'pursue']
    negatives = [r for r in successful if r[label_field] == 'decline']
    selected = [r for r in successful if r['score'] >= threshold]
    true_positives = [r for r in selected if r[label_field] == 'pursue']
    pairs = [(a, b) for a in positives for b in negatives]
    wins = sum(a['score'] > b['score'] for a, b in pairs)
    ties = sum(a['score'] == b['score'] for a, b in pairs)
    return {'labeled_n': len(labeled), 'assessed_labeled_n': len(successful),
            'unknown_or_unreviewed_n': len(rows) - len(labeled),
            'unassessed_error_n': sum(bool(r['error']) for r in labeled),
            'precision_at_k': sum(r[label_field] == 'pursue' for r in ranked[:k]) / k if k else None,
            'k': k, 'precision_denominator': 'successfully scored known labels only; unknowns/errors excluded',
            'threshold': threshold,
            'selected_known_n': len(selected), 'assessed_positive_n': len(positives),
            'true_positive_n': len(true_positives),
            'selected_precision': len(true_positives) / len(selected) if selected else None,
            'selected_precision_denominator': 'successfully scored known labels at or above threshold',
            'recall': len(true_positives) / len(positives) if positives else None,
            'recall_denominator': 'all successfully scored known pursue labels; unknowns/errors excluded',
            'precision_at_k_limitation': 'When n <= 10 this includes every assessed known label and equals cohort prevalence, not ranking discrimination.',
            'false_negative_ids': [r['id'] for r in positives if r['score'] < threshold],
            'unassessed_positive_ids': [r['id'] for r in labeled if r[label_field] == 'pursue' and r['error']],
            'pairwise_accuracy': (wins + 0.5 * ties) / len(pairs) if pairs else None,
            'pairwise_n': len(pairs), 'pairwise_ties': ties}


def run(*, mode='offline', dataset_path=DATASET, config_path=ROOT/'config.yaml',
        full_inputs_path=None, split='all', case_ids=None, endpoint=None, model=None,
        max_requests=12, max_tokens=1500, timeout=120, run_seconds=600,
        max_input_bytes=64000, stop_after_errors=2, checkpoint_path=None):
    code_start = code_fingerprints()
    if mode not in ('offline', 'dry-run', 'live'):
        raise ValueError('invalid mode')
    if not 1 <= max_requests <= 16 or not 1 <= stop_after_errors <= 16:
        raise ValueError('request/stop limit outside bounds')
    data = load_dataset(dataset_path)
    selected = [r for r in data['jobs'] if split == 'all' or r['split'] == split]
    if case_ids:
        if set(case_ids) - {r['id'] for r in selected}:
            raise ValueError('case id absent from selected split')
        selected = [r for r in selected if r['id'] in case_ids]
    selected = selected[:max_requests]
    config = yaml.safe_load(Path(config_path).read_text())
    cv_path = ROOT / config['cv']['path']
    cv = cv_path.read_text()
    full_inputs = None
    if full_inputs_path:
        full_inputs = {r['id']: r for r in json.loads(Path(full_inputs_path).read_text())}
    config = copy.deepcopy(config)
    config['scoring'].update(model=model or 'offline-contract', fallback_models=[],
                             max_tokens=max_tokens, concurrency=1)
    config.setdefault('llm', {})['structured_output'] = 'off'
    threshold = config['scoring']['threshold']
    client = None
    if mode == 'live':
        if not endpoint or not model:
            raise ValueError('live requires explicit local endpoint and model')
        client = LocalClient(endpoint=endpoint, model=model, max_requests=max_requests,
                             max_tokens=max_tokens, timeout=timeout, run_seconds=run_seconds,
                             max_input_bytes=max_input_bytes)
    def checkpoint(record):
        if checkpoint_path:
            with Path(checkpoint_path).open('a') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
    checkpoint({'event': 'start', 'mode': mode, 'dataset_sha256': digest(Path(dataset_path).read_bytes()),
                'selected_ids': [c['id'] for c in selected], 'cv_sha256': digest(cv.encode()),
                'config_sha256': digest(Path(config_path).read_bytes()),
                'model': model, 'endpoint': client.endpoint if client else None,
                'max_requests': max_requests, 'max_tokens': max_tokens,
                'timeout': timeout, 'run_seconds': run_seconds,
                'code_sha256_import': IMPORTED_CODE_SHA256, 'code_sha256_start': code_start})
    rows = []
    streak = 0
    stop_reason = None
    started = time.monotonic()
    for case in selected:
        if mode == 'live' and (time.monotonic() >= client.expires or streak >= stop_after_errors):
            stop_reason = 'run_deadline' if time.monotonic() >= client.expires else 'consecutive_errors'
            break
        job, context = make_job(case, full_inputs)
        active = client or OfflineClient()
        telemetry_before = len(client.telemetry) if client else 0
        calls_before = client.calls if client else 0
        active.request = None
        before = time.monotonic()
        if mode == 'dry-run':
            prompt = scoring.build_prompt(job, cv, scoring._applicant(config), rules=scoring._candidate_rules(config))
            score = None
        else:
            score = scoring.score_job(job, cv, config, client=active)
            prompt = active.request['prompt'] if active.request else ''
        case_telemetry = client.telemetry[telemetry_before:] if client else []
        request_telemetry = case_telemetry[-1] if case_telemetry else None
        case_http_requests = client.calls - calls_before if client else 0
        error = bool(score.error) if score is not None else False
        streak = streak + 1 if error else 0
        value = score.value if score is not None and not error else None
        expected_cap = case['rules']['expected_cap']
        title_pass, title_reason = passes_title(job, config)
        rows.append({'id': case['id'], 'split': case['split'], 'context': context,
                     'score': value, 'error': error,
                     'error_sha256': digest(score.error) if error else None,
                     'effective_model': (request_telemetry['served_model'] if request_telemetry else None) if mode == 'live' else (score.model if score else None),
                     'http_requests': case_http_requests,
                     'score_model_attribution': score.model if score else None,
                     'latency_seconds': time.monotonic() - before,
                     'prompt_sha256': digest(prompt), 'job_input_sha256': digest(job.to_dict()),
                     'description_sha256': digest(job.description.encode()),
                     'proposed_label': case['proposed_label']['decision'],
                     'human_label': (case.get('human_label') or {}).get('decision'),
                     'title_filter_pass': title_pass, 'title_filter_reason': title_reason,
                     'cap_violation': value is not None and expected_cap is not None and value > expected_cap,
                     'exclusion_high_score_diagnostic': bool(value is not None and value >= threshold and case['rules']['exclusion_expected']),
                     'score_range_violation': value is not None and not 0 <= value <= 100})
        checkpoint({'event': 'row', 'row': rows[-1], 'http_requests': client.calls if client else 0,
                    'request_telemetry': request_telemetry})
    code_end = code_fingerprints()
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    result = {'mode': mode, 'quality_evidence': mode == 'live', 'dataset_version': data['version'],
              'dataset_sha256': digest(Path(dataset_path).read_bytes()), 'label_sha256': digest([
                  (r['id'], r['proposed_label'], r.get('human_label')) for r in data['jobs']]),
              'cv_sha256': digest(cv.encode()), 'config_sha256': digest(Path(config_path).read_bytes()),
              'candidate_rules_sha256': digest(scoring._candidate_rules(config)),
              'system_prompt_sha256': digest(scoring.SYSTEM_PROMPT),
              'code_revision': revision.stdout.strip() if revision.returncode == 0 else 'unavailable: isolated folder copy',
              'code_sha256': code_start,
              'code_sha256_import': IMPORTED_CODE_SHA256,
              'code_sha256_start': code_start, 'code_sha256_end': code_end,
              'source_changed_during_run': code_start != code_end,
              'source_changed_since_import': IMPORTED_CODE_SHA256 != code_start,
              'code_fingerprint_scope': 'On-disk source snapshots at import/start/end; runtime monkeypatches are not fingerprinted.',
              'threshold': threshold, 'provider': 'loopback-only' if mode == 'live' else 'none',
              'endpoint': client.endpoint if client else None, 'requested_model': model,
              'limits': dict(max_requests=max_requests, max_output_tokens_per_request=max_tokens,
                             max_total_output_tokens=max_requests * max_tokens, max_input_bytes=max_input_bytes,
                             timeout_seconds=timeout, run_seconds=run_seconds, stop_after_errors=stop_after_errors,
                             retries=0, remote_fallbacks=0, reasoning_effort='none'),
              'elapsed_seconds': time.monotonic() - started, 'stop_reason': stop_reason,
              'selected_n': len(selected), 'evaluated_n': len(rows),
              'unattempted_ids': [c['id'] for c in selected if c['id'] not in {r['id'] for r in rows}],
              'http_requests': client.calls if client else 0, 'usage': client.telemetry if client else [],
              'tokens': {key: {'reported_total': sum(t[key] or 0 for t in client.telemetry),
                               'unknown_requests': sum(t[key] is None for t in client.telemetry)}
                         for key in ('input_tokens', 'output_tokens')} if client else {},
              'rule_diagnostic_scope': 'Caps are prompt instructions, not deterministic enforcement. Exclusion high scores are semantic/filter tension, not proof of application eligibility. Freshness and pipeline state are out of scope.',
              'cost': None, 'cost_status': 'unknown' if client else 'no network requests',
              'error_n': sum(r['error'] for r in rows),
              'cap_violation_ids': [r['id'] for r in rows if r['cap_violation']],
              'exclusion_high_score_ids': [r['id'] for r in rows if r['exclusion_high_score_diagnostic']],
              'range_violation_ids': [r['id'] for r in rows if r['score_range_violation']],
              'proposed_label_diagnostics': {}, 'human_grounded_metrics': {}, 'rows': rows}
    if mode == 'live':
        for partition in ('all', 'tuning', 'heldout'):
            subset = [r for r in rows if partition == 'all' or r['split'] == partition]
            result['proposed_label_diagnostics'][partition] = metrics(subset, 'proposed_label', threshold)
            result['human_grounded_metrics'][partition] = metrics(subset, 'human_label', threshold)
    else:
        result['metric_limitation'] = 'No ranking quality metrics: scripted/dry-run outputs are contract evidence only.'
    checkpoint({'event': 'complete', 'evaluated_n': len(rows), 'stop_reason': stop_reason})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['offline', 'dry-run', 'live'], default='offline')
    parser.add_argument('--allow-local-live', action='store_true')
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--config', type=Path, default=ROOT/'config.yaml')
    parser.add_argument('--full-inputs', type=Path)
    parser.add_argument('--split', choices=['all', 'tuning', 'heldout'], default='all')
    parser.add_argument('--case-ids', nargs='+')
    parser.add_argument('--endpoint')
    parser.add_argument('--model')
    parser.add_argument('--max-requests', type=int, default=12)
    parser.add_argument('--max-tokens', type=int, default=1500)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--run-seconds', type=float, default=600)
    parser.add_argument('--max-input-bytes', type=int, default=64000)
    parser.add_argument('--stop-after-errors', type=int, default=2)
    parser.add_argument('--checkpoint', type=Path, help='Append-only, fsynced JSONL; live defaults to OUTPUT.jsonl')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.mode == 'live' and not args.allow_local_live:
        parser.error('live requires --allow-local-live after cohort review')
    # Production warning messages may include raw malformed model payloads; ordinary
    # evaluation artifacts contain hashes/error classes only, never applicant text.
    logging.getLogger('src.scoring').setLevel(logging.CRITICAL)
    try:
        result = run(mode=args.mode, dataset_path=args.dataset, config_path=args.config,
                     full_inputs_path=args.full_inputs, split=args.split, case_ids=args.case_ids,
                     endpoint=args.endpoint, model=args.model, max_requests=args.max_requests,
                     max_tokens=args.max_tokens, timeout=args.timeout, run_seconds=args.run_seconds,
                     max_input_bytes=args.max_input_bytes, stop_after_errors=args.stop_after_errors,
                     checkpoint_path=args.checkpoint or (Path(str(args.output) + '.jsonl') if args.mode == 'live' else None))
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'mode': result['mode'], 'evaluated': result['evaluated_n'],
                      'errors': result['error_n'], 'http_requests': result['http_requests']}))
    return 0
