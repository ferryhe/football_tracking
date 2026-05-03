# AI-Native Frontend Plan

## Goal

Define the next product-facing phase for `foot_ball_tracking`:

- add a frontend as a first-class module in this repository
- expose the existing backend pipeline as a task-oriented service
- add an AI-native control surface that can recommend and modify settings
- keep the current tracking backend as the single source of truth

This plan assumes the current backend branch is already stable enough for productization:

- raw tracking works
- post-cleanup works
- follow-cam rendering works

## Current State

The repository already has backend capabilities for:

- full-video ball tracking
- cleaned output generation
- follow-cam 16:9 rendering
- cleanup reporting
- parameterized YAML-based control of tracking, cleanup, and follow-cam behavior

What is missing for a usable operator product:

- a frontend shell
- a task runner UX
- result review UX
- settings editing without direct YAML hand-editing
- AI assistance for recommending parameter changes

## Reference Decisions Borrowed From Neighboring Projects

The frontend structure should follow the same high-level approach used successfully in nearby repos:

- keep frontend in the same repository
- place frontend in a dedicated top-level module, not inside the Python package
- keep backend authority in Python
- use the UI as an orchestration and review layer, not as a second backend
- add AI assistance as an explicit product feature, not as hidden automation

The concrete inspirations are:

- same-repo separate frontend module pattern from `actuarial_platform`
- React + TypeScript + Vite product shell pattern from `AI_actuarial_inforsearch`

## Product Model

The product should expose three user workflows:

1. Run
- choose input video
- choose base config
- enable or disable post-cleanup
- enable or disable follow-cam
- start a task

2. Review
- watch `annotated.mp4`
- watch `annotated.cleaned.mp4`
- watch `follow_cam.mp4`
- inspect `cleanup_report.json`
- inspect `camera_path.csv`
- jump to problem frame ranges

3. Adjust
- view current settings in grouped form
- accept AI recommendations
- manually override selected parameters
- save a derived config

## Frontend Structure

Keep frontend in the same repo, but as a separate module:

```text
foot_ball_tracking/
  football_tracking/     # Python backend and pipeline
  frontend/              # React app
  docs/
```

Do not place React code under `football_tracking/`.
Do not split frontend into a separate repo yet.

## Backend Boundary

Frontend should only talk to a small service layer owned by this repo.

Frontend must not:

- read raw YAML directly from disk as its primary data source
- call OpenCV or pipeline internals directly
- re-implement cleanup or follow-cam logic

Instead, backend should expose a narrow API layer for:

- configs
- runs
- outputs
- reports
- AI recommendations

## AI-Native Design

The UI should include one visible AI panel, but the AI should be bounded by explicit tool scopes.

The AI panel should be able to:

- explain the current pipeline
- summarize output quality
- recommend tracking, cleanup, or follow-cam changes
- generate a derived config patch
- ask for confirmation before saving or launching a new run

The AI panel should not:

- silently launch runs
- silently overwrite configs
- directly mutate backend state without confirmation

## Safety Model

All write operations require user confirmation:

- save config changes
- create a derived config
- start a run
- delete or archive outputs

All AI actions must be reviewable:

- proposed parameter changes shown before apply
- config diff preview shown before save
- run launch preview shown before execute

## Recommended Stack

### Frontend

- Vite
- React
- TypeScript
- React Router
- TanStack Query
- Zustand
- a reusable component system such as `shadcn/ui`

### Backend API Layer

Recommended first backend service layer:

- FastAPI

Reason:

- easy local integration with Python pipeline code
- good fit for task APIs and structured JSON
- straightforward future support for SSE or WebSocket task progress

## Frontend Pages

Build only these first:

1. Dashboard
- recent runs
- key output directories
- last known stats

2. Runs
- create run
- list runs
- track status

3. Review
- video viewer
- frame jump
- reports

4. Config Studio
- grouped parameter editor
- AI suggestion panel
- diff preview

5. Outputs
- browse raw, cleaned, and follow-cam outputs

Do not start with:

- user accounts
- team collaboration
- multi-user permissions
- cloud deployment UX

## Backend APIs The Frontend Should Consume First

Phase 1 backend APIs:

- `GET /api/v1/health`
- `GET /api/v1/configs`
- `GET /api/v1/configs/{name}`
- `POST /api/v1/configs/derive`
- `GET /api/v1/runs`
- `POST /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/artifacts`
- `GET /api/v1/runs/{run_id}/report`

Phase 2 review APIs:

- `GET /api/v1/runs/{run_id}/cleanup-report`
- `GET /api/v1/runs/{run_id}/follow-cam-report`
- `GET /api/v1/runs/{run_id}/camera-path`
- `GET /api/v1/runs/{run_id}/frame/{frame_index}`

Phase 3 AI APIs:

- `POST /api/v1/ai/recommend`
- `POST /api/v1/ai/explain`
- `POST /api/v1/ai/config-diff`

## Run Model

Backend should formalize one run record that captures:

- input video
- config name
- output directory
- enabled modules
  - raw tracking
  - post-cleanup
  - follow-cam
- task status
- artifacts
- summary stats

This avoids treating output folders as the only state store.

## Config Studio Model

The frontend should not expose one giant YAML textarea as the primary UI.

Instead it should group settings into panels:

1. Tracking
- detector
- filtering
- scene bias
- selection
- tracking

2. Cleanup
- nuisance zones
- protected ranges
- cleanup thresholds

3. Follow-Cam
- output resolution
- crop range
- pan behavior
- zoom behavior
- lost behavior
- home framing

The YAML view can still exist as an advanced tab.

## AI Recommendation Model

The AI should operate on top of backend-produced evidence, not just free-form text.

Recommended inputs to recommendation engine:

- summary stats from raw run
- summary stats from cleaned run
- cleanup report
- follow-cam report
- selected frame ranges
- selected output files
- current config snapshot

Recommended outputs:

- explanation
- suggested parameter changes
- expected tradeoffs
- confidence
- config patch preview

## Why AI-Native Fits This Product

This repo already produces exactly the artifacts an AI assistant can reason over:

- CSV trajectories
- debug JSONL
- cleanup report
- camera path
- derived outputs

That means the AI should not be asked to guess blindly.
It should reason from run artifacts and propose controlled parameter edits.

## Development Sequence

### Branch A: API shell

- add `api/` or `football_tracking/api/` service layer
- expose health, configs, runs, and artifacts
- do not build AI features yet

### Branch B: Frontend shell

- create `frontend/`
- wire routing, layout, API client, query client
- build Dashboard, Runs, Review skeleton

### Branch C: Config Studio

- build grouped parameter panels
- support load, edit, diff, derive, save
- keep YAML view as advanced mode

### Branch D: AI panel v1

- add visible assistant panel
- support explanation and recommendation only
- no write actions without confirmation

### Branch E: AI-assisted run loop

- allow AI to propose config edits
- allow user to accept patch
- allow user to launch run from accepted patch

## Success Criteria

The first frontend branch is successful if:

- `frontend/` exists and runs locally
- users can launch a run without editing YAML by hand
- users can watch raw, cleaned, and follow-cam outputs in the UI
- users can inspect cleanup and follow-cam reports
- users can apply AI-recommended config patches with confirmation

## Recommendation

The next branch should be:

- `feat/frontend-api-and-shell`

And the first implementation target should be:

- FastAPI service shell
- React frontend shell
- read-only review pages first
- AI recommendations only after review and config editing are in place
