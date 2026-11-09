import json
from pathlib import Path
import tempfile
import unittest
from label_api import APIClient, numeric_strength_in_label
from clinical_candidates import MemoizedLookups, collect


class StubHTTP:
    def __init__(self):
        self.calls = []
    def get(self, url, params):
        self.calls.append((url, dict(params)))
        field = params['search'].split(':',1)[0]
        if field == "openfda.generic_name":
            return {"results": [
                {"set_id":"X", "effective_time":"20240101", "openfda":{"generic_name":["Drug"]}},
                {"set_id":"Y", "effective_time":"20240201", "openfda":{"generic_name":["Drug"]}},
            ]}
        if field == "openfda.substance_name":
            return {"results":[
                {"set_id":"X", "effective_time":"20260101", "openfda":{"generic_name":["Drug"]}},
                {"set_id":"Z", "effective_time":"20260301", "openfda":{"generic_name":["Drug"]}},
            ]}
        return {}


class ExactStrengthBoundaryTests(unittest.TestCase):
    def test_10mg_must_not_match_110mg(self):
        self.assertFalse(numeric_strength_in_label("10mg", {"dosage_forms_and_strengths": ["110 mg tablet"]}))
        self.assertTrue(numeric_strength_in_label("10mg", {"dosage_forms_and_strengths": ["10 mg tablet"]}))

    def test_5mg_not_15mg(self):
        self.assertFalse(numeric_strength_in_label("5mg", {"active_ingredient": ["15mg ingredient"]}))
        self.assertTrue(numeric_strength_in_label("5mg", {"active_ingredient": ["5mg ingredient"]}))


class MultiSourceFDATests(unittest.TestCase):
    def test_both_source_fields_and_latest_version_for_same_setid(self):
        api=StubHTTP()
        labels=APIClient.fda(api,"drug")
        self.assertEqual(len(api.calls),2)
        self.assertTrue(all(c['limit'] == "100" for _,c in api.calls))
        self.assertEqual({x['set_id'] for x in labels},{"X","Y","Z"})
        x=next(x for x in labels if x['set_id']=="X")
        self.assertEqual(x["effective_time"],"20260101")
        self.assertEqual(labels[0]["set_id"],"Z")

    def test_search_query_quotation_is_sanitized(self):
        api=StubHTTP()
        APIClient.fda(api,'drug" OR unrelated"')
        self.assertTrue(api.calls)
        self.assertTrue(all(c['search'].count('"') == 2 for _,c in api.calls))

    def test_bounded_and_empty_queries(self):
        api=StubHTTP()
        self.assertEqual(APIClient.fda(api, ""),[])
        self.assertEqual(APIClient.fda(api,"X"*86),[])
        self.assertEqual(api.calls, [])


class CountBackend:
    def __init__(self):
        self.fda_calls=0
        self.rx_calls=0
    def fda(self,ingredient):
        self.fda_calls+=1
        return []
    def rxnorm(self,ingredient):
        self.rx_calls+=1
        return ["123"]


class ClinicalLookupCacheTests(unittest.TestCase):
    def test_same_ingredient_reuses_http_research_without_merging_drug_rows(self):
        source=CountBackend()
        api=MemoizedLookups(source,max_entries=2)
        self.assertEqual(api.fda("metformin"),[])
        self.assertEqual(api.fda("metformin"),[])
        self.assertEqual(api.rxnorm("metformin"),["123"])
        self.assertEqual(api.rxnorm("METFORMIN"),["123"])
        self.assertEqual(source.fda_calls,1)
        self.assertEqual(source.rx_calls,1)
        self.assertEqual(api.metrics()["in_memory_cache_hits"],{"rxnorm":1,"fda":1})

    def test_lru_bounded(self):
        api=MemoizedLookups(CountBackend(),max_entries=2)
        for name in ("a","b","c","a"):
            api.fda(name)
        self.assertEqual(api.remote_calls['fda'],4)
        self.assertEqual(len(api.caches["fda"]),2)

    def test_end_to_end_repeats_still_keep_distinct_drug_identifiers(self):
        records = [
            {"id":1,"trade_name_en":"Brand A","scientific_name":"metformin"},
            {"id":2,"trade_name_en":"Brand B","scientific_name":"metformin"},
        ]
        backend=CountBackend()
        with tempfile.TemporaryDirectory() as temp:
            stats=collect(records,backend,0,2,Path(temp))
            self.assertEqual(sum(stats.values()),2)
            self.assertEqual(backend.fda_calls,1)
            self.assertEqual(backend.rx_calls,1)
            output=(Path(temp)/"clinical_label_candidates_REVIEW.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line)["id"] for line in output],[1,2])
            summary=json.loads((Path(temp)/"clinical_label_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["ingredient_lookup_cache"]["in_memory_cache_hits"]["fda"],1)


if __name__=="__main__":
    unittest.main()
