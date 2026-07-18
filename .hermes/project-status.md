# Step 3 Tiny-Ball Managed PR Status

- Program: Step 3 Tiny-Ball Detection, Tuning, and Camera Adaptation
- Canonical plan: docs/superpowers/plans/2026-07-17-step-3-tiny-ball-detection-and-camera-adaptation-plan.md
- Program status: active · five managed PRs authorized
- Current PR: PR-T1 · Fail-Closed Trial Diagnosis and Recovery Actions
- Current branch: codex/tiny-ball-trial-diagnosis
- Current phase: PR #112 open · two Copilot findings corrected and independently re-approved · replacement remote-feedback window scheduled for 2026-07-18T02:12:00Z–2026-07-18T02:22:00Z
- Approval: the user authorized the final independently reviewed plan and the established managed-PR operating method on 2026-07-17.
- Checks: the latest official Python route passed 1,748 tests with 64 skipped in 465.219 seconds. Feedback-focused gates passed backend 63 with 56 subtests, Production page 17/17, both TypeScript configurations, Ruff, and `git diff --check`; independent specification and quality/security re-reviews both APPROVE. Prior post-correction gates passed API 156 with 2 skipped and 14 subtests; config/provider/OpenAPI 34 with 6 subtests; Web Vitest 538/538; Chromium 43/43; production build and cutover bundle inspection; and two byte-identical OpenAPI/React/Zod generations. Test-only ports are closed and the user's existing 5173 service remains running.
- Blockers: none. Two Copilot findings were corrected: collected-count status/value invariants are fail-closed, and the stable workspace mount identity is now explicit and regression-tested across source/calibration lineage rotation versus full draft replacement.
- Next: push the approved corrections and this status record; reply to and resolve both Copilot threads; observe the complete replacement 10-minute remote-feedback window through 2026-07-18T02:22:00Z; restart after any material push; merge only when replacement CI and thread-aware audits are clear; then start PR-T2 from refreshed main
- Heartbeat: ACTIVE
- Heartbeat automation id: tiny-ball-step3-managed-pr-heartbeat
- Heartbeat interval: every 15 minutes
- Heartbeat scope: PR-T1 through PR-T5, including checks, feedback windows, merges, and cleanup
- Dirty-baseline preservation: the pre-existing line-ending-only modification to `docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md` is unrelated user state and must not be staged or changed by this program
- Last updated: 2026-07-17 ET

## Current Program PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR-T1 | codex/tiny-ball-trial-diagnosis | open · Copilot corrections complete · spec APPROVE · quality/security APPROVE · checks/feedback monitoring | https://github.com/ferryhe/football_tracking/pull/112 | replacement window scheduled 2026-07-18T02:12:00Z–2026-07-18T02:22:00Z | pending |
| PR-T2 | pending | not started | pending | not started | pending |
| PR-T3 | pending | not started | pending | not started | pending |
| PR-T4 | pending | not started | pending | not started | pending |
| PR-T5 | pending | not started | pending | not started | pending |

---

# Production Workflow Managed PR Status

- Program: Production Workflow, review-evidence provisioning, target audit, and configuration-lineage reconfirmation
- Canonical plan: docs/superpowers/plans/2026-07-14-production-workflow-final-managed-pr-plan.md
- Program status: completed · all eight engineering PRs merged
- Current PR: none · PR1, PR2, PR3, PR4A, PR4B, PR4, PR5, and PR6 completed
- Current branch: main
- Current phase: engineering program complete · PR #111 merged as `be46bc4` · heartbeat deleted and removal verified · operational release gate remains paused
- Approval: Option A approved by the user on 2026-07-16; PR4B combines the target-scoped frozen-population audit and append-only configuration-lineage reconfirmation, and the managed program now contains eight PRs. On 2026-07-16 the user also approved separating the engineering merge gate from the operational release gate: PR4 code may proceed through managed review and merge without a four-person evidence team, while real evidence and independent visual audit remain mandatory before any output is represented as production-validated.
- Checks: PR6 final local validation passed 505/505 Vitest tests with 93.68% statements, 91.52% branches, 99.48% functions, and 95.48% lines; application and test TypeScript; production build; enhanced bundle inspection with one initial, seven Production-static, and fifteen dynamic chunks; Konva absent from the initial and Production static closures; FieldPolygonEditor retained as a distinct dynamic chunk; and full Chromium 39/39 including exact-run cutover, browser-error monitoring, accessibility, mobile focus, bilingual/theme checks, History state screenshots, and 1,000-run lazy evidence. Copilot's sole valid pending-identity progress-status finding was fixed, replied to, resolved, and independently re-reviewed. The final 2026-07-17T05:45:13.4520917Z–05:55:13.4520917Z window completed on head `85d8844`; GitHub Actions run 29558137441 passed Node and Python; final REST and thread-aware audits found no other valid or ambiguous feedback. PR #111 merged at 2026-07-17T05:56:10Z as `be46bc4`.
- Blockers: no engineering PR blocker remains. The operational release gate is still blocked: the real target remains 6,406 candidates with zero labels and has no frozen qualified target audit, qualified evidence bundle, activated exact-SHA review queue, trusted independent reviewer evidence, independent visual activation audit, or published append-only configuration-lineage reconfirmation generation. The official full-video release candidate and one known real-match source-to-final-media validation remain release work. Default acceptance, fabricated labels, target-label training/tuning leakage, adaptive post-audit tuning, history rewriting, and empty-queue synthesis remain prohibited; no output may be represented as production-validated until those runtime gates pass.
- Next: no managed engineering PR remains. Resume only the separately governed operational release work: configuration-lineage publication, frozen target qualification, exact-SHA review queue activation, trusted independent visual activation audit, official full-video validation, known real-match source-to-final-media validation, and an explicit operator release decision.
- Heartbeat: INACTIVE · deleted after all eight PRs merged
- Heartbeat automation id: production-workflow-multi-pr-heartbeat · deletion verified (`AUTOMATION_NOT_FOUND`)
- Heartbeat interval: none
- Heartbeat scope: completed
- Last updated: 2026-07-17 01:57 ET

## PR Ledger

| PR | Branch | Status | Pull request | Remote feedback started | Merge |
| --- | --- | --- | --- | --- | --- |
| PR1 | codex/production-workflow-foundation | completed | https://github.com/ferryhe/football_tracking/pull/104 | Final window started 2026-07-15T10:32:54.4422764Z | Merged 2026-07-15T10:43:22Z · ea52548 |
| PR2 | codex/interactive-field-calibration | completed | https://github.com/ferryhe/football_tracking/pull/105 | Final window completed 2026-07-15T12:29:00.338Z | Merged 2026-07-15T12:29:09Z · c781b34 |
| PR3 | codex/trial-tuning-and-config-freeze | completed | https://github.com/ferryhe/football_tracking/pull/106 | Completed 2026-07-15T15:46:04Z with no actionable feedback | Merged 2026-07-15T15:46:13Z · 1dd538c |
| PR4A | codex/review-evidence-provisioning | completed · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/107 · `77adbc4` | Final window 2026-07-16T04:34:00Z–04:44:00Z; Node/Python green; Copilot thread resolved | Merged 2026-07-16T04:46:51Z · 3546280 |
| PR4B | codex/target-audit-config-lineage | completed · spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/108 · `c3cc780` | Final window 2026-07-16T17:22:20.107Z–17:32:20.107Z; Node/Python green; 2 valid Copilot threads resolved; no remaining valid or ambiguous feedback | Merged 2026-07-16T17:32:36Z · 3ee9091 |
| PR4 | codex/full-production-review-render | completed · final spec COMPLIANT · quality APPROVE | https://github.com/ferryhe/football_tracking/pull/109 · `e1870ca` | Final window 2026-07-17T01:12:13.411Z–01:22:13.411Z; run 29546835802 Node/Python green; final thread-aware audit clear; Copilot service errors non-actionable | Merged 2026-07-17T01:24:48Z · 6e22866 |
| PR5 | codex/grouped-production-history | completed · final spec COMPLIANT · quality/security APPROVE | https://github.com/ferryhe/football_tracking/pull/110 · `0b547f1` | Final window 2026-07-17T04:19:30.607Z–04:29:30.607Z; run 29554554535 Node/Python green; valid Copilot thread resolved; final thread-aware audit clear | Merged 2026-07-17T04:30:16Z · b55fb2f |
| PR6 | codex/production-workflow-cutover | completed · final spec COMPLIANT · quality/security APPROVE | https://github.com/ferryhe/football_tracking/pull/111 · final head `85d8844` | Final window 2026-07-17T05:45:13.4520917Z–05:55:13.4520917Z; run 29558137441 Node/Python green; valid Copilot thread fixed, replied to, resolved; final thread-aware audit clear | Merged 2026-07-17T05:56:10Z · be46bc4 |

## Remote Feedback Decisions

| PR | Feedback | Classification | Decision |
| --- | --- | --- | --- |
| PR6 | Copilot thread PRRT_kwDORq0Zkc6RqPBp: non-current run identity lookup safely disabled actions but provided no visible or accessible pending status | Valid | Add bilingual `role=status`/polite/atomic progress messaging while retaining list-only identity lookup and zero controller/current-run/write/button behavior; cover pending-to-recognized and pending-to-conflict transitions; independently re-review; push and restart the complete feedback window |
| PR5 | Copilot thread 3600212785: raw backslash, forward-slash, and trailing-slash variants could split the same input source and destabilize its alias | Valid | Canonicalize group identities while preserving Windows drive roots; deterministically merge duplicate source/config/run records; fail closed on conflicting identity evidence; add forward/reverse, unbound, conflict, drive-root, 1,000-run, coverage, and browser regressions; independently re-review; push once and restart the full feedback window |
| PR1 | Copilot discussion 3584477795: production tsconfig includes test files and test-only globals | Valid | Move test files and typings to a dedicated test tsconfig, retain explicit test typechecking, re-review, validate, push, and restart the 10-minute feedback window |
| PR1 | Node CI: axe scanned the controlled AlertDialog closing transition while main remained aria-hidden | Valid CI failure | Restore focus in product on close; wait for dialog detach and aria-hidden cleanup before axe; retain all accessibility rules and severity thresholds; re-review, validate, push, and restart the 10-minute feedback window |
| PR2 | Copilot thread PRRT_kwDORq0Zkc6RFffL: first polygon creation announces that prior frame confirmations were cleared | Valid | Announce framesClearedAfterEdit only when confirmed frames actually existed; add aria-live regression coverage, re-review, validate, push, and restart the 10-minute feedback window |
| PR4A | Copilot thread PRRT_kwDORq0Zkc6RUM6Z: annotation resolution persisted a basename source-contract binding without proving a resolvable dataset sibling | Valid core concern; proposed ledger co-location was not applicable | Require a stable regular no-follow dataset-sibling source-contract copy with exact SHA and unchanged identity; reject output alias, symlink/reparse, identity swap, and FIFO cases; retain ledger replay through its explicit package descriptor; align Dev/Qual runbook and native fixtures; independently re-review |
| PR4A | Node CI: exact Orval regeneration changed two generated API files | Valid CI failure | Commit the canonical postprocess output without an additional formatting pass; prove repeated direct codegen is byte-idempotent; rerun web, Chromium, typecheck, and build gates |
| PR4A | Python CI: new TestClient modules could not import because clean requirements omitted httpx | Valid CI failure | Declare httpx only in dev requirements within Starlette's supported range, pin CI to 0.28.1, rerun targeted integration tests and the official full Python route |
| PR4B | Copilot thread PRRT_kwDORq0Zkc6RhRco: target finite-population missing-binding error described target-audit fields as qualification bindings | Valid | Report the target-audit domain and deterministically list the exact missing binding names; add exact-message regression coverage |
| PR4B | Copilot thread PRRT_kwDORq0Zkc6RhRc9: workflow-binding JSON normalization could leak TypeError/ValueError as a 500 | Valid | Convert non-JSON and circular values into typed `CONFIG_LINEAGE_MISMATCH`, preserve pre-existing `ConfigLineageError` objects/codes/messages, and cover unit plus service propagation |
| PR4B | Python CI runs 29516096295, 29517419643, and 29518293896: mutable directory metadata caused false identity failures; a POSIX fixture patched a removed helper; then the lock context masked a staging-body OSError as a lock failure | Valid CI failures | Use stable device/inode directory identity with unchanged no-follow entry replay and full file tokens; patch the current write seam; separate lock acquisition/body/release/close exception boundaries so business errors propagate and every cleanup error stays typed/fail-closed; independently re-review and rerun Linux CI |
| PR4 | Copilot reviews 4718286133 and 4718312905 returned only an internal reviewer error on the code-equivalent heads `cf657d3` and `9a366a3`; exact API audit found no inline or top-level code comments | External reviewer service failure; non-code and non-actionable | Re-request once as instructed; after the identical second service error, retain the independent specification/quality reviews and continue only if all actual comments, threads, and required checks are clear |
| PR4 | Python runs 29541646867 and 29544285078 stalled in the official Python route; read-only diagnosis found nested acquisition of the non-reentrant service lock in POSIX CRLF configuration-lineage validation | Valid CI failure | Cancel both stalled runs; pass the already-held authoritative registry snapshot through all five locked review-evidence validation paths; keep exact target/config validation and fail-closed behavior; add bounded POSIX fork regressions for both getters; independently re-review; pass the 1,664-test official local route; restart Linux CI and the full feedback window |
| PR4 | Python run 29545695399 completed instead of hanging, then failed two Linux-only contract assertions: normalized LF incorrectly expected reconfirmation, and action submission surfaced the raw lineage mismatch | Valid CI follow-up findings | Model only uniform LF/CRLF state transitions; verify stale challenge rejection, no challenge for exact canonical LF, and current challenge after uniform CRLF restoration; normalize only `_ReviewEvidenceTargetContextError` to the established current-run-context failure before publishing decisions; independently re-review and rerun the full 1,664-test route |
