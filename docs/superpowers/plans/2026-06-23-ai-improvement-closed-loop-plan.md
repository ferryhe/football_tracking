# AI Improvement Closed Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn current AI review artifacts into a controlled AI improvement loop that can propose, execute, compare, and promote better tracking, follow-cam, and highlight outputs.

**Architecture:** Keep AI as a bounded candidate generator, not an automatic mutator of final output. Every AI action produces a candidate artifact, a comparison report, a candidate registry entry, and a quality-gate decision before anything is promoted.

**Tech Stack:** Python backend, FastAPI service layer, existing OpenAI-compatible provider wrapper, JSON artifacts, pytest, existing temporal chunk/high-recall/follow-cam/highlight modules.

---

## Requirement Summary

The user-facing requirement is AI improvement, not AI review only. The system should use AI in four concrete places:

1. Missing ball recovery: when the ball disappears, AI helps localize the likely ball area or says the ball is not visible.
2. Noise suppression: when detections include extra balls, shoes, heads, sideline objects, or background artifacts, AI classifies likely false positives and proposes bounded cleanup.
3. Follow-cam smoothing: when the final follow-cam camera path jumps, accelerates, or zooms abruptly, AI proposes a rerender or tracking-before-camera fix.
4. Highlight window tuning: default highlight clips should use a safe pre/post buffer, then AI may adjust the window so the final clip includes the shot/goal aftermath instead of cutting too early.

One cross-cutting rule applies to all four: AI suggestions are only useful if they become comparable candidates. A suggestion that cannot be traced to evidence, executed in a bounded way, and compared against the baseline should remain a review note, not an accepted improvement.

## Current Landing Map

Already available or mostly available:

- `python_backend/football_tracking/ball_audit.py`: ball trajectory audit and lost/noise events.
- `python_backend/football_tracking/ai_review_triggers.py`: trigger windows from ball audit.
- `python_backend/football_tracking/review_packets.py`: visual packet generation for AI or human review.
- `python_backend/football_tracking/ai_visual_review.py`: vision evidence source for ROI localization, false-positive classification, highlight review, and not-visible proof.
- `python_backend/football_tracking/ai_improvement.py`: run-level AI improvement report, approval file, config patch, ROI, follow-cam rerender plan, highlight adjustments.
- `python_backend/football_tracking/high_recall_windows.py`: bounded recovery windows for approved actions.
- `python_backend/football_tracking/missing_ball_recovery_comparison.py`: comparison contract for missing-ball recovery candidates.
- `python_backend/football_tracking/camera_motion_audit.py`: final camera-path audit for abrupt pan, acceleration, and zoom.
- `python_backend/football_tracking/ai_candidate_comparison.py`: shared candidate comparison payload and status derivation.
- `python_backend/football_tracking/ai_candidate_registry.py`: candidate index for missing-ball, noise, follow-cam, and highlight candidate reports.
- `python_backend/football_tracking/ai_improvement_quality_gate.py`: quality gate over audits, approvals, comparisons, registry, and manifests.
- `python_backend/scripts/run_stable_ai_improvement_workflow.py`: staged workflow wrapper.

Gaps to close:

- Missing-ball candidates can be planned and compared, but the workflow still needs a smoother explicit candidate execution/promotion path.
- Noise has diagnosis and tags, but lacks a candidate cleanup run plus comparison report.
- Follow-cam has camera audit and approval planning, but lacks a rerender candidate runner plus camera comparison report.
- Highlight rendering exists, and AI can suggest highlight adjustments, but default post-roll protection and AI-adjusted candidate comparison need to be first-class.
- The stable workflow stages are still partly "planned"; it should actually execute selected candidate actions in bounded form.
- Documentation should explain the difference between AI review, AI improvement, explicit approval, candidate promotion, and final artifact selection.

## Cross-Cutting Invariants

- Explicit approval is permission to try a candidate; it is not proof that the candidate improved output.
- A candidate is not an accepted improvement until it has an artifact, a comparison report, a registry entry, and a quality-gate result.
- In real/improve mode, a selected approval without a comparison report must produce `pending`, `warn`, `unavailable`, or `fail`; it must not become `pass`.
- Final promotion is blocked unless the validated comparison status is `pass`, or a `warn` candidate has explicit human confirmation recorded against the same `candidate_id`.
- AI localization that claims to find a ball must cite visual evidence from review packet media or `ai_visual_review`; text-only suggestions remain review-only.
- Spatial full-video split/SAHI is not a default improvement mechanism. It can improve GPU utilization, but it must be bounded by approval/ROI or fail candidate comparison as a noise-risky broad rerun.

## Managed PR Sequence

### PR0: Finish Candidate Registry Baseline

**Purpose:** Finish and merge the current candidate registry branch before adding more candidate types.

**Files:**

- Existing: `python_backend/football_tracking/ai_candidate_registry.py`
- Existing: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Existing: `python_backend/tests/test_ai_candidate_registry.py`
- Existing: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Build:**

- [ ] Recheck PR CI and review comments.
- [ ] Fix any valid Copilot or reviewer comments.
- [ ] Merge the branch only after checks are green.
- [ ] Delete local and remote merged branches when explicitly authorized by the managed PR program.
- [ ] Pull latest `origin/main` before PR1.

**Test:**

- [ ] `pytest python_backend/tests/test_ai_candidate_registry.py -q`
- [ ] `pytest python_backend/tests/test_ai_candidate_comparison.py python_backend/tests/test_final_artifact_manifest.py python_backend/tests/test_ai_improvement_quality_gate.py -q`
- [ ] GitHub Python and Node checks.

**Deliver:**

- Candidate registry accepted on `main`.
- Quality gate can read registry references and actual comparison reports.

### PR1: AI Improvement Contract And Prompt Hardening

**Purpose:** Make AI output explicitly improvement-oriented and model-routed, so it returns actionable candidates rather than generic review comments.

**Files:**

- Modify: `python_backend/football_tracking/ai_contracts.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/ai_provider.py`
- Modify: `python_backend/scripts/run_ai_improvement.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_config_and_provider.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Add an explicit `candidate_intent` field in AI improvement context, such as `review_only`, `suggest_candidates`, or `prepare_approved_candidates`; keep it distinct from workflow `--mode dry-run/artifact-only/real`.
- [ ] Tighten `_instructions()` and `_prompt()` so the model must state whether each suggestion is executable, bounded, and comparable.
- [ ] Preserve the full current `AIRecommendedAction` enum, including `noise_filter_adjustment`, `manual_review`, `reject_noise`, and `human_review_camera_motion`; add validation fields without breaking existing action values.
- [ ] Treat values such as `extra_ball`, `player_head`, and `advertising_board` as `false_positive_class`, not `failure_tags`, unless they are added to the shared failure-tag vocabulary in the same PR.
- [ ] Require every executable suggestion to include `problem_type`, `candidate_id` seed data, evidence ids, bounded frame window, expected artifact, and comparison criteria.
- [ ] Add model routing guidance: run-level improvement and hard visual localization should prefer a stronger configured model; small models are acceptable only for dry-run or low-risk tagging.
- [ ] Record selected model, configured source, and whether the provider was in real or dry-run mode in `ai_improvement_report.json`.
- [ ] Require executable `localize_ball_roi` to cite wide/crop packet evidence or `ai_visual_review` evidence with usable media; no media, corrupt media, dry-run, or unknown vision capability must downgrade it to review-only.

**Test:**

- [ ] Mock provider returns a generic suggestion with no candidate fields; validation rejects or downgrades it to non-executable.
- [ ] Mock provider returns missing-ball ROI with no packet/visual provenance; validation fails.
- [ ] Mock provider returns follow-cam action with neither config patch nor rerender plan; validation fails.
- [ ] Mock provider returns highlight adjustment that drops the event core window; validation fails.
- [ ] Existing action values from `ai_contracts.py` remain accepted after the schema change.
- [ ] Mock provider returns ROI with no vision-capable evidence; validation marks it review-only or rejects executable status.
- [ ] CLI passes `--model` through and records it.
- [ ] Stable workflow records real vs dry-run provider mode.

**Deliver:**

- `ai_improvement_report.json` clearly separates review notes from executable AI improvement candidates.
- Prompt wording updated so the model is asked to improve artifacts, not merely audit them.

### PR2: Missing-Ball AI Candidate Execution

**Purpose:** When a long lost gap appears, AI can localize a bounded ROI and produce a candidate rerun that is compared before promotion.

**Files:**

- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/chunk_runner.py`
- Modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_high_recall_windows.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Add a stable candidate output directory convention, such as `ai_candidates/missing_ball/<candidate_id>/`.
- [ ] Use the existing approved child-run execution path in `python_backend/football_tracking/api/service.py` for missing-ball candidates; PR6 later only orchestrates this path from the stable workflow.
- [ ] Execute only explicitly approved `localize_ball_roi` or `targeted_rerun` actions.
- [ ] Clamp each window to source video bounds and enforce max total frame budget.
- [ ] Use temporal frame-window parallelism for speed, not spatial full-video SAHI broad slicing.
- [ ] Produce `missing_ball_recovery_comparison.json` in the parent output directory or final candidate registry path.
- [ ] Register the candidate in `ai_candidate_registry.json`.
- [ ] Refuse promotion when the candidate adds short noisy islands or fails to cover the approved lost-gap window.
- [ ] Add first-class handling for the fixture `python_backend/tests/fixtures/right_bottom_gap_2049_2544.json`: lost gap `2049-2544`, key frame `2079`, expected region `right_bottom`, required coverage labels `start`, `middle`, `end`, and `tail`.

**Test:**

- [ ] Approved ROI action creates a bounded high-recall child candidate.
- [ ] Action without explicit approval is skipped even if an approval file exists in the output directory.
- [ ] Candidate outside max frame budget fails safely before running expensive work.
- [ ] Candidate comparison passes only when lost frames are reduced by a sustained span and no short noisy island is introduced.
- [ ] Candidate comparison includes packet coverage and approval provenance.
- [ ] Candidate comparison uses `require_packet_coverage=True` for the 2049-2544 right-bottom fixture.
- [ ] Review packets for the 2049-2544 gap either cover `start`, `middle`, `end`, and `tail`, or write explicit uncovered ranges.
- [ ] Recovery must cover the full approved lost-gap window, not just key frame 2079 or a short nearby span.
- [ ] Quality gate fails or warns when a long lost gap has no AI ROI, no not-visible evidence, and no candidate comparison.
- [ ] In real/improve mode, selected approval without `missing_ball_recovery_comparison.json` cannot pass quality gate or final promotion.

**Deliver:**

- A missing-ball candidate can be executed, compared, registered, and quality-gated.
- The known right-bottom gap `2049-2544`, including key frame `2079`, has a repeatable mechanism that checks packet coverage and full-window recovery.

### PR3: Noise AI Candidate Cleanup

**Purpose:** Make AI reduce false detections through bounded cleanup candidates instead of relying on broad SAHI slicing that increases noise.

**Files:**

- Create: `python_backend/football_tracking/noise_candidate_comparison.py`
- Modify: `python_backend/football_tracking/ai_review_triggers.py`
- Modify: `python_backend/football_tracking/ai_visual_review.py`
- Modify: `python_backend/football_tracking/postprocess.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_noise_candidate_comparison.py`
- Test: `python_backend/tests/test_ai_review_triggers.py`
- Test: `python_backend/tests/test_ai_visual_review.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Promote dense-noise and candidate-ambiguity windows into visual review packets.
- [ ] Let AI classify bounded noise as `extra_ball`, `shoe_confusion`, `foot_confusion`, `player_head`, `advertising_board`, `sideline_confusion`, `wall_background_drift`, or `unknown_false_positive`.
- [ ] Store that bounded classification as `false_positive_class`; reserve `failure_tags` for the shared `AIFailureTag` vocabulary.
- [ ] Add a cleanup candidate runner that operates on copied track/candidate artifacts, not the baseline files.
- [ ] Support conservative cleanup actions: remove short islands, downweight repeated false-positive zones, or tighten selector/postprocess thresholds within the bounded window.
- [ ] Add provenance checks so full-video spatial split or full-video SAHI candidates without bounded approval fail as broad-noise-risk candidates.
- [ ] Produce `noise_candidate_comparison.json`.
- [ ] Register noise candidates in `ai_candidate_registry.json`.

**Test:**

- [ ] Dense noise trigger creates review packets.
- [ ] AI noise classification without bounded frame evidence remains review-only.
- [ ] Cleanup candidate removes short false-positive islands.
- [ ] Cleanup candidate fails if it reduces sustained valid ball coverage.
- [ ] Cleanup candidate fails if it increases lost-frame count above tolerance.
- [ ] Candidate with provenance `full_video_spatial_split` or unbounded full-video SAHI fails unless tied to bounded approval/ROI and preserved recall.
- [ ] Candidate registry and quality gate count noise candidate status correctly.

**Deliver:**

- A noise cleanup candidate that can be accepted only when it reduces false-positive islands without damaging real tracking continuity.
- A documented answer to spatial splitting: spatial SAHI can stay for tiny-ball recall, but AI improvement should prefer temporal parallel windows and bounded ROI reruns to avoid multiplying noise.

### PR4: Follow-Cam AI Rerender Candidate

**Purpose:** Use camera-motion audit events to propose and test a smoother follow-cam rerender without changing the underlying ball track.

**Files:**

- Create: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_follow_cam_candidate_comparison.py`
- Test: `python_backend/tests/test_follow_cam.py`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Convert `camera_motion_audit.json` events into AI improvement context with camera-path evidence.
- [ ] AI must choose between `adjust_follow_cam` and `tracking_rerun_before_follow_cam`.
- [ ] If the camera event overlaps Lost or Predicted tracking context, AI must choose `tracking_rerun_before_follow_cam`; direct follow-cam rerender is allowed only when track context is stable enough.
- [ ] Execute approved `adjust_follow_cam` by rerendering a candidate follow-cam video into `ai_candidates/follow_cam/<candidate_id>/`.
- [ ] Do not mutate `ball_track.csv` or `ball_track.cleaned.csv` during follow-cam rerender.
- [ ] Produce candidate `camera_path.csv`, `follow_cam_report.json`, and `camera_motion_audit.json`.
- [ ] Compare baseline vs candidate camera audit in `follow_cam_candidate_comparison.json`.
- [ ] Register follow-cam candidates in `ai_candidate_registry.json`.
- [ ] Fail the candidate if smoothing improves camera metrics by hiding the ball or excessively zooming out.

**Test:**

- [ ] Camera motion spike is included in AI improvement context.
- [ ] Follow-cam action with no rerender plan/config patch is rejected.
- [ ] Approved follow-cam rerender writes candidate camera artifacts.
- [ ] Candidate comparison passes when pan/accel/zoom metrics improve within tolerance.
- [ ] Candidate comparison fails when camera metrics regress.
- [ ] Candidate comparison fails when candidate output loses too much ball-centered coverage.
- [ ] Candidate comparison quantifies max/p95 pan step, acceleration, zoom jump, ball-centered coverage, zoom-out ratio, and artifact availability.
- [ ] Camera event over Lost/Predicted context produces a tracking-rerun-before-follow-cam recommendation, not a direct rerender candidate.

**Deliver:**

- AI can propose and validate a less shaky follow-cam output.
- Camera motion audit becomes an improvement trigger, not only a warning report.

### PR5: Highlight Window AI Candidate

**Purpose:** Harden existing highlight buffers so clips include the event and enough aftermath, then let AI adjust the boundaries with comparison and explicit selection.

**Files:**

- Create: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/events.py`
- Modify: `python_backend/football_tracking/highlights.py`
- Modify: `python_backend/football_tracking/accepted_highlights.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_events.py`
- Test: `python_backend/tests/test_highlights.py`
- Test: `python_backend/tests/test_accepted_highlights.py`
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`

**Build:**

- [ ] Preserve and harden existing default highlight buffers with explicit `pre_roll_frames`, `post_roll_frames`, and `minimum_tail_after_core_frames`.
- [ ] Store `core_window` separately from `render_window`.
- [ ] Clamp render windows to source video length while preserving the event core.
- [ ] Ensure source-end clamp happens before rendering, so the renderer never receives an out-of-range window.
- [ ] AI may suggest `best_subclip` or `suggested_window`, but it cannot cut off the core event or required tail unless the source video ends.
- [ ] Execute approved `adjust_highlight_window` or `render_suggested_highlight` as a candidate highlight render.
- [ ] Produce `highlight_candidate_comparison.json`.
- [ ] Register highlight candidates in `ai_candidate_registry.json`.

**Test:**

- [ ] Default highlight window includes full core and post-roll tail.
- [ ] AI suggested window that cuts off the event core is rejected.
- [ ] AI suggested window that cuts off required tail is rejected unless source end clamps it.
- [ ] Candidate render creates `highlight.mp4` and `highlight_report.json` in a child output directory.
- [ ] Highlight comparison checks source candidate id, window bounds, core coverage, tail coverage, and render artifact availability.
- [ ] Multiple highlight candidates each get independent comparison and registry entries.
- [ ] Accepted highlight copier only accepts visually accepted/publishable clips with usable comparison evidence.

**Deliver:**

- Highlight output is no longer just a fixed buffer.
- AI can adjust clip boundaries while tests protect against the repeated failure mode where the final shot aftermath is missing.

### PR6: Stable Workflow Executes Candidate Actions

**Purpose:** Turn `run_stable_ai_improvement_workflow.py` into a real operator workflow that orchestrates selected candidate actions already implemented by PR2-PR5.

**Files:**

- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `python_backend/football_tracking/final_artifact_manifest.py`
- Modify: `python_backend/football_tracking/ai_improvement_quality_gate.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Test: `python_backend/tests/test_stable_ai_improvement_workflow.py`
- Test: `python_backend/tests/test_final_artifact_manifest.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_api_service.py`

**Build:**

- [ ] Add workflow flags that separate `--review`, `--improve`, `--execute-approved`, and `--promote-pass-candidates`.
- [ ] Keep default behavior safe: no approval file executes by presence.
- [ ] Execute only actions selected by `--approval-ids` or `--approved-action-id`.
- [ ] Run candidate comparison after each executed action.
- [ ] Do not duplicate PR2's missing-ball child-run implementation; call the candidate execution interfaces added by PR2-PR5.
- [ ] Write or update `ai_candidate_registry.json`.
- [ ] Write or update `final_ai_improvement_artifact_manifest.json`.
- [ ] Run quality gate after candidate execution.
- [ ] Return nonzero in real mode when required comparisons fail.

**Test:**

- [ ] Dry-run records planned stages without provider or video mutation.
- [ ] Artifact-only mode reports missing candidate artifacts as unavailable, not pass.
- [ ] Real mode fails when an approved action cannot produce a comparison.
- [ ] Real/improve mode does not pass quality gate merely because `approved_actions` was selected.
- [ ] Final manifest refuses promotion when a selected approval has no comparison report.
- [ ] Explicit selected approval executes only that approval.
- [ ] Multiple selected approvals execute independently and do not share candidate ids.
- [ ] Final manifest records accepted, rejected, and pending candidates.

**Deliver:**

- One repeatable command can run AI improvement, execute selected candidates, compare results, and produce a promotion manifest.

### PR7: API And UI Surfacing

**Purpose:** Make the improved workflow visible and controllable from the app without hiding risky automation.

**Files:**

- Modify: `python_backend/football_tracking/api/schemas.py`
- Modify: `python_backend/football_tracking/api/service.py`
- Modify: `python_backend/football_tracking/api/routes/runs.py`
- Modify: `lib/api-spec/openapi.yaml`
- Modify generated API client files under `lib/api-zod/src/generated/` and `lib/api-client-react/src/generated/` when the export command changes them.
- Modify frontend files that currently back the AI Analysis and Deliverable pages.
- Test: `python_backend/tests/test_api_service.py`
- Test: `python_backend/tests/test_export_openapi.py`
- Test frontend route/component tests if present.

**Build:**

- [ ] Expose candidate registry, comparison summaries, and quality gate status in run artifacts/API.
- [ ] Add an explicit "Run AI improvement" control that distinguishes review-only from execute-approved.
- [ ] Show candidate status: proposed, approved, executed, pass, warn, fail, promoted, rejected.
- [ ] Show why an AI suggestion cannot execute: missing evidence, no ROI, unsafe window, no comparison, failed quality gate.
- [ ] Allow highlight and follow-cam candidate actions to be launched explicitly from their source run.

**Test:**

- [ ] API returns candidate registry summaries.
- [ ] API refuses execution without explicit approval id.
- [ ] UI can display no-candidate, pending-candidate, passed-candidate, and failed-candidate states.
- [ ] UI does not label review-only suggestions as applied improvements.
- [ ] OpenAPI export and generated clients stay in sync with new response/request fields.

**Deliver:**

- Operators can understand what AI proposed, what was tried, what passed, and what became final.

### PR8: Documentation And Real Video Verification

**Purpose:** Lock the process into docs and verify it on a real match video, including the previously observed right-corner lost segment.

**Files:**

- Modify: `README.md`
- Modify: `python_backend/README.md`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `python_backend/docs/operation-guide.zh.md`
- Modify: `python_backend/docs/operation-guide.en.md`
- Create or update: `docs/operations/real-video-tuning-checklist.md`

**Build:**

- [ ] Document AI review vs AI improvement.
- [ ] Document when to use stronger models for improvement.
- [ ] Document temporal chunk parallelism vs spatial split/SAHI tradeoff.
- [ ] Document candidate comparison and promotion statuses.
- [ ] Document real-video checklist: inspect lost gaps, dense noise, camera motion, highlight tail, and final video.
- [ ] Document the fixture-backed real-video check for frames `2049-2544`, key frame `2079`, right-bottom expected region, and coverage labels `start`, `middle`, `end`, `tail`.
- [ ] Add example commands for dry-run, artifact-only, real mode, selected approvals, and candidate promotion.

**Test:**

- [ ] Run unit/integration tests from PR1-PR7.
- [ ] Run one real-video baseline or reuse latest stable output.
- [ ] Run AI improvement in real or provider-enabled mode when API key is configured.
- [ ] Execute at least one bounded missing-ball recovery candidate if a lost-gap approval exists.
- [ ] Render follow-cam and check `camera_motion_audit.json`.
- [ ] Render at least one highlight with post-roll validation.
- [ ] Manually inspect frames `2049-2544`, especially key frame `2079`, and record right-bottom ball/action visibility, packet coverage labels, candidate comparison status, and final promotion decision.

**Deliver:**

- Updated README and operation guides.
- A real-video verification report listing commands, output directory, artifacts, quality gate status, right-bottom 2049-2544 result, and visual inspection notes.

## Final Acceptance Criteria

- AI suggestions are not treated as improvements until they produce candidate artifacts and comparison reports.
- Missing-ball recovery has bounded ROI execution and comparison.
- Noise cleanup has bounded false-positive comparison.
- Follow-cam smoothing has candidate rerender and camera-motion comparison.
- Highlight generation protects post-event tail and supports AI-adjusted candidate windows.
- Stable workflow can run in dry-run, artifact-only, and real modes.
- Explicit approvals are required for any mutation or candidate execution.
- `ai_candidate_registry.json`, `*_comparison.json`, `ai_improvement_quality_gate.json`, and `final_ai_improvement_artifact_manifest.json` tell the same story.
- Real-video verification includes the known frame-2079 right-corner failure mode.

## Not In Scope For This Series

- Training a new YOLO model.
- Fully automatic event confirmation for fouls, offsides, or tactical analytics.
- Broad full-video spatial SAHI reruns as the default improvement path.
- Silent promotion of AI-suggested changes without explicit approval or passing comparisons.
- Large frontend redesign unrelated to AI improvement control.
