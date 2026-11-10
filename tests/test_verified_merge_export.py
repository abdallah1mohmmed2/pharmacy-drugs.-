import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from convert import load_schema
from verified_merge_export import merge_checked, export_verified, main


def drug(rid=1, *, extended=True):
    r = load_schema().copy()
    r.update({
        "id": rid, "uuid": f"catalog-uuid-{rid}",
        "trade_name_en": f"Brand {rid} 10mg tablets",
        "trade_name_ar": "اسم تجاري", "scientific_name": "verified ingredient",
        "strength_text": "10mg", "dosage_form": "TABLET", "route": "ORAL",
        "is_active": 0,
    })
    if extended:
        r.update({"manufacturer_name": "Example Manufacturer",
                  "package_description": "30 tablets"})
    return r


def research_record(record):
    item = {k: record[k] for k in (
        "id", "uuid", "trade_name_en", "trade_name_ar", "scientific_name",
        "strength_text", "dosage_form", "route"
    )}
    item.update({
        "manufacturer_name": record.get("manufacturer_name", ""),
        "clinical_use_allowed": False,
        "review_required": True,
        "auto_merged_into_json_batches": False,
        "fda_label_is_verified_egyptian_product": False,
        "status": "ingredient_reference_only",
        "source": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc",
        "source_set_id": "abc",
        "potential_strict_match": False,
        "reference_fields": {
            "contraindications": "Raw US reference requiring local product review.",
            "interactions_summary": "Raw US interaction section requiring review.",
        },
        "suggestions": {},
        "safety_review": {
            "sections": {
                "adverse_reactions": {
                    "source_field": "adverse_reactions",
                    "text": "Mixed effects, not automatically classified.",
                    "review_only": True,
                }
            }
        },
    })
    return item


def report(records):
    return {
        "status": "RESEARCH_ONLY_NOT_CLINICALLY_APPROVED",
        "clinical_use_allowed": False,
        "drugs": [research_record(r) for r in records],
    }


def approval(record):
    item = {k: record[k] for k in (
        "id", "uuid", "trade_name_en", "scientific_name",
        "strength_text", "dosage_form", "route",
    )}
    item.update({
        "approved": True,
        "exact_local_product_label_verified": True,
        "reviewed_by": "Independent pharmacist reviewer",
        "review_date": date.today().isoformat(),
        "source_url": "https://example.org/approved-local-product-leaflet",
        "reviewed_fields": {
            "contraindications": "Professionally reviewed contraindications from exact local product leaflet.",
            "interactions_summary": "Professionally reviewed interactions for exact local product.",
            "side_effects_common": "Reviewed commonly reported effects for exact local product.",
            "side_effects_serious": "Reviewed serious adverse reactions for exact local product.",
        },
    })
    return item


class VerifiedMergeTests(unittest.TestCase):
    def test_98_field_merge_preserves_all_other_values(self):
        rows = [drug(1), drug(2)]
        snapshot = copy.deepcopy(rows)
        merged, comparison, audit = merge_checked(rows, report([rows[0]]), [approval(rows[0])])
        self.assertEqual(rows, snapshot)
        self.assertEqual(len(merged[0]), 98)
        self.assertEqual(merged[1], rows[1])
        self.assertEqual(merged[0]["trade_name_en"], rows[0]["trade_name_en"])
        self.assertEqual(merged[0]["uuid"], rows[0]["uuid"])
        self.assertEqual(merged[0]["manufacturer_name"], rows[0]["manufacturer_name"])
        self.assertEqual(merged[0]["is_active"], 0)
        self.assertEqual(merged[0]["contraindications"], approval(rows[0])["reviewed_fields"]["contraindications"])
        self.assertEqual(merged[0]["side_effects_common"], approval(rows[0])["reviewed_fields"]["side_effects_common"])
        self.assertEqual(audit["approved_drugs"], 1)
        self.assertEqual(audit["merged_records"], 2)
        self.assertEqual(audit["changed_fields"]["side_effects_serious"], 1)
        self.assertNotEqual(audit["catalog_sha256_before"], audit["merged_sha256_after"])
        self.assertFalse(audit["reviewer_identity_authenticated_by_program"])
        self.assertFalse(comparison["automatic_merge_allowed"])

    def test_96_schema_also_supported(self):
        row=drug(1,extended=False)
        merged, _, _ = merge_checked([row],report([row]),[approval(row)])
        self.assertEqual(len(merged[0]),96)
        self.assertEqual(set(merged[0]),set(row))

    def test_unapproved_and_empty_rejected(self):
        row=drug()
        with self.assertRaisesRegex(ValueError,"At least one"):
            merge_checked([row],report([row]),[])
        proposal=approval(row)
        proposal["approved"]=False
        with self.assertRaisesRegex(ValueError,"approved=true"):
            merge_checked([row],report([row]),[proposal])

    def test_exact_local_label_gate_required(self):
        row=drug()
        proposal=approval(row)
        proposal["exact_local_product_label_verified"]=False
        with self.assertRaisesRegex(ValueError,"exact_local_product_label_verified"):
            merge_checked([row],report([row]),[proposal])

    def test_forged_identity_mismatch_rejected(self):
        row=drug()
        researched=report([row])
        researched["drugs"][0]["uuid"]="wrong-uuid"
        with self.assertRaisesRegex(ValueError,"absent from"):
            merge_checked([row],researched,[approval(row)])

    def test_unresearched_drug_cannot_be_updated(self):
        rows=[drug(1),drug(2)]
        with self.assertRaisesRegex(ValueError,"absent from"):
            merge_checked(rows,report([rows[0]]),[approval(rows[1])])

    def test_field_without_source_or_approved_comparison_rejected(self):
        row=drug()
        proposed=approval(row)
        proposed["reviewed_fields"]={"dosage_administration":"Unreviewed dose without supporting source"}
        with self.assertRaisesRegex(ValueError,"not available or in conflict"):
            merge_checked([row],report([row]),[proposed])

    def test_conflicting_existing_text_never_overwritten(self):
        row=drug()
        row["contraindications"]="Existing local validated contraindication"
        with self.assertRaisesRegex(ValueError,"not available or in conflict"):
            merge_checked([row],report([row]),[approval(row)])

    def test_mismatched_strength_never_copied(self):
        row=drug()
        proposed=approval(row)
        proposed["strength_text"]="20mg"
        with self.assertRaisesRegex(ValueError,"identity mismatch"):
            merge_checked([row],report([row]),[proposed])

    def test_duplicate_approvals_rejected(self):
        row=drug()
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            merge_checked([row],report([row]),[approval(row),approval(row)])

    def test_no_sensitive_numeric_or_boolean_flags(self):
        row=drug()
        for name in ("is_antibiotic","has_critical_interactions","adult_dose_min_mg","stock_total_units"):
            proposed=approval(row)
            proposed["reviewed_fields"]={name:"1"}
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,"not available or in conflict"):
                merge_checked([row],report([row]),[proposed])

    def test_end_to_end_301_drugs_two_json_files_only(self):
        rows=[drug(x) for x in range(1,302)]
        evidence=report([rows[1]])
        approved=[approval(rows[1])]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a=root/"catalog.json"
            b=root/"research.json"
            c=root/"approved.json"
            output=root/"verified"
            a.write_text(json.dumps(rows,ensure_ascii=False),encoding="utf-8")
            b.write_text(json.dumps(evidence,ensure_ascii=False),encoding="utf-8")
            c.write_text(json.dumps(approved,ensure_ascii=False),encoding="utf-8")
            orig=a.read_bytes()
            audit=export_verified(a,b,c,output)
            self.assertEqual(a.read_bytes(),orig)
            files=sorted((output/"json_batches").iterdir())
            self.assertEqual([f.name for f in files],["batch_01.json","batch_02.json"])
            contents=[json.loads(f.read_text(encoding="utf-8")) for f in files]
            self.assertEqual([len(x) for x in contents],[300,1])
            flattened=contents[0]+contents[1]
            self.assertEqual(len({r["id"] for r in flattened}),301)
            self.assertEqual(len({r["uuid"] for r in flattened}),301)
            self.assertEqual(flattened[1]["contraindications"], approved[0]["reviewed_fields"]["contraindications"])
            self.assertEqual(flattened[0],rows[0])
            self.assertEqual(flattened[2:],rows[2:])
            self.assertEqual(audit["approved_drugs"],1)
            self.assertTrue((output/"verified_merge_audit.json").is_file())
            self.assertTrue((output/"merged_drugs_DRAFT.json").is_file())
            self.assertTrue(all(x.suffix==".json" for x in files))
            self.assertFalse((output/"json_batches"/"verified_merge_audit.json").exists())
            self.assertEqual(main(["--catalog",str(a),"--research",str(b),
                                   "--approvals",str(c),"--output-dir",str(output)]),1)

    def test_atomic_export_rejects_no_approvals_without_creating_any_output(self):
        row=drug()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,content in (
                ("catalog.json",[row]),("research.json",report([row])),
                ("approvals.json",[])
            ):
                (root/name).write_text(json.dumps(content,ensure_ascii=False),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"At least one"):
                export_verified(root/"catalog.json",root/"research.json",root/"approvals.json",root/"out")
            self.assertFalse((root/"out").exists())


if __name__ == "__main__":
    unittest.main()
