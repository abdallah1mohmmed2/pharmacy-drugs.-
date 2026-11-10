import copy
import json
from pathlib import Path
import tempfile
import unittest

from approved_research_pipeline import research_reviewed_ids, run_connected_export
from test_verified_merge_export import drug, approval


def fake_lookup(_ingredient, _cache_dir):
    label = {
        "set_id": "11111111-1111-1111-1111-111111111111",
        "effective_time": "20261010",
        "openfda": {
            "generic_name": ["verified ingredient"],
            "substance_name": ["verified ingredient"],
            "brand_name": ["BRAND"],
            "route": ["ORAL"],
        },
        "dosage_forms_and_strengths": ["10 mg tablets"],
        "contraindications": ["Exact product contraindication reference only."],
        "drug_interactions": ["Interaction section from reference, unverified locally."],
        "adverse_reactions": ["Mixed common and serious reactions, not automatically classified."],
    }
    return {"rxcuis": ["12345"], "fda_labels": [label], "dailymed": [],
            "errors": {}}


class ConnectedPipelineTests(unittest.TestCase):
    def test_explicit_ids_not_brand_similarity(self):
        rows=[drug(1), drug(2), drug(3)]
        approvals=[approval(rows[1])]
        snapshot=copy.deepcopy(rows)
        report=research_reviewed_ids(rows,approvals,cache_dir=".unused",lookup=fake_lookup)
        self.assertEqual(rows,snapshot)
        self.assertEqual([r["id"] for r in report["drugs"]],[2])
        self.assertEqual(report["researched_drugs"],1)
        self.assertEqual(report["unique_ingredient_queries"],1)
        self.assertFalse(report["clinical_use_allowed"])
        self.assertEqual(report["drugs"][0]["uuid"], rows[1]["uuid"])
        self.assertEqual(report["drugs"][0]["reference_fields"]["contraindications"],
                         "Exact product contraindication reference only.")
        self.assertFalse(report["drugs"][0]["fda_label_is_verified_egyptian_product"])

    def test_reject_identity_forgery_before_api_calls(self):
        rows=[drug(1),drug(2)]
        proposal=approval(rows[1])
        proposal["strength_text"]="50mg"
        with self.assertRaisesRegex(ValueError,"mismatched strength_text"):
            research_reviewed_ids(rows,[proposal],cache_dir=".unused",lookup=fake_lookup)

    def test_reject_ambiguous_and_duplicate_ids(self):
        rows=[drug(1),drug(2)]
        with self.assertRaisesRegex(ValueError,"unknown or duplicated"):
            research_reviewed_ids(rows,[approval(rows[0]),approval(rows[0])],
                                  cache_dir=".unused",lookup=fake_lookup)
        proposal=approval(rows[0])
        proposal["id"]=999
        with self.assertRaisesRegex(ValueError,"unknown or duplicated"):
            research_reviewed_ids(rows,[proposal],cache_dir=".unused",lookup=fake_lookup)

    def test_full_connected_export_301_records_with_one_approved(self):
        rows=[drug(i) for i in range(1,302)]
        approved=approval(rows[1])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            catalog=root/"converted.json"
            approvals=root/"approvals.json"
            output=root/"verified"
            catalog.write_text(json.dumps(rows,ensure_ascii=False),encoding="utf-8")
            approvals.write_text(json.dumps([approved],ensure_ascii=False),encoding="utf-8")
            snapshot=catalog.read_bytes()
            audit=run_connected_export(catalog,approvals,output,
                                       cache_dir=root/"cache",lookup=fake_lookup)
            self.assertEqual(catalog.read_bytes(),snapshot)
            self.assertEqual(audit["approved_drugs"],1)
            self.assertEqual(audit["batches"],2)
            files=sorted((output/"json_batches").iterdir())
            self.assertEqual(len(files),2)
            data=[r for file in files for r in json.loads(file.read_text(encoding="utf-8"))]
            self.assertEqual([x["id"] for x in data],[x["id"] for x in rows])
            self.assertEqual(data[1]["side_effects_serious"],
                             approved["reviewed_fields"]["side_effects_serious"])
            self.assertEqual(data[0],rows[0])
            self.assertEqual(data[-1],rows[-1])
            self.assertFalse(any(p.name.endswith("REVIEW_ONLY.json")
                                 for p in (output/"json_batches").iterdir()))
            self.assertTrue((output/"verified_merge_audit.json").exists())
            self.assertFalse(any(x["is_active"] for x in data))


if __name__=="__main__":
    unittest.main()
