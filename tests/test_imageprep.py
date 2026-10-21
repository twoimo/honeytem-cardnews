"""Real deterministic Pillow/lcms fixtures; no network, AI calls, or real licenses."""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms, ImageDraw, ImageOps

from honeytem.imageprep import (OPERATIONS, handle_cli, load_rights, prepare_image,
                               register_cli, validate_rights, verify_prepared_image)
from honeytem.schema import ValidationError, canonical, digest, validate_manifest
from support import FixtureCase


def rights(**updates):
    record = {"schema_version": 1, "status": "granted", "operator": "Synthetic fixture operator",
              "source": "Locally generated test pixels", "reference": "Synthetic fixture, not a real license",
              "purpose": "Offline unit test only", "program": None, "basis": "own_work",
              "allowed_operations": sorted(OPERATIONS)}
    record.update(updates)
    return record


def pattern(size=(1200, 900), mode="RGB"):
    image = Image.new(mode, size)
    draw = ImageDraw.Draw(image)
    colors = [(220, 30, 80), (40, 210, 90), (15, 80, 230), (245, 220, 40)]
    if mode == "LAB":
        colors = [(128, 128, 128), (180, 155, 110), (90, 130, 150), (220, 120, 140)]
    width, height = size
    for box, color in zip(((0, 0, width // 2, height // 2), (width // 2, 0, width, height // 2),
                           (0, height // 2, width // 2, height), (width // 2, height // 2, width, height)), colors):
        draw.rectangle(box, fill=color)
    return image


class ImagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="honeytem-imageprep-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.serial = 0

    def source(self, image=None, *, filename="source.png", **kwargs):
        path = self.root / filename
        (image or pattern()).save(path, **kwargs)
        return path

    def prepare(self, source, **kwargs):
        self.serial += 1
        output = self.root / ("prepared-" + str(self.serial))
        result = prepare_image(source, output, rights=kwargs.pop("rights", rights()), **kwargs)
        manifest = json.loads((output / "manifest.json").read_bytes())
        return result, manifest, output

    def test_large_geometry_original_preserved_exact_and_reproducible(self):
        source = self.source(pattern((2000, 1500)))
        original = source.read_bytes()
        result, manifest, output = self.prepare(source, allow_transform=True)
        self.assertEqual(result["size"], [747, 560])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual((output / "original.bin").read_bytes(), original)
        self.assertEqual(result["source_sha256"], digest(original))
        self.assertEqual(manifest["geometry"]["scale"], [28, 75])
        self.assertFalse(manifest["geometry"]["crop"])
        self.assertFalse(manifest["geometry"]["upscale"])
        with Image.open(source) as image, Image.open(output / "prepared.png") as prepared:
            self.assertEqual(prepared.tobytes(), image.resize((747, 560), Image.Resampling.LANCZOS).tobytes())
        second, second_manifest, second_output = self.prepare(source, allow_transform=True)
        self.assertEqual(result, second)
        self.assertEqual(manifest, second_manifest)
        self.assertEqual((output / "manifest.json").read_bytes(), (second_output / "manifest.json").read_bytes())

    def test_real_lab_icc_converts_colors_before_stripping(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
        image = pattern((1600, 1200), mode="LAB")
        source = self.source(image, filename="lab.tif", icc_profile=icc)
        original = source.read_bytes()
        result, manifest, output = self.prepare(source, allow_transform=True)
        with Image.open(BytesIO(original)) as decoded:
            expected = ImageCms.profileToProfile(decoded, ImageCms.ImageCmsProfile(BytesIO(icc)),
                                                ImageCms.createProfile("sRGB"), outputMode="RGB",
                                                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, flags=0)
            expected = expected.resize((747, 560), Image.Resampling.LANCZOS)
        with Image.open(output / "prepared.png") as prepared:
            self.assertEqual(prepared.tobytes(), expected.tobytes())
            self.assertNotEqual(prepared.getpixel((30, 30)), (128, 128, 128))
            self.assertEqual(prepared.info, {})
        self.assertEqual(manifest["source"]["icc_sha256"], digest(icc))
        self.assertEqual(manifest["operations"][0]["operation"], "color_convert")
        self.assertEqual(manifest["operations"][-2]["operation"], "strip_metadata")
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(result["byte_exact_copy"])

    def test_genuine_srgb_profile_small_input_is_converted_not_raw_stripped(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = self.source(pattern((50, 40)), icc_profile=icc)
        with patch("honeytem.imageprep.ImageCms.profileToProfile", wraps=ImageCms.profileToProfile) as convert:
            result, manifest, output = self.prepare(source, allow_transform=True)
        self.assertGreaterEqual(convert.call_count, 2)  # preparation plus independent reproduction
        self.assertEqual(result["size"], [50, 40])
        self.assertEqual(manifest["geometry"]["scale"], [1, 1])
        self.assertFalse(result["byte_exact_copy"])
        with Image.open(output / "prepared.png") as image:
            self.assertEqual(image.info, {})

    def test_small_png_noop_preserves_entire_file(self):
        source = self.source(pattern((45, 32)))
        result, manifest, output = self.prepare(source, rights=rights(allowed_operations=["copy"]))
        self.assertTrue(result["byte_exact_copy"])
        self.assertEqual((output / result["output_file"]).read_bytes(), source.read_bytes())
        self.assertEqual(manifest["operations"], [{"operation": "copy", "byte_exact": True}])
        self.assertFalse(manifest["options"]["allow_transform"])

    def test_small_jpeg_noop_preserves_exif_and_original_compression(self):
        image = pattern((80, 50))
        exif = image.getexif()
        exif[274], exif[270], exif[40961] = 1, "Synthetic test description", 1
        source = self.source(image, filename="source.jpg", quality=92, exif=exif)
        result, _, output = self.prepare(source, rights=rights(allowed_operations=["copy"]))
        self.assertTrue(result["byte_exact_copy"])
        self.assertEqual(result["output_file"], "prepared.jpg")
        self.assertEqual((output / "prepared.jpg").read_bytes(), source.read_bytes())

    def test_no_implicit_resize_or_profile_conversion(self):
        for source in (self.source(), self.source(pattern((40, 30)), filename="icc.png",
                         icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())):
            with self.subTest(source=source.name), self.assertRaises(ValidationError):
                self.prepare(source)
        self.assertFalse(any(self.root.glob("prepared-*")))

    def test_denied_rights_never_overridden(self):
        source = self.source()
        for status in ("denied", "unknown", None, True):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.prepare(source, rights=rights(status=status), allow_transform=True)
        self.assertFalse(any(self.root.glob("prepared-*")))

    def test_rights_require_scoped_operations_and_provenance(self):
        for change in ({"operator": ""}, {"purpose": "todo"}, {"reference": "unknown"},
                       {"source": ""}, {"allowed_operations": ["copy", "copy"]},
                       {"allowed_operations": ["copy", "anything"]}, {"allowed_operations": ["resize"]},
                       {"allowed_operations": [["copy"]]}, {"program": []}, {"schema_version": True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_rights(rights(**change))
        with self.assertRaises(ValidationError):
            validate_rights({**rights(), "blanket_bypass": True})

    def test_each_intended_operation_needs_permission(self):
        source = self.source()
        for removed in ("resize", "encode_png", "strip_metadata", "copy"):
            with self.subTest(removed=removed), self.assertRaises(ValidationError):
                self.prepare(source, rights=rights(allowed_operations=sorted(OPERATIONS - {removed})), allow_transform=True)

    def test_toss_program_policy_never_allows_transform(self):
        source = self.source()
        with self.assertRaisesRegex(ValidationError, "Toss program-policy"):
            self.prepare(source, rights=rights(program="toss_sharelink", basis="program_policy"), allow_transform=True)
        for basis in ("own_work", "separate_permission"):
            result, _, _ = self.prepare(source, rights=rights(program="toss_sharelink", basis=basis), allow_transform=True)
            self.assertTrue(result["verified"])

    def test_toss_program_policy_allows_only_unchanged_compliant_copy(self):
        source = self.source(pattern((40, 30)))
        result, _, _ = self.prepare(source, rights=rights(program="toss_sharelink", basis="program_policy",
                                                        allowed_operations=["copy"]))
        self.assertTrue(result["byte_exact_copy"])

    def test_exif_transpose_explicit_and_exact_pixels(self):
        image = pattern((1200, 600))
        exif = image.getexif()
        exif[274] = 6
        source = self.source(image, filename="oriented.jpg", exif=exif, quality=95)
        with self.assertRaisesRegex(ValidationError, "allow-exif-transpose"):
            self.prepare(source, allow_transform=True)
        result, manifest, output = self.prepare(source, allow_transform=True, allow_exif_transpose=True)
        with Image.open(source) as original, Image.open(output / "prepared.png") as prepared:
            expected = ImageOps.exif_transpose(original).resize((280, 560), Image.Resampling.LANCZOS)
            self.assertEqual(prepared.tobytes(), expected.tobytes())
            self.assertNotIn(274, prepared.getexif())
        self.assertEqual(result["size"], [280, 560])
        self.assertEqual(manifest["source"]["size"], [1200, 600])
        self.assertEqual(manifest["geometry"]["oriented_size"], [600, 1200])
        self.assertEqual(manifest["operations"][0], {"operation": "exif_transpose", "orientation": 6})

    def test_exif_transpose_requires_separate_operation_rights(self):
        image = pattern((30, 20))
        exif = image.getexif()
        exif[274] = 3
        source = self.source(image, exif=exif)
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True, allow_exif_transpose=True,
                         rights=rights(allowed_operations=sorted(OPERATIONS - {"exif_transpose"})))

    def test_alpha_matte_explicit_and_recorded(self):
        source = self.source(Image.new("RGBA", (30, 20), (255, 0, 0, 128)))
        with self.assertRaisesRegex(ValidationError, "explicit matte"):
            self.prepare(source, allow_transform=True)
        result, manifest, output = self.prepare(source, allow_transform=True, matte="#ffffff")
        with Image.open(output / "prepared.png") as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 127, 127))
            self.assertEqual(image.mode, "RGB")
        flatten = next(op for op in manifest["operations"] if op["operation"] == "flatten_alpha")
        self.assertEqual(flatten["matte"], "#FFFFFF")
        self.assertEqual(manifest["options"]["matte"], "#FFFFFF")
        self.assertEqual(result["size"], [30, 20])

    def test_profile_transparent_input_converts_before_alpha_composite(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = self.source(Image.new("RGBA", (900, 700), (20, 40, 60, 100)), icc_profile=icc)
        _, manifest, output = self.prepare(source, allow_transform=True, matte="#000000")
        names = [op["operation"] for op in manifest["operations"]]
        self.assertLess(names.index("color_convert"), names.index("flatten_alpha"))
        self.assertLess(names.index("flatten_alpha"), names.index("resize"))
        with Image.open(output / "prepared.png") as image:
            self.assertEqual(image.getpixel((20, 20)), (8, 16, 24))

    def test_opaque_alpha_still_requires_explicit_matte(self):
        source = self.source(Image.new("RGBA", (30, 20), (10, 20, 30, 255)))
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)

    def test_rgb_transparency_chunk_is_not_a_silent_noop(self):
        image = Image.new("RGB", (30, 20), (20, 40, 60))
        image.putpixel((0, 0), (255, 0, 0))
        source = self.source(image, transparency=(20, 40, 60))
        with self.assertRaisesRegex(ValidationError, "explicit matte"):
            self.prepare(source, allow_transform=True)
        _, manifest, output = self.prepare(source, allow_transform=True, matte="#FFFFFF")
        self.assertTrue(manifest["source"]["has_alpha_or_transparency"])
        with Image.open(output / "prepared.png") as prepared:
            self.assertEqual(prepared.getpixel((1, 1)), (255, 255, 255))
            self.assertEqual(prepared.getpixel((0, 0)), (255, 0, 0))

    def test_icc_rgb_transparency_chunk_uses_rgb_cms_input(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = self.source(Image.new("RGB", (30, 20), (20, 40, 60)), transparency=(20, 40, 60), icc_profile=profile)
        _, _, output = self.prepare(source, allow_transform=True, matte="#FFFFFF")
        with Image.open(output / "prepared.png") as prepared:
            self.assertEqual(prepared.getpixel((1, 1)), (255, 255, 255))

    def test_invalid_matte_and_inconsistent_flags_rejected(self):
        source = self.source(pattern((20, 10)))
        for options in ({"matte": "white", "allow_transform": True}, {"matte": "#FFF", "allow_transform": True},
                        {"matte": "#FFFFFF"}, {"allow_exif_transpose": True}, {"allow_transform": 1}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.prepare(source, **options)

    def test_grayscale_conversion_is_explicit(self):
        source = self.source(Image.new("L", (20, 10), 73))
        with self.assertRaises(ValidationError):
            self.prepare(source)
        _, manifest, output = self.prepare(source, allow_transform=True)
        self.assertEqual(manifest["operations"][0]["method"], "untagged_gray_channel_replication")
        with Image.open(output / "prepared.png") as image:
            self.assertEqual(image.getpixel((0, 0)), (73, 73, 73))

    def test_malformed_and_mismatched_profiles_fail_closed(self):
        fake = bytearray(128)
        fake[:4] = (128).to_bytes(4, "big")
        fake[36:40] = b"acsp"
        invalid = [b"not an ICC", bytes(fake),
                   ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()]
        for number, profile in enumerate(invalid):
            source = self.source(pattern((20, 10)), filename=f"bad-{number}.png", icc_profile=profile)
            with self.subTest(number=number), self.assertRaises(ValidationError):
                self.prepare(source, allow_transform=True)
        self.assertFalse(any(self.root.glob("prepared-*")))

    def test_profile_header_length_must_match(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes() + b"extra"
        source = self.source(pattern((20, 10)), icc_profile=profile)
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)

    def test_non_icc_color_tags_are_not_silently_stripped(self):
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add(b"gAMA", struct.pack(">I", 45455))
        source = self.source(pattern((20, 10)), pnginfo=info)
        with self.assertRaisesRegex(ValidationError, "color tags"):
            self.prepare(source, allow_transform=True)

    def test_non_srgb_exif_without_icc_rejected(self):
        image = pattern((20, 10))
        exif = image.getexif()
        exif[40961] = 65535
        source = self.source(image, filename="adobergb.jpg", exif=exif)
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)

    def test_cmyk_without_profile_and_unsupported_palette_rejected(self):
        for number, image in enumerate((Image.new("CMYK", (20, 10)), Image.new("P", (20, 10)))):
            source = self.source(image, filename=f"unsupported-{number}.tif")
            with self.subTest(number=number), self.assertRaises(ValidationError):
                self.prepare(source, allow_transform=True)

    def test_oriented_tiff_rejected_instead_of_hidden_decoder_transform(self):
        image = pattern((30, 20))
        exif = image.getexif()
        exif[274] = 6
        source = self.source(image, filename="oriented.tif", exif=exif)
        with self.assertRaisesRegex(ValidationError, "oriented TIFF"):
            self.prepare(source, allow_transform=True, allow_exif_transpose=True)

    def test_animated_and_multiframe_inputs_rejected(self):
        image = pattern((30, 20))
        for extension in ("png", "tif", "gif"):
            source = self.source(image, filename="animated." + extension, save_all=True,
                                 append_images=[Image.new("RGB", image.size, "blue")])
            with self.subTest(extension=extension), self.assertRaises(ValidationError):
                self.prepare(source, allow_transform=True)

    def test_unsupported_format_and_truncated_image_rejected(self):
        source = self.source(pattern((20, 10)), filename="source.bmp")
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)
        source = self.source()
        source.write_bytes(source.read_bytes()[:70])
        with self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)

    def test_decompression_bomb_warning_and_error_both_rejected(self):
        source = self.source(pattern((100, 100)))
        for limit in (6000, 100):
            with self.subTest(limit=limit), patch("PIL.Image.MAX_IMAGE_PIXELS", limit), self.assertRaises(ValidationError):
                self.prepare(source, allow_transform=True)

    def test_own_pixel_limit_and_extreme_aspect_rejected(self):
        source = self.source(pattern((100, 100)))
        with patch("honeytem.imageprep.MAX_SOURCE_PIXELS", 100), self.assertRaises(ValidationError):
            self.prepare(source, allow_transform=True)
        source = self.source(Image.new("RGB", (2000, 1)), filename="extreme.png")
        with self.assertRaisesRegex(ValidationError, "aspect ratio"):
            self.prepare(source, allow_transform=True)

    def test_output_overwrite_never_allowed(self):
        source = self.source(pattern((30, 20)))
        _, _, output = self.prepare(source)
        before = {file.name: file.read_bytes() for file in output.iterdir()}
        with self.assertRaises(ValidationError):
            prepare_image(source, output, rights=rights())
        self.assertEqual(before, {file.name: file.read_bytes() for file in output.iterdir()})
        with self.assertRaises(ValidationError):
            prepare_image(source, source, rights=rights())

    def test_input_output_and_ancestor_symlinks_rejected(self):
        source = self.source(pattern((30, 20)))
        link = self.root / "source-link.png"
        link.symlink_to(source)
        with self.assertRaises(ValidationError):
            self.prepare(link)
        directory_link = self.root / "directory-link"
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValidationError):
            self.prepare(directory_link / source.name)
        with self.assertRaises(ValidationError):
            prepare_image(source, directory_link / "new", rights=rights())
        output_link = self.root / "output-link"
        output_link.symlink_to(self.root / "nonexistent")
        with self.assertRaises(ValidationError):
            prepare_image(source, output_link, rights=rights())

    def test_parent_traversal_and_nonregular_source_rejected(self):
        source = self.source(pattern((30, 20)))
        with self.assertRaises(ValidationError):
            self.prepare(self.root / ".." / self.root.name / source.name)
        fifo = self.root / "pipe.png"
        os.mkfifo(fifo)
        with self.assertRaises(ValidationError):
            self.prepare(fifo)

    def test_source_modified_during_processing_rejected(self):
        source = self.source(pattern((30, 20)))
        raw = source.read_bytes()
        with patch("honeytem.imageprep.safe_read", side_effect=[raw, b"changed"]), self.assertRaises(ValidationError):
            self.prepare(source)
        self.assertFalse(any(self.root.glob("prepared-*")))

    def test_verify_trusted_digest_and_tamper_detection(self):
        source = self.source()
        result, manifest, output = self.prepare(source, allow_transform=True)
        self.assertTrue(verify_prepared_image(output, expected_manifest_sha256=result["manifest_sha256"])["verified"])
        for expected in ("f" * 64, "invalid", 123):
            with self.subTest(expected=expected), self.assertRaises(ValidationError):
                verify_prepared_image(output, expected_manifest_sha256=expected)
        image_path = output / "prepared.png"
        original_output = image_path.read_bytes()
        Image.new("RGB", tuple(result["size"]), "red").save(image_path)
        # Even updating both stored hashes does not evade source reconstruction.
        manifest["output"]["sha256"] = digest(image_path.read_bytes())
        manifest["output"]["bytes"] = image_path.stat().st_size
        with Image.open(image_path) as image:
            manifest["output"]["pixel_sha256"] = digest(image.tobytes())
        (output / "manifest.json").write_bytes(canonical(manifest))
        with self.assertRaises(ValidationError):
            verify_prepared_image(output)
        self.assertNotEqual(original_output, image_path.read_bytes())

    def test_verify_rejects_modified_audit_original_and_extra_files(self):
        source = self.source()
        for case in ("audit", "original", "extra", "runtime", "rights", "options"):
            _, manifest, output = self.prepare(source, allow_transform=True)
            if case == "audit":
                manifest["operations"] = []
            elif case == "runtime":
                manifest["runtime"]["pillow"] = "different"
            elif case == "rights":
                manifest["rights"]["status"] = "denied"
            elif case == "options":
                manifest["options"]["allow_transform"] = False
            elif case == "original":
                (output / "original.bin").write_bytes(b"corrupt")
            else:
                (output / "unexpected.txt").write_text("unexpected")
            (output / "manifest.json").write_bytes(canonical(manifest))
            with self.subTest(case=case), self.assertRaises(ValidationError):
                verify_prepared_image(output)

    def test_verify_rejects_member_symlinks_and_bad_manifest(self):
        source = self.source(pattern((30, 20)))
        _, _, output = self.prepare(source)
        (output / "original.bin").unlink()
        (output / "original.bin").symlink_to(source)
        with self.assertRaises(ValidationError):
            verify_prepared_image(output)
        _, _, output = self.prepare(source)
        (output / "manifest.json").write_bytes(b'{"schema_version": 1, "schema_version": 1}')
        with self.assertRaises(ValidationError):
            verify_prepared_image(output)

    def test_cli_registration_and_dispatch(self):
        parser = argparse.ArgumentParser()
        register_cli(parser.add_subparsers(dest="command", required=True))
        source = self.source()
        rights_path = self.root / "rights.json"
        rights_path.write_bytes(canonical(rights()))
        output = self.root / "cli-prepared"
        args = parser.parse_args(["prepare-image", str(source), "--rights", str(rights_path),
                                  "--output", str(output), "--allow-transform"])
        result = handle_cli(args)
        self.assertTrue(result["verified"])
        args = parser.parse_args(["verify-image", str(output), "--expected-manifest-sha256", result["manifest_sha256"]])
        self.assertEqual(handle_cli(args), result)
        self.assertEqual(load_rights(rights_path), rights())


class PreparedAssetSchemaTests(FixtureCase):
    def test_prepared_asset_fits_unchanged_strict_schema(self):
        directory = self.base_dir / "imageprep-integration"
        directory.mkdir(exist_ok=True)
        source = directory / "large-profiled.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        pattern((1600, 1200)).save(source, icc_profile=profile)
        self.manifest["products"][0]["asset"]["path"] = str(source.relative_to(self.base_dir))
        with self.assertRaises(ValidationError):
            validate_manifest(self.manifest, self.base_dir)
        output = directory / "prepared"
        prepare_image(source, output, rights=rights(), allow_transform=True)
        asset = self.manifest["products"][0]["asset"]
        asset["path"] = str((output / "prepared.png").relative_to(self.base_dir))
        asset["source"] = "Synthetic local fixture; preparation audit: imageprep-integration/prepared/manifest.json"
        asset["license"] = "Synthetic fixture, not a real product license"
        job = validate_manifest(self.manifest, self.base_dir)
        self.assertEqual(job.assets["01"], (output / "prepared.png").read_bytes())
        asset["usage_confirmed"] = False
        with self.assertRaises(ValidationError):
            validate_manifest(self.manifest, self.base_dir)


if __name__ == "__main__":
    unittest.main()
