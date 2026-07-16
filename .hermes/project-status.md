# Production Workflow Managed PR Status

- Program: Production Workflow and review-evidence provisioning
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR4A · Review Evidence Provisioning
- Current branch: codex/review-evidence-provisioning
- Current phase: remote_feedback_remediation_ready_for_push
- Checks: the first complete feedback window for PR #107 identified one valid Copilot dataset-binding concern, a Node generated-client freshness failure, and a Python clean-environment dependency failure. Commit f69f3c6 fixes all three: candidate annotation resolution now requires a stable no-follow dataset-sibling source-contract binding, rejects output aliases, revalidates file identity/hash, and preserves external ledgers through explicit package descriptors; Dev and Qual runbook/native layouts both provide hash-equal binding copies; canonical generated clients match exact Orval output; and TestClient's httpx dependency is declared and CI-pinned. Independent results are spec COMPLIANT, quality APPROVE, and CI dependency PASS. Official Python verification passed 1,510 tests with 11 platform skips in 331.5 seconds; native unmocked evidence/recompute chain passed in 89.432 seconds; frontend passed 322 tests with 94.01% statement, 92.93% branch, 100% function, and 95.91% line coverage; Chromium passed 24 journeys; generated-client idempotence, production/test/generated-client TypeScript, all builds, Ruff, OpenAPI freshness, git diff, workspace/lock, and test-process cleanup passed; 390 px Broadcast visual QA remains valid.
- Blockers: no local PR4A engineering blockers; GitHub Node/Python checks and remote feedback must rerun after the remediation push. Real qualification remains externally blocked by zero target labels, only one independent local match group, no qualified bundle, no immutable config snapshot matching the confirmed digest for the current real run, and no trusted reviewer team; these gaps keep PR4 paused after PR4A and do not authorize a synthetic queue.
- Next: push f69f3c6 plus this status update, record the new remote-feedback start time, wait a full 10 minutes without reading remote feedback, then triage every Copilot/reviewer thread and CI result; merge only if all gates are green and no actionable feedback remains.
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this seven-PR program through PR1, PR2, PR3, PR4A, PR4, PR5, and PR6; then delete the heartbeat and verify removal.
- Last updated: 2026-07-16 00:33 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | remediation_ready_for_push · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/107 · f69f3c6 | First window completed after 2026-07-16T03:53:30Z; replacement window starts after remediation/status push | Pending |
| PR4 | codex/full-production-review-render | paused_waiting_for_qualified_target_queue · WIP df709a7 backed up on origin | Pending | Pending | Pending |
| PR5 | codex/grouped-production-history | pending | Pending | Pending | Pending |
| PR6 | codex/production-workflow-cutover | pending | Pending | Pending | Pending |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
| PR1 | Node CI: axe scanned the controlled AlertDialog closing transition while main remained aria-hidden | Valid CI failure | Restore focus in product on close; wait for dialog detach and aria-hidden cleanup before axe; retain all accessibility rules and severity thresholds; re-review, validate, push, and restart the 10-minute feedback window |
| PR2 | Copilot thread PRRT_kwDORq0Zkc6RFffL: first polygon creation announces that prior frame confirmations were cleared | Valid | Announce framesClearedAfterEdit only when confirmed frames actually existed; add aria-live regression coverage, re-review, validate, push, and restart the 10-minute feedback window |
| PR4A | Copilot thread PRRT_kwDORq0Zkc6RUM6Z: annotation resolution persisted a basename source-contract binding without proving a resolvable dataset sibling | Valid core concern; proposed ledger co-location was not applicable | Require a stable regular no-follow dataset-sibling source-contract copy with exact SHA and unchanged identity; reject output alias, symlink/reparse, identity swap, and FIFO cases; retain ledger replay through its explicit package descriptor; align Dev/Qual runbook and native fixtures; independently re-review |
| PR4A | Node CI: exact Orval regeneration changed two generated API files | Valid CI failure | Commit the canonical postprocess output without an additional formatting pass; prove repeated direct codegen is byte-idempotent; rerun web, Chromium, typecheck, and build gates |
| PR4A | Python CI: new TestClient modules could not import because clean requirements omitted httpx | Valid CI failure | Declare httpx only in dev requirements within Starlette's supported range, pin CI to 0.28.1, rerun targeted integration tests and the official full Python route |
