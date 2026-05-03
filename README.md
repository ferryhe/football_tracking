# Football Tracking Workspace

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
- The **Python backend** lives in `python_backend/` and is **not** part of the pnpm workspace; it's a standalone Python project with its own `pyproject.toml`. Its endpoints are preserved unchanged from upstream.
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
| `/deliverable`| Deliverable  | Render a follow-cam 16:9 deliverable from a completed run                                                |
| `/history`    | History      | Filter & search past runs, delete outputs                                                                |

Highlights of the new Baseline page:

- **Field Setup card** — captures a sample frame from the chosen video, requests an AI suggestion that marks the playing field, and forwards the accepted `config_patch` to the run. Suggestion is auto-invalidated when the source video or config changes.
- **Frame Range** — optional `start_frame` and `max_frames` inputs let you do quick partial-clip tests (leave both empty to process the full video).
- **Auto-redirect** — after a run is queued, the user is sent to `/history` to watch progress.

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
5. After a run completes, visit **Deliverable** to render a 16:9 follow-cam video, or **AI Analysis** to ask for tuning suggestions.

### What changed vs. upstream

- The original React/Vite UI in `python_backend/frontend/` has been **replaced** by `artifacts/web/`; the archived copy was removed to keep this repo layout minimal.
- A Node.js Express **reverse proxy** sits in front of FastAPI to fit Replit's path-routed proxy and to simplify local dev URLs.
- The frontend gained: 5 pages with sidebar nav, Dashboard overview, dark/light mode, EN/中文 i18n, mobile responsive layout.
- Frame-range partial-clip runs (`start_frame` / `max_frames`) were added to the baseline UI; the backend already accepted these fields.

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
- **Python 后端** 是独立 Python 项目，**不在** pnpm workspace 里；接口与上游完全一致，没动过。
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
| `/deliverable` | 成品任务 | 基于已完成的基线任务渲染干净的 16:9 跟随裁剪视频                                    |
| `/history`     | 历史     | 过滤 / 搜索过往任务、删除输出                                                       |

新版「跑基线」页要点：

- **球场设置卡片** —— 抽取一帧预览，让 AI 自动识别球场区域；接受后建议的 `config_patch` 会随任务提交。源视频或配置变更时建议自动失效。
- **帧范围** —— 可选 `start_frame` / `max_frames`，便于快速试跑一小段（留空则处理整段）。
- **自动跳转** —— 任务排队后自动跳到「历史」页让你看进度。

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
5. 完成后到「成品任务」渲染 16:9 跟随视频，或到「AI 分析」获取调参建议。

### 与上游的差异

- 上游 `python_backend/frontend/` 的旧 UI 已被 `artifacts/web/` 取代；归档副本已移除，以保持仓库目录精简。
- 在 FastAPI 前面加了一个 Node.js Express **反向代理**，匹配 Replit 的路径路由模型，也方便本地调用。
- 前端新增了：5 个页面 + 侧边栏、概览页、暗黑/明亮主题、中英切换、移动端响应式布局。
- 「跑基线」UI 增加了 `start_frame` / `max_frames` 帧范围（后端早已支持，只是 UI 没暴露）。
