# RTX 5080 Throughput Optimization Plan

## Goal

Raise end-to-end throughput of the local tracking pipeline on this workstation by using the `RTX 5080` and the `20-core` CPU more effectively, without breaking baseline tracking quality.

This plan is intentionally operational:

- it starts from measured bottlenecks in this repo
- it separates low-risk wins from heavier refactors
- it includes explicit threading and multiprocessing options
- it defines success metrics before implementation

## Execution Update

Implemented on `2026-03-29`:

- `football_tracking/detector.py`
  - replaced the previous serial `SAHI -> one slice at a time` path with batched sliced YOLO inference
  - kept SAHI-style postprocess and downstream tracker / selector flow intact
- `football_tracking/config.py`
  - added `sahi.batch_size`
  - default is now `6` on this workstation
- `config/default.yaml`
  - added explicit `sahi.batch_size: 6`
- `config/real_first_run.yaml`
  - added explicit `sahi.batch_size: 6`
- `config/real_best_full.yaml`
  - added explicit `sahi.batch_size: 6`
- `config/real_v24_full_postclean.yaml`
  - added explicit `sahi.batch_size: 6`
- `scripts/run_pipeline_benchmark.py`
  - added a reusable benchmark launcher that records wall-clock runtime to `benchmark_summary.json`

Validation completed before the overnight full run:

- config tests passed
- API service tests passed
- detector benchmark on the same representative frame dropped from about `0.3497 s/frame` to about `0.1575 s/frame`
- measured detector speedup is about `2.22x`
- 100-frame end-to-end smoke run completed successfully with postprocess and follow-cam enabled
- smoke-run raw stage landed around `4.5` to `4.7 fps`, versus the earlier live baseline of about `2.5 fps`
- sampled GPU utilization during the new full run reached about `55%`, up from the earlier `12%` to `16%`

Overnight full benchmark:

- command path: `scripts/run_pipeline_benchmark.py`
- source config: `config/generated/real_v24_full_postclean_field_setup_run_20260329_005554_73c0ec04_field_setup_run_20260329_010521_8294d41d.yaml`
- output directory: `outputs/bench_phase1_full_20260329_0147`
- result file to check in the morning: `outputs/bench_phase1_full_20260329_0147/benchmark_summary.json`

## Current Machine and Run Facts

Measured on `2026-03-29` in this workspace:

- CPU: `Intel Core Ultra 7 265KF`
- CPU cores / logical processors: `20 / 20`
- RAM: about `34 GB`
- GPU: `NVIDIA GeForce RTX 5080`
- GPU VRAM: about `16 GB`
- Python env: `3.11`
- PyTorch: `2.9.1+cu130`
- Source video: `5120 x 1440`, `20 fps`, `5194` frames

Current live-run observations:

- raw tracking speed is about `2.5 fps`
- GPU utilization during raw tracking sits around `12%` to `16%`
- GPU memory use during raw tracking is about `1.6 GB` to `2.0 GB`

Interpretation:

- the current bottleneck is not raw GPU compute saturation
- the pipeline is under-feeding the GPU
- the strongest opportunity is to reduce Python-side scheduling overhead and increase useful GPU batch work

## Local Profiling Evidence

Local measurements from the current code path:

| Stage | Measurement | Notes |
| --- | --- | --- |
| Video read | `0.012` to `0.017 s/frame` | not free, but not the primary bottleneck yet |
| Current full-frame SAHI detect | average `0.3497 s/frame` | dominant hot path |
| Direct full-frame YOLO detect | average `0.0095 s/frame` | very fast, but misses the tiny ball, so not a drop-in replacement |
| Current frame slicing | `18` slices per frame | from current `5120x1440` source and `960x640` / `10%` overlap |
| Batched sliced YOLO, batch `4` | average `0.1468 s/frame` | about `2.38x` faster than current SAHI path |
| Batched sliced YOLO, batch `6` | average `0.1439 s/frame` | about `2.43x` faster than current SAHI path |

Key conclusion:

- the current `SAHI -> one slice at a time -> YOLO` flow is leaving the `RTX 5080` mostly idle
- even before any architecture changes, batching slice inference should roughly double raw detector throughput

## Bottleneck Summary

### 1. Serial sliced inference

Today each full frame is sliced, and those slices are effectively fed to the detector one by one.
This creates too many small inference launches and too much Python overhead for a GPU of this size.

### 2. Full-frame sliced detection runs on every frame

Even when the tracker already has a strong state estimate, the pipeline still pays the cost of a full-frame sliced search.
That is expensive and unnecessary for many stable tracking frames.

### 3. The run pipeline is mostly single-lane

Decode, detect, track, render, and write are mostly performed inline in one loop.
That keeps control simple, but it limits overlap between:

- CPU decode
- GPU inference
- CPU render
- disk writes

### 4. The backend job model is thread-based, not process-based

The API service launches runs on Python threads.
That is acceptable for simple local orchestration, but it is not the right long-term model for:

- true multi-core CPU work
- safe concurrent job isolation
- dedicated resource control per run

### 5. Decode is still on a general OpenCV/FFmpeg path

The current source path uses OpenCV capture.
That is simple, but it does not yet exploit NVIDIA's dedicated video decode hardware.

## Optimization Targets

### Primary target

Raise raw tracking throughput from about `2.5 fps` to at least `5 fps` on the same source clip without a material accuracy regression.

### Stretch target

Reach `8 fps+` steady-state on long stable segments by reducing full-frame searches and overlapping decode and write with inference.

### Secondary targets

- raise GPU utilization during raw tracking above `40%`
- keep GPU memory comfortably below `12 GB`
- keep tracking quality within an agreed regression threshold
- shorten wall-clock time for a full baseline run, not just detector microbenchmarks

## Workstream A: Replace Serial SAHI Inference With Batched Slice Inference

### Why this is first

This is the clearest low-risk win with the strongest measured signal.
We already validated locally that batching the current `18` slices improves detector time by about `2.4x`.

### Implementation plan

1. Add explicit batching support to detector config.
2. In `football_tracking/detector.py`, replace the current per-slice call pattern with:
   - `sahi.slicing.slice_image(...)`
   - `YOLO.predict(list_of_slices, batch=N, ...)`
   - merge results back to full-frame coordinates
   - preserve current filtering and selection behavior
3. Keep the current SAHI-style postprocess semantics as close as possible during rollout.
4. Add a benchmark script or test helper to measure detector-only latency on representative frames.

### Recommended first defaults

- `batch_size = 6` on this `RTX 5080`
- keep current `imgsz = 1280` during the first rollout
- keep current slice overlap during the first rollout

### Success metric

- full-frame sliced detector latency at or below `0.16 s/frame` on the current video

## Workstream B: Add ROI-First Detection Scheduling

### Why this matters

Even batched full-frame slicing still pays for a global search every frame.
That is not necessary when the tracker is already confident and the motion model is stable.

### Proposed policy

Use three detector modes:

1. `steady_track`
- when tracker state is `TRACKING`
- when `lost_frames == 0`
- when recent confidence is healthy
- run a small direct ROI search centered on predicted position

2. `periodic_global_refresh`
- every `N` frames during steady tracking
- or when candidate quality drops
- run the full batched sliced detector

3. `reacquire`
- when state is `PREDICTING` or `LOST`
- when confidence collapses
- when ROI search fails
- run the existing larger-window or full-frame recovery logic

### Concrete initial policy

- direct ROI detect on most `TRACKING` frames
- full batched global search every `8` to `12` frames
- immediate global search on confidence drop or sudden motion jump

### Expected impact

This is the highest-upside change after batching.
If stable segments stop paying full-frame SAHI cost every frame, the steady-state throughput should improve substantially.

### Success metric

- steady-state throughput on long stable segments exceeds `8 fps`
- no material increase in lost-track events

## Workstream C: Overlap Decode, Inference, and Writes

### Recommended concurrency model

Use a bounded producer-consumer pipeline:

1. decode worker
- reads and timestamps frames ahead of inference
- fills a small queue

2. inference worker
- owns the detector and tracker state
- consumes frames from decode queue
- produces track results and optional rendered frames

3. writer worker
- writes video, CSV, and debug JSONL
- keeps disk I/O off the inference hot path

### Why thread first

This stage is mostly I/O and C-extension heavy:

- video decode
- OpenCV encode
- file writes
- PyTorch GPU kernels

A thread-based queue model is the fastest way to prove overlap value without a large architecture change.

### Queue policy

- decode queue size: `8` to `16` frames
- write queue size: `32` to `64` items
- measure queue starvation and backpressure explicitly

### Success metric

- GPU idle gaps shrink in profiler traces
- raw fps improves beyond the detector-only gain

## Workstream D: Use Multi-Core CPU Correctly

### Important design rule

Use threads for I/O overlap.
Use processes for CPU-heavy parallel work.

Python's official docs note that `ProcessPoolExecutor` uses `multiprocessing`, which side-steps the GIL, while process inputs and outputs must be picklable.

### Best candidates for process-based parallelism

1. postprocess cleanup
- reads CSV and debug outputs
- CPU-heavy rule application
- low GPU dependence

2. follow-cam rendering
- CPU-heavy frame transform and encoding
- can potentially be chunked by frame range

3. future batch report generation
- cleanup report
- camera path report
- summary stats

### Not recommended as the first multi-process target

- the live tracker loop itself

Reason:

- it owns state tightly frame to frame
- it is easier to speed up first by batching and overlap than by splitting one tracking run across multiple CPU workers

### Recommended near-term backend job model

Move from thread-launched runs to subprocess-launched runs.

Benefits:

- cleaner failure isolation
- better CPU scheduling
- easier future support for multiple queued jobs
- easier GPU resource policies per run

## Workstream E: Move Video Decode to NVIDIA Hardware

### Why this is promising

NVIDIA's Video Codec SDK exposes hardware decode through `NVDEC`, which is separate from CUDA cores.
NVIDIA also provides `PyNvVideoCodec`, which supports:

- hardware accelerated decode on Windows and Linux
- Blackwell GPUs
- threaded decoding
- better GIL handling in the C++ layer

### Why this is not phase 1

The current detector hot path is the bigger bottleneck right now.
Decode is measurable, but it is not yet the dominant cost.

### Operational plan

1. prototype `PyNvVideoCodec` decode for the current source format
2. measure:
   - decode latency
   - host CPU usage
   - end-to-end fps
3. compare:
   - OpenCV capture
   - OpenCV capture + decode thread
   - PyNvVideoCodec threaded decoder

### Success metric

- decode stage time becomes mostly hidden behind inference

## Workstream F: Exploit 5080 Inference Better With TensorRT

### Why this matters

Ultralytics documents TensorRT as a high-speed inference path for NVIDIA GPUs, with support for:

- FP16
- INT8
- layer fusion
- better memory efficiency

Torch-TensorRT also exposes engine caching and reuse options.

### Recommended scope

Treat TensorRT as a second-phase accelerator after batched slice inference is stable.

### Operational plan

1. export the Ultralytics detector weights to TensorRT
2. benchmark:
   - PyTorch `cu130`
   - Ultralytics TensorRT export
3. validate:
   - first-run build time
   - cached-engine reuse
   - accuracy drift
   - Windows stability

### Success metric

- another meaningful detector speedup beyond batched PyTorch
- acceptable cold-start and cache behavior for local UI usage

## Workstream G: Runtime and Config Defaults for Honest Benchmarking

### Benchmark defaults

When measuring throughput, avoid mixing tuning work with optional outputs.

Recommended benchmark profile:

- `save_frames: false`
- `save_video: false` for detector-only profiling
- `save_debug_jsonl: false` for pure detector timing
- `start_ui.cmd --no-reload`

### Production defaults after tuning

Keep user-facing defaults intact, but add a documented high-throughput profile for:

- baseline tuning runs
- long video sweeps
- hardware bring-up on new GPUs

## Parallelism Decision Table

| Stage | Best primitive | Why | First implementation |
| --- | --- | --- | --- |
| Frame decode | background thread or `PyNvVideoCodec` threaded decoder | overlaps I/O with GPU work | thread first |
| Sliced detector | single GPU worker with batched inputs | best way to keep one GPU busy | implement first |
| Tracker / selector | same process as detector | stateful, frame-sequential | keep inline |
| CSV / JSONL / video write | writer thread + bounded queue | removes disk stalls from hot path | implement early |
| Postprocess cleanup | `ProcessPoolExecutor` or subprocess | CPU-heavy, benefits from multi-core | implement after raw path |
| Follow-cam render | subprocess or process pool | CPU-heavy and video-write heavy | implement after raw path |
| Multiple queued runs | subprocess per run + scheduler | isolation and resource control | medium-term |

## Execution Order

### Phase 1: quickest wins

- [ ] add detector benchmark harness
- [ ] implement batched slice inference
- [ ] benchmark `batch_size = 4, 6, 8`
- [ ] choose default batch size for `RTX 5080`
- [ ] add no-reload benchmark profile

Expected result:

- raw throughput roughly doubles, or close to it

### Phase 2: steady-state acceleration

- [ ] add ROI-first detector scheduling
- [ ] add periodic global refresh
- [ ] add confidence-based fallback to global search
- [ ] compare tracking quality against current baseline

Expected result:

- large gains on stable segments

### Phase 3: pipeline overlap

- [ ] add decode thread and frame prefetch queue
- [ ] add writer thread and output queue
- [ ] measure queue stalls
- [ ] tune queue sizes

Expected result:

- better GPU occupancy and smoother pipeline

### Phase 4: hardware decode and process model

- [ ] prototype `PyNvVideoCodec`
- [ ] move runs from threads to subprocesses
- [ ] process-ize cleanup and follow-cam

Expected result:

- better use of dedicated hardware and multi-core CPU

### Phase 5: advanced GPU path

- [ ] export and benchmark TensorRT engine
- [ ] validate engine cache strategy
- [ ] choose whether to keep PyTorch and TensorRT as dual backends

## Risk Notes

### Accuracy risk

ROI-first scheduling can hurt reacquisition if the fallback policy is too conservative.
Mitigation:

- keep periodic global refresh
- trigger global search on uncertainty
- compare detected / predicted / lost ratios to baseline

### Complexity risk

A threaded or multiprocess pipeline increases failure modes.
Mitigation:

- stage rollout
- add bounded queues
- add explicit profiling and queue health logs

### Windows integration risk

TensorRT and hardware decode often have platform-specific rough edges.
Mitigation:

- keep the current PyTorch path as a supported fallback

## Benchmarks to Capture Before and After Each Phase

- raw fps
- total wall-clock time for one baseline run
- detector-only latency on fixed frames
- GPU utilization
- GPU memory usage
- CPU total usage
- queue occupancy if threaded pipeline is enabled
- output quality:
  - detected ratio
  - predicted ratio
  - lost ratio
  - obvious visual regressions

## Success Criteria

This plan is successful when all of the following are true:

- raw tracking throughput is at least `5 fps` on the current source clip
- stable sections exceed `8 fps` after scheduling improvements
- GPU utilization is materially higher than current baseline
- the pipeline remains stable on Windows
- tracking quality does not regress in a meaningful way
- the code path remains understandable enough for local experimentation

## References

- PyTorch Performance Tuning Guide: https://docs.pytorch.org/tutorials/recipes/recipes/tuning_guide.html
- Python `concurrent.futures`: https://docs.python.org/3/library/concurrent.futures.html
- NVIDIA Video Codec SDK: https://developer.nvidia.com/video-codec-sdk
- NVIDIA PyNvVideoCodec: https://developer.nvidia.com/pynvvideocodec
- Ultralytics TensorRT export docs: https://docs.ultralytics.com/integrations/tensorrt/
- Torch-TensorRT compile settings and engine caching: https://docs.pytorch.org/TensorRT/dynamo/torch_compile.html
