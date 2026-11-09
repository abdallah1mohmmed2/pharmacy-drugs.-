import unittest
from product_name_parser import product_name_fields


class ProductNameTests(unittest.TestCase):
    def test_tablet_strength_and_route(self):
        parsed, evidence = product_name_fields("concor 10mg 30 f.c. tablets", "bisoprolol fumarate")
        self.assertEqual(parsed['dosage_form'], 'TABLET')
        self.assertEqual(parsed['route'], 'ORAL')
        self.assertEqual(parsed['strength_value'], 10)
        self.assertEqual(parsed['strength_unit'], 'mg')
        self.assertIn('name:', evidence['dosage_form'])

    def test_syrup_120ml_is_volume_not_strength(self):
        parsed, _ = product_name_fields("1 2 3 syrup 120 ml")
        self.assertEqual(parsed['dosage_form'], 'SYRUP')
        self.assertNotIn('strength_value', parsed)

    def test_combination_slash_never_single_strength(self):
        parsed, _ = product_name_fields("concor plus 10/25mg 30 tablets", "bisoprolol+hydrochlorothiazide")
        self.assertNotIn('strength_value', parsed)

    def test_combination_one_number_still_not_single_strength(self):
        parsed, _ = product_name_fields("2mg tabs", "chlorpheniramine+paracetamol")
        self.assertNotIn('strength_value', parsed)

    def test_concentration_mg_per_ml_is_not_single_strength(self):
        parsed, _ = product_name_fields("injection 10mg/2ml")
        self.assertEqual(parsed['dosage_form'], 'INJECTION')
        self.assertNotIn('strength_value', parsed)
        self.assertNotIn('route', parsed)

    def test_percent_cream_tube_weight_not_strength(self):
        parsed, _ = product_name_fields("cream 0.5% 20g")
        self.assertEqual(parsed['dosage_form'], 'CREAM')
        self.assertEqual(parsed['route'], 'TOPICAL')
        self.assertEqual(parsed['strength_text'], '0.5%')
        self.assertEqual(parsed['strength_value'], 0.5)

    def test_ophthalmic_and_vaginal_routes_override_form(self):
        parsed, _ = product_name_fields("eye ointment 0.5%")
        self.assertEqual(parsed['route'], 'OPHTHALMIC')
        parsed, _ = product_name_fields("pessary 500mg")
        self.assertEqual(parsed['route'], 'VAGINAL')

    def test_no_guesses(self):
        parsed, _ = product_name_fields("Brand name XYZ 20ml", "")
        self.assertEqual(parsed, {})


if __name__ == '__main__':
    unittest.main()
