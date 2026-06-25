# Real-Video AI Improvement Validation

This record captures the PR8 real-video validation run. Use the same shape for future runs, but do not reuse this output directory as acceptance evidence for later algorithm changes.

## Scope

AI audit means evidence review: it explains likely problems from deterministic artifacts, review packets, and visual media. AI audit can write reports, but it cannot mutate `ball_track.csv`, `ball_track.cleaned.csv`, follow-cam videos, highlight clips, or final manifests.

AI improvement means approved candidate work: it creates isolated candidate outputs for a bounded problem, compares them with the baseline, runs quality gates, and selects final artifacts only through `final_ai_improvement_artifact_manifest.json`.

The four supported improvement classes are:

- `missing_ball`: recover a visible match ball in a bounded lost window, or record evidence-backed `not_visible`.
- `noise`: classify dense false positives and produce a bounded cleanup/rerun/config candidate.
- `follow_cam`: decide whether motion problems come from tracking or camera tuning, then compare candidate camera motion.
- `highlight`: adjust clip boundaries while preserving core action and required result tail.

## PR8 Run Summary

| Field | Value |
| --- | --- |
| Validation time | `2026-06-24T03:27:44Z` to `2026-06-24T03:28:38Z` |
| Operator | Codex managed PR controller |
| Branch / base commit | `chore/ai-improvement-real-video-docs` from `02ab29c` |
| Input video | `python_backend\data\raw5760x144020fps.mp4` |
| Output directory | `python_backend\outputs\full_workflow_latest_review_20260622_060600` |
| Workflow report | `stable_ai_improvement_workflow_report.pr8_20260623_232742.json` |
| Parallel mode | `temporal` |
| Workflow mode | `artifact-only` |
| Candidate intent | `suggest_candidates` |
| Model/provider | `gpt-5.4` recorded; provider-backed calls were not available |
| Provider status | artifact-only / provider dry-run |
| Approval source | none |
| Consumed approval ids | none |

Provider-dependent hard cases, including long missing-ball gaps, camera failures, follow-cam regressions, and highlight-tail decisions, should use the configured stronger model when provider access exists. This run had no usable provider key in the current environment, so provider-dependent decisions remain artifact-only and cannot be documented as clean `pass`.

The input video, output directory, media sheets, and generated workflow artifacts named in this record are local validation artifacts. They are ignored by Git and are not preserved in the repository history.

## Required Evidence For Future Runs

Every real-video validation update must record evidence, not just artifact presence:

- Exact command, source video, output directory, branch or build identifier, workflow mode, model/provider, approval source, consumed approval ids, and exit code.
- `stable_ai_improvement_workflow_report.json` `stage_timing` rows for each executed, skipped, or failed stage, including final manifest and quality-gate refresh stages.
- `stable_ai_improvement_workflow_report.json` `acceptance_summary`: status, `deliverable`, quality-gate status, selected final track/video/highlight counts and paths, blockers, `ai_lanes` finalization/approval needs, and recommended operator actions.
- `final_ai_improvement_artifact_manifest.json` summary, final selected artifacts, rejected candidates, comparison reports, and final tracking/follow-cam/highlight paths.
- Final tracking, follow-cam, and highlight playback checks. A file path is insufficient; record whether each final output opens, has sampled frames, and is the artifact selected by the final manifest.
- Highlight validation from `highlight_candidate_comparison.json`, `highlight_report.json`, or `candidate_manifest.json`: `core_window`, `render_window`, `core_window_preserved`, `required_tail_frames`, `actual_tail_frames`, `tail_status`, and `source_end_clamp`. Passing highlight evidence must show the core action and required post-event tail are preserved, or that the source video end limited the available tail.
- Baseline/candidate visual evidence from review packets, overlay sheets, crop/contact sheets, media sheets, or decoded video samples. Do not mark missing-ball, dense-noise, follow-cam, or highlight checks as pass by checking only that JSON or media files exist.

Interpret `acceptance_summary` conservatively. `pass` means the final selected artifacts are supported by the quality gate and `stable_final_outputs`; `warn`, `fail`, and `unavailable` require recording the listed blockers and following the recommended action before treating the run as deliverable.

## Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --parallel-mode temporal `
  --mode artifact-only `
  --model gpt-5.4 `
  --candidate-intent suggest_candidates `
  --report-name stable_ai_improvement_workflow_report.pr8_20260623_232742.json
```

The command exited with code `1` because the quality gate status was `fail`. The workflow itself completed and wrote the report, quality gate, and final manifest.

Approval semantics are strict: `ai_improvement_report.json` and `ai_improvement_approved_actions.json` do nothing by file presence. The workflow must consume explicit approval ids from an explicit approval source. Unknown, duplicate, malformed, or missing ids must be recorded as warnings in dry runs or failures in real runs.

## Timing

The workflow report now records `stage_timing` with per-stage timestamps and elapsed seconds.

| Step | Started UTC | Finished UTC | Elapsed seconds | Status | Notes |
| --- | --- | --- | ---: | --- | --- |
| Metrics/artifact refresh | `2026-06-24T03:27:44.073768Z` | `2026-06-24T03:27:44.119767Z` | `0.046192` | succeeded | Rebuilt `metrics_report.json`. |
| Before-review hash snapshot | `2026-06-24T03:27:44.119767Z` | `2026-06-24T03:27:44.120767Z` | `0.000909` | succeeded | Baseline tracks captured before review. |
| Review packets | `2026-06-24T03:27:44.120767Z` | `2026-06-24T03:28:38.907954Z` | `54.787574` | succeeded | Dominant cost; regenerated media-backed packets. |
| Visual review | `2026-06-24T03:28:38.907954Z` | `2026-06-24T03:28:38.907954Z` | `0.000003` | skipped | Provider-backed visual review runs only in real mode. |
| AI improvement report | `2026-06-24T03:28:38.907954Z` | `2026-06-24T03:28:38.931011Z` | `0.022880` | succeeded | Provider dry-run; no executable candidates. |
| After-improvement hash snapshot | `2026-06-24T03:28:38.931011Z` | `2026-06-24T03:28:38.931953Z` | `0.001308` | succeeded | Track hashes remained unchanged. |
| Selected approval dispatcher | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.931953Z` | `0.000010` | completed | No approval ids selected. |
| Approved child rerun | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.931953Z` | `0.000003` | skipped | No approved recovery candidate. |
| Follow-cam rerender plan | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.931953Z` | `0.000006` | skipped | No approved follow-cam action. |
| Highlight render | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.931953Z` | `0.000003` | skipped | No approved highlight action. |
| Missing-ball noop resolution | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.931953Z` | `0.000045` | skipped | No approved `not_visible` resolution. |
| Pre-manifest quality gate | `2026-06-24T03:28:38.931953Z` | `2026-06-24T03:28:38.946953Z` | `0.014436` | fail | Preliminary quality gate used by manifest creation. |
| Final artifact manifest | `2026-06-24T03:28:38.946953Z` | `2026-06-24T03:28:38.946953Z` | `0.000605` | succeeded | Empty final selection because no candidates were approved. |
| Final quality gate refresh | `2026-06-24T03:28:38.946953Z` | `2026-06-24T03:28:38.960461Z` | `0.013524` | fail | Final gate includes post-manifest refresh and manifest status sync. |
| Media playback check | local follow-up | local follow-up | `1.758` | pass | 27 mp4 files opened and sampled. |

Total workflow elapsed time was `54.888045` seconds.

## Speed And Recovery Policy

The report confirms `parallel_mode: temporal` and `full_video_speed_strategy: temporal_chunks` with chunk settings `1200` frames, `80` overlap frames, and `120` decode-preroll frames.

The report also records `full_video_sahi: do_not_run_full_video_sahi` and `targeted_sahi_roi: explicit_bounded_approved_windows_only`. This run did not execute broad full-video SAHI, and it did not execute bounded SAHI/ROI because no approval ids were consumed.

## Observed Artifacts

| Artifact | Observed path/status |
| --- | --- |
| `stable_ai_improvement_workflow_report.json` | PR8-specific report: `stable_ai_improvement_workflow_report.pr8_20260623_232742.json`. The older default report also remains from the prior run. |
| `ai_improvement_report.json` | Present. Summary: `needs_rerun`; primary issue `tracking`; one targeted rerun suggestion; provider mode `dry-run`; executable candidate count `0`. |
| `ai_improvement_approved_actions.json` | Not used in this validation. |
| `ai_candidate_registry.json` | Not present because no candidate was executed. |
| `missing_ball_recovery_comparison.json` | Not present because no missing-ball candidate was executed. |
| `noise_improvement_comparison.json` | Not present because no noise candidate was executed. |
| `follow_cam_comparison.json` | Not present because no follow-cam candidate was executed. |
| `highlight_comparison.json` | Not present because no highlight candidate was executed. |
| `ai_improvement_quality_gate.json` | Present. Summary status `fail`; `check_count: 9`; `failed_check_count: 1`; `warning_count: 2`. |
| `final_ai_improvement_artifact_manifest.json` | Present. Empty selection: candidate output count `0`, final artifact count `0`. |
| Ball tracks | `ball_track.csv` and `ball_track.cleaned.csv` present; hashes unchanged across review/improvement-only stages. |
| Final follow-cam media | `follow_cam.latest_review.mp4` present and decodable. |
| Highlight clips | No promoted highlight clip. Review packet clips are decodable but are not final highlights. |

Candidate output semantics are conservative: a candidate must have consumed approval linkage, comparison, quality gate, and final-manifest selection. A file, directory, video, or comparison report existing on disk is not enough to make it promoted.

## Artifact Metrics

| Metric source | Observed |
| --- | --- |
| `ball_audit.json` | `5192` frames, `130` tracklets, `122` suspicious tracklets, `348` review events, `110` lost gaps, max step `4114.63` px. |
| `review_packets.json` | `11` packets, `11` with media; labels: `2` highlight-worthy, `1` manual-review, `8` needs-AI-review. |
| `camera_motion_audit.json` | Status `warn`; max pan step `120.4989` px; p95 pan step `53.7869` px; max acceleration `120.4989` px; `15` review events. |
| `event_candidates.json` | `33` event candidates: `2` goal candidates, `31` shot candidates. |
| `player_tracks.json` | Present but empty: `0` detections and `0` tracks. |
| `pr8_media_decode_check.json` | `27` mp4 files opened and sampled successfully. |

## Visual Checklist

| Check | Evidence inspected | Result | Notes |
| --- | --- | --- | --- |
| Frame `2079` right-bottom lost-ball evidence | `manual_checks\frames_2020_2160_contact_seq_final.jpg` and `manual_checks\frames_2020_2160_crop_seq_final.jpg` | fail | The scene is near the right-bottom corner/goal area, but several crop cells are missing and visible markers at frames `2020`, `2079`, and `2160` are not confidently on the match ball. This is not a solved recovery. |
| Recovered missing-ball candidate | No candidate directory or comparison report | unavailable | No approved `localize_ball_roi` or targeted rerun was consumed, so there is no candidate to promote. |
| Dense-noise window | `review_packets\packet_002_dense_noise_cluster_245_308\crop_sheet.jpg` plus quality gate | fail | The crop sheet shows markers on grass/body/ambiguous regions rather than a clear ball. Quality gate failed `noise_failure_tags_present` with `45` noise windows missing classification coverage. |
| Camera spike or follow-cam motion event | `camera_motion_audit.json` and `manual_checks\pr8_follow_cam_sample_sheet.jpg` | warn | Media is decodable and often keeps play in frame, but camera audit has `15` review events and no approved follow-cam candidate/comparison exists. |
| Highlight tail | Goal/shot packets, `event_candidates.json`, and any final `highlight_candidate_comparison.json` / `highlight_report.json` / `candidate_manifest.json` | unavailable | Event candidates exist, but no approved final highlight render or comparison exists. Future pass evidence must list `core_window`, `render_window`, `core_window_preserved`, `required_tail_frames`, `actual_tail_frames`, `tail_status`, and `source_end_clamp`, then confirm the decoded clip covers the core action plus post-event tail. |
| Final tracking playback | Final manifest plus selected tracking media/overlays | unavailable | Baseline tracks exist and hashes were stable, but no final selected tracking media was produced by the AI improvement loop. Future pass evidence must verify the final-manifest-selected output opens and sampled overlays match the claimed recovery/cleanup. |
| Final follow-cam playback | Final manifest, `follow_cam.latest_review.mp4`, media decode check, and follow-cam sample sheet | warn | Video opens and samples correctly, but sampled overlays include questionable marker placement and camera audit is only `warn`. Future pass evidence must verify the selected final follow-cam media, not only a baseline render. |
| Final highlight playback | Final manifest plus promoted highlight clip and comparison/report fields | unavailable | Packet clips open, but no promoted highlight clip exists. Future pass evidence must decode the final-manifest-selected highlight and compare sampled frames against the recorded core/tail fields. |

## Quality Gate Result

The quality gate failed for one reason:

```json
{
  "noise_failure_tags_present": {
    "status": "fail",
    "noise_window_count": 45,
    "missing_window_count": 45
  }
}
```

Two checks were warning/unavailable by design in this artifact-only run:

- No explicit approved actions path was supplied, so no approvals were consumed.
- No candidate output directory was supplied for camera regression comparison.

The manifest is therefore empty rather than falsely selecting an output.

## Player Artifact Note

This run does not prove stable continuous player tracking. `player_tracks.json` exists, but its summary has `detection_count: 0`, `track_count: 0`, and `active_track_count: 0`.

## Final Decision

| Decision item | Status | Evidence |
| --- | --- | --- |
| Missing-ball loop | fail | The frame `2079` right-bottom window remains visually unresolved, and no approved recovery candidate was executed. |
| Noise loop | fail | Quality gate failed noise classification coverage; inspected dense-noise crops are not reliable ball detections. |
| Follow-cam loop | warn | Follow-cam media is playable, but camera audit is `warn` and no follow-cam candidate comparison exists. |
| Highlight loop | unavailable | Event candidates and packet clips exist, but no promoted highlight render/comparison exists. |
| Approval consumption recorded | unavailable | No approval source or ids were supplied. |
| Candidate comparison complete | not_applicable | The workflow had no candidates to compare; this is not evidence of improvement. |
| Final manifest selects only eligible artifacts | pass | Manifest selected nothing because no candidate was eligible. |
| Final media playable | warn | Follow-cam and packet clips are decodable; no final AI-selected tracking/highlight media exists. |

Residual risks:

- Provider-backed model improvement was not exercised in this environment.
- The right-bottom frame `2079` failure remains a real visual gap.
- Dense noise still needs false-positive classification and bounded cleanup candidates.
- Highlight clips need approved render/comparison before they can be called final highlights.

Next action:

- Run provider-backed AI improvement with a configured strong model, approve only bounded recovery/noise/camera/highlight actions by explicit id, execute candidates, and rerun this validation. The next successful run should produce candidate registry entries, domain comparison reports, and a non-empty final manifest only for candidates that actually improve the baseline.

## Future Targeted Localization Check

For PR1 and later validation runs that target a known missing-ball gap, add a bounded localization request to the command:

```powershell
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\my_run `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --targeted-localization-window 2049:2544:right_corner `
  --parallel-mode temporal `
  --mode real `
  --model gpt-5.4
```

Record a `targeted_visual_localization` timing row, `ai_visual_localization.json`, generated contact/crop sheets, decoded OpenCV dimensions, accepted/rejected ROI counts, and uncovered subwindows such as `2049-2078` or `2301-2544` when only part of the requested window is localized. This artifact is evidence only and should not be recorded as a promoted final track.
