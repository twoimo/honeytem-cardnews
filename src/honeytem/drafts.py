"""Offline cold-start ingestion. Extracted facts are candidates, never confirmations.

A draft is deliberately not a valid build manifest. All source bytes are retained
locally, hash-bound and treated as data. No URL fetching, HTML execution or remote
provider is used here. Promotion is an explicit, digest-bound operator review.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import uuid

from .schema import (PROGRAMS, ValidationError, canonical, clean_path, digest, exact,
                     integer, parse_json, safe_read, text, timestamp, url,
                     validate_manifest)

MAX_SOURCE_BYTES = 1024 * 1024
MAX_DRAFT_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 16
FORMATS = ("auto", "html", "json", "text")
_HASH = re.compile(r"[0-9a-f]{64}")
_FIXED = {"schema_version", "mode", "fixture", "products[0].id", "products[0].program",
          "products[0].original_url", "products[0].asset.kind", "channel.name",
          "disclosures[0].program"}
_BOOL = {"channel.profile_links_confirmed", "products[0].evidence.claims_verified",
         "products[0].evidence.link_ownership_confirmed", "products[0].asset.usage_confirmed",
         "products[0].asset.layout_permission_confirmed", "disclosures[0].verified"}
_CONFIRM = _BOOL - {"channel.profile_links_confirmed"}
_INT = {"variant": (0, 9), "fonts.regular.index": (0, 32), "fonts.bold.index": (0, 32),
        "products[0].quantity": (1, 1000), "products[0].price.amount": (0, 1_000_000_000)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(reason: str) -> None:
    raise ValidationError("draft: " + reason)


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _error("requires SHA256 digest")
    return value


def _new_file(path: Path, content: bytes) -> None:
    """Atomically install a new file without replacing another writer's file."""
    path = clean_path(path, "draft output")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    temp = ".draft-pending-" + uuid.uuid4().hex
    created = False
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.unlink(temp, dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError as exc:
        raise ValidationError("draft output: must be a new file in a safe existing directory") from exc
    finally:
        if created:
            os.unlink(temp, dir_fd=directory)
        os.close(directory)


def _template(product_url: str, program: str) -> dict:
    return {
        "schema_version": 1, "mode": "affiliate", "fixture": False,
        "edition": None, "title": None, "subtitle": None, "variant": 0, "checked_at": None,
        "fonts": {name: {"path": None, "index": 0} for name in ("regular", "bold")},
        "channel": {"name": "instagram", "status": None, "evidence_url": None,
                    "profile_links_confirmed": False},
        "disclosures": [{"program": program, "text": None, "reference_url": None,
                         "checked_at": None, "verified": False}],
        "products": [{"id": "01", "title": None, "comment": None, "program": program,
                      "seller": None, "option": None, "quantity": None,
                      "price": {"amount": None, "currency": None, "conditions": None, "shipping": None},
                      "original_url": product_url, "affiliate_url": None,
                      "evidence": {"url": None, "checked_at": None, "claims_verified": False,
                                   "link_ownership_confirmed": False},
                      "asset": {"path": None, "kind": "actual", "source": None, "license": None,
                                "usage_confirmed": False, "layout_permission_confirmed": False}}],
    }


def _leaves(value: object, prefix: str = "") -> dict:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            result.update(_leaves(child, prefix + "." + key if prefix else key))
        return result
    if isinstance(value, list):
        result = {}
        for i, child in enumerate(value):
            result.update(_leaves(child, f"{prefix}[{i}]"))
        return result
    return {prefix: value}


def _shape(value: object, template: object) -> None:
    if isinstance(template, dict):
        if not isinstance(value, dict) or set(value) != set(template):
            _error("invalid manifest field structure")
        for key in template:
            _shape(value[key], template[key])
    elif isinstance(template, list):
        if not isinstance(value, list) or len(value) != len(template):
            _error("a product draft contains exactly one product and one disclosure")
        for child, expected in zip(value, template):
            _shape(child, expected)
    elif isinstance(value, (dict, list)):
        _error("manifest leaf cannot contain an object or array")


def _partial_manifest(manifest: dict, product_url: str, program: str) -> None:
    template = _template(product_url, program)
    _shape(manifest, template)
    defaults = _leaves(template)
    for key, value in _leaves(manifest).items():
        if key in _FIXED:
            if type(value) is not type(defaults[key]) or value != defaults[key]:
                _error("draft identity and mode fields cannot change")
        elif key in _BOOL:
            if type(value) is not bool:
                _error("confirmation fields require explicit booleans")
        elif value is None:
            continue
        elif key in _INT:
            integer(value, key, *_INT[key])
        elif key.endswith("checked_at"):
            timestamp(value, key)
        elif key.endswith("_url") or key.endswith(".url"):
            url(value, key)
        else:
            text(value, key, 4096 if key.endswith(".path") else 1000)
            if key == "channel.status" and value != "confirmed":
                _error("channel status must be null or explicitly confirmed")
            if key == "products[0].price.currency" and value != "KRW":
                _error("only KRW can enter the reviewed manifest")
            if key.endswith(".path"):
                path = Path(value)
                if ".." in path.parts or "\\" in value or (path.is_absolute() and not key.startswith("fonts.")):
                    _error("unsafe asset or font path")


def _candidate_text(value: object, limit: int = 1000) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    try:
        return text(value, "candidate", limit)
    except ValidationError:
        return None


def _amount(value: object) -> int | None:
    # JSON floating-point parsing can round a fractional price to an integer or
    # underflow to zero. Only integer tokens and exact decimal strings qualify.
    if type(value) not in (int, str) or (isinstance(value, str) and not re.fullmatch(r"\d+(?:\.0+)?", value.strip())):
        return None
    try:
        number = Decimal(str(value))
        if number.is_finite() and number == number.to_integral_value() and 0 <= number <= 1_000_000_000:
            return int(number)
    except (InvalidOperation, ValueError):
        pass
    return None


class _StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self.active: list[str] | None = None
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        types = [value for key, value in attrs if key.lower() == "type"]
        if len(types) == 1 and isinstance(types[0], str) and types[0].lower().strip() == "application/ld+json":
            self.active = []
        elif len(types) > 1:
            self.invalid = True

    def handle_data(self, data):
        if self.active is not None:
            self.active.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.active is not None:
            self.blocks.append("".join(self.active))
            self.active = None


def _products(document: object) -> list[tuple[dict, str]]:
    """Only explicitly typed Product nodes or a plain product JSON object.

    Limit nesting and nodes independently of the input byte bound. A plain
    object needs a name/title plus at least one product-specific field.
    """
    found: list[tuple[dict, str]] = []
    stack = [(document, "$", 0)]
    count = 0
    while stack:
        node, locator, depth = stack.pop()
        count += 1
        if depth > 48 or count > 10000:
            _error("source structure exceeds extraction limits")
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            typed = any(k in ("Product", "https://schema.org/Product", "http://schema.org/Product") for k in kinds if isinstance(k, str))
            plain_location = locator == "$" or locator.endswith(".product") or bool(re.search(r"(?:^\$|\.products?)\[\d+\]$", locator))
            plain = plain_location and kind is None and any(k in node for k in ("name", "title")) and any(k in node for k in ("offers", "price", "seller", "sku", "image"))
            if typed or plain:
                found.append((node, locator))
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    # Do not expose arbitrary untrusted object keys as diagnostics.
                    stack.append((child, locator + "." + key if key in {"@graph", "product", "products", "itemListElement", "item", "mainEntity"} else locator + ".node", depth + 1))
        elif isinstance(node, list):
            stack.extend((child, f"{locator}[{i}]", depth + 1) for i, child in enumerate(node))
    return found


def _fields(product: dict) -> tuple[dict, list[str]]:
    fields, warnings = {}, []
    title = _candidate_text(product.get("name", product.get("title")), 240)
    if title:
        fields["title"] = title
    seller = product.get("seller")
    if isinstance(seller, dict):
        seller = seller.get("name")
    if _candidate_text(seller, 100):
        fields["seller"] = _candidate_text(seller, 100)
    for key in ("sku", "description"):
        if _candidate_text(product.get(key)):
            fields[key] = _candidate_text(product[key])
    for source, target in (("url", "product_url"), ("image", "image_url")):
        value = product.get(source)
        if isinstance(value, dict):
            value = value.get("url", value.get("contentUrl"))
        if isinstance(value, str):
            try:
                fields[target] = url(value, "candidate URL")
            except ValidationError:
                warnings.append("unsafe_candidate_url_omitted")
    offers = product.get("offers", product)
    if isinstance(offers, list):
        if len(offers) == 1:
            offers = offers[0]
        else:
            offers = None
            warnings.append("multiple_offers_not_selected")
    if isinstance(offers, dict):
        price = offers.get("price")
        price_object = price if isinstance(price, dict) else {}
        amount = _amount(price_object.get("amount") if price_object else price)
        currency = _candidate_text(offers.get("priceCurrency", offers.get("currency", price_object.get("currency"))), 12)
        if amount is not None:
            fields["price.amount"] = amount
        if currency:
            fields["price.currency"] = currency
        seller = offers.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("name")
        if "seller" not in fields and _candidate_text(seller, 100):
            fields["seller"] = _candidate_text(seller, 100)
    return fields, warnings


def _extract(raw: bytes, format: str, source_hash: str) -> dict:
    result = {"status": "no_product", "product_count": 0, "candidates": {}, "warnings": []}
    try:
        content = raw.decode("utf-8-sig")
        if "\x00" in content:
            raise ValueError
        if format == "text":
            # Deliberately narrow labeled text; free prose is retained, not guessed.
            values: dict[str, list[str]] = {}
            keys = {"title": "name", "name": "name", "상품명": "name", "seller": "seller", "판매자": "seller",
                    "price": "price", "가격": "price", "currency": "currency", "통화": "currency", "sku": "sku"}
            for line in content.splitlines():
                match = re.fullmatch(r"\s*([^:：]{1,30})\s*[:：]\s*(.{1,1000})\s*", line)
                if match and match[1].strip().casefold() in keys:
                    key = keys[match[1].strip().casefold()]
                    values.setdefault(key, []).append(match[2].strip())
            if any(len(v) > 1 for v in values.values()):
                result.update(status="ambiguous", warnings=["repeated_text_fields_not_selected"])
                return result
            product = {key: values[0] for key, values in values.items()}
            products = [(product, "labeled_text")] if product else []
        else:
            if format == "html":
                parser = _StructuredHTML()
                parser.feed(content)
                parser.close()
                if parser.active is not None or parser.invalid or len(parser.blocks) > 64:
                    raise ValueError
                documents = [parse_json(block.encode("utf-8")) for block in parser.blocks]
            else:
                documents = [parse_json(raw)]
            products = []
            for i, document in enumerate(documents):
                products.extend((p, f"document[{i}]:{loc}") for p, loc in _products(document))
        result["product_count"] = len(products)
        if len(products) > 1:
            result.update(status="ambiguous", warnings=["multiple_products_not_selected"])
            return result
        if products:
            fields, warnings = _fields(products[0][0])
            result.update(status="candidates" if fields else "no_product", warnings=warnings)
            result["candidates"] = {field: {"value": value, "source_sha256": source_hash,
                                           "locator": products[0][1], "verified": False}
                                    for field, value in fields.items()}
    except (ValidationError, UnicodeError, ValueError, TypeError, RecursionError):
        result.update(status="invalid", product_count=0, candidates={}, warnings=["source_could_not_be_safely_extracted"])
    return result


def _source(path: Path | str, format: str) -> tuple[bytes, str]:
    if format not in FORMATS:
        _error("unsupported source format")
    path = clean_path(Path(path), "source")
    raw = safe_read(path, "draft source", MAX_SOURCE_BYTES)
    if format == "auto":
        suffix = path.suffix.lower()
        stripped = raw.lstrip(b"\xef\xbb\xbf \t\r\n")
        format = "html" if suffix in (".html", ".htm") or stripped.startswith(b"<") else "json" if suffix == ".json" or stripped.startswith((b"{", b"[")) else "text"
    return raw, format


def _append_source(draft: dict, base: Path, raw: bytes, format: str) -> None:
    if len(draft["sources"]) >= MAX_SOURCES:
        _error("too many sources")
    sha = digest(raw)
    if any(source["sha256"] == sha for source in draft["sources"]):
        _error("source already ingested")
    relative = f"sources/{sha}.bin"
    target = clean_path(base / relative, "source snapshot")
    if target.exists():
        if safe_read(target, "source snapshot", MAX_SOURCE_BYTES) != raw:
            _error("snapshot digest collision or tampering")
    else:
        _new_file(target, raw)
    draft["sources"].append({"path": relative, "sha256": sha, "bytes": len(raw),
                             "format": format, "source_url": draft["product_url"],
                             "imported_at": _now(), "extraction": _extract(raw, format, sha)})


def _validate(draft: object, base: Path) -> dict:
    exact(draft, {"schema_version", "kind", "created_at", "product_url", "program", "manifest",
                  "sources", "review_log", "revision", "parent_sha256", "publish_ready"}, "draft")
    if type(draft["schema_version"]) is not int or draft["schema_version"] != 1 or draft["kind"] != "honeytem_product_draft" or draft["publish_ready"] is not False:
        _error("invalid draft type or publication claim")
    timestamp(draft["created_at"], "draft.created_at")
    url(draft["product_url"], "draft.product_url")
    if not isinstance(draft["program"], str) or draft["program"] not in PROGRAMS:
        _error("unsupported program")
    integer(draft["revision"], "draft.revision", 0, 1000)
    if draft["revision"] == 0:
        if draft["parent_sha256"] is not None:
            _error("initial draft cannot have a parent")
    else:
        _hash(draft["parent_sha256"])
    _partial_manifest(draft["manifest"], draft["product_url"], draft["program"])
    sources = draft["sources"]
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        _error("invalid sources list")
    seen = set()
    for source in sources:
        exact(source, {"path", "sha256", "bytes", "format", "source_url", "imported_at", "extraction"}, "draft source")
        sha = _hash(source["sha256"])
        if source["path"] != f"sources/{sha}.bin" or source["format"] not in FORMATS[1:] or source["source_url"] != draft["product_url"] or sha in seen:
            _error("invalid source snapshot metadata")
        seen.add(sha)
        integer(source["bytes"], "source bytes", 0, MAX_SOURCE_BYTES)
        timestamp(source["imported_at"], "source imported_at")
        raw = safe_read(base / source["path"], "source snapshot", MAX_SOURCE_BYTES)
        if len(raw) != source["bytes"] or digest(raw) != sha:
            _error("source snapshot was changed")
        # Never trust editable candidate flags or values; recompute from bound bytes.
        if canonical(source["extraction"]) != canonical(_extract(raw, source["format"], sha)):
            _error("extraction was changed; ingest source instead")
    reviews = draft["review_log"]
    if not isinstance(reviews, list) or len(reviews) > 1000:
        _error("invalid review log")
    allowed = set(_leaves(draft["manifest"])) - _FIXED
    for review in reviews:
        exact(review, {"reviewer", "reviewed_at", "parent_sha256", "fields"}, "draft review")
        text(review["reviewer"], "reviewer", 200)
        timestamp(review["reviewed_at"], "reviewed_at")
        _hash(review["parent_sha256"])
        if not isinstance(review["fields"], list) or not review["fields"] or any(not isinstance(k, str) or k not in allowed for k in review["fields"]) or len(set(review["fields"])) != len(review["fields"]):
            _error("invalid reviewed fields")
    return draft


def load_draft(path: Path | str) -> dict:
    path = clean_path(Path(path), "draft path")
    return _validate(parse_json(safe_read(path, "draft", MAX_DRAFT_BYTES)), path.parent)


def _read_bound(path: Path | str, expected_sha256: str) -> tuple[Path, dict, str]:
    path = clean_path(Path(path), "draft path")
    raw = safe_read(path, "draft", MAX_DRAFT_BYTES)
    sha = digest(raw)
    if sha != _hash(expected_sha256):
        _error("draft does not match reviewed SHA256")
    return path, _validate(parse_json(raw), path.parent), sha


def _destination(path: Path | str, base: Path) -> Path:
    path = clean_path(Path(path), "draft output")
    if path.parent != base or path.exists() or path.name.startswith("."):
        _error("output must be a new visible file in the same draft directory")
    return path


def _report(draft: dict, sha: str) -> dict:
    missing = [{"field": key, "reason": "explicit_confirmation_required" if key in _CONFIRM else "missing_value"}
               for key, value in sorted(_leaves(draft["manifest"]).items())
               if value is None or (key in _CONFIRM and value is not True)]
    return {"draft_sha256": sha, "revision": draft["revision"], "missing_items": missing,
            "missing_count": len(missing), "source_count": len(draft["sources"]),
            "extractions": [{"source_sha256": s["sha256"], **s["extraction"]} for s in draft["sources"]],
            "requires_explicit_promotion": True, "publish_ready": False}


def draft_report(path: Path | str) -> dict:
    path = clean_path(Path(path), "draft path")
    raw = safe_read(path, "draft", MAX_DRAFT_BYTES)
    return _report(_validate(parse_json(raw), path.parent), digest(raw))


def init_draft(product_url: str, program: str, output_dir: Path | str, *,
               source_path: Path | str | None = None, source_format: str = "auto") -> dict:
    url(product_url, "product URL")
    if not isinstance(program, str) or program not in PROGRAMS:
        _error("unsupported program")
    if source_format not in FORMATS:
        _error("unsupported source format")
    source = _source(source_path, source_format) if source_path is not None else None
    output = clean_path(Path(output_dir), "draft directory")
    if output.exists() or not output.parent.is_dir():
        _error("use a new directory inside an existing parent")
    output.mkdir(mode=0o700)
    (output / "sources").mkdir(mode=0o700)
    draft = {"schema_version": 1, "kind": "honeytem_product_draft", "created_at": _now(),
             "product_url": product_url, "program": program, "manifest": _template(product_url, program),
             "sources": [], "review_log": [], "revision": 0, "parent_sha256": None, "publish_ready": False}
    if source:
        _append_source(draft, output, *source)
    raw = canonical(draft)
    _new_file(output / "draft.json", raw)
    return _report(draft, digest(raw))


def ingest_source(draft_path: Path | str, source_path: Path | str, output_path: Path | str, *,
                  expected_draft_sha256: str, source_format: str = "auto") -> dict:
    path, draft, sha = _read_bound(draft_path, expected_draft_sha256)
    destination = _destination(output_path, path.parent)
    raw, format = _source(source_path, source_format)
    _append_source(draft, path.parent, raw, format)
    draft.update(revision=draft["revision"] + 1, parent_sha256=sha)
    raw = canonical(draft)
    _validate(draft, path.parent)
    _new_file(destination, raw)
    return _report(draft, digest(raw))


def _merge(target: object, patch: object, prefix: str = "") -> list[str]:
    changed = []
    if isinstance(target, dict):
        if not isinstance(patch, dict) or not set(patch) <= set(target):
            _error("review patch contains unknown fields")
        for key, value in patch.items():
            name = prefix + "." + key if prefix else key
            if isinstance(target[key], (dict, list)):
                changed.extend(_merge(target[key], value, name))
            else:
                if name in _FIXED:
                    if type(value) is not type(target[key]) or value != target[key]:
                        _error("review patch cannot change draft identity or mode")
                    continue
                target[key] = value
                changed.append(name)
    elif isinstance(target, list):
        if not isinstance(patch, list) or len(patch) != len(target):
            _error("review patch arrays must match the single-product draft")
        for i, value in enumerate(patch):
            changed.extend(_merge(target[i], value, f"{prefix}[{i}]"))
    return changed


def review_draft(draft_path: Path | str, patch: dict, output_path: Path | str, *,
                 reviewer: str, expected_draft_sha256: str, reviewed: bool = False) -> dict:
    if reviewed is not True:
        _error("explicit reviewed acknowledgement required")
    text(reviewer, "reviewer", 200)
    path, draft, sha = _read_bound(draft_path, expected_draft_sha256)
    destination = _destination(output_path, path.parent)
    changed = _merge(draft["manifest"], patch)
    if not changed:
        _error("review patch must supply at least one mutable field")
    draft["review_log"].append({"reviewer": reviewer, "reviewed_at": _now(), "parent_sha256": sha,
                                 "fields": sorted(changed)})
    draft.update(revision=draft["revision"] + 1, parent_sha256=sha)
    _validate(draft, path.parent)
    raw = canonical(draft)
    if len(raw) > MAX_DRAFT_BYTES:
        _error("reviewed draft exceeds size limit")
    _new_file(destination, raw)
    return _report(draft, digest(raw))


def promote_draft(draft_path: Path | str, output_path: Path | str, *, reviewer: str,
                  expected_draft_sha256: str, reviewed: bool = False) -> dict:
    if reviewed is not True:
        _error("explicit reviewed acknowledgement required")
    text(reviewer, "reviewer", 200)
    path, draft, sha = _read_bound(draft_path, expected_draft_sha256)
    destination = _destination(output_path, path.parent)
    report = _report(draft, sha)
    if report["missing_count"]:
        _error("missing required fields; run draft-report before promotion")
    # All existing policy, image, fonts and evidence gates remain authoritative.
    job = validate_manifest(deepcopy(draft["manifest"]), path.parent)
    manifest_raw = canonical(job.manifest)
    receipt = {"schema_version": 1, "draft_sha256": sha, "manifest_sha256": digest(manifest_raw),
               "reviewer": reviewer, "reviewed_at": _now(), "fingerprint": job.fingerprint,
               "source_sha256": [s["sha256"] for s in draft["sources"]],
               "operator_assertions_only": True, "publish_ready": False}
    receipt_path = _destination(destination.with_name(destination.name + ".promotion.json"), path.parent)
    # The receipt is written first. An interrupted run cannot leave an apparently
    # successful manifest without its review binding. Neither file is overwritten.
    _new_file(receipt_path, canonical(receipt))
    _new_file(destination, manifest_raw)
    return {**receipt, "manifest_written": True}


def register_cli(commands: argparse._SubParsersAction) -> None:
    init = commands.add_parser("draft-init", help="Start an offline unverified product draft")
    init.add_argument("product_url")
    init.add_argument("--program", required=True, choices=sorted(PROGRAMS))
    init.add_argument("--output", required=True, type=Path)
    init.add_argument("--source", type=Path)
    init.add_argument("--source-format", choices=FORMATS, default="auto")
    report = commands.add_parser("draft-report", help="Show missing fields and unverified extraction candidates")
    report.add_argument("draft", type=Path)
    for name in ("draft-ingest", "draft-review", "draft-promote"):
        command = commands.add_parser(name)
        command.add_argument("draft", type=Path)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--expected-draft-sha256", required=True)
        if name == "draft-ingest":
            command.add_argument("--source", required=True, type=Path)
            command.add_argument("--source-format", choices=FORMATS, default="auto")
        else:
            command.add_argument("--reviewer", required=True)
            command.add_argument("--reviewed", action="store_true", help="I checked the supplied fields and supporting evidence")
        if name == "draft-review":
            command.add_argument("--patch", required=True, type=Path)


def handle_cli(args: argparse.Namespace) -> dict:
    if args.command == "draft-init":
        return init_draft(args.product_url, args.program, args.output, source_path=args.source,
                          source_format=args.source_format)
    if args.command == "draft-report":
        return draft_report(args.draft)
    if args.command == "draft-ingest":
        return ingest_source(args.draft, args.source, args.output, expected_draft_sha256=args.expected_draft_sha256,
                             source_format=args.source_format)
    if args.command == "draft-review":
        patch = parse_json(safe_read(clean_path(args.patch, "review patch"), "review patch", 256 * 1024))
        return review_draft(args.draft, patch, args.output, reviewer=args.reviewer,
                            expected_draft_sha256=args.expected_draft_sha256, reviewed=args.reviewed)
    if args.command == "draft-promote":
        return promote_draft(args.draft, args.output, reviewer=args.reviewer,
                             expected_draft_sha256=args.expected_draft_sha256, reviewed=args.reviewed)
    _error("unknown draft command")
