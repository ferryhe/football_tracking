# Frontend Phase 1 Execution Plan

## Goal

Turn the current backend-only system into a usable local product shell without changing the tracking core.

Phase 1 should deliver:

- a backend API shell
- a frontend shell
- a run/review workflow
- a config studio
- an AI-native recommendation surface with explicit confirmation

It should not yet deliver:

- cloud deployment
- multi-user auth
- background job workers
- collaboration features
- full autonomous AI actions

## Why Phase 1 Starts Here

The backend is already strong enough to expose product value:

- raw tracking works
- post-cleanup works
- follow-cam works
- outputs are artifact-based and reviewable

The missing layer is product orchestration:

- choose config
- start run
- review outputs
- adjust settings
- accept or reject AI recommendations

## Product Boundary

Frontend should be a thin orchestration and review layer.

The backend remains the only authority for:

- config parsing
- pipeline execution
- artifact generation
- cleanup logic
- follow-cam logic
- recommendation evidence generation

Frontend must not:

- run the tracker directly
- parse YAML as business logic
- re-implement cleanup or follow-cam rules

## Phase 1 Deliverables

### 1. FastAPI shell

Add a lightweight service module, recommended path:

```text
football_tracking/
  api/
    __init__.py
    app.py
    schemas.py
    routes/
      health.py
      configs.py
      runs.py
      artifacts.py
```

Responsibilities:

- list configs
- load config details
- create a run request
- report run state
- list artifacts
- expose reports and metadata

Phase 1 should keep actual run execution local and simple.

### 2. Run registry

Introduce a local run registry owned by backend.

Recommended location:

- `data/run_registry.json`

Each run record should contain:

- `run_id`
- `created_at`
- `input_video`
- `config_name`
- `output_dir`
- `status`
- `modules_enabled`
  - `postprocess`
  - `follow_cam`
- `artifacts`
- `stats`
- `notes`

This avoids treating output folders as the only UI state.

### 3. React frontend shell

Add:

```text
frontend/
  src/
    app/
    components/
    pages/
    features/
      dashboard/
      runs/
      review/
      config-studio/
      ai-panel/
    lib/
      api/
      query/
      state/
```

Recommended stack:

- Vite
- React
- TypeScript
- React Router
- TanStack Query
- Zustand
- `shadcn/ui`

### 4. Pages

Phase 1 pages:

1. Dashboard
- recent runs
- current kept baselines
- quick links to outputs

2. Runs
- choose config
- choose input video
- enable/disable post-cleanup
- enable/disable follow-cam
- start run
- see run status

3. Review
- play `annotated.mp4`
- play `annotated.cleaned.mp4`
- play `follow_cam.mp4`
- inspect `cleanup_report.json`
- inspect `follow_cam_report.json`
- jump to selected frame ranges

4. Config Studio
- grouped settings editor
- diff preview
- derive and save config

### 5. AI-native panel v1

Phase 1 AI does not directly mutate state.

It should:

- summarize a selected run
- explain current config
- recommend parameter changes
- produce a config patch preview

It should not:

- save config without approval
- launch run without approval
- delete outputs

## API Design

### Phase 1 endpoints

- `GET /api/v1/health`
- `GET /api/v1/configs`
- `GET /api/v1/configs/{name}`
- `POST /api/v1/configs/derive`
- `GET /api/v1/runs`
- `POST /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/artifacts`
- `GET /api/v1/runs/{run_id}/cleanup-report`
- `GET /api/v1/runs/{run_id}/follow-cam-report`
- `GET /api/v1/runs/{run_id}/camera-path`

### Phase 1 AI endpoints

- `POST /api/v1/ai/explain`
- `POST /api/v1/ai/recommend`
- `POST /api/v1/ai/config-diff`

Phase 1 AI endpoints should consume backend-produced evidence bundles, not raw free text only.

## Config Studio Model

Do not start with a giant YAML editor.

Primary editing model should be grouped controls:

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
- crop range
- glide/catch-up settings
- zoom confirmation settings
- lost hold and recenter
- framing

Advanced tab:

- raw YAML view
- diff against base config

## AI Recommendation Inputs

The AI panel should be grounded in:

- selected config snapshot
- raw summary stats
- cleaned summary stats
- cleanup report
- follow-cam report
- selected frame ranges
- artifact names and locations

That means the backend should prepare one evidence object per run for AI use.

## AI Recommendation Outputs

Each recommendation response should include:

- short diagnosis
- recommended parameter edits
- expected tradeoff
- confidence
- patch preview
- whether the change should trigger a new run

## Execution Sequence

### Branch 1: API shell

Branch name:

- `feat/frontend-api-shell`

Tasks:

- add FastAPI app shell
- add health/config/run/artifact endpoints
- add run registry
- no frontend yet

Success:

- backend can serve config metadata and run history
- backend can register local runs

### Branch 2: Frontend shell

Branch name:

- `feat/frontend-react-shell`

Tasks:

- create `frontend/`
- wire routing
- add API client
- add Dashboard / Runs / Review skeleton

Success:

- frontend runs locally
- can read real backend data

### Branch 3: Config Studio

Branch name:

- `feat/config-studio`

Tasks:

- grouped settings editor
- derived config save flow
- diff preview

Success:

- operator no longer needs to edit YAML by hand for common settings

### Branch 4: AI panel v1

Branch name:

- `feat/ai-panel-v1`

Tasks:

- add recommendation and explanation panel
- show patch preview
- require confirmation before save/run

Success:

- AI can recommend changes without silently applying them

## UX Principles

The frontend should feel AI-native, but controlled:

- AI proposes, user confirms
- review first, modify second
- artifacts stay central
- every recommendation ties back to run evidence

The first release should be practical, not flashy.

## Acceptance Criteria

Phase 1 is successful if:

- a user can launch a run without manual YAML editing
- a user can review raw, cleaned, and follow-cam outputs in one place
- a user can inspect cleanup and follow-cam reports
- a user can accept AI-suggested config changes through a visible diff
- no frontend logic duplicates the backend tracking pipeline

## Recommended Next Step

Start with backend API shell first.

Do not build the React app before the backend has:

- config discovery
- run registry
- artifact listing
- report endpoints

That keeps the frontend thin and prevents the UI from hardcoding filesystem assumptions.
