import unittest
from unittest.mock import patch
from io import BytesIO
import json

from rxnorm_gap_research import admissible, normalize, selection, query_term


class RxNormGapTests(unittest.TestCase):
    def test_exclude_ambiguous_forms_and_invalid_names(self):
        self.assertFalse(admissible("multivitamin formula"))
        self.assertFalse(admissible("baby milk formula"))
        self.assertFalse(admissible(""))
        self.assertFalse(admissible("salbutamol+paracetamol"))
        self.assertTrue(admissible("cephradine"))
        self.assertTrue(admissible("hyoscin-n-butylbromid"))

    def test_unique_selection_and_skip_already_researched(self):
        rows = [
            {"id": 1, "scientific_name": "cephradine"},
            {"id": 2, "scientific_name": "cephradine"},
            {"id": 3, "scientific_name": "unknownone+unknownsecond"},
            {"id": 4, "scientific_name": "alreadyknown"},
            {"id": 5, "scientific_name": ""},
        ]
        results = selection(rows, {"alreadyknown"}, count=10)
        names = [r["term"] for r in results]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("alreadyknown", names)
        self.assertEqual(names[0], "cephradine")
        self.assertEqual(results[0]["single_drug_records"], 2)
        self.assertIn("unknownone", names)
        self.assertEqual(next(x for x in results if x["term"] == "unknownone")["combination_component_records"], 1)

    def test_api_data_always_kept_as_unapproved_candidates(self):
        response = {
            "approximateGroup": {"candidate": [
                {"rxcui": "1234", "name": "Cephradine", "rank": "1", "score": "95.5", "source": "RXNORM"},
                {"rxcui": "1234", "name": "duplicate", "rank": "2"},
                {"rxcui": "5678", "name": "Different antibiotic", "rank": "3"},
                {"rxcui": "INVALID", "name": "Missing id", "rank": "3"},
            ]}
        }
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, _max): return json.dumps(response).encode()
        with patch("rxnorm_gap_research.urlopen", return_value=FakeResponse()):
            result, status = query_term("cephradine")
        self.assertEqual(status, "FOUND_REFERENCE")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["rxcui"], "1234")
        self.assertFalse(result[0]["is_local_product_match_verified"])
        self.assertFalse(result[0]["is_clinically_approved"])
        self.assertEqual(result[0]["review_status"], "LEXICAL_RXNORM_REFERENCE_ONLY")

    def test_no_result_without_displayable_name(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, _max): return b'{"approximateGroup":{"candidate":[{"rxcui":"123"}]}}'
        with patch("rxnorm_gap_research.urlopen", return_value=FakeResponse()):
            result, status = query_term("cephradine")
        self.assertEqual((result, status), ([], "NO_NAMED_CANDIDATE"))


if __name__ == "__main__":
    unittest.main()
