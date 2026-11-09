#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only search for clinical LABEL proposals (never writes to drug records).

Sources: openFDA drug label, RxNorm. Never use draft output for clinical decisions
without qualified local verification. No original input is modified.
Python 3.9+; standard library only.
"""
import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from safety_sections import safety_review_bundle

FDA_URL = "https://api.fda.gov/drug/label.json"
RXNORM_URL = "https://rxnav.nlm.nih.gov/REST/rxcui.json"

# These are free text descriptions, NOT clinical decision-making flags or dose limits.
FIELD_MAP = {
    "indications": ("indications_and_usage",),
    "dosage_administration": ("dosage_and_administration",),
    "contraindications": ("contraindications",),
    "warnings_precautions": ("warnings_and_cautions", "warnings", "precautions"),
    "black_box_warning": ("boxed_warning",),
    "interactions_summary": ("drug_interactions",),
    # "adverse_reactions" combines frequencies and serious events; NEVER
    # misclassify its entire section as side_effects_common.
    "pregnancy_notes": ("pregnancy", "pregnancy_or_breast_feeding"),
    "lactation_notes": ("lactation", "nursing_mothers"),
    "pediatric_notes": ("pediatric_use",),
    "geriatric_notes": ("geriatric_use",),
    "storage_conditions": ("storage_and_handling",),
    "pharmacologic_class": ("openfda.pharm_class_epc",),
}

# A strong labelling difference (salt / ester) is never silently declared identical.
SALT_WORDS = set("hydrochloride hcl sodium potassium calcium magnesium phosphate sulfate sulphate citrate acetate bromide mesylate besylate succinate fumarate maleate tartrate hydrate monohydrate dihydrate trihydrate".split())


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def squish(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized(value):
    return squish(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()))


def without_salt(value):
    return " ".join(tok for tok in normalized(value).split() if tok not in SALT_WORDS)


def simple_ingredient(value):
    s = squish(value)
    # Multi ingredient, explanatory paragraphs, and incomplete/unknown entries
    # cannot be matched reliably using a single-ingredient US label.
    if not s or len(s) > 85 or any(x in s for x in ("+", ",", ";", "/", "(", ")")):
        return ""
    return s


def product_brand(name):
    raw = str(name or "").casefold()
    raw = re.split(r"(?<![a-z])\d+(?:[.,]\d+)?", raw, maxsplit=1)[0]
    raw = re.sub(r"\b(tablets?|capsules?|ampoules?|vials?|drops?|injection|syrup|cream|tab|caps|ml|gm|mg)\b", " ", raw)
    return normalized(raw)


def expected_routes(name):
    text = str(name or "").casefold()
    if "eye/ear" in text or "eye / ear" in text:
        return {"OPHTHALMIC", "OTIC"}
    if "eye" in text or "ophthalmic" in text:
        return {"OPHTHALMIC"}
    if "ear" in text or "otic" in text:
        return {"OTIC"}
    if "sublingual" in text:
        return {"SUBLINGUAL"}
    if "vaginal" in text:
        return {"VAGINAL"}
    if "nasal" in text:
        return {"NASAL"}
    if "mouth" in text:
        return {"ORAL", "OROMUCOSAL"}
    if any(t in text for t in ("cream", "lotion", "topical")):
        return {"TOPICAL"}
    if any(t in text for t in ("tablet", " tab", " caps", "capsule", "syrup", "oral susp")):
        return {"ORAL"}
    return set()


def strength_token(value):
    text = str(value or "").casefold().replace(",", ".")
    m = re.search(r"(?<!\w)(\d+(?:\.\d+)?)\s*(mg|mcg|µg|ug|iu|i\.u\.|%)", text)
    if not m:
        return ""
    num = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
    unit = m.group(2).replace("i.u.", "iu").replace("ug", "mcg").replace("µg", "mcg")
    return num + unit


def numeric_strength_in_label(strength, label):
    if not strength:
        return False
    # Only examine sections that describe product strength, not dose guidance.
    keys = ("dosage_forms_and_strengths", "active_ingredient", "package_label_principal_display_panel")
    blocks = " ".join(" ".join(str(v) for v in as_list(label.get(k))) for k in keys)
    compact = blocks.casefold().replace(",", ".")
    compact = compact.replace("i.u.", "iu").replace("µg", "mcg").replace("ug", "mcg")
    compact = re.sub(r"\s+", "", compact)
    return strength in compact


def nonempty(value):
    return value not in (None, "", [], {}, "UNKNOWN", "null") and str(value).strip() not in ("", "UNKNOWN")


def label_text(label, keys):
    for key in keys:
        arr = as_list(label.get(key))
        text = "\n\n".join(squish(x) for x in arr if squish(x))
        if text:
            # Do not truncate drug warnings, contraindications or dosage text.
            return text
    return ""


def classify_label(target, label):
    of = label.get("openfda") or {}
    if not isinstance(of, dict):
        return None
    src_generic = [normalized(x) for x in as_list(of.get("generic_name")) if x]
    substances = [normalized(x) for x in as_list(of.get("substance_name")) if x]
    # Reject any label explicitly listing more than one active ingredient.
    if len(substances) > 1 or any(any(sep in s for sep in (" and ", " + ", ";", " / ")) for s in src_generic + substances):
        return None
    ingredient = normalized(simple_ingredient(target.get("scientific_name")))
    if not ingredient:
        return None
    names = list(dict.fromkeys(src_generic + substances))
    if not names:
        return None
    if ingredient in names:
        generic_score, identity = 70, "exact_ingredient"
    elif any(without_salt(x) == without_salt(ingredient) and without_salt(x) for x in names):
        generic_score, identity = 50, "possible_salt_variant"
    else:
        return None

    trade_brand = product_brand(target.get("trade_name_en"))
    brands = [normalized(b) for b in as_list(of.get("brand_name"))]
    brand_match = bool(trade_brand and trade_brand in brands)
    routes = {squish(x).upper() for x in as_list(of.get("route") or label.get("route"))}
    expected = expected_routes(target.get("trade_name_en"))
    route_conflict = bool(expected and routes and not expected.intersection(routes))
    route_match = bool(expected and routes and expected.intersection(routes))
    s = strength_token(target.get("trade_name_en") or target.get("strength_text"))
    strength_match = numeric_strength_in_label(s, label)
    score = generic_score + (20 if brand_match else 0) + (10 if route_match else 0) + (10 if strength_match else 0)
    if route_conflict:
        score -= 30  # Can only be an ingredient-level reference, never a product label
    return {"score": score, "identity": identity, "brand_match": brand_match,
            "route_match": route_match, "route_conflict": route_conflict,
            "strength_match": strength_match,
            "label": label, "effective_time": str(label.get("effective_time", ""))}


class APIClient:
    def __init__(self, cache_dir, api_key="", delay=0.4):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.delay = delay
        self.last_request = 0.0

    def get(self, url, params):
        cache_params = dict(params)
        canonical = url + "?" + urlencode(sorted(cache_params.items()))
        fname = self.cache_dir / (hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".json")
        if fname.exists():
            try:
                return json.loads(fname.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        params = dict(params)
        if url == FDA_URL and self.api_key:
            params["api_key"] = self.api_key
        full_url = url + "?" + urlencode(params)
        for attempt in range(4):
            try:
                elapsed = time.monotonic() - self.last_request
                if elapsed < self.delay:
                    time.sleep(self.delay - elapsed)
                self.last_request = time.monotonic()
                req = Request(full_url, headers={"User-Agent": "DrugCatalogDraftEnricher/1.0 (research catalog)",
                                                 "Accept": "application/json"})
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                fname.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
            except HTTPError as exc:
                if exc.code == 404:
                    fname.write_text("null", encoding="utf-8")
                    return None
                if exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
                if attempt == 3:
                    raise RuntimeError(f"HTTP {exc.code} after retry from {url}") from exc
                retry_after = exc.headers.get("Retry-After", "")
                wait = min(float(retry_after), 30) if retry_after.isdigit() else 2 ** (attempt + 1)
                time.sleep(wait)
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"API connection failed: {type(exc).__name__}: {exc}") from exc
                time.sleep(2 ** (attempt + 1))
        return None

    def rxnorm(self, ingredient):
        payload = self.get(RXNORM_URL, {"name": ingredient, "search": "2"}) or {}
        return (payload.get("idGroup") or {}).get("rxnormId") or []

    def fda(self, ingredient):
        # Search only by active ingredient name; subsequently verify every candidate.
        for field in ("openfda.generic_name", "openfda.substance_name"):
            payload = self.get(FDA_URL, {"search": f'{field}:"{ingredient}"', "limit": "20"})
            results = (payload or {}).get("results") or []
            if results:
                return results
        return []


def propose(record, api, minimum):
    if record.get("is_deleted") == 1:
        return {"id": record.get("id", ""), "status": "skip_deleted", "suggestions": {}, "note": "soft-deleted record"}
    ingredient = simple_ingredient(record.get("scientific_name"))
    brand = record.get("trade_name_en", "")
    meta = {"id": record.get("id", ""), "trade_name_en": brand,
            "scientific_name": record.get("scientific_name", ""), "status": "", "score": 0,
            "rxnorm_ids": [], "source": "", "selected_fields": [], "suggestions": {},
            "reference_fields": {}, "unclassified_label_sections": {}, "safety_review": {}, "note": ""}
    if not ingredient:
        meta["status"] = "skip_complex_or_missing_ingredient"
        return meta
    try:
        meta["rxnorm_ids"] = api.rxnorm(ingredient)
    except RuntimeError as exc:
        meta["note"] = f"RxNorm unavailable: {exc}"
    try:
        labels = api.fda(ingredient)
    except RuntimeError as exc:
        meta["status"] = "api_error"
        meta["note"] += f"; openFDA unavailable: {exc}"
        return meta
    candidates = [x for l in labels if (x := classify_label(record, l))]
    if not candidates:
        meta["status"] = "no_verified_candidate"
        return meta
    candidates.sort(key=lambda x: (x["score"], x["effective_time"]), reverse=True)
    chosen = candidates[0]
    label = chosen["label"]
    meta["score"] = chosen["score"]
    meta["identity"] = chosen["identity"]
    meta["brand_match"] = chosen["brand_match"]
    meta["route_match"] = chosen["route_match"]
    meta["route_conflict"] = chosen["route_conflict"]
    meta["strength_match"] = chosen["strength_match"]
    meta["label_effective_time"] = chosen["effective_time"]
    openfda = label.get("openfda") or {}
    meta["us_label_brand_names"] = as_list(openfda.get("brand_name"))
    meta["us_label_generic_names"] = as_list(openfda.get("generic_name"))
    meta["us_label_routes"] = as_list(openfda.get("route") or label.get("route"))
    meta["same_score_candidates"] = sum(1 for x in candidates if x["score"] == chosen["score"])
    set_id = str(label.get("set_id", ""))
    meta["source_set_id"] = set_id
    meta["source"] = ("https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=" + set_id
                      if set_id else "https://open.fda.gov/apis/drug/label/")
    if chosen["identity"] != "exact_ingredient":
        meta["status"] = "match_needs_manual_verification"
        return meta
    # Preserve the complete source-label interaction and reaction sections
    # with field-level provenance, but never classify the reaction list.
    meta["safety_review"] = safety_review_bundle(label)
    for dest, src_fields in FIELD_MAP.items():
        if not nonempty(record.get(dest)):
            proposal = ("; ".join(map(squish, as_list((label.get("openfda") or {}).get("pharm_class_epc"))))
                        if dest == "pharmacologic_class" else label_text(label, src_fields))
            if proposal:
                # Even exact ingredients may be different formulations/markets. The
                # reference is only an English-language research aid, NOT drug data.
                meta["reference_fields"][dest] = proposal
    adverse_text = label_text(label, ("adverse_reactions",))
    if adverse_text:
        meta["unclassified_label_sections"]["adverse_reactions"] = adverse_text
    # No data are automatically inserted into side_effects_common or
    # side_effects_serious: the label section can include both categories.
    if chosen["score"] >= minimum and not chosen["route_conflict"]:
        meta["suggestions"] = dict(meta["reference_fields"])
        meta["selected_fields"] = list(meta["suggestions"])
        meta["status"] = "draft_suggestions" if meta["suggestions"] else "matched_nothing_missing"
    else:
        meta["status"] = "ingredient_reference_only" if (meta["reference_fields"] or meta["unclassified_label_sections"]) else "matched_nothing_missing"
    return meta
