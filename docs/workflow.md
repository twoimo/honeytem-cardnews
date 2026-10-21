# Durable multi-product workflow

This is a real provider-to-package orchestrator, not a scheduler or a stand-alone
queue demo. It connects `execute_provider`, `import_result`, resumable offline
`build`, and optional local `export_reel`. It never publishes, issues tracking
links, fetches product websites, installs software, provisions credentials, or
claims independent human/model-quality verification.

Start with a valid manifest, including an explicitly promoted draft when using
draft ingestion. Raw intake drafts are not eligible. One to five products each
have research, copy and image stages. Selected stages execute the configured
HTTP provider. Unselected stages explicitly retain the existing validated facts,
copy and authorized imagery. Affiliate research is source-excerpt-grounded, not
cold-start discovery. Generated images are allowed only for nonfixture concepts;
affiliate products must retain actual authorized product imagery.

## Completely offline path

```sh
honeytem workflow-plan examples/offline/generated/manifest.json
honeytem workflow-init examples/offline/generated/manifest.json --output /tmp/honeytem-flow
honeytem workflow-status /tmp/honeytem-flow
honeytem workflow-run /tmp/honeytem-flow
```

Use the actual manifest location returned by the offline fixture generator.
`workflow-plan` validates without writing or executing requests. `workflow-init`
requires a new directory and takes immutable local snapshots of the manifest,
assets, fonts, provider configurations, and source material. The snapshot uses
self-contained relative paths, so its fingerprint differs from the source
manifest fingerprint. Both source and snapshot provenance are retained. Original
source files may subsequently change without silently changing this workflow.
To use changed sources/configurations/code/runtime, initialize a new workflow.

A no-spec workflow retains every validated input and builds a review package
without any network access. Re-running validates existing artifacts and resumes
only missing local stages. Package approval is separate; completed workflow does
not mean approved or ready to publish.

## Provider plan

An explicit JSON spec has exactly these fields. Paths are relative to the spec
file; explicit absolute local paths are also accepted. Refer to provider docs for
the provider configuration schema. Only environment-variable *names*, never
inline keys, are stored in provider configurations.

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "product_id": "01",
      "kind": "product_research",
      "config": "chat-provider.json",
      "source_material": "product-01-sources.json"
    },
    {
      "product_id": "01",
      "kind": "editorial_copy",
      "config": "chat-provider.json",
      "source_material": "product-01-sources.json"
    },
    {
      "product_id": "02",
      "kind": "editorial_copy",
      "config": "chat-provider.json",
      "source_material": "product-02-sources.json"
    }
  ],
  "budget": {
    "max_requests": 6,
    "max_output_tokens": 12288,
    "max_request_bytes": 1572864
  },
  "reel": null
}
```

Chat source files contain `[{"url":"https://example.com/evidence", "excerpt":"…"}]`;
URLs must match retained evidence. Concept editorial copy can use `[]`. Image
source files contain `{"prompt":"…"}`. Source excerpts are untrusted data, not
instructions. Configured protocol must match the task. Research requires a valid
affiliate manifest; image generation requires a nonfixture concept manifest.

```sh
honeytem workflow-plan manifest.json --spec workflow-spec.json
honeytem workflow-init manifest.json --spec workflow-spec.json --output /tmp/honeytem-flow
honeytem workflow-run /tmp/honeytem-flow --allow-network
# Remote HTTPS additionally requires --allow-remote, on every run.
```

Network is never enabled by saved state or by a previous run. Read-only status,
review, resolution, and local builds never invoke a provider. One run executes at
most one selected task and stops at human review. Stages execute serially, ordered
by product ID and research → copy → image. An accepted import recomputes the full
manifest/dependency fingerprint before any later request. No candidate produced
against an earlier revision is silently rebased or accepted.

The workflow-wide budget is a lifetime ceiling, not a monetary estimate. Before
every actual HTTP attempt (including allowed 429 retries), the journal durably
reserves one request, the configured output-token maximum (zero for images), and
the configured maximum request-body bytes. Byte reservations are deliberately
conservative, not the actual payload length. Reservations are never refunded on
failure. A restart does not reset caps. Credentials remain runtime environment
values; source/configuration files and candidate artifacts should still be kept
private. Provider model choice, service cost and contractual permissions remain
the operator's responsibility.

## Review and import

Status returns the immutable candidate path and exact SHA-256. Inspect the
candidate, trace, images and evidence, then explicitly accept or reject:

```sh
honeytem workflow-review /tmp/honeytem-flow 01-editorial_copy \
  --candidate-sha256 SHA256_FROM_STATUS --reviewer 'Operator name' --decision accept
honeytem workflow-run /tmp/honeytem-flow --allow-network
```

Acceptance uses `import_result` and writes an immutable next manifest revision.
Rejection records the decision and retains the validated baseline for that task.
Research acceptance additionally requires `--claims-verified`. Image acceptance
requires all of `--image-license 'Applicable rights/terms reviewed by operator'`,
`--image-usage-confirmed`, and `--image-layout-permission-confirmed`. Oversized or
otherwise incompatible generated imagery remains unimportable; the workflow
never resizes, recolors, or converts it to bypass the asset contract.

Every actual provider result is unreviewed. Provider JSON cannot supply the
operator receipt, self-assert approval, or inherit the missing research/image
confirmations. Separate operator commands create digest-bound review receipts.
These are local operator assertions, not authenticated human identity or digital
signatures. Anyone with full filesystem control can replace the entire trust
store; protect the directory and retain trusted hashes separately when needed.
Automated code must not call review commands on the human's behalf without their
explicit review of the relevant exact candidate.

## Failure, uncertainty and recovery

`workflow-run` reports `failed` or `uncertain` and preserves all reservations. A
stopped process with a started attempt becomes uncertain on the next run, even
if an orphan candidate file was saved before the final journal checkpoint. It is
never automatically submitted again. HTTP transport timeouts and server errors
with unknown outcomes also stop. Internal retries are restricted to the provider
transport's configured explicit rejection policy (currently HTTP 429).

Inspect the provider's records first. Then choose an explicit resolution using
the attempt digest returned by status after recovery:

```sh
honeytem workflow-run /tmp/honeytem-flow
honeytem workflow-resolve /tmp/honeytem-flow 01-editorial_copy \
  --attempt-sha256 SHA256_FROM_STATUS --reviewer 'Operator name' \
  --resolution abandon --note 'Retain baseline; request outcome was uncertain.'
# Or --resolution retry only after consciously resolving possible duplicate cost.
```

`retry` does not refund budget and does not send a request. A subsequent run still
needs fresh network/remote opt-in. `abandon` explicitly retains the baseline and
allows later stages to continue. No operator resolution can raise the immutable
budget ceiling. If it is exhausted, start a newly reviewed workflow; do not
manually edit checkpoints.

A bounded strict JSON journal, immutable artifact hashes, replay-checked stage
ordering, full input fingerprints, atomic fsync/replace writes, safe regular-file
reads, and a nonblocking OS `flock` protect checkpoints. The workflow lock is
released automatically when its process dies. A concurrent operation fails
closed. Symlinked, malformed, missing, modified, or stale artifacts are rejected.

The underlying offline build/reel components use their own legacy exclusive lock
files. The workflow never deletes those locks, including apparently stale ones:
a separate direct build/reel process might own them. An operator must verify
ownership/process state before removing a stale lock. After that, build resumes
verified checkpoints; a fully saved package whose workflow commit was interrupted
is adopted only after verification. A fully saved reel is likewise verified
against the exact plan and source package before adoption. Incomplete temporary
files are not trusted as completed work. An immutable operator receipt is written
before its numbered manifest revision: a crash before the receipt cannot orphan a
revision. After the receipt, changing the bound decision is blocked; repeat the
identical acceptance/rejection to reconcile it, or start a new workflow. This
prevents orphan revisions colliding with later product imports.

## Optional local reel

Set `reel` in the spec to an object such as:

```json
{"durations":"3","fps":30,"padding_color":"#FFFFFF","vertical_align":"center","allow_affiliate_transcoding":false}
```

Or provide an ordered duration array (cover first). Existing installed FFmpeg and
FFprobe are required; nothing is installed. Affiliate transcoding requires the
operator's separate explicit rights confirmation in this spec. Encoding is local,
silent, and review-only. It does not inherit package publication approval.

## Python/CLI integration

The public functions are `plan_workflow`, `init_workflow`, `run_workflow`,
`workflow_status`, `review_workflow`, and `resolve_workflow`. CLI integration uses
`register_cli(subparsers)` and `handle_cli(args) -> dict`, with flat commands
`workflow-plan`, `workflow-init`, `workflow-run`, `workflow-status`,
`workflow-review`, and `workflow-resolve`. The core offline CLI can register these
lazily without loading optional providers during existing build commands.

Tests use a real loopback HTTP fixture (not a live model), including multiple
products, research→copy dependencies, exact image bytes, durable 429 budget
reservations, timeout ambiguity, crash injection before/after HTTP, review/import
recovery, concurrent process locking, tampered state, and a clean offline package
build/resume. Fixture success proves contracts and state transitions, not live
model output quality or real product/evidence truth.
