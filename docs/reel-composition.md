# Local audio and scripted-subtitle reels

`reel` remains the unchanged, silent default. `compose-reel` is an explicit,
separate review export built from an already verified silent reel. It accepts
local, provided narration and/or music. It does **not** generate speech, use a
TTS provider, discover/download music, contact a network service, publish, or
transfer approval from the source package.

```sh
honeytem compose-reel out/silent-reel --spec inputs/composition.json --output out/composed
honeytem verify-composition out/composed --expected-manifest-sha256 <saved-hash>
```

The Python equivalents are `compose_reel(source_reel_dir, specification_path,
output_dir)` and `verify_composition(output_dir, expected_manifest_sha256=...)`.
FFmpeg and FFprobe must be installed locally with libx264, AAC, and ebur128.
No dependency, font, or audio file is installed or downloaded automatically.

## Input contract

All fields below are required. `narration` or `music` may be `null`, but not both.
Paths must be relative to the JSON file, without traversal, URLs, or symlinks.
The font and each audio file need the SHA256 of their exact original bytes.
Use a new output directory, separate from the source reel.

```json
{
  "schema_version": 1,
  "fps": 30,
  "script": {
    "text": "제공된 원고 첫 문장\n제공된 원고 둘째 문장",
    "source": "User-provided and reviewed script, revision 1"
  },
  "captions": [
    {"start_frame": 0, "end_frame": 90, "text": "제공된 원고 첫 문장"},
    {"start_frame": 90, "end_frame": 180, "text": "제공된 원고 둘째 문장"}
  ],
  "subtitles": {
    "burn_in": true,
    "font": {"path": "NotoSansCJK-Regular.ttc", "index": 0, "sha256": "<64 lowercase hex characters>"},
    "font_size": 48
  },
  "audio": {
    "narration": {
      "path": "provided-voice.wav",
      "sha256": "<64 lowercase hex characters>",
      "source": "Locally supplied recording with speaker permission",
      "license": "Permission covers this use and mixing with music",
      "rights_confirmed": true,
      "duration_policy": "fail",
      "gain_db": "-3"
    },
    "music": {
      "path": "provided-music.wav",
      "sha256": "<64 lowercase hex characters>",
      "source": "Locally supplied licensed music; identify original source here",
      "license": "Identify actual license and derivative/synchronization permission",
      "rights_confirmed": true,
      "duration_policy": "truncate",
      "gain_db": "-18"
    }
  }
}
```

Replace example digests, sources, and rights declarations with real values. A
confirmation is a user declaration, not independent copyright verification.
Owning a recording does not necessarily grant music, speaker, or synchronization
rights. This software neither supplies such rights nor claims publication approval.

### Timeline and subtitles

- FPS must exactly match the verified source reel: integer 1–60.
- Caption intervals are half-open integer frames `[start_frame, end_frame)`.
  They must be sorted, nonoverlapping, and inside the source duration. Gaps are
  allowed. Conversion to SRT/VTT milliseconds rounds the exact rational
  `frame * 1000 / fps` half up, not accumulated floating-point durations.
- Captions must be ordered excerpts of the explicit script. No copy is invented.
- Maximum 24 captions, 160 characters each, two displayed lines, font size 32–96.
  HTML/ASS markup, braces, backslashes, format/control characters, and blank
  caption lines are rejected to avoid player-dependent interpretation.
- A real supplied font must cover Korean and every caption glyph. There is no
  font substitution or tofu fallback. Font bytes and the selected face are pinned.
- Pillow rasterizes the text; **no subtitle text enters FFmpeg commands or filters**.
  Captions fit entirely in top or bottom padding, never over any part of a source
  card, including product assets and disclosures. Per-segment text and ink bounds
  are recorded. Overflow fails rather than clipping, covering, or shrinking text.
- The canvas-safe padding margins are proportional to 80 pixels horizontally,
  96 at the top, and 144 at the bottom on 1080×1920. These are design margins,
  not a claim that every platform's changing interface leaves them unobscured.
  Review the final video in its intended app before use.
- `burn_in: false` retains unchanged padded card rasters with synchronized
  sidecar subtitles; `true` additionally burns in the same caption content.

### Audio and duration policy

Provided WAV, FLAC, Ogg, MP3, and M4A are recognized by header and opened with a
fixed demuxer. Playlists and remote URLs are not accepted. Each input must have
exactly one audio stream, no video/cover-art stream, one or two channels,
8–192 kHz, and duration at most 180 seconds. Inputs that cannot produce the
required loudness measurements fail explicitly rather than receiving invented
metrics. Very short audio (under one EBU R128 metadata block) can fail this check.

Inputs are decoded to 48 kHz stereo float PCM. The video's exact rational
frame duration is rounded half up to the nearest sample. Decoded sample count,
not an approximate metadata duration, decides the following explicit policy:

- `fail`: the audio must already have exactly the target sample count.
- `pad`: a shorter input is padded with silence; a longer input fails.
- `truncate`: a longer input is cut at the target sample; a shorter input fails.
- `pad_or_truncate`: either operation is explicitly permitted.

There is no looping, time stretching, silent policy fallback, speech generation,
auto-ducking, or automatic loudness normalization. Both tracks start at zero.
The specified gain (−60 to +12 dB) is applied before summing, without automatic
mix normalization. Full-scale samples are counted using a conservative PCM16 threshold
(32767/32768), and true-peak overs are checked separately. A clipped source,
clipped mix, or decoded AAC over/clip fails
and leaves no completed export; lower the explicit gains or supply clean audio.

The manifest reports original input metadata; decoded sample counts; adjustment
actions; sample peak, RMS, clipped sample count, EBU R128 integrated LUFS and
oversampled true peak; and measurements after lossy AAC encoding. Silence has
null logarithmic sample/RMS peaks and null integrated LUFS with a below-gate/insufficient-duration status;
FFmpeg's raw gated floor is recorded separately, never presented as measured
integrated loudness. Measurements are descriptive, not listening-quality or
broadcast-certification claims. AAC can decode a final padding block; fewer than
1024 extra sample frames are recorded separately, while MP4 presentation duration
must match the planned duration. MP4 movie timescale preserves fractional-FPS
audio edit-list timing instead of silently rounding it to milliseconds.

## Output and verification

The new directory includes:

- `composed.mp4`: H.264/yuv420p with one stereo AAC track.
- `captions.srt` and `captions.vtt`: frame-derived subtitle sidecars.
- `audio/mix.wav`: independently playable PCM mix for listening review.
- `source/`: a complete independently verifiable snapshot of the silent reel.
- `inputs/`: exact original audio and supplied font bytes.
- `segments/`: native-card-preserving PNGs, with optional burnt-in captions.
- `composition-input.json`: the archived spec with local artifact paths.
- `composition-manifest.json`: hashes, complete file inventory, geometry,
  duration policies, audio measurements, versions, and review limitations.

Verification checks the exact file inventory and hashes, reverifies the original
silent reel, rerenders every subtitle segment, regenerates both sidecars,
redecodes the original audio and rebuilds the declared mix byte-for-byte, repeats
audio measurements, deterministically re-encodes the verified segment rasters
and regenerated mix and compares the entire MP4 (including burned-in caption
pixels), and probes video dimensions, frame rate/count, codecs, and
audio/video duration. Native source-card pixels are compared exactly before
encoding. H.264/AAC are lossy; measurements and integrity checks do not replace
human visual/listening review or prove the truth of spoken/scripted claims.

Hashes are not signatures. Save the returned manifest hash independently and
provide it to verification to detect a wholesale replacement of both files and
manifest. Byte-repeatable mix/video reconstruction requires the same compatible FFmpeg
build and takes an additional full video encode; the
original runtime versions are retained. `publish_ready` remains `false`.

Resource limits: 180 seconds, minimum half-second composition, 24 captions,
160 million total segment-raster pixels, 80 MiB per original/decoded audio,
256 MiB per MP4, and 768 MiB final bundle. FFmpeg processes use bounded output,
timeouts, explicit local protocol allowlists, one encoder/filter thread, and
argument vectors rather than a shell. Failure removes only the newly created
staging directory and does not overwrite source or prior exports.

## Offline tests

```sh
PYTHONPATH=src:tests python -m unittest tests/test_composition.py -v
```

Integration tests create mathematical sine-wave WAV fixtures under CC0/public
domain terms. A tone in the narration slot tests actual audio composition; it is
**not a human voice, TTS output, or evidence of speech generation**. Tests exercise
real H.264/AAC encoding, Korean glyphs and safe padding, original-card preservation,
subtitle sidecars, measured loudness, rights/hash validation, explicit duration
policies, malformed media, FFmpeg failures, symlink rejection, and tamper checks.
