# Production Workflow Release Validation

This checklist is both the test record and the release-evidence template. Automated checks are necessary but do not satisfy the real-media release gate.

## Automated engineering checks

Record the commit and exact result for each check:

| Check | Commit / run | Result | Evidence link or artifact |
| --- | --- | --- | --- |
| Aggregate frontend unit/component coverage (`pnpm --filter @workspace/web run test:web`) | `[required]` | `[required]` | `[required]` |
| Chromium cutover and preserved journeys (`pnpm --filter @workspace/web run test:e2e`) | `[required]` | `[required]` | `[required]` |
| Application and test TypeScript | `[required]` | `[required]` | `[required]` |
| Production build and manifest static-closure check | `[required]` | `[required]` | `[required]` |
| Full repository Python/contract route | `[required]` | `[required]` | `[required]` |

The browser cutover journey must cover the default entry, exactly two primary navigation items, Baseline/Broadcast migration, exact-run History focus, product evidence without list N+1, contextual AI/highlight run preservation, mobile navigation focus (including choosing the already-active Production route), English/Chinese, light/dark rendering, and zero critical/serious axe findings on the primary Production and History shells. It attaches a 1280×720 light/English screenshot and a 390×844 dark/Chinese screenshot to the Playwright result. History coverage also attaches the existing multi-state fixture with active, completed, and failed runs in one image, the rendered empty state, and the existing 1,000-run large-list journey after its explicitly expanded product is verified. These deterministic screenshots document the tested layouts without creating brittle golden-image comparisons; they are engineering diagnostics, not independent visual acceptance of real match output.

## Fail-closed real-media gates

Do not mark a release approved until every field below contains real evidence. Never substitute fixtures, mocked APIs, file-existence checks, or JSON-only assertions.

| Required evidence | Value |
| --- | --- |
| Official full-video command and commit | `[required]` |
| Source video identity, size, decoded resolution, duration, and hash | `[required]` |
| Exact Production workflow ID and trial/full/operation run IDs | `[required]` |
| Confirmed configuration path and SHA-256 | `[required]` |
| Calibration polygon digest and three confirmed source frames | `[required]` |
| Frozen qualified-target audit: frozen model/policy/config/target-population/sampling identities and digests, qualification report, scope, result, auditor, and timestamp | `[required]` |
| Exact-SHA activated review queue: bundle SHA-256, queue SHA-256, activation generation/ID, exact run and candidate-population binding, final disposition, and activation time | `[required]` |
| Append-only configuration-lineage reconfirmation generation: generation ID, independent reviewer, old confirmed canonical SHA-256 `6fd624d76b2688982f3fd18922d6aaf16d519f3da598b653f44d62da9a5fda4c`, observed CRLF raw SHA-256 `c203ae605d8350e0212867287a79d5246c61b603eeda81c6ffbc2bf0bdeb69cc`, and proof neither historical evidence nor either digest was rewritten | `[required]` |
| Trusted independent visual activation audit: auditor identity/role separation, activated queue SHA-256, sampled media/windows, findings, limitations, decision, and timestamp | `[required]` |
| Tracking, recompute, render, quality-report, and product artifact paths/hashes | `[required]` |
| Known-match validation result and limitations | `[required]` |
| Independent reviewer, review time, sampled frames/windows, and decision | `[required]` |
| Final playable media result, duration/audio disposition, and visual limitations | `[required]` |
| Operator release decision and approver | `[required]` |

Current operational inventory: **6,406 candidates and 0 classifications, decisions, prelabels, or confirmed labels**. Consequently there is no frozen qualified target package, exact-SHA activated review queue, trusted independent visual activation audit, or releasable production generation. Release status is **BLOCKED until every required real-media field is completed and independently accepted.** A UI `ready` state proves sealed lineage and artifact verification only; it is not publication approval.

Engineering merge and operational release are separate decisions. Passing the automated engineering checks may permit merging the fail-closed implementation; it does not unblock activation, rendering for publication, or release while the real-evidence rows remain incomplete.

## Rollback record

If rollback is invoked, record timestamp, commit, reason, frontend route/navigation revision, evidence activation stop, affected workflow/run IDs, and confirmation that no backend outputs or consumed evidence generations were deleted or rewritten.
