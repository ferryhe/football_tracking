# Football Tracking Operation Guide

This guide is the practical operator playbook for the local workspace UI.

## 1. Before You Start

Make sure these are ready:

- `.venv` exists and dependencies are installed
- `frontend/node_modules` is installed
- your source video is under `data/`
- model weights are available
- at least one baseline config exists under `config/`

If you only want the simplest local launch:

```powershell
.\start_ui.cmd
```

For steadier Windows startup:

```powershell
.\start_ui.cmd --no-reload
```

## 2. What Each Tab Does

### Baseline

Use this tab to prepare the source clip and start the first run.

Main actions:

1. Choose the source video
2. Choose a baseline config
3. Capture a preview frame
4. Build field setup
5. Start the baseline run

### AI Analysis

Use this tab after you already have at least one completed run for the current source clip.

Main actions:

1. Pick a completed run
2. Click `Explain selected run`
3. Review the explanation and suggested config
4. Run the next task if the suggestion makes sense

### Deliverable Task

Use this tab when you want a clean `16:9` output from an existing run without rerunning the full baseline pipeline.

Main actions:

1. Pick a completed source run
2. Choose deliverable switches
3. Start the render

### History

Use this tab to inspect old runs and manage assets.

Main actions:

1. Filter history by `baseline / deliverable / failed`
2. Open an asset group by source video
3. Manage source files, configs, and outputs

## 3. Baseline Tab Workflow

### Step A: Select the Source Video

The selected video should already live under `data/`.

If no video appears:

- put the file under `data/`
- refresh the page
- confirm the file extension is supported

### Step B: Select the Baseline Config

Configs are shown newest-first. Start with:

- `real_first_run.yaml` for short probes
- `real_best_full.yaml` for raw full-video work
- `real_v24_full_postclean.yaml` for delivery-oriented runs

Hover labels explain:

- `Scope`
- `Cleanup`
- `Follow-cam`

### Step C: Build Field Setup

Under the selected video you will see the field setup module.

Recommended order:

1. `Capture preview`
2. `Load from config` if the YAML already contains good field geometry
3. Otherwise click `AI generate`
4. Review the field polygon and expanded polygon
5. Use quick adjustments or manual point input if needed
6. Click `Accept`

Notes:

- the preview keeps the original frame proportions
- clicking preview again cycles to another representative frame
- manual point input accepts `x,y | x,y | ...`

### Step D: Start Baseline

After field setup is accepted, click `Start baseline run`.

The run will create a new output folder under:

```text
outputs/runs/<input_slug>/<run_id>/
```

## 4. AI Analysis Tab Workflow

This tab only shows runs linked to the currently selected source clip.

Recommended order:

1. Pick the run you want to inspect
2. Click `Explain selected run`
3. Read the AI explanation
4. Review the suggested next config
5. Optionally adjust the objective and update the suggestion
6. Click `Run suggested config`

Important behavior:

- AI explanation is manual on purpose to avoid unnecessary token usage
- AI prompt/response language follows the current UI language
- derived configs are written into `config/generated/`

## 5. Deliverable Task Workflow

Use this when you already trust the track and only need a final video.

Recommended order:

1. Pick a completed source run
2. Leave deliverable options off unless you explicitly want overlays
3. Click `Start 16:9 deliverable render`

Options:

- `Prefer cleaned track CSV`
  - uses `ball_track.cleaned.csv` first when available
- `Show ball marker`
  - adds the ball marker overlay to the output
- `Show frame text / annotation`
  - adds status text and frame annotations

Default recommendation:

- keep marker off
- keep frame text off
- prefer cleaned track on

## 6. History and Asset Management

### History List

The top section shows all past runs, filtered by:

- `Baseline`
- `Deliverable`
- `Failed`

### Asset Groups

Assets are grouped by source video.

Each group header shows:

- source clip name
- last activity time
- counts for runs, configs, and outputs
- a light summary line for the latest baseline, deliverable, and failed activity

Each group opens into:

- `Source`
- `Configs`
- `Outputs`

Groups are collapsed by default.

### Deletes

Deletes require a typed confirmation:

- open the asset entry
- click `Delete`
- type `DELETE`
- confirm

This applies to:

- source videos
- config files
- output folders

## 7. Storage and Compatibility Notes

Current logical model:

- one source video can have many runs
- one config can be reused by many runs
- one run maps to one output directory
- a deliverable run may point back to a baseline run through `parent_run_id`

The app still scans legacy output folders so older experiments stay visible.

## 8. Shutdown

To stop managed UI processes:

```powershell
.\stop_ui.cmd
```

## 9. Troubleshooting

### UI will not start

Run:

```powershell
.\start_ui.cmd --no-reload
```

If that still fails, run:

```powershell
.\.venv\Scripts\python.exe scripts\start_ui.py --check
```

### Backend says a port is unavailable

The managed launcher will automatically search for a free port and print the chosen URLs.

### The field preview is bad

Click `Capture preview` again to cycle to another representative frame, then re-run config load or AI suggestion.

### AI explanation looks expensive to use repeatedly

That is why explanation is manual. The tab does not auto-explain every run.

### History shows old outputs

That is expected. The history scanner still reads legacy output layouts for backward compatibility.
