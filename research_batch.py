#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence-first high-throughput research: catalog retrieval + 3 US sources.

Deduplicates requests by ingredient, bounds parallelism, uses cached network
responses, and provides per-drug verification flags. Never writes to catalog.
Nothing returned here is authorized clinical guidance for Egyptian products.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any

from fast_search import DrugSearchIndex, load_index, normalize
from label_api import APIClient, FDA_URL, simple_ingredient, propose
from clinical_candidates import strict_candidate

DAILYMED_SPLS_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
MAX_DRUGS_TO_RESEARCH = 30
MAX_WORKERS = 3


def daily_med_summary(data: Any, max_results=5) -> list[dict]:
    if not isinstance(data, dict):
        return []
    list_data = data.get("data")
    if not isinstance(list_data, list):
        return []
    seen = set()
    rows = []
    for item in list_data:
        if not isinstance(item, dict):
            continue
        setid = str(item.get("setid") or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", setid) or setid in seen:
            continue
        seen.add(setid)
        rows.append({
            "set_id": setid,
            "title": str(item.get("title") or ""),
            "published_date": str(item.get("published_date") or ""),
            "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=" + setid,
            "product_equivalence_verified": False,
        })
        if len(rows) >= max_results:
            break
    return rows


def fetch_references(ingredient: str, cache_dir: Path, delay: float = 1.2):
    """Independent thread-local client avoids mutable timestamp races."""
    client = APIClient(str(cache_dir), api_key=os.getenv("OPENFDA_API_KEY", ""), delay=delay)
    status = {}
    rx = []
    labels = []
    dm = []
    try:
        rx = client.rxnorm(ingredient)
    except (RuntimeError, ValueError) as exc:
        status["rxnorm_error"] = type(exc).__name__ + ": " + str(exc)[:250]
    try:
        labels = client.fda(ingredient)
    except (RuntimeError, ValueError) as exc:
        status["openfda_error"] = type(exc).__name__ + ": " + str(exc)[:250]
    try:
        response = client.get(DAILYMED_SPLS_URL, {
            "drug_name": ingredient,
            "name_type": "generic",
            "pagesize": "10",
        })
        dm = daily_med_summary(response)
    except (RuntimeError, ValueError) as exc:
        status["dailymed_error"] = type(exc).__name__ + ": " + str(exc)[:250]
    return {"rxcuis": rx, "fda_labels": labels, "dailymed": dm, "errors": status}


class FrozenReferenceAPI:
    def __init__(self, fetched):
        self.data = fetched

    def rxnorm(self, _ingredient):
        return self.data.get("rxcuis", [])

    def fda(self, _ingredient):
        if "openfda_error" in self.data.get("errors", {}):
            raise RuntimeError(self.data["errors"]["openfda_error"])
        return self.data.get("fda_labels", [])


def select_matches(index: DrugSearchIndex, queries: list[str], max_records: int):
    """Select in query order, by catalog rank; IDs always unique."""
    if not queries:
        raise ValueError("At least one search query is required")
    if not 1 <= max_records <= MAX_DRUGS_TO_RESEARCH:
        raise ValueError(f"max_records must be between 1 and {MAX_DRUGS_TO_RESEARCH}")
    ids = set()
    selected = []
    catalog_results = []
    for query in queries:
        matches = index.search(query, limit=min(max_records, 10))
        catalog_results.append({"query": query, "matches": matches})
        for match in matches:
            rid = match["id"]
            if rid not in ids and len(selected) < max_records:
                ids.add(rid)
                selected.append(index.records[index.id_to_position[rid]])
    return selected, catalog_results


def research(index: DrugSearchIndex, queries: list[str], max_records: int = 10,
             workers: int = 2, cache_dir: Path = Path(".research_cache"),
             lookup=fetch_references) -> dict:
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be 1..{MAX_WORKERS}")
    started = perf_counter()
    selected, catalog_results = select_matches(index, queries, max_records)
    ing_by_id = {}
    ingredients = {}
    for row in selected:
        ingredient = simple_ingredient(row.get("scientific_name"))
        key = normalize(ingredient)
        ing_by_id[row["id"]] = key
        if key and key not in ingredients:
            ingredients[key] = ingredient
    fetched: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(lookup, name, cache_dir): key for key, name in ingredients.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                fetched[key] = future.result()
            except Exception as exc:
                fetched[key] = {
                    "rxcuis": [], "fda_labels": [], "dailymed": [],
                    "errors": {"pipeline_error": type(exc).__name__ + ": " + str(exc)[:250]},
                }

    researched = []
    status_counts: dict[str, int] = {}
    for row in selected:
        key = ing_by_id[row["id"]]
        if not key:
            result = {
                "id": row["id"], "status": "complex_ingredient_not_queried",
                "reason": "Combined/unclear ingredient cannot be safely searched as one substance.",
                "reference_fields": {}, "suggestions": {},
            }
            source_info = {"rxcuis": [], "dailymed": [], "errors": {}}
        else:
            source_info = fetched[key]
            result = propose(row, FrozenReferenceAPI(source_info), 110)
        result["uuid"] = row.get("uuid")
        result["trade_name_ar"] = row.get("trade_name_ar", "")
        result["strength_text"] = row.get("strength_text", "")
        result["dosage_form"] = row.get("dosage_form", "")
        result["route"] = row.get("route", "")
        result["manufacturer_name"] = row.get("manufacturer_name", "")
        result["dailymed_search_results"] = source_info.get("dailymed", [])
        result["rxcui_reference_ids"] = source_info.get("rxcuis", [])
        result["source_errors"] = source_info.get("errors", {})
        result["potential_strict_match"] = strict_candidate(result)
        result["clinical_use_allowed"] = False
        result["review_required"] = True
        result["fda_label_is_verified_egyptian_product"] = False
        result["auto_merged_into_json_batches"] = False
        # The highest match score is still a potential label, NOT approval.
        status = str(result.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        researched.append(result)

    return {
        "status": "RESEARCH_ONLY_NOT_CLINICALLY_APPROVED",
        "query_results": catalog_results,
        "catalog_total": len(index.records),
        "researched_drugs": len(selected),
        "unique_ingredient_queries": len(ingredients),
        "deduplicated_ingredient_requests": len(selected) - len(ingredients),
        "parallel_workers": workers,
        "cache_ttl_days": 14,
        "elapsed_seconds": round(perf_counter() - started, 2),
        "sources": {
            "catalog": "eg-drugs converted JSON",
            "rxnorm": "https://rxnav.nlm.nih.gov/REST/rxcui.json",
            "openfda": FDA_URL,
            "dailymed": DAILYMED_SPLS_URL,
        },
        "clinical_use_allowed": False,
        "reliability_note": "Source API labels are US market records and do not establish equivalence with an Egyptian product.",
        "statuses": status_counts,
        "drugs": researched,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Run indexed catalog + bounded official source research")
    p.add_argument("--input", required=True, help="CONVERTED 96/98-field drug catalog JSON")
    p.add_argument("--query", action="append", required=True, help="Search phrase (repeat for multiple queries)")
    p.add_argument("--max-records", type=int, default=10, help="Max distinct drugs to research, at most 30")
    p.add_argument("--workers", type=int, default=2, help="Parallel network workers, 1..3")
    p.add_argument("--cache-dir", default=".research_cache")
    p.add_argument("--output", default="research_report_REVIEW_ONLY.json")
    args = p.parse_args(argv)
    path = Path(args.output)
    if path.exists():
        p.error("Output already exists")
    index = load_index(Path(args.input))
    result = research(index, args.query, args.max_records, args.workers, Path(args.cache_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("RESEARCH COMPLETE:", result["researched_drugs"], "drugs",
          "| unique substances:", result["unique_ingredient_queries"],
          "| deduplicated requests:", result["deduplicated_ingredient_requests"],
          "| time:", result["elapsed_seconds"], "seconds")
    print("STATUSES:", result["statuses"])
    print("REPORT:", path)
    print("DO NOT IMPORT RESEARCH REFERENCES AS CLINICALLY VERIFIED DRUG DATA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
