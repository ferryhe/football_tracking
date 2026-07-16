# Production Workflow Managed PR Status

- Program: Production Workflow and review-evidence provisioning
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR4 · Full Production, Review, and Render
- Current branch: codex/full-production-review-render
- Current phase: evidence_feasibility_audited · independent resume audit PAUSED
- Checks: PR4A merged as PR #107 at 2026-07-16T04:46:51Z (`3546280`) after two complete remote-feedback windows; Node and Python CI passed; the only Copilot thread was fixed, replied to, and resolved. PR4 now integrates the same fail-closed evidence controller in both legacy Broadcast and the real Production full-run review stage; candidate review requires an evidence-ready queue with an exact SHA match while terminal-tail review remains independent. An independent combined quality review found three Important queue/target/generation consistency gaps plus one Minor inventory-exhaustiveness gap; all were fixed, regression tested, and the second review returned APPROVE with zero remaining findings. Review POST now binds the viewed queue SHA under lock, active consumers continuously validate the current target, revoke/recovery republishes a fresh facade generation, and inventory rejects undeclared or special filesystem nodes. The final combined branch passed the official Python route with 1,573 tests and 13 platform skips in 354.192 seconds, focused remediation coverage with 164 tests/5 skips/27 subtests, Web Vitest 427/427 with 92.52% statements, 91.38% branches, 99.36% functions, and 94.71% lines, Chromium 34/34, broadcastWorkflow 81/81, shared-library/product/test TypeScript, production Web build, generated-client byte idempotence (`3C265CE2...F4DF`), OpenAPI freshness, and diff/workspace/lock/port-cleanup checks. An independent post-merge evidence audit still returns PAUSED: the activated queue, target qualification artifacts, matching immutable configuration snapshot, and independent visual audit do not exist.
- Blockers: PR4A supplies the fail-closed evidence contract and tooling but does not manufacture qualification. The real target still has 6,406 candidates and zero labels, only one independent local match group, no qualified bundle, no trusted independent auditor team, and no immutable canonical snapshot even though the configuration content is recoverable. The current CRLF file hashes to `c203ae...69cc` as raw bytes but its LF-normalized text hashes exactly to the confirmed `6fd624...da4c`, showing a hash-canonicalization/snapshot-provenance gap rather than changed configuration content. A separate policy feasibility audit returned `INFEASIBLE_CURRENT_CONTRACT`: one independent match/source component contributes only one evaluation candidate, so the unchanged exact gates require roughly 738–1,151 ideally role-directed independent policy groups or 791–1,656 naturally balanced true-ball groups, not merely three matches. PR4 stays paused; default acceptance, fabricated labels, leakage-prone evaluation, and empty-queue synthesis remain prohibited.
- Next: preserve the old run and both raw/canonical hashes, then design a reviewed canonical LF immutable snapshot/reconfirmation procedure without rewriting run history. Do not start large-scale annotation under the current infeasible policy contract. Continuing toward a qualified queue now requires separate user approval for a policy-statistics defect fix or an explicit exhaustive-human route; after that decision, acquire the required independent matches and reviewer/auditor roles and produce frozen evidence packages.
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this seven-PR program through PR1, PR2, PR3, PR4A, PR4, PR5, and PR6; then delete the heartbeat and verify removal.
- Last updated: 2026-07-16 01:49 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | completed · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/107 · `77adbc4` | Final window 2026-07-16T04:34:00Z–04:44:00Z; Node/Python green; Copilot thread resolved | Merged 2026-07-16T04:46:51Z · 3546280 |
| PR4 | codex/full-production-review-render | paused_waiting_for_qualified_target_queue · validated integration `9439435` backed up on origin | Pending | Pending | Pending |
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
