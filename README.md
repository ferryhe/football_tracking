# Football Tracking Workspace

[![CI](https://github.com/ferryhe/football_tracking/actions/workflows/ci.yml/badge.svg)](https://github.com/ferryhe/football_tracking/actions/workflows/ci.yml)

[English](#english) | [中文](#中文)

A responsive React/Vite workspace UI for the **football video tracking** pipeline, running on Replit. The Python tracking pipeline is preserved as-is from the upstream project; this repo replaces the original Windows desktop UI with a path-routed multi-artifact web workspace, reverse-proxied through a Node.js API server.

> Upstream pipeline: [`github.com/ferryhe/football_tracking`](https://github.com/ferryhe/football_tracking) — see [`python_backend/README.md`](./python_backend/README.md) for the original docs (configs, detector weights, output formats).

## English

### Architecture

```
Browser
  │
  ▼  HTTPS via $REPLIT_DOMAINS
[ shared Replit proxy (port 80) ]
  │
  ├─ /        → artifacts/web         (React + Vite + shadcn/ui frontend)
  └─ /api/*   → artifacts/api-server  (Node.js Express reverse proxy)
                  │
                  ▼  pathRewrite ^/ → /api/v1/
                python_backend          (FastAPI tracking service, port 8000)
```

- The **Node API server** proxies `/api/*` to the Python FastAPI service. It uses `fixRequestBody` so JSON request bodies survive the round-trip through `express.json()`.
- The **Python backend** lives in `python_backend/` and is **not** part of the pnpm workspace; it's a standalone Python project with its own `pyproject.toml`. The tracking pipeline remains compatible with upstream, while the local FastAPI surface adds workspace endpoints for metrics, review reports, renders, and highlights.
- All paths are routed by the shared Replit proxy — never call service ports directly.

### Repository layout

```
.
├── artifacts/
│   ├── web/                 React frontend (5 pages, shadcn/ui, i18n, dark mode)
│   ├── api-server/          Express reverse proxy to Python backend
│   └── mockup-sandbox/      Component preview sandbox (unused for this app)
├── python_backend/          FastAPI tracking pipeline (standalone Python project)
│   ├── football_tracking/   Pipeline + API code
│   ├── config/              YAML tracking configs
│   ├── data/                Source videos (drop your videos here)
│   ├── outputs/             Run artifacts and rendered videos
│   └── weights/             Detector model checkpoints (.pt)
├── lib/                     Shared TS libs (workspace)
├── scripts/                 Workspace utility scripts
├── pnpm-workspace.yaml
└── replit.md                Project context for the Replit Agent
```

### Frontend pages (artifacts/web)

| Path          | Page         | Purpose                                                                                                  |
| ------------- | ------------ | -------------------------------------------------------------------------------------------------------- |
| `/`           | Dashboard    | System status (backend / configs / runs), recent runs, available configs                                 |
| `/baseline`   | Baseline     | Pick video + config, **preview field & accept AI field setup**, set frame range, launch baseline run     |
| `/ai`         | AI Analysis  | For any finished run (completed or failed), request AI tracking improvement suggestions with overlays    |
| `/deliverable`| Deliverable  | Render a follow-cam 16:9 deliverable and create short highlight clips from event candidates              |
| `/history`    | History      | Filter & search past runs, including baseline, deliverable, highlight, failed, and stopped jobs          |

Highlights of the new Baseline page:

- **Field Setup card** — captures a sample frame from the chosen video, requests an AI suggestion that marks the playing field, and forwards the accepted `config_patch` to the run. Suggestion is auto-invalidated when the source video or config changes.
- **Frame Range** — optional `start_frame` and `max_frames` inputs let you do quick partial-clip tests (leave both empty to process the full video).
- **Auto-redirect** — after a run is queued, the user is sent to `/history` to watch progress.

Review, improvement, and highlight outputs:

- **Run metrics and manifest** — each run writes reproducible summaries for raw/cleaned tracks, audit reports, AI review triggers, player-track artifacts when available, event candidates, and generated renders. `player_tracks.json` is an artifact, not a guarantee that stable continuous player tracking is fully productized.
- **Camera motion audit** — follow-cam renders also write `camera_motion_audit.json`, a standalone review report for abrupt pan, acceleration, or zoom changes in the final camera path.
- **Event candidates** — completed runs can expose `event_candidates.json` with shot and goal candidates. These are review candidates, not confirmed football events.
- **Highlight clips** — the Deliverable page can render a short `highlight.mp4` from a selected event candidate; the child run writes `highlight_report.json` and appears in History as a highlight job.
- **Stable AI improvement workflow** — use [`docs/operations/ai-improvement-workflow.md`](./docs/operations/ai-improvement-workflow.md) to rerun review/improvement checks against an existing output directory. AI audit explains issues; AI improvement creates bounded candidates for missing balls, dense noise, follow-cam motion, or highlight boundaries and then compares them before final selection. The recipe favors temporal chunks for full-video speed, reserves SAHI/ROI for explicit bounded approvals, and never executes approval files just because they exist.
- **Real-video validation record** — use [`docs/operations/real-video-ai-improvement-validation.md`](./docs/operations/real-video-ai-improvement-validation.md) to record the exact command, video, output directory, model/mode, timing, produced artifacts, visual checks, and final playable tracking/follow-cam/highlight outputs.

### Workflows (managed automatically on Replit)

| Workflow                                       | Command                                                                          |
| ---------------------------------------------- | -------------------------------------------------------------------------------- |
| `Python FastAPI Backend`                       | `python -m uvicorn football_tracking.api.app:app --host 0.0.0.0 --port 8000 --reload` |
| `artifacts/api-server: API Server`             | `pnpm --filter @workspace/api-server run dev`                                    |
| `artifacts/web: web`                           | `pnpm --filter @workspace/web run dev`                                           |
| `artifacts/mockup-sandbox: Component Preview`  | `pnpm --filter @workspace/mockup-sandbox run dev`                                |

### Verification

```bash
# Type-check the whole monorepo (libs + leaf packages)
pnpm run typecheck

# Type-check just the frontend or api-server
pnpm --filter @workspace/web run typecheck
pnpm --filter @workspace/api-server run typecheck

# Quick proxy smoke-tests
curl -s localhost:80/api/healthz                              # Node-side health
curl -s localhost:80/api/health                               # Python-side health
curl -s localhost:80/api/inputs                               # List source videos
curl -s -X POST -H "Content-Type: application/json" \
     -d '{}' localhost:80/api/inputs/field-suggestion         # Should return 422 (validation)
```

### Environment variables

| Name                      | Required | Purpose                                                                       |
| ------------------------- | -------- | ----------------------------------------------------------------------------- |
| `SESSION_SECRET`          | yes      | Express session secret                                                        |
| `PYTHON_API_URL`          | no       | Python backend URL — defaults to `http://localhost:8000`                      |
| `PROVIDER_OPENAI_API_KEY` | no       | OpenAI key for richer AI recommendations; if unset, local heuristics are used |
| `PROVIDER_OPENAI_BASE_URL`| no       | Override OpenAI-compatible base URL                                           |
| `PROVIDER_OPENAI_CHAT_MODEL` | no    | Override chat model name                                                      |

These are stored as Replit Secrets. **Do not** create `.env` files for them.

### Drop-in usage

1. Put one or more `.mp4` videos under `python_backend/data/`.
2. Make sure a YOLO detector checkpoint is available at `python_backend/weights/football_ball_yolo.pt` (or update `detector.model_path` in your YAML).
3. Open the web preview, go to **Baseline**, pick a video & config, optionally request an AI field suggestion, set a frame range for a quick test, then **Start Baseline Run**.
4. Watch progress in **History**.
5. After a run completes, visit **Deliverable** to render a 16:9 follow-cam video or create short highlight clips from event candidates; use **AI Analysis** to ask for tuning suggestions.

### What changed vs. upstream

- The original React/Vite UI in `python_backend/frontend/` has been **replaced** by `artifacts/web/`; the archived copy was removed to keep this repo layout minimal.
- A Node.js Express **reverse proxy** sits in front of FastAPI to fit Replit's path-routed proxy and to simplify local dev URLs.
- The frontend gained: 5 pages with sidebar nav, Dashboard overview, dark/light mode, EN/中文 i18n, mobile responsive layout.
- Frame-range partial-clip runs (`start_frame` / `max_frames`) were added to the baseline UI; the backend already accepted these fields.
- The backend now writes review artifacts (`ball_audit.json`, `ai_review_triggers.json`, `camera_motion_audit.json`, `event_candidates.json`, and player artifacts such as `player_tracks.json` when available) and supports child render jobs for follow-cam deliverables and highlight clips.

---

## 中文

一个跑在 Replit 上的足球视频追踪 Workspace UI。Python 追踪流水线沿用上游项目；本仓库把原来的 Windows 桌面 UI 替换成了一套路径路由的多 artifact 网页工作台，前面挂着一个 Node.js API 反向代理。

> 上游：[`github.com/ferryhe/football_tracking`](https://github.com/ferryhe/football_tracking) — 配置、检测权重、输出格式等原始文档见 [`python_backend/README.md`](./python_backend/README.md)。

### 架构

```
浏览器
  │
  ▼  HTTPS（$REPLIT_DOMAINS）
[ Replit 共享代理（80 端口） ]
  │
  ├─ /        → artifacts/web         （React + Vite + shadcn/ui 前端）
  └─ /api/*   → artifacts/api-server  （Node.js Express 反向代理）
                  │
                  ▼  路径重写 ^/ → /api/v1/
                python_backend          （FastAPI 追踪服务，8000 端口）
```

- **Node API server** 把 `/api/*` 转发到 FastAPI；通过 `fixRequestBody` 让 JSON 请求体能完整穿过 `express.json()`。
- **Python 后端** 是独立 Python 项目，**不在** pnpm workspace 里；追踪主流程保持与上游兼容，本地 FastAPI 额外提供指标、审核报告、渲染和集锦相关的工作台接口。
- 所有访问都走 Replit 共享代理，**别直接打服务端口**。

### 目录结构

```
.
├── artifacts/
│   ├── web/                 React 前端（5 个页面、shadcn/ui、国际化、暗黑模式）
│   ├── api-server/          Express 反向代理
│   └── mockup-sandbox/      组件预览沙箱（本项目暂未使用）
├── python_backend/          FastAPI 追踪流水线（独立 Python 项目）
│   ├── football_tracking/   流水线与 API 代码
│   ├── config/              YAML 追踪配置
│   ├── data/                源视频（把你的视频放这里）
│   ├── outputs/             任务产物与渲染视频
│   └── weights/             检测器权重（.pt）
├── lib/                     共享 TS 库（workspace）
├── scripts/                 Workspace 工具脚本
├── pnpm-workspace.yaml
└── replit.md                给 Replit Agent 的项目说明
```

### 前端页面（artifacts/web）

| 路径           | 页面     | 用途                                                                                |
| -------------- | -------- | ----------------------------------------------------------------------------------- |
| `/`            | 概览     | 系统状态（后端 / 配置 / 任务）、近期任务、可用配置                                  |
| `/baseline`    | 跑基线   | 选视频 + 配置，**预览球场并接受 AI 球场设置**，设置帧范围，启动基线任务             |
| `/ai`          | AI 分析  | 针对任意已结束（完成或失败）的任务，向 AI 请求改进建议，并叠加可视化标注           |
| `/deliverable` | 成品任务 | 渲染 16:9 跟随裁剪视频，并基于事件候选生成集锦短片                                  |
| `/history`     | 历史     | 过滤 / 搜索基线、成品、集锦、失败、已停止任务，删除输出                             |

新版「跑基线」页要点：

- **球场设置卡片** —— 抽取一帧预览，让 AI 自动识别球场区域；接受后建议的 `config_patch` 会随任务提交。源视频或配置变更时建议自动失效。
- **帧范围** —— 可选 `start_frame` / `max_frames`，便于快速试跑一小段（留空则处理整段）。
- **自动跳转** —— 任务排队后自动跳到「历史」页让你看进度。

审核、改进与集锦输出：

- **指标与运行清单** —— 每次任务都会写出 raw/cleaned 轨迹、审核报告、AI 审核触发、可用时的球员轨迹产物、事件候选和渲染结果摘要。`player_tracks.json` 是产物记录，不等同于已经完成稳定连续追人能力。
- **镜头运动审核** —— follow-cam 渲染会额外写出 `camera_motion_audit.json`，专门复核最终镜头路径里的突然平移、突然加速或突然缩放。
- **事件候选** —— 已完成任务可生成 `event_candidates.json`，其中包含射门和进球候选；这些是待复核候选，不是已确认事件。
- **集锦短片** —— 「成品任务」页可以从某个事件候选渲染 `highlight.mp4`；子任务会写 `highlight_report.json`，并在「历史」页显示为集锦任务。
- **稳定 AI improvement 工作流** —— 见 [`docs/operations/ai-improvement-workflow.md`](./docs/operations/ai-improvement-workflow.md)。AI audit 只解释问题；AI improvement 会为丢球、密集噪声、follow-cam 抖动或集锦边界创建有界候选，并在最终选择前比较质量。整视频提速默认走 temporal chunk；SAHI/ROI 只用于显式批准的有界恢复窗口；approval 文件不会因为存在就自动执行。
- **真实视频验证记录** —— 见 [`docs/operations/real-video-ai-improvement-validation.md`](./docs/operations/real-video-ai-improvement-validation.md)，记录命令、视频、输出目录、模型/模式、耗时、关键产物、视觉检查和最终 tracking/follow-cam/highlight 是否可播放。

### Replit 工作流（自动管理）

| 工作流                                     | 命令                                                                                |
| ------------------------------------------ | ----------------------------------------------------------------------------------- |
| `Python FastAPI Backend`                   | `python -m uvicorn football_tracking.api.app:app --host 0.0.0.0 --port 8000 --reload` |
| `artifacts/api-server: API Server`         | `pnpm --filter @workspace/api-server run dev`                                       |
| `artifacts/web: web`                       | `pnpm --filter @workspace/web run dev`                                              |
| `artifacts/mockup-sandbox: Component Preview` | `pnpm --filter @workspace/mockup-sandbox run dev`                                |

### 验证命令

```bash
# 类型检查整库
pnpm run typecheck

# 单独检查前端或代理
pnpm --filter @workspace/web run typecheck
pnpm --filter @workspace/api-server run typecheck

# 代理冒烟测试
curl -s localhost:80/api/healthz                              # Node 自身健康
curl -s localhost:80/api/health                               # Python 端健康
curl -s localhost:80/api/inputs                               # 列源视频
curl -s -X POST -H "Content-Type: application/json" \
     -d '{}' localhost:80/api/inputs/field-suggestion         # 应当返回 422（校验失败）
```

### 环境变量

| 名称                       | 必需 | 用途                                                                |
| -------------------------- | ---- | ------------------------------------------------------------------- |
| `SESSION_SECRET`           | 是   | Express session 密钥                                                |
| `PYTHON_API_URL`           | 否   | Python 后端地址，默认 `http://localhost:8000`                       |
| `PROVIDER_OPENAI_API_KEY`  | 否   | OpenAI key；不设则使用本地启发式建议                                |
| `PROVIDER_OPENAI_BASE_URL` | 否   | 自定义 OpenAI 兼容 base URL                                         |
| `PROVIDER_OPENAI_CHAT_MODEL` | 否 | 自定义对话模型名                                                    |

这些都通过 Replit Secrets 配置，**不要**写到 `.env` 里。

### 上手流程

1. 把你的 `.mp4` 视频放进 `python_backend/data/`。
2. 确认 `python_backend/weights/football_ball_yolo.pt` 存在（或在 YAML 里改 `detector.model_path`）。
3. 打开网页预览，进入「跑基线」，选视频和配置，可以让 AI 给球场建议，可以填一个帧范围先试跑一小段，然后点「启动基线任务」。
4. 在「历史」页看进度。
5. 完成后到「成品任务」渲染 16:9 跟随视频，或从事件候选生成集锦短片；也可以到「AI 分析」获取调参建议。

### 与上游的差异

- 上游 `python_backend/frontend/` 的旧 UI 已被 `artifacts/web/` 取代；归档副本已移除，以保持仓库目录精简。
- 在 FastAPI 前面加了一个 Node.js Express **反向代理**，匹配 Replit 的路径路由模型，也方便本地调用。
- 前端新增了：5 个页面 + 侧边栏、概览页、暗黑/明亮主题、中英切换、移动端响应式布局。
- 「跑基线」UI 增加了 `start_frame` / `max_frames` 帧范围（后端早已支持，只是 UI 没暴露）。
- 后端新增审核产物（`ball_audit.json`、`ai_review_triggers.json`、`camera_motion_audit.json`、`event_candidates.json`，以及可用时的 `player_tracks.json` 等球员产物），并支持跟随镜头成品和集锦短片两类子渲染任务。
