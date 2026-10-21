# Rights-aware local image preparation

`prepare-image` is an explicit preprocessing step, not a relaxation of the
renderer or input schema. Existing `validate` and `build` commands still reject
oversized, non-RGB, rotated, and color-tagged assets. Nothing is downloaded,
generated with AI, uploaded, or published by this feature.

## Rights first, transformations off by default

Every source needs an operator-authored rights JSON file, even for an unchanged
copy. It records the operator, source, purpose, permission reference, affiliate
program when applicable, rights basis, and exact permitted operations. The
software checks this assertion's structure and scope. It **does not verify a
license, ownership, permission reference, or legal entitlement**.

Example for locally created synthetic test material, not a claim about any
seller's product image:

```json
{
  "schema_version": 1,
  "status": "granted",
  "operator": "Local test operator",
  "source": "Locally created synthetic fixture",
  "reference": "Fixture creation record held by the operator",
  "purpose": "Offline preparation test, not product advertising",
  "program": null,
  "basis": "own_work",
  "allowed_operations": [
    "copy", "resize", "color_convert", "exif_transpose",
    "flatten_alpha", "strip_metadata", "encode_png"
  ]
}
```

- `status` must be `granted`. Denied/unknown rights cannot be overridden by a flag.
- `program` is `null`, `toss_sharelink`, `coupang_partners`, or
  `kakao_gift_curator`. It must describe the intended actual use, not be omitted
  to evade a restriction.
- `basis` is `own_work`, `separate_permission`, or `program_policy`.
- Include only operations actually covered by the evidence. `copy` is required
  because the package retains the original. Any alteration additionally requires
  the CLI's `--allow-transform`, and each operation is separately checked.
- Re-encoding and metadata removal are alterations too. Transformed results need
  `encode_png` and `strip_metadata` permission, even if a conversion happens to
  leave the decoded pixels unchanged.
- EXIF orientation additionally needs `--allow-exif-transpose`. Transparency
  additionally needs an explicit `--matte '#RRGGBB'`. Neither option grants rights.

### Toss's no-edit rule stays in force

For `program: "toss_sharelink"` with `basis: "program_policy"`, **all alterations
are blocked**, even if the operator lists every operation and uses every flag.
An already-compliant, byte-exact copy is the only preparation this basis allows.
Resizing, color conversion, alpha flattening, EXIF rotation, profile stripping,
and container conversion are not presumed permitted by the program's image-use
policy. See [the repository's Toss rules](../rules/toss-sharelink.md).

Use independently owned material or genuinely separate permission when that
permission covers the intended transformation and use. Labeling a downloaded
seller image `own_work` is not a valid workaround. The tool cannot establish the
truth of an operator assertion. Image use and layout approvals still apply.

## Commands

Run from an installed checkout or prefix commands with `PYTHONPATH=src` and use
`python -m honeytem`:

```sh
# A small, compliant RGB PNG/JPEG is copied without changing a single byte.
honeytem prepare-image assets/original.png \
  --rights rights.json --output assets/prepared-product

# For a large/profiled source with separately verified transformation rights:
honeytem prepare-image assets/authorized-source.tif \
  --rights rights.json --output assets/prepared-product \
  --allow-transform

# For an authorized rotated/transparent PNG or JPEG, as applicable:
honeytem prepare-image assets/authorized-source.png \
  --rights rights.json --output assets/prepared-product \
  --allow-transform --allow-exif-transpose --matte '#FFFFFF'

honeytem verify-image assets/prepared-product
honeytem verify-image assets/prepared-product \
  --expected-manifest-sha256 TRUSTED_64_CHARACTER_LOWERCASE_SHA256
```

Output must be a **new directory whose parent already exists**. Existing files,
directories, and symlink destinations are never overwritten. The original input
is opened read-only and checked again before output creation. Interrupted writes
may leave a partial directory; inspection and a new destination are required,
not automatic replacement.

The preparation package contains exactly:

- `original.bin`: the full source file, byte for byte, including its original ICC,
  EXIF, compression, and other metadata. The extension is deliberately neutral;
  the source format is recorded in the manifest.
- `prepared.png`, or `prepared.jpg` for a byte-exact JPEG copy: the asset to use.
- `manifest.json`: canonical, deterministic transformation/provenance record.

The command returns JSON with `verified`, source/output/manifest SHA-256 digests,
the output filename, output dimensions, `byte_exact_copy`, and
`publish_ready: false`. Retain a manifest digest independently if later checks
must establish that this is the same prepared package.

## Image and color contract

Inputs are single-frame PNG, JPEG, or TIFF, at most 32 MiB, 40 million pixels,
and 32,768 pixels on either edge. GIF, WebP, other containers, animation,
multi-page TIFF, palette images, unsupported high-bit-depth modes, malformed
profiles, and decompression-bomb warnings/errors fail closed. Symbolic links in
input or output paths, parent traversal, nonregular files, and output collisions
are rejected. Reads and writes use no-follow directory-relative descriptors.

Supported pixel modes are RGB, RGBA, L, LA, CMYK, and LAB. CMYK/LAB require an
embedded, matching ICC profile. ICC headers, profile size, color-space/mode
agreement, and the lcms transform must validate; unsupported profiles fail.
PNG gamma, chromaticity, or sRGB chunks without a supported unambiguous ICC-only
path are rejected rather than silently stripped. Conflicting color tags also
fail. Non-sRGB EXIF color space without ICC fails.

Processing order is fixed and recorded:

1. Apply a nonidentity EXIF orientation only with explicit operator permission
   and opt-in. PNG/JPEG orientation values 1–8 are supported. Oriented TIFF is
   rejected because Pillow applies TIFF orientation inside its decoder, which
   would bypass this separately controlled step. Ordinary unrotated TIFF works.
2. Convert ICC-tagged pixels to sRGB using Pillow/lcms, relative-colorimetric
   intent and flags `0`. **The source profile is used for conversion before it
   is removed.** Untagged RGB follows the existing pipeline's sRGB convention;
   untagged grayscale is expanded by replicating its channel with explicit
   `color_convert` permission. This is not a claim of calibrated source color.
3. If transparency exists, composite onto the explicit sRGB matte using Pillow's
   integer alpha compositing. RGBA/LA and RGB/grayscale PNG `tRNS` transparency
   all require this; even an all-opaque alpha channel needs a matte choice.
   The matte is recorded. No silent white or black background is supplied.
4. If necessary, uniformly downscale using LANCZOS to fit within 800 × 560. There
   is no crop or upscale. A single exact rational scale is used for both axes,
   with half-up nearest-integer edge rounding (at most half a pixel per edge).
   An extreme ratio that would make either scaled edge smaller than one pixel
   is rejected rather than stretched.
5. Encode transformed pixels into a fresh RGB PNG with no ICC, EXIF, or other
   color metadata. This drops tags only **after** their required interpretation.
   Small already-compliant PNG/JPEG files instead retain the entire original
   byte sequence, including metadata allowed by the existing schema.

Resampling, matte compositing, and wide-gamut-to-sRGB conversion can change
appearance. Relative-colorimetric conversion can clip out-of-gamut colors.
The pipeline is color-managed, not a guarantee of identical appearance on every
display or mathematical pixel preservation across transformations. Review the
result visually before approving a package.

## Verification and provenance

The audit records the complete rights assertion and options, every operation,
source and output file digests, decoded pixel digests, input/profile properties,
orientation, original and oriented dimensions, output dimensions, common scale,
no-crop/no-upscale settings, and Python/Pillow/lcms/zlib/JPEG/libtiff versions.
It has no clock-dependent field, so repeatability is testable.

`verify-image` does more than compare stored hashes: it decodes the retained
original, rechecks rights and operation gates, reruns the pipeline, verifies
the RGB/no-profile contract, and compares all prepared bytes and the complete
canonical audit. Wrong geometry, changed pixels, altered source, unauthorized
operations, missing/extra files, modified options, and runtime changes fail.
The same inputs and library builds are required for byte-level reproduction.

A coordinated replacement of the original, rights assertion, derivative, and
audit cannot be detected by self-contained consistency checks alone. Supplying
an independently retained `--expected-manifest-sha256` binds verification to
the previously trusted package. A valid digest still does not prove the asserted
rights, source identity, or suitability for advertising. The saved source copy
is verified; the verifier does not revisit the external original's old pathname.

## Use with the existing asset schema

No schema change or automatic import is needed. Store the preparation directory
inside the job's input directory, then set the intended product's existing
`asset.path` to its prepared file, for example
`assets/prepared-product/prepared.png`. Preserve the existing mode/asset-kind
contract and truthful `asset.source` and `asset.license`; the source text can
refer to the relative preparation audit path. Keep the actual permission
reference in the rights record.

`asset.usage_confirmed` and `asset.layout_permission_confirmed` must still be
explicitly true based on the operator's review. Preparation does not set these,
authorize a layout, transfer approval, change a real image into a synthetic
one, or publish anything. Run the unchanged `validate` and `build`, then review
and approve the newly built package under the normal workflow. The core schema
does not automatically enforce or ingest preparation audits, so retain and
verify the preparation package separately as provenance evidence.

## API and tests

`honeytem.imageprep.prepare_image(source, output_dir, *, rights,
allow_transform=False, allow_exif_transpose=False, matte=None)` creates and
verifies the package. `verify_prepared_image(output_dir, *,
expected_manifest_sha256=None)` reproduces it. `load_rights(path)` validates a
local rights file. `register_cli(commands)` and `handle_cli(args)` provide the
CLI integration without changing the existing asset validator.

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_imageprep.py' -v
```

Tests use actual generated sRGB and LAB ICC profiles and real Pillow/lcms
conversion, large geometry, JPEG EXIF rotation, alpha/tRNS matte handling,
byte-exact no-op copies, untouched source bytes, strict existing-schema asset
integration, per-operation rights/Toss gates, tampering, unsafe paths, malformed
profiles, animation, and decompression limits. They do not call a model, an image
generation API, a paid API, or any network endpoint.
