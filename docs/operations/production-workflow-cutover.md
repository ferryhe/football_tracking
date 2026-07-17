# Production Workflow Cutover

## Operator entry points

The primary interface has two destinations:

- **Production** (`/production`) is the sequential workspace: original video, field calibration, trial and tuning, full tracking and review, then verified product.
- **Production History** (`/history`) groups trials, full runs, and products by original video. A link such as `/history?run=<run_id>` opens the exact group and run without loading evidence for unrelated products.

The root URL and the former Dashboard URL open Production. **Save and exit** goes to Production History, so it cannot loop back into the default workspace.

## Legacy URL migration

Legacy files and routes remain available for one release as a rollback boundary.

| URL | Cutover behavior |
| --- | --- |
| `/baseline` | Opens Production with a migration notice. The draft-derived prerequisite gate decides the earliest valid step; it never skips original-video selection or three-frame calibration. |
| `/broadcast` | Opens Production with a migration notice. |
| `/broadcast?run=<id>` | Opens Production only when `<id>` is the current parent run of the saved local draft. Older or unknown IDs open exact-run Production History. |
| `/ai?run=<id>` | Unlisted compatibility page. It selects only that completed/failed run and fails closed if unavailable. |
| `/deliverable?run=<id>` | Unlisted compatibility page for highlight tools. It selects only a completed run with event-candidate evidence and fails closed if unavailable. |

History shows advanced AI or highlight actions only when the selected run satisfies the corresponding compatibility-page contract. A missing exact run never falls back to another run.

## Operational use

1. Open Production and select the original video.
2. Approve a polygon and confirm it on three distinct source frames.
3. Run the bounded trial, adjust settings, and explicitly accept the trial.
4. Confirm the immutable configuration identity and start the full run.
5. Complete required review, recomputation, and rendering steps.
6. Open the verified product or use **Save and exit** to inspect its exact run in Production History.
7. Do not publish solely because the UI reports `ready` or artifact verification passes. Complete the release gates in [Production release validation](./production-workflow-release-validation.md).

## Rollback

Rollback is a frontend navigation/routing revert: restore the prior navigation and legacy page routing, then prevent new review-evidence activations. Do not delete backend runs, artifacts, generated clients, or locally saved production data. Evidence already consumed remains governed by its generation and lineage rules; disabling the cutover must not mutate or relabel it. Legacy code is removed only by a later, explicitly approved cleanup.
