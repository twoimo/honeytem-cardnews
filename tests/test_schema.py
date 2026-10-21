from __future__ import annotations

import copy
import json
from pathlib import Path

from support import FixtureCase
from honeytem.schema import ValidationError, load_manifest, validate_manifest, parse_json


class ManifestValidationTests(FixtureCase):
    def valid(self, value=None):
        return validate_manifest(self.manifest if value is None else value, self.base_dir)

    def invalid(self, value=None):
        with self.assertRaises(ValidationError):
            self.valid(value)

    def test_valid_concept_preserves_ids_and_assets(self):
        job = self.valid()
        self.assertEqual([p["id"] for p in job.manifest["products"]], ["01", "02", "03"])
        self.assertEqual(set(job.assets), {"01", "02", "03"})
        for product in self.manifest["products"]:
            self.assertEqual(job.assets[product["id"]], (self.base_dir / product["asset"]["path"]).read_bytes())
        self.assertEqual(len(job.fingerprint), 64)

    def test_required_top_level_fields(self):
        for field in self.original:
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                del self.manifest[field]
                self.invalid()

    def test_required_product_fields(self):
        for field in self.original["products"][0]:
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                del self.manifest["products"][0][field]
                self.invalid()

    def test_exact_affiliate_url_preserved(self):
        self.manifest = self.affiliate()
        originals = [p["affiliate_url"] for p in self.manifest["products"]]
        job = self.valid()
        self.assertEqual([p["affiliate_url"] for p in job.manifest["products"]], originals)
        self.assertIn("a%2Bb%2Fc&x=1&x=2&empty=&Upper=%2f#keep", originals[0])

    def test_duplicate_or_nonsequential_ids_rejected(self):
        for ids in [("01", "01", "03"), ("02", "01", "03"), ("01", "02", "04")]:
            with self.subTest(ids=ids):
                self.manifest = copy.deepcopy(self.original)
                for product, product_id in zip(self.manifest["products"], ids):
                    product["id"] = product_id
                self.invalid()

    def test_boolean_is_not_integer(self):
        for field in ["schema_version", "variant"]:
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                self.manifest[field] = True
                self.invalid()
        self.manifest = self.affiliate()
        self.manifest["products"][0]["quantity"] = True
        self.invalid()
        self.manifest = self.affiliate()
        self.manifest["products"][0]["price"]["amount"] = True
        self.invalid()
        self.manifest = copy.deepcopy(self.original)
        self.manifest["fonts"]["regular"]["index"] = False
        self.invalid()

    def test_nonfinite_json_rejected(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                raw = json.dumps(self.original).replace('"variant": 0', '"variant": ' + token)
                path = self.base_dir / "nonfinite.json"
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValidationError):
                    load_manifest(path)

    def test_duplicate_json_keys_rejected(self):
        raw = json.dumps(self.original)
        for search, replacement in [('"variant": 0', '"variant": 0, "variant": 1'),
                                    ('"kind": "synthetic_fixture"', '"kind": "actual", "kind": "synthetic_fixture"')]:
            with self.subTest(search=search):
                path = self.base_dir / "duplicate.json"
                path.write_text(raw.replace(search, replacement, 1), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    load_manifest(path)

    def test_unsafe_asset_paths_rejected(self):
        for path in ("../outside.png", "/tmp/outside.png", "input/../input/images/geometry-01.png",
                     "input\\images\\geometry-01.png"):
            with self.subTest(path=path):
                self.manifest = copy.deepcopy(self.original)
                self.manifest["products"][0]["asset"]["path"] = path
                self.invalid()

    def test_asset_symlink_rejected_even_within_input_root(self):
        link = self.base_dir / "linked.png"
        link.symlink_to(self.base_dir / self.original["products"][0]["asset"]["path"])
        self.addCleanup(link.unlink)
        self.manifest["products"][0]["asset"]["path"] = "linked.png"
        self.invalid()

    def test_font_symlink_rejected(self):
        link = self.base_dir / "linked-font.ttc"
        link.symlink_to(self.base_dir / self.original["fonts"]["regular"]["path"])
        self.addCleanup(link.unlink)
        self.manifest["fonts"]["regular"]["path"] = "linked-font.ttc"
        self.invalid()

    def test_concept_cannot_have_sales_claims_or_links(self):
        values = {"program": "toss_sharelink", "seller": "Vendor", "option": "small", "quantity": 1,
                  "price": {"amount": 10, "currency": "KRW", "conditions": "none", "shipping": "none"},
                  "original_url": "https://shop.vendor.co.kr/item", "affiliate_url": "https://share.vendor.co.kr/item",
                  "evidence": {"url": "https://shop.vendor.co.kr/item"}}
        for field, value in values.items():
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                self.manifest["products"][0][field] = value
                self.invalid()

    def test_synthetic_fixture_cannot_be_actual_affiliate(self):
        self.manifest["fixture"] = False
        self.invalid()
        self.manifest = self.affiliate()
        self.manifest["products"][0]["asset"]["kind"] = "synthetic_fixture"
        self.invalid()

    def test_affiliate_requires_price_evidence_and_ownership(self):
        for field in ("price", "evidence", "seller", "option", "quantity", "original_url", "affiliate_url"):
            with self.subTest(field=field):
                self.manifest = self.affiliate()
                self.manifest["products"][0][field] = None
                self.invalid()
        for field in ("claims_verified", "link_ownership_confirmed"):
            with self.subTest(field=field):
                self.manifest = self.affiliate()
                self.manifest["products"][0]["evidence"][field] = False
                self.invalid()

    def test_disclosures_complete_verified_and_matching(self):
        for mutation in ("missing", "unverified", "wrong_program"):
            with self.subTest(mutation=mutation):
                self.manifest = self.affiliate()
                if mutation == "missing":
                    self.manifest["disclosures"] = []
                elif mutation == "unverified":
                    self.manifest["disclosures"][0]["verified"] = False
                else:
                    self.manifest["disclosures"][0]["program"] = "coupang_partners"
                self.invalid()

    def test_invalid_prices_rejected(self):
        for amount in (-1, 0.5, "12000", float("nan"), float("inf")):
            with self.subTest(amount=amount):
                self.manifest = self.affiliate()
                self.manifest["products"][0]["price"]["amount"] = amount
                self.invalid()

    def test_timezone_required(self):
        self.manifest["checked_at"] = "2026-10-21T12:00:00"
        self.invalid()

    def test_asset_usage_and_layout_permission_required(self):
        for field in ("usage_confirmed", "layout_permission_confirmed"):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                self.manifest["products"][0]["asset"][field] = False
                self.invalid()

    def test_fingerprint_changes_with_manifest_asset_code_and_runtime(self):
        from hashlib import sha256
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image
        import honeytem.schema as schema

        baseline = self.valid()
        self.assertEqual(baseline.fingerprint, self.valid().fingerprint)
        self.manifest["title"] = "다른 제목"
        self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        self.manifest = copy.deepcopy(self.original)
        path = self.base_dir / self.manifest["products"][0]["asset"]["path"]
        original_bytes = path.read_bytes()
        with Image.open(BytesIO(original_bytes)) as image:
            image.putpixel((0, 0), (1, 2, 3))
            image.save(path, format="PNG")
        try:
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        finally:
            path.write_bytes(original_bytes)
        self.assertEqual(baseline.input_hashes["assets"]["01"], sha256(original_bytes).hexdigest())
        with patch.object(schema.platform, "python_version", return_value="runtime-test-only"):
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        with patch.object(schema.PIL, "__version__", "pillow-test-only"):
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        with patch.object(schema.fontTools, "__version__", "fonttools-test-only"):
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        with patch.object(schema.features, "version", return_value="native-library-test-only"):
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        source_path = Path(schema.__file__).resolve()
        original_read = Path.read_bytes

        def changed_source(path):
            content = original_read(path)
            return content + b"\n# fingerprint-only test\n" if path.resolve() == source_path else content

        with patch.object(Path, "read_bytes", changed_source):
            self.assertNotEqual(baseline.fingerprint, self.valid().fingerprint)
        self.assertEqual(baseline.fingerprint, self.valid().fingerprint)

    def test_input_hashes_cover_fonts_code_and_runtime(self):
        from hashlib import sha256
        job = self.valid()
        for key, content in zip(("regular", "bold"), job.fonts):
            self.assertEqual(job.input_hashes["fonts"][key], sha256(content).hexdigest())
        self.assertIn("schema.py", job.input_hashes["code"])
        self.assertTrue({"python", "pillow", "fonttools", "freetype", "libjpeg", "zlib", "platform"} <= job.input_hashes["runtime"].keys())

    def test_oversized_or_non_rgb_assets_rejected_without_conversion(self):
        from PIL import Image
        for mode, size in (("RGBA", (600, 420)), ("RGB", (801, 420)), ("RGB", (600, 561)), ("L", (600, 420))):
            with self.subTest(mode=mode, size=size):
                path = self.base_dir / "invalid-image.png"
                Image.new(mode, size).save(path)
                self.manifest = copy.deepcopy(self.original)
                self.manifest["products"][0]["asset"]["path"] = path.name
                self.invalid()

    def test_url_secrets_local_hosts_and_controls_rejected(self):
        urls = ["http://shop.vendor.co.kr/item", "https://user:password@shop.vendor.co.kr/item",
                "https://127.0.0.1/item", "https://service.local/item", "https://shop.vendor.co.kr/a%0a",
                "https://shop.vendor.co.kr/?access_token=secret", "https://shop.vendor.co.kr/#access_token=secret",
                "https://shop.vendor.co.kr/item\n"]
        for url in urls:
            with self.subTest(url=url):
                self.manifest = self.affiliate()
                self.manifest["products"][0]["affiliate_url"] = url
                self.invalid()

    def test_concept_text_cannot_smuggle_prices_or_purchase_links(self):
        tokens = ("지금 12,000원", "USD 10", "$10", "가격 10 KRW", "https://shop.vendor.co.kr/item", "www.shop.vendor.co.kr")
        for token in tokens:
            for field in ("title", "comment"):
                with self.subTest(token=token, field=field):
                    self.manifest = copy.deepcopy(self.original)
                    self.manifest["products"][0][field] = token
                    self.invalid()
        for field in ("title", "subtitle", "edition"):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                self.manifest[field] = "지금 12,000원"
                self.invalid()

    def test_color_profile_and_exif_transform_inputs_fail_closed(self):
        import struct
        from PIL import Image, PngImagePlugin
        variants = ["icc", "gamma", "srgb", "exif_rotation"]
        for variant in variants:
            with self.subTest(variant=variant):
                self.manifest = copy.deepcopy(self.original)
                path = self.base_dir / f"profile-{variant}.png"
                image = Image.new("RGB", (600, 420), (123, 125, 127))
                options = {}
                if variant == "icc":
                    options["icc_profile"] = b"TEST PROFILE: unverified color management"
                elif variant in ("gamma", "srgb"):
                    metadata = PngImagePlugin.PngInfo()
                    metadata.add(b"gAMA" if variant == "gamma" else b"sRGB",
                                 struct.pack(">I", 45455) if variant == "gamma" else b"\x00")
                    options["pnginfo"] = metadata
                else:
                    exif = Image.Exif()
                    exif[274] = 6
                    options["exif"] = exif
                image.save(path, **options)
                self.manifest["products"][0]["asset"]["path"] = path.name
                self.invalid()

    def test_overlong_json_integer_fails_with_sanitized_validation_error(self):
        import sys
        if not hasattr(sys, "get_int_max_str_digits") or not sys.get_int_max_str_digits():
            self.skipTest("Runtime does not enforce an integer string digit limit")
        raw = b'{"number":' + b'9' * (sys.get_int_max_str_digits() + 1) + b'}'
        with self.assertRaises(ValidationError) as error:
            parse_json(raw)
        self.assertEqual(str(error.exception), "manifest: invalid JSON")
