"""Content-addressed resumable stages, atomic writes, and exact review packages."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import zipfile
import uuid

from .copy import caption, links
from .schema import Job, ValidationError, canonical, clean_path, digest, load_manifest, parse_json, safe_read, text

STAGES = ("validated", "rendered", "packaged")


def atomic_write(path: Path, data: bytes) -> None:
    path = clean_path(path, "output file")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    temp = ".pending-" + uuid.uuid4().hex
    created = False
    try:
        for part in path.parts[1:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError as exc:
        raise ValidationError("output: cannot safely write artifact") from exc
    finally:
        if created:
            os.unlink(temp, dir_fd=directory)
        os.close(directory)


@contextmanager
def locked(root: Path):
    """Fail closed on concurrent/stale lock; operator removes stale lock after inspection."""
    lock = root / ".honeytem.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValidationError("output: busy or stale lock; inspect active process before removing lock") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        clean_path(path, "artifact path")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(safe_read(path, "artifact file"))
        elif not path.is_dir():
            raise ValidationError("artifact: non-regular file forbidden")
    return result


def _check_files(root: Path, expected: dict) -> None:
    if not isinstance(expected, dict) or file_hashes(root) != expected:
        raise ValidationError("artifact: missing, added, or tampered stage file")


def _stage(state: dict, name: str, root: Path) -> None:
    if name == "rendered":
        _check_files(root / "rendered", state["stages"][name])
    else:
        for file, expected in state["stages"][name].items():
            path = clean_path(root / file, "stage file")
            if not path.is_relative_to(root) or digest(safe_read(path, "stage file", 256 * 1024 * 1024)) != expected:
                raise ValidationError("artifact: missing or tampered stage file")


def _read_state(root: Path) -> dict:
    state = parse_json(safe_read(root / "state.json", "state", 256 * 1024))
    if (not isinstance(state, dict) or set(state) != {"schema_version", "fingerprint", "stages"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 1
            or not isinstance(state["fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["fingerprint"])
            or state["fingerprint"] != root.name or not isinstance(state["stages"], dict)):
        raise ValidationError("state: invalid checkpoint structure")
    stages = state["stages"]
    if set(stages) not in [set(STAGES[:n]) for n in range(4)]:
        raise ValidationError("state: invalid stage sequence")
    for name, files in stages.items():
        if not isinstance(files, dict) or not files or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in files.values()):
            raise ValidationError("state: invalid stage digest map")
        if name == "validated" and set(files) != {"validated.json"}:
            raise ValidationError("state: missing or unexpected validation artifact")
        if name == "packaged" and set(files) != {"package.zip", "package-manifest.json"}:
            raise ValidationError("state: missing or unexpected package artifact")
        if name == "rendered":
            required = {"cover.png", "caption.txt", "links.json", "provenance.json", "qa.json", "REVIEW.txt"}
            product_names = {f for f in files if re.fullmatch(r"product-0[1-5]\.png", f)}
            originals = {f for f in files if re.fullmatch(r"originals/0[1-5]\.(?:png|jpg)", f)}
            n = len(product_names)
            if (not 1 <= n <= 5 or product_names != {f"product-{i:02d}.png" for i in range(1, n + 1)}
                    or len(originals) != n or {f.split("/")[1].split(".")[0] for f in originals} != {f"{i:02d}" for i in range(1, n + 1)}
                    or set(files) != required | product_names | originals):
                raise ValidationError("state: missing or unexpected render artifact")
    return state


def _save_state(root: Path, state: dict) -> None:
    atomic_write(root / "state.json", canonical(state))


def _zip(payload: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def _render(job: Job, root: Path) -> dict:
    from .render import render
    images, geometry = render(job.manifest, job.assets, *job.fonts)
    payload = dict(images)
    payload["caption.txt"] = caption(job).encode("utf-8")
    payload["links.json"] = canonical(links(job))
    originals = {}
    for p in job.manifest["products"]:
        raw = job.assets[p["id"]]
        extension = ".png" if raw.startswith(b"\x89PNG") else ".jpg"
        name = "originals/" + p["id"] + extension
        payload[name] = raw
        originals[p["id"]] = {"file": name, "sha256": digest(raw), "source": p["asset"]["source"], "license": p["asset"]["license"], "kind": p["asset"]["kind"], "transformation": "none; native-size paste into separate frame"}
    qa = {"automated_checks": "passed", "publish_ready": False, "fixture": job.manifest["fixture"],
          "manual_gates": {
              "product_evidence_truth_and_current_policy": "not_applicable" if job.manifest["mode"] == "concept" else "pending_operator_verification",
              "visual_aesthetic_and_semantic_quality": "pending_operator_review",
              "model_quality_non_regression": "blocked_no_real_model_comparison",
              "actual_product_visual_fidelity": "blocked_no_real_product_comparison" if job.manifest["mode"] == "concept" else "pending_source_comparison",
              "mac_performance": "blocked_not_run_on_mac",
              "publication": "not_implemented"}, "geometry": geometry}
    provenance = {"schema_version": 1, "fingerprint": job.fingerprint, "input_hashes": job.input_hashes,
                  "mode": job.manifest["mode"], "checked_at": job.manifest["checked_at"],
                  "disclosures": job.manifest["disclosures"], "assets": originals,
                  "products": [{k: p[k] for k in ("id", "title", "program", "seller", "option", "quantity", "price", "original_url", "affiliate_url", "evidence")} for p in job.manifest["products"]],
                  "limitations": ["Evidence confirmations are operator assertions, not independent online verification.", "No API, link issuance, model execution, or publication occurred.", "Original files can retain metadata; inspect rights and privacy before sharing."]}
    payload["qa.json"] = canonical(qa)
    payload["provenance.json"] = canonical(provenance)
    payload["REVIEW.txt"] = ("HONEYTEM REVIEW DRAFT\nNot approved for publication. Inspect every PNG, caption, exact link mapping, current policy, product evidence and image rights.\nThe offline synthetic fixture is neither a real product nor model-generated imagery.\nApproval records bind exact ZIP bytes only; no account or posting action is implemented.\n").encode()
    temp = Path(tempfile.mkdtemp(prefix=".render-", dir=root))
    try:
        for name, raw in payload.items():
            atomic_write(temp / name, raw)
        hashes = file_hashes(temp)
        if (root / "rendered").exists():
            # Recover an interrupted rename-before-state checkpoint only on exact equality.
            _check_files(root / "rendered", hashes)
            return hashes
        os.replace(temp, root / "rendered")
        return hashes
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def build(path: Path | str, output: Path | str, stop_after: str = "packaged") -> dict:
    if stop_after not in STAGES:
        raise ValidationError("stop_after: invalid stage")
    job = load_manifest(path)
    output = clean_path(Path(output), "output directory")
    source = Path(path).absolute().parent
    # Inputs must never live inside mutable output; sibling/subdirectory output is fine.
    if source == output or source.is_relative_to(output):
        raise ValidationError("output: cannot contain the input directory")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(output):
        root = clean_path(output / job.fingerprint, "run directory")
        root.mkdir(exist_ok=True, mode=0o700)
        state_path = root / "state.json"
        clean_path(state_path, "state")
        resumed = state_path.exists()
        state = _read_state(root) if resumed else {"schema_version": 1, "fingerprint": job.fingerprint, "stages": {}}
        for stage in STAGES:
            if stage in state["stages"]:
                _stage(state, stage, root)
        for stage in STAGES:
            if stage not in state["stages"]:
                if stage == "validated":
                    raw = canonical({"fingerprint": job.fingerprint, "input_hashes": job.input_hashes})
                    atomic_write(root / "validated.json", raw)
                    state["stages"][stage] = {"validated.json": digest(raw)}
                elif stage == "rendered":
                    state["stages"][stage] = _render(job, root)
                else:
                    payload = {name: safe_read(root / "rendered" / name, "rendered artifact") for name in state["stages"]["rendered"]}
                    manifest = {"schema_version": 1, "fingerprint": job.fingerprint, "files": state["stages"]["rendered"]}
                    manifest_raw = canonical(manifest)
                    payload["package-manifest.json"] = manifest_raw
                    package = _zip(payload)
                    atomic_write(root / "package.zip", package)
                    atomic_write(root / "package-manifest.json", manifest_raw)
                    state["stages"][stage] = {"package.zip": digest(package), "package-manifest.json": digest(manifest_raw)}
                _save_state(root, state)
            if stage == stop_after:
                break
        result = {"fingerprint": job.fingerprint, "run_dir": str(root), "stage": stop_after, "resumed": resumed}
        if "packaged" in state["stages"]:
            result["package_sha256"] = state["stages"]["packaged"]["package.zip"]
        return result


def verify(run_dir: Path | str) -> dict:
    root = clean_path(Path(run_dir), "run directory")
    try:
        state = _read_state(root)
        if set(state["stages"]) != set(STAGES) or state["fingerprint"] != root.name:
            raise ValidationError("verify: incomplete or mismatched run")
        for stage in STAGES:
            _stage(state, stage, root)
        manifest_raw = safe_read(root / "package-manifest.json", "package manifest", 256 * 1024)
        manifest = parse_json(manifest_raw)
        if manifest["fingerprint"] != state["fingerprint"] or manifest["files"] != state["stages"]["rendered"]:
            raise ValidationError("verify: manifest mismatch")
        package_raw = safe_read(root / "package.zip", "package", 256 * 1024 * 1024)
        with zipfile.ZipFile(BytesIO(package_raw)) as archive:
            infos = archive.infolist()
            if (len(infos) > 64 or sum(i.file_size for i in infos) > 256 * 1024 * 1024
                    or any(i.compress_type != zipfile.ZIP_STORED or i.file_size > 32 * 1024 * 1024 or i.flag_bits & 1 for i in infos)):
                raise ValidationError("verify: unsafe archive sizes, compression or encryption")
            expected = set(manifest["files"]) | {"package-manifest.json"}
            if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
                raise ValidationError("verify: archive contents mismatch")
            if archive.read("package-manifest.json") != manifest_raw:
                raise ValidationError("verify: archived manifest mismatch")
            for name, sha in manifest["files"].items():
                if digest(archive.read(name)) != sha:
                    raise ValidationError("verify: archived file digest mismatch")
    except (OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise ValidationError("verify: missing or malformed artifact") from exc
    sha = digest(package_raw)
    approval_path = root / "approval.json"
    approved = False
    if approval_path.exists():
        clean_path(approval_path, "approval path")
        approval = parse_json(safe_read(approval_path, "approval", 16 * 1024))
        if (not isinstance(approval, dict)
                or set(approval) != {"schema_version", "fingerprint", "package_sha256", "reviewer", "recorded_at", "scope", "publish_ready"}
                or type(approval["schema_version"]) is not int or approval["schema_version"] != 1
                or approval["publish_ready"] is not False):
            raise ValidationError("approval: invalid receipt")
        if approval.get("package_sha256") != sha or approval.get("fingerprint") != state["fingerprint"]:
            raise ValidationError("approval: does not match exact package")
        approved = True
    return {"fingerprint": state["fingerprint"], "package_sha256": sha, "integrity": "passed", "operator_receipt_present": approved, "publish_ready": False}


def record_approval(run_dir: Path | str, expected_sha: str, reviewer: str) -> dict:
    """Explicit local operator attestation, NOT authenticated permission or publication."""
    text(reviewer, "reviewer", 80)
    root = clean_path(Path(run_dir), "run directory")
    with locked(root.parent):
        result = verify(root)
        if expected_sha != result["package_sha256"]:
            raise ValidationError("approval: expected SHA256 does not match package")
        receipt = {"schema_version": 1, "fingerprint": result["fingerprint"], "package_sha256": expected_sha,
                   "reviewer": reviewer, "recorded_at": datetime.now(timezone.utc).isoformat(),
                   "scope": "Operator attestation for these exact review-draft bytes; not a publishing authorization or proof of quality.", "publish_ready": False}
        if (root / "approval.json").exists():
            raise ValidationError("approval: receipt already exists; preserve the original attestation")
        atomic_write(root / "approval.json", canonical(receipt))
        return receipt
