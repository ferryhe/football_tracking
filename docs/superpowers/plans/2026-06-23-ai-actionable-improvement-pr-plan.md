# AI Actionable Improvement PR Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `managed-pr-development` for the full program. For each implementation PR, use `superpowers:subagent-driven-development` or a scoped worker, then request independent spec and code-quality review before opening or merging the PR. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade AI from passive review into bounded, testable improvement for missing-ball recovery, noise cleanup, follow-cam stability, and highlight timing.

**Architecture:** Deterministic artifacts remain the source of truth. AI proposes bounded candidates with traceable packet/visual evidence; code executes only explicitly approved candidates in isolated folders; deterministic comparisons, quality gates, registry entries, and final manifests decide whether candidates can be promoted.

**Tech Stack:** Python backend, pytest, FastAPI service/API schema, generated API clients, React UI artifacts, existing tracking artifacts, `review_packets`, `ai_visual_review`, `ai_improvement`, `run_stable_ai_improvement_workflow`, candidate registry, quality gate, follow-cam renderer, highlight renderer.

---

## Requirement Summary

The product need is **AI improvement**, not just **AI audit**.

The four user-visible improvement lanes are:

- **Ball cannot be found:** AI inspects review packets and visual evidence, then proposes a bounded recovery ROI or a full-window evidence-backed `not_visible` resolution.
- **Too much noise:** AI classifies false positives and proposes bounded cleanup candidates that reduce noise without removing sustained real-ball signal.
- **Camera is too shaky:** AI uses `camera_motion_audit.json` plus nearby ball-track status to decide whether the fix is tracking recovery first or follow-cam rerender tuning.
- **Highlights need better timing:** default pre/post buffers remain the safe baseline; AI can adjust boundaries only if the event core and post-event tail are preserved.

Speed rule:

- Prefer **temporal chunk parallelism** for full-video throughput. Broad full-video spatial splitting/SAHI is not the default because it creates too many false positives. Use spatial/SAHI only inside explicitly approved bounded recovery windows.

## Precise Current State

- **Open PR #48:** `feat/noise-ai-cleanup-candidates` is open, mergeable, and Node/Python checks passed. It adds bounded noise cleanup candidates and comparison reports, but it is not merged yet.
- **Already on `origin/main`:**
  - `camera_motion_audit.json` generation from follow-cam output exists.
  - Missing-ball API child-run execution exists under `ai_candidates/missing_ball/<candidate_id>/`.
  - `missing_ball_recovery_comparison.json` and many `2049-2544` / `2079` tests already exist.
  - Review packets include long-gap start/middle/end/tail coverage fixtures.
  - Strong visual model routing exists in `ai_visual_review.py`.
  - Stable workflow currently records some missing-ball approvals as `pending_api_required` instead of executing them directly.
- **Not yet complete:**
  - Stable CLI workflow does not yet execute missing-ball candidates through the same reusable path as the API.
  - Follow-cam AI rerender candidates are not fully executable/comparable.
  - Highlight AI boundary candidates are not fully executable/comparable.
  - API/UI lifecycle visibility is still too weak to prevent confusion between review-only notes and applied improvements.

## Shared Artifact Proof Contract

Every executable-candidate PR must prove the same chain:

- [ ] Baseline artifacts are hash-snapshotted before/after review-only stages and remain unchanged.
- [ ] Execution requires explicit approval ids; approval-file presence alone never executes.
- [ ] Candidate directory is created only after approval validation passes.
- [ ] Candidate writes `candidate_manifest.json`.
- [ ] Candidate writes a problem-specific comparison report with `pass`, `warn`, `fail`, or `unavailable`.
- [ ] Candidate registry records candidate id, problem type, candidate dir, comparison report, comparison status, and promotion status.
- [ ] Quality gate sees the comparison and fails or warns when evidence is missing.
- [ ] Final manifest can trace baseline, consumed approval, candidate output, comparison, rejected/pending/promoted status, and final artifact.
- [ ] `pass` may promote; `warn` requires explicit human confirmation; `fail`, `unavailable`, and `unsupported` do not promote.

## PR0: Finish Current Noise PR And Baseline

**Purpose:** Merge the current noise candidate work before starting new branches.

**Files:**

- Existing branch: `feat/noise-ai-cleanup-candidates`
- Existing PR: `https://github.com/ferryhe/football_tracking/pull/48`

**Build:**

- [ ] Check PR #48 GitHub reviews, checks, comments, Copilot feedback, and review threads.
- [ ] Fix only valid remote comments.
- [ ] Re-run focused and full relevant validation.
- [ ] Merge PR #48 after checks and valid feedback are resolved.
- [ ] Delete merged remote and local branch.
- [ ] Update local `main` from `origin/main`.

**Tests:**

- [ ] `pytest python_backend/tests/test_noise_candidate_comparison.py python_backend/tests/test_ai_review_triggers.py python_backend/tests/test_review_packets.py -q`
- [ ] `pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_final_artifact_manifest.py -q`
- [ ] Full backend suite if remote comments require non-trivial changes.

**Deliver:**

- Noise candidate loop merged into `main`.
- Confirmed artifact proof: false-positive islands decrease without damaging sustained true-ball coverage.
- Clean latest `main` for later PRs.

## PR1: Evidence Contract Delta And Guardrails

**Purpose:** Treat current `origin/main` behavior as the baseline, then fill any remaining evidence-contract gaps instead of rebuilding existing missing-ball logic.

**Files:**

- Modify as needed: `python_backend/football_tracking/ai_improvement.py`
- Modify as needed: `python_backend/football_tracking/ai_visual_review.py`
- Modify as needed: `python_backend/football_tracking/review_packets.py`
- Modify as needed: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify as needed: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`

**Build:**

- [ ] Audit current long-gap, strong-model, packet-coverage, and `2079` fixtures; do not duplicate already-passing behavior.
- [ ] Add only missing context fields needed by downstream candidates, such as `required_window_coverage`, `coverage_status`, `covered_start_frame`, `covered_end_frame`, and `uncovered_ranges`, if they are not already present.
- [ ] Strengthen prompt validation so AI cannot close `2049-2544` with a short `2079`-only note.
- [ ] Ensure real mode records `unavailable` or fails closed when strong visual/improvement model configuration is absent.
- [ ] Ensure weak model fallback is limited to dry-run or advisory tagging.
- [ ] Add one shared test helper for artifact proof checks that later PRs can reuse.

**Tests:**

- [ ] Full-gap recovery suggestion for `2049-2544` passes.
- [ ] Full-gap evidence-backed `not_visible` resolution passes.
- [ ] Short window around `2079` fails when the required gap is `2049-2544`.
- [ ] Missing visual evidence is not treated as successful localization.
- [ ] Strong-model-unavailable real mode is not marked as successful improvement.
- [ ] Review-only stages preserve baseline track hashes.

**Deliver:**

- Clear evidence contract for AI suggestions.
- Hardened long-gap guardrails around the right-bottom `2049-2544` case.
- Reusable artifact-proof test helper.

## PR2: Candidate Lifecycle Visibility Foundation

**Purpose:** Make API/UI state truthful before adding more executable candidates, so users can see the difference between “AI suggested” and “AI improved”.

**Files:**

- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/routes/runs.py`
- Modify: `lib/api-spec/openapi.yaml`
- Regenerate: `lib/api-zod/src/generated/`
- Regenerate: `lib/api-client-react/src/generated/`
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`
- Add or modify frontend tests available in the repo for `ai-analysis` and `deliverable`.

**Build:**

- [ ] Expose candidate registry, comparison summaries, quality-gate summary, and final manifest summary through API responses.
- [ ] Expose statuses: `proposed`, `approved`, `executed`, `pass`, `warn`, `fail`, `unavailable`, `resolved_not_visible`, `unsupported`, `pending_confirmation`, `promoted`, `rejected`.
- [ ] Show why a suggestion cannot execute: missing evidence, unsafe window, unsupported type, missing comparison, failed gate, or pending API/service execution.
- [ ] UI must label review-only recommendations as review-only until there is execution, comparison, gate, and manifest evidence.
- [ ] Mutation controls must require explicit approval ids.

**Tests:**

- [ ] API returns registry/comparison/gate/manifest summaries.
- [ ] API refuses execution without explicit approval ids.
- [ ] Generated OpenAPI clients are synchronized.
- [ ] UI handles empty, proposed, pending, pass, warn, fail, unsupported, resolved no-op, promoted, and rejected states.
- [ ] UI does not display review-only notes as applied improvements.

**Deliver:**

- Operator-visible candidate lifecycle.
- Early protection against confusing AI audit with AI improvement.

## PR3: Missing-Ball Executor Unification

**Purpose:** Reuse the existing API child-run recovery capability from the stable workflow, removing the current `pending_api_required` gap.

**Chosen Approach:**

Extract the reusable execution core from `ApiService` into a backend module, then call it from both API service and CLI workflow. Do not make the CLI call the HTTP API. The existing API behavior remains the compatibility surface; the new module becomes the canonical executor.

**Files:**

- Create: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_missing_ball_candidate_executor.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`

**Build:**

- [ ] Move only the reusable approved recovery execution logic into `missing_ball_candidate_executor.py`.
- [ ] Preserve existing API child-run request/response behavior.
- [ ] Stable workflow executes selected `targeted_rerun` and `localize_ball_roi` approvals through the shared executor.
- [ ] Candidate outputs remain under `ai_candidates/missing_ball/<candidate_id>/`.
- [ ] Candidate writes recovered tracks, `ball_audit.json`, `metrics_report.json`, `missing_ball_recovery_comparison.json`, `candidate_manifest.json`, and registry entry.
- [ ] Stable workflow no longer marks selected executable missing-ball approvals as `pending_api_required` when execution succeeds.
- [ ] Full-video localize/SAHI remains rejected; bounded ROI/SAHI is allowed only inside approved windows.

**Tests:**

- [ ] API child-run behavior remains backward compatible.
- [ ] CLI workflow executes an explicit selected approval end to end.
- [ ] Approval-file presence alone does not execute.
- [ ] Candidate comparison passes when long lost gap decreases or resolves.
- [ ] Candidate comparison fails when only a short subwindow is recovered.
- [ ] Baseline track hashes remain unchanged.
- [ ] Registry, quality gate, and final manifest record pass/warn/fail/unavailable.
- [ ] Temporal chunk strategy remains the full-video speed default.
- [ ] Broad full-video spatial/SAHI recovery is rejected before output is created.

**Deliver:**

- One canonical missing-ball candidate executor.
- Stable workflow can actually produce missing-ball improvement candidates.
- Real or fixture artifact proof for the `2049-2544` gap.

## PR4: Follow-Cam AI Contract And Routing

**Purpose:** Define the follow-cam AI action contract before implementing the renderer candidate loop.

**Files:**

- Modify: `python_backend/football_tracking/ai_contracts.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `lib/api-spec/openapi.yaml`
- Regenerate: generated API clients if schema changes require it.
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`

**Build:**

- [ ] Add or validate action names: `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, and `tracking_recovery_then_follow_cam_rerender`.
- [ ] Add camera-motion audit events and nearby ball-track status to AI context.
- [ ] Route Lost/Predicted overlap to tracking recovery first.
- [ ] Stable Detected context may route to follow-cam-only rerender.
- [ ] Record selected follow-cam approvals as executable-pending, not generic unsupported, until PR5 implements execution.
- [ ] API/UI lifecycle from PR2 must expose pending follow-cam candidate state accurately.

**Tests:**

- [ ] Camera-motion events appear in AI context.
- [ ] Detected-only camera spike can produce `adjust_follow_cam`.
- [ ] Lost/Predicted overlap produces tracking-first action.
- [ ] Unknown follow-cam actions are rejected by contract validation.
- [ ] Pending follow-cam state is visible and not labeled as applied improvement.

**Deliver:**

- Follow-cam AI action contract and routing.
- No renderer mutation yet; this PR is contract and visibility only.

## PR5: Follow-Cam Rerender Candidate And Comparison

**Purpose:** Execute approved follow-cam candidates and prove the new video is smoother without hiding the play.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam.py`
- Modify: `python_backend/football_tracking/camera_motion_audit.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_camera_motion_audit.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`

**Build:**

- [ ] Execute follow-cam-only candidates under `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] For tracking-first actions, link to a missing-ball candidate before rerendering.
- [ ] Candidate artifacts include `follow_cam.mp4`, `camera_path.csv`, `follow_cam_report.json`, `camera_motion_audit.json`, `follow_cam_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Compare baseline and candidate review event count, p95 pan step, max acceleration, max zoom jump, ball/crop coverage proxy, and excessive zoom-out ratio.
- [ ] Fail candidates that reduce shake only by zooming too far out or by letting the ball leave crop coverage.
- [ ] Require linked tracking recovery to pass before a linked follow-cam rerender can pass.

**Tests:**

- [ ] Approved follow-cam-only candidate writes all artifacts.
- [ ] Comparison passes when motion improves and crop coverage is preserved.
- [ ] Comparison fails when zoom-out is excessive.
- [ ] Comparison fails when ball/crop coverage drops.
- [ ] Comparison fails when motion metrics regress.
- [ ] Follow-cam-only candidate does not mutate ball-track files.
- [ ] Tracking-first candidate fails if linked missing-ball comparison does not pass.
- [ ] Registry, quality gate, and final manifest summarize follow-cam candidate status.
- [ ] A small fixture or short real-video canary renders actual `follow_cam.mp4` and `camera_path.csv`.

**Deliver:**

- Candidate `follow_cam.mp4`.
- `follow_cam_candidate_comparison.json`.
- Evidence that smoother camera motion did not hide tracking failure.

## PR6: Highlight AI Contract, Candidate, And Comparison

**Purpose:** Let AI adjust highlight boundaries while preserving the event core and result tail.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/events.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/ai_contracts.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify/regenerate API spec and clients if schema changes require it.
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_highlight_candidate_comparison.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_api_service.py`

**Build:**

- [ ] Preserve default pre/post buffer as baseline.
- [ ] Require AI-adjusted windows to include `core_window`.
- [ ] Require post-event tail coverage unless source video end clamps the tail.
- [ ] Record before/after windows, reason, source event id, source-end clamp, and tail coverage.
- [ ] Execute selected candidates under `ai_candidates/highlights/<candidate_id>/`.
- [ ] Candidate artifacts include `highlight.mp4`, `highlight_report.json`, `highlight_candidate_comparison.json`, and `candidate_manifest.json`.
- [ ] Accepted highlight publishing requires comparison evidence.
- [ ] API/UI lifecycle exposes highlight candidate states immediately.

**Tests:**

- [ ] Default highlight includes core event and post-event tail.
- [ ] AI window cutting core frames fails.
- [ ] AI window cutting available tail fails.
- [ ] End-of-video clamp preserves all available tail frames and records the reason.
- [ ] Candidate render writes video/report/comparison artifacts.
- [ ] Accepted highlight copier accepts only publishable comparison-backed clips.
- [ ] Registry, quality gate, and final manifest summarize highlight status.
- [ ] A fixture or short real-video canary renders a readable highlight clip.

**Deliver:**

- AI-adjusted highlight candidates.
- Safer goal/shot clips with readable pre/post context.
- Publishable highlight evidence in final manifest.

## PR7: API/UI Lifecycle Polish And Promotion Controls

**Purpose:** Close remaining product gaps after all candidate types exist.

**Files:**

- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/routes/runs.py`
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/pages/deliverable.tsx`
- Modify/regenerate API spec and clients if needed.
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`
- Add/modify frontend tests or Playwright checks available in the repo.

**Build:**

- [ ] Add final promotion/rejection controls that respect `pass`, `warn`, `fail`, and `unavailable`.
- [ ] Require explicit human confirmation for `warn`.
- [ ] Show comparison details for missing-ball, noise, follow-cam, and highlight candidates.
- [ ] Show final selected artifacts and rejected/pending candidates from manifest.
- [ ] Ensure UI copy distinguishes AI audit, AI suggestion, AI executed candidate, and final promoted output.

**Tests:**

- [ ] Promotion accepts `pass`.
- [ ] Promotion blocks `warn` without human confirmation.
- [ ] Promotion blocks `fail` and `unavailable`.
- [ ] UI shows comparison evidence and final manifest status.
- [ ] UI still never labels review-only notes as applied improvement.

**Deliver:**

- Complete operator-facing lifecycle from AI suggestion to final output.
- Promotion controls with clear reasons.

## PR8: Real-Video Verification, Docs, And Skill Capture

**Purpose:** Prove the full workflow on real match output and preserve the working procedure.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Create: `docs/operations/real-video-ai-improvement-checklist.md`
- After repository PRs merge and with user confirmation only: update `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Document AI review vs AI improvement.
- [ ] Document strong-model routing and recommended environment variables.
- [ ] Document temporal chunk speed strategy and bounded spatial/SAHI recovery policy.
- [ ] Document every candidate type, comparison report, quality gate, promotion rule, and rollback path.
- [ ] Add real-video checklist for missing ball, noise, camera shake, highlight tail, and manual final inspection.
- [ ] Record the `2049-2544` / `2079` verification case.
- [ ] Capture skill notes only after the real-video workflow proves stable.

**Tests:**

- [ ] Run full Python tests.
- [ ] Run stable AI improvement workflow in `real` mode when provider configuration exists.
- [ ] Use artifact-only mode only as diagnostics, not release proof.
- [ ] Real or fixture proof for missing-ball, noise, follow-cam, and highlight candidates.
- [ ] Render final follow-cam and verify `camera_motion_audit.json`.
- [ ] Render at least one highlight and verify tail coverage.
- [ ] Manually inspect the known `2049-2544` right-bottom action window.

**Deliver:**

- Updated README and operation docs.
- Real-video evidence pack with commands, output dirs, model settings, candidate decisions, final videos, and highlights.
- Separate local skill update proposal after repo docs are merged.

## End-To-End Acceptance

- Missing-ball cases are closed only by comparison-backed recovery or evidence-backed full-window `not_visible`.
- Noise candidates reduce false-positive islands without damaging sustained true-ball coverage.
- Follow-cam candidates reduce motion spikes without hiding tracking loss.
- Highlight clips preserve event core and post-event tail.
- Review-only stages leave baseline artifacts unchanged.
- Approved actions execute only through explicit ids.
- UI/API never present review-only text as applied improvement.
- Final output is traceable from baseline artifact to AI suggestion, approval, candidate, comparison, quality gate, final manifest, and promoted artifact.
