"""Machine-readable CLI output; safe diagnostics never echo raw input secrets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .adapters import import_result, tasks
from .pipeline import atomic_write, build, record_approval, verify
from .schema import ValidationError, canonical, clean_path, load_manifest, parse_json, safe_read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="honeytem", description="Evidence-first drafts. Build is offline; optional providers require explicit network opt-in. No publication.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "build", "tasks", "import-result"):
        cmd = commands.add_parser(name)
        cmd.add_argument("manifest", type=Path)
        if name == "build":
            cmd.add_argument("--output", required=True, type=Path)
            cmd.add_argument("--stop-after", choices=("validated", "rendered", "packaged"), default="packaged")
        if name == "import-result":
            cmd.add_argument("result", type=Path)
            cmd.add_argument("--output", required=True, type=Path)
    for name in ("verify", "record-approval"):
        cmd = commands.add_parser(name)
        cmd.add_argument("run_dir", type=Path)
        if name == "record-approval":
            cmd.add_argument("--package-sha256", required=True)
            cmd.add_argument("--reviewer", required=True)
        else:
            cmd.add_argument("--expected-sha256")
    provider = commands.add_parser("execute-provider")
    provider.add_argument("manifest", type=Path)
    provider.add_argument("product_id")
    provider.add_argument("kind", choices=("product_research", "editorial_copy", "authorized_image"))
    provider.add_argument("--config", required=True, type=Path)
    provider.add_argument("--source-material", required=True, type=Path)
    provider.add_argument("--output", required=True, type=Path)
    provider.add_argument("--allow-network", action="store_true")
    provider.add_argument("--allow-remote", action="store_true")
    reel = commands.add_parser("reel")
    reel.add_argument("run_dir", type=Path)
    reel.add_argument("--output", required=True, type=Path)
    reel.add_argument("--durations", nargs="+", default=["3"])
    reel.add_argument("--allow-affiliate-transcoding", action="store_true")
    reel.add_argument("--fps", type=int, default=30)
    reel.add_argument("--padding-color", default="#FFFFFF")
    reel.add_argument("--vertical-align", choices=("top", "center", "bottom"), default="center")
    reel_verify = commands.add_parser("verify-reel")
    reel_verify.add_argument("export_dir", type=Path)
    reel_verify.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "execute-provider":
            # Lazy imports keep the offline path independent of network adapters.
            from .providers import ProviderError, execute_provider, load_provider_config
            job = load_manifest(args.manifest)
            config = load_provider_config(args.config)
            material = parse_json(safe_read(clean_path(args.source_material, "source material"),
                                            "source material", 256 * 1024))
            destination = clean_path(args.output, "provider output")
            if destination.exists() or not destination.parent.is_dir():
                raise ValidationError("provider output: use a new directory inside an existing parent")
            # Reserve before a potentially billable call. Never overwrite or retry
            # an existing attempt automatically, even if no candidate was saved.
            destination.mkdir(mode=0o700)
            atomic_write(destination / "attempt.json", canonical({"status": "started", "reviewed": False}))
            try:
                proposal = execute_provider(job, args.product_id, args.kind, config,
                                            allow_network=args.allow_network, allow_remote=args.allow_remote,
                                            source_material=material)
            except ProviderError as exc:
                atomic_write(destination / "trace.json", canonical(exc.trace))
                atomic_write(destination / "attempt.json", canonical({
                    "status": "failed", "error_code": exc.code,
                    "outcome_unknown": exc.outcome_unknown, "reviewed": False}))
                raise
            if proposal.image_bytes is not None:
                filename = proposal.image_filename
                if not isinstance(filename, str) or Path(filename).name != filename or filename in (".", "..", "attempt.json", "candidate.json", "trace.json") or "\\" in filename:
                    raise ValidationError("provider image: unsafe output filename")
                atomic_write(destination / filename, proposal.image_bytes)
            atomic_write(destination / "candidate.json", canonical(proposal.adapter_result))
            atomic_write(destination / "trace.json", canonical(proposal.trace))
            atomic_write(destination / "attempt.json", canonical({"status": "completed", "reviewed": False}))
            result = {"candidate_written": True, "reviewed": False, "publish_ready": False}
        elif args.command == "reel":
            from .reels import export_reel
            durations = args.durations[0] if len(args.durations) == 1 else args.durations
            result = export_reel(args.run_dir, args.output, durations=durations, fps=args.fps,
                                 padding_color=args.padding_color, vertical_align=args.vertical_align,
                                 allow_affiliate_transcoding=args.allow_affiliate_transcoding)
        elif args.command == "verify-reel":
            from .reels import verify_reel
            result = verify_reel(args.export_dir)
            if args.expected_manifest_sha256 is not None and args.expected_manifest_sha256 != result["manifest_sha256"]:
                raise ValidationError("verify-reel: manifest does not match trusted expected SHA256")
        elif args.command == "build":
            result = build(args.manifest, args.output, args.stop_after)
        elif args.command == "verify":
            result = verify(args.run_dir)
            if args.expected_sha256 is not None and args.expected_sha256 != result["package_sha256"]:
                raise ValidationError("verify: package does not match trusted expected SHA256")
        elif args.command == "record-approval":
            result = record_approval(args.run_dir, args.package_sha256, args.reviewer)
        else:
            job = load_manifest(args.manifest)
            if args.command == "validate":
                result = {"valid": True, "fingerprint": job.fingerprint, "publish_ready": False}
            elif args.command == "tasks":
                result = tasks(job)
            else:
                source = clean_path(args.result, "adapter result")
                if not source.is_file() or source.stat().st_size > 256 * 1024:
                    raise ValidationError("adapter result: missing or oversized file")
                new = import_result(job, parse_json(safe_read(source, "adapter result", 256 * 1024)), args.manifest.absolute().parent)
                destination = clean_path(args.output, "output manifest")
                if destination.exists():
                    raise ValidationError("output manifest: must be a new file")
                if destination.parent != args.manifest.absolute().parent:
                    raise ValidationError("output manifest: use same directory to preserve relative asset paths")
                atomic_write(destination, canonical(new.manifest))
                result = {"fingerprint": new.fingerprint, "manifest_written": True, "publish_ready": False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        # Do not echo Pillow messages, raw data, filesystem paths, credentials, or URLs.
        print('{"error":"invalid input or artifact; inspect local files and schema"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
