# AI Improvement Remaining Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `managed-pr-development` for the full PR program. For each implementation PR, use `superpowers:subagent-driven-development` or `superpowers:executing-plans`, keep TDD active, and run separate spec/code review agents before publishing.

**Goal:** finish the remaining AI-improvement loop so AI can help recover missing balls, reduce false positives, stabilize follow-cam output, and tune highlight clips, then prove whether each approved candidate should become the final output.

**Architecture:** the current tracker artifacts remain the baseline. AI produces evidence-backed suggestions only. Explicit approval creates a bounded candidate run/render. Candidate artifacts are re-audited and compared to baseline. A promotion gate decides whether the candidate becomes the final output, stays warning-only, or is rejected.

**Tech Stack:** Python backend, FastAPI service layer, React operator UI, pytest, existing AI provider routing, review packets, visual review, AI improvement report, approved actions, high-recall windows, camera motion audit, event/highlight artifacts, stable workflow runner, and managed PR workflow.

---

## Requirement Restatement

This work is **AI improvement**, not just AI review.

The loop must support four practical cases:

- **Ball missing:** AI inspects packet/visual evidence, proposes a bounded ROI or explicitly says `not_visible`. Long windows like the right-bottom corner sequence around frame 2079 must not pass with only early-window coverage.
- **Noise too high:** AI classifies likely false-positive source and suggests bounded suppression or rerun strategy. Broad spatial slicing/SAHI is not the default full-video speed path because it adds too much noise.
- **Follow-cam too jumpy:** AI uses camera motion audit plus nearby track status to choose between tracking rerun and follow-cam rerender/tuning.
- **Highlight clips:** default clips keep pre/post buffer, and AI may adjust windows only if the shot/result tail remains intact.

Preferred speed strategy:

- Full-video speed: temporal chunks.
- Spatial slicing/SAHI: only bounded approved recovery windows or ROI reruns unless later benchmark data proves otherwise.

## Current Baseline, Do Not Rebuild

Already present on current `main`:

- Prompt/provenance hardening for AI improvement.
- Packet-level AI visual review and run-level AI improvement reports.
- Approval safety: `ai_improvement_approved_actions.json` does not execute by presence.
- Approved `targeted_rerun` child-run path in the API/service layer.
- Approved targeted high-recall windows in `high_recall_windows.py`.
- ROI provenance policy for AI suggestions.
- Camera motion audit and AI camera-action routing.
- `follow_cam_rerender_plan.json` generation.
- Highlight event candidates with `core_window`, `render_window`, and `buffer_policy`.
- Highlight tail checks in the quality gate.
- Operator AI improvement UI and approval surfaces.
- Stable workflow runner with dry-run/artifact/real modes and explicit approval selection.
- Quality gate with track hash, long lost gap, noise, camera, highlight tail, and model routing checks.

Important current gap:

- `targeted_rerun` can already become an approved child run; `localize_ball_roi` is validated and approvable, but it is not yet a first-class executable bounded recovery window.
- Candidate output is not yet consistently compared, accepted, rejected, or promoted to a final artifact set across ball recovery, noise, follow-cam, and highlights.
- Candidate output is not yet consistently re-audited after apply/render, so AI can suggest a fix without a closed feedback loop proving the fix helped.

## Shared Output Vocabulary

All remaining PRs should use these artifact roles:

- `baseline`: original completed tracking output directory.
- `candidate`: explicitly approved child run or render output.
- `final`: selected output after comparison/promotion.

Shared candidate status values:

- `pass`: candidate improves or preserves required metrics and can be promoted.
- `warn`: candidate is usable only with documented caveats or human confirmation.
- `fail`: candidate regresses or does not address the approved problem.
- `unavailable`: required optional artifact was not requested or cannot be produced in the current mode.

Minimum comparison payload:

```json
{
  "schema_version": "1.0",
  "generated_at": "...",
  "problem_type": "missing_ball",
  "baseline": {"output_dir": "...", "metrics": {}},
  "candidate": {"output_dir": "...", "metrics": {}},
  "approval": {"approval_id": "...", "approved_action": "..."},
  "summary": {
    "status": "pass",
    "primary_reason": "...",
    "regression_count": 0,
    "improvement_count": 1
  },
  "checks": {}
}
```

## PR A: Candidate Comparison And Promotion Contract

**Purpose:** create one shared schema and helper layer so every later candidate uses the same pass/warn/fail and promotion semantics.

**Files:**

- Create: `python_backend/football_tracking/ai_candidate_comparison.py`
- Create: `python_backend/football_tracking/final_artifact_manifest.py`
- Create: `python_backend/tests/test_ai_candidate_comparison.py`
- Create: `python_backend/tests/test_final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify: `docs/operations/ai-improvement-workflow.md`

**Build:**

- Define baseline/candidate/final roles.
- Add shared `build_candidate_comparison(...)` helpers for status aggregation and regression counting.
- Add a promotion helper that writes `final_ai_improvement_artifact_manifest.json`.
- Promotion must record source baseline, candidate run/render, consumed approval ids, comparison reports, quality gate status, final video paths, and rejected candidates.
- Quality gate should consume candidate comparison summaries without each feature inventing its own status semantics.
- No helper should mutate baseline track files.

**Tests:**

- Golden JSON for pass/warn/fail/unavailable comparison reports.
- Promotion manifest includes baseline, candidate, final, approvals, quality gate, and videos/clips.
- A failed candidate is recorded but not promoted.
- A warning candidate requires `requires_human_confirmation: true`.
- Track hashes are unchanged by comparison-only helpers.
- Quality gate summarizes candidate comparison reports consistently.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Shared comparison schema.
- Shared final artifact manifest writer.
- Quality-gate hook for candidate comparison summaries.

## PR B: `localize_ball_roi` Recovery Executor And Long-Gap Tail Coverage

**Purpose:** make AI-located ball ROIs executable and ensure long missing-ball windows are reviewed across the whole span, including late frames.

**Files:**

- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/high_recall_reconcile.py`
- Modify: `python_backend/football_tracking/api/service.py` only if approved-child-run selection needs service wiring.
- Create or modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Modify: `python_backend/tests/test_high_recall_windows.py`
- Modify: `python_backend/tests/test_review_packets.py`
- Modify: `python_backend/tests/test_high_recall_reconcile.py`
- Add service/API tests only if service behavior changes.

**Build:**

- Treat approved `localize_ball_roi` as an executable bounded recovery input when it has frame bounds, ROI, and packet/visual provenance.
- Keep approved `targeted_rerun` behavior unchanged.
- Ensure executable ROI reruns still honor max-frame budget and never enable broad full-video SAHI.
- Strengthen long lost-gap packet coverage so a large gap can create or reference start/middle/end/tail diagnostic packets, not only a single early-biased packet.
- Ensure the frame-2079/right-bottom case has packet coverage that includes the late/right-bottom sequence or explicitly reports uncovered tail coverage.
- Write `missing_ball_recovery_comparison.json` using the shared PR A schema.
- Compare baseline versus candidate using lost-gap length, sustained recovered frames, new short false-positive islands, and provenance.

**Tests:**

- Approved `localize_ball_roi` with bounded frames and valid provenance creates executable high-recall windows.
- `localize_ball_roi` without ROI, frame bounds, or provenance is rejected.
- Existing approved `targeted_rerun` tests still pass.
- Long lost gap 2049-2544 containing frame 2079 has diagnostic coverage for the tail or reports the uncovered tail explicitly.
- Candidate that reduces the long gap with sustained detections passes.
- Candidate that only creates short noisy islands fails.
- Parent baseline hashes remain unchanged.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py python_backend/tests/test_review_packets.py python_backend/tests/test_high_recall_reconcile.py python_backend/tests/test_ai_candidate_comparison.py -q
git diff --check
```

**Deliverables:**

- Executable `localize_ball_roi` recovery path.
- Tail-aware long-gap packet coverage.
- `missing_ball_recovery_comparison.json`.

## PR C: Post-Candidate Review Loop

**Purpose:** close the AI feedback loop after a candidate is produced, instead of trusting the first AI suggestion.

**Files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_improvement.py` only if candidate context must be added to prompts.
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Modify: `python_backend/tests/test_ai_improvement.py` only if prompt/context changes.
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Add optional post-candidate stages to the stable workflow:
  - refresh candidate metrics;
  - run candidate ball/camera/highlight audits when artifacts exist;
  - build affected review packets for the candidate;
  - run quality gate with `candidate_output_dir`;
  - optionally run AI improvement in compare mode.
- Compare-mode AI prompt must receive baseline and candidate summaries, not raw paths.
- Candidate AI may confirm, reject, or request human review, but cannot auto-promote output.
- Workflow report must show baseline review, candidate review, comparison, promotion decision, and exact approval ids consumed.
- Dry-run and artifact-only modes must remain provider-safe.

**Tests:**

- Dry-run records post-candidate stages without provider calls.
- Candidate output missing optional artifacts reports `unavailable`, not crash.
- Candidate quality gate fail prevents promotion.
- Candidate AI `reject` or `needs_human_review` prevents auto-promotion.
- Explicit approval ids remain the only way to create candidate work.
- Report records baseline/candidate/final roles and consumed approvals.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_ai_candidate_comparison.py -q
git diff --check
```

**Deliverables:**

- Post-candidate review stages in the stable workflow.
- Candidate-aware quality-gate summary.
- Closed-loop report that can reject a bad candidate.

## PR D: Dense Noise Candidate Comparison

**Purpose:** make AI useful when detector/slicing output produces too many false positives, while keeping temporal chunks as the default speed path.

**Files:**

- Create: `python_backend/football_tracking/noise_improvement.py`
- Create: `python_backend/tests/test_noise_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify: `docs/operations/ai-improvement-workflow.md`

**Build:**

- Summarize dense-noise windows from `ball_audit.json`, `ai_review_triggers.json`, `review_packets.json`, and visual-review results.
- Normalize false-positive classes: `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `sideline_confusion`, `advertising_board`, `wall_background_drift`, `unknown_false_positive`.
- Require bounded noise actions with frame windows and accepted false-positive classes.
- Write `noise_improvement_comparison.json` using the shared schema.
- Compare candidate versus baseline using false-positive island count, lost-gap coverage, and sustained valid track quality.
- Candidate may pass only when noise improves without meaningful recall regression, or recall improvement clearly outweighs bounded noise increase.
- Workflow docs must keep the strategy explicit: temporal chunks first, bounded spatial recovery second.

**Tests:**

- Dense-noise context enters AI improvement prompt.
- Noise action without false-positive class is rejected.
- Unbounded noise action is rejected.
- Candidate with increased false-positive islands and no recall gain fails.
- Candidate with lower noise and unchanged/better recall passes.
- Workflow strategy still forbids broad full-video SAHI by default.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_noise_improvement.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_stable_ai_improvement_workflow.py -q
git diff --check
```

**Deliverables:**

- `noise_improvement_comparison.json`.
- AI noise suggestion contract and comparator.
- Documented temporal-vs-spatial split policy.

## PR E: Follow-Cam Candidate Rerender Comparison

**Purpose:** finish the follow-cam improvement loop by comparing rerendered candidates and deciding whether a new video is actually better.

**Files:**

- Modify: `python_backend/football_tracking/follow_cam.py` only if plan consumption needs a small compatibility hook.
- Modify: `python_backend/football_tracking/api/service.py` only if explicit follow-cam render/promotion needs service wiring.
- Create or modify: `python_backend/football_tracking/follow_cam_comparison.py`
- Create or modify: `python_backend/tests/test_follow_cam_comparison.py`
- Modify: `python_backend/tests/test_follow_cam.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Reuse existing AI routing: lost/predicted track context means tracking rerun before follow-cam tuning; stable track with camera spike may use follow-cam tuning.
- Execute follow-cam rerender only through explicit approval/action.
- After rerender, run `camera_motion_audit.json` on the candidate.
- Write `follow_cam_comparison.json` using shared schema.
- Compare baseline versus candidate using `review_event_count`, `max_pan_step_px`, `p95_pan_step_px`, zoom jumps, and final video presence.
- Candidate fail prevents promotion even if the video file exists.

**Tests:**

- Existing camera-routing tests still pass.
- Plan file presence alone does not render.
- Explicit approved action renders/plans a candidate only for selected action id.
- Candidate camera audit regression fails.
- Candidate camera audit improvement passes.
- Final manifest records chosen follow-cam video only when comparison passes or is human-confirmed warning.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam.py python_backend/tests/test_follow_cam_comparison.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- `follow_cam_comparison.json`.
- Candidate follow-cam promotion rule.
- Final follow-cam video path in final manifest.

## PR F: Highlight Candidate Render Summary And Comparison

**Purpose:** finish the highlight loop so generated goal/shot clips are tail-safe and promotion-ready.

**Files:**

- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/api/service.py` only if explicit render summary wiring is needed.
- Create or modify: `python_backend/football_tracking/highlight_comparison.py`
- Create or modify: `python_backend/tests/test_highlight_comparison.py`
- Modify: `python_backend/tests/test_highlights.py`
- Modify: `python_backend/tests/test_accepted_highlights.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Do not rebuild core/tail fundamentals already present.
- Ensure approved highlight render writes a summary with source candidate id, approval id, requested window, rendered window, source-end clamp, and tail status.
- Write `highlight_comparison.json` using shared schema.
- Candidate passes only when rendered window includes the core action and required tail, unless clamped by source-video end.
- Multiple highlight candidates should each have independent comparison and promotion status.

**Tests:**

- Existing core/tail tests still pass.
- Render summary records approved action id and actual rendered window.
- Tail clipping fails unless source-video end clamps it.
- Source-end clamp warns/passes according to policy.
- Final manifest includes only promoted highlight clips.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_highlights.py python_backend/tests/test_accepted_highlights.py python_backend/tests/test_highlight_comparison.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- `highlight_comparison.json`.
- Tail-safe highlight render summaries.
- Promoted highlight clip list in final manifest.

## PR G: Real-Video Stable Output Run And Final Promotion

**Purpose:** prove the end-to-end flow on the real video/output and produce stable final outputs.

**Files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/tests/test_final_artifact_manifest.py`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `README.md`

**Build:**

- Wire the workflow to collect comparison reports from PR B-F.
- Add explicit promotion options:
  - promote pass candidates automatically within workflow mode;
  - require human confirmation for warn candidates;
  - never promote fail candidates.
- Run real-video workflow with a strong run-level improvement model when API key is available.
- If provider is unavailable, run artifact-only mode and mark AI-dependent checks as unavailable/warn, not pass.
- Inspect/report the frame-2079/right-bottom class in the final manifest.
- Record generated final tracking/follow-cam/highlight paths.

**Tests:**

- Dry-run final promotion records intended decisions without rendering.
- Artifact-only final promotion handles unavailable provider safely.
- Failed comparison prevents promotion.
- Warning comparison requires human confirmation.
- Final manifest records problem-specific comparison reports and selected final artifacts.

**Real-Video Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --parallel-mode temporal `
  --mode real
```

Also run focused tests:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- `final_ai_improvement_artifact_manifest.json`.
- Final selected tracking/follow-cam/highlight artifacts.
- Real-video quality status with the frame-2079 failure class explicitly reported.

## PR H: Docs And Local Skill Capture

**Purpose:** make the process repeatable by the user and future agents.

**Files:**

- Modify: `README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `docs/superpowers/plans/2026-06-23-ai-improvement-execution-plan.md`
- After repository PRs merge and after explicit user confirmation, update local skill: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- Explain AI review versus AI improvement.
- Document the baseline/candidate/final artifact model.
- Document exact commands for dry-run, artifact-only, real mode, approval selection, candidate comparison, promotion, follow-cam rerender, and highlight render.
- Document model routing:
  - `OPENAI_IMPROVEMENT_MODEL` or explicit `--model` for hard run-level decisions;
  - smaller chat/visual model only for low-risk tagging or smoke checks.
- Document temporal chunk strategy and why broad spatial slicing is not the default.
- Document the frame-2079/right-bottom lost-ball pattern and expected gate behavior.
- Capture skill notes after the verified real-video run.

**Tests:**

- Run documented focused commands.
- Run `git diff --check`.
- Manually scan docs for any claim that AI report or approval-file presence auto-mutates output.

**Deliverables:**

- Updated README and operator docs.
- Updated local skill after confirmation.
- Final managed PR report listing all PRs, branches, checks, Copilot comments handled, merges, branch cleanup, artifacts, and remaining risks.

## End-To-End Acceptance Criteria

- AI suggestions are evidence-backed and cannot invent ROI without packet/visual provenance.
- Long missing-ball windows include whole-window/tail coverage or explicit uncovered explanations.
- The frame-2079/right-bottom class cannot pass silently.
- Approved `localize_ball_roi` can run as bounded recovery.
- Candidate runs/renders are compared before promotion.
- Bad candidates are rejected even if AI originally suggested them.
- Noise suggestions are bounded and false-positive-tagged.
- Temporal chunks remain the default full-video speed strategy.
- Follow-cam final videos pass camera-motion comparison or require human confirmation.
- Highlight clips preserve core action and shot/result tail.
- Review/improvement-only phases preserve baseline track hashes.
- Approval files remain inert unless explicitly consumed.
- Final workflow emits machine-readable pass/warn/fail reports and concrete final artifacts.
