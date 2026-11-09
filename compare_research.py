#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare CONVERTED pharmacy JSON with external research results, fail closed.

Inputs:
  1. convert.py -> drugs_96_DRAFT.json (96/98 keys)
  2. research_batch.py -> medical_sources_REVIEW_ONLY.json
Output: comparison_REVIEW_ONLY.json and unapproved per-product review templates.
Never changes a medicine record or authorizes merging US labeling into Egyptian drugs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile

from apply_reviewed_medical import REVIEWABLE_FIELDS, REQUIRED_IDENTIFIERS
from catalog_details import DETAIL_EXTRA_KEYS
from convert import load_schema

COMPARABLE_FIELDS = frozenset(REVIEWABLE_FIELDS | {
    "indications", "pharmacologic_class",
})
# Keep defaults (numeric, booleans) out of evidence comparisons.
RAW_EFFECT_FIELDS = ("adverse_reactions", "adverse_reactions_table")
IDENTITY_FIELDS = REQUIRED_IDENTIFIERS + ("trade_name_ar",)
MAX_FILE_BYTES = 250 * 1024 * 1024


def load_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Input too large: {path}")
    with path.open(encoding="utf-8") as fp:
        return json.load(fp)


def catalog_index(catalog):
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("Converted catalog must be a nonempty JSON array")
    keys_96 = set(load_schema())
    acceptable = (keys_96, keys_96 | set(DETAIL_EXTRA_KEYS))
    expected = None
    by_id, uuids = {}, set()
    for index, drug in enumerate(catalog, 1):
        if not isinstance(drug, dict):
            raise ValueError(f"Catalog row {index} is not an object")
        if expected is None:
            expected = set(drug)
            if expected not in acceptable:
                raise ValueError("Catalog must use the converted 96- or 98-key schema")
        if set(drug) != expected:
            raise ValueError(f"Catalog row {index} uses a different schema")
        rid, uid = drug.get("id"), drug.get("uuid")
        if type(rid) is not int or rid < 1 or rid in by_id:
            raise ValueError(f"Invalid or duplicate catalog id: {rid!r}")
        if not isinstance(uid, str) or not uid or uid in uuids:
            raise ValueError(f"Invalid or duplicate catalog UUID at row {index}")
        if drug.get("is_active") != 0:
            raise ValueError("Input must be inactive converted draft data")
        by_id[rid] = drug
        uuids.add(uid)
    return by_id


def source_fields(result):
    references = result.get("reference_fields") or {}
    suggestions = result.get("suggestions") or {}
    if not isinstance(references, dict) or not isinstance(suggestions, dict):
        raise ValueError("Research result fields must be JSON objects")
    # An apparent product suggestion must be the same value as its label text.
    for key, value in suggestions.items():
        if references.get(key) != value:
            raise ValueError(f"Research suggestion differs from reference: {key}")
    if any(key not in COMPARABLE_FIELDS for key in set(references) | set(suggestions)):
        # Unknown/unreviewable fields must never silently enter medical reviews.
        raise ValueError("Research includes unexpected or unsafe candidate field")
    if any(not isinstance(v, str) for v in references.values()):
        raise ValueError("Clinical evidence must be textual")
    return references


def _verified_identity(catalog_drug, external):
    """ID and UUID alone are insufficient; compare all copied identity fields."""
    mismatches = {}
    for key in IDENTITY_FIELDS:
        # Research pipeline always emits these fields, even as empty strings.
        actual = external.get(key, None)
        expected = catalog_drug.get(key)
        if actual is None or actual != expected:
            mismatches[key] = {"catalog": expected, "research": actual}
    if "manufacturer_name" in catalog_drug:
        if external.get("manufacturer_name", None) != catalog_drug["manufacturer_name"]:
            mismatches["manufacturer_name"] = {
                "catalog": catalog_drug["manufacturer_name"],
                "research": external.get("manufacturer_name", None),
            }
    return mismatches


def compare(catalog, research_report):
    by_id = catalog_index(catalog)
    if not isinstance(research_report, dict) or research_report.get("clinical_use_allowed") is not False:
        raise ValueError("Expected a research-only report with clinical_use_allowed=false")
    researched = research_report.get("drugs")
    if not isinstance(researched, list):
        raise ValueError("Research report must contain drugs array")
    report_rows, templates = [], []
    counts = Counter()
    seen = set()
    for idx, result in enumerate(researched, 1):
        if not isinstance(result, dict) or type(result.get("id")) is not int:
            raise ValueError(f"Invalid research record at position {idx}")
        rid = result["id"]
        if rid in seen:
            raise ValueError(f"Duplicate research result for drug ID {rid}")
        seen.add(rid)
        if rid not in by_id:
            raise ValueError(f"Research drug ID {rid} is not in the converted catalog")
        original = by_id[rid]
        if (result.get("clinical_use_allowed") is not False or
            result.get("review_required") is not True or
            result.get("auto_merged_into_json_batches") is not False or
            result.get("fda_label_is_verified_egyptian_product") is not False):
            raise ValueError(f"Research drug ID {rid} must be reference-only and unapproved")

        mismatches = _verified_identity(original, result)
        if mismatches:
            counts["REJECTED_IDENTITY_MISMATCH"] += 1
            report_rows.append({
                "id": rid, "uuid": original["uuid"],
                "comparison_status": "REJECTED_IDENTITY_MISMATCH",
                "identity_mismatches": mismatches,
                "field_comparisons": [],
                "automatic_merge_allowed": False,
                "catalog_unchanged": True,
            })
            continue

        references = source_fields(result)
        safety = result.get("safety_review") or {}
        sections = safety.get("sections") or {} if isinstance(safety, dict) else {}
        if not isinstance(sections, dict):
            raise ValueError(f"Invalid safety sections for drug ID {rid}")

        field_rows, eligible_keys = [], []
        for key in sorted(COMPARABLE_FIELDS):
            evidence = references.get(key, "")
            if not evidence:
                continue
            existing = original.get(key, "")
            if existing in ("", None):
                status = "MISSING_REQUIRES_REVIEW"
                eligible_keys.append(key)
            elif isinstance(existing, str) and existing.strip() == evidence.strip():
                status = "EXACT_TEXT_ALREADY_PRESENT"
            else:
                status = "CONFLICT_REQUIRES_REVIEW"
            field_rows.append({
                "field": key, "catalog_value": existing,
                "reference_text": evidence,
                "comparison_status": status,
                "reference_source": result.get("source", ""),
                "confirmed_for_local_product": False,
            })
            counts[status] += 1

        adverse = {}
        for raw_key in RAW_EFFECT_FIELDS:
            item = sections.get(raw_key)
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                adverse[raw_key] = {
                    "raw_text": text,
                    "source_field": item.get("source_field", raw_key),
                    "classification": "UNCLASSIFIED_MIXED_EFFECTS",
                    "review_required": True,
                }
        possible_template_fields = set(eligible_keys) & REVIEWABLE_FIELDS
        if adverse:
            # Blank placeholders ONLY. Never paste a mixed reaction section
            # into either a 'common' or 'serious' pharmacy field.
            possible_template_fields.update({"side_effects_common", "side_effects_serious"})
        technical_match = bool(result.get("potential_strict_match") is True)
        status = "TECHNICAL_MATCH_STILL_REVIEW_REQUIRED" if technical_match else "REFERENCE_ONLY_NOT_LOCAL_PRODUCT_MATCH"
        if not field_rows and not adverse:
            status = "NO_RELEVANT_EXTERNAL_EVIDENCE"
        counts[status] += 1
        report_rows.append({
            "id": rid,
            "uuid": original["uuid"],
            "trade_name_en": original["trade_name_en"],
            "scientific_name": original["scientific_name"],
            "strength_text": original["strength_text"],
            "dosage_form": original["dosage_form"],
            "route": original["route"],
            "comparison_status": status,
            "research_match_status": result.get("status"),
            "technical_match_only": technical_match,
            "label_source": result.get("source", ""),
            "label_set_id": result.get("source_set_id", ""),
            "label_effective_time": result.get("label_effective_time", ""),
            "source_errors": result.get("source_errors", {}),
            "field_comparisons": field_rows,
            "unclassified_adverse_effects": adverse,
            "eligible_review_field_names": sorted(possible_template_fields),
            "automatic_merge_allowed": False,
            "catalog_unchanged": True,
        })
        if possible_template_fields:
            # apply_reviewed_medical.py still refuses these until manually
            # researched, filled, and marked approved by a qualified reviewer.
            template = {key: original.get(key) for key in REQUIRED_IDENTIFIERS}
            template.update({
                "approved": False, "reviewed_by": "", "review_date": "",
                "source_url": result.get("source", ""),
                "reviewed_fields": {key: "" for key in sorted(possible_template_fields)},
                "_review_note": "NOT APPROVED. Confirm the EXACT local product; enter verified text manually, remove blank fields and _review_note before submission.",
            })
            templates.append(template)

    return {
        "status": "COMPARISON_REVIEW_ONLY_NOT_CLINICALLY_APPROVED",
        "catalog_records": len(catalog),
        "researched_records": len(researched),
        "compared_records": len(report_rows),
        "counts": dict(sorted(counts.items())),
        "catalog_sha256": hashlib.sha256(json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "same_drug_identity_required": list(IDENTITY_FIELDS) + ["manufacturer_name_if_present"],
        "notes": [
            "No source data was rewritten, merged, reordered, or clinically approved.",
            "A technical US-label match does not establish local Egyptian product equivalence.",
            "Adverse-reaction sections are never automatically classified as common/serious.",
            "Review templates contain no clinical assertions and approved=false.",
            "Any mismatch between converted and research product identities is rejected.",
        ],
        "automatic_merge_allowed": False,
        "results": report_rows,
    }, templates


def atomic_json(path, value):
    path = Path(path)
    if path.exists():
        raise ValueError(f"Refusing to overwrite an existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".comparison-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(value, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Compare converter JSON and research evidence, produce review-only JSON")
    p.add_argument("--catalog", required=True, help="Converter's final 96/98-key JSON array")
    p.add_argument("--research", required=True, help="Researcher's medical_sources_REVIEW_ONLY.json")
    p.add_argument("--output", required=True, help="JSON comparison report (must not exist)")
    p.add_argument("--review-template", required=True, help="Unapproved review template JSON (must not exist)")
    args = p.parse_args(argv)
    if Path(args.output).resolve() == Path(args.review_template).resolve():
        p.error("Output and template must be different files")
    if any(Path(x).resolve() in (Path(args.catalog).resolve(), Path(args.research).resolve())
           for x in (args.output, args.review_template)):
        p.error("Outputs cannot overwrite inputs")
    for x in (args.output, args.review_template):
        if Path(x).exists():
            p.error(f"Output already exists: {x}")
    try:
        catalog = load_json(args.catalog)
        research_report = load_json(args.research)
        report, templates = compare(catalog, research_report)
        atomic_json(args.output, report)
        atomic_json(args.review_template, templates)
        print("COMPARISON REVIEW ONLY:", report["compared_records"],
              "drugs; identity mismatches:",
              report["counts"].get("REJECTED_IDENTITY_MISMATCH", 0),
              "| review templates:", len(templates))
        print("No unreviewed medical fields added to the original converter JSON.")
        return 0
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        p.exit(1, f"COMPARISON FAILED: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
