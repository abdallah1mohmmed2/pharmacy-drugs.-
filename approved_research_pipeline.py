#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Connected research -> comparison -> approved merge -> JSON-only 300 export.

Input approvals must be reviewed and authorized outside this script.
We fetch external research *only for the requested exact converted drug IDs*,
without selecting a different high-scoring brand by accident. Ingredient-level
FDA labels are reference-only and never become automatic clinical assertions.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from apply_reviewed_medical import REQUIRED_IDENTIFIERS
from compare_research import catalog_index, load_json
from fast_search import DrugSearchIndex
from research_batch import research, fetch_references, MAX_DRUGS_TO_RESEARCH
from verified_merge_export import export_verified


def research_reviewed_ids(catalog, approvals, *, cache_dir, workers=2,
                          lookup=fetch_references):
    by_id = catalog_index(catalog)
    if not isinstance(approvals, list) or not 1 <= len(approvals) <= MAX_DRUGS_TO_RESEARCH:
        raise ValueError(f"Requires 1..{MAX_DRUGS_TO_RESEARCH} independently approved records per run")
    ids = []
    seen = set()
    for pos, approval in enumerate(approvals, 1):
        if not isinstance(approval, dict) or type(approval.get("id")) is not int:
            raise ValueError(f"Approval #{pos} must have an integer drug ID")
        rid = approval["id"]
        if rid not in by_id or rid in seen:
            raise ValueError(f"Approval #{pos} drug ID unknown or duplicated: {rid}")
        seen.add(rid)
        for key in REQUIRED_IDENTIFIERS:
            if approval.get(key) != by_id[rid].get(key):
                raise ValueError(f"Approval for drug {rid} has mismatched {key}")
        if approval.get("approved") is not True or approval.get("exact_local_product_label_verified") is not True:
            raise ValueError(f"Approval for drug {rid} must have approved=true and exact_local_product_label_verified=true")
        ids.append(rid)
    index = DrugSearchIndex(catalog)
    # Search exact numeric source IDs; never use fuzzy matched product names.
    result = research(index, [str(rid) for rid in ids],
                      max_records=len(ids), workers=workers,
                      cache_dir=Path(cache_dir), lookup=lookup)
    found = {item["id"] for item in result["drugs"]}
    if found != set(ids) or len(result["drugs"]) != len(ids):
        raise ValueError(f"Research selected the wrong drug IDs: expected {ids}, found {sorted(found)}")
    return result


def run_connected_export(catalog_path, approvals_path, output_dir, *,
                         cache_dir=".research_cache", workers=2,
                         lookup=fetch_references):
    catalog = load_json(catalog_path)
    approvals = load_json(approvals_path)
    research_report = research_reviewed_ids(
        catalog, approvals, cache_dir=cache_dir, workers=workers, lookup=lookup
    )
    # The intermediate official-source report is deliberately not placed in
    # json_batches; only the converted merged drug records are split/uploaded.
    with TemporaryDirectory(prefix="drug-research-review-") as tmp:
        report_path = Path(tmp) / "medical_sources_REVIEW_ONLY.json"
        report_path.write_text(
            json.dumps(research_report, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        audit = export_verified(catalog_path, report_path, approvals_path, output_dir)
    return audit


def main(argv=None):
    p = argparse.ArgumentParser(description="Research exact approved drug IDs, compare and export <=300 approved JSON drafts")
    p.add_argument("--catalog", required=True, help="Converter 96/98-field full draft JSON")
    p.add_argument("--approvals", required=True, help="JSON file containing 1..30 pharmacist-reviewed exact-product approvals")
    p.add_argument("--output-dir", required=True, help="New output directory with json_batches subdirectory")
    p.add_argument("--cache-dir", default=".research_cache")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--github-output", default="")
    a = p.parse_args(argv)
    if not 1 <= a.workers <= 3:
        p.error("workers must be 1..3")
    try:
        audit = run_connected_export(
            a.catalog, a.approvals, a.output_dir,
            cache_dir=a.cache_dir, workers=a.workers
        )
        if a.github_output:
            with Path(a.github_output).open("a", encoding="utf-8") as fp:
                fp.write(f"batch_count={audit['batches']}\n")
                fp.write(f"total_records={audit['exported_records']}\n")
        print("CONNECTED COMPARISON AND VERIFIED JSON EXPORT:", json.dumps({
            "approved_drugs": audit["approved_drugs"],
            "changed_fields": audit["changed_fields"],
            "batches": audit["batches"],
            "total_records": audit["exported_records"],
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"CONNECTED PIPELINE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
