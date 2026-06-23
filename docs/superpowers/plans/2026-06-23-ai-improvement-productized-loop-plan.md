# AI Improvement Productized Loop Implementation Plan

> **For Codex-managed execution:** use the local `managed-pr-development` skill when it is installed. Repository-only contributors should follow the **Managed PR Gates** section in this document. Each PR starts from latest `main`, uses a fresh branch and worker/reviewer split where available, waits for remote CI/Copilot comments, merges only after valid feedback is resolved, and deletes merged local/remote branches after merge.

**Goal:** turn AI from "review text" into an operator-approved improvement loop that can recover missing balls, reduce noisy false positives, stabilize follow-cam motion, and produce safe highlight clips with comparison-backed final output.

**Architecture:** deterministic baseline artifacts remain immutable until an explicit approved candidate is executed. AI produces bounded, evidence-backed candidate proposals; selected candidates run in isolated `ai_candidates/<type>/<candidate_id>/` folders; comparison reports, registry, lifecycle, quality gate, and final manifest decide whether a candidate can become a final deliverable.

**Tech Stack:** Python backend modules and scripts, FastAPI service, JSON artifacts, pytest, React operator UI, generated OpenAPI clients, existing review packets, visual review, candidate registry/lifecycle, quality gate, follow-cam, event/highlight renderers, and the real match video under `python_backend/data/raw5760x144020fps.mp4`.

---

## Requirement Summary

The product requirement is **AI improvement**, not just **AI audit**.

1. **Ball not found:** AI should inspect packet media and artifact context, localize the ball when possible, propose bounded recovery candidates, or mark the whole relevant window as `not_visible` only with packet/visual evidence. A short frame neighborhood such as frame `2079` must not be allowed to "solve" a longer right-bottom corner gap like `2049-2544`.
2. **Noise too high:** AI should help identify extra balls, shoes, sideline objects, heads, boards, and background false positives, then propose bounded cleanup candidates. A cleanup candidate is useful only if it reduces false-positive islands without damaging sustained true-ball signal.
3. **Camera too shaky:** AI should read `camera_motion_audit.json` and nearby ball-track status, decide whether the problem is tracking loss or follow-cam tuning, then execute a comparable follow-cam candidate only when safe. It must not hide bad tracking by zooming out.
4. **Highlight clips:** deterministic event candidates should provide default pre/post buffers. AI may adjust start/end frames only if the core event and required post-event tail remain covered, including source-video-end clamp cases.

Cross-cutting requirements:

- Use stronger model settings for run-level improvement and hard recovery decisions; smaller models are acceptable only for low-risk tagging or dry-run smoke.
- Prefer temporal chunk parallelism for speed. Broad spatial split/SAHI across the full video is not the stable default because it increases noise; SAHI/ROI should be used inside bounded recovery windows.
- UI must clearly distinguish: AI reviewed, AI suggested, approved, waiting to execute, executed candidate, comparison passed/warned/failed, promoted final output, rejected/blocked.
- Final videos and highlights must be traceable from baseline -> AI evidence -> approval id -> candidate artifacts -> comparison -> quality gate -> final manifest.

## Current Landed Baseline

Already present on `main` and should be preserved:

- Ball, camera, and review-trigger audits:
  - `python_backend/football_tracking/ball_audit.py`
  - `python_backend/football_tracking/camera_motion_audit.py`
  - `python_backend/football_tracking/ai_review_triggers.py`
- Packet and AI review surfaces:
  - `python_backend/football_tracking/review_packets.py`
  - `python_backend/football_tracking/ai_visual_review.py`
  - `python_backend/football_tracking/ai_improvement.py`
  - `review_packets.py` already emits bounded packet windows for long high-recall/lost-gap spans, including start/middle/end/tail coverage.
  - Existing tests already cover the `2049-2544` / frame `2079` partial-coverage failure class.
  - `ai_improvement.py` already contains candidate-intent, provenance, model-routing, camera tracking-first, and highlight-tail prompt/validation guardrails.
- Candidate proof primitives:
  - `python_backend/football_tracking/ai_candidate_comparison.py`
  - `python_backend/football_tracking/ai_candidate_registry.py`
  - `python_backend/football_tracking/ai_candidate_lifecycle.py`
  - `python_backend/football_tracking/ai_improvement_quality_gate.py`
  - `python_backend/football_tracking/final_artifact_manifest.py`
- Operator lifecycle visibility from PR1 has landed and should be preserved by later candidate executors and final-delivery surfaces.
- Candidate lanes already partially supported:
  - missing-ball API child-run execution and `missing_ball_recovery_comparison.json`
  - noise cleanup execution and `noise_candidate_comparison.json`
  - event candidates and basic highlight rendering
  - follow-cam output plus camera motion audit
- Stable workflow script:
  - `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Operator docs:
  - `docs/operations/ai-improvement-workflow.md`

## Main Gaps To Close

- Later backend executors must preserve the landed lifecycle UI contract so an operator cannot confuse review-only text with applied improvement.
- The stable workflow still records selected missing-ball recovery approvals as `pending_api_required`; missing-ball execution should be reusable outside the API service.
- Missing-ball has two separate valid closures and they must stay separate:
  - recovery candidate: execute bounded rerun/ROI and compare with `missing_ball_recovery_comparison.json`
  - `not_visible` resolution: write full-window `missing_ball_resolution.json` only when packet/visual evidence covers the full requested window
- Noise cleanup already executes candidates; remaining noise work is lifecycle visibility, promotion/rejection, final-manifest clarity, and real-video verification.
- Follow-cam has audit and AI suggestions, but lacks a candidate rerender executor, concrete comparison report, quality-gate integration, and final-manifest proof.
- Highlights have event candidates and rendering, but AI-adjusted highlight candidates need comparison and publish gating.
- Promotion/rejection controls are manifest-oriented but not yet a clear operator action across all candidate types.
- Real-video proof must be repeatable after the full loop is complete, including the `2049-2544` right-bottom corner gap, noise windows, camera spikes, and highlight tails.

## Independent Review Adjustments

An independent plan-review agent returned `Needs revision`. The plan was adjusted as follows:

- Added PR0 to lock AI/candidate contracts before building more executors.
- Made follow-cam comparison metrics concrete so "smoother" cannot mean "zoomed far out".
- Split missing-ball recovery execution from evidence-backed `not_visible` resolution.
- Kept PR4 focused on comparison/publish gating, not rewriting existing highlight window validation.
- Added OpenAPI export/codegen validation to PR5.
- Made PR7 real-video evidence reproducible despite ignored `data/` and `outputs/` folders.

---

## PR0: Contract And Threshold Audit

**Purpose:** lock the contracts that later executors will rely on, without rebuilding already-landed guardrails.

**Build:**

- Create `docs/operations/ai-improvement-contract.md`.
- Update `docs/operations/ai-improvement-workflow.md`.
- Inspect as read-only context for field names and existing guardrails:
  - `python_backend/football_tracking/ai_improvement.py`
  - `python_backend/football_tracking/ai_visual_review.py`
  - `python_backend/football_tracking/ai_candidate_comparison.py`
  - `python_backend/football_tracking/final_artifact_manifest.py`
- Document candidate lifecycle stages, `candidate_id`, `approval_id`, `problem_type`, comparison statuses, promotion statuses, and blocking reasons.
- Document model policy:
  - run-level improvement and hard recovery decisions use the configured strong model
  - low-risk dry-run/tagging may use a smaller model
  - real workflow reports record provider mode and model selection
- Document missing-ball closure rules:
  - bounded recovery execution writes candidate artifacts and `missing_ball_recovery_comparison.json`
  - evidence-backed invisibility writes `missing_ball_resolution.json`
  - neither path may claim the long `2049-2544` gap from only the `2079` neighborhood
- Document follow-cam comparison thresholds:
  - `camera_motion_audit.summary.review_event_count` candidate must not exceed baseline
  - candidate `p95_pan_step_px` passes if it is at least 10% better than baseline or both baseline and candidate are below `PAN_STEP_WARN_PX`
  - candidate `max_pan_accel_px` passes if it is at least 10% better than baseline or both baseline and candidate are below `PAN_ACCEL_WARN_PX`
  - candidate `max_zoom_step_px` must not exceed baseline by more than 10% and must stay below `ZOOM_STEP_FAIL_PX`
  - candidate p95 crop height must not exceed baseline p95 crop height by more than 15%
  - candidate max crop height must not exceed baseline max crop height by more than 20%
  - crop coverage uses `ball_track.csv` plus `camera_path.csv`; a Detected/Predicted ball frame is covered when the ball center is inside the crop rectangle
  - candidate coverage must be at least `baseline_coverage - 0.02` and at least `0.95` for Detected/Predicted frames when enough frames exist
- Document highlight comparison rules:
  - render window must contain `core_window`
  - render window must contain required post-event tail: `core_end + min_tail_frames`, clamped by source-video end
  - source-end clamp is a recorded pass/warn reason, not a silent trim

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py -q`
- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_final_artifact_manifest.py -q`
- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_candidate_lifecycle.py python_backend/tests/test_ai_improvement_quality_gate.py -q`
- Set `PYTHONPATH=python_backend` because this workspace's repo-root pytest commands otherwise cannot import the `football_tracking` package.
- Existing `2079` / `2049-2544`, provenance, camera tracking-first, and highlight-tail tests still pass.
- If code changes are needed, add focused tests for the exact missing contract field or downgrade rule.

**Deliver:**

- A stable contract document for worker agents.
- A clear threshold table for follow-cam and highlight comparison.
- Confirmation that existing long-window and tail guardrails are reused, not duplicated.

## PR1: Operator Lifecycle Visibility (landed)

**Purpose:** make the UI stop treating AI review text as if it were an applied improvement.

**Build:**

- Modify `artifacts/web/src/pages/ai-analysis.tsx`.
- Modify `artifacts/web/src/pages/deliverable.tsx`.
- Modify `artifacts/web/src/lib/types.ts` only if UI-specific types are missing from generated clients.
- Modify `artifacts/web/src/lib/i18n.ts` for plain English/Chinese operator labels.
- Add `artifacts/web/src/lib/aiLifecycle.ts` so status mapping can be tested without rendering the whole page.
- Read both `run.ai_candidate_lifecycle` and summary-only `run.stats.ai_candidate_lifecycle`.
- Show per-candidate status:
  - AI review only
  - suggested
  - approved
  - waiting to execute
  - executed candidate
  - passed comparison
  - warned comparison
  - failed comparison
  - promoted final output
  - rejected or blocked
  - resolved not visible
- Show problem type and evidence for missing-ball, noise, follow-cam, and highlight candidates.
- Disable execution buttons unless a concrete approval id exists.
- On the deliverable page, label final/publishable output only when backed by final manifest or comparison evidence.

**Tests:**

- `corepack pnpm --filter @workspace/web run test:lifecycle`
- `corepack pnpm run typecheck:libs`
- `corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck`
- `$env:PORT='3000'; $env:BASE_PATH='/'; corepack pnpm --filter @workspace/web run build`
- Verify UI/helper states for empty, review-only, proposed, approved, pending execution, pass, warn, fail, unavailable, promoted, rejected, blocked, and resolved-not-visible.
- Verify review-only items never show as "applied", "final", or "improved".
- Verify noise candidates show comparison and promotion state, not only approval state.

**Deliver:**

- Operator-visible lifecycle.
- Clear separation between "AI said this" and "AI executed and passed comparison".

## PR2: Missing-Ball Stable Executor

**Purpose:** make the stable workflow actually execute approved missing-ball recovery candidates, while keeping `not_visible` as a separate evidence-backed resolution lane.

**Build:**

- Create `python_backend/football_tracking/missing_ball_candidate_executor.py`.
- Extract reusable child-run execution helpers from `python_backend/football_tracking/api/service.py`.
- Keep API behavior backward compatible for existing child-rerun paths.
- Update `python_backend/scripts/run_stable_ai_improvement_workflow.py` so selected `targeted_rerun` and `localize_ball_roi` approvals execute when explicit `--approved-actions-path` plus `--approval-ids` are supplied.
- Keep current `missing_ball_resolution.json` lane for evidence-backed `not_visible`; do not send `not_visible` through the child-run executor.
- Candidate output must stay under `ai_candidates/missing_ball/<candidate_id>/`.
- Candidate artifacts must include:
  - `ball_track.csv`
  - `ball_track.cleaned.csv`
  - `ball_audit.json`
  - `metrics_report.json`
  - `run_manifest.json`
  - `candidate_manifest.json`
  - `missing_ball_recovery_comparison.json`
- Parent output must update:
  - `ai_candidate_registry.json`
  - `ai_improvement_quality_gate.json`
  - `final_ai_improvement_artifact_manifest.json`
  - `ai_candidate_lifecycle` in API run responses
- Reject broad full-video SAHI recovery from this path.
- Allow SAHI/ROI only for bounded approved windows with packet/visual evidence.

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_missing_ball_candidate_executor.py -q`
- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`
- Required cases:
  - explicit selected missing-ball recovery approval executes end to end in stable workflow
  - approval-file presence alone does not execute
  - unknown or duplicate approval ids fail before execution
  - `2049-2544` gap fails if only a short `2079` subwindow is covered
  - full-window recovery or evidence-backed full-window `not_visible` passes/warns according to gate policy
  - `not_visible` writes `missing_ball_resolution.json`, not recovery candidate artifacts
  - baseline tracks are hash-stable
  - registry, quality gate, final manifest, and lifecycle all show the candidate or resolution
  - broad full-video SAHI request is rejected

**Deliver:**

- One reusable missing-ball recovery executor.
- Stable workflow can produce missing-ball candidate outputs without manual API follow-up.
- Evidence-backed `not_visible` remains a separate, full-window closure.
- The right-bottom corner long-gap failure pattern is protected by tests.

## PR3: Follow-Cam Candidate Rerender And Comparison

**Purpose:** let AI improve shaky final follow-cam output only when the candidate is measurably smoother and still keeps play coverage.

**Build:**

- Create `python_backend/football_tracking/follow_cam_candidate_comparison.py`.
- Add a follow-cam candidate executor in the backend or script layer, reusing `python_backend/football_tracking/follow_cam.py`.
- Execute approved `adjust_follow_cam` candidates under `ai_candidates/follow_cam/<candidate_id>/`.
- Preserve `tracking_rerun_before_follow_cam` as a tracking-first block until the linked missing-ball candidate passes.
- Candidate artifacts must include:
  - `follow_cam.mp4`
  - `camera_path.csv`
  - `follow_cam_report.json`
  - `camera_motion_audit.json`
  - `follow_cam_candidate_comparison.json`
  - `candidate_manifest.json`
- Comparison fields must include:
  - baseline/candidate `review_event_count`
  - baseline/candidate `p95_pan_step_px`
  - baseline/candidate `max_pan_accel_px`
  - baseline/candidate `max_zoom_step_px`
  - baseline/candidate `max_zoom_step_ratio`
  - baseline/candidate mean/p95/max crop height
  - baseline/candidate Detected/Predicted crop coverage
  - `zoom_out_guard_status`
  - `coverage_guard_status`
- Comparison policy:
  - fail if camera review event count worsens
  - fail if candidate p95 pan step is not at least 10% lower than baseline unless both baseline and candidate are below `PAN_STEP_WARN_PX`
  - fail if candidate max pan acceleration is not at least 10% lower than baseline unless both baseline and candidate are below `PAN_ACCEL_WARN_PX`
  - fail if max zoom step worsens by more than 10% or exceeds fail threshold
  - fail if p95 crop height grows by more than 15% or max crop height grows by more than 20%
  - fail if Detected/Predicted crop coverage falls below `baseline_coverage - 0.02` or below `0.95` when enough frames exist
  - warn if metrics improve but data is too sparse to prove coverage
  - pass only when pan-step and pan-acceleration improvement requirements are met, or both compared values are already below their warn thresholds, and anti-zoom/coverage guards pass or warn acceptably
- No ball-track files are mutated by follow-cam candidate execution.
- Update registry, quality gate, final manifest, lifecycle, and UI status.

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_follow_cam_candidate_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py -q`
- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`
- Run OpenAPI export/codegen/typecheck if any API schema or generated-client-facing response changes.
- Candidate rerender writes every required artifact.
- Comparison passes when motion improves and coverage is preserved.
- Comparison fails when shake worsens.
- Comparison fails when candidate simply zooms far out.
- Comparison fails when crop coverage drops.
- Tracking-first action remains blocked until linked missing-ball candidate passes.
- Stable workflow can execute a selected follow-cam candidate.

**Deliver:**

- Candidate follow-cam video with measurable stability proof.
- A report that distinguishes "smoother camera" from "hidden tracking failure".

## PR4: Highlight Candidate Comparison And Publish Gate

**Purpose:** let AI adjust highlight boundaries while preserving the event and aftermath, then prevent review-only clips from being published as improvements.

**Build:**

- Create `python_backend/football_tracking/highlight_candidate_comparison.py`.
- Update `python_backend/football_tracking/highlights.py` only where candidate artifact metadata is needed.
- Update `python_backend/football_tracking/accepted_highlights.py`.
- Update API highlight render helpers in `python_backend/football_tracking/api/service.py` without duplicating existing core/tail validation.
- Update `python_backend/scripts/run_stable_ai_improvement_workflow.py`.
- Execute selected `adjust_highlight_window` or `render_suggested_highlight` approvals under `ai_candidates/highlight/<candidate_id>/`.
- Candidate artifacts must include:
  - `highlight.mp4`
  - `highlight_report.json`
  - `highlight_candidate_comparison.json`
  - `candidate_manifest.json`
- Reuse event fields:
  - `core_window`
  - `render_window`
  - `buffer_policy`
- Comparison must require:
  - core event fully covered
  - minimum post-event tail covered unless clamped by source-video end
  - start/end frame validity
  - source event id/candidate id match
  - source-end clamp recorded as pass/warn evidence
- Accepted highlight publishing must require comparison-backed candidate evidence, except for explicitly documented legacy/manual mode.
- Update registry, quality gate, final manifest, lifecycle, and UI status.

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_highlight_candidate_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py -q`
- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_api_service.py -q`
- Run OpenAPI export/codegen/typecheck if any API schema or generated-client-facing response changes.
- Default highlight includes pre/post buffers.
- AI window cutting core frames fails.
- AI window cutting available tail fails.
- End-of-video clamp records a pass/warn reason.
- Candidate render writes clip/report/comparison/manifest.
- Accepted highlight copier rejects review-only clips as final AI improvements.
- Accepted highlight copier accepts comparison-backed clips.
- Stable workflow executes a selected highlight candidate.

**Deliver:**

- AI-adjusted highlight candidates with safe timing.
- Final clips keep the shot/goal result and post-event context.

## PR5: Promotion And Rejection Controls

**Purpose:** close the loop from candidate evidence to final output with explicit operator decisions.

**Build:**

- Add promotion/rejection service helpers in `python_backend/football_tracking/api/service.py`.
- Reuse the policy and data model in `python_backend/football_tracking/final_artifact_manifest.py`; do not invent a second promotion contract.
- Add API routes under existing run/AI route patterns:
  - promote candidate with `approval_id`, `candidate_id`, `problem_type`, optional `confirm_warn`, and `operator_note`
  - reject candidate with `approval_id`, `candidate_id`, `problem_type`, and `rejection_reason`
- Update API schemas and generated clients.
- Reject promotion when:
  - no explicit approval id exists
  - candidate is review-only
  - comparison is missing or unavailable
  - comparison failed
  - comparison warned and `confirm_warn` is false
  - candidate type is unsupported
- Write decisions to `final_ai_improvement_artifact_manifest.json`.
- Refresh lifecycle after decisions.
- Add UI controls in `ai-analysis.tsx` or a small focused component if the page is getting too large.
- Keep final delivery page tied to promoted final artifacts, not raw AI reports.

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_candidate_promotion.py python_backend/tests/test_api_service.py -q`
- `python_backend/scripts/export_openapi.py --output lib/api-spec/openapi.yaml`
- `corepack pnpm --filter @workspace/api-spec run codegen`
- `corepack pnpm run typecheck:libs`
- `corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck`
- `$env:PORT='3000'; $env:BASE_PATH='/'; corepack pnpm --filter @workspace/web run build`
- Required cases:
  - pass candidate promotes
  - warn candidate requires explicit confirmation
  - fail/unavailable/review-only/unsupported candidates cannot promote
  - rejection writes reason and updates lifecycle
  - generated client compiles after route/schema changes
  - UI shows promoted/rejected/blocked state without implying automatic mutation

**Deliver:**

- Human-safe finalization controls.
- A final output can be traced to comparison-backed candidate evidence.
- API and frontend clients stay in sync.

## PR6: Residual AI Prompt And Model Hardening

**Purpose:** harden only the residual AI contract gaps after verifying what is already covered on `main`.

**Build:**

- Update `python_backend/football_tracking/ai_improvement.py`.
- Update `python_backend/football_tracking/ai_visual_review.py` only if packet-level prompt wording needs the same contract.
- Use the PR0 contract audit to decide whether any code change is needed.
- Do not rebuild already-landed guardrails for provenance, `2079` partial coverage, camera tracking-first, or highlight tail preservation.
- Add only missing residual fields or checks discovered by inspection, for example:
  - explicit `coverage_ratio` if absent and needed by UI/quality gate
  - explicit `sampled_subwindows` if absent and useful for long-window operator review
  - clearer model policy fields if stronger-model routing is not visible enough
  - stricter downgrade from executable to review-only for any newly found untraceable candidate shape
- Keep all schema changes backward compatible with existing artifacts.

**Tests:**

- `$env:PYTHONPATH='python_backend'; pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_review_packets.py -q`
- Existing tests for frame `2079` partial coverage, highlight tail, provenance, and camera tracking-first still pass.
- New tests cover only newly added residual fields or policy visibility.
- If no residual code gap is found, this PR becomes a docs/test-audit PR that records the existing coverage and exits with no behavior change.

**Deliver:**

- Residual hardening without duplicating already-landed logic.
- A coverage note listing which long-window, highlight-tail, provenance, and camera-routing checks already exist.

## PR7: Real-Video Verification Pack And Documentation

**Purpose:** prove the full loop on the real match video and capture the workflow for repeat use.

**Build:**

- Run against `python_backend/data/raw5760x144020fps.mp4`.
- Use temporal parallelism for full-video speed.
- Use targeted SAHI/ROI only for approved bounded recovery windows.
- Use configured OpenAI API key through the approved local secret flow when available.
- Produce a dated local evidence folder under `python_backend/outputs/`.
- Do not commit large source videos or generated mp4 outputs unless repo policy changes.
- Produce commit-friendly durable evidence:
  - `docs/operations/real-video-ai-improvement-checklist.md`
  - paths to local output folders
  - SHA256 checksums for final mp4s and key JSON reports
  - contact-sheet screenshots or frame snapshots for review windows if small enough for the repo
  - regeneration commands
  - packet ids, approval ids, candidate ids, comparison statuses, quality gate status, and final manifest summary
- Record:
  - model/provider mode
  - command sequence
  - output directory
  - review packet ids
  - approval ids
  - candidate ids
  - comparison report paths
  - quality-gate status
  - promoted/rejected decisions
  - final `follow_cam.mp4`
  - final highlight clips
- Manually inspect and document:
  - right-bottom corner gap `2049-2544`
  - frame `2079` neighborhood
  - dense noise windows
  - camera-motion spike windows
  - highlight post-event tails
- If no safe highlight candidate exists, record candidate count, rejection reason, and supporting packet/screenshot evidence.
- Update:
  - `README.md`
  - `python_backend/README.md`
  - `docs/operations/ai-improvement-workflow.md`
  - `python_backend/docs/operation-guide.zh.md`
  - `python_backend/docs/operation-guide.en.md`

**Tests:**

- Full backend tests:
  - `$env:PYTHONPATH='python_backend'; pytest python_backend/tests -q`
- API/codegen when schemas change:
  - `python_backend/scripts/export_openapi.py --output lib/api-spec/openapi.yaml`
  - `corepack pnpm --filter @workspace/api-spec run codegen`
  - `corepack pnpm run typecheck:libs`
  - `corepack pnpm -r --filter "./artifacts/**" --filter "./scripts" --if-present run typecheck`
  - `$env:PORT='3000'; $env:BASE_PATH='/'; corepack pnpm --filter @workspace/web run build`
- Real-video acceptance:
  - review packets exist and include media when input video is available
  - each executed lane has candidate manifest, comparison, registry, quality gate, final manifest, and lifecycle evidence
  - final follow-cam renders and has `camera_motion_audit.json`
  - at least one highlight renders when safe event candidates exist
  - if no highlight renders, the no-safe-event reason is explicit
  - final output passes manual visual inspection

**Deliver:**

- Real-video evidence pack.
- Updated operator docs.
- A reproducible summary even though large local videos remain ignored.
- A post-merge local-skill update note. The actual local skill edit is outside the GitHub PR and should be done after this PR merges.

## Post-Merge Local Skill Update

**Purpose:** capture the proven workflow as a reusable local Codex skill after repository docs and real-video evidence are merged.

**Build:**

- Update `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`.
- Add the verified command sequence, temporal-parallelism guidance, targeted SAHI/ROI rule, quality-gate command, model guidance, and real-video inspection checklist.

**Tests:**

- Read the skill after editing and verify it references merged repo docs and current command names.

**Deliver:**

- Local skill update outside GitHub PR scope.

---

## End-To-End Acceptance

- Missing-ball recovery closes only through comparison-backed recovery or full-window evidence-backed `not_visible`.
- Noise cleanup reduces false-positive islands without damaging sustained real-ball signal.
- Follow-cam candidates reduce motion spikes without hiding tracking loss or over-zooming out.
- Highlight clips preserve event core and post-event tail.
- Review-only AI text cannot mutate artifacts and cannot appear as applied improvement.
- Approved execution always requires explicit approval ids.
- Final output is traceable from baseline to AI evidence, approval, candidate, comparison, quality gate, final manifest, and promoted artifact.
- Real-video verification has recorded commands, output paths, checksums, model settings, visual notes, final videos, and highlight clips or no-safe-highlight reasons.

## Managed PR Gates

For every PR:

- Pull latest `origin/main` before branching.
- Use a fresh branch.
- Use a worker agent for implementation.
- Use a separate reviewer agent for plan/code review.
- Run focused tests first, then broader validation.
- Push and open a GitHub PR.
- Wait for CI and Copilot comments.
- Fix valid comments and document non-applicable comments.
- Merge only after checks and review feedback are resolved.
- Delete the merged remote and local branch.

Functional merge gate:

- The PR-specific tests pass locally.
- Any touched API schema regenerates clean clients.
- The PR deliverables are visible in artifacts, UI, docs, or evidence as specified.
- Lifecycle/final manifest semantics do not regress: review-only is never final, final output requires comparison-backed evidence.
