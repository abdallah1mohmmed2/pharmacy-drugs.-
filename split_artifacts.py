#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Partition the converter's FINAL DRAFT JSON, without changing or repeating records.

Only JSON files are produced in output_dir. No upstream source, manifest, audit,
or clinical files are copied into artifact payloads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from convert import load_schema

MAX_DRUGS_PER_FILE = 1000
MAX_ARTIFACTS = 64


def valid_record_id(record: dict[str, Any]) -> int:
    value = record.get("id")
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid drug ID {value!r}")
    return value


def partition_records(
    records: list[dict[str, Any]],
    output_dir: Path,
    *,
    batch_size: int = MAX_DRUGS_PER_FILE,
    max_artifacts: int = MAX_ARTIFACTS,
) -> tuple[int, list[Path]]:
    """Write part_01.json etc. Each original object appears exactly once.

    Does not sort, merge, deduplicate, normalize, or fill any field; invalid
    duplicate IDs/UUIDs cause a hard error instead of silent data loss.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_DRUGS_PER_FILE:
        raise ValueError("batch_size must be an integer between 1 and 1000")
    if not isinstance(records, list) or not records:
        raise ValueError("The converted JSON must be a nonempty array")
    total = len(records)
    expected_chunks = (total + batch_size - 1) // batch_size
    if expected_chunks > max_artifacts:
        raise ValueError(
            f"Would create {expected_chunks} artifacts, exceeding limit {max_artifacts}; "
            f"choose a larger batch_size or increase max_artifacts"
        )
    if output_dir.exists():
        raise ValueError(f"Output directory already exists: {output_dir}")

    schema_keys = set(load_schema())
    unique_ids: set[int] = set()
    unique_uuids: set[str] = set()
    for index, row in enumerate(records, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Record {index} is not a JSON object")
        if set(row) != schema_keys:
            raise ValueError(f"Record {index} is not the expected 96-field converted output")
        drug_id = valid_record_id(row)
        if drug_id in unique_ids:
            raise ValueError(f"Duplicate ID {drug_id} at source row {index}")
        unique_ids.add(drug_id)

        uuid_value = row.get("uuid")
        if not isinstance(uuid_value, str) or not uuid_value.strip():
            raise ValueError(f"Record {index} has an empty/invalid UUID")
        if uuid_value in unique_uuids:
            raise ValueError(f"Duplicate UUID {uuid_value} at source row {index}")
        unique_uuids.add(uuid_value)

    # All validation completes BEFORE any files are created.
    output_dir.mkdir(parents=True, exist_ok=False)
    paths: list[Path] = []
    reconstructed: list[dict[str, Any]] = []
    for start in range(0, total, batch_size):
        batch = records[start:start + batch_size]  # contiguous, nonoverlapping
        ordinal = start // batch_size + 1
        path = output_dir / f"batch_{ordinal:02d}.json"
        with path.open("w", encoding="utf-8") as out:
            json.dump(batch, out, ensure_ascii=False, indent=2)
            out.write("\n")
        # Read back each file to guard against truncation/corruption.
        with path.open("r", encoding="utf-8") as f:
            check = json.load(f)
        if len(check) != len(batch) or not 1 <= len(check) <= batch_size or check != batch:
            raise ValueError(f"Post-write verification failed: {path.name}")
        reconstructed.extend(check)
        paths.append(path)
        print(f"{path.name}: {len(batch)} records; rows {start + 1}..{start + len(batch)}")

    if reconstructed != records:
        raise ValueError("Integrity failure: joining JSON batches does not reproduce converted input")
    if len({r["id"] for r in reconstructed}) != total:
        raise ValueError("Integrity failure: duplicate drug IDs across batches")
    if len({r["uuid"] for r in reconstructed}) != total:
        raise ValueError("Integrity failure: duplicate UUIDs across batches")
    if any(p.suffix != ".json" for p in output_dir.iterdir()):
        raise ValueError("Non-JSON output detected")

    print(
        f"VERIFIED: {total} input records = {sum(len(records[i:i + batch_size]) for i in range(0, total, batch_size))} "
        f"exported records in {len(paths)} JSON-only batches; "
        "ZERO lost or duplicate IDs; full objects preserved in original order"
    )
    return total, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split converted drugs into JSON-only artifacts (<= 1000 each)")
    parser.add_argument("--input", required=True, help="Path to converter's drugs_96_DRAFT.json")
    parser.add_argument("--output-dir", default="json_batches", help="New directory containing only batch_NN.json")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--github-output", help="GitHub Actions $GITHUB_OUTPUT file")
    args = parser.parse_args(argv)
    try:
        src_path = Path(args.input).resolve()
        if not src_path.is_file():
            raise ValueError(f"Cannot find converted JSON: {src_path}")
        with src_path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        total, paths = partition_records(records, Path(args.output_dir).resolve(), batch_size=args.batch_size)
        if args.github_output:
            with Path(args.github_output).open("a", encoding="utf-8") as fh:
                fh.write(f"batch_count={len(paths)}\n")
                fh.write(f"total_records={total}\n")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
