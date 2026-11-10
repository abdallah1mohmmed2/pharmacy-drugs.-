#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fallback RxNorm nomenclature search for ingredients missing official US labels.

RXCUI/RxNorm *lexical* matches are reference candidates, not proof of a
marketed Egyptian product, clinical equivalence, dosage, or correct ingredient.
No output here may be merged automatically into pharmacy clinical fields.
"""
from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import argparse
import json
import re
import time

RXNORM = "https://rxnav.nlm.nih.gov/REST/approximateTerm.json"
SKIP = ("formula", "multivit", "milk", "herbal", "shampoo", "sun protection",
        "massage", "cosmetic", "extract", "perfume", "sunscreen", "lotion",
        "nutritive", "beauty", "cleansing", "mouth wash")
ALPHA = re.compile(r"[a-z][a-z0-9 .()\-]{3,64}", re.I)


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def admissible(term):
    return bool(4 <= len(term) <= 65 and ALPHA.fullmatch(term)
                and len(term.split()) <= 5 and not any(x in term for x in SKIP)
                and not any(x in term for x in ("/", ";", ",", "&", " and ")))


def selection(rows, known, count=500):
    """Focus on previously unmapped names, prioritizing full single ingredients."""
    singles, compound = Counter(), Counter()
    examples = {}
    for row in rows:
        raw = normalize(row.get("scientific_name"))
        if not raw:
            continue
        parts = raw.split("+")
        for name in set(parts):
            name = normalize(name)
            if not admissible(name) or name in known:
                continue
            (singles if len(parts) == 1 else compound)[name] += 1
            examples.setdefault(name, [])
            if len(examples[name]) < 12:
                examples[name].append(row["id"])
    terms = sorted(set(singles) | set(compound),
                   key=lambda x: (-singles[x], -(singles[x] + compound[x]), x))
    return [{
        "term": name,
        "single_drug_records": singles[name],
        "combination_component_records": compound[name],
        "example_egyptian_catalog_ids": examples[name],
    } for name in terms[:count]]


def query_term(term):
    params = urlencode({"term": term, "maxEntries": 6, "option": 1})
    url = RXNORM + "?" + params
    for attempt in range(3):
        try:
            req = Request(url, headers={"Accept": "application/json",
                         "User-Agent": "Drug-review-source-mapper/1.0"})
            with urlopen(req, timeout=25) as handle:
                raw = handle.read(200001)
                if len(raw) > 200000:
                    return [], "OVERSIZE"
                doc = json.loads(raw)
            candidates = (doc.get("approximateGroup") or {}).get("candidate") or []
            if not isinstance(candidates, list):
                return [], "BAD_JSON"
            output = []
            seen = set()
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                rx = str(item.get("rxcui") or "")
                if not rx.isdecimal() or rx in seen:
                    continue
                text = item.get("name")
                if not isinstance(text, str) or not text.strip():
                    continue
                seen.add(rx)
                label = normalize(text)
                output.append({
                    "rxcui": rx,
                    "rxnorm_candidate_name": text,
                    "rxnav_concept_url": "https://rxnav.nlm.nih.gov/REST/rxcui/" + rx + "/properties.json",
                    "reported_rank": str(item.get("rank") or ""),
                    "reported_score": str(item.get("score") or ""),
                    "source_vocabulary": str(item.get("source") or ""),
                    "lexical_similarity_not_identity": round(SequenceMatcher(
                        None, normalize(term), label).ratio(), 3),
                    "is_local_product_match_verified": False,
                    "is_clinically_approved": False,
                    "review_status": "LEXICAL_RXNORM_REFERENCE_ONLY",
                })
                if len(output) >= 3:
                    break
            return output, "FOUND_REFERENCE" if output else "NO_NAMED_CANDIDATE"
        except HTTPError as ex:
            if ex.code in (400, 401, 403, 404):
                return [], "HTTP_" + str(ex.code)
            if ex.code == 429:
                time.sleep(5 * (attempt + 1))
            elif attempt < 2:
                time.sleep(attempt + 1)
            else:
                return [], "HTTP_" + str(ex.code)
        except (ValueError, OSError, URLError, TimeoutError):
            if attempt == 2:
                return [], "REQUEST_ERROR"
            time.sleep(attempt + 1)
    return [], "RETRIES_EXHAUSTED"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--existing-profiles", nargs="+", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--max-queries", type=int, default=500)
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args(argv)
    if not 1 <= args.max_queries <= 650 or not 1 <= args.workers <= 3:
        p.error("max-queries must be 1..650 and workers 1..3")
    rows = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) < 20000:
        p.error("Full converted catalog required")
    known = set()
    for path in args.existing_profiles:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if document.get("no_clinical_approval_claimed") is not True:
            raise ValueError("Expected verified review-only FDA profile file")
        known.update(document.get("profiles", {}))
    tasks = selection(rows, known, count=args.max_queries)
    results = {}
    statuses = Counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(query_term, row["term"]): row for row in tasks}
        for i, f in enumerate(as_completed(jobs), 1):
            info = jobs[f]
            try:
                matches, status = f.result()
            except Exception:
                matches, status = [], "UNEXPECTED_ERROR"
            statuses[status] += 1
            results[info["term"]] = {**info,
                "rxnorm_request_status": status,
                "candidate_identity_matches": matches,
                "local_egyptian_product_verified": False,
                "approved_for_clinical_use": False,
                "review_note_ar": "تقارب الأسماء في RxNorm لا يثبت صحة المادة الفعالة أو التركيز أو الشكل أو الجرعة للمستحضر المحلي."
            }
            if i % 50 == 0:
                print("RXNORM PROGRESS", i, len(tasks), "found", statuses["FOUND_REFERENCE"], flush=True)
    output = {
        "status": "UNVERIFIED_RXNORM_NAME_REFERENCES_FOR_PHARMACIST_REVIEW",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "catalog_records": len(rows),
        "queried_ingredient_names": len(tasks),
        "names_with_candidate_rxcui": statuses["FOUND_REFERENCE"],
        "request_outcomes": dict(statuses),
        "source_api": RXNORM,
        "local_product_identity_verified": False,
        "clinical_use_authorized": False,
        "note_ar": "معرف RxCUI وحده ليس نشرة أدوية ولا جرعة ولا تحققًا من المستحضر المصري.",
        "results": dict(sorted(results.items())),
    }
    path = Path(args.output)
    if path.exists():
        raise ValueError("Output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    print("RXNORM FINALE", json.dumps({
        "queried": len(tasks), "with_candidates": statuses["FOUND_REFERENCE"],
        "request_outcomes": dict(statuses),
    }, flush=True))


if __name__ == "__main__":
    main()
