#!/usr/bin/env python3
# coding: utf-8
"""Find a second official-source lead for ingredients lacking an openFDA label.

DailyMed is a US regulator-linked SPL library, not an Egyptian registration
registry. All matches in this report remain unapproved ingredient-level
candidates; neither dosing nor interactions may be copied as product-specific.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

SPL_LIST = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
SPL_XML = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.xml"
SPL_INFO = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
XML_NAMESPACE = "{urn:hl7-org:v3}"
STOP_TERMS = ("formula", "multivitamin", "milk", "sun protection",
              "shampoo", "extract", "vitamin", "herbal", "cosmetic",
              "lotion", "sunscreen", "mouth wash", "massage",
              "baby food", "nutritive", "probiotic", "not specified")
SPL_TITLES = ("INDICATION", "DOSAGE", "CONTRAINDICATION", "WARNINGS",
              "ADVERSE", "DRUG INTERACTION", "PREGNANCY", "LACTATION",
              "PEDIATRIC", "GERIATRIC", "STORAGE", "OVERDOSAGE")


def clean(s):
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def candidates(catalog, profiles, *, max_queries=450):
    if not isinstance(catalog, list) or len(catalog) < 20000:
        raise ValueError("Complete converted catalog required")
    counts = Counter(clean(r.get("scientific_name")) for r in catalog)
    matches = [
        (term, count) for term, count in counts.items()
        if term and term not in profiles and 4 <= len(term) <= 48
        and len(term.split()) <= 4 and re.fullmatch(r"[a-z][a-z0-9 -]+", term)
        and not any(stop in term for stop in STOP_TERMS)
    ]
    return sorted(matches, key=lambda x: (-x[1], x[0]))[:max_queries]


def get(url):
    for attempt in range(2):
        try:
            req = Request(url, headers={
                "User-Agent": "Pharmacy-Review-Only/1.0",
                "Accept": "application/json, application/xml;q=0.8",
            })
            with urlopen(req, timeout=16) as response:
                blob = response.read(1800000 + 1)
                if len(blob) > 1800000:
                    return None, "OVERSIZED"
                return blob, "OK"
        except HTTPError as e:
            if e.code == 404:
                return None, "NO_MATCH"
            if e.code in (400, 401, 403, 429):
                return None, "HTTP_" + str(e.code)
            if attempt == 1:
                return None, "HTTP_" + str(e.code)
        except (OSError, TimeoutError, URLError):
            if attempt == 1:
                return None, "NETWORK_ERROR"
        time.sleep(0.4 * (attempt + 1))
    return None, "FAILED"


def extract_xml_sections(xml_bytes):
    """Use displayed SPL sections; never infer a clinical classification."""
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    sections = []
    seen = set()
    for section in root.iter(XML_NAMESPACE + "section"):
        title = section.find(XML_NAMESPACE + "title")
        text = section.find(XML_NAMESPACE + "text")
        if title is None or text is None:
            continue
        heading = clean("".join(title.itertext())).upper()
        if not any(x in heading for x in SPL_TITLES):
            continue
        raw = re.sub(r"\s+", " ", " ".join(text.itertext())).strip()
        if len(raw) < 12 or (heading, raw[:100]) in seen:
            continue
        seen.add((heading, raw[:100]))
        sections.append({
            "source_section_heading": heading[:160],
            "raw_source_text": raw[:7000],
            "truncated": len(raw) > 7000,
            "review_status": "NOT_PRODUCT_VERIFIED",
        })
        if len(sections) >= 18:
            break
    return sections


def research_term(item):
    name, count = item
    url = SPL_LIST + "?" + urlencode({
        "drug_name": name, "name_type": "generic", "pagesize": "3",
    })
    payload, status = get(url)
    if payload is None:
        return name, count, status, []
    try:
        listings = json.loads(payload).get("data") or []
    except (ValueError, UnicodeDecodeError):
        return name, count, "INVALID_JSON", []
    if not isinstance(listings, list):
        return name, count, "INVALID_RESPONSE", []
    result = []
    seen = set()
    for item in listings[:3]:
        if not isinstance(item, dict):
            continue
        setid = str(item.get("setid") or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", setid) or setid in seen:
            continue
        seen.add(setid)
        source, label_status = get(SPL_XML.format(setid=setid))
        sections = extract_xml_sections(source)
        result.append({
            "set_id": setid,
            "title_from_dailymed": str(item.get("title") or ""),
            "published_date": str(item.get("published_date") or ""),
            "source_url": SPL_INFO.format(setid=setid),
            "source_market": "US",
            "search_term": name,
            "search_matched_generic_name": True,
            "exact_egyptian_product_verified": False,
            "ingredient_identity_verified_from_label": False,
            "strength_form_route_verified": False,
            "review_status": "RAW_US_LABEL_FOR_HUMAN_COMPARISON",
            "xml_fetch_status": label_status,
            "unverified_source_sections": sections,
        })
        if len(result) >= 2:
            break
    return name, count, "FOUND_CANDIDATE" if result else "NO_MATCH", result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--existing-profiles", nargs="+", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--max-queries", type=int, default=450)
    p.add_argument("--workers", type=int, default=3)
    args = p.parse_args(argv)
    if not 1 <= args.max_queries <= 750 or not 1 <= args.workers <= 4:
        p.error("max-queries 1..750, workers 1..4")
    profiles = set()
    for path in args.existing_profiles:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        obj = data.get("profiles", {})
        if not isinstance(obj, dict):
            raise ValueError("Invalid official reference profile file")
        profiles.update(obj)
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    tasks = candidates(catalog, profiles, max_queries=args.max_queries)
    outcomes = Counter()
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {pool.submit(research_term, x): x[0] for x in tasks}
        for position, future in enumerate(as_completed(future_map), 1):
            try:
                name, count, status, labels = future.result()
            except Exception as ex:
                name, count, status, labels = future_map[future], 0, "UNEXPECTED_" + type(ex).__name__, []
            outcomes[status] += 1
            if labels:
                results[name] = {
                    "ingredient_in_converted_catalog": name,
                    "potential_catalog_records": count,
                    "local_product_verified": False,
                    "labels": labels,
                }
            if position % 75 == 0:
                print("DAILYMED PROGRESS", position, "of", len(tasks),
                      "found", len(results), flush=True)
    report = {
        "status": "OFFICIAL_US_DAILYMED_ADDITIONAL_REFERENCE_REVIEW_ONLY",
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "catalog_total": len(catalog),
        "searched_unique_ingredient_names": len(tasks),
        "candidate_ingredients_with_sources": len(results),
        "potential_catalog_records": sum(x["potential_catalog_records"] for x in results.values()),
        "outcomes": dict(outcomes),
        "all_sources_unapproved": True,
        "local_egyptian_product_identity_verified": False,
        "note": "DailyMed generic-name search is not verification of local Egyptian product, strength, route, or safety.",
        "references": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("Output already exists")
    output.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("DAILYMED FINAL SUMMARY", json.dumps({
        "searched": len(tasks),
        "with_sources": len(results),
        "potential_products": report["potential_catalog_records"],
        "statuses": outcomes,
    }, default=dict), flush=True)


if __name__ == "__main__":
    main()
