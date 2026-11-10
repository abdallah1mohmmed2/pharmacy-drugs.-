#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a review-ready JSON entry for EVERY converted medicine in the catalogue.

This is a review queue, NOT a set of medically approved products. It preserves
the original converted drug object inside each template, ensures uniqueness,
and writes a complete JSON plus <=300-record review batches. No external drug
labels are guessed or merged. A qualified pharmacist must independently
verify the exact locally marketed product before entering any medical text.
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

from apply_reviewed_medical import REQUIRED_IDENTIFIERS, REVIEWABLE_FIELDS
from compare_research import catalog_index, load_json

MAX_BATCH_SIZE = 300
MAX_BATCH_FILES = 500
FULL_FILENAME = "all_drugs_REVIEW_REQUIRED_NOT_APPROVED.json"
BATCH_FOLDER = "review_batches"
MANIFEST_FILENAME = "review_manifest.json"

REVIEW_MESSAGE_AR = (
    "هذا السجل للمراجعة فقط، وليس اعتمادًا طبيًا. لا تملأ جرعة أو تفاعلًا أو أثرًا جانبيًا "
    "إلا بعد التحقق من نشرة المستحضر المحلي نفسه ومراجعة صيدلي مختص. "
    "لا تغيّر approved أو exact_local_product_label_verified إلى true دون تحقق حقيقي."
)


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_review_entries(catalog):
    """Return one strictly unapproved and lossless template per converted row."""
    catalog_index(catalog)
    records = []
    for row in catalog:
        item = {field: row[field] for field in REQUIRED_IDENTIFIERS}
        item.update({
            "trade_name_ar": row.get("trade_name_ar", ""),
            "manufacturer_name": row.get("manufacturer_name", ""),
            "package_description": row.get("package_description", ""),
            "therapeutic_class": row.get("therapeutic_class", ""),
            "pharmacologic_class": row.get("pharmacologic_class", ""),
            "approved": False,
            "exact_local_product_label_verified": False,
            "reviewed_by": "",
            "review_date": "",
            "source_url": "",
            "review_status": "AWAITING_INDEPENDENT_PRODUCT_LEVEL_REVIEW",
            "reviewed_fields": {field: "" for field in sorted(REVIEWABLE_FIELDS)},
            # The complete converted JSON object is deliberately preserved:
            # the reviewer receives ALL 96/98 fields for EVERY medicine.
            "converted_record": row.copy(),
            "_instructions_ar": REVIEW_MESSAGE_AR,
        })
        records.append(item)
    return records


def _write_json(path: Path, rows):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")


def export_full_review_catalog(input_path, output_dir, *, batch_size=MAX_BATCH_SIZE, expected_min_rows=0):
    """Atomic full review export; never overwrite converter input or existing output."""
    input_path, output_dir = Path(input_path).resolve(), Path(output_dir).resolve()
    if not input_path.is_file():
        raise ValueError("Converted 96/98-field input JSON is missing")
    if output_dir.exists():
        raise ValueError("Output path already exists; refusing overwrite")
    if output_dir == input_path or output_dir in input_path.parents:
        raise ValueError("Output must never contain or overwrite the original converter input")
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("Review batch size must be an integer between 1 and 300")
    catalog = load_json(input_path)
    if type(expected_min_rows) is not int or expected_min_rows < 0:
        raise ValueError("expected_min_rows must be a nonnegative integer")
    if len(catalog) < expected_min_rows:
        raise ValueError("Review catalogue smaller than expected minimum")
    entries = make_review_entries(catalog)
    total = len(entries)
    batch_count = (total + batch_size - 1) // batch_size
    if batch_count > MAX_BATCH_FILES:
        raise ValueError(f"Refusing {batch_count} batches; exceeds safety bound {MAX_BATCH_FILES}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".full-review-", dir=output_dir.parent))
    try:
        complete_path = stage / FULL_FILENAME
        _write_json(complete_path, entries)
        parts_dir = stage / BATCH_FOLDER
        parts_dir.mkdir()
        inspected = 0
        ids = []
        uuids = []
        for start in range(0, total, batch_size):
            batch = entries[start:start + batch_size]
            path = parts_dir / f"batch_{start // batch_size + 1:03d}.json"
            _write_json(path, batch)
            with path.open(encoding="utf-8") as stream:
                restored = json.load(stream)
            if restored != batch or not 1 <= len(restored) <= batch_size:
                raise ValueError("Round-trip JSON verification failed for " + path.name)
            for item in restored:
                if item["approved"] is not False or item["exact_local_product_label_verified"] is not False:
                    raise ValueError("An unverified review entry was marked approved")
                if any(item["reviewed_fields"].values()):
                    raise ValueError("An unverified medical field has been filled")
                ids.append(item["id"])
                uuids.append(item["uuid"])
                inspected += 1
        if inspected != total or len(set(ids)) != total or len(set(uuids)) != total:
            raise ValueError("Mismatch between full catalogue and review batches")
        if [x["id"] for x in entries] != ids or [x["uuid"] for x in entries] != uuids:
            raise ValueError("Review batches altered medicine identity/order")
        if any(item["converted_record"] != row for item, row in zip(entries, catalog)):
            raise ValueError("Review snapshot differs from converted input")
        with complete_path.open(encoding="utf-8") as stream:
            full_again = json.load(stream)
        if full_again != entries:
            raise ValueError("Full review export does not roundtrip")
        manifest = {
            "status": "COMPLETE_DATABASE_REVIEW_QUEUE_NOT_CLINICALLY_APPROVED",
            "converted_input_records": len(catalog),
            "review_records": len(entries),
            "review_batches": batch_count,
            "max_drugs_per_batch": batch_size,
            "converted_fields_per_drug": len(catalog[0]),
            "unique_ids": len(set(ids)),
            "unique_uuids": len(set(uuids)),
            "source_converted_sha256": _sha256(input_path),
            "full_review_sha256": _sha256(complete_path),
            "all_clinical_reviews_approved": False,
            "approved_drugs": 0,
            "unapproved_drugs": total,
            "contains_exact_converted_record_for_every_drug": True,
            "no_original_upstream_database_in_artifacts": True,
            "manual_verification_required": True,
            "clinical_use_allowed": False,
            "review_note": REVIEW_MESSAGE_AR,
        }
        (stage / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stage.rename(output_dir)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate an unapproved review JSON for ALL converted drugs, split <=300")
    parser.add_argument("--catalog", required=True, help="Converted drugs_96_DRAFT.json (96 or 98 fields)")
    parser.add_argument("--output-dir", required=True, help="New output folder")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--expected-min-rows", type=int, default=0,
                        help="Fail if the converted source has fewer than this many drugs")
    args = parser.parse_args(argv)
    try:
        result = export_full_review_catalog(
            args.catalog, args.output_dir, batch_size=args.batch_size,
            expected_min_rows=args.expected_min_rows,
        )
        print("FULL REVIEW GENERATED:", json.dumps({
            "reviews": result["review_records"],
            "batches": result["review_batches"],
            "converted_fields": result["converted_fields_per_drug"],
            "approved": 0,
            "full_json_sha256": result["full_review_sha256"],
        }, ensure_ascii=False))
        print("NOT APPROVED. Product-specific medical facts require independently authenticated review.")
        return 0
    except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
        print(f"FULL REVIEW EXPORT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
