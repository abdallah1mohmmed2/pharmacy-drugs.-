#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply medically reviewed text ONLY after explicit per-product verification.

This is NOT an automatic drug information generator. Do not generate reviewer
approvals from ingredient-level openFDA references. The caller is responsible for
authenticating the pharmacist and validating the regulator/product label.
The output is still inactive (is_active=0) pending deployment approval.
"""
from __future__ import annotations
import argparse
from datetime import date
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from convert import load_schema
from catalog_details import DETAIL_EXTRA_KEYS

# Only free-text medication sections. Never infer controlled status,
# pregnancy risk categories, patient dosing numbers or safety boolean flags.
REVIEWABLE_FIELDS = frozenset({
    "dosage_administration",
    "contraindications",
    "warnings_precautions",
    "black_box_warning",
    "interactions_summary",
    "side_effects_common",
    "side_effects_serious",
    "pregnancy_notes",
    "lactation_notes",
    "pediatric_notes",
    "geriatric_notes",
    "storage_conditions",
})
REQUIRED_IDENTIFIERS = ("id", "uuid", "trade_name_en", "scientific_name",
                        "strength_text", "dosage_form", "route")


def approved_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        raise ValueError("Every approved medical section must be nonempty text no longer than 100000 characters")
    return value.strip()


def is_https_source(url):
    if not isinstance(url, str):
        return False
    u = urlparse(url)
    return u.scheme == "https" and bool(u.netloc) and not u.username and not u.password


def apply_reviews(rows, approvals):
    """Returns a new list, never touches input; strict identity/field validation."""
    if not isinstance(rows, list) or not isinstance(approvals, list):
        raise ValueError("Both input files must contain arrays")
    schema = set(load_schema())
    expected_shapes = (schema, schema | set(DETAIL_EXTRA_KEYS))
    if any(not isinstance(r, dict) or set(r) not in expected_shapes for r in rows):
        raise ValueError("Input is not a 96-core or 98-field converted drug catalog")
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Converted drug input has duplicate IDs")
    by_id = {r["id"]: r for r in rows}
    seen_approval_ids = set()
    patches = {}
    for index, review in enumerate(approvals, 1):
        if not isinstance(review, dict) or review.get("approved") is not True:
            raise ValueError(f"Approval row {index}: requires explicitly approved=true")
        rid = review.get("id")
        if type(rid) is not int or rid not in by_id:
            raise ValueError(f"Approval row {index}: unknown or invalid drug ID")
        if rid in seen_approval_ids:
            raise ValueError(f"Duplicate medical approval for ID {rid}")
        seen_approval_ids.add(rid)
        record = by_id[rid]
        for key in REQUIRED_IDENTIFIERS:
            if key not in review or review[key] != record[key]:
                raise ValueError(f"Approval ID {rid}: identity mismatch in {key}")
        reviewer = review.get("reviewed_by")
        if not isinstance(reviewer, str) or len(reviewer.strip()) < 3:
            raise ValueError(f"Approval ID {rid}: missing named medical reviewer")
        review_date = review.get("review_date")
        try:
            parsed_date = date.fromisoformat(review_date)
        except (ValueError, TypeError):
            raise ValueError(f"Approval ID {rid}: review_date must be ISO YYYY-MM-DD")
        if parsed_date > date.today():
            raise ValueError(f"Approval ID {rid}: review date is in the future")
        if not is_https_source(review.get("source_url")):
            raise ValueError(f"Approval ID {rid}: must provide a complete HTTPS source URL")
        items = review.get("reviewed_fields")
        if not isinstance(items, dict) or not items:
            raise ValueError(f"Approval ID {rid}: no reviewed_fields")
        if set(items) - REVIEWABLE_FIELDS:
            raise ValueError(f"Approval ID {rid}: disallowed medical fields: {set(items) - REVIEWABLE_FIELDS}")
        patch = {}
        for field, value in items.items():
            value = approved_text(value)
            existing = record[field]
            if existing and existing != value:
                raise ValueError(f"Approval ID {rid}: refuses to overwrite existing {field}")
            patch[field] = value
        patches[rid] = patch

    result = []
    for original in rows:
        row = original.copy()
        if row["id"] in patches:
            row.update(patches[row["id"]])
        row["is_active"] = 0
        result.append(row)
    if len(result) != len(rows) or [r["id"] for r in result] != ids:
        raise ValueError("Review merge lost or reordered drugs")
    if any(set(a) != set(b) for a, b in zip(result, rows)):
        raise ValueError("Review merge altered the converted JSON schema")
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description="Merge explicitly pharmacist-reviewed text into an inactive JSON draft")
    p.add_argument("--input", required=True, help="Converted 96/98-field drug JSON file")
    p.add_argument("--approvals", required=True, help="Per-drug pharmacist-approved JSON array")
    p.add_argument("--output", required=True, help="New output path, must not already exist")
    a = p.parse_args(argv)
    out = Path(a.output)
    if out.exists():
        p.error("Output already exists; refusing to overwrite")
    try:
        rows = json.loads(Path(a.input).read_text(encoding="utf-8"))
        approvals = json.loads(Path(a.approvals).read_text(encoding="utf-8"))
        updated = apply_reviews(rows, approvals)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"REVIEW-MERGED-DRAFT: {len(updated)} records; {len(approvals)} independently reviewed approvals applied.")
        print("Medical content is applied only with per-product declarations; this code cannot authenticate reviewers.")
        return 0
    except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
