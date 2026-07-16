# Production Workflow Managed PR Status

- Program: Production Workflow, review-evidence provisioning, target audit, and configuration-lineage reconfirmation
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: in_progress
- Current PR: PR4B · Target Audit and Configuration-Lineage Reconfirmation
- Current branch: codex/target-audit-config-lineage
- Current phase: final_validation_complete · specification COMPLIANT · quality APPROVE · preparing GitHub PR
- Approval: Option A approved by the user on 2026-07-16; PR4B combines the target-scoped frozen-population audit and append-only configuration-lineage reconfirmation, and the managed program now contains eight PRs.
- PR4B implementation result: the target-only finite-population audit now traverses the real 16-field policy decision producer through a label-independent, domain-separated sample plan, validated blind votes and adjudication, qualification, queue, evidence bundle, and Broadcast activation. The server owns one append-only target commitment, exact run/population/model/config/policy bindings, target labels are training-ineligible, textual metadata is scanned for leakage, and the adjudication queue is derived only from validated annotation resolution. Configuration lineage uses append-only durable publication, typed API blockers, transactional workflow authority, component-handle validation on POSIX, hardened retained directory handles and identity checks on Windows, and fail-closed conflict handling.
- PR4B independent reviews: eight specification-review cycles closed every Critical and Important finding; the final specification verdict is COMPLIANT with no findings. Four separate quality/security review cycles closed queue aliasing, nested-package smuggling, Windows rename/substitution, commitment-registry concurrency, and config-authority consistency findings; the final quality verdict is APPROVE with no Critical, Important, or Minor findings.
- Checks: the root-controlled final gate passed OpenAPI freshness and the official Python route with 1,586 tests and 50 platform/fixture skips in 414.405 seconds. The direct frontend gate passed broadcastWorkflow 80, broadcastComponents 4, Vitest 322/322 across 13 files with 94.01% statements, 92.93% branches, 100% functions, and 95.91% lines, Chromium 24/24, shared-library/product/test TypeScript, and the production Vite build. Focused final adjudication coverage passed 7 tests/36 subtests; the complete bundle set passed 38 tests/18 skips/36 subtests; the principal target/config/API set passed 224 tests/25 skips/154 subtests; native integration passed 1 test/1 platform skip; Windows hardening passed 30 tests/6 skips/17 subtests. Ruff on the changed Python surface, generated-client byte idempotence, diff checks, and workspace/lock integrity passed. The pnpm wrapper remains locally blocked by the existing ignored-esbuild-script policy, so equivalent installed tools were invoked directly without changing package, workspace, or lock policy.
- Blockers: PR4B has no remaining implementation or review blocker. External operational evidence is still absent: the real target has 6,406 candidates and zero labels, no qualified bundle, no trusted independent auditor team, and no published immutable canonical snapshot. PR4 remains paused; default acceptance, fabricated labels, target-label training/tuning leakage, adaptive post-audit tuning, history rewriting, and empty-queue synthesis remain prohibited.
- Next: commit and push the reviewed PR4B implementation, open the GitHub pull request, start a fresh full 10-minute remote-feedback window after the latest material push, classify and resolve every valid or ambiguous Copilot/reviewer/CI issue, and merge only when all required checks are green.
- Heartbeat: ACTIVE
- Heartbeat automation id: production-workflow-multi-pr-heartbeat
- Heartbeat interval: 15 minutes
- Heartbeat scope: Continue and report this eight-PR program through PR1, PR2, PR3, PR4A, PR4B, PR4, PR5, and PR6; then delete the heartbeat and verify removal.
- Last updated: 2026-07-16 12:30 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | completed · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/107 · `77adbc4` | Final window 2026-07-16T04:34:00Z–04:44:00Z; Node/Python green; Copilot thread resolved | Merged 2026-07-16T04:46:51Z · 3546280 |
| PR4B | codex/target-audit-config-lineage | final_validation_complete · spec COMPLIANT · quality APPROVE | Pending | Pending | Pending |
| PR4 | codex/full-production-review-render | paused_waiting_for_pr4b_and_qualified_target_queue · validated integration `9439435` backed up on origin | Pending | Pending | Pending |
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
