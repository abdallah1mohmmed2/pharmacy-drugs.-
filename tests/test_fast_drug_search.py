import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fast_drug_search import build_index, norm, search_index, strength_key, _fts_query
from convert import load_schema


def drug(i, en, ar, scientific, strength, barcode=None):
    rec = load_schema().copy()
    rec.update({
        "id": i, "uuid": f"stable-id-{i}",
        "trade_name_en": en, "trade_name_ar": ar,
        "scientific_name": scientific, "strength_text": strength,
        "barcode_international": barcode, "dosage_form": "TABLET",
        "route": "ORAL", "is_active": 0,
    })
    rec["manufacturer_name"] = "Example Pharma"
    rec["package_description"] = "30 tablets"
    return rec


RECORDS = [
    drug(1,"Concor 10mg 30 tablets", "كونكور ١٠ مجم", "bisoprolol fumarate","10mg","6221000003295"),
    drug(2,"Concor 5mg 30 tablets", "كونكور ٥ مجم", "bisoprolol fumarate","5mg","6221000003301"),
    drug(3,"Glucophage 500mg tablets", "جلوكوفاج", "metformin hydrochloride","500mg"),
    drug(4,"Amoxicillin 500mg capsules", "أموكسيسيللين","amoxicillin","500mg"),
    drug(5,"Amoxicillin 250mg capsules", "اموكسيسيللين","amoxicillin","250mg"),
]


class FastSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "converted.json"
        self.index = self.root / "search.sqlite3"
        self.source.write_text(json.dumps(RECORDS, ensure_ascii=False), encoding="utf-8")
        self.before = copy.deepcopy(RECORDS)
        self.meta = build_index(self.source,self.index)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_changed_records_and_index_count(self):
        self.assertEqual(json.loads(self.source.read_text(encoding="utf-8")), self.before)
        self.assertEqual(self.meta["records"],5)
        self.assertEqual(self.meta["input_fields"],98)
        with sqlite3.connect(self.index) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lex").fetchone()[0],5)

    def test_exact_barcode_has_top_priority(self):
        results=search_index(self.index,"6221000003301")
        self.assertEqual(results[0]["id"],2)
        self.assertEqual(results[0]["match_method"],"exact_barcode")
        self.assertFalse(results[0]["clinical_equivalence_verified"])

    def test_exact_id(self):
        results=search_index(self.index,"4")
        self.assertEqual(results[0]["id"],4)
        self.assertEqual(results[0]["match_method"],"exact_id")

    def test_exact_trade_name_and_full_record_lossless(self):
        results=search_index(self.index,"Concor 10mg 30 tablets",full_record=True)
        self.assertEqual(results[0]["id"],1)
        self.assertEqual(results[0]["record"], RECORDS[0])
        self.assertEqual(results[0]["match_method"],"exact_brand")

    def test_strength_conflict_does_not_override_exact_match(self):
        results=search_index(self.index,"Concor 10mg", limit=5)
        self.assertEqual(results[0]["id"],1)
        rec2=next(x for x in results if x["id"]==2)
        self.assertTrue(rec2["strength_conflict"])

    def test_normalized_arabic_and_arabic_digits(self):
        self.assertEqual(norm("أَمُوكْسِيسِيلْلِين"),norm("اموكسيسيللين"))
        self.assertEqual(norm("١٠mg"),"10mg")
        self.assertEqual(strength_key("500MG"),"500mg")
        results=search_index(self.index,"أموكسيسيللين")
        self.assertEqual(results[0]["id"],4) # Equal Arabic names, tie by stable drug ID

    def test_misspelling_returns_candidate_not_certainty(self):
        results=search_index(self.index,"Glucophge",limit=5)
        self.assertTrue(results)
        self.assertIn(3,[r["id"] for r in results])
        self.assertTrue(all(not r["clinical_equivalence_verified"] for r in results))

    def test_generic_ingredient(self):
        results=search_index(self.index,"metformin hydrochloride")
        self.assertEqual(results[0]["id"],3)

    def test_prefix_search(self):
        results=search_index(self.index,"Glucoph")
        self.assertEqual(results[0]["id"],3)

    def test_query_escaping_injection_safety(self):
        for query in ('"" OR ("', 'Concor" OR *', "'); DROP TABLE records; --"):
            _=search_index(self.index,query)
        self.assertEqual(self.meta["records"],
                         sqlite3.connect(self.index).execute("SELECT COUNT(*) FROM records").fetchone()[0])
        self.assertEqual(_fts_query(["x","y"]),'"x" AND "y"')

    def test_duplicate_id_refused_atomically(self):
        bad=RECORDS+[dict(RECORDS[0],uuid="duplicate-other-uuid")]
        self.source.write_text(json.dumps(bad), encoding="utf-8")
        before=self.index.read_bytes()
        with self.assertRaisesRegex(ValueError,"duplicate drug ID"):
            build_index(self.source,self.index)
        self.assertEqual(self.index.read_bytes(),before)

    def test_mixed_schema_rejected(self):
        bad=copy.deepcopy(RECORDS)
        del bad[0]["manufacturer_name"]
        self.source.write_text(json.dumps(bad),encoding="utf-8")
        with self.assertRaises(ValueError):
            build_index(self.source,self.index)

    def test_limits(self):
        for query in ("", " " * 2, "x"*161):
            with self.assertRaises(ValueError):
                search_index(self.index, query)
        with self.assertRaises(ValueError):
            search_index(self.index,"Concor",limit=51)

    def test_built_index_works_for_96_core_fields(self):
        rows = copy.deepcopy(RECORDS[:2])
        for r in rows:
            del r["manufacturer_name"]
            del r["package_description"]
        self.source.write_text(json.dumps(rows,ensure_ascii=False),encoding="utf-8")
        build_index(self.source,self.index)
        self.assertEqual(search_index(self.index,"Concor 10mg")[0]["id"],1)


if __name__=="__main__":
    unittest.main()
