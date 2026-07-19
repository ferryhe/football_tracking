# Football Tracking P3 Operations and Acceptance Guide

This is the operator guide for the repository. Run every command from the **repository root**; do not enter `python_backend/` or set `PYTHONPATH` manually.

## Official commands

```bash
pnpm check
pnpm start
pnpm status
pnpm stop
pnpm test
pnpm train -- --help
pnpm validate:full-video -- --run-dir python_backend/outputs/runs/<video>/<run-id> --resume
```

The entrypoint selects the root virtual environment, prepends the absolute backend path, fixes the working directory, and forwards arguments and exit status. Compatibility CMD files and low-level Python scripts are not alternate operator entrypoints.

Before starting, make sure the root `.venv`, pnpm dependencies, `python_backend/data/`, `python_backend/config/`, and config-referenced detector weights are present. `pnpm check` must pass.

`pnpm start` gives the backend up to 180 seconds to finish bounded durable-state recovery. A cold start after detector or review-proxy work can remain at `Waiting for application startup` for more than a minute; keep it running unless the launcher reaches the bounded health-check failure and points to `backend.log`.

## P3 broadcast workflow

Use `/broadcast` for full-match P3 delivery. `/baseline`, `/ai`, and `/deliverable` remain useful for legacy baselines, tuning, follow-cam, and highlight work, but they do not replace the evidence-bound broadcast flow.

1. **Setup** — choose a source and config; confirm three distinct real frames of one resolution; validate a non-zero field polygon and legal exclusions; choose a review limit from 1–30. P3 requires the complete video (`start_frame=0` and no partial `max_frames`). Save `/broadcast?run=<run-id>`.
2. **Review and recompute** — decide every candidate exactly once as `confirm_ball`, `reject_noise` with a subtype, or `mark_unknown`. A non-empty queue requires a reviewer. A zero-candidate queue still requires explicit confirmation. Refresh stale evidence instead of reusing hashes. If decisions committed but queuing failed, use retry recompute.
3. **Render and deliver** — render only from `trajectory_ready`; 1920×1080 is the default. `ready` requires verified generation-scoped URLs and no blockers.

The public delivery set is exactly:

- `broadcast.mp4`
- `broadcast_quality_report.json`
- `camera_target.csv`
- `ball_track.v2.csv`
- `review_decisions.json`
- `action_track.csv`
- `candidate_classifications.jsonl`
- `ball_candidates.jsonl`

## Full-video acceptance

`ready` proves immutable lineage, not media or visual quality. Run:

```bash
pnpm validate:full-video -- --run-dir python_backend/outputs/runs/<video>/<run-id> --resume
```

The gate revalidates lineage, probes source/output video and audio durations, decodes the output in complete deterministic segments, checks first/middle/last and segment-center frames, and atomically writes `broadcast_acceptance_report.v1.json`. Even when segments are reused, a strict independent FFmpeg full decode and exact frame-count check run before `pass` is published. The hash-bound `broadcast_acceptance_progress.v1.json` is only a scheduling cache for acceptance segments; it cannot bypass that final decode and is not a claim that initial tracking or rendering supports frame-level checkpoints.

The strict FFmpeg pass deliberately does not trust the writable checkpoint. Every `--resume` therefore decodes the final 1080p output once from the beginning, at roughly one full-decode cost. The CLI writes strict-stage start/completion JSON to stderr. Interrupting that stage publishes no new `pass`; the next attempt reruns the terminal gate.

`pass` means the machine gate passed but still requires visual sign-off; `fail` is a verified defect, and `unavailable` means the gate could not establish integrity and is also blocking. The current quality contract declares `source_audio_not_preserved`; a source with audio and a silent output is recorded as a known limitation, while any mismatch between declared capability and actual streams fails closed.

After the machine gate passes, visually inspect evidence montages, ball-path jumps and false positives, camera swings, and the beginning/middle/end of the video. Delivery requires both checks.

## Recovery and cancellation

- Refresh or reopen the saved broadcast URL to recover server-owned workflow state.
- Recompute/render generations reconcile through immutable operation reports after a service restart.
- Initial full-match tracking does **not** have frame-level restart recovery; an interrupted run fails and must be recreated.
- Cancel the active parent or child from the page. Never edit the registry, hashes, decisions, or a ready generation by hand.
- HTTP 409 means evidence changed; refresh and restart review from current evidence.

Use `pnpm stop` for managed processes. Stop is idempotent and only terminates a recorded root process whose PID and creation identity still match; `status` additionally verifies that the current port listener remains inside that root process tree. For detailed Chinese troubleshooting and the visual checklist, see [operation-guide.zh.md](operation-guide.zh.md).
