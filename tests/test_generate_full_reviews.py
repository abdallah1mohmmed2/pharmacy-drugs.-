import copy
import json
from pathlib import Path
import tempfile
import unittest

from apply_reviewed_medical import apply_reviews, REVIEWABLE_FIELDS
from convert import load_schema
from generate_full_reviews import (
    export_full_review_catalog, make_review_entries, FULL_FILENAME,
    BATCH_FOLDER, MANIFEST_FILENAME,
)


def record(i, extended=True):
    result = load_schema().copy()
    result.update({
        "id": i, "uuid": f"valid-drug-uuid-{i}",
        "trade_name_en": f"Medicine {i} 500mg",
        "trade_name_ar": f"دواء {i}",
        "scientific_name": "Example ingredient", "strength_text": "500mg",
        "dosage_form": "TABLET", "route": "ORAL", "is_active": 0,
        "indications": "Catalog indications not approved as a clinical review",
    })
    if extended:
        result["manufacturer_name"] = "Recorded company"
        result["package_description"] = "30 tablets"
    return result


class CompleteReviewTests(unittest.TestCase):
    def test_full_catalog_301_records_300_and_one_preserve_every_field(self):
        catalog = [record(i) for i in range(1, 302)]
        before = copy.deepcopy(catalog)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "converted.json"
            source.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            original_bytes = source.read_bytes()
            target = folder / "review"
            manifest = export_full_review_catalog(source, target, expected_min_rows=300)
            full = json.loads((target / FULL_FILENAME).read_text(encoding="utf-8"))
            batches = sorted((target / BATCH_FOLDER).glob("*.json"))
            self.assertEqual([p.name for p in batches], ["batch_001.json", "batch_002.json"])
            lists = [json.loads(p.read_text(encoding="utf-8")) for p in batches]
            self.assertEqual([len(x) for x in lists], [300, 1])
            self.assertEqual(full, lists[0] + lists[1])
            self.assertEqual([e["converted_record"] for e in full], catalog)
            self.assertEqual(catalog, before)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(len(full), 301)
            self.assertEqual(len({x["id"] for x in full}), 301)
            self.assertEqual(len({x["uuid"] for x in full}), 301)
            self.assertEqual(manifest["review_records"], 301)
            self.assertEqual(manifest["converted_fields_per_drug"], 98)
            self.assertEqual(manifest["approved_drugs"], 0)
            self.assertEqual(manifest["unapproved_drugs"], 301)
            self.assertEqual(manifest["review_batches"], 2)
            self.assertEqual(json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8")), manifest)
            self.assertFalse(any(p.suffix != ".json" for p in (target / BATCH_FOLDER).iterdir()))

    def test_all_entries_unapproved_empty_clinical_fields_and_original_untouched(self):
        records = [record(1), record(2)]
        before = copy.deepcopy(records)
        output = make_review_entries(records)
        self.assertEqual(before, records)
        self.assertEqual(len(output), 2)
        for item, original in zip(output, records):
            self.assertEqual(item["converted_record"], original)
            self.assertIs(item["approved"], False)
            self.assertIs(item["exact_local_product_label_verified"], False)
            self.assertEqual(item["reviewed_by"], "")
            self.assertEqual(item["source_url"], "")
            self.assertEqual(item["review_status"], "AWAITING_INDEPENDENT_PRODUCT_LEVEL_REVIEW")
            self.assertEqual(set(item["reviewed_fields"]), REVIEWABLE_FIELDS)
            self.assertTrue(all(value == "" for value in item["reviewed_fields"].values()))
        with self.assertRaisesRegex(ValueError, "approved=true"):
            apply_reviews(records, output)

    def test_96_core_schema_supported(self):
        rows = [record(1, extended=False)]
        result = make_review_entries(rows)
        self.assertEqual(len(result[0]["converted_record"]), 96)
        self.assertEqual(result[0]["manufacturer_name"], "")
        self.assertEqual(result[0]["uuid"], "valid-drug-uuid-1")

    def test_reject_duplicate_id_and_uuid(self):
        one = record(1)
        with self.assertRaisesRegex(ValueError, "duplicate catalog id"):
            make_review_entries([one, dict(one, uuid="different-uuid")])
        with self.assertRaisesRegex(ValueError, "duplicate catalog UUID"):
            make_review_entries([one, dict(record(2), uuid=one["uuid"])])

    def test_reject_mixed_schema_and_active_records(self):
        a = record(1)
        b = record(2, extended=False)
        with self.assertRaisesRegex(ValueError, "different schema"):
            make_review_entries([a, b])
        a["is_active"] = 1
        with self.assertRaisesRegex(ValueError, "inactive converted draft"):
            make_review_entries([a])

    def test_minimum_count_fails_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "data.json"
            src.write_text(json.dumps([record(1)]), encoding="utf-8")
            output = root / "should-not-exist"
            with self.assertRaisesRegex(ValueError, "smaller than expected minimum"):
                export_full_review_catalog(src, output, expected_min_rows=27617)
            self.assertFalse(output.exists())

    def test_batch_size_and_existing_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "data.json"
            src.write_text(json.dumps([record(1)]), encoding="utf-8")
            for batch_size in (0, 301, True, -1):
                with self.subTest(batch_size=batch_size), self.assertRaisesRegex(ValueError, "between 1 and 300"):
                    export_full_review_catalog(src, root / "output", batch_size=batch_size)
            out = root / "already-exists"
            out.mkdir()
            with self.assertRaisesRegex(ValueError, "already exists"):
                export_full_review_catalog(src, out)

    def test_total_records_must_be_nonempty_and_homogeneous(self):
        with self.assertRaisesRegex(ValueError, "nonempty JSON array"):
            make_review_entries([])
        corrupted = record(2)
        corrupted.pop("uuid")
        with self.assertRaises(ValueError):
            make_review_entries([record(1), corrupted])


if __name__ == "__main__":
    unittest.main()
