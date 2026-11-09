import json
from pathlib import Path
import tempfile
import unittest
from clinical_candidates import collect, strict_candidate
from label_api import propose


class FakeAPI:
    def __init__(self, generic=True, brand='ANOTHER BRAND', route='ORAL'):
        self.generic = generic
        self.brand = brand
        self.route = route

    def rxnorm(self, ingredient):
        return ['99999']

    def fda(self, ingredient):
        g = 'metformin hydrochloride' if self.generic else 'unrelated drug'
        return [{
            'openfda': {
                'generic_name': [g],
                'substance_name': [g],
                'brand_name': [self.brand],
                'route': [self.route],
            },
            'dosage_forms_and_strengths': ['500 mg tablet'],
            'warnings': ['Do not use this content without verifying the local label.'],
            'contraindications': ['Example clinical warning (fixture).'],
            'adverse_reactions': ['Example adverse event (fixture).'],
            'set_id': '00000000-0000-0000-0000-000000000001',
            'effective_time': '20260606',
        }]


REC = {'id': 7, 'trade_name_en': 'glucophage 500mg tablets',
       'scientific_name': 'metformin hydrochloride',
       'route': 'ORAL', 'contraindications': '', 'warnings_precautions': ''}


class ClinicalReferencesTests(unittest.TestCase):
    def test_ingredient_only_reference_not_product_suggestion(self):
        r = propose(REC, FakeAPI(), 110)
        self.assertEqual(r['status'], 'ingredient_reference_only')
        self.assertIn('warnings_precautions', r['reference_fields'])
        self.assertEqual(r['suggestions'], {})
        self.assertFalse(strict_candidate(r))

    def test_exact_brand_route_strength_draft_still_review_only(self):
        r = propose(REC, FakeAPI(brand='GLUCOPHAGE'), 110)
        self.assertTrue(strict_candidate(r))
        self.assertIn('contraindications', r['suggestions'])

    def test_conflicting_route_not_product_match(self):
        r = propose(REC, FakeAPI(brand='GLUCOPHAGE',route='TOPICAL'), 110)
        self.assertEqual(r['status'], 'ingredient_reference_only')
        self.assertTrue(r['route_conflict'])
        self.assertEqual(r['suggestions'], {})

    def test_wrong_active_ingredient_rejected(self):
        r = propose(REC, FakeAPI(generic=False), 110)
        self.assertEqual(r['status'], 'no_verified_candidate')
        self.assertFalse(r.get('reference_fields'))

    def test_sidecar_never_modifies_record(self):
        with tempfile.TemporaryDirectory() as td:
            original = json.dumps(REC,sort_keys=True)
            stats = collect([REC], FakeAPI(), 0, 1, Path(td))
            self.assertIn('INGREDIENT_REFERENCE_ONLY_NOT_PRODUCT_MATCH',stats)
            self.assertEqual(json.dumps(REC,sort_keys=True), original)
            result = json.loads((Path(td) / 'clinical_label_candidates_REVIEW.jsonl').read_text().strip())
            self.assertEqual(result['suggestions'], {})
            self.assertIn('warnings_precautions', result['reference_fields'])
            self.assertFalse(result['clinical_use_allowed'])


if __name__ == '__main__':
    unittest.main()
