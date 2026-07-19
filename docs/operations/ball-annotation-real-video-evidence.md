# PR-T3 real-video annotation and feasibility evidence

**Evidence status:** sealed, fail-closed, and independently auditable
**Created:** 2026-07-19 UTC
**Scope:** six development frames followed by one frozen, disjoint 20-frame check attempt on the real 5120×1440 source

This report satisfies the PR-T3 real-video annotation gate. It records human
point/box decisions, every detector-candidate decision, the immutable package
bindings, and the resulting directional feasibility decision. The outcome is
`insufficient_evidence`. It does not authorize training, dataset expansion,
another trial, a full run, camera qualification, or production use.

## Source and detector binding

| Field | Immutable value |
| --- | --- |
| Parent trial | `production_trial_bae9dc0c-ed98-4e55-9c89-bb0612da88ac` |
| Source | `data/BXZFAuu1GQo_20260629_YRSL_U13B_LSSC_vs_DSC_PANO_5120x1440_hevc.mp4` |
| Source SHA-256 | `170ac214cdda131bc6d1178d78c7e28712d29cbd83a94ceb2fd7a995deb230f2` |
| Source identity SHA-256 | `bd4f2ec0dd80e142976c54220b2275f6eb6f51acbe6654324d2bf98b46898520` |
| Source size/media | 11,258,707,917 bytes · 5120×1440 · 20 fps · 104,820 frames |
| Locked profile | `official-coco-yolo11s-sahi` |
| Locked profile SHA-256 | `97a3fd5922b18a6817f78dfd827ab8bfbeeeaf276438879dcf7381e168bac218` |
| Locked weights SHA-256 | `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5` |
| Control profile | `current-coco-yolov8n-direct` |
| Control profile SHA-256 | `22260749012e58ddad183ade4f0dda0a98ca6b7c8a12cf97cd5bbd6fad9c6adb` |

The development lineage consumes the audited T2 probe and its verified
review-proxy child. The check probe is a server-authorized full-source exact
frame request bound to the already-persisted sampling lock and manifest. The
public detector-probe API remains restricted to the parent trial interval.

## Development package

| Field | Immutable value |
| --- | --- |
| Session | `annotation-86e4908c1e91588a-4a91cc571805` |
| Sampling manifest SHA-256 | `5a1685f85a66a610d9a1e8018e59672994a09ddae4d8b0169be3565b7944a4b1` |
| Package SHA-256 | `6a5005c89b0d09e7d6e9b4a5dd8192d9398f1a2b7f10814c65825642d1f42558` |
| Report SHA-256/status | `2a48e5da6d4a103211f4b07b5db454f4c291b3c15b62f40851c916c29a9777db` · `not_applicable` |
| Frame evidence SHA-256 | `1e9315de73fd0c7e2cf1f7d5fc6e142015e2f5ca1de1cbf7f9554e1de1fe05e1` |
| Frame media SHA-256 | `c60dc21964fc1cfa69a72a209f06809306c141cbf971845b72234aaf53d82499` |
| Candidate evidence SHA-256 | `f5225323c369f92382bcd52016fceedf685fbca4864da0101ff998b7cbf9ffa9` |
| Human result | 4 localizable positives; 2 unknown/unresolvable |
| Dataset state | `may_seed_dataset_expansion=true`; `training_eligible=false` |

Frames 1560 and 1679 were manually boxed, frames 1620 and 1799 accepted
exact detector candidates, and frames 1500 and 1739 remained
`unknown/unresolvable`. The package is eligible only as safe development seed
data. It is not training truth or an expansion authorization without a passing
disjoint check package.

## Frozen check attempt

| Field | Immutable value |
| --- | --- |
| Session | `annotation-91e658c38a2b1796-7ad7b9ba3fcb` |
| Sampling manifest SHA-256 | `14ce95b8ae0e4a5da637bae67d2438fa2b60ab21a16582595303c51561847aec` |
| Check probe | `probe-d3f09467b0a80242-33491c95816e` |
| Check probe report SHA-256 | `8c716cf9400df1eef82d0327f54febae3d14f55eb52884106fc946fc2919ea45` |
| Check runtime environment SHA-256 | `c3f214b7d695cede1aed1698264421e7b1f6f373eb618a87b784fd381aa3dc9a` |
| Package SHA-256 | `530b732d228b37b1e592ce2ef8b652dad125126185fa113ca9d5ec6b1265bbad` |
| Report SHA-256/status | `c359a269c6e7df15940d82875c8b8fab5f9d71ed1b182371df4ae8b00ab74f8e` · `insufficient_evidence` |
| Final-result manifest SHA-256 | `367a2bc8e63779ebf7c376ea06cb107a442c47c0f272e6136d4ebf4bdf081d22` |
| Frame evidence SHA-256 | `7523d0daae2092f93dd514ca90f9b49ee7f918f6d19a57228fc7200d797ebace` |
| Frame media SHA-256 | `edb8c6abeacbc1502460adfada816dfcf862fa9b90914c74d0c6dda4b195f25d` |
| Candidate evidence SHA-256 | `10fff3c11fdc155e3c271a9b22e02e8f1ba11502aa12e4ad0f138979f59de69a` |

The exact frozen frames were `1704, 6246, 13960, 20738, 26084, 27493,
34265, 41296, 43962, 52346, 55460, 61815, 64354, 70557, 78287, 79707,
85379, 92827, 97814, 101140`. Every committed JPEG is 5120×1440 and is
re-read from the sealed result tree against its declared SHA-256 and size.

### Human review decisions

`unknown/unresolvable` means the single frame did not support a reliable ball
location. It never means confirmed absence and never becomes a negative
training example.

| Frame | Final human annotation | Detector-candidate decision | Visual basis |
| ---: | --- | --- | --- |
| 1704 | unknown/unresolvable | no candidate | crowded goal-mouth/occlusion |
| 6246 | unknown/unresolvable | no candidate | shoe, sock, and line ambiguity |
| 13960 | unknown/unresolvable | dismiss 1 | irregular foreground grass/debris |
| 20738 | unknown/unresolvable | no candidate | no independently localizable ball |
| 26084 | unknown/unresolvable | no candidate | dense leg/foot overlap |
| 27493 | unknown/unresolvable | no candidate | likely occluded or outside frame |
| 34265 | unknown/unresolvable | dismiss 1 | center-line/circle paint intersection |
| 41296 | present · mid · partial · ground · bbox `[2321,868,2337,884]` | detector missed; manual box | ball between player 18's feet, partly leg-occluded |
| 43962 | unknown/unresolvable | no candidate | no independently localizable ball |
| 52346 | unknown/unresolvable | no candidate | action at/right of frame edge |
| 55460 | present · far · visible · ground | accept candidate 1 exactly | clear black/white ball |
| 61815 | unknown/unresolvable | no candidate | single frame needs temporal context |
| 64354 | unknown/unresolvable | no candidate | single frame needs temporal context |
| 70557 | present · far · partial · ground · bbox `[1753,828,1764,840]` | dismiss all 3; manual box | candidates were shoe, leg, and field line; true ball was elsewhere |
| 78287 | present · far · visible · ground | accept candidate 1 exactly | clear ball at ground level |
| 79707 | unknown/unresolvable | no candidate | crowded/occluded play |
| 85379 | unknown/unresolvable | dismiss all 3 | shoe, leg, and background equipment |
| 92827 | present · far · visible · airborne | accept candidate 1; dismiss candidate 2 | low airborne ball; false candidate was player body part |
| 97814 | unknown/unresolvable | no candidate | no independently localizable ball |
| 101140 | present · human label far · visible · airborne | accept candidate 1; dismiss candidate 2 | true ball accepted; second candidate was spectator equipment |

Across all 20 frames, the immutable annotation package contains 6 present and
14 unknown/unresolvable decisions. All 14 detector suggestions are resolved:
4 accepted and 10 dismissed. There are no confirmed-absent frames.

The metric profile derived frame 101140's 32.45-pixel box diagonal as `mid`,
while the human annotation labeled it `far`. The sealed report publishes this
as `scale_stratum_mismatch:far:mid`, excludes that frame from evaluable support,
and requires a new unseen attempt rather than rewriting the finalized package.
Consequently the report has 5 evaluable localizable positives: mid 1, far 4,
near 0.

## Feasibility result

| Metric | Frozen result |
| --- | --- |
| Top-1 recall | 3/5 = 0.60; one-sided 95% lower bound 0.2725 |
| Top-5 recall | 3/5 = 0.60; one-sided 95% lower bound 0.2725 |
| False candidates/evaluable frame | 4/5 = 0.80; one-sided 95% upper bound 3.5367 |
| Candidates/evaluable frame | 7/5 = 1.40 |
| Required support | missing localizable-positive, confirmed-absent, near, and sufficient mid support; one scale mismatch |
| Resolution | `requires_new_attempt=true`; reason `scale_strata_mismatch` |

Every authorization is `false`:

- `may_expand_to_100_300_boxes`
- `trial_eligible`
- `source_segment_qualified`
- `camera_qualified`
- `production_approved`
- `full_run_authorized`

PR-T4 therefore remains fail-closed. The development package may remain as
retired exploratory seed evidence, but this check result does not authorize a
100–300-box expansion or training project.

## Finalization recovery finding

The first finalization attempt correctly stopped because its frame-evidence
verifier compared check-probe frame runtime identity with the older development
lineage runtime. The implementation now selects the exact validated check job's
`check_probe_authority.runtime_environment_sha256` for check-frame evidence,
while development packages continue to use their development lineage runtime.
The complete check authority is independently rebuilt from the durable job and
matched on job, request, intent, report, result manifest, execution bundle,
runtime environment, and frozen profiles before this value is trusted.

A regression test finalizes a check whose runtime and execution bundle differ
from its development package, then re-verifies every sealed frame row against
the check runtime. The persisted `finalizing` transaction recovered after
restart and published the same immutable annotation revisions without data
loss or manual state editing.

## Decision

1. Source-bound point/box annotation, exact candidate accept/dismiss decisions,
   immutable frame media, revision chains, and one-time feasibility reporting
   work on the real HEVC source.
2. YOLO11s + SAHI localized 3 of 5 evaluable positives in its top five, missed
   two human-localized balls, and produced false candidates including shoes,
   legs, field paint, player body parts, and spectator equipment.
3. This 20-frame check does not meet support or interval gates. No threshold is
   weakened and no unknown frame is converted into a false negative.
4. Any future check must use a new frozen unseen temporal group and correct
   apparent-size labels before finalization. The sealed attempt remains
   immutable evidence of the measured insufficiency.
