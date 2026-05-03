# Football Tracking Workspace

## Overview
A pnpm monorepo that provides a responsive React UI for the **football video tracking** pipeline (originally from `github.com/ferryhe/football_tracking`). The UI lets users pick a source video, calibrate the playing field, run the tracking pipeline, review AI suggestions, and render a follow-cam deliverable.

## Architecture

```
Browser
  ↓
artifacts/web              (React + Vite + shadcn/ui frontend)
  ↓ /api/*
artifacts/api-server       (Node.js Express, acts as reverse proxy)
  ↓ /api/v1/*
python_backend             (FastAPI service that runs the actual tracking pipeline)
```

- The Node.js api-server proxies every `/api/*` (except `/api/healthz`) to the Python FastAPI service via `pathRewrite: { "^/": "/api/v1/" }`.
- `PYTHON_API_URL` env var controls the proxy target (default `http://localhost:8000`).
- The Python backend lives in `python_backend/` and is **not** part of the pnpm workspace; it is its own Python project with its own `pyproject.toml`.

## Workflows

| Workflow | Purpose |
|----------|---------|
| `Python FastAPI Backend` | `uvicorn` running `football_tracking.api.app:app` on port 8000 |
| `artifacts/api-server: API Server` | Express proxy on port 8080 |
| `artifacts/web: web` | Vite dev server for the frontend |
| `artifacts/mockup-sandbox: Component Preview Server` | Component preview (unused for this app) |

## Frontend pages (artifacts/web)

| Path | Page | Purpose |
|------|------|---------|
| `/` | Dashboard | System status, recent runs, available configs |
| `/baseline` | Baseline | Pick video + config, **preview field + accept AI field setup**, set frame range, launch baseline run |
| `/ai` | AI Analysis | For any completed or failed run, request AI tracking improvement suggestions; visualises zone overlays |
| `/deliverable` | Deliverable | Render a follow-cam 16:9 video from a completed run |
| `/history` | History | Filter / search past runs and delete outputs |

### Baseline page highlights
- **Field Setup card** — captures a sample frame from the chosen video and lets the user request an AI suggestion that marks the playing field (`config_patch` is forwarded to the run when accepted).
- **Frame Range** — optional `start_frame` and `max_frames` inputs let users do quick partial-clip tests.
- After starting a run the user is auto-navigated to `/history` so they can watch progress.

## Stack
- **Monorepo tool**: pnpm workspaces
- **Node.js**: 24
- **Frontend**: React 19 + Vite + TypeScript + shadcn/ui + TanStack Query + wouter
- **API server**: Express 5 + http-proxy-middleware
- **Python backend**: FastAPI + Uvicorn + ultralytics/sahi/torch (CPU)
- **i18n**: simple language context, supports EN / 中文
- **Theme**: light / dark mode toggle (saved in localStorage)

## Key API endpoints (proxied via /api)
- `GET /api/healthz` — frontend Node.js health (local)
- `GET /api/health` — Python backend health
- `GET /api/configs`, `DELETE /api/configs?name=…`
- `GET /api/inputs`, `DELETE /api/inputs?name=…`
- `POST /api/inputs/field-preview` — capture a sample frame
- `POST /api/inputs/field-suggest` — AI-suggest field zones
- `GET /api/runs`, `POST /api/runs`, `DELETE /api/runs?run_id=…`
- `POST /api/runs/{run_id}/follow-cam-render`
- `POST /api/ai/recommend`, `POST /api/ai/explain`

## Environment variables
- `SESSION_SECRET` — Express session secret (required)
- `PYTHON_API_URL` — overrides Python backend URL (optional, defaults to `http://localhost:8000`)
- `PROVIDER_OPENAI_API_KEY` — optional OpenAI key for richer AI recommendations. If unset, the Python backend falls back to local heuristics.
- `PROVIDER_OPENAI_BASE_URL`, `PROVIDER_OPENAI_CHAT_MODEL` — optional overrides.

These are stored as Replit Secrets, **not** in `.env` files.

## Repository layout
```
artifacts/
  api-server/           Express proxy to Python backend
  web/                  React frontend (5 pages, i18n, shadcn/ui)
  mockup-sandbox/       Component preview (unused for this app)
python_backend/         FastAPI tracking service (standalone Python project)
lib/                    Shared TS libs (workspace)
scripts/                Workspace utility scripts
```

## Conventions
- Always reach services through the shared proxy `localhost:80/api/...`, not service ports directly.
- Frontend uses relative `/api/*` URLs.
- See the `pnpm-workspace` skill for monorepo specifics.
