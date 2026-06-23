# AI Executable Improvement Gap Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or the repository managed-PR process to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** convert the current AI review workflow into an AI improvement workflow that can safely recover missing balls, reduce false positives, smooth follow-cam output, and adjust highlight windows before producing final deliverables.

**Architecture:** baseline tracking artifacts remain immutable. AI produces bounded, traceable candidate actions; operators approve action ids; executors write isolated candidate artifacts under `ai_candidates/<problem_type>/<candidate_id>/`; comparison, quality gate, lifecycle, and final manifests decide whether each candidate is promoted, rejected, blocked, or still pending.

**Tech Stack:** Python backend, FastAPI service, JSON artifacts, pytest, existing review packets and visual review, candidate registry/lifecycle, quality gate, final artifact manifest, follow-cam renderer, highlight renderer, React operator UI, OpenAPI generated clients, and real-video verification against `python_backend/data/raw5760x144020fps.mp4`.

---

## Requirement Summary

The user requirement is **AI improvement**, not only **AI audit**.

Four user-visible improvement lanes must be covered:

1. **Ball cannot be found:** AI helps localize or recover the ball inside bounded missing-ball windows. A short local neighborhood, such as frame `2079`, cannot close a long gap such as `2049-2544` unless the full window is covered by recovery or not-visible evidence.
2. **Too many noisy ball detections:** AI helps classify and remove false-positive islands from extra balls, shoes, heads, sideline clutter, boards, and background drift. Full-video spatial split/SAHI is not the default because it tends to increase noise; use temporal parallelism for speed and bounded ROI/SAHI only for approved windows.
3. **Follow-cam is too shaky:** AI helps decide whether tracking must be repaired first or whether follow-cam parameters can be rerendered. Final judgment must use `camera_motion_audit.json` and crop coverage, not subjective text alone.
4. **Highlight clips need better boundaries:** deterministic event candidates provide default pre/post buffers. AI may adjust clip start/end, but the event core and post-event tail must remain covered.

Additional product requirements:

- Strong model is required for run-level improvement and hard recovery decisions.
- Smaller models may be used only for dry-run, labeling, or low-risk advisory work.
- Review-only output cannot mutate final track, video, or highlight deliverables.
- Every applied change must trace: baseline artifact -> packet/visual evidence -> AI suggestion -> explicit `approval_id` -> candidate artifact -> comparison -> quality gate -> final manifest.

## Current State

| Lane | Current state | Remaining gap |
| --- | --- | --- |
| Missing ball | Candidate executor and comparison exist; stable workflow integration has been built in the current managed-PR series. | Finish review/merge, then prove long-gap coverage and failed-candidate visibility on real video. |
| Noise cleanup | Candidate comparison exists and current branch is hardening bounded provenance. | Finish PR3, then connect promotion/finalization so good cleanup can become final output. |
| Camera motion audit | `camera_motion_audit.py` is landed and `follow_cam_report.json` references the audit. | Build follow-cam candidate executor/comparison and block rerenders when tracking must be fixed first. |
| Highlights | Basic render/copy flow exists; deterministic event windows already live in `python_backend/football_tracking/events.py`. | Add comparison-backed AI highlight candidate lane and publish gate without duplicating event-window ownership. |
| Finalization | Candidate lifecycle, registry, comparison, and quality gate exist. | Add explicit idempotent backend/API/UI promote/reject controls across lanes. |
| Prompt/model policy | Contract exists. | Harden prompts so AI produces executable candidates only when evidence satisfies the contract. |
| Real-video proof | Ad hoc real-video runs exist. | Produce repeatable evidence pack, checksums, inspection notes, and docs. |

## Development Program

### PR3: Finish Noise Cleanup Promotion-Ready Verification

**Purpose:** complete the current `feat/noise-promotion-ready-verification` branch so selected noise approvals either write safe bounded candidate evidence or become explicit rejected candidates.

**Files:**

- Modify: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Treat explicit `full_video_sahi` and `full_video_spatial_split` flags as hard failures even when the strategy name starts with `bounded`.
- [ ] Refuse unsafe noise approvals before writing `ai_candidates/noise/<candidate_id>/`.
- [ ] Preserve approval ids and candidate ids in rejected-candidate lifecycle summaries.
- [ ] Ensure stable workflow records selected noise execution failures in `final_ai_improvement_artifact_manifest.json` as rejected/unavailable, not invisible errors.
- [ ] Keep bounded packet/visual evidence as the only executable cleanup path.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_noise_candidate_comparison.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_api_service.py -q`.
- [ ] Run `git diff --check`.
- [ ] Cover a bounded strategy name with `full_video_sahi: true` failing before candidate directory creation.
- [ ] Cover selected noise execution failure appearing in manifest and lifecycle.

**Deliver:**

- Noise cleanup lane is promotion-ready.
- Spatial split/SAHI remains a bounded tool, not a full-video default.

### PR4: Backend Finalization Core for Candidate Promotion

**Purpose:** add a reusable, path-safe promotion/rejection layer so successful candidates can become selected final artifacts only through explicit operator decisions.

**Files:**

- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_api_service.py`

**Build:**

- [ ] Add a reusable backend helper such as `finalize_ai_candidate(...)` that accepts `problem_type`, `candidate_id`, `approval_id`, decision, optional `confirm_warn`, output role, and operator note.
- [ ] Make finalization idempotent: repeating the same decision for the same candidate and approval should not duplicate final manifest entries.
- [ ] Validate all candidate and final-selected paths are relative to, or safely contained by, the run output directory.
- [ ] Prevent automatic promotion after execution. A generated `pass` comparison stays `not_promoted` until an explicit finalization decision is recorded.
- [ ] Record operator decision metadata: decision id or timestamp, `approval_id`, `candidate_id`, `problem_type`, output role, note, and confirmation status.
- [ ] Define replacement semantics per lane/output role:
  - `missing_ball_track`: one promoted track artifact supersedes the baseline track for downstream selection.
  - `noise_cleaned_track`: one promoted cleaned track artifact supersedes the baseline cleaned track for downstream selection.
  - `follow_cam_video`: at most one promoted follow-cam video is selected as the current final follow-cam output.
  - `highlight_clip`: multiple promoted clips are allowed, keyed by event or highlight candidate id.
- [ ] Allow promotion of `pass` missing-ball recovery candidates and `pass` noise cleanup candidates.
- [ ] Allow `warn` candidates only when the finalization request includes explicit human confirmation.
- [ ] Reject `fail`, `unavailable`, unsupported, review-only, missing-comparison, missing-approval, and unknown-candidate cases.
- [ ] Write consumed approvals, comparison refs, final selected artifacts, rejected candidates, warnings, and notes to `final_ai_improvement_artifact_manifest.json`.
- [ ] Keep baseline artifacts immutable; downstream consumers select candidate paths from the manifest.
- [ ] Refresh lifecycle after finalization.

**Tests:**

- [ ] Pass candidate promotes.
- [ ] Warn candidate requires confirmation.
- [ ] Fail/unavailable/review-only candidates cannot promote.
- [ ] Executed `pass` candidate is not final-selected until explicit promotion.
- [ ] Repeating the same promotion is idempotent and does not duplicate final-selected artifacts.
- [ ] Candidate paths outside the output directory are rejected.
- [ ] Replacement semantics allow one current `follow_cam_video` but multiple `highlight_clip` entries.
- [ ] Rejection records reason and lifecycle state.
- [ ] Missing-ball and noise candidate selections are visible to quality gate and lifecycle.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`.

**Deliver:**

- A shared finalization contract that PR5 follow-cam and PR6 highlight candidates can plug into.
- Missing-ball and noise AI improvements can become explicit final output without auto-promoting generated candidates.

### PR5: Follow-Cam AI Candidate Executor and Comparison

**Purpose:** let AI improve shaky follow-cam output by producing an isolated rerender candidate and proving it is smoother without hiding bad tracking.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_executor.py`
- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_camera_motion_audit.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Execute selected `adjust_follow_cam` approvals under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Candidate artifacts must include `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Compare baseline and candidate over the same evaluable window.
- [ ] Compare `review_event_count`, `p95_pan_step_px`, `max_pan_accel_px`, `max_zoom_step_px`, `max_zoom_step_ratio`, p95/max crop height, and Detected/Predicted crop coverage.
- [ ] Fail candidates that reduce shaking only by zooming far out.
- [ ] Fail when candidate p95 crop height is more than 15% above baseline p95 crop height.
- [ ] Fail when candidate max crop height is more than 20% above baseline max crop height.
- [ ] Fail when candidate p95 crop height exceeds a configured source-size guard such as 85% of source video height, unless the baseline already exceeds that guard and the candidate does not worsen it.
- [ ] Fail candidates that lower ball crop coverage beyond tolerance.
- [ ] Treat `tracking_rerun_before_follow_cam` as blocked until a linked missing-ball/noise candidate passes or is explicitly promoted.
- [ ] Require `depends_on_candidate_id` or `linked_recovery_candidate_id` for tracking-first camera actions.
- [ ] Register follow-cam candidates in the candidate registry with comparison refs and promotion status.
- [ ] Update lifecycle, quality gate, and final manifest summaries so follow-cam candidates are visible and promotable.
- [ ] Use the PR4 finalization helper for `follow_cam_video` promotion and rejection.

**Tests:**

- [ ] Smoother candidate passes.
- [ ] Shakier candidate fails.
- [ ] Over-zoomed candidate fails.
- [ ] Source-size over-zoom guard fails when the crop becomes too wide to be a meaningful tracking improvement.
- [ ] Crop coverage regression fails.
- [ ] Sparse data returns warn or unavailable, not pass.
- [ ] Tracking-first camera action blocks without a valid linked recovery candidate.
- [ ] Generated pass follow-cam candidate is not final-selected until explicit promotion.
- [ ] Promoted follow-cam candidate appears in lifecycle, quality gate, and final manifest as `follow_cam_video`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_stable_ai_improvement_workflow.py -q`.

**Deliver:**

- A measurable follow-cam improvement lane.
- Final video stability can be improved by AI candidate rerendering, not just audited after the fact.

### PR6: Highlight AI Candidate Comparison and Publish Gate

**Purpose:** let AI adjust highlight windows while guaranteeing the play and aftermath are not cut off.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify deterministic event/window owner as needed: `python_backend/football_tracking/events.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Execute selected `adjust_highlight_window` and `render_suggested_highlight` approvals under `ai_candidates/highlight/<candidate_id>/`.
- [ ] Candidate artifacts must include `highlight.mp4`, `highlight_report.json`, `highlight_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Reuse the deterministic `core_window`, default pre-buffer, default post-buffer, and `buffer_policy.min_tail_frames` from `events.py`; do not redefine the owner in review packets.
- [ ] Clamp deterministic event windows to the actual source frame count before render.
- [ ] Require AI-adjusted windows to contain the event `core_window`.
- [ ] Require AI-adjusted windows to contain `core_window.end_frame + min_tail_frames`, clamped by source video end.
- [ ] Record source-end clamps as explicit pass/warn evidence.
- [ ] Fail invalid bounds, mismatched event ids, cut core, and cut available tail.
- [ ] Keep legacy visual-review accepted clips labeled as review-only or legacy unless they have comparison-backed candidate evidence.
- [ ] Register highlight candidates in the candidate registry with comparison refs and promotion status.
- [ ] Update lifecycle, quality gate, and final manifest summaries so highlight candidates are visible and promotable.
- [ ] Use the PR4 finalization helper for `highlight_clip` promotion and rejection.

**Tests:**

- [ ] Default pre/post buffer is applied before render.
- [ ] `events.py` remains the deterministic owner of `core_window`, `render_window`, and `buffer_policy.min_tail_frames`.
- [ ] AI window cutting event core fails.
- [ ] AI window cutting available tail fails.
- [ ] End-of-video clamp is recorded using actual source frame count.
- [ ] Comparison-backed highlight can be accepted as final output.
- [ ] Review-only highlight cannot become AI-improved final output.
- [ ] Generated pass highlight candidate is not final-selected until explicit promotion.
- [ ] Promoted highlight candidate appears in lifecycle, quality gate, and final manifest as `highlight_clip`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_highlight_candidate_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py python_backend/tests/test_events.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_stable_ai_improvement_workflow.py -q`.

**Deliver:**

- AI-adjusted highlight clips with safe, explainable frame boundaries.
- Shot/goal aftermath is preserved by default.

### PR7: API and UI Finalization Controls

**Purpose:** make candidate promotion/rejection understandable and controllable from the operator workflow.

**Files:**

- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `lib/api-spec/openapi.yaml`
- Modify generated API clients if schema changes.
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Modify or create: `artifacts/web/src/lib/aiLifecycle.ts`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `artifacts/web/src/lib/aiLifecycle.test.ts`

**Build:**

- [ ] Add API actions to promote or reject a candidate using the backend finalization helper from PR4.
- [ ] Surface clear reasons for blocked promotion: review-only, missing comparison, fail, unavailable, warn without confirmation, unsupported type, or missing approval.
- [ ] Show lifecycle states: approved, pending execution, executed, compared, gated, pending confirmation, promoted, rejected, blocked.
- [ ] Keep deliverable page tied to final manifest selections, not raw AI suggestions.
- [ ] Regenerate OpenAPI and clients when schemas change.

**Tests:**

- [ ] API pass promotion works.
- [ ] API warn promotion requires confirmation.
- [ ] API fail/unavailable/review-only promotion is rejected.
- [ ] API/UI finalization works for `missing_ball_track`, `noise_cleaned_track`, `follow_cam_video`, and `highlight_clip`.
- [ ] UI shows follow-cam and highlight candidates as pending finalization until explicit promotion or rejection.
- [ ] UI lifecycle helper maps all states without implying automatic mutation.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_api_service.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_final_artifact_manifest.py -q`.
- [ ] Run `python_backend/scripts/export_openapi.py --output lib/api-spec/openapi.yaml` if schemas change.
- [ ] Run `corepack pnpm --filter @workspace/api-spec run codegen` if schemas change.
- [ ] Run `corepack pnpm --filter @workspace/web run test:lifecycle`.
- [ ] Run `corepack pnpm run typecheck:libs`.
- [ ] Run `corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck`.

**Deliver:**

- Operators can see what AI proposed, what was executed, what can be promoted, and why something is blocked.
- Final output selection becomes explicit and traceable.

### PR8: Prompt, Model, and Suggestion Contract Hardening

**Purpose:** make model output line up with executable candidate lanes, especially when using stronger models for improvement instead of smaller models for review.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify if needed: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `docs/operations/ai-improvement-contract.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_review_packets.py`

**Build:**

- [ ] Record provider mode, selected model, and candidate intent in workflow reports.
- [ ] Require missing-ball suggestions to cite full-window coverage or uncovered subwindows.
- [ ] Require noise suggestions to be bounded and to include false-positive class.
- [ ] Require camera suggestions to choose tracking repair before follow-cam rerender when ball track is Lost/Predicted.
- [ ] Require highlight suggestions to preserve core window and post-event tail.
- [ ] Downgrade unsupported, broad, or untraceable suggestions to review-only/manual-review.
- [ ] Document strong-model default for run-level improvement and hard recovery decisions.

**Tests:**

- [ ] Untraceable ROI/localization suggestion becomes review-only.
- [ ] Frame `2079` evidence cannot close `2049-2544` without full-window coverage.
- [ ] Lost/Predicted camera context routes to tracking-first action.
- [ ] Highlight tail validation remains enforced.
- [ ] Provider/model fields are visible.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py -q`.

**Deliver:**

- AI suggestions become executor-compatible by construction.
- Stronger model use is visible and justified when output can lead to candidate execution.

### PR9: Real-Video Evidence Pack and Documentation

**Purpose:** prove the complete loop on the real match video and document how to repeat it.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`
- Create: `docs/operations/real-video-ai-improvement-evidence.schema.json`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Optional local-only update after merge: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Run baseline full workflow on `python_backend/data/raw5760x144020fps.mp4` with temporal parallelism.
- [ ] Run AI improvement with the configured strong model and explicit approvals.
- [ ] Execute approved missing-ball, noise, follow-cam, and highlight candidates when safe candidates exist.
- [ ] Produce comparison reports, quality gate, lifecycle, final manifest, final follow-cam video, and highlight clips or explicit no-safe-highlight evidence.
- [ ] Inspect right-bottom gap `2049-2544`, frame `2079`, dense-noise windows, camera-motion windows, and highlight tails.
- [ ] Validate review media/contact sheets against source frames so HEVC seeking or frame extraction artifacts do not mislead AI evidence.
- [ ] Write local `real_video_ai_improvement_evidence.json` with run id, input video path/hash, output directory, provider/model, candidate ids, approval ids, comparison statuses, promoted/rejected decisions, final artifact refs, checksums, manual inspection notes, and no-safe-highlight reason when applicable.
- [ ] Validate evidence JSON against the committed schema.
- [ ] Do not commit large videos or generated MP4 files.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests -q`.
- [ ] Run frontend/typecheck/build commands if API/UI or docs references changed.
- [ ] Confirm final follow-cam includes `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, and `camera_motion_audit.json`.
- [ ] Confirm every executed candidate lane has candidate manifest, comparison, registry entry, quality-gate evidence, final manifest entry, and lifecycle state.
- [ ] Confirm highlight clips preserve core/tail or record explicit rejection/no-safe-highlight evidence.

**Deliver:**

- Repeatable real-video evidence pack.
- Updated README and operation docs.
- Final recommendation stating which follow-cam and highlight artifacts are stable enough to use.

## Managed PR Gate for Every PR

- [ ] Start from latest `origin/main`; merge/rebase latest main before continuing a stale branch.
- [ ] Keep unrelated local changes intact.
- [ ] Use a fresh worker agent for implementation when the PR is not already in progress.
- [ ] Use a separate reviewer agent before commit or PR update.
- [ ] Fix all valid Critical and Important reviewer findings.
- [ ] Run focused tests, then required broader tests.
- [ ] Push branch and open/update GitHub PR.
- [ ] Wait 10-15 minutes for CI, Copilot, and remote comments.
- [ ] Evaluate comments on merit and fix confirmed issues.
- [ ] Merge only after checks and valid comments are resolved.
- [ ] Delete merged remote and local branches.

## End-to-End Acceptance

- [ ] Missing-ball gaps close only through bounded recovery comparison or full-window not-visible evidence.
- [ ] Noise cleanup reduces false positives without damaging sustained true-ball continuity.
- [ ] Follow-cam candidates reduce motion spikes without over-zooming or hiding tracking loss.
- [ ] Highlight candidates preserve event core and required post-event tail.
- [ ] Review-only AI text never becomes final output.
- [ ] Explicit approval ids are required for execution and promotion.
- [ ] Final output is traceable from baseline through AI evidence, approval, candidate, comparison, quality gate, and final manifest.
- [ ] Real-video verification produces a stable follow-cam output and highlight clips or explicit no-safe-highlight evidence.
