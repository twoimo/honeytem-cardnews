from __future__ import annotations

import copy

from support import FixtureCase
from honeytem.adapters import import_result, tasks
from honeytem.copy import caption, links
from honeytem.schema import ValidationError, validate_manifest


class CopyAndAdapterTests(FixtureCase):
    def job(self, affiliate=False):
        return validate_manifest(self.affiliate() if affiliate else self.manifest, self.base_dir)

    def result(self, job):
        return {"schema_version": 1, "input_fingerprint": job.fingerprint, "product_id": "01",
                "kind": "editorial_copy", "reviewed": True,
                "output": {"title": "새로운 도형", "comment": "다른 여백을 살펴봅니다.", "evidence_urls": []}}

    def test_advertising_disclosure_is_caption_first_line(self):
        job = self.job(affiliate=True)
        output = caption(job)
        self.assertEqual(output.splitlines()[0], job.manifest["disclosures"][0]["text"])
        self.assertTrue(output.startswith("[광고] "))
        self.assertLess(output.index("수수료"), output.index(job.manifest["title"]))
        self.assertIn("12,000원", output)
        self.assertIn("배송비 별도 3000원", output)
        self.assertIn(job.manifest["products"][0]["evidence"]["checked_at"], output)

    def test_link_mapping_and_query_strings_are_exact(self):
        job = self.job(affiliate=True)
        for record, product in zip(links(job), job.manifest["products"]):
            for field in ("id", "title", "program", "option", "original_url", "affiliate_url"):
                self.assertEqual(record[field], product[field])

    def test_concept_caption_has_no_fake_price_or_advertising_relationship(self):
        job = self.job()
        output = caption(job)
        self.assertTrue(output.startswith("합성 테스트 이미지 · 판매용 아님\n"))
        self.assertNotIn("[광고]", output)
        self.assertNotIn("수수료", output)
        self.assertTrue(all(item["affiliate_url"] is None for item in links(job)))

    def test_unconfirmed_profile_links_not_claimed(self):
        manifest = self.affiliate()
        manifest["channel"]["profile_links_confirmed"] = False
        output = caption(validate_manifest(manifest, self.base_dir))
        self.assertNotIn("프로필 링크", output)
        self.assertIn("별도 링크 목록", output)

    def test_exported_tasks_are_unexecuted_and_fingerprint_bound(self):
        job = self.job()
        output = tasks(job)
        self.assertEqual(output["fingerprint"], job.fingerprint)
        self.assertIn("no model executed", output["execution"])
        self.assertEqual(len(output["tasks"]), 9)
        self.assertTrue(all(item["status"] == "not_started" for item in output["tasks"]))
        self.assertTrue(all(item["input_fingerprint"] == job.fingerprint for item in output["tasks"]))
        self.assertEqual({item["product_id"] for item in output["tasks"]}, {"01", "02", "03"})

    def test_reviewed_result_creates_new_manifest_without_mutating_input(self):
        job = self.job()
        before = copy.deepcopy(job.manifest)
        result = self.result(job)
        updated = import_result(job, result, self.base_dir)
        self.assertEqual(job.manifest, before)
        self.assertEqual(updated.manifest["products"][0]["title"], result["output"]["title"])
        self.assertNotEqual(updated.fingerprint, job.fingerprint)

    def test_stale_unreviewed_bad_id_and_boolean_version_results_rejected(self):
        job = self.job()
        for field, value in (("input_fingerprint", "0" * 64), ("reviewed", False),
                             ("product_id", "04"), ("schema_version", True)):
            with self.subTest(field=field):
                result = self.result(job)
                result[field] = value
                with self.assertRaises(ValidationError):
                    import_result(job, result, self.base_dir)

    def test_research_adapter_cannot_change_issued_or_original_links(self):
        job = self.job(affiliate=True)
        for field in ("affiliate_url", "original_url"):
            with self.subTest(field=field):
                output = copy.deepcopy(job.manifest["products"][0])
                output[field] = "https://share.vendor.co.kr/changed"
                result = self.result(job)
                result.update(kind="product_research", output=output)
                with self.assertRaises(ValidationError):
                    import_result(job, result, self.base_dir)

    def test_affiliate_editorial_adapter_requires_evidence(self):
        job = self.job(affiliate=True)
        with self.assertRaises(ValidationError):
            import_result(job, self.result(job), self.base_dir)

    def test_mixed_program_disclosures_all_precede_caption_body(self):
        manifest = self.affiliate()
        programs = ["toss_sharelink", "coupang_partners", "kakao_gift_curator"]
        manifest["disclosures"] = []
        labels = {"toss_sharelink": "토스쇼핑 쉐어링크", "coupang_partners": "쿠팡 파트너스", "kakao_gift_curator": "선물하기 큐레이터"}
        for product, program in zip(manifest["products"], programs):
            product["program"] = program
            manifest["disclosures"].append({"program": program,
                "text": f"[광고] {labels[program]} 활동으로 주문 시 수수료를 지급받습니다.",
                "reference_url": "https://policy.vendor.co.kr/disclosure",
                "checked_at": manifest["checked_at"], "verified": True})
        job = validate_manifest(manifest, self.base_dir)
        lines = caption(job).splitlines()
        self.assertEqual(lines[:3], [item["text"] for item in manifest["disclosures"]])
        self.assertEqual({item["program"] for item in links(job)}, set(programs))

    def test_editorial_evidence_must_match_retained_product_evidence(self):
        job = self.job(affiliate=True)
        result = self.result(job)
        result["output"]["evidence_urls"] = ["https://shop.vendor.co.kr/new-evidence-not-retained"]
        with self.assertRaises(ValidationError):
            import_result(job, result, self.base_dir)
        result["output"]["evidence_urls"] = [job.manifest["products"][0]["evidence"]["url"]]
        updated = import_result(job, result, self.base_dir)
        self.assertEqual(updated.manifest["products"][0]["evidence"], job.manifest["products"][0]["evidence"])
