# Stable AI Improvement Workflow

This workflow runs against an existing tracking output directory. It is meant to make AI review repeatable without letting advisory artifacts mutate tracking output by surprise.

## Contract Reference

The stable worker/operator contract lives in [ai-improvement-contract.md](ai-improvement-contract.md). Treat that document as authoritative for lifecycle stages, `candidate_id` / `approval_id` / `problem_type` traceability, model policy, missing-ball closure rules, follow-cam thresholds, highlight comparison, and final-output gating.

This workflow document explains how to run the stable workflow. If a later executor or UI needs to decide whether an AI artifact is review-only, executable, comparable, promotable, rejected, or final, use the contract document first.

## Audit Versus Improvement

AI audit is the review layer. It reads deterministic evidence such as `ball_audit.json`, `ai_review_triggers.json`, `review_packets.json`, `camera_motion_audit.json`, `event_candidates.json`, and visual-review media, then explains likely problems. Audit output may recommend follow-up work, but it is advisory and cannot mutate tracks, renders, clips, or manifests.

AI improvement is the candidate layer. It turns an evidence-backed recommendation into an isolated candidate only after an explicit approval id is consumed. Improvement work must stay scoped to one of the four supported problem types: missing ball, dense noise, follow-cam motion, and highlight boundary. Every candidate needs traceable evidence, a bounded window, comparison criteria, a valid comparison report, quality-gate evidence, and final-manifest selection before it can affect final output.

## Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --targeted-localization-window 2049:2544:right_corner `
  --dry-run `
  --parallel-mode temporal
```

The workflow writes `stable_ai_improvement_workflow_report.json` with the planned stages, per-stage `stage_timing`, hash snapshots, produced artifacts, quality-gate summary, strategy, warnings, and explicit approval intent.

Stable rules summary:

- Review-only AI output is advisory and cannot mutate artifacts or become final output.
- Executable candidates require `candidate_id`, `approval_id`, `problem_type`, traceable evidence, a bounded frame window, expected artifacts, and comparison criteria.
- Public executable approvals use the closed `approved_action` set from the contract. Legacy `targeted_rerun` is accepted as input only by normalization to `rerun_ball_window`.
- Missing-ball closure is either a bounded recovery candidate with `missing_ball_recovery_comparison.json` or an evidence-backed full-window `missing_ball_resolution.json`.
- The `2049-2544` long missing-ball window cannot be closed by only proving a short frame `2079` neighborhood.
- Follow-cam candidates must improve or stay below motion thresholds while preserving Detected/Predicted crop coverage; sparse data warns instead of silently passing.
- Highlight candidates must preserve the event `core_window` and required post-event tail. Reports must expose `core_window`, `render_window`, `core_window_preserved`, `required_tail_frames`, `actual_tail_frames`, `tail_status`, and `source_end_clamp`; source-video-end clamps are valid only when recorded as evidence.
- Final output requires explicit approval, candidate artifacts or a valid resolved-noop, comparison, quality gate, and `final_ai_improvement_artifact_manifest.json`.

## Stage Order

1. Metrics/artifacts refresh.
2. `before_review` track hash snapshot.
3. Review packets.
4. Optional visual review.
5. Optional targeted visual localization.
6. Run-level AI improvement.
7. `after_ai_improvement` track hash snapshot.
8. Explicit approved child rerun plan.
9. Optional follow-cam rerender plan.
10. Optional highlight render plan.
11. Missing-ball noop resolution.
12. Pre-manifest AI improvement quality gate.
13. Final artifact manifest.
14. Final AI improvement quality-gate refresh and final-manifest status sync.

In `--dry-run`, provider calls and expensive video work are not required. Hash snapshots and lightweight JSON reports may still be written so the quality gate has stable inputs.

## Targeted Visual Localization

Use `--targeted-localization-window start:end:label` when a known missing-ball window needs a stronger visual look before a bounded `localize_ball_roi` approval can be trusted. The stage writes `ai_visual_localization.json` after decoding the source video dimensions with OpenCV, generating contact/crop sheets, and validating any model ROI against the decoded image width and height.

Example:

```powershell
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\my_run `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --targeted-localization-window 2049:2544:right_corner `
  --mode real `
  --model gpt-5.4
```

`ai_visual_localization.json` is evidence only. It can provide a traceable `visual_localization_id` and a bounded `local_search_roi` suggestion for later approval, but it is not a final selected artifact and does not mutate `ball_track.csv`, `ball_track.cleaned.csv`, follow-cam videos, or highlight clips by itself. If a model proposes an ROI outside the decoded source frame, the workflow records it as rejected rather than silently clamping the coordinates.

## Speed Strategy

Use temporal chunks first for full-video speed. The stable default is `--parallel-mode temporal`, which records the intended temporal chunk settings in the workflow report.

Do not use broad full-video SAHI from this workflow. SAHI/ROI is reserved for bounded recovery windows from explicit approved actions, such as a missing-ball window with packet or visual evidence. That keeps recall work targeted and avoids flooding the full run with false positives.

## Approval Safety

Approval files are never auto-executed by presence. An `ai_improvement_approved_actions.json` file sitting in the output directory does not trigger child reruns or highlight renders.

Use explicit arguments when you mean to consume approvals:

```powershell
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\my_run `
  --approved-actions-path python_backend\outputs\my_run\ai_improvement_approved_actions.json `
  --approval-ids approval_2079 `
  --parallel-mode temporal
```

Use `--approved-action-id` only to record one explicit highlight or camera follow-up action from `--approved-actions-path`. `--approval-ids` can be supplied in the same command for separate bounded rerun approvals; each flag consumes only the ids it names, and neither implies approval of every action in the file. Targeted ball-recovery reruns must use `--approval-ids` from an explicit approval file. Highlight rendering should still be treated as an explicit operator action, not a side effect of an approval artifact.

`--approval-ids` only filters actions from the explicitly supplied approval file. In non-dry-run runs, unknown ids, duplicate ids, missing approval files, or malformed approval payloads fail the workflow before provider-backed improvement work starts. In `--dry-run` or `--mode dry-run`, the workflow records the same problem in `warnings` so an operator can rehearse the command safely.

The workflow report records `approval_selection` with `approval_source`, requested, consumed, skipped, and unknown action ids. This makes it clear which approvals were actually used.

## Quality Gate Modes

`--mode dry-run` is provider-safe and may report unavailable checks as warnings.

`--mode artifact-only` is the default when `--dry-run` is not set. It checks existing artifacts without requiring a real provider run.

`--mode real` treats missing required artifacts as quality-gate failures. Quality-gate or workflow failures return exit code `1` only when `--dry-run` is not set. Other CLI failures, such as invalid arguments or approval-selection errors, can also return nonzero.

## Candidate Comparison And Promotion

AI improvement artifacts use three shared roles. The full status and promotion policy is defined in the contract document; this section is the workflow-facing summary:

- `baseline`: the existing output used as the comparison source.
- `candidate`: an alternative output produced by a bounded improvement attempt.
- `final`: the artifact selected for downstream use after comparison and promotion checks.

Candidate comparison reports are pure JSON files, normally named `*_comparison.json`. They do not copy media and must not mutate `ball_track.csv` or `ball_track.cleaned.csv`. Each report records `problem_type`, `baseline`, `candidate`, optional `approval`, `checks`, and a `summary` status. Shared statuses are `pass`, `warn`, `fail`, and `unavailable`. The quality gate derives comparison status from `checks`; an empty check list or a summary/check mismatch is not accepted as a clean pass.

Promotion uses the status derived from validated comparison checks as the contract:

- `pass` candidates may be promoted into final artifacts.
- `warn` candidates require both `requires_human_confirmation: true` on the final artifact and a consumed approval for the same `candidate_id` with `approval_type: human_confirmation`; otherwise they stay out of final output and are recorded as rejected or pending confirmation.
- `fail` candidates are recorded in `rejected_candidates` and cannot be promoted.
- `unavailable` candidates are not promoted until a usable comparison exists.

The final promotion manifest is `final_ai_improvement_artifact_manifest.json`. It records baseline output, candidate outputs, final selected artifacts, consumed approvals, comparison reports, quality gate status, rejected candidates, warnings, videos, and clips by path/status only. It is a manifest, not a media copier.

The quality gate reads direct `*_comparison.json` reports and comparison references in `final_ai_improvement_artifact_manifest.json`. Its `candidate_comparisons_ok` check and `summary.candidate_comparisons` field summarize pass, warn, fail, and unavailable comparison statuses for later missing-ball, noise, follow-cam, and highlight candidate workflows. Missing comparison reports remain backward compatible only when no candidate output or final selection exists; once a manifest contains candidates, missing or invalid comparisons become unavailable or failing evidence rather than a pass.

## Model Guidance

Use the configured strong model for run-level improvement and hard recovery cases. Reserve smaller or cheaper models for low-risk tagging and dry-run smoke checks. Provider mode, candidate intent, resolved model, model-selection source, and whether the call can lead to executable candidates are visible in the workflow/improvement artifacts so operators can distinguish strong-model candidate work from low-risk review-only output.

When the improvement-capable model is unavailable in real mode, the workflow falls back to non-mutating review-only/unavailable behavior. It does not use a small-model fallback to create executable approvals.

Hard cases include long missing-ball gaps, camera-audit failures, follow-cam regressions, highlight-tail decisions, and candidate promotions that would otherwise land as `warn`. These should use a stronger configured model when a provider is available. Without a provider, record artifact-only evidence and mark provider-dependent decisions `warn` or `unavailable`; do not document them as `pass`.

## AI Suggestion Contract

Run-level AI suggestions must be evidence-backed before they are eligible for approval. See the contract document for the final gating rules; the stable workflow enforces these operator-facing constraints:

- Long missing-ball or lost-gap suggestions must cover the full lost gap, or explicitly describe uncovered subwindows.
- ROI and localization suggestions must cite a `source_packet_id`, `visual_review_id`, or a `visual_localization_id` from `ai_visual_localization.json`.
- `not_visible` is only valid when packet or visual evidence supports that the ball is hidden, off-frame, or impossible to identify.
- Noise suggestions must use bounded frame windows, evidence ids, and an accepted false-positive class: `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, `sideline_confusion`, `wall_background_drift`, `unknown_false_positive`, or `unknown`.
- Camera suggestions must distinguish tracking recovery from follow-cam tuning. Lost or Predicted ball-track context should produce `tracking_rerun_before_follow_cam`; stable Detected context can produce `adjust_follow_cam`.
- Highlight suggestions must preserve the candidate `core_window` and required tail unless the source-video end clamps the tail.

Generated missing-ball and noise candidates are written as pending finalization, even when their comparison status is `pass`. They become final output only through `finalize_ai_candidate` with `missing_ball_track` or `noise_cleaned_track`; `warn` candidates require explicit confirmation, while `fail`, `unavailable`, review-only, missing-approval, and unknown-candidate cases cannot promote.

## Real-Video Validation Record

For PR8 or later real-video runs, record the exact command, source video, output directory, parallel mode, model/mode, stage timing, final manifest, visual checks, and final playable outputs in [real-video-ai-improvement-validation.md](real-video-ai-improvement-validation.md). The validation record should explicitly cover the frame `2079` right-bottom lost-ball window, at least one dense-noise window, at least one camera spike or follow-cam motion event, at least one highlight tail, and whether final tracking, follow-cam, and highlight media are decodable. Use baseline/candidate overlay or media-sheet evidence for those checks; file existence alone is not acceptance evidence.
