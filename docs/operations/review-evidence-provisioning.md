# Review Evidence Provisioning Runbook

## Status

This runbook describes the approved PR4A workflow. It is not operational until the PR4A API, bundle validator, deterministic fixture, and rollback tests have merged.

The online service imports and activates an already qualified, self-contained evidence bundle. It does not create human truth, train a model, lower policy thresholds, or accept arbitrary local paths.

## Roles

- Producer: builds the offline evidence packages and bundle.
- Primary reviewer A and B: independently label blinded work packets.
- Adjudicator: resolves conflicts and is distinct from both primary reviewers.
- Activator: imports a qualified bundle for one target run.
- Auditor: visually audits the activated generation and is not its activator.

Reviewer identities and fingerprints must come from a trusted operating process. Client-supplied aliases alone do not establish independent reviewers.

## Evidence Populations

Keep these populations mutually exclusive:

1. Model development: train, temperature calibration, and held-out test.
2. Policy qualification: previously unseen policy calibration and policy audit.
3. Target application: only the production run whose queue will be activated.

Alternate encodes, resolutions, crops, and temporal excerpts of the same match retain the same match/source-group identity. The target application population never contributes to training, calibration, threshold selection, or qualification audit.

## Annotation Protocol

Use the existing seven labels:

- `match_ball`
- `player_body_or_shoe`
- `field_line_or_mark`
- `sideline_or_spare_ball`
- `equipment_or_background`
- `lighting_shadow_or_blur`
- `unknown`

The label describes the object at the candidate-box center. Only the active match ball is `match_ball`; a spare or non-match ball is `sideline_or_spare_ball`. Occluded or ambiguous evidence is `unknown`, not noise.

Each training or qualification truth requires two blind human primary votes with confidence at least 0.8. A primary packet must not reveal detector confidence, a model prediction, another vote, or adjudication. Disagreement, unknown, or insufficient confidence requires an independent human adjudicator. Corrections append a superseding event and never rewrite the original ledger.

All media used to decide a label must be hash-bound, including the wide source frame, candidate bbox, five-frame tight/context evidence, and any sequential context clip.

## Qualification Protocol

Before opening the final audit population, freeze and hash:

- taxonomy and annotation instructions;
- source-group inventory and population assignments;
- sampling and class-support rules;
- model and policy configuration;
- metrics and promotion thresholds;
- reviewer roles; and
- retention and capacity plan.

Do not tune an attempt after observing its audit result. A protocol or threshold change creates a new attempt with a previously unseen audit population.

The production safety floor cannot be lowered inside PR4A. It includes leakage-safe model splits, accept precision target at least 0.98, true-ball false-reject target at most 0.01, the existing exact statistical tests, the existing audit/support requirements, and at most 30 review windows of 5–10 seconds. An unqualified policy remains review-only and cannot be activated as a qualified target queue.

## Bundle Construction

The bundle contains separate model-development, policy-qualification, and target-application packages. It also contains the target queue, review timing, activation inputs, and a complete path/SHA-256/size inventory.

Requirements:

- Every path is relative to and contained by the bundle root.
- Absolute paths, `..`, links, junctions, and reparse points are prohibited.
- The target source copy, root tracking contract, action-signal binding, configuration/profile, and maximum review windows match the target run exactly.
- The target application dataset contains exactly the target source.
- Queue bindings separately identify qualification evidence and application dataset/predictions/decisions.
- Coverage is complete, no additional round is required, no candidate is dropped, and the queue has at most 30 windows.

Build against the source located inside the bundle from the beginning. Do not copy a path-bound dataset afterward and edit its manifest.

## Inbox Delivery

1. Write the bundle under a hidden staging name inside the server-managed inbox.
2. Finish every artifact and the manifest inventory.
3. Rehash and validate the complete bundle.
4. Atomically rename the staging directory to the discoverable `bundle_id`.

The service ignores hidden or incomplete directories. Never deliver through an arbitrary path supplied by an API client.

## Managed Import

1. Read the target run's review-evidence state.
2. Select a compatible `bundle_id` and verify its displayed manifest SHA-256.
3. Explicitly start import. Rendering the page must not start it.
4. Monitor queued, copying, validating, and committing stages.
5. Cancellation is allowed before commit and rejected after commit begins.
6. After ready, verify that the existing review-window endpoint loads the queue and all counts reconcile.
7. Record an independent visual audit with auditor identity, time, generation ID, queue SHA-256, and inspected media.

PR4 resumes only after the target queue is activated, validated by the existing consumer, count-balanced, and independently audited.

## Activation and Recovery

- Import copies to staging and rehashes every file.
- The immutable generation directory and activation manifest are complete before activation.
- The root `selective_review_queue.v1.json` is created exclusively and is the only commit marker.
- Activation, review submission, and revocation share the target-run mutation lock.
- A crash before the commit marker exposes no queue. A crash after it reconciles registry/facade state from the generation manifest.
- Activation changes only review-evidence blockers. Terminal-tail acknowledgement, unrelated blockers, and existing limitations remain unchanged.

## Rollback

Before any review decision or trajectory generation consumes a queue, revocation is permitted only while holding the target-run lock and only if the activation manifest and queue SHA-256 still match.

After consumption, do not delete or replace evidence in place. Create a new generation or enter incident response. Preserve votes, model and policy evidence, activation reports, consumed generations, and audit records.

## Capacity and Retention

Peak-space preflight includes:

- retained generations;
- all three evidence populations;
- the maximum staging copy;
- checkpoints and model packages;
- review media/cache; and
- a safety margin.

The current 6,406-candidate application dataset alone needs about 1.83 GiB for raw tight/context tensors. Plan substantially more for the complete bundle. Enforce an attempt quota, clean only incomplete/cancelled staging according to policy, and retain every activated or consumed generation.

## Stable Blocking Outcomes

Do not publish a queue when any prerequisite is missing. The current real target is expected to report blockers equivalent to:

- `insufficient_independent_training_groups`
- `policy_inferential_unit_infeasible`
- `missing_human_confirmed_corpus`
- `missing_qualified_review_evidence_bundle`

Never replace these with an empty queue, default acceptance, synthetic labels, dummy model/policy, or one-hot values presented as model probabilities.
