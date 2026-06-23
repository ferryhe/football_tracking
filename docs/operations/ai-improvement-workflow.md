# Stable AI Improvement Workflow

This workflow runs against an existing tracking output directory. It is meant to make AI review repeatable without letting advisory artifacts mutate tracking output by surprise.

## Command

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --dry-run `
  --parallel-mode temporal
```

The workflow writes `stable_ai_improvement_workflow_report.json` with the planned stages, hash snapshots, produced artifacts, quality-gate summary, strategy, warnings, and explicit approval intent.

## Stage Order

1. Metrics/artifacts refresh.
2. `before_review` track hash snapshot.
3. Review packets.
4. Optional visual review.
5. Run-level AI improvement.
6. `after_ai_improvement` track hash snapshot.
7. Explicit approved child rerun plan.
8. Optional follow-cam rerender plan.
9. Optional highlight render plan.
10. AI improvement quality gate.

In `--dry-run`, provider calls and expensive video work are not required. Hash snapshots and lightweight JSON reports may still be written so the quality gate has stable inputs.

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

`--mode real` treats missing required artifacts as failures. The CLI returns nonzero only for a failing quality gate in non-dry-run real mode.

## Model Guidance

Use a stronger model for run-level improvement and hard recovery cases. Reserve smaller or cheaper models for low-risk tagging and dry-run smoke checks.

## AI Suggestion Contract

Run-level AI suggestions must be evidence-backed before they are eligible for approval:

- Long missing-ball or lost-gap suggestions must cover the full lost gap, or explicitly describe uncovered subwindows.
- ROI and localization suggestions must cite a `source_packet_id` or `visual_review_id`.
- `not_visible` is only valid when packet or visual evidence supports that the ball is hidden, off-frame, or impossible to identify.
- Noise suggestions must use bounded frame windows and an accepted false-positive class: `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, `sideline_confusion`, `wall_background_drift`, `unknown_false_positive`, or `unknown`.
- Camera suggestions must distinguish tracking recovery from follow-cam tuning. Lost or Predicted ball-track context should produce `tracking_rerun_before_follow_cam`; stable Detected context can produce `adjust_follow_cam`.
- Highlight suggestions must preserve the candidate `core_window` and required tail unless the source-video end clamps the tail.
