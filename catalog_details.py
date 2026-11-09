"""Catalogue-only details from the Egyptian source, not clinical advice.

Preserves the source's company *name* without pretending it is a manufacturer ID.
Only derives exact package counts from unambiguous text in the trade name.
"""
from __future__ import annotations

import re

DETAIL_EXTRA_KEYS = ("manufacturer_name", "package_description")

UNITS = {
    "tab": "tablets", "tabs": "tablets", "tablet": "tablets", "tablets": "tablets",
    "cap": "capsules", "caps": "capsules", "capsule": "capsules", "capsules": "capsules",
    "amp": "ampoules", "amps": "ampoules", "ampoule": "ampoules", "ampoules": "ampoules",
    "vial": "vials", "vials": "vials",
    "sachet": "sachets", "sachets": "sachets",
    "supp": "suppositories", "supps": "suppositories",
    "suppository": "suppositories", "suppositories": "suppositories",
    "patch": "patches", "patches": "patches",
    "pessary": "pessaries", "pessaries": "pessaries",
}
UNIT_WORDS = "|".join(sorted(map(re.escape, UNITS), key=len, reverse=True))
MULTI = re.compile(
    rf"(?<![\w.])(?P<outer>[1-9]\d{{0,2}})\s*[x×*]\s*(?P<inner>[1-9]\d{{0,2}})\s*(?P<unit>{UNIT_WORDS})\b",
    re.I,
)
SIMPLE = re.compile(
    rf"(?<![\w.])(?P<count>[1-9]\d{{0,3}})\s*(?P<unit>{UNIT_WORDS})\b",
    re.I,
)
EXPLICIT_STRIPS = re.compile(
    r"\b(?:strips?|blisters?)\b", re.I
)
# Avoid pseudo-ID creation or guessing registration/manufacturer keys.


def safe_label(value) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return str(value).strip()


def packaging_from_name(name: str):
    """Returns package fields, evidence. Unknown package size remains unknown."""
    text = safe_label(name)
    fields, evidence = {}, {}
    combos = list(MULTI.finditer(text))
    if len(combos) == 1:
        m = combos[0]
        outer, inner = int(m.group("outer")), int(m.group("inner"))
        unit = UNITS[m.group("unit").lower()]
        fields["package_description"] = f"{outer} x {inner} {unit}"
        fields["units_per_pack"] = outer * inner
        evidence["package_description"] = f"source_name:{m.group(0)}"
        evidence["units_per_pack"] = f"source_name:{m.group(0)}"
        if EXPLICIT_STRIPS.search(text):
            fields.update({"strips_per_pack": outer, "units_per_strip": inner, "is_strip_based": 1})
            for key in ("strips_per_pack", "units_per_strip", "is_strip_based"):
                evidence[key] = f"source_name:{m.group(0)}; explicit strip/blister"
        return fields, evidence

    if len(combos) > 1:
        return fields, evidence

    matches = list(SIMPLE.finditer(text))
    # Two different package counts in one name are ambiguous.
    if len(matches) == 1:
        m = matches[0]
        quantity = int(m.group("count"))
        if quantity <= 1000:
            unit = UNITS[m.group("unit").lower()]
            fields["package_description"] = f"{quantity} {unit}"
            fields["units_per_pack"] = quantity
            evidence["package_description"] = f"source_name:{m.group(0)}"
            evidence["units_per_pack"] = f"source_name:{m.group(0)}"
    return fields, evidence


def category_from_source(description: str):
    """One exact suffix in a dot-separated taxonomy, not inferred ATC class."""
    label = safe_label(description)
    parts = [p.strip() for p in label.split(".")]
    if len(parts) != 2 or any(not (2 <= len(p) <= 55) for p in parts):
        return {}, {}
    if any(c in label for c in "!?:;\n"):
        return {}, {}
    return {"pharmacologic_class": parts[1]}, {
        "pharmacologic_class": "source_description_taxonomy:" + label
    }


def derive_catalog_details(source: dict):
    """Returns 96-field overrides, optional extra display fields, audit evidence."""
    name = safe_label(source.get("name"))
    manufacturer = safe_label(source.get("company"))
    source_category = safe_label(source.get("description"))
    result, evidence = packaging_from_name(name)
    taxonomy, taxonomy_evidence = category_from_source(source_category)
    result.update(taxonomy)
    evidence.update(taxonomy_evidence)
    extras = {
        "manufacturer_name": manufacturer,
        "package_description": result.pop("package_description", ""),
    }
    if manufacturer:
        evidence["manufacturer_name"] = "source_company_exact"
    return result, extras, evidence
