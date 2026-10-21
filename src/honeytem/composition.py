"""Local-only, review-draft audio/subtitle composition of verified silent reels.

No TTS, network fetch, music discovery, account action, or publication is done.
Text is rasterized by Pillow, never interpolated into an FFmpeg expression.
"""
from __future__ import annotations

from array import array
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from io import BytesIO
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

from PIL import Image, ImageDraw, ImageFont

from .pipeline import atomic_write, locked
from .reels import MAX_VIDEO_BYTES, _run, _version, verify_reel
from .render import check_glyph_coverage, rectangles_intersect, _wrap
from .schema import ValidationError, canonical, clean_path, digest, parse_json, safe_read

SAMPLE_RATE = 48000
CHANNELS = 2
MAX_SECONDS = 180
MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_CAPTIONS = 24
MAX_RENDERED_PIXELS = 160_000_000
MAX_BUNDLE_BYTES = 768 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}")
POLICIES = ("fail", "pad", "truncate", "pad_or_truncate")
LIMITATIONS = [
    "Review draft only; neither source approval nor composition grants publication approval.",
    "Rights are explicit user declarations, not independently verified legal clearance.",
    "No TTS, speech generation, music download, network call, or publication occurred.",
    "Synthetic test tones are not human narration or synthesized speech.",
    "Subtitles occupy padding, not source-card pixels; platform UI safe areas still require review.",
    "PNG source-card pixels are preserved; H.264/AAC output is lossy.",
    "Hashes are integrity records, not signatures; pin a separately trusted manifest hash.",
    "Loudness is measured, not automatically normalized; content and listening quality need human review.",
]


def _error(message: str) -> ValidationError:
    return ValidationError("composition: " + message)


def _exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise _error(label + " has missing or unknown fields")
    return value


def _text(value: object, label: str, limit: int = 1000, multiline: bool = False) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith("C") and not (multiline and c == "\n") for c in value)):
        raise _error(label + " requires bounded plain text")
    return value


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _error(label + " requires an integer in range")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise _error(label + " requires a lowercase SHA256")
    return value


def _decimal(value: object, label: str, low: int, high: int) -> str:
    if type(value) not in (str, int, float) or len(str(value)) > 40:
        raise _error(label + " requires a bounded finite decimal")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not low <= number <= high or number.as_tuple().exponent < -12:
            raise _error(label + " requires a bounded finite decimal")
        return format(number.normalize(), "f")
    except InvalidOperation as exc:
        raise _error(label + " requires a bounded finite decimal") from exc


def _round(value: Fraction) -> int:
    value += Fraction(1, 2)
    return value.numerator // value.denominator


def caption_plan(captions: object, fps: int, total_frames: int) -> list[dict]:
    """Validate half-open frame boundaries; no floating-point time conversion."""
    _integer(fps, 1, 60, "fps")
    _integer(total_frames, 1, MAX_SECONDS * fps, "total frames")
    if not isinstance(captions, list) or not 1 <= len(captions) <= MAX_CAPTIONS:
        raise _error("captions require one to 24 entries")
    previous = 0
    result = []
    for entry in captions:
        row = _exact(entry, {"start_frame", "end_frame", "text"}, "caption")
        start = _integer(row["start_frame"], 0, total_frames - 1, "caption start")
        end = _integer(row["end_frame"], start + 1, total_frames, "caption end")
        text = _text(row["text"], "caption text", 160, True)
        if start < previous or text.count("\n") > 1 or any(c in text for c in "<>&{}\\") or any(not line.strip() for line in text.splitlines()):
            raise _error("captions overlap, are unsorted, or contain subtitle markup/empty lines")
        result.append({"start_frame": start, "end_frame": end, "text": text})
        previous = end
    return result


def _stamp(frame: int, fps: int, separator: str) -> str:
    millis = _round(Fraction(frame * 1000, fps))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, remainder = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{remainder:03}"


def subtitle_files(captions: list[dict], fps: int, total_frames: int) -> tuple[bytes, bytes]:
    rows = caption_plan(captions, fps, total_frames)
    srt, vtt = [], ["WEBVTT\n"]
    for index, row in enumerate(rows, 1):
        srt.append(f"{index}\n{_stamp(row['start_frame'], fps, ',')} --> {_stamp(row['end_frame'], fps, ',')}\n{row['text']}\n")
        vtt.append(f"{index}\n{_stamp(row['start_frame'], fps, '.')} --> {_stamp(row['end_frame'], fps, '.')}\n{row['text']}\n")
    return ("\n".join(srt) + "\n").encode(), ("\n".join(vtt) + "\n").encode()


def _local(base: Path, value: object, label: str) -> Path:
    name = _text(value, label, 500)
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise _error(label + " requires a local relative path")
    return clean_path(base / path, label)


def _validate_spec(spec: object, reel: dict) -> dict:
    spec = _exact(spec, {"schema_version", "fps", "script", "captions", "subtitles", "audio"}, "specification")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise _error("unsupported specification version")
    if type(spec["fps"]) is not int or spec["fps"] != reel["plan"]["fps"]:
        raise _error("specification fps must match the verified source reel")
    captions = caption_plan(spec["captions"], spec["fps"], reel["plan"]["total_frames"])
    script = _exact(spec["script"], {"text", "source"}, "script")
    _text(script["text"], "script text", 16000, True)
    _text(script["source"], "script source", 1000)
    # Captions may be excerpts, but must occur in script order. No invented copy.
    remaining = script["text"].replace("\n", " ")
    for row in captions:
        text = row["text"].replace("\n", " ")
        position = remaining.find(text)
        if position < 0:
            raise _error("captions must be ordered excerpts of the supplied script")
        remaining = remaining[position + len(text):]
    subtitles = _exact(spec["subtitles"], {"burn_in", "font", "font_size"}, "subtitles")
    if type(subtitles["burn_in"]) is not bool:
        raise _error("burn_in requires an explicit boolean")
    _integer(subtitles["font_size"], 32, 96, "subtitle font size")
    font = _exact(subtitles["font"], {"path", "index", "sha256"}, "font")
    _text(font["path"], "font path", 500)
    _integer(font["index"], 0, 32, "font face")
    _sha(font["sha256"], "font digest")
    audio = _exact(spec["audio"], {"narration", "music"}, "audio")
    if all(audio[key] is None for key in audio):
        raise _error("at least one supplied audio track is required; use reel for silent export")
    for key, track in audio.items():
        if track is None:
            continue
        _exact(track, {"path", "sha256", "source", "license", "rights_confirmed", "duration_policy", "gain_db"}, key)
        _text(track["path"], key + " path", 500)
        _sha(track["sha256"], key + " digest")
        _text(track["source"], key + " source", 1000)
        _text(track["license"], key + " license", 1000)
        if track["rights_confirmed"] is not True:
            raise _error("each audio track requires explicit usage and derivative/mixing rights confirmation")
        if not isinstance(track["duration_policy"], str) or track["duration_policy"] not in POLICIES:
            raise _error("audio duration_policy must explicitly select fail, pad, truncate, or pad_or_truncate")
        _decimal(track["gain_db"], key + " gain", -60, 12)
    return spec


def _audio_format(data: bytes) -> str:
    # Force a known media demuxer; never let a local playlist open other files.
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 255 and data[1] & 224 == 224):
        return "mp3"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "mov"
    raise _error("audio must be a local WAV, FLAC, Ogg, MP3, or M4A media file")


def _audio_probe(path: Path, demuxer: str, ffprobe: str) -> dict:
    raw = _run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", demuxer,
                "-show_entries", "stream=codec_type,codec_name,sample_rate,channels:format=duration", "-of", "json", str(path)], "ffprobe")
    try:
        value = parse_json(raw)
        streams = value["streams"]
        duration = Fraction(value["format"]["duration"])
        if len(streams) != 1 or streams[0]["codec_type"] != "audio" or not 0 < duration <= MAX_SECONDS:
            raise _error("input must have exactly one audio stream, no video, and duration at most 180 seconds")
        stream = streams[0]
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 2:
            raise _error("input audio must have one or two channels at 8–192 kHz")
        return {"codec": stream["codec_name"], "sample_rate": rate, "channels": channels, "duration_seconds": value["format"]["duration"], "demuxer": demuxer}
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("invalid audio media metadata") from exc


def _decode(path: Path, destination: Path, demuxer: str, ffmpeg: str, *, allow_aac_padding: bool = False) -> bytes:
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
          "-protocol_whitelist", "file", "-f", demuxer, "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
          "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", "-f", "f32le",
          "-t", str(MAX_SECONDS + 1), "-fs", str(MAX_AUDIO_BYTES - 4096), str(destination)], "ffmpeg")
    data = safe_read(destination, "decoded audio", MAX_AUDIO_BYTES)
    limit = (MAX_SECONDS * SAMPLE_RATE + (1024 if allow_aac_padding else 0)) * CHANNELS * 4
    if not data or len(data) % 8 or len(data) > limit:
        raise _error("decoded audio exceeds resource limits or is malformed")
    return data


def _pcm_metrics(data: bytes) -> dict:
    if not isinstance(data, bytes) or not data or len(data) % 8:
        raise _error("decoded audio contains no complete samples")
    values = array("f")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    peak, power, clipped = 0.0, 0.0, 0
    for value in values:
        if not math.isfinite(value):
            raise _error("decoded audio contains nonfinite samples")
        absolute = abs(value)
        peak = max(peak, absolute)
        power += value * value
        clipped += absolute >= 32767 / 32768
    if not values or len(values) % CHANNELS:
        raise _error("decoded audio contains no complete samples")
    return {"sample_frames": len(values) // CHANNELS, "sample_rate": SAMPLE_RATE, "channels": CHANNELS,
            "duration_seconds": format(Decimal(len(values) // CHANNELS) / SAMPLE_RATE, ".9f"),
            "sample_peak": round(peak, 9), "sample_peak_dbfs": round(20 * math.log10(peak), 6) if peak else None,
            "rms_dbfs": round(10 * math.log10(power / len(values)), 6) if power else None, "clipped_samples": clipped, "clipping_threshold": 32767 / 32768}


def _loudness(path: Path, ffmpeg: str, *, raw: bool = False) -> dict:
    arguments = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1", "-protocol_whitelist", "file"]
    if raw:
        arguments += ["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS)]
    arguments += ["-i", str(path), "-map", "0:a:0", "-af",
                  "ebur128=metadata=1:peak=true,ametadata=print:key=lavfi.r128.I:file=-,ametadata=print:key=lavfi.r128.true_peak:file=-",
                  "-f", "null", "-"]
    output = _run(arguments, "ffmpeg")
    integrated = re.findall(rb"lavfi\.r128\.I=(-?\d+(?:\.\d+)?)", output)
    peaks = re.findall(rb"lavfi\.r128\.true_peak=(-?\d+(?:\.\d+)?)", output)
    if not integrated or not peaks:
        raise _error("audio is too short for EBU R128 measurement or loudness analysis failed")
    loudness, peak = float(integrated[-1]), max(float(value) for value in peaks)
    if not math.isfinite(loudness) or not math.isfinite(peak) or peak < 0:
        raise _error("invalid loudness analysis")
    return {"integrated_lufs": loudness if loudness > -70 else None,
            "integrated_lufs_status": "measured" if loudness > -70 else "below_gate_or_insufficient_duration",
            "reported_integrated_lufs": loudness, "true_peak": peak,
            "true_peak_dbtp": round(20 * math.log10(peak), 6) if peak else None,
            "method": "FFmpeg ebur128 integrated loudness and oversampled true peak"}


def _duration_adjust(data: bytes, sample_frames: int, policy: str) -> tuple[bytes, dict]:
    _integer(sample_frames, 1, MAX_SECONDS * SAMPLE_RATE, "target sample frames")
    if not isinstance(data, bytes) or not data or len(data) % 8:
        raise _error("audio duration adjustment requires complete PCM frames")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise _error("invalid audio duration policy")
    current = len(data) // 8
    action = "unchanged"
    if current < sample_frames:
        if policy not in ("pad", "pad_or_truncate"):
            raise _error("audio is shorter than video; explicitly choose pad or pad_or_truncate")
        action = "padded_with_silence"
        data += bytes((sample_frames - current) * 8)
    elif current > sample_frames:
        if policy not in ("truncate", "pad_or_truncate"):
            raise _error("audio is longer than video; explicitly choose truncate or pad_or_truncate")
        action = "truncated"
        data = data[:sample_frames * 8]
    return data, {"input_sample_frames": current, "output_sample_frames": sample_frames, "action": action, "policy": policy}


def _render_segments(root: Path, reel: dict, spec: dict, font_bytes: bytes, *, write: bool) -> list[dict]:
    width, height = reel["settings"]["width"], reel["settings"]["height"]
    try:
        check_glyph_coverage("가힣" + "".join(row["text"] for row in spec["captions"]), font_bytes, spec["subtitles"]["font"]["index"])
        font = ImageFont.truetype(BytesIO(font_bytes), size=spec["subtitles"]["font_size"],
                                  index=spec["subtitles"]["font"]["index"], layout_engine=ImageFont.Layout.BASIC)
    except (ValueError, OSError) as exc:
        raise _error("supplied font must contain real Korean and all subtitle glyphs; no fallback") from exc
    boundaries = {0, reel["plan"]["total_frames"]}
    for row in [*reel["timeline"], *spec["captions"]]:
        boundaries.update((row["start_frame"], row["end_frame"]))
    ordered = sorted(boundaries)
    if (len(ordered) - 1) * width * height > MAX_RENDERED_PIXELS:
        raise _error("subtitle segment raster budget exceeded; reduce caption count or canvas size")
    result = []
    for i, (start, end) in enumerate(zip(ordered, ordered[1:])):
        card = next(row for row in reel["timeline"] if row["start_frame"] <= start < row["end_frame"])
        caption = next((row for row in spec["captions"] if row["start_frame"] <= start < row["end_frame"]), None)
        with Image.open(BytesIO(safe_read(root / "source" / card["frame_file"], "source frame"))) as source:
            frame = source.copy()
        box = None
        ink_bounds = []
        if caption is not None:
            margin_x, margin_top, margin_bottom = _round(Fraction(width * 80, 1080)), _round(Fraction(height * 96, 1920)), _round(Fraction(height * 144, 1920))
            card_box = card["card_bounds"]
            regions = [(margin_top, card_box[1] - 16), (card_box[3] + 16, height - margin_bottom)]
            top, bottom = max(regions, key=lambda region: region[1] - region[0])
            try:
                lines = _wrap(caption["text"], font, width - 2 * margin_x - 32, 2, "subtitle")
            except ValueError as exc:
                raise _error("subtitle exceeds two-line safe width") from exc
            metrics = [font.getbbox(line) for line in lines]
            line_height = spec["subtitles"]["font_size"] + 10
            needed = line_height * len(lines) + 24
            if needed > bottom - top:
                raise _error("subtitle does not fit padding safe area; change source alignment, canvas, or font size")
            y = top + (bottom - top - needed) // 2
            box = [margin_x, y, width - margin_x, y + needed]
            if rectangles_intersect(box, card_box):
                raise _error("subtitle overlaps source card")
            draw = ImageDraw.Draw(frame)
            if spec["subtitles"]["burn_in"]:
                draw.rounded_rectangle((box[0], box[1], box[2] - 1, box[3] - 1), radius=12, fill="#171917")
            for line, bounds in zip(lines, metrics):
                ink_width, ink_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                if ink_height > line_height:
                    raise _error("subtitle glyph exceeds line height")
                x, ink_y = (width - ink_width) // 2, y + 12 + (line_height - ink_height) // 2
                ink_bounds.append([x, ink_y, x + ink_width, ink_y + ink_height])
                if spec["subtitles"]["burn_in"]:
                    draw.text((x - bounds[0], ink_y - bounds[1]), line, font=font, fill="white")
                y += line_height
        if frame.crop(card["card_bounds"]).tobytes() != source.crop(card["card_bounds"]).tobytes():
            raise _error("subtitles changed native source-card pixels")
        stream = BytesIO()
        frame.save(stream, format="PNG", optimize=False, compress_level=6)
        content = stream.getvalue()
        filename = f"segments/{i:03d}.png"
        if write:
            atomic_write(root / filename, content)
        elif safe_read(root / filename, "composition segment") != content:
            raise _error("subtitle raster mismatch")
        result.append({"frame_file": filename, "frame_sha256": digest(content), "start_frame": start, "end_frame": end,
                       "frame_count": end - start, "source_frame": card["frame_file"], "card_bounds": card["card_bounds"],
                       "caption_bounds": box, "ink_bounds": ink_bounds, "caption_text": caption["text"] if caption else None})
    return result


def _mix(root: Path, tracks: list[tuple[str, dict]], sample_frames: int, ffmpeg: str) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_complex_threads", "1"]
    filters = []
    for index, (name, track) in enumerate(tracks):
        command += ["-protocol_whitelist", "file", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", str(root / f"{name}.f32")]
        filters.append(f"[{index}:a]volume={_decimal(track['gain_db'], 'gain', -60, 12)}dB[a{index}]")
    filters.append("".join(f"[a{index}]" for index in range(len(tracks))) + f"amix=inputs={len(tracks)}:duration=longest:dropout_transition=0:normalize=0,atrim=end_sample={sample_frames}[out]")
    command += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_f32le", "-f", "f32le", "-fs", str(MAX_AUDIO_BYTES - 4096), str(root / "mix.f32")]
    _run(command, "ffmpeg")
    data = safe_read(root / "mix.f32", "mixed PCM", MAX_AUDIO_BYTES)
    metrics = _pcm_metrics(data)
    if metrics["sample_frames"] != sample_frames or metrics["clipped_samples"]:
        raise _error("mixed audio clips or has wrong duration; lower explicit gain values")
    # PCM16 WAV is portable and retained for listening/rechecking independently.
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-protocol_whitelist", "file",
          "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", str(root / "mix.f32"),
          "-map_metadata", "-1", "-c:a", "pcm_s16le", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(root / "audio" / "mix.wav")], "ffmpeg")


def _encode(root: Path, segments: list[dict], plan: dict, ffmpeg: str) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_complex_threads", "1"]
    for row in segments:
        command += ["-protocol_whitelist", "file", "-threads", "1", "-loop", "1", "-framerate", str(plan["fps"]), "-i", str(root / row["frame_file"])]
    command += ["-protocol_whitelist", "file", "-i", str(root / "audio" / "mix.wav")]
    filters = [f"[{index}:v]trim=end_frame={row['frame_count']},settb=expr=1/{plan['fps']},setpts=N+{row['start_frame']}[v{index}]" for index, row in enumerate(segments)]
    filters.append("".join(f"[v{i}]" for i in range(len(segments))) + f"interleave=nb_inputs={len(segments)}:duration=longest[out]")
    command += ["-filter_complex", ";".join(filters), "-map", "[out]", "-map", f"{len(segments)}:a:0", "-map_metadata", "-1",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "1",
                "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-r", str(plan["fps"]),
                "-fps_mode", "cfr", "-frames:v", str(plan["total_frames"]), "-video_track_timescale", str(plan["fps"] * 1000),
                "-movie_timescale", str(SAMPLE_RATE * plan["fps"]), "-movflags", "+faststart", "-fs", str(MAX_VIDEO_BYTES - 1024 * 1024), str(root / "composed.mp4")]
    _run(command, "ffmpeg")


def _probe_video(path: Path, reel: dict, ffprobe: str) -> dict:
    raw = _run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-count_frames", "-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_read_frames,duration,sample_rate,channels:format=duration,format_name", "-of", "json", str(path)], "ffprobe")
    try:
        value = parse_json(raw)
        streams = value["streams"]
        video = [stream for stream in streams if stream["codec_type"] == "video"]
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        expected = Fraction(reel["plan"]["total_frames"], reel["plan"]["fps"])
        if len(streams) != 2 or len(video) != 1 or len(audio) != 1:
            raise _error("composition requires exactly one video and one audio stream")
        v, a = video[0], audio[0]
        if (v["codec_name"] != "h264" or a["codec_name"] != "aac" or v["pix_fmt"] != "yuv420p"
                or v["width"] != reel["settings"]["width"] or v["height"] != reel["settings"]["height"]
                or Fraction(v["r_frame_rate"]) != reel["plan"]["fps"] or Fraction(v["avg_frame_rate"]) != reel["plan"]["fps"]
                or int(v["nb_read_frames"]) != reel["plan"]["total_frames"]
                or int(a["sample_rate"]) != SAMPLE_RATE or a["channels"] != CHANNELS
                or abs(Fraction(v["duration"]) - expected) > Fraction(1, 1000000)
                or abs(Fraction(a["duration"]) - expected) > Fraction(1, SAMPLE_RATE)
                or abs(Fraction(value["format"]["duration"]) - expected) > Fraction(1, 1000)
                or "mp4" not in value["format"]["format_name"].split(",")):
            raise _error("encoded composition dimensions, fps, frame count, or audio/video duration mismatch")
        return value
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("invalid composition media metadata") from exc


def _inventory(root: Path) -> dict:
    files, total = {}, 0
    allowed_dirs = {"source", "source/frames", "inputs", "segments", "audio"}
    for path in root.rglob("*"):
        clean_path(path, "composition artifact")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            if relative not in allowed_dirs:
                raise _error("unexpected composition directory")
        elif path.is_file():
            if relative == "composition-manifest.json":
                continue
            data = safe_read(path, "composition artifact", MAX_VIDEO_BYTES)
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise _error("composition bundle exceeds resource limit")
            files[relative] = {"sha256": digest(data), "bytes": len(data)}
        else:
            raise _error("non-regular composition artifact")
    return files


def compose_reel(source_reel_dir: Path | str, specification_path: Path | str, output_dir: Path | str,
                 *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    """Create a separate local composition; never alter or overwrite silent work."""
    source = clean_path(Path(source_reel_dir), "composition source")
    spec_path = clean_path(Path(specification_path), "composition specification")
    output = clean_path(Path(output_dir), "composition output")
    if output.exists() or source == output or output.is_relative_to(source) or source.is_relative_to(output):
        raise _error("output must be a new directory separate from the source reel")
    checked = verify_reel(source, ffprobe=ffprobe)
    source_manifest = safe_read(source / "reel-manifest.json", "source reel manifest", 256 * 1024)
    if digest(source_manifest) != checked["manifest_sha256"]:
        raise _error("source reel changed after verification")
    reel = parse_json(source_manifest)
    try:
        spec = _validate_spec(parse_json(safe_read(spec_path, "composition specification", 256 * 1024)), reel)
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("invalid or oversized numeric value in composition specification") from exc
    sample_frames = _round(Fraction(reel["plan"]["total_frames"] * SAMPLE_RATE, reel["plan"]["fps"]))
    if sample_frames < SAMPLE_RATE // 2:
        raise _error("composition must be at least half a second for meaningful audio measurement")
    font = safe_read(_local(spec_path.parent, spec["subtitles"]["font"]["path"], "font"), "subtitle font")
    if digest(font) != spec["subtitles"]["font"]["sha256"]:
        raise _error("subtitle font digest mismatch")
    audio_inputs = {}
    for name, track in spec["audio"].items():
        if track is not None:
            data = safe_read(_local(spec_path.parent, track["path"], "audio"), "audio input", MAX_AUDIO_BYTES)
            if digest(data) != track["sha256"]:
                raise _error("audio input digest mismatch")
            audio_inputs[name] = (data, _audio_format(data))
    runtime = {"ffmpeg": _version(ffmpeg, "ffmpeg"), "ffprobe": _version(ffprobe, "ffprobe"),
               "implementation_sha256": digest(safe_read(Path(__file__), "composition implementation"))}
    temporary = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with locked(output.parent):
            clean_path(output, "composition output")
            if output.exists():
                raise _error("output must be new")
            temporary = Path(tempfile.mkdtemp(prefix=".composition-", dir=output.parent))
            root = temporary
            for directory in ("source/frames", "inputs", "segments", "audio"):
                (root / directory).mkdir(parents=True, mode=0o700)
            for filename in ["reel.mp4", "reel-manifest.json", *(row["frame_file"] for row in reel["timeline"])]:
                atomic_write(root / "source" / filename, safe_read(source / filename, "source reel artifact", MAX_VIDEO_BYTES))
            if verify_reel(root / "source", ffprobe=ffprobe)["manifest_sha256"] != checked["manifest_sha256"]:
                raise _error("source reel changed while snapshotting")
            atomic_write(root / "inputs" / "font.bin", font)
            spec["subtitles"]["font"]["path"] = "inputs/font.bin"
            reports, tracks = {}, []
            for name, (data, demuxer) in audio_inputs.items():
                track = spec["audio"][name]
                track["path"] = f"inputs/{name}.media"
                path = root / track["path"]
                atomic_write(path, data)
                probe = _audio_probe(path, demuxer, ffprobe)
                pcm = _decode(path, root / f"{name}.f32", demuxer, ffmpeg)
                metrics = _pcm_metrics(pcm)
                if metrics["clipped_samples"]:
                    raise _error("supplied audio clips; provide a clean source")
                loudness = _loudness(root / f"{name}.f32", ffmpeg, raw=True)
                adjusted, adjustment = _duration_adjust(pcm, sample_frames, track["duration_policy"])
                atomic_write(root / f"{name}.f32", adjusted)
                reports[name] = {"probe": probe, "pcm": metrics, "loudness": loudness, "adjustment": adjustment}
                tracks.append((name, track))
            atomic_write(root / "composition-input.json", canonical(spec))
            srt, vtt = subtitle_files(spec["captions"], spec["fps"], reel["plan"]["total_frames"])
            atomic_write(root / "captions.srt", srt)
            atomic_write(root / "captions.vtt", vtt)
            segments = _render_segments(root, reel, spec, font, write=True)
            _mix(root, tracks, sample_frames, ffmpeg)
            mix_pcm = _decode(root / "audio" / "mix.wav", root / "mix-check.f32", "wav", ffmpeg)
            mix_metrics = {**_pcm_metrics(mix_pcm), **_loudness(root / "audio" / "mix.wav", ffmpeg)}
            if mix_metrics["clipped_samples"] or mix_metrics["true_peak"] >= 1:
                raise _error("mixed audio has clipping or intersample overs; lower explicit gains")
            _encode(root, segments, reel["plan"], ffmpeg)
            media = _probe_video(root / "composed.mp4", reel, ffprobe)
            encoded_pcm = _decode(root / "composed.mp4", root / "encoded-check.f32", "mov", ffmpeg, allow_aac_padding=True)
            encoded_metrics = {**_pcm_metrics(encoded_pcm), **_loudness(root / "composed.mp4", ffmpeg)}
            # AAC decode includes at most one final padded codec frame. Container
            # duration is validated above; record the padding instead of hiding it.
            padding = encoded_metrics["sample_frames"] - sample_frames
            if not 0 <= padding < 1024 or encoded_metrics["clipped_samples"] or encoded_metrics["true_peak"] >= 1:
                raise _error("encoded audio clips or decoded duration exceeds AAC padding tolerance")
            encoded_metrics["aac_padding_sample_frames"] = padding
            for scratch in root.glob("*.f32"):
                scratch.unlink()
            manifest = {"schema_version": 1, "source_manifest_sha256": checked["manifest_sha256"], "specification_sha256": digest(canonical(spec)),
                        "sample_frames": sample_frames, "segments": segments, "input_audio": reports, "mixed_audio": mix_metrics,
                        "encoded_audio": encoded_metrics, "media": media, "runtime": runtime, "files": _inventory(root),
                        "publish_ready": False, "limitations": LIMITATIONS}
            manifest_bytes = canonical(manifest)
            atomic_write(root / "composition-manifest.json", manifest_bytes)
            verify_composition(root, ffmpeg=ffmpeg, ffprobe=ffprobe)
            os.rename(root, output)
            temporary = None
    except OSError as exc:
        raise _error("cannot safely create composition artifacts") from exc
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
    return {"output_dir": str(output), "video": str(output / "composed.mp4"), "manifest": str(output / "composition-manifest.json"),
            "manifest_sha256": digest(manifest_bytes), "video_sha256": manifest["files"]["composed.mp4"]["sha256"],
            "integrity": "passed", "publish_ready": False}


def verify_composition(output_dir: Path | str, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                       expected_manifest_sha256: str | None = None) -> dict:
    root = clean_path(Path(output_dir), "composition export")
    raw = safe_read(root / "composition-manifest.json", "composition manifest", 512 * 1024)
    if expected_manifest_sha256 is not None and digest(raw) != _sha(expected_manifest_sha256, "expected manifest digest"):
        raise _error("manifest does not match trusted expected SHA256")
    try:
        manifest = _exact(parse_json(raw), {"schema_version", "source_manifest_sha256", "specification_sha256", "sample_frames", "segments", "input_audio", "mixed_audio", "encoded_audio", "media", "runtime", "files", "publish_ready", "limitations"}, "manifest")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["publish_ready"] is not False or manifest["limitations"] != LIMITATIONS:
            raise _error("invalid composition provenance")
        if canonical(_inventory(root)) != canonical(manifest["files"]):
            raise _error("composition artifact inventory or digest mismatch")
        checked = verify_reel(root / "source", ffprobe=ffprobe)
        if checked["manifest_sha256"] != manifest["source_manifest_sha256"]:
            raise _error("source reel manifest mismatch")
        reel = parse_json(safe_read(root / "source" / "reel-manifest.json", "source manifest", 256 * 1024))
        spec_raw = safe_read(root / "composition-input.json", "composition specification", 256 * 1024)
        spec = _validate_spec(parse_json(spec_raw), reel)
        if digest(spec_raw) != manifest["specification_sha256"]:
            raise _error("composition specification mismatch")
        expected_files = {"composition-input.json", "captions.srt", "captions.vtt", "inputs/font.bin", "audio/mix.wav", "composed.mp4", "source/reel.mp4", "source/reel-manifest.json"}
        expected_files.update("source/" + row["frame_file"] for row in reel["timeline"])
        font_spec = spec["subtitles"]["font"]
        if font_spec["path"] != "inputs/font.bin":
            raise _error("invalid archived font path")
        font = safe_read(root / "inputs" / "font.bin", "subtitle font")
        if digest(font) != font_spec["sha256"]:
            raise _error("subtitle font digest mismatch")
        segments = _render_segments(root, reel, spec, font, write=False)
        if canonical(segments) != canonical(manifest["segments"]):
            raise _error("subtitle segment geometry mismatch")
        expected_files.update(row["frame_file"] for row in segments)
        srt, vtt = subtitle_files(spec["captions"], spec["fps"], reel["plan"]["total_frames"])
        if safe_read(root / "captions.srt", "SRT") != srt or safe_read(root / "captions.vtt", "VTT") != vtt:
            raise _error("subtitle sidecar mismatch")
        sample_frames = _round(Fraction(reel["plan"]["total_frames"] * SAMPLE_RATE, spec["fps"]))
        if type(manifest["sample_frames"]) is not int or sample_frames != manifest["sample_frames"]:
            raise _error("mixed audio duration plan mismatch")
        names = {name for name, track in spec["audio"].items() if track is not None}
        if set(manifest["input_audio"]) != names:
            raise _error("audio report inventory mismatch")
        with tempfile.TemporaryDirectory(prefix="honeytem-composition-verify-") as scratch:
            work = Path(scratch)
            for name in sorted(names):
                track = spec["audio"][name]
                if track["path"] != f"inputs/{name}.media":
                    raise _error("invalid archived audio path")
                expected_files.add(track["path"])
                data = safe_read(root / track["path"], "input audio", MAX_AUDIO_BYTES)
                if digest(data) != track["sha256"]:
                    raise _error("audio provenance digest mismatch")
                demuxer = _audio_format(data)
                report = manifest["input_audio"][name]
                pcm = _decode(root / track["path"], work / f"{name}.f32", demuxer, ffmpeg)
                metrics = _pcm_metrics(pcm)
                adjusted, adjustment = _duration_adjust(pcm, sample_frames, track["duration_policy"])
                actual = {"probe": _audio_probe(root / track["path"], demuxer, ffprobe), "pcm": metrics,
                          "loudness": _loudness(work / f"{name}.f32", ffmpeg, raw=True), "adjustment": adjustment}
                if canonical(report) != canonical(actual) or metrics["clipped_samples"]:
                    raise _error("input audio measurements mismatch")
                atomic_write(work / f"{name}.f32", adjusted)
            (work / "audio").mkdir()
            _mix(work, [(name, spec["audio"][name]) for name in ("narration", "music") if name in names], sample_frames, ffmpeg)
            if safe_read(work / "audio" / "mix.wav", "recomputed mix", MAX_AUDIO_BYTES) != safe_read(root / "audio" / "mix.wav", "stored mix", MAX_AUDIO_BYTES):
                raise _error("audio mix does not match declared inputs, gains, and duration policy")
            # Metadata and file hashes alone cannot establish that the MP4
            # actually contains the declared caption pixels. Re-encode from
            # verified rasters and regenerated PCM with single-thread bitexact
            # settings, then compare the complete file. Runtime compatibility
            # is required, just as it is for byte-exact mix verification.
            reproduction_segments = [dict(row, frame_file=str(root / row["frame_file"])) for row in segments]
            _encode(work, reproduction_segments, reel["plan"], ffmpeg)
            if digest(safe_read(work / "composed.mp4", "reproduced composition", MAX_VIDEO_BYTES)) != manifest["files"]["composed.mp4"]["sha256"]:
                raise _error("encoded video does not match declared subtitle rasters and audio; use the original FFmpeg build")
            for filename, field, demuxer in (("audio/mix.wav", "mixed_audio", "wav"), ("composed.mp4", "encoded_audio", "mov")):
                pcm = _decode(root / filename, work / "check.f32", demuxer, ffmpeg, allow_aac_padding=(field == "encoded_audio"))
                metrics = {**_pcm_metrics(pcm), **_loudness(root / filename, ffmpeg)}
                if field == "encoded_audio":
                    metrics["aac_padding_sample_frames"] = metrics["sample_frames"] - sample_frames
                    if not 0 <= metrics["aac_padding_sample_frames"] < 1024:
                        raise _error("encoded audio duration mismatch")
                elif metrics["sample_frames"] != sample_frames:
                    raise _error("mixed audio duration mismatch")
                if canonical(metrics) != canonical(manifest[field]) or metrics["clipped_samples"] or metrics["true_peak"] >= 1:
                    raise _error("output audio measurements mismatch or clipping detected")
        if set(manifest["files"]) != expected_files:
            raise _error("unexpected or missing composition file")
        if canonical(_probe_video(root / "composed.mp4", reel, ffprobe)) != canonical(manifest["media"]):
            raise _error("composition media mismatch")
        runtime = _exact(manifest["runtime"], {"ffmpeg", "ffprobe", "implementation_sha256"}, "runtime")
        _sha(runtime["implementation_sha256"], "implementation digest")
        for name in ("ffmpeg", "ffprobe"):
            if not _text(runtime[name], "runtime version", 1000).startswith(name + " version "):
                raise _error("invalid runtime version")
    except (KeyError, TypeError, ValueError, OSError, IndexError, ZeroDivisionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise _error("missing or malformed composition artifact") from exc
    return {"output_dir": str(root), "manifest_sha256": digest(raw), "video_sha256": manifest["files"]["composed.mp4"]["sha256"],
            "integrity": "passed", "publish_ready": False}


def register_cli(commands) -> None:
    command = commands.add_parser("compose-reel", help="Local provided-audio and scripted-subtitle review export")
    command.add_argument("source_reel_dir", type=Path)
    command.add_argument("--spec", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command = commands.add_parser("verify-composition")
    command.add_argument("output_dir", type=Path)
    command.add_argument("--expected-manifest-sha256")


def handle_cli(args) -> dict:
    if args.command == "compose-reel":
        return compose_reel(args.source_reel_dir, args.spec, args.output)
    if args.command == "verify-composition":
        return verify_composition(args.output_dir, expected_manifest_sha256=args.expected_manifest_sha256)
    raise _error("unsupported command")
