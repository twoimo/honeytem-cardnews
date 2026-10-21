#!/usr/bin/env python3
"""Compare two trusted local Honeytem source trees on one unchanged offline input.

No API calls, downloads, model-quality claims, or publication. See
 docs/regression-method.md for strict gate semantics and unresolved evidence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

from PIL import Image, ImageChops


class ComparisonError(ValueError):
    """Comparison evidence is incomplete or inconsistent."""


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def source_identity(source: Path) -> dict:
    source = source.resolve(strict=True)
    files = sorted((source / "src" / "honeytem").glob("*.py"))
    if not files or not (source / "src/honeytem/pipeline.py").is_file():
        raise ComparisonError("Expected a trusted local Honeytem source tree")
    code = {path.name: digest(path.read_bytes()) for path in files}
    return {"code": code, "code_tree_sha256": digest(canonical(code)),
            "pyproject_sha256": digest((source / "pyproject.toml").read_bytes())}


def image_difference(left: bytes, right: bytes) -> dict:
    """Count changes in every RGBA channel, including RGB under equal/zero alpha."""
    with Image.open(BytesIO(left)) as a, Image.open(BytesIO(right)) as b:
        a.load()
        b.load()
        result = {"old_size": list(a.size), "new_size": list(b.size),
                  "old_mode": a.mode, "new_mode": b.mode}
        if a.size != b.size:
            return dict(result, equal=False, reason="dimensions_changed")
        difference = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA"))
        # RGBA.getbbox() can inspect alpha only. Split and union ALL channels.
        channels = difference.split()
        mask = channels[0]
        for channel in channels[1:]:
            mask = ImageChops.lighter(mask, channel)
        histogram = mask.histogram()
        changed_pixels = sum(histogram[1:])
        extrema = difference.getextrema()
        return dict(result, equal=changed_pixels == 0 and a.mode == b.mode,
                    changed_pixels=changed_pixels,
                    changed_components=sum(sum(channel.histogram()[1:]) for channel in channels),
                    max_channel_difference=max(value[1] for value in extrema),
                    changed_bounds=list(mask.getbbox()) if changed_pixels else None)


def normalized_payload(payload: dict[str, bytes], identity: dict | None) -> dict[str, bytes]:
    """Normalize ONLY verified implementation identity, never business/QA content."""
    result = dict(payload)
    if "provenance.json" not in result:
        return result
    if identity is None:
        raise ComparisonError("Provenance requires independently observed implementation identity")
    provenance = json.loads(result["provenance.json"])
    if not isinstance(provenance, dict):
        raise ComparisonError("Malformed provenance")
    expected = identity["input_hashes"]
    if (provenance.get("fingerprint") != identity["fingerprint"]
            or provenance.get("input_hashes") != expected
            or digest(canonical(expected)) != identity["fingerprint"]):
        raise ComparisonError("Provenance does not match observed source and input identity")
    provenance = deepcopy(provenance)
    provenance.pop("fingerprint")
    provenance["input_hashes"].pop("code")
    result["provenance.json"] = canonical(provenance)
    return result


def compare_payloads(old: dict[str, bytes], new: dict[str, bytes],
                     old_identity: dict | None = None, new_identity: dict | None = None) -> dict:
    old_names, new_names = set(old), set(new)
    common = old_names & new_names
    raw_changed = sorted(name for name in common if old[name] != new[name])
    old_normalized = normalized_payload(old, old_identity)
    new_normalized = normalized_payload(new, new_identity)
    changed = sorted(name for name in common if old_normalized[name] != new_normalized[name])
    pixels = {name: image_difference(old[name], new[name]) for name in sorted(common)
              if name.lower().endswith((".png", ".jpg", ".jpeg"))}
    return {"equal": old_names == new_names and not changed and all(p["equal"] for p in pixels.values()),
            "raw_equal": old == new,
            "added": sorted(new_names - old_names), "removed": sorted(old_names - new_names),
            "raw_changed": raw_changed, "unexplained_changed": changed,
            "expected_identity_only_changes": sorted(set(raw_changed) - set(changed)),
            "images": pixels}


def read_artifacts(run_dir: Path, record: dict) -> tuple[dict[str, bytes], dict]:
    """Independent package checks, not just trust in the candidate's verify()."""
    rendered = run_dir / "rendered"
    paths = list(rendered.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ComparisonError("Artifact symlink is not allowed")
    payload = {path.relative_to(rendered).as_posix(): path.read_bytes() for path in paths if path.is_file()}
    required = {"cover.png", "caption.txt", "links.json", "qa.json", "provenance.json", "REVIEW.txt"}
    if not required <= set(payload):
        raise ComparisonError("Missing required artifacts")
    manifest_raw = (run_dir / "package-manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "fingerprint", "files"}
            or manifest["schema_version"] != 1
            or manifest.get("fingerprint") != record["fingerprint"]
            or manifest.get("files") != {name: digest(raw) for name, raw in payload.items()}):
        raise ComparisonError("Package manifest does not match exact rendered files")
    package_raw = (run_dir / "package.zip").read_bytes()
    if digest(package_raw) != record["package_sha256"]:
        raise ComparisonError("Package does not match measured digest")
    expected = dict(payload, **{"package-manifest.json": manifest_raw})
    with zipfile.ZipFile(BytesIO(package_raw)) as archive:
        infos = archive.infolist()
        if archive.namelist() != sorted(expected):
            raise ComparisonError("Archive members missing, added, duplicated or unsorted")
        for info in infos:
            if (info.date_time != (1980, 1, 1, 0, 0, 0) or info.compress_type != zipfile.ZIP_STORED
                    or info.create_system != 3 or info.external_attr != 0o100644 << 16
                    or archive.read(info.filename) != expected[info.filename]):
                raise ComparisonError("Archive metadata or content differs from deterministic contract")
    rebuilt = BytesIO()
    with zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(expected.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    if rebuilt.getvalue() != package_raw:
        raise ComparisonError("Package bytes contain unexpected metadata or trailing data")
    normalized_payload(payload, record)  # Validate identity even when comparing equal files.
    return payload, {"sha256": digest(package_raw), "bytes": len(package_raw),
                     "members": sorted(expected), "independently_verified": True,
                     "rendered_png_bytes": sum(len(raw) for name, raw in payload.items() if name.endswith(".png") and "/" not in name)}


def negative_controls() -> dict:
    """Run deliberate defects through the SAME comparator, including alpha trap."""
    def png(color):
        image = Image.new("RGBA", (2, 2), color)
        stream = BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()
    old = {"cover.png": png((10, 20, 30, 255)), "caption.txt": b"Original caption\n",
           "links.json": b'{"url":"https://vendor.invalid/a?token=a%2Bb&x=1&x=2"}\n',
           "qa.json": b'{"publish_ready":false,"manual_gates":{"quality":"blocked"}}\n'}
    mutations = {
        "rgb_change_with_equal_alpha": dict(old, **{"cover.png": png((11, 20, 30, 255))}),
        "alpha_only_change": dict(old, **{"cover.png": png((10, 20, 30, 254))}),
        "caption_change": dict(old, **{"caption.txt": b"Changed caption\n"}),
        "exact_link_encoding_change": dict(old, **{"links.json": old["links.json"].replace(b"%2B", b"+")}),
        "qa_gate_upgrade": dict(old, **{"qa.json": old["qa.json"].replace(b"blocked", b"passed")}),
        "missing_package_payload": {k: v for k, v in old.items() if k != "caption.txt"},
        "added_package_payload": dict(old, **{"extra.txt": b"unexpected"}),
    }
    results = {name: not compare_payloads(old, mutated)["equal"] for name, mutated in mutations.items()}
    results["hidden_rgb_change_with_zero_alpha"] = not image_difference(png((1, 0, 0, 0)), png((2, 0, 0, 0)))["equal"]
    results["identical_control_accepted"] = compare_payloads(old, dict(old))["equal"]
    return {"passed": all(results.values()), "checks": results}


def summarize(values: list[float | int]) -> dict:
    return {"median": statistics.median(values), "minimum": min(values), "maximum": max(values),
            "samples": values}


def metric_gate(old: list[float | int | None], new: list[float | int | None]) -> dict:
    """Zero allowance for observed slowdown; uncertainty is not silently a pass."""
    if (len(old) < 5 or len(old) != len(new)
            or any(type(value) not in (int, float) or not 0 < value < float("inf") for value in old + new)):
        return {"status": "blocked", "reason": "Need at least five paired measurements with supported metrics"}
    old_summary, new_summary = summarize(old), summarize(new)
    ratio = new_summary["median"] / old_summary["median"]
    paired = [b / a for a, b in zip(old, new)]
    # Descriptive paired bootstrap, not a universal or causal performance proof.
    randomizer = random.Random(20261021)
    draws = sorted(statistics.median(randomizer.choices(paired, k=len(paired))) for _ in range(2000))
    low, high = draws[49], draws[1949]
    observed_pass = ratio <= 1.0
    if observed_pass and high <= 1.0:
        status = "pass"
    elif not observed_pass and low > 1.0:
        status = "regression"
    else:
        status = "inconclusive"
    return {"status": status, "old": old_summary, "new": new_summary,
            "new_over_old_median_ratio": ratio, "observed_median_non_increase": observed_pass,
            "allowed_regression_percent": 0, "paired_ratio_bootstrap_95_percent": [low, high],
            "uncertainty_note": "Descriptive paired bootstrap on this fixture/session only; not universal speed or memory proof."}


def _worker(source: Path, manifest: Path, output: Path) -> dict:
    # Each cold/resume probe is a NEW interpreter. RSS is not accumulated across trials.
    sys.path.insert(0, str(source / "src"))
    import honeytem.pipeline as pipeline
    from honeytem.schema import load_manifest
    if Path(pipeline.__file__).resolve() != (source / "src/honeytem/pipeline.py").resolve():
        raise ComparisonError("Imported the wrong source tree")
    before = source_identity(source)
    start = time.perf_counter()
    result = pipeline.build(manifest, output)
    elapsed = time.perf_counter() - start
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        system = platform.system()
        peak_bytes = int(peak if system == "Darwin" else peak * 1024) if system in {"Darwin", "Linux"} else None
    except (ImportError, AttributeError):
        peak_bytes = None
    # Checks are outside build timing and outside the recorded RSS high-water sample.
    job = load_manifest(manifest)
    if source_identity(source) != before or job.input_hashes["code"] != before["code"]:
        raise ComparisonError("Source changed during measurement")
    verified = pipeline.verify(Path(result["run_dir"]))
    if verified.get("integrity") != "passed" or verified.get("publish_ready") is not False:
        raise ComparisonError("Build verification or publication block failed")
    return dict(result, input_hashes=job.input_hashes, source_identity=before,
                build_seconds=elapsed, peak_rss_bytes=peak_bytes,
                platform=platform.platform(), fixture=job.manifest["fixture"],
                products=len(job.manifest["products"]))


def run_probe(source: Path, manifest: Path, output: Path) -> dict:
    environment = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    environment["PYTHONPATH"] = str(source / "src")
    started = time.perf_counter()
    process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                              str(source), str(manifest), str(output)], cwd=source,
                             env=environment, text=True, capture_output=True, timeout=120, check=False)
    total_seconds = time.perf_counter() - started
    if process.returncode:
        # Do not echo potentially private manifest fields or candidate exception text.
        raise ComparisonError("Isolated build probe failed; no valid comparison was established")
    result = json.loads(process.stdout)
    result["probe_process_seconds"] = total_seconds
    return result


def compare_sources(baseline: Path, candidate: Path, manifest: Path, work_dir: Path, runs: int = 9) -> dict:
    if not 5 <= runs <= 50:
        raise ComparisonError("Use between 5 and 50 paired trials")
    baseline, candidate, manifest = baseline.resolve(), candidate.resolve(), manifest.resolve()
    if not work_dir.is_dir() or any(work_dir.iterdir()):
        raise ComparisonError("Measurement work directory must exist and be empty")
    identities = {"baseline": source_identity(baseline), "candidate": source_identity(candidate)}
    manifest_sha = digest(manifest.read_bytes())
    sources = {"baseline": baseline, "candidate": candidate}
    pairs, captures = [], {"baseline": [], "candidate": []}
    controls = negative_controls()
    for index in range(runs):
        order = ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"]
        pair = {"index": index, "order": order}
        for label in order:
            output = work_dir / f"{index:02d}-{label}"
            cold = run_probe(sources[label], manifest, output)
            resume = run_probe(sources[label], manifest, output)
            if (cold["resumed"] or not resume["resumed"] or cold["package_sha256"] != resume["package_sha256"]
                    or cold["input_hashes"] != resume["input_hashes"]):
                raise ComparisonError("Cold/resume state or identity mismatch")
            if cold["source_identity"] != identities[label]:
                raise ComparisonError("Source changed between trials")
            captures[label].append(cold)
            pair[label] = {"cold": cold, "resume": resume}
        pairs.append(pair)
    if manifest_sha != digest(manifest.read_bytes()) or any(source_identity(sources[k]) != v for k, v in identities.items()):
        raise ComparisonError("Input or source changed during the comparison")
    for label in captures:
        if len({c["package_sha256"] for c in captures[label]}) != 1 or len({digest(canonical(c["input_hashes"])) for c in captures[label]}) != 1:
            raise ComparisonError("Repeated identical builds were not deterministic")
    # Independently check EVERY final artifact, not just worker-reported hashes.
    checked = {label: [read_artifacts(Path(c["run_dir"]), c) for c in records]
               for label, records in captures.items()}
    a, b = captures["baseline"][0], captures["candidate"][0]
    a_payload, a_package = checked["baseline"][0]
    b_payload, b_package = checked["candidate"][0]
    correctness = compare_payloads(a_payload, b_payload, a, b)
    same_input = {k: v for k, v in a["input_hashes"].items() if k != "code"} == {k: v for k, v in b["input_hashes"].items() if k != "code"}
    metrics = {stage + "_" + metric: metric_gate(
        [p["baseline"][stage][metric] for p in pairs], [p["candidate"][stage][metric] for p in pairs])
        for stage in ("cold", "resume") for metric in ("build_seconds", "peak_rss_bytes")}
    deterministic_pass = same_input and correctness["equal"] and controls["passed"]
    statuses = [value["status"] for value in metrics.values()]
    scoped = "regression" if not deterministic_pass or "regression" in statuses else "pass" if all(s == "pass" for s in statuses) else "inconclusive"
    return {
        "schema_version": 1,
        "scope": "Same-input current deterministic offline pipeline baseline versus candidate, not the original manual/AI workflow.",
        "baseline": {"source": identities["baseline"], "fingerprint": a["fingerprint"], "package": a_package},
        "candidate": {"source": identities["candidate"], "fingerprint": b["fingerprint"], "package": b_package},
        "input": {"manifest_sha256": manifest_sha, "fixture": a["fixture"], "products": a["products"], "identical_except_code": same_input},
        "runtime": a["input_hashes"]["runtime"], "runs": runs, "trial_pairs": pairs,
        "artifact_size_deltas_bytes": {
            "raw_zip": b_package["bytes"] - a_package["bytes"],
            "rendered_png": b_package["rendered_png_bytes"] - a_package["rendered_png_bytes"],
            "note": "Raw ZIP identity metadata can grow with new source files; this is reported, not called byte-identical or covered by the latency/RSS gate."},
        "correctness": correctness, "negative_controls": controls,
        "deterministic_repeated_package_bytes": True, "metrics": metrics,
        "measurement_notes": [
            "Each cold build uses fresh output and a fresh interpreter; cold means no application cache, not flushed OS disk cache.",
            "Each resume uses another fresh interpreter and validates the matching cold output; imports excluded from build_seconds.",
            "RSS is fresh child-process high-water at build completion, including interpreter/imports, not incremental or per-stage allocation.",
            "probe_process_seconds includes interpreter startup and post-build verification, so is diagnostic, not the gated API timing.",
            "Alternating paired order reduces order bias. Host contention, OS caches and measurement noise remain.",
            "No tolerated slowdown: threshold is 0%. Overlapping bootstrap ranges are inconclusive, never silently passed.",
            "Raw ZIP hashes may differ solely because source identity changes. Package members and every payload byte are checked; only verified provenance code hashes/fingerprint are normalized.",
        ],
        "gates": {"deterministic_offline_correctness": "pass" if deterministic_pass else "regression",
                  "observed_offline_quantitative": scoped,
                  "original_manual_or_ai_quality": "blocked_no_same_input_original_model_outputs",
                  "actual_product_semantic_fidelity": "blocked_no_real_product_goldens",
                  "user_mac_performance": "blocked_not_measured_on_user_mac",
                  "live_provider_quality_cost_latency": "blocked_no_authorized_live_provider_evaluation",
                  "overall_no_nerf": "regression" if scoped == "regression" else "blocked"},
        "publish_ready": False,
    }


def gate_exit_code(report: dict, gate: str) -> int:
    status = report["gates"]["overall_no_nerf" if gate == "overall" else "observed_offline_quantitative"]
    return 0 if status == "pass" else 1 if status == "regression" else 2


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--worker":
        try:
            print(json.dumps(_worker(*(Path(arg) for arg in sys.argv[2:])), ensure_ascii=False))
            return 0
        except Exception:
            print('{"status":"blocked","reason":"isolated_build_failed"}', file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Trusted immutable pre-change source tree")
    parser.add_argument("--candidate", type=Path, required=True, help="Trusted candidate source tree")
    parser.add_argument("--manifest", type=Path, required=True, help="Identical local manifest for both versions")
    parser.add_argument("--runs", type=int, default=9, help="Paired trials, 5–50; default 9")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, help="New/empty directory; keep outputs for review")
    parser.add_argument("--gate", choices=("overall", "offline"), default="overall",
                        help="Default overall remains blocked without original model/Mac evidence")
    args = parser.parse_args()
    try:
        if args.work_dir:
            args.work_dir.mkdir(parents=True, exist_ok=True)
            report = compare_sources(args.baseline, args.candidate, args.manifest, args.work_dir, args.runs)
        else:
            with tempfile.TemporaryDirectory(prefix="honeytem-compare-") as directory:
                report = compare_sources(args.baseline, args.candidate, args.manifest, Path(directory), args.runs)
        report["requested_gate"] = args.gate
        code = gate_exit_code(report, args.gate)
    except (ComparisonError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, zipfile.BadZipFile):
        report = {"schema_version": 1, "gates": {"overall_no_nerf": "blocked", "observed_offline_quantitative": "blocked"},
                  "error": "Comparison incomplete or inconsistent; no pass established.", "publish_ready": False}
        code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(canonical(report))
    print(json.dumps({"report": str(args.report), "gates": report["gates"], "exit_code": code}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
