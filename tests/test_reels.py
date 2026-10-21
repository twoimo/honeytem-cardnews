from __future__ import annotations

from fractions import Fraction
from io import BytesIO
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

from support import FixtureCase
from honeytem.pipeline import build, verify
from honeytem.reels import _pad_card, _probe, _run, _settings, export_reel, frame_plan, verify_reel
from honeytem.schema import ValidationError, canonical, digest


class ReelTimingTests(unittest.TestCase):
    def test_cumulative_rounding_has_no_per_card_accumulated_drift(self):
        plan = frame_plan(["0.15"] * 6, 10)
        self.assertEqual([row["frame_count"] for row in plan["intervals"]], [2, 1, 2, 1, 2, 1])
        self.assertEqual(plan["total_frames"], 9)  # Independent rounding would give 12.

    def test_randomized_exact_rational_boundary_invariants(self):
        rng = random.Random(70421)
        for _ in range(1000):
            fps = rng.randint(1, 60)
            durations = [str(rng.randint(100, 5000) / 100) for _ in range(rng.randint(2, 3))]
            plan = frame_plan(durations, fps)
            cumulative = Fraction()
            prior = 0
            for duration, interval in zip(durations, plan["intervals"]):
                cumulative += Fraction(duration) * fps
                self.assertEqual(interval["start_frame"], prior)
                self.assertGreaterEqual(interval["frame_count"], 1)
                self.assertEqual(interval["end_frame"] - prior, interval["frame_count"])
                self.assertLessEqual(abs(interval["end_frame"] - cumulative), Fraction(1, 2))
                prior = interval["end_frame"]
            self.assertEqual(sum(row["frame_count"] for row in plan["intervals"]), plan["total_frames"])
            self.assertEqual(prior, plan["total_frames"])

    def test_invalid_or_nonfinite_durations_and_rates_fail_closed(self):
        invalid = [True, False, None, "", "garbage", "NaN", "sNaN", "Infinity", "-Infinity", float("nan"),
                   float("inf"), -1, 0, "0.000001", 60.0001, "1e1000000", "1e-1000000", "1" * 65, {}, []]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                frame_plan([value, 1], 30)
        for fps in (True, False, None, 0, -1, 61, 29.97, "30"):
            with self.subTest(fps=fps), self.assertRaises(ValidationError):
                frame_plan([1, 1], fps)
        for values in ([], [1], [1] * 7, [60] * 4, "1,1"):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                frame_plan(values)

    def test_half_frame_ties_round_up_and_canonical_duration_normalizes(self):
        plan = frame_plan(["0.1500", "1.00e-1"], 10)
        self.assertEqual(plan["durations"], ["0.15", "0.1"])
        self.assertEqual(plan["total_frames"], 3)
        self.assertEqual(plan["intervals"][0]["end_frame"], 2)
        with self.assertRaises(ValidationError):
            frame_plan(["0.09999999999999999999999999999999999999999999999", 1], 10)

    def test_settings_reject_unsafe_or_distorted_canvas_and_palette(self):
        for arguments in ((1080, 1921, "#FFFFFF", "center"), (1920, 1080, "#FFFFFF", "center"),
                          (1080, 1920, "red", "center"), (1080, 1920, "#FFFFFF", "stretch"),
                          (1080.0, 1920, "#FFFFFF", "center"), (True, 1920, "#FFFFFF", "center")):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                _settings(*arguments)

    def test_native_padding_exact_pixels_and_alignment(self):
        source = Image.new("RGB", (1080, 1350), (10, 20, 30))
        source.putpixel((0, 0), (250, 0, 30))
        source.putpixel((1079, 1349), (20, 250, 90))
        raw = BytesIO()
        source.save(raw, format="PNG")
        for alignment, y in (("top", 0), ("center", 285), ("bottom", 570)):
            settings = _settings(1080, 1920, "#F5F4F0", alignment)
            padded, geometry = _pad_card(raw.getvalue(), settings)
            self.assertEqual(geometry["card_bounds"], [0, y, 1080, y + 1350])
            with Image.open(BytesIO(padded)) as image:
                self.assertEqual(image.crop(geometry["card_bounds"]).tobytes(), source.tobytes())
                padding_y = 1919 if y == 0 else 0
                self.assertEqual(image.getpixel((0, padding_y)), (245, 244, 240))
        with self.assertRaises(ValidationError):
            _pad_card(b"not a PNG", _settings(1080, 1920, "#FFFFFF", "center"))


class ReelProcessTests(unittest.TestCase):
    def test_missing_tool_nonzero_and_secret_stderr_are_safe(self):
        with self.assertRaisesRegex(ValidationError, "unavailable"):
            _run(["/no/such/honeytem-ffmpeg"], "ffmpeg")
        secret = "DO_NOT_ECHO_PRIVATE_PATH_OR_TOKEN"
        with self.assertRaisesRegex(ValidationError, "exit code 7") as error:
            _run([sys.executable, "-c", f"import sys;sys.stderr.write('{secret}');sys.exit(7)"], "ffmpeg")
        self.assertNotIn(secret, str(error.exception))

    def test_subprocess_timeout_and_output_limit_are_enforced(self):
        with patch("honeytem.reels.PROCESS_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(ValidationError, "timed out"):
                _run([sys.executable, "-c", "import time;time.sleep(5)"], "ffmpeg")
        for destination in ("stdout", "stderr"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValidationError, "oversized"):
                _run([sys.executable, "-c", f"import sys;sys.{destination}.write('x'*2000000)"], "ffmpeg")


class ReelProbeTests(unittest.TestCase):
    def test_malformed_and_mismatched_probe_results_fail_closed(self):
        valid = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
                              "pix_fmt": "yuv420p", "r_frame_rate": "10/1", "avg_frame_rate": "10/1",
                              "nb_read_frames": "2", "nb_frames": "2", "duration": "0.200000", "time_base": "1/10000"}],
                 "format": {"duration": "0.200000", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        plan, settings = frame_plan(["0.1", "0.1"], 10), _settings(1080, 1920, "#FFFFFF", "center")
        with patch("honeytem.reels._run", return_value=canonical(valid)):
            self.assertEqual(_probe(Path("unused.mp4"), plan, settings, "ffprobe")["frame_count"], 2)
        variants = [b"not JSON", b"[]", b"{}"]
        for field, value in (("nb_read_frames", "1"), ("nb_read_frames", "NaN"), ("width", 720),
                             ("r_frame_rate", "0/0"), ("pix_fmt", "rgb24"), ("duration", "99"),
                             ("codec_type", "audio"), ("time_base", "0/1")):
            changed = json.loads(json.dumps(valid))
            changed["streams"][0][field] = value
            variants.append(canonical(changed))
        changed = json.loads(json.dumps(valid))
        changed["streams"].append({"codec_type": "audio"})
        variants.append(canonical(changed))
        for raw in variants:
            with self.subTest(raw=raw), patch("honeytem.reels._run", return_value=raw), self.assertRaises(ValidationError):
                _probe(Path("unused.mp4"), plan, settings, "ffprobe")


class ReelExportTests(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = Path(build(cls.manifest_path, cls.root / "cards")["run_dir"])

    def destination(self):
        return self.output_directory() / "reel"

    def test_input_output_path_and_duration_errors_fail_before_execution(self):
        for destination in (self.source, self.source.parent, self.source / "reel", self.output_directory()):
            with self.subTest(destination=destination), self.assertRaises(ValidationError):
                export_reel(self.source, destination)
        symlink = self.output_directory() / "linked"
        symlink.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValidationError):
            export_reel(symlink, self.destination())
        with self.assertRaises(ValidationError):
            export_reel(self.source, symlink)
        for durations in ([1, 2], float("nan"), True):
            with self.subTest(durations=durations), self.assertRaises(ValidationError):
                export_reel(self.source, self.destination(), durations=durations)
        with self.assertRaises(ValidationError):
            export_reel(self.source, self.destination(), allow_affiliate_transcoding=1)
        with self.assertRaises(ValidationError):
            export_reel(self.root / "missing-run", self.destination())

    def test_unverified_or_tampered_source_rejected_before_encoder(self):
        for kind in ("tampered", "incomplete"):
            with self.subTest(kind=kind):
                source = Path(build(self.manifest_path, self.output_directory(), "validated" if kind == "incomplete" else "packaged")["run_dir"])
                if kind == "tampered":
                    (source / "rendered/cover.png").write_bytes(b"tampered")
                with patch("honeytem.reels._encode") as encode, self.assertRaises(ValidationError):
                    export_reel(source, self.destination())
                encode.assert_not_called()

    def test_missing_encoder_propagates_without_an_export(self):
        destination = self.destination()
        with self.assertRaisesRegex(ValidationError, "unavailable"):
            export_reel(self.source, destination, ffmpeg="/no/such/honeytem-ffmpeg")
        self.assertFalse(destination.exists())

    def test_encoder_and_probe_failure_leave_no_success_or_partial_export(self):
        for step in ("_encode", "_probe"):
            destination = self.destination()
            with self.subTest(step=step), patch("honeytem.reels._version", return_value="test version"):
                with patch("honeytem.reels._encode") if step == "_probe" else patch("honeytem.reels._probe"):
                    with patch("honeytem.reels." + step, side_effect=ValidationError("reel: injected failure")):
                        with self.assertRaisesRegex(ValidationError, "injected failure"):
                            export_reel(self.source, destination, durations=0.1, fps=10)
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_affiliate_requires_separate_transcoding_confirmation(self):
        affiliate = self.affiliate()
        source = Path(build(self.write_manifest(affiliate, "reel-affiliate.json"), self.output_directory())["run_dir"])
        with patch("honeytem.reels._encode") as encoder, self.assertRaisesRegex(ValidationError, "separate derivative/transcoding"):
            export_reel(source, self.destination())
        encoder.assert_not_called()
        with patch("honeytem.reels._version", side_effect=ValidationError("reel: reached local tools")):
            with self.assertRaisesRegex(ValidationError, "reached local tools"):
                export_reel(source, self.destination(), allow_affiliate_transcoding=True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_one_frame_segments_and_fractional_rate_boundaries(self):
        for fps, durations in ((10, "0.1"), (1, "1"), (60, "0.016666666666666667"),
                               (29, ["0.052", "0.09", "0.12", "0.14"])):
            with self.subTest(fps=fps):
                result = export_reel(self.source, self.destination(), durations=durations, fps=fps)
                manifest = json.loads(Path(result["manifest"]).read_text())
                expected = frame_plan(durations if isinstance(durations, list) else [durations] * 4, fps)
                self.assertEqual(result["total_frames"], expected["total_frames"])
                self.assertEqual(manifest["media"]["frame_count"], expected["total_frames"])
                decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", result["video"], "-vf", "scale=216:384",
                                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True,
                                         capture_output=True, timeout=60).stdout
                frame_size = 216 * 384 * 3
                self.assertEqual(len(decoded), frame_size * result["total_frames"])
                for card in manifest["timeline"]:
                    with Image.open(Path(result["output_dir"]) / card["frame_file"]) as image:
                        expected_image = image.resize((216, 384), Image.Resampling.BICUBIC)
                        for index in range(card["start_frame"], card["end_frame"]):
                            actual = Image.frombytes("RGB", (216, 384), decoded[index * frame_size:(index + 1) * frame_size])
                            self.assertLess(max(ImageStat.Stat(ImageChops.difference(expected_image, actual)).rms), 8)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_encode_repeat_verify_decoded_timing_and_tamper_detection(self):
        source_before = (self.source / "package.zip").read_bytes()
        state_before = (self.source / "state.json").read_bytes()
        first = export_reel(self.source, self.destination(), durations=["0.1", "0.2", "0.3", "0.4"], fps=10)
        second = export_reel(self.source, self.destination(), durations=["0.1", "0.2", "0.3", "0.4"], fps=10)
        self.assertEqual(first["video_sha256"], second["video_sha256"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(verify_reel(first["output_dir"])["total_frames"], 10)
        self.assertFalse(first["publish_ready"])
        root = Path(first["output_dir"])
        manifest = json.loads((root / "reel-manifest.json").read_text())
        self.assertEqual(manifest["source"]["package_sha256"], digest(source_before))
        self.assertEqual(manifest["media"]["audio_streams"], 0)
        self.assertEqual((self.source / "package.zip").read_bytes(), source_before)
        self.assertEqual((self.source / "state.json").read_bytes(), state_before)
        self.assertEqual(verify(self.source)["integrity"], "passed")
        # Decode every actual frame, not only container metadata. This miniature
        # synthetic check verifies ordering/boundaries, not real-product quality.
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "reel.mp4"), "-vf", "scale=216:384",
                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True, timeout=60).stdout
        frame_size = 216 * 384 * 3
        self.assertEqual(len(decoded), frame_size * 10)
        for card in manifest["timeline"]:
            with Image.open(root / card["frame_file"]) as padded:
                expected = padded.resize((216, 384), Image.Resampling.BICUBIC)
                for index in range(card["start_frame"], card["end_frame"]):
                    actual = Image.frombytes("RGB", (216, 384), decoded[index * frame_size:(index + 1) * frame_size])
                    self.assertLess(max(ImageStat.Stat(ImageChops.difference(expected, actual)).rms), 8)
        with self.assertRaises(ValidationError):
            export_reel(self.source, root, durations=0.1, fps=10)
        original = (root / "reel.mp4").read_bytes()
        (root / "reel.mp4").write_bytes(original + b"tamper")
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            verify_reel(root)
        # Updating an integrity hash cannot make corrupt media pass ffprobe.
        manifest["video_sha256"] = digest(b"truncated non-MP4")
        (root / "reel.mp4").write_bytes(b"truncated non-MP4")
        (root / "reel-manifest.json").write_bytes(canonical(manifest))
        with self.assertRaisesRegex(ValidationError, "ffprobe failed"):
            verify_reel(root)
        manifest["video_sha256"] = digest(original)
        (root / "reel-manifest.json").write_bytes(canonical(manifest))
        (root / "reel.mp4").write_bytes(original)
        (root / "extra.txt").write_text("extra")
        with self.assertRaisesRegex(ValidationError, "unexpected export"):
            verify_reel(root)
        (root / "extra.txt").unlink()
        manifest["plan"]["total_frames"] += 1
        (root / "reel-manifest.json").write_bytes(canonical(manifest))
        with self.assertRaisesRegex(ValidationError, "frame plan"):
            verify_reel(root)
