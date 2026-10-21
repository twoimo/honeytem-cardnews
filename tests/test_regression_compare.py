from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import zipfile

from PIL import Image
from support import FixtureCase, REPO
from honeytem.pipeline import build

spec = importlib.util.spec_from_file_location("honeytem_regression_compare", REPO / "tools/regression_compare.py")
regression = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regression)


def png(color, size=(3, 2), mode="RGBA"):
    image = Image.new(mode, size, color)
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class PixelComparisonTests(unittest.TestCase):
    def test_rgb_change_detected_with_unchanged_opaque_alpha(self):
        difference = regression.image_difference(png((10, 20, 30, 255)), png((11, 20, 30, 255)))
        self.assertFalse(difference["equal"])
        self.assertEqual(difference["changed_pixels"], 6)
        self.assertEqual(difference["changed_components"], 6)
        self.assertEqual(difference["max_channel_difference"], 1)
        self.assertEqual(difference["changed_bounds"], [0, 0, 3, 2])

    def test_all_rgba_channels_and_hidden_rgb_are_compared(self):
        for index in range(4):
            a = [10, 20, 30, 0]
            b = list(a)
            b[index] += 1
            with self.subTest(channel=index):
                self.assertFalse(regression.image_difference(png(tuple(a)), png(tuple(b)))["equal"])

    def test_dimensions_and_mode_are_part_of_contract(self):
        self.assertFalse(regression.image_difference(png((1, 2, 3, 255)), png((1, 2, 3, 255), size=(2, 3)))["equal"])
        self.assertFalse(regression.image_difference(png((1, 2, 3, 255)), png((1, 2, 3), mode="RGB"))["equal"])
        self.assertTrue(regression.image_difference(png((1, 2, 3, 255)), png((1, 2, 3, 255)))["equal"])

    def test_deliberate_defects_are_rejected(self):
        controls = regression.negative_controls()
        self.assertTrue(controls["passed"])
        self.assertEqual(len(controls["checks"]), 9)


class MetricGateTests(unittest.TestCase):
    def test_faster_slower_and_unsupported_are_distinct(self):
        self.assertEqual(regression.metric_gate([10] * 5, [9] * 5)["status"], "pass")
        self.assertEqual(regression.metric_gate([10] * 5, [11] * 5)["status"], "regression")
        self.assertEqual(regression.metric_gate([10] * 4, [9] * 4)["status"], "blocked")
        self.assertEqual(regression.metric_gate([10] * 5, [None] * 5)["status"], "blocked")
        self.assertEqual(regression.metric_gate([0] * 5, [10] * 5)["status"], "blocked")
        self.assertEqual(regression.metric_gate([10] * 5, [float("nan")] * 5)["status"], "blocked")

    def test_noisy_measurements_are_not_passed_with_a_tolerance(self):
        result = regression.metric_gate([10] * 9, [9, 11, 9, 11, 9, 11, 9, 11, 9])
        self.assertTrue(result["observed_median_non_increase"])
        self.assertEqual(result["allowed_regression_percent"], 0)
        self.assertEqual(result["status"], "inconclusive")

    def test_machine_gate_keeps_overall_unknown_blocked(self):
        report = {"gates": {"observed_offline_quantitative": "pass", "overall_no_nerf": "blocked"}}
        self.assertEqual(regression.gate_exit_code(report, "offline"), 0)
        self.assertEqual(regression.gate_exit_code(report, "overall"), 2)
        report["gates"]["observed_offline_quantitative"] = "regression"
        self.assertEqual(regression.gate_exit_code(report, "offline"), 1)


class ArtifactComparisonTests(FixtureCase):
    def artifact(self):
        result = build(self.manifest_path, self.output_directory())
        result["input_hashes"] = json.loads((Path(result["run_dir"]) / "validated.json").read_text())["input_hashes"]
        return Path(result["run_dir"]), result

    def test_package_members_and_exact_payload_are_independently_checked(self):
        root, result = self.artifact()
        payload, package = regression.read_artifacts(root, result)
        self.assertTrue(package["independently_verified"])
        self.assertEqual(package["sha256"], result["package_sha256"])
        same = regression.compare_payloads(payload, payload, result, result)
        self.assertTrue(same["equal"])
        self.assertTrue(same["raw_equal"])

    def test_only_verified_source_identity_is_normalized(self):
        root, result = self.artifact()
        payload, _ = regression.read_artifacts(root, result)
        changed = deepcopy(result)
        changed["input_hashes"]["code"]["new_module.py"] = "a" * 64
        changed["fingerprint"] = regression.digest(regression.canonical(changed["input_hashes"]))
        provenance = json.loads(payload["provenance.json"])
        provenance["fingerprint"] = changed["fingerprint"]
        provenance["input_hashes"] = changed["input_hashes"]
        new_payload = dict(payload, **{"provenance.json": regression.canonical(provenance)})
        comparison = regression.compare_payloads(payload, new_payload, result, changed)
        self.assertTrue(comparison["equal"])
        self.assertFalse(comparison["raw_equal"])
        self.assertEqual(comparison["expected_identity_only_changes"], ["provenance.json"])
        provenance["products"][0]["title"] = "Not the same product"
        new_payload["provenance.json"] = regression.canonical(provenance)
        self.assertFalse(regression.compare_payloads(payload, new_payload, result, changed)["equal"])

    def test_lying_provenance_and_changed_runtime_are_not_normalized(self):
        root, result = self.artifact()
        payload, _ = regression.read_artifacts(root, result)
        provenance = json.loads(payload["provenance.json"])
        provenance["fingerprint"] = "0" * 64
        new_payload = dict(payload, **{"provenance.json": regression.canonical(provenance)})
        with self.assertRaises(regression.ComparisonError):
            regression.compare_payloads(payload, new_payload, result, result)
        changed = deepcopy(result)
        changed["input_hashes"]["runtime"]["python"] = "different"
        changed["fingerprint"] = regression.digest(regression.canonical(changed["input_hashes"]))
        provenance["input_hashes"] = changed["input_hashes"]
        provenance["fingerprint"] = changed["fingerprint"]
        new_payload["provenance.json"] = regression.canonical(provenance)
        self.assertFalse(regression.compare_payloads(payload, new_payload, result, changed)["equal"])

    def test_archive_mutation_is_rejected_even_if_its_digest_is_updated(self):
        for mutation in ("missing", "changed", "trailing"):
            with self.subTest(mutation=mutation):
                root, result = self.artifact()
                package = root / "package.zip"
                with zipfile.ZipFile(package) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                if mutation == "trailing":
                    package.write_bytes(package.read_bytes() + b"unexplained trailing bytes")
                else:
                    if mutation == "missing":
                        del members["caption.txt"]
                    else:
                        members["caption.txt"] += b"changed"
                    with zipfile.ZipFile(package, "w") as archive:
                        for name, raw in sorted(members.items()):
                            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                            info.create_system = 3
                            info.external_attr = 0o100644 << 16
                            archive.writestr(info, raw)
                result["package_sha256"] = regression.digest(package.read_bytes())
                with self.assertRaises(regression.ComparisonError):
                    regression.read_artifacts(root, result)

    def test_fresh_worker_verifies_source_and_resume(self):
        output = self.output_directory()
        cold = regression.run_probe(REPO, self.manifest_path, output)
        resume = regression.run_probe(REPO, self.manifest_path, output)
        self.assertFalse(cold["resumed"])
        self.assertTrue(resume["resumed"])
        self.assertEqual(cold["package_sha256"], resume["package_sha256"])
        self.assertEqual(cold["source_identity"], regression.source_identity(REPO))
        self.assertGreater(cold["build_seconds"], 0)
        self.assertGreater(resume["build_seconds"], 0)
        if cold["peak_rss_bytes"] is not None:
            self.assertGreater(cold["peak_rss_bytes"], 0)

    def test_end_to_end_changed_code_preserves_content_but_not_raw_package(self):
        baseline = self.root / "independent-baseline"
        candidate = self.root / "independent-candidate"
        for destination in (baseline, candidate):
            shutil.copytree(REPO / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(REPO / "pyproject.toml", destination / "pyproject.toml")
        (candidate / "src/honeytem/regression_test_extra.py").write_text("# Provenance-only test module.\n")
        report = regression.compare_sources(baseline, candidate, self.manifest_path,
                                            self.output_directory(), runs=5)
        self.assertEqual(report["gates"]["deterministic_offline_correctness"], "pass")
        self.assertNotEqual(report["gates"]["overall_no_nerf"], "pass")
        self.assertTrue(report["input"]["identical_except_code"])
        self.assertFalse(report["correctness"]["raw_equal"])
        self.assertNotEqual(report["baseline"]["package"]["sha256"], report["candidate"]["package"]["sha256"])
        self.assertEqual(report["correctness"]["expected_identity_only_changes"], ["provenance.json"])
        self.assertEqual(len(report["trial_pairs"]), 5)
        for pair in report["trial_pairs"]:
            for label in ("baseline", "candidate"):
                self.assertFalse(pair[label]["cold"]["resumed"])
                self.assertTrue(pair[label]["resume"]["resumed"])
        self.assertFalse(report["publish_ready"])
