"""Explicitly authorized, local, reproducible image preparation.

This is deliberately separate from schema validation and rendering: neither
path silently resizes, edits, strips profiles, or grants image rights. A rights
record is an operator assertion, not independent verification of a license.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
from io import BytesIO
import os
from pathlib import Path
import platform
import re
import stat
import warnings

import PIL
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError, features

from .schema import (MAX_ASSET_SIZE, MAX_FILE_BYTES, MAX_MANIFEST_BYTES, PROGRAMS,
                     ValidationError, canonical, clean_path, digest, exact,
                     parse_json, safe_read, text)

MAX_SOURCE_PIXELS = 40_000_000
MAX_SOURCE_EDGE = 32_768
MAX_ICC_BYTES = 4 * 1024 * 1024
OPERATIONS = frozenset({"copy", "resize", "color_convert", "exif_transpose",
                        "flatten_alpha", "strip_metadata", "encode_png"})
COMMANDS = frozenset({"prepare-image", "verify-image"})
_RIGHTS_KEYS = {"schema_version", "status", "operator", "source", "reference",
               "purpose", "program", "basis", "allowed_operations"}
_COLOR_TAGS = {"gamma", "chromaticity", "srgb"}


def _error(message: str) -> ValidationError:
    return ValidationError("image preparation: " + message)


def validate_rights(value: object) -> dict:
    """Validate an explicit, operation-scoped assertion, never infer rights."""
    rights = exact(value, _RIGHTS_KEYS, "image rights")
    if type(rights["schema_version"]) is not int or rights["schema_version"] != 1:
        raise _error("unsupported rights schema")
    if rights["status"] != "granted":
        raise _error("denied, unknown, or missing usage rights cannot be overridden")
    for key in ("operator", "source", "reference", "purpose"):
        text(rights[key], "image rights." + key, 1000)
    if rights["program"] is not None and (not isinstance(rights["program"], str)
                                         or rights["program"] not in PROGRAMS):
        raise _error("unsupported rights program")
    if rights["basis"] not in ("own_work", "separate_permission", "program_policy"):
        raise _error("rights basis must identify own work, separate permission, or program policy")
    allowed = rights["allowed_operations"]
    if (not isinstance(allowed, list) or not allowed or len(allowed) > len(OPERATIONS)
            or any(not isinstance(op, str) or op not in OPERATIONS for op in allowed)
            or len(set(allowed)) != len(allowed) or "copy" not in allowed):
        raise _error("rights require a unique explicit list of permitted operations including copy")
    # Canonical deep copy prevents later caller mutation of the saved assertion.
    return parse_json(canonical(rights))


def load_rights(path: Path | str) -> dict:
    return validate_rights(parse_json(safe_read(Path(path), "image rights", MAX_MANIFEST_BYTES)))


def _options(allow_transform: bool, allow_exif_transpose: bool, matte: str | None) -> dict:
    if type(allow_transform) is not bool or type(allow_exif_transpose) is not bool:
        raise _error("transformation options require explicit booleans")
    if matte is not None:
        if not isinstance(matte, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", matte):
            raise _error("matte must be an explicit six-digit sRGB hex color")
        matte = matte.upper()
    if (allow_exif_transpose or matte is not None) and not allow_transform:
        raise _error("EXIF transpose and matte require explicit transformation opt-in")
    return {"allow_transform": allow_transform,
            "allow_exif_transpose": allow_exif_transpose, "matte": matte}


def _authorize(operation: str, rights: dict, options: dict) -> None:
    if operation not in rights["allowed_operations"]:
        raise _error("required operation is not covered by the rights assertion: " + operation)
    if operation != "copy":
        if not options["allow_transform"]:
            raise _error("image needs transformations; explicit allow-transform is required")
        if rights["program"] == "toss_sharelink" and rights["basis"] == "program_policy":
            raise _error("Toss program-policy rights do not permit editing; separate rights are required")


def _target_size(size: tuple[int, int]) -> tuple[tuple[int, int], Fraction]:
    width, height = size
    scale = min(Fraction(1), Fraction(MAX_ASSET_SIZE[0], width),
                Fraction(MAX_ASSET_SIZE[1], height))
    if min(width * scale, height * scale) < 1:
        raise _error("aspect ratio is too extreme to fit without losing a dimension")
    # Rational half-up rounding is deterministic; each edge differs by <=0.5 px
    # from the common scale. No thumbnail(), crop(), or independent-axis scaling.
    def rounded(value: Fraction) -> int:
        value += Fraction(1, 2)
        return value.numerator // value.denominator
    return (rounded(width * scale), rounded(height * scale)), scale


def _pixels(image: Image.Image) -> str:
    # Mode and size are separately included, so this is an unambiguous pixel hash.
    return digest(image.tobytes())


def _output_descriptor(raw: bytes, filename: str) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            if (image.format not in {"PNG", "JPEG"} or image.mode != "RGB"
                    or getattr(image, "n_frames", 1) != 1
                    or image.getexif().get(274, 1) != 1
                    or not 1 <= image.width <= MAX_ASSET_SIZE[0]
                    or not 1 <= image.height <= MAX_ASSET_SIZE[1]
                    or any(key in image.info for key in _COLOR_TAGS | {"icc_profile"})):
                raise _error("prepared image violates the existing RGB/no-profile asset contract")
            exif = image.getexif()
            colors = [exif.get(40961)]
            if 34665 in exif:
                colors.append(exif.get_ifd(34665).get(40961))
            if any(color is not None and color != 1 for color in colors):
                raise _error("prepared image has an unsupported EXIF color space")
            image.load()
            return {"path": filename, "sha256": digest(raw), "bytes": len(raw),
                    "format": image.format, "mode": image.mode, "size": list(image.size),
                    "pixel_sha256": _pixels(image)}


def _prepare_bytes(raw: bytes, rights: dict, options: dict) -> tuple[bytes, dict]:
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise _error("source is empty or exceeds the 32 MiB file limit")
    _authorize("copy", rights, options)
    operations = []
    def operation(name: str, **details) -> None:
        _authorize(name, rights, options)
        operations.append({"operation": name, **details})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if (source.format not in {"PNG", "JPEG", "TIFF"}
                        or getattr(source, "n_frames", 1) != 1
                        or getattr(source, "is_animated", False)):
                    raise _error("source must be a single-frame PNG, JPEG, or TIFF")
                if (source.width * source.height > MAX_SOURCE_PIXELS
                        or max(source.size) > MAX_SOURCE_EDGE or min(source.size) < 1):
                    raise _error("source dimensions exceed safe decode limits")
                if source.mode not in {"RGB", "RGBA", "L", "LA", "CMYK", "LAB"}:
                    raise _error("unsupported source pixel mode")
                orientation = source.getexif().get(274, 1)
                if type(orientation) is not int or not 1 <= orientation <= 8:
                    raise _error("invalid EXIF orientation")
                # Pillow's TIFF decoder applies orientation during load, unlike
                # PNG/JPEG. Reject this ambiguous path rather than claim an
                # explicit, independently controlled transpose was performed.
                if source.format == "TIFF" and orientation != 1:
                    raise _error("oriented TIFF is unsupported; use an authorized PNG/JPEG source")
                icc = source.info.get("icc_profile")
                if "icc_profile" in source.info and (not isinstance(icc, bytes)
                                                       or not 128 <= len(icc) <= MAX_ICC_BYTES):
                    raise _error("invalid or oversized embedded ICC profile")
                if icc is not None and (int.from_bytes(icc[:4], "big") != len(icc)
                                        or icc[36:40] != b"acsp"):
                    raise _error("malformed ICC profile header")
                # Do not silently discard gamma/chromaticity or guess precedence.
                if any(key in source.info for key in _COLOR_TAGS):
                    raise _error("non-ICC or conflicting color tags are unsupported")
                exif = source.getexif()
                colors = [exif.get(40961)]
                if 34665 in exif:
                    colors.append(exif.get_ifd(34665).get(40961))
                if icc is None and any(color is not None and color != 1 for color in colors):
                    raise _error("non-sRGB EXIF color space requires a matching ICC profile")
                if source.mode in {"CMYK", "LAB"} and icc is None:
                    raise _error("CMYK and LAB inputs require a matching ICC profile")
                if orientation != 1 and not options["allow_exif_transpose"]:
                    raise _error("nonidentity EXIF orientation requires explicit allow-exif-transpose")
                source.load()
                source_record = {"path": "original.bin", "sha256": digest(raw), "bytes": len(raw),
                                 "format": source.format, "mode": source.mode, "size": list(source.size),
                                 "exif_orientation": orientation, "pixel_sha256": _pixels(source),
                                 "has_alpha_or_transparency": "A" in source.getbands() or "transparency" in source.info,
                                 "icc_sha256": digest(icc) if icc is not None else None}
                image = source.copy()
                if orientation != 1:
                    operation("exif_transpose", orientation=orientation)
                    image = ImageOps.exif_transpose(image)
                oriented_size = image.size
                target, scale = _target_size(oriented_size)
                alpha = image.getchannel("A") if image.mode in {"RGBA", "LA"} else None
                if alpha is None and "transparency" in image.info:
                    # PNG tRNS can carry transparency while Pillow reports RGB/L.
                    # Treat it exactly like an alpha channel, not as a safe copy.
                    alpha = image.convert("RGBA").getchannel("A")
                if alpha is not None and options["matte"] is None:
                    raise _error("alpha input requires an explicit matte, even when currently opaque")
                if icc is not None:
                    operation("color_convert", method="littlecms_icc_to_srgb",
                              source_icc_sha256=digest(icc), intent="relative_colorimetric", flags=0)
                    profile = ImageCms.ImageCmsProfile(BytesIO(icc))
                    expected_space = {"RGB": "RGB", "RGBA": "RGB", "L": "GRAY", "LA": "GRAY",
                                      "CMYK": "CMYK", "LAB": "LAB"}[image.mode]
                    if profile.profile.xcolor_space.strip().upper() != expected_space:
                        raise _error("ICC color space does not match the source pixel mode")
                    # Remove alpha only from the CMS input; preserve it separately.
                    cms_input = image.convert({"RGBA": "RGB", "LA": "L"}[image.mode]) if image.mode in {"RGBA", "LA"} else image
                    image = ImageCms.profileToProfile(
                        cms_input, profile, ImageCms.createProfile("sRGB"), outputMode="RGB",
                        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, flags=0)
                elif image.mode in {"L", "LA"}:
                    operation("color_convert", method="untagged_gray_channel_replication")
                    image = image.convert("RGB")
                elif image.mode == "RGBA":
                    image = image.convert("RGB")
                if alpha is not None:
                    operation("flatten_alpha", matte=options["matte"], working_space="sRGB",
                              method="pillow_integer_alpha_composite")
                    foreground = image.convert("RGBA")
                    foreground.putalpha(alpha)
                    background = Image.new("RGBA", image.size, options["matte"])
                    image = Image.alpha_composite(background, foreground).convert("RGB")
                if image.size != target:
                    operation("resize", from_size=list(image.size), to_size=list(target),
                              filter="LANCZOS", crop=False, upscale=False)
                    image = image.resize(target, resample=Image.Resampling.LANCZOS)
                # TIFF always needs an explicit container conversion, including a
                # small RGB TIFF. PNG/JPEG that already comply are exact byte copies.
                transformed = bool(operations) or source.format == "TIFF"
                if transformed:
                    operation("strip_metadata", after_color_conversion=True)
                    operation("encode_png", mode="RGB", compress_level=6, optimize=False)
                    # A fresh pixel-only image cannot leak the input ICC/EXIF tags.
                    clean = Image.frombytes("RGB", image.size, image.tobytes())
                    buffer = BytesIO()
                    clean.save(buffer, format="PNG", optimize=False, compress_level=6)
                    output, filename = buffer.getvalue(), "prepared.png"
                else:
                    operation("copy", byte_exact=True)
                    output = raw
                    filename = "prepared.jpg" if source.format == "JPEG" else "prepared.png"
                output_record = _output_descriptor(output, filename)
                if output_record["size"] != list(target) or output_record["pixel_sha256"] != _pixels(image):
                    raise _error("output pixel or geometry verification failed")
                manifest = {
                    "schema_version": 1, "kind": "honeytem_prepared_image",
                    "source": source_record, "output": output_record,
                    "rights": rights, "options": options, "operations": operations,
                    "geometry": {"oriented_size": list(oriented_size), "output_size": list(target),
                                 "scale": [scale.numerator, scale.denominator],
                                 "rounding": "half_up_to_nearest_pixel", "crop": False, "upscale": False},
                    "runtime": {"python": platform.python_version(), "pillow": PIL.__version__,
                                "littlecms": ImageCms.core.littlecms_version,
                                "zlib": features.version("zlib"), "jpeg": features.version("jpg"),
                                "libtiff": features.version("libtiff")},
                    "source_unchanged": True, "rights_independently_verified": False,
                    "publish_ready": False,
                }
                return output, manifest
    except ValidationError:
        raise
    except (OSError, ValueError, TypeError, SyntaxError, OverflowError, ImageCms.PyCMSError,
            UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise _error("source image or ICC profile is invalid, unsupported, or unsafe") from exc


@contextmanager
def _directory_fd(path: Path, field: str):
    """Hold every path lookup to a directory descriptor and reject symlinks."""
    path = clean_path(path, field)
    descriptor = None
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        raise _error("cannot safely access directory") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_new(directory: int, name: str, content: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_member(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise _error("artifact member is not a bounded regular file")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise _error("artifact member exceeds its size limit")
        return content


def prepare_image(source: Path | str, output_dir: Path | str, *, rights: dict,
                  allow_transform: bool = False, allow_exif_transpose: bool = False,
                  matte: str | None = None) -> dict:
    """Create a new package with an untouched source, prepared asset, and audit.

    Rights plus allow_transform are both necessary for any alteration. No-op
    copies do not require allow_transform. Never modifies or overwrites inputs.
    """
    rights = validate_rights(rights)
    options = _options(allow_transform, allow_exif_transpose, matte)
    source_path = clean_path(Path(source), "source image")
    destination = clean_path(Path(output_dir), "prepared output")
    if destination.exists():
        raise _error("output must be a new directory; overwrite is forbidden")
    raw = safe_read(source_path, "source image")
    output, manifest = _prepare_bytes(raw, rights, options)
    # An input changed by another process is not silently blessed as unchanged.
    if safe_read(source_path, "source image") != raw:
        raise _error("source changed during preparation")
    try:
        with _directory_fd(destination.parent, "output parent") as parent:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
            directory = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                _write_new(directory, "original.bin", raw)
                _write_new(directory, manifest["output"]["path"], output)
                _write_new(directory, "manifest.json", canonical(manifest))
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise _error("cannot safely create new output; existing or partial output is never overwritten") from exc
    return verify_prepared_image(destination)


def verify_prepared_image(output_dir: Path | str, *, expected_manifest_sha256: str | None = None) -> dict:
    """Recompute from the retained original, not just trust stored output hashes.

    A trusted manifest digest detects coordinated replacement. Without one,
    this checks consistency, not identity, provenance truth, or legal rights.
    """
    path = clean_path(Path(output_dir), "prepared image directory")
    with _directory_fd(path, "prepared image directory") as directory:
        manifest_raw = _read_member(directory, "manifest.json", MAX_MANIFEST_BYTES)
        if expected_manifest_sha256 is not None:
            if (not isinstance(expected_manifest_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
                    or digest(manifest_raw) != expected_manifest_sha256):
                raise _error("manifest does not match trusted expected SHA256")
        manifest = parse_json(manifest_raw)
        exact(manifest, {"schema_version", "kind", "source", "output", "rights", "options",
                         "operations", "geometry", "runtime", "source_unchanged",
                         "rights_independently_verified", "publish_ready"}, "image manifest")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["kind"] != "honeytem_prepared_image":
            raise _error("unsupported preparation manifest")
        rights = validate_rights(manifest["rights"])
        options = exact(manifest["options"], {"allow_transform", "allow_exif_transpose", "matte"}, "image options")
        options = _options(**options)
        raw = _read_member(directory, "original.bin", MAX_FILE_BYTES)
        expected_output, expected_manifest = _prepare_bytes(raw, rights, options)
        filename = expected_manifest["output"]["path"]
        if set(os.listdir(directory)) != {"original.bin", filename, "manifest.json"}:
            raise _error("unexpected or missing preparation files")
        output = _read_member(directory, filename, MAX_FILE_BYTES)
        if output != expected_output or manifest_raw != canonical(expected_manifest):
            raise _error("preparation manifest, original, output pixels, or runtime failed reproduction")
    return {"verified": True, "manifest_sha256": digest(manifest_raw),
            "source_sha256": digest(raw), "output_sha256": digest(output),
            "output_file": filename, "size": expected_manifest["output"]["size"],
            "byte_exact_copy": output == raw, "publish_ready": False}


def register_cli(commands) -> None:
    """Register optional commands without changing existing schema/render paths."""
    prepare = commands.add_parser("prepare-image", help="explicitly authorized local image preparation")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("--rights", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--allow-transform", action="store_true")
    prepare.add_argument("--allow-exif-transpose", action="store_true")
    prepare.add_argument("--matte")
    verify = commands.add_parser("verify-image", help="reproduce and verify prepared image provenance")
    verify.add_argument("prepared_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256")


def handle_cli(args) -> dict:
    if args.command == "prepare-image":
        return prepare_image(args.source, args.output, rights=load_rights(args.rights),
                             allow_transform=args.allow_transform,
                             allow_exif_transpose=args.allow_exif_transpose, matte=args.matte)
    if args.command == "verify-image":
        return verify_prepared_image(args.prepared_dir, expected_manifest_sha256=args.expected_manifest_sha256)
    raise _error("unsupported image preparation command")
