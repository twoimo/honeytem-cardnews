"""Geometry/pixel tests prove mechanical invariants, not artistic quality."""
from __future__ import annotations

import copy
from hashlib import sha256
from io import BytesIO

from PIL import Image

from support import FixtureCase
from honeytem.schema import validate_manifest
from honeytem.render import CANVAS_SIZE, VARIANT_NAMES, check_glyph_coverage, rectangles_intersect, render


class RendererInvariantTests(FixtureCase):
    def render_manifest(self, manifest=None):
        job = validate_manifest(manifest or self.manifest, self.base_dir)
        images, qa = render(job.manifest, job.assets, job.fonts[0], job.fonts[1])
        return job, images, qa

    def test_native_source_pixels_preserved_and_product_id_mapping_exact(self):
        job, images, qa = self.render_manifest()
        self.assertEqual(set(images), {"cover.png", "product-01.png", "product-02.png", "product-03.png"})
        cards = [card for card in qa["cards"] if card["role"] == "product"]
        self.assertEqual([card["product_id"] for card in cards], ["01", "02", "03"])
        for card in cards:
            product_id = card["product_id"]
            self.assertEqual(card["filename"], f"product-{product_id}.png")
            asset = card["asset"]
            self.assertEqual(asset["sha256"], sha256(job.assets[product_id]).hexdigest())
            self.assertTrue(asset["pixel_preserved"])
            with Image.open(BytesIO(job.assets[product_id])) as source, Image.open(BytesIO(images[card["filename"]])) as image:
                self.assertEqual(image.size, CANVAS_SIZE)
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(list(source.size), asset["native_size"])
                crop = image.crop(tuple(asset["bounds"]))
                self.assertEqual(crop.size, source.size)
                self.assertEqual(crop.tobytes(), source.tobytes())
                self.assertEqual(asset["pixel_sha256"], sha256(source.tobytes()).hexdigest())

    def test_ten_variants_have_contained_nonoverlapping_readable_geometry(self):
        self.assertEqual(len(VARIANT_NAMES), 10)
        self.assertEqual(len(set(VARIANT_NAMES)), 10)
        cover_hashes = set()
        for variant in range(10):
            with self.subTest(variant=variant):
                manifest = copy.deepcopy(self.original)
                manifest["variant"] = variant
                _, images, qa = self.render_manifest(manifest)
                self.assertEqual(qa["variant"], variant)
                self.assertEqual(qa["canvas"], list(CANVAS_SIZE))
                cover_hashes.add(sha256(images["cover.png"]).hexdigest())
                for card in qa["cards"]:
                    texts = card["texts"]
                    for record in texts:
                        left, top, right, bottom = record["bounds"]
                        self.assertGreaterEqual(left, 0)
                        self.assertGreaterEqual(top, 0)
                        self.assertLessEqual(right, CANVAS_SIZE[0])
                        self.assertLessEqual(bottom, CANVAS_SIZE[1])
                        self.assertGreater(right, left)
                        self.assertGreater(bottom, top)
                        if record["kind"] == "disclosure":
                            self.assertGreaterEqual(record["font_size"], 22)
                        if card.get("asset"):
                            self.assertFalse(rectangles_intersect(record["bounds"], card["asset"]["bounds"]))
                    for i, record in enumerate(texts):
                        for other in texts[i + 1:]:
                            self.assertFalse(rectangles_intersect(record["bounds"], other["bounds"]),
                                             f"variant={variant} {card['filename']} {record['name']} vs {other['name']}")
                    if card.get("asset"):
                        left, top, right, bottom = card["asset"]["bounds"]
                        fl, ft, fr, fb = card["asset"]["frame"]
                        self.assertTrue(0 <= fl <= left < right <= fr <= CANVAS_SIZE[0])
                        self.assertTrue(0 <= ft <= top < bottom <= fb <= CANVAS_SIZE[1])
        self.assertEqual(len(cover_hashes), 10)

    def test_synthetic_disclosure_is_not_false_ai_claim(self):
        _, _, qa = self.render_manifest()
        for card in qa["cards"]:
            self.assertEqual(card["mandatory_label"], "합성 테스트 이미지 · 판매용 아님")
            labels = [record["text"] for record in card["texts"]]
            self.assertIn("합성 테스트 이미지 · 판매용 아님", labels)
            self.assertFalse(any("AI 이미지" in text for text in labels))

    def test_ai_concept_has_explicit_ai_label(self):
        self.manifest["fixture"] = False
        for product in self.manifest["products"]:
            product["asset"]["kind"] = "ai_concept"
        _, _, qa = self.render_manifest()
        self.assertTrue(all(card["mandatory_label"] == "콘셉트 샘플 · AI 이미지" for card in qa["cards"]))

    def test_title_and_comment_overflow_raise_not_clip_or_shrink(self):
        for field in ("title", "comment"):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(self.original)
                self.manifest["products"][0][field] = "가" * 240
                with self.assertRaises(ValueError):
                    self.render_manifest()

    def test_repeated_render_has_identical_png_bytes_and_metadata(self):
        _, first, first_qa = self.render_manifest()
        _, second, second_qa = self.render_manifest()
        self.assertEqual(first, second)
        self.assertEqual(first_qa, second_qa)

    def test_missing_glyph_is_rejected(self):
        job = validate_manifest(self.manifest, self.base_dir)
        with self.assertRaises(ValueError):
            check_glyph_coverage("\u0378", job.fonts[0])

    def test_half_open_rectangle_edges_do_not_overlap(self):
        self.assertFalse(rectangles_intersect((0, 0, 10, 10), (10, 0, 20, 10)))
        self.assertTrue(rectangles_intersect((0, 0, 10, 10), (9, 0, 20, 10)))
