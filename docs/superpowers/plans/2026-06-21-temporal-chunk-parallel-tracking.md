# Temporal Chunk Parallel Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace always-on spatial tiled detection as the default speed strategy with time-sliced full-frame tracking, then reserve spatial tiling/high-recall reruns for targeted uncertain windows.

**Architecture:** Keep `BallTrackingPipeline` as the frame-range worker, but add an explicit detector inference mode so chunk workers can run direct full-frame YOLO instead of SAHI. The temporal orchestration layer splits a video into overlapping chunks, runs each chunk raw-only in isolated directories, stitches both `ball_track.csv` and `debug.jsonl`, then runs global postprocess, metrics, follow-cam, highlights, and review packets on the merged output.

**Tech Stack:** Python 3.10/3.11, OpenCV, existing dataclass/YAML config loader, Windows-safe Python subprocess workers, unittest, current FastAPI service patterns.

---

## Current Baseline And Constraint

- PR0 has already formalized the review-packet work from the previous real-video pass and was merged before PR1 started.
- PR1+ must continue from refreshed `main`; each PR uses a fresh branch, agent review, local verification, GitHub CI/Copilot wait, and merge before the next PR starts.
- This plan file is introduced after PR0 as the execution tracker for the remaining temporal-chunk series.

## Revised PR Series

### PR0: Formalize Review Packets And Real Tuned Config

**Branch:** `feat/review-packets`

**Status:** Done and merged as PR #16.

**Purpose:** Land the already-built review-packet capability so later temporal-chunk work has a stable AI-review target.

**Files:**
- Create: `python_backend/football_tracking/review_packets.py`
- Create: `python_backend/scripts/build_review_packets.py`
- Create: `python_backend/tests/test_review_packets.py`
- Create: `python_backend/config/real_tuned_strict_bottom_full.yaml`
- Modify: `python_backend/football_tracking/metrics.py`
- Modify: `python_backend/tests/test_metrics.py`

- [x] **Step 1: Verify PR0 scope**

Run:

```powershell
git status --short --branch
```

Expected: only the PR0 files above are modified/untracked. If this plan file or other docs appear, stash or move them out of the PR0 branch before continuing.

- [x] **Step 2: Run focused tests**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_review_packets python_backend.tests.test_metrics.MetricsTests.test_build_metrics_report_includes_compact_review_packet_summary
```

Expected: all tests pass.

- [x] **Step 3: Run full backend tests**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

- [x] **Step 4: Regenerate review packets on the real output**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe python_backend\scripts\build_review_packets.py python_backend\outputs\tuned_strict_bottom_full --input-video python_backend\data\raw5760x144020fps.mp4 --follow-cam-video python_backend\outputs\tuned_strict_bottom_full\follow_cam.tuned.mp4 --max-packets 10
```

Expected summary:

```text
packet_count: 10
media_packet_count: 10
```

- [x] **Step 5: Commit and publish**

Commit message:

```text
feat: add review packet generation
```

Do not merge until spec-compliance and code-quality reviews pass.

### PR1: Temporal Chunk Planning And Detector Mode Config

**Branch:** `feat/temporal-chunk-config`

**Purpose:** Add config and pure planning logic, plus the detector mode required to actually avoid SAHI during chunked full-frame runs.

**Files:**
- Modify: `python_backend/football_tracking/config.py`
- Modify: `python_backend/football_tracking/detector.py`
- Create: `python_backend/football_tracking/temporal_chunks.py`
- Create: `python_backend/tests/test_temporal_chunks.py`
- Modify: `python_backend/tests/test_config_and_provider.py`
- Modify: `python_backend/config/default.yaml`

**Config shape:**

```yaml
detector:
  inference_mode: "sahi"  # "sahi" or "direct_full_frame"

temporal_chunks:
  enabled: false
  chunk_frames: 1200
  overlap_frames: 80
  max_workers: 1
  allow_gpu_oversubscription: false
  devices: []
  decode_preroll_frames: 120
  output_dir_name: "chunks"
  merge_strategy: "overlap_quality"
```

**Core API:**

```python
@dataclass(slots=True)
class TemporalChunkConfig:
    enabled: bool = False
    chunk_frames: int = 1200
    overlap_frames: int = 80
    max_workers: int = 1
    allow_gpu_oversubscription: bool = False
    devices: tuple[str, ...] = ()
    decode_preroll_frames: int = 120
    output_dir_name: str = "chunks"
    merge_strategy: str = "overlap_quality"


@dataclass(frozen=True, slots=True)
class TemporalChunk:
    index: int
    decode_start_frame: int
    start_frame: int
    end_frame: int
    core_start_frame: int
    core_end_frame: int
    output_dir_name: str


def plan_temporal_chunks(
    *,
    source_total_frames: int,
    chunk_frames: int,
    overlap_frames: int,
    start_frame: int = 0,
    max_frames: int | None = None,
    decode_preroll_frames: int = 0,
) -> list[TemporalChunk]:
    """Return global-frame chunks whose core ranges cover the requested runtime frame range exactly once."""
```

- [x] **Step 1: Write failing planner and detector-mode tests**

Test cases:

```python
def test_plan_temporal_chunks_adds_overlap_but_keeps_core_ranges_non_overlapping(self) -> None:
    chunks = plan_temporal_chunks(source_total_frames=250, chunk_frames=100, overlap_frames=10)
    self.assertEqual(
        [(c.decode_start_frame, c.start_frame, c.end_frame, c.core_start_frame, c.core_end_frame) for c in chunks],
        [(0, 0, 109, 0, 99), (90, 90, 209, 100, 199), (190, 190, 249, 200, 249)],
    )


def test_plan_temporal_chunks_respects_runtime_start_and_max_frames(self) -> None:
    chunks = plan_temporal_chunks(
        source_total_frames=500,
        chunk_frames=100,
        overlap_frames=10,
        start_frame=50,
        max_frames=180,
    )
    self.assertEqual((50, 149), (chunks[0].core_start_frame, chunks[0].core_end_frame))
    self.assertEqual((150, 229), (chunks[-1].core_start_frame, chunks[-1].core_end_frame))


def test_plan_temporal_chunks_keeps_decode_preroll_separate_from_overlap(self) -> None:
    chunks = plan_temporal_chunks(
        source_total_frames=250,
        chunk_frames=100,
        overlap_frames=10,
        decode_preroll_frames=30,
    )
    self.assertEqual((60, 90, 209, 100, 199), (
        chunks[1].decode_start_frame,
        chunks[1].start_frame,
        chunks[1].end_frame,
        chunks[1].core_start_frame,
        chunks[1].core_end_frame,
    ))


def test_detector_inference_mode_rejects_unknown_values(self) -> None:
    with self.assertRaises(ValueError):
        load_config(config_with_detector_inference_mode("tile_everything"))
```

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_temporal_chunks python_backend.tests.test_config_and_provider
```

Expected: fail because module/config does not exist yet.

- [x] **Step 2: Implement planner and config parsing**

Implementation rules:
- `chunk_frames > 0`.
- `0 <= overlap_frames < chunk_frames`.
- `max_workers >= 1`.
- `decode_preroll_frames >= 0`.
- `detector.inference_mode` is `"sahi"` or `"direct_full_frame"`.
- Core ranges cover the effective global frame range exactly once.
- Chunk ranges may overlap for context and decode preroll.

- [x] **Step 3: Implement direct full-frame detector path**

Implementation rules:
- `"sahi"` preserves current behavior.
- `"direct_full_frame"` calls the underlying YOLO model once on the full frame and does not call SAHI slicing.
- Keep candidate filtering and selection unchanged.

- [x] **Step 4: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_temporal_chunks python_backend.tests.test_config_and_provider
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

### PR2: Raw-Only Chunk Worker And Direct Full-Frame Proof

**Branch:** `feat/temporal-chunk-worker`

**Purpose:** Prove one chunk can run raw-only with direct full-frame inference and without running postprocess/follow-cam inside the chunk.

**Files:**
- Create: `python_backend/football_tracking/chunk_runner.py`
- Create: `python_backend/football_tracking/chunk_worker.py`
- Create: `python_backend/tests/test_chunk_runner.py`
- Modify: `python_backend/main.py` only if PR2 exposes the worker through the primary entrypoint. Current PR2 keeps `main.py` unchanged because PR3 owns the `temporal_chunks.enabled` orchestration switch.

**Core API:**

```python
def build_chunk_config(config: AppConfig, chunk: TemporalChunk, chunk_output_dir: Path) -> AppConfig:
    """Copy AppConfig for one raw-only chunk."""


def write_chunk_config(config: AppConfig, chunk: TemporalChunk, chunks_root: Path) -> Path:
    """Write <chunks_root>/<chunk.output_dir_name>/chunk_config.yaml."""


def enforce_raw_chunk_config(config: AppConfig) -> AppConfig:
    """Apply raw-only guarantees before any chunk worker pipeline run."""


def run_chunk(config_path: Path) -> int:
    """Load a chunk config and run raw BallTrackingPipeline output only."""
```

Raw-only guarantees:

```python
chunk_config.postprocess.enabled = False
chunk_config.follow_cam.enabled = False
chunk_config.detector.inference_mode = "direct_full_frame"
chunk_config.temporal_chunks.enabled = False
```

CLI module:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m football_tracking.chunk_worker --config <chunk_config.yaml>
```

- [x] **Step 1: Write failing raw-only config tests**

Test:

```python
def test_build_chunk_config_disables_global_outputs_inside_chunk(self) -> None:
    chunk_config = build_chunk_config(config, chunk, temp_dir / "chunk_000")
    self.assertFalse(chunk_config.postprocess.enabled)
    self.assertFalse(chunk_config.follow_cam.enabled)
    self.assertEqual("direct_full_frame", chunk_config.detector.inference_mode)
    self.assertEqual(chunk.decode_start_frame, chunk_config.runtime.start_frame)
    self.assertEqual(chunk.end_frame - chunk.decode_start_frame + 1, chunk_config.runtime.max_frames)
```

- [x] **Step 2: Implement chunk worker**

Implementation rules:
- Write chunk configs under `<chunks_root>/<chunk.output_dir_name>/chunk_config.yaml`.
- Generated chunk configs must set `temporal_chunks.enabled = false` to avoid recursive orchestration if they are ever passed to the primary entrypoint.
- `run_chunk()` must reapply raw-only guarantees after `load_config()` before constructing `BallTrackingPipeline`.
- Chunk worker returns non-zero on exception.
- Chunk output includes raw `ball_track.csv` and `debug.jsonl` when normal output settings request them.
- Chunk worker does not write cleaned CSV, follow-cam, review packets, or highlights.

- [x] **Step 3: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_chunk_runner python_backend.tests.test_temporal_chunks
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

### PR3: Sequential Runner, CSV/Debug Stitcher, And Global Postprocess

**Branch:** `feat/temporal-chunk-sequential-runner`

**Purpose:** Run all chunks sequentially, merge raw outputs, and then run existing global postprocess/follow-cam once on the merged output.

**Files:**
- Create: `python_backend/football_tracking/chunk_stitcher.py`
- Create: `python_backend/tests/test_chunk_stitcher.py`
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/tests/test_chunk_runner.py`
- Modify: `python_backend/main.py`

**Core API:**

```python
def stitch_chunk_outputs(
    chunks: list[TemporalChunk],
    chunk_dirs: list[Path],
    output_dir: Path,
    output_config: OutputConfig | None = None,
) -> dict[str, Any]:
    """Merge ball_track.csv and debug.jsonl into final output_dir."""


def run_temporal_chunks(config: AppConfig, progress_callback=None, should_cancel=None) -> None:
    """Run chunk workers sequentially, stitch outputs, then run global postprocess/follow-cam if enabled."""
```

Merge rules:
- Merge `ball_track.csv` and `debug.jsonl` from the same selected frame source.
- Preserve only selected global frame rows.
- Keep one row and one debug JSONL line per global frame.
- Reject missing CSV/debug pairs.
- PR3 keeps the deterministic core-frame default and writes a `boundary_events` report field.
- Boundary quality scoring and `boundary_review_event` population are deferred to the metrics/review-trigger PRs.

- [x] **Step 1: Write failing stitcher tests**

Create fixture CSV and debug JSONL files:

```text
chunk_000: frames 0..109
chunk_001: frames 90..209
chunk_002: frames 190..249
```

Expected output keeps core frames:

```text
0..99 from chunk_000
100..199 from chunk_001
200..249 from chunk_002
```

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_chunk_stitcher
```

Expected: fail until implementation exists.

- [x] **Step 2: Implement stitcher**

Implementation rules:
- Preserve CSV header: `Frame,X,Y,Confidence,Status`.
- Sort merged output by frame.
- Reject duplicate selected frames with a clear `ValueError`.
- Write `temporal_chunks_report.json` with chunk count, frame count, source chunk names, chunk ranges, and a placeholder `boundary_events` list.

- [x] **Step 3: Write sequential runner tests using mock mode**

Use `MockConfig(enabled=True)` and `OutputConfig(save_video=False, save_frames=False, save_csv=True, save_debug_jsonl=True)`.

Expected:
- runner creates `chunks/chunk_000`, `chunks/chunk_001`, etc.
- merged `ball_track.csv` exists in final output dir.
- merged `debug.jsonl` exists in final output dir.
- report lists planned chunk ranges.

- [x] **Step 4: Integrate CLI entry**

Modify `python_backend/main.py`:

```python
if config.temporal_chunks.enabled:
    run_temporal_chunks(config)
else:
    pipeline = BallTrackingPipeline(config)
    pipeline.run()
```

- [x] **Step 5: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_chunk_stitcher python_backend.tests.test_chunk_runner python_backend.tests.test_pipeline
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

### PR4: Subprocess Parallel Workers And GPU-Safe Scheduling

**Branch:** `feat/temporal-chunk-parallel-workers`

**Status:** Implemented locally; focused and full backend unittest validation pass.

**Purpose:** Add Windows-safe subprocess scheduling while avoiding single-GPU oversubscription.

**Files:**
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/football_tracking/temporal_chunks.py`
- Modify: `python_backend/tests/test_chunk_runner.py`
- Modify: `python_backend/config/default.yaml`

**Design:** Use command-level subprocess workers instead of `ProcessPoolExecutor` for true runs:

```powershell
.\.venv\Scripts\python.exe -m football_tracking.chunk_worker --config <chunk_config.yaml>
```

Rules:
- Default `max_workers: 1`.
- If detector device starts with `cuda` and no multi-GPU device list is configured, cap effective workers to `1` unless `allow_gpu_oversubscription: true`.
- If `temporal_chunks.devices` contains multiple devices, assign one device per active worker.
- CPU/mock mode may use `max_workers`.
- Failure in any chunk cancels pending chunks and writes failure details.
- Capture stdout, stderr, start/end time, exit code, and chunk config path in `temporal_chunks_report.json`.

- [x] **Step 1: Write failing scheduler tests**

Test cases:

```python
def test_effective_worker_count_caps_single_gpu_without_oversubscription(self) -> None:
    self.assertEqual(
        1,
        effective_worker_count(requested=4, detector_device="cuda:0", devices=(), allow_gpu_oversubscription=False),
    )


def test_effective_worker_count_allows_cpu_parallelism(self) -> None:
    self.assertEqual(
        4,
        effective_worker_count(requested=4, detector_device="cpu", devices=(), allow_gpu_oversubscription=False),
    )


def test_effective_worker_count_uses_multi_gpu_device_list(self) -> None:
    self.assertEqual(
        2,
        effective_worker_count(
            requested=4,
            detector_device="cuda:0",
            devices=("cuda:0", "cuda:1"),
            allow_gpu_oversubscription=False,
        ),
    )
```

- [x] **Step 2: Implement scheduler**

Add:

```python
def effective_worker_count(
    *,
    requested: int,
    detector_device: str,
    devices: tuple[str, ...],
    allow_gpu_oversubscription: bool,
) -> int:
    """Return the safe worker count for CPU, single-GPU, or multi-GPU chunk execution."""
```

- [x] **Step 3: Add subprocess execution path**

Keep sequential in-process path for `effective_workers == 1`; use subprocess workers otherwise.

- [x] **Step 4: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_chunk_runner
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

Verified on 2026-06-21:

```text
python_backend.tests.test_chunk_runner: Ran 30 tests, OK
unittest discover python_backend\tests: Ran 178 tests, OK
ruff check chunk_runner/test_chunk_runner: OK
```

### PR5: Metrics, Review Packets, And API Visibility

**Branch:** `feat/temporal-chunk-artifacts`

**Purpose:** Make chunked runs visible and reviewable like normal runs.

**Files:**
- Modify: `python_backend/football_tracking/metrics.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/tests/test_metrics.py`
- Modify: `python_backend/tests/test_api_service.py`
- Modify: `python_backend/README.md`

**Metrics shape:**

```json
"temporal_chunks": {
  "enabled": true,
  "chunk_count": 5,
  "effective_workers": 1,
  "merged_frame_count": 5192,
  "overlap_frames": 80,
  "boundary_review_event_count": 2
}
```

API run execution must call `run_temporal_chunks(config, progress_callback, should_cancel)` when `config.temporal_chunks.enabled` is true. Progress should include chunk index/current frame when available, and cancellation should stop pending chunk workers.

- [ ] **Step 1: Write failing metrics test**

Create temp `temporal_chunks_report.json`, call `build_metrics_report`, assert compact summary is present.

- [ ] **Step 2: Implement compact metrics summary**

Add `compact_temporal_chunk_summary(report: dict[str, Any])`.

- [ ] **Step 3: Add API execution and artifact visibility**

Expose:

```text
temporal_chunks_report.json
chunks/<chunk_name>/ball_track.csv
chunks/<chunk_name>/debug.jsonl
```

- [ ] **Step 4: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_metrics python_backend.tests.test_api_service
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

### PR6: Triggered High-Recall Window Execution And Merge Back

**Branch:** `feat/high-recall-window-execution`

**Purpose:** Keep temporal full-frame tracking as the default, but rerun SAHI/ROI-tile only inside planned suspicious windows and reconcile accepted results back into the merged track.

**Files:**
- Create: `python_backend/football_tracking/high_recall_windows.py`
- Create: `python_backend/football_tracking/high_recall_reconcile.py`
- Create: `python_backend/tests/test_high_recall_windows.py`
- Create: `python_backend/tests/test_high_recall_reconcile.py`
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/config/default.yaml`

**Window planning inputs:**
- `ai_review_triggers.json`
- `ball_audit.json`
- `event_candidates.json`

**Output shape:**

```json
"high_recall_windows": [
  {
    "start_frame": 272,
    "end_frame": 344,
    "reason": "large_jump",
    "mode": "roi_tile",
    "priority": "high",
    "decision": "accepted"
  }
]
```

Rules:
- Never rerun full-match SAHI.
- Merge nearby windows if gap <= 30 frames.
- Cap total second-pass frame count with `max_total_frames`.
- Save window outputs under `high_recall_windows/window_###`.
- Reconcile only when the rerun improves continuity and passes jump/field/player-context checks.
- Rejected windows go to review packets instead of silently changing the track.

- [ ] **Step 1: Write failing window-planning tests**

Test:

```python
def test_high_recall_windows_merge_nearby_large_jump_triggers(self) -> None:
    windows = build_high_recall_windows(triggers, merge_gap_frames=30)
    self.assertEqual(
        [{"start_frame": 270, "end_frame": 365, "reason": "large_jump"}],
        [{"start_frame": w.start_frame, "end_frame": w.end_frame, "reason": w.reason} for w in windows],
    )
```

- [ ] **Step 2: Write failing reconcile tests**

Use synthetic CSV rows where a lost gap can be filled by a window rerun without exceeding speed/jump gates.

- [ ] **Step 3: Implement planner, execution report, and reconcile rules**

Return a report with accepted windows, rejected windows, and reasons.

- [ ] **Step 4: Run validation**

Run:

```powershell
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest python_backend.tests.test_high_recall_windows python_backend.tests.test_high_recall_reconcile
$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe -m unittest discover python_backend\tests
```

Expected: all tests pass.

## Final Acceptance For The Series

- Direct full-frame temporal chunk mode can process mock and real videos without default spatial tiling.
- Chunk merge output preserves one CSV row and one debug JSONL row per global frame.
- Global postprocess/follow-cam run after merge, not inside each chunk.
- Review packets work on merged outputs.
- Metrics show chunk count, worker count, overlap, merged frame count, and boundary review count.
- Single-GPU Windows runs default to safe sequential execution; multi-GPU/CPU can opt into parallel subprocesses.
- Spatial tiling becomes a targeted recovery tool for suspicious windows instead of the default speed strategy.
