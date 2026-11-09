import copy
import json
from pathlib import Path
import tempfile
import unittest

from apply_reviewed_medical import apply_reviews
from compare_research import compare, catalog_index, main
from convert import load_schema


def medicine(id=1, extended=True):
    row=load_schema().copy()
    row.update({
        "id":id, "uuid":f"stable-{id}", "trade_name_en":f"Concor {id}0mg tablets",
        "trade_name_ar":"كونكور", "scientific_name":"bisoprolol fumarate",
        "strength_text":f"{id}0mg", "dosage_form":"TABLET", "route":"ORAL", "is_active":0,
        "indications":"Hypertension",
        "pharmacologic_class":"beta blocker",
    })
    if extended:
        row["manufacturer_name"]="Example Pharma"
        row["package_description"]="30 tablets"
    return row


def source(row, *, status="ingredient_reference_only", strict=False, references=None):
    return {
        "id":row["id"], "uuid":row["uuid"],
        "trade_name_en":row["trade_name_en"], "trade_name_ar":row["trade_name_ar"],
        "scientific_name":row["scientific_name"],
        "strength_text":row["strength_text"],
        "dosage_form":row["dosage_form"], "route":row["route"],
        "manufacturer_name":row.get("manufacturer_name",""),
        "status":status, "identity":"exact_ingredient", "potential_strict_match":strict,
        "clinical_use_allowed":False, "review_required":True,
        "fda_label_is_verified_egyptian_product":False,
        "auto_merged_into_json_batches":False,
        "source":"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=sample",
        "source_set_id":"sample",
        "reference_fields":references or {
            "indications":"Hypertension",
            "contraindications":"Source contraindications for pharmacist review only",
            "dosage_administration":"Source dose guidance pending verification",
            "interactions_summary":"Full interaction section from US label",
        },
        "suggestions":{},
        "safety_review":{
            "sections":{
                "adverse_reactions":{"source_field":"adverse_reactions",
                                     "text":"Serious and common adverse reactions appear together",
                                     "review_only":True}
            }
        },
    }


def search_result(rows):
    return {"clinical_use_allowed":False, "status":"RESEARCH_ONLY_NOT_CLINICALLY_APPROVED",
            "drugs":rows}


class ComparisonTests(unittest.TestCase):
    def test_exact_identity_and_field_level_comparison(self):
        catalog=[medicine(1),medicine(2)]
        snap=copy.deepcopy(catalog)
        data=search_result([source(catalog[0]),source(catalog[1],strict=True)])
        result, templates=compare(catalog,data)
        self.assertEqual(catalog,snap)
        self.assertEqual(result["catalog_records"],2)
        self.assertEqual(result["compared_records"],2)
        self.assertFalse(result["automatic_merge_allowed"])
        self.assertEqual([x["id"] for x in result["results"]],[1,2])
        self.assertEqual(result["counts"]["TECHNICAL_MATCH_STILL_REVIEW_REQUIRED"],1)
        self.assertEqual(result["counts"]["REFERENCE_ONLY_NOT_LOCAL_PRODUCT_MATCH"],1)
        self.assertEqual(len(templates),2)
        for template in templates:
            self.assertFalse(template["approved"])
            self.assertTrue(all(x == "" for x in template["reviewed_fields"].values()))
        fields={x["field"]:x for x in result["results"][0]["field_comparisons"]}
        self.assertEqual(fields["indications"]["comparison_status"],"EXACT_TEXT_ALREADY_PRESENT")
        self.assertEqual(fields["interactions_summary"]["comparison_status"],"MISSING_REQUIRES_REVIEW")
        self.assertEqual(fields["contraindications"]["comparison_status"],"MISSING_REQUIRES_REVIEW")
        self.assertEqual(result["results"][0]["unclassified_adverse_effects"]["adverse_reactions"]["classification"],
                         "UNCLASSIFIED_MIXED_EFFECTS")
        with self.assertRaisesRegex(ValueError,"approved=true"):
            apply_reviews(catalog,templates)

    def test_identity_mismatch_rejected_without_any_references_in_output(self):
        for field,value in (
            ("uuid","wrong"), ("scientific_name","different"),
            ("strength_text","100mg"), ("dosage_form","CAPSULE"),
            ("route","TOPICAL"), ("trade_name_en","Other trade"),
            ("trade_name_ar","غير"), ("manufacturer_name","Unknown Company"),
        ):
            row=medicine()
            r=source(row)
            r[field]=value
            report, templates=compare([row],search_result([r]))
            with self.subTest(field=field):
                self.assertEqual(report["results"][0]["comparison_status"],"REJECTED_IDENTITY_MISMATCH")
                self.assertEqual(report["results"][0]["field_comparisons"],[])
                self.assertEqual(templates,[])
                self.assertIn(field,report["results"][0]["identity_mismatches"])

    def test_98_and_96_compatible_without_invented_extra_fields(self):
        row=medicine(1,extended=False)
        result, templates=compare([row],search_result([source(row)]))
        self.assertEqual(result["compared_records"],1)
        self.assertEqual(len(row),96)
        self.assertNotIn("manufacturer_name_if_present",row)
        self.assertEqual(templates[0]["uuid"],row["uuid"])

    def test_duplicate_ids_in_catalog_rejected(self):
        one=medicine(1)
        two=dict(one,uuid="new")
        with self.assertRaisesRegex(ValueError,"duplicate catalog id"):
            compare([one,two],search_result([]))

    def test_duplicate_ids_in_research_rejected(self):
        row=medicine()
        with self.assertRaisesRegex(ValueError,"Duplicate research result"):
            compare([row],search_result([source(row),source(row)]))

    def test_unknown_product_rejected(self):
        with self.assertRaisesRegex(ValueError,"not in the converted catalog"):
            compare([medicine(1)],search_result([source(medicine(2))]))

    def test_unsafe_reference_fields_rejected(self):
        row=medicine()
        for field in ("adult_dose_min_mg","has_critical_interactions","stock_total_units"):
            r=source(row,references={field:"999"})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,"unsafe candidate field"):
                compare([row],search_result([r]))

    def test_research_suggestion_can_never_disagree_with_provenance(self):
        row=medicine()
        r=source(row)
        r["suggestions"]={"interactions_summary":"invented unsourced medical claim"}
        with self.assertRaisesRegex(ValueError,"differs from reference"):
            compare([row],search_result([r]))

    def test_no_clinical_approval_allowed(self):
        row=medicine()
        for flag,bad in [
            ("clinical_use_allowed",True),
            ("review_required",False),
            ("auto_merged_into_json_batches",True),
            ("fda_label_is_verified_egyptian_product",True)
        ]:
            r=source(row)
            r[flag]=bad
            with self.subTest(flag=flag),self.assertRaisesRegex(ValueError,"reference-only and unapproved"):
                compare([row],search_result([r]))

    def test_existing_values_never_overwritten_by_different_research(self):
        row=medicine()
        row["contraindications"]="Locally reviewed leaflet text"
        r=source(row)
        report, templates=compare([row],search_result([r]))
        fields={f["field"]:f for f in report["results"][0]["field_comparisons"]}
        self.assertEqual(fields["contraindications"]["comparison_status"],"CONFLICT_REQUIRES_REVIEW")
        self.assertEqual(row["contraindications"],"Locally reviewed leaflet text")
        self.assertNotIn("contraindications",templates[0]["reviewed_fields"])

    def test_missing_external_evidence_no_implied_medical_safety(self):
        row=medicine()
        r=source(row,references={})
        r["reference_fields"]={}
        r["safety_review"]={"sections":{}}
        report, templates=compare([row],search_result([r]))
        self.assertEqual(report["results"][0]["comparison_status"],"NO_RELEVANT_EXTERNAL_EVIDENCE")
        self.assertEqual(templates,[])

    def test_cli_json_only_no_source_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            catalog=[medicine()]
            catalog_path=base/"converted.json"
            research_path=base/"research.json"
            catalog_path.write_text(json.dumps(catalog,ensure_ascii=False),encoding="utf-8")
            research_path.write_text(json.dumps(search_result([source(catalog[0])]),ensure_ascii=False),encoding="utf-8")
            saved=catalog_path.read_bytes()
            output=base/"comparison.json"
            template=base/"template.json"
            code=main(["--catalog",str(catalog_path),"--research",str(research_path),
                       "--output",str(output),"--review-template",str(template)])
            self.assertEqual(code,0)
            self.assertEqual(catalog_path.read_bytes(),saved)
            report=json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["results"][0]["id"],1)
            self.assertEqual(len(json.loads(template.read_text(encoding="utf-8"))),1)


if __name__ == "__main__":
    unittest.main()
