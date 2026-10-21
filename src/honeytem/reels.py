"""Optional, local-only, silent reel exports from verified Honeytem packages.

Cards remain at native resolution on a 9:16 canvas. Only H.264 compression is
lossy; the retained padded PNGs preserve every decoded source-card RGB pixel.
No publishing, music, remote inputs, or package-approval transfer is performed.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from io import BytesIO
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import zipfile

import PIL
from PIL import Image, UnidentifiedImageError

from .pipeline import atomic_write, locked, verify
from .schema import ValidationError, canonical, clean_path, digest, parse_json, safe_read

MAX_SECONDS_PER_CARD = 60
MAX_TOTAL_SECONDS = 180
MAX_VIDEO_BYTES = 256 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 600
PADDING_COLORS = ("#FFFFFF", "#F5F4F0", "#ECEDEC", "#000000")
_ENCODING = {"codec": "libx264", "preset": "medium", "crf": 18, "pixel_format": "yuv420p",
             "encoder_threads": 1, "filter_threads": 1, "bitexact": True, "faststart": True, "audio": False}
_SHA = re.compile(r"[0-9a-f]{64}")
_LIMITATIONS = [
    "Review draft only: source package approval does not approve this video.",
    "Native-size PNG padding is pixel-exact; H.264/yuv420p encoding is lossy.",
    "Integrity and timing checks do not prove visual quality or product truth.",
    "No network request, music sourcing, account action, or publication occurred.",
    "Byte repeatability requires the same inputs, code, Pillow and FFmpeg builds.",
]


def _error(message: str) -> ValidationError:
    return ValidationError("reel: " + message)


def _duration(value: object, fps: int) -> tuple[str, Fraction]:
    if type(value) not in (str, int, float) or len(str(value)) > 64:
        raise _error("duration must be bounded, finite decimal seconds")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > MAX_SECONDS_PER_CARD:
            raise _error("duration must be positive, finite, and at most 60 seconds")
        # Reject extreme negative exponents before allocating a rational denominator.
        if number < Decimal("0.01"):
            raise _error("each duration must be at least one frame")
        fraction = Fraction(number)
        if fraction * fps < 1:
            raise _error("each duration must be at least one frame")
    except (InvalidOperation, ValueError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("duration must be bounded, finite decimal seconds") from exc
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized, fraction


def frame_plan(durations: list | tuple, fps: int = 30) -> dict:
    """Round cumulative rational frame positions half up, without float drift.

    For S_k = fps * sum(d_1..d_k), B_k = floor(S_k + 1/2), B_0 = 0,
    interval k is [B_(k-1), B_k). Thus counts telescope to B_n and cumulative
    timing error is at most 1/(2*fps), regardless of the number of cards.
    """
    if type(fps) is not int or not 1 <= fps <= 60:
        raise _error("fps must be an integer from 1 to 60")
    if not isinstance(durations, (list, tuple)) or not 2 <= len(durations) <= 6:
        raise _error("requires cover and one to five product durations")
    values = [_duration(value, fps) for value in durations]
    total = sum((value for _, value in values), Fraction())
    if total > MAX_TOTAL_SECONDS:
        raise _error("total requested duration must not exceed 180 seconds")
    cumulative = Fraction()
    previous = 0
    intervals = []
    for _, duration in values:
        cumulative += duration * fps
        rounded = cumulative + Fraction(1, 2)
        boundary = rounded.numerator // rounded.denominator
        intervals.append({"start_frame": previous, "end_frame": boundary, "frame_count": boundary - previous})
        previous = boundary
    return {"fps": fps, "durations": [value for value, _ in values],
            "total_frames": previous, "intervals": intervals}


def _settings(width: int, height: int, padding_color: str, vertical_align: str) -> dict:
    if (type(width) is not int or type(height) is not int or not 1080 <= width <= 2160
            or not 1920 <= height <= 3840 or width % 2 or height % 2 or width * 16 != height * 9):
        raise _error("canvas must be even-pixel 9:16, from 1080x1920 to 2160x3840")
    if not isinstance(padding_color, str) or padding_color.upper() not in PADDING_COLORS:
        raise _error("padding color must use the existing white, offwhite, lightgray, or black palette")
    if vertical_align not in ("top", "center", "bottom"):
        raise _error("vertical alignment must be top, center, or bottom")
    return {"width": width, "height": height, "padding_color": padding_color.upper(), "vertical_align": vertical_align}


def _pad_card(raw: bytes, settings: dict) -> tuple[bytes, dict]:
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format != "PNG" or source.mode != "RGB" or source.size != (1080, 1350) or getattr(source, "n_frames", 1) != 1:
                raise _error("source card must be a single RGB 1080x1350 PNG")
            source.load()
            width, height = settings["width"], settings["height"]
            x = (width - source.width) // 2
            y = {"top": 0, "center": (height - source.height) // 2,
                 "bottom": height - source.height}[settings["vertical_align"]]
            bounds = [x, y, x + source.width, y + source.height]
            canvas = Image.new("RGB", (width, height), settings["padding_color"])
            canvas.paste(source, (x, y))  # No resampling, cropping, or colour adjustment.
            source_pixels = source.tobytes()
            if canvas.crop(bounds).tobytes() != source_pixels:
                raise _error("native pixel preservation check failed")
            buffer = BytesIO()
            canvas.save(buffer, format="PNG", optimize=False, compress_level=6)
            return buffer.getvalue(), {"source_size": list(source.size), "source_rgb_sha256": digest(source_pixels),
                                       "card_bounds": bounds}
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise _error("cannot decode source card") from exc


def _run(command: list[str], label: str) -> bytes:
    """No shell, no stderr disclosure, bounded time and combined pipe output."""
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        captured = bytearray()
        total_bytes = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error(label + " timed out")
                for key, _ in selector.select(timeout=min(remaining, 1)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total_bytes += len(block)
                    if total_bytes > 1024 * 1024:
                        raise _error(label + " returned oversized output")
                    if key.fileobj is process.stdout:
                        captured.extend(block)
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if returncode:
            raise _error(label + " failed with exit code " + str(returncode))
        return bytes(captured)
    except subprocess.TimeoutExpired as exc:
        raise _error(label + " timed out") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error(label + " executable unavailable or could not start") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


def _version(executable: str, label: str) -> str:
    try:
        first = _run([executable, "-version"], label).decode("utf-8").splitlines()[0]
    except (UnicodeError, IndexError) as exc:
        raise _error(label + " returned invalid version metadata") from exc
    if not first.startswith(label + " version ") or len(first) > 1000:
        raise _error(label + " returned invalid version metadata")
    return first


def _probe(path: Path, plan: dict, settings: dict, executable: str) -> dict:
    raw = _run([executable, "-v", "error", "-protocol_whitelist", "file", "-count_frames",
                "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_read_frames,nb_frames,duration,time_base:format=duration,format_name",
                "-of", "json", str(path)], "ffprobe")
    try:
        record = parse_json(raw)
        streams = record["streams"]
        if not isinstance(streams, list) or len(streams) != 1:
            raise _error("video must have exactly one stream and no audio")
        stream, container = streams[0], record["format"]
        count = int(stream["nb_read_frames"])
        expected_duration = Fraction(plan["total_frames"], plan["fps"])
        tick = Fraction(stream["time_base"])
        if (stream["codec_type"] != "video" or stream["codec_name"] != "h264"
                or stream["width"] != settings["width"] or stream["height"] != settings["height"]
                or stream["pix_fmt"] != "yuv420p" or count != plan["total_frames"]
                or int(stream["nb_frames"]) != count or Fraction(stream["r_frame_rate"]) != plan["fps"]
                or Fraction(stream["avg_frame_rate"]) != plan["fps"]
                or "mp4" not in container["format_name"].split(",")
                or tick != Fraction(1, plan["fps"] * 1000)
                or abs(Fraction(stream["duration"]) - expected_duration) > max(tick, Fraction(1, 1000000))
                or abs(Fraction(container["duration"]) - expected_duration) > Fraction(1, 1000)):
            raise _error("encoded dimensions, rate, frame count, or duration mismatch")
        return {"codec": "h264", "pixel_format": "yuv420p", "width": settings["width"], "height": settings["height"],
                "fps": plan["fps"], "frame_count": count, "duration_seconds": stream["duration"],
                "container_duration_seconds": container["duration"], "time_base": stream["time_base"], "audio_streams": 0}
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("ffprobe returned malformed or mismatched media metadata") from exc


def _encoder_arguments(directory: Path, timeline: list[dict], plan: dict) -> list[str]:
    command = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_complex_threads", "1"]
    for card in timeline:
        command.extend(["-protocol_whitelist", "file", "-threads", "1", "-loop", "1", "-framerate", str(plan["fps"]),
                        "-i", str(directory / card["frame_file"])])
    filters = [f"[{i}:v]trim=end_frame={card['frame_count']},settb=expr=1/{plan['fps']},setpts=N+{card['start_frame']}[v{i}]" for i, card in enumerate(timeline)]
    filters.append("".join(f"[v{i}]" for i in range(len(timeline))) + f"interleave=nb_inputs={len(timeline)}:duration=longest[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-an", "-map_metadata", "-1",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "1",
                    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-r", str(plan["fps"]), "-fps_mode", "cfr",
                    "-frames:v", str(plan["total_frames"]), "-video_track_timescale", str(plan["fps"] * 1000),
                    "-movflags", "+faststart", "-fs", str(MAX_VIDEO_BYTES - 1024 * 1024), str(directory / "reel.mp4")])
    return command


def _encode(directory: Path, timeline: list[dict], plan: dict, executable: str) -> None:
    _run([executable, *_encoder_arguments(directory, timeline, plan)], "ffmpeg")


def export_reel(run_dir: Path | str, output_dir: Path | str, *, durations: object = 3,
                fps: int = 30, padding_color: str = "#FFFFFF", vertical_align: str = "center",
                width: int = 1080, height: int = 1920, allow_affiliate_transcoding: bool = False, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    """Create a new export directory; fail closed without overwriting prior work.

    ``durations`` is one decimal duration for all cards or an ordered sequence
    (cover first, then product-01, ...). A verified, completed card package is
    required. Input snapshots and deterministic output stay separate.
    """
    source = clean_path(Path(run_dir), "reel source")
    output = clean_path(Path(output_dir), "reel output")
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise _error("output must be separate from the source run")
    if output.exists():
        raise _error("output must be a new directory")
    settings = _settings(width, height, padding_color, vertical_align)
    if type(allow_affiliate_transcoding) is not bool:
        raise _error("affiliate transcoding permission must be an explicit boolean")
    checked = verify(source)
    package = safe_read(source / "package.zip", "reel source package", MAX_VIDEO_BYTES)
    if digest(package) != checked["package_sha256"]:
        raise _error("source package changed after verification")
    with zipfile.ZipFile(BytesIO(package)) as archive:
        package_manifest = parse_json(archive.read("package-manifest.json"))
        provenance = parse_json(archive.read("provenance.json"))
        if provenance.get("mode") not in ("affiliate", "concept"):
            raise _error("source provenance has an unsupported mode")
        if provenance["mode"] == "affiliate" and not allow_affiliate_transcoding:
            raise _error("affiliate video requires separate derivative/transcoding rights confirmation")
        names = ["cover.png"] + sorted(name for name in package_manifest["files"] if re.fullmatch(r"product-0[1-5]\.png", name))
        values = durations if isinstance(durations, (list, tuple)) else [durations] * len(names)
        if len(values) != len(names):
            raise _error("duration count must match the cover and product cards")
        plan = frame_plan(values, fps)
        originals = [(name, archive.read(name)) for name in names]
    runtime = {"ffmpeg": _version(ffmpeg, "ffmpeg"), "ffprobe": _version(ffprobe, "ffprobe"), "pillow": PIL.__version__,
               "implementation_sha256": digest(safe_read(Path(__file__), "reel implementation"))}
    temp = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with locked(output.parent):
            clean_path(output, "reel output")
            if output.exists():
                raise _error("output must be a new directory")
            temp = Path(tempfile.mkdtemp(prefix=".reel-", dir=output.parent))
            timeline = []
            for i, (name, raw) in enumerate(originals):
                padded, geometry = _pad_card(raw, settings)
                filename = f"frames/{i:02d}.png"
                atomic_write(temp / filename, padded)
                timeline.append({"source_file": name, "source_sha256": digest(raw), "frame_file": filename,
                                 "frame_sha256": digest(padded), **geometry, **plan["intervals"][i]})
            _encode(temp, timeline, plan, ffmpeg)
            media = _probe(temp / "reel.mp4", plan, settings, ffprobe)
            video_sha = digest(safe_read(temp / "reel.mp4", "reel video", MAX_VIDEO_BYTES))
            manifest = {"schema_version": 1, "source": {"fingerprint": checked["fingerprint"], "package_sha256": checked["package_sha256"],
                        "mode": provenance["mode"], "affiliate_transcoding_confirmed": allow_affiliate_transcoding},
                        "settings": settings, "encoding": _ENCODING, "encoder_arguments": _encoder_arguments(Path("{export_dir}"), timeline, plan), "plan": plan, "timeline": timeline, "media": media,
                        "video_sha256": video_sha, "runtime": runtime, "publish_ready": False, "limitations": _LIMITATIONS}
            manifest_bytes = canonical(manifest)
            atomic_write(temp / "reel-manifest.json", manifest_bytes)
            verify_reel(temp, ffprobe=ffprobe)
            os.rename(temp, output)
            temp = None
    except OSError as exc:
        raise _error("cannot safely create export artifacts") from exc
    finally:
        if temp is not None:
            shutil.rmtree(temp)
    return {"output_dir": str(output), "video": str(output / "reel.mp4"), "manifest": str(output / "reel-manifest.json"),
            "video_sha256": video_sha, "manifest_sha256": digest(manifest_bytes), "total_frames": plan["total_frames"],
            "integrity": "passed", "publish_ready": False}


def verify_reel(output_dir: Path | str, *, ffprobe: str = "ffprobe") -> dict:
    """Verify the exact export inventory, native PNG pixels, and decoded MP4.

    Hashes are integrity records, not signatures. Compare the returned manifest
    hash with a separately trusted hash to detect wholesale replacement.
    """
    root = clean_path(Path(output_dir), "reel export")
    raw = safe_read(root / "reel-manifest.json", "reel manifest", 256 * 1024)
    manifest = parse_json(raw)
    try:
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "source", "settings", "encoding", "encoder_arguments", "plan", "timeline", "media", "video_sha256", "runtime", "publish_ready", "limitations"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or manifest["publish_ready"] is not False or manifest["limitations"] != _LIMITATIONS or canonical(manifest["encoding"]) != canonical(_ENCODING)):
            raise _error("invalid manifest structure")
        source = manifest["source"]
        if (not isinstance(source, dict) or set(source) != {"fingerprint", "package_sha256", "mode", "affiliate_transcoding_confirmed"}
                or source["mode"] not in ("concept", "affiliate")
                or type(source["affiliate_transcoding_confirmed"]) is not bool
                or (source["mode"] == "affiliate" and not source["affiliate_transcoding_confirmed"])
                or any(not isinstance(source[key], str) or not _SHA.fullmatch(source[key]) for key in ("fingerprint", "package_sha256"))):
            raise _error("invalid source provenance")
        settings = _settings(**manifest["settings"])
        plan = frame_plan(manifest["plan"]["durations"], manifest["plan"]["fps"])
        if canonical(settings) != canonical(manifest["settings"]) or canonical(plan) != canonical(manifest["plan"]):
            raise _error("invalid padding or frame plan")
        timeline = manifest["timeline"]
        if not isinstance(timeline, list) or len(timeline) != len(plan["intervals"]):
            raise _error("invalid timeline length")
        expected_files = {"reel.mp4", "reel-manifest.json"}
        for i, card in enumerate(timeline):
            expected_name = "cover.png" if i == 0 else f"product-{i:02d}.png"
            expected_frame = f"frames/{i:02d}.png"
            if (not isinstance(card, dict) or set(card) != {"source_file", "source_sha256", "frame_file", "frame_sha256", "source_size", "source_rgb_sha256", "card_bounds", "start_frame", "end_frame", "frame_count"}
                    or card["source_file"] != expected_name or card["frame_file"] != expected_frame
                    or card["source_size"] != [1080, 1350]
                    or any(type(card[key]) is not int or card[key] != value for key, value in plan["intervals"][i].items())
                    or any(not isinstance(card[key], str) or not _SHA.fullmatch(card[key]) for key in ("source_sha256", "frame_sha256", "source_rgb_sha256"))):
                raise _error("invalid card provenance or interval")
            frame_bytes = safe_read(root / expected_frame, "padded reel frame")
            if digest(frame_bytes) != card["frame_sha256"]:
                raise _error("padded frame is missing or tampered")
            with Image.open(BytesIO(frame_bytes)) as frame:
                x = (settings["width"] - 1080) // 2
                y = {"top": 0, "center": (settings["height"] - 1350) // 2, "bottom": settings["height"] - 1350}[settings["vertical_align"]]
                bounds = [x, y, x + 1080, y + 1350]
                if (frame.format != "PNG" or frame.mode != "RGB" or frame.size != (settings["width"], settings["height"])
                        or getattr(frame, "n_frames", 1) != 1 or canonical(card["card_bounds"]) != canonical(bounds)
                        or digest(frame.crop(bounds).tobytes()) != card["source_rgb_sha256"]):
                    raise _error("native source pixels or padding geometry mismatch")
                padded = Image.new("RGB", frame.size, settings["padding_color"])
                padded.paste(frame.crop(bounds), (x, y))
                if padded.tobytes() != frame.tobytes():
                    raise _error("padding color mismatch")
            expected_files.add(expected_frame)
        if manifest["encoder_arguments"] != _encoder_arguments(Path("{export_dir}"), timeline, plan):
            raise _error("encoder arguments mismatch")
        actual_files = set()
        for path in root.rglob("*"):
            clean_path(path, "reel artifact")
            if path.is_file():
                actual_files.add(path.relative_to(root).as_posix())
            elif not path.is_dir() or path != root / "frames":
                raise _error("unexpected export entry")
        if actual_files != expected_files:
            raise _error("missing or unexpected export file")
        video_sha = digest(safe_read(root / "reel.mp4", "reel video", MAX_VIDEO_BYTES))
        if video_sha != manifest["video_sha256"]:
            raise _error("video digest mismatch")
        media = _probe(root / "reel.mp4", plan, settings, ffprobe)
        if canonical(media) != canonical(manifest["media"]):
            raise _error("video metadata mismatch")
        runtime = manifest["runtime"]
        if (not isinstance(runtime, dict) or set(runtime) != {"ffmpeg", "ffprobe", "pillow", "implementation_sha256"}
                or not isinstance(runtime["implementation_sha256"], str) or not _SHA.fullmatch(runtime["implementation_sha256"])
                or any(not isinstance(runtime[key], str) or not runtime[key] or len(runtime[key]) > 1000 for key in ("ffmpeg", "ffprobe", "pillow"))):
            raise _error("invalid runtime provenance")
    except (OSError, KeyError, TypeError, ValueError, IndexError, Image.DecompressionBombError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("missing or malformed export artifact") from exc
    return {"output_dir": str(root), "video_sha256": video_sha, "manifest_sha256": digest(raw),
            "total_frames": plan["total_frames"], "integrity": "passed", "publish_ready": False}
