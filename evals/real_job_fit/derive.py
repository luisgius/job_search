"""Recompute diagnostics from retained scores; never invokes generation."""
from __future__ import annotations

import argparse
import copy
import inspect
import json
import re
from pathlib import Path

from .harness import digest, metrics

LEGACY_LIMITATION = (
    'The original run fingerprinted on-disk source only after scoring. The harness '
    'file changed during the run to add derived metrics/provenance; its recorded '
    'hash does not verify the exact loaded harness revision. Production scoring '
    'and prompts were unchanged by those edits. Original rows and reported hashes '
    'remain unmodified; current metric-source hashes identify derivation only.'
)


def audit_sanitized(value):
    """Reject raw applicant/model/description fields and obvious contact strings."""
    forbidden = {'cv_markdown', 'prompt', 'system', 'description', 'messages',
                 'email', 'phone', 'applicant', 'reasons', 'strengths', 'gaps',
                 'verdict', 'content', 'reasoning', 'reasoning_content'}
    if isinstance(value, dict):
        if forbidden.intersection(value):
            raise ValueError('private/raw content field in result artifact')
        for item in value.values():
            audit_sanitized(item)
    elif isinstance(value, list):
        for item in value:
            audit_sanitized(item)
    elif isinstance(value, str):
        if re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', value):
            raise ValueError('contact email in result artifact')
        if re.search(r'(?:https?://)?(?:www\.)?linkedin\.com/in/', value, re.I):
            raise ValueError('personal contact URL in result artifact')
        if re.search(r'\+\d[\d ()-]{8,}\d', value):
            raise ValueError('possible phone in result artifact')


def validate_checkpoints(original, events):
    if not events or events[0].get('event') != 'start' or events[-1].get('event') != 'complete':
        raise ValueError('checkpoint stream incomplete')
    checkpoints = [e for e in events if e.get('event') == 'row']
    if [e['row'] for e in checkpoints] != original['rows']:
        raise ValueError('checkpoint rows differ from original report')
    if [e['request_telemetry'] for e in checkpoints] != original['usage']:
        raise ValueError('checkpoint telemetry differs from original report')
    if events[0]['selected_ids'] != [r['id'] for r in original['rows']]:
        raise ValueError('selected IDs differ from completed rows')
    if events[-1]['evaluated_n'] != original['evaluated_n']:
        raise ValueError('completion count differs')
    for key in ('dataset_sha256', 'cv_sha256', 'config_sha256'):
        if events[0][key] != original[key]:
            raise ValueError('checkpoint start fingerprints differ')
    return {'validated': True, 'events': len(events), 'completed_rows': len(checkpoints),
            'rows_and_telemetry_identical': True}


def derive(original_bytes, checkpoint_bytes=None):
    original = json.loads(original_bytes)
    audit_sanitized(original)
    result = copy.deepcopy(original)
    validation = None
    if checkpoint_bytes is not None:
        events = [json.loads(line) for line in checkpoint_bytes.splitlines() if line.strip()]
        audit_sanitized(events)
        validation = validate_checkpoints(original, events)
    result['derivation'] = {
        'kind': 'retained-score metric recomputation; no generation',
        'original_sha256': digest(original_bytes),
        'checkpoints_sha256': digest(checkpoint_bytes) if checkpoint_bytes is not None else None,
        'checkpoint_validation': validation,
        'metric_source_sha256': digest(inspect.getsource(metrics).encode()),
        'derivation_source_sha256': digest(Path(__file__).read_bytes()),
        'legacy_harness_hash_limitation': LEGACY_LIMITATION,
        'original_rows_unchanged': result['rows'] == original['rows'],
        'additional_http_requests': 0,
        'human_quality_evidence': False,
        'interpretation': 'Model measurements and AI-proposed-label diagnostics only; zero human labels, no baseline comparison.'}
    for split in ('all', 'tuning', 'heldout'):
        rows = [r for r in original['rows'] if split == 'all' or r['split'] == split]
        result['proposed_label_diagnostics'][split] = metrics(rows, 'proposed_label', original['threshold'])
        result['human_grounded_metrics'][split] = metrics(rows, 'human_label', original['threshold'])
    audit_sanitized(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--checkpoints', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.original.resolve() == args.output.resolve():
        parser.error('derived output must differ from original evidence')
    result = derive(args.original.read_bytes(), args.checkpoints.read_bytes() if args.checkpoints else None)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
