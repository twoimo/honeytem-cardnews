"""Durable, serial provider -> operator review -> offline package orchestration.

The journal reserves conservative request/token/byte caps before every POST.
A stopped process is never evidence that a request was not sent. Provider
responses cannot authorize their own import. Local receipts are operator
assertions, not signatures or independent evidence of product truth.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import stat

from .adapters import TASK_KINDS, import_result
from .pipeline import atomic_write, build, verify
from .providers import ProviderConfig, ProviderError, execute_provider, load_provider_config
from .schema import (ValidationError, canonical, clean_path, digest, exact, integer,
                     load_manifest, parse_json, safe_read, text, timestamp, validate_manifest)

_SHA = re.compile(r"[0-9a-f]{64}")
_MAX_JSON = 8 * 1024 * 1024
_DONE = {"retained", "imported", "rejected", "abandoned"}


def _error(message):
    return ValidationError("workflow: " + message)


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _error("invalid digest")
    return value


def _read(path, limit=_MAX_JSON):
    return parse_json(safe_read(path, "workflow JSON", limit))


def _checked(path, expected, limit=_MAX_JSON):
    raw = safe_read(path, "workflow artifact", limit)
    if digest(raw) != _sha(expected):
        raise _error("artifact digest mismatch")
    return raw


def _once(path, raw):
    if path.exists():
        if safe_read(path, "existing workflow artifact", max(len(raw), _MAX_JSON)) != raw:
            raise _error("existing artifact differs; refusing overwrite")
    else:
        atomic_write(path, raw)


@contextmanager
def _locked(directory):
    root = clean_path(Path(directory), "workflow directory")
    if not root.is_dir():
        raise _error("workflow directory missing")
    fd = os.open(root / ".workflow.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _error("invalid lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _error("busy; another workflow operation holds the lock") from None
        yield root
    finally:
        os.close(fd)  # OS releases flock on close or process death. Never unlink it.


def _config(config):
    return {key: deepcopy(getattr(config, key)) for key in config.__dataclass_fields__}


def _budget(value):
    exact(value, {"max_requests", "max_output_tokens", "max_request_bytes"}, "workflow budget")
    integer(value["max_requests"], "workflow requests", 0, 1000)
    integer(value["max_output_tokens"], "workflow tokens", 0, 32_768_000)
    integer(value["max_request_bytes"], "workflow bytes", 0, 2_097_152_000)
    return value


def _reel_settings(value, count):
    if value is None:
        return None
    allowed = {"durations", "fps", "padding_color", "vertical_align", "allow_affiliate_transcoding"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise _error("invalid reel options")
    from .reels import frame_plan, _settings
    settings = {"durations": "3", "fps": 30, "padding_color": "#FFFFFF",
                "vertical_align": "center", "allow_affiliate_transcoding": False, **value}
    if type(settings["allow_affiliate_transcoding"]) is not bool:
        raise _error("invalid reel rights confirmation")
    durations = settings["durations"]
    if not isinstance(durations, list):
        durations = [durations] * (count + 1)
    if len(durations) != count + 1:
        raise _error("reel durations must match cover and products")
    settings["durations"] = frame_plan(durations, settings["fps"])["durations"]
    settings.update({k: v for k, v in _settings(1080, 1920, settings["padding_color"], settings["vertical_align"]).items()
                     if k in ("padding_color", "vertical_align")})
    return settings


def _prepare(manifest_path, spec_path=None):
    path = clean_path(Path(manifest_path), "workflow manifest")
    source_raw = safe_read(path, "workflow manifest", 256 * 1024)
    job = validate_manifest(parse_json(source_raw), path.parent)
    spec = {"schema_version": 1, "tasks": [], "budget": {"max_requests": 0, "max_output_tokens": 0, "max_request_bytes": 0}, "reel": None}
    base = path.parent
    if spec_path is not None:
        spec_path = clean_path(Path(spec_path), "workflow spec")
        spec = _read(spec_path, 256 * 1024)
        base = spec_path.parent
    exact(spec, {"schema_version", "tasks", "budget", "reel"}, "workflow spec")
    integer(spec["schema_version"], "workflow spec version", 1, 1)
    _budget(spec["budget"])
    if not isinstance(spec["tasks"], list) or len(spec["tasks"]) > 15:
        raise _error("invalid task list")
    selected = {}
    payload = {}
    product_ids = {p["id"] for p in job.manifest["products"]}
    for item in spec["tasks"]:
        exact(item, {"product_id", "kind", "config", "source_material"}, "workflow task")
        if item["product_id"] not in product_ids or item["kind"] not in TASK_KINDS:
            raise _error("invalid product or task kind")
        task_id = item["product_id"] + "-" + item["kind"]
        if task_id in selected:
            raise _error("duplicate task")
        config = load_provider_config(clean_path(base / text(item["config"], "config path"), "provider config"))
        material = _read(clean_path(base / text(item["source_material"], "source path"), "source material"), 256 * 1024)
        image = item["kind"] == "authorized_image"
        if config.protocol != ("images_generations" if image else "chat_completions"):
            raise _error("task and provider protocol mismatch")
        if image and (job.manifest["mode"] != "concept" or job.manifest["fixture"]):
            raise _error("provider images require nonfixture concept inputs")
        if item["kind"] == "product_research" and job.manifest["mode"] != "affiliate":
            raise _error("provider research requires a promoted affiliate manifest")
        # Validate sources without network or credentials.
        from .providers import _source_material
        if image:
            exact(material, {"prompt"}, "image material")
            text(material["prompt"], "image prompt", 16000)
        else:
            product = next(p for p in job.manifest["products"] if p["id"] == item["product_id"])
            _source_material(material, product, item["kind"] == "product_research")
        config_raw, material_raw = canonical(_config(config)), canonical(material)
        payload["providers/" + task_id + ".json"] = config_raw
        payload["sources/" + task_id + ".json"] = material_raw
        selected[task_id] = {"config_sha256": digest(config_raw), "source_sha256": digest(material_raw)}
    manifest = deepcopy(job.manifest)
    for key, raw in zip(("regular", "bold"), job.fonts):
        name = "fonts/" + key + ".font"
        manifest["fonts"][key]["path"] = name
        payload["input/" + name] = raw
    for product in manifest["products"]:
        raw = job.assets[product["id"]]
        name = "assets/" + product["id"] + (".png" if raw.startswith(b"\x89PNG") else ".jpg")
        product["asset"]["path"] = name
        payload["input/" + name] = raw
    payload["input/manifest-0000.json"] = canonical(manifest)
    plan = {"schema_version": 1, "source_manifest_sha256": digest(source_raw), "source_fingerprint": job.fingerprint,
            "budget": deepcopy(spec["budget"]), "reel": _reel_settings(spec["reel"], len(product_ids)),
            "files": {name: digest(raw) for name, raw in payload.items()},
            "tasks": [{"id": p["id"] + "-" + kind, "product_id": p["id"], "kind": kind,
                       "mode": "provider" if p["id"] + "-" + kind in selected else "retain",
                       **selected.get(p["id"] + "-" + kind, {})}
                      for p in manifest["products"] for kind in TASK_KINDS]}
    return plan, payload


def plan_workflow(manifest_path, spec_path=None):
    """Validate and describe work without writes, requests, or credentials."""
    plan, _ = _prepare(manifest_path, spec_path)
    return {"plan": plan, "network_executed": False, "publish_ready": False}


def init_workflow(manifest_path, output, spec_path=None):
    plan, payload = _prepare(manifest_path, spec_path)
    root = clean_path(Path(output), "workflow output")
    if root.exists() or not root.parent.is_dir():
        raise _error("use a new output directory under an existing parent")
    root.mkdir(mode=0o700)
    with _locked(root):
        for name, raw in payload.items():
            _once(root / name, raw)
        initial = load_manifest(root / "input/manifest-0000.json")
        plan["initial_fingerprint"] = initial.fingerprint
        raw = canonical(plan)
        _once(root / "plan.json", raw)
        state = {"schema_version": 1, "plan_sha256": digest(raw), "events": []}
        atomic_write(root / "state.json", canonical(state))
        return _summary(root, *_load(root))


def _attempt_dir(root, task_id, attempt):
    return root / "candidates" / task_id / str(attempt)


def _candidate(root, task, info, event):
    directory = _attempt_dir(root, task["id"], info["attempt"])
    candidate = parse_json(_checked(directory / "candidate.json", event["candidate_sha256"]))
    exact(candidate, {"schema_version", "input_fingerprint", "product_id", "kind", "reviewed", "output"}, "workflow candidate")
    if (type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1
            or candidate["reviewed"] is not False or candidate["input_fingerprint"] != info["input_fingerprint"]
            or candidate["product_id"] != task["product_id"] or candidate["kind"] != task["kind"]):
        raise _error("candidate cannot approve itself or change its task")
    trace = parse_json(_checked(directory / "trace.json", event["trace_sha256"]))
    if (not isinstance(trace, dict) or trace.get("reviewed") is not False or trace.get("publish_ready") is not False
            or trace.get("input_fingerprint") != info["input_fingerprint"]
            or trace.get("status") != "candidate_returned" or trace.get("output_sha256") != digest(canonical(candidate["output"]))):
        raise _error("candidate trace mismatch")
    filename, image_sha = event["image_filename"], event["image_sha256"]
    if task["kind"] == "authorized_image":
        if not isinstance(filename, str) or not re.fullmatch(r"product-[0-9]{2}-[0-9a-f]{16}\.(?:png|jpg)", filename):
            raise _error("invalid generated image name")
        _checked(directory / filename, image_sha, 16 * 1024 * 1024)
        if candidate["output"].get("path") != filename or trace.get("image_sha256") != image_sha:
            raise _error("generated image mismatch")
    elif filename is not None or image_sha is not None:
        raise _error("unexpected image artifact")
    return candidate


def _reviewed(job, task, candidate, receipt, image_name=None):
    approved = deepcopy(candidate)
    approved["reviewed"] = True
    if task["kind"] == "product_research":
        if receipt["claims_verified"] is not True:
            raise _error("research acceptance needs explicit claims verification")
        approved["output"]["evidence"]["claims_verified"] = True
    elif receipt["claims_verified"] is not False:
        raise _error("unexpected claims confirmation")
    if task["kind"] == "authorized_image":
        if receipt["image_usage_confirmed"] is not True or receipt["image_layout_permission_confirmed"] is not True:
            raise _error("image acceptance needs explicit usage and layout rights")
        text(receipt["image_license"], "operator image license", 1000)
        approved["output"].update(path=image_name, license=receipt["image_license"], usage_confirmed=True, layout_permission_confirmed=True)
    elif receipt["image_license"] is not None or receipt["image_usage_confirmed"] is not False or receipt["image_layout_permission_confirmed"] is not False:
        raise _error("unexpected image confirmation")
    return approved


def _next(plan, runtime):
    return next((task for task in plan["tasks"] if runtime[task["id"]]["status"] not in _DONE), None)


def _load(root):
    try:
        return _replay(root)
    except (KeyError, TypeError, IndexError, ValueError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("malformed checkpoint; refusing recovery") from None


def _replay(root):
    """Strict replay: persisted counters/status/path strings are never trusted."""
    state = _read(root / "state.json")
    exact(state, {"schema_version", "plan_sha256", "events"}, "workflow state")
    integer(state["schema_version"], "workflow state version", 1, 1)
    plan = parse_json(_checked(root / "plan.json", state["plan_sha256"]))
    exact(plan, {"schema_version", "source_manifest_sha256", "source_fingerprint", "initial_fingerprint", "budget", "reel", "files", "tasks"}, "workflow plan")
    integer(plan["schema_version"], "workflow plan version", 1, 1)
    for key in ("source_manifest_sha256", "source_fingerprint", "initial_fingerprint"):
        _sha(plan[key])
    _budget(plan["budget"])
    if not isinstance(plan["files"], dict) or not isinstance(plan["tasks"], list) or not 3 <= len(plan["tasks"]) <= 15:
        raise _error("invalid plan inventory")
    # Derive every allowed pathname from validated task IDs, not checkpoint paths.
    runtime, expected_files = {}, {"input/manifest-0000.json", "input/fonts/regular.font", "input/fonts/bold.font"}
    for index, task in enumerate(plan["tasks"]):
        kind, product_id = TASK_KINDS[index % 3], f"{index // 3 + 1:02d}"
        keys = {"id", "product_id", "kind", "mode"}
        if not isinstance(task, dict) or task.get("mode") not in ("provider", "retain"):
            raise _error("invalid task mode")
        if task["mode"] == "provider":
            keys |= {"config_sha256", "source_sha256"}
        exact(task, keys, "workflow planned task")
        if task["id"] != product_id + "-" + kind or task["product_id"] != product_id or task["kind"] != kind:
            raise _error("invalid task ordering")
        runtime[task["id"]] = {"status": "pending" if task["mode"] == "provider" else "retained", "attempt": 0}
        if task["mode"] == "provider":
            for folder, field in (("providers", "config_sha256"), ("sources", "source_sha256")):
                name = folder + "/" + task["id"] + ".json"
                if plan["files"].get(name) != _sha(task[field]):
                    raise _error("provider/source snapshot mismatch")
                expected_files.add(name)
    if len(plan["tasks"]) % 3:
        raise _error("incomplete product tasks")
    for i in range(1, len(plan["tasks"]) // 3 + 1):
        names = {f"input/assets/{i:02d}.png", f"input/assets/{i:02d}.jpg"} & set(plan["files"])
        if len(names) != 1:
            raise _error("invalid asset snapshot inventory")
        expected_files |= names
    if set(plan["files"]) != expected_files:
        raise _error("unexpected snapshot path")
    for name, sha in plan["files"].items():
        _checked(root / name, sha, 64 * 1024 * 1024)
    job = load_manifest(root / "input/manifest-0000.json")
    if job.fingerprint != plan["initial_fingerprint"] or len(job.manifest["products"]) * 3 != len(plan["tasks"]):
        raise _error("stale input, code, runtime, or dependency fingerprint; initialize a new workflow")
    if _reel_settings(plan["reel"], len(job.manifest["products"])) != plan["reel"]:
        raise _error("noncanonical reel configuration")
    events = state["events"]
    if not isinstance(events, list) or len(events) > 6000:
        raise _error("invalid or oversized journal")
    context = {"job": job, "revision": 0, "requests_used": 0, "output_tokens_reserved": 0, "request_bytes_reserved": 0, "build": None, "reel": None}
    tasks = {task["id"]: task for task in plan["tasks"]}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise _error("invalid journal event")
        kind = event["type"]
        if kind in ("build", "reel"):
            if _next(plan, runtime) is not None:
                raise _error("build preceded task completion")
            if kind == "build":
                exact(event, {"type", "fingerprint", "package_sha256"}, "build event")
                if context["build"] is not None or event["fingerprint"] != context["job"].fingerprint:
                    raise _error("stale or duplicate build")
                checked = verify(root / "build" / event["fingerprint"])
                if checked["package_sha256"] != event["package_sha256"]:
                    raise _error("package changed")
                context["build"] = checked
            else:
                exact(event, {"type", "manifest_sha256"}, "reel event")
                if context["build"] is None or context["reel"] is not None or plan["reel"] is None:
                    raise _error("invalid reel sequence")
                context["reel"] = _verify_reel(root, plan, context)
                if context["reel"]["manifest_sha256"] != event["manifest_sha256"]:
                    raise _error("reel changed")
            continue
        task = tasks.get(event.get("task_id"))
        if task is None or task["mode"] != "provider" or context["build"] is not None:
            raise _error("invalid journal task")
        info = runtime[task["id"]]
        if kind == "start":
            exact(event, {"type", "task_id", "attempt", "input_fingerprint"}, "start event")
            if (_next(plan, runtime) != task or info["status"] != "pending" or type(event["attempt"]) is not int
                    or event["attempt"] != info["attempt"] + 1 or event["input_fingerprint"] != context["job"].fingerprint):
                raise _error("invalid start or stale dependency")
            info.update(status="running", attempt=event["attempt"], input_fingerprint=event["input_fingerprint"], reservations=0)
            info.pop("candidate", None)
            continue
        if event.get("attempt") != info["attempt"] or type(event.get("attempt")) is not int or info["attempt"] < 1:
            raise _error("invalid journal attempt")
        if kind == "reserve":
            exact(event, {"type", "task_id", "attempt", "tokens", "bytes"}, "reservation event")
            config = load_provider_config(root / "providers" / (task["id"] + ".json"))
            tokens = 0 if task["kind"] == "authorized_image" else config.max_output_tokens
            if info["status"] != "running" or event["tokens"] != tokens or type(event["tokens"]) is not int or event["bytes"] != config.max_request_bytes or type(event["bytes"]) is not int:
                raise _error("invalid durable reservation")
            info["reservations"] += 1
            if info["reservations"] > config.max_attempts:
                raise _error("attempt count exceeds configuration")
            context["requests_used"] += 1
            context["output_tokens_reserved"] += tokens
            context["request_bytes_reserved"] += config.max_request_bytes
        elif kind == "candidate":
            exact(event, {"type", "task_id", "attempt", "candidate_sha256", "trace_sha256", "image_filename", "image_sha256"}, "candidate event")
            if info["status"] != "running" or not info["reservations"]:
                raise _error("candidate without reserved request")
            _candidate(root, task, info, event)
            info.update(status="awaiting_review", candidate=deepcopy(event))
        elif kind in ("failure", "interrupted"):
            keys = {"type", "task_id", "attempt"}
            if kind == "failure":
                keys |= {"error_code", "outcome_unknown", "trace_sha256"}
            exact(event, keys, "failure event")
            if info["status"] != "running":
                raise _error("invalid failure sequence")
            if kind == "failure":
                if type(event["outcome_unknown"]) is not bool or not isinstance(event["error_code"], str) or not re.fullmatch(r"[a-z_]{1,100}", event["error_code"]):
                    raise _error("invalid provider failure")
                _checked(_attempt_dir(root, task["id"], info["attempt"]) / "trace.json", event["trace_sha256"])
            info["status"] = "uncertain" if kind == "interrupted" or event["outcome_unknown"] else "failed"
            info["error_code"] = "process_interrupted" if kind == "interrupted" else event["error_code"]
        elif kind == "review":
            exact(event, {"type", "task_id", "attempt", "receipt_sha256"}, "review event")
            if info["status"] != "awaiting_review":
                raise _error("review without candidate")
            receipt = parse_json(_checked(root / "reviews" / f"{task['id']}-{info['attempt']}.json", event["receipt_sha256"]))
            exact(receipt, {"task_id", "attempt", "candidate_sha256", "reviewer", "reviewed_at", "decision", "claims_verified", "image_license", "image_usage_confirmed", "image_layout_permission_confirmed", "input_fingerprint", "output_fingerprint"}, "operator review")
            _receipt(receipt, task, info)
            if receipt["candidate_sha256"] != info["candidate"]["candidate_sha256"] or receipt["input_fingerprint"] != context["job"].fingerprint:
                raise _error("stale operator review")
            if receipt["decision"] == "accept":
                candidate = _candidate(root, task, info, info["candidate"])
                image_name = _image_name(info) if task["kind"] == "authorized_image" else None
                if image_name is not None:
                    _checked(root / "input" / image_name, info["candidate"]["image_sha256"], 16 * 1024 * 1024)
                approved = _reviewed(context["job"], task, candidate, receipt, image_name)
                new = import_result(context["job"], approved, root / "input")
                context["revision"] += 1
                revision = load_manifest(root / "input" / f"manifest-{context['revision']:04d}.json")
                if revision.fingerprint != new.fingerprint or receipt["output_fingerprint"] != new.fingerprint:
                    raise _error("imported manifest mismatch")
                context["job"] = new
                info["status"] = "imported"
            elif receipt["decision"] == "reject":
                if receipt["output_fingerprint"] != context["job"].fingerprint:
                    raise _error("rejection changed the manifest")
                info["status"] = "rejected"
            else:
                raise _error("invalid review decision")
        elif kind == "resolve":
            exact(event, {"type", "task_id", "attempt", "receipt_sha256"}, "resolution event")
            if info["status"] not in ("failed", "uncertain"):
                raise _error("resolution requires stopped attempt")
            receipt = parse_json(_checked(root / "resolutions" / f"{task['id']}-{info['attempt']}.json", event["receipt_sha256"]))
            exact(receipt, {"task_id", "attempt", "reviewer", "reviewed_at", "resolution", "note", "attempt_sha256"}, "operator resolution")
            _receipt(receipt, task, info)
            text(receipt["note"], "operator resolution note", 1000)
            if receipt["attempt_sha256"] != _attempt_digest(task, info):
                raise _error("resolution changed or is stale")
            if receipt["resolution"] not in ("retry", "abandon"):
                raise _error("invalid resolution")
            info["status"] = "pending" if receipt["resolution"] == "retry" else "abandoned"
        else:
            raise _error("unknown journal event")
        if (context["requests_used"] > plan["budget"]["max_requests"] or context["output_tokens_reserved"] > plan["budget"]["max_output_tokens"]
                or context["request_bytes_reserved"] > plan["budget"]["max_request_bytes"]):
            raise _error("journal exceeds total budget")
    return plan, state, runtime, context


def _receipt(receipt, task, info):
    if receipt["task_id"] != task["id"] or type(receipt["attempt"]) is not int or receipt["attempt"] != info["attempt"]:
        raise _error("receipt task mismatch")
    text(receipt["reviewer"], "operator reviewer", 200)
    timestamp(receipt["reviewed_at"], "operator review time")


def _image_name(info):
    return "generated/" + info["candidate"]["image_sha256"] + Path(info["candidate"]["image_filename"]).suffix


def _attempt_digest(task, info):
    return digest(canonical({"task_id": task["id"], **info}))


def _summary(root, plan, state, runtime, context):
    items = []
    for task in plan["tasks"]:
        info = runtime[task["id"]]
        item = {"task_id": task["id"], "status": info["status"], "attempt": info["attempt"]}
        if "candidate" in info:
            item["candidate_sha256"] = info["candidate"]["candidate_sha256"]
            item["candidate_path"] = str(_attempt_dir(root, task["id"], info["attempt"]) / "candidate.json")
        if info["status"] in ("failed", "uncertain"):
            item["attempt_sha256"] = _attempt_digest(task, info)
            item["error_code"] = info.get("error_code")
        if info["status"] == "running":
            item["status"] = "interrupted_requires_recovery"
        items.append(item)
    complete = context["build"] is not None and (plan["reel"] is None or context["reel"] is not None)
    return {"workflow_dir": str(root), "plan_sha256": state["plan_sha256"], "fingerprint": context["job"].fingerprint,
            "revision": context["revision"], "status": "completed" if complete else "pending", "tasks": items,
            "budget": {**plan["budget"], **{key: context[key] for key in ("requests_used", "output_tokens_reserved", "request_bytes_reserved")}},
            "build": context["build"], "reel": context["reel"], "publish_ready": False}


def _append(root, state, event):
    if len(state["events"]) >= 6000:
        raise _error("journal event limit reached")
    proposed = {**state, "events": [*state["events"], event]}
    raw = canonical(proposed)
    if len(raw) > _MAX_JSON:
        raise _error("journal size limit reached")
    atomic_write(root / "state.json", raw)
    state["events"].append(event)


class _DurableBudget:
    def __init__(self, root, plan, state, task, info, context, config):
        self.root, self.plan, self.state, self.task, self.info = root, plan, state, task, info
        self.requests_used = context["requests_used"]
        self.output_tokens_reserved = context["output_tokens_reserved"]
        self.bytes_reserved = context["request_bytes_reserved"]
        self.config = config

    def reserve(self, tokens):
        cap = self.plan["budget"]
        if (self.requests_used + 1 > cap["max_requests"] or self.output_tokens_reserved + tokens > cap["max_output_tokens"]
                or self.bytes_reserved + self.config.max_request_bytes > cap["max_request_bytes"]):
            raise ProviderError("budget_exhausted")
        _append(self.root, self.state, {"type": "reserve", "task_id": self.task["id"], "attempt": self.info["attempt"],
                                     "tokens": tokens, "bytes": self.config.max_request_bytes})
        self.requests_used += 1
        self.output_tokens_reserved += tokens
        self.bytes_reserved += self.config.max_request_bytes


def _recover(root, plan, state, runtime):
    for task in plan["tasks"]:
        info = runtime[task["id"]]
        if info["status"] == "running":
            _append(root, state, {"type": "interrupted", "task_id": task["id"], "attempt": info["attempt"]})


def _check_legacy_lock(path):
    """Never remove a legacy lock: direct build/reel users do not share flock."""
    path = clean_path(path, "private legacy lock")
    if path.exists():
        safe_read(path, "private legacy lock", 100)
        raise _error("legacy build/reel lock exists; operator must inspect ownership before removing it")


def _verify_reel(root, plan, context):
    from .reels import verify_reel, frame_plan
    checked = verify_reel(root / "reel")
    manifest = _read(root / "reel/reel-manifest.json")
    if (manifest["source"]["package_sha256"] != context["build"]["package_sha256"]
            or manifest["source"]["fingerprint"] != context["job"].fingerprint
            or manifest["plan"] != frame_plan(plan["reel"]["durations"], plan["reel"]["fps"])
            or manifest["settings"] != {"width": 1080, "height": 1920, "padding_color": plan["reel"]["padding_color"], "vertical_align": plan["reel"]["vertical_align"]}
            or manifest["source"]["affiliate_transcoding_confirmed"] != plan["reel"]["allow_affiliate_transcoding"]):
        raise _error("reel does not match workflow plan")
    return checked


def workflow_status(directory):
    with _locked(directory) as root:
        return _summary(root, *_load(root))


def run_workflow(directory, *, allow_network=False, allow_remote=False):
    if type(allow_network) is not bool or type(allow_remote) is not bool:
        raise _error("network permissions must be explicit booleans")
    with _locked(directory) as root:
        plan, state, runtime, context = _load(root)
        _recover(root, plan, state, runtime)
        plan, state, runtime, context = _load(root)
        task = _next(plan, runtime)
        if task is not None:
            info = runtime[task["id"]]
            if info["status"] != "pending":
                return _summary(root, plan, state, runtime, context)
            if not allow_network:
                raise _error("network opt-in required for this run")
            config = load_provider_config(root / "providers" / (task["id"] + ".json"))
            from .providers import _endpoint
            if not _endpoint(config.base_url)[4] and not allow_remote:
                raise _error("remote opt-in required for this run")
            material = _read(root / "sources" / (task["id"] + ".json"), 256 * 1024)
            _append(root, state, {"type": "start", "task_id": task["id"], "attempt": info["attempt"] + 1, "input_fingerprint": context["job"].fingerprint})
            info = {**info, "attempt": info["attempt"] + 1}
            directory = _attempt_dir(root, task["id"], info["attempt"])
            budget = _DurableBudget(root, plan, state, task, info, context, config)
            try:
                result = execute_provider(context["job"], task["product_id"], task["kind"], config,
                                          allow_network=allow_network, allow_remote=allow_remote,
                                          source_material=material, budget=budget)
            except ProviderError as exc:
                raw = canonical(exc.trace)
                _once(directory / "trace.json", raw)
                _append(root, state, {"type": "failure", "task_id": task["id"], "attempt": info["attempt"],
                                     "error_code": exc.code, "outcome_unknown": exc.outcome_unknown, "trace_sha256": digest(raw)})
                return _summary(root, *_load(root))
            # Persist exact unreviewed bytes. Any crash before journal commit stays
            # uncertain even if an orphan candidate exists; it is never resubmitted.
            candidate_raw, trace_raw = canonical(result.adapter_result), canonical(result.trace)
            image_sha = None
            if result.image_bytes is not None:
                filename = result.image_filename
                if not isinstance(filename, str) or not re.fullmatch(r"product-[0-9]{2}-[0-9a-f]{16}\.(?:png|jpg)", filename):
                    raise _error("invalid provider image filename")
                image_sha = digest(result.image_bytes)
                _once(directory / filename, result.image_bytes)
            _once(directory / "candidate.json", candidate_raw)
            _once(directory / "trace.json", trace_raw)
            _append(root, state, {"type": "candidate", "task_id": task["id"], "attempt": info["attempt"],
                                 "candidate_sha256": digest(candidate_raw), "trace_sha256": digest(trace_raw),
                                 "image_filename": result.image_filename, "image_sha256": image_sha})
            return _summary(root, *_load(root))
        if context["build"] is None:
            # This private build location is used only under the workflow flock.
            # A killed local build can leave the legacy O_EXCL lock behind;
            # preserve it until an operator verifies ownership and removes it.
            _check_legacy_lock(root / "build/.honeytem.lock")
            result = build(root / "input" / f"manifest-{context['revision']:04d}.json", root / "build")
            _append(root, state, {"type": "build", "fingerprint": result["fingerprint"], "package_sha256": result["package_sha256"]})
            plan, state, runtime, context = _load(root)
        if plan["reel"] is not None and context["reel"] is None:
            if not (root / "reel").exists():
                from .reels import export_reel
                _check_legacy_lock(root / ".honeytem.lock")
                export_reel(root / "build" / context["job"].fingerprint, root / "reel", **plan["reel"])
            result = _verify_reel(root, plan, context)
            _append(root, state, {"type": "reel", "manifest_sha256": result["manifest_sha256"]})
        return _summary(root, *_load(root))


def review_workflow(directory, task_id, candidate_sha256, reviewer, *, decision="accept", claims_verified=False,
                    image_license=None, image_usage_confirmed=False, image_layout_permission_confirmed=False):
    _sha(candidate_sha256)
    text(reviewer, "operator reviewer", 200)
    if decision not in ("accept", "reject") or any(type(v) is not bool for v in (claims_verified, image_usage_confirmed, image_layout_permission_confirmed)):
        raise _error("invalid operator review options")
    with _locked(directory) as root:
        plan, state, runtime, context = _load(root)
        task = _next(plan, runtime)
        if task is None or task["id"] != task_id:
            raise _error("review must target the next dependency")
        info = runtime[task_id]
        if info["status"] != "awaiting_review" or info["candidate"]["candidate_sha256"] != candidate_sha256:
            raise _error("review needs the exact pending candidate digest")
        candidate = _candidate(root, task, info, info["candidate"])
        next_revision = root / "input" / f"manifest-{context['revision'] + 1:04d}.json"
        if decision == "reject" and next_revision.exists():
            raise _error("interrupted acceptance already wrote the next revision; repeat acceptance or initialize a new workflow")
        receipt = {"task_id": task_id, "attempt": info["attempt"], "candidate_sha256": candidate_sha256, "reviewer": reviewer,
                   "reviewed_at": datetime.now(timezone.utc).isoformat(), "decision": decision,
                   "claims_verified": claims_verified, "image_license": image_license, "image_usage_confirmed": image_usage_confirmed,
                   "image_layout_permission_confirmed": image_layout_permission_confirmed,
                   "input_fingerprint": context["job"].fingerprint, "output_fingerprint": context["job"].fingerprint}
        revision_raw = None
        if decision == "accept":
            image_name = None
            if task["kind"] == "authorized_image":
                image_name = _image_name(info)
                raw = _checked(_attempt_dir(root, task_id, info["attempt"]) / info["candidate"]["image_filename"], info["candidate"]["image_sha256"], 16 * 1024 * 1024)
                _once(root / "input" / image_name, raw)
            approved = _reviewed(context["job"], task, candidate, receipt, image_name)
            new = import_result(context["job"], approved, root / "input")
            receipt["output_fingerprint"] = new.fingerprint
            revision_raw = canonical(new.manifest)
        receipt_path = root / "reviews" / f"{task_id}-{info['attempt']}.json"
        # Recover a crash between immutable receipt write and journal commit only
        # if the repeated operator decision is identical (timestamp excepted).
        if receipt_path.exists():
            previous = _read(receipt_path)
            if {k: v for k, v in previous.items() if k != "reviewed_at"} != {k: v for k, v in receipt.items() if k != "reviewed_at"}:
                raise _error("an interrupted review has a different decision")
            receipt = previous
        raw = canonical(receipt)
        # Bind the operator decision durably before writing a numbered revision.
        # A crash before the receipt cannot orphan a revision; after the receipt,
        # only an identical operator decision can complete this transaction.
        _once(receipt_path, raw)
        if revision_raw is not None:
            _once(next_revision, revision_raw)
        _append(root, state, {"type": "review", "task_id": task_id, "attempt": info["attempt"], "receipt_sha256": digest(raw)})
        return _summary(root, *_load(root))


def resolve_workflow(directory, task_id, attempt_sha256, reviewer, resolution, note):
    """Explicitly authorize retry risk or retain the baseline after a stopped call."""
    _sha(attempt_sha256)
    text(reviewer, "operator reviewer", 200)
    text(note, "operator resolution note", 1000)
    if resolution not in ("retry", "abandon"):
        raise _error("resolution must be retry or abandon")
    with _locked(directory) as root:
        plan, state, runtime, context = _load(root)
        _recover(root, plan, state, runtime)
        plan, state, runtime, context = _load(root)
        task = _next(plan, runtime)
        if task is None or task["id"] != task_id:
            raise _error("resolution must target the next dependency")
        info = runtime[task_id]
        if info["status"] not in ("failed", "uncertain") or _attempt_digest(task, info) != attempt_sha256:
            raise _error("resolution needs the exact stopped attempt digest")
        receipt = {"task_id": task_id, "attempt": info["attempt"], "reviewer": reviewer,
                   "reviewed_at": datetime.now(timezone.utc).isoformat(), "resolution": resolution, "note": note, "attempt_sha256": attempt_sha256}
        path = root / "resolutions" / f"{task_id}-{info['attempt']}.json"
        if path.exists():
            previous = _read(path)
            if {k: v for k, v in previous.items() if k != "reviewed_at"} != {k: v for k, v in receipt.items() if k != "reviewed_at"}:
                raise _error("an interrupted resolution has a different decision")
            receipt = previous
        raw = canonical(receipt)
        _once(path, raw)
        _append(root, state, {"type": "resolve", "task_id": task_id, "attempt": info["attempt"], "receipt_sha256": digest(raw)})
        return _summary(root, *_load(root))


def register_cli(commands):
    for action in ("plan", "init", "run", "status", "review", "resolve"):
        parser = commands.add_parser("workflow-" + action)
        if action in ("plan", "init"):
            parser.add_argument("manifest", type=Path)
            parser.add_argument("--spec", type=Path)
            if action == "init":
                parser.add_argument("--output", type=Path, required=True)
        else:
            parser.add_argument("workflow_dir", type=Path)
        if action == "run":
            parser.add_argument("--allow-network", action="store_true")
            parser.add_argument("--allow-remote", action="store_true")
        if action in ("review", "resolve"):
            parser.add_argument("task_id")
            parser.add_argument("--reviewer", required=True)
        if action == "review":
            parser.add_argument("--candidate-sha256", required=True)
            parser.add_argument("--decision", choices=("accept", "reject"), required=True)
            parser.add_argument("--claims-verified", action="store_true")
            parser.add_argument("--image-license")
            parser.add_argument("--image-usage-confirmed", action="store_true")
            parser.add_argument("--image-layout-permission-confirmed", action="store_true")
        if action == "resolve":
            parser.add_argument("--attempt-sha256", required=True)
            parser.add_argument("--resolution", choices=("retry", "abandon"), required=True)
            parser.add_argument("--note", required=True)


def handle_cli(args):
    action = args.command.removeprefix("workflow-")
    if action == "plan":
        return plan_workflow(args.manifest, args.spec)
    if action == "init":
        return init_workflow(args.manifest, args.output, args.spec)
    if action == "run":
        return run_workflow(args.workflow_dir, allow_network=args.allow_network, allow_remote=args.allow_remote)
    if action == "status":
        return workflow_status(args.workflow_dir)
    if action == "review":
        return review_workflow(args.workflow_dir, args.task_id, args.candidate_sha256, args.reviewer, decision=args.decision,
                               claims_verified=args.claims_verified, image_license=args.image_license,
                               image_usage_confirmed=args.image_usage_confirmed, image_layout_permission_confirmed=args.image_layout_permission_confirmed)
    if action == "resolve":
        return resolve_workflow(args.workflow_dir, args.task_id, args.attempt_sha256, args.reviewer, args.resolution, args.note)
    raise _error("unknown command")
