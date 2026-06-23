# AI Improvement Action Loop Development Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** turn the current AI review artifacts into a controlled AI improvement loop that can help recover missing balls, suppress noisy detections, stabilize follow-cam output, and tune highlight boundaries, then prove whether the final output actually improved.

**Architecture:** deterministic tracking artifacts remain the source of truth. AI produces evidence-backed suggestions; explicit operator approval turns selected suggestions into bounded child reruns, follow-cam rerender plans, or highlight renders; comparison reports and quality gates decide whether the result is acceptable.

**Tech Stack:** Python backend modules and scripts, pytest, existing FastAPI/React operator surfaces, existing review packets, visual review, high-recall windows, camera motion audit, event/highlight artifacts, and managed PR workflow.

---

## Requirement Summary

The need is **AI improvement, not only AI audit**. A good result is not "the model says this run is okay"; a good result is "the model proposes bounded fixes, the operator explicitly approves the useful ones, the system applies them without hidden side effects, and the final artifacts prove the output is more stable."

The four primary improvement cases are:

- **Ball missing:** when long gaps or important windows lose the match ball, AI should inspect packet/visual evidence, propose a plausible ROI or say `not_visible` with evidence, and create a bounded recovery action. A right-bottom corner sequence around frame 2079 must not pass silently.
- **Too many noise detections:** when slicing/SAHI or detector settings create extra-ball/foot/shoe/head/ad-board noise, AI should classify the false-positive cause and recommend bounded suppression, config patch, or targeted rerun advice.
- **Follow-cam too jumpy:** AI should use `camera_motion_audit.json` and track context to decide whether instability comes from bad ball tracking or follow-cam tuning, then propose an explicit rerender plan or tracking rerun.
- **Highlight boundaries:** default highlight generation should include pre/post buffers, and AI should be able to adjust start/end frames while preserving the final shot/result tail, especially near the end of a play.

Secondary engineering requirement:

- Prefer **temporal parallelism** for full-video speed. Broad full-video spatial slicing can improve GPU utilization, but it creates too many false positives and should be reserved for approved bounded recovery windows or ROI reruns.

## Current State To Preserve

Already present or recently implemented in the repository:

- `ball_audit.json` and `ai_review_triggers.json` identify suspicious trajectory issues.
- `review_packets.json` can package frame windows for review.
- `ai_visual_review.json` and `ai_improvement_report.json` support AI review/improvement artifacts.
- `high_recall_windows` supports bounded child recovery windows.
- `camera_motion_audit.json` reviews abrupt final follow-cam camera motion.
- `event_candidates.json`, highlight rendering, and highlight reports exist.
- `ai_improvement_quality_gate.json` checks long-gap coverage, approval safety, camera regression, highlight tail preservation, and track hash immutability.
- `stable_ai_improvement_workflow_report.json` is being introduced as the repeatable workflow report.

Safety boundaries that must stay true:

- AI reports are advisory.
- `ai_improvement_approved_actions.json` must never execute anything by file presence alone.
- Review/improvement-only stages must not mutate `ball_track.csv` or `ball_track.cleaned.csv`.
- Config patches, follow-cam rerendering, high-recall child reruns, and highlight renders require explicit approval arguments or API calls.
- Tests should use fixtures, fake clients, and JSON artifacts first; real video is reserved for smoke validation.

## Gate Severity Policy

The quality gate must distinguish missing information from known-bad output.

Must be `fail` in `real` mode and should fail the relevant unit/integration test:

- A long missing-ball gap has no packet/visual coverage.
- A long missing-ball gap has packet coverage but no AI improvement coverage.
- A long missing-ball gap is only partially covered and the uncovered subwindow has no explicit explanation.
- `not_visible` is used without packet/visual evidence.
- An approved missing-ball recovery action is unbounded, uses full-video SAHI, or lacks frame bounds.
- A noise action is unbounded or lacks an accepted false-positive tag.
- A candidate rerun increases false-positive islands beyond the configured threshold without improving lost-gap coverage.
- A follow-cam candidate regresses camera-motion metrics beyond the configured threshold.
- A highlight suggestion or render clips the required post-shot/result tail when source-video length would allow it.
- Track hashes change during review/improvement-only stages.
- An approval action is counted without explicit approval input.

May be `warn` instead of `fail`:

- Optional provider-backed visual review is unavailable in dry-run or artifact-only mode.
- A model/provider field is unavailable in dry-run mode.
- A remaining subwindow is explicitly marked `not_visible` with packet/visual evidence.
- A source-video boundary clamps a highlight tail that otherwise would exceed available frames.
- A comparison artifact is absent in artifact-only mode and the corresponding comparison was not requested.

## Approval Consumption Contract

- Approval files are inert by default. A file named `ai_improvement_approved_actions.json` in the output directory must not trigger child reruns, rerenders, highlight renders, or quality-gate approval credit by its mere presence.
- `--approved-actions-path` is the explicit source of approved action payloads for CLI workflows.
- `--approval-ids` only filters actions from an explicitly supplied approval payload. Unknown IDs, duplicate IDs, or IDs that match no action must fail in non-dry-run execution and must be recorded as warnings in dry-run.
- `--approved-action-id` is only for a single explicit highlight/follow-up action and must not imply all actions in the approval file are approved.
- Reports must record `approval_source`, every consumed action id, every skipped action id, and the reason an action was skipped.

## PR Dependency Matrix

| PR | Depends on | Output maturity |
| --- | --- | --- |
| PR 1 Stable workflow runner | Existing quality gate and AI improvement artifacts | Orchestrates and records stages. Child rerun, follow-cam rerender, and highlight render stages may be no-op/planned until later PRs connect real apply behavior. |
| PR 2 Prompt contract/model routing | PR 1 report shape | Makes AI recommendations stricter without applying changes. |
| PR 3 Missing-ball recovery apply loop | PR 2 prompt contract | Connects approved bounded recovery actions to child recovery plans/runs and comparison. |
| PR 4 Dense-noise suppression loop | PR 2 prompt contract and PR 3 comparison patterns | Adds bounded noise advice and candidate noise regression checks. |
| PR 5 Follow-cam improvement apply loop | Camera motion audit and PR 2 prompt contract | Converts camera review into explicit rerender plans and candidate comparisons. |
| PR 6 Highlight boundary improvement loop | Event/highlight artifacts and PR 2 prompt contract | Adds tail-safe AI suggested windows and explicit highlight rendering. |
| PR 7 API/artifact visibility | Stable artifact schemas from PR 1-6 | Exposes reports and approval payloads through backend surfaces. |
| PR 8 UI approval controls | PR 7 API contract | Gives operators explicit review/approve/reject controls. |
| PR 9 Real-video validation/docs/skill capture | PR 1-8 | Runs the full workflow, records final artifacts, and captures reusable operating knowledge. |

## PR Sequence

### PR 1: Stable AI Improvement Workflow Runner

**Purpose:** create one repeatable command that runs the review/improvement/check sequence against an existing output directory and records what happened.

**Files:**

- Create or finish: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Create or finish: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Modify: `README.md`
- Create or modify: `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Validate `--output-dir` exists before doing any work.
- [ ] Support `--dry-run`, `--mode dry-run|artifact-only|real`, `--model`, `--parallel-mode temporal|none`, `--approved-actions-path`, `--approval-ids`, and `--approved-action-id`.
- [ ] Run stages in this order: metrics/artifacts refresh, `before_review` hash snapshot, review packets, optional visual review, run-level AI improvement, `after_ai_improvement` hash snapshot, approved child rerun planning, follow-cam rerender planning, highlight render planning, AI quality gate.
- [ ] Write `stable_ai_improvement_workflow_report.json` with stage statuses, produced artifacts, explicit approval intent, strategy, warnings, and quality-gate summary.
- [ ] Record the default speed strategy as temporal chunks for full-video speed.
- [ ] Record that broad full-video SAHI is not used by this workflow; SAHI/ROI is only for explicit bounded approved windows.
- [ ] Treat child rerun, follow-cam rerender, and highlight render stages as `planned`/`skipped` orchestration stages until later PRs connect real apply behavior.
- [ ] Enforce the approval consumption contract for `--approved-actions-path`, `--approval-ids`, and `--approved-action-id`.
- [ ] Return nonzero only for non-dry-run `real` mode when the quality gate fails.

**Tests:**

- [ ] Dry run records all planned stages without provider or heavy video work.
- [ ] Missing `--output-dir` fails cleanly.
- [ ] Before/after track hash snapshots are written.
- [ ] Existing approval files are ignored unless passed explicitly.
- [ ] Temporal mode records chunk settings and no broad full-video SAHI.
- [ ] Explicit approved actions produce bounded child-rerun planning.
- [ ] Unknown approval ids fail in non-dry-run execution and warn in dry-run.
- [ ] A 2049-2544 lower-right long-gap fixture containing frame 2079 fails the workflow quality gate when packet/improvement/approval coverage is missing.
- [ ] Workflow report includes quality gate summary and produced artifact names.
- [ ] `real` mode surfaces quality-gate failures.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py -q
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 --input-video python_backend\data\raw5760x144020fps.mp4 --dry-run --parallel-mode temporal
git diff --check
```

**Deliverables:**

- `stable_ai_improvement_workflow_report.json`
- Operator docs for the stable workflow
- README link to the workflow
- Managed PR with review agents, CI/Copilot gate, merge, and branch cleanup

### PR 2: Prompt Contract And Model Routing

**Purpose:** make run-level AI suggestions stricter and more useful so they cover complete problem windows, preserve shot tails, and recommend the right type of fix.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify only if needed: `python_backend/football_tracking/ai_visual_review.py`
- Modify only if needed: `python_backend/tests/test_ai_visual_review.py`
- Modify docs: `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Add prompt rules that long missing-ball suggestions must cover the whole lost-gap window or explain uncovered subwindows.
- [ ] Require ROI/localization actions to include `source_packet_id` or `visual_review_id`.
- [ ] Accept `not_visible` only with evidence that the ball is hidden, off-frame, or visually impossible to identify.
- [ ] Require noise suggestions to include bounded frame windows and false-positive tags such as `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, or `unknown_false_positive`.
- [ ] Require camera suggestions to distinguish `tracking_rerun_before_follow_cam` from `adjust_follow_cam`.
- [ ] Require highlight suggestions to preserve `core_window`, minimum post-event tail, and source-video boundary constraints.
- [ ] Add model guidance: use a stronger model for run-level improvement and hard recovery cases; reserve smaller models for low-risk tagging or dry-run smoke.
- [ ] Keep the output schema backward compatible.

**Tests:**

- [ ] Fake-client missing-ball suggestion without packet/visual provenance is rejected.
- [ ] Partial long-gap suggestion without an explanation is rejected.
- [ ] `not_visible` without evidence is rejected.
- [ ] Noise suggestion without accepted false-positive tag is rejected.
- [ ] Camera suggestion overlapping Lost/Predicted track context chooses tracking rerun before follow-cam tuning.
- [ ] Highlight suggestion that trims the required tail is rejected unless source-video end clamps the tail.
- [ ] Existing dry-run and approval tests still pass.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Stricter AI improvement prompt/contract
- Backward-compatible validation
- Tests that prevent under-covered long gaps and clipped highlight tails

### PR 3: Missing-Ball Recovery Apply Loop

**Purpose:** turn approved missing-ball AI actions into bounded recovery attempts and compare the result instead of trusting the suggestion.

**Files:**

- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/high_recall_reconcile.py`
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/tests/test_high_recall_windows.py`
- Modify: `python_backend/tests/test_high_recall_reconcile.py`
- Modify docs: `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Ensure approved `targeted_rerun` and `localize_ball_roi` actions create child runs only for bounded frame windows.
- [ ] Ensure ROI recovery can focus on lower-right or other explicit areas from packet evidence without enabling full-video SAHI.
- [ ] Write comparison evidence for the parent run and child recovery window.
- [ ] Reject or warn when recovery only creates short noisy detected islands without sustained tracking improvement.
- [ ] Feed recovery result summaries into `ai_improvement_quality_gate.json`.
- [ ] Preserve parent track files unless an explicit reconcile/apply action is supplied.

**Tests:**

- [ ] Approved bounded missing-ball action creates the expected child-run plan.
- [ ] Lower-right ROI action is accepted only when bounded by frames and ROI.
- [ ] A 2049-2544 lower-right fixture containing frame 2079 requires packet/visual evidence plus approved bounded recovery or evidence-backed `not_visible`.
- [ ] Full-video unbounded SAHI approval is rejected or marked unsafe.
- [ ] Child recovery that reduces a long gap by at least the required threshold passes comparison.
- [ ] Child recovery that adds noisy short islands without gap improvement fails comparison.
- [ ] Parent `ball_track.csv` hashes remain unchanged during planning/review.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py python_backend/tests/test_high_recall_reconcile.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Bounded child recovery apply loop
- Recovery comparison report
- Quality-gate coverage for long missing-ball windows

### PR 4: Dense-Noise Suppression Loop

**Purpose:** make AI useful when detector output is too noisy, especially when spatial splitting/SAHI creates many false positives.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Create: `python_backend/football_tracking/noise_improvement.py`
- Create: `python_backend/tests/test_noise_improvement.py`

**Build:**

- [ ] Summarize dense-noise windows from `ball_audit.json`, `ai_review_triggers.json`, and review packet tags.
- [ ] Ask AI to classify likely noise sources and propose bounded actions.
- [ ] Support advisory actions such as reject window, ignore zone, confidence/size threshold patch, or targeted rerun with narrower ROI.
- [ ] Keep suggested config patches advisory until explicitly applied.
- [ ] Compare candidate false-positive island count against baseline.
- [ ] Fail or warn when a proposed rerun increases false-positive islands by more than the allowed threshold without improving lost-gap coverage.

**Tests:**

- [ ] Dense noise window is included in AI improvement prompt context.
- [ ] Missing false-positive tag is rejected or warned.
- [ ] Unbounded reject/config actions are rejected or warned.
- [ ] Candidate with increased false-positive islands fails comparison.
- [ ] Candidate with lower noise and unchanged/ better recall passes comparison.
- [ ] Suggested config patch does not mutate runtime config without explicit apply.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_noise_improvement.py -q
git diff --check
```

**Deliverables:**

- Dense-noise AI suggestions with failure tags
- Optional noise comparison helper
- Quality-gate checks for noise regressions

### PR 5: Follow-Cam Improvement Apply Loop

**Purpose:** move from camera-motion audit to explicit, testable follow-cam improvement planning.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/follow_cam.py` only if rerender plan consumption needs a small hook
- Modify: `python_backend/football_tracking/camera_motion_audit.py` only if summary fields need a backward-compatible addition
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_follow_cam.py`
- Modify: `python_backend/tests/test_camera_motion_audit.py`
- Modify docs: `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Join `camera_motion_audit.json` with track context around the same frame windows.
- [ ] Ask AI to choose between bad-track recovery and follow-cam tuning.
- [ ] Write approved follow-cam actions to `follow_cam_rerender_plan.json`.
- [ ] Keep rerendering explicit; no video render should happen just because a plan exists.
- [ ] Compare candidate camera metrics to baseline and fail if `review_event_count`, `max_pan_step_px`, or `p95_pan_step_px` regresses beyond threshold.

**Tests:**

- [ ] Camera spike with Lost/Predicted track context recommends tracking rerun first.
- [ ] Camera spike with stable track context recommends follow-cam tuning.
- [ ] Approved follow-cam action writes a rerender plan.
- [ ] Rerender plan presence alone does not render a video.
- [ ] Candidate camera audit regression fails the quality gate.
- [ ] Candidate camera audit improvement passes the quality gate.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_follow_cam.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- `follow_cam_rerender_plan.json`
- Camera improvement comparison path
- Tests proving no silent rerendering

### PR 6: Highlight Boundary Improvement Loop

**Purpose:** ensure highlight clips keep enough context before and after important action, and let AI adjust boundaries without clipping the result.

**Files:**

- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_highlights.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify docs: `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Keep default pre/post buffers for every highlight candidate.
- [ ] Require AI suggested windows to include `core_window`, `suggested_window`, `reason`, and provenance.
- [ ] Enforce minimum post-event tail unless clamped by the source video end.
- [ ] Support explicit approved highlight render action id.
- [ ] Include highlight tail validation in the quality gate and workflow report.

**Tests:**

- [ ] Default highlight window includes pre/post buffer.
- [ ] AI suggested window cannot start after `core_window.start_frame`.
- [ ] AI suggested window cannot end before required tail.
- [ ] Source-video end boundary clamps the window without failing incorrectly.
- [ ] Approved highlight action renders or plans only the selected action id.
- [ ] Quality gate fails when a rendered/suggested highlight clips the tail.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_highlights.py python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Highlight boundary safeguards
- Approved highlight render path
- Quality-gate validation for shot/result tail preservation

### PR 7: API Artifact Visibility And Approval Contract

**Purpose:** expose AI improvement artifacts and explicit approval payloads through backend/API surfaces before adding UI controls.

**Files:**

- Modify relevant API/service files after confirming existing endpoint patterns.
- Modify: `python_backend/tests/test_api_service.py`
- Modify: `python_backend/tests/test_metrics.py`
- Modify docs: `README.md` and `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Add or extend artifact listing so workflow report, quality gate, child recovery comparison, camera audit, follow-cam rerender plan, highlight report, and final videos are discoverable.
- [ ] Add a backend representation for grouped AI improvement items: missing ball, noise, camera motion, and highlights.
- [ ] Expose evidence fields: frame window, packet id, visual review id, failure tags, confidence, suggested action, approval status, consumed action ids, and quality-gate status.
- [ ] Add explicit API handling for approval payload validation without executing actions by file presence.
- [ ] Return readable empty states when an artifact is missing or unavailable.

**Tests:**

- [ ] API lists workflow, quality-gate, camera, recovery, and highlight artifacts.
- [ ] API groups AI improvement items by user problem type.
- [ ] API rejects implicit approval-file execution.
- [ ] Unknown approval ids fail or return a clear validation error.
- [ ] Missing artifacts return non-crashing unavailable summaries.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py python_backend/tests/test_metrics.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Backend artifact manifest and grouped AI improvement API contract
- Explicit approval validation endpoint or service helper
- API tests for missing artifacts and approval safety

### PR 8: Operator UI Approval Controls

**Purpose:** make the AI improvement loop usable in the app without hiding risk behind raw JSON or accidental apply behavior.

**Files:**

- Modify relevant React pages under `src/` or `app/` after confirming current frontend layout.
- Modify frontend API client files after confirming existing patterns.
- Modify or add frontend tests according to current test setup.
- Modify docs: `README.md` and `docs/operations/ai-improvement-workflow.md`

**Build:**

- [ ] Show AI improvement items grouped by missing ball, noise, camera motion, and highlights.
- [ ] Show evidence fields: frame window, packet id, visual review id, failure tags, confidence, suggested action, approval status, and quality-gate result.
- [ ] Provide explicit controls for approve targeted rerun, approve follow-cam rerender plan, approve highlight render, and reject suggestion.
- [ ] Make approval controls call only the explicit API/command path from PR 7.
- [ ] Show that approval files do not execute by presence.
- [ ] Surface final artifacts: workflow report, quality gate, child recovery report, camera audit, highlight report, and final videos.

**Tests:**

- [ ] UI renders grouped improvement items from a mock artifact contract.
- [ ] UI shows missing/unavailable artifact states.
- [ ] UI does not expose an accidental auto-apply path.
- [ ] Approval controls submit explicit action ids only.
- [ ] Quality-gate fail/warn/pass states are visible.

**Validation:**

```powershell
pnpm test
pnpm lint
git diff --check
```

**Deliverables:**

- Operator-visible AI improvement workflow
- Explicit approval controls
- Artifact visibility for debugging and final review

### PR 9: Real-Video Validation, Docs, And Skill Capture

**Purpose:** prove the workflow on the user's real video and record the reusable operating procedure.

**Files:**

- Modify: `README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `docs/superpowers/plans/2026-06-23-ai-improvement-action-loop-development-plan.md`
- Prepare after PR merge, outside repository PR scope: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Run the full stable workflow on the current real video/output directory.
- [ ] Run AI improvement with the stronger model selected for hard recovery/camera/highlight cases.
- [ ] Apply only explicitly approved actions.
- [ ] Generate or verify final tracking video, follow-cam video, quality-gate report, and highlight clips.
- [ ] Inspect the known frame-2079/right-bottom-corner class of failure.
- [ ] Record whether the final output is pass/warn/fail and why.
- [ ] Write `final_ai_improvement_artifact_manifest.json` listing report paths, quality-gate status, child recovery comparison, camera audit, highlight report, final tracking/follow-cam/highlight videos, and pass/warn/fail reasons.
- [ ] Update docs with exact commands, artifact names, and expected decisions.
- [ ] Prepare the local skill update as a separate post-merge diff and ask for user confirmation before writing outside the repository.

**Tests:**

- [ ] Unit and integration tests from the affected PRs pass.
- [ ] Real-video smoke produces `stable_ai_improvement_workflow_report.json`.
- [ ] Real-video smoke produces `ai_improvement_quality_gate.json`.
- [ ] Real-video smoke produces `final_ai_improvement_artifact_manifest.json`.
- [ ] Final follow-cam output exists and camera audit does not regress.
- [ ] Highlight clips exist when event candidates or approved highlight actions exist.
- [ ] Manual visual spot check covers the known problematic sequence and at least one highlight tail.

**Validation:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 --input-video python_backend\data\raw5760x144020fps.mp4 --parallel-mode temporal --mode real
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_improvement_quality_gate.py python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_highlights.py python_backend/tests/test_follow_cam.py -q
git diff --check
```

**Deliverables:**

- Final real-video validation record
- `final_ai_improvement_artifact_manifest.json`
- Updated README and operator docs
- User-confirmed update to local `football-tracking-real-video-tuning` skill after repository PRs are merged
- List of generated final videos/highlights and their quality status

## End-To-End Acceptance Criteria

- Long missing-ball gaps require packet/visual evidence plus AI improvement coverage plus explicit approved action or evidence-backed `not_visible`.
- The known right-bottom-corner/around-frame-2079 failure class cannot pass silently.
- Dense-noise cases carry bounded windows and false-positive tags.
- Full-video speed comes from temporal chunks first, not broad spatial slicing.
- SAHI/ROI is used only for bounded approved recovery windows unless a benchmark later proves broad slicing is worth the noise.
- Camera-motion issues are separated into tracking recovery versus follow-cam tuning.
- Follow-cam rerendering is explicit and compared against baseline camera metrics.
- Highlight clips keep the post-shot/result tail.
- Review/improvement-only stages preserve track hashes.
- Approved actions execute only through explicit approval arguments or endpoints.
- The final workflow can be rerun from docs and produces machine-readable pass/warn/fail artifacts.

## Managed PR Gates

Every PR in this program must follow the managed PR loop:

- [ ] Start from latest `origin/main`.
- [ ] Create a fresh branch from clean `main`.
- [ ] Use a worker subagent for implementation.
- [ ] Run spec-compliance review with a separate agent.
- [ ] Run code-quality review with a separate agent.
- [ ] Fix all valid Critical/Important findings.
- [ ] Run focused local validation.
- [ ] Push and open a GitHub PR.
- [ ] Wait for GitHub checks and Copilot comments.
- [ ] Evaluate remote comments on merit and fix confirmed issues.
- [ ] Merge only after checks and valid review feedback are resolved.
- [ ] Delete merged remote and local branches when cleanup is authorized.
