# AI Improvement Reviewed Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert AI from a review-only assistant into a bounded improvement loop that can recover missing balls, reduce false-positive noise, smooth final follow-cam output, and adjust highlight clip windows without silently mutating final artifacts.

**Architecture:** AI proposes evidence-backed candidates. A candidate can affect final output only after explicit approval ids, isolated execution, objective comparison, registry entry, quality gate, and final manifest promotion. Full-video speedups use temporal chunk parallelism by default; spatial splitting/SAHI is reserved for bounded approved ROI windows because broad splitting increases false positives.

**Tech Stack:** Python backend, FastAPI service layer, OpenAI-compatible provider wrapper, JSON artifacts, existing tracking/follow-cam/highlight modules, pytest, real-video verification.

---

## 1. Requirement Summary

The product need is AI improvement, not AI review.

User-visible needs:

1. **Missing ball:** when a long gap occurs, AI inspects packet media and either localizes the ball for bounded recovery or produces evidence that the ball is not visible.
2. **Too much noise:** when YOLO/SAHI produces extra ball-like detections, AI classifies likely false positives and proposes bounded cleanup candidates.
3. **Shaky follow-cam:** when final `follow_cam.mp4` has sudden pan, acceleration, or zoom, AI decides whether the fix should be tracking recovery or follow-cam rerendering, then produces a comparable candidate.
4. **Highlight timing:** default clips keep a safe pre/post buffer; AI may adjust boundaries, but must preserve the core shot/goal and the required post-event tail unless the source video ends.
5. **Model choice:** hard visual localization and run-level improvement should use the stronger configured improvement model by default. Smaller models are acceptable only for dry-run, low-risk tagging, or smoke checks.
6. **Parallelism:** use temporal segment parallelism for throughput. Avoid broad full-frame spatial splitting as a default because it increases false positives; keep SAHI/ROI bounded to approved windows.

## 2. Current Status Matrix

| Area | Status | Existing modules/artifacts | Gap to close |
| --- | --- | --- | --- |
| Ball quality audit | Landed in repository | `ball_audit.json`, `python_backend/football_tracking/ball_audit.py` | Use audit events as candidate triggers and final gate evidence. |
| AI review triggers | Landed in repository | `ai_review_triggers.json`, `python_backend/football_tracking/ai_review_triggers.py` | Keep review triggers advisory; do not treat them as improvements. |
| Review packets | Landed in repository | `review_packets.json`, `python_backend/football_tracking/review_packets.py` | Ensure missing-ball, noise, camera, and highlight packet coverage is sufficient for AI evidence. |
| Visual review | Partly landed | `ai_visual_review.json`, `python_backend/football_tracking/ai_visual_review.py`, `python_backend/scripts/run_ai_visual_review.py` | Provider-backed visual localization must be wired into the stable workflow and routed to a strong model. |
| AI improvement report | Partly landed | `ai_improvement_report.json`, `python_backend/football_tracking/ai_improvement.py` | Separate review-only notes from executable candidate suggestions in every downstream UI/API path. |
| Candidate registry | Partly landed, current PR1 branch is hardening it | `ai_candidate_registry.json`, `python_backend/football_tracking/ai_candidate_registry.py` | Finish safe candidate id/path handling and register candidates only after child artifacts are valid. |
| Missing-ball comparison | Partly landed, current PR1 branch is hardening it | `missing_ball_recovery_comparison.json`, `python_backend/football_tracking/missing_ball_recovery_comparison.py` | Finish selected approval coverage checks, right-bottom gap regression, and fail-closed comparison rules. |
| Camera motion audit | Landed in repository | `camera_motion_audit.json`, `python_backend/football_tracking/camera_motion_audit.py`, follow-cam report summary | It is an audit trigger only; add follow-cam rerender candidates and comparison. |
| Event/highlight generation | Partly landed | `event_candidates.json`, `highlight_report.json`, `accepted_highlights.json`, `python_backend/football_tracking/events.py`, `highlights.py`, `accepted_highlights.py` | Do not rebuild existing `core_window`, `render_window`, or `buffer_policy`; add candidate comparison, registry/gate integration, and publish evidence. |
| Stable workflow | Partly landed | `stable_ai_improvement_workflow_report.json`, `python_backend/scripts/run_stable_ai_improvement_workflow.py` | Execute selected candidate ids, compare, gate, and promote. Approval-file presence must never execute work. |
| API/UI visibility | Partly landed | FastAPI routes, generated clients, web AI pages | Show proposed/approved/executed/pass/warn/fail/promoted state clearly. |
| Docs and real-video verification | Partly landed | `docs/operations/ai-improvement-workflow.md`, READMEs | Add operator checklist and real-video evidence for frame `2079`, `2049-2544`, camera shake, noise, and highlight tail. |

## 3. Cross-Cutting Product Rules

- AI suggestions are not improvements until they produce candidate artifacts and comparison reports.
- Every executable candidate must have `candidate_id`, problem type, bounded frame window, evidence ids, approval id, provenance, comparison report, and registry entry.
- Use one canonical approval input for execution: `--approval-ids` and API `approval_ids`. Legacy single-action highlight/follow-cam arguments may remain as compatibility aliases, but docs must steer users to the canonical list form.
- Approval-file presence alone is never permission to execute.
- Real/improve mode cannot pass if a selected executable approval has no comparison or no resolved no-op record.
- `not_visible` is valid only when packet or visual evidence shows the ball is hidden, off-frame, or impossible to identify. It should create a resolved no-op record, not an empty failure loop.
- Follow-cam rerender must not hide tracking failure. If camera shake overlaps Lost/Predicted track context, recommend tracking recovery first.
- Highlight suggestions must preserve `core_window` and required tail.
- Final promotion accepts `pass`; `warn` requires same-candidate human confirmation; `fail` and `unavailable` are not promoted.

## 4. Model Routing Rules

Use this model selection order:

1. Request-level `model`, if supplied.
2. `PROVIDER_OPENAI_VISUAL_REVIEW_MODEL` for packet image localization, if added/configured.
3. `PROVIDER_OPENAI_IMPROVEMENT_MODEL` for both visual localization and run-level improvement.
4. Existing chat model fallback only for dry-run, smoke, or low-risk advisory tagging.

Reports must record `model`, `model_selection_source`, provider mode, `candidate_intent`, and whether the result was visual localization, run-level reasoning, or both.

## 5. Candidate Artifact Contract

Every candidate directory follows:

```text
ai_candidates/<problem_type>/<candidate_id>/
```

Each candidate writes:

- `candidate_manifest.json`: candidate id, problem type, source approval ids, baseline output, frame window, evidence ids, model metadata, generated artifacts.
- `*_comparison.json`: problem-specific comparison with `summary.status` of `pass`, `warn`, `fail`, or `unavailable`.
- Candidate-scoped audit artifacts when relevant, for example `ball_audit.json`, `camera_motion_audit.json`, `highlight_report.json`.
- No final artifact is overwritten during candidate execution.

## 6. Managed PR Plan

### PR1: Finish Missing-Ball Candidate Execution

**Purpose:** Complete the current branch so selected AI-approved missing-ball work can create an isolated child candidate, compare it to baseline, and register it only after artifact validation.

**Primary files:**

- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_ai_candidate_registry.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Keep outputs under `ai_candidates/missing_ball/<candidate_id>/`.
- [ ] Execute only selected `localize_ball_roi` or `targeted_rerun` approvals from explicit approval ids.
- [ ] Require and validate safe `candidate_id` before writing approval or candidate artifacts.
- [ ] Reject unsafe nested paths and path traversal, including `..` embedded in candidate ids.
- [ ] Clamp approved windows to source video bounds and max frame budget.
- [ ] Fail closed when source frame count is unknown for direct localize/high-recall execution.
- [ ] Use the runner's effective ROI for plausibility and wrong-target checks.
- [ ] Generate `missing_ball_recovery_comparison.json`.
- [ ] Register the candidate only after child comparison, manifest, and metrics artifacts succeed.
- [ ] Fail closed instead of overwriting corrupt parent `ai_candidate_registry.json`.
- [ ] Preserve the known right-bottom gap fixture: frames `2049-2544`, key frame `2079`, with labels `start`, `middle`, `end`, `tail`.
- [ ] Preserve candidate-scoped `ball_audit.json`; final snapshot generation must not overwrite it with parent evidence.

**Test:**

- [ ] Approved ROI action creates a child candidate directory and comparison report.
- [ ] Approval-file presence without selected approval id does not execute.
- [ ] Missing or unsafe `candidate_id` is rejected before artifacts are written.
- [ ] Parent registry is unchanged when child artifact finalization fails.
- [ ] Corrupt parent registry causes fail-closed behavior and is not overwritten.
- [ ] Candidate artifact paths remain inside the parent output directory.
- [ ] Direct recovery without source frame count fails closed.
- [ ] Effective ROI, not requested ROI alone, is used by comparison checks.
- [ ] Packet coverage fails when required labels leave uncovered ranges.
- [ ] Packet labels on related approvals count toward coverage.
- [ ] The `2049-2544` fixture cannot pass by recovering only a few frames near `2079`.
- [ ] Real-mode quality gate reports selected missing-ball approval without comparison as unavailable or fail.

**Deliver:**

- Missing-ball selected approval -> candidate output -> comparison -> registry -> quality gate evidence.
- Regression protection for the right-bottom corner sequence.

### PR2: Stable Workflow Dispatcher, Strong Model Routing, And No-Op Resolution

**Purpose:** Make the workflow execute selected candidate approvals through a dispatcher, wire provider-backed visual localization into the workflow, and define how evidence-backed `not_visible` closes a missing-ball issue without pretending a track was recovered.

**Primary files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/ai_provider.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_config_and_provider.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Run review packets before visual review in real/improve mode.
- [ ] Route visual review/localization to request model, `PROVIDER_OPENAI_VISUAL_REVIEW_MODEL`, or `PROVIDER_OPENAI_IMPROVEMENT_MODEL` in that order.
- [ ] Feed `ai_visual_review.json` into `ai_improvement.py` so visual ROI evidence can become executable missing-ball candidates.
- [ ] Add a small dispatcher that maps `problem_type` and action to an executor.
- [ ] Support missing-ball execution and resolved-not-visible no-op in this PR.
- [ ] For unsupported noise/follow-cam/highlight approvals, record `unsupported_candidate_type` in the manifest without executing.
- [ ] Use canonical `--approval-ids` and API `approval_ids`; document legacy single id flags as compatibility aliases.
- [ ] Add `missing_ball_resolution.json` for evidence-backed `not_visible`.
- [ ] Register resolved-not-visible as a no-op candidate with status `resolved_not_visible`.
- [ ] Let quality gate accept resolved-not-visible only when source packet or visual evidence is traceable.
- [ ] Add final manifest entries for accepted, rejected, pending, unsupported, and resolved no-op candidates.

**Test:**

- [ ] Dry-run records stages and warnings without mutating final media.
- [ ] Real/improve mode runs visual review before run-level improvement when provider is configured.
- [ ] Visual review uses the strong model routing order and records model selection.
- [ ] Missing/unavailable `ai_visual_review.json` is not described as successful localization.
- [ ] Unknown, duplicate, or malformed approval ids fail safely in non-dry-run mode.
- [ ] Only explicitly selected approvals execute.
- [ ] Unsupported candidate types are recorded as pending/unsupported, not silently executed.
- [ ] Evidence-backed `not_visible` writes `missing_ball_resolution.json` and passes the no-op gate.
- [ ] `not_visible` without packet or visual evidence fails the gate.

**Deliver:**

- One workflow path that can run review, visual localization, selected missing-ball execution, no-op resolution, quality gate, and manifest recording.
- Clear model routing for stronger AI use.

### PR3: Noise AI Cleanup Candidate Closed Loop

**Purpose:** Turn noisy detection review into bounded cleanup candidates that reduce false-positive islands without damaging real ball coverage.

**Primary files:**

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

- [ ] Promote dense-noise and candidate-ambiguity windows into review packets.
- [ ] Let AI classify bounded false positives as `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, `sideline_confusion`, `wall_background_drift`, `unknown_false_positive`, or `unknown`.
- [ ] Store the noise label as `false_positive_class`, not as generic failure tags.
- [ ] Execute approved cleanup on copied candidate artifacts only.
- [ ] Write candidates under `ai_candidates/noise/<candidate_id>/`.
- [ ] Candidate artifacts must include `ball_track.cleaned.csv`, `cleanup_report.json`, `ball_audit.json`, `noise_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Support conservative actions only: remove short islands, downweight repeated false-positive zones, or tighten postprocess thresholds inside a bounded window.
- [ ] Reject broad full-video spatial split/SAHI cleanup unless explicitly bounded and recall-preserving.
- [ ] Register noise candidates, run quality gate, and update final manifest in the same PR.

**Test:**

- [ ] Dense noise trigger creates review-packet coverage.
- [ ] AI classification without bounded evidence remains review-only.
- [ ] Cleanup removes short false-positive islands.
- [ ] Cleanup fails if sustained valid ball coverage drops.
- [ ] Cleanup fails if lost-frame count increases beyond tolerance.
- [ ] Candidate cleanup reruns `ball_audit.json` against candidate-scoped tracks.
- [ ] Comparison records removed island ranges and before/after coverage.
- [ ] Candidate with broad unbounded SAHI provenance fails comparison.
- [ ] Dispatcher can execute a selected noise approval end to end.
- [ ] Registry, quality gate, and final manifest summarize the noise candidate status.

**Deliver:**

- AI-assisted noise cleanup path with candidate comparison.
- Product rule encoded: temporal parallelism broadly, bounded spatial/SAHI only for approved ROI or cleanup windows.

### PR4: Follow-Cam AI Rerender Candidate Closed Loop

**Purpose:** Convert camera-motion audit events into AI-improvable follow-cam candidates that rerender a smoother final video without hiding tracking failures.

**Primary files:**

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

**Allowed AI follow-cam patch schema:**

```json
{
  "follow_cam": {
    "pan_smoothing": 0.2,
    "zoom_smoothing": 0.15,
    "glide_max_pan_per_frame_x": 80.0,
    "glide_max_pan_per_frame_y": 60.0,
    "catch_up_max_pan_per_frame_x": 140.0,
    "catch_up_max_pan_per_frame_y": 100.0,
    "max_zoom_in_per_frame": 24.0,
    "max_zoom_out_per_frame": 48.0,
    "zoom_hold_frames_after_change": 24
  }
}
```

**Build:**

- [ ] Include `camera_motion_audit.json` events in AI improvement context.
- [ ] Require AI to choose `adjust_follow_cam` or `tracking_rerun_before_follow_cam`.
- [ ] Route to `tracking_rerun_before_follow_cam` when the camera event overlaps Lost/Predicted track context.
- [ ] Execute approved follow-cam-only rerenders under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Follow-cam-only candidates must not mutate ball-track artifacts.
- [ ] Candidate must produce `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Comparison must check p95/max pan step, max acceleration, max zoom jump, shake event count, visible-ball/crop coverage proxy, and excessive zoom-out ratio.
- [ ] Fail candidates that reduce shake by zooming out beyond threshold or losing ball-centered coverage.
- [ ] Register follow-cam candidates, run quality gate, and update final manifest in the same PR.

**Test:**

- [ ] Camera motion events appear in AI context.
- [ ] Invalid follow-cam action without rerender plan is rejected.
- [ ] Approved rerender writes all candidate artifacts including `follow_cam.mp4`.
- [ ] Comparison passes when motion metrics improve and coverage is preserved.
- [ ] Comparison fails when motion metrics regress, ball coverage drops, or zoom-out exceeds threshold.
- [ ] Comparison uses `camera_motion_audit.json` metrics, not only follow-cam report metadata.
- [ ] Lost/Predicted overlap routes to tracking recovery instead of direct camera smoothing.
- [ ] Dispatcher can execute a selected follow-cam approval end to end.
- [ ] Registry, quality gate, and final manifest summarize candidate status.

**Deliver:**

- Follow-cam motion audit becomes an AI improvement trigger with executable rerender candidates.
- Operators can compare baseline and smoother candidate videos before promotion.

### PR5: Highlight Window AI Candidate Closed Loop

**Purpose:** Use existing event/highlight structures to make highlight clips preserve the event and aftermath, then allow AI to adjust clip boundaries safely through candidates.

**Primary files:**

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

- [ ] Keep existing `core_window`, `render_window`, and `buffer_policy` structures; harden them instead of recreating them.
- [ ] Preserve `pre_buffer_frames`, `post_buffer_frames`, `min_tail_frames`, and `min_post_event_frames`.
- [ ] Clamp render windows to source video length while preserving core event and required tail when possible.
- [ ] Reject AI windows that cut off the core event.
- [ ] Reject AI windows that cut off required tail unless source-video end forces the clamp.
- [ ] Execute approved highlight candidates under `ai_candidates/highlights/<candidate_id>/`.
- [ ] Candidate must produce `highlight.mp4`, `highlight_report.json`, `highlight_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Comparison must check source candidate id, bounds, core coverage, tail coverage, artifact existence, and render success.
- [ ] Accepted highlight publishing must require comparison evidence.
- [ ] Register highlight candidates, run quality gate, and update final manifest in the same PR.

**Test:**

- [ ] Default highlight window includes core event and post-event tail.
- [ ] Existing `buffer_policy` schema remains backward compatible.
- [ ] AI suggested window cutting off core is rejected.
- [ ] AI suggested window cutting off tail is rejected unless source-end clamp applies.
- [ ] Candidate render writes `highlight.mp4` and `highlight_report.json`.
- [ ] Comparison checks core coverage, tail coverage, bounds, and artifact existence.
- [ ] Accepted highlight copier accepts only publishable clips with comparison evidence.
- [ ] Dispatcher can execute a selected highlight approval end to end.
- [ ] Registry, quality gate, and final manifest summarize highlight candidate status.

**Deliver:**

- Highlight clips no longer end too early after a shot or goal.
- AI can tune clip boundaries without losing the key event or its aftermath.

### PR6: API And UI Visibility

**Purpose:** Let operators see what AI found, what AI proposed, what was executed, what passed, and what became final.

**Primary files:**

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
- [ ] Show why a suggestion cannot execute: missing evidence, unsafe window, unsupported candidate type, no comparison, failed gate.
- [ ] Add controls that distinguish review-only from execute-approved.
- [ ] Require explicit approval ids for all mutations.
- [ ] Ensure UI does not label review-only notes as applied improvements.

**Test:**

- [ ] API returns registry, comparison, quality-gate, and final-manifest summaries.
- [ ] API refuses candidate execution without explicit approval ids.
- [ ] OpenAPI export and generated clients stay in sync.
- [ ] UI handles no-candidate, pending, passed, failed, resolved no-op, and promoted states.

**Deliver:**

- Operators can trace every final output back to AI suggestion, approval, execution, comparison, and gate status.

### PR7: Docs, Real-Video Verification, And Evidence Pack

**Purpose:** Make the workflow repeatable and prove it against the known real-video failure modes.

**Primary files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`

**Build:**

- [ ] Document AI review vs AI improvement.
- [ ] Document model routing and recommended strong-model settings.
- [ ] Document canonical approval ids and compatibility aliases.
- [ ] Document temporal parallelism vs spatial split/SAHI tradeoff.
- [ ] Document candidate statuses, comparison reports, quality gate, no-op resolution, and final manifest.
- [ ] Add commands for dry-run, artifact-only, real mode, selected approvals, and promotion.
- [ ] Add a real-video checklist covering missing ball, noise, camera shake, highlight tail, and final inspection.
- [ ] Record the right-bottom `2049-2544` / key-frame `2079` verification case.

**Real-video verification requirements:**

- [ ] Record baseline output directory and candidate output directories.
- [ ] Record commands and model settings used.
- [ ] Save baseline/candidate evidence images or short clips for frames `2049-2544`, especially `2079`.
- [ ] Save baseline/candidate follow-cam evidence around camera-motion spikes.
- [ ] Save highlight before/after evidence showing core event and post-event tail.
- [ ] Record quality-gate status and final manifest decision.

**Test:**

- [ ] Run focused tests from PR1-PR6.
- [ ] Run full Python test suite.
- [ ] Run or reuse a real baseline output.
- [ ] Run AI review/improvement with provider configured when available.
- [ ] Execute at least one bounded missing-ball candidate if approval exists.
- [ ] Execute or fixture-test one candidate from noise, follow-cam, and highlight classes.
- [ ] Render follow-cam and verify `camera_motion_audit.json`.
- [ ] Render at least one highlight and verify tail coverage.
- [ ] Manually inspect frames `2049-2544`, especially `2079`, and record whether the right-bottom action is visible and covered.

**Deliver:**

- Updated docs and operator checklist.
- Real-video verification report with artifacts, decisions, and remaining risks.

## 7. PR Operation Checklist

Apply this checklist to each PR under the managed PR process:

- [ ] Start from latest `origin/main`.
- [ ] Identify unrelated local changes and leave them untouched.
- [ ] Use a fresh branch for the PR.
- [ ] Use TDD for behavior changes.
- [ ] Run focused tests listed in the PR.
- [ ] Run relevant broader tests before opening PR.
- [ ] Request independent spec and code-quality review.
- [ ] Fix Critical and Important review findings before PR publication.
- [ ] Push branch and open PR.
- [ ] Wait for remote checks and Copilot comments.
- [ ] Evaluate remote comments on merit and fix confirmed issues.
- [ ] Merge only after checks and valid review feedback are resolved.
- [ ] Delete merged remote and local branches when authorized.

## 8. Final Acceptance Criteria

- AI review and AI improvement are clearly separated in artifacts, API, UI, and docs.
- Missing-ball recovery can execute bounded AI-localized candidates and can close evidence-backed not-visible windows.
- Noise cleanup can reduce false positives without harming sustained ball coverage.
- Follow-cam camera-motion events can produce smoother rerender candidates with comparison evidence.
- Highlight clips preserve core event and aftermath; AI adjustments are validated.
- Stable workflow can run review, selected execution, comparison, quality gate, and promotion.
- Every final artifact can be traced back to baseline, candidate, comparison, approval, and quality-gate result.
- The known long right-bottom gap around frames `2049-2544` is protected by tests and real-video verification.

## 9. Independent Review Feedback Applied

This revision addresses the independent reviewer feedback by:

- Splitting current status into landed, partly landed, and current-branch/incomplete areas.
- Requiring PR3-PR5 to integrate dispatcher, registry, quality gate, and final manifest in the same PR.
- Treating highlight `core_window`, `render_window`, and `buffer_policy` as existing structures to harden, not rebuild.
- Defining strong-model routing for visual review and run-level improvement.
- Defining canonical approval ids for every executable mutation.
- Defining evidence-backed `not_visible` as a resolved no-op candidate path.
- Adding concrete follow-cam rerender patch schema and comparison metrics.
- Adding real-video visual evidence requirements for frame `2079`, gap `2049-2544`, camera shake, and highlight tail.
