import copy
import json
from pathlib import Path
import tempfile
import unittest

from convert import load_schema
from split_artifacts import MAX_DRUGS_PER_FILE, main, partition_records


def make_row(i):
    row = load_schema().copy()
    row["id"] = i
    row["uuid"] = f"unique-uuid-{i}"
    row["trade_name_en"] = f"Medicine product {i}"
    row["scientific_name"] = f"ingredient-{i}"
    row["strength_text"] = "10mg"
    row["warnings_precautions"] = ""
    row["is_active"] = 0
    return row


class JSONOnlyArtifactTests(unittest.TestCase):
    def test_chunk_slices_and_leftover_no_duplicates(self):
        rows = [make_row(i) for i in range(1, 13)]
        before = copy.deepcopy(rows)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "parts"
            count, files = partition_records(rows, output, batch_size=5)
            self.assertEqual(count, 12)
            self.assertEqual([p.name for p in files], ["batch_01.json", "batch_02.json", "batch_03.json"])
            slices = [json.loads(p.read_text(encoding="utf-8")) for p in files]
            self.assertEqual([len(x) for x in slices], [5, 5, 2])
            self.assertEqual([record for chunk in slices for record in chunk], before)
            self.assertEqual({r["id"] for chunk in slices for r in chunk}, set(range(1, 13)))
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ["batch_01.json", "batch_02.json", "batch_03.json"])
            self.assertEqual(rows, before)  # No source mutation

    def test_up_to_300_and_last_smaller(self):
        rows = [make_row(i) for i in range(1, 302)]
        with tempfile.TemporaryDirectory() as tmp:
            total, files = partition_records(rows, Path(tmp) / "out")
            self.assertEqual(total, 301)
            self.assertEqual([len(json.loads(p.read_text(encoding="utf-8"))) for p in files], [300, 1])

    def test_duplicate_id_rejected_without_creating_outputs(self):
        rows = [make_row(1), make_row(1)]
        rows[1]["uuid"] = "different"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with self.assertRaisesRegex(ValueError, "Duplicate ID"):
                partition_records(rows, out, batch_size=2)
            self.assertFalse(out.exists())

    def test_duplicate_uuid_rejected_even_with_different_ids(self):
        rows = [make_row(1), make_row(2)]
        rows[1]["uuid"] = rows[0]["uuid"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with self.assertRaisesRegex(ValueError, "Duplicate UUID"):
                partition_records(rows, out)
            self.assertFalse(out.exists())

    def test_malformed_schema_rejected(self):
        rows = [make_row(1), make_row(2)]
        del rows[1]["scientific_name"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with self.assertRaisesRegex(ValueError, "96-field"):
                partition_records(rows, out)
            self.assertFalse(out.exists())

    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "important.json").write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                partition_records([make_row(1)], out)
            self.assertEqual((out / "important.json").read_text(), "preserve")

    def test_bad_batch_sizes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (0, -1, MAX_DRUGS_PER_FILE + 1):
                with self.assertRaisesRegex(ValueError, "between 1 and 300"):
                    partition_records([make_row(1)], Path(tmp) / "out", batch_size=value)

    def test_over_artifact_limit_fails_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with self.assertRaisesRegex(ValueError, "exceeding limit"):
                partition_records([make_row(i) for i in range(1, 18)], out,
                                  batch_size=1, max_artifacts=16)
            self.assertFalse(out.exists())

    def test_cli_github_output_and_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_json = root / "input.json"
            github_output = root / "github-output.txt"
            input_json.write_text(json.dumps([make_row(i) for i in range(1, 8)]), encoding="utf-8")
            code = main(["--input", str(input_json), "--output-dir", str(root / "parts"),
                         "--batch-size", "3", "--github-output", str(github_output)])
            self.assertEqual(code, 0)
            self.assertIn("batch_count=3", github_output.read_text())
            self.assertIn("total_records=7", github_output.read_text())
            self.assertEqual(sorted(p.name for p in (root / "parts").iterdir()),
                             ["batch_01.json", "batch_02.json", "batch_03.json"])


if __name__ == "__main__":
    unittest.main()
