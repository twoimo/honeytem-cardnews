from hashlib import sha256
from pathlib import Path
import tempfile

from PIL import Image

from support import FixtureCase, generator


class OfflineFixtureTests(FixtureCase):
    def test_generated_assets_are_rgb_native_sized_and_declared_synthetic(self):
        self.assertTrue(self.original["fixture"])
        self.assertEqual(self.original["mode"], "concept")
        for product in self.original["products"]:
            self.assertEqual(product["asset"]["kind"], "synthetic_fixture")
            self.assertIsNone(product["price"])
            self.assertIsNone(product["affiliate_url"])
            with Image.open(self.base_dir / product["asset"]["path"]) as image:
                self.assertEqual(image.size, (600, 420))
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.format, "PNG")

    def test_fixture_image_bytes_deterministic_for_same_font(self):
        with tempfile.TemporaryDirectory(prefix="honeytem-images-", dir=self.root) as temp:
            paths = generator().make_images(Path(temp), self.base_dir / self.original["fonts"]["regular"]["path"])
            for path, product in zip(paths, self.original["products"]):
                expected = (self.base_dir / product["asset"]["path"]).read_bytes()
                self.assertEqual(sha256(path.read_bytes()).digest(), sha256(expected).digest())

    def test_generator_rejects_nonempty_destination_without_overwriting(self):
        destination = self.root / "occupied"
        destination.mkdir(exist_ok=True)
        marker = destination / "keep.txt"
        marker.write_text("keep me", encoding="utf-8")
        fonts = [self.base_dir / self.original["fonts"][key]["path"] for key in ("regular", "bold")]
        with self.assertRaises(ValueError):
            generator().generate(destination, *fonts)
        self.assertEqual(marker.read_text(), "keep me")
        self.assertEqual(list(destination.iterdir()), [marker])

    def test_generator_rejects_font_and_destination_symlinks(self):
        fonts = [self.base_dir / self.original["fonts"][key]["path"] for key in ("regular", "bold")]
        linked_font = self.root / "font-link.ttc"
        linked_font.symlink_to(fonts[0])
        self.addCleanup(linked_font.unlink)
        with self.assertRaises(ValueError):
            generator().generate(self.root / "unsafe-font", linked_font, fonts[1])
        target = self.root / "empty-destination"
        target.mkdir()
        link = self.root / "destination-link"
        link.symlink_to(target, target_is_directory=True)
        self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):
            generator().generate(link, *fonts)
        self.assertEqual(list(target.iterdir()), [])
