# AI Improvement Actionable Candidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or the repository managed-PR process to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert AI from passive review into a bounded improvement system for missing-ball recovery, noise cleanup, follow-cam stabilization, and highlight boundary tuning.

**Architecture:** Baseline tracking output remains immutable. AI can only propose bounded actions with evidence; explicit approval turns them into isolated candidates under `ai_candidates/<problem_type>/<candidate_id>/`; comparison reports, lifecycle, quality gate, and explicit finalization decide whether a candidate becomes final output.

**Tech Stack:** Python backend, FastAPI service, JSON artifacts, pytest, existing review packets and visual review, AI improvement workflow, candidate registry/lifecycle, final artifact manifest, follow-cam renderer, highlight renderer, React operator UI, generated OpenAPI clients, and real-video verification against `python_backend/data/raw5760x144020fps.mp4`.

---

## Requirement Summary

The core requirement is **AI improvement, not AI audit alone**.

1. Missing ball: when the tracker loses the match ball, AI should help localize the ball in bounded windows or explicitly mark it not visible. A short local observation near frame `2079` must not be allowed to close a long gap such as `2049-2544` unless the whole gap is covered by recovery evidence, not-visible evidence, or explicit uncovered subranges.
2. Too much noise: when YOLO/SAHI creates many false ball detections, AI should classify likely false-positive classes and create bounded cleanup candidates. Broad full-video spatial splitting should not be the default because it improves speed but often increases noise.
3. Follow-cam is shaky: when the final follow-cam camera path has abrupt pan, acceleration, or zoom, AI should decide whether the root cause is tracking quality or follow-cam tuning, then create a rerender candidate only when the track is good enough.
4. Highlight clips: deterministic event candidates should use safe default pre/post buffers; AI can adjust boundaries only when the final clip still contains the event core and enough post-shot/result tail.

The repeatable lifecycle is:

```text
baseline artifacts
  -> review packet / visual evidence
  -> AI suggested action shape
  -> explicit operator/API approval
  -> isolated candidate artifact
  -> comparison report
  -> lifecycle + quality gate
  -> explicit finalization
  -> final manifest selected output
```

## Independent Review Revisions

This plan was reviewed by an independent agent before handoff. The final version incorporates the review by:

- Making `mark_ball_not_visible` a first-class missing-ball resolution path with manifest, lifecycle, and quality-gate coverage.
- Naming `noise_candidate_comparison.json` and requiring explicit pass/warn/fail checks so noise cleanup cannot silently delete the real ball.
- Expanding evidence ids by lane: packet/visual evidence for missing-ball and noise, `camera_motion_event_id` or camera audit evidence for follow-cam, and `event_candidate_id` plus core-window evidence for highlights.
- Separating AI `suggested_action` from operator `approved_action`: AI may propose a bounded shape, but `approval_id` must come from an operator/API/finalization action.
- Adding API route/schema-level finalization tests, not only service-level tests.
- Clarifying that broad spatial split/SAHI is not executable unless bounded to approved windows.

## Current Grounding

Already available or partially built:

- `ball_audit.json`, `ai_review_triggers.json`, `review_packets.json`, and `ai_visual_review.json` can identify review windows and visual evidence.
- Missing-ball and noise candidate/comparison modules exist, but need regression gates proving they still finalize safely.
- `camera_motion_audit.json` exists and is referenced from `follow_cam_report.json`.
- Event candidates and highlight rendering exist, but AI-adjusted highlight candidates need comparison-backed promotion.
- Candidate lifecycle, candidate registry, quality gate, and final artifact manifest exist.
- `finalize_ai_candidate(...)` exists as the explicit promotion/rejection boundary.

Remaining gaps:

- AI action shapes need a closed, public executable contract.
- Existing missing-ball and noise lanes need current regression proof across lifecycle, quality gate, and final manifest.
- Follow-cam needs an executable rerender candidate lane plus comparison.
- Highlights need an AI-adjusted candidate lane plus publish gate.
- API/UI need to show candidate states and expose promote/reject controls.
- Prompts/model routing need to force bounded, executable suggestions or safe review-only fallback.
- A real-video evidence pack must prove the full loop, especially the right-bottom long gap around `2049-2544` and final follow-cam/highlight outputs.

## Non-Negotiable Safety Rules

- Review-only AI output must never mutate tracks, videos, highlight clips, or final manifests.
- AI may propose `suggested_action` shapes, but it must not invent `approval_id`; executable `approved_action` entries come only from operator/API/finalization approval.
- Every executable action needs `candidate_id`, `approval_id`, `problem_type`, bounded frame window when it touches frames, lane-specific evidence ids, expected artifact, and comparison criteria.
- Unknown, broad, unbounded, or untraceable AI actions become review-only/manual-review.
- Spatial split/SAHI is not executable unless bounded to approved windows; use temporal chunk parallelism for full-video speed.
- `pass` candidates still require explicit finalization.
- `warn` candidates require explicit human confirmation.
- `fail`, `unavailable`, missing-approval, and review-only candidates cannot become final output.
- Follow-cam improvement cannot hide tracking loss by zooming out too far.
- Highlight improvement cannot cut the event core or available post-event tail.

## PR1: Executable AI Action Contract And Existing Lane Regression

**Purpose:** lock the contract before adding new executors, and prove missing-ball/noise lanes are finalizable but non-mutating until explicit promotion.

**Files:**

- Modify: `python_backend/football_tracking/ai_contracts.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Modify: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `docs/operations/ai-improvement-contract.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_missing_ball_candidate_executor.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Define the public executable action set: `localize_ball_roi`, `rerun_ball_window`, `mark_ball_not_visible`, `noise_filter_adjustment`, `tighten_noise_filter`, `reject_noise`, `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, `adjust_highlight_window`, and `render_suggested_highlight`.
- [ ] Normalize legacy `targeted_rerun` input to public `rerun_ball_window` at artifact boundaries.
- [ ] Split `suggested_action` from `approved_action`: AI output can be proposed and executable-shaped, but only explicit operator/API approval can assign `approval_id` and create consumable approved actions.
- [ ] Require common executable fields: `approval_id`, `candidate_id`, `problem_type`, `approved_action`, lane-specific evidence id, expected artifact, comparison criteria, and bounded frame window where frames are touched.
- [ ] Allow lane-specific evidence ids:
  - Missing-ball/noise: `source_packet_id`, `visual_review_id`, or equivalent packet/visual provenance.
  - Follow-cam: `camera_motion_event_id`, `camera_motion_audit` event reference, or packet generated from camera-motion evidence.
  - Highlight: `event_candidate_id`, `core_window`, and source event metadata.
- [ ] Reject or downgrade unknown, broad, full-video, or untraceable model actions to review-only.
- [ ] Require missing-ball suggestions to include full-window coverage or explicit uncovered subranges.
- [ ] Add a regression guard so evidence near frame `2079` cannot close the full `2049-2544` gap unless the full gap is covered.
- [ ] Require `mark_ball_not_visible` to produce `missing_ball_resolution.json` rather than a recovery comparison.
- [ ] Require `missing_ball_resolution.json` full-window evidence before it can enter `resolved_noop_candidates` in the final manifest.
- [ ] Ensure a valid not-visible resolution is recorded as `resolved_not_visible` / resolved-noop, not as a recovered track.
- [ ] Require noise suggestions to include bounded window, false-positive class, source packet/visual evidence, and safe `candidate_id`.
- [ ] Treat `noise_candidate_comparison.json` as the required comparison report for noise cleanup candidates.
- [ ] Require noise comparison checks for bounded scope, false-positive island reduction, retained trusted match-ball frames, no new long gap, evidence-backed removal class, and no broad full-video SAHI provenance.
- [ ] Keep generated missing-ball/noise candidates pending in final manifest until `finalize_ai_candidate(...)` is called.
- [ ] Preserve previous finalization selections when the stable workflow refreshes `final_ai_improvement_artifact_manifest.json`.
- [ ] Record provider mode, selected model, candidate intent, and whether a model call can lead to executable candidates.

**Tests:**

- [ ] Contract accepts canonical `rerun_ball_window` and `mark_ball_not_visible`.
- [ ] Legacy `targeted_rerun` is normalized but not emitted as the new public action.
- [ ] Unknown action becomes review-only.
- [ ] Unbounded full-video spatial split/SAHI is non-executable.
- [ ] Frame `2079` local evidence cannot close `2049-2544` without full coverage.
- [ ] `mark_ball_not_visible` covering only frame `2079` cannot close `2049-2544`.
- [ ] `mark_ball_not_visible` covering the full `2049-2544` window appears in lifecycle, quality gate, and final manifest as resolved-noop.
- [ ] A not-visible resolution never appears as a recovered track output.
- [ ] Missing-ball pass candidate is not final until explicit promotion.
- [ ] Noise pass candidate is not final until explicit promotion.
- [ ] Noise cleanup comparison writes `noise_candidate_comparison.json`.
- [ ] Noise cleanup reducing false-positive islands passes only when trusted match-ball recall is preserved.
- [ ] Noise cleanup that removes trusted real-ball detections fails.
- [ ] Noise cleanup that hides or creates a long gap fails.
- [ ] Noise cleanup with unbounded full-video SAHI provenance fails.
- [ ] Missing-ball/noise warn candidates require human confirmation.
- [ ] Fail/unavailable/review-only/missing-approval/unknown-candidate cases cannot promote.
- [ ] `mark_ball_not_visible` creates resolved-noop evidence and does not require recovery comparison.
- [ ] Final manifest refresh does not erase existing finalized selections.
- [ ] Lifecycle, quality gate, and manifest show promoted, rejected, pending, and blocked states.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests/test_ai_improvement.py `
  python_backend/tests/test_ai_visual_review.py `
  python_backend/tests/test_review_packets.py `
  python_backend/tests/test_ai_candidate_lifecycle.py `
  python_backend/tests/test_ai_improvement_quality_gate.py `
  python_backend/tests/test_final_artifact_manifest.py `
  python_backend/tests/test_missing_ball_candidate_executor.py `
  python_backend/tests/test_missing_ball_recovery_comparison.py `
  python_backend/tests/test_noise_candidate_comparison.py `
  python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

**Deliver:**

- Closed executable action contract.
- Current missing-ball/noise lanes proven safe and finalizable.
- First-class not-visible resolution lane for missing-ball gaps.
- Explicit `noise_candidate_comparison.json` contract protecting true-ball recall.
- Regression protection for the long right-bottom gap.
- Updated contract and workflow docs.

## PR2: Follow-Cam AI Candidate Executor And Comparison

**Purpose:** turn shaky-camera AI suggestions into isolated follow-cam rerender candidates and compare them against the baseline camera path.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_executor.py`
- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Add `execute_follow_cam_candidate(output_dir, approved_action, *, input_video=None, baseline_dir=None)`.
- [ ] Write candidates under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Accept only selected `adjust_follow_cam` actions for pure rerender.
- [ ] Allow full-video follow-cam rerender candidates, because rerendering camera crop does not rerun detection or mutate baseline tracks.
- [ ] Keep tracking/detection reruns bounded or linked to already passing recovery/noise candidates.
- [ ] Treat `tracking_rerun_before_follow_cam` as blocked unless linked to a passing or promoted missing-ball/noise candidate.
- [ ] Apply only allowlisted follow-cam config keys: pan smoothing, zoom smoothing, max pan per frame, max zoom per frame, zoom confirm frames, and zoom hold frames.
- [ ] Render candidate `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, and `camera_motion_audit.json`.
- [ ] Write `follow_cam_candidate_comparison.json`.
- [ ] Compare baseline and candidate over the same evaluable frames.
- [ ] Compare review event count, p95 pan step, max pan acceleration, max zoom step, max zoom ratio, p95 crop height, max crop height, and Detected/Predicted crop coverage.
- [ ] Fail candidates that look smoother mainly because they zoom out too far.
- [ ] Register follow-cam candidates and require `finalize_ai_candidate(..., output_role="follow_cam_video")` for promotion.

**Tests:**

- [ ] Smoother candidate passes.
- [ ] Shakier candidate fails.
- [ ] Over-zoomed candidate fails.
- [ ] Source-size zoom guard fails when crop is too wide.
- [ ] Crop coverage regression fails.
- [ ] Unknown follow-cam config patch key fails.
- [ ] Sparse camera path returns warn/unavailable, not pass.
- [ ] Tracking-first action blocks without linked recovery candidate.
- [ ] Tracking-first action allows linked passing recovery candidate.
- [ ] Follow-cam pass candidate remains pending until explicit promotion.
- [ ] Promoted follow-cam candidate appears in lifecycle, quality gate, and final manifest.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests/test_follow_cam_candidate_comparison.py `
  python_backend/tests/test_follow_cam.py `
  python_backend/tests/test_review_packets.py `
  python_backend/tests/test_ai_candidate_lifecycle.py `
  python_backend/tests/test_ai_improvement_quality_gate.py `
  python_backend/tests/test_final_artifact_manifest.py `
  python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

**Deliver:**

- Executable follow-cam improvement lane.
- Camera comparison report with pass/warn/fail reasons.
- Final-manifest support for promoted follow-cam video.

## PR3: Highlight AI Candidate Comparison And Publish Gate

**Purpose:** let AI adjust highlight windows while guaranteeing the event and aftermath remain in the clip.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/events.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Add `execute_highlight_candidate(output_dir, approved_action, *, input_video=None)`.
- [ ] Accept selected `adjust_highlight_window` and `render_suggested_highlight` actions.
- [ ] Use `event_candidates.json` as the owner of `core_window`, default pre-buffer, default post-buffer, and `buffer_policy.min_tail_frames`.
- [ ] Record the trusted source of `core_window`: deterministic event metadata, operator-confirmed metadata, or AI evidence that is still bounded by event metadata.
- [ ] Clamp render windows to source video bounds and record clamps.
- [ ] Require AI-adjusted windows to include the full event core.
- [ ] Require AI-adjusted windows to include the available required post-event tail.
- [ ] Render candidate `highlight.mp4` and `highlight_report.json`.
- [ ] Write `highlight_candidate_comparison.json`.
- [ ] Fail invalid bounds, mismatched event ids, cut event core, and cut available tail.
- [ ] Register highlight candidates and require explicit finalization with `output_role="highlight_clip"`.
- [ ] Allow multiple promoted highlight clips by event id or candidate id.

**Tests:**

- [ ] Default pre/post buffer is applied.
- [ ] Highlight comparison records the trusted source of `core_window`.
- [ ] Highlight candidate with mismatched or untrusted core-window source cannot pass.
- [ ] AI window cutting event core fails.
- [ ] AI window cutting available tail fails.
- [ ] End-of-video clamp is recorded.
- [ ] Review-only highlight cannot be promoted.
- [ ] Generated highlight candidate is not final until promotion.
- [ ] Comparison-backed highlight can be promoted.
- [ ] Multiple highlight promotions are allowed.
- [ ] Highlight final output appears in lifecycle, quality gate, and final manifest.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests/test_highlight_candidate_comparison.py `
  python_backend/tests/test_events.py `
  python_backend/tests/test_highlights.py `
  python_backend/tests/test_accepted_highlights.py `
  python_backend/tests/test_review_packets.py `
  python_backend/tests/test_ai_candidate_lifecycle.py `
  python_backend/tests/test_ai_improvement_quality_gate.py `
  python_backend/tests/test_final_artifact_manifest.py `
  python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

**Deliver:**

- Comparison-backed highlight improvement lane.
- Safe AI-adjusted highlight clips.
- Explicit no-safe-highlight evidence when AI cannot safely improve boundaries.

## PR4: API And UI Candidate Finalization Controls

**Purpose:** make the operator able to see, promote, reject, or confirm AI candidates for all lanes.

**Files:**

- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `lib/api-spec/openapi.yaml`
- Regenerate: `lib/api-zod/src/generated/**`
- Regenerate: `lib/api-client-react/src/generated/**`
- Modify: `artifacts/web/src/lib/api.ts`
- Modify: `artifacts/web/src/lib/aiLifecycle.ts`
- Modify: `artifacts/web/src/lib/types.ts`
- Modify: `artifacts/web/src/lib/i18n.ts`
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Test: `python_backend/tests/test_api_service.py`
- Test: `artifacts/web/src/lib/aiLifecycle.test.ts`

**Build:**

- [ ] Add API schemas for candidate finalization: run id, problem type, candidate id, approval id, decision, output role, warn confirmation, and note.
- [ ] Add promote/reject endpoint that calls `ApiService.ai_candidate_finalize(...)`.
- [ ] Repeat the approval safety rule at the API boundary: approval-file presence alone is not approval; route calls must supply the intended candidate/action ids.
- [ ] Return blocked reasons for review-only, missing comparison, fail, unavailable, warn without confirmation, unsupported output role, missing approval, unknown candidate, and unsafe path.
- [ ] Show lifecycle states in UI: suggested, approved, pending execution, executed, compared, pending confirmation, promoted, rejected, and blocked.
- [ ] Show promote/reject controls only for finalizable candidates.
- [ ] Require an explicit confirmation control for `warn` candidates.
- [ ] Keep deliverable views tied to `final_ai_improvement_artifact_manifest.json`, not raw AI suggestions.

**Tests:**

- [ ] API promotes pass candidates for missing-ball, noise, follow-cam, and highlight roles.
- [ ] Route/schema test rejects missing or malformed `approval_id`.
- [ ] Route/schema test rejects unsupported `output_role`.
- [ ] API blocks warn promotion without confirmation.
- [ ] API accepts warn promotion with explicit confirmation.
- [ ] Route/schema test supports multiple highlight promotions without overwriting earlier promoted clips.
- [ ] API blocks fail/unavailable/review-only/missing-approval/unknown-candidate.
- [ ] UI labels finalizable, blocked, promoted, and rejected states correctly.
- [ ] UI does not display unfinalized pass candidates as final deliverables.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests/test_api_service.py `
  python_backend/tests/test_ai_candidate_lifecycle.py `
  python_backend/tests/test_final_artifact_manifest.py -q
python_backend/scripts/export_openapi.py --output lib/api-spec/openapi.yaml
corepack pnpm --filter @workspace/api-spec run codegen
corepack pnpm --filter @workspace/web run test:lifecycle
corepack pnpm run typecheck:libs
corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck
```

**Deliver:**

- Operator-facing promote/reject workflow.
- Final deliverable pages that reflect final manifest selections.
- Updated OpenAPI and generated clients.

## PR5: Prompt, Improvement-Capable Model Routing, And Workflow Hardening

**Purpose:** force AI to produce executable, bounded improvement candidates or safe review-only output.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/scripts/run_ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `docs/operations/ai-improvement-contract.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Update prompts so every executable suggestion maps to the closed action set from PR1.
- [ ] Require missing-ball prompts to return recovered ROI, not-visible closure, or uncovered subranges.
- [ ] Require noise prompts to return false-positive class and bounded cleanup scope.
- [ ] Require camera prompts to choose tracking repair before follow-cam rerender when track context is Lost/Predicted or has large missing windows.
- [ ] Require follow-cam rerender suggestions to include allowlisted config patch and target metric.
- [ ] Require highlight suggestions to preserve event core and post-event tail.
- [ ] Record provider, resolved model, candidate intent, and model-selection source in artifacts.
- [ ] If an improvement-capable model is unavailable in real mode, fall back to non-mutating review-only/unavailable behavior.

**Tests:**

- [ ] Missing-ball prompt requires coverage/uncovered subranges.
- [ ] Noise prompt requires bounded false-positive class.
- [ ] Lost-track camera prompt routes to tracking-first.
- [ ] Follow-cam prompt requires target metric and allowlisted config patch.
- [ ] Highlight prompt requires core and tail.
- [ ] Untraceable suggestions are review-only.
- [ ] Review-only mode does not emit executable approval.
- [ ] Workflow report records model and candidate intent.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests/test_ai_improvement.py `
  python_backend/tests/test_ai_visual_review.py `
  python_backend/tests/test_review_packets.py `
  python_backend/tests/test_stable_ai_improvement_workflow.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
```

**Deliver:**

- Improved prompts for executable candidates.
- Config-driven improvement-capable model policy.
- Non-mutating fallback for weak/unavailable model paths.

## PR6: Real-Video Evidence Pack And Documentation

**Purpose:** prove the complete loop on the real match video and document how to repeat it.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`
- Create: `docs/operations/real-video-ai-improvement-evidence.schema.json`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`

**Build:**

- [ ] Run baseline full workflow on `python_backend/data/raw5760x144020fps.mp4`.
- [ ] Use temporal chunk parallelism for speed.
- [ ] Keep SAHI/ROI bounded to approved windows; broad full-video spatial split/SAHI is not executable from the AI workflow.
- [ ] Run AI improvement with the configured improvement-capable model.
- [ ] Execute safe approved candidates for missing-ball, noise, follow-cam, and highlight lanes.
- [ ] Promote or reject candidates through finalization.
- [ ] Save before/after evidence for the `2049-2544` right-bottom gap and frame `2079`.
- [ ] Save subrange coverage or explicit not-visible ranges for any long missing-ball gap AI claims to address.
- [ ] Save before/after dense-noise evidence with false-positive classes.
- [ ] Save follow-cam before/after camera-motion metrics and over-zoom guard metrics.
- [ ] Save highlight core/tail frame windows and final clip windows.
- [ ] Validate review media/contact sheets against source frames so AI evidence is not based on seek artifacts.
- [ ] Write local `real_video_ai_improvement_evidence.json` containing run id, input path/hash, output dir, provider/model, candidate ids, approval ids, comparison statuses, promotion decisions, final artifact refs, checksums, manual notes, and no-safe-highlight reason when relevant.
- [ ] Validate evidence JSON against the committed schema.
- [ ] Keep generated MP4 files and large run artifacts out of git.

**Tests:**

- [ ] Run backend test suite after final backend PRs.
- [ ] Run frontend/API type checks after API/UI PR.
- [ ] Confirm final follow-cam has `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, and `camera_motion_audit.json`.
- [ ] Confirm each executed candidate has `candidate_manifest.json`, comparison report, registry entry, quality-gate evidence, final manifest entry, and lifecycle state.
- [ ] Confirm highlight clips preserve event core and required tail, or record explicit no-safe-highlight evidence.
- [ ] Confirm final manifest explains every rejected candidate.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
pytest python_backend/tests -q
corepack pnpm run typecheck:libs
corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck
```

**Deliver:**

- Repeatable real-video evidence pack.
- Updated README and operation docs.
- Final recommendation naming the stable follow-cam output and usable highlight clips.
- Local post-merge skill update for `football-tracking-real-video-tuning`; this is outside managed PR scope and must not block CI.

## Managed PR Process

For each PR:

- [ ] Start from latest `origin/main`.
- [ ] Use one branch per PR.
- [ ] Let a worker agent implement the PR.
- [ ] Let a separate reviewer agent review the branch before remote PR.
- [ ] Resolve valid Critical and Important review findings.
- [ ] Run listed focused tests and relevant broader tests.
- [ ] Push, open/update GitHub PR, and wait for CI/Copilot comments.
- [ ] Fix confirmed remote comments.
- [ ] Merge only after checks and valid comments are resolved.
- [ ] Delete merged remote/local branches after merge and explicit managed-run authorization.

## Final Acceptance

- [ ] AI can improve missing-ball gaps through bounded recovery or not-visible resolution.
- [ ] AI can improve noisy detections through bounded cleanup candidates.
- [ ] AI can improve shaky follow-cam output through comparison-backed rerender candidates.
- [ ] AI can improve highlight boundaries without cutting the play or tail.
- [ ] Every final artifact is traceable from baseline evidence to final manifest selection.
- [ ] The real-video run produces a stable follow-cam output and either usable highlight clips or explicit no-safe-highlight evidence.
