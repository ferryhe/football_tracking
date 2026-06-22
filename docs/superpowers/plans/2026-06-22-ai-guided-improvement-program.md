# AI Guided Improvement Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or managed-pr-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn AI from a passive reviewer into a bounded improvement assistant for missing-ball recovery, noise reduction, follow-cam stability, and highlight clip boundaries.

**Architecture:** Deterministic pipeline artifacts remain the source of truth. AI reads review packets, tracking audits, camera motion audits, event candidates, and optional visual-review results, then writes structured recommendations. Any recommendation that can change tracking, high-recall reruns, configs, follow-cam rendering, or highlight windows must be explicitly approved and must preserve provenance.

**Tech Stack:** Python backend, FastAPI/Pydantic schemas, existing AI provider abstraction, review packet media, camera motion audit, high-recall rerun/reconcile modules, event/highlight pipeline, OpenAPI/generated clients, pytest.

---

## Requirement Summary

The user-visible requirement is not "AI says pass/fail." It is:

1. **Ball is missing:** AI should inspect the relevant packet/window and help locate where the ball likely is, or state that the ball is not visible.
2. **Noise is too high:** AI should classify false positives and suggest a bounded filtering/recovery adjustment instead of asking us to eyeball thousands of frames.
3. **Camera is too jumpy:** AI should diagnose whether the follow-cam jump came from camera tuning or from bad/lost tracking, then suggest rerender or rerun action.
4. **Highlights need better boundaries:** The system should create default pre/post buffers, then AI can extend/trim/split the clip window when the default misses the shot result or celebration.

Additional product rules:

- AI improvement should be stronger than AI review. Add explicit model routing so packet triage can use a smaller review model while run-level improvement diagnosis uses a configured improvement-capable model when available.
- AI suggestions are advisory until approved.
- AI must not silently edit `ball_track.csv`, `ball_track.cleaned.csv`, active configs, or rendered highlights.
- Every actionable suggestion must include `improvement_id`, affected frame window, evidence, confidence, and source provenance.
- Pipeline stages must not auto-apply `ai_improvement_report.json` or `ai_improvement_approved_actions.json` just because those files exist. Track changes, high-recall reruns, follow-cam rerenders, highlight renders, and config patches require an explicit approve/apply API call or an explicit CLI argument.

## Current State

- **PR1 merged:** AI improvement core exists: `ai_improvement_report.json`, CLI, API `POST /api/v1/ai/improve`, metrics, OpenAPI/client support.
- **PR2 merged:** Review packets and visual review are richer: dense-noise/high-recall micro packets, failure tags, packet purpose, ROI/localization fields, source packet IDs, and visual-review provenance.
- **PR3 merged:** Approved recovery action flow has landed. It adds `ai_improvement_approved_actions.json`, approval API, approved targeted high-recall planning, ROI merge, config-patch artifact, reconcile provenance, generated clients, and provider/model routing for AI improvement.
- **PR4 in progress:** Camera-motion AI improvement is implemented on `feat/ai-camera-improvement` but still needs review-finding fixes before publishing: stable detected large track jumps must not be dismissed as fast play, missing/malformed `ball_track.csv` must produce an ambiguous camera review item, mixed camera approvals must prioritize `tracking_rerun_before_follow_cam`, and API schemas/generated clients must preserve camera summary and evidence fields.
- **Remaining gaps:** Finish PR4, then implement highlight boundary improvement, operator UI/docs, and full real-video validation.

## Deliverable Map

| Need | What We Build | Tests | Final Artifacts |
| --- | --- | --- | --- |
| Missing ball | AI localization plus approved targeted rerun windows | AI improvement, high-recall windows, reconcile, API approval tests | `ai_improvement_report.json`, `ai_improvement_approved_actions.json`, high-recall window/reconcile provenance |
| Too much noise | Dense-noise packet splitting, AI failure tags, tighten/loosen suggestions | review packet, visual review, AI improvement tests | packet diagnostics, noise-root-cause suggestions, safe config patch artifact |
| Jumpy camera | Camera audit joined with track status, AI camera action recommendations, rerender plan | camera audit, follow-cam, AI improvement, API service tests | `camera_motion_audit.json`, camera improvement items, `follow_cam_rerender_plan.json` |
| Highlights | event core/render windows, longer post-roll, AI boundary suggestions, approved suggested-window render | event, highlight service, API/OpenAPI, review packet tests | `event_candidates.json`, `highlight_report.json`, AI highlight adjustment entries |
| Operator control | Approval API and UI/docs showing what AI suggested and what can be applied | API, generated client, frontend typecheck, docs review | AI Improvement UI, operation guide, README updates |

## Core Contracts

### AI Improvement Report

`ai_improvement_report.json` is the run-level diagnosis artifact.

Required fields:

- `schema_version`
- `generated_at`
- `model`
- `source_artifacts`
- `artifact_status`
- `summary`
- `improvements`
- `highlight_adjustments`

Each improvement should include:

- `id`, used as `improvement_id` by approval requests and approval artifacts
- `priority`
- `area`
- `failure_tags`
- `root_cause_module`
- `start_frame`
- `end_frame`
- `diagnosis`
- `recommended_action`
- `rerun_scope` when applicable
- `likely_ball_region` or `local_search_roi` for visible missing-ball cases
- `config_patch` only after strict allowlist validation
- `evidence`
- `confidence`
- provenance fields such as `source_packet_id`, `visual_review_id`, and `camera_motion_event_id` when available
- action-specific fields:
  - missing-ball actions require `targeted_rerun`, `localize_ball_roi`, or explicit `not_visible` evidence
  - noise actions require `false_positive_class`, bounded frame/window scope, and either `noise_filter_adjustment` or `reject_noise`
  - camera actions require `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, or `human_review_camera_motion`
  - highlight actions require `adjust_highlight_window` or `render_suggested_highlight`

### Approval Artifact

`ai_improvement_approved_actions.json` is the only artifact that turns AI advice into an executable intent.

Required behavior:

- Approving `targeted_rerun` can feed high-recall windows.
- Approving `adjust_follow_cam` can write `follow_cam_rerender_plan.json`.
- Approving `tracking_rerun_before_follow_cam` records that tracking must be rerun before rerender.
- Approving `adjust_highlight_window` or `render_suggested_highlight` can render a suggested explicit frame window.
- Approving config-like actions writes a derived config patch artifact but does not mutate active config.
- Approved actions are not auto-discovered by default rerun/render paths. The caller must explicitly pass the approved-action artifact or use a dedicated approval/apply endpoint.

## AI Usage And Prompt Mechanism

AI has two different jobs and they should use different prompts and, when available, different model strength:

1. **Packet-level review:** low-cost triage of small visual packets. This can answer whether the ball is visible, whether a candidate is likely noise, and whether a highlight packet is publishable.
2. **Run-level improvement:** stronger diagnosis over artifacts plus selected packet evidence. This must produce bounded actions, not just "good/bad" labels.

The run-level improvement prompt must require:

- strict JSON with `summary`, `improvements`, and `highlight_adjustments`;
- affected `start_frame` and `end_frame` for every action;
- explicit `not visible` when the ball cannot be localized, instead of inventing an ROI;
- `local_search_roi` only when visual evidence supports it;
- false-positive class for noise actions, such as `foot_confusion`, `shoe_confusion`, `sideline_confusion`, or `wall_background_drift`;
- camera diagnosis that distinguishes tracking-driven camera jumps, follow-cam tuning problems, and acceptable fast play;
- highlight suggestions that protect the shot/result tail, especially the frames after the shot or goal event;
- confidence and provenance for every actionable suggestion.

The system prompt should say that AI advice is advisory until approval. The model must not claim it edited tracks, configs, rerenders, or highlights; it can only propose actions that the approval flow may apply later.

## PR Plan

### PR3: Approved Recovery Actions

**Branch:** `feat/ai-approved-recovery-actions`

**Current status:** Complete and merged as GitHub PR #30. Keep this section as historical scope and regression coverage for later PRs.

**Build:**

- Approval artifact writer/validator in `python_backend/football_tracking/ai_improvement.py`.
- API `POST /api/v1/ai/improve/{run_id}/approve`.
- Pydantic request/response schemas for selected `improvement_ids` and optional overrides.
- Safe config-patch filtering with warnings for invalid paths.
- Visual-review ROI merge into improvement items by `source_packet_id`.
- Explicit actionable schemas for missing-ball and noise improvements:
  - `localize_ball_roi` with `local_search_roi`, `rerun_scope`, evidence, confidence, and provenance
  - `targeted_rerun` with bounded frame scope and optional local search ROI
  - `noise_filter_adjustment` with `false_positive_class`, affected window, safe config patch, evidence, confidence, and provenance
  - `reject_noise` for confirmed false-positive windows that should not trigger rerun
- Improvement-model routing:
  - `POST /api/v1/ai/improve` accepts or resolves an improvement model separately from packet triage defaults.
  - The selected model and fallback/provider status are persisted in `ai_improvement_report.json`.
  - Provider-unavailable runs write stable unavailable/error status without blocking deterministic artifacts.
- `build_high_recall_windows(..., approved_actions_path=...)`.
- Reconcile provenance and `changed_frame_count` for AI-approved windows.
- OpenAPI and generated clients.

**Test:**

- `python_backend/tests/test_ai_improvement.py`
- `python_backend/tests/test_high_recall_windows.py`
- `python_backend/tests/test_high_recall_reconcile.py`
- `python_backend/tests/test_api_service.py`
- `python_backend/tests/test_export_openapi.py`
- `python_backend/scripts/export_openapi.py --check`
- generated client typecheck

**Acceptance:**

- No approved artifact means high-recall behavior is unchanged.
- Only approved `targeted_rerun` creates AI high-recall windows.
- Invalid config patch keys are stripped and recorded.
- `not visible` visual review does not invent a fake ROI.
- Reconcile report identifies AI-approved windows and frame changes.
- Missing-ball and noise actions are not just labels; they include bounded scope, evidence, confidence, and an approvable action.
- AI improvement uses the configured improvement model when available and records fallback/provider status.

**Deliver:**

- Merged PR with approval endpoint, approval artifact, derived config patch artifact, generated clients, and tests.

### PR4: Camera Motion AI Improvement

**Branch:** `feat/ai-camera-improvement`

**Current status:** In progress. Implementation must resolve the review findings listed in "Current State" before commit, PR creation, or merge.

**Preflight:**

- Sync from latest `origin/main`.
- Confirm current working-tree changes are only PR4 scope.
- If the in-progress branch is stale or polluted, rebuild a fresh branch and reapply only the camera-motion AI improvement changes.

**Build:**

- Extend AI improvement context to read `camera_motion_audit.json`.
- Join camera motion events with nearby `ball_track.csv` status in a +/- 12 frame window.
- Distinguish:
  - track-driven jump: Lost/Predicted/large jump nearby
  - follow-cam tuning issue: stable tracking but sudden camera movement
  - acceptable fast-play movement: high action speed with continuous track
- Add recommended actions:
  - `adjust_follow_cam`
  - `tracking_rerun_before_follow_cam`
  - `human_review_camera_motion`
- Add approval handling for `follow_cam_rerender_plan.json`.
- Add metrics summary for camera improvement severity and action counts.
- Validate follow-cam config patches by both path and value:
  - allowed root: `follow_cam`
  - allowed keys: `pan_smoothing`, `zoom_smoothing`, `glide_pan_smoothing`, `glide_max_pan_per_frame_x`, `glide_max_pan_per_frame_y`, `catch_up_pan_smoothing`, `catch_up_max_pan_per_frame_x`, `catch_up_max_pan_per_frame_y`, `predicted_pan_decay`, `max_pan_per_frame_x`, `max_pan_per_frame_y`, `max_zoom_in_per_frame`, `max_zoom_out_per_frame`, `zoom_out_confirm_frames`, `zoom_in_confirm_frames`, `zoom_hold_frames_after_change`, `dead_zone_ratio_x`, `dead_zone_ratio_y`, `enabled`
  - reject negative pan/zoom limits, non-numeric values, and dead-zone ratios outside 0.0-0.8

**Test:**

- Smooth camera path produces no camera improvement item.
- Camera event overlapping Lost/Predicted creates `tracking_rerun_before_follow_cam`.
- Camera event with stable detected tracking creates `adjust_follow_cam`.
- Camera event with stable detected tracking but an extreme nearby ball-track jump creates `human_review_camera_motion`, not "acceptable fast play".
- Camera event without readable `ball_track.csv` creates `human_review_camera_motion` with unavailable/ambiguous track context.
- Approving mixed camera actions prioritizes `tracking_rerun_before_follow_cam` in `follow_cam_rerender_plan.json`.
- Invalid follow-cam patch names are stripped.
- Approval writes `follow_cam_rerender_plan.json`.
- API schemas, OpenAPI, and generated clients preserve camera summary fields, `camera_motion_event_id`, `camera_motion_severity`, and evidence payload fields.
- Metrics include camera improvement counts.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_camera_motion_audit.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_export_openapi.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check
corepack pnpm run typecheck:libs
```

**Deliver:**

- AI camera improvement items.
- `follow_cam_rerender_plan.json` for approved camera-only rerenders.
- Metrics/release-gate view of camera stability.

### PR5: Highlight Boundary AI Improvement

**Branch:** `feat/highlight-boundary-improvement`

**Build:**

- Update event candidates to carry:
  - `core_window`
  - `render_window`
  - `buffer_policy`
- Use duration-based defaults and convert them to frames from candidate/video FPS:
  - shot: 0.75 seconds before, 4.5 seconds after
  - goal: 0.75 seconds before, 6.0 seconds after
- Record the converted frame counts in `buffer_policy` for reproducibility.
- Make candidate-based highlight rendering use `candidate.render_window` by default.
- Add explicit override mode for manual pre/post roll.
- Add AI `highlight_adjustments` with `clip_action`, `suggested_window`, reason, confidence.
- Allow approved AI highlight windows to render as explicit frame windows with provenance.
- Validate every AI suggested highlight window:
  - it must include the candidate `core_window`;
  - it must preserve at least the event-type minimum tail after the shot/result anchor;
  - it must be clamped to video boundaries;
  - if a human override violates those invariants, record a warning in `highlight_report.json`.

**Test:**

- Candidate event has both core and render windows.
- Candidate render defaults to `render_window`, not legacy 15/30 roll.
- Buffer seconds convert correctly at two frame rates, for example 20fps and 30fps.
- Manual override still works for explicit operator choices.
- AI highlight adjustment validates `candidate_id`, `suggested_window`, and `clip_action`.
- AI `trim_tail` cannot remove the shot/result tail below the configured minimum post-event frames.
- Approved suggested highlight writes `highlight_report.json` with approval provenance.
- OpenAPI and generated clients stay current.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_events.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_review_packets.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_highlights.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check
```

**Deliver:**

- Highlight candidates with safe default buffers.
- AI suggested clip boundaries.
- Approved AI-suggested highlight rendering path.
- Tests proving shot/goal render windows include enough post-event frames so the final shot result is not clipped away.

### PR6: Operator UI And Documentation

**Branch:** `feat/ai-improvement-ui-docs`

**Build:**

- AI Improvement UI panel showing:
  - primary issue
  - missing-ball suggestions and ROIs
  - noise root-cause tags
  - camera-motion suggestions
  - highlight boundary suggestions
  - approval state
- UI actions:
  - approve selected targeted rerun
  - approve follow-cam rerender plan
  - approve/render suggested highlight window
  - copy/save config patch without applying it silently
- README and operation-guide updates:
  - AI review vs AI improvement
  - model choice guidance
  - approval workflow
  - real-video smoke workflow
  - artifact list and interpretation

**Test:**

- API client generated types compile.
- Frontend typecheck passes.
- Approval buttons call the approval API with the right action payload.
- Docs mention no silent mutation of tracks/configs.

**Commands:**

```powershell
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check
pnpm exec tsc --noEmit
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_export_openapi.py -q
```

**Deliver:**

- Operator-facing AI improvement workflow.
- Updated README and operation docs.

### PR7: Real-Video Quality Gate And Stable Output Recipe

**Branch:** `feat/ai-improvement-real-video-gate`

**Build:**

- Add a reproducible smoke script or documented command sequence that runs:
  - review packet generation
  - visual review when API key exists
  - AI improvement
  - approval dry-run/sample approval
  - targeted high-recall planning from approved actions
  - follow-cam motion audit
  - highlight render selection
- Add a summary artifact such as `ai_improvement_quality_gate.json`.
- The quality gate should compare before/after track hashes to prove AI report generation did not mutate tracks.
- The gate should report:
  - missing-ball suggestions
  - dense-noise suggestions
  - camera-motion suggestions
  - highlight-window suggestions
  - whether a stronger model was used
  - artifacts produced
- Add optional before/after quality comparison for approved actions:
  - camera spike counts before and after an approved follow-cam rerender plan
  - highlight windows containing enough post-shot/result frames after default or AI-adjusted boundaries
- Quality gate pass/fail fields:
  - `track_hash_unchanged`: pass only when review/improvement-only phases leave track CSV hashes unchanged.
  - `camera_regression`: fail if approved rerender increases `review_event_count`, `max_pan_step_px`, or `p95_pan_step_px` by more than 5%.
  - `highlight_tail_ok`: pass only when each rendered candidate ends at or after `event_anchor_frame + min_post_frames`.
  - `provider_status`: pass with `unavailable` only in dry-run/fake-client mode; real AI runs must record the selected model.

**Test:**

- Script runs without a real API key in dry-run/fake-client mode.
- Script records unavailable provider status instead of failing hard.
- Track hashes are unchanged by review/improvement-only phases.
- Malformed AI responses produce stable error/unavailable reports without mutating tracks or renders.
- When an approved action file exists, targeted high-recall planning consumes it and records provenance.
- Camera quality-gate comparison reports whether spike count or worst spike improved after approved rerender planning.
- Highlight quality-gate comparison reports whether render windows include the configured post-event duration.

**Commands:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_metrics.py -q
```

**Deliver:**

- One repeatable real-video validation recipe.
- Stable output quality-gate artifact.
- Final docs showing how to reproduce the AI improvement loop.
- A comparison report that tells the operator whether the AI-approved rerun/rerender actually improved the visible result, instead of only proving that artifacts exist.

## End-To-End Acceptance Criteria

- AI improvement can identify missing-ball windows and produce likely region or explicit "not visible" output.
- Dense-noise and high-recall rejection windows are small enough for vision review, not multi-thousand-frame blobs.
- Approved targeted reruns are consumed only from `ai_improvement_approved_actions.json`.
- Camera-motion spikes are tied back to either tracking loss or follow-cam tuning.
- Follow-cam rerender is planned, not silently executed.
- Highlight default render window includes enough post-shot frames, especially after shots/goals.
- AI highlight suggestions can extend or trim windows and be rendered after approval.
- UI/docs clearly distinguish review, improvement, approval, and execution.
- Real-video smoke output includes all expected artifacts.
- Real-video smoke reports user-visible quality deltas, not just artifact presence.
- Track CSV files are unchanged by review/improvement report generation.

## Managed PR Operating Rules

- Start each PR from latest `origin/main`.
- Use one fresh branch per PR.
- Use a worker agent for implementation.
- Use a separate spec-review agent and code-quality review before publishing.
- Run local focused tests before PR.
- Push and create PR.
- Wait for CI and Copilot/comments.
- Fix valid remote feedback.
- Merge only after checks and valid feedback are resolved.
- Delete merged local and remote branches after the PR is merged and this project/user authorization applies.

## Independent Review Request

Ask a separate agent to review this plan for:

- Whether it fully covers missing-ball, noise, camera stability, and highlight-boundary needs.
- Whether AI improvement is clearly separated from AI review.
- Whether any step lets AI mutate tracks/configs/renders without approval.
- Whether PR boundaries are small enough for managed PR execution.
- Whether tests cover unit, API, generated clients, real-video smoke, and provider-unavailable cases.
- Whether deliverables are concrete enough for acceptance.

## Prior Independent Review Incorporated

A prior independent reviewer found no critical gaps. The plan was updated for five points:

- Model routing is now concrete: improvement diagnosis must use a configured improvement-capable model when available, while lightweight packet triage can stay on a smaller model.
- PR3 now requires actionable missing-ball and noise schemas, including `localize_ball_roi`, `noise_filter_adjustment`, `false_positive_class`, bounded scope, evidence, confidence, and provenance.
- PR3 execution now explicitly protects managed PR hygiene: local worker changes must be reviewed against latest `origin/main`, and reapplied to a fresh branch if the branch baseline is stale or polluted.
- Highlight buffers are now duration-based and converted from FPS, with tests at multiple frame rates.
- The real-video quality gate now checks user-visible quality deltas for camera smoothness and highlight boundary coverage, not only artifact existence.

## 2026-06-22 Independent Review Incorporated

The latest independent reviewer found no critical gaps and recommended five refinements. This plan now includes them:

- Approved action artifacts are never auto-applied by file presence; callers must explicitly approve/apply or pass the artifact path.
- PR5 highlight validation now protects `core_window` and the post-shot/result tail, including a test against bad `trim_tail` suggestions.
- PR4 now includes fresh-branch preflight, OpenAPI/generated-client gates, and follow-cam patch value validation.
- PR7 now defines concrete quality-gate pass/fail fields for track hash safety, camera regression, highlight tails, and provider status.
- The plan clarifies the report `id` versus approval `improvement_id` linkage and marks the earlier review section as historical.
