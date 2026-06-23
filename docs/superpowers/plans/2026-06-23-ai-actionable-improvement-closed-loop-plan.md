# AI Actionable Improvement Closed Loop Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `managed-pr-development` for the PR program. Each PR must use a fresh branch from latest `main`, a worker agent, a spec-review agent, a code-quality review agent, focused tests, GitHub checks, Copilot comment triage, merge, and branch cleanup.

**Goal:** turn AI from a reviewer into a bounded improvement operator for missing-ball recovery, dense-noise cleanup, follow-cam stabilization, and highlight boundary tuning.

**Architecture:** keep deterministic tracking artifacts as the source of truth. AI can only propose evidence-backed actions. Explicit approval produces candidate artifacts. Candidate artifacts are re-audited, compared to baseline, and promoted only through a final artifact manifest.

**Tech Stack:** Python backend, pytest, current tracking artifacts, review packets, visual review, AI improvement reports, approval artifacts, camera motion audit, highlight candidates, stable workflow runner, FastAPI service layer, React operator UI, and managed PR delivery.

---

## Requirement Summary

This is an AI improvement requirement, not an AI audit requirement.

The system should support these operator-visible cases:

1. Missing match ball: AI helps find the ball or explains that it is not visible, with packet or visual evidence.
2. Too much noise: AI classifies false positives and recommends bounded suppression or rerun strategy.
3. Follow-cam too unstable: AI uses camera motion metrics and track context to decide whether tracking or camera settings need work.
4. Highlight clips: default pre/post buffers are applied, and AI may adjust start/end frames without cutting off the actual shot or result.

The key safety rule is unchanged: no AI report, approval file, rerender plan, or comparison report should mutate final output just by existing. Apply behavior must require explicit approval ids or explicit API actions.

## Current State

Already available or partially available:

- Ball audit, AI review triggers, review packets, and visual review artifacts.
- Run-level `ai_improvement_report.json`.
- Prompt validation for whole-window missing-ball coverage, ROI provenance, `not_visible` evidence, noise tags, camera action routing, and highlight tail preservation.
- Approval artifact generation with explicit selected improvement ids.
- `camera_motion_audit.json` and follow-cam report references.
- Highlight candidates with core window and tail policy.
- Stable workflow runner and quality gate scaffolding.
- Candidate comparison and final manifest work is in progress on the current branch.

Important gaps:

- AI-located ROI is validated and approvable, but not yet a first-class executable recovery path.
- Candidate outputs are not consistently re-audited before final promotion.
- Dense-noise, follow-cam, and highlight candidates do not yet have complete comparison modules.
- Final output selection is not yet a single stable manifest across tracking video, follow-cam video, and highlight clips.
- Real-video validation should explicitly cover the frame-2079/right-bottom lost-ball failure class.
- The current branch already contains comparison/manifest work. The first delivery step must finish or repair that branch, not rebuild a parallel contract.

## Non-Negotiable Design Rules

- Full-video speed should prefer temporal chunks.
- Broad spatial split or full-video SAHI is not the default because it increases false positives.
- Spatial/SAHI recovery is allowed for bounded approved windows or ROI reruns.
- Review-only and improvement-only stages must preserve baseline track hashes.
- Candidate promotion must use `pass`, `warn`, `fail`, and `unavailable`.
- `warn` promotion requires a real consumed human confirmation, not only a flag on the final artifact.
- A missing comparison or invalid comparison cannot silently promote a candidate.
- Stronger models should be used for hard run-level improvement and recovery decisions; smaller models are acceptable for dry-run smoke checks or low-risk tagging.

## Shared Comparison Payload Contract

Every comparison module must write the same minimum shape:

```json
{
  "schema_version": "1.0",
  "generated_at": "...",
  "problem_type": "missing_ball",
  "baseline": {"role": "baseline", "path": "...", "metrics": {}},
  "candidate": {"role": "candidate", "id": "candidate-001", "path": "...", "metrics": {}},
  "approval": {"approval_id": "approval_001", "approved_action": "localize_ball_roi"},
  "metrics": {},
  "checks": [
    {
      "name": "lost_gap_reduced",
      "status": "pass",
      "baseline_value": 496,
      "candidate_value": 42,
      "reason": "candidate reduces the sustained lost gap"
    }
  ],
  "summary": {
    "status": "pass",
    "primary_reason": "candidate improves the approved problem",
    "regression_count": 0,
    "improvement_count": 1
  },
  "promotion_eligible": true,
  "requires_human_confirmation": false
}
```

Status precedence is `fail > unavailable > warn > pass`. Summary status must be derived from `checks`; a summary/check mismatch is invalid and should fail or become unavailable.

Warning promotion requires a consumed human confirmation, not only `requires_human_confirmation: true` on the final artifact. The consumed approval entry must include at least:

```json
{
  "approval_id": "approval_123",
  "candidate_id": "candidate-warn",
  "approval_type": "human_confirmation",
  "comparison_report": "noise_improvement_comparison.json",
  "approved_by": "operator",
  "approved_at": "..."
}
```

Final outputs are selected only by `final_ai_improvement_artifact_manifest.json`. No approval file, AI report, rerender plan, comparison report, or media file can apply or promote output by presence alone.

## Regression Fixture Set

The plan relies on machine-readable fixtures, not only prose examples:

- `right_bottom_gap_2049_2544.json`: lost gap `2049-2544`, key frame `2079`, source size `5760x1440`, expected region `right_bottom`, required packet coverage labels `start`, `middle`, `end`, and `tail`, or explicit uncovered ranges.
- `full_video_spatial_split_noise.json`: candidate provenance marks full-video spatial split or full-video SAHI, plus short false-positive islands and recall delta.
- `camera_motion_regression.json`: baseline and candidate camera audit summaries with review-event, pan-step, acceleration, and zoom metrics.
- `highlight_tail_boundary.json`: event core window, default pre/post buffer, required tail, source-end clamp, and rendered window.

These fixtures should be introduced in the PR that first uses each one.

## PR 0: Reconcile Current Comparison And Promotion Branch

**Purpose:** finish the shared baseline/candidate/final contract currently in progress so later feature PRs have one promotion language.

**Do:**

- Finish or repair the current `feat/ai-candidate-comparison-contract` work. If the work must be recreated, port the exact intended deltas rather than building a second parallel contract.
- Finish `python_backend/football_tracking/ai_candidate_comparison.py`.
- Finish `python_backend/football_tracking/final_artifact_manifest.py`.
- Make comparison checks mandatory and derive summary status from checks.
- Prevent unsafe path names in report writers.
- Aggregate duplicate comparison reports by worst status.
- Reject final artifacts that have no `candidate_id`.
- Require consumed human confirmation for `warn` candidate promotion.
- Teach the quality gate to read comparison reports and final manifests safely.
- Make the no-comparison behavior candidate-aware: no comparison reports can remain backward-compatible only when there are no candidates or final selections. If a manifest has candidates/final selections and comparison reports are missing, invalid, or outside the output directory, status must be fail or unavailable, not pass.
- Update `docs/operations/ai-improvement-workflow.md` with exact promotion semantics.

**Tests:**

- `python_backend/tests/test_ai_candidate_comparison.py`
- `python_backend/tests/test_final_artifact_manifest.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- Cases: empty checks, summary/check mismatch, duplicate comparison reports, missing candidate id, fail followed by pass, unavailable reports, warn without consumed confirmation, path traversal names, missing manifest report.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `*_comparison.json` contract.
- `final_ai_improvement_artifact_manifest.json` writer.
- Quality-gate summary for comparison statuses.

## PR 1: Executable Missing-Ball ROI Recovery

**Purpose:** make approved AI ball-localization suggestions executable as bounded recovery attempts.

**Do:**

- Extend `python_backend/football_tracking/high_recall_windows.py` to accept approved `localize_ball_roi` actions with frame bounds, ROI, and packet or visual provenance.
- Keep approved `targeted_rerun` behavior intact.
- Ensure ROI reruns honor max frame budgets and never expand into full-video SAHI.
- Improve `python_backend/football_tracking/review_packets.py` so long gaps produce start, middle, end, and tail coverage or report uncovered tail windows.
- Add or finish `python_backend/football_tracking/missing_ball_recovery_comparison.py`.
- Compare baseline and candidate using lost-gap reduction, sustained recovered frames, new short false-positive islands, and provenance.
- Add `right_bottom_gap_2049_2544.json` with source size `5760x1440`, key frame `2079`, expected `right_bottom` region, and required packet coverage labels.

**Tests:**

- `python_backend/tests/test_high_recall_windows.py`
- `python_backend/tests/test_review_packets.py`
- `python_backend/tests/test_high_recall_reconcile.py`
- New or updated tests for `missing_ball_recovery_comparison.py`.
- Cases: valid bounded ROI creates executable recovery, missing ROI/frame/provenance rejects, unbounded SAHI rejects, frame-2079/right-bottom long gap receives start/middle/end/tail coverage or exact uncovered ranges, short noisy islands fail comparison, parent hashes stay unchanged.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py python_backend/tests/test_review_packets.py python_backend/tests/test_high_recall_reconcile.py python_backend/tests/test_missing_ball_recovery_comparison.py python_backend/tests/test_ai_candidate_comparison.py -q
git diff --check
```

**Deliver:**

- Executable `localize_ball_roi` recovery input.
- `missing_ball_recovery_comparison.json`.
- A gate that prevents the frame-2079/right-bottom failure class from passing silently.

## PR 2: Dense-Noise AI Improvement And Comparison

**Purpose:** make AI useful when detector or split recognition creates too many false positives.

**Do:**

- Add `python_backend/football_tracking/noise_improvement.py`.
- Summarize dense-noise windows from ball audit, review triggers, review packets, and visual review.
- Normalize false-positive classes: `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `sideline_confusion`, `advertising_board`, `wall_background_drift`, `unknown_false_positive`, and `unknown`.
- Require bounded noise actions with frame windows and accepted classes.
- Keep config patches advisory until explicit apply.
- Write `noise_improvement_comparison.json` using the shared comparison contract.
- Compare false-positive island count, long-gap coverage, sustained track quality, and any recall regression.
- Add `full_video_spatial_split_noise.json` and fail candidates whose provenance says full-video spatial split or full-video SAHI unless the candidate is tied to explicit bounded approval windows.
- Define the recall/noise tradeoff threshold in code. Extra short false-positive islands should fail unless lost-gap coverage improves beyond the configured threshold.

**Tests:**

- `python_backend/tests/test_noise_improvement.py`
- `python_backend/tests/test_ai_improvement.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- Cases: dense-noise context enters prompt, missing false-positive class rejects, unbounded action rejects, full-video spatial split candidate without bounded approval fails, worse false-positive islands fail, less noise with preserved recall passes, bounded recall improvement can justify limited noise only past the threshold.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_noise_improvement.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- Noise suggestion contract.
- `noise_improvement_comparison.json`.
- Documented temporal chunk first policy.

## PR 3: Follow-Cam Improvement Candidate Comparison

**Purpose:** turn camera motion audit findings into explicit follow-cam or tracking improvement candidates.

**Do:**

- Add `python_backend/football_tracking/follow_cam_comparison.py`.
- Keep existing routing: Lost/Predicted track context means tracking rerun first; stable Detected track with camera spike may mean follow-cam tuning.
- Execute follow-cam rerender only through explicit approval/action.
- After rerender, run `camera_motion_audit.json` on the candidate.
- Compare review event count, max pan step, p95 pan step, acceleration, zoom jumps, and final video presence.
- Prevent promotion when the candidate video exists but the camera metrics regress.
- Add `camera_motion_regression.json` fixture.
- Add workflow-level tests proving plan presence alone does not render and only explicit approval/action can render or plan.

**Tests:**

- `python_backend/tests/test_follow_cam_comparison.py`
- `python_backend/tests/test_follow_cam.py`
- `python_backend/tests/test_camera_motion_audit.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- Cases: plan presence alone does not render, explicit selected action can render/plan, camera regression fails, camera improvement passes, warning candidate needs consumed human confirmation.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam_comparison.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `follow_cam_comparison.json`.
- Promotion rule for final follow-cam video.
- Clear distinction between tracking failure and camera tuning failure.

## PR 4: Highlight Boundary Candidate Comparison

**Purpose:** ensure goal and shot clips include useful pre/post context and do not cut off the result tail.

**Do:**

- Add `python_backend/football_tracking/highlight_comparison.py`.
- Ensure approved highlight renders write a summary with candidate id, approval id, requested window, rendered window, core window, source-end clamp, and tail status.
- Keep default pre/post buffer behavior.
- Candidate passes only if rendered output includes core action and required tail, unless clamped by source video end.
- Support multiple independent highlight candidates and per-clip promotion.
- Add `highlight_tail_boundary.json` fixture.

**Tests:**

- `python_backend/tests/test_highlight_comparison.py`
- `python_backend/tests/test_highlights.py`
- `python_backend/tests/test_accepted_highlights.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- Cases: default buffer present, AI cannot trim core window, AI cannot cut required tail, source end clamp handled, final manifest includes only promoted clips.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_highlight_comparison.py python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `highlight_comparison.json`.
- Tail-safe highlight render summary.
- Promoted highlight clips in final manifest.

## PR 5: Post-Candidate AI Review And Final Promotion Gate

**Purpose:** close the loop after domain candidates exist, so AI suggestions are not trusted without candidate evidence.

**Do:**

- Extend `python_backend/scripts/run_stable_ai_improvement_workflow.py` with candidate stages after comparison modules exist.
- Refresh candidate metrics and artifacts when a candidate output directory exists.
- Run candidate audits for ball, noise, camera, and highlights when artifacts exist.
- Build candidate review packets for affected windows.
- Feed baseline and candidate summaries into AI compare mode, not raw paths.
- Ensure candidate AI can confirm, reject, or request human review, but cannot auto-promote.
- Collect `missing_ball_recovery_comparison.json`, `noise_improvement_comparison.json`, `follow_cam_comparison.json`, and `highlight_comparison.json`.
- Record baseline, candidate, final, consumed approvals, comparisons, and promotion decisions in the workflow report.
- Add model selection metadata: `model_name`, `model_selection_source`, `model_tier`, and `hard_case_model_policy`. In real hard-recovery/camera/highlight cases, `mini` or `unknown` model tier should warn or fail according to mode instead of producing a clean pass.

**Tests:**

- `python_backend/tests/test_stable_ai_improvement_workflow.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`
- `python_backend/tests/test_ai_improvement.py` if compare prompt/context changes.
- Cases: dry-run provider-safe candidate stages, missing candidate artifacts become `unavailable`, manifest with candidates and missing comparisons does not pass, candidate quality-gate fail prevents promotion, AI reject prevents promotion, approval ids remain explicit, hard case with mini/unknown model tier is not a clean pass.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_final_artifact_manifest.py -q
git diff --check
```

**Deliver:**

- Candidate-aware workflow report.
- Candidate review packets.
- Final promotion decision that can reject a bad AI suggestion.
- Final manifest as the only output selection mechanism.

## PR 6: Backend API Artifact Contract

**Purpose:** make the loop usable without raw JSON editing while preserving explicit approval semantics.

**Do:**

- Extend backend artifact listing for workflow report, quality gate, comparisons, final manifest, camera audit, rerender plan, and highlight reports.
- Add grouped AI improvement items for missing ball, noise, camera motion, and highlights.
- Expose frame windows, evidence ids, failure tags, confidence, recommended action, approval status, consumed approvals, comparison status, and final promotion status.
- Add backend helpers or endpoints for explicit approve/reject operations.
- Show missing/unavailable artifacts without crashing.

**Tests:**

- Backend service/API tests for artifact listing, grouped items, explicit approvals, unknown ids, candidate comparison statuses, final promotion status, and unavailable artifacts.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py python_backend/tests/test_metrics.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- Explicit approval API contract.
- Artifact visibility for final output review.

## PR 7: Operator UI For Explicit AI Improvement

**Purpose:** give the operator a clear approval surface for the four AI improvement categories.

**Do:**

- Show grouped improvement items for missing ball, noise, camera motion, and highlights.
- Show evidence ids, frame windows, false-positive class, confidence, recommended action, model tier, approval status, comparison status, and final promotion status.
- Add explicit approve/reject controls.
- Make controls submit exact action ids to the backend contract from PR 6.
- Show missing/unavailable artifact states.
- Avoid any UI affordance that implies approval files, AI reports, plans, or comparison reports auto-apply by presence.

**Tests:**

- Frontend tests or existing UI test equivalent for grouped items, pass/warn/fail/unavailable states, approve/reject calls, exact action id submission, and no auto-apply path.

**Validation:**

```powershell
pnpm test
pnpm lint
git diff --check
```

**Deliver:**

- Operator-facing AI improvement screen.
- Explicit approval controls for all four cases.
- Visible final artifacts and quality status.

## PR 8: Real-Video Full Workflow, Docs, And Skill Capture

**Purpose:** prove the complete loop on the real video and capture the repeatable operating procedure.

**Do:**

- Run the full workflow against the current real video/output.
- Use a stronger model for hard recovery/camera/highlight improvement when API key is available.
- If provider is unavailable, run artifact-only mode and mark AI-dependent checks as unavailable or warn, not pass.
- Inspect the frame-2079/right-bottom failure class.
- Verify final tracking video, final follow-cam video, quality-gate report, final manifest, and generated highlights.
- Update `README.md` and `docs/operations/ai-improvement-workflow.md`.
- Update the local `football-tracking-real-video-tuning` skill only after explicit user confirmation.

**Tests:**

- Focused unit and integration tests from PR 1 to PR 7.
- Real-video smoke produces `stable_ai_improvement_workflow_report.json`.
- Real-video smoke produces `ai_improvement_quality_gate.json`.
- Real-video smoke produces `final_ai_improvement_artifact_manifest.json`.
- Manual visual spot check covers frame 2079/right-bottom and at least one final highlight tail.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --parallel-mode temporal `
  --mode real
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- Final real-video validation record.
- Final selected tracking/follow-cam/highlight artifacts.
- Updated README and operator documentation.
- User-approved skill update.
- Managed PR final report with PRs, branches, checks, comments handled, merge status, branch cleanup, artifacts, and residual risk.

## End-To-End Acceptance Criteria

- AI suggestions are evidence-backed, not self-certified.
- Long missing-ball windows have full-window/tail coverage or explicit uncovered explanations.
- Frame-2079/right-bottom lost-ball class cannot pass silently.
- Approved `localize_ball_roi` can run as a bounded recovery candidate.
- Noise actions are bounded and false-positive tagged.
- Candidate output is re-audited and compared before promotion.
- Bad candidates are rejected even when AI originally suggested them.
- Follow-cam output is compared by camera-motion metrics, not by video existence.
- Highlight clips preserve core action and shot/result tail.
- Baseline track hashes stay stable during review/improvement-only stages.
- Approval artifacts remain inert unless explicit ids are consumed.
- Final outputs are described by one machine-readable final manifest.

## Managed PR Execution Gates

For every PR:

1. Merge latest `origin/main` into local `main`.
2. Create a fresh task branch.
3. Assign a worker agent with precise file ownership.
4. Require TDD or focused regression-first tests.
5. Run a spec-review agent.
6. Run a code-quality review agent.
7. Fix all valid Critical and Important findings.
8. Run focused local validation.
9. Push and open a GitHub PR.
10. Wait for GitHub checks and Copilot comments.
11. Fix confirmed remote feedback.
12. Merge only after checks and valid comments are resolved.
13. Delete merged local and remote branches.
14. Update local `main` before starting the next PR.
