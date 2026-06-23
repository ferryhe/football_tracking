# AI Improvement Actionable Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or the repository's managed-PR process to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** turn AI from a reviewer into an approved improvement loop that can find missing balls, reduce false positives, stabilize follow-cam output, and adjust highlight clip windows with comparison-backed final artifacts.

**Architecture:** baseline tracking outputs stay immutable. AI proposes bounded, evidence-backed candidate actions; operators explicitly approve selected action ids; executors write isolated candidate artifacts under `ai_candidates/<problem_type>/<candidate_id>/`; comparison reports, quality gate, lifecycle, and final manifest decide whether output is promoted, rejected, or still pending.

**Tech Stack:** Python backend, FastAPI service, JSON artifacts, pytest, React operator UI, OpenAPI generated clients, existing review packets, visual review, candidate lifecycle, quality gate, final artifact manifest, follow-cam renderer, highlight renderer, and real-video verification against `python_backend/data/raw5760x144020fps.mp4`.

---

## Requirement Summary

This requirement is about **AI improvement**, not **AI audit**.

The four primary user-visible improvement lanes are:

1. **Missing ball:** when the ball is lost, AI should inspect packet or visual evidence, localize a bounded recovery area, execute a targeted rerun candidate, or close the whole window as `not_visible` only when evidence covers that whole window.
2. **Too much noise:** when detections include extra balls, shoes, sideline clutter, heads, boards, or background objects, AI should propose bounded cleanup candidates and keep only candidates that reduce false-positive islands without damaging real-ball continuity.
3. **Shaky follow-cam:** when the final camera path is jumpy, AI should decide whether tracking must be fixed first or whether follow-cam parameters can be safely rerendered. A follow-cam candidate must prove smoother motion without simply zooming far out.
4. **Highlights:** deterministic event candidates should create default pre/post buffers. AI can adjust clip windows, but the core event and required post-event tail must remain covered.

Cross-cutting rules:

- Strong model is required for run-level improvement and hard recovery candidates; smaller models are only for low-risk labeling or dry-run smoke.
- Spatial split/SAHI across the full video is not the stable default because it increases noise. Prefer temporal chunk parallelism for speed and targeted ROI/SAHI only inside approved bounded windows.
- Review-only AI text cannot mutate `ball_track.csv`, `ball_track.cleaned.csv`, follow-cam videos, highlight clips, or final manifests.
- Every applied improvement must trace: baseline artifact -> packet/visual evidence -> AI suggestion -> explicit `approval_id` -> candidate artifacts -> comparison -> quality gate -> final manifest.

## Current Landing Status

| Area | Status | Notes |
| --- | --- | --- |
| AI contract and model policy | Landed | `docs/operations/ai-improvement-contract.md` defines candidate lifecycle, model policy, missing-ball closure, follow-cam thresholds, highlight comparison, and final-output rules. |
| Operator lifecycle visibility | Landed | UI lifecycle helpers and pages distinguish review-only, proposed, approved, pending execution, executed, compared, promoted, rejected, and blocked states. |
| Review packets and visual review | Landed | Existing packet/visual modules provide evidence windows and model-backed review artifacts. |
| Missing-ball candidate execution | In progress | Current branch `feat/missing-ball-stable-executor` adds `missing_ball_candidate_executor.py` and stable-workflow execution for selected `targeted_rerun` / `localize_ball_roi` approvals. |
| Noise cleanup candidate execution | Mostly landed | `noise_candidate_comparison.py` can execute bounded cleanup candidates and compare false-positive reduction vs continuity loss. Remaining work is final promotion clarity and real-video verification. |
| Camera motion audit | Landed | `camera_motion_audit.py` analyzes final `camera_path.csv`; follow-cam writes `camera_motion_audit.json` and references it in `follow_cam_report.json`. |
| Follow-cam AI improvement | Not fully landed | AI can suggest `adjust_follow_cam` or `tracking_rerun_before_follow_cam`, but there is no isolated follow-cam candidate executor/comparison/promotion lane yet. |
| Highlight AI improvement | Partly landed | Basic highlight rendering and accepted-highlight copying exist. AI-adjusted candidate comparison and publish gate are still needed. |
| Promotion/rejection controls | Partly landed | Final manifest and lifecycle policy exist; backend finalization must move earlier so executed candidates can become applied output before UI polish. |
| Real-video repeatable proof | Not landed | Need a stable command sequence, evidence pack, checksums, and manual inspection notes for the real match video. |

## Independent Review Adjustments

An independent reviewer returned **Needs Revision**. The plan was revised as follows:

- Promotion/rejection was split into an early backend finalization PR and a later API/UI polish PR. This prevents missing-ball/noise candidates from stopping at "executed candidate" without any applied final output.
- PR2 was recast as finish/verify for the current in-progress branch, with explicit additions for grouped multiple `candidate_id`s and failed-candidate traceability.
- Follow-cam dependencies now require a concrete `linked_recovery_candidate_id` or `depends_on_candidate_id` before `tracking_rerun_before_follow_cam` can unblock rerendering.
- Highlight publishing now separates legacy visual-review accepted clips from comparison-backed AI-improved highlight output.
- The plan names the deterministic event/highlight window owner instead of assuming `render_highlight_clip(...)` creates buffers.
- Real-video verification now includes a machine-readable evidence-pack schema.
- Frontend validation now names concrete lifecycle helper/build commands rather than only saying "frontend lifecycle tests."

## PR Plan

### PR2: Finish Missing-Ball Stable Executor

**Purpose:** finish and verify the current missing-ball executor branch so selected approvals are executable in the stable workflow, while preserving `not_visible` as a separate evidence-backed resolution.

**Files:**

- Create or finish: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_missing_ball_candidate_executor.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_api_service.py`

**Build:**

- [ ] Verify `execute_missing_ball_candidate(...)` is reusable from both stable workflow and API child-run paths.
- [ ] Verify API child-run behavior remains backward compatible after delegating to the reusable executor.
- [ ] Execute only explicitly selected `approval_id`s from `--approved-actions-path` plus `--approval-ids`.
- [ ] Reject approval-file presence alone.
- [ ] Reject unknown, duplicate, malformed, or missing approval ids before expensive provider or video work.
- [ ] Group selected missing-ball approvals by `candidate_id`; execute each group independently when multiple distinct candidates are selected.
- [ ] If one candidate group fails, record that group as `blocked` or `rejected` with error evidence without pretending the candidate was applied.
- [ ] Write recovery candidate artifacts under `ai_candidates/missing_ball/<candidate_id>/`.
- [ ] Write required candidate artifacts: `ball_track.csv`, `ball_track.cleaned.csv`, `ball_audit.json`, `metrics_report.json`, `run_manifest.json`, `candidate_manifest.json`, and `missing_ball_recovery_comparison.json`.
- [ ] Keep `not_visible` as `missing_ball_resolution.json` only; do not route it through recovery execution.
- [ ] Reject broad full-video `localize_ball_roi` / SAHI scopes.
- [ ] Allow targeted ROI/SAHI only when bounded by frame window and packet/visual evidence.
- [ ] Preserve baseline track hashes.
- [ ] Update registry, quality gate, final manifest, and lifecycle after execution, failure, or resolved-noop closure.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_missing_ball_candidate_executor.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_missing_ball_recovery_comparison.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py -q`.
- [ ] Cover selected recovery approval executes end to end.
- [ ] Cover two selected missing-ball `candidate_id`s execute as separate candidate lanes.
- [ ] Cover one failed selected candidate records blocked/rejected lifecycle evidence while another valid selected candidate can still be compared.
- [ ] Cover approval artifact presence alone does not execute.
- [ ] Cover unknown and duplicate ids fail before output.
- [ ] Cover `2049-2544` cannot be closed by only a short `2079` neighborhood.
- [ ] Cover full-window `not_visible` requires packet or visual evidence over the whole window.
- [ ] Cover baseline hashes do not change.
- [ ] Cover candidate registry, quality gate, final manifest, and lifecycle include the candidate or resolution.

**Deliver:**

- Stable workflow can produce missing-ball candidate outputs without manual API follow-up.
- Long right-bottom gaps are either recovered by a bounded candidate or explicitly unresolved/resolved-not-visible with full-window evidence.
- Failed recovery attempts are visible as failed candidates, not invisible workflow errors.

### PR3: Noise Cleanup Promotion-Ready Verification

**Purpose:** verify the mostly-landed noise lane is promotion-ready and can safely remove false positives without harming real-ball signal.

**Files:**

- Modify if needed: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`

**Build:**

- [ ] Verify current noise executor already writes isolated candidate artifacts and comparison.
- [ ] Ensure stable workflow executes only selected `noise_filter_adjustment`, `tighten_noise_filter`, or `reject_noise` approval ids.
- [ ] Ensure cleanup refuses unbounded full-video SAHI provenance.
- [ ] Ensure comparison fails when sustained true-ball coverage drops or lost-frame count worsens beyond tolerance.
- [ ] Ensure final manifest records pass, warn, fail, unavailable, rejected, and pending-confirmation states for noise candidates once backend finalization is available.
- [ ] Ensure lifecycle exposes noise candidates as executed/compared/gated/finalized, not only approved.
- [ ] Add focused tests only for missing lifecycle/final-manifest gaps found during inspection.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_noise_candidate_comparison.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_candidate_lifecycle.py -q`.
- [ ] Cover removal of short false-positive islands passes or warns according to threshold.
- [ ] Cover useful signal loss fails.
- [ ] Cover full-video/noisy provenance fails.
- [ ] Cover selected noise approvals update registry, quality gate, lifecycle, and final manifest.

**Deliver:**

- Noise lane produces promotion-ready candidate evidence for backend finalization.
- Full-video spatial splitting remains documented as a speed/noise tradeoff, not the default improvement path.

### PR4: Backend Candidate Finalization For Missing-Ball And Noise

**Purpose:** move applied-output finalization early enough that missing-ball and noise candidates can become real final outputs before follow-cam/highlight work continues.

**Files:**

- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py` only for backend helper reuse; defer UI polish to PR7.
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Add backend helper to promote or reject a candidate by `approval_id`, `candidate_id`, and `problem_type`.
- [ ] Support missing-ball recovery candidates, evidence-backed `not_visible` resolution, and noise cleanup candidates.
- [ ] Reject promotion when candidate is review-only, comparison is missing/unavailable/fail, candidate type is unsupported, or approval id is missing.
- [ ] Require explicit human confirmation before promoting `warn` candidates.
- [ ] Write final selections, rejections, blocked candidates, consumed approvals, comparison report refs, and operator notes to `final_ai_improvement_artifact_manifest.json`.
- [ ] Let stable workflow optionally consume explicit promotion/rejection decisions after candidate execution, without auto-promoting every pass candidate.
- [ ] Preserve baseline artifacts unless a final manifest selection explicitly points downstream consumers to a candidate artifact.
- [ ] Refresh lifecycle after finalization.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py -q`.
- [ ] Cover pass candidate can be promoted with explicit approval.
- [ ] Cover warn candidate requires explicit confirmation.
- [ ] Cover fail/unavailable/review-only candidates cannot be promoted.
- [ ] Cover rejection writes reason and updates lifecycle.
- [ ] Cover missing-ball/noise final manifest selections are visible to lifecycle and quality gate.

**Deliver:**

- Missing-ball and noise candidates can become applied final output through explicit backend finalization.
- AI improvement no longer stops at candidate generation for the first two lanes.

### PR5: Follow-Cam Candidate Rerender And Comparison

**Purpose:** let AI improve shaky final videos by producing a comparable follow-cam candidate.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Create or modify: `python_backend/football_tracking/follow_cam_candidate_executor.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_camera_motion_audit.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Execute selected `adjust_follow_cam` approvals under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Require `tracking_rerun_before_follow_cam` actions to carry `linked_recovery_candidate_id` or `depends_on_candidate_id`.
- [ ] Preserve `tracking_rerun_before_follow_cam` as blocked/pending until the linked missing-ball/noise recovery comparison passes or is explicitly promoted.
- [ ] Fail or block rerender when the dependency id is missing, unknown, failed, unavailable, or still pending confirmation.
- [ ] Candidate artifacts must include `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Compare baseline vs candidate over the same evaluable window.
- [ ] Compare `review_event_count`, `p95_pan_step_px`, `max_pan_accel_px`, `max_zoom_step_px`, `max_zoom_step_ratio`, mean/p95/max crop height, and Detected/Predicted crop coverage.
- [ ] Fail if motion events worsen.
- [ ] Fail if p95 pan step or max pan acceleration do not improve by at least 10%, unless both baseline and candidate are already below warn thresholds.
- [ ] Fail if the candidate simply zooms out: p95 crop height grows more than 15% or max crop height grows more than 20%.
- [ ] Fail if Detected/Predicted crop coverage drops below `baseline_coverage - 0.02` or below `0.95` when enough data exists.
- [ ] Warn, not pass, when data is too sparse to prove coverage.
- [ ] Update registry, quality gate, final manifest, and lifecycle.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`.
- [ ] Cover smoother candidate passes.
- [ ] Cover shakier candidate fails.
- [ ] Cover over-zoomed candidate fails.
- [ ] Cover crop coverage regression fails.
- [ ] Cover sparse coverage warns or is unavailable.
- [ ] Cover tracking-first action does not rerender follow-cam until tracking recovery passes.
- [ ] Cover missing/fail/warn dependency states block or require confirmation before rerender.
- [ ] Cover baseline ball tracks are not mutated.

**Deliver:**

- A candidate follow-cam video that is measurably smoother.
- A comparison report proving the video did not hide bad tracking by zooming out.

### PR6: Highlight Candidate Comparison And Publish Gate

**Purpose:** let AI adjust highlight windows while preserving the event core and aftermath.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify as deterministic window owner: `python_backend/football_tracking/events.py`
- Modify as deterministic window owner if needed: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Execute selected `adjust_highlight_window` and `render_suggested_highlight` approvals under `ai_candidates/highlight/<candidate_id>/`.
- [ ] Candidate artifacts must include `highlight.mp4`, `highlight_report.json`, `highlight_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Define the deterministic owner of `core_window`, default pre-buffer, post-buffer, and `buffer_policy.min_tail_frames` in event candidate or review-packet generation, not in the low-level clip renderer.
- [ ] Default deterministic highlight window should include configured pre/post buffer before it reaches `render_highlight_clip(...)`.
- [ ] AI-adjusted render window must contain the event `core_window`.
- [ ] AI-adjusted render window must contain required post-event tail through `core_window.end_frame + buffer_policy.min_tail_frames`, clamped by actual source-video end.
- [ ] Source-video-end clamp must be recorded as explicit pass/warn evidence.
- [ ] Candidate fails if it cuts the event core, cuts available post-event tail, uses invalid frame bounds, or mismatches event/candidate ids.
- [ ] Preserve existing visual-review accepted clip copying as a legacy/review-only output.
- [ ] Add a separate manifest-backed AI-improved highlight acceptance path, or explicitly label legacy accepted clips so they cannot be mistaken for comparison-backed final AI improvements.
- [ ] Update registry, quality gate, final manifest, and lifecycle.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_highlight_candidate_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py -q`.
- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`.
- [ ] Cover default pre/post buffer.
- [ ] Cover the deterministic event/review-packet layer owns default buffer fields.
- [ ] Cover AI window cutting core fails.
- [ ] Cover AI window cutting available tail fails.
- [ ] Cover end-of-video tail clamp is recorded.
- [ ] Cover comparison-backed highlight can be accepted as AI-improved final output.
- [ ] Cover legacy visual-review accepted clips are still copyable but labeled as legacy/review-only.
- [ ] Cover review-only highlight cannot be accepted as final AI improvement.

**Deliver:**

- AI-adjusted highlight clips with safe frame boundaries.
- Final highlight output preserves shot/goal result and enough aftermath.

### PR7: API/UI Finalization Polish

**Purpose:** expose the backend finalization controls clearly in API and UI across missing-ball, noise, follow-cam, and highlight lanes.

**Files:**

- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `lib/api-spec/openapi.yaml`
- Modify generated clients if schema changes.
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Modify or create: `artifacts/web/src/lib/aiLifecycle.ts`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `artifacts/web/src/lib/aiLifecycle.test.ts`
- Test: `artifacts/web/src/pages/ai-analysis.tsx` behavior through existing build/typecheck coverage.
- Test: `artifacts/web/src/pages/deliverable.tsx` behavior through existing build/typecheck coverage.

**Build:**

- [ ] Wrap the backend finalization helper from PR4 with API actions to promote a candidate with `approval_id`, `candidate_id`, `problem_type`, optional `confirm_warn`, and operator note.
- [ ] Wrap the backend finalization helper from PR4 with API actions to reject a candidate with `approval_id`, `candidate_id`, `problem_type`, and rejection reason.
- [ ] Surface backend rejection reasons when candidate is review-only, comparison is missing/unavailable/fail, warn lacks confirmation, candidate type is unsupported, or approval id is missing.
- [ ] Record API/UI decisions in `final_ai_improvement_artifact_manifest.json` through the backend helper.
- [ ] Refresh lifecycle after decisions.
- [ ] Update UI so promoted/rejected/pending-confirmation states are visible and do not imply automatic mutation.
- [ ] Keep deliverable page tied to final manifest selections, not raw AI suggestions.
- [ ] Regenerate OpenAPI and client types if schema changes.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_api_service.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_final_artifact_manifest.py -q`.
- [ ] Run `python_backend/scripts/export_openapi.py --output lib/api-spec/openapi.yaml` if API schema changes.
- [ ] Run `corepack pnpm --filter @workspace/api-spec run codegen` if API schema changes.
- [ ] Run `corepack pnpm --filter @workspace/web run test:lifecycle`.
- [ ] Run `corepack pnpm run typecheck:libs`.
- [ ] Run `corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck`.
- [ ] Run `$env:PORT='3000'; $env:BASE_PATH='/'; corepack pnpm --filter @workspace/web run build`.
- [ ] Cover pass promotes.
- [ ] Cover warn requires explicit confirmation.
- [ ] Cover fail/unavailable/review-only cannot promote.
- [ ] Cover rejection writes reason and updates lifecycle.

**Deliver:**

- Human-safe finalization controls.
- Final outputs are explicitly selected, traceable, and reversible by new candidate decisions.

### PR8: Prompt/Model Hardening For Executable Improvements

**Purpose:** make AI suggestions match the executors that now exist.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify if needed: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_review_packets.py`

**Build:**

- [ ] Keep strong model as default for run-level improvement and candidate-producing decisions.
- [ ] Record provider mode and model selection in workflow reports.
- [ ] Make prompt wording explicit that missing-ball suggestions need full-window coverage or uncovered-subwindow notes.
- [ ] Make prompt wording explicit that noise suggestions must be bounded and classify likely false-positive type.
- [ ] Make prompt wording explicit that camera suggestions must choose tracking recovery before follow-cam adjustment when tracking is Lost/Predicted.
- [ ] Make prompt wording explicit that highlight adjustments must preserve core window and post-event tail.
- [ ] Downgrade untraceable or unsupported suggestions to review-only/manual-review, not executable candidates.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py -q`.
- [ ] Cover untraceable ROI/localization suggestions become review-only.
- [ ] Cover frame `2079` cannot close full `2049-2544` without full-window evidence.
- [ ] Cover camera Lost/Predicted context routes to `tracking_rerun_before_follow_cam`.
- [ ] Cover highlight tail validation remains enforced.
- [ ] Cover provider/model fields are visible for real candidate-producing runs.

**Deliver:**

- AI suggestions line up with the newly executable candidate lanes.
- Smaller model output remains safe because it cannot silently become final output.

### PR9: Real-Video Verification Pack And Documentation

**Purpose:** prove the whole loop on the real match video and make the workflow repeatable.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`
- Create: `docs/operations/real-video-ai-improvement-evidence.schema.json`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Optional local-only skill update after merge: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Run baseline full workflow on `python_backend/data/raw5760x144020fps.mp4` with temporal parallelism.
- [ ] Run AI improvement workflow with configured strong model and explicit model recorded.
- [ ] Use targeted ROI/SAHI only for approved bounded recovery windows.
- [ ] Execute approved candidates for missing-ball, noise, follow-cam, and highlight lanes when safe candidates exist.
- [ ] Run comparison, quality gate, lifecycle, and final manifest.
- [ ] Produce final follow-cam video.
- [ ] Produce highlight clips when safe event candidates exist.
- [ ] If no safe highlight exists, record no-safe-highlight reason and evidence.
- [ ] Manually inspect right-bottom gap `2049-2544`, frame `2079`, dense-noise windows, camera-motion windows, and highlight tails.
- [ ] Record output paths, report paths, candidate ids, approval ids, quality-gate status, final manifest summary, and SHA256 checksums.
- [ ] Write a machine-readable `real_video_ai_improvement_evidence.json` in the local output folder and validate it against `docs/operations/real-video-ai-improvement-evidence.schema.json`.
- [ ] Evidence JSON fields must include run id, input video path/hash when available, output directory, model/provider mode, candidate ids, approval ids, comparison statuses, promoted decisions, rejected decisions, final artifact refs, checksums, manual inspection notes, and no-safe-highlight reason when applicable.
- [ ] Do not commit large source videos or generated MP4s unless repo policy changes.

**Tests:**

- [ ] Run `$env:PYTHONPATH='python_backend'; pytest python_backend/tests -q`.
- [ ] Run frontend/typecheck/build commands if docs or API/UI changes require them.
- [ ] Confirm `stable_ai_improvement_workflow_report.json` includes provider mode, model, strategy, approval selection, stages, warnings, and artifacts.
- [ ] Confirm each executed candidate lane has candidate manifest, comparison, registry entry, quality gate evidence, final manifest entry, and lifecycle state.
- [ ] Confirm final follow-cam has `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, and `camera_motion_audit.json`.
- [ ] Confirm final highlight clips preserve core/tail or have explicit rejection/no-safe-event reason.
- [ ] Confirm `real_video_ai_improvement_evidence.json` validates against the committed schema.

**Deliver:**

- Reproducible real-video evidence pack.
- Updated README and operation docs.
- Final stable output recommendation: which artifacts to use, which candidates were promoted/rejected, and what still requires human review.
- Local skill update note after PR merge.

## Managed PR Gates

Each PR must follow this gate:

- [ ] Merge latest `origin/main` before starting or continuing the branch.
- [ ] Use a fresh branch unless the PR is already in progress.
- [ ] Use a worker agent for implementation.
- [ ] Use a separate reviewer agent before commit or before PR update.
- [ ] Run focused tests first, then broader tests.
- [ ] Push and open/update GitHub PR.
- [ ] Wait for CI and Copilot comments.
- [ ] Fix valid comments.
- [ ] Merge only after checks and valid review comments are resolved.
- [ ] Delete merged remote and local branches.

## End-To-End Acceptance

- [ ] Missing-ball windows close only through bounded recovery comparison or evidence-backed full-window `not_visible`.
- [ ] Noise cleanup reduces false positives without damaging sustained real-ball continuity.
- [ ] Follow-cam candidates reduce motion spikes without hiding tracking loss or over-zooming.
- [ ] Highlight candidates preserve core event and required post-event tail.
- [ ] Review-only AI text never becomes applied output.
- [ ] Explicit approval ids are required for execution and promotion.
- [ ] Final output is traceable from baseline through AI evidence, approval, candidate, comparison, quality gate, and final manifest.
- [ ] Real-video verification produces stable follow-cam output and highlights or explicit no-safe-highlight evidence.
