"""Faithful safety sections from an openFDA drug-label RESPONSE (review only).

This module does not diagnose interactions, compute severity, or infer which
reactions are "common" or "serious". US labeling is not a verified Egyptian
product leaflet. Every extracted section retains its exact source field.
"""
from __future__ import annotations

import re
from typing import Any

# These are raw official label *sections*, not approved pharmacy fields.
SAFETY_SECTION_MAP = {
    "drug_interactions": ("drug_interactions",),
    "drug_interactions_table": ("drug_interactions_table",),
    "adverse_reactions": ("adverse_reactions",),
    "adverse_reactions_table": ("adverse_reactions_table",),
    "warnings_and_cautions": ("warnings_and_cautions", "warnings", "precautions"),
    "boxed_warning": ("boxed_warning",),
}

# Evidence of a heading or exact wording in the label only. NOT a list of
# confirmed symptoms, frequencies, severities, or patient-specific advice.
COMMON_CUES = (
    "most common adverse reactions",
    "most commonly reported adverse reactions",
    "common adverse reactions",
    "most frequently reported adverse reactions",
)
SERIOUS_CUES = (
    "serious adverse reactions",
    "serious adverse events",
    "severe adverse reactions",
    "fatal adverse reactions",
    "life-threatening adverse reactions",
)


def text_parts(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [x.strip() for x in raw if isinstance(x, str) and x.strip()]
    return []


def raw_safety_sections(label: dict) -> dict:
    """Never invent text and never label whole adverse section as 'common'."""
    if not isinstance(label, dict):
        return {}
    sections = {}
    for output_name, source_fields in SAFETY_SECTION_MAP.items():
        for field in source_fields:
            parts = text_parts(label.get(field))
            if parts:
                sections[output_name] = {
                    "source_field": field,
                    "text": "\n\n".join(parts),
                    "review_only": True,
                }
                break
    return sections


def detected_heading_cues(text: str, cues: tuple[str, ...]) -> list[str]:
    # Plain-text presence tests; do not interpret clinical frequency or seriousness.
    norm = re.sub(r"\s+", " ", text).casefold()
    return [c for c in cues if c in norm]


def safety_review_bundle(label: dict) -> dict:
    sections = raw_safety_sections(label)
    adverse = sections.get("adverse_reactions", {}).get("text", "")
    warnings = sections.get("warnings_and_cautions", {}).get("text", "")
    boxed = sections.get("boxed_warning", {}).get("text", "")
    serious_text = "\n".join([adverse, warnings, boxed])
    return {
        "sections": sections,
        "review_targets": {
            "interactions_summary": {
                "label_field": "drug_interactions",
                "available": "drug_interactions" in sections or "drug_interactions_table" in sections,
                "interpretation": "Keep full text; product applicability and interaction severity require pharmacist review",
            },
            "side_effects_common": {
                "label_field": "adverse_reactions",
                "available": "adverse_reactions" in sections or "adverse_reactions_table" in sections,
                "wording_cues": detected_heading_cues(adverse, COMMON_CUES),
                "interpretation": "Common frequency cannot be inferred from a general adverse_reactions section",
            },
            "side_effects_serious": {
                "label_field": "adverse_reactions / warnings_and_cautions / boxed_warning",
                "available": any(k in sections for k in (
                    "adverse_reactions", "adverse_reactions_table",
                    "warnings_and_cautions", "boxed_warning",
                )),
                "wording_cues": detected_heading_cues(serious_text, SERIOUS_CUES),
                "interpretation": "Not all warnings are adverse effects; pharmacist must select serious reactions",
            },
        },
        "auto_populated_pharmacy_fields": [],
        "review_required": True,
        "product_verified_for_egypt": False,
    }
