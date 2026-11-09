import json
from pathlib import Path
import tempfile
import unittest

from catalog_details import DETAIL_EXTRA_KEYS, derive_catalog_details, packaging_from_name, category_from_source
from convert import convert, load_schema, safe_text
from split_artifacts import partition_records


def source(i, name="Concor 10mg 30 tablets", company="Merck EG", description="cardiovascular.beta blocker"):
    return {"id": i, "name": name, "arabic": "كونكور", "active": "bisoprolol",
            "uses": "ارتفاع ضغط الدم", "company": company, "description": description,
            "barcode": "", "barcode_valid": 0, "units": 3, "price": 10}


class CatalogDetailsTests(unittest.TestCase):
    def test_company_name_kept_verbatim_as_display_string(self):
        fields, extras, evidence = derive_catalog_details(source(1))
        self.assertEqual(extras["manufacturer_name"], "Merck EG")
        self.assertEqual(evidence["manufacturer_name"], "source_company_exact")
        self.assertNotIn("manufacturer_id", fields)

    def test_explicit_package_count(self):
        fields, evidence = packaging_from_name("Concor 10mg 30 tablets")
        self.assertEqual(fields["package_description"], "30 tablets")
        self.assertEqual(fields["units_per_pack"], 30)
        self.assertIn("source_name", evidence["units_per_pack"])
        self.assertNotIn("strips_per_pack", fields)

    def test_explicit_strips_have_correct_sizes(self):
        fields, _ = packaging_from_name("Atorva 20mg 3 x 10 tablets strips")
        self.assertEqual(fields["units_per_pack"], 30)
        self.assertEqual(fields["strips_per_pack"], 3)
        self.assertEqual(fields["units_per_strip"], 10)
        self.assertEqual(fields["is_strip_based"], 1)

    def test_packaging_not_inferred_from_ml_and_mg(self):
        fields, _ = packaging_from_name("Syrup 120ml 5mg")
        self.assertEqual(fields, {})

    def test_ambiguous_packaging_preserved_as_unknown(self):
        fields, _ = packaging_from_name("10 tablets / 20 capsules")
        self.assertEqual(fields, {})

    def test_category_literal_suffix_not_atc_guess(self):
        f, ev = category_from_source("antihypertensive.beta blocker")
        self.assertEqual(f["pharmacologic_class"], "beta blocker")
        self.assertIn("source_description", ev["pharmacologic_class"])
        self.assertEqual(category_from_source("sentence. With clinical instructions."), ({}, {}))

    def test_98_fields_from_converter_company_and_packaging(self):
        sample = [source(1), source(2, name="Cream 20g 0.5%", company="Egypt Pharmaceuticals")]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            output.mkdir()
            report = convert(sample, load_schema(), output, "abc", "def",
                             include_catalog_details=True)
            rows = json.loads((output / "drugs_96_DRAFT.json").read_text(encoding="utf-8"))
            self.assertEqual([len(r) for r in rows], [98, 98])
            self.assertEqual([r["manufacturer_name"] for r in rows],
                             ["Merck EG", "Egypt Pharmaceuticals"])
            self.assertEqual(rows[0]["package_description"], "30 tablets")
            self.assertEqual(rows[0]["units_per_pack"], 30)
            self.assertEqual(rows[0]["pharmacologic_class"], "beta blocker")
            self.assertIsNone(rows[0]["manufacturer_id"])
            self.assertEqual(rows[0]["dosage_administration"], "")
            self.assertEqual(rows[0]["contraindications"], "")
            self.assertEqual(report["target_fields"], 96)
            self.assertEqual(report["export_record_fields"], 98)
            self.assertEqual(report["catalog_details_nonempty_counts"]["manufacturer_name"], 2)
            parts = Path(tmp) / "parts"
            total, paths = partition_records(rows, parts, batch_size=1)
            self.assertEqual(total, len(rows))
            self.assertEqual(
                [r for p in paths for r in json.loads(p.read_text(encoding="utf-8"))], rows
            )
            self.assertTrue(all(list(r)[-2:] == list(DETAIL_EXTRA_KEYS) for r in rows))

    def test_96_legacy_mode_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = convert([source(1)],load_schema(),output,"abc","def")
            data = json.loads((output/"drugs_96_DRAFT.json").read_text())
            self.assertEqual(len(data[0]),96)
            self.assertNotIn("manufacturer_name", data[0])
            self.assertEqual(report["export_record_fields"],96)

    def test_mixed_96_98_refused(self):
        full = load_schema().copy()
        full.update({"id": 1, "uuid": "uuid-a"})
        ext = dict(full)
        ext.update({"id": 2, "uuid": "uuid-b", "manufacturer_name": "Acme", "package_description": "10 tablets"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "mixed record fields"):
                partition_records([full,ext],Path(tmp)/"export")


if __name__ == "__main__":
    unittest.main()
