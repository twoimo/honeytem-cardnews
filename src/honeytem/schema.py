"""Small strict input contract; rejecting uncertainty is preferable to inventing it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import ipaddress
import json
import os
import stat
from pathlib import Path
import platform
import re
import unicodedata
from urllib.parse import unquote, urlsplit

import PIL
from PIL import Image, features
import fontTools

PROGRAMS = {"toss_sharelink", "coupang_partners", "kakao_gift_curator"}
RULES_REVISION = "50f78a860d096f845a33aa94968692a4634abe19"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ASSET_SIZE = (800, 560)


class ValidationError(ValueError):
    """Safe, field-only diagnostic (never echo input values or credentials)."""


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def canonical(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def fail(field: str, reason: str) -> None:
    raise ValidationError(f"{field}: {reason}")


def exact(obj: object, keys: set[str], field: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != keys:
        fail(field, "missing or unknown fields")
    return obj


def text(value: object, field: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        fail(field, "requires bounded, nonempty text without surrounding whitespace")
    if any(unicodedata.category(c).startswith("C") for c in value):
        fail(field, "control/format characters forbidden")
    if value.casefold() in {"unknown", "todo", "tbd", "확인 필요", "미확인", "확인필요", "n/a"}:
        fail(field, "unverified placeholder forbidden")
    return value


def concept_text(value: str, field: str) -> None:
    # Conservative syntax guard; this does not prove arbitrary copy semantics.
    if re.search(r"https?://|www\.|(?:(?:KRW|USD|EUR)\s*\d)|(?:[₩$€¥]\s*\d)|(?:\d[\d,.]*\s*(?:원|달러|KRW|USD|EUR))", value, re.I):
        fail(field, "concept copy cannot contain price or purchase-link tokens")


def integer(value: object, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        fail(field, "integer outside permitted range")
    return value


def boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        fail(field, "requires boolean")
    return value


def confirmed(value: object, field: str) -> None:
    if value is not True:
        fail(field, "explicit confirmation required")


def timestamp(value: object, field: str) -> datetime:
    text(value, field, 40)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        fail(field, "requires ISO-8601 timezone-aware timestamp")
    if date > datetime.now(timezone.utc):
        fail(field, "future evidence is not valid")
    return date


def url(value: object, field: str) -> str:
    """Validate without ever normalizing or reserializing the original string."""
    text(value, field, 4096)
    if any(c.isspace() for c in value) or "\\" in value:
        fail(field, "unsafe URL characters")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError
        if host.endswith(".") or "." not in host or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in host.split(".")):
            raise ValueError
        if host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".example.com", ".example.org", ".example.net")) or host in {"localhost", "example.com", "example.org", "example.net"}:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if re.search(r"%(?![0-9a-fA-F]{2})", value) or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I):
            raise ValueError
        for pair in (parsed.query + "&" + parsed.fragment).split("&"):
            key = unquote(pair.split("=", 1)[0]).casefold()
            if key in {"password", "passwd", "access_token", "refresh_token", "api_key", "secret", "authorization", "session", "sessionid"}:
                raise ValueError
    except ValueError:
        fail(field, "requires public HTTPS URL without credentials or secret parameters")
    return value


def clean_path(path: Path, field: str) -> Path:
    """No symlink component, even if the ultimate target lies inside the root."""
    absolute = Path(path.absolute())
    if ".." in absolute.parts:
        fail(field, "parent traversal forbidden")
    for part in [absolute, *absolute.parents]:
        if part.is_symlink():
            fail(field, "symlink forbidden")
    return absolute


def safe_read(path: Path, field: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Open each component relative to a held descriptor; never follow symlinks.

    This protects input reads from pathname/symlink swaps. It is not a general
    hostile multi-user sandbox: a privileged process can still mutate file data.
    """
    path = clean_path(path, field)
    directory = None
    file_fd = None
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            fail(field, "missing, non-regular, or oversized file")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            fail(field, "oversized file")
        return data
    except OSError:
        fail(field, "cannot safely open regular file")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def read_local(base: Path, value: object, field: str, *, font: bool = False) -> tuple[Path, bytes]:
    name = text(value, field, 500)
    candidate = Path(name)
    if ".." in candidate.parts or (candidate.is_absolute() and not font) or "\\" in name:
        fail(field, "requires safe relative path (fonts may be explicit absolute paths)")
    path = clean_path(candidate if candidate.is_absolute() else base / candidate, field)
    if not font and not path.is_relative_to(base):
        fail(field, "path escapes input directory")
    return path, safe_read(path, field)


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            fail("manifest", "duplicate JSON key")
        result[key] = value
    return result


def parse_json(raw: bytes) -> dict:
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: fail("manifest", "non-finite JSON number"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        fail("manifest", "invalid JSON")


@dataclass(frozen=True)
class Job:
    manifest: dict
    assets: dict[str, bytes]
    fonts: tuple[bytes, bytes]
    input_hashes: dict
    fingerprint: str


def validate_manifest(data: dict, base_dir: Path | str) -> Job:
    base = clean_path(Path(base_dir), "input directory")
    exact(data, {"schema_version", "mode", "fixture", "edition", "title", "subtitle", "variant", "checked_at", "fonts", "channel", "disclosures", "products"}, "manifest")
    integer(data["schema_version"], "schema_version", 1, 1)
    if data["mode"] not in ("concept", "affiliate"):
        fail("mode", "unsupported mode")
    boolean(data["fixture"], "fixture")
    affiliate = data["mode"] == "affiliate"
    if affiliate and data["fixture"]:
        fail("fixture", "synthetic fixtures cannot be sales content")
    for key in ("edition", "title", "subtitle"):
        text(data[key], key, 120)
        if not affiliate:
            concept_text(data[key], key)
    integer(data["variant"], "variant", 0, 9)
    checked = timestamp(data["checked_at"], "checked_at")
    exact(data["fonts"], {"regular", "bold"}, "fonts")
    fonts = []
    hashes = {"assets": {}, "fonts": {}, "rules_revision": RULES_REVISION}
    for key in ("regular", "bold"):
        spec = exact(data["fonts"][key], {"path", "index"}, f"fonts.{key}")
        integer(spec["index"], f"fonts.{key}.index", 0, 32)
        _, content = read_local(base, spec["path"], f"fonts.{key}.path", font=True)
        fonts.append(content)
        hashes["fonts"][key] = digest(content)
    channel = exact(data["channel"], {"name", "status", "evidence_url", "profile_links_confirmed"}, "channel")
    if channel["name"] != "instagram":
        fail("channel.name", "only Instagram draft packages supported")
    boolean(channel["profile_links_confirmed"], "channel.profile_links_confirmed")
    if affiliate:
        if channel["status"] != "confirmed":
            fail("channel.status", "channel eligibility evidence required")
        url(channel["evidence_url"], "channel.evidence_url")
    elif channel != {"name": "instagram", "status": "not_applicable", "evidence_url": None, "profile_links_confirmed": False}:
        fail("channel", "concept mode has no sales channel claim")
    products = data["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 5:
        fail("products", "requires 1 to 5 products")
    assets = {}
    used_programs = set()
    product_keys = {"id", "title", "comment", "program", "seller", "option", "quantity", "price", "original_url", "affiliate_url", "evidence", "asset"}
    for i, product in enumerate(products, 1):
        field = f"products[{i}]"
        exact(product, product_keys, field)
        if product["id"] != f"{i:02d}":
            fail(field + ".id", "IDs must be unique sequential two-digit strings")
        for key in ("title", "comment"):
            text(product[key], field + "." + key, 240)
            if not affiliate:
                concept_text(product[key], field + "." + key)
        if affiliate:
            if not isinstance(product["program"], str) or product["program"] not in PROGRAMS:
                fail(field + ".program", "unsupported affiliate program")
            used_programs.add(product["program"])
            for key in ("seller", "option"):
                text(product[key], field + "." + key, 100)
            integer(product["quantity"], field + ".quantity", 1, 1000)
            price = exact(product["price"], {"amount", "currency", "conditions", "shipping"}, field + ".price")
            integer(price["amount"], field + ".price.amount", 0, 1_000_000_000)
            if price["currency"] != "KRW":
                fail(field + ".price.currency", "only verified KRW integer prices supported")
            for key in ("conditions", "shipping"):
                text(price[key], field + ".price." + key, 200)
            for key in ("original_url", "affiliate_url"):
                url(product[key], field + "." + key)
            evidence = exact(product["evidence"], {"url", "checked_at", "claims_verified", "link_ownership_confirmed"}, field + ".evidence")
            url(evidence["url"], field + ".evidence.url")
            if timestamp(evidence["checked_at"], field + ".evidence.checked_at") > checked:
                fail(field + ".evidence.checked_at", "evidence later than manifest review")
            confirmed(evidence["claims_verified"], field + ".evidence.claims_verified")
            confirmed(evidence["link_ownership_confirmed"], field + ".evidence.link_ownership_confirmed")
        elif any(product[k] is not None for k in ("program", "seller", "option", "quantity", "price", "original_url", "affiliate_url", "evidence")):
            fail(field, "concept mode must not contain prices, sales links, or sales claims")
        asset = exact(product["asset"], {"path", "kind", "source", "license", "usage_confirmed", "layout_permission_confirmed"}, field + ".asset")
        if asset["kind"] != ("actual" if affiliate else "synthetic_fixture" if data["fixture"] else "ai_concept"):
            fail(field + ".asset.kind", "asset kind conflicts with mode")
        for key in ("source", "license"):
            text(asset[key], field + ".asset." + key, 1000)
        for key in ("usage_confirmed", "layout_permission_confirmed"):
            confirmed(asset[key], field + ".asset." + key)
        _, content = read_local(base, asset["path"], field + ".asset.path")
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG"} or image.mode != "RGB" or getattr(image, "n_frames", 1) != 1 or image.getexif().get(274, 1) != 1:
                    fail(field + ".asset", "requires single-frame unrotated RGB PNG/JPEG; no automatic conversions")
                if not (1 <= image.width <= MAX_ASSET_SIZE[0] and 1 <= image.height <= MAX_ASSET_SIZE[1]):
                    fail(field + ".asset", "native dimensions exceed 800x560; supply separately authorized correctly sized material")
                exif = image.getexif()
                color_spaces = [exif.get(40961)]
                if 34665 in exif:
                    color_spaces.append(exif.get_ifd(34665).get(40961))
                if any(key in image.info for key in ("icc_profile", "gamma", "chromaticity", "srgb")) or any(value is not None and value != 1 for value in color_spaces):
                    fail(field + ".asset", "color-tagged imagery is unsupported without a verified color-preserving pipeline")
                image.load()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail(field + ".asset", "invalid or unsafe image")
        assets[product["id"]] = content
        hashes["assets"][product["id"]] = digest(content)
    disclosures = data["disclosures"]
    if not isinstance(disclosures, list) or len(disclosures) != len(used_programs):
        fail("disclosures", "one latest verified disclosure per used program required")
    seen = set()
    for d in disclosures:
        exact(d, {"program", "text", "reference_url", "checked_at", "verified"}, "disclosures")
        if not isinstance(d["program"], str) or d["program"] not in used_programs or d["program"] in seen:
            fail("disclosures.program", "missing/duplicate/unused program")
        seen.add(d["program"])
        wording = text(d["text"], "disclosures.text", 500)
        program_token = {"toss_sharelink": "토스", "coupang_partners": "쿠팡", "kakao_gift_curator": "큐레이터"}[d["program"]]
        if (not wording.startswith("[광고] ") or "수수료" not in wording or program_token not in wording
                or not re.search(r"(?:지급|제공) ?받습니다", wording)
                or re.search(r"않|없|아닙|아니|가능|수도|미지급", wording)):
            fail("disclosures.text", "must clearly begin [광고] and state commission relationship")
        url(d["reference_url"], "disclosures.reference_url")
        if timestamp(d["checked_at"], "disclosures.checked_at") > checked:
            fail("disclosures.checked_at", "evidence later than manifest review")
        confirmed(d["verified"], "disclosures.verified")
    hashes["manifest"] = digest(canonical(data))
    hashes["code"] = {p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    hashes["runtime"] = {"python": platform.python_version(), "pillow": PIL.__version__, "fonttools": fontTools.__version__, "platform": platform.platform(), "freetype": features.version("freetype2"), "libjpeg": features.version("jpg"), "zlib": features.version("zlib")}
    return Job(data, assets, tuple(fonts), hashes, digest(canonical(hashes)))


def load_manifest(path: Path | str) -> Job:
    path = clean_path(Path(path), "manifest path")
    return validate_manifest(parse_json(safe_read(path, "manifest", MAX_MANIFEST_BYTES)), path.parent)
