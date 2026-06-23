# AI Improvement Final Managed PR Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `managed-pr-development` for implementation. Every PR must start from latest `main`, use a worker agent, receive separate spec-compliance and code-quality reviews, run focused validation, wait for GitHub/Copilot feedback, merge only after valid feedback is resolved, and delete merged local/remote branches after merge.

**Goal:** turn AI from a passive reviewer into a controlled improvement loop that can recover missing balls, reduce false detections, stabilize follow-cam output, and improve highlight boundaries while proving whether each approved candidate is actually better.

**Architecture:** deterministic artifacts remain authoritative. AI can propose evidence-backed actions, but only explicit approvals create bounded candidate outputs. Candidate outputs are re-audited, compared with baseline by domain-specific comparison reports, and promoted only through one final artifact manifest.

**Tech Stack:** Python backend, pytest, FastAPI service layer, React operator UI, tracking artifacts, review packets, visual review, AI improvement reports, approval artifacts, high-recall recovery, camera motion audit, highlight reports, candidate comparison reports, final artifact manifest, and managed PR delivery.

---

## Canonical Plan Status

This document is the canonical development plan for the AI improvement program.

It consolidates and supersedes the older planning documents:

- `docs/superpowers/plans/2026-06-23-ai-actionable-improvement-reviewed-development-plan.md`
- `docs/superpowers/plans/2026-06-23-ai-improvement-stable-output-plan.md`

Those older documents remain useful background, but workers must execute this plan unless a later reviewed plan explicitly replaces it.

## User Requirement Summary

The user needs **AI improvement**, not only AI audit.

The four user-visible improvement cases are:

1. **Ball missing:** when the tracker loses the match ball, AI should inspect review evidence and propose a bounded `localize_ball_roi` or `targeted_rerun`, or explicitly say `not_visible` with packet/visual evidence.
2. **Too many noise detections:** when detections are noisy, AI should classify the false-positive source and create a bounded cleanup/rerun/config candidate, not broadly increase recall across the whole video.
3. **Follow-cam too shaky:** AI should use final `camera_path.csv` and `camera_motion_audit.json` to decide whether the problem is bad tracking or follow-cam tuning, then create a rerun/rerender candidate and compare motion quality.
4. **Highlights:** default clips keep pre/post buffers. AI may adjust boundaries, but cannot cut the core shot/action or the required result tail unless source-video end forces the clamp.

System-level requirements:

- Full-video speed should come from temporal chunk parallelism.
- Broad spatial split/SAHI over the whole video is not the default because it increases false positives.
- SAHI/ROI is reserved for explicit, bounded, approved recovery windows.
- Hard cases should use a stronger model than low-risk dry-run/tagging tasks.
- No AI report, approval file, or candidate artifact can promote itself by file presence.

## Current Demand Decomposition

The latest product demand is to move from "AI tells us something may be wrong" to "AI helps produce a better candidate and the system proves whether that candidate is better."

There are four concrete improvement loops:

1. **Find the ball when tracking is lost.**
   - Input signals: long `Lost` gaps from `ball_audit.json`, high-recall rejection windows, `review_packets.json`, optional visual review crops, and known failure windows such as the right-bottom corner sequence around frame `2079`.
   - AI role: decide whether the ball is visible, propose a bounded `localize_ball_roi` or `targeted_rerun`, or mark the window `not_visible` with evidence.
   - Program role: execute only explicitly approved bounded recovery, re-audit the candidate track, compare it with baseline, and reject stable wrong targets.
   - User-facing result: a candidate tracking/follow-cam output that either improves the lost segment or clearly explains why no safe recovery was promoted.

2. **Reduce noise when detections are too noisy.**
   - Input signals: dense false-positive islands from `ball_audit.json`, dense-noise triggers from `ai_review_triggers.json`, review packets, and false-positive labels.
   - AI role: classify the false-positive source and propose a bounded cleanup, rerun, or config candidate.
   - Program role: prevent broad full-video spatial splitting as a default, split oversized dense-noise ranges into bounded windows, enforce frame budgets, and compare precision/recall impact.
   - User-facing result: noise candidate reports that say whether the candidate removed bad detections without losing the real match ball.

3. **Stabilize shaky follow-cam output.**
   - Input signals: final `camera_path.csv`, `camera_motion_audit.json`, `follow_cam_report.json`, and nearby ball-track status.
   - AI role: decide whether the shake comes from bad ball tracking or from follow-cam tuning, then propose either `tracking_rerun_before_follow_cam` or `adjust_follow_cam`.
   - Program role: render an isolated follow-cam candidate only after explicit approval, compare camera motion metrics, and reject candidates that merely create a video but keep or worsen sudden pan/zoom.
   - User-facing result: a more stable follow-cam candidate or a clear reason that tracking must be fixed before camera tuning.

4. **Improve highlight clips without cutting the action.**
   - Input signals: event candidates, highlight reports, default pre/post buffers, shot/action core windows, result-tail requirements, and source-video bounds.
   - AI role: adjust clip boundaries around a default buffered window.
   - Program role: enforce that AI cannot trim the core action or required result tail unless the source video ends first, then compare per-clip candidates.
   - User-facing result: highlight clips with better start/end timing and explicit pass/warn/fail status per clip.

Cross-cutting behavior:

- AI suggestions are advisory until an explicit approval id is consumed.
- Every approved improvement creates an isolated candidate, not an in-place mutation of baseline artifacts.
- Candidate promotion requires deterministic comparison plus final manifest selection.
- Hard decisions use a stronger model tier when provider access exists; otherwise the system marks the provider-dependent decision as `warn` or `unavailable`, not `pass`.

## Current Landing Status

| Area | Landed | Still required |
| --- | --- | --- |
| Ball missing | Audit/triggers/review packets/visual review exist; PR1 is implementing bounded `localize_ball_roi` and `missing_ball_recovery_comparison.json`. | Finish PR1 reviewer fixes, prove frame-2079/right-bottom coverage, reject stable wrong targets, merge branch. |
| Noise | Dense-noise signals and packet tags exist. | Build executable bounded noise candidate contract, comparison report, and final promotion integration. |
| Follow-cam shake | `camera_motion_audit.json` is generated from final camera path and referenced in `follow_cam_report.json`. | Add approved follow-cam candidate rendering and comparison against baseline camera motion. |
| Highlights | Event candidates and basic highlight render paths exist; core/tail protections are partially tested. | Add independent highlight comparison and per-clip promotion semantics. |
| Final output | Shared comparison/final manifest base exists. | Add candidate registry, workflow hooks, hard-case model policy, final selected artifact manifest, API/UI visibility, real-video proof. |

## Development Delivery Summary

The implementation should proceed in this order:

1. **PR1: Missing-ball recovery execution.**
   - Complete the current branch before starting anything else.
   - Deliver bounded `localize_ball_roi`, candidate re-audit, packet coverage checks, and `missing_ball_recovery_comparison.json`.
   - Prove the frame-2079/right-bottom long-loss case cannot silently pass.

2. **PR1.5: Shared candidate registry.**
   - Deliver `ai_candidate_registry.json` and schema validation before adding more candidate types.
   - This prevents noise, camera, and highlight PRs from inventing incompatible metadata.

3. **PR2a/PR2b: Noise improvement.**
   - First make AI noise suggestions bounded and classifiable.
   - Then add executable noise candidates and `noise_improvement_comparison.json`.

4. **PR3a/PR3b: Follow-cam improvement.**
   - First create explicit approved follow-cam candidate rendering.
   - Then compare baseline and candidate camera motion to decide whether the output is actually more stable.

5. **PR4: Highlight boundary improvement.**
   - Add per-clip comparison and promotion while preserving default buffers, core action, and result tail.

6. **PR5a/PR5b: Workflow, model policy, and final promotion.**
   - Make the stable workflow execute candidate stages only through explicit approvals.
   - Apply stronger-model policy for hard cases.
   - Write `final_ai_improvement_artifact_manifest.json` as the single final selector.

7. **PR6/PR7: API and operator UI.**
   - Expose grouped AI improvement status, approvals, comparison results, and final selection without hand-copying JSON.

8. **PR8: Real-video validation, documentation, and skill capture.**
   - Run the full process on the real video.
   - Confirm final tracking video, follow-cam video, and highlights are produced where expected.
   - Update README/operator docs and then update local skill only after explicit confirmation.

## Requirement-To-PR Matrix

This matrix is the working contract for the next development sequence.

| User need | What AI should do | Current state | Remaining PRs | Main proof artifact |
| --- | --- | --- | --- | --- |
| Ball cannot be found | Inspect packet/video evidence, propose bounded ROI recovery or say not visible | Review packets, visual review, AI improvement report, approved high-recall path, and PR1 in-progress localize execution exist | PR1, PR1.5, PR5a, PR5b, PR6, PR7, PR8 | `missing_ball_recovery_comparison.json` plus final manifest |
| Too many noisy ball detections | Classify false-positive source and propose bounded cleanup/rerun/config candidate | Noise triggers and packet tags exist; executable candidate/comparison path is incomplete | PR1.5, PR2a, PR2b, PR5a, PR5b, PR6, PR7, PR8 | `noise_improvement_plan.json`, `noise_improvement_comparison.json` |
| Follow-cam is too shaky | Decide whether the cause is bad ball tracking or follow-cam tuning, then propose rerun/rerender candidate | `camera_motion_audit.json` and follow-cam AI suggestions exist; candidate execution/comparison is incomplete | PR1.5, PR3a, PR3b, PR5a, PR5b, PR6, PR7, PR8 | `follow_cam_candidate.json`, `follow_cam_comparison.json` |
| Highlights need better boundaries | Preserve default buffers, let AI adjust start/end, never cut core action or required result tail | Event candidates, highlight rendering, and tail checks exist; independent comparison/promotion is incomplete | PR4, PR5a, PR5b, PR6, PR7, PR8 | `highlight_comparison.json` |
| Final output must be stable | Compare candidates and select only verified improvements | Shared comparison helpers and final manifest base exist | PR1.5, PR5a, PR5b, PR8 | `final_ai_improvement_artifact_manifest.json` |
| Operator should not hand-copy JSON | Show evidence, approvals, comparisons, and final choice in app | Backend/API surfaces are partially present; UI is not complete | PR6, PR7 | grouped AI improvement UI/API response |
| Process should be repeatable | Run same real-video workflow and document exact artifacts | Stable workflow script and docs exist, but need full candidate loop validation | PR8 | real-video validation record and docs |

## AI Improvement Control Loop

The planned loop is deliberately conservative:

1. Deterministic artifacts identify suspicious windows: `ball_audit.json`, `ai_review_triggers.json`, `camera_motion_audit.json`, `event_candidates.json`, and `review_packets.json`.
2. AI reads only bounded evidence packets or summarized run evidence and writes advisory suggestions to `ai_improvement_report.json`.
3. Operator approval creates explicit approved actions with stable ids. File presence alone is never approval.
4. Approved actions produce isolated candidate outputs, not mutations to the baseline run.
5. Candidate outputs are re-audited and compared with domain-specific reports.
6. A final manifest selects only passing candidates, or warning candidates with explicit human confirmation.
7. README, operation docs, and the UI must describe the same flow with the same artifact names.

## Model Routing Policy

Use the model tier according to consequence, not convenience:

- Low-risk dry-run, schema smoke, or simple packet tagging can use a small/cheap model.
- Missing-ball recovery over long gaps, key-event windows, camera `fail` events, follow-cam regressions, and highlight tail decisions should use a stronger model.
- A hard-case candidate reviewed only by a small/unknown model cannot become a clean `pass`; it must be `warn`, `unavailable`, or require human confirmation unless deterministic comparison already proves the result.
- The selected model, provider status, and dry-run/real mode must be recorded in the report that produced the suggestion.

## Already Landed Or In Progress

| Area | State |
| --- | --- |
| Candidate comparison contract | Shared comparison helpers and final manifest base exist. |
| AI improvement report | Run-level suggestions, model metadata, approval contracts, and prompt validation exist. |
| Approval safety | Approval files are inert unless explicit ids are consumed. |
| Missing-ball PR1 | Current branch `feat/localize-ball-roi-recovery` already has uncommitted work for executable `localize_ball_roi`, long-gap packet coverage, and `missing_ball_recovery_comparison.py`. Finish this branch first. |
| Camera audit | `camera_motion_audit.json` is generated from `camera_path.csv` and referenced from `follow_cam_report.json`. |
| Highlight protection | Approved highlight render paths already protect core/tail in API tests, but still need independent comparison and promotion semantics. |
| Workflow/docs | Stable workflow docs exist, but need to be aligned with the final candidate-comparison loop. |

## Non-Negotiable Safety Rules

- `ai_improvement_report.json` is advisory.
- `ai_improvement_approved_actions.json` is not execution authorization by file presence.
- Executable behavior must consume explicit approval ids or explicit API/CLI arguments.
- Review and improvement-only stages must not mutate `ball_track.csv` or `ball_track.cleaned.csv`.
- Every candidate needs `candidate_id`, `problem_type`, source evidence, and comparison status.
- Candidate comparison statuses are `pass`, `warn`, `fail`, and `unavailable`.
- Status precedence is `fail > unavailable > warn > pass`.
- A `warn` candidate can be promoted only with explicit human confirmation.
- A missing or invalid comparison cannot silently pass.
- Full-video spatial/SAHI candidates fail unless explicitly tied to bounded approved windows.

## Independent Plan Review Resolutions

An independent review agent checked this plan against current code and the latest user requirements. The review found no Critical issues, but requested these revisions before execution:

- Move candidate registry/schema validation earlier, before domain-specific candidate PRs.
- Strengthen missing-ball comparison so a stable wrong target cannot pass as recovery.
- Make approved follow-cam candidate rendering an explicit deliverable, not only a rerender plan.
- Add frame-budget and cluster-splitting rules for dense-noise suggestions.
- Add UI coverage for `localize_ball_roi`.
- Normalize `player_foot` as an alias of `foot_confusion`.

This document incorporates those changes in PR1, PR1.5, PR2a/PR2b, PR3a/PR3b, and PR7.

A second independent review of the current demand found no Critical issues and requested additional acceptance tightening:

- Add the missing execution chain for tracking-caused camera shake: a passed tracking recovery candidate must be usable as the input track for a follow-cam candidate render and comparison.
- Because PR1 is candidate-producing before PR1.5 exists, PR1 must write registry-compatible metadata or PR1.5 must backfill missing-ball candidates into the registry.
- Noise reduction cannot pass by increasing false-positive islands; that tradeoff belongs to missing-ball recovery and must require warning or human confirmation.
- Final promotion must reject any candidate, even with a passing comparison report, if it lacks consumed approval linkage.
- A noise cleanup plan alone is not evidence of improvement; a real candidate track/rerun/cleaned artifact plus candidate audit is required for a clean pass.
- Highlight tail protection must define the post-result tail source and test that frames after the shot/result are not clipped.

This document incorporates those review changes in PR1, PR1.5, PR2a/PR2b, PR3b/PR5a, PR4, PR5b, PR8, and the end-to-end acceptance criteria.

## Shared Candidate Artifact Schema

Add and validate this schema before expanding domain candidate execution, API, or UI surfaces, so each improvement path writes the same candidate shape.

Every candidate-producing PR must write or expose:

```json
{
  "candidate_id": "candidate_missing_ball_001",
  "approval_id": "approval_001",
  "problem_type": "missing_ball",
  "baseline_dir": "path-or-run-id",
  "candidate_dir": "path-or-run-id",
  "candidate_artifacts": [],
  "comparison_report": "missing_ball_recovery_comparison.json",
  "comparison_status": "pass",
  "promotion_status": "not_promoted",
  "consumed_approval_ids": ["approval_001"],
  "warnings": []
}
```

Valid `problem_type` values:

- `missing_ball`
- `noise`
- `follow_cam`
- `highlight`

PR1.5 creates the lightweight registry and schema validator that domain PRs must call. PR2b, PR3a/PR3b, and PR4 must not invent private candidate metadata shapes.

PR1 is the one exception because it is already in progress before PR1.5. PR1 must still write registry-compatible candidate metadata fields in its comparison/report payloads. PR1.5 then backfills or adapts those missing-ball records into `ai_candidate_registry.json` without changing PR1's recovery behavior.

## PR0: Shared Candidate Comparison And Final Manifest

**Status:** Done. Keep as the base contract.

**Delivered:**

- `python_backend/football_tracking/ai_candidate_comparison.py`
- `python_backend/football_tracking/final_artifact_manifest.py`
- Quality-gate loading of candidate comparisons.
- Tests for status derivation, invalid comparison reports, path safety, and final manifest semantics.

**No new work:** do not rebuild PR0 unless a regression is proven.

## PR1: Executable Missing-Ball ROI Recovery

**Current state:** in progress on `feat/localize-ball-roi-recovery`. Finish and merge this branch before starting another feature PR.

**Primary files:**

- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Create/finish: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Create/finish: `python_backend/tests/fixtures/right_bottom_gap_2049_2544.json`
- Modify/add tests listed below.

**Build:**

- Make approved `localize_ball_roi` executable as bounded high-recall recovery windows.
- Require frame bounds, `local_search_roi`, approval id, candidate id, and packet/visual provenance.
- Reject missing ROI, missing frame bounds, unknown provenance, malformed frames, frame-budget overflow, and full-video localize attempts.
- Preserve existing `targeted_rerun` behavior.
- Route approved `localize_ball_roi` through the same ROI clamp/intersection/manual-resolution policy as targeted reruns.
- Improve long lost-gap review packets so start/middle/end/tail are covered or exact uncovered ranges are reported.
- Ensure the 2079/right-bottom failure class cannot pass silently.
- Write `missing_ball_recovery_comparison.json` comparing baseline and candidate tracks.
- Include registry-compatible candidate fields in the missing-ball comparison or child-run metadata:
  - `candidate_id`
  - `approval_id`
  - `problem_type: "missing_ball"`
  - baseline/candidate artifact references
  - `comparison_report`
  - `comparison_status`
  - `consumed_approval_ids`
- Count only sustained `Detected` recovery as recovery. `Predicted` alone does not count.
- Fail candidates that replace a long lost gap with short false-positive islands.
- Re-run deterministic ball audit on candidate tracks before comparison when candidate output exists.
- Fail or warn candidates with large jumps, implausible ROI movement, missing candidate audit, or packet/crop evidence that does not overlap the claimed recovery window.
- Add a regression case for a sustained but wrong target: a long stable false detection must not pass merely because it is continuous and `Detected`.

**Test:**

- `python_backend/tests/test_high_recall_windows.py`
- `python_backend/tests/test_review_packets.py`
- `python_backend/tests/test_high_recall_reconcile.py`
- `python_backend/tests/test_missing_ball_recovery_comparison.py`
- `python_backend/tests/test_ai_candidate_comparison.py`
- `python_backend/tests/test_chunk_runner.py`
- `python_backend/tests/test_config_and_provider.py`
- `python_backend/tests/test_api_service.py`
- `python_backend/tests/test_ai_improvement.py`
- `python_backend/tests/test_ai_improvement_quality_gate.py`

Required cases:

- Approved bounded `localize_ball_roi` creates one executable ROI recovery window.
- Adjacent localize windows that merge into full-video scope are rejected.
- ROI frame must be inside the approved frame window.
- ROI clamp stays inside source dimensions.
- Parent run track hashes are unchanged.
- API child run consumes only selected approval ids.
- API rejects full-video localize before creating an output directory.
- Candidate passes on sustained detected recovery.
- Candidate fails on short noisy islands, prediction-only recovery, missing frames, or missing approval linkage.
- Candidate fails or warns on large-jump recovery, implausible ROI movement, missing candidate re-audit, or sustained wrong-target evidence.
- Missing-ball comparison exposes registry-compatible fields so PR1.5 can ingest the candidate without guessing.
- Long gap `2049-2544` around frame `2079` has start/middle/end/tail packet coverage or exact uncovered ranges.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_high_recall_windows.py `
  python_backend/tests/test_review_packets.py `
  python_backend/tests/test_high_recall_reconcile.py `
  python_backend/tests/test_missing_ball_recovery_comparison.py `
  python_backend/tests/test_ai_candidate_comparison.py `
  python_backend/tests/test_chunk_runner.py `
  python_backend/tests/test_config_and_provider.py `
  python_backend/tests/test_api_service.py `
  python_backend/tests/test_ai_improvement.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- Executable bounded `localize_ball_roi` recovery.
- `missing_ball_recovery_comparison.json`.
- Registry-compatible missing-ball candidate metadata.
- Regression coverage for frame-2079/right-bottom long-loss behavior.
- PR merged and branch deleted through managed PR flow.

## PR1.5: Candidate Registry And Schema Guard

**Purpose:** make candidate metadata stable before noise, follow-cam, highlight, API, and UI work consume it.

**Primary files:**

- Create: `python_backend/football_tracking/ai_candidate_registry.py`
- Add: `python_backend/tests/test_ai_candidate_registry.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Implement a minimal candidate record validator using the shared candidate schema.
- Require:
  - `candidate_id`
  - `approval_id` when a candidate came from an approved action
  - `problem_type`
  - `baseline_dir` or baseline artifact reference
  - `candidate_dir` or candidate artifact reference
  - `candidate_artifacts`
  - `comparison_report` when comparison is expected
  - `comparison_status`
  - `consumed_approval_ids`
- Accept only canonical problem types:
  - `missing_ball`
  - `noise`
  - `follow_cam`
  - `highlight`
- Reject or mark `unavailable` for unsafe paths outside the output root.
- Provide a helper that domain PRs can call to append candidate records to `ai_candidate_registry.json`.
- Backfill/adapt PR1 missing-ball candidate metadata into `ai_candidate_registry.json` so early missing-ball candidates participate in the same final manifest flow as later noise, follow-cam, and highlight candidates.
- Keep the helper pure JSON and path validation logic; do not run tracking, rendering, or provider calls.
- Update the quality gate to read `ai_candidate_registry.json` in addition to direct `*_comparison.json` files and final manifest references.

**Test:**

- Valid candidate record is accepted and written.
- Missing `candidate_id`, `problem_type`, or `comparison_status` fails validation.
- Unknown `problem_type` fails validation.
- Unsafe absolute/outside paths are rejected or recorded as `unavailable`.
- Duplicate `candidate_id` fails unless explicitly replacing the same candidate record.
- Quality gate treats registry candidates without comparison reports as `unavailable`, not pass.
- Missing-ball candidate metadata produced by PR1 can be registered and later referenced by the final manifest.
- Existing direct comparison report discovery still works.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_ai_candidate_registry.py `
  python_backend/tests/test_ai_candidate_comparison.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `ai_candidate_registry.json` contract.
- Shared schema guard for all candidate-producing PRs.
- Quality-gate visibility for registered candidates.

## PR2a: Dense-Noise Suggestion Contract

**Purpose:** make AI noise suggestions concrete and safe before any executable cleanup path is exposed.

**Primary files:**

- Create: `python_backend/football_tracking/noise_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Add: `python_backend/tests/test_noise_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Normalize accepted false-positive classes:
  - `extra_ball`
  - `shoe_confusion`
  - `foot_confusion`
  - `player_head`
  - `sideline_confusion`
  - `field_marking`
  - `line_marking`
  - `advertising_board`
  - `wall_background_drift`
  - `unknown_false_positive`
  - `unknown`
- Treat `player_foot` as an input alias for canonical `foot_confusion`, not as a separate stored class.
- Treat `pitch_line`, `field_line`, and generic line-marking labels as aliases for canonical `line_marking`.
- Require bounded frame windows for noise suggestions.
- Enforce a maximum per-window frame length and maximum total frame budget for dense-noise actions.
- Require large dense-noise clusters to be split into smaller subwindows or marked `needs_human_review`.
- Require accepted false-positive tag and evidence overlap.
- Require packet/crop evidence overlap for the proposed noise window.
- Treat config patches as advisory until explicitly applied.
- Reject full-video spatial split/SAHI as a default noise solution.
- Write a deterministic `noise_improvement_plan.json` for suggested actions.

**Test:**

- Missing false-positive tag rejects or downgrades.
- Missing bounded window rejects.
- Unknown class normalizes to `unknown` with warning.
- Full-video SAHI/spatial split fails unless tied to bounded approval.
- Oversized bounded windows fail or are downgraded to `needs_human_review`.
- Dense cluster splitting creates specific subwindows and preserves evidence ids.
- Noise suggestions without packet/crop overlap fail.
- Config patch suggestion does not mutate runtime config.
- Dense-noise trigger overlap is required.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_noise_improvement.py `
  python_backend/tests/test_ai_improvement.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `noise_improvement_plan.json`.
- Validated AI noise vocabulary and bounded-action contract.
- Quality-gate checks for noise tag/window coverage.

## PR2b: Dense-Noise Candidate Execution And Comparison

**Purpose:** turn approved bounded noise suggestions into measurable candidate outputs.

**Primary files:**

- Modify: `python_backend/football_tracking/noise_improvement.py`
- Create: `python_backend/football_tracking/noise_improvement_comparison.py`
- Modify: `python_backend/football_tracking/chunk_runner.py` only if bounded rerun execution needs plumbing.
- Modify: `python_backend/football_tracking/api/service.py` only if approval execution needs service support.
- Add: `python_backend/tests/test_noise_improvement_comparison.py`
- Modify: `python_backend/tests/test_noise_improvement.py`
- Modify: `python_backend/tests/test_api_service.py` if API changes.

**Build:**

- Approved bounded noise actions can produce one of:
  - `noise_cleanup_plan.json` plus a concrete candidate track, rerun output, or cleaned-track artifact
  - bounded rerun candidate metadata
  - config-patch candidate metadata
- Candidate output must live in candidate/child output, not mutate baseline.
- Re-run deterministic audit on candidate outputs.
- Write `noise_improvement_comparison.json`.
- Register each noise candidate through `ai_candidate_registry.py`.
- Compare false-positive island count, sustained lost-gap coverage, recall regression, and approval linkage.
- A plan-only artifact is advisory and must produce comparison status `unavailable`; it cannot pass without a concrete candidate artifact and candidate audit.
- Fail noise-reduction candidates when false-positive islands or short noisy islands increase.
- If a candidate intentionally accepts more noise to recover a missing ball, classify it as a missing-ball tradeoff, not a clean noise-reduction pass; it must be `warn` with explicit human confirmation or `fail`.

**Test:**

- Approved bounded noise action creates the expected candidate artifact.
- Candidate metadata validates through the shared registry.
- Candidate is re-audited before comparison.
- Candidate with fewer false-positive islands and stable recall passes.
- Candidate with more short noisy islands fails for noise-reduction mode.
- Candidate with more short noisy islands and better lost-gap recovery is downgraded to missing-ball tradeoff `warn`, not clean noise pass.
- Plan-only `noise_cleanup_plan.json` with no concrete candidate artifact is `unavailable`.
- Missing candidate audit is `unavailable`, not pass.
- Parent track hashes remain unchanged.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_noise_improvement.py `
  python_backend/tests/test_noise_improvement_comparison.py `
  python_backend/tests/test_ai_candidate_comparison.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `noise_cleanup_plan.json` plus concrete bounded candidate metadata when execution is approved.
- Candidate `ball_audit.json` or equivalent re-audit summary.
- `noise_improvement_comparison.json`.

## PR3a: Approved Follow-Cam Candidate Render Execution

**Purpose:** turn approved follow-cam tuning advice into an isolated candidate render with provenance, instead of stopping at `follow_cam_rerender_plan.json`.

**Primary files:**

- Create: `python_backend/football_tracking/follow_cam_candidate.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Add: `python_backend/tests/test_follow_cam_candidate.py`
- Modify: `python_backend/tests/test_follow_cam.py`
- Modify: `python_backend/tests/test_camera_motion_audit.py`
- Modify: `python_backend/tests/test_api_service.py`

**Build:**

- Keep `follow_cam_rerender_plan.json` inert until an explicit render/apply action.
- Add an approved candidate render path for `adjust_follow_cam`.
- Require `approval_id`, `candidate_id`, camera-motion evidence id, and consumed approval ids.
- Apply only allowlisted `follow_cam` config patch keys to the candidate render.
- Reject unrecognized follow-cam patch keys, unsafe values, and path-like values.
- Reject follow-cam-only render when the approved action is `tracking_rerun_before_follow_cam`; tracking recovery must happen first.
- Create a child/candidate output that contains:
  - `follow_cam.mp4` or the configured follow-cam video name
  - `camera_path.csv`
  - `camera_motion_audit.json`
  - `follow_cam_report.json`
  - `follow_cam_candidate.json`
- Register the candidate through `ai_candidate_registry.py`.
- Do not mutate baseline track files or baseline follow-cam artifacts.

**Test:**

- Plan file presence alone does not render video.
- Approved `adjust_follow_cam` with explicit id creates a candidate child output.
- Candidate metadata includes `candidate_id`, `approval_id`, consumed approval ids, config patch, and camera evidence id.
- Unapproved or unknown approval ids do not render.
- `tracking_rerun_before_follow_cam` is rejected for follow-cam-only rendering.
- Unsafe config patch keys/values are rejected.
- Candidate render writes `camera_path.csv`, `camera_motion_audit.json`, and `follow_cam_candidate.json`.
- Baseline artifacts remain unchanged.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_follow_cam_candidate.py `
  python_backend/tests/test_follow_cam.py `
  python_backend/tests/test_camera_motion_audit.py `
  python_backend/tests/test_api_service.py `
  python_backend/tests/test_ai_candidate_registry.py -q
git diff --check
```

**Deliver:**

- Explicit approved follow-cam candidate render path.
- `follow_cam_candidate.json`.
- Registered follow-cam candidate output with provenance.

## PR3b: Follow-Cam Candidate Comparison

**Purpose:** make camera-motion AI useful by proving whether a candidate rerender or tracking recovery improves final video stability.

**Primary files:**

- Create: `python_backend/football_tracking/follow_cam_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam_candidate.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Add: `python_backend/tests/test_follow_cam_comparison.py`
- Modify: `python_backend/tests/test_follow_cam_candidate.py`
- Modify: `python_backend/tests/test_camera_motion_audit.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Join camera motion events with nearby ball-track status.
- Route Lost/Predicted context to `tracking_rerun_before_follow_cam`.
- Allow `adjust_follow_cam` only when the ball track around the camera event is stable enough.
- After candidate rerender, require candidate `camera_path.csv`, `camera_motion_audit.json`, `follow_cam_candidate.json`, and registry record.
- For `tracking_rerun_before_follow_cam`, consume a passed tracking recovery candidate as the input track, render follow-cam from that candidate track, write candidate camera artifacts, and compare the resulting camera motion against baseline.
- Do not let a passed missing-ball recovery stop at track comparison when the user-visible problem is camera shake; the follow-cam candidate must be rendered and audited before camera stability is considered improved.
- Write `follow_cam_comparison.json`.
- Compare review-event count, max pan step, p95 pan step, acceleration spikes, zoom jumps, candidate video presence, and approval linkage.
- Fail if candidate video exists but motion metrics regress beyond threshold.
- Mark comparison `unavailable` if candidate render metadata or candidate camera audit is missing.

**Test:**

- Lost/Predicted camera spike recommends tracking recovery first.
- Stable Detected camera spike can recommend follow-cam tuning.
- Candidate camera audit improvement passes.
- Candidate camera audit regression fails.
- Passed tracking recovery candidate can be used to render a follow-cam candidate and compare camera motion.
- Tracking recovery without a rendered/audited follow-cam candidate is `unavailable` for the camera-stability problem.
- Missing candidate render metadata is `unavailable`, not pass.
- Warn candidate requires explicit human confirmation before final promotion.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_follow_cam_comparison.py `
  python_backend/tests/test_follow_cam_candidate.py `
  python_backend/tests/test_camera_motion_audit.py `
  python_backend/tests/test_ai_improvement.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `follow_cam_comparison.json`.
- Follow-cam comparison integrated with registered candidate metadata.
- Clear distinction between tracking failure and camera tuning failure.

## PR4: Highlight Boundary Comparison And Per-Clip Promotion

**Purpose:** let AI improve highlight windows without cutting off the action or result tail.

**Primary files:**

- Create: `python_backend/football_tracking/highlight_comparison.py`
- Modify: `python_backend/football_tracking/highlights.py` only if render metadata is missing.
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Add: `python_backend/tests/test_highlight_comparison.py`
- Modify: `python_backend/tests/test_highlights.py`
- Modify: `python_backend/tests/test_accepted_highlights.py`
- Modify: `python_backend/tests/test_api_service.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Add candidate highlight metadata:
  - `candidate_id`
  - `approval_id`
  - `requested_window`
  - `rendered_window`
  - `core_window`
  - `required_tail_frames`
  - `source_end_clamped`
  - `tail_status`
- Preserve default pre/post buffer behavior.
- Derive `core_window` from the event candidate's action/contact/result frames when present; otherwise use the deterministic event window.
- Derive required tail from the result/shot frame plus the configured minimum post-result frames. If the result frame is unknown, use the core window end plus the default post buffer.
- AI may adjust start/end only within core/tail rules.
- Write `highlight_comparison.json`.
- Register each highlight candidate through `ai_candidate_registry.py`.
- Support multiple independent clip candidates and per-clip promotion.
- Final manifest can select only passing clips or explicitly confirmed warning clips.

**Test:**

- Default buffer is applied.
- AI cannot trim core action.
- AI cannot trim required shot/result tail.
- AI cannot trim the configured post-result frames after a shot, goal, save, or clear outcome.
- Source-end clamp is accepted only when source end prevents full tail.
- Fixture covers a suggested highlight that looks visually good but cuts the last post-result frames; it must fail.
- Missing `core_window` or tail metadata is `unavailable` or `fail`.
- Multiple clips have independent comparison statuses.
- Final manifest promotes only selected clips.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_highlight_comparison.py `
  python_backend/tests/test_highlights.py `
  python_backend/tests/test_accepted_highlights.py `
  python_backend/tests/test_api_service.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `highlight_comparison.json`.
- Tail-safe `highlight_report.json`.
- Per-clip final manifest selection.

## PR5a: Candidate-Aware Workflow Execution Hooks

**Purpose:** make the stable workflow execute real candidate stages instead of only recording planned status.

**Primary files:**

- Modify: `python_backend/football_tracking/ai_candidate_registry.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/tests/test_ai_candidate_registry.py`
- Modify: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Modify: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- Use the PR1.5 registry helper to collect all candidate records.
- Collect candidate artifacts from missing-ball, noise, follow-cam, and highlight paths.
- When a camera issue was routed to `tracking_rerun_before_follow_cam`, collect the passed tracking candidate, render follow-cam from that candidate track, and register the resulting follow-cam candidate artifacts.
- Run candidate stages only when explicit approvals or candidate artifacts exist.
- Keep approval selection explicit:
  - approval source path
  - requested ids
  - consumed ids
  - skipped ids
  - unknown ids
- Write `stable_ai_improvement_workflow_report.json` with executed candidate stages.
- Do not promote candidates in this PR.

**Test:**

- Workflow dry-run records stages without provider calls.
- Explicit approval ids are required for execution.
- Unknown approval ids fail in real mode and warn in dry-run mode.
- Workflow report includes registry records with `candidate_id`, `approval_id`, `problem_type`, `comparison_report`, and `consumed_approval_ids`.
- Workflow records the tracking-candidate-to-follow-cam render chain when camera shake is caused by Lost/Predicted tracking.
- Missing candidate artifacts become `unavailable`.
- Baseline track hashes stay unchanged during review/improvement-only stages.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_ai_candidate_registry.py `
  python_backend/tests/test_stable_ai_improvement_workflow.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- `ai_candidate_registry.json` or embedded registry section in workflow report.
- Candidate-aware `stable_ai_improvement_workflow_report.json`.
- Explicit approval consumption record.

## PR5b: Post-Candidate AI Compare, Hard-Case Model Policy, And Final Promotion

**Purpose:** close the loop: compare candidates, route hard cases to a stronger model, and write the final manifest.

**Primary files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Add/modify tests:
  - `python_backend/tests/test_stable_ai_improvement_workflow.py`
  - `python_backend/tests/test_final_artifact_manifest.py`
  - `python_backend/tests/test_ai_improvement_quality_gate.py`
  - `python_backend/tests/test_ai_improvement.py`

**Build:**

- Collect all domain comparison reports:
  - `missing_ball_recovery_comparison.json`
  - `noise_improvement_comparison.json`
  - `follow_cam_comparison.json`
  - `highlight_comparison.json`
- Feed baseline/candidate summaries into AI compare mode where provider is available.
- Let AI confirm, reject, or request human review, but never auto-promote by itself.
- Define hard cases:
  - missing-ball gap length >= 120 frames or key-event overlap
  - camera audit `fail`
  - follow-cam comparison regression
  - highlight tail validation
  - any `warn` promotion candidate
- Real hard cases using `mini` or unknown model tier cannot produce clean `pass`.
- Write `final_ai_improvement_artifact_manifest.json` as the only final output selector.

**Test:**

- Missing comparison report blocks pass once a candidate exists.
- Comparison `fail` prevents promotion.
- Comparison `pass` without a consumed approval id cannot be promoted.
- Candidate artifact existence without explicit consumed approval linkage cannot enter final selected artifacts.
- AI reject prevents promotion.
- `warn` candidate requires consumed human confirmation.
- Hard case with mini/unknown model does not produce clean pass.
- Final manifest records selected, rejected, and unavailable candidates.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_stable_ai_improvement_workflow.py `
  python_backend/tests/test_final_artifact_manifest.py `
  python_backend/tests/test_ai_improvement_quality_gate.py `
  python_backend/tests/test_ai_improvement.py -q
git diff --check
```

**Deliver:**

- Post-candidate AI compare mode.
- Hard-case model policy.
- `final_ai_improvement_artifact_manifest.json` with approval-linked selected artifacts only.

## PR6: Backend API Artifact Contract

**Purpose:** expose candidate status and final selection to the app without raw JSON editing.

**Primary files:**

- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify routes under `python_backend/football_tracking/api/routes/` only if needed.
- Modify: `python_backend/tests/test_api_service.py`
- Modify: `python_backend/tests/test_export_openapi.py` if schema changes.
- Regenerate API clients only if OpenAPI changes.

**Build:**

- Extend existing backend surfaces instead of creating a parallel API.
- List workflow report, quality gate, domain comparison reports, final manifest, camera audit, rerender plan, and highlight reports.
- Group AI improvement items by:
  - missing ball
  - noise
  - camera motion
  - highlights
- Return frame windows, evidence ids, confidence, false-positive class, recommended action, approval status, consumed approvals, comparison status, and promotion status.
- Add explicit approve/reject helpers if current service methods are not enough.
- Missing artifacts return `unavailable` summaries rather than crashing.

**Test:**

- Artifact list includes workflow, quality gate, comparisons, final manifest, camera, recovery, and highlight artifacts.
- API groups items by problem type.
- API rejects implicit approval-file execution.
- Unknown approval ids return clear validation errors.
- Existing approved child rerun, approved highlight render, and follow-cam rerender tests still pass.
- Missing artifacts produce readable unavailable states.

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_api_service.py `
  python_backend/tests/test_export_openapi.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check
git diff --check
```

**Deliver:**

- Backend artifact contract for candidate status and final output status.
- API visibility for approval consumption and comparison results.

## PR7: Operator UI For Explicit AI Improvement

**Purpose:** give the operator a clear screen to approve AI improvements and see whether candidates actually improved output.

**Primary files:**

- Modify files under `artifacts/web/src/`.
- Modify API client/types only if PR6 changed generated clients.

**Build:**

- Group improvement items by missing ball, noise, camera motion, and highlights.
- Show evidence ids, frame windows, ROI/not-visible state, false-positive class, confidence, recommended action, model tier, approval status, comparison status, and promotion status.
- Provide explicit approve/reject controls.
- Submit exact action ids to the backend.
- Provide an explicit `localize_ball_roi` execution path in the UI, not only `targeted_rerun`.
- Show candidate artifacts and final manifest selection.
- Show `pass`, `warn`, `fail`, and `unavailable` states plainly.
- UI copy must not imply AI reports or approval files auto-apply.

**Test:**

- Grouped items render from mock API data.
- Approve/reject submits explicit action ids only.
- `localize_ball_roi` approval queues/executes the correct backend action and preserves ROI/provenance ids.
- Missing artifacts render without crashing.
- Warning candidates show human confirmation requirement.
- No auto-apply path exists in UI behavior.

**Validate:**

```powershell
pnpm --filter @workspace/web run typecheck
pnpm --filter @workspace/web run build
pnpm run typecheck:libs
git diff --check
```

**Deliver:**

- Operator-facing AI improvement workflow.
- Explicit controls for all four problem categories.
- Final artifact and quality status visibility.

## PR8: Real-Video Validation, Docs, And Skill Capture

**Purpose:** prove the full loop on the real video and capture the operating process.

**Primary files:**

- Modify: `README.md`
- Modify: `python_backend/README.md` if needed.
- Modify/Create: `docs/operations/ai-improvement-workflow.md`
- Modify this plan with final PR references if useful.
- Do not modify local skill files inside this PR.

**Build:**

- Run the full workflow against the current real-video output directory selected for validation and record that run id/output dir in the validation report; do not hard-code old output directories as acceptance evidence.
- Use stronger model routing for hard recovery, camera, and highlight decisions when an API key is available.
- If provider is unavailable, use artifact-only mode and mark provider-dependent checks `warn` or `unavailable`, not `pass`.
- Verify full-video speed path uses temporal chunk parallelism.
- Verify broad spatial split/SAHI is not the default path.
- Verify targeted SAHI/ROI only appears for explicit bounded approved recovery windows.
- Update operator docs:
  - AI audit versus AI improvement
  - missing-ball recovery flow
  - dense-noise flow
  - camera-motion flow
  - highlight boundary flow
  - model routing
  - approval/apply semantics
  - candidate comparison and final manifest
  - real-video commands and expected artifacts

**Real-video visual checklist:**

- Inspect frame `2079` and the right-bottom corner lost-ball sequence.
- Inspect any recovered missing-ball candidate against review packet/crop evidence to ensure it is the match ball, not a stable wrong target.
- Inspect at least one dense-noise window.
- Inspect at least one camera spike or follow-cam motion event.
- Inspect at least one highlight tail.
- Confirm review media and candidate clips are non-empty and decodable.
- Confirm final tracking and follow-cam videos exist when expected.

**Test:**

- Focused unit/integration tests from PR1-PR7.
- Real workflow produces:
  - `stable_ai_improvement_workflow_report.json`
  - `ai_improvement_quality_gate.json`
  - domain comparison reports for generated candidates
  - `ai_candidate_registry.json`
  - `final_ai_improvement_artifact_manifest.json`
  - final tracking output
  - final follow-cam output
  - highlight clips when candidates exist

**Validate:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir <current-validation-output-dir> `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --parallel-mode temporal `
  --mode real
.\.venv\Scripts\python.exe -m pytest `
  python_backend/tests/test_stable_ai_improvement_workflow.py `
  python_backend/tests/test_final_artifact_manifest.py `
  python_backend/tests/test_ai_improvement_quality_gate.py -q
git diff --check
```

**Deliver:**

- Real-video validation record.
- Final selected tracking/follow-cam/highlight artifacts.
- Updated README and operator docs.
- Managed PR final report with PR URLs, branches, checks, review comments handled, merge status, branch cleanup, artifacts, and residual risks.

## Post-PR Local Skill Update

**Purpose:** update local operating knowledge only after repository workflow is merged and verified.

**Out of GitHub PR scope:**

- `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`

**Do only after explicit user confirmation:**

- Copy the verified workflow sequence from docs into the local skill.
- Include temporal-first speed strategy.
- Include targeted SAHI/ROI-only recovery guidance.
- Include quality-gate and real-video checklist.
- Include stronger-model guidance for hard cases.

## Managed PR Execution Checklist

For every PR:

1. Sync latest `origin/main`.
2. Confirm `git status --short --branch` and identify unrelated local changes.
3. Create a fresh branch from clean `main`, except PR1 which must finish the current in-progress branch.
4. Assign a worker agent with precise file ownership and acceptance criteria.
5. Require regression-first tests for behavior changes.
6. Run a spec-compliance reviewer agent.
7. Run a code-quality reviewer agent.
8. Fix all valid Critical and Important findings.
9. Run focused local validation and `git diff --check`.
10. Commit intentionally.
11. Push and open a GitHub PR.
12. Wait 10-15 minutes for CI and Copilot/review comments.
13. Fix confirmed-safe feedback only.
14. Merge only after checks and valid feedback are resolved.
15. Delete merged local and remote branches.
16. Update local `main` before starting the next PR.

## End-To-End Acceptance Criteria

- Missing-ball gaps have packet/visual coverage plus AI improvement coverage, or exact uncovered explanations.
- The 2079/right-bottom failure class cannot pass silently.
- Approved `localize_ball_roi` can run only as bounded candidate recovery.
- Missing-ball candidates are re-audited and cannot pass by following a stable wrong target.
- Dense-noise suggestions have false-positive class, bounded window, and executable candidate/comparison path.
- Dense-noise candidates respect per-window and total-frame budgets.
- Dense-noise candidates cannot pass by increasing short false-positive islands; any recall-for-noise tradeoff is warning/human-confirmation territory.
- Follow-cam candidates are judged by camera-motion metrics, not video existence.
- Approved follow-cam tuning can create a real isolated candidate render with `candidate_id` and `approval_id`.
- Tracking-caused camera shake is fixed only after a passed tracking candidate is rendered through follow-cam and its camera audit improves.
- Highlight candidates preserve core action and required result tail.
- Highlight candidates preserve the configured post-result frames after the shot/action outcome.
- All candidate-producing paths validate through the shared registry.
- Candidate outputs are re-audited and compared before promotion.
- Bad candidates are rejected even if AI originally suggested them.
- Baseline tracks stay stable during review/improvement-only stages.
- Approval artifacts remain inert unless explicit ids are consumed.
- Passing comparisons without consumed approval ids are not promotable.
- Final outputs are described by `final_ai_improvement_artifact_manifest.json`.
