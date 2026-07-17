import type {
  ConfigDetail,
  DeriveConfigRequest,
} from "@workspace/api-client-react";

import {
  calibrationIsComplete,
  type ProductionCalibrationDraft,
} from "./productionCalibration";
import {
  buildProductionTrialIntent,
  canonicalJson,
  deepMergeProductionPatch,
  isProductionTrialState,
  productionTrialArtifactContract,
  productionTrialMatchesContext,
  sha256Text,
  type ProductionTrialState,
} from "./productionTrial";
import type { SourceSignature } from "./productionWorkflow";

export const PRODUCTION_CONFIG_METADATA_VERSION = "1.0" as const;

export interface ProductionPendingConfigConfirmation {
  generation: number;
  output_id: string;
  output_name: string;
  request: DeriveConfigRequest;
  persistent_patch: Record<string, unknown>;
  patch_sha256: string;
  trial_patch_sha256: string;
  workflow_id: string;
  base_config_name: string;
  accepted_trial_run_id: string;
  trial_intent_sha256: string;
  trial_request_sha256: string;
  calibration_digest: string;
  source_signature: SourceSignature;
  confirmed_at: string;
}

export interface ProductionConfigEvidence {
  name: string;
  sha256: string;
  base_config_name: string;
  patch: Record<string, unknown>;
  patch_sha256: string;
  trial_patch_sha256: string;
  workflow_id: string;
  accepted_trial_run_id: string;
  trial_intent_sha256: string;
  trial_request_sha256: string;
  calibration_digest: string;
  source_signature: SourceSignature;
  confirmed_at: string;
}

export type ProductionConfigVerification =
  | { status: "verified"; sha256: string }
  | {
      status:
        | "missing"
        | "name_mismatch"
        | "digest_mismatch"
        | "lineage_mismatch"
        | "unverifiable";
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function sha256String(value: unknown): value is string {
  return typeof value === "string" && /^[a-f\d]{64}$/i.test(value);
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

function cloneJsonObject(
  value: Record<string, unknown>,
): Record<string, unknown> {
  return JSON.parse(canonicalJson(value)) as Record<string, unknown>;
}

function clonePolygon(points: [number, number][]): [number, number][] {
  return points.map(([x, y]) => [x, y]);
}

function deleteLeaf(target: Record<string, unknown>, path: readonly string[]) {
  let parent = target;
  const ancestors: Array<[Record<string, unknown>, string]> = [];
  for (const segment of path.slice(0, -1)) {
    const child = parent[segment];
    if (!isRecord(child)) return;
    ancestors.push([parent, segment]);
    parent = child;
  }
  delete parent[path.at(-1) ?? ""];
  for (const [owner, key] of ancestors.reverse()) {
    const child = owner[key];
    if (isRecord(child) && Object.keys(child).length === 0) delete owner[key];
  }
}

function recursiveSubset(expected: unknown, actual: unknown): boolean {
  if (isRecord(expected)) {
    return (
      isRecord(actual) &&
      Object.entries(expected).every(([key, value]) =>
        recursiveSubset(value, actual[key]),
      )
    );
  }
  if (Array.isArray(expected)) {
    return (
      Array.isArray(actual) &&
      expected.length === actual.length &&
      expected.every((value, index) => recursiveSubset(value, actual[index]))
    );
  }
  return Object.is(expected, actual);
}

export function expectedProductionConfigName(outputName: string): string {
  return `generated/${outputName}`;
}

function productionOutputName(workflowId: string, outputId: string): string {
  return `production_${workflowId}_${outputId}.yaml`;
}

function canonicalEvidenceName(name: unknown, workflowId: unknown): boolean {
  if (!nonEmpty(name) || !safeWorkflowName(String(workflowId))) return false;
  const prefix = `generated/production_${String(workflowId)}_`;
  return (
    name.startsWith(prefix) &&
    name.endsWith(".yaml") &&
    uuid(name.slice(prefix.length, -".yaml".length))
  );
}

function polygonBounds(
  points: [number, number][],
): [number, number, number, number] {
  return [
    Math.min(...points.map(([x]) => x)),
    Math.min(...points.map(([, y]) => y)),
    Math.max(...points.map(([x]) => x)),
    Math.max(...points.map(([, y]) => y)),
  ];
}

function safeWorkflowName(value: string): boolean {
  return /^[A-Za-z0-9_-]+$/.test(value);
}

function uuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}

function acceptedAttempt(trial: ProductionTrialState) {
  if (!isProductionTrialState(trial) || !trial.accepted) {
    throw new TypeError("An accepted production trial is required");
  }
  const attempt = trial.attempts.find(
    (candidate) => candidate.run_id === trial.accepted?.run_id,
  );
  if (!attempt || attempt.last_observed.status !== "completed") {
    throw new TypeError("Accepted trial attempt is not completed");
  }
  return { accepted: trial.accepted, attempt };
}

export async function buildProductionConfigConfirmation(input: {
  workflow_id: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  trial: ProductionTrialState;
  output_id: string;
  generation: number;
  confirmed_at: string;
}): Promise<ProductionPendingConfigConfirmation> {
  if (!safeWorkflowName(input.workflow_id) || !uuid(input.output_id)) {
    throw new TypeError("A safe workflow name and fresh UUID are required");
  }
  if (!Number.isInteger(input.generation) || input.generation <= 0) {
    throw new TypeError("generation must be a positive integer");
  }
  if (
    !sourceSignature(input.source) ||
    !sha256String(input.calibration.polygon_digest) ||
    !calibrationIsComplete(input.calibration, input.source.path)
  ) {
    throw new TypeError("Valid source and calibration evidence are required");
  }
  const { accepted, attempt } = acceptedAttempt(input.trial);
  if (
    !productionTrialArtifactContract({
      enable_postprocess: Boolean(attempt.request.enable_postprocess),
      video_artifact_name: accepted.readiness.video_artifact_name,
      artifact_names: accepted.readiness.artifact_names,
    }).matches
  ) {
    throw new TypeError("Accepted trial evidence artifacts are incomplete");
  }
  if (
    !productionTrialMatchesContext(input.trial, {
      workflow_id: input.workflow_id,
      source: input.source,
      calibration: input.calibration,
    })
  ) {
    throw new TypeError("Accepted trial does not match the production context");
  }
  const currentIntent = buildProductionTrialIntent({
    workflow_id: input.workflow_id,
    source: input.source,
    calibration: input.calibration,
    settings: input.trial.settings,
  });
  if (
    (await sha256Text(canonicalJson(currentIntent))) !== accepted.intent_sha256
  ) {
    throw new TypeError("Accepted trial intent is stale");
  }
  const trialPatch = isRecord(attempt.request.config_patch)
    ? cloneJsonObject(attempt.request.config_patch)
    : {};
  const tuningPatch = cloneJsonObject(trialPatch);
  delete tuningPatch.output;
  delete tuningPatch.runtime;
  delete tuningPatch.follow_cam;
  for (const trialOnlyLeaf of [["metadata", "production_workflow"]] as const) {
    deleteLeaf(tuningPatch, trialOnlyLeaf);
  }
  const points = clonePolygon(input.calibration.approved_polygon);
  const exclusions = input.calibration.exclusions.map(clonePolygon);
  const persistent_patch = deepMergeProductionPatch(tuningPatch, {
    input_video: input.source.path,
    filtering: { roi: polygonBounds(points) },
    scene_bias: {
      enabled: true,
      ground_zones: [{ name: "production_field", points }],
      negative_rois: exclusions.map((negativePoints, index) => ({
        name: `production_exclusion_${index + 1}`,
        points: negativePoints,
      })),
    },
    postprocess: {
      enabled: Boolean(attempt.request.enable_postprocess),
    },
    runtime: { start_frame: 0, max_frames: null },
    follow_cam: { enabled: false },
    output: { save_tracking_contract: true },
  });
  const patch_sha256 = await sha256Text(canonicalJson(persistent_patch));
  const trial_patch_sha256 = await sha256Text(canonicalJson(trialPatch));
  const output_name = productionOutputName(input.workflow_id, input.output_id);
  const workflowMetadata = {
    schema_version: PRODUCTION_CONFIG_METADATA_VERSION,
    workflow_id: input.workflow_id,
    base_config_name: input.trial.settings.base_config_name,
    accepted_trial_run_id: accepted.run_id,
    calibration_digest: input.calibration.polygon_digest,
    source_signature: { ...input.source },
    trial_intent_sha256: accepted.intent_sha256,
    trial_request_sha256: accepted.request_sha256,
    trial_patch_sha256,
    patch_sha256,
    confirmed_at: input.confirmed_at,
  };
  const request: DeriveConfigRequest = {
    base_config_name: input.trial.settings.base_config_name,
    output_name,
    patch: deepMergeProductionPatch(persistent_patch, {
      metadata: { production_workflow: workflowMetadata },
    }),
  };
  return {
    generation: input.generation,
    output_id: input.output_id,
    output_name,
    request,
    persistent_patch,
    patch_sha256,
    trial_patch_sha256,
    workflow_id: input.workflow_id,
    base_config_name: input.trial.settings.base_config_name,
    accepted_trial_run_id: accepted.run_id,
    trial_intent_sha256: accepted.intent_sha256,
    trial_request_sha256: accepted.request_sha256,
    calibration_digest: input.calibration.polygon_digest,
    source_signature: { ...input.source },
    confirmed_at: input.confirmed_at,
  };
}

function lineageMatches(
  expected: Omit<ProductionConfigEvidence, "name" | "sha256" | "patch">,
  raw: Record<string, unknown>,
): boolean {
  const metadata = raw.metadata;
  const workflow = isRecord(metadata) ? metadata.production_workflow : null;
  return Boolean(
    isRecord(workflow) &&
    workflow.schema_version === PRODUCTION_CONFIG_METADATA_VERSION &&
    workflow.workflow_id === expected.workflow_id &&
    workflow.base_config_name === expected.base_config_name &&
    workflow.accepted_trial_run_id === expected.accepted_trial_run_id &&
    workflow.calibration_digest === expected.calibration_digest &&
    workflow.trial_intent_sha256 === expected.trial_intent_sha256 &&
    workflow.trial_request_sha256 === expected.trial_request_sha256 &&
    workflow.trial_patch_sha256 === expected.trial_patch_sha256 &&
    workflow.patch_sha256 === expected.patch_sha256 &&
    workflow.confirmed_at === expected.confirmed_at &&
    canonicalJson(workflow.source_signature) ===
      canonicalJson(expected.source_signature),
  );
}

function validPatchPolygon(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.length >= 3 &&
    value.every(
      (point) =>
        Array.isArray(point) &&
        point.length === 2 &&
        point.every(
          (coordinate) =>
            typeof coordinate === "number" && Number.isFinite(coordinate),
        ),
    )
  );
}

function forcedExecutionObjectsAreExact(
  patch: Record<string, unknown>,
): boolean {
  const runtime = patch.runtime;
  const followCam = patch.follow_cam;
  const output = patch.output;
  return Boolean(
    isRecord(runtime) &&
    Object.keys(runtime).length === 2 &&
    runtime.start_frame === 0 &&
    runtime.max_frames === null &&
    isRecord(followCam) &&
    Object.keys(followCam).length === 1 &&
    followCam.enabled === false &&
    isRecord(output) &&
    Object.keys(output).length === 1 &&
    output.save_tracking_contract === true,
  );
}

function mergedExecutionConfigMatchesForcedValues(
  patch: Record<string, unknown>,
): boolean {
  const runtime = patch.runtime;
  const followCam = patch.follow_cam;
  const output = patch.output;
  return Boolean(
    isRecord(runtime) &&
    runtime.start_frame === 0 &&
    runtime.max_frames === null &&
    !Object.prototype.hasOwnProperty.call(runtime, "trial_stride") &&
    isRecord(followCam) &&
    followCam.enabled === false &&
    !Object.prototype.hasOwnProperty.call(followCam, "legacy_render") &&
    !Object.prototype.hasOwnProperty.call(followCam, "preview_codec") &&
    isRecord(output) &&
    output.save_tracking_contract === true &&
    !Object.prototype.hasOwnProperty.call(output, "dir"),
  );
}

function executionPatchMatchesLineage(
  expected: Omit<ProductionConfigEvidence, "name" | "sha256" | "patch">,
  patch: unknown,
): patch is Record<string, unknown> {
  if (
    !isRecord(patch) ||
    patch.input_video !== expected.source_signature.path
  ) {
    return false;
  }
  const filtering = patch.filtering;
  const sceneBias = patch.scene_bias;
  const postprocess = patch.postprocess;
  if (
    !isRecord(filtering) ||
    !Array.isArray(filtering.roi) ||
    filtering.roi.length !== 4 ||
    !filtering.roi.every(
      (coordinate) =>
        typeof coordinate === "number" && Number.isFinite(coordinate),
    ) ||
    !isRecord(sceneBias) ||
    sceneBias.enabled !== true ||
    !Array.isArray(sceneBias.ground_zones) ||
    !sceneBias.ground_zones.some(
      (zone) =>
        isRecord(zone) &&
        zone.name === "production_field" &&
        validPatchPolygon(zone.points),
    ) ||
    !Array.isArray(sceneBias.negative_rois) ||
    !sceneBias.negative_rois.every(
      (zone) => isRecord(zone) && validPatchPolygon(zone.points),
    ) ||
    !isRecord(postprocess) ||
    typeof postprocess.enabled !== "boolean" ||
    !forcedExecutionObjectsAreExact(patch)
  ) {
    return false;
  }
  return lineageMatches(expected, patch);
}

function configDetail(value: unknown): value is ConfigDetail {
  return (
    isRecord(value) &&
    nonEmpty(value.name) &&
    nonEmpty(value.text) &&
    isRecord(value.raw)
  );
}

export async function finalizeProductionConfigConfirmation(
  pending: ProductionPendingConfigConfirmation,
  detail: ConfigDetail,
): Promise<ProductionConfigEvidence> {
  if (
    !isProductionPendingConfigConfirmation(pending) ||
    !configDetail(detail)
  ) {
    throw new TypeError("Configuration confirmation response is invalid");
  }
  if (detail.name !== expectedProductionConfigName(pending.output_name)) {
    throw new Error("Derived configuration name does not match the request");
  }
  const expected = {
    patch_sha256: pending.patch_sha256,
    trial_patch_sha256: pending.trial_patch_sha256,
    workflow_id: pending.workflow_id,
    base_config_name: pending.base_config_name,
    accepted_trial_run_id: pending.accepted_trial_run_id,
    trial_intent_sha256: pending.trial_intent_sha256,
    trial_request_sha256: pending.trial_request_sha256,
    calibration_digest: pending.calibration_digest,
    source_signature: pending.source_signature,
    confirmed_at: pending.confirmed_at,
  };
  if (
    !mergedExecutionConfigMatchesForcedValues(detail.raw) ||
    !lineageMatches(expected, detail.raw)
  ) {
    throw new Error("Derived configuration lineage does not match the request");
  }
  if (!recursiveSubset(pending.request.patch ?? {}, detail.raw)) {
    throw new Error(
      "Derived configuration raw patch does not match the request",
    );
  }
  return {
    name: detail.name,
    sha256: await sha256Text(detail.text),
    patch: cloneJsonObject(pending.request.patch ?? {}),
    ...expected,
  };
}

export async function verifyProductionConfigDetail(
  expected: ProductionConfigEvidence,
  detail: ConfigDetail | null,
): Promise<ProductionConfigVerification> {
  if (!detail) return { status: "missing" };
  if (!isProductionConfigEvidence(expected) || !configDetail(detail)) {
    return { status: "unverifiable" };
  }
  if (detail.name !== expected.name) return { status: "name_mismatch" };
  const digest = await sha256Text(detail.text);
  if (digest !== expected.sha256) return { status: "digest_mismatch" };
  if (
    !mergedExecutionConfigMatchesForcedValues(detail.raw) ||
    !lineageMatches(expected, detail.raw) ||
    !recursiveSubset(expected.patch, detail.raw)
  ) {
    return { status: "lineage_mismatch" };
  }
  return { status: "verified", sha256: digest };
}

export function isProductionPendingConfigConfirmation(
  value: unknown,
): value is ProductionPendingConfigConfirmation {
  if (
    !isRecord(value) ||
    !(
      Number.isInteger(value.generation) &&
      Number(value.generation) > 0 &&
      uuid(String(value.output_id)) &&
      value.output_name ===
        productionOutputName(
          String(value.workflow_id),
          String(value.output_id),
        ) &&
      isRecord(value.request) &&
      value.request.output_name === value.output_name &&
      isRecord(value.persistent_patch) &&
      sha256String(value.patch_sha256) &&
      sha256String(value.trial_patch_sha256) &&
      safeWorkflowName(String(value.workflow_id)) &&
      nonEmpty(value.base_config_name) &&
      nonEmpty(value.accepted_trial_run_id) &&
      sha256String(value.trial_intent_sha256) &&
      sha256String(value.trial_request_sha256) &&
      sha256String(value.calibration_digest) &&
      sourceSignature(value.source_signature) &&
      nonEmpty(value.confirmed_at)
    )
  )
    return false;
  const request = value.request as Record<string, unknown>;
  return (
    request.base_config_name === value.base_config_name &&
    isRecord(request.patch) &&
    forcedExecutionObjectsAreExact(value.persistent_patch) &&
    recursiveSubset(value.persistent_patch, request.patch) &&
    executionPatchMatchesLineage(
      value as unknown as Omit<
        ProductionConfigEvidence,
        "name" | "sha256" | "patch"
      >,
      request.patch,
    )
  );
}

export function isProductionConfigEvidence(
  value: unknown,
): value is ProductionConfigEvidence {
  if (
    !isRecord(value) ||
    !(
      canonicalEvidenceName(value.name, value.workflow_id) &&
      sha256String(value.sha256) &&
      nonEmpty(value.base_config_name) &&
      isRecord(value.patch) &&
      sha256String(value.patch_sha256) &&
      sha256String(value.trial_patch_sha256) &&
      safeWorkflowName(String(value.workflow_id)) &&
      nonEmpty(value.accepted_trial_run_id) &&
      sha256String(value.trial_intent_sha256) &&
      sha256String(value.trial_request_sha256) &&
      sha256String(value.calibration_digest) &&
      sourceSignature(value.source_signature) &&
      nonEmpty(value.confirmed_at)
    )
  )
    return false;
  return executionPatchMatchesLineage(
    value as unknown as Omit<
      ProductionConfigEvidence,
      "name" | "sha256" | "patch"
    >,
    value.patch,
  );
}
