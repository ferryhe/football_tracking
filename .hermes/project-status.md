# Production Workflow Managed PR Status

- Program: Production Workflow frontend consolidation
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR3 · Trial, Tuning, and Configuration Freeze
- Current branch: codex/trial-tuning-and-config-freeze
- Current phase: ready_to_publish
- Checks: spec COMPLIANT and quality SAFE with 0 findings; root gates pass with Python 1450, Broadcast 80+4, Vitest 267, Playwright 24, coverage, typechecks/build/frozen lock/diff
- Blockers: none
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this six-PR program until PR1–PR6 are merged; then delete the heartbeat and verify removal.
- Last updated: 2026-07-15 11:33 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | ready_to_publish | Pending | Pending | Pending |
| PR4 | codex/full-production-review-render | pending | Pending | Pending | Pending |
| PR5 | codex/grouped-production-history | pending | Pending | Pending | Pending |
| PR6 | codex/production-workflow-cutover | pending | Pending | Pending | Pending |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
| PR1 | Node CI: axe scanned the controlled AlertDialog closing transition while main remained aria-hidden | Valid CI failure | Restore focus in product on close; wait for dialog detach and aria-hidden cleanup before axe; retain all accessibility rules and severity thresholds; re-review, validate, push, and restart the 10-minute feedback window |
| PR2 | Copilot thread PRRT_kwDORq0Zkc6RFffL: first polygon creation announces that prior frame confirmations were cleared | Valid | Announce framesClearedAfterEdit only when confirmed frames actually existed; add aria-live regression coverage, re-review, validate, push, and restart the 10-minute feedback window |
