import type {
  ArtifactSummary,
  CreateRunRequest,
  InputCatalogResponse,
  RunRecord,
} from "@workspace/api-client-react";

import {
  deriveBroadcastWorkflowState,
  type BroadcastWorkflowStateName,
} from "./broadcastWorkflow";
import {
  calibrationIsComplete,
  type ProductionCalibrationDraft,
} from "./productionCalibration";
import {
  isProductionConfigEvidence,
  type ProductionConfigEvidence,
  type ProductionConfigVerification,
} from "./productionConfigFreeze";
import {
  canonicalJson,
  isProductionTrialState,
  materializedProductionTrialConfigName,
  productionTrialAcceptanceIsValid,
  productionTrialArtifactContract,
  productionTrialMatchesContext,
  sha256Text,
  type ProductionTrialState,
} from "./productionTrial";
import type { SourceSignature } from "./productionWorkflow";

export const PRODUCTION_FULL_RUN_METADATA_VERSION = "1.0" as const;

export type ProductionFullRunStatus = Exclude<
  BroadcastWorkflowStateName,
  "setup"
>;

export interface ProductionFullRunOperationObservation {
  run_id: string;
  kind: "recompute" | "render";
  status: string;
}

export interface ProductionFullRunObservation {
  run_status: RunRecord["status"];
  workflow_state: ProductionFullRunStatus;
  status_generation: string | null;
  trajectory_generation_id: string | null;
  operation: ProductionFullRunOperationObservation | null;
  observed_at: string;
}

export interface ProductionFullRunRecoveryObservation {
  state: ProductionFullRunStatus;
  operation_run: RunRecord | null;
}

export interface ProductionFullRunPendingSubmission {
  generation: number;
  submission_id: string;
  output_id: string;
  expected_run_id: string;
  request: CreateRunRequest;
  request_sha256: string;
  workflow_id: string;
  accepted_trial_run_id: string;
  accepted_trial_request_sha256: string;
  config_name: string;
  config_sha256: string;
  config_patch_sha256: string;
  calibration_digest: string;
  source_signature: SourceSignature;
  created_at: string;
}

export interface ProductionFullRunAttempt {
  run_id: string;
  generation: number;
  submission_id: string;
  parent_trial_run_id: string;
  config_name: string;
  config_sha256: string;
  request_sha256: string;
  request: CreateRunRequest;
  created_at: string;
  last_observed: ProductionFullRunObservation;
}

export interface ProductionFullRunState {
  revision: number;
  attempts: ProductionFullRunAttempt[];
  pending_submission: ProductionFullRunPendingSubmission | null;
  current_run_id: string | null;
}

export interface ProductionFullRunSubmission {
  pending: ProductionFullRunPendingSubmission;
}

interface ProductionFullRunContext {
  workflow_id: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  trial: ProductionTrialState;
  confirmed_config: ProductionConfigEvidence;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

function nonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function sha256String(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function uuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function sourceSignature(value: unknown): value is SourceSignature {
  return (
    isRecord(value) &&
    nonEmpty(value.path) &&
    typeof value.size_bytes === "number" &&
    Number.isFinite(value.size_bytes) &&
    value.size_bytes >= 0 &&
    nonEmpty(value.modified_at)
  );
}

function sourcesMatch(left: SourceSignature, right: SourceSignature): boolean {
  return (
    left.path === right.path &&
    left.size_bytes === right.size_bytes &&
    left.modified_at === right.modified_at
  );
}

function parseMachineNote(notes: string | null | undefined) {
  if (!notes) return null;
  try {
    const value: unknown = JSON.parse(notes);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

function postprocessEnabled(config: ProductionConfigEvidence): boolean | null {
  const postprocess = config.patch.postprocess;
  return isRecord(postprocess) && typeof postprocess.enabled === "boolean"
    ? postprocess.enabled
    : null;
}

function saveTrackingContractEnabled(
  config: ProductionConfigEvidence,
): boolean {
  const output = config.patch.output;
  return isRecord(output) && output.save_tracking_contract === true;
}

function acceptedTrialAttempt(trial: ProductionTrialState) {
  if (!productionTrialAcceptanceIsValid(trial)) {
    throw new TypeError("An accepted trial is required");
  }
  return { accepted: trial.accepted, attempt: trial.attempts.at(-1)! };
}

function calibrationConfirmation(calibration: ProductionCalibrationDraft) {
  if (!calibration.source_resolution) {
    throw new TypeError("Calibration source resolution is required");
  }
  const frames = [...calibration.confirmed_frames].sort(
    (left, right) => left.frame_index - right.frame_index,
  );
  return {
    source_resolution: [
      calibration.source_resolution.width,
      calibration.source_resolution.height,
    ] as [number, number],
    confirmed_sample_frames: frames.map((frame) => frame.frame_index) as [
      number,
      number,
      number,
    ],
    field_polygon: calibration.approved_polygon.map(
      ([x, y]) => [x, y] as [number, number],
    ),
    exclusion_polygons: calibration.exclusions.map((polygon) =>
      polygon.map(([x, y]) => [x, y] as [number, number]),
    ),
  };
}

function calibrationConfirmationMatches(
  request: CreateRunRequest,
  calibration: ProductionCalibrationDraft,
): boolean {
  try {
    return (
      canonicalJson(request.calibration_confirmation) ===
      canonicalJson(calibrationConfirmation(calibration))
    );
  } catch {
    return false;
  }
}

function contextIsReady(context: ProductionFullRunContext): boolean {
  if (
    !sourceSignature(context.source) ||
    !calibrationIsComplete(context.calibration, context.source.path) ||
    !isProductionTrialState(context.trial) ||
    !isProductionConfigEvidence(context.confirmed_config) ||
    !saveTrackingContractEnabled(context.confirmed_config)
  ) {
    return false;
  }
  if (
    !productionTrialMatchesContext(context.trial, {
      workflow_id: context.workflow_id,
      source: context.source,
      calibration: context.calibration,
    })
  ) {
    return false;
  }
  if (!productionTrialAcceptanceIsValid(context.trial)) return false;
  const accepted = context.trial.accepted;
  return Boolean(
    accepted &&
    context.confirmed_config.workflow_id === context.workflow_id &&
    context.confirmed_config.accepted_trial_run_id === accepted.run_id &&
    context.confirmed_config.trial_request_sha256 === accepted.request_sha256 &&
    context.confirmed_config.trial_intent_sha256 === accepted.intent_sha256 &&
    context.confirmed_config.calibration_digest ===
      context.calibration.polygon_digest &&
    sourcesMatch(context.confirmed_config.source_signature, context.source),
  );
}

export function createProductionFullRunState(): ProductionFullRunState {
  return {
    revision: 0,
    attempts: [],
    pending_submission: null,
    current_run_id: null,
  };
}

export function nextProductionFullRunGeneration(
  state: ProductionFullRunState,
): number {
  return (
    Math.max(
      0,
      state.pending_submission?.generation ?? 0,
      ...state.attempts.map((attempt) => attempt.generation),
    ) + 1
  );
}

export async function buildProductionFullRunSubmission(
  input: ProductionFullRunContext & {
    config_verification: ProductionConfigVerification;
    submission_id: string;
    output_id: string;
    generation: number;
    created_at: string;
    max_manual_review_windows?: number;
  },
): Promise<ProductionFullRunSubmission> {
  if (
    !nonEmpty(input.workflow_id) ||
    !nonEmpty(input.submission_id) ||
    !uuid(input.output_id) ||
    !positiveInteger(input.generation) ||
    !nonEmpty(input.created_at) ||
    !contextIsReady(input)
  ) {
    throw new TypeError("Production full-run prerequisites are invalid");
  }
  if (
    input.config_verification.status !== "verified" ||
    input.config_verification.sha256 !== input.confirmed_config.sha256
  ) {
    throw new TypeError("A fresh verified configuration is required");
  }
  const maxManualReviewWindows = input.max_manual_review_windows ?? 30;
  if (!positiveInteger(maxManualReviewWindows) || maxManualReviewWindows > 30) {
    throw new TypeError("Manual review window limit must be between 1 and 30");
  }
  const { accepted } = acceptedTrialAttempt(input.trial);
  const enablePostprocess = postprocessEnabled(input.confirmed_config);
  if (enablePostprocess === null) {
    throw new TypeError("Confirmed postprocess state is unavailable");
  }
  const expectedRunId = `production_full_${input.output_id}`;
  const note = {
    schema_version: PRODUCTION_FULL_RUN_METADATA_VERSION,
    purpose: "production_full",
    workflow_id: input.workflow_id,
    submission_id: input.submission_id,
    output_id: input.output_id,
    generation: input.generation,
    accepted_trial_run_id: accepted.run_id,
    accepted_trial_request_sha256: accepted.request_sha256,
    confirmed_config_name: input.confirmed_config.name,
    expected_config_sha256: input.confirmed_config.sha256,
    config_patch_sha256: input.confirmed_config.patch_sha256,
    calibration_digest: input.calibration.polygon_digest,
    source_signature: { ...input.source },
  };
  const request: CreateRunRequest = {
    config_name: input.confirmed_config.name,
    input_video: input.source.path,
    parent_run_id: accepted.run_id,
    output_dir_name: expectedRunId,
    enable_postprocess: enablePostprocess,
    enable_follow_cam: false,
    start_frame: 0,
    max_frames: null,
    pipeline_mode: "broadcast_hybrid",
    calibration_confirmation: calibrationConfirmation(input.calibration),
    quality_profile: "stable_broadcast",
    max_manual_review_windows: maxManualReviewWindows,
    notes: canonicalJson(note),
  };
  const requestSha256 = await sha256Text(canonicalJson(request));
  return {
    pending: {
      generation: input.generation,
      submission_id: input.submission_id,
      output_id: input.output_id,
      expected_run_id: expectedRunId,
      request,
      request_sha256: requestSha256,
      workflow_id: input.workflow_id,
      accepted_trial_run_id: accepted.run_id,
      accepted_trial_request_sha256: accepted.request_sha256,
      config_name: input.confirmed_config.name,
      config_sha256: input.confirmed_config.sha256,
      config_patch_sha256: input.confirmed_config.patch_sha256,
      calibration_digest: input.calibration.polygon_digest!,
      source_signature: { ...input.source },
      created_at: input.created_at,
    },
  };
}

function requestMatchesPending(
  pending: ProductionFullRunPendingSubmission,
): boolean {
  const request = pending.request;
  const note = parseMachineNote(request.notes);
  const calibration = request.calibration_confirmation;
  return Boolean(
    pending.expected_run_id === `production_full_${pending.output_id}` &&
    request.output_dir_name === pending.expected_run_id &&
    request.config_name === pending.config_name &&
    request.input_video === pending.source_signature.path &&
    request.parent_run_id === pending.accepted_trial_run_id &&
    request.start_frame === 0 &&
    request.max_frames === null &&
    request.enable_follow_cam === false &&
    typeof request.enable_postprocess === "boolean" &&
    request.pipeline_mode === "broadcast_hybrid" &&
    request.quality_profile === "stable_broadcast" &&
    positiveInteger(request.max_manual_review_windows) &&
    Number(request.max_manual_review_windows) <= 30 &&
    request.config_patch === undefined &&
    isRecord(calibration) &&
    Array.isArray(calibration.confirmed_sample_frames) &&
    calibration.confirmed_sample_frames.length === 3 &&
    note?.schema_version === PRODUCTION_FULL_RUN_METADATA_VERSION &&
    note.purpose === "production_full" &&
    note.workflow_id === pending.workflow_id &&
    note.submission_id === pending.submission_id &&
    note.output_id === pending.output_id &&
    note.generation === pending.generation &&
    note.accepted_trial_run_id === pending.accepted_trial_run_id &&
    note.accepted_trial_request_sha256 ===
      pending.accepted_trial_request_sha256 &&
    note.confirmed_config_name === pending.config_name &&
    note.expected_config_sha256 === pending.config_sha256 &&
    note.config_patch_sha256 === pending.config_patch_sha256 &&
    note.calibration_digest === pending.calibration_digest &&
    canonicalJson(note.source_signature) ===
      canonicalJson(pending.source_signature),
  );
}

async function requestSha256Matches(
  request: CreateRunRequest,
  expected: string,
): Promise<boolean> {
  try {
    return (await sha256Text(canonicalJson(request))) === expected;
  } catch {
    return false;
  }
}

export async function productionFullRunRequestHashesMatch(
  state: ProductionFullRunState,
): Promise<boolean> {
  if (!isProductionFullRunState(state)) return false;
  const snapshots = [
    ...(state.pending_submission
      ? [
          {
            request: state.pending_submission.request,
            request_sha256: state.pending_submission.request_sha256,
          },
        ]
      : []),
    ...state.attempts.map((attempt) => ({
      request: attempt.request,
      request_sha256: attempt.request_sha256,
    })),
  ];
  const matches = await Promise.all(
    snapshots.map((snapshot) =>
      requestSha256Matches(snapshot.request, snapshot.request_sha256),
    ),
  );
  return matches.every(Boolean);
}

export async function productionFullRunAuthoritativeContextMatches(input: {
  source: SourceSignature;
  trial: ProductionTrialState;
  input_catalog: InputCatalogResponse;
  accepted_trial_run: RunRecord;
  accepted_trial_artifacts: readonly ArtifactSummary[];
}): Promise<boolean> {
  if (
    !isProductionTrialState(input.trial) ||
    !productionTrialAcceptanceIsValid(input.trial)
  )
    return false;
  const accepted = input.trial.accepted;
  const attempt = input.trial.attempts.at(-1)!;

  const sourceMatches = (input.input_catalog.videos ?? []).filter(
    (candidate) => candidate.path === input.source.path,
  );
  if (
    sourceMatches.length !== 1 ||
    sourceMatches[0].size_bytes !== input.source.size_bytes ||
    sourceMatches[0].modified_at !== input.source.modified_at
  ) {
    return false;
  }

  if (
    accepted.request_sha256 !== attempt.request_sha256 ||
    accepted.readiness.request_sha256 !== attempt.request_sha256 ||
    !(await requestSha256Matches(attempt.request, attempt.request_sha256))
  ) {
    return false;
  }

  const run = input.accepted_trial_run;
  const expectedConfigName = materializedProductionTrialConfigName(
    attempt.request.config_name ?? input.trial.settings.base_config_name,
    attempt.run_id,
  );
  if (
    run.run_id !== attempt.run_id ||
    run.status !== "completed" ||
    run.source !== "api" ||
    run.notes !== attempt.request.notes ||
    run.config_name !== expectedConfigName ||
    run.input_video !== input.source.path ||
    run.input_video !== attempt.request.input_video ||
    run.parent_run_id !== attempt.request.parent_run_id ||
    run.modules_enabled?.postprocess !== attempt.request.enable_postprocess ||
    run.modules_enabled?.follow_cam !== attempt.request.enable_follow_cam
  ) {
    return false;
  }

  const contract = productionTrialArtifactContract({
    enable_postprocess: input.trial.settings.enable_postprocess,
    video_artifact_name: accepted.readiness.video_artifact_name,
    artifact_names: accepted.readiness.artifact_names,
  });
  if (contract.matches !== true) return false;
  return accepted.readiness.artifact_names.every((name) => {
    const matches = input.accepted_trial_artifacts.filter(
      (artifact) => artifact.name === name,
    );
    return (
      matches.length === 1 &&
      matches[0].exists &&
      typeof matches[0].size_bytes === "number" &&
      Number.isFinite(matches[0].size_bytes) &&
      matches[0].size_bytes > 0
    );
  });
}

export function setPendingProductionFullRun(
  state: ProductionFullRunState,
  pending: ProductionFullRunPendingSubmission,
): ProductionFullRunState {
  if (
    !isProductionFullRunState(state) ||
    !isProductionFullRunPendingSubmission(pending) ||
    state.pending_submission !== null ||
    pending.generation !== nextProductionFullRunGeneration(state)
  ) {
    throw new TypeError("Invalid production full-run pending transition");
  }
  return {
    ...state,
    revision: state.revision + 1,
    pending_submission: pending,
  };
}

export function clearPendingProductionFullRun(
  state: ProductionFullRunState,
  pending: ProductionFullRunPendingSubmission,
): ProductionFullRunState {
  if (
    !isProductionFullRunState(state) ||
    !isProductionFullRunPendingSubmission(pending) ||
    !state.pending_submission ||
    canonicalJson(state.pending_submission) !== canonicalJson(pending)
  ) {
    throw new TypeError("Pending production full run does not match");
  }
  return {
    ...state,
    revision: state.revision + 1,
    pending_submission: null,
  };
}

const ACTIVE_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "reconciling",
  "committing",
]);

function exactActiveRecoveryOperation(
  parent: RunRecord,
  recovery: ProductionFullRunRecoveryObservation | undefined,
): ProductionFullRunOperationObservation | null {
  const expectedKind =
    recovery?.state === "recomputing"
      ? "recompute"
      : recovery?.state === "rendering"
        ? "render"
        : null;
  const operation = recovery?.operation_run;
  if (
    !expectedKind ||
    !operation ||
    operation.parent_run_id !== parent.run_id ||
    (operation.broadcast?.parent_run_id != null &&
      operation.broadcast.parent_run_id !== parent.run_id) ||
    operation.broadcast?.operation !== expectedKind ||
    operation.source !== `broadcast_hybrid_${expectedKind}`
  ) {
    return null;
  }
  const operationStatus =
    operation.broadcast.operation_status ?? operation.status;
  if (
    !ACTIVE_OPERATION_STATUSES.has(operationStatus) &&
    operation.status !== "queued" &&
    operation.status !== "running"
  ) {
    return null;
  }
  return {
    run_id: operation.run_id,
    kind: expectedKind,
    status: operationStatus,
  };
}

function observationForRun(
  run: RunRecord,
  observedAt: string,
  recovery?: ProductionFullRunRecoveryObservation,
): ProductionFullRunObservation {
  const derived = deriveBroadcastWorkflowState(run);
  const lastOperation = run.broadcast?.last_operation;
  const recoveredOperation = exactActiveRecoveryOperation(run, recovery);
  return {
    run_status: run.status,
    workflow_state: recoveredOperation
      ? recovery!.state
      : derived.state === "setup"
        ? "failed"
        : derived.state,
    status_generation:
      typeof run.broadcast?.status_generation === "string"
        ? run.broadcast.status_generation
        : null,
    trajectory_generation_id:
      typeof run.broadcast?.trajectory_generation_id === "string"
        ? run.broadcast.trajectory_generation_id
        : null,
    operation:
      recoveredOperation ??
      (lastOperation
        ? {
            run_id: lastOperation.operation_run_id,
            kind: lastOperation.operation,
            status: lastOperation.status,
          }
        : null),
    observed_at: observedAt,
  };
}

function runMatchesPending(
  run: RunRecord,
  pending: ProductionFullRunPendingSubmission,
): boolean {
  return Boolean(
    run.run_id === pending.expected_run_id &&
    run.source === "broadcast_hybrid" &&
    run.notes === pending.request.notes &&
    run.config_name === pending.config_name &&
    run.input_video === pending.source_signature.path &&
    run.parent_run_id === pending.accepted_trial_run_id &&
    run.modules_enabled?.follow_cam === false &&
    run.modules_enabled?.postprocess === pending.request.enable_postprocess &&
    run.broadcast?.quality_profile === "stable_broadcast" &&
    run.broadcast?.max_manual_review_windows ===
      pending.request.max_manual_review_windows,
  );
}

export function appendProductionFullRunAttempt(
  state: ProductionFullRunState,
  input: {
    run: RunRecord;
    pending: ProductionFullRunPendingSubmission;
    observed_at: string;
  },
): ProductionFullRunState {
  if (
    !isProductionFullRunState(state) ||
    state.pending_submission !== input.pending ||
    !isProductionFullRunPendingSubmission(input.pending) ||
    !runMatchesPending(input.run, input.pending) ||
    state.attempts.some((attempt) => attempt.run_id === input.run.run_id)
  ) {
    throw new TypeError("Full-run response does not match its pending request");
  }
  const attempt: ProductionFullRunAttempt = {
    run_id: input.run.run_id,
    generation: input.pending.generation,
    submission_id: input.pending.submission_id,
    parent_trial_run_id: input.pending.accepted_trial_run_id,
    config_name: input.pending.config_name,
    config_sha256: input.pending.config_sha256,
    request_sha256: input.pending.request_sha256,
    request: input.pending.request,
    created_at: input.pending.created_at,
    last_observed: observationForRun(input.run, input.observed_at),
  };
  return {
    revision: state.revision + 1,
    attempts: [...state.attempts, attempt],
    pending_submission: null,
    current_run_id: attempt.run_id,
  };
}

export async function reconcilePendingProductionFullRun(
  state: ProductionFullRunState,
  input: {
    runs: readonly RunRecord[];
    observed_at: string;
  },
): Promise<ProductionFullRunState> {
  const pending = state.pending_submission;
  if (
    !pending ||
    !requestMatchesPending(pending) ||
    !(await requestSha256Matches(pending.request, pending.request_sha256))
  ) {
    return state;
  }
  const matches = input.runs.filter((run) => runMatchesPending(run, pending));
  return matches.length === 1
    ? appendProductionFullRunAttempt(state, {
        run: matches[0],
        pending,
        observed_at: input.observed_at,
      })
    : state;
}

export async function observeProductionFullRun(
  state: ProductionFullRunState,
  input: {
    run: RunRecord;
    observed_at: string;
    recovery?: ProductionFullRunRecoveryObservation;
  },
): Promise<ProductionFullRunState> {
  const index = state.attempts.findIndex(
    (attempt) => attempt.run_id === input.run.run_id,
  );
  if (index < 0) return state;
  const attempt = state.attempts[index];
  if (
    !(await requestSha256Matches(attempt.request, attempt.request_sha256)) ||
    !runMatchesPending(input.run, {
      generation: attempt.generation,
      submission_id: attempt.submission_id,
      output_id: String(
        parseMachineNote(attempt.request.notes)?.output_id ?? "",
      ),
      expected_run_id: attempt.run_id,
      request: attempt.request,
      request_sha256: attempt.request_sha256,
      workflow_id: String(
        parseMachineNote(attempt.request.notes)?.workflow_id ?? "",
      ),
      accepted_trial_run_id: attempt.parent_trial_run_id,
      accepted_trial_request_sha256: String(
        parseMachineNote(attempt.request.notes)
          ?.accepted_trial_request_sha256 ?? "",
      ),
      config_name: attempt.config_name,
      config_sha256: attempt.config_sha256,
      config_patch_sha256: String(
        parseMachineNote(attempt.request.notes)?.config_patch_sha256 ?? "",
      ),
      calibration_digest: String(
        parseMachineNote(attempt.request.notes)?.calibration_digest ?? "",
      ),
      source_signature: (parseMachineNote(attempt.request.notes)
        ?.source_signature ?? {}) as SourceSignature,
      created_at: attempt.created_at,
    })
  ) {
    return state;
  }
  const observation = observationForRun(
    input.run,
    input.observed_at,
    input.recovery,
  );
  const previous = attempt.last_observed;
  if (
    previous.run_status === observation.run_status &&
    previous.workflow_state === observation.workflow_state &&
    previous.status_generation === observation.status_generation &&
    previous.trajectory_generation_id ===
      observation.trajectory_generation_id &&
    canonicalJson(previous.operation) === canonicalJson(observation.operation)
  ) {
    return state;
  }
  const attempts = [...state.attempts];
  attempts[index] = { ...attempt, last_observed: observation };
  return { ...state, revision: state.revision + 1, attempts };
}

function validOperation(
  value: unknown,
): value is ProductionFullRunOperationObservation {
  return (
    isRecord(value) &&
    nonEmpty(value.run_id) &&
    (value.kind === "recompute" || value.kind === "render") &&
    nonEmpty(value.status)
  );
}

function validObservation(
  value: unknown,
): value is ProductionFullRunObservation {
  if (!isRecord(value)) return false;
  const statuses = new Set([
    "tracking",
    "needs_review",
    "recomputing",
    "trajectory_ready",
    "rendering",
    "ready",
    "failed",
    "cancelled",
  ]);
  return (
    nonEmpty(value.run_status) &&
    nonEmpty(value.workflow_state) &&
    statuses.has(value.workflow_state) &&
    (value.status_generation === null ||
      sha256String(value.status_generation)) &&
    (value.trajectory_generation_id === null ||
      (typeof value.trajectory_generation_id === "string" &&
        /^trajectory-[0-9a-f]{24}$/.test(value.trajectory_generation_id))) &&
    (value.operation === null || validOperation(value.operation)) &&
    nonEmpty(value.observed_at)
  );
}

export function isProductionFullRunPendingSubmission(
  value: unknown,
): value is ProductionFullRunPendingSubmission {
  return Boolean(
    isRecord(value) &&
    positiveInteger(value.generation) &&
    nonEmpty(value.submission_id) &&
    uuid(value.output_id) &&
    value.expected_run_id === `production_full_${value.output_id}` &&
    isRecord(value.request) &&
    sha256String(value.request_sha256) &&
    nonEmpty(value.workflow_id) &&
    nonEmpty(value.accepted_trial_run_id) &&
    sha256String(value.accepted_trial_request_sha256) &&
    nonEmpty(value.config_name) &&
    sha256String(value.config_sha256) &&
    sha256String(value.config_patch_sha256) &&
    sha256String(value.calibration_digest) &&
    sourceSignature(value.source_signature) &&
    nonEmpty(value.created_at) &&
    requestMatchesPending(
      value as unknown as ProductionFullRunPendingSubmission,
    ),
  );
}

function validAttempt(value: unknown): value is ProductionFullRunAttempt {
  if (
    !isRecord(value) ||
    !nonEmpty(value.run_id) ||
    !positiveInteger(value.generation) ||
    !nonEmpty(value.submission_id) ||
    !nonEmpty(value.parent_trial_run_id) ||
    !nonEmpty(value.config_name) ||
    !sha256String(value.config_sha256) ||
    !sha256String(value.request_sha256) ||
    !isRecord(value.request) ||
    !nonEmpty(value.created_at) ||
    !validObservation(value.last_observed)
  ) {
    return false;
  }
  const note = parseMachineNote((value.request as CreateRunRequest).notes);
  if (!note || !sourceSignature(note.source_signature)) return false;
  return isProductionFullRunPendingSubmission({
    generation: value.generation,
    submission_id: value.submission_id,
    output_id: note.output_id,
    expected_run_id: value.run_id,
    request: value.request,
    request_sha256: value.request_sha256,
    workflow_id: note.workflow_id,
    accepted_trial_run_id: value.parent_trial_run_id,
    accepted_trial_request_sha256: note.accepted_trial_request_sha256,
    config_name: value.config_name,
    config_sha256: value.config_sha256,
    config_patch_sha256: note.config_patch_sha256,
    calibration_digest: note.calibration_digest,
    source_signature: note.source_signature,
    created_at: value.created_at,
  });
}

export function isProductionFullRunState(
  value: unknown,
): value is ProductionFullRunState {
  if (
    !isRecord(value) ||
    !nonNegativeInteger(value.revision) ||
    !Array.isArray(value.attempts) ||
    !value.attempts.every(validAttempt) ||
    !(
      value.pending_submission === null ||
      isProductionFullRunPendingSubmission(value.pending_submission)
    ) ||
    !(value.current_run_id === null || nonEmpty(value.current_run_id))
  ) {
    return false;
  }
  const attempts = value.attempts as ProductionFullRunAttempt[];
  if (
    new Set(attempts.map((attempt) => attempt.run_id)).size !== attempts.length
  ) {
    return false;
  }
  if (
    new Set(attempts.map((attempt) => attempt.generation)).size !==
    attempts.length
  ) {
    return false;
  }
  if (
    value.current_run_id !== null &&
    !attempts.some((attempt) => attempt.run_id === value.current_run_id)
  ) {
    return false;
  }
  const pending =
    value.pending_submission as ProductionFullRunPendingSubmission | null;
  if (
    pending &&
    attempts.some(
      (attempt) =>
        attempt.run_id === pending.expected_run_id ||
        attempt.generation === pending.generation,
    )
  ) {
    return false;
  }
  return true;
}

function pendingMatchesContext(
  pending: ProductionFullRunPendingSubmission,
  context: ProductionFullRunContext,
): boolean {
  const accepted = context.trial.accepted;
  return Boolean(
    accepted &&
    pending.workflow_id === context.workflow_id &&
    pending.accepted_trial_run_id === accepted.run_id &&
    pending.accepted_trial_request_sha256 === accepted.request_sha256 &&
    pending.config_name === context.confirmed_config.name &&
    pending.config_sha256 === context.confirmed_config.sha256 &&
    pending.config_patch_sha256 === context.confirmed_config.patch_sha256 &&
    pending.calibration_digest === context.calibration.polygon_digest &&
    sourcesMatch(pending.source_signature, context.source) &&
    requestMatchesPending(pending) &&
    calibrationConfirmationMatches(pending.request, context.calibration) &&
    pending.request.enable_postprocess ===
      postprocessEnabled(context.confirmed_config),
  );
}

export function productionFullRunMatchesContext(
  state: ProductionFullRunState,
  context: ProductionFullRunContext,
): boolean {
  if (!isProductionFullRunState(state) || !contextIsReady(context))
    return false;
  if (
    state.pending_submission &&
    !pendingMatchesContext(state.pending_submission, context)
  ) {
    return false;
  }
  return state.attempts.every((attempt) => {
    const note = parseMachineNote(attempt.request.notes);
    if (!note || !sourceSignature(note.source_signature)) return false;
    return pendingMatchesContext(
      {
        generation: attempt.generation,
        submission_id: attempt.submission_id,
        output_id: String(note.output_id ?? ""),
        expected_run_id: attempt.run_id,
        request: attempt.request,
        request_sha256: attempt.request_sha256,
        workflow_id: String(note.workflow_id ?? ""),
        accepted_trial_run_id: attempt.parent_trial_run_id,
        accepted_trial_request_sha256: String(
          note.accepted_trial_request_sha256 ?? "",
        ),
        config_name: attempt.config_name,
        config_sha256: attempt.config_sha256,
        config_patch_sha256: String(note.config_patch_sha256 ?? ""),
        calibration_digest: String(note.calibration_digest ?? ""),
        source_signature: note.source_signature,
        created_at: attempt.created_at,
      },
      context,
    );
  });
}

export function productionFullRunRequiresStop(
  state: ProductionFullRunState | null,
): boolean {
  if (!state) return false;
  if (state.pending_submission) return true;
  const current = state.attempts.find(
    (attempt) => attempt.run_id === state.current_run_id,
  );
  return Boolean(
    current &&
    (current.last_observed.workflow_state === "tracking" ||
      current.last_observed.workflow_state === "recomputing" ||
      current.last_observed.workflow_state === "rendering"),
  );
}
