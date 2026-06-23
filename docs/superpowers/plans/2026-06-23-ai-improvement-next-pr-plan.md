# AI Improvement Next PR Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn AI from a passive reviewer into an improvement loop for four visible problems: missing ball recovery, noisy false positives, shaky follow-cam motion, and highlight clip timing.

**Architecture:** Deterministic tracking artifacts remain the source of truth. AI can propose bounded candidates only when the source evidence is traceable. A selected candidate affects final output only after explicit approval ids, isolated execution, objective comparison, registry entry, quality-gate validation, and final artifact manifest recording.

**Tech Stack:** Python backend, FastAPI service layer, OpenAI-compatible provider wrapper, JSON artifact contracts, pytest, existing review packets, visual review, quality gate, candidate registry, follow-cam renderer, highlight renderer, and managed PR workflow.

---

## Requirement Summary

The product requirement is **AI improvement**, not merely **AI audit**.

The user-visible AI improvement needs are:

- **Missing ball:** when a long lost span occurs, AI should inspect packet media and either find a plausible ball ROI for bounded recovery or record evidence-backed `not_visible`.
- **Too much noise:** when detector output includes extra ball-like objects, AI should identify the false-positive type and propose bounded cleanup that does not damage real-ball coverage.
- **Shaky follow-cam:** when the final `follow_cam.mp4` has abrupt pan, acceleration, or zoom, AI should decide whether the correct fix is tracking recovery or follow-cam rerender tuning.
- **Highlight timing:** default clips should keep pre/post buffers; AI may adjust boundaries, but must preserve the core shot/goal and enough tail frames after the event.
- **Speed strategy:** broad full-video spatial splitting can improve throughput and small-ball recall but tends to add false positives. Prefer temporal chunk parallelism for full-video speed, and reserve spatial split/SAHI/ROI for explicit bounded recovery windows.
- **Model strategy:** use a configured strong model for run-level improvement and hard visual localization. In real/improve mode, missing strong-model configuration must fail closed or record `unavailable`; weak chat-model fallback is allowed only for dry-run, smoke, or low-risk advisory tagging.

## Current Landing Matrix

| Area | Current state | What is landed | Remaining gap |
| --- | --- | --- | --- |
| Ball audit and AI review triggers | Landed | `ball_audit.json`, `ai_review_triggers.json` | Keep these advisory; do not confuse trigger coverage with improvement success. |
| Review packets | Landed | `review_packets.json` with media/evidence windows | Ensure packet labels cover full long spans, not just one interesting frame. |
| Visual review | Partly landed | `ai_visual_review.json`, visual packet review script | Wire strong model routing into stable workflow and pass visual ROI evidence into improvement candidates. |
| AI improvement report | Partly landed | `ai_improvement_report.json`, approval contracts, prompt hardening | Separate executable candidates from notes in every downstream path. |
| Missing-ball execution | Landed in PR #46 | selected approvals create isolated `ai_candidates/missing_ball/<candidate_id>/` outputs and comparison reports | Add dispatcher integration and evidence-backed `not_visible` no-op resolution. |
| Candidate registry | Landed, needs extension | safe candidate ids, registry validation, comparison binding | Extend statuses for unsupported, resolved no-op, noise, follow-cam, highlight. |
| Camera motion audit | Landed | `camera_motion_audit.json` from follow-cam camera path | Convert motion events into executable follow-cam rerender candidates and comparisons. |
| Event/highlight render | Partly landed | `event_candidates.json`, `highlight.mp4`, `highlight_report.json`, buffer/tail checks in service | Add AI-adjusted highlight candidates with comparison and publish gate. |
| Quality gate | Landed, needs expansion | long-gap coverage, selected comparison validation, final manifest checks | Accept no-op resolution only with evidence; add candidate classes for noise/camera/highlight. |
| Stable workflow | Partly landed | `run_stable_ai_improvement_workflow.py`, explicit approval handling, quality gate stage | Add dispatcher, visual-review ordering, no-op resolution, and candidate class routing. |
| API/UI visibility | Partly landed | AI analysis, deliverable render, artifact reads | Show proposed/approved/executed/pass/warn/fail/resolved/promoted state clearly. |
| Docs and skill capture | Partly landed | README and workflow guide mention current artifacts | Add final operator checklist and skill update after implementation proves stable real-video output. |

## Cross-Cutting Rules

- AI suggestions are not improvements until they produce candidate artifacts and comparison reports.
- AI may propose bounded actions only. The dispatcher validates problem type, action, evidence, and frame window; quality gates decide pass/warn/fail; human approval controls execution and promotion.
- Approval-file presence alone never executes work.
- Use canonical `approval_ids` in API and `--approval-ids` in CLI for mutation. Legacy single-id flags may remain only as compatibility aliases.
- Review-only stages must not mutate `ball_track.csv` or `ball_track.cleaned.csv`; PRs that add review/improvement stages must record pre/post SHA-256 hash snapshots in tests.
- Every executable candidate must have `candidate_id`, `problem_type`, bounded frame window, evidence ids, approval id, candidate directory, comparison report, and registry entry.
- `not_visible` is a resolved no-op candidate only when packet or visual evidence shows the ball is hidden, off-frame, or not identifiable.
- Follow-cam smoothing cannot hide tracking failure. If a camera jump overlaps Lost/Predicted ball-track context, the AI action should route to tracking recovery first.
- Highlight windows must preserve `core_window` and configured post-event tail unless the source video ends.
- Candidate execution must write under `ai_candidates/<problem_type>/<candidate_id>/` and must not overwrite baseline artifacts.
- Final promotion accepts `pass`. A `warn` candidate requires explicit human confirmation. `fail`, `unsupported`, and `unavailable` must not be promoted.
- Promotion writes a manifest and explicit final/promoted artifact references; rollback must be possible by ignoring the manifest and returning to the baseline output directory.

## Managed PR Rules

Each PR in this plan follows the managed PR loop:

- [ ] Start from a clean latest `origin/main`; record current branch and unrelated local changes.
- [ ] Create a fresh feature branch for that PR.
- [ ] Assign implementation to a scoped worker when agent capacity allows; worker owns only the files listed for the PR and must not revert unrelated changes.
- [ ] Use TDD for behavior changes: failing focused test first, then implementation, then focused test pass.
- [ ] Run the PR's focused tests, related regression tests, `git diff --check`, and any documented real-video or fixture smoke.
- [ ] Request independent spec review and code-quality review before publishing.
- [ ] Fix all Critical and Important review findings before opening the PR.
- [ ] Push, open a GitHub PR, wait for checks and Copilot comments, and evaluate comments on merit.
- [ ] Merge only after required checks pass and valid remote feedback is resolved.
- [ ] Delete merged remote and local branches when authorized.
- [ ] Update this plan or operation docs when behavior or commands change.

## Per-PR Improvement Proof

Every feature PR must prove improvement before merge. PR7 collects the final evidence pack, but it is not the first place improvement is validated.

- **PR2 missing-ball/no-op:** selected missing-ball execution must reuse PR #46 candidate creation; evidence-backed `not_visible` must close only the exact covered window. A fixture for `2049-2544` with key frame `2079` must still fail if neither recovery nor no-op evidence covers the whole required span.
- **PR3 noise:** pass requires false-positive island count to decrease by at least 20 percent or at least 2 islands, while lost-frame count increases by no more than 1 percent or 15 frames and sustained detected coverage decreases by no more than 2 percent. A smaller decrease with no coverage loss is `warn`; any false-positive increase above 10 percent or coverage loss beyond tolerance is `fail`.
- **PR4 follow-cam:** pass requires `camera_motion_audit.review_event_count` to decrease or stay zero, p95 pan step and max acceleration not worsen by more than 5 percent, max zoom jump not worsen, and crop/ball coverage proxy stay within 2 percent of baseline. Excess zoom-out ratio above 15 percent of candidate frames is `fail` unless explicitly human-confirmed as tactical view.
- **PR5 highlights:** pass requires candidate window to contain `core_window`, preserve required post-event tail, clamp only at source-video end, and render a readable `highlight.mp4` with a matching `highlight_report.json`.
- **PR6 API/UI:** pass requires no UI/API state to label review-only suggestions as applied improvements and every mutation path to require explicit approval ids.
- **PR7 docs/evidence:** pass requires the docs to reference commands and artifacts that were already validated in earlier PRs, plus a consolidated real-video evidence record.

## Metric Contracts

These formulas are part of the implementation contract. If a PR needs to refine a metric, it must update this table and its fixture expectations in the same PR.

| Metric | Inputs | Formula | Baseline/Candidate Rule | Minimal fixture |
| --- | --- | --- | --- | --- |
| False-positive island count | `ball_track.cleaned.csv`, `ball_audit.json`, approved window, optional visual/packet labels | Count contiguous non-empty ball detections inside the approved window that are shorter than the local minimum duration or are marked by audit/AI evidence as false-positive islands. Adjacent islands separated by at most 1 frame merge before counting. | Candidate must reduce this count by the PR3 thresholds while preserving coverage tolerances. | Window with 5 short isolated detections and 1 sustained true-ball segment; cleanup should remove the short islands and keep the sustained segment. |
| Lost-frame count | `ball_track.cleaned.csv`, `ball_audit.json`, approved window | Count frames in the approved window whose status is Lost, Missing, empty, or equivalent audit missing state. | Candidate lost-frame count may increase by no more than 1 percent of window length or 15 frames, whichever is larger. | Window where removing false positives would create a large hole unless the sustained true-ball segment is preserved. |
| Sustained detected coverage | `ball_track.cleaned.csv`, approved window | Count frames that belong to detected runs at least `min_sustained_run_frames` long, divided by approved-window frame count. Default fixture value is 5 frames unless local code already defines a stricter threshold. | Candidate coverage may decrease by no more than 2 percent absolute. | One long true-ball run plus short noise bursts; candidate should keep the long run. |
| Camera pan step and acceleration | `camera_path.csv`, `camera_motion_audit.json` | Use output-space apparent displacement from `camera_motion_audit.py`: source delta scaled by target/crop size and divided by frame delta; acceleration is output-space velocity delta. | Candidate p95 pan step and max acceleration must follow PR4 pass/warn/fail thresholds. | Smooth baseline candidate and one jumpy path with a single abrupt center change. |
| Ball/crop coverage proxy | `ball_track.cleaned.csv`, baseline and candidate `camera_path.csv`, target dimensions | For frames with a valid ball center, map the ball into each candidate crop; count frames where the ball is inside crop bounds with a margin of at least 5 percent of target width/height. Coverage is count divided by valid-ball frames. | Candidate coverage may drop by no more than 2 percent absolute. | Path that smooths too aggressively and lets the ball leave the crop must fail. |
| Excessive zoom-out ratio | baseline and candidate `camera_path.csv` | Count candidate frames where crop height is more than 15 percent larger than baseline crop height for the same frame, divided by compared frame count. | Ratio above 15 percent is `fail` unless the candidate is explicitly approved as tactical view. | Candidate that removes motion spikes only by zooming far out must fail. |
| Highlight core coverage | `event_candidates.json`, `highlight_report.json`, source video frame count | Candidate render window must contain every frame from `core_window.start_frame` through `core_window.end_frame`. | Any missing core frame is `fail`. | Candidate starts after the shot frame and must fail. |
| Highlight tail coverage | `event_candidates.json`, `highlight_report.json`, source video frame count | Candidate must include the configured post-event tail after `core_window.end_frame`, clamped only by the actual source-video end. | Missing tail is `fail` when source frames exist; source-end clamp is `warn` or `pass` with evidence. | Goal near end of video preserves every available tail frame and records source-end clamp. |
| `2049-2544` long-gap coverage | `ai_review_triggers.json`, `review_packets.json`, `ai_visual_review.json`, selected approvals, recovery comparison or `missing_ball_resolution.json` | The full required range must be covered by approved recovery comparison evidence or by evidence-backed `resolved_not_visible` windows. Short subwindows do not close the gap. | Full recovery coverage passes; full no-op coverage passes; key-frame-only coverage around `2079` fails. | Three fixtures: full no-op coverage pass, full recovery coverage pass, short `2079` subwindow fail. |

## PR2: Workflow Dispatcher, Strong Visual Model, And Missing-Ball No-Op

**Purpose:** Make the stable workflow execute selected approvals through a deterministic dispatcher, use strong model routing for visual localization, and close true not-visible missing-ball windows without pretending a track was recovered. This is a foundation PR; if implementation grows beyond the listed files, split it into model/workflow ordering first and dispatcher/no-op/manifest second.

**Files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/ai_provider.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_config_and_provider.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`

**Build:**

- [ ] Add `PROVIDER_OPENAI_VISUAL_REVIEW_MODEL` to provider settings.
- [ ] Select visual model in this order: request model, visual-review model, improvement model.
- [ ] Allow chat-model fallback only when mode is dry-run/smoke/advisory; in real/improve mode, missing visual/improvement model records `unavailable` and must not be reported as successful localization.
- [ ] Record selected model, model selection source, provider mode, and candidate intent in `ai_visual_review.json`.
- [ ] Ensure real/improve workflow stages run review packets before visual review, and visual review before run-level AI improvement.
- [ ] Feed `ai_visual_review.json` into `ai_improvement.py` so localized ROI evidence can become executable missing-ball approvals.
- [ ] Add a small dispatcher keyed by `(problem_type, approved_action)`.
- [ ] Reuse the PR #46 missing-ball child execution path; do not reimplement candidate mutation or comparison logic in the dispatcher.
- [ ] Record unsupported noise, follow-cam, and highlight approvals as `unsupported_candidate_type` without executing them.
- [ ] Add `missing_ball_resolution.json` for evidence-backed `not_visible`.
- [ ] Register evidence-backed `not_visible` as `resolved_not_visible` no-op candidate.
- [ ] Update quality gate so `resolved_not_visible` only passes when packet or visual evidence is traceable to the covered frame window.
- [ ] Update final artifact manifest to show accepted, rejected, pending, unsupported, and resolved no-op candidates.

**Tests:**

- [ ] Dry-run records workflow stages and warnings without mutating track or media artifacts.
- [ ] Real/improve mode runs visual review before run-level AI improvement when provider is configured.
- [ ] Visual review uses the model routing order and records source of model selection.
- [ ] Missing or unavailable `ai_visual_review.json` is not treated as successful localization evidence.
- [ ] Unknown, duplicate, malformed, or unselected approval ids fail safely in non-dry-run mode.
- [ ] Only explicitly selected approval ids execute.
- [ ] Unsupported candidate classes are recorded as unsupported and never executed.
- [ ] Evidence-backed `not_visible` writes `missing_ball_resolution.json`, registry entry, quality-gate evidence, and final manifest entry.
- [ ] `not_visible` without packet or visual evidence fails the gate.
- [ ] Fixture `2049-2544` with key frame `2079` passes when recovery evidence covers the whole range.
- [ ] Fixture `2049-2544` with key frame `2079` passes when `resolved_not_visible` evidence covers the whole range.
- [ ] Fixture `2049-2544` with key frame `2079` fails when the selected approval covers only a short subwindow.
- [ ] Pre/post hash snapshots prove review, visual review, and run-level improvement stages did not mutate track CSVs.

**Deliver:**

- `stable_ai_improvement_workflow_report.json` records review, visual review, improvement, selected execution, no-op resolution, quality gate, and manifest stages.
- `missing_ball_resolution.json` exists for evidence-backed not-visible cases.
- Strong visual model routing is documented in artifacts.
- Workflow becomes the single safe entry point for selected missing-ball AI improvement.
- Before/after evidence shows a selected missing-ball approval either produced a comparison-backed candidate or a no-op resolution for the exact covered span.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_config_and_provider.py -q
git diff --check
```

## PR3: Noise AI Cleanup Candidate Loop

**Purpose:** Let AI classify and reduce false-positive detection noise without sacrificing sustained real-ball coverage.

**Improvement thresholds:**

- `pass`: false-positive island count decreases by at least 20 percent or at least 2 islands, lost-frame count increases by no more than 1 percent or 15 frames, and sustained detected coverage decreases by no more than 2 percent.
- `warn`: false-positive island count decreases by 5-20 percent with no coverage loss beyond tolerance, or labels are useful but cleanup improvement is marginal.
- `fail`: false-positive island count increases by more than 10 percent, lost-frame count increases beyond tolerance, sustained detected coverage drops beyond tolerance, or cleanup provenance is unbounded full-video spatial splitting.

**Files:**

- Create: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/football_tracking/ai_review_triggers.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/postprocess.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_ai_review_triggers.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Promote dense-noise, short-island, and candidate-ambiguity windows into review packets.
- [ ] Let AI classify bounded false positives as `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, `sideline_confusion`, `wall_background_drift`, `unknown_false_positive`, or `unknown`.
- [ ] Store the class as `false_positive_class`.
- [ ] Execute cleanup only on copied candidate artifacts under `ai_candidates/noise/<candidate_id>/`.
- [ ] Candidate artifacts must include `ball_track.cleaned.csv`, `cleanup_report.json`, `ball_audit.json`, `noise_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Allow only conservative bounded cleanup: remove short islands, downweight repeated false-positive zones, or tighten postprocess thresholds inside the approved window.
- [ ] Reject broad full-video spatial split/SAHI cleanup unless it is explicitly bounded and recall-preserving.
- [ ] Keep full-video spatial cleanup disabled by default in workflow and API; it can only run behind an explicit bounded-window approval and must record bounded ROI/SAHI provenance.
- [ ] Record inference strategy provenance: temporal chunk, bounded ROI/SAHI, or rejected broad spatial split.
- [ ] Extend dispatcher, registry, quality gate, and final manifest for noise candidates.

**Tests:**

- [ ] Dense-noise trigger creates packet coverage.
- [ ] AI classification without bounded evidence remains review-only.
- [ ] Cleanup removes short false-positive islands.
- [ ] Cleanup fails if sustained valid-ball coverage drops.
- [ ] Cleanup fails if lost-frame count increases beyond tolerance.
- [ ] Candidate-scoped `ball_audit.json` is regenerated from candidate tracks.
- [ ] Comparison records removed island ranges and before/after coverage.
- [ ] Broad unbounded SAHI provenance fails comparison.
- [ ] Full-video workflow records temporal chunk strategy for speed.
- [ ] Bounded approved ROI/SAHI cleanup records frame window and evidence ids.
- [ ] Dispatcher executes selected noise approvals end to end.
- [ ] Registry, gate, and manifest summarize pass/warn/fail/unavailable noise status.

**Deliver:**

- AI-assisted noise cleanup candidates.
- Objective comparison proving whether false positives decreased without harming coverage.
- Clear product rule: temporal parallelism for speed, bounded spatial/SAHI only for approved windows.
- Numeric comparison summary with before/after false-positive islands, lost frames, sustained coverage, and strategy provenance.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_noise_candidate_comparison.py python_backend/tests/test_ai_review_triggers.py python_backend/tests/test_review_packets.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

## PR4: Follow-Cam AI Rerender Candidate Loop

**Purpose:** Convert `camera_motion_audit.json` events into smoother follow-cam rerender candidates while preventing camera smoothing from masking tracking loss.

**Improvement thresholds:**

- `pass`: camera review event count decreases or remains zero, p95 pan step and max acceleration worsen by no more than 5 percent, max zoom jump does not worsen, ball/crop coverage proxy stays within 2 percent of baseline, and excessive zoom-out ratio is below 15 percent of candidate frames.
- `warn`: motion improves but one secondary metric worsens within 5-10 percent, or the candidate requires human confirmation because tactical view intentionally zooms out.
- `fail`: review event count increases, p95 pan step or acceleration worsens beyond 10 percent, ball/crop coverage drops beyond 2 percent, or zoom-out ratio exceeds 15 percent without explicit tactical-view confirmation.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam.py`
- Modify: `python_backend/football_tracking/camera_motion_audit.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_camera_motion_audit.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Include camera motion audit events and overlapping track status in AI improvement context.
- [ ] Ask AI to propose `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, or `tracking_recovery_then_follow_cam_rerender`; deterministic validation and human approval decide execution.
- [ ] Route to tracking recovery when a camera event overlaps Lost or Predicted status.
- [ ] Execute approved follow-cam-only rerenders under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Follow-cam-only candidates must not mutate ball-track artifacts.
- [ ] Candidate artifacts must include `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Execute approved `tracking_recovery_then_follow_cam_rerender` candidates as a linked two-step candidate: first reuse the selected missing-ball/tracking recovery candidate path, then rerender follow-cam from the candidate track.
- [ ] Linked tracking+follow-cam artifacts must include `linked_tracking_candidate_id`, candidate `ball_track.cleaned.csv`, tracking comparison, rerendered `follow_cam.mp4`, `camera_path.csv`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, quality-gate result, and final manifest entry.
- [ ] Compare baseline and candidate p95/max pan step, max acceleration, zoom jump, review event count, ball/crop coverage proxy, and excessive zoom-out ratio.
- [ ] Compare tracking recovery coverage before accepting a linked rerender; a smoother camera path fails if the underlying lost span is still unresolved.
- [ ] Fail candidates that reduce shake only by zooming out too far or losing ball-centered coverage.
- [ ] Extend dispatcher, registry, quality gate, and final manifest for follow-cam candidates.

**Tests:**

- [ ] Camera motion events appear in AI context.
- [ ] Invalid follow-cam action without a safe rerender plan is rejected.
- [ ] Approved rerender writes all candidate artifacts.
- [ ] Comparison passes when motion metrics improve and coverage is preserved.
- [ ] Comparison fails when motion metrics regress, coverage drops, or zoom-out exceeds threshold.
- [ ] Pre/post track hashes remain identical for follow-cam-only candidates.
- [ ] Lost/Predicted overlap routes to tracking recovery instead of direct camera smoothing.
- [ ] Lost/Predicted overlap can execute `tracking_recovery_then_follow_cam_rerender` end to end and writes both tracking and follow-cam comparisons.
- [ ] Linked rerender fails when tracking recovery does not cover the lost span, even if camera motion metrics improve.
- [ ] Dispatcher executes selected follow-cam approvals end to end.
- [ ] Registry, gate, and manifest summarize follow-cam status.

**Deliver:**

- Executable AI follow-cam rerender candidates.
- Before/after motion comparison for final output stability.
- Operator-visible evidence that a smoother candidate did not simply hide the play.
- Candidate `follow_cam.mp4` plus `follow_cam_candidate_comparison.json` proving motion stability changed within the allowed thresholds.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

## PR5: Highlight Window AI Candidate Loop

**Purpose:** Generate highlight clips with safe default buffers and allow AI boundary adjustments without cutting off the shot, goal, or aftermath.

**Improvement thresholds:**

- `pass`: candidate window contains the full `core_window`, includes `min_tail_frames` or `min_post_event_frames` after `core_window.end_frame`, respects source-video bounds, and renders a readable clip.
- `warn`: candidate preserves core event but tail is clamped by source-video end or requires human confirmation because the event candidate has incomplete metadata.
- `fail`: candidate cuts off any core frame, cuts off required tail when source frames exist, starts after the configured pre-buffer without explanation, or renders no readable frames.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/events.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Preserve existing `core_window`, `render_window`, and `buffer_policy` schema.
- [ ] Preserve `pre_buffer_frames`, `post_buffer_frames`, `min_tail_frames`, and `min_post_event_frames`.
- [ ] Clamp render windows to source video length while preserving core event and required tail when possible.
- [ ] Reject AI windows that cut off core event frames.
- [ ] Reject AI windows that cut off required tail unless the source-video end forces the clamp.
- [ ] Default render still uses configured pre/post buffer before AI adjustments.
- [ ] AI-adjusted windows must record the reason for moving start/end frames and the exact before/after frame numbers.
- [ ] Execute approved highlight candidates under `ai_candidates/highlights/<candidate_id>/`.
- [ ] Candidate artifacts must include `highlight.mp4`, `highlight_report.json`, `highlight_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Comparison must check source candidate id, bounds, core coverage, tail coverage, artifact existence, and render success.
- [ ] Accepted highlight publishing must require comparison evidence.
- [ ] Extend dispatcher, registry, quality gate, and final manifest for highlight candidates.

**Tests:**

- [ ] Default highlight window includes core event and post-event tail.
- [ ] Existing buffer policy remains backward compatible.
- [ ] AI window cutting off core frames is rejected.
- [ ] AI window cutting off tail is rejected unless source-end clamp applies.
- [ ] AI extension of post-event tail is accepted when it preserves bounds and renders readable frames.
- [ ] End-of-video clamp preserves every available post-event frame and records the clamp reason.
- [ ] Candidate render writes `highlight.mp4` and `highlight_report.json`.
- [ ] Comparison checks core coverage, tail coverage, bounds, and artifact existence.
- [ ] Accepted highlight copier accepts only publishable clips with comparison evidence.
- [ ] Dispatcher executes selected highlight approvals end to end.
- [ ] Registry, gate, and manifest summarize highlight status.

**Deliver:**

- AI-adjustable highlight candidates.
- Safer clips that include the event and enough result/tail frames.
- Publishable highlight evidence in the final manifest.
- Before/after highlight window report showing default buffer, AI-adjusted frames, core coverage, tail coverage, and source-end clamp if any.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_events.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py python_backend/tests/test_highlight_candidate_comparison.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

## PR6: API And UI Visibility

**Purpose:** Make the AI improvement lifecycle understandable to an operator: proposed, approved, executed, compared, gated, promoted, rejected, unsupported, or resolved.

**Files:**

- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/runs.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `lib/api-spec/openapi.yaml`
- Regenerate: `lib/api-zod/src/generated/`
- Regenerate: `lib/api-client-react/src/generated/`
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`

**Build:**

- [ ] Expose candidate registry summaries and comparison statuses in API responses.
- [ ] Expose final manifest summaries and promoted/rejected/pending candidate states.
- [ ] Show statuses: proposed, approved, executed, pass, warn, fail, unavailable, resolved_not_visible, promoted, rejected, unsupported.
- [ ] Show why a suggestion cannot execute: missing evidence, unsafe window, unsupported candidate type, no comparison, or failed gate.
- [ ] Add controls that distinguish review-only from execute-approved.
- [ ] Require explicit approval ids for all mutations.
- [ ] Ensure UI does not label review-only notes as applied improvements.

**Tests:**

- [ ] API returns registry, comparison, gate, and final-manifest summaries.
- [ ] API refuses execution without explicit approval ids.
- [ ] OpenAPI export and generated clients stay in sync.
- [ ] UI handles no-candidate, pending, passed, failed, resolved no-op, unsupported, and promoted states.
- [ ] UI labels advisory report items as review-only until a selected candidate has execution, comparison, and gate evidence.
- [ ] UI/API never exposes a mutation button that can execute every action merely because an approval file exists.

**Deliver:**

- Operator-facing trace from AI suggestion to final artifact.
- Fewer ambiguous "AI said OK" moments.
- API and UI evidence chain for every visible candidate state.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py python_backend/tests/test_export_openapi.py -q
pnpm run typecheck
git diff --check
```

## PR7: Docs, Real-Video Verification, And Skill Capture

**Purpose:** Consolidate already-proven workflow behavior into operator docs, run a final real-video evidence pass, and prepare a separate local skill update.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`
- Outside PR, after user confirmation: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Document AI review vs AI improvement.
- [ ] Document strong-model routing and recommended environment variables.
- [ ] Document canonical approval ids and compatibility aliases.
- [ ] Document temporal parallelism vs broad spatial split/SAHI tradeoff.
- [ ] Document candidate statuses, comparison reports, quality gate, no-op resolution, and final manifest.
- [ ] Add commands for dry-run, artifact-only, real mode, selected approvals, and promotion.
- [ ] Add real-video checklist covering missing ball, noise, camera shake, highlight tail, and final inspection.
- [ ] Record the right-bottom `2049-2544` gap and key frame `2079` verification case.
- [ ] Add a "Skill Capture Notes" section with the verified workflow, but do not edit the local skill inside the repository PR.
- [ ] After PR merge and separate user confirmation, update the local `football-tracking-real-video-tuning` skill with the verified workflow and commands.

**Tests:**

- [ ] Run focused tests from PR2-PR6.
- [ ] Run the full Python test suite.
- [ ] Run or reuse a real baseline output.
- [ ] Run AI review/improvement in real/provider mode with a configured strong visual/improvement model; this is required for the final evidence pack to be complete.
- [ ] If provider is unavailable, record the run as `incomplete_provider_unavailable` and rerun deterministic artifact-only checks only as fallback diagnostics, not as release-ready proof.
- [ ] Execute or fixture-prove at least one bounded missing-ball candidate and one `resolved_not_visible` no-op.
- [ ] Fixture-test and, where source artifacts exist, real-test one candidate each for noise, follow-cam, and highlight classes.
- [ ] Render follow-cam and verify `camera_motion_audit.json`.
- [ ] Render at least one highlight and verify tail coverage.
- [ ] Manually inspect frames `2049-2544`, especially `2079`, and record whether the right-bottom action is visible and covered.

**Deliver:**

- Updated operator docs.
- Real-video evidence pack with output dirs, commands, model settings, quality-gate status, candidate decisions, final videos, and highlight clips.
- Skill-capture notes in docs, plus a separate local skill update only after explicit post-merge confirmation.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests -q
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py --output-dir <real-output-dir> --input-video <real-video> --mode real --parallel-mode temporal --approval-ids <approved-id>
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py --output-dir <real-output-dir> --input-video <real-video> --mode artifact-only --parallel-mode temporal
git diff --check
```

## End-To-End Acceptance

- Missing-ball cases pass only with packet/visual evidence plus approved recovery or evidence-backed `not_visible`.
- Noise cases reduce false-positive islands without increasing missed-ball coverage beyond tolerance.
- Follow-cam cases produce smoother candidate videos without hiding tracking loss.
- Highlight clips preserve the core event and post-event tail.
- Review-only phases leave track CSV hashes unchanged.
- Approved actions execute only through explicit ids.
- Final outputs can be traced from baseline to candidate, comparison, approval, gate, and manifest.
- The known right-bottom gap around `2049-2544`, key frame `2079`, remains covered by tests and real-video verification.
