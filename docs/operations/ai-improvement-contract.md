# AI Improvement Contract

This contract is the stable reference for AI improvement workers and operators. It defines when AI output is only review text, when it becomes an executable candidate, and when a candidate may become final output. The model-facing prompt contract lives in `python_backend/football_tracking/ai_improvement_prompt_contract.py` and mirrors the public action set below. Executors for missing-ball recovery, follow-cam rerendering, and highlights must satisfy this document before they mutate or publish artifacts.

## Lifecycle

Candidate lifecycle stages are ordered as follows:

| Stage | Meaning |
| --- | --- |
| `review_only` | AI produced advisory text or a dry-run note. It is not executable and cannot be final output. |
| `proposed` | AI proposed a bounded, traceable candidate shape. |
| `approved` | An operator explicitly approved an action by id. Approval-file presence alone is not approval. |
| `pending_execution` | The approved action still needs a candidate executor or API-backed run. |
| `executed` | Candidate artifacts were written under an `ai_candidates/<problem_type>/<candidate_id>/` lane. |
| `compared` | A candidate comparison report exists and is valid enough to derive status. |
| `gated` | The quality gate consumed candidate evidence. |
| `finalized` | A final manifest recorded promotion, rejection, pending confirmation, unsupported status, or resolved-noop status. |

Comparison report statuses are `pass`, `warn`, `fail`, and `unavailable`. Lifecycle summaries may use `none` only to mean no comparison exists yet; `none` is not a valid comparison report status.

Promotion statuses are:

| Status | Meaning |
| --- | --- |
| `not_promoted` | Default state before final selection. |
| `pending_confirmation` | A `warn` candidate needs explicit human confirmation before final promotion. |
| `promoted` | The candidate is selected in `final_ai_improvement_artifact_manifest.json`. |
| `rejected` | The candidate is recorded as rejected. |
| `blocked` | Promotion is impossible until a contract problem is fixed. |

Blocking reasons are `missing_evidence`, `unsafe_window`, `unsupported_type`, `missing_candidate_id`, `missing_comparison`, `failed_quality_gate`, `pending_api_execution`, and `pending_human_confirmation`.

| Blocking reason | Meaning |
| --- | --- |
| `missing_evidence` | The candidate or resolution lacks packet, visual, approval, or artifact evidence required for its problem type. |
| `unsafe_window` | The requested frame window is invalid, too broad, outside source bounds, or would use an unsafe recovery scope. |
| `unsupported_type` | The candidate problem type or action is not supported by the current executor/promotion path. |
| `missing_candidate_id` | An executable or promotable item lacks a stable `candidate_id`. |
| `missing_comparison` | Candidate artifacts exist or are selected, but no usable comparison report is linked. |
| `failed_quality_gate` | The run-level quality gate failed and blocks final promotion. |
| `pending_api_execution` | Approval exists, but the candidate still needs an executor or API-backed run. |
| `pending_human_confirmation` | A `warn` candidate needs explicit human confirmation before promotion. |

## Traceability

Executable candidates are stricter than ordinary review suggestions. They must carry these fields from suggestion through approval, candidate artifacts, comparison, quality gate, and final manifest:

| Field | Contract |
| --- | --- |
| `candidate_id` | Required for executable candidates. It must be stable, path-safe, unique within the run, and reused in candidate artifact refs, comparison reports, lifecycle state, and final manifest entries. |
| `approval_id` | Required for any operator-approved execution or promotion. It must come from an explicitly supplied approval file or promotion action and must be recorded as consumed evidence. |
| `problem_type` | Required for candidate workflows. Valid values are `missing_ball`, `noise`, `follow_cam`, and `highlight`. |
| Artifact refs | Candidate artifacts stay under `ai_candidates/<problem_type>/<candidate_id>/`. Reports reference paths and statuses only; comparison and final manifest builders must not copy media or mutate baseline tracks. |

An executable candidate must also include evidence ids, a bounded frame window, `expected_artifact`, and `comparison_criteria`. Evidence ids may be packet ids, visual review ids, visual localization ids, camera motion event ids, or event candidate ids as appropriate for the problem type. `visual_localization_id` is valid only when it appears in `ai_visual_localization.json`. If evidence is insufficient, the AI output must stay `review_only` rather than pretending to be approval-ready.

## Executable Actions

The public executable `approved_action` set is closed:

- `localize_ball_roi`
- `rerun_ball_window`
- `mark_ball_not_visible`
- `noise_filter_adjustment`
- `tighten_noise_filter`
- `reject_noise`
- `adjust_follow_cam`
- `tracking_rerun_before_follow_cam`
- `adjust_highlight_window`
- `render_suggested_highlight`

Legacy `targeted_rerun` inputs are normalized to `rerun_ball_window` for new approval artifacts. Executors may still adapt normalized approvals to older internal names at the execution boundary, but new public artifacts should emit the canonical action name.
Schemas may continue to accept `targeted_rerun` as a legacy input value during the migration window. That compatibility value is not part of the closed executable output set; approved artifacts should expose `approved_action: "rerun_ball_window"` and may keep `legacy_approved_action: "targeted_rerun"` only as provenance metadata.

Unknown, broad, unbounded, full-video spatial split/SAHI, or untraceable actions are review-only/manual-review. They must not become executable candidates and must not mutate final output.

Review-only missing-ball localization requests may use `recommended_action: "request_targeted_localization"` when packet or visual-review evidence is traceable but the AI cannot yet provide a bounded `local_search_roi`. This action is not executable and must not appear as an approved recovery action. It is a request to create `ai_visual_localization.json` evidence first.

## Model Policy

Use the configured strong model for run-level improvement and hard recovery decisions, including missing-ball localization, long-gap reasoning, and any decision that could produce candidate artifacts.

Smaller or cheaper models may be used only for low-risk tagging, operator labeling, or dry-run smoke checks. Dry-run output remains review-only unless a later real run produces the required approval, candidate, comparison, and gate evidence.

Workflow reports must record provider mode, candidate intent, and model selection so an operator can tell whether the output came from a strong-model candidate path or a low-risk review path.

If an improvement-capable model is unavailable in real mode, the workflow records non-mutating review-only/unavailable state. It must not manufacture executable approvals from a small-model fallback.

## Missing-Ball Closure

Missing-ball problems have exactly two valid closures.

| Closure | Required artifact | Contract |
| --- | --- | --- |
| Bounded recovery candidate | `missing_ball_recovery_comparison.json` | Runs a bounded rerun or ROI candidate under `ai_candidates/missing_ball/<candidate_id>/`, writes candidate artifacts, and compares candidate recovery against baseline plus approval evidence. |
| Evidence-backed not-visible resolution | `missing_ball_resolution.json` | Records that the ball is hidden, off-frame, or impossible to identify for the full requested window, backed by packet or visual evidence. This is a resolution lane, not a recovery candidate. |

The long right-bottom gap `2049-2544` is protected as a full-window requirement. A short neighborhood around frame `2079` may be useful evidence, but it cannot close `2049-2544` unless packet, visual, AI suggestion, approval, recovery comparison, or not-visible resolution evidence covers the entire long window or explicitly records or lists uncovered subwindows.

`localize_ball_roi` is bounded-window-only. It must not expand into broad full-video SAHI. Broad full-video SAHI is not a stable closure path; SAHI/ROI work belongs inside approved bounded recovery windows with packet or visual provenance.

Valid executable `localize_ball_roi` example:

```json
{
  "recommended_action": "localize_ball_roi",
  "problem_type": "missing_ball",
  "candidate_id": "candidate_2049_2544_right_corner",
  "start_frame": 2049,
  "end_frame": 2544,
  "visual_localization_id": "visual_localization:2049_2544_right_corner",
  "local_search_roi": {
    "coordinate_space": "image",
    "frame": 2121,
    "x": 5000,
    "y": 960,
    "width": 120,
    "height": 200,
    "confidence": 0.9
  },
  "expected_artifact": {"name": "ball_track.csv", "role": "candidate"},
  "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"}
}
```

Valid non-executable localization request example:

```json
{
  "recommended_action": "request_targeted_localization",
  "requested_action": "localize_ball_roi",
  "source_packet_id": "packet_2049_2544_right_corner",
  "candidate_contract": {
    "approved_action": "localize_ball_roi",
    "required_fields_present": false,
    "missing_fields": ["local_search_roi"]
  }
}
```

Valid evidence-backed not-visible example:

```json
{
  "recommended_action": "mark_ball_not_visible",
  "problem_type": "missing_ball",
  "candidate_id": "resolution_2049_2078_hidden",
  "source_packet_id": "packet_2049_2078",
  "start_frame": 2049,
  "end_frame": 2078,
  "likely_ball_region": {
    "description": "not visible",
    "confidence": 0.0
  },
  "expected_artifact": {"name": "missing_ball_resolution.json", "role": "resolved_noop"},
  "comparison_criteria": {"resolution": "not_visible"}
}
```

## Follow-Cam Thresholds

Follow-cam candidates must prove smoother motion without hiding bad tracking by zooming out. Compare the baseline and candidate `camera_motion_audit.json`, `camera_path.csv`, and `ball_track.csv` over the same evaluable window.

| Check | Pass rule |
| --- | --- |
| Review events | Candidate `summary.review_event_count` is no greater than baseline. |
| P95 pan step | Candidate `p95_pan_step_px` is at least 10% lower than baseline, or both baseline and candidate are below `PAN_STEP_WARN_PX` (`90.0`). |
| Max pan acceleration | Candidate `max_pan_accel_px` is at least 10% lower than baseline, or both baseline and candidate are below `PAN_ACCEL_WARN_PX` (`80.0`). |
| Max zoom step | Candidate `max_zoom_step_px` is not more than 10% worse than baseline and remains below `ZOOM_STEP_FAIL_PX` (`48.0`). |
| P95 crop height | Candidate p95 crop height is no more than 15% above baseline. |
| Max crop height | Candidate max crop height is no more than 20% above baseline. |
| Detected/Predicted crop coverage | For frames with enough matching ball/camera rows, candidate coverage is at least `baseline_coverage - 0.02` and at least `0.95`. |

Crop coverage is computed from Detected or Predicted `ball_track.csv` frames and `camera_path.csv`: a frame is covered when the ball center lies inside the crop rectangle. Sparse data must produce `warn` or `unavailable` evidence with sample counts and reasons; it must not silently pass.

If a camera event overlaps Lost/Predicted tracking or nearby tracking instability, the valid action is `tracking_rerun_before_follow_cam`, not `adjust_follow_cam`, until linked tracking recovery passes. If tracking is stable and the issue is camera-only, the valid action is `adjust_follow_cam`.

## Highlight Comparison

Highlight candidates compare event candidate metadata to the rendered candidate clip.

Highlight suggestions use a path-safe `candidate_id` for the output candidate and `event_candidate_id` for the source event from `event_candidates.json`.

The candidate `render_window` must contain the event `core_window`. It must also contain the required post-event tail through `core_window.end_frame + buffer_policy.min_tail_frames`, clamped by the source video end.

When the source-video end clamps the tail, the comparison must record that clamp as explicit pass or warn evidence. It must not silently trim the tail. A candidate that cuts the core event, cuts available tail, uses an invalid frame range, or mismatches the source event id/candidate id fails comparison.

Review-only highlight clips cannot be accepted or published as AI improvements. Accepted highlights need comparison-backed candidate evidence unless a separate legacy/manual mode is explicitly documented.

## Final Output

Review-only artifacts cannot mutate `ball_track.csv`, `ball_track.cleaned.csv`, follow-cam videos, highlight clips, or final manifests as applied improvements. They cannot be final output.

Final output requires all of the following:

- Explicit operator approval with a consumed `approval_id`.
- Candidate artifacts under the correct `ai_candidates/<problem_type>/<candidate_id>/` lane, or a valid `missing_ball_resolution.json` resolved-noop for not-visible closure.
- A candidate comparison report whose status is derived from non-empty `checks`; summary/check mismatches are not clean passes.
- Quality-gate evidence in `ai_improvement_quality_gate.json`.
- A `final_ai_improvement_artifact_manifest.json` entry that records baseline output, candidate output, comparison report, consumed approvals, quality gate status, final selection or rejection, and warnings.

`pass` candidates may be promoted. `warn` candidates require `requires_human_confirmation: true` on the final artifact plus a consumed approval for the same `candidate_id` with `approval_type: human_confirmation`. `fail` candidates are rejected. `unavailable` candidates stay out of final output until usable comparison evidence exists.

Generated missing-ball and noise `pass` candidates remain pending in `final_ai_improvement_artifact_manifest.json` until an operator calls `finalize_ai_candidate` with `output_role="missing_ball_track"` or `output_role="noise_cleaned_track"`. `warn` candidates also require explicit confirmation. Review-only, missing-approval, unknown-candidate, `fail`, and `unavailable` cases cannot mutate final output.
