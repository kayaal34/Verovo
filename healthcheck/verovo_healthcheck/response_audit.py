"""Read-only Claude response evidence across explicitly selected JSONL files.

Message IDs join usage snapshots, never attempts or billable operations. No
snapshot wins a conflict; no session or invoice total is inferred.
"""
import hashlib
import json
import os
from pathlib import Path

USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                'cache_creation_input_tokens')
MAX_FILE_BYTES = 400 * 1024 * 1024
FILE_LIMIT_ENV = 'VEROVO_MAX_RESPONSE_AUDIT_MB'


def _max_file_bytes():
    raw = os.environ.get(FILE_LIMIT_ENV, '').strip()
    if raw:
        try:
            configured = int(float(raw) * 1024 * 1024)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured
    return MAX_FILE_BYTES


def audit(paths):
    groups = {}
    seen_files = set()
    limit = _max_file_bytes()
    counts = dict(files=0, repeated_file_inputs=0, assistant_rows=0,
                  missing_response_id=0, invalid_rows=0, ignored_rows=0)
    for filename in paths:
        path = Path(filename)
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen_files:
            counts['repeated_file_inputs'] += 1
            continue
        if stat.st_size > limit:
            raise ValueError(
                'Input exceeds the %d-byte per-file limit; split it by session or date, '
                'or set %s to raise the cap' % (limit, FILE_LIMIT_ENV))
        seen_files.add(identity)
        counts['files'] += 1
        with path.open('rb') as stream:
            consumed = 0
            for raw in stream:
                consumed += len(raw)
                if consumed > limit:
                    raise ValueError(
                        'Input grew beyond the %d-byte per-file limit while reading; '
                        'split it by session or date, or set %s to raise the cap'
                        % (limit, FILE_LIMIT_ENV))
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode('utf-8-sig'))
                except (ValueError, UnicodeError, RecursionError):
                    counts['invalid_rows'] += 1
                    continue
                if not isinstance(row, dict):
                    counts['invalid_rows'] += 1
                    continue
                if row.get('type') != 'assistant':
                    counts['ignored_rows'] += 1
                    continue
                counts['assistant_rows'] += 1
                message = row.get('message')
                message = message if isinstance(message, dict) else {}
                response_id = message.get('id')
                if not isinstance(response_id, str) or not response_id.strip():
                    counts['missing_response_id'] += 1
                    continue
                key = hashlib.sha256(response_id.encode('utf-8')).hexdigest()
                group = groups.setdefault(key, dict(rows=0, files=set(), snapshots=set(),
                                                     request_ids=set(), models=set(), incomplete=0))
                group['rows'] += 1
                group['files'].add(identity)
                for field, target in [(row.get('requestId'), 'request_ids'),
                                      (message.get('model'), 'models')]:
                    if isinstance(field, str) and field:
                        group[target].add(field)
                usage = message.get('usage')
                usage = usage if isinstance(usage, dict) else {}
                if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_FIELDS):
                    group['incomplete'] += 1
                else:
                    group['snapshots'].add(tuple(usage[k] for k in USAGE_FIELDS))
    records = []
    for key, group in sorted(groups.items()):
        conflicting = (len(group['snapshots']) > 1 or len(group['request_ids']) > 1
                       or len(group['models']) > 1)
        state = 'CONFLICT' if conflicting else 'INCOMPLETE' if group['incomplete'] else 'OBSERVED'
        records.append(dict(response_id_sha256=key, occurrences=group['rows'],
                            file_count=len(group['files']), distinct_complete_usage_snapshots=len(group['snapshots']),
                            incomplete_usage_rows=group['incomplete'], state=state,
                            repeated_across_files=len(group['files']) > 1))
    return dict(kind='claude-response-evidence', counts=counts, responses=records,
                response_groups=len(records),
                repeated_across_files=sum(r['repeated_across_files'] for r in records),
                conflicting_groups=sum(r['state']=='CONFLICT' for r in records),
                incomplete_groups=sum(r['state']=='INCOMPLETE' for r in records),
                measurement_verdict='UNPROVABLE', session_token_total=None, invoice_total=None,
                limits='Response IDs are supplied by the selected logs. Repeated snapshots do not prove repeated execution or billing. Conflicts are not resolved by first/last/max. No session completeness, final usage or invoice total is inferred. Cache fields are not added to input totals.',
                next_steps=['Inspect the source snapshots for conflicting response IDs; retain every version.',
                            'For missing identities or usage fields, obtain the original response evidence; do not fill unknown fields with zero.',
                            'Use applicable provider billing records for invoice questions. This report is not a billing verdict.'])
