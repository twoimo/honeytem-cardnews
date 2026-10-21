"""Explicit manual/model adapter boundary; no pretend model, network, or subprocess."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .schema import Job, ValidationError, exact, text, url, validate_manifest

TASK_KINDS = ("product_research", "editorial_copy", "authorized_image")


def tasks(job: Job) -> dict:
    return {"schema_version": 1, "fingerprint": job.fingerprint,
            "execution": "external_adapter_required; no model executed",
            "tasks": [{"product_id": p["id"], "kind": kind,
                       "input_fingerprint": job.fingerprint, "status": "not_started",
                       "constraints": ["Preserve IDs and exact issued links.", "Do not invent prices, facts, usage claims or licenses.", "Sales imagery must represent the actual authorized product.", "No upload, paid API, login or external action is authorized by this task file."]}
                      for p in job.manifest["products"] for kind in TASK_KINDS]}


def import_result(job: Job, result: dict, base_dir: Path | str) -> Job:
    """Validate an independently reviewed result into a new manifest and fingerprint.

    This does not execute an adapter or prove source truth. Cross-manifest evidence
    and human semantic review remain mandatory and visible in package QA.
    """
    exact(result, {"schema_version", "input_fingerprint", "product_id", "kind", "reviewed", "output"}, "adapter result")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1 or result["input_fingerprint"] != job.fingerprint or result["reviewed"] is not True:
        raise ValidationError("adapter result: stale input or missing explicit review")
    ids = [p["id"] for p in job.manifest["products"]]
    if result["product_id"] not in ids or result["kind"] not in TASK_KINDS:
        raise ValidationError("adapter result: invalid ID or task kind")
    new = deepcopy(job.manifest)
    product = new["products"][ids.index(result["product_id"])]
    output = result["output"]
    if result["kind"] == "editorial_copy":
        exact(output, {"title", "comment", "evidence_urls"}, "adapter output")
        text(output["title"], "adapter title", 240)
        text(output["comment"], "adapter comment", 240)
        if not isinstance(output["evidence_urls"], list) or (job.manifest["mode"] == "affiliate" and not output["evidence_urls"]):
            raise ValidationError("adapter output: sales copy requires evidence references")
        for reference in output["evidence_urls"]:
            url(reference, "adapter evidence")
        existing = [product["evidence"]["url"]] if product["evidence"] else []
        if output["evidence_urls"] != existing:
            raise ValidationError("adapter evidence: cite the retained product evidence URL exactly; research-update evidence before using a new source")
        product.update({k: output[k] for k in ("title", "comment")})
    elif result["kind"] == "authorized_image":
        product["asset"] = output
    else:
        if not isinstance(output, dict) or output.get("id") != product["id"]:
            raise ValidationError("adapter output: research must return full product with unchanged ID")
        # Research can correct facts, never issue or mutate affiliate tracking URLs.
        for key in ("affiliate_url", "original_url"):
            if output.get(key) != product[key]:
                raise ValidationError("adapter output: link mutation requires a fresh explicit input manifest")
        new["products"][ids.index(product["id"])] = output
    return validate_manifest(new, base_dir)
