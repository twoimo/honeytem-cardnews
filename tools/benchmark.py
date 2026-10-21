#!/usr/bin/env python3
"""Measure the current offline implementation; never imply an old/new speedup.

Example: PYTHONPATH=src python tools/benchmark.py /tmp/fixture/manifest.json --runs 3
All run outputs are temporary and removed. Input files remain untouched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from honeytem.pipeline import build, verify
from honeytem.schema import load_manifest


def summary(values):
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def benchmark(manifest: Path, runs: int) -> dict:
    job = load_manifest(manifest)
    samples = []
    hashes = set()
    package_bytes = None
    png_bytes = None
    with tempfile.TemporaryDirectory(prefix="honeytem-benchmark-") as root:
        for index in range(runs):
            output = Path(root) / str(index)
            start = time.perf_counter()
            cold = build(manifest, output)
            cold_seconds = time.perf_counter() - start
            start = time.perf_counter()
            warm = build(manifest, output)
            warm_seconds = time.perf_counter() - start
            if cold["fingerprint"] != warm["fingerprint"] or not warm["resumed"]:
                raise RuntimeError("Warm run did not resume the same fingerprint")
            run_dir = Path(cold["run_dir"])
            verify(run_dir)
            hashes.add(cold["package_sha256"])
            package_bytes = (run_dir / "package.zip").stat().st_size
            png_bytes = sum(path.stat().st_size for path in (run_dir / "rendered").glob("*.png"))
            samples.append({"cold_seconds": cold_seconds, "warm_verified_resume_seconds": warm_seconds})
    if len(hashes) != 1:
        raise RuntimeError("Identical cold builds produced different package hashes")
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = int(peak if platform.system() == "Darwin" else peak * 1024)
    except (ImportError, AttributeError):
        peak_bytes = None
    return {
        "measurement": "Current implementation baseline only; no earlier implementation or speedup claim.",
        "scope": "Local offline builds, including validation, input hashing, render, QA, and deterministic packaging; warm run revalidates integrity.",
        "runs": runs, "samples": samples,
        "cold_seconds": summary([sample["cold_seconds"] for sample in samples]),
        "warm_verified_resume_seconds": summary([sample["warm_verified_resume_seconds"] for sample in samples]),
        "process_peak_rss_bytes": peak_bytes,
        "memory_note": "Whole benchmark process high-water RSS, not incremental or per-stage allocation.",
        "runtime": job.input_hashes["runtime"], "platform": platform.platform(),
        "fingerprint": job.fingerprint, "package_sha256": next(iter(hashes)),
        "package_bytes": package_bytes, "rendered_png_bytes": png_bytes,
        "products": len(job.manifest["products"]), "fixture": job.manifest["fixture"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path, help="Optional JSON report path; stdout always receives report")
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    report = json.dumps(benchmark(args.manifest.absolute(), args.runs), ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
