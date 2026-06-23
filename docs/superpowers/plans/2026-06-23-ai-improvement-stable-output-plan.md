# AI Improvement Stable Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** turn AI from a passive reviewer into a bounded improvement loop that can help recover missing balls, suppress noise, stabilize follow-cam output, and tune highlight boundaries while proving the final video quality improved.

**Architecture:** Deterministic tracking artifacts remain the source of truth. AI only proposes bounded, evidence-backed actions; operator approval and explicit apply/rerun APIs turn selected actions into child runs, follow-cam plans, or highlight renders; a quality gate verifies track immutability during review-only phases and validates final output quality.

**Tech Stack:** Python backend modules and pytest, existing FastAPI service surfaces, React operator UI already merged in PR #38, existing AI provider routing, existing review packets, high-recall windows, camera motion audit, event/highlight artifacts, and managed PR workflow.

---

## Requirement Summary

The user-facing requirement is not "AI audit says good/bad." The requirement is "AI helps improve the run, then the program proves whether the approved change helped."

The four concrete improvement needs are:

- **Ball missing:** AI should inspect relevant packet windows, either locate a plausible ball ROI or explicitly say `not_visible`, then propose bounded `targeted_rerun` or `localize_ball_roi` actions. Long missing spans such as the right-bottom corner sequence near frame 2079 must not be allowed to pass silently.
- **Too many noise detections:** AI should classify the noise source, such as extra ball, shoe, foot, sideline, player head, advertising board, or background drift, and recommend bounded reject/config/rerun actions with provenance.
- **Follow-cam too jumpy:** AI should use `camera_motion_audit.json` plus track context to decide whether jumpiness is a follow-cam tuning issue or a tracking-loss issue. It should produce an auditable plan, not silently rerender video.
- **Highlight windows:** the default render should keep a pre/post buffer, and AI can suggest better start/end frames. The result must preserve the final shot/result tail, including near the source video's end.

Secondary requirement:

- For speed, prefer temporal time-split parallelism for full-video processing. Image slicing/SAHI is still useful for small-ball recall, but whole-video sliced inference produces too many false positives and should be reserved for targeted windows or ROI recovery unless benchmarks prove otherwise.

## Current State To Preserve

Already merged and should not be rebuilt:

- AI improvement report generation and approval contracts.
- Packet-level visual review and packet tags.
- Approved targeted-rerun child execution.
- Approved ROI policy and explicit `approved_actions_path` handling.
- Camera motion audit and AI follow-cam rerender planning.
- Highlight boundary improvement and approved highlight rendering.
- Operator AI improvement UI from PR #38.

Mandatory safety boundaries:

- `ai_improvement_report.json` is advisory.
- `ai_improvement_approved_actions.json` does not execute anything by file presence.
- Review/improvement-only phases do not modify `ball_track.csv` or `ball_track.cleaned.csv`.
- Config patches remain suggested patches until an explicit apply path is used.
- Follow-cam AI approval may write `follow_cam_rerender_plan.json`, but must not silently render.
- Highlight rendering from AI must use an explicit approved action id or explicit candidate/window request.

## Planned PR Sequence

### PR 2: AI Improvement Quality Gate

**Purpose:** add a machine-readable quality gate that proves the AI improvement loop is safe and catches the exact failure class seen around frame 2079: long missing ball spans with insufficient review/improvement coverage.

**Files:**

- Create: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Create: `python_backend/scripts/run_ai_improvement_quality_gate.py`
- Create: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify only if needed: `docs/superpowers/plans/2026-06-23-ai-improvement-stable-output-plan.md`

**Quality Gate Modes And Aggregation:**

- CLI supports `--mode dry-run|artifact-only|real`.
- Default mode is `artifact-only` for local checks against existing output directories.
- `dry-run` may report provider/model checks as `unavailable` without failing, but must still fail deterministic safety checks when their required inputs are present and contradictory.
- `artifact-only` may report missing optional candidate artifacts as `unavailable`, but must fail if a known long lost gap exists and there is no packet/improvement/approval coverage to explain it.
- `real` mode treats missing required artifacts as failures, not neutral unavailable results.
- Required `real` artifacts: `ball_audit.json`, `review_packets.json`, `ai_improvement_report.json`, `ai_improvement_hash_snapshots.json`, and selected model provenance for real provider calls.
- Required candidate artifacts only become required when the corresponding comparison is requested: candidate follow-cam output requires candidate `camera_motion_audit.json`; highlight validation requires `event_candidates.json` or an explicit highlight report.
- Overall `summary.status` is `fail` when any required check fails, `warn` when required checks pass but warning checks exist, and `pass` only when all required checks pass with no warnings.

**Build:**

- [ ] Add a pure-data quality gate module that reads an output directory and writes `ai_improvement_quality_gate.json`.
- [ ] Add `build_track_hash_snapshot(output_dir, stage_name)` that computes SHA-256 hashes for `ball_track.csv` and `ball_track.cleaned.csv`.
- [ ] Add `write_track_hash_snapshot(output_dir, stage_name, report_name="ai_improvement_hash_snapshots.json")` that appends stage snapshots without rewriting track CSVs.
- [ ] Let the quality gate accept `--pre-review-stage` and `--post-review-stage`, defaulting to `before_review` and `after_ai_improvement`.
- [ ] Report `track_hash_unchanged` as pass/fail/unavailable with per-file hash details; in `real` mode missing hash snapshots fail.
- [ ] Detect long lost gaps from `ball_audit.json` using a default threshold of 120 frames.
- [ ] Require each long lost gap to have review packet coverage from `review_packets.json`, AI improvement coverage from `ai_improvement_report.json`, and either an explicit approved targeted rerun/local ROI action or explicit evidence-backed `not_visible`.
- [ ] Add a named gate check for the 2079-style problem: `long_lost_gap_improvement_coverage`.
- [ ] Use exact frame-window semantics: packet windows must overlap the gap by at least 1 frame and covering action windows must either contain the full gap or be reported as partial coverage with uncovered ranges.
- [ ] Treat `not_visible` as satisfying only the subwindow it explicitly covers and only when it has packet or visual-review provenance; `unavailable` never satisfies a long-gap check in `real` mode.
- [ ] Add a fixture representing a 2049-2544 long lost gap that contains frame 2079; it must fail without packet/action coverage and pass or warn only with explicit coverage.
- [ ] Verify approved actions are only counted when the script receives an explicit approved-actions path or explicit `--approved-actions` argument.
- [ ] Report dense-noise coverage from deterministic triggers in `ai_review_triggers.json`, suspicious short detected islands in `ball_audit.json`, or dense-noise packet labels in `review_packets.json`.
- [ ] Require dense-noise AI suggestions to overlap the deterministic noise window by at least 1 frame and include an accepted false-positive tag such as `extra_ball`, `shoe_confusion`, `foot_confusion`, `sideline_confusion`, `player_head`, `advertising_board`, `wall_background_drift`, or `unknown_false_positive`.
- [ ] Report camera regression by comparing baseline and candidate `camera_motion_audit.json` summaries when a candidate output directory is supplied.
- [ ] Fail camera regression if candidate `review_event_count`, `max_pan_step_px`, or `p95_pan_step_px` worsens by more than 5 percent.
- [ ] Report highlight tail validation by checking candidate/render windows against `core_window.end_frame + min_tail_frames`.
- [ ] Report provider/model routing from `ai_visual_review.json` and `ai_improvement_report.json`; real-provider mode must record selected models, dry-run/fake-client mode may report unavailable.
- [ ] Keep the module independent of real videos and OpenCV rendering so unit tests are fast.

**Suggested JSON deliverable:**

```json
{
  "schema_version": "1.0",
  "summary": {
    "status": "pass",
    "check_count": 8,
    "failed_check_count": 0,
    "warning_count": 0
  },
  "checks": {
    "track_hash_unchanged": {"status": "pass"},
    "approved_actions_explicitly_consumed": {"status": "pass"},
    "long_lost_gap_improvement_coverage": {"status": "pass"},
    "missing_ball_roi_or_not_visible_present": {"status": "pass"},
    "noise_failure_tags_present": {"status": "pass"},
    "camera_regression": {"status": "pass"},
    "highlight_tail_ok": {"status": "pass"},
    "model_routing_recorded": {"status": "pass"}
  },
  "artifacts": {
    "source_output_dir": "..."
  }
}
```

**Tests:**

- [ ] `test_missing_inputs_are_unavailable_in_artifact_mode_without_crashing`: missing input artifacts produce stable `unavailable` checks and do not raise.
- [ ] `test_real_mode_fails_when_required_artifacts_are_missing`: `real` mode fails when required artifacts are unavailable.
- [ ] `test_hash_snapshots_pass_when_tracks_are_unchanged`: review/improvement-only hash snapshots pass when both track hashes match.
- [ ] `test_hash_snapshots_fail_when_review_phase_mutates_track_csv`: hash comparison fails when a review-only phase changes either track CSV.
- [ ] `test_approved_actions_file_presence_is_not_execution`: an `ai_improvement_approved_actions.json` sitting in the output directory is not counted unless passed explicitly.
- [ ] `test_long_lost_gap_2079_fails_without_packet_coverage`: a 2049-2544 lost gap containing frame 2079 fails without review packet coverage.
- [ ] `test_long_lost_gap_2079_fails_without_ai_or_approval_coverage`: packet coverage alone is insufficient when no AI suggestion or approved/not-visible result covers the gap.
- [ ] `test_long_lost_gap_2079_passes_with_explicit_targeted_rerun_approval`: explicit `targeted_rerun` approval with provenance passes.
- [ ] `test_long_lost_gap_2079_warns_for_evidence_backed_not_visible`: explicit `not_visible` evidence passes with warning in artifact mode.
- [ ] `test_long_lost_gap_2079_real_mode_fails_for_unavailable_not_visible`: `unavailable` does not satisfy real mode.
- [ ] `test_dense_noise_requires_failure_tag_and_window_overlap`: dense-noise AI suggestions without accepted `failure_tags` or window overlap fail or warn based on mode.
- [ ] `test_camera_regression_passes_within_five_percent`: camera comparison passes when candidate motion metrics improve or stay within 5 percent.
- [ ] `test_camera_regression_fails_beyond_five_percent`: camera comparison fails when candidate motion metrics worsen beyond 5 percent.
- [ ] `test_highlight_tail_fails_when_suggested_window_clips_tail`: highlight tail validation fails if a suggested/rendered end frame cuts off the configured tail.
- [ ] `test_highlight_tail_passes_when_tail_reaches_source_boundary`: highlight tail validation passes when the render reaches the required tail or the source video boundary.
- [ ] `test_real_provider_mode_requires_selected_models`: real-provider mode fails model routing if selected model provenance is missing.
- [ ] `test_dry_run_provider_unavailable_is_warning_not_failure`: dry-run/fake-client mode allows provider unavailable but records the mode.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement_quality_gate.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_high_recall_windows.py python_backend/tests/test_camera_motion_audit.py python_backend/tests/test_highlights.py -q
.\.venv\Scripts\python.exe python_backend\scripts\run_ai_improvement_quality_gate.py --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 --dry-run --report-name ai_improvement_quality_gate.json
git diff --check
```

**Deliverables:**

- `ai_improvement_quality_gate.json`
- CLI command for repeatable quality checks
- Unit tests proving the 2079-style long-lost-gap issue is caught
- Managed PR with local review agent, GitHub checks, Copilot comment handling, merge, and branch cleanup

### PR 3: Stable Real-Video Recipe And Time-Split Strategy

**Purpose:** produce a repeatable real-video workflow that favors stable output over maximum detector recall, and documents when temporal parallelism is preferable to image slicing.

**Files:**

- Create: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Create: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/chunk_runner.py` only if an explicit CLI/API gap prevents approved child rerun execution from the recipe.
- Modify: `README.md`
- Modify: `docs/operations/ai-improvement-workflow.md` if the docs directory exists; otherwise create it.

**Improvement/No-Regression Thresholds:**

- Missing-ball improvement passes when a long lost gap is closed by an approved child rerun, reduced by at least 20 percent, or explicitly marked `not_visible` with packet/visual evidence for the remaining span.
- Missing-ball improvement fails when a long gap remains uncovered, or a rerun produces only short noisy detected islands without sustained recovery.
- Dense-noise improvement passes when false-positive island count does not increase and AI suggestions contain bounded windows plus accepted false-positive tags.
- Dense-noise improvement fails when a rerun/config suggestion increases false-positive island count by more than 10 percent without improving lost-gap coverage.
- Camera improvement passes when candidate camera audit metrics do not worsen beyond the PR 2 quality-gate threshold and any approved follow-cam plan is explicit.
- Highlight improvement passes when every rendered or suggested highlight preserves `core_window.end_frame + min_tail_frames`, unless clamped by the source video end frame.

**Build:**

- [ ] Add a workflow script that can run against an existing output directory and optional source video.
- [ ] The workflow sequence should be: metrics/artifacts refresh, `before_review` hash snapshot, review packets, optional visual review, run-level AI improvement, `after_ai_improvement` hash snapshot, explicit approval arguments, explicit approved child rerun, optional follow-cam rerender plan, optional highlight render, quality gate.
- [ ] Support `--dry-run` so the workflow can be tested without API keys.
- [ ] Support `--model` or existing provider settings so a stronger run-level improvement model can be selected.
- [ ] Default model policy in docs: use a stronger model for run-level improvement and hard cases; reserve small/cheap models for low-risk tagging or dry-run smoke.
- [ ] Add a `--parallel-mode temporal` path that splits by time windows rather than slicing every frame spatially.
- [ ] Document recommended detector strategy: full-video temporal chunks first; SAHI/ROI only for approved high-recall windows, right-bottom corner recovery, or other bounded missing-ball cases.
- [ ] Capture before/after metrics for missing ball, dense noise, camera motion, and highlight tail checks.
- [ ] Write `stable_ai_improvement_workflow_report.json` with every command stage, status, produced artifacts, and quality-gate result.
- [ ] Never discover approval intent by convention; child rerun and highlight render stages require `--approved-actions-path`, `--approval-ids`, or `--approved-action-id`.

**Tests:**

- [ ] `test_dry_run_workflow_records_stages_without_provider`: dry-run workflow creates stage records without calling a real provider.
- [ ] `test_workflow_records_before_and_after_hash_snapshots`: workflow writes `ai_improvement_hash_snapshots.json` before and after review/improvement-only stages.
- [ ] `test_workflow_refuses_implicit_approved_actions_artifact`: workflow refuses to count approved actions unless an explicit approval path or approval ids are supplied.
- [ ] `test_temporal_mode_passes_chunk_settings`: temporal mode passes the intended settings to the chunk runner.
- [ ] `test_sahi_roi_selected_only_for_bounded_approved_windows`: SAHI/ROI rerun is only selected for bounded approved windows.
- [ ] `test_missing_source_video_keeps_artifact_only_stages`: missing source video still allows artifact-only review stages and reports unavailable video-dependent stages.
- [ ] `test_workflow_report_includes_quality_gate_and_artifacts`: workflow report includes quality gate result and produced artifact names.
- [ ] `test_workflow_fails_quality_gate_when_real_mode_missing_required_artifacts`: real-mode workflow surfaces quality-gate failure instead of hiding missing inputs.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_stable_ai_improvement_workflow.py python_backend/tests/test_ai_improvement_quality_gate.py -q
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 --input-video python_backend\data\raw5760x144020fps.mp4 --dry-run --parallel-mode temporal
git diff --check
```

**Deliverables:**

- Stable workflow script
- `stable_ai_improvement_workflow_report.json`
- Documented recommendation: temporal chunk parallelism for speed, targeted SAHI/ROI for recovery
- Real-video smoke result with quality-gate status

### PR 4: Prompt Contract And AI Improvement Hardening

**Purpose:** make AI suggestions more actionable and less likely to miss late frames, especially for shots/highlights and long missing-ball windows.

**Files:**

- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py` only if packet-level prompt wording needs the same contract.
- Modify: `python_backend/tests/test_ai_visual_review.py` only if packet-level prompt wording changes.

**Build:**

- [ ] Tighten run-level improvement instructions so missing-ball actions must cover the entire long-lost-gap window or explicitly explain why only a sub-window is actionable.
- [ ] Require missing-ball suggestions to preserve provenance: `source_packet_id` or `visual_review_id` for ROI/localization actions.
- [ ] Add instruction that `not_visible` is acceptable only when packet evidence supports that the ball is genuinely hidden/out of frame.
- [ ] Tighten highlight instructions: suggested windows must preserve `core_window`, `buffer_policy.min_tail_frames`, and end-of-source-video constraints.
- [ ] Add instruction that camera actions must distinguish tracking-loss camera jumps from follow-cam-only tuning issues.
- [ ] Add optional `verification_plan` fields in AI output validation only if it can be accepted without breaking old reports; otherwise record verification expectations in the quality gate instead.
- [ ] Keep accepted schema backward compatible with existing artifacts.

**Tests:**

- [ ] `test_dry_run_report_still_works_with_prompt_contract`: dry-run report still works.
- [ ] `test_missing_ball_roi_requires_packet_or_visual_provenance`: fake client response missing required ROI provenance is rejected or downgraded with warnings.
- [ ] `test_highlight_suggestion_warns_when_tail_is_trimmed`: fake client response trimming highlight tail is rejected or warned.
- [ ] `test_long_lost_gap_partial_window_requires_explanation`: fake client response that proposes a sub-window for a long lost gap without explanation is rejected or warned.
- [ ] `test_camera_lost_or_predicted_context_requires_tracking_rerun_before_follow_cam`: camera recommendation chooses `tracking_rerun_before_follow_cam` when evidence overlaps Lost/Predicted status.
- [ ] Existing approval and config patch tests still pass.

**Validation Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py python_backend/tests/test_ai_visual_review.py python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliverables:**

- Stronger AI prompt contract
- Tests that prevent clipped highlight tails and under-covered long gaps
- Backward-compatible report handling

### PR 5: README, Operator Docs, And Skill Update

**Purpose:** make the completed workflow easy to run again and prepare the reusable operating knowledge for a separate local skill update.

**Files:**

- Modify: `README.md`
- Create or modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `docs/superpowers/plans/2026-06-23-ai-improvement-stable-output-plan.md` with final merged PR references.

**Build:**

- [ ] Explain "AI review" versus "AI improvement" in operator terms.
- [ ] Document when to use a stronger model for run-level improvement and when a small model is acceptable.
- [ ] Document the artifact chain: `review_packets.json`, `ai_visual_review.json`, `ai_improvement_report.json`, `ai_improvement_approved_actions.json`, child rerun outputs, `follow_cam_rerender_plan.json`, highlight outputs, `ai_improvement_quality_gate.json`.
- [ ] Document the recommended speed strategy: temporal chunks first, targeted SAHI/ROI second.
- [ ] Document the right-bottom corner/long-lost-gap failure pattern and how the quality gate catches it.
- [ ] Document the final real-video smoke command and expected output files.
- [ ] Add a "Skill Capture Notes" section with the exact reusable sequence to copy into `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md` after the PR is merged.

**Tests:**

- [ ] Run PR 2 and PR 3 validation commands after docs updates to confirm the documented commands still work.
- [ ] Run `git diff --check`.
- [ ] Manually scan docs for claims that AI report or approval-file presence automatically mutates output; remove any such claims.

**Deliverables:**

- README workflow section
- Operator guide
- Skill-capture notes ready for a separate local, non-PR skill update
- Final validation record listing output directory, model/provider mode, quality-gate status, and generated videos/highlights

### Post-PR Local Skill Update

**Purpose:** update the local `football-tracking-real-video-tuning` skill only after the repository workflow is merged and verified.

**Files outside GitHub PR scope:**

- Modify: `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Build:**

- [ ] Copy the verified stable workflow sequence from PR 5 docs into the skill.
- [ ] Include the current guidance that full-video speed should come from temporal parallelism first, with SAHI/ROI used for bounded recovery windows.
- [ ] Include the quality-gate command and the real-mode rule that missing required artifacts fail.
- [ ] Include the model guidance: use a stronger model for run-level improvement/hard recovery, smaller models only for lower-risk tagging or dry-run checks.

**Deliverables:**

- Updated local skill, not part of the GitHub PR.
- Short note in the managed PR final report that the local skill update was completed after merge.

## End-To-End Acceptance

- Missing-ball cases do not pass merely because a packet exists; they pass only with packet coverage plus AI improvement coverage plus approved rerun/ROI or explicit `not_visible`.
- Dense-noise cases carry failure tags and bounded frame evidence.
- Camera-motion cases produce either follow-cam tuning advice or tracking-rerun-before-follow-cam advice, and final candidate camera metrics do not regress.
- Highlight windows keep the result tail after shots/goals and respect source video boundaries.
- Review/improvement-only stages leave track CSV hashes unchanged.
- Approved actions execute only through explicit arguments or explicit API calls.
- The final stable workflow can produce a tracking/follow-cam output and machine-readable pass/fail report.
- The docs and skill explain the workflow in a way that can be run again without rediscovering the process.

## Managed PR Gates

Each PR must follow the managed PR loop:

- Start from latest `origin/main`.
- Use a fresh branch.
- Use a worker subagent for implementation.
- Use a separate spec-compliance reviewer.
- Use a separate code-quality reviewer.
- Run focused local validation.
- Push and open a GitHub PR.
- Wait for GitHub checks and Copilot comments.
- Fix valid comments only.
- Merge only after checks and valid review feedback are resolved.
- Delete merged remote and local branches only when the user has explicitly authorized cleanup for the program.
