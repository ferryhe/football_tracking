# AI Improvement Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current AI review workflow into an AI improvement loop that can diagnose missed balls, excessive noise, follow-cam instability, and highlight clip boundary problems, then produce actionable rerun/config/clip-window recommendations.

**Architecture:** Keep deterministic tracking artifacts as the source of truth, and add AI improvement reports as a separate layer that reads those artifacts and packet media. Do not make AI silently rewrite tracks or configs; AI outputs structured suggestions, confidence, affected frame windows, and optional config patches that the operator can apply or use for targeted reruns.

**Tech Stack:** Python backend, FastAPI/Pydantic schemas, existing `OpenAIResponsesClient`, review packet image artifacts, existing React AI Analysis page, pytest/unittest test suite, generated OpenAPI clients.

---

## Current Repo State And Remaining Work

- Already landed:
  - `ball_audit.json` detects lost gaps, large jumps, suspicious tracklets, low-confidence islands, and postprocess actions.
  - `ai_review_triggers.json` converts ball audit findings into deterministic review triggers.
  - `review_packets.json` and `review_packets/` create contact sheets, crop sheets, clips, and routing labels.
  - `ai_visual_review.json` can ask a vision model whether highlight packets are publishable.
  - `camera_motion_audit.json` flags abrupt final follow-cam pan, acceleration, and zoom events.
  - `event_candidates.json` creates shot/goal candidates with a single `render_window`.
  - `ai_contracts.py`, `ai_improvement.py`, `scripts/run_ai_improvement.py`, `POST /api/v1/ai/improve`, and metrics support for `ai_improvement_report.json` landed in PR 1 (`feat/ai-improvement-core`, merged as GitHub PR #28).
  - `/api/v1/ai/recommend` exists, but it is a broad config recommendation endpoint, not a run-artifact-specific improvement loop.
  - `ai_review_recommendations.json` was produced during manual experimentation as a one-off report; it is useful evidence for prompt shape, but it is not a durable program artifact or API contract.
- Remaining managed PR execution should start at PR 2 from latest `main` after PR #28, unless a branch already contains equivalent reviewed changes.
- Main gaps:
  - AI review is too focused on pass/fail, not "how to improve the next run".
  - Missed-ball recovery packets do not ask AI to localize where the ball likely is.
  - Noise review lacks stable failure tags such as `foot_confusion`, `sideline_confusion`, or `wall_background_drift`, and long `dense_noise_cluster` ranges are not yet split into useful AI windows.
  - Camera motion warnings are not tied back to tracking/follow-cam parameter suggestions.
  - Highlight generation still uses simple pre/post roll defaults and does not let AI recommend better start/end frames.

## Requirement Summary And Coverage

This program changes the AI role from "review the output" to "suggest the next improvement." The deterministic pipeline still owns tracking, reconciliation, rendering, and artifact generation. AI reads the artifacts and packet media, then proposes bounded actions that an operator can approve.

| User need | Current state | Planned solution | Main PR | Tests | Deliverables |
| --- | --- | --- | --- | --- | --- |
| Ball is missing and AI should help find it | `lost_gap` and high-recall windows exist, but AI does not return a likely ball location | Add missing-ball improvement items with `likely_ball_region`, `local_search_roi`, and `rerun_scope`; approved suggestions can feed targeted high-recall planning | PR 1, PR 2, PR 3 | `test_ai_improvement.py`, `test_ai_visual_review.py`, `test_high_recall_windows.py`, `test_high_recall_reconcile.py` | `ai_improvement_report.json`, `ai_improvement_approved_actions.json`, reconcile provenance |
| Too many noise detections and AI should help classify them | Ball audit can flag bad segments, but packets do not carry enough root-cause labels; `dense_noise_cluster` is skipped too aggressively for diagnosis | Split high-recall rejections and dense-noise ranges into micro-windows; add failure tags such as `foot_confusion`, `shoe_confusion`, `sideline_confusion`, `wall_background_drift` | PR 2, PR 3 | `test_review_packets.py`, `test_ai_visual_review.py`, `test_ai_improvement.py` | Better `review_packets.json`, enhanced `ai_visual_review.json`, noise-focused improvement actions |
| Follow-cam is too jumpy and AI should help adjust tracking or camera settings | `camera_motion_audit.json` exists, but it only flags camera-path problems after render | Join camera motion events with nearby track status, then suggest `adjust_follow_cam` or `tracking_rerun_before_follow_cam` | PR 4 | `test_camera_motion_audit.py`, `test_follow_cam.py`, `test_ai_improvement.py` | Camera-specific improvement actions, metrics release gate summary |
| Highlight clips need default buffer and AI should adjust clip boundaries | Event candidates have render windows, but defaults can miss post-shot/result frames and AI does not tune boundaries | Separate core event window from render window, extend default post-roll, and let AI suggest `highlight_adjustments` | PR 5 | `test_events.py`, `test_review_packets.py`, `test_api_service.py`, `test_high_recall_windows.py` | `event_candidates.json` with `core_window`/`render_window`, `highlight_report.json`, AI boundary suggestions |
| Operator needs one stable place to see and apply suggestions | Existing AI endpoints are config recommendation or visual review, not run-specific improvement plans | Add CLI/API/UI for run-level AI improvement; keep suggestions advisory unless explicitly approved | PR 1, PR 3, PR 6 | `test_api_service.py`, `test_export_openapi.py`, frontend type checks | `POST /api/v1/ai/improve`, `POST /api/v1/ai/improve/{run_id}/approve`, frontend AI Improvement panel, docs |

## Review Vs Improvement Boundary

- **AI Review** answers: "Is this packet/output acceptable, suspicious, publishable, or worth manual inspection?"
- **AI Improvement** answers: "Given the artifacts and packets, what should the next run or render change?"
- AI improvement may propose config patches, rerun windows, local search ROIs, follow-cam tuning, or highlight boundaries.
- AI improvement must not silently mutate `ball_track.csv`, `ball_track.cleaned.csv`, configs, or highlight windows. Track-changing or config-changing suggestions require explicit operator approval and provenance.
- Model quality expectation: smaller models can do first-pass classification; improvement diagnosis should allow a stronger model override because it needs root-cause reasoning and bounded action synthesis.

## Shared Contracts

The following enums should be centralized in the backend and reused by report validation, API schemas, UI rendering, and tests:

- `failure_tags`: `ball_lost`, `foot_confusion`, `shoe_confusion`, `sideline_confusion`, `wall_background_drift`, `large_jump_after_reacquire`, `camera_catchup_spike`, `black_frames`, `post_roll_too_short`, `highlight_boundary_unclear`, `unknown`.
- `root_cause_module`: `detection`, `selection`, `reacquisition`, `postprocess`, `stitching`, `packetization`, `event_scoring`, `follow_cam`, `rendering`, `unknown`.
- `recommended_action`: `targeted_rerun`, `tighten_noise_filter`, `loosen_ball_recovery`, `split_packet`, `manual_review`, `reject_noise`, `adjust_highlight_window`, `adjust_follow_cam`, `tracking_rerun_before_follow_cam`, `render_suggested_highlight`.
- `clip_action`: `extend_tail`, `trim_head`, `trim_tail`, `split`, `keep`.

`config_patch` is always a suggested YAML merge patch with a strict code allowlist. Unknown paths, dotted flat keys such as `"tracking.max_lost_frames"`, invalid values, and unsupported roots must be stripped and recorded as warnings. Unknown fields may appear in `evidence_payload`, but not in `config_patch`.

## Approval Contract

AI suggestions become executable only through explicit operator approval. Use one concrete artifact and one API path:

- Artifact: `ai_improvement_approved_actions.json`
- API: `POST /api/v1/ai/improve/{run_id}/approve`

Approval artifact shape:

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-22T00:00:00+00:00",
  "run_id": "run_abc",
  "source_report": "ai_improvement_report.json",
  "approved_by": "operator",
  "approved_actions": [
    {
      "approval_id": "approval_001",
      "improvement_id": "imp_001",
      "approved_action": "targeted_rerun",
      "rerun_scope": {"start_frame": 2010, "end_frame": 2070},
      "local_search_roi": {
        "coordinate_space": "image",
        "frame": 2040,
        "x": 4200,
        "y": 820,
        "width": 1200,
        "height": 620,
        "confidence": 0.66
      },
      "config_patch": {},
      "approval_source": "api",
      "approved_at": "2026-06-22T00:00:00+00:00",
      "provenance": {
        "source": "ai_improvement",
        "improvement_id": "imp_001",
        "model": "gpt-5.4",
        "confidence": 0.82
      }
    }
  ]
}
```

Approved actions can be consumed by high-recall planning, follow-cam rerender planning, or highlight rendering. Default behavior remains unchanged when this artifact is absent.

Action-specific approval fields:
- `targeted_rerun` requires `rerun_scope` and may include `local_search_roi`.
- `adjust_highlight_window` and `render_suggested_highlight` require `candidate_id`, `suggested_window`, and `clip_action`.
- `adjust_follow_cam` requires either a validated `config_patch` under the `follow_cam` root or a `follow_cam_rerender_plan`.
- `tighten_noise_filter` and `loosen_ball_recovery` may produce a derived config patch artifact, but remain advisory unless explicitly applied by the operator.
- `manual_review` and `reject_noise` do not mutate tracks, configs, or renders; they only record operator intent and provenance.
- PR 4 may extend the shared enum with `human_review_camera_motion` while implementing camera-specific approvals.

Approval artifact entries must preserve stable linkage:
- `improvement_id`
- `source_packet_id` when the suggestion came from a packet
- `visual_review_id` when the suggestion came from `ai_visual_review.json`
- `candidate_id` when the suggestion targets a highlight
- `provenance.source`, `provenance.model`, and `provenance.confidence`

## Target AI Capabilities

1. **Ball Missing Assistance**
   - AI reviews lost/reacquire windows with packet images and suggests whether to run high-recall, loosen detection, search a specific region, or accept that the ball is not visible.
   - Output includes frame window, likely region when visible, frame-level local search ROI, suggested rerun scope, and confidence.

2. **Noise Reduction Assistance**
   - AI classifies false positives by failure tag and suggests filtering/selection fixes.
   - Output includes noise type, evidence frames, suspected module, and recommended tuning direction.

3. **Camera Stabilization Assistance**
   - AI combines `camera_motion_audit.json` with track status around the same frames.
   - Output distinguishes true fast play from track-driven甩镜 and suggests follow-cam or tracking fixes.

4. **Highlight Boundary Assistance**
   - The system creates a default clip buffer, then AI recommends whether to extend, trim, or split.
   - Output includes `suggested_window`, `clip_action`, and why.

5. **Run-Level Improvement Plan**
   - AI produces a ranked list of actions: config patch, targeted rerun windows, packet split improvements, or manual review.
   - Output is persisted as `ai_improvement_report.json`.
   - AI suggestions are advisory by default. Any suggestion that can affect track generation, high-recall reruns, reconcile, or config persistence requires explicit operator opt-in and must record provenance.

## PR Program

| Order | Branch | Depends on | Main deliverable | Required gates |
| --- | --- | --- | --- | --- |
| PR 1 | `feat/ai-improvement-core` | merged as PR #28 | `ai_improvement_report.json`, CLI, API, metrics summary | Complete; keep as historical scope, do not re-implement unless regression is found |
| PR 2 | `feat/ai-packet-vision-tags` | latest `main` after PR #28 | micro review packets, dense-noise packets, failure tags, visual ROI fields | Packet/visual-review tests, real-output packet smoke run, reviewers, remote checks |
| PR 3 | `feat/ai-approved-recovery-actions` | PR 2 merged | approved action artifact/API, targeted rerun planning, derived config-patch artifact | AI improvement/high-recall/reconcile tests, approval-flow tests, OpenAPI/client updates, reviewers, remote checks |
| PR 4 | `feat/ai-camera-improvement` | PR 3 merged | camera-motion-to-improvement suggestions and release metrics | camera/follow-cam/AI tests, dry-run smoke, reviewers, remote checks |
| PR 5 | `feat/highlight-boundary-improvement` | PR 4 merged | core/render windows, roll policy, AI highlight boundary render path | event/review/API/highlight tests, OpenAPI export/check, generated clients, reviewers, remote checks |
| PR 6 | `feat/ai-improvement-ui-docs` | PR 5 merged | operator UI and docs | frontend typecheck, API schema tests, docs review, reviewers, remote checks |

Every PR follows the managed PR loop: refresh `main`, create a fresh branch, implement through a worker agent, run spec and code-quality review agents, fix valid findings, push PR, wait for remote comments/checks, merge only after accepted feedback is handled, then delete local and remote branches.

### PR 1: AI Improvement Report Core

**Status:** Complete. This scope landed in GitHub PR #28 and remains in the plan only as historical context for the remaining PRs. Managed PR execution should resume at PR 2 unless tests show a regression in the PR 1 surface.

**Purpose:** Add the structured improvement report layer without changing tracking behavior.

**Files:**
- Create: `python_backend/football_tracking/ai_contracts.py`
- Create: `python_backend/football_tracking/ai_improvement.py`
- Create: `python_backend/scripts/run_ai_improvement.py`
- Modify: `python_backend/football_tracking/api/ai_provider.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/metrics.py`
- Modify generated API artifacts after OpenAPI export:
  - `lib/api-zod/src/generated/*`
  - `lib/api-client-react/src/generated/*`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_config_and_provider.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`

**Data Contract:**

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-22T00:00:00+00:00",
  "model": "gpt-5.4",
  "source_artifacts": {
    "ball_audit": "ball_audit.json",
    "ai_review_triggers": "ai_review_triggers.json",
    "review_packets": "review_packets.json",
    "ai_visual_review": "ai_visual_review.json",
    "camera_motion_audit": "camera_motion_audit.json",
    "event_candidates": "event_candidates.json"
  },
  "artifact_status": {
    "ball_audit": "available",
    "ai_review_triggers": "available",
    "review_packets": "available",
    "ai_visual_review": "missing",
    "camera_motion_audit": "available",
    "event_candidates": "available"
  },
  "summary": {
    "status": "needs_rerun",
    "primary_issue": "noise",
    "improvement_count": 1,
    "targeted_rerun_count": 1,
    "config_patch_count": 0,
    "highlight_adjustment_count": 1
  },
  "improvements": [
    {
      "id": "imp_001",
      "priority": "P0",
      "area": "tracking",
      "failure_tags": ["foot_confusion"],
      "root_cause_module": "reacquisition",
      "start_frame": 2034,
      "end_frame": 2045,
      "diagnosis": "marker likely reacquired a shoe during a lost gap",
      "recommended_action": "targeted_rerun",
      "config_patch": {},
      "rerun_scope": {"start_frame": 2010, "end_frame": 2070},
      "likely_ball_region": {
        "frame": 2040,
        "description": "right-bottom corner area near the cluster of players",
        "confidence": 0.66
      },
      "local_search_roi": {
        "coordinate_space": "image",
        "frame": 2040,
        "x": 4200,
        "y": 820,
        "width": 1200,
        "height": 620,
        "confidence": 0.66
      },
      "evidence": ["lost gap overlaps active corner sequence"],
      "confidence": 0.82
    }
  ],
  "highlight_adjustments": [
    {
      "candidate_id": "cleaned:shot_candidate:5056-5191",
      "current_window": {"start_frame": 5041, "end_frame": 5191},
      "suggested_window": {"start_frame": 5025, "end_frame": 5270},
      "reason": "post-shot result is likely truncated",
      "confidence": 0.74
    }
  ]
}
```

**Implementation Tasks:**

- [ ] **Step 0: Add shared AI contract constants**

Create `ai_contracts.py` with the shared enum values listed in "Shared Contracts". Import these values from `ai_improvement.py`, `ai_visual_review.py`, and API schemas instead of copying string lists by hand.

- [ ] **Step 1: Write missing-artifact tests**

Add `test_write_ai_improvement_report_handles_missing_artifacts` in `python_backend/tests/test_ai_improvement.py`.

Expected behavior:
- The function writes `ai_improvement_report.json`.
- Summary status is `unavailable`.
- No exception is raised when source artifacts are missing.

- [ ] **Step 2: Implement artifact collection**

In `ai_improvement.py`, add:
- `build_ai_improvement_context(output_dir: Path) -> dict[str, Any]`
- `build_ai_improvement_report(output_dir: Path, *, client: Any = None, model: str | None = None, dry_run: bool = False) -> dict[str, Any]`
- `write_ai_improvement_report(...) -> dict[str, Any]`

Rules:
- Read optional JSON artifacts safely.
- Truncate long lists for prompt payloads: top 20 ball audit events, top 20 packets, top 20 camera events, top 20 event candidates.
- Never include image base64 in PR 1.
- On provider failure, write a stable report with `summary.status = "error"` and a redacted error.
- Validate suggested config patches with a code-level allowlist. Prompt instructions are not sufficient.
- Allowed patch paths in PR 1 are limited to read-only suggestions under `follow_cam`, `postprocess`, `scene_bias.dynamic_air_recovery`, `selection`, and `tracking`; each path must have a type/range validator before it can appear in `config_patch`.

- [ ] **Step 2a: Support safe model override**

Update `OpenAIResponsesClient.create_json_response()` to accept `model: str | None = None`, matching the existing vision method behavior.

Rules:
- If `model` is omitted, use `settings.chat_model`.
- If `model` is provided by CLI/API, persist the selected model name in `ai_improvement_report.json`.
- Tests must prove the request payload uses the override and never logs the API key.

- [ ] **Step 3: Add strict response validation**

Validate:
- `summary.status` is one of `ok`, `needs_rerun`, `unavailable`, `error`.
- `improvements` is a list.
- Each improvement has `priority`, `area`, `failure_tags`, `root_cause_module`, `recommended_action`, `confidence`.
- `targeted_rerun` improvements must include `rerun_scope`.
- Missing-ball improvements should include either `likely_ball_region` or `local_search_roi`; if the ball is not visible, use `likely_ball_region.description = "not visible"` and omit `local_search_roi`.
- Unknown fields are allowed inside `evidence_payload`, but required top-level fields must exist.
- Unknown or invalid `config_patch` paths are stripped with warnings and never persisted as accepted patch content.
- `local_search_roi.coordinate_space` must be exactly `"image"`; frame/x/y must be non-negative; width/height must be positive.
- `summary.status = "ok"` with non-empty improvement or highlight action lists is normalized to `needs_rerun` or rejected by validation.
- `artifact_status` is persisted in `ai_improvement_report.json` so consumers do not need to infer missing/corrupt source artifacts from warning strings.

- [ ] **Step 4: Add CLI**

`python_backend/scripts/run_ai_improvement.py` accepts:
- `output_dir`
- `--model`
- `--dry-run`
- `--max-items`

The CLI prints summary JSON and writes `ai_improvement_report.json`.

- [ ] **Step 5: Add API endpoint**

Add `POST /api/v1/ai/improve`.

Request:
- `run_id`
- `objective`
- `model`
- `dry_run`

Response:
- compact report summary
- report artifact path
- `improvements`
- `highlight_adjustments`

The Pydantic response model should be structured, not `dict[str, Any]`, for:
- `AIImproveSummary`
- `AIFrameWindow`
- `AILikelyBallRegion`
- `AILocalSearchRoi`
- `AIImprovementItem`
- `AIHighlightAdjustment`
- `AIImproveResponse`

- [ ] **Step 5a: Export OpenAPI and clients in the same PR**

Run OpenAPI export and regenerate checked-in clients in PR 1, because this PR introduces a new public API contract.

- [ ] **Step 6: Include metrics summary**

`metrics.py` should include compact `ai_improvement` stats when `ai_improvement_report.json` exists:
- `status`
- `primary_issue`
- `improvement_count`
- `targeted_rerun_count`
- `config_patch_count`
- `highlight_adjustment_count`

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_config_and_provider.py::ConfigAndProviderTests::test_create_json_response_accepts_model_override -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_ai_improve_writes_report -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_export_openapi.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py
```

Additional fake-client coverage:
- Provider timeout writes `summary.status = "error"` with a redacted message.
- Corrupt JSON artifacts are skipped and recorded in report warnings.
- `--dry-run` writes deterministic improvement suggestions without calling the provider.
- No configured API key produces an `unavailable` or dry-run-safe result instead of crashing.
- `--max-items` limits prompt context size.

**Deliverables:**
- `ai_improvement_report.json`
- `POST /api/v1/ai/improve`
- CLI command for local runs
- Metrics summary field `ai_improvement`
- Generated OpenAPI/client updates for the new endpoint

---

### PR 2: Review Packet Micro-Windows And Failure Tags

**Purpose:** Stop sending huge mixed windows to AI and make packets suitable for improvement diagnosis.

**Files:**
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/scripts/build_review_packets.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_ai_visual_review.py`

**Implementation Tasks:**

- [ ] **Step 1: Add micro-window tests for high-recall rejections**

Add a test where a high-recall rejection spans 118-5191.

Expected:
- No packet window is longer than 96 frames for `high_recall_rejection` unless it is an explicit long lost gap.
- Generated packets preserve source evidence with `parent_window`.
- Packet IDs include the narrowed window, not only the original giant window.

- [ ] **Step 2: Add dense-noise packet tests**

Add a test where `ai_review_triggers.json` contains a long `dense_noise_cluster`.

Expected:
- Long dense-noise ranges generate multiple diagnostic packets instead of being skipped.
- Each dense-noise packet is no longer than 96 frames unless an explicit safety limit requires dropping lower-priority packets.
- Dense-noise packets include `packet_purpose = "diagnose_noise"` and a likely `suspected_failure_tags` value such as `foot_confusion`, `shoe_confusion`, `sideline_confusion`, or `wall_background_drift`.
- Dense-noise packet sources preserve `parent_window` and the original trigger id.

- [ ] **Step 3: Implement high-recall and dense-noise splitting**

In `review_packets.py`:
- Add constants:
  - `MICRO_PACKET_MIN_FRAMES = 24`
  - `MICRO_PACKET_TARGET_FRAMES = 64`
  - `MICRO_PACKET_MAX_FRAMES = 96`
- Split high-recall rejection sources by:
  - large jump frames from `ball_audit.review_events`
  - lost gap starts/ends
  - postprocess actions
  - camera motion events when available
- Split `dense_noise_cluster` sources by:
  - local density peaks when evidence frames exist
  - postprocess actions that replaced nearby detections
  - centered target windows when no better anchor exists
- If no anchor exists, use centered target windows across the source span, capped by `max_packets`.
- Do not globally skip `dense_noise_cluster`; only drop lower-priority dense packets when the packet budget is exhausted after preserving higher-priority lost-gap and large-jump packets.

- [ ] **Step 4: Add packet evidence tags and stable linkage**

Each packet should include:
- `suspected_failure_tags`
- `root_cause_candidates`
- `packet_purpose`
- `packet_id`
- `source_packet_id` when derived from a parent packet/window
- `parent_window` when split from a larger source
- `frame_dimensions` when known from packet media generation

Example tags:
- `ball_lost`
- `foot_confusion`
- `shoe_confusion`
- `sideline_confusion`
- `wall_background_drift`
- `large_jump_after_reacquire`
- `camera_catchup_spike`
- `black_frames`
- `post_roll_too_short`

- [ ] **Step 5: Extend AI visual review schema without breaking existing reports**

Add optional fields to `ai_visual_review.py`:
- `failure_tags`
- `root_cause_module`
- `suggested_fixes`
- `likely_ball_region`
- `local_search_roi`
- `best_subclip`
- `tuning_direction`

The required legacy fields remain unchanged:
- `verdict`
- `confidence`
- `reason`
- `match_ball_visible`
- `marker_alignment`
- `highlight_publishable`
- `recommended_action`
- `visual_evidence`

Because `AI_VISUAL_REVIEW_RESPONSE_SCHEMA` currently uses `additionalProperties: False`, this step must update all of the following together:
- `AI_VISUAL_REVIEW_INSTRUCTIONS`
- `AI_VISUAL_REVIEW_RESPONSE_SCHEMA`
- `_validate_review_response`
- `_dry_run_review`
- `compact_ai_visual_review_summary` only if compact counts are affected

Allowed optional values:
- `failure_tags`: list of strings from the shared failure tag enum.
- `root_cause_module`: one of `detection`, `reacquisition`, `stitching`, `packetization`, `event_scoring`, `follow_cam`, `rendering`, `unknown`.
- `tuning_direction`: one of `tighten`, `loosen`, `split_packets`, `rerank_events`, `retrack_segment`, `none`.
- `likely_ball_region`: object with `frame`, `description`, and `confidence`, or `null`.
- `local_search_roi`: image-space object with `coordinate_space = "image"`, `frame`, `x`, `y`, `width`, `height`, and `confidence`, or `null`.
- `best_subclip`: object with `start_frame`, `end_frame`, and `reason`, or `null`.
- `source_packet_id`: packet id from `review_packets.json`.
- `visual_review_id`: stable id for this visual review item.

Clarification:
- `high_recall_rejection` is the packet source kind.
- `high_recall_rejected` is the packet/event type shown to the reviewer.

- [ ] **Step 6: Ensure packet images can drive localization**

For missing-ball and reacquire packets, the visual review prompt must attach existing packet media such as contact sheets or crop sheets and ask for localization only when the ball is visible. If the ball is not visible, it should return `likely_ball_region.description = "not visible"` and `local_search_roi = null`.

Tests must use a fake vision client response and verify:
- valid ROI fields are persisted in `ai_visual_review.json`.
- valid ROI fields include `source_packet_id`, `visual_review_id`, and `provenance.source = "ai_visual_review"`.
- non-image coordinate spaces are rejected.
- impossible negative frame or ROI coordinates are rejected.
- ROI values outside known frame dimensions are rejected or clipped with an explicit warning.
- missing-ball packets can return `not visible` without creating a fake ROI.

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_review_packets.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_visual_review.py -q
```

Real-output smoke validation:

```powershell
Copy-Item -Recurse python_backend\outputs\full_workflow_latest_review_20260622_060600 python_backend\outputs\scratch_ai_packets_pr2
.\.venv\Scripts\python.exe python_backend\scripts\build_review_packets.py python_backend\outputs\scratch_ai_packets_pr2 --max-packets 10
```

Expected:
- No normal high-recall rejection packet spans thousands of frames.
- No normal dense-noise diagnostic packet spans thousands of frames.
- Packet manifests include `parent_window` when they were split from a larger source.
- Packet manifests include stable packet ids that can be joined to `ai_visual_review.json`.

**Deliverables:**
- Review packets suitable for AI improvement.
- No huge `high_recall_rejected` packet in normal review flows.
- Dense-noise packets that AI can classify instead of one unusable giant noise range.
- Structured failure tags and optional localization ROI flowing into `review_packets.json` and `ai_visual_review.json`.

---

### PR 3: Ball Missing And Noise Improvement Suggestions

**Purpose:** Make AI actively suggest recovery/rerun actions for missing ball windows and noisy detections.

**Files:**
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/high_recall_reconcile.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/routes/ai.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/metrics.py`
- Modify generated API artifacts after OpenAPI export:
  - `lib/api-zod/src/generated/*`
  - `lib/api-client-react/src/generated/*`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_high_recall_windows.py`
- Test: `python_backend/tests/test_high_recall_reconcile.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`

**Implementation Tasks:**

- [ ] **Step 1: Add deterministic recovery action types**

AI improvements should support:
- `targeted_rerun`
- `tighten_noise_filter`
- `loosen_ball_recovery`
- `split_packet`
- `manual_review`
- `reject_noise`
- `adjust_highlight_window`
- `adjust_follow_cam`
- `tracking_rerun_before_follow_cam`

Schema naming rules:
- Use `config_patch` everywhere for suggested YAML merge patches.
- Use `rerun_scope` everywhere for frame ranges that could be rerun.
- Use `local_search_roi` everywhere for image-space search boxes.
- Use `evidence` as a list of human-readable strings; nested evidence objects live under `evidence_payload`.

- [ ] **Step 2: Consume visual localization in improvement generation**

`ai_improvement.py` should read `ai_visual_review.json` packet-level fields from PR 2 and merge them into candidate improvements.

Rules:
- If a missing-ball packet has `local_search_roi`, the corresponding improvement should include that ROI unless the model proposes a better valid ROI.
- If visual review says `not visible`, the improvement should not invent an ROI.
- The final report must show whether `local_search_roi` came from `ai_visual_review`, the model response, or dry-run heuristic under `provenance`.
- Join visual evidence by `source_packet_id` / `packet_id`, not only by approximate frame range.
- Preserve `visual_review_id`, `source_packet_id`, `frame_dimensions`, and ROI provenance in `evidence_payload`.

- [ ] **Step 3: Link improvements to rerun windows**

For `targeted_rerun`, emit:
- `rerun_scope.start_frame`
- `rerun_scope.end_frame`
- `rerun_reason`
- `config_patch`
- `approval_required = true`
- `provenance.source = "ai_improvement"`
- `provenance.improvement_id`
- `provenance.confidence`

For local search assistance, emit:
- `likely_ball_region.description`
- `local_search_roi.coordinate_space = "image"`
- `local_search_roi.frame`
- `local_search_roi.x`
- `local_search_roi.y`
- `local_search_roi.width`
- `local_search_roi.height`
- `local_search_roi.confidence`

- [ ] **Step 4: Add explicit opt-in approved-action artifact and API**

Do not let `high_recall_windows.py` automatically read `ai_improvement_report.json` just because the file exists.

Implement the concrete approval contract from this plan:
- Write `ai_improvement_approved_actions.json`.
- Add `POST /api/v1/ai/improve/{run_id}/approve`.
- The request accepts a list of `improvement_id` values plus optional operator overrides for `rerun_scope`, `local_search_roi`, `config_patch`, `suggested_window`, `clip_action`, and `follow_cam_rerender_plan`.
- The service validates each selected improvement against `ai_improvement_report.json`.
- The service writes the approval artifact and refreshes run artifacts/metrics.
- Config-producing approvals write a derived patch artifact such as `ai_improvement_config_patch.yaml` or `ai_improvement_config_patch.json`; they do not overwrite the active config.

Rules:
- Only use AI windows with `recommended_action = "targeted_rerun"`.
- Only use AI windows after explicit operator opt-in.
- Never exceed existing high-recall window budget.
- Preserve deterministic sources as higher priority than low-confidence AI suggestions.
- Record `approval_source`, `approved_at`, and `approved_by` when the API path is used.
- Default behavior remains unchanged when no approved AI improvement plan is supplied.
- Approval validation covers all action families even when only `targeted_rerun` feeds high-recall planning in this PR.

- [ ] **Step 5: Consume approved actions in high-recall planning**

Add an explicit argument to high-recall planning code: `approved_actions_path: Path | None`. Do not auto-read the artifact. The API/CLI can pass `output_dir / "ai_improvement_approved_actions.json"` only after approval.

Rules:
- Only `approved_action = "targeted_rerun"` creates a high-recall window.
- Approved windows keep deterministic windows as higher priority.
- Approved windows preserve `approval_id`, `improvement_id`, and `local_search_roi` in source metadata.

- [ ] **Step 6: Add reconcile evidence**

When a high-recall run accepts or rejects a segment that came from AI improvement, write:
- `source = "ai_improvement"`
- `improvement_id`
- `approval_source`
- `accepted`
- `reason`
- `changed_frame_count`

- [ ] **Step 7: Export OpenAPI and regenerate generated clients**

Because this PR adds the approval API and new approval schemas, run OpenAPI export and regenerate the generated API clients in the same PR.

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_reconcile.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_ai_improvement_approve_writes_approved_actions -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_export_openapi.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py --check
```

Specific approval-flow coverage:
- approving a targeted-rerun improvement writes `ai_improvement_approved_actions.json`.
- approving highlight actions validates `candidate_id`, `suggested_window`, and `clip_action`.
- approving follow-cam actions validates config patch paths and/or `follow_cam_rerender_plan`.
- approving noise/config actions writes a derived patch artifact without changing the active config.
- approving a non-rerun action for high-recall planning does not create a window.
- API overrides are validated and cannot introduce invalid `config_patch` paths.
- ROI from `ai_visual_review.json` is merged into `ai_improvement_report.json` by `source_packet_id`; `not visible` does not create a fake ROI.
- no approved artifact means high-recall planning output is unchanged.

**Deliverables:**
- AI can propose localized ball recovery reruns.
- Noise-heavy windows can be turned into tighten-filter recommendations.
- High-recall planning can consume `ai_improvement_approved_actions.json` only after explicit operator opt-in, in a bounded and auditable way.
- `POST /api/v1/ai/improve/{run_id}/approve`.
- Generated OpenAPI/client updates for the approval endpoint.
- Derived config patch artifact for approved config suggestions.
- Reconcile provenance for approved AI windows.
- No AI suggestion can silently modify `ball_track.csv` or `ball_track.cleaned.csv`.

---

### PR 4: Camera Motion Improvement Integration

**Purpose:** Use camera motion audit results to recommend follow-cam and tracking fixes, not only warn after rendering.

**Files:**
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/follow_cam.py`
- Modify: `python_backend/football_tracking/camera_motion_audit.py`
- Modify: `python_backend/football_tracking/metrics.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_camera_motion_audit.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_api_service.py`

**Implementation Tasks:**

- [ ] **Step 1: Add packet-level camera overlap**

In AI improvement context, for each camera motion event, include:
- track status counts in a +/- 12 frame window
- whether the event overlaps Lost/Predicted
- whether it overlaps a review packet or event candidate

- [ ] **Step 2: Add follow-cam action suggestions**

AI can emit:
- `adjust_follow_cam`
- `tracking_rerun_before_follow_cam`
- `human_review_camera_motion`

Example config patch fields:
- `follow_cam.glide_pan_smoothing`
- `follow_cam.catch_up_pan_smoothing`
- `follow_cam.glide_max_pan_per_frame_x`
- `follow_cam.glide_max_pan_per_frame_y`
- `follow_cam.catch_up_max_pan_per_frame_x`
- `follow_cam.catch_up_max_pan_per_frame_y`
- `follow_cam.dead_zone_ratio_x`
- `follow_cam.dead_zone_ratio_y`
- `follow_cam.predicted_pan_decay`
- `follow_cam.zoom_out_confirm_frames`
- `follow_cam.zoom_hold_frames_after_change`

Do not introduce invented config names. If an existing validator still accepts `follow_cam.dead_zone_ratio`, replace it with `dead_zone_ratio_x` and `dead_zone_ratio_y` in this PR.

- [ ] **Step 3: Add follow-cam rerender planning**

Approved `adjust_follow_cam` suggestions should write a lightweight `follow_cam_rerender_plan.json` instead of silently rerendering.

Plan fields:
- `source = "ai_improvement_approved_action"`
- `approval_id`
- `improvement_id`
- `recommended_config_patch`
- `requires_tracking_rerun = false`
- `reason`

Approved `tracking_rerun_before_follow_cam` suggestions should set `requires_tracking_rerun = true` and should not create a direct follow-cam rerender.

- [ ] **Step 4: Add camera release gate summary**

Metrics should expose:
- `camera_motion.status`
- `camera_motion.review_event_count`
- `camera_motion.events_overlapping_lost_or_predicted`
- `camera_motion.requires_tracking_rerun`

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_camera_motion_audit.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_follow_cam.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_ai_improvement.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_ai_follow_cam_approval_writes_rerender_plan -q
```

Additional fake-client coverage:
- a camera motion event overlapping Lost/Predicted status produces `tracking_rerun_before_follow_cam`.
- a camera motion event with stable Detected tracking produces `adjust_follow_cam`.
- invalid follow-cam config patch names are stripped with warnings.
- approving `adjust_follow_cam` writes `follow_cam_rerender_plan.json`.
- approving `tracking_rerun_before_follow_cam` records that tracking must be rerun before follow-cam rerender.

Real-output smoke validation:

```powershell
Copy-Item -Recurse python_backend\outputs\full_workflow_latest_review_20260622_060600 python_backend\outputs\scratch_ai_camera_pr4
.\.venv\Scripts\python.exe python_backend\scripts\run_ai_improvement.py python_backend\outputs\scratch_ai_camera_pr4 --dry-run
```

Expected:
- Camera motion events overlapping Lost/Predicted frames appear as `adjust_follow_cam` or `tracking_rerun_before_follow_cam` suggestions.
- The smoke run does not modify track CSV files.

**Deliverables:**
- `ai_improvement_report.json` contains camera-specific root cause and action suggestions.
- `follow_cam_rerender_plan.json` for approved follow-cam-only adjustments.
- Follow-cam release quality can be judged from metrics without manually opening every file.

---

### PR 5: Highlight Core/Render Windows And AI Clip Boundary Suggestions

**Purpose:** Ensure highlight clips include enough post-shot frames and let AI recommend final clip boundaries.

**Files:**
- Modify: `python_backend/football_tracking/events.py`
- Modify: `python_backend/football_tracking/review_packets.py`
- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify generated API artifacts after OpenAPI export:
  - `lib/api-zod/src/generated/*`
  - `lib/api-client-react/src/generated/*`
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_review_packets.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_high_recall_windows.py`

**Implementation Tasks:**

- [ ] **Step 1: Add event candidate window tests**

Expected event candidate shape:

```json
{
  "core_window": {"start_frame": 20, "end_frame": 24},
  "render_window": {"start_frame": 5, "end_frame": 114},
  "buffer_policy": {
    "pre_roll_frames": 15,
    "post_roll_frames": 90,
    "reason": "shot_candidate_default"
  }
}
```

- [ ] **Step 2: Implement dual windows**

In `events.py`:
- Preserve `start_frame` and `end_frame` as the core event window for compatibility.
- Add `core_window`.
- Increase default shot post-roll to 90 frames.
- Increase default goal post-roll to 120 frames.
- Keep pre-roll at 15 frames unless later tuning suggests otherwise.

- [ ] **Step 3: Make highlight render prefer candidate render_window**

In `ApiService._resolve_highlight_selection`:
- First fix request semantics so the service can tell default behavior from explicit override:
  - Change `HighlightRenderRequest.pre_roll_frames` and `post_roll_frames` to optional values, or add `roll_policy` with values `candidate_render_window`, `manual_roll`, and `legacy_default`.
  - Recommended: add `roll_policy`, defaulting to `candidate_render_window` for candidate-based highlights and `manual_roll` for explicit frame windows.
- When `candidate_id` is provided and `roll_policy = "candidate_render_window"`, use `candidate.render_window`.
- If request supplies explicit pre/post roll or `roll_policy = "manual_roll"`, use the request override.
- Store both the original candidate core window and chosen render window in `highlight_report.json`.
- Export OpenAPI and regenerate clients in the same PR because this PR changes request and event candidate schemas.

Integration tests must cover:
- candidate render with omitted roll values uses `candidate.render_window`.
- candidate render with `roll_policy = "manual_roll"` uses explicit pre/post roll.
- explicit `start_frame`/`end_frame` render remains manual and does not require a candidate.
- `highlight_report.json` records `core_window`, `render_window`, `roll_policy`, and whether the window came from candidate default or operator override.
- The old service behavior of taking a candidate core window and applying the legacy 15/30 roll is rejected by tests; candidate-based highlights must default to `candidate.render_window`.

- [ ] **Step 4: Add AI highlight adjustment output**

In `ai_improvement.py`, support `highlight_adjustments`:
- `candidate_id`
- `current_window`
- `suggested_window`
- `reason`
- `confidence`
- `clip_action`: `extend_tail`, `trim_head`, `trim_tail`, `split`, `keep`

- [ ] **Step 5: Allow approved AI highlight windows to render**

Use the same `ai_improvement_approved_actions.json` contract from PR 3 for highlight actions.

Rules:
- `approved_action = "render_suggested_highlight"` or `approved_action = "adjust_highlight_window"` can create a highlight render request.
- The renderer uses `suggested_window` as an explicit frame window and records `source = "ai_improvement_approved_action"` in `highlight_report.json`.
- The approval item must include `candidate_id`, `suggested_window`, `clip_action`, `approval_id`, and `improvement_id`.
- The UI/API must still allow manual edits before render.

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_events.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_review_packets.py -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_create_highlight_render_creates_child_task_from_event_candidate -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_create_highlight_render_uses_candidate_render_window_by_default -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_api_service.py::ApiServiceTests::test_create_highlight_render_uses_ai_approved_window -q
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_high_recall_windows.py -q
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py
```

Real-output smoke validation:

```powershell
Copy-Item -Recurse python_backend\outputs\full_workflow_latest_review_20260622_060600 python_backend\outputs\scratch_ai_highlights_pr5
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_events.py -q
```

Expected:
- At least one shot/goal candidate has `render_window.end_frame` greater than `core_window.end_frame + 30`.
- Highlight selection by `candidate_id` uses candidate `render_window` unless `roll_policy = "manual_roll"`.
- Approved AI highlight actions can be rendered with the suggested frame window.

**Deliverables:**
- `event_candidates.json` has `core_window`, longer `render_window`, and `buffer_policy`.
- Highlight renders include post-shot frames by default.
- AI can recommend clip start/end adjustments, and an operator can render an approved suggested window.

---

### PR 6: UI, Docs, And Operator Workflow

**Purpose:** Surface the AI improvement loop in the app and document how to use it.

**Files:**
- Modify: `artifacts/web/src/pages/ai-analysis.tsx`
- Modify: `artifacts/web/src/lib/api.ts`
- Modify: `artifacts/web/src/lib/types.ts`
- Modify: `README.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Verify generated clients from PR 1 and PR 5 are current before wiring UI:
  - `lib/api-zod/src/generated/*`
  - `lib/api-client-react/src/generated/*`

**Implementation Tasks:**

- [ ] **Step 1: Add API client support**

Add an `aiImprove` call to the frontend API helper.

- [ ] **Step 2: Add AI Improvement panel**

In AI Analysis page, show:
- primary issue
- improvements grouped by area
- targeted rerun windows
- config patch suggestions
- highlight window suggestions
- camera motion suggestions

- [ ] **Step 3: Add operator actions**

Actions:
- copy suggested objective into AI recommend box
- approve selected targeted rerun windows into `ai_improvement_approved_actions.json`
- save/copy config patch when provided, without applying it silently
- show frame windows for manual rerun
- edit and render a suggested highlight window
- show camera-motion suggestions with whether the action is a tracking rerun or follow-cam rerender

- [ ] **Step 4: Update docs**

Docs must explain:
- AI Review means publishability/quality review.
- AI Improvement means diagnosis and next-run suggestions.
- Mini model is suitable for first-pass classification.
- Higher model is recommended for improvement diagnosis.

**Tests:**

Run:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\export_openapi.py
pnpm exec tsc --noEmit
.\.venv\Scripts\python.exe -m pytest python_backend/tests/test_export_openapi.py -q
```

**Deliverables:**
- UI exposes AI improvement reports.
- Docs distinguish review vs improvement.
- API clients include the new endpoint and schemas.

---

## End-To-End Validation

After all PRs merge, run on the known real video output flow:

```powershell
$env:PYTHONPATH='python_backend'
$beforeTrack = Get-FileHash python_backend\outputs\full_workflow_latest_review_20260622_060600\ball_track.csv
$beforeCleaned = Get-FileHash python_backend\outputs\full_workflow_latest_review_20260622_060600\ball_track.cleaned.csv
.\.venv\Scripts\python.exe python_backend\scripts\build_review_packets.py python_backend\outputs\full_workflow_latest_review_20260622_060600 --input-video python_backend\data\raw5760x144020fps.mp4 --follow-cam-video python_backend\outputs\full_workflow_latest_review_20260622_060600\follow_cam.latest_review.mp4 --max-packets 10
.\.venv\Scripts\python.exe python_backend\scripts\run_ai_visual_review.py python_backend\outputs\full_workflow_latest_review_20260622_060600 --max-packets 10
.\.venv\Scripts\python.exe python_backend\scripts\run_ai_improvement.py python_backend\outputs\full_workflow_latest_review_20260622_060600
$afterTrack = Get-FileHash python_backend\outputs\full_workflow_latest_review_20260622_060600\ball_track.csv
$afterCleaned = Get-FileHash python_backend\outputs\full_workflow_latest_review_20260622_060600\ball_track.cleaned.csv
if ($beforeTrack.Hash -ne $afterTrack.Hash -or $beforeCleaned.Hash -ne $afterCleaned.Hash) { throw "AI improvement flow modified track CSV files" }
```

Use the provider-configured model by default. Override the model only when the environment supports the requested model and the run is meant to compare model quality.

Expected final artifacts:
- `review_packets.json`
- `ai_visual_review.json`
- `ai_improvement_report.json`
- `ai_improvement_approved_actions.json` when the operator approves suggestions
- `camera_motion_audit.json`
- `event_candidates.json`
- `metrics_report.json` with `ai_improvement`

Expected quality outcomes:
- No normal `high_recall_rejected` packet covers thousands of frames.
- No normal dense-noise diagnostic packet covers thousands of frames.
- `ai_visual_review.json` can carry packet-level failure tags and image-space ROI evidence when the ball is visible.
- `ai_improvement_report.json` lists actionable suggestions for missing balls, noise, camera motion, and highlight boundaries.
- Approved actions can create targeted high-recall windows or render AI-suggested highlight windows with provenance.
- Approved follow-cam-only actions can create `follow_cam_rerender_plan.json`; tracking-dependent camera actions require a rerun first.
- At least one highlight candidate has a render window that extends materially beyond the core shot/goal window.
- Camera motion warnings overlapping Lost/Predicted frames are reflected in AI improvement suggestions.
- Track CSV checksums are unchanged by packet building, visual review, and AI improvement report generation.

## Agent Review Checklist

Ask a separate reviewer agent to verify:
- The plan clearly distinguishes AI review from AI improvement.
- Each user-visible need maps to at least one PR task.
- Missing-ball, noise, camera motion, and highlight buffer cases have separate deliverables.
- The plan does not let AI silently mutate tracks/configs without operator approval.
- Tests cover missing artifacts, provider failures, schema compatibility, packet splitting, camera overlap, and highlight render windows.
- PR boundaries are small enough for managed PR execution.

## Independent Agent Review Incorporated

The plan was reviewed by a separate agent before finalization. The review found several important execution gaps, all addressed in this revision:
- The current repo state is now explicit: PR 1 has already landed as GitHub PR #28, and managed PR execution should resume with PR 2.
- Missing-ball localization now has a real vision path: PR 2 attaches packet media and allows `ai_visual_review.json` to emit `likely_ball_region` / `local_search_roi`; PR 3 consumes that visual evidence in `ai_improvement_report.json`.
- Dense-noise ranges are now part of PR 2 micro-window packetization instead of being skipped as one unusable giant range.
- Packet-to-visual-review-to-improvement linkage now requires stable ids, ROI provenance, and frame-dimension validation.
- The approval/apply loop now has one concrete contract: `ai_improvement_approved_actions.json` and `POST /api/v1/ai/improve/{run_id}/approve`, with action-specific fields for rerun, highlight, follow-cam, and config-patch approvals.
- `config_patch` validation now explicitly strips unknown paths, dotted flat keys, invalid values, and unsupported roots.
- Follow-cam suggested patch names now use current config fields such as `glide_pan_smoothing`, `catch_up_pan_smoothing`, `dead_zone_ratio_x`, and `dead_zone_ratio_y`.
- Highlight work now tests actual render selection behavior, including candidate `render_window`, manual roll override, and approved AI suggested windows; the old 15/30 default cannot silently override a candidate render window.
- The UI plan now includes approve/render actions, not only passive display.
- End-to-end validation now hashes track CSV files before and after AI report generation to prove AI does not silently mutate tracking outputs.

## Execution Notes For Managed PR

- Start every PR from latest `origin/main`.
- Use one fresh branch per PR.
- Keep generated output videos and `.env` out of commits.
- Use fake AI clients in tests; do not require a real OpenAI API key in CI.
- After each PR, run focused tests plus any touched API/OpenAPI/frontend validation.
- Before merge, run a separate spec-compliance review and code-quality review.
