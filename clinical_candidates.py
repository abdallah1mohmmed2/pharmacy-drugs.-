#!/usr/bin/env python3
# coding: utf-8
"""Collect STRICT openFDA label suggestions in sidecar ONLY (no medicine JSON edits).

Matching a US label never establishes equivalence with a marketed Egyptian product.
Qualified review of every candidate and any clinical use remains mandatory.
"""
from __future__ import annotations
import argparse
import csv
from collections import OrderedDict
import json
import os
from pathlib import Path
import sys
from label_api import APIClient, propose


class MemoizedLookups:
    """Bounded in-run reuse for multiple brands with one active ingredient.

    Reuses only the remote research *response*, never a proposed product match.
    Every product is rechecked independently by classify_label().
    """
    def __init__(self, backend, max_entries=48):
        self.backend = backend
        self.max_entries = max_entries
        self.caches = {"rxnorm": OrderedDict(), "fda": OrderedDict()}
        self.cache_hits = {"rxnorm": 0, "fda": 0}
        self.remote_calls = {"rxnorm": 0, "fda": 0}

    def _cached(self, method, ingredient):
        key = ingredient.casefold().strip()
        cache = self.caches[method]
        if key in cache:
            cache.move_to_end(key)
            self.cache_hits[method] += 1
            return cache[key]
        result = getattr(self.backend, method)(ingredient)
        self.remote_calls[method] += 1
        cache[key] = result
        if len(cache) > self.max_entries:
            cache.popitem(last=False)
        return result

    def rxnorm(self, ingredient):
        return self._cached("rxnorm", ingredient)

    def fda(self, ingredient):
        return self._cached("fda", ingredient)

    def metrics(self):
        return {"remote_calls": self.remote_calls.copy(),
                "in_memory_cache_hits": self.cache_hits.copy(),
                "max_cached_ingredients": self.max_entries}


def strict_candidate(meta):
    if meta.get('status') != 'draft_suggestions':
        return False
    return (meta.get('identity') == 'exact_ingredient' and
            meta.get('brand_match') is True and
            meta.get('route_match') is True and
            meta.get('strength_match') is True and
            meta.get('same_score_candidates') == 1 and
            meta.get('score', 0) >= 110)


def collect(records, client, offset, count, output_dir):
    if offset >= len(records):
        raise ValueError(f'Offset {offset} >= total {len(records)}')
    chosen = records[offset:offset + count]
    client = MemoizedLookups(client)
    outfile = output_dir / 'clinical_label_candidates_REVIEW.jsonl'
    auditfile = output_dir / 'clinical_label_audit.csv'
    stats = {}
    safety_counts = {key: 0 for key in ('interaction_sources', 'adverse_sources', 'warnings_sources', 'common_heading_cues', 'serious_heading_cues')}
    review_templates = []
    with outfile.open('w', encoding='utf-8') as out, auditfile.open('w', newline='', encoding='utf-8-sig') as csvfile:
        columns = ['id', 'status', 'trade_name_en', 'scientific_name', 'score', 'source',
                   'label_effective_time', 'reason', 'suggested_fields', 'reference_fields',
                   'label_brand', 'label_routes', 'route_conflict', 'unclassified_sections',
                   'interaction_source', 'adverse_source', 'warnings_source',
                   'common_heading_cues', 'serious_heading_cues']
        writer = csv.DictWriter(csvfile, fieldnames=columns)
        writer.writeheader()
        for idx, rec in enumerate(chosen, 1):
            data = propose(rec, client, 110)
            is_candidate = strict_candidate(data)
            if not is_candidate:
                # Keep exact-ingredient US label information as REFERENCE ONLY,
                # never as a product-specific suggestion or confirmed clinical field.
                data['suggestions'] = {}
            status = 'CANDIDATE_REQUIRES_PHARMACIST_REVIEW' if is_candidate else data.get('status','unknown')
            if not is_candidate and (data.get('reference_fields') or data.get('unclassified_label_sections')):
                status = 'INGREDIENT_REFERENCE_ONLY_NOT_PRODUCT_MATCH'
            elif not is_candidate and data.get('status') == 'draft_suggestions':
                status = 'REJECTED_AMBIGUOUS_MATCH'
            data['status'] = status
            safety = data.get('safety_review') or {}
            sections = safety.get('sections') or {}
            targets = safety.get('review_targets') or {}
            interaction_source = sections.get('drug_interactions') or sections.get('drug_interactions_table') or {}
            adverse_source = sections.get('adverse_reactions') or sections.get('adverse_reactions_table') or {}
            warnings_source = sections.get('warnings_and_cautions') or sections.get('boxed_warning') or {}
            common_cues = (targets.get('side_effects_common') or {}).get('wording_cues') or []
            serious_cues = (targets.get('side_effects_serious') or {}).get('wording_cues') or []
            safety_counts['interaction_sources'] += bool(interaction_source)
            safety_counts['adverse_sources'] += bool(adverse_source)
            safety_counts['warnings_sources'] += bool(warnings_source)
            safety_counts['common_heading_cues'] += bool(common_cues)
            safety_counts['serious_heading_cues'] += bool(serious_cues)
            if sections:
                # A non-approved template. A pharmacist must check the exact
                # marketed product and replace placeholders. Empty fields must be removed.
                identifiers = ('id', 'uuid', 'trade_name_en', 'scientific_name',
                               'strength_text', 'dosage_form', 'route')
                review_templates.append({
                    **{key: rec.get(key) for key in identifiers},
                    'approved': False,
                    'reviewed_by': '',
                    'review_date': '',
                    'source_url': data.get('source', ''),
                    'reviewed_fields': {
                        'interactions_summary': '',
                        'side_effects_common': '',
                        'side_effects_serious': '',
                    },
                    '_INSTRUCTIONS': 'Review the exact product leaflet; delete blank fields; verify all identity fields; set approved true only after pharmacist review; remove _INSTRUCTIONS before use.',
                })
            data['clinical_use_allowed'] = False
            data['source_country'] = 'US label — may differ from Egyptian brand authorization'
            data['review_required'] = True
            data['safety_classifications_verified'] = False
            out.write(json.dumps(data, ensure_ascii=False) + '\n')
            writer.writerow({
                'id': data.get('id'), 'status': status,
                'trade_name_en': data.get('trade_name_en'),
                'scientific_name': data.get('scientific_name'),
                'score': data.get('score'), 'source': data.get('source'),
                'label_effective_time': data.get('label_effective_time'),
                'reason': data.get('note'),
                'suggested_fields': ';'.join(data.get('suggestions',{}).keys()),
                'reference_fields': ';'.join(data.get('reference_fields',{}).keys()),
                'label_brand': ';'.join(map(str, data.get('us_label_brand_names', []))),
                'label_routes': ';'.join(map(str, data.get('us_label_routes', []))),
                'route_conflict': data.get('route_conflict', ''),
                'unclassified_sections': ';'.join(data.get('unclassified_label_sections', {}).keys()),
                'interaction_source': interaction_source.get('source_field', ''),
                'adverse_source': adverse_source.get('source_field', ''),
                'warnings_source': warnings_source.get('source_field', ''),
                'common_heading_cues': ';'.join(common_cues),
                'serious_heading_cues': ';'.join(serious_cues),
            })
            stats[status] = stats.get(status, 0) + 1
            if idx % 10 == 0 or idx == len(chosen):
                print(f'Clinical candidates: {idx}/{len(chosen)} (offset {offset})',flush=True)
    (output_dir / 'medical_safety_review_TEMPLATE.json').write_text(
        json.dumps(review_templates, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output_dir / 'clinical_label_summary.json').write_text(json.dumps({
        'status': 'REVIEW_ONLY_NOT_CLINICALLY_VALIDATED',
        'input_rows': len(records), 'offset': offset, 'attempted': len(chosen),
        'counts': stats, 'safety_label_coverage': safety_counts,
        'medical_review_templates': len(review_templates),
        'ingredient_lookup_cache': client.metrics(),
        'may_apply_to_database_automatically': False,
        'source_api': 'https://api.fda.gov/drug/label.json',
        'warnings': ['USA labels may not represent Egyptian brands or authorizations',
                     'Matching does not establish clinical correctness or dosing suitability',
                     'Combination products and uncertain matches intentionally skipped',
                     'INGREDIENT_REFERENCE_ONLY is NOT product-level matching. It must not be copied into the 96-field dataset without professional verification',
                     'Adverse reactions remain unclassified; do not automatically label them common or serious',
                     'The medical_safety_review_TEMPLATE.json intentionally has approved=false and blank fields: only a qualified pharmacist may fill it from the exact product leaflet'],
    },ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if stats.get('api_error', 0) == len(chosen):
        raise ValueError('All API lookups failed: clinical candidate run must NOT be treated as success')
    return stats


def main():
    p=argparse.ArgumentParser(description='Prepare label suggestions only; never write into medicine records')
    p.add_argument('--input',default='outputs/drugs_96_DRAFT.json')
    p.add_argument('--output-dir',default='clinical_outputs')
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--count',type=int,default=20)
    p.add_argument('--delay',type=float,default=1.0)
    p.add_argument('--cache',default='.label_cache')
    args=p.parse_args()
    if args.offset < 0 or not 1 <= args.count <= 200 or args.delay < 0.4:
        p.error('offset >= 0, count between 1 and 200, delay >= 0.4 seconds required')
    input_path=Path(args.input).resolve()
    target=Path(args.output_dir).resolve()
    if not input_path.is_file():
        p.error(f'Input not found: {input_path}')
    if target.exists():
        p.error('Output folder already exists: refusing to overwrite')
    target.mkdir(parents=True)
    try:
        records=json.loads(input_path.read_text(encoding='utf-8'))
        if not isinstance(records,list):
            raise ValueError('Input must be a list')
        client=APIClient(args.cache,os.getenv('OPENFDA_API_KEY',''),args.delay)
        print('Status:',collect(records,client,args.offset,args.count,target))
        return 0
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        (target/'FAILED.txt').write_text(str(exc)+'\n',encoding='utf-8')
        print('FAILED:',exc,file=sys.stderr)
        return 1

if __name__=='__main__':
    sys.exit(main())
