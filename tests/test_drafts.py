from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from honeytem.drafts import (MAX_SOURCE_BYTES, draft_report, handle_cli, ingest_source,
                             init_draft, load_draft, promote_draft, register_cli, review_draft)
from honeytem.schema import ValidationError, canonical, digest, load_manifest

URL = "https://shop.vendor.co.kr/product/01?option=a%2Fb&x=1&x=2#keep"
PROGRAM = "toss_sharelink"


class DraftTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="honeytem-draft-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / "draft"
        self.path = self.directory / "draft.json"

    def source(self, content, name="source.json"):
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def start(self, content=None, name="source.json", **kwargs):
        source = self.source(content, name) if content is not None else None
        return init_draft(URL, PROGRAM, self.directory, source_path=source, **kwargs)

    def check_error(self, operation, *args, **kwargs):
        with self.assertRaises(ValidationError):
            operation(*args, **kwargs)

    def test_url_only_is_useful_unverified_draft(self):
        report = self.start()
        draft = load_draft(self.path)
        p = draft["manifest"]["products"][0]
        self.assertEqual(p["original_url"], URL)
        self.assertIsNone(p["title"])
        self.assertIsNone(p["price"]["amount"])
        self.assertFalse(p["evidence"]["claims_verified"])
        self.assertFalse(p["asset"]["usage_confirmed"])
        self.assertGreater(report["missing_count"], 25)
        self.assertFalse(report["publish_ready"])
        self.assertEqual(report, draft_report(self.path))
        self.check_error(load_manifest, self.path)

    def test_html_jsonld_is_snapshot_bound_candidate_only(self):
        raw = '<html><script>throw new Error("never execute")</script><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"TEST ONLY 제품", "offers":{"@type":"Offer","price":"12000","priceCurrency":"KRW","seller":{"name":"TEST ONLY 판매자"}}}</script></html>'
        result = self.start(raw, "source.html")
        candidate = result["extractions"][0]
        self.assertEqual(candidate["status"], "candidates")
        self.assertEqual(candidate["candidates"]["price.amount"]["value"], 12000)
        self.assertFalse(candidate["candidates"]["title"]["verified"])
        draft = load_draft(self.path)
        self.assertIsNone(draft["manifest"]["products"][0]["title"])
        source = draft["sources"][0]
        self.assertEqual((self.directory / source["path"]).read_bytes(), raw.encode())
        self.assertEqual(source["sha256"], digest(raw.encode()))

    def test_plain_json_and_labeled_text(self):
        for i, (name, content) in enumerate([
            ("item.json", '{"title":"TEST ONLY item","seller":"TEST ONLY shop","price":{"amount":12000,"currency":"KRW"}}'),
            ("item.txt", '상품명: TEST ONLY item\n판매자: TEST ONLY shop\n가격: 12000\n통화: KRW'),
        ]):
            with self.subTest(name=name):
                result = init_draft(URL, PROGRAM, self.root / f"d{i}", source_path=self.source(content, name))
                fields = result["extractions"][0]["candidates"]
                self.assertEqual(fields["title"]["value"], "TEST ONLY item")
                self.assertEqual(fields["price.amount"]["value"], 12000)
                self.assertEqual(fields["price.currency"]["value"], "KRW")

    def test_multiple_products_never_pick_first(self):
        variants = [
            '[{"@type":"Product","name":"one"},{"@type":"Product","name":"two"}]',
            '{"@graph":[{"@type":"Product","name":"one"},{"@type":"Product","name":"two"}]}',
            '{"products":[{"name":"one","price":1},{"name":"two","price":2}]}',
            '[{"name":"one","price":1},{"name":"two","price":2}]',
        ]
        for i, content in enumerate(variants):
            with self.subTest(i=i):
                result = init_draft(URL, PROGRAM, self.root / f"d{i}", source_path=self.source(content))
                extraction = result["extractions"][0]
                self.assertEqual(extraction["status"], "ambiguous")
                self.assertEqual(extraction["candidates"], {})
                self.assertEqual(extraction["product_count"], 2)

    def test_repeated_text_is_ambiguous(self):
        result = self.start("title: one\ntitle: two\nprice: 10", "source.txt")
        self.assertEqual(result["extractions"][0]["status"], "ambiguous")
        self.assertEqual(result["extractions"][0]["candidates"], {})

    def test_multiple_offers_omit_price_not_guess(self):
        result = self.start('{"@type":"Product","name":"item","offers":[{"price":10},{"price":20}]}')
        extraction = result["extractions"][0]
        self.assertEqual(extraction["status"], "candidates")
        self.assertNotIn("price.amount", extraction["candidates"])
        self.assertIn("multiple_offers_not_selected", extraction["warnings"])

    def test_no_rounding_or_inferred_currency(self):
        for i, price in enumerate(["12.34", "1,200", True, -4, "NaN", "1e3"]):
            result = init_draft(URL, PROGRAM, self.root / f"d{i}", source_path=self.source(json.dumps({"@type":"Product", "name":"item", "offers":{"price":price}})))
            fields = result["extractions"][0]["candidates"]
            self.assertNotIn("price.amount", fields)
            self.assertNotIn("price.currency", fields)

    def test_json_float_prices_never_round_or_underflow_into_candidates(self):
        literals = ("999999999.999999999", "1000000000.00000001", "1e-9999",
                    "12.0", "1e3", "-1e-9999", "1e9999")
        for format in ("json", "html"):
            for i, literal in enumerate(literals):
                with self.subTest(format=format, literal=literal):
                    content = '{"@type":"Product","name":"TEST ONLY","offers":{"price":' + literal + ',"priceCurrency":"KRW"}}'
                    if format == "html":
                        content = '<script type="application/ld+json">' + content + '</script>'
                    report = init_draft(URL, PROGRAM, self.root / f"{format}{i}",
                                        source_path=self.source(content, f"source.{format}"))
                    extraction = report["extractions"][0]
                    self.assertEqual(extraction["status"], "candidates")
                    self.assertNotIn("price.amount", extraction["candidates"])

    def test_exact_integer_tokens_and_decimal_strings_still_extract(self):
        for i, literal in enumerate(("0", "1000000000", '"12000"', '"12000.000"')):
            report = init_draft(URL, PROGRAM, self.root / f"exact{i}", source_path=self.source(
                '{"@type":"Product","name":"TEST ONLY","price":' + literal + '}'))
            amount = report["extractions"][0]["candidates"]["price.amount"]["value"]
            self.assertEqual(amount, (0, 1000000000, 12000, 12000)[i])
            self.assertIs(type(amount), int)

    def test_bad_sources_retained_without_candidates(self):
        samples = [("s.json", b'{"name":"one","name":"two","price":10}'),
                   ("s.json", b'{"name":"one","price":NaN}'),
                   ("s.json", b'{oops'), ("s.txt", b'\xff\x00'),
                   ("s.html", b'<script type="application/ld+json">{oops}</script>'),
                   ("s.html", b'<script type="application/ld+json">{}'),
                   ("s.json", ("[" * 60 + "0" + "]" * 60).encode())]
        for i, (name, raw) in enumerate(samples):
            with self.subTest(i=i):
                result = init_draft(URL, PROGRAM, self.root / f"d{i}", source_path=self.source(raw, name))
                self.assertEqual(result["extractions"][0]["status"], "invalid")
                self.assertEqual(result["extractions"][0]["candidates"], {})
                self.assertEqual((self.root / f"d{i}" / f"sources/{digest(raw)}.bin").read_bytes(), raw)

    def test_unstructured_html_and_prose_not_guessed(self):
        for i, (name, data) in enumerate([("a.html", "<h1>item</h1><p>price 12000 won</p>"),
                                        ("a.txt", "The item costs 12000 won and is amazing.")]):
            report = init_draft(URL, PROGRAM, self.root / f"d{i}", source_path=self.source(data, name))
            self.assertEqual(report["extractions"][0]["status"], "no_product")

    def test_url_secret_is_rejected_without_echo(self):
        for value in ("https://shop.vendor.co.kr/?access_token=VERY_SECRET", "https://user:secret@shop.vendor.co.kr/", "https://127.0.0.1/", "http://shop.vendor.co.kr/"):
            with self.assertRaises(ValidationError) as raised:
                init_draft(value, PROGRAM, self.directory)
            self.assertNotIn("VERY_SECRET", str(raised.exception))
        self.assertFalse(self.directory.exists())

    def test_source_unsafe_candidate_urls_omitted(self):
        result = self.start('{"@type":"Product","name":"item","url":"https://shop.vendor.co.kr/?access_token=secret","image":"javascript:alert(1)"}')
        fields = result["extractions"][0]["candidates"]
        self.assertNotIn("product_url", fields)
        self.assertNotIn("image_url", fields)

    def test_source_file_bounds_symlinks_nonregular_and_parent(self):
        oversized = self.source(b"x" * (MAX_SOURCE_BYTES + 1))
        self.check_error(init_draft, URL, PROGRAM, self.directory, source_path=oversized)
        regular = self.source(b"ok", "real.txt")
        link = self.root / "link.txt"
        link.symlink_to(regular)
        self.check_error(init_draft, URL, PROGRAM, self.directory, source_path=link)
        self.check_error(init_draft, URL, PROGRAM, self.directory, source_path=self.root)
        self.check_error(init_draft, URL, PROGRAM, self.directory, source_path=self.root / "sub" / ".." / "real.txt")
        self.assertFalse(self.directory.exists())

    def test_initial_output_not_overwritten(self):
        self.start()
        before = self.path.read_bytes()
        self.check_error(init_draft, URL, PROGRAM, self.directory)
        self.assertEqual(self.path.read_bytes(), before)

    def test_ingest_is_immutable_and_digest_bound(self):
        result = self.start()
        original = self.path.read_bytes()
        source = self.source('{"@type":"Product","name":"item"}')
        output = self.directory / "ingested.json"
        self.check_error(ingest_source, self.path, source, output, expected_draft_sha256="0" * 64)
        after = ingest_source(self.path, source, output, expected_draft_sha256=result["draft_sha256"])
        self.assertEqual(after["revision"], 1)
        self.assertEqual(after["source_count"], 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.check_error(ingest_source, output, source, self.directory / "duplicate.json", expected_draft_sha256=after["draft_sha256"])
        self.check_error(ingest_source, self.path, source, output, expected_draft_sha256=result["draft_sha256"])

    def test_changed_snapshot_or_candidates_fail_closed(self):
        self.start('{"@type":"Product","name":"item"}')
        draft = load_draft(self.path)
        draft["sources"][0]["extraction"]["candidates"]["title"]["verified"] = True
        self.path.write_bytes(canonical(draft))
        self.check_error(load_draft, self.path)
        draft["sources"][0]["extraction"]["candidates"]["title"]["verified"] = False
        self.path.write_bytes(canonical(draft))
        (self.directory / draft["sources"][0]["path"]).write_bytes(b"changed")
        self.check_error(draft_report, self.path)

    def test_snapshot_symlink_rejected(self):
        self.start('{"@type":"Product","name":"item"}')
        draft = load_draft(self.path)
        snap = self.directory / draft["sources"][0]["path"]
        moved = self.root / "snapshot"
        snap.rename(moved)
        snap.symlink_to(moved)
        self.check_error(load_draft, self.path)

    def test_review_changes_only_supplied_fields_and_records_binding(self):
        result = self.start('{"@type":"Product","name":"candidate only","offers":{"price":10}}')
        output = self.directory / "reviewed.json"
        report = review_draft(self.path, {"products":[{"title":"MANUALLY CHECKED title"}]}, output,
                              reviewer="operator", expected_draft_sha256=result["draft_sha256"], reviewed=True)
        draft = load_draft(output)
        product = draft["manifest"]["products"][0]
        self.assertEqual(product["title"], "MANUALLY CHECKED title")
        self.assertIsNone(product["price"]["amount"])
        self.assertFalse(product["evidence"]["claims_verified"])
        self.assertEqual(draft["review_log"][0]["fields"], ["products[0].title"])
        self.assertEqual(draft["review_log"][0]["parent_sha256"], result["draft_sha256"])
        self.assertEqual(report["missing_count"], result["missing_count"] - 1)

    def test_review_requires_explicit_acknowledgement_and_digest(self):
        result = self.start()
        kwargs = {"reviewer":"operator", "expected_draft_sha256":result["draft_sha256"]}
        self.check_error(review_draft, self.path, {"title":"title"}, self.directory / "r.json", **kwargs)
        self.check_error(review_draft, self.path, {"title":"title"}, self.directory / "r.json", **{**kwargs,"reviewed":True,"expected_draft_sha256":"0"*64})

    def test_review_rejects_unknown_fields_identity_changes_and_bad_types(self):
        result = self.start()
        for payload in ({"approved":True}, {"mode":"concept"}, {"fixture":True},
                        {"products":[{"program":"coupang_partners"}]},
                        {"products":[{"original_url":"https://other.vendor.co.kr/"}]},
                        {"products":[{"quantity":True}]},
                        {"products":[{"evidence":{"claims_verified":"true"}}]},
                        {"products":[{"asset":{"path":"../escape.png"}}]},
                        {"products":[{"price":{"currency":"USD"}}]},
                        {"products":[]}, {"title":{}}, {"unknown":{}}, {}):
            self.check_error(review_draft, self.path, payload, self.directory / "r.json", reviewer="operator",
                             expected_draft_sha256=result["draft_sha256"], reviewed=True)
        self.assertFalse((self.directory / "r.json").exists())

    def test_review_can_revoke_a_confirmation(self):
        result = self.start()
        one = self.directory / "r1.json"
        result = review_draft(self.path, {"products":[{"evidence":{"claims_verified":True}}]}, one,
                              reviewer="operator", expected_draft_sha256=result["draft_sha256"], reviewed=True)
        two = self.directory / "r2.json"
        result = review_draft(one, {"products":[{"evidence":{"claims_verified":False}}]}, two,
                              reviewer="operator", expected_draft_sha256=result["draft_sha256"], reviewed=True)
        self.assertIn("products[0].evidence.claims_verified", [item["field"] for item in result["missing_items"]])

    def full_test_patch(self):
        """Synthetic validation fixture, not evidence or authorization for real sales."""
        manifest = deepcopy(load_draft(self.path)["manifest"])
        now = datetime.now(timezone.utc).isoformat()
        manifest.update(edition="TEST ONLY", title="TEST ONLY", subtitle="TEST ONLY", checked_at=now)
        for name in ("regular", "bold"):
            # schema accepts supplied font bytes, renderer separately parses fonts.
            (self.directory / (name + ".ttf")).write_bytes(b"TEST ONLY font stand-in")
            manifest["fonts"][name]["path"] = name + ".ttf"
        Image.new("RGB", (20, 20), "white").save(self.directory / "test.png")
        manifest["channel"].update(status="confirmed", evidence_url="https://channel.vendor.co.kr/test")
        manifest["disclosures"][0].update(text="[광고] 토스 TEST ONLY 수수료를 지급받습니다.",
                                         reference_url="https://policy.vendor.co.kr/test", checked_at=now, verified=True)
        p = manifest["products"][0]
        p.update(title="TEST ONLY", comment="TEST ONLY", seller="TEST ONLY", option="TEST ONLY", quantity=1,
                 affiliate_url="https://share.vendor.co.kr/test")
        p["price"].update(amount=123, currency="KRW", conditions="TEST ONLY", shipping="TEST ONLY")
        p["evidence"].update(url=URL, checked_at=now, claims_verified=True, link_ownership_confirmed=True)
        p["asset"].update(path="test.png", source="TEST ONLY synthetic test data", license="TEST ONLY test fixture",
                          usage_confirmed=True, layout_permission_confirmed=True)
        return manifest

    def reviewed_full(self):
        result = self.start()
        output = self.directory / "reviewed.json"
        result = review_draft(self.path, self.full_test_patch(), output, reviewer="TEST ONLY",
                              expected_draft_sha256=result["draft_sha256"], reviewed=True)
        return output, result

    def test_full_smoke_promotes_exact_valid_manifest_with_receipt(self):
        reviewed, report = self.reviewed_full()
        self.assertEqual(report["missing_count"], 0)
        output = self.directory / "manifest.json"
        promoted = promote_draft(reviewed, output, reviewer="TEST ONLY",
                                  expected_draft_sha256=report["draft_sha256"], reviewed=True)
        job = load_manifest(output)
        self.assertEqual(job.manifest, load_draft(reviewed)["manifest"])
        self.assertEqual(promoted["manifest_sha256"], digest(output.read_bytes()))
        self.assertEqual(promoted["fingerprint"], job.fingerprint)
        self.assertFalse(promoted["publish_ready"])
        self.assertTrue(promoted["operator_assertions_only"])
        self.assertTrue((self.directory / "manifest.json.promotion.json").is_file())
        self.assertNotIn("approved", promoted)
        self.check_error(promote_draft, reviewed, output, reviewer="TEST ONLY",
                         expected_draft_sha256=report["draft_sha256"], reviewed=True)

    def test_incomplete_and_unreviewed_promotion_rejected(self):
        report = self.start()
        self.check_error(promote_draft, self.path, self.directory / "m.json", reviewer="operator",
                         expected_draft_sha256=report["draft_sha256"], reviewed=True)
        self.check_error(promote_draft, self.path, self.directory / "m.json", reviewer="operator",
                         expected_draft_sha256=report["draft_sha256"])
        self.assertFalse((self.directory / "m.json").exists())

    def test_promotion_does_not_bypass_existing_asset_and_disclosure_rules(self):
        reviewed, report = self.reviewed_full()
        Image.new("RGB", (801, 20), "white").save(self.directory / "test.png")
        self.check_error(promote_draft, reviewed, self.directory / "m.json", reviewer="operator",
                         expected_draft_sha256=report["draft_sha256"], reviewed=True)
        Image.new("RGB", (20, 20), "white").save(self.directory / "test.png")
        invalid = self.directory / "bad-disclosure.json"
        result = review_draft(reviewed, {"disclosures":[{"text":"TEST ONLY no commission"}]}, invalid,
                              reviewer="operator", expected_draft_sha256=report["draft_sha256"], reviewed=True)
        self.check_error(promote_draft, invalid, self.directory / "m.json", reviewer="operator",
                         expected_draft_sha256=result["draft_sha256"], reviewed=True)

    def test_output_traversal_outside_symlink_or_collision_rejected(self):
        report = self.start()
        for output in (self.root / "outside.json", self.path, self.directory / "sub" / ".." / "r.json", self.directory / ".hidden.json"):
            self.check_error(review_draft, self.path, {"title":"title"}, output,
                             reviewer="operator", expected_draft_sha256=report["draft_sha256"], reviewed=True)
        target = self.root / "target.json"
        target.write_text("keep")
        link = self.directory / "link.json"
        link.symlink_to(target)
        self.check_error(review_draft, self.path, {"title":"title"}, link,
                         reviewer="operator", expected_draft_sha256=report["draft_sha256"], reviewed=True)
        self.assertEqual(target.read_text(), "keep")

    def test_duplicate_or_nonfinite_draft_json_rejected(self):
        self.start()
        before = self.path.read_text()
        for raw in (before.replace('"revision":0', '"revision":0,"revision":1'),
                    before.replace('"revision":0', '"revision":NaN')):
            self.path.write_text(raw)
            self.check_error(load_draft, self.path)

    def test_all_cli_commands_registration_and_dispatch(self):
        parser = argparse.ArgumentParser()
        register_cli(parser.add_subparsers(dest="command", required=True))
        report = handle_cli(parser.parse_args(["draft-init", URL, "--program", PROGRAM, "--output", str(self.directory)]))
        self.assertEqual(handle_cli(parser.parse_args(["draft-report", str(self.path)]))["draft_sha256"], report["draft_sha256"])
        source = self.source("title: TEST ONLY", "text.txt")
        ingested = self.directory / "ingested.json"
        report = handle_cli(parser.parse_args(["draft-ingest", str(self.path), "--source", str(source), "--output", str(ingested), "--expected-draft-sha256", report["draft_sha256"]]))
        patch_path = self.source('{"title":"TEST ONLY"}', "patch.json")
        reviewed = self.directory / "reviewed.json"
        result = handle_cli(parser.parse_args(["draft-review", str(ingested), "--patch", str(patch_path), "--output", str(reviewed), "--expected-draft-sha256", report["draft_sha256"], "--reviewer", "TEST ONLY", "--reviewed"]))
        self.assertEqual(result["revision"], 2)
        self.check_error(handle_cli, parser.parse_args(["draft-promote", str(reviewed), "--output", str(self.directory / "manifest.json"), "--expected-draft-sha256", result["draft_sha256"], "--reviewer", "TEST ONLY", "--reviewed"]))

    def test_no_network_path(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = self.start('{"@type":"Product","name":"TEST ONLY"}')
            self.assertEqual(report["source_count"], 1)
            self.assertEqual(draft_report(self.path)["draft_sha256"], report["draft_sha256"])


if __name__ == "__main__":
    unittest.main()
