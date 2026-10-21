from __future__ import annotations

from array import array
import argparse
import copy
from fractions import Fraction
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from PIL import Image, ImageChops

from support import FixtureCase, FONT_DEFAULTS
from honeytem.composition import (caption_plan, subtitle_files, compose_reel, verify_composition,
    _audio_format, _decimal, _duration_adjust, _pcm_metrics, _loudness, _validate_spec, register_cli)
from honeytem.pipeline import build
from honeytem.reels import export_reel
from honeytem.schema import ValidationError, canonical, digest


def tone(path: Path, seconds: Fraction = Fraction(4, 5), frequency: int = 440, level: float = .1) -> bytes:
    """Public-domain test fixture: mathematical sine, NOT TTS or a human voice."""
    count = int(seconds * 48000)
    pcm = b"".join(struct.pack("<h", int(level * 32767 * math.sin(2 * math.pi * frequency * i / 48000))) for i in range(count))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 48000, count, "NONE", "not compressed"))
        output.writeframes(pcm)
    return path.read_bytes()


def specification(base: Path, fps: int = 10, total_frames: int = 8) -> dict:
    font = Path(FONT_DEFAULTS["regular"]).read_bytes()
    (base / "font.ttc").write_bytes(font)
    narration = tone(base / "provided-test-tone.wav", Fraction(total_frames, fps))
    music = tone(base / "provided-test-music.wav", Fraction(total_frames, fps), 220, .04)
    def track(filename, data, label):
        return {"path": filename, "sha256": digest(data), "source": "Mathematical sine-wave fixture; " + label + "; not speech",
                "license": "CC0-1.0 public-domain test fixture generated locally", "rights_confirmed": True,
                "duration_policy": "fail", "gain_db": "-3"}
    return {"schema_version": 1, "fps": fps,
            "script": {"text": "합성 테스트\n실제 판매용 아님", "source": "Explicit local test script; no product claims"},
            "captions": [{"start_frame": 0, "end_frame": total_frames // 2, "text": "합성 테스트"},
                         {"start_frame": total_frames // 2, "end_frame": total_frames, "text": "실제 판매용 아님"}],
            "subtitles": {"burn_in": True, "font": {"path": "font.ttc", "index": 0, "sha256": digest(font)}, "font_size": 48},
            "audio": {"narration": track("provided-test-tone.wav", narration, "narration-slot tone"),
                      "music": track("provided-test-music.wav", music, "music-slot tone")}}


class CompositionUnitTests(unittest.TestCase):
    def test_rational_frames_and_sidecars(self):
        captions = [{"start_frame": 1, "end_frame": 2, "text": "가나다"}, {"start_frame": 2, "end_frame": 29, "text": "한글"}]
        srt, vtt = subtitle_files(captions, 29, 29)
        self.assertIn(b"00:00:00,034 --> 00:00:00,069", srt)
        self.assertIn(b"00:00:00.069 --> 00:00:01.000", vtt)
        self.assertTrue(vtt.startswith(b"WEBVTT\n"))
        self.assertEqual(caption_plan(captions, 29, 29), captions)

    def test_invalid_ranges_fps_markup_controls(self):
        good = {"start_frame": 0, "end_frame": 10, "text": "hello"}
        for fps in (True, 0, 61, 29.97, "30", float("nan")):
            with self.subTest(fps=fps), self.assertRaises(ValidationError):
                caption_plan([good], fps, 10)
        for key, value in (("start_frame", -1), ("start_frame", .2), ("start_frame", True), ("end_frame", 0),
                           ("end_frame", 11), ("end_frame", float("nan")), ("text", "x\x00"), ("text", "<script>"), ("text", r"first\Nsecond"), ("text", r"{\an8}UP"),
                           ("text", "a\nb\nc"), ("text", "a\n\n"), ("text", "x" * 161)):
            row = dict(good, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                caption_plan([row], 10, 10)
        for rows in ([good, good], [], [good] * 25):
            with self.assertRaises(ValidationError):
                caption_plan(rows, 10, 10)

    def test_explicit_duration_policy(self):
        data = bytes(80)
        for policy in ("fail", "truncate"):
            with self.assertRaises(ValidationError):
                _duration_adjust(data, 11, policy)
        for policy in ("fail", "pad"):
            with self.assertRaises(ValidationError):
                _duration_adjust(data, 9, policy)
        self.assertEqual(_duration_adjust(data, 10, "fail")[1]["action"], "unchanged")
        self.assertEqual(len(_duration_adjust(data, 11, "pad")[0]), 88)
        self.assertEqual(len(_duration_adjust(data, 9, "truncate")[0]), 72)
        self.assertEqual(_duration_adjust(data, 11, "pad_or_truncate")[1]["action"], "padded_with_silence")
        with self.assertRaises(ValidationError):
            _duration_adjust(data, 10, "loop")

    def test_nonfinite_gain_and_samples_rejected(self):
        for value in (True, "NaN", "Infinity", float("nan"), "1e-9999999", "1e9999999", 13, -61, {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _decimal(value, "gain", -60, 12)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValidationError):
                _pcm_metrics(struct.pack("<ff", value, 0))
        metrics = _pcm_metrics(struct.pack("<ffff", -1, 1, 0, .25))
        self.assertEqual(metrics["clipped_samples"], 2)
        self.assertEqual(metrics["sample_frames"], 2)
        self.assertIsNone(_pcm_metrics(bytes(16))["sample_peak_dbfs"])

    def test_playlists_are_not_media_and_cli_is_separate(self):
        for raw in (b"#EXTM3U\nfile:///etc/passwd", b"ffconcat version 1.0", b"https://host/track.mp3", b"not-audio"):
            with self.assertRaises(ValidationError):
                _audio_format(raw)
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        register_cli(commands)
        args = parser.parse_args(["compose-reel", "silent", "--spec", "composition.json", "--output", "composed"])
        self.assertEqual(args.command, "compose-reel")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CompositionIntegrationTests(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source_run = cls.root / "verified-run"
        cls.source_run = Path(build(cls.manifest_path, cls.source_run)["run_dir"])
        cls.silent = cls.root / "silent"
        export_reel(cls.source_run, cls.silent, durations="0.2", fps=10)
        cls.inputs = cls.root / "composition-inputs"
        cls.inputs.mkdir()
        cls.specification = specification(cls.inputs)
        cls.specfile = cls.inputs / "composition.json"
        cls.specfile.write_bytes(canonical(cls.specification))
        cls.output = cls.root / "composed"
        cls.result = compose_reel(cls.silent, cls.specfile, cls.output)

    def write_spec(self, value=None):
        self.specfile.write_bytes(canonical(value or self.specification))
        return self.specfile

    def test_real_encode_loudness_timing_rights_sidecars_and_safe_korean_pixels(self):
        result = verify_composition(self.output, expected_manifest_sha256=self.result["manifest_sha256"])
        self.assertEqual(result["integrity"], "passed")
        self.assertFalse(result["publish_ready"])
        manifest = json.loads((self.output / "composition-manifest.json").read_bytes())
        self.assertEqual(manifest["sample_frames"], 38400)
        self.assertEqual(manifest["mixed_audio"]["clipped_samples"], 0)
        self.assertLess(manifest["encoded_audio"]["integrated_lufs"], 0)
        self.assertLess(manifest["encoded_audio"]["true_peak"], 1)
        self.assertEqual(len(manifest["input_audio"]), 2)
        self.assertIn("not speech", json.loads((self.output / "composition-input.json").read_bytes())["audio"]["narration"]["source"])
        self.assertEqual((self.output / "source" / "reel.mp4").read_bytes(), (self.silent / "reel.mp4").read_bytes())
        for row in manifest["segments"]:
            with Image.open(self.output / row["frame_file"]) as image, Image.open(self.output / "source" / row["source_frame"]) as source:
                self.assertEqual(image.crop(row["card_bounds"]).tobytes(), source.crop(row["card_bounds"]).tobytes())
                if row["caption_bounds"]:
                    self.assertIsNotNone(ImageChops.difference(image, source).getbbox())
                    box = row["caption_bounds"]
                    self.assertTrue(box[3] <= row["card_bounds"][1] or box[1] >= row["card_bounds"][3])
                    for ink in row["ink_bounds"]:
                        self.assertTrue(box[0] <= ink[0] <= ink[2] <= box[2])
                        self.assertTrue(box[1] <= ink[1] <= ink[3] <= box[3])
        self.assertIn("합성 테스트", (self.output / "captions.srt").read_text())

    def test_tampering_and_trusted_digest(self):
        with self.assertRaisesRegex(ValidationError, "trusted"):
            verify_composition(self.output, expected_manifest_sha256="0" * 64)
        target = self.output_directory() / "tampered"
        shutil.copytree(self.output, target)
        (target / "captions.srt").write_text("tampered")
        with self.assertRaisesRegex(ValidationError, "digest"):
            verify_composition(target)
        (target / "captions.srt").write_bytes((self.output / "captions.srt").read_bytes())
        (target / "unexpected.txt").write_text("bad")
        with self.assertRaises(ValidationError):
            verify_composition(target)

    def test_rights_digest_script_fps_paths_and_duration_fail_before_export(self):
        mutations = [lambda s: s["audio"]["music"].update(rights_confirmed=False),
                     lambda s: s["audio"]["music"].update(sha256="0" * 64),
                     lambda s: s["audio"]["music"].update(path="https://example.org/music.wav"),
                     lambda s: s["audio"]["music"].update(path="../outside.wav"),
                     lambda s: s["audio"]["music"].update(duration_policy="loop"),
                     lambda s: s["script"].update(text="Unrelated script"),
                     lambda s: s.update(fps=30),
                     lambda s: s["subtitles"]["font"].update(sha256="0" * 64),
                     lambda s: s["audio"].update(music=None, narration=None)]
        for mutation in mutations:
            spec = copy.deepcopy(self.specification)
            mutation(spec)
            destination = self.output_directory() / "rejected"
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                compose_reel(self.silent, self.write_spec(spec), destination)
            self.assertFalse(destination.exists())

    def test_bad_media_ffmpeg_failure_no_overwrite_and_symlinks(self):
        spec = copy.deepcopy(self.specification)
        bad = b"not audio"
        (self.inputs / "bad.wav").write_bytes(bad)
        spec["audio"]["music"].update(path="bad.wav", sha256=digest(bad))
        with self.assertRaises(ValidationError):
            compose_reel(self.silent, self.write_spec(spec), self.output_directory() / "bad")
        with self.assertRaises(ValidationError):
            compose_reel(self.silent, self.write_spec(), self.output)
        destination = self.output_directory() / "ffmpeg-failed"
        with self.assertRaises(ValidationError):
            compose_reel(self.silent, self.write_spec(), destination, ffmpeg="/missing/ffmpeg")
        self.assertFalse(destination.exists())
        with patch("honeytem.composition._encode", side_effect=ValidationError("ffmpeg failed")):
            with self.assertRaises(ValidationError):
                compose_reel(self.silent, self.write_spec(), destination)
        self.assertFalse(destination.exists())
        (self.inputs / "linked.wav").symlink_to(self.inputs / "provided-test-tone.wav")
        spec = copy.deepcopy(self.specification)
        spec["audio"]["narration"]["path"] = "linked.wav"
        with self.assertRaises(ValidationError):
            compose_reel(self.silent, self.write_spec(spec), destination)

    def test_silence_loudness_is_not_falsely_reported_as_measured(self):
        tone(self.inputs / "silence.wav", Fraction(4, 5), level=0)
        metrics = _loudness(self.inputs / "silence.wav", "ffmpeg")
        self.assertIsNone(metrics["integrated_lufs"])
        self.assertEqual(metrics["integrated_lufs_status"], "below_gate_or_insufficient_duration")
        self.assertEqual(metrics["reported_integrated_lufs"], -70)
        self.assertEqual(metrics["true_peak"], 0)

    def test_clipping_overflow_font_and_raw_numeric_bounds_fail_closed(self):
        destination = self.output_directory() / "rejected"
        spec = copy.deepcopy(self.specification)
        spec["audio"]["narration"]["gain_db"] = 12
        spec["audio"]["music"]["gain_db"] = 12
        loud = tone(self.inputs / "loud.wav", Fraction(4, 5), 440, .8)
        spec["audio"]["narration"].update(path="loud.wav", sha256=digest(loud))
        with self.assertRaisesRegex(ValidationError, "clips"):
            compose_reel(self.silent, self.write_spec(spec), destination)
        self.assertFalse(destination.exists())
        spec = copy.deepcopy(self.specification)
        spec["subtitles"]["font_size"] = 96
        spec["captions"] = [{"start_frame": 0, "end_frame": 8, "text": "합성 테스트\n실제 판매용 아님"}]
        with self.assertRaisesRegex(ValidationError, "padding safe area"):
            compose_reel(self.silent, self.write_spec(spec), destination)
        self.assertFalse(destination.exists())
        latin_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if latin_font.is_file():
            font = latin_font.read_bytes()
            (self.inputs / "latin.ttf").write_bytes(font)
            spec = copy.deepcopy(self.specification)
            spec["subtitles"]["font"].update(path="latin.ttf", sha256=digest(font))
            with self.assertRaisesRegex(ValidationError, "Korean"):
                compose_reel(self.silent, self.write_spec(spec), destination)
        self.specfile.write_text('{"schema_version":' + '9' * 5000 + '}')
        with self.assertRaises(ValidationError):
            compose_reel(self.silent, self.specfile, destination)
        self.assertFalse(destination.exists())

    def test_fractional_fps_real_encode_preserves_audio_sample_timing(self):
        silent = self.output_directory() / "fractional-fps"
        export_reel(self.source_run, silent, durations=["0.15", "0.19", "0.23", "0.29"], fps=29)
        spec = copy.deepcopy(self.specification)
        spec["fps"] = 29
        spec["captions"] = [{"start_frame": 0, "end_frame": 12, "text": "합성 테스트"},
                            {"start_frame": 13, "end_frame": 25, "text": "실제 판매용 아님"}]
        for track in spec["audio"].values():
            track["duration_policy"] = "pad"
        target = self.output_directory() / "fractional-composition"
        compose_reel(silent, self.write_spec(spec), target)
        manifest = json.loads((target / "composition-manifest.json").read_bytes())
        self.assertEqual(manifest["sample_frames"], 41379)
        audio = next(row for row in manifest["media"]["streams"] if row["codec_type"] == "audio")
        self.assertLessEqual(abs(Fraction(audio["duration"]) - Fraction(25, 29)), Fraction(1, 48000))

    def test_optional_burn_in_off_and_explicit_pad_truncate_real_encode(self):
        spec = copy.deepcopy(self.specification)
        short = tone(self.inputs / "short.wav", Fraction(1, 2))
        long = tone(self.inputs / "long.wav", Fraction(1), 220, .04)
        spec["audio"]["narration"].update(path="short.wav", sha256=digest(short), duration_policy="pad")
        spec["audio"]["music"].update(path="long.wav", sha256=digest(long), duration_policy="truncate")
        spec["subtitles"]["burn_in"] = False
        target = self.output_directory() / "sidecars-only"
        compose_reel(self.silent, self.write_spec(spec), target)
        manifest = json.loads((target / "composition-manifest.json").read_bytes())
        self.assertEqual(manifest["input_audio"]["narration"]["adjustment"]["action"], "padded_with_silence")
        self.assertEqual(manifest["input_audio"]["music"]["adjustment"]["action"], "truncated")
        for row in manifest["segments"]:
            self.assertEqual((target / row["frame_file"]).read_bytes(), (target / "source" / row["source_frame"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
