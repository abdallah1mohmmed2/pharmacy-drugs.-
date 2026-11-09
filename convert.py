#!/usr/bin/env python3
# coding: utf-8
"""Safe, auditable structural conversion of public eg-drugs JSON to 96 fields.

No unreviewed clinical inference. Never overwrites the source. Python 3.11+ stdlib.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import sys
import tempfile
import uuid
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

SOURCE_URL = 'https://raw.githubusercontent.com/mahmoudfalous/eg-drugs/main/data/eg_drugs.json'
REPO_URL = 'https://github.com/mahmoudfalous/eg-drugs'
SCHEMA_FILE = Path(__file__).with_name('schema_96.json')
REQUIRED_SOURCE_COLUMNS = ('id', 'name', 'arabic', 'active')
TEXT_MAPPING = {
    'name': 'trade_name_en',
    'arabic': 'trade_name_ar',
    'active': 'scientific_name',  # In this source, 'active' means ingredients, NOT enabled flag.
    'uses': 'indications',
    'description': 'therapeutic_class',  # Source's informal category, not a verified ATC class.
}
RISK_FLAGS = [
    'is_antibiotic', 'is_controlled', 'requires_prescription',
    'prescription_restricted', 'has_critical_interactions',
    'is_nephrotoxic', 'neonate_allowed', 'requires_refrigeration', 'protect_from_light',
]
SCHEMA_VERSION = 'v2.0'
MAX_FILE_BYTES = 150 * 1024 * 1024


def load_schema() -> dict:
    data = json.loads(SCHEMA_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or len(data) != 96:
        raise ValueError('The expected target schema has exactly 96 columns')
    return data


def get_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha1(b'blob ' + str(size).encode('ascii') + b'\0')
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download_original(dest: Path) -> None:
    req = Request(SOURCE_URL, headers={'User-Agent': 'eg-drugs-safe-converter/2.0',
                                       'Accept': 'application/json'})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=120) as response, dest.open('wb') as f:
                received = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_FILE_BYTES:
                        raise ValueError('Source file exceeds 150 MiB security limit')
                    f.write(chunk)
            return
        except (HTTPError, URLError, TimeoutError) as exc:
            dest.unlink(missing_ok=True)
            if isinstance(exc, HTTPError) and exc.code in (401, 403, 404):
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def load_source(path: Path):
    if not path.is_file():
        raise ValueError(f'Cannot find source JSON: {path}')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Source file exceeds 150 MiB security limit')
    with path.open(encoding='utf-8-sig') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError('Expected original data as a JSON list, not another structure')
    return data


def safe_text(value):
    if value is None:
        return ''
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ''


def gtin_checksum_valid(value):
    s = safe_text(value)
    if not s.isascii() or not s.isdigit() or len(s) not in (8, 12, 13, 14):
        return False
    digits = [int(x) for x in s]
    weighted = sum(d * (3 if i % 2 == 0 else 1)
                   for i, d in enumerate(reversed(digits[:-1])))
    return (-weighted) % 10 == digits[-1]


def verified_check_digit(source):
    flag = safe_text(source.get('barcode_valid')).lower()
    return flag == '1' and gtin_checksum_valid(source.get('barcode'))


def parse_id(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, int) and val > 0:
        return val
    if isinstance(val, str) and re.fullmatch(r'[1-9][0-9]*', val.strip()):
        return int(val.strip())
    return None


def preflight(records):
    """Fail closed on fundamental problems, reporting rather than dropping rows."""
    issues = []
    ids = set()
    if not records:
        issues.append({'row': 0, 'code': 'EMPTY_DATASET', 'detail': 'No records'})
        return issues
    for idx, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            issues.append({'row': idx, 'code': 'NOT_OBJECT', 'detail': type(rec).__name__})
            continue
        absent = [k for k in REQUIRED_SOURCE_COLUMNS if k not in rec]
        if absent:
            issues.append({'row': idx, 'code': 'SOURCE_SCHEMA_CHANGED', 'detail': ','.join(absent)})
        rid = parse_id(rec.get('id'))
        if rid is None:
            issues.append({'row': idx, 'code': 'INVALID_ID', 'detail': str(rec.get('id'))[:100]})
        elif rid in ids:
            issues.append({'row': idx, 'code': 'DUPLICATE_ID', 'detail': str(rid)})
        else:
            ids.add(rid)
        if not (safe_text(rec.get('name')) or safe_text(rec.get('arabic'))):
            issues.append({'row': idx, 'code': 'NAME_MISSING', 'detail': str(rid)})
        for key in TEXT_MAPPING:
            if key in rec and rec[key] is not None and (isinstance(rec[key], bool) or not isinstance(rec[key], (str, int, float))):
                issues.append({'row': idx, 'code': 'INVALID_FIELD_TYPE', 'detail': key})
    return issues


def write_json_atomic(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def convert(records, defaults, output_dir, source_sha, source_git_sha,
            sample=0, expected_min_rows=0):
    if sample < 0:
        raise ValueError('sample cannot be negative')
    chosen = records[:sample] if sample else records
    if not sample and len(records) < expected_min_rows:
        raise ValueError(f'Full source has {len(records)} records, expected at least {expected_min_rows}')
    problems = preflight(chosen)
    if problems:
        write_json_atomic(output_dir / 'preflight_errors.json', {
            'errors': len(problems), 'first_500_errors': problems[:500],
            'message': 'Refused to convert source with missing names, duplicate/invalid IDs or changed columns',
        })
        raise ValueError(f'Preflight failed with {len(problems)} errors (see preflight_errors.json)')

    clinical_fields = [k for k in defaults if k in (
        'adult_dose_min_mg', 'adult_dose_max_mg_per_day', 'ped_dose_mg_per_kg_min',
        'pregnancy_notes', 'lactation_notes', 'contraindications', 'black_box_warning',
        'interactions_summary', 'renal_notes', 'hepatic_notes', 'storage_conditions'
    )]
    audit_path = output_dir / 'mapping_audit.csv'
    original_path = output_dir / 'original_records.jsonl'
    draft_path = output_dir / 'drugs_96_DRAFT.json'
    gtin_valid = 0
    gtin_unverified = 0
    missing_ingredient = 0
    mapped_counts = {v: 0 for v in TEXT_MAPPING.values()}
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    seen_ids = set()

    with (draft_path.open('w', encoding='utf-8') as f_out,
          original_path.open('w', encoding='utf-8') as f_original,
          audit_path.open('w', encoding='utf-8-sig', newline='') as f_audit):
        writer = csv.DictWriter(f_audit, fieldnames=[
            'source_row', 'id', 'trade_name_en', 'scientific_name',
            'copied_fields', 'barcode', 'barcode_status', 'source_price_egp',
            'source_company', 'unverified_safety_fields', 'requires_clinical_review'
        ])
        writer.writeheader()
        f_out.write('[\n')
        for idx, source in enumerate(chosen, 1):
            rid = parse_id(source['id'])
            assert rid not in seen_ids
            seen_ids.add(rid)
            record = defaults.copy()
            record.update({
                'id': rid,
                'uuid': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{REPO_URL}/data/eg_drugs.json#id={rid}')),
                'internal_code': f'EG-{rid}',
                'created_at': now,
                'updated_at': now,
                'branch_id': 1,
                # Explicit import gate: drafts are inactive until external review/approval.
                'is_active': 0,
            })
            copied = []
            for orig, dst in TEXT_MAPPING.items():
                val = safe_text(source.get(orig))
                if val:
                    record[dst] = val
                    copied.append(f'{orig}->{dst}')
                    mapped_counts[dst] += 1
            if not record['scientific_name']:
                missing_ingredient += 1
            barcode = safe_text(source.get('barcode'))
            barcode_status = 'missing'
            if barcode:
                if verified_check_digit(source):
                    record['barcode_international'] = barcode
                    gtin_valid += 1
                    barcode_status = 'valid_check_digit_only'
                else:
                    barcode_status = 'failed_or_unconfirmed_check_digit'
                    gtin_unverified += 1
            # Keep price and manufacturer only in audit/original. No exchange-rate guesses.
            # Crucially, 0/false values in defaults DO NOT imply confirmed clinical negatives.
            if idx != 1:
                f_out.write(',\n')
            f_out.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')))
            f_original.write(json.dumps({'row': idx, 'source_id': rid, 'record': source},
                                        ensure_ascii=False, separators=(',', ':')) + '\n')
            writer.writerow({
                'source_row': idx, 'id': rid, 'trade_name_en': record['trade_name_en'],
                'scientific_name': record['scientific_name'], 'copied_fields': ';'.join(copied),
                'barcode': barcode, 'barcode_status': barcode_status,
                'source_price_egp': safe_text(source.get('price')),
                'source_company': safe_text(source.get('company')),
                'unverified_safety_fields': ';'.join(RISK_FLAGS),
                'requires_clinical_review': 'YES',
            })
        f_out.write('\n]\n')

    # An independent second read catches missing or truncated rows/incorrect keys.
    actual = load_source(draft_path)
    if len(actual) != len(chosen) or len({r['id'] for r in actual}) != len(chosen):
        raise ValueError('Validation failed: output count or IDs differ from source')
    if any(set(r) != set(defaults) or len(r) != 96 for r in actual):
        raise ValueError('Validation failed: the output does not have exactly 96 target fields')
    if any(r['scientific_name'] != safe_text(s.get('active')) for r, s in zip(actual, chosen)):
        raise ValueError('Validation failed: source active ingredient mapping mismatch')
    with original_path.open(encoding='utf-8') as fh:
        original_count = sum(1 for _ in fh)
    if original_count != len(chosen):
        raise ValueError('Validation failed: provenance row count mismatch')

    files = [draft_path, audit_path, original_path]
    report = {
        'status': 'DRAFT_NOT_CLINICALLY_VALIDATED',
        'schema_version': SCHEMA_VERSION,
        'source': SOURCE_URL,
        'license': 'CC BY 4.0 — Egyptian Drug Database (eg-drugs) by Mahmoud Mohamed',
        'source_sha256': source_sha,
        'source_git_blob_sha1': source_git_sha,
        'source_records_total': len(records),
        'converted_records': len(chosen),
        'is_sample': sample > 0,
        'target_fields': len(defaults),
        'barcodes_valid_check_digit': gtin_valid,
        'barcodes_unverified': gtin_unverified,
        'missing_ingredient': missing_ingredient,
        'mapped_nonempty_counts': mapped_counts,
        'unverified_safety_flags': RISK_FLAGS,
        'not_enriched_clinically': clinical_fields,
        'notes': [
            'Barcodes with a valid check digit have NOT been validated as registered products.',
            'Draft records are is_active=0; approval must happen outside this converter.',
            'All numerical and boolean clinical defaults are unverified placeholders, including zeros.',
            'Source price is EGP, and was intentionally NOT copied into Yemen retail price.',
            'Company is a name, NOT a numeric manufacturer_id; stored in audit/original only.',
            'Source units does not reliably represent tablets per pack; stored in original only.',
            'Free-text category is copied verbatim, NOT verified as ATC or pharmacological class.',
            'This structural conversion must NOT be used as verified clinical guidance.',
        ],
        'files_sha256': {p.name: get_sha256(p) for p in files},
        'generated_at_utc': now,
    }
    write_json_atomic(output_dir / 'manifest.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Safe audited 96-column JSON converter for eg-drugs')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--download', action='store_true', help='Fetch public eg_drugs.json')
    source.add_argument('--source', help='Local copy of source JSON')
    parser.add_argument('--output-dir', default='outputs', help='New output directory (must not exist)')
    parser.add_argument('--sample', type=int, default=0, help='First N records; 0 = ALL records')
    parser.add_argument('--expected-min-rows', type=int, default=20000,
                        help='Safety threshold for full conversion (default 20000)')
    parser.add_argument('--expected-source-sha256', default='', help='Pin exact source SHA256 (optional)')
    parser.add_argument('--expected-git-blob-sha1', default='', help='Pin exact Git blob ID (optional)')
    args = parser.parse_args(argv)
    if args.sample < 0 or args.expected_min_rows < 0:
        parser.error('sample and expected-min-rows must be >= 0')
    for opt, value, length in [('sha256', args.expected_source_sha256, 64),
                               ('git-blob-sha1', args.expected_git_blob_sha1, 40)]:
        if value and not re.fullmatch('[a-fA-F0-9]{' + str(length) + '}', value):
            parser.error(f'Invalid --expected-{opt} hash')
    target = Path(args.output_dir).resolve()
    if target.exists():
        parser.error('Output directory already exists: refusing to overwrite any outputs')
    target.mkdir(parents=True)
    try:
        input_path = Path(args.source).resolve() if args.source else target / 'eg_drugs_original.json'
        if not args.source:
            download_original(input_path)
        if target in input_path.parents and args.source:
            raise ValueError('Your source cannot reside in the new output folder')
        sha = get_sha256(input_path)
        blob = git_blob_sha1(input_path)
        if args.expected_source_sha256 and sha.lower() != args.expected_source_sha256.lower():
            raise ValueError('Source SHA256 mismatch — review changed source before converting')
        if args.expected_git_blob_sha1 and blob.lower() != args.expected_git_blob_sha1.lower():
            raise ValueError('Source Git blob SHA1 mismatch — review changed source before converting')
        records = load_source(input_path)
        result = convert(records, load_schema(), target, sha, blob,
                         sample=args.sample, expected_min_rows=args.expected_min_rows)
        print('PASS: converted', result['converted_records'], 'of', result['source_records_total'],
              '| 96 fields each | source SHA256', sha, flush=True)
        print('Generated files:', *(p.name for p in target.iterdir()), sep='\n - ')
        print('IMPORTANT: This is a structural DRAFT, NOT clinically validated.')
        return 0
    except (ValueError, OSError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print('FAIL:', type(exc).__name__, ':', exc, file=sys.stderr)
        for name in ('drugs_96_DRAFT.json', 'mapping_audit.csv', 'original_records.jsonl', 'manifest.json'):
            (target / name).unlink(missing_ok=True)
        (target / 'FAILED.txt').write_text(str(exc) + '\n', encoding='utf-8')
        return 1

if __name__ == '__main__':
    sys.exit(main())
