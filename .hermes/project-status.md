# Production Workflow Managed PR Status

- Program: Production Workflow and review-evidence provisioning
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR4A · Review Evidence Provisioning
- Current branch: codex/review-evidence-provisioning
- Current phase: remote_feedback_wait
- Checks: fresh branch created from main at 1dd538c; PR4 WIP df709a7 independently reviewed SAFE, fully validated, and backed up at origin/codex/full-production-review-render; heartbeat expanded to seven PRs; all prior and latest PR4A findings have implementation/test remediation; official Python verification passed 1,506 tests with 9 skipped in 326.2 seconds; native unmocked multi-video E2E now uses the documented input/dataset/annotations/predictions/roles/policy layout and spans real training, 1,472 qualification evidence items, frozen-model re-inference, tamper rejection, target inference/policy, bundle/import/activation, review actions, materialization, production recompute, and exact lineage/no-retrain assertions; classifier batch size is capped at 128 in training, replay, inference, low-level prediction, and trajectory consumption, with 128 accepted and 129/10^12 rejected before model/tensor work; frontend passed 322 tests with 94.01% statement, 92.91% branch, 100% function, and 95.91% line coverage; production/test/generated-client TypeScript, Ruff, OpenAPI freshness, git diff, and pnpm workspace/lock checks passed; 390 px Broadcast visual QA has no horizontal overflow
- Blockers: no PR4A engineering blockers; GitHub Node and Python checks are running. Real qualification remains externally blocked by zero target labels, only one independent local match group, no qualified bundle, no immutable config snapshot matching the confirmed digest for the current real run, and no trusted reviewer team; these gaps keep PR4 paused after PR4A and do not authorize a synthetic queue.
- Next: after this status update is pushed, wait the full remote-feedback window from 2026-07-16T03:43:30Z, then triage every Copilot/reviewer comment and CI result; fix valid feedback and restart the window, or merge only when all gates remain green.
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this seven-PR program through PR1, PR2, PR3, PR4A, PR4, PR5, and PR6; then delete the heartbeat and verify removal.
- Last updated: 2026-07-15 23:42 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | remote_feedback_wait · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/107 · 80b4ed5 | Window starts 2026-07-16T03:43:30Z after status push | Pending |
| PR4 | codex/full-production-review-render | paused_waiting_for_qualified_target_queue · WIP df709a7 backed up on origin | Pending | Pending | Pending |
| PR5 | codex/grouped-production-history | pending | Pending | Pending | Pending |
| PR6 | codex/production-workflow-cutover | pending | Pending | Pending | Pending |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
| PR1 | Node CI: axe scanned the controlled AlertDialog closing transition while main remained aria-hidden | Valid CI failure | Restore focus in product on close; wait for dialog detach and aria-hidden cleanup before axe; retain all accessibility rules and severity thresholds; re-review, validate, push, and restart the 10-minute feedback window |
| PR2 | Copilot thread PRRT_kwDORq0Zkc6RFffL: first polygon creation announces that prior frame confirmations were cleared | Valid | Announce framesClearedAfterEdit only when confirmed frames actually existed; add aria-live regression coverage, re-review, validate, push, and restart the 10-minute feedback window |
