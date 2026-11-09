import copy
from datetime import date, timedelta
import unittest

from apply_reviewed_medical import apply_reviews
from convert import load_schema


def make_drug():
    row = load_schema().copy()
    row.update({"id": 123, "uuid": "uuid-123",
                "trade_name_en": "Brand 10mg tablets",
                "scientific_name": "example ingredient",
                "strength_text": "10mg", "dosage_form": "TABLET", "route": "ORAL",
                "is_active": 0})
    row["manufacturer_name"] = "Real manufacturer from source"
    row["package_description"] = "30 tablets"
    return row


def approval():
    drug = make_drug()
    keys = ("id", "uuid", "trade_name_en", "scientific_name",
            "strength_text", "dosage_form", "route")
    review = {key:drug[key] for key in keys}
    review.update({"approved": True, "reviewed_by":"Pharmacist reviewer",
                   "review_date": date.today().isoformat(),
                   "source_url":"https://www.accessdata.fda.gov/drugsatfda_docs/label/official",
                   "reviewed_fields": {
                       "dosage_administration": "Validated instructions from the exact product's approved label.",
                       "contraindications": "Reviewed product-label contraindication section.",
                   }})
    return review


class MedicallyReviewedMergeTests(unittest.TestCase):
    def test_apply_two_clinical_fields_preserves_others(self):
        old = make_drug()
        snapshot = copy.deepcopy(old)
        data = apply_reviews([old], [approval()])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["manufacturer_name"], old["manufacturer_name"])
        self.assertEqual(data[0]["package_description"], old["package_description"])
        self.assertEqual(data[0]["is_active"], 0)
        self.assertEqual(len(data[0]), 98)
        self.assertTrue(data[0]["dosage_administration"])
        self.assertTrue(data[0]["contraindications"])
        self.assertEqual(old, snapshot)

    def test_identity_mismatch_rejected(self):
        for key, value in (("uuid","other"),("scientific_name","different"),
                           ("trade_name_en","other brand"),("strength_text","20mg"),
                           ("dosage_form","CAPSULE"),("route","TOPICAL")):
            review = approval()
            review[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,"identity mismatch"):
                apply_reviews([make_drug()], [review])

    def test_no_unreviewed_medical_info(self):
        review = approval()
        review["approved"] = False
        with self.assertRaisesRegex(ValueError,"approved=true"):
            apply_reviews([make_drug()], [review])

    def test_reviews_need_source_and_reviewer(self):
        bad = approval()
        bad["source_url"] = "http://unverified.example"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            apply_reviews([make_drug()], [bad])
        bad = approval()
        bad["reviewed_by"] = ""
        with self.assertRaisesRegex(ValueError, "medical reviewer"):
            apply_reviews([make_drug()], [bad])

    def test_future_approval_rejected(self):
        bad = approval()
        bad["review_date"] = (date.today()+timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "future"):
            apply_reviews([make_drug()], [bad])

    def test_disallow_unverified_pregnancy_category_and_flag(self):
        for key in ("pregnancy_category", "requires_prescription", "is_active"):
            bad=approval()
            bad["reviewed_fields"][key] = "true"
            with self.subTest(key=key),self.assertRaisesRegex(ValueError, "disallowed"):
                apply_reviews([make_drug()], [bad])

    def test_refuse_overwrite_conflicting_medical_text(self):
        drug=make_drug()
        drug["contraindications"]="Existing approved text"
        with self.assertRaisesRegex(ValueError, "refuses to overwrite"):
            apply_reviews([drug],[approval()])

    def test_duplicate_approvals_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate medical approval"):
            apply_reviews([make_drug()],[approval(),approval()])

    def test_no_approved_records_cannot_create_medical_text(self):
        drug=make_drug()
        original=copy.deepcopy(drug)
        result=apply_reviews([drug],[])
        self.assertEqual(result[0],original)
        self.assertEqual(result[0]["dosage_administration"],"")
        self.assertEqual(result[0]["contraindications"],"")


if __name__ == "__main__":
    unittest.main()
