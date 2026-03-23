# High-Resolution Football Ball Tracking

English | [中文](#中文说明)

This repository tracks a single in-play football from high-resolution fisheye-style match video and provides a local workspace UI for baseline runs, AI-assisted tuning, deliverable rendering, and history management.

## English

### What This Repo Includes

- Python tracking pipeline for raw tracking, cleanup, and follow-cam rendering
- Local FastAPI backend for configs, runs, artifacts, AI suggestions, and asset management
- Local React/Vite workspace UI with 4 main tabs:
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
   - Pick a completed source run
   - Render a clean `16:9` deliverable without rerunning the full baseline pipeline
4. `History`
   - Review past runs
   - Filter `baseline / deliverable / failed`
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
- `follow_cam_report.json`

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
- 本地 React/Vite workspace 界面，当前有 4 个主标签：
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
   - 选择一个已完成 run
   - 单独导出干净的 `16:9` 成品，不需要重新跑完整基线
4. `历史`
   - 查看过往 run
   - 按 `baseline / deliverable / failed` 过滤
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
- `follow_cam_report.json`

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
