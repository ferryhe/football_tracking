# PR-T2 bounded real-video detector probe evidence

**Evidence status:** complete, independently auditable, exploratory, and not ground truth
**Created:** 2026-07-18 UTC
**Scope:** six fixed free COCO detector profiles on six identical source-bound frames, followed by an exact retry

This report satisfies the PR-T2 bounded real-video evidence gate. It records
what the detectors retained as mapped `sports ball` candidates. It does **not**
assert that a retained box is the match ball, that a zero-candidate frame
contains no ball, or that any model/profile is qualified for a production
trial. Human-confirmed boxes and the later feasibility and qualification gates
remain mandatory.

## Parent trial and source binding

| Field | Immutable value |
| --- | --- |
| Parent trial | `production_trial_bae9dc0c-ed98-4e55-9c89-bb0612da88ac` |
| Parent interval/outcome | frames 1500–1799; 0 detected, 0 predicted, 300 lost |
| Step-3 gate | `retune_required` (`all_candidates_class_rejected`, `all_lost`, `zero_tracklet`) |
| Base config SHA-256 | `deb81e9a30d96f12a8bb88445ec6f7283918bd494880430a93d56b26d1a939c1` |
| Effective config SHA-256 | `e94a94bce2b573eb75becfee10abdcb7b244d72fc55ba943cc70400ec15074de` |
| Trial intent SHA-256 | `1df51d9ed485357dd03ecbd9b9fdd8695728c5bf33a27e0435acb6a0b912d3ba` |
| Tuning patch SHA-256 | `d24ed1340bfb15c0161a7a0a4af8244175fb8577d9a2172368c8defac79daca1` |
| Source | `data/BXZFAuu1GQo_20260629_YRSL_U13B_LSSC_vs_DSC_PANO_5120x1440_hevc.mp4` |
| Source SHA-256 | `170ac214cdda131bc6d1178d78c7e28712d29cbd83a94ceb2fd7a995deb230f2` |
| Source identity SHA-256 | `bd4f2ec0dd80e142976c54220b2275f6eb6f51acbe6654324d2bf98b46898520` |
| Source size | 11,258,707,917 bytes |
| Source media | 5120×1440, 20 fps, 104,820 frames |
| Tracking contract SHA-256 | `3aa37142117990c58d2a407dcb635f78e1fefdb78a6154faa94f7ab4e2041cf6` |

The parent is an explicit legacy-chain restart from
`production_trial_270258b4-031f-4629-b084-ab4663ca6e49`. The older trial had no
runtime configuration digest and was not accepted as probe authority. No
historical digest was backfilled.

## Final first run and retry

Both durable jobs ended in `ready`, completed all 36 profile/frame operations,
and published 42 JPEG artifacts each: six exact source frames and 36 raw
overlays. All 84 committed JPEGs were independently opened and verified as
5120×1440, with declared byte size and SHA-256 matching the report. No gray,
low-information, corrupt, short, or wrong-index frame was accepted.

| Binding | First run | Exact retry |
| --- | --- | --- |
| Job ID | `probe-308adcc1feaa99cc-bddecb26d127` | `probe-b59e904ee0b8a14f-e2aee08f414e` |
| Retry parent | none | `probe-308adcc1feaa99cc-bddecb26d127` |
| Request SHA-256 | `308adcc1feaa99cc818ed05abba3d960ffe5a724439d2b86eddcc1b823954eb1` | `b59e904ee0b8a14fd8ee61d4d26909b2c4c08be8d2d8787687d19e3f6b2ca293` |
| Report content SHA-256 | `9f422a6b0270e7e4e933505949cab5aabc32eaa0d4e36ebe28a269bdcf083644` | `bc339d742993075849a0b892d90e36800ea7c4bcab1be0349b601056bfc67bcc` |
| Report file SHA-256 | `958d2f1b2744a657eb455d26eaa727b056ea217fceb3f0631225586f54afc92f` | `e24dba86446c8bb83453f1ce2494cff0ff0f8dd177b30de089087fa221e9621b` |
| Report file size | 107,013 bytes | 107,043 bytes |
| Manifest file SHA-256 | `cee7fe623740356457d40493d45ba98fdd109138de14bb7d73bbeeabb60bcbee` | `78258e7078c7892814891f7e5797623dfc778c601613c0ea67a7609b79af81b6` |
| Total observed time | 304.068354 s | 322.423419 s |
| Source verification stage | 14.085477 s | 1.354360 s |
| Inference stage | 286.582867 s | 317.498270 s |
| Atomic commit stage | 3.288458 s | 3.459625 s |

The first run performed exactly one full source hash in 12.769377 seconds. The
retry reused the identity-bound in-process digest cache; the hash-call list was
unchanged. The retry reproduced every retained candidate exactly. Different
report/manifest hashes are expected because job identity, retry lineage, and
timestamps differ.

Frozen frames were `1500, 1560, 1620, 1679, 1739, 1799`. Requested decode mode
was `preroll`; effective mode was `preroll_verified`, with all requested frame
positions verified.

## Code and runtime provenance

| Field | Immutable value |
| --- | --- |
| Code commit | `7b8d45e121184f6bebe22e9a1fed4c9dca8a3a50` |
| Code commit status | `bound` |
| Binding kind | `exact_or_crlf_to_lf_commit_blob` |
| Runtime worktree code bundle SHA-256 | `aa23d207df609403a6abde6de3435914ed4c27bc151498069c2821fa3a6ef5e1` |
| Commit blob bundle SHA-256 | `4d381c6de23caa97e08fb6602e6876d425f2f16043d95aacb8b912eb4f59c59e` |
| Files in each code map | 17 |
| Raw/blob pairs differing only by CRLF→LF | 12 |
| Execution bundle SHA-256 | `fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494` |
| Runtime environment SHA-256 | `6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3` |
| Decoder fingerprint SHA-256 | `83a04695979f9fe8198ec7d655dda450bad9a9c2dfac17678ce5e754f30e5a43` |
| Execution | local `cuda:0`, FP32, NVIDIA GeForce RTX 5080 |
| Installed runtime | Ultralytics `8.4.31`, SAHI `0.11.36`, Torch `2.9.1+cu130` |
| Schema runtime | Pydantic `2.12.5`, Pydantic Core `2.41.5` |
| Python/media runtime | CPython `3.11.9`, NumPy `2.4.3`, OpenCV `4.11.0` |

`code_bundle_files` hashes the exact raw bytes executed from the Windows
worktree. `code_commit_blob_files` separately hashes the exact Git blobs at the
declared commit. A `bound` result is allowed only when every pair is byte equal
or differs solely by CRLF→LF normalization. Arbitrary Git clean filters are not
trusted. All inherited `GIT_*` variables are removed, the reported repository
root must match the trusted root, and the worktree, status, and HEAD are checked
again before binding. The two 17-file maps, their aggregates, the binding kind,
and the commit are included in the execution and runtime-environment digests.

The local code/contract review also verified that `unbound` and `unavailable`
states must explicitly carry `null` commit-blob evidence; missing fields cannot
be silently interpreted as proof.

## Exact model identities

| Model | Version | Weights SHA-256 | Bytes | Profiles |
| --- | --- | --- | ---: | --- |
| Current COCO YOLOv8n diagnostic baseline | `yolov8n-coco-2022-12-30` | `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36` | 6,549,796 | direct, SAHI |
| Official COCO YOLO11n | `yolo11n-coco-v8.4.0` | `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` | 5,613,764 | direct, SAHI |
| Official COCO YOLO11s | `yolo11s-coco-v8.4.0` | `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5` | 19,313,732 | direct, SAHI |

No weight, generated probe result, or source media is committed to Git.

## Fixed profile results

All profiles map the COCO `sports ball` class to `ball`, use confidence 0.05,
input size 1280, and a top-five retained-candidate cap. SAHI profiles use
1280×720 slices, 0.2 overlap, IOS 0.5, and NMS. Latency is exploratory wall
time on these runs and is not a benchmark.

| Profile | Profile SHA-256 | Candidates first/retry | Frames first/retry | Mean ms first/retry |
| --- | --- | ---: | ---: | ---: |
| `current-coco-yolov8n-direct` | `22260749012e58ddad183ade4f0dda0a98ca6b7c8a12cf97cd5bbd6fad9c6adb` | 0 / 0 | 0/6 / 0/6 | 371.102 / 290.447 |
| `current-coco-yolov8n-sahi` | `906ce224cd36ed59310ee054ffde71b59cde2c05b31be3266ef120e877e9b473` | 0 / 0 | 0/6 / 0/6 | 440.360 / 428.734 |
| `official-coco-yolo11n-direct` | `e5f85654047e8b772f68027d815d9207c0853508d08322f48762af08a5a8b6d5` | 0 / 0 | 0/6 / 0/6 | 36.865 / 27.226 |
| `official-coco-yolo11n-sahi` | `4431b7e35a28052ff1cb983781892fcdc20d26f469ccc9ba54be96f566e48421` | 0 / 0 | 0/6 / 0/6 | 441.302 / 448.923 |
| `official-coco-yolo11s-direct` | `97bef090f295df4e5f9db56786057c4623816db3b9d63706d9c4fb1276b50039` | 0 / 0 | 0/6 / 0/6 | 57.284 / 58.010 |
| `official-coco-yolo11s-sahi` | `97a3fd5922b18a6817f78dfd827ab8bfbeeeaf276438879dcf7381e168bac218` | 2 / 2 | 2/6 / 2/6 | 437.896 / 480.279 |

The two exactly reproduced YOLO11s + SAHI outputs were:

| Frame | Confidence | Source-pixel XYXY box | Evidence meaning |
| ---: | ---: | --- | --- |
| 1620 | 0.248639 | `[3823.5365, 861.0009, 3832.3195, 870.8508]` | Unconfirmed detector suggestion |
| 1799 | 0.056744 | `[4046.7306, 848.3883, 4055.0638, 856.1389]` | Unconfirmed detector suggestion |

Both boxes are roughly 9×9 source pixels in crowded, distant play areas.
Visual inspection alone does not convert either into a positive label. They
may enter PR-T3 only as suggestions, retaining source frame, coordinates,
profile digest, and provenance for human confirmation.

## Decision

1. The six-profile comparison is operational and reproducible on the real HEVC
   source. Exact-frame decode, profile freezing, local GPU execution, bounded
   top-k overlays, atomic publication, retry, and provenance all worked.
2. YOLO11s + SAHI was the only profile to retain mapped candidates in this
   six-frame exploration. It is an annotation-suggestion source, not a
   qualified detector and not proof that YOLO11 is generally better.
3. Zero means only “no retained candidate under this frozen profile.” It never
   means that the frame contains no ball.
4. PR-T3 should present the two boxes for confirmation and collect additional
   point/box evidence. These six exploratory frames cannot satisfy the later
   stratified 20–50-frame feasibility protocol.
5. The all-lost parent remains `retune_required`; this probe must not enable
   “Accept this trial.”

## Fail-closed development history

Earlier attempts are historical diagnostics, not accepted evidence. They
exposed and led to fixes for Windows worker containment, sibling heartbeat
directory churn, atomic heartbeat sharing violations, incomplete code/runtime
lineage, CRLF-vs-commit ambiguity, and quarantine-watcher lease cleanup. The
previously audited `c34d2d0` first/retry pair was superseded after the official
full suite exposed the close/lease race; the final pair above reran unchanged
model/frame evidence against the repaired `7b8d45e` execution code. Jobs
created before the final provenance schema—including the earlier `a1946a9`
pair—are also excluded. No failed or partial attempt published selectable
evidence, and no integrity or process-containment check was weakened to obtain
the final result.
