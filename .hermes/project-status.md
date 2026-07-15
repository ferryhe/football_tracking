# Production Workflow Managed PR Status

- Program: Production Workflow frontend consolidation
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR1 · Production Workflow Foundation
- Current branch: codex/production-workflow-foundation
- Current phase: ready_to_republish
- Checks: valid Copilot comment fixed; independent feedback spec review compliant and quality review safe to publish; Web tests, test/runtime typechecks, 5 Playwright tests, and build pass
- Blockers: none
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this six-PR program until PR1–PR6 are merged; then delete the heartbeat and verify removal.
- Last updated: 2026-07-15 06:10 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | ready_to_republish | https://github.com/ferryhe/football_tracking/pull/104 | Reset after feedback-fix push | Pending |
| PR2 | codex/interactive-field-calibration | pending | Pending | Pending | Pending |
| PR3 | codex/trial-tuning-and-config-freeze | pending | Pending | Pending | Pending |
| PR4 | codex/full-production-review-render | pending | Pending | Pending | Pending |
| PR5 | codex/grouped-production-history | pending | Pending | Pending | Pending |
| PR6 | codex/production-workflow-cutover | pending | Pending | Pending | Pending |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
