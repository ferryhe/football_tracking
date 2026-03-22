# High-Resolution Football Ball Tracking

This repository tracks a single in-play football from `5120 x 1440 / 20 FPS` fisheye-style match video.
The current backend is stabilized around two practical workflows:

- raw tracking
- raw tracking plus conservative post-cleanup
- raw tracking plus post-cleanup plus 16:9 follow-cam rendering

## Current Recommended Configs

- `config/real_first_run.yaml`
  Use for short debugging runs and first-pass tuning on the first 200 frames.
- `config/real_best_full.yaml`
  Best current full-video raw tracking config.
- `config/real_v24_full_postclean.yaml`
  Best current full-video delivery config with post-cleanup enabled.
  It also enables follow-cam rendering.

## Kept Output Baselines

- `outputs/real_first_run_full_accept000`
  Early historical baseline kept for comparison.
- `outputs/real_best_full`
  Best current raw full-video output.
- `outputs/real_v24_full_postclean`
  Best current cleaned full-video output.

## Pipeline

The active backend flow is:

1. Detection: YOLO + SAHI candidate generation
2. Filtering: confidence, size, aspect ratio, base spatial filtering
3. Scene bias: ground polygon, negative zones, dynamic air recovery
4. Selection: choose one candidate using distance, direction, velocity, and history
5. Tracking: state machine + Kalman CA + adaptive gating + burst recovery
6. Raw export: video, CSV, debug JSONL
7. Postprocess cleanup: conservative cleanup of short isolated noise islands
8. Follow-cam: render a smoothed 16:9 auto-follow view from the cleaned track

## Main Modules

- `football_tracking/config.py`
- `football_tracking/pipeline.py`
- `football_tracking/scene_bias.py`
- `football_tracking/tracker.py`
- `football_tracking/postprocess.py`
- `football_tracking/types.py`

## Environment

- Windows 10 / 11
- Python 3.10 or 3.11
- NVIDIA GPU, recommended 8 GB VRAM or higher
- CUDA runtime and cuDNN installed correctly

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

## Data Layout

```text
foot_ball_tracking/
|-- data/
|   `-- raw5760x144020fps.mp4
`-- weights/
    `-- football_ball_yolo.pt
```

If your local names differ, update `input_video` and `detector.model_path` in the chosen config.

## Run

Short integration run:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_first_run.yaml
```

Full raw tracking:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_best_full.yaml
```

Full cleaned delivery:

```powershell
.\.venv\Scripts\python.exe main.py --config config/real_v24_full_postclean.yaml
```

One-click local UI startup:

```powershell
.\start_ui.cmd
```

`start_ui.cmd` now waits for the backend health endpoint before launching the Vite frontend.
The frontend also polls the backend every few seconds, so it can recover if the API starts slightly later
or is restarted during local development.

## Outputs

Raw tracking normally writes:

- `annotated.mp4`
- `ball_track.csv`
- `debug.jsonl`

When postprocess is enabled it also writes:

- `annotated.cleaned.mp4`
- `ball_track.cleaned.csv`
- `debug.cleaned.jsonl`
- `cleanup_report.json`

When follow-cam is enabled it also writes:

- `follow_cam.mp4`
- `camera_path.csv`
- `follow_cam_report.json`

`cleanup_report.json` is the main handoff artifact for future UI integration. It records:

- which frames were modified by cleanup
- why they were modified
- which nuisance zone was hit
- which short detected islands were scrubbed

`follow_cam_report.json` records:

- which track source was used (`raw` or `cleaned`)
- output resolution
- crop size range
- status counts carried into follow-cam rendering

## Postprocess Scope

The current post-cleanup is intentionally conservative. It targets only very short isolated
`Detected` islands so that it can:

- remove obvious spare-ball or head-like noise
- avoid damaging the main recovery gains already achieved in raw tracking
- stay compatible with later manual or UI-assisted tuning

Current postprocess controls are defined in `config/real_v24_full_postclean.yaml`, including:

- `nuisance_zones`
- `protected_ranges`
- island length threshold
- jump distance threshold
- low-confidence threshold

Current follow-cam controls are also defined in `config/real_v24_full_postclean.yaml`, including:

- output resolution
- crop size range
- dead-zone size
- glide / catch-up pan behavior
- speed-based zoom out
- zoom confirmation, deadband, and hold behavior
- predicted and lost behavior
- home framing and recenter behavior

## Known Limits

- Fast airborne ball segments can still drop detector recall.
- Keeper possession, heavy occlusion, and out-of-field spare balls can still create local noise.
- Some bad segments are not short isolated islands, so they need stronger temporal logic or manual protection.
- The first follow-cam version is intentionally conservative and may lag behind very fast ball flight.

## Follow-Cam Design Notes

The current follow-cam is intentionally closer to a slow operator camera than a frame-perfect crop.

It uses:

- `glide` and `catch_up` pan states
- zoom deadband and confirmation windows
- slower zoom-in than zoom-out
- hold behavior so it does not immediately reverse zoom direction
- weaker camera response on `Predicted`
- recenter behavior after longer `Lost` stretches

The goal is to reduce visual jitter and avoid amplifying single-frame tracking noise.

## Repo Conventions

- `outputs/`, `data/`, and `weights/` are ignored and are not committed.
- The repo keeps only the most useful configs and docs, not every historical experiment config.
- New work should start from:
  - `config/real_first_run.yaml`
  - `config/real_best_full.yaml`
  - `config/real_v24_full_postclean.yaml`

## Frontend Planning Docs

- `docs/plans/2026-03-21-ai-native-frontend-plan.md`
- `docs/plans/2026-03-21-frontend-phase1-execution-plan.md`

## API Shell

The repo now includes a local FastAPI shell for frontend integration.

Run locally:

```powershell
.\.venv\Scripts\python.exe -m uvicorn football_tracking.api.app:app --reload
```

The Vite frontend proxies `/api/*` to `http://127.0.0.1:8000`, so the default local setup does not need
`VITE_API_BASE_URL`. If you want the frontend to point at a different backend, set `VITE_API_BASE_URL`
explicitly.

Current phase-1 endpoints:

- `GET /api/v1/health`
- `GET /api/v1/inputs`
- `GET /api/v1/configs`
- `GET /api/v1/configs/{name}`
- `POST /api/v1/configs/derive`
- `GET /api/v1/runs`
- `POST /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/artifacts`
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_name}`
- `GET /api/v1/runs/{run_id}/cleanup-report`
- `GET /api/v1/runs/{run_id}/follow-cam-report`
- `GET /api/v1/runs/{run_id}/camera-path`

## Current Local UI Shape

The local UI is intentionally being simplified into a single AI-native workspace instead of a traditional
multi-page control surface.

The primary workflow is now:

1. Put a source video under `data/`
2. Select that video from the discovered input list
3. Launch one baseline run
4. Review the latest output in-place
5. Ask AI to recommend and run the next config from the selected run evidence

The default workspace keeps only four things in focus:

- input directory and selectable videos
- current / recent tasks
- latest outputs
- AI console

Detailed reports, patch previews, and config diffs stay secondary and are shown only when needed.
- `POST /api/v1/ai/explain`
- `POST /api/v1/ai/recommend`
- `POST /api/v1/ai/config-diff`

The API shell is filesystem-backed:

- configs come from `config/`
- run history comes from `data/run_registry.json`
- kept baselines are discovered from `outputs/`
- new API-triggered runs default to `outputs/api_runs/<run_id>/`

AI behavior:

- if `PROVIDER_OPENAI_API_KEY` is configured in `.env`, the backend uses the OpenAI provider for:
  - `POST /api/v1/ai/explain`
  - `POST /api/v1/ai/recommend`
- if no provider is configured, the backend falls back to local heuristic suggestions

## Frontend Shell

The repo now includes a local React/Vite frontend shell in `frontend/`.

Run locally:

```powershell
# terminal 1
.\.venv\Scripts\python.exe -m uvicorn football_tracking.api.app:app --reload

# terminal 2
cd frontend
npm install
npm run dev
```

Current frontend scope:

- Single workspace view for:
  - choosing one discovered input video
  - choosing one kept baseline config
  - launching one baseline run
  - reviewing the focused run evidence in place
- AI-native side panel that can:
  - explain a selected run
  - generate a grounded recommendation
  - preview a config patch
  - derive a new generated config with explicit confirmation
