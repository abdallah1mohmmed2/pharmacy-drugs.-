#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect official US label *reference candidates* for frequent single ingredients.

No US label is verified for the Egyptian marketed product. No clinical claims
are approved. Preserve primary-source text with SPL IDs, fields, and truncation
flags. The daily openFDA anonymous limit is 1,000 requests/IP (240/minute).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://api.fda.gov/drug/label.json"
MAX_QUERIES = 850
SECTIONS = (
    "indications_and_usage", "dosage_and_administration",
    "dosage_forms_and_strengths", "contraindications",
    "warnings", "warnings_and_precautions", "precautions", "boxed_warning",
    "drug_interactions", "adverse_reactions", "adverse_reactions_table",
    "pregnancy", "nursing_mothers", "lactation", "pediatric_use",
    "geriatric_use", "use_in_specific_populations", "how_supplied",
    "storage_and_handling", "information_for_patients",
    "clinical_pharmacology", "mechanism_of_action", "drug_abuse_and_dependence",
)
SALT_TERMS = (
    "hydrochloride", "sodium", "potassium", "calcium", "maleate", "fumarate",
    "besylate", "succinate", "sulfate", "mesylate", "phosphate", "acetate",
    "tartrate", "citrate", "hydrobromide", "monohydrate", "dihydrate",
)
ALIASES = {
    "paracetamol(acetaminophen)": "acetaminophen",
    "cholecalciferol(vitamin d3)": "cholecalciferol",
    "ergocalciferol(vitamin d2)": "ergocalciferol",
    "salbutamol": "albuterol",
    "adrenaline": "epinephrine",
    "noradrenaline": "norepinephrine",
}
BLOCK = (
    "formula", "multivitamin", "cosmetic", "shampoo", "sun protection",
    "herbal", "extract", "vitamins", "minerals", "fragrance",
    "compound", "essential oil",
)


def norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def single_ingredient(value):
    s = norm(value)
    if not s or any(x in s for x in ("+", "/", ",", "&", ";", " and ", " with ")):
        return ""
    if s in ALIASES:
        return ALIASES[s]
    if any(x in s for x in BLOCK):
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9 .-]{2,74}", s):
        return ""
    if len(s.split()) > 5:
        return ""
    return s


def similar_substance(query, value):
    value = norm(value)
    if query == value:
        return "EXACT_INGREDIENT_NAME"
    if value.startswith(query + " ") and value[len(query)+1:] in SALT_TERMS:
        return "SALT_FORM_REQUIRES_REVIEW"
    if query.startswith(value + " ") and query[len(value)+1:] in SALT_TERMS:
        return "SALT_FORM_REQUIRES_REVIEW"
    return ""


def match_label(ingredient, label):
    info = label.get("openfda") or {}
    raw_generic = info.get("generic_name") or []
    raw_substances = info.get("substance_name") or []
    if isinstance(raw_generic, str):
        raw_generic = [raw_generic]
    if isinstance(raw_substances, str):
        raw_substances = [raw_substances]
    generic = [norm(x) for x in raw_generic if isinstance(x, str)]
    substances = [norm(x) for x in raw_substances if isinstance(x, str)]
    if any(re.search(r"\s+(?:and|with)\s+|[+/;,]", x) for x in generic + substances):
        return None
    if len(set(substances)) > 1:
        return None
    kinds = [similar_substance(ingredient, x) for x in (generic + substances)]
    kinds = [x for x in kinds if x]
    if not kinds:
        return None
    matching = "EXACT_INGREDIENT_NAME" if "EXACT_INGREDIENT_NAME" in kinds else kinds[0]
    return matching


def snippets(label):
    fields = {}
    trunc = {}
    for key in SECTIONS:
        chunks = label.get(key) or []
        if isinstance(chunks, str):
            chunks = [chunks]
        if not isinstance(chunks, list):
            continue
        text = "\n".join(x.strip() for x in chunks if isinstance(x, str) and x.strip())
        if not text:
            continue
        trunc[key] = len(text) > 14000
        fields[key] = text[:14000] if trunc[key] else text
    return fields, {k: v for k, v in trunc.items() if v}


def get_api(ingredient, api_key, delay=0.55):
    query = f'openfda.generic_name:"{ingredient}" OR openfda.substance_name:"{ingredient}"'
    params = {"search": query, "limit": "8"}
    if api_key:
        params["api_key"] = api_key
    url = API + "?" + urlencode(params)
    for attempt in range(3):
        try:
            time.sleep(delay)
            req = Request(url, headers={"User-Agent": "DrugResearchReview/1.0 (nonclinical research)",
                                        "Accept": "application/json"})
            with urlopen(req, timeout=22) as response:
                return json.load(response), "OK"
        except HTTPError as ex:
            if ex.code == 404:
                return {}, "NO_US_LABEL_MATCH"
            if ex.code == 429 and attempt < 2:
                time.sleep(5*(attempt+1))
                continue
            if ex.code in (400, 401, 403):
                return {}, "API_HTTP_" + str(ex.code)
            if attempt == 2:
                return {}, "API_HTTP_" + str(ex.code)
            time.sleep(1+attempt)
        except (URLError, TimeoutError, ConnectionError, ValueError) as ex:
            if attempt == 2:
                return {}, "REQUEST_" + type(ex).__name__
            time.sleep(1+attempt)
    return {}, "UNKNOWN_ERROR"


def select_labels(ingredient, results):
    selected = []
    seen = set()
    for label in results:
        if not isinstance(label, dict):
            continue
        kind = match_label(ingredient, label)
        if not kind:
            continue
        info = label.get("openfda") or {}
        setid = str(label.get("set_id") or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", setid):
            continue
        text, truncated = snippets(label)
        if not text:
            continue
        if setid in seen:
            continue
        seen.add(setid)
        routes = info.get("route") or []
        if isinstance(routes, str): routes = [routes]
        brands = info.get("brand_name") or []
        if isinstance(brands, str): brands = [brands]
        generic = info.get("generic_name") or []
        if isinstance(generic, str): generic = [generic]
        selected.append({
            "set_id": setid, "source_url":
                "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=" + setid,
            "source_api": API,
            "source_market": "US",
            "effective_time": str(label.get("effective_time") or ""),
            "match_type": kind,
            "brand_names_on_US_label": brands[:5],
            "generic_names_on_US_label": generic[:5],
            "routes_on_US_label": routes[:6],
            "sections": text,
            "truncated_sections": truncated,
            "local_Egyptian_product_match_verified": False,
            "medical_use_approved": False,
        })
    selected.sort(key=lambda d: (
        d["match_type"] != "EXACT_INGREDIENT_NAME",
        -(len(d["sections"])),
        -(len(d["effective_time"])),
        d["set_id"],
    ))
    # Preserve alternative reference labels, not dozens of near-duplicates.
    return selected[:3]


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--max-queries", type=int, default=750)
    args = p.parse_args(argv)
    if not 1 <= args.max_queries <= MAX_QUERIES:
        p.error("max-queries must be 1..850 (stay under anonymous daily API cap)")
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    if not isinstance(catalog, list) or len(catalog) < 20000:
        p.error("Require full converted catalog of >=20000 items")
    by_ingredient = Counter(single_ingredient(x.get("scientific_name")) for x in catalog)
    by_ingredient.pop("", None)
    ordered = sorted(by_ingredient, key=lambda x: (-by_ingredient[x], x))[:args.max_queries]
    api_key = os.environ.get("OPENFDA_API_KEY", "")
    profiles = {}
    statuses = Counter()
    candidates = 0
    for i, ingredient in enumerate(ordered, 1):
        data, status = get_api(ingredient, api_key)
        labels = select_labels(ingredient, data.get("results", []))
        if labels:
            candidates += by_ingredient[ingredient]
        statuses[status] += 1
        if labels:
            profiles[ingredient] = {
                "ingredient": ingredient,
                "catalog_records_with_same_ingredient": by_ingredient[ingredient],
                "matching_method": "exact text or explicitly marked salt-family name",
                "approved_for_Egyptian_products": False,
                "US_label_references": labels,
            }
        if i % 50 == 0:
            print(f"PROGRESS {i}/{len(ordered)} ingredients, {len(profiles)} with labels, potential records {candidates}", flush=True)
    output = {
        "status": "OFFICIAL_US_REFERENCE_CANDIDATES_NEVER_LOCAL_PRODUCT_APPROVAL",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "api": API,
        "requested_ingredient_count": len(ordered),
        "candidate_ingredient_profiles": len(profiles),
        "catalog_drug_records_with_matching_ingredient_names": candidates,
        "request_outcomes": dict(statuses),
        "clinical_review_required": True,
        "no_clinical_approval_claimed": True,
        "source_license_and_disclaimer": "https://open.fda.gov/apis/",
        "profiles": profiles,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("Output exists - will not overwrite")
    target.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("US REFERENCE PROFILE SUMMARY:", json.dumps({
        "queried": len(ordered),
        "candidate_profiles": len(profiles),
        "potential_catalog_drugs": candidates,
        "statuses": statuses,
        "output_bytes": target.stat().st_size,
    }, default=dict), flush=True)


if __name__ == "__main__":
    main()
