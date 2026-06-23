# AI Improvement Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `managed-pr-development` for the full program. Each PR starts from latest `main`, uses a fresh branch and worker, receives independent spec/code review, waits for remote CI/Copilot comments, merges only after valid feedback is resolved, and deletes merged branches.

**Goal:** Turn AI from passive review into bounded, executable, measurable improvement for ball recovery, noise cleanup, follow-cam stability, and highlight timing.

**Architecture:** Baseline tracking artifacts stay immutable. AI may propose changes, but code executes only explicit approval ids into isolated candidate folders. Every executable candidate must write a manifest, comparison report, registry entry, quality-gate result, and final-manifest trace before it can be promoted.

**Tech Stack:** Python backend, pytest, FastAPI API service, generated OpenAPI clients, React operator UI, existing tracking/follow-cam/highlight artifacts, OpenAI-backed visual/improvement review when configured.

---

## Status And Review Revisions

Already merged before this plan:

- PR #48: noise AI cleanup candidates.
- PR #49: AI evidence-contract guardrails.

Independent plan review returned **Needs revision**. This revision applies the feedback:

- Follow-cam candidates now explicitly update registry, quality gate, final manifest, stable workflow, and API lifecycle.
- Highlight publishing now blocks AI-review-only clips and requires comparison-backed candidate evidence.
- Real-video verification is now mandatory on `python_backend/data/raw5760x144020fps.mp4`; fixture-only proof is not acceptable for final sign-off.
- Noise cleanup remains a first-class lane for lifecycle, promotion, and real-video regression.
- Lifecycle status is split into `stage`, `comparison_status`, `promotion_status`, `resolution_status`, and `blocking_reasons` so UI does not blur AI audit with AI improvement.
- API/codegen commands and generated files are explicit.

## Requirement Summary

The user-facing requirement is **AI improvement**, not only **AI audit**:

- **Ball not found:** AI inspects packets/frames and proposes a bounded recovery window or a full-window `not_visible` resolution. A short note around frame `2079` must not close a longer right-bottom corner gap such as `2049-2544`.
- **Too much noise:** AI identifies false-positive islands and proposes cleanup candidates. Cleanup is useful only when false positives decrease while sustained real-ball signal is preserved.
- **Camera too shaky:** AI uses `camera_motion_audit.json` to decide whether the root cause is tracking loss or follow-cam path tuning. It must not hide bad tracking by merely zooming out.
- **Highlight timing:** Default pre/post buffers remain the safe baseline. AI may adjust boundaries only when the event core and post-event tail remain covered.

Throughput rule:

- Prefer **temporal chunk parallelism** for full-video speed.
- Use spatial split/SAHI only in bounded, approved recovery windows, because broad spatial splitting produced too many extra ball-like noise points.

## Shared Candidate Proof Contract

Every executable AI candidate must satisfy this chain:

- Baseline artifacts are hash-snapshotted before candidate execution.
- Execution requires explicit approval ids; approval-file presence alone is not enough.
- Candidate output is isolated under `ai_candidates/<problem_type>/<candidate_id>/`.
- Candidate writes `candidate_manifest.json`.
- Candidate writes a problem-specific comparison report with `pass`, `warn`, `fail`, or `unavailable`.
- Parent output writes or updates `ai_candidate_registry.json`.
- `ai_improvement_quality_gate.json` sees the comparison and blocks missing evidence.
- `final_ai_improvement_artifact_manifest.json` traces baseline, AI suggestion, approval, candidate output, comparison, gate result, and promotion state.
- `pass` can promote according to policy; `warn` requires explicit confirmation; `fail`, `unavailable`, `unsupported`, and unexecuted review-only notes cannot be promoted.

## Required Validation Commands

Use the smallest focused command first, then broader checks before each PR:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe -m pytest python_backend\tests\<focused_test>.py -q
.\.venv\Scripts\python.exe -m pytest python_backend\tests -q
.\.venv\Scripts\ruff.exe check python_backend --select F401,F841
python_backend\scripts\export_openapi.py --output lib\api-spec\openapi.yaml
pnpm --filter @workspace/api-spec run codegen
pnpm run typecheck
pnpm --filter @workspace/web run build
```

Generated/API files that must stay synchronized when schemas change:

- `lib/api-spec/openapi.yaml`
- `lib/api-client-react/src/generated/`
- `lib/api-zod/src/generated/`
- `artifacts/web/src/lib/types.ts`

---

## PR2: Backend Candidate Lifecycle Contract

**Purpose:** Add a backend source of truth that tells whether AI only reviewed, suggested, approved, executed, compared, gated, promoted, rejected, or marked a full window not visible.

**Build:**

- Create `python_backend/football_tracking/ai_candidate_lifecycle.py`.
- Aggregate lightweight summaries from:
  - `ai_improvement_report.json`
  - `ai_improvement_approved_actions.json`
  - `ai_candidate_registry.json`
  - comparison reports under parent output and `ai_candidates/**`
  - `ai_improvement_quality_gate.json`
  - `final_ai_improvement_artifact_manifest.json`
  - `missing_ball_resolution.json`
- Represent lifecycle as separate fields:
  - `stage`: `review_only`, `proposed`, `approved`, `pending_execution`, `executed`, `compared`, `gated`, `finalized`
  - `comparison_status`: `pass`, `warn`, `fail`, `unavailable`, or `none`
  - `promotion_status`: `not_promoted`, `pending_confirmation`, `promoted`, `rejected`, or `blocked`
  - `resolution_status`: `none`, `resolved_not_visible`, or `candidate_output`
  - `blocking_reasons`: list of machine-readable reasons
- Blocking reasons include:
  - `missing_evidence`
  - `unsafe_window`
  - `unsupported_type`
  - `missing_candidate_id`
  - `missing_comparison`
  - `failed_quality_gate`
  - `pending_api_execution`
  - `pending_human_confirmation`
- Expose lifecycle summaries through existing run/API responses in `python_backend/football_tracking/api/schemas.py`, `api/service.py`, and relevant routes.
- Keep execution mutations explicit: any execution or promotion action must include approval ids.
- Export OpenAPI and regenerate clients if schemas change.

**Tests:**

- Add `python_backend/tests/test_ai_candidate_lifecycle.py`.
- Empty output returns stable empty lifecycle.
- Review report without approval is `review_only` or `proposed`, never applied.
- Approval without candidate artifact is `pending_execution`.
- Registry plus comparison `pass/warn/fail/unavailable` maps correctly.
- `missing_ball_resolution.json` with full-window evidence maps to `resolved_not_visible`.
- Final manifest promoted/rejected state overrides raw suggestion text.
- Missing comparison creates `missing_comparison`.
- `python_backend/tests/test_api_service.py` verifies run responses include lifecycle summary.
- `python_backend/tests/test_export_openapi.py` verifies schema fields are exported.

**Deliver:**

- Backend/API lifecycle contract.
- Generated client updates when needed.
- No UI redesign yet; this PR keeps risk contained.

---

## PR3: Operator UI Lifecycle Visibility

**Purpose:** Make the UI stop confusing AI audit text with executed AI improvement.

**Build:**

- Update `artifacts/web/src/pages/ai-analysis.tsx`.
- Update `artifacts/web/src/pages/deliverable.tsx`.
- Update `artifacts/web/src/lib/types.ts` if generated clients do not fully cover UI-specific shape.
- Show each AI item as one of:
  - AI review only
  - suggested
  - approved
  - waiting to execute
  - executed candidate
  - passed/warned/failed comparison
  - promoted final output
  - rejected/blocked
- Show comparison evidence for missing-ball, noise, follow-cam, and highlight candidates.
- Show blocking reasons in plain operator language.
- Deliverable page must tie final/publishable output to manifest or comparison-backed artifacts.
- Keep buttons disabled unless explicit approval ids exist.

**Tests:**

- `pnpm run typecheck`
- `pnpm --filter @workspace/web run build`
- Add or update frontend tests if the repo has an established test harness for these pages.
- UI handles empty, proposed, pending, pass, warn, fail, unsupported, resolved no-op, promoted, and rejected states.
- UI never labels review-only notes as applied improvement.
- Noise lifecycle is visible, including pass/fail comparison and promotion state.

**Deliver:**

- Operator-visible lifecycle.
- Clear separation between "AI said" and "AI executed and passed comparison".

---

## PR4: Missing-Ball Executor Unification

**Purpose:** Make stable workflow execute missing-ball candidates through the same reusable path as the API, removing `pending_api_required`.

**Build:**

- Create `python_backend/football_tracking/missing_ball_candidate_executor.py`.
- Extract reusable logic from existing API service methods, especially the child-run comparison/registration path around:
  - `_write_approved_child_missing_ball_comparison`
  - `_register_approved_child_missing_ball_candidate`
- Keep existing API child-run request/response behavior backward compatible.
- Let `python_backend/scripts/run_stable_ai_improvement_workflow.py` execute selected `targeted_rerun` and `localize_ball_roi` approvals directly through the shared executor.
- Candidate artifacts include:
  - recovered track CSV
  - `ball_audit.json`
  - `metrics_report.json`
  - `missing_ball_recovery_comparison.json`
  - `candidate_manifest.json`
  - registry entry
- Reject broad full-video spatial/SAHI recovery before output is created.
- Allow bounded ROI/SAHI only inside approved recovery windows.

**Tests:**

- API child-run behavior remains compatible.
- Stable workflow executes an explicit selected missing-ball approval end to end.
- Approval-file presence alone does not execute.
- Candidate comparison passes only when the full required gap is recovered or resolved.
- Short `2079`-only coverage fails for a `2049-2544` required gap.
- Baseline track hashes remain unchanged.
- Registry, quality gate, final manifest, and lifecycle record the candidate.
- Broad full-video spatial/SAHI request is rejected.

**Deliver:**

- One canonical missing-ball candidate executor.
- Stable workflow can produce real missing-ball candidate outputs without manual API follow-up.
- Right-bottom corner case is protected by full-window evidence.

---

## PR5: Follow-Cam AI Contract And Root-Cause Routing

**Purpose:** Define what AI is allowed to suggest for shaky camera output before renderer mutation exists.

**Build:**

- Update `python_backend/football_tracking/ai_contracts.py` and `ai_improvement.py`.
- Reuse existing actions where already present:
  - `adjust_follow_cam`
  - `tracking_rerun_before_follow_cam`
  - `human_review_camera_motion`
- Do **not** introduce `tracking_recovery_then_follow_cam_rerender` unless this PR also carries it through schema, approval, executor, comparison, lifecycle, and tests. Default plan: do not add it yet.
- Include `camera_motion_audit.json` events and nearby ball-track status in AI context.
- Route Lost/Predicted overlap to tracking recovery first.
- Route Detected-only camera spikes to follow-cam tuning.
- Record follow-cam approvals as `pending_execution` or `unsupported` with clear reason until PR6 implements execution.
- Surface the pending state through lifecycle from PR2/PR3.

**Tests:**

- Camera-motion events appear in AI context.
- Lost/Predicted overlap produces tracking-first action.
- Detected-only motion spike may produce `adjust_follow_cam`.
- Unknown camera actions are rejected by contract validation.
- Pending follow-cam state is visible and not labeled as applied improvement.

**Deliver:**

- Safe AI contract for shaky follow-cam cases.
- No renderer mutation yet; this PR prevents ambiguous AI recommendations.

---

## PR6: Follow-Cam Rerender Candidate And Comparison

**Purpose:** Execute approved follow-cam candidates and prove smoother motion without hiding bad play coverage.

**Build:**

- Create `python_backend/football_tracking/follow_cam_candidate_comparison.py`.
- Execute approved candidates under `ai_candidates/follow_cam/<candidate_id>/`.
- Candidate artifacts:
  - `follow_cam.mp4`
  - `camera_path.csv`
  - `follow_cam_report.json`
  - `camera_motion_audit.json`
  - `follow_cam_candidate_comparison.json`
  - `candidate_manifest.json`
- Update parent output:
  - `ai_candidate_registry.json`
  - `ai_improvement_quality_gate.json`
  - `final_ai_improvement_artifact_manifest.json`
  - lifecycle summary
- Compare baseline vs candidate:
  - review event count
  - p95 pan step
  - max pan acceleration
  - max zoom jump
  - crop/ball coverage proxy
  - excessive zoom-out ratio
- Fail candidates that reduce shake by zooming too far out or losing the ball from the crop.
- For tracking-first actions, require linked missing-ball candidate to pass before follow-cam rerender can pass.
- Add stable workflow execution for selected follow-cam approvals.

**Tests:**

- Approved follow-cam-only candidate writes all artifacts.
- Registry, quality gate, final manifest, and lifecycle summarize follow-cam candidate status.
- Comparison passes when motion improves and coverage is preserved.
- Comparison fails on excessive zoom-out.
- Comparison fails when ball/crop coverage drops.
- Comparison fails when motion metrics regress.
- Follow-cam-only candidate does not mutate ball-track files.
- Tracking-first candidate fails when linked missing-ball candidate fails.
- Stable workflow executes a selected follow-cam candidate.
- A short fixture/canary renders real `follow_cam.mp4` and `camera_path.csv`.

**Deliver:**

- Candidate follow-cam video with measurable stability proof.
- A comparison report that separates "smoother" from "hid the problem".

---

## PR7: Highlight AI Candidate, Comparison, And Publish Gate

**Purpose:** Let AI adjust highlight boundaries while preserving the goal/shot core and enough aftermath, and prevent AI-review-only clips from being published as improvements.

**Build:**

- Create `python_backend/football_tracking/highlight_candidate_comparison.py`.
- Update:
  - `python_backend/football_tracking/events.py`
  - `python_backend/football_tracking/highlights.py`
  - `python_backend/football_tracking/accepted_highlights.py`
  - `python_backend/scripts/run_ai_visual_review.py`
  - `python_backend/scripts/run_stable_ai_improvement_workflow.py`
  - API service highlight render helpers, especially `create_highlight_render` and `_resolve_approved_highlight_selection`
- Reuse existing event fields:
  - `core_window`
  - `render_window`
  - `buffer_policy`
- Execute selected `adjust_highlight_window` or `render_suggested_highlight` approvals under `ai_candidates/highlights/<candidate_id>/`.
- Candidate artifacts:
  - `highlight.mp4`
  - `highlight_report.json`
  - `highlight_candidate_comparison.json`
  - `candidate_manifest.json`
- Preserve default pre/post buffer as baseline.
- Require `core_window` coverage.
- Require post-event tail coverage unless source video end clamps it.
- Record before/after window, reason, event id, source-end clamp, and tail coverage.
- Accepted highlight publishing must require comparison-backed candidate evidence; visual-review-only `accept_highlight` is advisory unless paired with candidate comparison/pass or explicit legacy mode.
- Update registry, quality gate, final manifest, and lifecycle for highlight candidates.

**Tests:**

- Default highlight includes core event and post-event tail.
- AI window cutting core frames fails.
- AI window cutting available tail fails.
- End-of-video clamp records reason and preserves available tail.
- Candidate render writes video/report/comparison artifacts.
- Accepted highlight copier rejects visual-review-only clips as final AI improvements.
- Accepted highlight copier accepts comparison-backed publishable clips.
- Registry, quality gate, final manifest, and lifecycle summarize highlight status.
- Short real or fixture clip is readable.

**Deliver:**

- AI-adjusted highlight candidates with safe timing.
- Goal/shot clips keep context after the event instead of cutting too early.
- No AI-review-only clip is mislabeled as an AI improvement.

---

## PR8: Promotion Controls And Product Polish

**Purpose:** Close the operator workflow after all candidate types exist.

**Build:**

- Add promotion/rejection API controls.
- Proposed endpoint shape:
  - `POST /runs/{run_id}/ai/candidates/{candidate_id}/promote`
  - request fields: `approval_id`, `candidate_id`, `problem_type`, `confirm_warn: bool = false`, `operator_note: str | null`
  - reject without explicit `approval_id`.
  - reject `warn` unless `confirm_warn` is true.
  - reject `fail`, `unavailable`, `unsupported`, and review-only candidates.
- Add rejection endpoint or request mode:
  - `POST /runs/{run_id}/ai/candidates/{candidate_id}/reject`
  - request fields: `approval_id`, `candidate_id`, `problem_type`, `rejection_reason`
- Write promotion/rejection decisions into `final_ai_improvement_artifact_manifest.json`.
- Show comparison details for missing-ball, noise, follow-cam, and highlight candidates.
- UI language must distinguish:
  - AI audit
  - AI suggestion
  - approved candidate
  - executed candidate
  - passed comparison
  - promoted final output

**Tests:**

- Promotion accepts `pass`.
- Promotion blocks `warn` without confirmation.
- Promotion accepts `warn` with explicit confirmation and note.
- Promotion blocks `fail`, `unavailable`, `unsupported`, and review-only candidates.
- Rejection writes reason to final manifest and lifecycle.
- UI shows comparison evidence and final manifest status.
- Noise candidates can be promoted/rejected through the same controls.
- UI never labels review-only notes as applied improvement.

**Deliver:**

- Complete visible lifecycle from AI idea to final artifact.
- Human-safe promotion controls.

---

## PR9: Real-Video Verification, Docs, And Skill Capture

**Purpose:** Prove the whole system on the real match video and preserve the workflow as documentation/skill.

**Build:**

- Use the real video: `python_backend/data/raw5760x144020fps.mp4`.
- Produce a dated evidence output folder under `python_backend/outputs/` or the project-standard output location.
- Run the full stable workflow with provider configuration when available; if the current environment lacks the API key, copy/use the configured key from the adjacent working environment only in the approved local secret flow.
- Record:
  - model settings
  - output directory
  - review packet media paths
  - approval ids
  - candidate ids
  - comparison reports
  - promoted/rejected decisions
  - final follow-cam video
  - final highlights
- Manually inspect:
  - the `2049-2544` right-bottom corner gap
  - frame `2079` neighborhood
  - noisy false-positive islands
  - camera-motion spike windows
  - highlight tails after goal/shot events
- Update:
  - `README.md`
  - `python_backend/README.md`
  - `docs/operations/ai-improvement-workflow.md`
  - `python_backend/docs/operation-guide.zh.md`
  - `python_backend/docs/operation-guide.en.md`
- Create `docs/operations/real-video-ai-improvement-checklist.md`.
- After repo docs merge, propose an update to `C:/Users/ferry/.codex/skills/football-tracking-real-video-tuning/SKILL.md`.

**Tests:**

- Full backend tests pass.
- `pnpm run typecheck` and web build pass after docs/UI changes.
- Real-video run produces review packets with media.
- Real-video run produces at least one executable candidate for each available lane:
  - missing-ball or evidence-backed `not_visible`
  - noise cleanup
  - follow-cam rerender or documented tracking-first block
  - highlight candidate or documented no-event/no-safe-window block
- Each executed real-video candidate has comparison, registry, quality gate, final manifest, and lifecycle evidence.
- Final follow-cam renders and includes `camera_motion_audit.json`.
- At least one highlight renders and passes tail coverage when event candidates exist.
- Fixture-only proof is acceptable for unit/integration tests, but not for this PR's final sign-off.

**Deliver:**

- Updated user/operator docs.
- Real-video evidence pack with commands, output dirs, model settings, approval ids, candidate decisions, final videos, and highlights.
- Local skill update proposal after the workflow is proven stable.

---

## End-To-End Done Criteria

- Ball-missing cases close only via comparison-backed recovery or full-window evidence-backed `not_visible`.
- Noise cleanup reduces false-positive islands without damaging sustained true-ball signal.
- Follow-cam candidates reduce motion spikes without hiding tracking loss.
- Highlight clips preserve event core and post-event tail.
- Review-only AI text cannot mutate artifacts and cannot appear as applied improvement.
- Approved execution always requires explicit ids.
- Final output is traceable from baseline to AI suggestion, approval, candidate, comparison, quality gate, manifest, and promoted artifact.
- The real video `python_backend/data/raw5760x144020fps.mp4` has a recorded evidence pack and manual inspection notes.
- Merged branches are deleted locally and remotely after each PR is merged.
