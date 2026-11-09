import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from convert import (SCHEMA_VERSION, convert, get_sha256, git_blob_sha1,
                     gtin_checksum_valid, load_schema, parse_id, preflight,
                     safe_text, verified_check_digit, main)
from clinical_candidates import strict_candidate, collect


SAMPLE = [
    {'id': 1, 'name': 'concor 10mg 30 tablets', 'arabic': 'كونكور',
     'active': 'bisoprolol fumarate', 'company':'Example Company',
     'price': '99', 'oldprice':'70.5', 'barcode': '6221000003295',
     'barcode_valid': '1', 'units': '3', 'uses': 'ارتفاع الضغط',
     'description': 'antihypertensive.beta blocker'},
    {'id': 2, 'name': 'apo-lorazepam 2mg 10 tablets', 'arabic': 'لورازيبام',
     'active': 'lorazepam', 'company':'Another Company',
     'price': '20', 'barcode':'6221000003295', 'barcode_valid':'0',
     'units':'1', 'uses':'', 'description':'sedative'},
]


class ConverterTests(unittest.TestCase):
    def test_expected_schema_exact(self):
        defaults = load_schema()
        self.assertEqual(len(defaults),96)
        self.assertEqual(defaults['pregnancy_category'],'UNKNOWN')

    def test_id_parsing(self):
        self.assertEqual(parse_id('12'),12)
        self.assertIsNone(parse_id(True))
        self.assertIsNone(parse_id(0))
        self.assertIsNone(parse_id('bad'))

    def test_gtin_check_digit(self):
        self.assertTrue(gtin_checksum_valid('6221000003295'))
        self.assertFalse(gtin_checksum_valid('6221000003296'))
        self.assertFalse(gtin_checksum_valid('62210000032951'))
        self.assertFalse(gtin_checksum_valid('bad'))

    def test_barcodes_require_source_flag(self):
        self.assertTrue(verified_check_digit(SAMPLE[0]))
        self.assertFalse(verified_check_digit(SAMPLE[1]))
        self.assertFalse(verified_check_digit({'barcode':'6221000003295'}))

    def test_missing_and_duplicates_detected(self):
        self.assertIn('DUPLICATE_ID', [x['code'] for x in preflight(SAMPLE + [SAMPLE[0]] )])
        self.assertIn('NAME_MISSING', [x['code'] for x in preflight([{'id':1,'name':'','arabic':'','active':'x'}])])
        self.assertIn('SOURCE_SCHEMA_CHANGED',[x['code'] for x in preflight([{'id':1,'name':'hi'}])])
        self.assertIn('NOT_OBJECT',[x['code'] for x in preflight([None])])

    def test_boolean_ingredient_rejected(self):
        broken = dict(SAMPLE[0],active=True)
        self.assertIn('INVALID_FIELD_TYPE',[x['code'] for x in preflight([broken])])

    def test_converter_preserves_all_rows_and_originals(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            manifest = convert(SAMPLE,load_schema(),out,'abc','def')
            result = json.loads((out/'drugs_96_DRAFT.json').read_text(encoding='utf-8'))
            self.assertEqual(len(result), len(SAMPLE))
            self.assertEqual(manifest['converted_records'],2)
            self.assertEqual(manifest['schema_version'],SCHEMA_VERSION)
            for r in result:
                self.assertEqual(len(r),96)
                self.assertEqual(r['public_price_pack'],0.0)
                self.assertEqual(r['stock_total_units'],0)
                self.assertEqual(r['manufacturer_id'],None)
                self.assertEqual(r['is_active'],0)  # inactive by default until approval; source 'active' is ingredient
                self.assertEqual(r['is_controlled'],0)  # unverified placeholder!
                self.assertTrue(r['uuid'])
            self.assertEqual(result[0]['scientific_name'],'bisoprolol fumarate')
            self.assertEqual(result[1]['scientific_name'],'lorazepam')
            self.assertEqual(result[0]['barcode_international'],'6221000003295')
            self.assertIsNone(result[1]['barcode_international'])
            originals=[json.loads(row) for row in (out/'original_records.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([x['record'] for x in originals], SAMPLE)
            with (out/'mapping_audit.csv').open(encoding='utf-8-sig') as f:
                audit=list(csv.DictReader(f))
            self.assertEqual(len(audit),len(SAMPLE))
            self.assertIn('is_controlled',audit[1]['unverified_safety_fields'])
            self.assertEqual(manifest['files_sha256']['drugs_96_DRAFT.json'],get_sha256(out/'drugs_96_DRAFT.json'))

    def test_stable_uuids_across_runs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            convert(SAMPLE,load_schema(),Path(a),'abc','def')
            convert(SAMPLE,load_schema(),Path(b),'xyz','ghi')
            one=json.loads((Path(a)/'drugs_96_DRAFT.json').read_text())
            two=json.loads((Path(b)/'drugs_96_DRAFT.json').read_text())
            self.assertEqual([r['uuid'] for r in one],[r['uuid'] for r in two])

    def test_sample_and_full_minimum(self):
        with tempfile.TemporaryDirectory() as td:
            sub=Path(td)/'test'; sub.mkdir()
            report=convert(SAMPLE,load_schema(),sub,'a','b',sample=1)
            self.assertEqual(report['converted_records'],1)
            sub2=Path(td)/'full'; sub2.mkdir()
            with self.assertRaises(ValueError):
                convert(SAMPLE,load_schema(),sub2,'a','b',expected_min_rows=20000)

    def test_preflight_saves_report_no_draft(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'Preflight failed'):
                convert(SAMPLE+[SAMPLE[0]],load_schema(),Path(td),'a','b')
            self.assertTrue((Path(td)/'preflight_errors.json').exists())
            self.assertFalse((Path(td)/'drugs_96_DRAFT.json').exists())

    def test_cli_source_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            inp=Path(td)/'input.json'
            inp.write_text(json.dumps(SAMPLE,ensure_ascii=False),encoding='utf-8')
            target=Path(td)/'outputs'
            self.assertEqual(main(['--source',str(inp),'--output-dir',str(target),'--sample','1']),0)
            with self.assertRaises(SystemExit):
                main(['--source',str(inp),'--output-dir',str(target)])
            self.assertEqual(json.loads(inp.read_text()),SAMPLE)

    def test_sha256_and_git_blob_hash(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'s'; path.write_bytes(b'abc')
            self.assertEqual(get_sha256(path),hashlib.sha256(b'abc').hexdigest())
            self.assertEqual(git_blob_sha1(path),hashlib.sha1(b'blob 3\0abc').hexdigest())

    def test_pinning_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            inp=Path(td)/'source.json'; inp.write_text(json.dumps(SAMPLE))
            target=Path(td)/'out'
            self.assertEqual(main(['--source',str(inp),'--output-dir',str(target),
                                   '--expected-git-blob-sha1','0'*40]),1)
            self.assertFalse((target/'drugs_96_DRAFT.json').exists())
            self.assertTrue((target/'FAILED.txt').exists())


class FakeClient:
    def __init__(self, meta):
        self.meta=meta
    def rxnorm(self, _):
        return ['1']
    def fda(self, _):
        return []

class ClinicalProposalTests(unittest.TestCase):
    def test_full_strict_match_only(self):
        good={'status':'draft_suggestions','identity':'exact_ingredient','brand_match':True,
              'route_match':True,'strength_match':True,'same_score_candidates':1,'score':110}
        self.assertTrue(strict_candidate(good))
        for field,value in [('brand_match',False),('route_match',False),('strength_match',False),
                            ('same_score_candidates',2),('identity','possible_salt_variant'),('score',90)]:
            self.assertFalse(strict_candidate(dict(good, **{field:value})))

    def test_clinical_output_not_modify_input(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)
            before=json.dumps(SAMPLE,sort_keys=True)
            counts=collect(SAMPLE,FakeClient({}),0,1,path)
            self.assertEqual(before,json.dumps(SAMPLE,sort_keys=True))
            self.assertEqual(sum(counts.values()),1)
            d=json.loads((path/'clinical_label_candidates_REVIEW.jsonl').read_text().strip())
            self.assertFalse(d['clinical_use_allowed'])
            self.assertEqual(d['suggestions'],{})

if __name__ == '__main__':
    unittest.main()
