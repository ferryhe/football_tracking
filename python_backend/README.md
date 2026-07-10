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
pretrained model, and never changes the live detector hot path. Prerequisite: the current tracking pipeline does not
yet emit a candidate-populated V2 contract, and its runtime `Candidate` has no stable deterministic ID. Supply an
externally prepared `data\candidate_contract.v2.json` with deterministic, source-scoped candidate IDs. PR5 owns wiring
detector candidates into this contract and the normal tracking run. Once that prerequisite exists, run these commands
from the repository root:

```powershell
$env:PYTHONPATH='python_backend'
$sourceContract = 'data\candidate_contract.v2.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $sourceContract `
  --source-map data\candidate_source_map.v1.json `
  --output-dir data\candidate_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $sourceContract `
  --ledger data\candidate_votes.v1.jsonl `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\train_candidate_classifier.py `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --annotation-resolution data\candidate_resolution_v1\annotation_resolution.v1.json `
  --contract data\candidate_resolution_v1\tracking_contract.v2.json `
  --output-dir weights\candidate_classifier_v1 `
  --epochs 3 --batch-size 8 --seed 1337

.\.venv\Scripts\python.exe python_backend\scripts\classify_candidates.py `
  --package weights\candidate_classifier_v1 `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --contract $sourceContract `
  --output-dir outputs\candidate_inference_v1 `
  --batch-size 32
```

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
data/candidate_dataset_v1/
  candidate_dataset_manifest.json
  samples/<sample_id>/{tight.npy,context.npy,review_montage.png}
data/candidate_resolution_v1/
  annotation_resolution.v1.json
  annotation_adjudication_queue.v1.json
  tracking_contract.v2.json
weights/candidate_classifier_v1/
  model.pt
  model_manifest.v1.json
  training_report.v1.json
outputs/candidate_inference_v1/
  candidate_predictions.v1.json
  tracking_contract.v2.json
```

Inference probabilities and labels are prelabels only: they do not create `ai_confirmed` labels or
accept/reject/abstain decisions, and existing confirmed or unknown history is retained. Calibrated selective thresholds
and the human-review loop belong to PR4. `data/`, `weights/`, and `outputs/` are ignored by Git; keep videos, tensors,
review media, and checkpoints there rather than committing them. Every CLI fails closed: validation or argument errors
produce concise JSON on stderr with a non-zero exit code, and no partial artifact set is promoted. Dataset, model, and
inference output directories must be new paths.

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

这套 P1 流程在 CPU 上把检测候选分成比赛用球或噪点，不下载预训练模型，也不接入实时 detector 热路径。
前置条件：当前跟踪主流程还不会自动生成带候选的 V2 契约，运行时 `Candidate` 也没有稳定的确定性 ID。
需要先从外部准备 `data\candidate_contract.v2.json`，其中候选 ID 必须稳定且带来源作用域。把 detector
候选接入该契约和常规跟踪 run 属于 PR5。满足前置条件后，在仓库根目录依次执行：

```powershell
$env:PYTHONPATH='python_backend'
$sourceContract = 'data\candidate_contract.v2.json'

.\.venv\Scripts\python.exe python_backend\scripts\build_candidate_dataset.py `
  --contract $sourceContract `
  --source-map data\candidate_source_map.v1.json `
  --output-dir data\candidate_dataset_v1

.\.venv\Scripts\python.exe python_backend\scripts\resolve_candidate_annotations.py `
  --contract $sourceContract `
  --ledger data\candidate_votes.v1.jsonl `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --output-dir data\candidate_resolution_v1 `
  --min-confidence 0.8

.\.venv\Scripts\python.exe python_backend\scripts\train_candidate_classifier.py `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --annotation-resolution data\candidate_resolution_v1\annotation_resolution.v1.json `
  --contract data\candidate_resolution_v1\tracking_contract.v2.json `
  --output-dir weights\candidate_classifier_v1 `
  --epochs 3 --batch-size 8 --seed 1337

.\.venv\Scripts\python.exe python_backend\scripts\classify_candidates.py `
  --package weights\candidate_classifier_v1 `
  --dataset-manifest data\candidate_dataset_v1\candidate_dataset_manifest.json `
  --contract $sourceContract `
  --output-dir outputs\candidate_inference_v1 `
  --batch-size 32
```

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

数据集输出 `candidate_dataset_manifest.json` 与每个 sample 的 `tight.npy`、`context.npy`、
`review_montage.png`；解析输出 `annotation_resolution.v1.json`、
`annotation_adjudication_queue.v1.json` 和派生 `tracking_contract.v2.json`；模型包输出 `model.pt`、
`model_manifest.v1.json`、`training_report.v1.json`；推理输出 `candidate_predictions.v1.json` 和派生
`tracking_contract.v2.json`。推理结果永远只是 prelabel，不会自动生成 `ai_confirmed`，也不会生成
accept/reject/abstain；既有 confirmed 与 unknown 历史继续保留。校准后的选择性阈值和人工复核闭环属于 PR4。

`.gitignore` 已忽略 `data/`、`weights/`、`outputs/`，视频、tensor、复核图片和权重必须留在这些目录，
不要提交到 Git。所有 CLI 都失败关闭：参数或完整性校验失败时向 stderr 输出简短 JSON、返回非零退出码，
并且不会发布半套产物；数据集、模型和推理的 `--output-dir` 必须是尚不存在的新目录。

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
