# High-Resolution Football Ball Tracking

This repository tracks a single in-play football from `5120 x 1440 / 20 FPS` fisheye-style match video.
The current backend is stabilized around two practical workflows:

- raw tracking
- raw tracking plus conservative post-cleanup

## Current Recommended Configs

- `config/real_first_run.yaml`
  Use for short debugging runs and first-pass tuning on the first 200 frames.
- `config/real_best_full.yaml`
  Best current full-video raw tracking config.
- `config/real_v24_full_postclean.yaml`
  Best current full-video delivery config with post-cleanup enabled.

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

`cleanup_report.json` is the main handoff artifact for future UI integration. It records:

- which frames were modified by cleanup
- why they were modified
- which nuisance zone was hit
- which short detected islands were scrubbed

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

## Known Limits

- Fast airborne ball segments can still drop detector recall.
- Keeper possession, heavy occlusion, and out-of-field spare balls can still create local noise.
- Some bad segments are not short isolated islands, so they need stronger temporal logic or manual protection.

## Repo Conventions

- `outputs/`, `data/`, and `weights/` are ignored and are not committed.
- The repo keeps only the most useful configs and docs, not every historical experiment config.
- New work should start from:
  - `config/real_first_run.yaml`
  - `config/real_best_full.yaml`
  - `config/real_v24_full_postclean.yaml`
