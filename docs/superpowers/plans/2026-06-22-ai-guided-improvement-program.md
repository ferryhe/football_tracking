# AI Guided Improvement Program Contract-State Plan

> **For managed PR workers:** this document is the authoritative execution plan after the merged AI improvement backend work. Do not re-implement merged backend capabilities unless a regression is proven. Use focused PRs for the remaining operator, documentation, and real-video validation work.

**Goal:** keep deterministic football-tracking artifacts as the source of truth while letting AI provide bounded, reviewable improvement advice for missing-ball recovery, dense noise, camera stability, and highlight boundaries.

**Current branch for this reconciliation:** `docs/ai-improvement-contract-state`.

---

## Current Merged State

The following backend and contract capabilities are already merged and should be treated as existing system behavior:

- **GitHub PR #28:** AI improvement core is merged.
- **GitHub PR #29:** packet and visual tags are merged.
- **GitHub PR #30:** approved recovery actions are merged.
- **GitHub PR #31:** camera motion AI improvement is merged.
- **GitHub PR #32:** highlight boundary improvement is merged.
- **GitHub PR #33:** approval/apply boundary hardening is merged.
- **GitHub PR #34:** provenance and model routing are merged.
- **GitHub PR #35:** approved child rerun execution is merged.
- **GitHub PR #36:** approved ROI policy is merged.

The remaining mainline is not to rebuild those backend contracts. The remaining mainline is:

- Operator AI Improvement UI.
- Real-video quality gate and stable output recipe.
- README and operation docs.
- Final end-to-end validation.

Optional highlight `roll_policy` cleanup requires a separate scoped PR. It must not be folded into PR B, PR C, or PR D unless the user explicitly expands that PR's scope.

## Contract Boundaries

These boundaries are mandatory for every remaining PR:

- `ai_improvement_report.json` is always advisory.
- Only the approve/apply API or explicit CLI parameters may execute suggestions.
- `ai_improvement_approved_actions.json` must not trigger high-recall reruns, child reruns, rerenders, config patches, or highlight renders by file presence alone.
- High-recall child execution from approved actions requires an explicit `approved_actions_path` or an equivalent explicit apply request.
- Review and improvement stages must not modify `ball_track.csv` or `ball_track.cleaned.csv`.
- AI suggestions must preserve provenance: improvement id, frame window, evidence, confidence, source packet or visual review id when available, selected model, and approval source when applied.
- Config suggestions remain derived patches until an operator explicitly applies them through the supported path.
- Follow-cam AI actions may write a `follow_cam_rerender_plan.json` after approval, but they must not silently rerender.

## User Needs Covered By Existing Contracts

### 1. Ball Is Missing

Current contract:

- Packet-level visual review can inspect the relevant packet/window and return a likely ROI or explicit `not_visible`.
- Run-level AI improvement can propose `targeted_rerun` or `localize_ball_roi` with bounded frame scope, evidence, confidence, and provenance.
- Approved high-recall child execution exists and must be invoked explicitly from approved actions.
- PR #36 approved ROI policy governs when ROI evidence can be used.

Remaining work:

- Surface packet evidence, ROI/not-visible state, approval state, and child-run execution status in the operator UI.
- Validate the path on real video: packet review -> improvement report -> approval -> explicit high-recall child run -> comparison.

Acceptance:

- The operator can see whether the AI found a region or concluded the ball is not visible.
- The operator can approve a bounded rerun without hand-copying frame windows.
- The high-recall child run is clearly shown as explicitly approved/executed, not automatic.

### 2. Noise Is Too High

Current contract:

- Dense-noise and high-recall problem areas can be split into micro packets.
- Packet and visual review tags can classify failure modes such as foot, shoe, sideline, or background drift confusion.
- Run-level AI improvement can propose `noise_filter_adjustment`, `reject_noise`, or a safe config patch.
- Approval/apply hardening prevents config mutation unless the operator explicitly applies a derived patch.

Remaining work:

- Show dense-noise/high-recall packet evidence and failure tags in the UI.
- Add real-video before/after comparison for approved config/rerun actions.
- Document how operators decide between reject-noise, targeted rerun, and config patch review.

Acceptance:

- The operator can identify the noise class and affected frame window.
- Approved config/rerun steps produce a comparison that says whether visible quality improved.
- Review/improvement-only phases leave track CSV hashes unchanged.

### 3. Camera Is Too Jumpy

Current contract:

- `camera_motion_audit.json` is joined with track context for run-level AI improvement.
- AI can distinguish tracking-driven camera jumps, follow-cam tuning issues, ambiguous/human-review cases, and acceptable fast play.
- Recommended actions include `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, and `human_review_camera_motion`.
- Approved follow-cam actions write `follow_cam_rerender_plan.json`.
- Tracking-dependent camera actions require rerun before follow-cam rerender.

Remaining work:

- Surface camera-motion evidence, track context, and required next step in the operator UI.
- Add real-video quality gate checks for spike counts and camera regression after approved rerender planning.
- Document that approval writes a plan and does not silently rerender.

Acceptance:

- The operator can tell whether jumpiness came from tracking loss or camera tuning.
- Mixed camera approvals clearly prioritize `tracking_rerun_before_follow_cam` when needed.
- Follow-cam rerender planning is explicit and auditable.

### 4. Highlights Need Better Boundaries

Current contract:

- Event candidates can carry `core_window`, `render_window`, and `buffer_policy`.
- Highlight rendering can use approved `suggested_window` actions with provenance.
- Boundary validation protects the core event window and required post-shot/result tail.

Remaining work:

- Surface current and suggested highlight windows in the UI.
- Add real-video tail-frame validation, especially near the end of source videos.
- Document default buffers, approved suggested-window rendering, and manual override behavior.

Acceptance:

- The operator can compare default and AI-suggested windows before rendering.
- Rendered highlights retain the shot/result tail.
- UI and quality gate cover tail-frame behavior on real video, including near video boundaries.

## Remaining PR Plan

### PR A: Current Docs/Contract/State Reconciliation

**Branch:** `docs/ai-improvement-contract-state`

**Purpose:** make the plan documents reflect the real merged state and set the remaining managed PR path.

**Allowed files for this PR:**

- `docs/superpowers/plans/2026-06-22-ai-guided-improvement-program.md`
- `docs/superpowers/plans/2026-06-22-ai-improvement-loop.md`

**Scope:**

- Replace stale PR sequencing with the current PR #28-#36 merged state.
- Mark the remaining mainline as UI, real-video quality gate, README/operation docs, and final validation.
- Preserve the safety boundaries around advisory reports, explicit approval/apply, explicit `approved_actions_path`, and track CSV immutability.
- Replace the older AI improvement loop document with a short superseded/status pointer so workers do not execute stale PR sequencing.

**Validation:**

- Documentation contains no unresolved placeholder markers.
- Documentation does not claim unmerged status for PR #28-#36.
- `git diff --check` passes.

**Deliverable:** this document becomes the authority for the next managed PRs.

### PR B: Operator AI Improvement UI

**Purpose:** give operators one place to inspect, approve, and execute AI improvement suggestions without hand-copying artifacts.

**Build:**

**Primary files:**

- `artifacts/web/src/pages/ai-analysis.tsx`
- `artifacts/web/src/lib/api.ts`
- `artifacts/web/src/lib/types.ts`
- `artifacts/web/src/lib/i18n.ts`
- Generated clients only if the API schema changes:
  - `lib/api-client-react/src/generated/*`
  - `lib/api-zod/src/generated/*`

**Do not touch in PR B unless a failing test proves it is required:**

- backend tracking, high-recall, follow-cam, highlight, or AI improvement logic;
- real output videos or generated run artifacts;
- API schema semantics beyond adding frontend-compatible types.

- Add an AI Improvement panel or route that reads the run-level report and approval state.
- Group items by user need: missing ball, noise, camera motion, and highlights.
- Show packet-level evidence for missing-ball and noise items, including ROI or `not_visible`, failure tags, source packet id, visual review id, and affected frame window.
- Show camera-motion evidence, nearby track context, recommended action, and whether tracking rerun is required before follow-cam rerender.
- Show highlight `core_window`, `render_window`, `buffer_policy`, AI `suggested_window`, clip action, and boundary warnings.
- Provide operator actions for:
  - approve selected targeted rerun or ROI-localized child run;
  - explicitly execute approved child rerun through the supported API/CLI path;
  - approve config patch as a derived patch without silently applying it;
  - approve follow-cam rerender plan without silently rerendering;
  - approve/render a suggested highlight window.
- Display provenance and model routing information so operators know whether packet review and run-level improvement used different models.

**Safety requirements:**

- UI actions must call the approval/apply surfaces; they must not infer execution from artifact presence.
- UI copy must distinguish advisory AI reports from approved executable intent.
- UI must not expose a path that mutates track CSV files during review/improvement-only phases.

**Validation:**

- `pnpm --filter @workspace/web run typecheck`
- `pnpm run typecheck:libs`
- `$env:PYTHONPATH='python_backend'; .\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check`
- If API schemas are touched: `pnpm --filter @workspace/api-spec run codegen`, then rerun the two typecheck commands above.
- Focused API/UI tests prove approval buttons submit the correct action payload.
- Manual smoke confirms no action runs merely because `ai_improvement_approved_actions.json` exists.

**Deliverable:** operator-facing workflow for reviewing and applying AI improvement suggestions.

### PR C: Real-Video Quality Gate And Stable Output Recipe

**Purpose:** prove the merged backend contracts improve real operator outcomes without hidden mutation.

**Build:**

**Known local smoke input:**

- Source video: `python_backend/data/raw5760x144020fps.mp4`
- Current known output fixture: `python_backend/outputs/full_workflow_latest_review_20260622_060600`
- If that fixture is unavailable, PR C must document the exact replacement output directory and why it is equivalent.

**Primary files:**

- Create: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Create: `python_backend/scripts/run_ai_improvement_quality_gate.py`
- Create: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify docs only as needed to document the new command sequence.

- Add a stable documented command sequence or script that runs on the known real-video output:
  - packet generation;
  - optional visual review when a provider is configured;
  - run-level AI improvement;
  - approval dry-run or sample approval;
  - explicit approved child rerun planning/execution;
  - camera motion audit and approved follow-cam rerender planning;
  - highlight default and approved suggested-window validation.
- Write a quality summary artifact such as `ai_improvement_quality_gate.json`.
- Record before/after hashes for `ball_track.csv` and `ball_track.cleaned.csv` across review/improvement-only phases.
- Report missing-ball, dense-noise, camera-motion, and highlight-boundary suggestions.
- Report whether packet review and improvement used the expected model routing.
- Compare quality after approved actions where available:
  - child rerun or config/rerun impact for missing-ball/noise cases;
  - camera spike counts and regression thresholds for follow-cam plans/rerenders;
  - highlight windows preserving required post-shot/result frames.

**Quality fields:**

- `track_hash_unchanged`
- `approved_actions_explicitly_consumed`
- `missing_ball_roi_or_not_visible_present`
- `noise_failure_tags_present`
- `camera_regression`
- `highlight_tail_ok`
- `provider_status`
- `model_routing_recorded`
- `artifacts_produced`

**Pass/fail rules:**

- `track_hash_unchanged` passes only when both `ball_track.csv` and `ball_track.cleaned.csv` hashes are identical before and after packet/review/improvement-only phases.
- `approved_actions_explicitly_consumed` fails if approved actions are consumed only because `ai_improvement_approved_actions.json` exists in the output directory.
- `camera_regression` fails if an approved rerender comparison increases `review_event_count`, `max_pan_step_px`, or `p95_pan_step_px` by more than 5%.
- `highlight_tail_ok` fails if any checked highlight render window ends before the candidate core end frame plus the configured minimum post-event tail.
- `provider_status` may be `unavailable` only for dry-run or fake-client mode; real-provider mode must record the selected packet-review and run-level improvement models.

**Validation:**

- Gate runs without a real provider in dry-run/fake-client mode.
- Real-provider runs record selected models instead of silently falling back without provenance.
- Review/improvement-only phases do not mutate track CSV files.
- The gate fails if approved actions are consumed implicitly by file presence.
- The gate fails if highlight render windows clip required post-shot/result tails.
- Required commands:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement_quality_gate.py -q
$sourceOutput = 'python_backend\outputs\full_workflow_latest_review_20260622_060600'
$inputVideo = 'python_backend\data\raw5760x144020fps.mp4'
.\.venv\Scripts\python.exe python_backend\scripts\run_ai_improvement_quality_gate.py --output-dir $sourceOutput --input-video $inputVideo --dry-run --report-name ai_improvement_quality_gate.json
```

**Deliverable:** repeatable real-video validation recipe and quality-gate artifact.

### PR D: README/Operation Docs And Final Validation

**Purpose:** make the completed AI improvement loop usable and supportable by operators.

**Build:**

- Update README and operation guides to explain:
  - AI Review versus AI Improvement;
  - packet-level visual review versus run-level improvement;
  - model routing and when to use a stronger improvement model;
  - approval/apply workflow;
  - explicit child rerun execution from approved actions;
  - config patch handling;
  - follow-cam rerender planning;
  - highlight default buffers and approved suggested-window rendering;
  - quality-gate workflow and expected artifacts.
- Include troubleshooting guidance for unavailable providers, malformed AI responses, missing packet evidence, and no-op approvals.
- Document the artifact list and the meaning of advisory versus approved files.
- Run final end-to-end validation on the stable real-video recipe from PR C.

**Validation:**

- README and operation docs match current API/CLI behavior.
- Final end-to-end run produces the expected artifacts.
- Track hashes stay unchanged until an explicitly approved apply/rerun path is invoked.
- Operator docs do not imply that reports, approved-action files, configs, tracks, rerenders, or highlights mutate by discovery alone.

**Deliverable:** final operator documentation and end-to-end validation record.

## End-To-End Acceptance Criteria

- Missing-ball cases show packet-level ROI or explicit `not_visible`, run-level `targeted_rerun` or `localize_ball_roi`, and explicit approved high-recall child execution.
- Dense-noise cases show micro packets, failure tags, bounded AI suggestions, and post-approval quality comparison.
- Camera-motion cases show audit evidence, track context, `adjust_follow_cam` or `tracking_rerun_before_follow_cam`, and approved `follow_cam_rerender_plan.json` without silent rerendering.
- Highlight cases show `core_window`, `render_window`, `buffer_policy`, approved suggested-window rendering, and real-video tail-frame validation.
- `ai_improvement_report.json` remains advisory in every path.
- `ai_improvement_approved_actions.json` is never auto-consumed by file presence.
- Review and improvement phases never mutate `ball_track.csv` or `ball_track.cleaned.csv`.
- UI and docs make approval, execution, and provenance visible to operators.

## Managed PR Operating Rules

- Start each remaining PR from latest `main`.
- Use one focused branch per PR.
- Do not touch generated videos or real output artifacts unless the PR explicitly owns a quality-gate fixture or documented validation output.
- Use fake AI clients in automated tests.
- Run focused local validation before publishing.
- Use review agents for spec compliance and code quality before merge.
- Merge only after valid review feedback and CI failures are resolved.
