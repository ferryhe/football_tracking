# AI Improvement Closed Loop V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn AI from a passive reviewer into a controlled improvement operator for missing-ball recovery, false-positive noise cleanup, follow-cam stabilization, and highlight boundary tuning.

**Architecture:** Preserve deterministic tracking artifacts as source of truth. AI may propose bounded, evidence-backed candidates, but only explicit operator approval can execute them; every candidate must be compared, quality-gated, and promoted through `final_ai_improvement_artifact_manifest.json`.

**Tech Stack:** Python backend, FastAPI service layer, existing React/Vite UI, OpenAPI-generated clients, pytest, Vitest/typecheck, current artifact-first tracking pipeline.

---

## Worker Guardrails

- Do not reimplement existing missing-ball, noise, registry, quality-gate, or final-manifest modules. Verify current behavior first, then extend only the missing links.
- Keep edits scoped to the PR lane being implemented. Follow-cam and highlight candidate work may reuse existing API/service helpers, but should not fork a second media-rendering system.
- Treat AI output as advisory until explicit approval, candidate execution, comparison, quality gate, and final manifest promotion all agree.
- Keep temporal chunking as the preferred full-video speed strategy. Use spatial split, SAHI, or ROI only in bounded approved recovery windows.

## Requirement Summary

The product requirement is "AI improvement", not just "AI review".

Four user-visible AI improvement lanes:

1. **Ball missing:** when the ball disappears, especially in long gaps or corner areas, AI should help localize the ball or prove it is not visible. A single local frame cannot close a long gap.
2. **Too much noise:** when spatial split or high-recall detection creates many false balls, AI should classify and remove bounded false-positive windows without broad full-video SAHI.
3. **Shaky follow-cam:** when final video movement is too abrupt, AI should suggest follow-cam tuning or first require tracking rerun when the camera issue is caused by bad ball tracking.
4. **Highlights:** start from default pre/post buffers, then let AI adjust clip boundaries while preserving the event core and enough post-shot/post-goal tail.

Additional requirements:

- Prefer temporal segment parallelism for speed. Avoid broad spatial split as a default because it increases noise; use spatial/SAHI only inside approved bounded recovery windows.
- Use a strong model for executable improvement suggestions. Mini models are acceptable for low-risk labels, dry-run smoke checks, and review-only summaries, not for candidate-producing recovery decisions.
- Do not auto-mutate outputs because a JSON approval file exists. Execution and promotion must be explicit.
- Produce stable final deliverables: tracking video, follow-cam video, highlight clips, comparison reports, and a final manifest that explains which artifacts are trusted.

## Current Landing Status

Already largely landed:

- Ball track audit: `python_backend/football_tracking/ball_audit.py`
- AI review triggers: `python_backend/football_tracking/ai_review_triggers.py`
- Review packets: `python_backend/football_tracking/review_packets.py`
- Visual review wrapper: `python_backend/football_tracking/ai_visual_review.py`
- AI improvement report and prompt contract: `python_backend/football_tracking/ai_improvement.py`, `python_backend/football_tracking/ai_improvement_prompt_contract.py`
- Candidate registry/lifecycle/comparison gate: `ai_candidate_registry.py`, `ai_candidate_lifecycle.py`, `ai_candidate_comparison.py`, `ai_improvement_quality_gate.py`
- Missing-ball candidate executor/comparison: `missing_ball_candidate_executor.py`, `missing_ball_recovery_comparison.py`
- Noise cleanup candidate/comparison: `noise_candidate_comparison.py`
- Camera motion audit: `camera_motion_audit.py`
- Basic highlight render: `highlights.py`
- Stable workflow scaffold: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Final manifest scaffold: `final_artifact_manifest.py`

More precisely: selected missing-ball and noise approvals already have execution/comparison paths in the stable workflow. The next work for those lanes is hardening, regression coverage, and real-video acceptance, not a fresh executor design.

Known gaps:

- Follow-cam AI actions are still skipped as unsupported in the stable workflow.
- Highlight AI actions are still skipped as unsupported in the stable workflow.
- Existing API child render paths for follow-cam/highlight are useful, but not yet fully candidate-aware.
- The UI can show reports and render deliverables, but it does not yet provide a full approve -> execute -> compare -> promote workflow.
- Real-video acceptance is not yet a repeatable one-command evidence pack.

## PR 1: Lock Prompt Contract And Model Policy

**Purpose:** Ensure AI output is strict enough to become executable only when evidence, frame windows, action type, and comparison criteria are present.

**Main work:**

- [ ] Finish/verify `ai_improvement_prompt_contract.py` as the single source for public executable actions.
- [ ] Ensure action/problem-type mismatches become review-only and cannot be approved.
- [ ] Keep `localize_ball_roi` as bounded-window-only, never full-video SAHI.
- [ ] Require strong-model policy for executable missing-ball, camera, and highlight decisions.
- [ ] Preserve legacy `targeted_rerun` only as normalized input provenance; public output should use `rerun_ball_window`.

**Tests:**

- `python_backend/tests/test_ai_improvement_prompt_contract.py`
- `python_backend/tests/test_ai_improvement.py`
- `python_backend/tests/test_ai_visual_review.py`

**Deliver:**

- `ai_improvement_report.json` has clear review-only vs executable candidate semantics.
- `docs/operations/ai-improvement-contract.md` documents the closed action set and model policy.

## PR 2: Harden And Regress Missing-Ball And Noise Lanes

**Purpose:** Tighten already-present missing-ball/noise execution paths against real failure modes. This is a hardening and regression PR, not a new executor rewrite.

**Main work:**

- [ ] Verify the current stable workflow executes selected missing-ball approvals through `execute_missing_ball_candidate`.
- [ ] Verify the current stable workflow executes selected noise approvals through `execute_noise_cleanup_candidate`.
- [ ] Make long lost gaps generate start/middle/end/tail review packet coverage.
- [ ] Treat frame `2079` evidence as partial evidence for a larger `2049-2544` gap unless the whole gap is covered.
- [ ] Add explicit uncovered-subwindow reporting when review evidence does not cover the full gap.
- [ ] Require missing-ball candidate comparison to cover the approved window with either recovered ball points or not-visible evidence.
- [ ] Require noise candidate comparison to preserve continuous valid ball track while removing bounded false-positive islands.
- [ ] Record whether each recovery used temporal parallelism or bounded ROI/SAHI.

**Tests:**

- `python_backend/tests/test_review_packets.py`
- `python_backend/tests/test_missing_ball_candidate_executor.py`
- `python_backend/tests/test_missing_ball_recovery_comparison.py`
- `python_backend/tests/test_noise_candidate_comparison.py`
- `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Deliver:**

- `review_packets.json` with long-gap coverage metadata.
- `missing_ball_recovery_comparison.json` that fails partial long-gap closure.
- `noise_candidate_comparison.json` that rejects cleanup which deletes true ball frames.
- Stable workflow evidence that selected missing-ball/noise candidates still write registry, comparison, quality-gate, and pending-finalization manifest entries.

## PR 3: Follow-Cam Candidate Execution And Comparison

**Purpose:** Make camera-motion audit actionable, so AI can improve shaky final video rather than only flag it.

**Main work:**

- [ ] Add `python_backend/football_tracking/follow_cam_candidate_executor.py`.
- [ ] Add `python_backend/football_tracking/follow_cam_candidate_comparison.py`.
- [ ] Execute approved `adjust_follow_cam` actions under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Reuse existing follow-cam render logic from the API/service path instead of duplicating media writing.
- [ ] Block `tracking_rerun_before_follow_cam` until linked missing-ball/noise tracking recovery evidence passes.
- [ ] When linked tracking recovery passes, rerender follow-cam from the improved candidate track, then rerun `camera_motion_audit.json` and compare camera motion again.
- [ ] Compare baseline and candidate using `camera_motion_audit.json`, `camera_path.csv`, and ball crop coverage.
- [ ] Reject candidates that look smoother only because they zoom out too much or hide the ball.
- [ ] Register candidate outputs and keep them pending until explicit finalization.

**Tests:**

- `python_backend/tests/test_follow_cam_candidate_executor.py`
- `python_backend/tests/test_follow_cam_candidate_comparison.py`
- `python_backend/tests/test_follow_cam.py`
- `python_backend/tests/test_camera_motion_audit.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- `python_backend/tests/test_final_artifact_manifest.py`

Required cases:

- Smooth candidate compared to shaky baseline passes.
- Candidate with larger zoom-out but worse context/coverage fails.
- Candidate with ball crop coverage below threshold fails.
- Sparse or unavailable `camera_path.csv` produces `warn` or `unavailable`, not pass.
- Lost/Predicted tracking near a camera event routes to `tracking_rerun_before_follow_cam`.
- Stable Detected tracking with abrupt camera movement routes to `adjust_follow_cam`.
- Max acceleration, zoom step, and p95 pan step thresholds are enforced.
- Follow-cam can be rerendered from a passing linked missing-ball/noise candidate track.

**Deliver:**

- `ai_candidates/follow_cam/<candidate_id>/follow_cam.mp4`
- `ai_candidates/follow_cam/<candidate_id>/camera_path.csv`
- `ai_candidates/follow_cam/<candidate_id>/follow_cam_report.json`
- `ai_candidates/follow_cam/<candidate_id>/camera_motion_audit.json`
- `ai_candidates/follow_cam/<candidate_id>/follow_cam_candidate_comparison.json`
- `ai_candidates/follow_cam/<candidate_id>/candidate_manifest.json`
- `ai_candidate_registry.json` entry for the follow-cam candidate.
- Candidate lifecycle and final manifest support for `follow_cam_video`.

## PR 4: Highlight Candidate Execution And Tail-Safe Comparison

**Purpose:** Make AI-adjusted highlights reliable, especially avoiding clips that stop before the shot aftermath is visible.

**Main work:**

- [ ] Add `python_backend/football_tracking/highlight_window_validation.py`.
- [ ] Add `python_backend/football_tracking/highlight_candidate_executor.py`.
- [ ] Add `python_backend/football_tracking/highlight_candidate_comparison.py`.
- [ ] Share highlight window validation between API child renders and AI candidate execution.
- [ ] Execute approved `adjust_highlight_window` and `render_suggested_highlight` actions under `ai_candidates/highlight/<candidate_id>/`.
- [ ] Preserve event `core_window`.
- [ ] Preserve required post-event tail unless the source video end clamps it.
- [ ] Record baseline default render window, default pre/post buffer, AI suggested window, pre/post frame deltas, source-end clamp, final frame count, and final duration.
- [ ] Fail candidates with missing event id, invalid window, cut core event, cut available tail, or frame-count mismatch.
- [ ] Support multiple promoted highlight clips in the final manifest.

**Tests:**

- `python_backend/tests/test_highlight_candidate_executor.py`
- `python_backend/tests/test_highlight_candidate_comparison.py`
- `python_backend/tests/test_highlights.py`
- `python_backend/tests/test_accepted_highlights.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- `python_backend/tests/test_final_artifact_manifest.py`

**Deliver:**

- `ai_candidates/highlight/<candidate_id>/highlight.mp4`
- `ai_candidates/highlight/<candidate_id>/highlight_report.json`
- `ai_candidates/highlight/<candidate_id>/highlight_window_validation.json`
- `ai_candidates/highlight/<candidate_id>/highlight_candidate_comparison.json`
- `ai_candidates/highlight/<candidate_id>/candidate_manifest.json`
- Comparison fields for `event_candidate_id`, `core_window`, `baseline_render_window`, `suggested_window`, `render_window`, `tail_status`, `source_end_clamp`, `frame_count`, and `duration_seconds`.
- Final manifest support for promoted `highlight_clip` artifacts.

## PR 5: Stable Workflow Orchestration

**Purpose:** Connect all four lanes into one repeatable approve -> execute -> compare -> gate -> manifest flow.

**Main work:**

- [ ] Extend `run_stable_ai_improvement_workflow.py` so selected follow-cam and highlight approvals execute instead of being skipped.
- [ ] Keep approval-file presence non-mutating unless explicit ids are supplied.
- [ ] Write one workflow report that records selected approval ids, consumed approval ids, candidate outputs, comparison reports, quality gate, and finalization requirements.
- [ ] Ensure `ai_improvement_quality_gate.py` fails real mode when selected candidate comparison is missing or unavailable.
- [ ] Verify existing finalization roles for `missing_ball_track`, `noise_cleaned_track`, `follow_cam_video`, and `highlight_clip` consume the new follow-cam/highlight candidate outputs correctly.
- [ ] Keep passing follow-cam/highlight candidates pending until an explicit finalization action promotes them.

**Tests:**

- `python_backend/tests/test_stable_ai_improvement_workflow.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- `python_backend/tests/test_final_artifact_manifest.py`
- `python_backend/tests/test_ai_candidate_lifecycle.py`
- `python_backend/tests/test_ai_candidate_comparison.py`

**Deliver:**

- `stable_ai_improvement_workflow_report.json`
- `ai_candidate_registry.json`
- `ai_improvement_quality_gate.json`
- `final_ai_improvement_artifact_manifest.json`

## PR 6: API Operator Workflow

**Purpose:** Expose candidate execution, comparison, lifecycle, and finalization through stable HTTP contracts before UI work starts.

**Main work:**

- [ ] Add/extend API methods for candidate execution, candidate lifecycle, comparison summaries, and finalization.
- [ ] Add route tests for explicit approval selection, unsupported action rejection, candidate execution summaries, and finalization.
- [ ] Update OpenAPI and generated TypeScript clients.
- [ ] Ensure API responses include candidate ids, approval ids, problem type, execution status, comparison status, quality-gate status, and finalization status.
- [ ] Keep media rendering through existing child-run status/history/cancel paths where async behavior is needed.

**Tests:**

- `python_backend/tests/test_api_service.py`
- API route tests if present or newly added.
- OpenAPI export/generation checks.
- Generated client typecheck.

**Deliver:**

- Candidate execution/finalization endpoints.
- Updated OpenAPI and generated clients.
- API-visible candidate lifecycle for all four lanes.

## PR 7: UI Operator Workflow

**Purpose:** Let an operator run the improvement loop without manually editing JSON files.

**Main work:**

- [ ] Update AI Analysis / Deliverable UI to show:
  - review-only vs executable suggestions
  - approval selection
  - execution status
  - comparison status
  - quality gate status
  - promote/reject controls
  - final selected videos/clips
- [ ] Keep UI copy clear that AI does not mutate final output without explicit approval and comparison.

**Tests:**

- `artifacts/web/src/lib/aiLifecycle.test.ts`
- web typecheck/Vitest commands used by the repo.

**Deliver:**

- Operator-visible AI candidate lifecycle.
- Approve -> execute -> compare -> promote/reject controls in the app.
- Final selected videos/clips are visible without reading JSON manually.

## PR 8: Real-Video Acceptance And Documentation

**Purpose:** Prove the new loop on a real match video and leave a repeatable recipe.

**Main work:**

- [ ] Add `python_backend/scripts/run_real_ai_improvement_acceptance.py`.
- [ ] Runner inputs:
  - `--output-dir`
  - `--input-video`
  - `--approved-actions-path`
  - `--approval-ids`
  - `--approved-action-id`
  - `--model`
  - `--mode real`
- [ ] Runner output:
  - review packet summary
  - visual review summary
  - model/provider metadata
  - selected approvals
  - candidate execution counts
  - comparison status by lane
  - final manifest summary
  - follow-cam path
  - highlight paths
- [ ] Update `README.md`, `python_backend/README.md`, and operation docs.
- [ ] Document temporal chunking as default speed strategy and bounded ROI/SAHI as exception path.
- [ ] Document strong-model recommendation for executable AI improvement.
- [ ] Write evidence pack into `real_ai_improvement_acceptance/` under the selected output directory, including workflow report, final manifest, review packet summary, top problem windows, and final media references.

**Tests:**

- `python_backend/tests/test_real_ai_improvement_acceptance.py`
- focused doc/assertion tests if present.
- manual real-video acceptance run recorded in PR notes.

**Deliver:**

- `real_ai_improvement_acceptance_report.json`
- `real_ai_improvement_acceptance/stable_ai_improvement_workflow_report.json`
- `real_ai_improvement_acceptance/final_ai_improvement_artifact_manifest.json`
- `real_ai_improvement_acceptance/review_packet_summary.json`
- `real_ai_improvement_acceptance/top_problem_windows.json`
- README/workflow docs with one repeatable command.
- Real-video evidence pack showing final tracking/follow-cam/highlight status.

## Cross-PR Test Gate

Run after every PR:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_candidate_registry.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py -q
```

Run before merging the full series:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py python_backend/tests/test_missing_ball_candidate_executor.py python_backend/tests/test_missing_ball_recovery_comparison.py python_backend/tests/test_noise_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_follow_cam_candidate_executor.py python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py python_backend/tests/test_highlight_candidate_executor.py python_backend/tests/test_highlight_candidate_comparison.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_api_service.py python_backend/tests/test_real_ai_improvement_acceptance.py -q
```

## Managed PR Rules

- Start each PR from latest `origin/main`.
- Use a fresh branch per PR.
- Use a worker agent for implementation.
- Use a separate spec reviewer and code-quality reviewer before publishing.
- Fix all valid Critical/Important findings.
- Run focused tests before opening each PR.
- After opening each PR, wait for CI and remote comments, evaluate Copilot comments on merit, fix confirmed issues, then merge.
- Delete merged local and remote branches only after merge is confirmed.

## Done Definition

The series is complete only when a real run can produce and explain:

- baseline tracking artifacts
- AI review packets and visual review where enabled
- `ai_improvement_report.json`
- explicit approved actions
- candidate artifacts for selected lanes
- comparison reports for selected lanes
- `ai_improvement_quality_gate.json`
- `final_ai_improvement_artifact_manifest.json`
- final trusted follow-cam video
- final trusted highlight clips
- `real_ai_improvement_acceptance_report.json`
