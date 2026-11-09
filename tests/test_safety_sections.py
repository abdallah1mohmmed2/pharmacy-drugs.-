import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from safety_sections import raw_safety_sections, safety_review_bundle
from clinical_candidates import collect
from apply_reviewed_medical import apply_reviews
from convert import load_schema


EXAMPLE_LABEL = {
    "openfda": {
        "generic_name": ["metformin hydrochloride"],
        "substance_name": ["metformin hydrochloride"],
        "brand_name": ["GLUCOPHAGE"],
        "route": ["ORAL"],
    },
    "set_id": "11111111-1111-1111-1111-111111111111",
    "effective_time": "20260630",
    "dosage_forms_and_strengths": ["500 mg tablet"],
    "drug_interactions": [
        "Full label Drug Interactions section (example only). "
        "Contains details requiring exact product and professional verification."
    ],
    "adverse_reactions": [
        "Most common adverse reactions include symptoms described in the "
        "product label. Serious adverse reactions are reviewed elsewhere."
    ],
    "adverse_reactions_table": ["Example trial-frequency table, not classified."],
    "warnings_and_cautions": [
        "Warnings and precautions. Not every warning is an adverse reaction."
    ],
    "boxed_warning": ["Example boxed safety warning."]
}

RECORD = {
    **load_schema(),
    "id": 123,
    "uuid": "unique-product-123",
    "trade_name_en": "Glucophage 500mg tablets",
    "scientific_name": "metformin hydrochloride",
    "strength_text": "500mg",
    "dosage_form": "TABLET",
    "route": "ORAL",
    "is_active": 0,
    "interactions_summary": "",
    "side_effects_common": "",
    "side_effects_serious": "",
}


class FakeFDA:
    def rxnorm(self, ingredient):
        return ["860975"]
    def fda(self, ingredient):
        return [copy.deepcopy(EXAMPLE_LABEL)]


class SafetySectionsTests(unittest.TestCase):
    def test_preserves_full_raw_label_sections_with_provenance(self):
        out = raw_safety_sections(EXAMPLE_LABEL)
        self.assertEqual(out["drug_interactions"]["source_field"], "drug_interactions")
        self.assertEqual(out["adverse_reactions"]["source_field"], "adverse_reactions")
        self.assertIn("Most common adverse reactions", out["adverse_reactions"]["text"])
        self.assertIn("Example trial-frequency table", out["adverse_reactions_table"]["text"])
        self.assertEqual(out["boxed_warning"]["source_field"], "boxed_warning")
        self.assertTrue(all(s["review_only"] for s in out.values()))

    def test_reaction_section_never_automatically_classified(self):
        result = safety_review_bundle(EXAMPLE_LABEL)
        self.assertEqual(result["auto_populated_pharmacy_fields"], [])
        self.assertTrue(result["review_required"])
        self.assertFalse(result["product_verified_for_egypt"])
        targets = result["review_targets"]
        self.assertTrue(targets["interactions_summary"]["available"])
        self.assertTrue(targets["side_effects_common"]["available"])
        self.assertIn("most common adverse reactions",
                      targets["side_effects_common"]["wording_cues"])
        self.assertTrue(targets["side_effects_serious"]["available"])
        self.assertIn("serious adverse reactions",
                      targets["side_effects_serious"]["wording_cues"])

    def test_no_reactions_means_no_invented_sections(self):
        bundle = safety_review_bundle({"drug_interactions": ["Source interaction section"]})
        self.assertEqual(set(bundle["sections"]), {"drug_interactions"})
        self.assertFalse(bundle["review_targets"]["side_effects_common"]["available"])
        self.assertFalse(bundle["review_targets"]["side_effects_serious"]["available"])

    def test_tables_without_text_still_available_for_review(self):
        bundle = safety_review_bundle({"adverse_reactions_table": ["Table content"]})
        self.assertEqual(bundle["sections"]["adverse_reactions_table"]["text"], "Table content")
        self.assertTrue(bundle["review_targets"]["side_effects_common"]["available"])
        self.assertFalse(bundle["review_targets"]["side_effects_common"]["wording_cues"])

    def test_invalid_mixed_data_skips_non_text_items(self):
        out = raw_safety_sections({"adverse_reactions": [None, False, 1, "  valid label text \n  "]})
        self.assertEqual(out["adverse_reactions"]["text"], "valid label text")

    def test_proposal_and_review_template_maintain_separation(self):
        from label_api import propose
        result = propose(copy.deepcopy(RECORD), FakeFDA(), 110)
        self.assertIn("safety_review", result)
        self.assertIn("drug_interactions", result["safety_review"]["sections"])
        self.assertIn("adverse_reactions", result["safety_review"]["sections"])
        self.assertNotIn("side_effects_common", result["suggestions"])
        self.assertNotIn("side_effects_serious", result["suggestions"])
        self.assertIn("interactions_summary", result["reference_fields"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = copy.deepcopy(RECORD)
            stats = collect([RECORD], FakeFDA(), 0, 1, root)
            self.assertEqual(RECORD, original)
            self.assertEqual(sum(stats.values()), 1)
            summary = json.loads((root / "clinical_label_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["safety_label_coverage"]["interaction_sources"], 1)
            self.assertEqual(summary["safety_label_coverage"]["adverse_sources"], 1)
            templates = json.loads((root / "medical_safety_review_TEMPLATE.json").read_text(encoding="utf-8"))
            self.assertEqual(len(templates), 1)
            self.assertFalse(templates[0]["approved"])
            self.assertEqual(templates[0]["id"], 123)
            self.assertEqual(templates[0]["uuid"], RECORD["uuid"])
            with self.assertRaisesRegex(ValueError, "approved=true"):
                apply_reviews([RECORD], templates)

    def test_all_three_reviewed_fields_can_be_merged_after_manual_approval(self):
        review = {
            **{k: RECORD[k] for k in (
                "id", "uuid", "trade_name_en", "scientific_name",
                "strength_text", "dosage_form", "route")},
            "approved": True,
            "reviewed_by": "Authorized pharmacist",
            "review_date": date.today().isoformat(),
            "source_url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=11111111-1111-1111-1111-111111111111",
            "reviewed_fields": {
                "interactions_summary": "Reviewed label interaction text for this exact product.",
                "side_effects_common": "Reviewed most-common trial-reaction summary for exact product.",
                "side_effects_serious": "Reviewed clinically significant adverse reactions for exact product.",
            },
        }
        original = copy.deepcopy(RECORD)
        reviewed = apply_reviews([RECORD], [review])
        self.assertEqual(RECORD, original)
        self.assertEqual(len(reviewed[0]), len(RECORD))
        self.assertEqual(reviewed[0]["is_active"], 0)
        for field, text in review["reviewed_fields"].items():
            self.assertEqual(reviewed[0][field], text)


if __name__ == "__main__":
    unittest.main()
