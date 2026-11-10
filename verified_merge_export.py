#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verified-review merge: converter + search comparator + pharmacist approvals -> JSON-only batches.

Clinical search results are research evidence, never approval. No automatic
clinical claims are accepted. A human-reviewed EXACT local product label is
required for each approved field. The program checks declarations and catalog
identity; it CANNOT authenticate a pharmacist or a real-world authorization.
The source JSON, research report and approvals are never overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from apply_reviewed_medical import apply_reviews, REVIEWABLE_FIELDS
from compare_research import compare, catalog_index, load_json
from split_artifacts import partition_records, MAX_DRUGS_PER_FILE


def _digest(records):
    return hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def merge_checked(catalog, research, approvals):
    """Verify approved data against *both* comparator and converter before merging.

    No numeric/boolean safety values can be changed. Every approved field must
    be explicitly available in its product-specific comparison review template.
    """
    catalog_index(catalog)
    comparison, templates = compare(catalog, research)
    if not isinstance(approvals, list) or not approvals:
        raise ValueError("At least one manually reviewed product approval is required; no automatic medical merge")
    identities = {row["id"]: row for row in comparison["results"]}
    eligible = {row["id"]: set(row.get("eligible_review_field_names", []))
                for row in comparison["results"]}
    template_ids = {row["id"] for row in templates}
    for idx, approval in enumerate(approvals, 1):
        if not isinstance(approval, dict) or type(approval.get("id")) is not int:
            raise ValueError(f"Approval {idx} has an invalid product ID")
        rid = approval["id"]
        entry = identities.get(rid)
        if not entry or rid not in template_ids:
            raise ValueError(f"Approval for drug {rid} is absent from the researched and compared review candidates")
        if entry["comparison_status"] == "REJECTED_IDENTITY_MISMATCH":
            raise ValueError(f"Drug {rid}: comparator rejected drug identity")
        if approval.get("exact_local_product_label_verified") is not True:
            raise ValueError(f"Drug {rid}: exact_local_product_label_verified must be true after independent review")
        approved_fields = approval.get("reviewed_fields")
        if not isinstance(approved_fields, dict) or not approved_fields:
            raise ValueError(f"Drug {rid}: approved reviewed_fields must be nonempty object")
        if not set(approved_fields) <= eligible[rid]:
            unexpected = sorted(set(approved_fields) - eligible[rid])
            raise ValueError(f"Drug {rid}: fields not available or in conflict in comparison: {unexpected}")
        if not set(approved_fields) <= REVIEWABLE_FIELDS:
            raise ValueError(f"Drug {rid}: nonclinical or restricted field cannot be merged")
        # Declared product identity, source URL, reviewer and date are
        # independently checked by apply_reviews(), not inferred here.
    merged = apply_reviews(catalog, approvals)
    approved_by_id = {r["id"]: r["reviewed_fields"] for r in approvals}
    approved_ids = set(approved_by_id)
    if len(approved_ids) != len(approvals):
        raise ValueError("Duplicate approvals cannot be merged")
    changed = Counter()
    for original, updated in zip(catalog, merged):
        if original["id"] != updated["id"] or original["uuid"] != updated["uuid"]:
            raise ValueError("Drug identities were changed during merge")
        if set(original) != set(updated):
            raise ValueError("Converted record shape was changed")
        fields = set(approved_by_id.get(original["id"], {}))
        actual_changes = {key for key in original if updated[key] != original[key]}
        if actual_changes - fields:
            raise ValueError(f"Drug {original['id']}: unreviewed field was changed")
        if updated.get("is_active") != 0:
            raise ValueError(f"Drug {original['id']}: record must remain inactive")
        for key in actual_changes:
            changed[key] += 1
    audit = {
        "status": "PHARMACIST_REVIEW_DECLARED_NOT_INDEPENDENTLY_AUTHENTICATED",
        "original_records": len(catalog),
        "merged_records": len(merged),
        "approved_drugs": len(approvals),
        "changed_fields": dict(sorted(changed.items())),
        "catalog_sha256_before": _digest(catalog),
        "merged_sha256_after": _digest(merged),
        "reviewed_approval_ids": sorted(approved_ids),
        "all_records_remain_inactive": True,
        "medical_information_imported_without_approval": False,
        "reviewer_identity_authenticated_by_program": False,
        "notes": [
            "The program validates declarations, ID/UUID, brand, ingredient, strength, form and route; it cannot certify medical correctness.",
            "The external US label is supporting reference only, not proof of local product equivalence.",
            "Only explicit reviewed_fields may differ; unapproved draft values and all other fields are preserved.",
        ],
    }
    return merged, comparison, audit


def export_verified(catalog_path, research_path, approvals_path, output_dir,
                    *, batch_size=MAX_DRUGS_PER_FILE):
    """Atomic output folder: one merged draft, one audit, one JSON-only batch folder.

    Only the json_batches subdirectory is appropriate for drug ZIP artifacts.
    """
    catalog_path = Path(catalog_path).resolve()
    research_path = Path(research_path).resolve()
    approvals_path = Path(approvals_path).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError(f"Output directory exists, refusing overwrite: {output_dir}")
    if len({catalog_path, research_path, approvals_path}) != 3:
        raise ValueError("Converter, research and approvals must be three distinct files")
    if any(output_dir == p or output_dir in p.parents for p in (catalog_path, research_path, approvals_path)):
        raise ValueError("Output must not contain or overwrite input files")
    catalog, research, approvals = (
        load_json(catalog_path), load_json(research_path), load_json(approvals_path)
    )
    merged, comparison, audit = merge_checked(catalog, research, approvals)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".verified-json-", dir=output_dir.parent))
    try:
        merged_path = stage / "merged_drugs_DRAFT.json"
        merged_path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        total, paths = partition_records(merged, stage / "json_batches", batch_size=batch_size)
        audit["batches"] = len(paths)
        audit["batch_size_limit"] = MAX_DRUGS_PER_FILE
        audit["exported_records"] = total
        audit["medical_merge_comparison_status"] = comparison["status"]
        (stage / "verified_merge_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        # Audit files and merged full drafts are NOT inside json_batches.
        if any(p.suffix != ".json" for p in (stage / "json_batches").iterdir()):
            raise ValueError("Batch directory contains non-JSON content")
        stage.rename(output_dir)
        return audit
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    p = argparse.ArgumentParser(description="Merge only explicitly pharmacist-reviewed product data and export <=300 JSON-only batches")
    p.add_argument("--catalog", required=True, help="Converter's original 96/98-field draft JSON")
    p.add_argument("--research", required=True, help="Research report from research_batch.py")
    p.add_argument("--approvals", required=True, help="Explicit product-level pharmacist approval JSON list")
    p.add_argument("--output-dir", required=True, help="New folder, containing only json_batches/ plus separate local audit/draft")
    p.add_argument("--batch-size", type=int, default=MAX_DRUGS_PER_FILE)
    p.add_argument("--github-output", help="GitHub Actions output file (batch_count and total_records)")
    a = p.parse_args(argv)
    try:
        audit = export_verified(
            a.catalog, a.research, a.approvals, a.output_dir, batch_size=a.batch_size
        )
        if a.github_output:
            with Path(a.github_output).open("a", encoding="utf-8") as fh:
                fh.write(f"batch_count={audit['batches']}\n")
                fh.write(f"total_records={audit['exported_records']}\n")
        print("VERIFIED REVIEW MERGE:", json.dumps({
            "approved_drugs": audit["approved_drugs"],
            "changed_fields": audit["changed_fields"],
            "exported_records": audit["exported_records"],
            "batches": audit["batches"],
        }, ensure_ascii=False))
        print("ONLY output_dir/json_batches/*.json should be uploaded into the 300-drug ZIP artifacts.")
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"VERIFIED EXPORT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
