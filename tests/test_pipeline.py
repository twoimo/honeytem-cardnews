from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from support import FixtureCase, REPO
from honeytem.pipeline import build, record_approval, verify
from honeytem.schema import ValidationError


class PipelineIntegrityTests(FixtureCase):
    def test_cold_build_is_reproducible_with_exact_originals_and_manifest(self):
        first = build(self.manifest_path, self.output_directory())
        second = build(self.manifest_path, self.output_directory())
        self.assertFalse(first["resumed"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["package_sha256"], second["package_sha256"])
        first_root, second_root = Path(first["run_dir"]), Path(second["run_dir"])
        self.assertEqual((first_root / "package.zip").read_bytes(), (second_root / "package.zip").read_bytes())
        self.assertEqual(verify(first_root)["integrity"], "passed")
        manifest = json.loads((first_root / "package-manifest.json").read_text())
        with zipfile.ZipFile(first_root / "package.zip") as archive:
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            self.assertEqual(set(archive.namelist()), set(manifest["files"]) | {"package-manifest.json"})
            for entry in archive.infolist():
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
            for name, expected in manifest["files"].items():
                self.assertEqual(sha256(archive.read(name)).hexdigest(), expected)
                self.assertEqual(archive.read(name), (first_root / "rendered" / name).read_bytes())
            for product in self.original["products"]:
                original = (self.base_dir / product["asset"]["path"]).read_bytes()
                self.assertEqual(archive.read(f"originals/{product['id']}.png"), original)
        qa = json.loads((first_root / "rendered/qa.json").read_text())
        self.assertFalse(qa["publish_ready"])
        self.assertEqual(qa["manual_gates"]["visual_aesthetic_and_semantic_quality"], "pending_operator_review")
        self.assertEqual(qa["manual_gates"]["model_quality_non_regression"], "blocked_no_real_model_comparison")

    def test_stop_resume_skips_validated_and_rendered_stage_rewrites(self):
        output = self.output_directory()
        first = build(self.manifest_path, output, "validated")
        root = Path(first["run_dir"])
        validated = root / "validated.json"
        stamp = validated.stat().st_mtime_ns
        self.assertFalse((root / "rendered").exists())
        second = build(self.manifest_path, output, "rendered")
        self.assertTrue(second["resumed"])
        self.assertEqual(validated.stat().st_mtime_ns, stamp)
        image = root / "rendered/product-01.png"
        image_stamp = image.stat().st_mtime_ns
        third = build(self.manifest_path, output)
        self.assertEqual(third["stage"], "packaged")
        self.assertEqual(image.stat().st_mtime_ns, image_stamp)
        package_stamp = (root / "package.zip").stat().st_mtime_ns
        fourth = build(self.manifest_path, output)
        self.assertTrue(fourth["resumed"])
        self.assertEqual((root / "package.zip").stat().st_mtime_ns, package_stamp)

    def test_tampered_artifact_fails_resume_and_verify_without_overwrite(self):
        output = self.output_directory()
        result = build(self.manifest_path, output)
        root = Path(result["run_dir"])
        target = root / "rendered/caption.txt"
        poisoned = b"TAMPERED"
        target.write_bytes(poisoned)
        for operation in (lambda: build(self.manifest_path, output), lambda: verify(root)):
            with self.assertRaises(ValidationError):
                operation()
        self.assertEqual(target.read_bytes(), poisoned)

    def test_extra_or_missing_rendered_files_are_detected(self):
        output = self.output_directory()
        result = build(self.manifest_path, output)
        root = Path(result["run_dir"])
        extra = root / "rendered/extra.txt"
        extra.write_text("unexpected", encoding="utf-8")
        with self.assertRaises(ValidationError):
            verify(root)
        extra.unlink()
        (root / "rendered/links.json").unlink()
        with self.assertRaises(ValidationError):
            build(self.manifest_path, output)

    def test_orphan_render_checkpoint_recovers_only_identical_content(self):
        output = self.output_directory()
        result = build(self.manifest_path, output, "rendered")
        root = Path(result["run_dir"])
        state_path = root / "state.json"
        state = json.loads(state_path.read_text())
        del state["stages"]["rendered"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        resumed = build(self.manifest_path, output)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(verify(root)["integrity"], "passed")
        state = json.loads(state_path.read_text())
        del state["stages"]["rendered"]
        del state["stages"]["packaged"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (root / "rendered/caption.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValidationError):
            build(self.manifest_path, output)

    def test_changed_manifest_gets_fresh_run(self):
        output = self.output_directory()
        original = build(self.manifest_path, output, "validated")
        self.manifest["title"] = "다른 제목"
        changed = build(self.write_manifest(), output, "validated")
        self.assertNotEqual(original["fingerprint"], changed["fingerprint"])
        self.assertFalse(changed["resumed"])
        self.assertTrue(Path(original["run_dir"]).is_dir())

    def test_output_may_not_contain_source_or_follow_symlinks(self):
        with self.assertRaises(ValidationError):
            build(self.manifest_path, self.base_dir)
        output = self.output_directory()
        link = self.root / ("linked-output-" + output.name)
        link.symlink_to(output, target_is_directory=True)
        self.addCleanup(link.unlink)
        with self.assertRaises(ValidationError):
            build(self.manifest_path, link)

    def test_approval_binds_exact_hash_without_enabling_publication(self):
        result = build(self.manifest_path, self.output_directory())
        root = Path(result["run_dir"])
        with self.assertRaises(ValidationError):
            record_approval(root, "0" * 64, "Test reviewer")
        self.assertFalse((root / "approval.json").exists())
        receipt = record_approval(root, result["package_sha256"], "Test reviewer")
        self.assertFalse(receipt["publish_ready"])
        verified = verify(root)
        self.assertTrue(verified["operator_receipt_present"])
        self.assertFalse(verified["publish_ready"])
        with self.assertRaises(ValidationError):
            record_approval(root, result["package_sha256"], "Second reviewer")
        receipt["package_sha256"] = "f" * 64
        (root / "approval.json").write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(ValidationError):
            verify(root)

    def test_stale_lock_fails_closed(self):
        output = self.output_directory()
        (output / ".honeytem.lock").write_text("12345", encoding="utf-8")
        with self.assertRaises(ValidationError):
            build(self.manifest_path, output)
        self.assertEqual((output / ".honeytem.lock").read_text(), "12345")

    def test_cli_error_does_not_echo_secret_input(self):
        # Deliberately obvious public fixture, never an account credential.
        secret = "public-test-only-do-not-echo"
        self.manifest = self.affiliate()
        self.manifest["products"][0]["affiliate_url"] = "https://share.vendor.co.kr/?access_token=" + secret
        path = self.write_manifest(name="secret-input.json")
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        result = subprocess.run([sys.executable, "-m", "honeytem", "validate", str(path)],
                                cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error", json.loads(result.stderr))

    def test_malformed_checkpoint_structure_fails_with_validation_error(self):
        for malformed in ([], {"fingerprint": None, "stages": {}},
                          {"schema_version": 1, "fingerprint": "MATCH", "stages": {"validated": []}},
                          {"schema_version": 1, "fingerprint": "MATCH", "stages": {"validated": {}}}):
            with self.subTest(malformed=malformed):
                output = self.output_directory()
                result = build(self.manifest_path, output, "validated")
                root = Path(result["run_dir"])
                if isinstance(malformed, dict) and malformed.get("fingerprint") == "MATCH":
                    malformed = dict(malformed, fingerprint=result["fingerprint"])
                (root / "state.json").write_text(json.dumps(malformed), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    build(self.manifest_path, output)

    def test_cli_verify_trusted_expected_digest_rejects_mismatch(self):
        result = build(self.manifest_path, self.output_directory())
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        command = [sys.executable, "-m", "honeytem", "verify", result["run_dir"], "--expected-sha256"]
        bad = subprocess.run(command + ["0" * 64], cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(bad.returncode, 2)
        good = subprocess.run(command + [result["package_sha256"]], cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(good.returncode, 0)
        self.assertEqual(json.loads(good.stdout)["package_sha256"], result["package_sha256"])

    def test_jpeg_bytes_and_decoded_native_pixels_both_preserved(self):
        from io import BytesIO
        from PIL import Image
        source = self.base_dir / self.manifest["products"][0]["asset"]["path"]
        jpeg = self.base_dir / "geometry-jpeg.jpg"
        with Image.open(source) as image:
            image.save(jpeg, format="JPEG", quality=90)
        self.manifest["products"][0]["asset"]["path"] = jpeg.name
        result = build(self.write_manifest(name="jpeg-manifest.json"), self.output_directory())
        root = Path(result["run_dir"])
        self.assertEqual((root / "rendered/originals/01.jpg").read_bytes(), jpeg.read_bytes())
        qa = json.loads((root / "rendered/qa.json").read_text())
        record = next(card for card in qa["geometry"]["cards"] if card.get("product_id") == "01" and card["role"] == "product")
        with Image.open(jpeg) as original, Image.open(root / "rendered/product-01.png") as card:
            self.assertEqual(card.crop(record["asset"]["bounds"]).tobytes(), original.tobytes())

    def test_state_symlink_is_not_followed_on_resume_or_verify(self):
        output = self.output_directory()
        result = build(self.manifest_path, output)
        root = Path(result["run_dir"])
        state = root / "state.json"
        backup = root / "state-backup.json"
        state.rename(backup)
        state.symlink_to(backup)
        with self.assertRaises(ValidationError):
            build(self.manifest_path, output)
        with self.assertRaises(ValidationError):
            verify(root)
        self.assertTrue(state.is_symlink())
