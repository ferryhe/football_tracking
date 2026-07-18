# Detector Model Acquisition Dossier

**Contract:** `ball_detector_development_v1`
**Scope:** bounded, local Step 3 feasibility probes only
**Decision:** the current COCO YOLOv8n diagnostic baseline and the official COCO YOLO11n/YOLO11s assets are the only initial executable candidates. Public football projects remain visible, unavailable findings until an exact version, weight identity, access route, and four-layer license review are complete.

This dossier records acquisition facts. It does not claim that a generic `sports ball` detector works on the 5120×1440 panoramic source, make a model trial-eligible, or approve any production deployment.

## 1. Fixed initial catalog

| Model ID | Role | Architecture | Asset identity | Expected local file | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | ---: | --- |
| `current-coco-yolov8n` | Existing diagnostic floor | YOLOv8n | workspace baseline; checkpoint metadata `8.0.0.dev0`, 2022-12-30 | `python_backend/weights/football_ball_yolo.pt` | 6,549,796 | `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36` |
| `official-coco-yolo11n` | Free speed comparator | YOLO11n | Ultralytics assets `v8.4.0`; checkpoint metadata `8.2.100`, 2024-09-25 | `python_backend/weights/yolo11n.pt` | 5,613,764 | `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` |
| `official-coco-yolo11s` | Free initial quality comparator | YOLO11s | Ultralytics assets `v8.4.0`; checkpoint metadata `8.2.100`, 2024-09-25 | `python_backend/weights/yolo11s.pt` | 19,313,732 | `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5` |

The asset release, checkpoint metadata, model family, installed runtime version, and registry schema version are separate identities. They must not be collapsed into one ambiguous “YOLO version”. All three checkpoints use the COCO 80-class map; class index 32, `sports ball`, is explicitly mapped to the product label `ball`.

Each model has two immutable profiles:

- `<model-id>-direct`: direct Ultralytics inference, source-pixel boxes, fixed top-5 evidence.
- `<model-id>-sahi`: SAHI tiled inference, source-pixel boxes, fixed top-5 evidence, FP32 execution.

`official-coco-yolo11s-sahi` is a recommended *probe* profile. “Recommended” is not qualification. Every initial descriptor starts `unverified`, with `trial_eligible=false`, `source_segment_qualified=false`, and `camera_qualified=false`.

## 2. Repeatable acquisition and runtime proof

From the repository root, use the repository Python environment:

```powershell
.venv\Scripts\python.exe python_backend/scripts/acquire_detector_models.py official-coco-yolo11n official-coco-yolo11s
.venv\Scripts\python.exe python_backend/scripts/observe_detector_model_runtime.py current-coco-yolov8n official-coco-yolo11n official-coco-yolo11s
```

The command accepts fixed catalog IDs only. It does not accept a caller-supplied URL or destination. It downloads to a private temporary file in the ignored `python_backend/weights/` root, stops at the first byte beyond the pinned size, then reads exactly the expected bytes plus an EOF check. It validates regular-file identity, exact byte count, and SHA-256 before and after atomic publication and fails closed on identity drift. A valid existing file is reused; a mismatched file is not overwritten or registered.

Runtime qualification is an explicit controlled operation, not a side effect of `GET /api/v1/detector-models`. For each of the three fixed built-ins it must:

1. Copy the exact registered bytes into a unique private runtime snapshot while validating source identity, byte count, and SHA-256.
2. Launch a bounded subprocess with the repository interpreter and give it only that snapshot path.
3. Load the checkpoint with Ultralytics and SAHI and run a 64×64 local smoke image in each runtime, then revalidate both the snapshot and original source before publishing evidence.
4. Read and verify the checkpoint class map contains `sports ball`.
5. Persist an atomic observation bound to the weight digest, runtime-contract digest, and installed Ultralytics, SAHI, and Torch versions.

The ignored observation path is:

```text
python_backend/data/ball_detector_development_v1/model_observations/
```

Missing, stale, corrupt, or runtime-mismatched observation evidence leaves that model and its profiles `unavailable`; catalog reads never trigger multi-minute model loading. Imported weights cannot use this built-in observation route.

Official acquisition URLs are pinned, not `latest` aliases:

- [YOLO11n `v8.4.0` asset](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt)
- [YOLO11s `v8.4.0` asset](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11s.pt)
- [Ultralytics YOLO11 model documentation](https://docs.ultralytics.com/models/yolo11/)
- [Ultralytics SAHI tiled-inference documentation](https://docs.ultralytics.com/guides/sahi-tiled-inference/)

Weights and runtime observations are local ignored artifacts and must never be committed to Git.

## 3. License and egress review

The registry keeps four decisions separate:

| Layer | Initial finding | Step 3 local-probe decision |
| --- | --- | --- |
| Dataset | COCO images have source-specific terms recorded through the [COCO terms of use](https://cocodataset.org/#termsofuse); do not describe the whole dataset as one simple license | Metadata reviewed for the bounded local probe |
| Model/weights | Ultralytics publishes AGPL-3.0 and Enterprise routes in its [license terms](https://www.ultralytics.com/license) | AGPL local evaluation accepted for this development scope only |
| Runtime | Ultralytics AGPL-3.0, SAHI MIT, Torch BSD-3-Clause | Installed versions and license metadata recorded in the runtime observation |
| Intended deployment | Private/commercial distribution and service use require a separate release-time review | Not approved by this dossier |

The three initial candidates execute locally. `frames_leave_local_machine=false`, there is no external destination, and no frame-transfer consent is needed. A hosted API adapter must remain disabled until its destination, data handling, account/plan, license, and explicit operator consent are recorded.

## 4. Public football catalog findings

The following projects are discovery evidence, not selectable models:

| Finding | Exactness/access status | License/egress status | Catalog result |
| --- | --- | --- | --- |
| [Roboflow `soccer-ball-detection-s2sg3` version 3](https://universe.roboflow.com/soccerdata-cnauk/soccer-ball-detection-s2sg3/model/3) | Project and version identified; exact local weight bytes and account/plan route not validated | Dataset, model, runtime, deployment, and possible frame egress require review | `unavailable`; no profiles |
| [Roboflow `football-players-detection-3zvbc`](https://universe.roboflow.com/roboflow-jvuqo/football-players-detection-3zvbc) | No exact project version/architecture is bound | Exact access, all license layers, and egress unknown | `unavailable`; no profiles |

The UI may explain these findings, but it must not render a selection control, invent a digest, imply that weights were downloaded, or copy publisher metrics into our qualification result.

## 5. Trusted import boundary

`POST /api/v1/detector-models/import` is for a server-controlled package below the configured trusted import root. It is not a general upload or download endpoint.

An import is rejected unless all of the following hold:

- The package and every referenced file use safe relative paths under the trusted root; path segments reject traversal, control/unsafe Unicode, Windows reserved names, trailing dots/spaces, and alternate-data-stream syntax.
- At each open, hash, copy, and publication checkpoint the implementation rejects observed symlinks, junctions/reparse points, special files, and ancestor identity drift. This is a fail-closed application boundary, not a hostile same-user filesystem sandbox and not a guarantee against an arbitrary privileged path replacement after the final check.
- The source manifest binds an exact model ID/version, weight relative path and SHA-256, class names and explicit `ball` mapping, execution envelope, source/access identity, four license layers, and egress/consent state. The observed byte count is added to the immutable published descriptor. Catalog reads always revalidate the complete package membership and cheap file/directory change identities. A bounded 64-entry LRU cache may then return only a private copy of a previously fully verified record; any membership or identity drift invalidates the entry and forces a complete byte/digest verification before reuse. Adding or removing an unrelated sibling model/version does not rehash unchanged weights.
- Weight and manifest identities remain unchanged across open, hash, validation, and atomic registry publication.
- No arbitrary URL, Python module, repository code, or custom executable model implementation is accepted. A `.pt` file can contain pickle-based, potentially executable untrusted bytes: import and catalog reads never deserialize it, keep it blocked, and do not treat SHA-256 as a safety proof. Any future imported-weight runtime validation must run in a separately approved isolation boundary; ONNX or safetensors should be preferred where practical.

A structurally valid import remains `unverified` and unavailable with `server_validation_required`. The API process does not execute imported model bytes. A later controlled validation job may create independent runtime evidence and a new immutable registry record.

## 6. Operational acceptance checklist

- Exact constants are covered by offline unit tests; CI never downloads a model and never requires ignored real weights.
- Acquisition rejects unknown IDs, size/digest mismatches, unsafe roots, concurrent publication conflicts, and hash-to-publish replacement.
- Each available profile has passing file, digest, class-map, license, and mode-specific runtime-load observations.
- Catalog descriptor/profile digests do not change when installed runtime versions change; availability observations do.
- A probe freezes two to six exact profile IDs and digests, one trusted parent trial lineage, one source/contract/config lineage, one sorted frame set, and fixed top-5 output.
- Real-video evidence is bounded, reports honest zero, partial, or all-failed outcomes, and never promotes a model or starts a full-video run.
- Probe execution has one global slot per development root, a 1.2 GB decoded-frame envelope, and a 256 MB unique-weight snapshot budget. Each probe runs in a supervised subprocess with a 20-minute wall deadline, a 10-second heartbeat timeout, a 1-second cooperative-cancel grace, a 3-second terminate grace, and up to 5 seconds to verify forced tree termination. POSIX uses a new process group plus a parent-watch pipe and worker parent-death handling; Windows attaches the worker tree to a kill-on-close Job Object. Shutdown does not claim new queued work and requeues an interrupted running job without recording operator cancellation. If containment cannot be attached or complete process-tree death cannot be confirmed, staging and the global execution lease are quarantined until the child is observed exited; the service never cleans or reuses that execution boundary optimistically. These controls are an application boundary and still depend on the host OS and service account retaining the required process-control privileges.
