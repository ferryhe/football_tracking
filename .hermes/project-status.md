# Production Workflow Managed PR Status

- Program: Production Workflow and review-evidence provisioning
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR4A · Review Evidence Provisioning
- Current branch: codex/review-evidence-provisioning
- Current phase: backend_implementation
- Checks: fresh branch created from main at 1dd538c; PR4 WIP df709a7 independently reviewed SAFE, fully validated, and backed up at origin/codex/full-production-review-render; heartbeat expanded to seven PRs; architecture and evidence feasibility reviews completed; final independent PR4A specification review COMPLIANT with 0 Critical, 0 Important, and 2 resolved Minor suggestions
- Blockers: no blocker to implementing the fail-closed PR4A bundle/import shell after final spec re-review. Real qualification remains blocked: current target has 6,406 application candidates and zero confirmed labels; local evidence has one independent match group versus at least three for classifier splitting; current policy contract may require hundreds to more than one thousand independent videos; no qualified bundle or trusted reviewer team exists. Target candidates cannot train or qualify their own model.
- Next: implement bundle builder/validator and managed backend import fixture-first, then integrate the shared Production/Broadcast UI and run independent specification/code-quality reviews.
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this seven-PR program through PR1, PR2, PR3, PR4A, PR4, PR5, and PR6; then delete the heartbeat and verify removal.
- Last updated: 2026-07-15 17:55 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | in_progress_backend_implementation | Pending | Pending | Pending |
| PR4 | codex/full-production-review-render | paused_waiting_for_qualified_target_queue · WIP df709a7 backed up on origin | Pending | Pending | Pending |
| PR5 | codex/grouped-production-history | pending | Pending | Pending | Pending |
| PR6 | codex/production-workflow-cutover | pending | Pending | Pending | Pending |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
| PR1 | Node CI: axe scanned the controlled AlertDialog closing transition while main remained aria-hidden | Valid CI failure | Restore focus in product on close; wait for dialog detach and aria-hidden cleanup before axe; retain all accessibility rules and severity thresholds; re-review, validate, push, and restart the 10-minute feedback window |
| PR2 | Copilot thread PRRT_kwDORq0Zkc6RFffL: first polygon creation announces that prior frame confirmations were cleared | Valid | Announce framesClearedAfterEdit only when confirmed frames actually existed; add aria-live regression coverage, re-review, validate, push, and restart the 10-minute feedback window |
