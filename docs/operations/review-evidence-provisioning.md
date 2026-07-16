# Review Evidence Provisioning Runbook

## Purpose and Current Status

This runbook is the operator contract for PR4A. It covers the offline evidence chain, server-managed delivery, import, audit, pre-consumption revocation, and incident recovery. It does not turn the unauthenticated local UI into an annotation or training system.

The engineering path may be exercised with the deterministic test fixture. A real queue may be activated only when all evidence and resource gates below pass. The current real-run result is recorded in [Review Evidence Inventory — 2026-07-15](review-evidence-inventory-2026-07-15.md) and remains blocked.

## Non-Negotiable Boundaries

- The online service imports an already qualified, self-contained bundle. It does not create truth, train a model, lower thresholds, or accept a client filesystem path.
- Model development, policy qualification, and target application are mutually exclusive populations. Alternate encodes, crops, resolutions, clips, and temporal excerpts of one match retain one source-group identity.
- The target application population contributes to neither model weights nor calibration, threshold selection, qualification audit, or promotion.
- Real video, tensors, reviewer ledgers, model weights, and bundles are runtime evidence outside Git.
- No synthetic truth, empty queue, default acceptance, dummy model, or one-hot values presented as probabilities may satisfy a missing prerequisite.
- Every producer output directory is new and immutable. To retry, create a new attempt directory; do not edit a published artifact in place.

## Roles and Separation of Duties

- Producer: prepares the three offline populations and builds the bundle.
- Primary reviewer A and B: independently review blinded packets.
- Adjudicator: human reviewer distinct from both primary reviewers and their fingerprints.
- Activator: selects and imports one compatible server-managed bundle.
- Auditor: independently inspects the activated generation and cannot be its activator.

Reviewer identity and fingerprint must come from a trusted external operating process. A client-supplied alias does not prove a person, independence, or blindness.

## Frozen Attempt Protocol

Before opening the final audit population, freeze and hash:

1. taxonomy and annotation instructions;
2. source-group inventory and the three population assignments;
3. sampling and class-support rules;
4. model and policy configuration, deterministic seed, and pinned runtime;
5. metrics, promotion thresholds, and statistical inferential unit;
6. reviewer role assignments;
7. attempt quota and retention deadline; and
8. target run identity and maximum review windows.

Do not tune an attempt after seeing its audit result. A protocol, population, model, or threshold change creates a new attempt and requires a previously unseen audit population.

The production safety floor cannot be lowered in PR4A. It includes leakage-safe model splits, accept precision target at least 0.98, true-ball false-reject target at most 0.01, the existing exact statistical tests and support requirements, and at most 30 review windows of 5–10 seconds. An unqualified policy remains review-only.

## Controlled Annotation

### Labels

- `match_ball`
- `player_body_or_shoe`
- `field_line_or_mark`
- `sideline_or_spare_ball`
- `equipment_or_background`
- `lighting_shadow_or_blur`
- `unknown`

The label describes the object at the candidate-box center. Only the active match ball is `match_ball`; spare and non-match balls are `sideline_or_spare_ball`. Ambiguous or occluded evidence is `unknown`, not noise.

### Blind packets and evidence binding

Each training or qualification truth requires exactly two blind primary human votes with confidence at least 0.8. A primary packet must not reveal detector confidence, model prediction, another vote, or adjudication. Unknown, disagreement, insufficient confidence, or an identity collision requires one independent human adjudication.

Each vote is bound to `dataset_version`, `sample_id`, and `evidence_sha256`. The evidence hash covers the tight tensor, context tensor, and review montage hashes for that candidate. The source map and dataset additionally bind the wide source video. If extra sequential media is used operationally, hash and retain it in the attempt record; it cannot replace the required dataset evidence.

### Append-only ledger operation

The JSONL ledger begins with one `ledger_header`; all later lines are vote events. Preserve the exact bytes of every published sequence.

```json
{"schema_version":"1.0","record_type":"ledger_header","contract_sha256":"<sha256>","dataset_version":"<version>","evidence_manifest_sha256":"<sha256>","append_only_chain":{"algorithm":"sha256-ledger-chain-v1","sequence":1,"previous_ledger_sha256":null}}
```

- Primary votes use `stage=primary`, `reviewer_type=human`, distinct `annotator_id` and distinct `fingerprint`, and `blind=true`.
- Adjudication uses `stage=adjudication`, `reviewer_type=human`, a reviewer/fingerprint distinct from both primaries, and the exact same candidate and evidence binding.
- Never edit, reorder, or delete an existing event. Append a new event, then run the resolver into a new output directory.
- A correction uses the implemented constrained adjudication-supersession contract only. It must identify an earlier adjudication vote for the same candidate, be made by the same human adjudicator, have a later timestamp, and must not fork an already superseded event. A primary vote cannot supersede another vote.
- Each published ledger sequence declares the SHA-256 of its preserved predecessor and must retain the predecessor file for validation. Sequence 1 has no predecessor. A missing predecessor, hash mismatch, changed prefix, fork, or unsupported correction remains blocked.

The local UI does not enforce reviewer identity, blindness, packet custody, or ledger append-only storage. The producer must enforce these controls outside the application and retain the assignment/audit record.

### Packet custody sequence

1. Finish and hash the dataset manifest before assigning any packet.
2. Create two separately delivered primary assignments with the same evidence coverage, randomized order, immutable packet ID, dataset version, sample ID, and evidence SHA-256. Exclude detector confidence, predictions, policy output, and all other votes.
3. Keep reviewer A and B submissions in separate access-controlled locations until both close. Record packet issue/return times and reviewer identity fingerprints.
4. Append both primary events to a new ledger sequence and run the resolver. Do not merge the assignments by editing earlier lines.
5. Export only the resulting pending-adjudication candidates to the independent adjudicator. The adjudicator may see the conflict reason but must use the same hash-bound media.
6. Append one adjudication event, publish a new ledger sequence, and resolve into a new output directory.
7. For an adjudication correction, append one constrained event with `supersedes_vote_id`; never overwrite the earlier adjudication.

A primary vote event has this minimum shape:

```json
{
  "schema_version": "1.0",
  "record_type": "vote",
  "vote_id": "<immutable-id>",
  "candidate_id": "<candidate-id>",
  "stage": "primary",
  "reviewer_type": "human",
  "annotator_id": "<trusted-reviewer-id>",
  "fingerprint": "<trusted-reviewer-fingerprint>",
  "label": "match_ball",
  "confidence": 0.95,
  "blind": true,
  "created_at": "2026-07-15T00:00:00+00:00",
  "dataset_version": "<version>",
  "sample_id": "<sample-id>",
  "evidence_sha256": "<sha256>"
}
```

There is no in-app packet issuer or identity provider in PR4A. Use an approved external custody process; if it cannot prove these controls, record `missing_human_confirmed_corpus` and stop.

## Reproducible Offline Commands

The examples below use PowerShell from the repository root. Replace the input paths with one approved attempt stored outside Git. All source videos must already be inside the prepared attempt tree before dataset construction; do not copy them later and rewrite manifests.

```powershell
$Repo = (Resolve-Path .).Path
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
$Attempt = "D:\football-evidence\attempt-2026-07-15-001"
$Dev = Join-Path $Attempt "prepared\model-development"
$Qual = Join-Path $Attempt "prepared\policy-qualification"
$Target = Join-Path $Attempt "prepared\target-application"

Set-Location (Join-Path $Repo "python_backend")
```

Classifier training and inference accept `--batch-size` values from 1 through 128 only. `128` is the hard safety maximum; `129` and larger values are rejected before model execution or tensor loading. Keep the recorded training and inference batch sizes at or below this limit. The examples below use an explicit bounded value so an operator does not depend on an implicit default.

The source-map JSON for each population uses schema version `1.0`. Every entry binds a relative `video_path`, its SHA-256, dimensions, frame count, decode mode, `variant_id`, `group_id`, `temporal_group`, `split_group`, and the exact candidate IDs. The video path must remain below the source-map directory. Identical video hashes and all derivatives of one match must retain the same group and split identities.

### 1. Build and resolve model-development truth

```powershell
& $Py scripts/build_candidate_dataset.py `
  --contract "$Dev\input\tracking_contract.v2.json" `
  --source-map "$Dev\input\source-map.v1.json" `
  --output-dir "$Dev\dataset"

& $Py scripts/resolve_candidate_annotations.py `
  --contract "$Dev\input\tracking_contract.v2.json" `
  --ledger "$Dev\input\votes.sequence-001.jsonl" `
  --dataset-manifest "$Dev\dataset\candidate_dataset_manifest.json" `
  --output-dir "$Dev\annotations" `
  --min-confidence 0.8
```

Stop unless `annotation_resolution.v1.json` reports every candidate confirmed and training-eligible, zero pending adjudications, and valid independent reviewers. Preserve the ledger, resolution, adjudication queue, and derived `tracking_contract.v2.json` together. The resolver records the chain declaration but does not by itself prove the predecessor file; the strict bundle validator later replays the ledger, verifies the predecessor hash and vote-history extension, and rejects a missing or changed predecessor. Do not treat resolver success alone as package qualification.

### 2. Train the model-development package

```powershell
& $Py scripts/train_candidate_classifier.py `
  --dataset-manifest "$Dev\dataset\candidate_dataset_manifest.json" `
  --annotation-resolution "$Dev\annotations\annotation_resolution.v1.json" `
  --contract "$Dev\annotations\tracking_contract.v2.json" `
  --output-dir "$Dev\model" `
  --batch-size 8 `
  --seed 1337
```

The new package must contain `model.pt`, `model_manifest.v1.json`, and `training_report.v1.json`. Stop unless train/calibration/test assignments are source-group disjoint, every required split has human-confirmed true-ball and noise support, calibration succeeds, and held-out gates pass.

### 3. Build separate policy-qualification evidence

```powershell
& $Py scripts/build_candidate_dataset.py `
  --contract "$Qual\input\tracking_contract.v2.json" `
  --source-map "$Qual\input\source-map.v1.json" `
  --output-dir "$Qual\dataset"

$QualOriginalContract = "$Qual\input\tracking_contract.v2.json"
$QualBoundContract = "$Qual\dataset\tracking_contract.v2.json"
if (Test-Path -LiteralPath $QualBoundContract) { throw "Qualification dataset contract binding already exists" }
Copy-Item -LiteralPath $QualOriginalContract -Destination $QualBoundContract
$QualOriginalHash = (Get-FileHash -LiteralPath $QualOriginalContract -Algorithm SHA256).Hash.ToLowerInvariant()
$QualBoundHash = (Get-FileHash -LiteralPath $QualBoundContract -Algorithm SHA256).Hash.ToLowerInvariant()
if ($QualOriginalHash -ne $QualBoundHash) { throw "Qualification dataset contract binding copy mismatch" }

& $Py scripts/resolve_candidate_annotations.py `
  --contract "$Qual\input\tracking_contract.v2.json" `
  --ledger "$Qual\input\votes.sequence-001.jsonl" `
  --dataset-manifest "$Qual\dataset\candidate_dataset_manifest.json" `
  --output-dir "$Qual\annotations" `
  --min-confidence 0.8

& $Py scripts/classify_candidates.py `
  --package "$Dev\model" `
  --dataset-manifest "$Qual\dataset\candidate_dataset_manifest.json" `
  --contract "$Qual\dataset\tracking_contract.v2.json" `
  --batch-size 128 `
  --output-dir "$Qual\predictions"

& $Py scripts/build_selective_policy_roles.py `
  --predictions "$Qual\predictions\candidate_predictions.v1.json" `
  --dataset-manifest "$Qual\dataset\candidate_dataset_manifest.json" `
  --annotation-resolution "$Qual\annotations\annotation_resolution.v1.json" `
  --resolved-contract "$Qual\annotations\tracking_contract.v2.json" `
  --model-manifest "$Dev\model\model_manifest.v1.json" `
  --training-report "$Dev\model\training_report.v1.json" `
  --output-dir "$Qual\roles"

& $Py scripts/fit_selective_policy.py `
  --predictions "$Qual\predictions\candidate_predictions.v1.json" `
  --dataset-manifest "$Qual\dataset\candidate_dataset_manifest.json" `
  --annotation-resolution "$Qual\annotations\annotation_resolution.v1.json" `
  --resolved-contract "$Qual\annotations\tracking_contract.v2.json" `
  --model-manifest "$Dev\model\model_manifest.v1.json" `
  --training-report "$Dev\model\training_report.v1.json" `
  --policy-roles "$Qual\roles\selective_policy_roles.v1.json" `
  --output-dir "$Qual\policy"
```

The qualification classifier must use the original source contract, not the annotation-derived resolved contract. The resolver records the original contract basename and SHA-256 in `annotation_resolution.v1.json`; strict qualification replay resolves that basename beside the dataset manifest. Therefore `$Qual\dataset\tracking_contract.v2.json` must remain the byte-identical, hash-checked copy created above. Roles and policy fitting intentionally continue to use `$Qual\annotations\tracking_contract.v2.json`, which is the human-resolved contract.

Stop unless `selective_policy.v1.json` has `status=qualified` and its separately recomputable calibration/audit evidence passes. An audit failure cannot be repaired by tuning the same attempt.

### 4. Apply the frozen model and policy to the target

The target tree contains only the target run source and byte-identical root tracking contract. It contains no development or qualification truth.

```powershell
& $Py scripts/build_candidate_dataset.py `
  --contract "$Target\input\tracking_contract.v2.json" `
  --source-map "$Target\input\source-map.v1.json" `
  --output-dir "$Target\dataset"

& $Py scripts/classify_candidates.py `
  --package "$Dev\model" `
  --dataset-manifest "$Target\dataset\candidate_dataset_manifest.json" `
  --contract "$Target\input\tracking_contract.v2.json" `
  --batch-size 128 `
  --output-dir "$Target\predictions"

& $Py scripts/apply_frozen_selective_policy.py `
  --policy "$Qual\policy\selective_policy.v1.json" `
  --predictions "$Target\predictions\candidate_predictions.v1.json" `
  --dataset-manifest "$Target\dataset\candidate_dataset_manifest.json" `
  --target-contract "$Target\input\tracking_contract.v2.json" `
  --model-manifest "$Dev\model\model_manifest.v1.json" `
  --output-dir "$Target\application"
```

`selective_application.v1.json` must be a truth-free recomputation from the frozen qualified policy. Its candidate IDs and fingerprints must exactly match the target contract and remain disjoint from both earlier populations.

### 5. Build the bounded target queue

Use the target application artifacts for target decisions and the three explicit qualification inputs for policy proof. Supply an FPS override when the target source manifest has no FPS.

```powershell
& $Py scripts/build_selective_review_queue.py `
  --dataset-manifest "$Target\dataset\candidate_dataset_manifest.json" `
  --predictions "$Target\predictions\candidate_predictions.v1.json" `
  --policy "$Qual\policy\selective_policy.v1.json" `
  --decisions "$Target\application\selective_application.v1.json" `
  --model-manifest "$Dev\model\model_manifest.v1.json" `
  --contract "$Target\input\tracking_contract.v2.json" `
  --annotation-resolution "$Qual\annotations\annotation_resolution.v1.json" `
  --resolved-contract "$Qual\annotations\tracking_contract.v2.json" `
  --policy-roles "$Qual\roles\selective_policy_roles.v1.json" `
  --qualification-dataset-manifest "$Qual\dataset\candidate_dataset_manifest.json" `
  --qualification-predictions "$Qual\predictions\candidate_predictions.v1.json" `
  --qualification-decisions "$Qual\policy\selective_decisions.v1.json" `
  --fps-override "target-source=20" `
  --window-seconds 10 `
  --max-windows 30 `
  --output-dir "$Target\queue"
```

Replace `target-source` with the exact target `variant_id`. Stop unless `coverage_complete=true`, `requires_additional_round=false`, `dropped=0`, the window count is at most 30, and candidate accounting reconciles across the root contract, predictions, automatic accept/reject, abstentions/review items, and exclusions.

## Bundle Assembly and Validation

The prepared bundle has three non-overlapping roots plus one bundle-root queue commit candidate. Every binding is a contained relative POSIX path. The bundle builder is the only supported publisher: it rebases producer paths into the final layout, builds the complete path/SHA-256/size inventory and reconciliation artifact, revalidates every semantic package, and atomically publishes a new output directory.

The draft manifest must bind at least:

- target run ID, source and root-contract SHA-256;
- action-signal **binding** SHA-256, confirmed config-file SHA-256, quality profile and profile digest;
- both maximum-window fields, provisioner version, candidate count and candidate-population SHA-256;
- every package artifact path and SHA-256;
- `previous_vote_ledger_path` and `previous_vote_ledger_sha256` in each development or qualification package whose current ledger sequence is greater than 1;
- `provisioning.attempt_quota_bytes` plus `provisioning.retention` with policy, retention deadline, and automatic-delete setting; and
- the queue producer directory and final queue identity required by the builder contract.

In the draft manifest, `packages.policy_qualification.source_contract_path` must point to the dataset-sibling binding copy, for example `policy-qualification/dataset/tracking_contract.v2.json`, and its `source_contract_sha256` must equal the original `$Qual\input\tracking_contract.v2.json` digest. Do not point this descriptor at the annotation-derived contract.

The two operational blocks use this shape; all sizes and hashes are attempt-specific:

```json
{
  "provisioning": {
    "attempt_quota_bytes": 10737418240,
    "retention": {
      "policy": "manual-audit-retention-v1",
      "retain_until": "2027-07-15T00:00:00+00:00",
      "automatic_delete": false
    }
  },
  "queue": {
    "source_path": "target-application/queue/selective_review_queue.v1.json",
    "source_sha256": "<producer-queue-sha256>"
  }
}
```

`attempt_quota_bytes` must be chosen from the approved capacity plan; 10 GiB above is only a shape example, not a recommended quota.

Do not substitute `action_signal_report.v1.json` for `action_signal_binding.v1.json`. The binding artifact hashes the report and the other action-signal artifacts; the two file digests are intentionally different.

```powershell
$BundleSource = Join-Path $Attempt "prepared"
$Inbox = Join-Path $Repo "python_backend\outputs\review_evidence_inbox"
$BundleStage = Join-Path $Inbox ".review-evidence-attempt-2026-07-15-001.staging"

New-Item -ItemType Directory -Force -Path $Inbox | Out-Null

& $Py scripts/build_review_evidence_bundle.py `
  "$BundleSource" `
  "$BundleStage" `
  --draft-manifest "$BundleSource\review_evidence_bundle.draft.json"
```

Record the emitted `bundle_id`, bundle-manifest SHA-256, queue SHA-256, and byte count, then record the quota and retention deadline from the validated manifest. A successful command has already rehashed and validated the published directory; do not edit it afterward.

## Server-Managed Inbox Delivery

The default server inbox is `python_backend/outputs/review_evidence_inbox`.

1. Build under a hidden direct child such as `.review-evidence-<id>.staging` on the same volume.
2. Finish and validate the entire bundle there.
3. Atomically rename that directory to its discoverable `bundle_id`.
4. Never copy a partially built directory to a visible name. Discovery ignores hidden, staging, link/reparse, non-directory, incompatible, and invalid entries.

Example final publish after the bundle builder succeeds:

```powershell
$BundleId = "review-evidence-attempt-2026-07-15-001"
$VisibleBundle = Join-Path $Inbox $BundleId

if (Test-Path -LiteralPath $VisibleBundle) { throw "Visible bundle already exists" }
Move-Item -LiteralPath $BundleStage -Destination $VisibleBundle
```

The rename must remain on one volume. Cross-volume `Move-Item` is a copy and is not an atomic publish.

## Import, Retry, and Cancellation

Start the managed UI/API, then use the API state as authoritative:

```powershell
& $Py scripts/start_ui.py
$Api = "http://127.0.0.1:8000/api/v1"
$RunId = "production_full_0276b82e-3dc5-445e-8758-e2de527ea216"

$State = Invoke-RestMethod -Method Get -Uri "$Api/runs/$RunId/broadcast/review-evidence"
$State | ConvertTo-Json -Depth 12
```

If the launcher reports a different backend port, update `$Api` before continuing.

Select one `bundles` entry with `status=available` and `capacity_status=sufficient`. Pin the exact displayed manifest SHA-256 when importing:

```powershell
$Bundle = $State.bundles |
  Where-Object { $_.status -eq "available" -and $_.capacity_status -eq "sufficient" } |
  Select-Object -First 1
if ($null -eq $Bundle) { throw "No compatible bundle with sufficient capacity" }

$Body = @{
  bundle_id = $Bundle.bundle_id
  bundle_manifest_sha256 = $Bundle.bundle_manifest_sha256
} | ConvertTo-Json

$Import = Invoke-RestMethod -Method Post `
  -Uri "$Api/runs/$RunId/broadcast/review-evidence/import" `
  -ContentType "application/json" `
  -Body $Body
$Import | ConvertTo-Json -Depth 12
```

Rendering the page or calling GET never starts an import. Poll GET through `queued`, `copying`, `validating`, and `committing`. Cancellation is allowed only while `can_cancel=true` and uses the returned child job ID:

```powershell
Invoke-RestMethod -Method Post -Uri "$Api/runs/$($Import.run_id)/cancel"
```

After a failed or cancelled attempt, retry the same content only with the exact terminal child ID returned as `retry_from_job_id`:

```powershell
$Failed = Invoke-RestMethod -Method Get -Uri "$Api/runs/$RunId/broadcast/review-evidence"
if (-not $Failed.retryable) { throw "The latest import is not retryable" }

$RetryBody = @{
  bundle_id = $Bundle.bundle_id
  bundle_manifest_sha256 = $Bundle.bundle_manifest_sha256
  retry_from_job_id = $Failed.retry_from_job_id
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "$Api/runs/$RunId/broadcast/review-evidence/import" `
  -ContentType "application/json" `
  -Body $RetryBody
```

Once state is `ready`, record the generation ID and queue SHA-256 and verify the existing consumer:

```powershell
$Ready = Invoke-RestMethod -Method Get -Uri "$Api/runs/$RunId/broadcast/review-evidence"
$Windows = Invoke-RestMethod -Method Get -Uri "$Api/runs/$RunId/broadcast/review-windows"
$Ready | ConvertTo-Json -Depth 12
$Windows | ConvertTo-Json -Depth 20
```

## Independent Activation Audit

Before PR4 resumes, an auditor distinct from the activator records:

- auditor identity and timestamp;
- target run ID, generation ID, and activated queue SHA-256;
- bundle manifest, source, root contract, action-signal binding, config/profile, model, policy, dataset, predictions, application, and reconciliation digests;
- the queue/count reconciliation result; and
- inspected queue items and media, including source frame, bbox, tight/context evidence, montage, model output, policy decision/reason, and annotation lineage.

The auditor verifies the current root queue with the existing GET review contract. Audit failure does not permit in-place edits; create a new attempt or enter incident response.

## Pre-Consumption Revocation

Revocation is allowed only before any `review_decisions.json` or downstream broadcast generation consumes the queue. Pin both the exact generation ID and queue SHA-256:

```powershell
$GenerationId = $Ready.generation_id
$QueueSha = $Ready.queue_sha256
$EscapedQueueSha = [uri]::EscapeDataString($QueueSha)
$RevokeUri = "$Api/runs/$RunId/broadcast/review-evidence/$GenerationId" +
  "?queue_sha256=$EscapedQueueSha"

Invoke-RestMethod -Method Delete `
  -Uri $RevokeUri
```

The service rejects changed identity, an active commit, or consumed evidence. Revocation removes only the root queue facade; it preserves the immutable generation, activation report, revocation report, votes, and related audit evidence.

## Incident Recovery

Never repair an incident by manually copying, editing, or deleting `selective_review_queue.v1.json`.

| Observed state | Operator action | Expected invariant |
| --- | --- | --- |
| Crash/cancel before `committing` | Restart service, GET state, then explicit retry if terminal | No root queue is consumable |
| Crash during or after commit | Restart service and GET state | Registry and facade reconcile from the immutable activation manifest |
| GET reports invalid/stale evidence | Preserve generation and logs; stop review; investigate hashes | No replacement or downstream work |
| Capacity blocker | Provision space or create a smaller newly qualified attempt | No partial generation or root queue |
| Queue ready but unaudited | Keep PR4 paused and perform independent audit | Activation alone is insufficient |
| Review or downstream generation already exists | Refuse revocation; preserve evidence and open incident | Consumed evidence is never deleted/replaced in place |

Useful service checks:

```powershell
& $Py scripts/start_ui.py --status
Get-Content "$Repo\.run\ui\backend.log" -Tail 200
& $Py scripts/start_ui.py --stop
& $Py scripts/start_ui.py
Invoke-RestMethod -Method Get -Uri "$Api/runs/$RunId/broadcast/review-evidence"
```

Hidden incomplete staging may be quarantined only after the service is stopped, the owning job is terminal, and the root queue is absent. Record original path, size, timestamps, job ID, reason, operator, and quarantine location. Do not delete a visible bundle, qualified package, activation, revoked generation, consumed generation, vote ledger, or audit record under the incomplete-staging policy.

## Capacity, Quota, and Retention

Peak-space approval includes retained generations, all three populations, the maximum staging copy, checkpoints/model package, review media/cache, and safety margin. The importer requires the bundle quota plus a concurrent staging peak and records required/free/quota/retention data in activation evidence.

Provisioner hard ceilings are 100,000 files per bundle, 256 GiB per bundle, 64 GiB per file, and 256 MiB per parsed JSON file. These are safety ceilings, not a promise that the host has enough space. The current 6,406-candidate target alone needs about 1.83 GiB for raw tight/context tensors before other populations and overhead.

Every attempt declares `provisioning.attempt_quota_bytes` and `provisioning.retention`. The accepted retention contract is `policy=manual-audit-retention-v1`, a valid `retain_until` timestamp, and `automatic_delete=false`. A quota below actual content, above the server ceiling, or without the required staging headroom blocks import. Qualified, activated, revoked, and consumed evidence is retained for audit and is never automatically removed as incomplete staging.

## Stable Blocking Outcomes

Do not publish or activate when any prerequisite is missing. Expected stable blockers include:

- `missing_human_confirmed_corpus`
- `insufficient_independent_training_groups`
- `policy_inferential_unit_infeasible`
- `review_evidence_bundle_not_available`
- `insufficient_review_evidence_capacity`

The current real target remains `not_available` with recovery action `provision_qualified_review_evidence`. PR4 resumes only after a real qualified bundle is activated, loaded by the existing consumer, count-balanced, and independently audited.

## Release-Time Command Verification

This runbook is coupled to the PR4A command/API contract. Before approving or operating the PR, rerun `--help` for every script above and export OpenAPI. Block release if the frozen-policy application script, qualification inputs for target queue construction, bundle layout rebasing, DELETE revocation route, quota/retention fields, or append-only predecessor/supersession validation is absent or differs from this runbook. Do not silently adapt commands to an older implementation.
