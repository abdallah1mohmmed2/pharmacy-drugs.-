import copy
import json
from pathlib import Path
import tempfile
import unittest

from convert import load_schema
from fast_search import DrugSearchIndex, benchmark, normalize
from research_batch import daily_med_summary, research, select_matches


def drug(i, name, arabic, scientific, strength, company, barcode=None):
    row = load_schema().copy()
    row.update({
        "id":i, "uuid":f"uuid-{i}", "trade_name_en":name,
        "trade_name_ar":arabic, "scientific_name":scientific,
        "strength_text":strength, "dosage_form":"TABLET", "route":"ORAL",
        "barcode_international":barcode
    })
    row["manufacturer_name"] = company
    row["package_description"] = "30 tablets"
    return row


PRODUCTS = [
    drug(1, "Glucophage 500mg 30 tablets", "جلوكوفاج ٥٠٠", "metformin hydrochloride", "500mg", "Merck", "6221000003295"),
    drug(2, "Glucophage 1000mg 30 tablets", "جلوكوفاج ١٠٠٠", "metformin hydrochloride", "1000mg", "Merck"),
    drug(3, "Metform 500mg tablets", "ميتفورم 500", "metformin hydrochloride", "500mg", "Other Company"),
    drug(4, "Amlodipine 5mg tablets", "املوديبين 5", "amlodipine besylate", "5mg", "Other Company"),
    drug(5, "Atorvastatin 20mg tablets", "اتورفاستاتين 20", "atorvastatin", "20mg", "Example"),
]


class FastRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.index = DrugSearchIndex(copy.deepcopy(PRODUCTS))

    def test_arabic_hamza_diacritic_digits_fold(self):
        self.assertEqual(normalize("إمْلوديپين ٥ مجم").split()[1], "5")
        self.assertEqual(normalize("جُلُوكُوفَاج ٥٠٠"), normalize("جلوكوفاج 500"))

    def test_exact_brand_returns_separate_strengths(self):
        results = self.index.search("Glucophage 500mg")
        self.assertEqual(results[0]["id"], 1)
        self.assertNotEqual(results[0]["id"], 2)
        self.assertTrue(all(r["clinical_product_equivalence_verified"] is False for r in results))

    def test_arabic_lookup_and_generic_field(self):
        ar = self.index.search("جلوكوفاج")
        self.assertEqual({r["id"] for r in ar[:2]}, {1,2})
        generic = self.index.search("metformin hydrochloride")
        self.assertEqual({r["id"] for r in generic[:3]}, {1,2,3})

    def test_exact_barcode_priority(self):
        hits = self.index.search("6221000003295")
        self.assertEqual(hits[0]["id"], 1)
        self.assertIn("exact_barcode", hits[0]["match_reasons"])

    def test_fuzzy_typo(self):
        hits = self.index.search("Glucophag")
        self.assertTrue(hits)
        self.assertIn(hits[0]["id"], (1,2))

    def test_no_id_merges_duplicate_products_are_distinct(self):
        r = self.index.search("Glucophage")
        self.assertGreaterEqual(len(r), 2)
        self.assertNotEqual(r[0]["uuid"], r[1]["uuid"])

    def test_invalid_duplicate_source_id_fails_index(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            DrugSearchIndex(PRODUCTS+[PRODUCTS[0]])

    def test_retrieval_benchmark_has_consistent_query_counts(self):
        result = benchmark(self.index, ["Glucophage 500mg", "جلوكوفاج", "atorvastatin"])
        self.assertEqual(result["queries"], 3)
        self.assertEqual(result["returned_results_for"], 3)
        self.assertGreater(result["queries_per_second"], 0)


class StaticLookup:
    def __init__(self):
        self.calls = []

    def __call__(self, ingredient, cache_dir):
        self.calls.append(ingredient)
        return {
            "rxcuis": ["1234"],
            "fda_labels": [{
                "openfda": {
                    "generic_name": ["metformin hydrochloride"],
                    "substance_name": ["metformin hydrochloride"],
                    "brand_name": ["GLUCOPHAGE"],
                    "route": ["ORAL"]
                },
                "dosage_forms_and_strengths": ["500 mg tablet"],
                "drug_interactions": ["Reference interactions - NOT clinical advice."],
                "adverse_reactions": ["Example adverse events; needs medical review."],
                "set_id": "11111111-1111-1111-1111-111111111111",
                "effective_time": "20261101"
            }],
            "dailymed": [{
                "set_id": "11111111-1111-1111-1111-111111111111",
                "title": "US product label (not Egyptian equivalence)",
                "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=11111111-1111-1111-1111-111111111111",
                "product_equivalence_verified": False
            }],
            "errors": {},
        }


class ResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.index = DrugSearchIndex(copy.deepcopy(PRODUCTS))

    def test_dedup_one_ingredient_for_multiple_strengths(self):
        look = StaticLookup()
        result = research(self.index, ["metformin hydrochloride"], max_records=3, workers=2,
                          cache_dir=Path("/tmp/no-network-needed"), lookup=look)
        self.assertEqual(result["researched_drugs"], 3)
        self.assertEqual(result["unique_ingredient_queries"], 1)
        self.assertEqual(result["deduplicated_ingredient_requests"], 2)
        self.assertEqual(len(look.calls),1)
        self.assertEqual(len({r["id"] for r in result["drugs"]}),3)
        self.assertTrue(all(not r["clinical_use_allowed"] for r in result["drugs"]))
        self.assertTrue(all(not r["auto_merged_into_json_batches"] for r in result["drugs"]))
        self.assertTrue(all(r["review_required"] for r in result["drugs"]))
        self.assertTrue(any("safety_review" in r for r in result["drugs"]))

    def test_queries_do_not_mix_different_products(self):
        look = StaticLookup()
        result = research(self.index, ["metformin hydrochloride", "Atorvastatin"], max_records=4,
                          workers=2, lookup=look)
        self.assertEqual(result["researched_drugs"], 4)
        self.assertEqual(len({r["uuid"] for r in result["drugs"]}),4)

    def test_daily_med_only_returns_verified_format_link_not_claim(self):
        payload = {"data":[
            {"setid":"11111111-1111-1111-1111-111111111111","title":"Example"},
            {"setid":"invalid-value","title":"bad"},
            {"setid":"11111111-1111-1111-1111-111111111111","title":"dup"}
        ]}
        hits = daily_med_summary(payload)
        self.assertEqual(len(hits),1)
        self.assertFalse(hits[0]["product_equivalence_verified"])

    def test_bad_query_and_worker_limits(self):
        with self.assertRaisesRegex(ValueError,"required"):
            research(self.index, [])
        with self.assertRaisesRegex(ValueError,"workers"):
            research(self.index,["glucophage"],workers=10)

    def test_no_results_means_no_external_calls(self):
        look = StaticLookup()
        result = research(self.index, ["ZZZZZ-not-in-catalog-XYZ"],lookup=look)
        self.assertEqual(result["researched_drugs"],0)
        self.assertEqual(look.calls, [])


if __name__ == "__main__":
    unittest.main()
