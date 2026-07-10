# High-Resolution Football Ball Tracking — Backend

English | [中文](#中文说明)

> **Replit deployment note**: This README is the original upstream README and describes a self-contained Windows-based workflow with the legacy `frontend/` desktop UI. On Replit, **the frontend in this folder has been replaced** by [`artifacts/web/`](../artifacts/web/) (a React/Vite app served via a Node.js reverse proxy). For Replit-specific setup, architecture, and run instructions, see the **[root README](../README.md)**. The Python tracking pipeline remains compatible with the original flow, while this workspace adds API reports, review artifacts, and render jobs.

---

This repository tracks a single in-play football from high-resolution fisheye-style match video and provides a local workspace UI for baseline runs, AI-assisted tuning, deliverable rendering, and history management.

## English

### What This Repo Includes

- Python tracking pipeline for raw tracking, cleanup, and follow-cam rendering
- Local FastAPI backend for configs, runs, artifacts, AI suggestions, and asset management
- Local React/Vite workspace UI with 5 main tabs:
  - `Dashboard`
  - `Baseline`
  - `AI analysis`
  - `Deliverable task`
  - `History`
- Managed Windows launcher scripts for one-click local startup

### Recommended Starting Configs

- `config/real_first_run.yaml`
  - Best for short probe runs and first-pass tuning
- `config/real_best_full.yaml`
  - Best current full-video raw tracking config
- `config/real_v24_full_postclean.yaml`
  - Best current full-video delivery config with cleanup and follow-cam enabled

### Environment

- Windows 10 / 11
- Python 3.10 or 3.11
- NVIDIA GPU recommended
- CUDA and cuDNN installed correctly
- Node.js and `npm` available in PATH

### Detector Weights

This is critical for the first successful run.

- All shipped YAML configs default to:
  - `detector.model_path: "./weights/football_ball_yolo.pt"`
- That path is resolved relative to the repo root.
- The default expected file is:
  - `weights/football_ball_yolo.pt`
- The `.pt` file must be an Ultralytics YOLO detection checkpoint.
  - Good: detect model weights exported for Ultralytics YOLO
  - Not suitable: classification, segmentation, pose, or OBB checkpoints
- The filename does not have to stay `football_ball_yolo.pt` if you update `detector.model_path` in the YAML.
- Default configs accept labels `sports ball` and `ball`, so your model should emit one of those labels or you should update `detector.allowed_labels`.
- If you run on CPU only, set:
  - `detector.device: "cpu"`
  - `detector.use_half: false`

If the weight file is missing, the baseline run will fail before detection starts.

### Quick Start

1. Create and activate a virtual environment.
2. Install Python dependencies.
3. Install frontend dependencies.
4. Start the managed local UI.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt

cd frontend
npm install
cd ..

.\start_ui.cmd
```

If you want the steadiest backend startup on Windows:

```powershell
.\start_ui.cmd --no-reload
```

To stop managed UI processes:

```powershell
.\stop_ui.cmd
```

### Workspace Flow

1. `Baseline`
   - Pick a source video from `data/`
   - Pick a baseline config
   - Capture a preview frame
   - Load field setup from config or ask AI for a suggestion
   - Accept the field setup
   - Start a baseline run
2. `AI analysis`
   - Select a finished run tied to the current source clip
   - Trigger AI explanation manually
   - Review the suggested config
   - Run the next task if the suggestion looks right
3. `Deliverable task`
   - Pick a source video and tuned config to run the full video
   - Optionally post-process the ball track and render the final `16:9` follow-cam output
   - Pick a completed run with `event_candidates.json` to render short highlight clips from shot/goal candidates
4. `History`
   - Review past runs
   - Filter `baseline / deliverable / highlight / failed`
   - Manage source videos, configs, and output folders grouped by source clip

### Current Storage Model

- Source videos live under `data/`
- Configs live under `config/`
- Generated configs live under `config/generated/`
- New runs are written to:

```text
outputs/runs/<input_slug>/<run_id>/
```

- History scanning is backward-compatible and still reads:
  - `outputs/*`
  - `outputs/api_runs/*`
  - `outputs/runs/<input_slug>/<run_id>`

### Common Commands

Short probe run:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_first_run.yaml
```

Full raw run:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_best_full.yaml
```

Full cleaned delivery run:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_v24_full_postclean.yaml
```

Run backend only:

```powershell
.\.venv\Scripts\python.exe -m uvicorn football_tracking.api.app:app --reload
```

### Official Candidate AI Classification Workflow

This P1 workflow classifies detector candidates as the match ball or noise. It is CPU-only, uses no downloaded
pretrained model, and never changes the live detector hot path. The calibrated selective policy and bounded human
review loop are implemented and fail closed. Prerequisite: the current tracking pipeline does not yet emit a
candidate-populated V2 contract, and its runtime `Candidate` has no stable deterministic ID. Supply externally prepared
training and policy V2 contracts with deterministic, source-scoped candidate IDs. PR5 owns wiring detector candidates
into these contracts and the normal tracking run. Once that prerequisite exists, prepare two evidence-disjoint
candidate populations and run these commands from the repository root. The training population is used only to
train/calibrate/test the classifier. The separate policy population supplies human-confirmed binary evaluation
holdouts plus application candidates. Candidate, video, group, split, temporal, and source evidence must not overlap
between those populations; the policy-role builder rejects any such leakage.

```powershell
$env:PYTHONPATH='python_backend'
$trainingSourceContract = 'data\candidate_training_contract.v2.json'
$policySourceContract = 'data\candidate_policy_contract.v2.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $trainingSourceContract `
  --source-map data\candidate_training_source_map.v1.json `
  --output-dir data\candidate_training_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $trainingSourceContract `
  --ledger data\candidate_training_votes.v1.jsonl `
  --dataset-manifest data\candidate_training_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_training_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\train_candidate_classifier.py `
  --dataset-manifest data\candidate_training_dataset_v1\candidate_dataset_manifest.json `
  --annotation-resolution data\candidate_training_resolution_v1\annotation_resolution.v1.json `
  --contract data\candidate_training_resolution_v1\tracking_contract.v2.json `
  --output-dir weights\candidate_classifier_v1 `
  --epochs 3 --batch-size 8 --seed 1337

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $policySourceContract `
  --source-map data\candidate_policy_source_map.v1.json `
  --output-dir data\candidate_policy_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $policySourceContract `
  --ledger data\candidate_policy_votes.v1.jsonl `
  --dataset-manifest data\candidate_policy_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_policy_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\classify_candidates.py `
  --package weights\candidate_classifier_v1 `
  --dataset-manifest data\candidate_policy_dataset_v1\candidate_dataset_manifest.json `
  --contract $policySourceContract `
  --output-dir outputs\candidate_policy_inference_v1 `
  --batch-size 32

$datasetManifest = 'data\candidate_policy_dataset_v1\candidate_dataset_manifest.json'
$annotationResolution = 'data\candidate_policy_resolution_v1\annotation_resolution.v1.json'
$resolvedContract = 'data\candidate_policy_resolution_v1\tracking_contract.v2.json'
$modelManifest = 'weights\candidate_classifier_v1\model_manifest.v1.json'
$trainingReport = 'weights\candidate_classifier_v1\training_report.v1.json'
$predictions = 'outputs\candidate_policy_inference_v1\candidate_predictions.v1.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_selective_policy_roles.py `
  --predictions $predictions `
  --dataset-manifest $datasetManifest `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --model-manifest $modelManifest `
  --training-report $trainingReport `
  --output-dir outputs\candidate_policy_roles_v1

.\.venv\Scripts\python.exe python_backend\scripts\fit_selective_policy.py `
  --predictions $predictions `
  --dataset-manifest $datasetManifest `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --model-manifest $modelManifest `
  --training-report $trainingReport `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_policy_v1

.\.venv\Scripts\python.exe python_backend\scripts\build_selective_review_queue.py `
  --dataset-manifest $datasetManifest `
  --predictions $predictions `
  --policy outputs\candidate_selective_policy_v1\selective_policy.v1.json `
  --decisions outputs\candidate_selective_policy_v1\selective_decisions.v1.json `
  --model-manifest $modelManifest `
  --contract $policySourceContract `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_review_queue_v1 `
  --window-seconds 7.5 `
  --max-windows 30

$actions = 'data\candidate_selective_review_actions_v1.json'
# Use a review client to read selective_review_queue.v1.json and export a bound
# selective_review_actions envelope to $actions. Do not type or invent hashes.

.\.venv\Scripts\python.exe python_backend\scripts\materialize_selective_review_actions.py `
  --queue outputs\candidate_selective_review_queue_v1\selective_review_queue.v1.json `
  --actions $actions `
  --dataset-manifest $datasetManifest `
  --predictions $predictions `
  --policy outputs\candidate_selective_policy_v1\selective_policy.v1.json `
  --decisions outputs\candidate_selective_policy_v1\selective_decisions.v1.json `
  --model-manifest $modelManifest `
  --contract $policySourceContract `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_review_round_v1
```

If a dataset source has no FPS, repeat `--fps-override <variant_id>=<fps>` on
`build_selective_review_queue.py`; unknown variants or non-positive values fail closed. Review windows may be set only
between 5 and 10 seconds, and `--max-windows` may not exceed 30.
Both review commands re-open the annotation resolution, resolved contract, and deterministic role manifest bound by
the policy lineage. They recompute human-confirmed evaluation cohorts, calibration, audit, and decisions before any
queue or materialization output is published; a self-resealed policy/decision pair cannot replace those inputs.

The source map is schema `1.0`. Each candidate must be bound exactly once to a real video; `candidate.source` remains
detector provenance and is not a video identifier. Video paths are relative to and contained by the source-map
directory. `video_sha256`, dimensions, and frame count must match the file. Use `sequential` for the safest HEVC
decode; `preroll` and verified `direct` are also supported.

```json
{
  "schema_version": "1.0",
  "sources": [
    {
      "variant_id": "match-main",
      "video_path": "match.mp4",
      "video_sha256": "<64 lowercase hex characters>",
      "decode_mode": "sequential",
      "width": 5120,
      "height": 1440,
      "frame_count": 5194,
      "group_id": "match-001",
      "temporal_group": "match-001-block-000",
      "split_group": "match-001",
      "candidate_ids": ["candidate-000001", "candidate-000002"]
    }
  ]
}
```

The vote ledger is finite JSONL. Its first row binds the exact contract and visual evidence. Every visual vote, human
or AI and whether primary or adjudication, binds one unique sample and the canonical tight/context/review-montage
evidence hash; these values must come from the dataset/annotation tooling, not be invented manually. Only a ledger with
no votes may omit the dataset manifest and its dataset/evidence fields.

```json
{"schema_version":"1.0","record_type":"ledger_header","contract_sha256":"<contract sha256>","dataset_version":"<dataset version>","evidence_manifest_sha256":"<manifest sha256>"}
{"schema_version":"1.0","record_type":"vote","vote_id":"vote-001","candidate_id":"candidate-000001","stage":"primary","reviewer_type":"ai","annotator_id":"model-a","fingerprint":"model-a-build-1","label":"match_ball","confidence":0.97,"blind":true,"created_at":"2026-07-09T12:00:00Z","dataset_version":"<dataset version>","sample_id":"000000-candidate-000001","evidence_sha256":"<canonical sample evidence sha256>"}
```

The seven labels are `match_ball`, `player_body_or_shoe`, `field_line_or_mark`, `sideline_or_spare_ball`,
`equipment_or_background`, `lighting_shadow_or_blur`, and `unknown`. Confirmation requires two blind primary votes
of the same reviewer type with distinct `annotator_id` and `fingerprint`, the same non-unknown label, and confidence at
or above the configured minimum. Consistent AI votes produce `ai_confirmed`; consistent human votes produce
`human_confirmed`. Unknown, disagreement, duplicate identity, non-blind, low-confidence, or incomplete voting stays
`unknown` and enters the adjudication queue. One independent human `adjudication` vote may finalize any of the seven
labels, including `human_confirmed` unknown. Existing confirmed rows are never overwritten; conflicting confirmed rows
remain non-training-eligible. Only explicitly `training_eligible` `ai_confirmed`/`human_confirmed` resolutions train the
model; prelabels, single votes, and unresolved candidates are excluded.

Artifacts are published atomically:

```text
data/candidate_training_dataset_v1/
  candidate_dataset_manifest.json
  samples/<sample_id>/{tight.npy,context.npy,review_montage.png}
data/candidate_training_resolution_v1/
  annotation_resolution.v1.json
  annotation_adjudication_queue.v1.json
  tracking_contract.v2.json
data/candidate_policy_dataset_v1/
  candidate_dataset_manifest.json
  samples/<sample_id>/{tight.npy,context.npy,review_montage.png}
data/candidate_policy_resolution_v1/
  annotation_resolution.v1.json
  annotation_adjudication_queue.v1.json
  tracking_contract.v2.json
weights/candidate_classifier_v1/
  model.pt
  model_manifest.v1.json
  training_report.v1.json
outputs/candidate_policy_inference_v1/
  candidate_predictions.v1.json
  tracking_contract.v2.json
outputs/candidate_policy_roles_v1/
  selective_policy_roles.v1.json
outputs/candidate_selective_policy_v1/
  selective_policy.v1.json
  selective_acceptance_report.v1.json
  selective_decisions.v1.json
  tracking_contract.v2.json
outputs/candidate_selective_review_queue_v1/
  review_timing.v1.json
  selective_review_queue.v1.json
outputs/candidate_selective_review_round_v1/
  human_adjudication_votes.v1.jsonl
  trajectory_corrections.v1.json
  active_learning_round.v1.json
  selective_review_materialization.v1.json
  annotations/{annotation_resolution.v1.json,annotation_adjudication_queue.v1.json,tracking_contract.v2.json}
```

Inference probabilities and labels are prelabels only: they do not create `ai_confirmed` labels or
accept/reject/abstain decisions, and existing confirmed or unknown history is retained. The policy-role builder uses
only `human_confirmed` binary truth (`match_ball` versus the concrete noise labels). A connected evidence component,
formed by shared variant, video, group, split, or temporal evidence, is the independent inferential unit. The builder
requires an exact one-to-one `candidate_id`/component mapping: every calibration or audit component must contain exactly
one human-confirmed evaluation candidate. Repeated frames or candidates from the same source therefore add zero sample
size and fail closed instead of increasing statistical power. Complete components are partitioned deterministically
into `policy_calibration` and `policy_audit`; overlap between those roles or any model train/calibration/test evidence
also fails closed. Policy qualification requires both calibration certification and an untouched audit pass. Its fixed
safety targets are at least 98% precision among auto-accepted candidates and at most 1% false rejection of true balls,
with family-wise error control. Per-variant/video/group/split/temporal tables are diagnostic only, carry no per-cluster
statistical guarantee, and never veto aggregate qualification. Insufficient or failed aggregate evidence produces a
review-only policy and abstentions instead of weakening those targets.

Calibration and audit candidates are `evaluation_holdout`: they are recorded in `selective_decisions.v1.json` with
`decision_scope="evaluation_only"`, forced to abstain, and never written back into the derived contract. Application
decisions are kept in that independent decisions artifact; the review queue requires it through `--decisions` rather
than reconstructing decisions from `tracking_contract.v2.json`.

The review queue gives uncertainty and conflict windows mandatory priority. Remaining capacity is filled by stable,
deterministic sampling across accept/reject and video-variant strata. A queue contains at most 30 windows; if mandatory
windows alone exceed the limit, narrow the input scope. `selection.coverage_complete=true` means every eligible
candidate was included. Otherwise `requires_additional_round=true` and `dropped_candidate_ids` identify work that must
be covered by another narrowed round.

A review client must generate the schema `1.0`, `artifact_type="selective_review_actions"` envelope from the exact
queue items and candidates. The four actions are `confirm_ball`, `reject_noise` with a concrete V2 `noise_subtype`,
`mark_unknown`, and `correct_trajectory` with ordered, in-window keypoints. In addition to action/reviewer/timestamp and
queue item/candidate IDs, every action must carry queue-derived `bindings`: `queue_sha256`, `timing_sha256`,
`policy_sha256`, `decisions_sha256`, `model_sha256`, `training_report_sha256`, `model_weights_sha256`, `dataset_sha256`,
`predictions_sha256`, `contract_sha256`, `annotation_resolution_sha256`,
`resolved_tracking_contract_sha256`, `policy_roles_sha256`, `evidence_sha256`, and `candidate_fingerprint`. Do not
type, copy between rounds, or invent these values. Materialization rejects stale or conflicting bindings, validates
every source snapshot again, and leaves the source contract unchanged.

`materialize_selective_review_actions.py` never trains a model; its reports explicitly set `training_invoked=false`.
Retraining is a separate operator decision. The materialized annotations and derived contract become training-only
evidence for the next model package. After training that package, build and annotate a fresh policy population from
evidence-disjoint candidates, videos, sources, groups, splits, and time ranges; classify it into a new inference
directory, then build policy roles and fit the new policy/version. Never reuse the new model's training population as
its policy evaluation or application population.
`model_manifest.v1.json`, `training_report.v1.json`, and `model.pt` form one hash-bound model package; the queue also
binds the independent `selective_decisions.v1.json`. Every command above that accepts `--output-dir` requires a new path
and publishes by atomic staging/rename. Validation, binding, or argument failures return non-zero and publish no partial
directory. `data/`, `weights/`, and `outputs/` are ignored by Git; keep videos, tensors, review media, and checkpoints
there rather than committing them.

### Main Outputs

Raw tracking usually writes:

- `annotated.mp4`
- `ball_track.csv`
- `debug.jsonl`

Cleanup adds:

- `annotated.cleaned.mp4`
- `ball_track.cleaned.csv`
- `debug.cleaned.jsonl`
- `cleanup_report.json`

Follow-cam adds:

- `follow_cam.mp4`
- `camera_path.csv`
- `camera_motion_audit.json`
- `follow_cam_report.json`

Temporal chunk runs add:

- `temporal_chunks_report.json`
- `chunks/<chunk_name>/ball_track.csv`
- `chunks/<chunk_name>/debug.jsonl`
- `metrics_report.json` with `stats.temporal_chunks` in API run history

Review and highlight artifacts add:

- `ball_audit.json`
- `ai_review_triggers.json`
- `camera_motion_audit.json`
- `player_tracks.json` when player artifacts are produced; this is not yet a promise of stable continuous player tracking.
- `event_candidates.json`
- `highlight.mp4`
- `highlight_report.json`

Stable AI improvement adds candidate and finalization artifacts:

- `ai_improvement_report.json` for advisory AI audit/improvement suggestions.
- `ai_improvement_approved_actions.json` as an approval source only when explicit approval ids are consumed.
- `ai_candidate_registry.json` for isolated missing-ball, noise, follow-cam, and highlight candidates.
- Domain comparisons such as `missing_ball_recovery_comparison.json`, `noise_improvement_comparison.json`, `follow_cam_comparison.json`, and `highlight_comparison.json`.
- `ai_improvement_quality_gate.json` and `final_ai_improvement_artifact_manifest.json` for final selection. Candidate files or media are not promoted just because they exist.

Use [`../docs/operations/ai-improvement-workflow.md`](../docs/operations/ai-improvement-workflow.md) for the stable workflow and [`../docs/operations/real-video-ai-improvement-validation.md`](../docs/operations/real-video-ai-improvement-validation.md) for the PR8 real-video validation record. Full-video speed should use temporal chunk parallelism by default; broad full-video SAHI is not the default path and belongs only inside explicit bounded recovery approvals.

### Docs

- English operation guide: [docs/operation-guide.en.md](docs/operation-guide.en.md)
- 中文操作指南: [docs/operation-guide.zh.md](docs/operation-guide.zh.md)
- Frontend planning notes:
  - `docs/plans/2026-03-21-ai-native-frontend-plan.md`
  - `docs/plans/2026-03-21-frontend-phase1-execution-plan.md`

### Verification

Frontend:

```powershell
cd frontend
npm run lint
npm run typecheck
npm test
npm run build
```

Backend:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Type-check scope note / 类型检查范围说明：`pyright` 当前先覆盖 `pyrightconfig.json` 里配置的稳定入口面：API schema/provider 和本地启动脚本。依赖 OpenCV 的跟踪主流水线还没有完成全量类型化。

---

## 中文说明

这个仓库用于从高分辨率鱼眼比赛视频中跟踪单个比赛用球，并提供本地 workspace 界面来完成基线运行、AI 调参、成品导出和历史管理。

### 仓库包含什么

- Python 跟踪主流程：原始跟踪、清洗、跟随裁剪
- 本地 FastAPI 后端：配置、任务、产物、AI 建议、资源管理
- 本地 React/Vite workspace 界面，当前有 5 个主标签：
  - `概览`
  - `跑基线`
  - `AI 分析`
  - `成品任务`
  - `历史`
- Windows 一键启动脚本，负责本地 UI 的托管启动和停止

### 建议优先使用的配置

- `config/real_first_run.yaml`
  - 适合短探测和首轮调参
- `config/real_best_full.yaml`
  - 当前较好的全量原始跟踪配置
- `config/real_v24_full_postclean.yaml`
  - 当前较好的全量交付配置，已启用清洗和 follow-cam

### 环境要求

- Windows 10 / 11
- Python 3.10 或 3.11
- 建议使用 NVIDIA GPU
- 正确安装 CUDA 和 cuDNN
- PATH 中可用 `npm`

### 检测权重

这一步很关键，第一次跑不起来多数就是这里没放对。

- 仓库里自带的 YAML 默认都指向：
  - `detector.model_path: "./weights/football_ball_yolo.pt"`
- 这个相对路径是按仓库根目录解析的。
- 默认应放在这里：
  - `weights/football_ball_yolo.pt`
- 这个 `.pt` 必须是 Ultralytics YOLO 的检测模型权重。
  - 可以：`detect` 类型的 YOLO `.pt`
  - 不适合：`classification`、`segmentation`、`pose`、`OBB` 这类权重
- 文件名不一定非要叫 `football_ball_yolo.pt`，但如果你改了文件名或放到别处，就要同步修改 YAML 里的 `detector.model_path`
- 当前默认配置接受的类别名是 `sports ball` 和 `ball`，如果你的模型输出别的类别名，要同步修改 `detector.allowed_labels`
- 如果只用 CPU，建议改成：
  - `detector.device: "cpu"`
  - `detector.use_half: false`

如果权重文件不存在，基线任务会在检测开始前直接失败。

### 快速开始

1. 创建并激活虚拟环境
2. 安装 Python 依赖
3. 安装前端依赖
4. 启动本地托管 UI

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt

cd frontend
npm install
cd ..

.\start_ui.cmd
```

如果你想要更稳的后端启动方式：

```powershell
.\start_ui.cmd --no-reload
```

停止托管的 UI 进程：

```powershell
.\stop_ui.cmd
```

### 当前 Workspace 流程

1. `跑基线`
   - 从 `data/` 里选择原视频
   - 选择一个基线配置
   - 截取预览帧
   - 从配置读取球场设置，或者让 AI 给建议
   - 接受球场设置
   - 启动一次基线任务
2. `AI 分析`
   - 选择与当前原视频关联的已完成 run
   - 人工触发 AI 解释
   - 查看建议的新配置
   - 如果建议合理，直接启动下一次任务
3. `成品任务`
   - 选择原视频和调好的配置，跑完整视频
   - 按需后处理球轨迹，并渲染最终 `16:9` 跟随镜头成品
   - 选择带有 `event_candidates.json` 的已完成 run，从射门/进球候选渲染集锦短片
4. `历史`
   - 查看过往 run
   - 按 `baseline / deliverable / highlight / failed` 过滤
   - 按原视频分组管理源视频、配置和输出目录

### 当前存储结构

- 原视频在 `data/`
- 配置文件在 `config/`
- 派生配置在 `config/generated/`
- 新任务输出会写到：

```text
outputs/runs/<input_slug>/<run_id>/
```

- 历史扫描仍兼容旧目录：
  - `outputs/*`
  - `outputs/api_runs/*`
  - `outputs/runs/<input_slug>/<run_id>`

### 常用命令

短探测运行：

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_first_run.yaml
```

全量原始跟踪：

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_best_full.yaml
```

全量清洗交付：

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_v24_full_postclean.yaml
```

只启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn football_tracking.api.app:app --reload
```

### 候选球 AI 分类官方流程

这套 P1 流程在 CPU 上把检测候选分成比赛用球或噪点，不下载预训练模型，也不接入实时 detector 热路径；
校准后的选择性策略和有上限的人工复核闭环已经实现，并采用失败关闭。前置条件：当前跟踪主流程还不会
自动生成带候选的 V2 契约，运行时 `Candidate` 也没有稳定的确定性 ID。需要先从外部准备
两套 V2 契约，其中候选 ID 必须稳定且带来源作用域。把 detector 候选接入这些契约和常规跟踪 run 仍属于
PR5。训练数据只允许用于分类器的 train/calibration/test；另一套证据完全独立的策略数据提供
`human_confirmed` 二元评估留出候选和应用候选。两套数据的 candidate、video、group、split、temporal 和
source 证据都不得重叠，否则策略角色生成器会失败关闭。准备好后，在仓库根目录依次执行：

```powershell
$env:PYTHONPATH='python_backend'
$trainingSourceContract = 'data\candidate_training_contract.v2.json'
$policySourceContract = 'data\candidate_policy_contract.v2.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $trainingSourceContract `
  --source-map data\candidate_training_source_map.v1.json `
  --output-dir data\candidate_training_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $trainingSourceContract `
  --ledger data\candidate_training_votes.v1.jsonl `
  --dataset-manifest data\candidate_training_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_training_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\train_candidate_classifier.py `
  --dataset-manifest data\candidate_training_dataset_v1\candidate_dataset_manifest.json `
  --annotation-resolution data\candidate_training_resolution_v1\annotation_resolution.v1.json `
  --contract data\candidate_training_resolution_v1\tracking_contract.v2.json `
  --output-dir weights\candidate_classifier_v1 `
  --epochs 3 --batch-size 8 --seed 1337

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $policySourceContract `
  --source-map data\candidate_policy_source_map.v1.json `
  --output-dir data\candidate_policy_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $policySourceContract `
  --ledger data\candidate_policy_votes.v1.jsonl `
  --dataset-manifest data\candidate_policy_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_policy_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\classify_candidates.py `
  --package weights\candidate_classifier_v1 `
  --dataset-manifest data\candidate_policy_dataset_v1\candidate_dataset_manifest.json `
  --contract $policySourceContract `
  --output-dir outputs\candidate_policy_inference_v1 `
  --batch-size 32

$datasetManifest = 'data\candidate_policy_dataset_v1\candidate_dataset_manifest.json'
$annotationResolution = 'data\candidate_policy_resolution_v1\annotation_resolution.v1.json'
$resolvedContract = 'data\candidate_policy_resolution_v1\tracking_contract.v2.json'
$modelManifest = 'weights\candidate_classifier_v1\model_manifest.v1.json'
$trainingReport = 'weights\candidate_classifier_v1\training_report.v1.json'
$predictions = 'outputs\candidate_policy_inference_v1\candidate_predictions.v1.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_selective_policy_roles.py `
  --predictions $predictions `
  --dataset-manifest $datasetManifest `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --model-manifest $modelManifest `
  --training-report $trainingReport `
  --output-dir outputs\candidate_policy_roles_v1

.\.venv\Scripts\python.exe python_backend\scripts\fit_selective_policy.py `
  --predictions $predictions `
  --dataset-manifest $datasetManifest `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --model-manifest $modelManifest `
  --training-report $trainingReport `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_policy_v1

.\.venv\Scripts\python.exe python_backend\scripts\build_selective_review_queue.py `
  --dataset-manifest $datasetManifest `
  --predictions $predictions `
  --policy outputs\candidate_selective_policy_v1\selective_policy.v1.json `
  --decisions outputs\candidate_selective_policy_v1\selective_decisions.v1.json `
  --model-manifest $modelManifest `
  --contract $policySourceContract `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_review_queue_v1 `
  --window-seconds 7.5 `
  --max-windows 30

$actions = 'data\candidate_selective_review_actions_v1.json'
# 复核客户端必须读取 selective_review_queue.v1.json，再把带完整绑定的
# selective_review_actions envelope 导出到 $actions；不要手填或猜测任何哈希。

.\.venv\Scripts\python.exe python_backend\scripts\materialize_selective_review_actions.py `
  --queue outputs\candidate_selective_review_queue_v1\selective_review_queue.v1.json `
  --actions $actions `
  --dataset-manifest $datasetManifest `
  --predictions $predictions `
  --policy outputs\candidate_selective_policy_v1\selective_policy.v1.json `
  --decisions outputs\candidate_selective_policy_v1\selective_decisions.v1.json `
  --model-manifest $modelManifest `
  --contract $policySourceContract `
  --annotation-resolution $annotationResolution `
  --resolved-contract $resolvedContract `
  --policy-roles outputs\candidate_policy_roles_v1\selective_policy_roles.v1.json `
  --output-dir outputs\candidate_selective_review_round_v1
```

如果数据集中的某个 source 没有 FPS，应在 `build_selective_review_queue.py` 上为它重复添加
`--fps-override <variant_id>=<fps>`；未知 variant 或非正数会失败关闭。复核窗口只能设为 5 到 10 秒，
`--max-windows` 不能超过 30。
两个复核命令都会重新打开 policy lineage 绑定的标注裁决、resolved contract 和确定性角色清单，并在发布
队列或物化结果前重新计算 human-confirmed 评估队列、校准、审计与决策。因此，仅重封口 policy/decisions
无法把未确认的 application 候选替换成 calibration/audit 候选。

`candidate_source_map.v1.json` 使用 schema `1.0`。每个 `sources[]` 必须给出
`variant_id`、受 source-map 目录约束的相对 `video_path`、`video_sha256`、`decode_mode`、`width`、`height`、
`frame_count`、`group_id`、`temporal_group`、`split_group` 和非空 `candidate_ids`。每个候选必须且只能绑定
一个视频；V2 候选里的 `source` 仍表示 detector 来源，不是视频 ID。同一场比赛的不同编码应使用相同
`group_id`/`split_group`，相邻五帧窗口应归入同一时间组。HEVC 优先使用 `sequential`，也支持
`preroll` 和经过验证的 `direct`。

投票账本是有限 JSONL。第一行必须绑定原始契约和数据集证据；每张人工或 AI 的主票/裁决票都必须绑定
唯一 sample 及 tight/context/review-montage 三类产物的规范聚合哈希。只有完全没有投票的空账本可以省略
dataset manifest 及其 dataset/evidence 字段：

```json
{"schema_version":"1.0","record_type":"ledger_header","contract_sha256":"<contract sha256>","dataset_version":"<dataset version>","evidence_manifest_sha256":"<manifest sha256>"}
{"schema_version":"1.0","record_type":"vote","vote_id":"vote-001","candidate_id":"candidate-000001","stage":"primary","reviewer_type":"ai","annotator_id":"model-a","fingerprint":"model-a-build-1","label":"match_ball","confidence":0.97,"blind":true,"created_at":"2026-07-09T12:00:00Z","dataset_version":"<dataset version>","sample_id":"000000-candidate-000001","evidence_sha256":"<canonical sample evidence sha256>"}
```

合法标签固定为 `match_ball`、`player_body_or_shoe`、`field_line_or_mark`、`sideline_or_spare_ball`、
`equipment_or_background`、`lighting_shadow_or_blur`、`unknown`。确认需要两张同类型、相互盲审的主票：
`annotator_id` 与 `fingerprint` 都不同、标签相同且不是 unknown、置信度不低于阈值。两张 AI 票生成
`ai_confirmed`，两张人工票生成 `human_confirmed`。unknown、分歧、重复身份、非盲审、低置信度或票数不足
都保留为 unknown 并进入人工裁决队列；一个身份独立的人工 `adjudication` 票可最终确认任意七类，包括
`human_confirmed` unknown。既有 confirmed 行不会被覆盖；既有 confirmed 冲突会保留且禁止训练。只有明确
标记 `training_eligible` 的 `ai_confirmed`/`human_confirmed` 结果可训练，prelabel、单票和未决项全部排除。

所有产物按下面的边界原子发布：

```text
data/candidate_training_dataset_v1/
  candidate_dataset_manifest.json
  samples/<sample_id>/{tight.npy,context.npy,review_montage.png}
data/candidate_training_resolution_v1/
  annotation_resolution.v1.json
  annotation_adjudication_queue.v1.json
  tracking_contract.v2.json
data/candidate_policy_dataset_v1/
  candidate_dataset_manifest.json
  samples/<sample_id>/{tight.npy,context.npy,review_montage.png}
data/candidate_policy_resolution_v1/
  annotation_resolution.v1.json
  annotation_adjudication_queue.v1.json
  tracking_contract.v2.json
weights/candidate_classifier_v1/
  model.pt
  model_manifest.v1.json
  training_report.v1.json
outputs/candidate_policy_inference_v1/
  candidate_predictions.v1.json
  tracking_contract.v2.json
outputs/candidate_policy_roles_v1/
  selective_policy_roles.v1.json
outputs/candidate_selective_policy_v1/
  selective_policy.v1.json
  selective_acceptance_report.v1.json
  selective_decisions.v1.json
  tracking_contract.v2.json
outputs/candidate_selective_review_queue_v1/
  review_timing.v1.json
  selective_review_queue.v1.json
outputs/candidate_selective_review_round_v1/
  human_adjudication_votes.v1.jsonl
  trajectory_corrections.v1.json
  active_learning_round.v1.json
  selective_review_materialization.v1.json
  annotations/{annotation_resolution.v1.json,annotation_adjudication_queue.v1.json,tracking_contract.v2.json}
```

推理概率和标签永远只是 prelabel，不会自动生成 `ai_confirmed`，也不会自行产生
accept/reject/abstain；既有 confirmed 与 unknown 历史继续保留。策略角色生成器只使用
`human_confirmed` 的二元真值（`match_ball` 与所有具体噪点标签）。由共享 variant、video、group、split 或
temporal 证据形成的连通组件才是独立推断单位；生成器要求 `candidate_id` 与组件严格一一对应，每个 calibration
或 audit 组件必须恰好包含一个人工确认评估候选。同一来源的重复帧或重复候选不会增加样本量，而会在统计检验
前失败关闭。完整组件再被确定性地划分为 `policy_calibration` 和 `policy_audit`；两组之间，或与模型的
train/calibration/test 证据之间只要有重叠，也会失败关闭。策略只有在校准认证和独立 audit 都通过后才
qualified；固定安全目标是自动 accept 的 precision 至少 98%，真球 false reject 不超过 1%，并进行族错误率
控制。按 variant/video/group/split/temporal 展示的表格仅用于诊断，不提供单组统计保证，也不会否决聚合资格。
聚合证据不足或未通过时，系统只生成 review-only 策略并 abstain，不会降低门槛。

校准和 audit 候选都是 `evaluation_holdout`：它们在 `selective_decisions.v1.json` 中记录为
`decision_scope="evaluation_only"`，强制 abstain，也不会回写派生契约。应用侧 decisions 保存在这份独立
产物中；复核队列必须通过 `--decisions` 明确绑定它，不能从 `tracking_contract.v2.json` 反推 decisions。

复核队列优先完整纳入 uncertainty/conflict 窗口；剩余容量按 accept/reject 与视频 variant 分层，进行稳定、
确定性的轮转抽样。每个队列最多 30 个窗口；如果仅必审窗口就超限，必须先缩小输入范围。
`selection.coverage_complete=true` 表示所有 eligible 候选已覆盖；否则
`requires_additional_round=true`，并由 `dropped_candidate_ids` 指出需要在下一轮缩小范围后继续复核的候选。

复核客户端必须从精确的 queue item 和 candidate 生成 schema `1.0`、
`artifact_type="selective_review_actions"` 的 envelope。四类 action 是 `confirm_ball`、带具体 V2
`noise_subtype` 的 `reject_noise`、`mark_unknown`，以及带窗口内有序关键点的 `correct_trajectory`。除 action、
reviewer、时间戳及 queue item/candidate ID 外，每条 action 还必须携带由 queue 生成的 `bindings`：
`queue_sha256`、`timing_sha256`、`policy_sha256`、`decisions_sha256`、`model_sha256`、
`training_report_sha256`、`model_weights_sha256`、`dataset_sha256`、`predictions_sha256`、`contract_sha256`、
`annotation_resolution_sha256`、`resolved_tracking_contract_sha256`、`policy_roles_sha256`、`evidence_sha256`
和 `candidate_fingerprint`。不要手填、跨轮复制或猜测这些值。物化阶段会再次验证全部绑定和输入快照；
过期、篡改或冲突的 action 都会失败关闭，原始 source contract 保持不变。

`materialize_selective_review_actions.py` 永远不会训练模型，报告会明确写入 `training_invoked=false`。是否
再训练必须由操作员显式决定。本轮物化出的 annotations 和派生 contract 在下一轮只能作为训练侧证据，用它们
在新的模型包目录训练出新 model/version 后，还必须从候选、视频、source、group、split 和时间范围都不重叠的
证据重新构建并标注一套策略数据，再写入新的 inference 目录，然后生成 policy roles 并拟合新 policy/version。
禁止把新模型的训练数据同时作为它的策略评估或应用数据。
`model_manifest.v1.json`、`training_report.v1.json`、`model.pt` 是哈希绑定的模型包三件套；queue 还会绑定
独立的 `selective_decisions.v1.json`。以上所有带 `--output-dir` 的命令都要求目标路径尚不存在，并通过
staging/rename 原子发布；参数、完整性或绑定校验失败会返回非零，且不会留下半套目录。`.gitignore` 已忽略
`data/`、`weights/`、`outputs/`，视频、tensor、复核图片和权重应留在这些目录，不要提交到 Git。

### 主要输出文件

原始跟踪通常会输出：

- `annotated.mp4`
- `ball_track.csv`
- `debug.jsonl`

启用清洗后还会输出：

- `annotated.cleaned.mp4`
- `ball_track.cleaned.csv`
- `debug.cleaned.jsonl`
- `cleanup_report.json`

启用 follow-cam 后还会输出：

- `follow_cam.mp4`
- `camera_path.csv`
- `camera_motion_audit.json`
- `follow_cam_report.json`

启用时间分块后还会输出：

- `temporal_chunks_report.json`
- `chunks/<chunk_name>/ball_track.csv`
- `chunks/<chunk_name>/debug.jsonl`
- API 历史里的 `metrics_report.json` 会包含 `stats.temporal_chunks` 摘要

审核与集锦相关输出还包括：

- `ball_audit.json`
- `ai_review_triggers.json`
- `camera_motion_audit.json`
- 可用时的 `player_tracks.json`；它是球员产物记录，不等同于已经完成稳定连续追人能力。
- `event_candidates.json`
- `highlight.mp4`
- `highlight_report.json`

稳定 AI improvement 还会增加候选与最终选择产物：

- `ai_improvement_report.json`：AI 审核/改进建议，仍是 advisory。
- `ai_improvement_approved_actions.json`：只有显式消费 approval id 时才是执行来源。
- `ai_candidate_registry.json`：记录丢球、噪声、follow-cam、集锦候选。
- `missing_ball_recovery_comparison.json`、`noise_improvement_comparison.json`、`follow_cam_comparison.json`、`highlight_comparison.json` 等领域比较报告。
- `ai_improvement_quality_gate.json` 与 `final_ai_improvement_artifact_manifest.json`：用于最终选择；候选文件或视频存在本身不能 promoted。

稳定流程见 [`../docs/operations/ai-improvement-workflow.md`](../docs/operations/ai-improvement-workflow.md)，PR8 真实视频验证记录见 [`../docs/operations/real-video-ai-improvement-validation.md`](../docs/operations/real-video-ai-improvement-validation.md)。整视频提速默认使用 temporal chunk；广义整视频 SAHI 不是默认路径，只能出现在显式批准的有界恢复窗口中。

### 文档入口

- English operation guide: [docs/operation-guide.en.md](docs/operation-guide.en.md)
- 中文操作指南: [docs/operation-guide.zh.md](docs/operation-guide.zh.md)
- 前端规划文档：
  - `docs/plans/2026-03-21-ai-native-frontend-plan.md`
  - `docs/plans/2026-03-21-frontend-phase1-execution-plan.md`

### 验证命令

前端：

```powershell
cd frontend
npm run lint
npm run typecheck
npm test
npm run build
```

后端：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```
