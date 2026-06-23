# AI Improvement Executable Closed Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current AI review flow into an AI improvement flow that can find missing balls, suppress false positives, stabilize follow-cam output, adjust highlight windows, compare candidates, and publish only approved final artifacts.

**Architecture:** Keep the existing artifact-first design. AI suggestions stay advisory until an operator explicitly approves a bounded action; each approved action writes a candidate under `ai_candidates/<problem_type>/<candidate_id>/`, produces a comparison report, enters `ai_candidate_registry.json`, passes `ai_improvement_quality_gate.json`, and is promoted only through `final_ai_improvement_artifact_manifest.json`.

**Tech Stack:** Python backend, FastAPI service layer, existing React/Vite UI in `artifacts/web`, OpenAPI-generated TypeScript clients, pytest, pnpm/Vitest/typecheck.

---

## Requirement Summary

The user-facing need is not "AI audits and writes advice"; it is "AI proposes and helps execute bounded improvements, then the system proves whether the new artifact is better."

The four main AI improvement lanes are:

1. **Ball missing:** AI helps localize the ball in lost windows, especially long gaps such as `2049-2544`; a short neighborhood around `2079` is evidence, not closure for the whole gap.
2. **Too much noise:** AI identifies false positives created by spatial split or high-recall detection and proposes bounded cleanup candidates.
3. **Camera too shaky:** AI uses `camera_motion_audit.json` to suggest follow-cam tuning, then the system rerenders and compares the final camera path, not just the ball track.
4. **Highlights:** The system starts from default pre/post buffers; AI may adjust boundaries, but the comparison must preserve the event core and available post-event tail so the final shot aftermath is not cut off.

Additional system needs:

- Prefer temporal chunk parallelism for full-run speed; reserve spatial split/SAHI ROI for approved bounded recovery windows.
- Use a strong model for executable improvement suggestions and visual localization. Smaller models are acceptable only for low-risk labels, dry-run smoke checks, or review-only summaries.
- Keep explicit operator control: approval-file presence alone must not execute or promote anything.
- Keep `localize_ball_roi` as a first-class missing-ball executable action, but only inside explicit approved bounded windows.

## Current State

Already present and usable:

- `python_backend/football_tracking/ball_audit.py` audits raw/cleaned ball tracks.
- `python_backend/football_tracking/review_packets.py` creates review evidence windows.
- `python_backend/football_tracking/ai_visual_review.py` can call a model for visual review.
- `python_backend/football_tracking/ai_improvement.py` writes `ai_improvement_report.json`.
- `python_backend/football_tracking/ai_candidate_registry.py`, `ai_candidate_lifecycle.py`, `ai_candidate_comparison.py`, `ai_improvement_quality_gate.py`, and `final_artifact_manifest.py` define the candidate and promotion scaffolding.
- `python_backend/football_tracking/missing_ball_candidate_executor.py` and `missing_ball_recovery_comparison.py` support bounded missing-ball recovery candidates.
- `python_backend/football_tracking/noise_candidate_comparison.py` supports noise cleanup candidates.
- `python_backend/football_tracking/camera_motion_audit.py` audits final follow-cam camera movement.
- `python_backend/football_tracking/highlights.py` renders simple highlight clips.
- `python_backend/scripts/run_stable_ai_improvement_workflow.py` orchestrates review, approvals, selected missing-ball/noise execution, quality gate, and final manifest.

Main gaps to close:

- `adjust_follow_cam` and `tracking_rerun_before_follow_cam` are currently recorded as unsupported/skipped in the stable workflow.
- `adjust_highlight_window` and `render_suggested_highlight` are currently recorded as unsupported/skipped in the stable workflow.
- Follow-cam and highlight already have child-run execution paths in the API service; the AI candidate work must reuse or wrap those paths instead of creating a disconnected second execution system.
- The AI prompt/output contract needs to emphasize executable improvement, full-window missing-ball coverage, strong-model routing, camera-vs-tracking distinction, and highlight tail preservation.
- The API/UI should let an operator see candidate status, execute selected approvals, compare candidates, and promote/reject final output without reading JSON files manually.
- A real-video acceptance recipe should prove stable output: final follow-cam, AI checks, candidate comparisons, and highlight clips.

---

## File Responsibility Map

Create:

- `python_backend/football_tracking/ai_improvement_prompt_contract.py`  
  Centralizes model-facing instructions, allowed action schema hints, model routing policy text, and examples for missing-ball/noise/follow-cam/highlight improvements.

- `python_backend/football_tracking/follow_cam_candidate_executor.py`  
  Executes approved follow-cam candidates under `ai_candidates/follow_cam/<candidate_id>/`.

- `python_backend/football_tracking/follow_cam_candidate_comparison.py`  
  Compares baseline and candidate follow-cam outputs using `camera_motion_audit.json`, `camera_path.csv`, and `ball_track.csv`.

- `python_backend/football_tracking/highlight_candidate_executor.py`  
  Executes approved highlight candidates under `ai_candidates/highlight/<candidate_id>/`.

- `python_backend/football_tracking/highlight_candidate_comparison.py`  
  Compares event `core_window`, required post-event tail, source-end clamps, rendered frame count, and approval metadata.

- `python_backend/football_tracking/highlight_window_validation.py`  
  Shared validator for event `core_window`, default buffer, source-end clamp, and required tail checks used by both API highlight rendering and AI highlight candidates.

- `python_backend/scripts/run_real_ai_improvement_acceptance.py`  
  Manual real-video acceptance runner that records one reproducible end-to-end evidence pack.

- `python_backend/tests/test_ai_improvement_prompt_contract.py`
- `python_backend/tests/test_follow_cam_candidate_comparison.py`
- `python_backend/tests/test_follow_cam_candidate_executor.py`
- `python_backend/tests/test_highlight_candidate_comparison.py`
- `python_backend/tests/test_highlight_candidate_executor.py`
- `python_backend/tests/test_real_ai_improvement_acceptance.py`

Modify:

- `python_backend/football_tracking/ai_improvement.py`  
  Use the prompt contract and emit executable candidate suggestions only when evidence, bounded windows, candidate ids, expected artifacts, and comparison criteria are present.

- `python_backend/football_tracking/ai_contracts.py`  
  Keep allowed actions and problem types aligned with the executable contract.

- `python_backend/scripts/run_stable_ai_improvement_workflow.py`  
  Dispatch selected follow-cam and highlight approvals to their executors instead of only writing skipped stages.

- `python_backend/football_tracking/ai_improvement_quality_gate.py`  
  Read follow-cam and highlight comparison reports from disk/registry/final manifest and fail unavailable selected comparisons in real mode.

- `python_backend/football_tracking/final_artifact_manifest.py`  
  Ensure `follow_cam_video` and `highlight_clip` finalization paths are fully covered by tests and reject unapproved or comparison-failing candidates.

- `python_backend/football_tracking/ai_candidate_lifecycle.py`  
  Show follow-cam/highlight candidate stages with the same clarity as missing-ball/noise.

- `python_backend/football_tracking/api/service.py`
- `python_backend/football_tracking/api/routes/ai.py`
- `python_backend/football_tracking/api/schemas.py`
- `lib/api-spec/openapi.yaml`
- `lib/api-client-react/src/generated/*`
- `lib/api-zod/src/generated/*`
  Expose candidate execution/finalization summaries and refresh generated clients.

- `artifacts/web/src/*`  
  Show candidate lifecycle, required execution, comparison status, and promote/reject controls in the AI Analysis / Deliverable flow.

- `docs/operations/ai-improvement-contract.md`
- `docs/operations/ai-improvement-workflow.md`
- `README.md`
  Document the executable AI improvement loop, model policy, temporal-vs-spatial strategy, and real-video acceptance command.

---

## Execution Sequencing And Dependency Rules

- PRs must run in order: `PR1 -> PR2 -> PR3 -> PR4 -> PR5 -> PR6`.
- PR3, PR4, and PR5 all touch `python_backend/scripts/run_stable_ai_improvement_workflow.py`, `python_backend/football_tracking/ai_improvement_quality_gate.py`, `python_backend/football_tracking/final_artifact_manifest.py`, and `python_backend/football_tracking/ai_candidate_lifecycle.py`; each one must start from the prior merged PR on latest `main`.
- Follow-cam and highlight candidate execution must not create a separate status/history/cancel path. The implementation choice is:
  - the candidate executor calls the same underlying render helpers used by existing child runs, then writes candidate registry/manifests; and
  - API child-run routes become candidate-aware wrappers when an operator triggers the same work from the UI.
- Candidate media stays in `ai_candidates/<problem_type>/<candidate_id>/`; existing history child runs can reference that candidate directory, but final promotion still happens only through `finalize_ai_candidate`.

---

## PR 1: Prompt Contract And Improvement Intent

**Goal:** Extract and de-duplicate the existing prompt/contract rules into a shared prompt contract, preserving current behavior while making executable improvement suggestions stricter than vague review advice.

**Files:**

- Create: `python_backend/football_tracking/ai_improvement_prompt_contract.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_contracts.py`
- Modify: `docs/operations/ai-improvement-contract.md`
- Test: `python_backend/tests/test_ai_improvement_prompt_contract.py`
- Test: `python_backend/tests/test_ai_improvement.py`

### Build

- [ ] Define a single prompt contract with four lanes: `missing_ball`, `noise`, `follow_cam`, `highlight`.
- [ ] Preserve current accepted missing-ball ROI behavior by including `localize_ball_roi` in the public executable action set.
- [ ] Require executable suggestions to include:
  - `candidate_id`
  - `approval_id` or approval-ready source id
  - `problem_type`
  - `recommended_action`
  - `source_packet_id` or `visual_review_id`
  - bounded `start_frame` and `end_frame`
  - `expected_artifact`
  - `comparison_criteria`
- [ ] Add model-policy text:
  - strong model for executable suggestions, missing-ball localization, and candidate-producing visual reasoning
  - smaller model only for low-risk tagging, dry-run, or review-only summaries
- [ ] Add missing-ball rule:
  - long lost gaps must be covered end to end or list explicit uncovered subwindows
  - `2049-2544` cannot be closed by only checking `2079`
  - `localize_ball_roi` is valid only for explicitly approved bounded windows and must never expand into broad full-video SAHI
- [ ] Add noise rule:
  - spatial split/high-recall false positives should produce bounded cleanup suggestions, not broad full-video SAHI
- [ ] Add follow-cam rule:
  - if camera instability overlaps Lost/Predicted track frames, suggest `tracking_rerun_before_follow_cam`
  - if tracking is stable and the camera is the problem, suggest `adjust_follow_cam`
- [ ] Add highlight rule:
  - default buffer may be adjusted, but the event `core_window` and required post-event tail must remain inside the render window unless source video end clamps it

### Tests

- [ ] Unit test that prompt text contains the closed action set: `localize_ball_roi`, `rerun_ball_window`, `mark_ball_not_visible`, `noise_filter_adjustment`, `tighten_noise_filter`, `reject_noise`, `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, `adjust_highlight_window`, `render_suggested_highlight`.
- [ ] Unit test that prompt text includes long-gap full coverage and `2049-2544` protection language.
- [ ] Unit test that prompt text says `localize_ball_roi` is bounded-window-only and not broad full-video SAHI.
- [ ] Unit test that prompt text distinguishes follow-cam tuning from tracking rerun.
- [ ] Unit test that prompt text requires highlight tail preservation.
- [ ] Existing AI improvement tests still pass with normalized public action names.

### Deliver

- `ai_improvement_report.json` suggestions become approval-ready when evidence is sufficient.
- Review-only output is explicitly labeled review-only when evidence is insufficient.
- `localize_ball_roi` remains available for missing-ball recovery and is documented as bounded-window-only.
- Documentation says "AI improvement" means candidate-producing improvement, not automatic mutation.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement_prompt_contract.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py -q
```

---

## PR 2: Missing-Ball And Noise Candidate Hardening

**Goal:** Make the already-present missing-ball/noise lanes stricter on real failure modes: long gaps, right-bottom corner loss, and false positives from spatial split/high recall.

**Files:**

- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Modify: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_missing_ball_candidate_executor.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

### Build

- [ ] Ensure long lost gaps generate review packets that cover the full gap window and include representative `start`, `middle`, `end`, and `tail` evidence frames.
- [ ] If source-video end clamps the `tail` packet, record that clamp as `warn` or `unavailable` evidence; do not silently treat a missing tail as covered.
- [ ] Ensure a small evidence window around `2079` is linked as partial evidence for `2049-2544`, not as full closure.
- [ ] In missing-ball comparison, require candidate recovery or not-visible evidence to cover the full selected approval window.
- [ ] Preserve the no broad full-video SAHI rule:
  - temporal chunks are allowed for full-video speed
  - SAHI/ROI is allowed only inside selected bounded recovery windows
- [ ] In noise comparison, require bounded false-positive windows and accepted `false_positive_class` values.
- [ ] Add comparison evidence fields that explain when a candidate is rejected because it improved recall by adding too many false positives.

### Tests

- [ ] `2049-2544` long gap with only `2079-2100` evidence fails full-window closure.
- [ ] `2049-2544` long gap with `start/middle/end/tail` packet coverage can become approval-ready.
- [ ] Long gap with missing `tail` coverage reports the exact unavailable/clamped tail reason instead of passing silently.
- [ ] Missing-ball candidate that improves recovery but removes existing valid detected frames fails or warns with explicit evidence.
- [ ] Noise candidate that removes short false-positive islands passes when valid ball frames are preserved.
- [ ] Noise candidate that deletes true continuous ball frames fails.
- [ ] Workflow selected missing-ball/noise approvals produce candidate outputs, comparison reports, registry entries, quality gate evidence, and pending finalization.

### Deliver

- `review_packets.json` covers long gaps in a way the model can reason about.
- `missing_ball_recovery_comparison.json` cannot pass on partial window evidence.
- `noise_candidate_comparison.json` proves cleanup did not destroy valid ball track.
- `stable_ai_improvement_workflow_report.json` explains temporal chunk strategy and bounded SAHI/ROI use.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_review_packets.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_missing_ball_candidate_executor.py python_backend/tests/test_missing_ball_recovery_comparison.py python_backend/tests/test_noise_candidate_comparison.py python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

---

## PR 3: Follow-Cam AI Candidate Executor And Comparison

**Goal:** Make AI camera suggestions executable and comparable, so shaky follow-cam output can be improved without hiding tracking failures.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_executor.py`
- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_follow_cam_candidate_executor.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

### Build

- [ ] Implement `execute_follow_cam_candidate(output_dir, approved_action, *, input_video=None, baseline_dir=None)`.
- [ ] Write candidate output under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Reuse the same follow-cam render helper/path used by existing API child renders; if private service code needs extraction, extract a small shared helper instead of duplicating render logic.
- [ ] Keep API child-run status/history/cancel semantics intact. Candidate execution may run synchronously in the stable workflow, but API-triggered execution should use the existing child-run wrapper and then register the candidate artifacts.
- [ ] Accept `adjust_follow_cam` only when the approved action is explicitly selected and has an allowlisted follow-cam config patch.
- [ ] Reject unknown follow-cam config patch keys.
- [ ] Treat `tracking_rerun_before_follow_cam` as blocked until linked to a passing/promoted missing-ball or noise candidate.
- [ ] Copy baseline inputs needed to rerender:
  - source video reference
  - config reference
  - `ball_track.csv` or `ball_track.cleaned.csv`
  - optional player/action-center inputs when available
- [ ] Rerender follow-cam candidate using existing `FollowCamGenerator`.
- [ ] Require generated artifacts when render is exercised:
  - `follow_cam.mp4`
  - `camera_path.csv`
  - `follow_cam_report.json`
  - `camera_motion_audit.json`
  - `follow_cam_candidate_comparison.json`
  - `candidate_manifest.json`
- [ ] Register the candidate in `ai_candidate_registry.json`.
- [ ] Keep passing follow-cam candidates pending until explicit `finalize_ai_candidate(..., output_role="follow_cam_video")`.

### Comparison Rules

- [ ] Compare baseline and candidate over the same evaluable frames.
- [ ] Pass if review events are not worse.
- [ ] Pass if p95 pan step is at least 10% lower or both values are below `90.0`.
- [ ] Pass if max pan acceleration is at least 10% lower or both values are below `80.0`.
- [ ] Fail if max zoom step exceeds `48.0`.
- [ ] Fail if p95 crop height is more than 15% above baseline.
- [ ] Fail if max crop height is more than 20% above baseline.
- [ ] Fail if Detected/Predicted ball crop coverage drops below `baseline_coverage - 0.02` or below `0.95`.
- [ ] Warn or mark unavailable on sparse data; do not silently pass.

### Tests

- [ ] Smooth candidate compared to shaky baseline passes.
- [ ] Shaky candidate compared to stable baseline fails.
- [ ] Candidate that simply zooms out too much fails.
- [ ] Candidate with unknown config patch key fails before render.
- [ ] Candidate with sparse or unavailable `camera_path.csv` is warn/unavailable.
- [ ] Candidate executor uses the shared follow-cam render helper and does not fork a second video-writing implementation.
- [ ] API child-run wrapper can reference the same candidate directory without losing history/cancel behavior.
- [ ] `tracking_rerun_before_follow_cam` is blocked without linked tracking candidate evidence.
- [ ] `tracking_rerun_before_follow_cam` is allowed after linked tracking candidate evidence passes.
- [ ] Candidate appears in lifecycle and quality gate.
- [ ] Candidate does not become final until promoted through `finalize_ai_candidate`.
- [ ] Promoted follow-cam appears in final manifest `videos`.

### Deliver

- `ai_candidates/follow_cam/<candidate_id>/follow_cam.mp4`
- `ai_candidates/follow_cam/<candidate_id>/camera_path.csv`
- `ai_candidates/follow_cam/<candidate_id>/camera_motion_audit.json`
- `ai_candidates/follow_cam/<candidate_id>/follow_cam_candidate_comparison.json`
- `ai_candidates/follow_cam/<candidate_id>/candidate_manifest.json`
- Registry/lifecycle/quality-gate/final-manifest support for `follow_cam`.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam_candidate_executor.py python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

---

## PR 4: Highlight AI Candidate Executor And Comparison

**Goal:** Make AI highlight suggestions executable, and prevent clips from cutting off the shot or post-event aftermath.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_executor.py`
- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Create: `python_backend/football_tracking/highlight_window_validation.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_highlight_candidate_executor.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

### Build

- [ ] Implement `execute_highlight_candidate(output_dir, approved_action, *, input_video=None)`.
- [ ] Write candidate output under `ai_candidates/highlight/<candidate_id>/`.
- [ ] Extract current highlight selection/tail validation from `ApiService` into `highlight_window_validation.py` so the API child-run path and AI candidate executor share one validator.
- [ ] Reuse the same highlight render helper/path used by existing API child renders; do not create a separate media-writing implementation.
- [ ] Keep API child-run status/history/cancel semantics intact. Candidate execution may run synchronously in the stable workflow, but API-triggered execution should use the existing child-run wrapper and then register the candidate artifacts.
- [ ] Support `adjust_highlight_window` and `render_suggested_highlight`.
- [ ] Resolve the source event from `event_candidates.json`.
- [ ] Start from the event candidate default render window and buffer policy.
- [ ] Apply AI suggested start/end only if it still contains:
  - event `core_window.start_frame`
  - event `core_window.end_frame`
  - required post-event tail through `core_window.end_frame + min_tail_frames`, clamped by source video end
- [ ] Render via existing `render_highlight_clip`.
- [ ] Write:
  - `highlight.mp4`
  - `highlight_report.json`
  - `highlight_candidate_comparison.json`
  - `candidate_manifest.json`
- [ ] Register the candidate in `ai_candidate_registry.json`.
- [ ] Keep passing highlight candidates pending until explicit `finalize_ai_candidate(..., output_role="highlight_clip")`.

### Comparison Rules

- [ ] Fail if the render window cuts the event core.
- [ ] Fail if it cuts available post-event tail.
- [ ] Pass or warn with explicit evidence if the source video end clamps the tail.
- [ ] Fail if the approved action references a missing event candidate id.
- [ ] Fail if the rendered frame count does not match the requested/clamped window.
- [ ] Preserve event id, approval id, candidate id, buffer policy, and clamp evidence.

### Tests

- [ ] Default buffer clip passes.
- [ ] AI-expanded pre-buffer/post-buffer clip passes.
- [ ] AI-shrunk clip that cuts final shot aftermath fails.
- [ ] Source-video-end clamp records explicit clamp evidence.
- [ ] Missing event candidate id fails cleanly.
- [ ] Rendered frame count mismatch fails comparison.
- [ ] Existing API highlight render path and new AI highlight candidate executor call the same window validator.
- [ ] API child-run wrapper can reference the same candidate directory without losing history/cancel behavior.
- [ ] Candidate appears in lifecycle, registry, quality gate, and final manifest.
- [ ] Promoted highlight appears in final manifest `clips`.

### Deliver

- `ai_candidates/highlight/<candidate_id>/highlight.mp4`
- `ai_candidates/highlight/<candidate_id>/highlight_report.json`
- `ai_candidates/highlight/<candidate_id>/highlight_candidate_comparison.json`
- `ai_candidates/highlight/<candidate_id>/candidate_manifest.json`
- Final manifest can list multiple promoted `highlight_clip` artifacts.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_highlight_candidate_executor.py python_backend/tests/test_highlight_candidate_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_stable_ai_improvement_workflow.py -q
```

---

## PR 5: API And UI Operator Loop

**Goal:** Let the operator run the AI improvement loop, execute approved candidates, inspect comparison status, and promote/reject final artifacts without manually editing JSON.

**Files:**

- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `lib/api-spec/openapi.yaml`
- Regenerate: `lib/api-client-react/src/generated/*`
- Regenerate: `lib/api-zod/src/generated/*`
- Modify: `artifacts/web/src/*`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_api_routes.py`
- Test: OpenAPI/generated-client contract checks used by the repo
- Test: relevant web tests under `artifacts/web`

### Build

- [ ] Add an API method to execute selected approved AI actions for all supported lanes:
  - missing-ball
  - noise
  - follow-cam
  - highlight
- [ ] Return a compact execution summary:
  - selected approval ids
  - candidate ids
  - candidate dirs
  - comparison statuses
  - required finalization actions
  - warnings/errors
- [ ] Expose candidate lifecycle and finalization state in run detail responses.
- [ ] Add Pydantic request/response schemas in `api/schemas.py` for candidate execution, comparison summaries, and finalization decisions.
- [ ] Register explicit route handlers in `api/routes/ai.py`; do not rely on service methods that are unreachable from HTTP.
- [ ] Add promote/reject endpoint coverage for:
  - `missing_ball_track`
  - `noise_cleaned_track`
  - `follow_cam_video`
  - `highlight_clip`
- [ ] Update OpenAPI and generated clients.
- [ ] UI should show:
  - AI suggestion type
  - whether it is review-only or executable
  - whether approval is selected
  - whether execution is required
  - candidate comparison result
  - promote/reject controls
  - final selected follow-cam and highlight outputs
- [ ] UI text should make clear that AI can improve artifacts only after explicit approval and comparison.

### Tests

- [ ] API rejects execution when approval ids are missing.
- [ ] API rejects unsupported action types with clear reason.
- [ ] API executes follow-cam/highlight candidates through service with monkeypatched renderers.
- [ ] API finalizes pass candidates and rejects fail/unavailable candidates.
- [ ] Route tests verify new execute/finalize endpoints are registered and return the declared schema.
- [ ] OpenAPI test verifies the new request/response schemas and enum values are exported.
- [ ] Type generation stays in sync.
- [ ] UI lifecycle rendering shows pending, pass, fail, promoted, and rejected states.

### Deliver

- Operator can run: review -> approve -> execute candidate -> compare -> promote/reject from app/API.
- Generated clients include the candidate execution and finalization schema.
- UI stops treating AI output as only a report and shows the improvement lifecycle.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py -q
corepack pnpm run typecheck:libs
corepack pnpm --filter @workspace/web run test:lifecycle
```

---

## PR 6: Real-Video Acceptance Pack And Documentation

**Goal:** Prove the loop on a real video and leave a repeatable recipe for stable tracking output, AI improvement, follow-cam, and highlights.

**Files:**

- Create: `python_backend/scripts/run_real_ai_improvement_acceptance.py`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `README.md`
- Test: `python_backend/tests/test_real_ai_improvement_acceptance.py`

### Build

- [ ] Add a manual acceptance runner that accepts:
  - `--output-dir`
  - `--input-video`
  - `--approved-actions-path`
  - `--approval-ids`
  - `--approved-action-id`
  - `--model`
  - `--mode real`
- [ ] The runner should call the stable workflow and summarize:
  - review packet count
  - visual review status
  - AI improvement model and provider mode
  - selected approvals
  - candidate execution count by problem type
  - comparison status by problem type
  - final manifest summary
  - follow-cam video path
  - highlight clip paths
- [ ] It should write `real_ai_improvement_acceptance_report.json`.
- [ ] It should not create final promotions automatically; promotions remain explicit.
- [ ] Documentation should include:
  - recommended temporal chunk command
  - why broad spatial split/SAHI is not the default
  - how to use bounded SAHI/ROI for missing-ball recovery
  - model recommendation for executable improvement
  - expected artifact tree
  - manual review checklist for final video quality

### Tests

- [ ] Runner validates required path arguments.
- [ ] Runner can be tested with monkeypatched workflow output.
- [ ] Runner writes the expected JSON summary.
- [ ] Docs mention the four AI improvement lanes and final manifest gating.

### Deliver

- `real_ai_improvement_acceptance_report.json`
- Repeatable command for the user's real video.
- Updated README and workflow docs.
- A stable evidence pack suitable for comparing future tuning runs.

### Verification Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_real_ai_improvement_acceptance.py -q
```

Manual real-video command shape:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_real_ai_improvement_acceptance.py `
  --output-dir python_backend\outputs\<existing_run> `
  --input-video python_backend\data\<match_video>.mp4 `
  --approved-actions-path python_backend\outputs\<existing_run>\ai_improvement_approved_actions.json `
  --approval-ids <comma_separated_ids> `
  --approved-action-id <single_follow_cam_or_highlight_id> `
  --model gpt-5.4 `
  --mode real
```

---

## Cross-PR Quality Gates

Run after each PR:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_candidate_registry.py python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py -q
```

Run before merging the full series:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py python_backend/tests/test_missing_ball_candidate_executor.py python_backend/tests/test_missing_ball_recovery_comparison.py python_backend/tests/test_noise_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_highlights.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_api_service.py -q
corepack pnpm run typecheck:libs
corepack pnpm --filter @workspace/web run test:lifecycle
```

Manual acceptance requires a real video and should be recorded in PR 6:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\<existing_run> `
  --input-video python_backend\data\<match_video>.mp4 `
  --mode real `
  --candidate-intent suggest_candidates `
  --parallel-mode temporal `
  --model gpt-5.4
```

## Deliverable Definition

The whole series is complete only when a real run can produce:

- `review_packets.json`
- `ai_visual_review.json` when real provider mode is enabled
- `ai_improvement_report.json`
- `ai_improvement_approved_actions.json`
- `ai_candidates/missing_ball/<candidate_id>/missing_ball_recovery_comparison.json` when missing-ball approval is executed
- `ai_candidates/noise/<candidate_id>/noise_candidate_comparison.json` when noise approval is executed
- `ai_candidates/follow_cam/<candidate_id>/follow_cam.mp4`
- `ai_candidates/follow_cam/<candidate_id>/follow_cam_candidate_comparison.json`
- `ai_candidates/highlight/<candidate_id>/highlight.mp4`
- `ai_candidates/highlight/<candidate_id>/highlight_candidate_comparison.json`
- `ai_candidate_registry.json`
- `ai_candidate_lifecycle` visible in API run detail/history
- `ai_improvement_quality_gate.json`
- `final_ai_improvement_artifact_manifest.json`
- Final promoted `follow_cam.mp4` reference in manifest `videos`
- Final promoted highlight references in manifest `clips`
- `real_ai_improvement_acceptance_report.json`

## Managed PR Execution Rules

- Start every PR from latest `origin/main`.
- Use a fresh branch per PR.
- Use a worker agent for implementation.
- Use a separate spec-compliance reviewer and a code-quality reviewer before publishing.
- Fix all valid Critical/Important findings.
- Run focused tests before opening the PR.
- After opening each PR, wait for CI and remote comments, evaluate Copilot comments on merit, fix confirmed issues, then merge.
- Delete merged local and remote branches only after merge is confirmed.
