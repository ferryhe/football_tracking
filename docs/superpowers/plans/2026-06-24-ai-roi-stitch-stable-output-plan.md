# AI ROI Stitch Stable Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert targeted AI ball localization into a safe, approval-backed recovery path that can improve missed-ball windows, render stable follow-cam candidates, and preserve highlight tails without promoting unsafe artifacts.

**Architecture:** First define the contract for AI localization evidence and provenance. Then generate validated `ai_visual_localization.json` evidence from actual decoded video dimensions. Then allow only `localize_ball_roi` windows with valid evidence to stitch ROI-bounded child tracks over old wrong trajectories. Follow-cam and highlight candidates consume only passed or explicitly finalized artifacts.

**Tech Stack:** Python 3, OpenCV, pytest, existing OpenAI Responses provider wrapper, current candidate registry/final artifact manifest, existing high-recall/follow-cam/highlight candidate modules.

---

## Real-Run Findings This Plan Must Preserve

- The source file is named `raw5760x144020fps.mp4`, but OpenCV decodes it as `5120x1440`. All AI coordinates must be validated against decoded dimensions, not filenames.
- Generic `gpt-5.4` AI improvement can return incomplete missing-ball candidates. The system must route useful-but-incomplete advice to targeted localization instead of failing the whole report.
- A targeted `gpt-5.4` crop localized the right-corner ball near frames `2079-2300`; the child ROI run reduced lost frames in that subwindow from `22` to `9`.
- Existing reconcile rejected the useful child window with `jump_gate_failed` because it compared against an old wrong trajectory around frame `2121`.
- The broader right-bottom issue must be treated as `2049-2544`, not only `2079-2300`. Any final claim must either recover or explicitly classify every subwindow as not-visible/manual-review/uncovered.
- A manual stitch experiment improved frame `2121`, but it is not a promotable path until implemented through approval, comparison, registry, and finalization gates.

## PR 0: AI Localization Contract And Provenance

**Branch:** `feature/ai-localization-contract`

**Purpose:** Define the schema and contract before implementing provider calls or mutation. This prevents a new evidence artifact from bypassing existing approval checks.

**Files:**
- Modify: `python_backend/football_tracking/ai_improvement_prompt_contract.py`
- Modify: `python_backend/football_tracking/ai_improvement.py`
- Modify: `python_backend/football_tracking/high_recall_windows.py`
- Modify: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `docs/operations/ai-improvement-contract.md`
- Test: `python_backend/tests/test_ai_improvement.py`
- Test: `python_backend/tests/test_high_recall_windows.py`
- Test: `python_backend/tests/test_missing_ball_candidate_executor.py`

**Contract Changes:**
- Add review-only action `request_targeted_localization`.
- Add provenance key `visual_localization_id`.
- Add evidence artifact name `ai_visual_localization.json`.
- `visual_localization_id` is valid only when it appears in `ai_visual_localization.json`.
- `localize_ball_roi` remains executable only with complete ROI and traceable provenance.
- Incomplete missing-ball advice with traceable packet/window becomes non-executable `request_targeted_localization`, not a whole-report error.
- Incomplete missing-ball advice without traceable provenance remains `error`.

**Implementation Steps:**

- [x] Add `request_targeted_localization` to the prompt contract as review-only, not executable.
- [x] Update `_REVIEW_ONLY_ACTIONS` in `ai_improvement.py` to include `request_targeted_localization`.
- [x] Update validation so `localize_ball_roi` without ROI but with `source_packet_id` or `visual_review_id` normalizes to `request_targeted_localization`.
- [x] Add `candidate_contract.missing_fields=["local_search_roi"]` for normalized incomplete suggestions.
- [x] Extend provenance readers in `high_recall_windows.py` and `missing_ball_candidate_executor.py` to collect `visual_localization_id` from `ai_visual_localization.json`, while preserving existing packet/visual review behavior.
- [x] Update docs with examples of valid `localize_ball_roi`, valid `mark_ball_not_visible`, and non-executable `request_targeted_localization`.

**Commit Plan:**
- `test: cover targeted localization request contract`
- `feat: add ai localization provenance contract`
- `docs: document ai localization approval contract`

**Tests:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\pytest.exe python_backend\tests\test_ai_improvement.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_high_recall_windows.py python_backend\tests\test_missing_ball_candidate_executor.py -q
```

**Acceptance Gate:**
- A model can ask for targeted localization without creating an executable mutation.
- A future approval can reference `visual_localization_id`, but only after the artifact exists.

---

## PR 1: Formal AI Visual Localization Artifact

**Branch:** `feature/ai-visual-localization-artifact`

**Purpose:** Productize the successful targeted crop approach as a provider-backed, dimension-safe evidence report.

**Files:**
- Create: `python_backend/football_tracking/ai_visual_localization.py`
- Create: `python_backend/tests/test_ai_visual_localization.py`
- Modify: `python_backend/scripts/run_stable_ai_improvement_workflow.py`
- Modify: `docs/operations/ai-improvement-workflow.md`
- Modify: `docs/operations/real-video-ai-improvement-validation.md`

**Artifact Schema:**

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-24T00:00:00+00:00",
  "model": "gpt-5.4",
  "source_video": {
    "path": "<redacted-or-relative>",
    "width": 5120,
    "height": 1440,
    "fps": 20.0,
    "dimension_source": "opencv"
  },
  "requests": [
    {
      "visual_localization_id": "visual_localization:2049_2544_right_corner",
      "source_packet_id": "packet_ai_localize_2049_2544_right_corner",
      "start_frame": 2049,
      "end_frame": 2544,
      "coverage": {
        "covered_subwindows": [{"start_frame": 2079, "end_frame": 2300, "status": "localized"}],
        "uncovered_subwindows": [
          {"start_frame": 2049, "end_frame": 2078, "status": "needs_review"},
          {"start_frame": 2301, "end_frame": 2544, "status": "needs_review"}
        ]
      },
      "crop": {"x": 3400, "y": 520, "width": 1720, "height": 920, "coordinate_space": "image"},
      "media": {
        "contact_sheet": "manual_checks/ai_localization_2049_2544_contact_sheet.jpg",
        "crop_sheet": "manual_checks/ai_localization_2049_2544_crop_sheet.jpg",
        "sha256": "..."
      },
      "frames": [
        {
          "frame": 2121,
          "ball_visible": true,
          "confidence": 0.9,
          "local_search_roi": {
            "frame": 2121,
            "x": 5000,
            "y": 960,
            "width": 120,
            "height": 200,
            "coordinate_space": "image",
            "confidence": 0.9
          }
        }
      ]
    }
  ],
  "summary": {
    "status": "ok",
    "request_count": 1,
    "invalid_roi_count": 0,
    "uncovered_subwindow_count": 1
  }
}
```

**Implementation Steps:**

- [x] Implement `write_ai_visual_localization_report(output_dir, input_video, windows, model=None, client=None, dry_run=False)`.
- [x] Decode dimensions from OpenCV and store `dimension_source="opencv"`.
- [x] Reject out-of-bounds model ROI with a warning; do not silently accept coordinates beyond decoded width/height.
- [x] Normalize provider `"original"` coordinates to `"image"` only after bounds validation against decoded dimensions.
- [x] Record coverage over the requested full window, including uncovered subwindows.
- [x] Generate evidence media paths and hashes for contact/crop sheets when media is produced.
- [x] Add workflow stage `targeted_visual_localization`, gated by explicit CLI input such as `--targeted-localization-window 2049:2544:right_corner`.
- [x] Include `ai_visual_localization.json` as produced evidence in artifact lists, not as a final selected artifact.

**Commit Plan:**
- `feat: add ai visual localization artifact`
- `feat: wire targeted localization workflow stage`
- `docs: add localization validation guidance`

**Tests:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\pytest.exe python_backend\tests\test_ai_visual_localization.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_stable_ai_improvement_workflow.py -q
```

**Acceptance Gate:**
- The `5120x1440` vs filename mismatch is prevented by tests.
- `2049-2544` evidence explicitly records recovered and uncovered subwindows.

---

## PR 2: Strict ROI Stitching For `localize_ball_roi`

**Branch:** `feature/localize-roi-track-stitch`

**Purpose:** Let a valid AI-localized child track override an old wrong baseline segment, but only for narrow `localize_ball_roi` windows with strong internal evidence.

**Files:**
- Create: `python_backend/football_tracking/recovery_stitcher.py`
- Create: `python_backend/tests/test_recovery_stitcher.py`
- Modify: `python_backend/football_tracking/high_recall_reconcile.py`
- Modify: `python_backend/football_tracking/missing_ball_candidate_executor.py`
- Modify: `python_backend/football_tracking/missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_high_recall_reconcile.py`
- Test: `python_backend/tests/test_missing_ball_recovery_comparison.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`

**Non-Negotiable Safety Rule:**
Only `approved_action == "localize_ball_roi"` can downgrade `jump_gate_failed` into `boundary_transition_warning`. This does not apply to ordinary high-recall windows or broad `rerun_ball_window`.

**Pass Criteria For Stitch:**

```json
{
  "approved_action": "localize_ball_roi",
  "has_valid_provenance": true,
  "full_video_scope": false,
  "candidate_lost_frames": 9,
  "baseline_lost_frames": 22,
  "min_sustained_roi_run_frames": 12,
  "outside_roi_ratio": 0.05,
  "max_internal_step_px": 320,
  "boundary_transition_warning": true,
  "comparison_status": "pass"
}
```

Boundary warnings remain warning evidence inside a `pass` comparison. They must not change the tracking candidate to `warn`, because existing follow-cam candidate execution consumes only passed tracking candidates. If the boundary warning makes the rendered camera worse, PR 3's follow-cam comparison blocks promotion.

**Implementation Steps:**

- [ ] Add constants in `recovery_stitcher.py` with docstrings:
  - `MIN_STITCH_RUN_FRAMES = 12`
  - `ROI_INTERNAL_MAX_STEP_PX = 320.0`
  - `MAX_OUTSIDE_ROI_RATIO = 0.05`
- [ ] Implement `build_stitch_metrics(parent_rows, child_rows, start_frame, end_frame, roi)`.
- [ ] Implement `stitch_recovery_window(parent_track_csv, child_track_csv, output_csv, window, effective_roi)`.
- [ ] In `high_recall_reconcile.py`, call the stitcher only when window provenance says `localize_ball_roi`.
- [ ] Preserve old `jump_gate_failed` behavior for all non-localize windows.
- [ ] In `missing_ball_candidate_executor.py`, write stitched top-level `ball_track.csv` and `ball_track.cleaned.csv` when stitch passes.
- [ ] Write `recovery_stitch_report.json`.
- [ ] Update missing-ball comparison so `localize_roi_plausibility` checks the stitched candidate top-level track, not only parent-copied baseline.
- [ ] Update `missing_ball_recovery_comparison.py` so existing sustained recovery checks can account for ROI stitch evidence. A `localize_ball_roi` stitch that reduces lost frames and passes internal ROI checks should not fail only because it does not meet the current generic `SUSTAINED_RECOVERY_MIN_FRAMES = 24` threshold; record this as `sustained_recovered_frames` pass-with-localize-evidence or a separate `roi_stitch_recovery` pass check.
- [ ] Quality gate must require full required-window coverage for long windows such as `2049-2544`; partial `2079-2300` recovery leaves uncovered subwindows.

**Commit Plan:**
- `test: cover localize roi stitch decisions`
- `feat: stitch approved localize roi windows`
- `feat: gate stitched recovery candidates`

**Tests:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\pytest.exe python_backend\tests\test_recovery_stitcher.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_high_recall_reconcile.py python_backend\tests\test_missing_ball_recovery_comparison.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_missing_ball_candidate_executor.py python_backend\tests\test_ai_improvement_quality_gate.py python_backend\tests\test_ai_candidate_lifecycle.py -q
```

**Acceptance Gate:**
- A synthetic frame `2121` parent false point can be replaced by ROI-contained child point.
- A `rerun_ball_window` with the same jump remains rejected.
- A partial fix for `2079-2300` reports uncovered `2049-2078` and `2301-2544` unless evidence covers them.

---

## PR 3: Follow-Cam Candidate Visibility Comparison

**Branch:** `feature/follow-cam-target-window-comparison`

**Purpose:** Use the existing follow-cam candidate executor, but add the missing check that the AI-recovered ROI is actually visible in the resulting video.

**Files:**
- Modify: `python_backend/football_tracking/follow_cam_candidate_comparison.py`
- Modify: `python_backend/football_tracking/follow_cam_candidate_executor.py` only if artifact plumbing is missing.
- Modify: `python_backend/football_tracking/final_artifact_manifest.py` only if finalization metadata needs a new field.
- Test: `python_backend/tests/test_follow_cam_candidate_executor.py`
- Test: `python_backend/tests/test_ai_candidate_lifecycle.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`

**Implementation Steps:**

- [ ] Keep existing behavior: follow-cam candidates consume passed tracking candidates.
- [ ] Add `target_window_visibility` to `follow_cam_candidate_comparison.json`.
- [ ] For each approved ROI frame sampled in the target window, confirm the ROI center lies inside the candidate crop in `camera_path.csv`.
- [ ] Fail if fewer than `80%` of visible/predicted ROI samples are inside crop.
- [ ] Fail if camera motion audit is worse than baseline:
  - `review_event_count` increases.
  - `max_pan_step_px` increases by more than `5%`.
  - `max_pan_accel_px` increases by more than `5%`.
- [ ] If tracking comparison is pass but camera comparison fails, do not promote follow-cam.

**Commit Plan:**
- `feat: compare follow cam target window visibility`
- `test: block follow cam promotion when roi leaves crop`

**Tests:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\pytest.exe python_backend\tests\test_follow_cam_candidate_executor.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_ai_candidate_lifecycle.py python_backend\tests\test_ai_improvement_quality_gate.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_follow_cam.py python_backend\tests\test_camera_motion_audit.py -q
```

**Acceptance Gate:**
- A candidate like the manual stitch experiment can only become final if its ROI stays visible and camera motion does not regress.

---

## PR 4: Highlight Tail And Validation Docs

**Branch:** `feature/highlight-tail-validation-docs`

**Purpose:** Preserve shot/goal aftermath frames and document the real validation loop. Keep this PR small; most highlight tail logic already exists.

**Files:**
- Modify: `python_backend/football_tracking/highlight_candidate_executor.py` only if render metadata misses required tail fields.
- Modify: `python_backend/football_tracking/highlight_candidate_comparison.py`
- Modify: `python_backend/football_tracking/highlight_window_validation.py`
- Test: `python_backend/tests/test_highlight_candidate_executor.py`
- Test: `python_backend/tests/test_ai_improvement_quality_gate.py`
- Modify: `docs/operations/real-video-ai-improvement-validation.md`
- Modify: `README.md`

**Implementation Steps:**

- [ ] Ensure highlight comparison records `core_window_preserved`, `required_tail_frames`, `actual_tail_frames`, and `tail_status`.
- [ ] Reject AI suggested windows that trim event core or required post-event tail unless source-video end clamps the tail.
- [ ] Document real validation commands with `--approved-actions-path`, `--approval-ids`, and targeted localization inputs.
- [ ] Require baseline/candidate overlay sheets in real validation docs, not just media existence checks.

**Commit Plan:**
- `test: cover highlight tail comparison metadata`
- `feat: preserve highlight tail comparison gates`
- `docs: update real ai validation checklist`

**Tests:**

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\pytest.exe python_backend\tests\test_highlight_candidate_executor.py -q
.\.venv\Scripts\pytest.exe python_backend\tests\test_ai_improvement_quality_gate.py -q
```

**Acceptance Gate:**
- AI cannot produce a highlight that cuts off the important post-shot/goal frames.

---

## Real Validation Command Shape After PRs

The final reproducible validation must include targeted localization and explicit approved action consumption. Exact flags may be introduced in PR 1, but the shape must be:

```powershell
$env:PYTHONPATH='python_backend'
.\.venv\Scripts\python.exe python_backend\scripts\run_stable_ai_improvement_workflow.py `
  --output-dir python_backend\outputs\full_workflow_latest_review_20260622_060600 `
  --input-video python_backend\data\raw5760x144020fps.mp4 `
  --parallel-mode temporal `
  --mode real `
  --model gpt-5.4 `
  --candidate-intent suggest_candidates `
  --targeted-localization-window 2049:2544:right_corner `
  --approved-actions-path python_backend\outputs\full_workflow_latest_review_20260622_060600\ai_improvement_approved_actions.json `
  --approval-ids approval_ai_2049_2544_right_corner `
  --report-name stable_ai_improvement_workflow_report.real_gpt54_final.json
```

Required final assertions:

- `ai_visual_localization.json` exists and reports decoded source size `5120x1440`.
- Required long window `2049-2544` is fully covered by recovered, not-visible, or explicit uncovered subwindows.
- Missing-ball candidate top-level track differs from parent when stitch passes.
- Follow-cam candidate uses the passed stitched track.
- Frame `2121` is no longer the false center-field point.
- Camera motion comparison does not regress.
- Failed or partial candidates are not selected in final manifest.
- Highlight candidates preserve core plus post-event tail.

## Managed PR Operating Procedure

For every PR:

1. Update local `main` from `origin/main`.
2. Create the named branch from latest `main`.
3. Assign a worker subagent with disjoint file ownership.
4. Require tests first for behavior changes.
5. Run focused tests listed above.
6. Run spec-compliance reviewer.
7. Run code-quality reviewer.
8. Fix Critical and Important findings.
9. Commit, push, open PR.
10. Wait for remote checks and comments.
11. Fix valid remote comments.
12. Merge only when checks and valid feedback are resolved.
13. Delete merged local and remote branches if authorized.
14. Refresh `main` before starting the next PR.

## Do Not Do

- Do not promote `ai_visual_localization.json` as a final selected artifact; it is evidence only.
- Do not accept AI ROI beyond decoded frame bounds.
- Do not run full-video SAHI from `localize_ball_roi`.
- Do not weaken provenance checks.
- Do not downgrade `jump_gate_failed` except for strict `localize_ball_roi` windows with valid ROI evidence.
- Do not let a partial `2079-2300` fix claim closure for `2049-2544`.
- Do not let follow-cam consume `warn` tracking candidates unless a future PR explicitly implements human-confirmed finalization for that status.
- Do not trim highlight post-event tail.
