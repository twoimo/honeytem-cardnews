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
    parser = argparse.ArgumentParser(prog="honeytem", description="Offline evidence-first cardnews review drafts. No network or publication.")
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
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
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
