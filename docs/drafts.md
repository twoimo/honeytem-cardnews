# Offline product drafts and cold-start ingestion

`draft-init` starts with a product URL and one affiliate program, without requiring
an already completed sales manifest. It does **not** fetch the URL. You can supply
locally saved HTML, JSON or text, then progressively add sources and manually
review facts. No network, model, account, link issuance or publication is involved.

A draft is **not a build manifest**. Extracted values remain candidates, separate
from the manifest-shaped fields. An extracted price does not become a verified
price; an image URL is not an image license; a product URL is not an owned
commission link. No confirmation or approval is inferred from source content.

## Start from only a URL

From the repository, with the package installed or `PYTHONPATH=src`:

```sh
python -m honeytem draft-init 'https://shop.vendor.co.kr/product/01' \
  --program toss_sharelink --output /tmp/honeytem-product-draft
python -m honeytem draft-report /tmp/honeytem-product-draft/draft.json
```

Use the actual product URL and one of `toss_sharelink`, `coupang_partners`, or
`kakao_gift_curator`. The vendor URLs in this document are test strings only;
commands do not visit them or claim they are real product evidence.

The directory contains `draft.json` and `sources/`. Unknown title, option,
quantity, seller, price, affiliate URL, policy evidence and asset rights stay
`null` or explicitly unconfirmed. `draft-report` returns:

- `draft_sha256`: exact file-byte digest to bind the next operation.
- `missing_items`: field paths and missing/confirmation reasons.
- `extractions`: unverified candidates, source digests and extraction status.
- `publish_ready: false`, always.

A zero missing count means required field values were supplied, not that files,
rights, claims or policies have been independently verified. Strict validation
still runs at promotion and downstream build.

## Supply local source material

Create a new draft with a source:

```sh
python -m honeytem draft-init 'https://shop.vendor.co.kr/product/01' \
  --program toss_sharelink --source /tmp/product-page.html \
  --source-format html --output /tmp/honeytem-product-with-source
```

Or add a source to a previous revision. Copy `draft_sha256` from its report:

```sh
python -m honeytem draft-ingest /tmp/honeytem-product-draft/draft.json \
  --source /tmp/product-page.html --source-format html \
  --expected-draft-sha256 HASH_FROM_REPORT \
  --output /tmp/honeytem-product-draft/with-source.json
```

Source formats:

- HTML: reads only `application/ld+json` script data. It never executes HTML or
  JavaScript, loads images, follows redirects, or fetches remote JSON-LD contexts.
- JSON: accepts explicitly typed Schema.org Product objects, including arrays or
  `@graph`, and plain product objects with a title/name and product-specific field.
- Text: conservatively extracts labeled lines such as `title:`, `상품명:`,
  `seller:`, `판매자:`, `price:`, `가격:`, `currency:`, `통화:`, and `sku:`.
  Free-form prose is saved but not guessed into facts.
- `auto`: chooses using filename extension or initial content. Specify the format
  explicitly when a filename/content is misleading.

Exact raw bytes are stored under `sources/<sha256>.bin`. Metadata records byte
count, supplied product URL, import time, format and extraction. Import time is
**not** a product-price verification time. The URL is operator-supplied source
attribution, not proof those bytes were retrieved from that URL. Snapshots can
contain private data from the supplied file; inspect before sharing the directory.

Multiple Product objects produce `ambiguous`, without selecting the first.
Multiple offers omit price fields. Repeated labeled fields are also ambiguous.
Malformed, duplicate-key, non-finite, undecodable or excessively nested source
JSON is retained as evidence but marked `invalid`, with no candidates. This is
intentionally different from malformed draft/review JSON, which is rejected.

No rounding, currency inference, discount reconstruction, image downloading,
affiliate-link generation or rights inference occurs. Price extraction accepts
only exact bounded nonnegative JSON integer literals or exact integer decimal
strings (for example, `"12000"` or `"12000.00"`). JSON floating-point tokens,
including `12000.0`, scientific notation and underflowing numbers, are omitted
rather than risking parser rounding. A fractional or formatted string such as
`"12.34"` or `"1,200"` is not converted into a manifest amount.

## Review and progressively merge confirmed fields

A review patch has the **manifest's nested shape**, but can contain only the fields
you checked. Arrays still have exactly one product and one disclosure. For example,
save this as `/tmp/checked-fields.json` only after verifying these values yourself:

```json
{
  "products": [{
    "title": "Manually checked product title",
    "seller": "Manually checked seller",
    "option": "Manually checked exact option",
    "quantity": 1,
    "price": {
      "amount": 12000,
      "currency": "KRW",
      "conditions": "Manually checked price conditions",
      "shipping": "Manually checked shipping terms"
    }
  }]
}
```

The numbers above illustrate the patch schema only, not verified sales data.

```sh
python -m honeytem draft-report /tmp/honeytem-product-draft/with-source.json
python -m honeytem draft-review /tmp/honeytem-product-draft/with-source.json \
  --patch /tmp/checked-fields.json --reviewer 'Operator name' --reviewed \
  --expected-draft-sha256 HASH_FROM_REPORT \
  --output /tmp/honeytem-product-draft/reviewed-01.json
```

Only supplied fields are merged. Candidates are never copied automatically.
Each review records the reviewer, local timestamp, exact previous draft digest,
and reviewed field paths. Omitting fields leaves them unchanged. Supplying `null`
or setting a confirmation back to `false` can revoke previously supplied facts.

Supply the remaining edition/copy/font/channel/disclosure/link/evidence/asset fields
in further patches. A full manifest-shaped patch is also accepted if the fixed
identity fields match. These drafts keep exactly one product, affiliate mode,
`fixture: false`, the originally supplied program and exact original URL. Start a
new draft to change that identity. Multi-product production assembly can use the
existing manifest contract after each product is independently reviewed.

The following booleans require human assertions; extraction cannot set them:
`claims_verified`, `link_ownership_confirmed`, `usage_confirmed`,
`layout_permission_confirmed`, disclosure `verified`, and channel
`profile_links_confirmed`. The latter may remain `false` under the existing
manifest contract. This tool records operator assertions, not independent proof.

## Explicit promotion to a strict manifest

Place separately authorized, already compatible product imagery in the draft
directory. Use relative asset paths; fonts may also use explicit absolute paths.
The existing dimensions, RGB, profile, licensing, link and disclosure requirements
remain in force. See [the input contract](python-workflow.md).

```sh
python -m honeytem draft-report /tmp/honeytem-product-draft/reviewed-final.json
python -m honeytem draft-promote /tmp/honeytem-product-draft/reviewed-final.json \
  --reviewer 'Operator name' --reviewed \
  --expected-draft-sha256 HASH_FROM_FINAL_REPORT \
  --output /tmp/honeytem-product-draft/manifest.json
python -m honeytem validate /tmp/honeytem-product-draft/manifest.json
```

Promotion fails unless all mandatory fields/confirmations are supplied and the
existing `validate_manifest` succeeds, including file checks. It writes the exact
validated manifest plus `manifest.json.promotion.json` with draft/manifest/source
hashes, reviewer and validation fingerprint. The receipt explicitly states
`operator_assertions_only: true` and `publish_ready: false`; it is not a publication
approval, online verification, or cryptographic attestation of a human identity.

Promotion never overwrites anything. Its receipt is installed first, so a crash
cannot leave a successful-looking manifest without a review receipt. If only the
receipt exists after an interruption, inspect it and retry with a **new output
filename**. Do not treat the receipt alone as a completed promotion.

## Runnable end-to-end synthetic smoke

This test starts with only a URL, fills **clearly labeled TEST ONLY** declarations,
creates a tiny local synthetic image, reviews, promotes, loads the resulting strict
manifest and checks exact digest/receipt binding:

```sh
PYTHONPATH=src:tests python -m unittest \
  test_drafts.DraftTests.test_full_smoke_promotes_exact_valid_manifest_with_receipt -v
```

It is a local contract test, not real sales evidence, image rights, model quality,
rendered-font quality or production readiness. It uses test font byte stand-ins
because manifest validation only reads font bytes. Actual builds must use valid
licensed fonts. For render/build tests use the repository's existing real-font
fixture generator and test harness.

Run all draft tests:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_drafts.py' -v
```

## Callable API

All functions are in `honeytem.drafts`; no CLI parsing is needed for callers:

```python
created = init_draft(product_url, program, output_dir,
                     source_path=None, source_format="auto")
report = draft_report(draft_path)
next_report = ingest_source(draft_path, source_path, next_revision_path,
                            expected_draft_sha256=report["draft_sha256"])
reviewed = review_draft(draft_path, manifest_patch, reviewed_path,
                        reviewer="Operator", reviewed=True,
                        expected_draft_sha256=report["draft_sha256"])
promoted = promote_draft(reviewed_path, manifest_path,
                          reviewer="Operator", reviewed=True,
                          expected_draft_sha256=reviewed["draft_sha256"])
```

`load_draft` validates the draft and rechecks snapshots/extractions. The registrar
`register_cli(subparsers)` and dispatcher `handle_cli(args)` integrate with the
main CLI. Downstream jobs should consume the promoted manifest with the existing
`load_manifest`, never a raw draft.

## Limits and fail-closed behavior

- One product per draft; up to 16 distinct source files, each at most 1 MiB.
- Draft files at most 2 MiB; CLI review patches at most 256 KiB.
- Source traversal at most 48 nested levels / 10,000 nodes and 64 JSON-LD blocks.
- UTF-8 sources only; unsupported bytes remain invalid snapshots.
- No symlinks, parent traversal, non-regular inputs, credential-bearing URLs, or
  input/output collisions. Files are new-only and source reads are bounded.
- Revisions must remain in the same directory so snapshot and asset paths retain
  their meaning. Prior revisions are not mutated or automatically deleted.
- Expected digests bind exact current file bytes; source tampering or edits to
  candidate values/flags fail validation. Candidate content is recomputed from
  the stored raw source before further operations.
- The local filesystem is not a hostile privileged-process sandbox. A person
  with write access can create their own records and claim to have reviewed them.
  Production manual review and publication controls remain separate.
