import type {
  ArtifactSummary,
  BallAuditReport,
  CreateRunRequest,
  RunRecord,
  TrialNumericObservation,
  TrialSignalGateV2,
  TrialTuningAction as ApiTrialTuningAction,
  TrialTuningControl as ApiTrialTuningControl,
} from "@workspace/api-client-react";

import {
  calibrationIsComplete,
  type ProductionCalibrationDraft,
} from "./productionCalibration";
import { validatePolygon, type FieldPoint } from "./fieldGeometry";
import type { SourceSignature } from "./productionWorkflow";

export const PRODUCTION_TRIAL_METADATA_VERSION = "1.0" as const;
export const PRODUCTION_TUNING_PATCH_VERSION = "1.0" as const;

export type ProductionTuningValue = number | string | boolean | string[];

export type ProductionTrialTuningControl = ApiTrialTuningControl;
export type ProductionTrialTuningAction = ApiTrialTuningAction;

export interface ProductionTrialTuningSchema {
  schema_version: "1.0";
  patch_schema_version: "1.0";
  controls: ProductionTrialTuningControl[];
  actions: ProductionTrialTuningAction[];
}

export type TrialDiagnosticStatus = TrialNumericObservation["status"];
export type TrialDiagnosticObservation = TrialNumericObservation;
export type ProductionTrialDiagnostics = TrialSignalGateV2["diagnostics"];
export type ProductionTrialDetectionStages = NonNullable<
  TrialSignalGateV2["stage_counts"]
>;
export type ProductionTrialSignalGateV2 = TrialSignalGateV2;

export interface ProductionTuningVersionSnapshot {
  version_id: string;
  created_at: string;
  values_sha256: string;
  values: Record<string, ProductionTuningValue>;
}

export interface ProductionTuningVersion extends ProductionTuningVersionSnapshot {
  schema_version: typeof PRODUCTION_TUNING_PATCH_VERSION;
  parent_version_id: string | null;
  history: ProductionTuningVersionSnapshot[];
}

export interface ProductionTuningDiff {
  path: string;
  previous_value: ProductionTuningValue | null;
  next_value: ProductionTuningValue;
}

export interface ProductionTrialSettings {
  base_config_name: string;
  start_frame: number;
  max_frames: number;
  enable_postprocess: boolean;
  enable_follow_cam: boolean;
  tuning_patch: Record<string, unknown>;
}

export interface ProductionTrialObservation {
  status: RunRecord["status"];
  observed_at: string;
  evidence_generation: string | null;
}

export interface ProductionTrialAttempt {
  run_id: string;
  generation: number;
  submission_id: string;
  parent_run_id: string | null;
  intent_sha256: string;
  request_sha256: string;
  request: CreateRunRequest;
  created_at: string;
  last_observed: ProductionTrialObservation;
}

export interface ProductionTrialPendingSubmission {
  generation: number;
  submission_id: string;
  output_id: string;
  intent_sha256: string;
  request_sha256: string;
  request: CreateRunRequest;
  created_at: string;
}

export interface ProductionTrialReadinessSummary {
  run_id: string;
  request_sha256: string;
  evidence_generation: string;
  verified_at: string;
  video_artifact_name: string;
  artifact_names: string[];
  quality: ProductionTrialQualitySignals;
  operator_visual_confirmation?: ProductionTrialVisualConfirmation;
}

export interface ProductionTrialVisualConfirmation {
  confirmed: true;
  confirmed_at: string;
  evidence_generation: string;
  threshold_profile_sha256: string;
}

export function productionTrialArtifactContract(input: {
  enable_postprocess: boolean;
  video_artifact_name: string;
  artifact_names?: readonly unknown[];
}): { required_names: string[]; matches: boolean | null } {
  const requiredNames = [
    "run_manifest.json",
    "metrics_report.json",
    "ball_track.csv",
    "ball_audit.json",
    ...(input.enable_postprocess ? ["ball_track.cleaned.csv"] : []),
    input.video_artifact_name,
  ];
  if (input.artifact_names === undefined) {
    return { required_names: requiredNames, matches: null };
  }
  const expected = new Set(requiredNames);
  const actual = new Set(input.artifact_names);
  const matches =
    nonEmpty(input.video_artifact_name) &&
    expected.size === requiredNames.length &&
    input.artifact_names.length === requiredNames.length &&
    actual.size === input.artifact_names.length &&
    input.artifact_names.every(nonEmpty) &&
    requiredNames.every((name) => actual.has(name));
  return { required_names: requiredNames, matches };
}

export interface ProductionTrialAcceptance {
  run_id: string;
  intent_sha256: string;
  request_sha256: string;
  accepted_at: string;
  readiness: ProductionTrialReadinessSummary;
}

export interface ProductionTrialState {
  settings: ProductionTrialSettings;
  attempts: ProductionTrialAttempt[];
  active_run_id: string | null;
  pending_submission: ProductionTrialPendingSubmission | null;
  accepted: ProductionTrialAcceptance | null;
}

export interface ProductionTrialIntent {
  schema_version: typeof PRODUCTION_TRIAL_METADATA_VERSION;
  purpose: "production_trial_intent";
  workflow_id: string;
  source_signature: SourceSignature;
  calibration_digest: string;
  approved_polygon: FieldPoint[];
  exclusions: FieldPoint[][];
  base_config_name: string;
  start_frame: number;
  max_frames: number;
  enable_postprocess: boolean;
  enable_follow_cam: boolean;
  tuning_patch: Record<string, unknown>;
}

export interface ProductionTrialSubmission {
  pending: ProductionTrialPendingSubmission;
  intent: ProductionTrialIntent;
}

export interface ProductionTrialSubmissionLineage {
  state: ProductionTrialState;
  parent_run_id: string | null;
  generation: number;
  legacy_restart_run_id: string | null;
  base_config_locked: boolean;
}

export interface ProductionTrialQualitySignals {
  frame_count: number;
  detected: number;
  predicted: number;
  lost: number;
  detected_ratio: number;
  predicted_ratio: number;
  lost_ratio: number;
  longest_lost_streak: number | null;
  false_positive_island_count: number | null;
  max_step_px: number | null;
  audit_tracklet_count: number;
  audit_suspicious_tracklet_count: number;
  audit_review_event_count: number;
  audit_lost_gap_count: number;
  quality_gate_status: string | null;
  trial_signal_gate_v2?: ProductionTrialSignalGateV2 | null;
}

export type ProductionTrialEvidenceResult =
  | {
      ready: true;
      video: ArtifactSummary;
      required_artifacts: ArtifactSummary[];
      quality: ProductionTrialQualitySignals;
    }
  | { ready: false; reasons: string[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function nonNegativeInteger(value: unknown): value is number {
  return finite(value) && Number.isInteger(value) && value >= 0;
}

function positiveInteger(value: unknown): value is number {
  return nonNegativeInteger(value) && value > 0;
}

function sha256String(value: unknown): value is string {
  return typeof value === "string" && /^[a-f\d]{64}$/i.test(value);
}

function clonePoint([x, y]: FieldPoint): FieldPoint {
  return [x, y];
}

function clonePolygon(points: FieldPoint[]): FieldPoint[] {
  return points.map(clonePoint);
}

function cloneJsonObject(
  value: Record<string, unknown>,
): Record<string, unknown> {
  return JSON.parse(canonicalJson(value)) as Record<string, unknown>;
}

export function deepMergeProductionPatch(
  base: Record<string, unknown>,
  override: Record<string, unknown>,
): Record<string, unknown> {
  const result = cloneJsonObject(base);
  for (const [key, value] of Object.entries(override)) {
    const current = result[key];
    result[key] =
      isRecord(current) && isRecord(value)
        ? deepMergeProductionPatch(current, value)
        : Array.isArray(value)
          ? value.map((item) => canonicalize(item, `$.${key}`))
          : canonicalize(value, `$.${key}`);
  }
  return result;
}

function canonicalize(value: unknown, path: string): unknown {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError(`${path} must be finite`);
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => canonicalize(item, `${path}[${index}]`));
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`${path} must be a plain JSON object`);
    }
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item === undefined)
        throw new TypeError(`${path}.${key} is undefined`);
      result[key] = canonicalize(item, `${path}.${key}`);
    }
    return result;
  }
  throw new TypeError(`${path} is not JSON-serializable`);
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value, "$"));
}

export async function sha256Text(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function valueAtPath(value: unknown, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (current, key) => (isRecord(current) ? current[key] : undefined),
      value,
    );
}

function setValueAtPath(
  target: Record<string, unknown>,
  path: string,
  value: unknown,
) {
  const keys = path.split(".");
  let current = target;
  for (const key of keys.slice(0, -1)) {
    const nested = current[key];
    if (!isRecord(nested)) current[key] = {};
    current = current[key] as Record<string, unknown>;
  }
  current[keys.at(-1)!] = value;
}

function validStoredTuningValue(
  value: unknown,
): value is ProductionTuningValue {
  return (
    typeof value === "string" ||
    typeof value === "boolean" ||
    finite(value) ||
    (Array.isArray(value) &&
      value.length > 0 &&
      new Set(value).size === value.length &&
      value.every(nonEmpty))
  );
}

function cloneTuningValue(value: ProductionTuningValue): ProductionTuningValue {
  return Array.isArray(value) ? [...value] : value;
}

function sameTuningValue(left: unknown, right: unknown): boolean {
  if (!Array.isArray(left) || !Array.isArray(right))
    return Object.is(left, right);
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function validTuningValue(
  control: ProductionTrialTuningControl,
  value: unknown,
): value is ProductionTuningValue {
  if (control.kind === "boolean") return typeof value === "boolean";
  if (control.kind === "select") {
    return (
      typeof value === "string" && Boolean(control.options?.includes(value))
    );
  }
  if (control.kind === "multi_select") {
    return (
      Array.isArray(value) &&
      value.length > 0 &&
      new Set(value).size === value.length &&
      value.every(
        (item) =>
          typeof item === "string" && Boolean(control.options?.includes(item)),
      )
    );
  }
  if (!finite(value)) return false;
  if (control.kind === "integer" && !Number.isInteger(value)) return false;
  const minimum = control.minimum;
  const maximum = control.maximum;
  const step = control.step;
  if (
    !finite(minimum) ||
    !finite(maximum) ||
    !finite(step) ||
    step <= 0 ||
    value < minimum ||
    value > maximum
  )
    return false;
  const steps = (value - minimum) / step;
  return Math.abs(steps - Math.round(steps)) <= 1e-7;
}

function validTuningControlPath(path: string): boolean {
  const segments = path.split(".");
  return (
    segments.length >= 2 &&
    segments.every(
      (segment) =>
        /^[a-z][a-z0-9_]*$/.test(segment) &&
        !["__proto__", "prototype", "constructor"].includes(segment),
    )
  );
}

const TUNING_CONTROL_KINDS = new Set([
  "number",
  "integer",
  "boolean",
  "select",
  "multi_select",
]);
const TUNING_CONTROL_SECTIONS = new Set([
  "detector",
  "sahi",
  "filtering",
  "selection",
  "tracking",
  "postprocess",
]);
const TUNING_RUNTIME_IMPACTS = new Set(["low", "medium", "high"]);
const FIELD_SETUP_AFFECTED_PATHS = new Set([
  "filtering.roi",
  "scene_bias.ground_zones",
  "scene_bias.negative_rois",
]);

function validTuningControl(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !nonEmpty(value.path) ||
    !validTuningControlPath(value.path) ||
    !TUNING_CONTROL_KINDS.has(String(value.kind)) ||
    !TUNING_CONTROL_SECTIONS.has(String(value.section)) ||
    !TUNING_RUNTIME_IMPACTS.has(String(value.runtime_impact)) ||
    !nonEmpty(value.description) ||
    !nonEmpty(value.description_zh)
  ) {
    return false;
  }
  if (value.kind === "select" || value.kind === "multi_select") {
    return (
      Array.isArray(value.options) &&
      value.options.length > 0 &&
      new Set(value.options).size === value.options.length &&
      value.options.every(nonEmpty)
    );
  }
  if (value.kind === "number" || value.kind === "integer") {
    return (
      finite(value.minimum) &&
      finite(value.maximum) &&
      finite(value.step) &&
      value.minimum < value.maximum &&
      value.step > 0
    );
  }
  return value.kind === "boolean";
}

function validTuningAction(value: unknown): boolean {
  return (
    isRecord(value) &&
    value.action_code === "return_to_field_setup" &&
    value.target_step === "field_setup" &&
    value.reason_code === "field_geometry_requires_new_calibration" &&
    Array.isArray(value.affected_paths) &&
    value.affected_paths.length === FIELD_SETUP_AFFECTED_PATHS.size &&
    new Set(value.affected_paths).size === value.affected_paths.length &&
    value.affected_paths.every(
      (path) => nonEmpty(path) && FIELD_SETUP_AFFECTED_PATHS.has(path),
    ) &&
    value.lineage_constraint ===
      "invalidate_trial_and_downstream_then_create_new_calibration_version"
  );
}

export function productionTrialTuningSchema(
  value: unknown,
): ProductionTrialTuningSchema | null {
  if (
    !isRecord(value) ||
    value.schema_version !== "1.0" ||
    value.patch_schema_version !== PRODUCTION_TUNING_PATCH_VERSION ||
    !Array.isArray(value.controls) ||
    value.controls.length === 0 ||
    !value.controls.every(validTuningControl) ||
    new Set(
      value.controls.map((control) =>
        isRecord(control) ? control.path : undefined,
      ),
    ).size !== value.controls.length ||
    !Array.isArray(value.actions) ||
    value.actions.length !== 1 ||
    !value.actions.every(validTuningAction)
  ) {
    return null;
  }
  return cloneJsonObject(value) as unknown as ProductionTrialTuningSchema;
}

function tuningVersion(value: unknown): ProductionTuningVersion | null {
  if (
    !isRecord(value) ||
    value.schema_version !== PRODUCTION_TUNING_PATCH_VERSION ||
    !nonEmpty(value.version_id) ||
    !(value.parent_version_id === null || nonEmpty(value.parent_version_id)) ||
    !nonEmpty(value.created_at) ||
    !sha256String(value.values_sha256) ||
    !isRecord(value.values) ||
    !Array.isArray(value.history)
  )
    return null;
  const values = value.values as Record<string, ProductionTuningValue>;
  const validValues = Object.values(values).every(validStoredTuningValue);
  const validHistory = value.history.every(
    (item) =>
      isRecord(item) &&
      nonEmpty(item.version_id) &&
      nonEmpty(item.created_at) &&
      sha256String(item.values_sha256) &&
      isRecord(item.values) &&
      Object.values(item.values).every(validStoredTuningValue),
  );
  return validValues && validHistory
    ? (cloneJsonObject(value) as unknown as ProductionTuningVersion)
    : null;
}

export function productionTuningVersion(
  patch: Record<string, unknown>,
): ProductionTuningVersion | null {
  return tuningVersion(valueAtPath(patch, "metadata.production_tuning"));
}

export function productionTuningHistory(
  patch: Record<string, unknown>,
): ProductionTuningVersionSnapshot[] {
  return productionTuningVersion(patch)?.history ?? [];
}

export function productionTuningDraft(input: {
  base_config: Record<string, unknown>;
  patch: Record<string, unknown>;
  controls: readonly ProductionTrialTuningControl[];
}): Record<string, ProductionTuningValue> {
  const version = productionTuningVersion(input.patch);
  const result: Record<string, ProductionTuningValue> = {};
  for (const control of input.controls) {
    const candidates = [
      version?.values[control.path],
      valueAtPath(input.patch, control.path),
      valueAtPath(input.base_config, control.path),
    ];
    const selected = candidates.find((candidate) =>
      validTuningValue(control, candidate),
    );
    if (selected !== undefined) result[control.path] = selected;
  }
  return result;
}

function validateTuningRelations(
  values: Record<string, ProductionTuningValue>,
) {
  for (const [minimumPath, maximumPath] of [
    ["filtering.min_width", "filtering.max_width"],
    ["filtering.min_height", "filtering.max_height"],
    ["filtering.min_aspect_ratio", "filtering.max_aspect_ratio"],
  ]) {
    const minimum = values[minimumPath];
    const maximum = values[maximumPath];
    if (
      typeof minimum === "number" &&
      typeof maximum === "number" &&
      minimum > maximum
    ) {
      throw new TypeError(`${minimumPath} must not exceed ${maximumPath}`);
    }
  }
}

export async function buildVersionedProductionTuningPatch(input: {
  base_config: Record<string, unknown>;
  previous_patch: Record<string, unknown>;
  controls: readonly ProductionTrialTuningControl[];
  values: Record<string, unknown>;
  version_id: string;
  created_at: string;
}): Promise<{
  patch: Record<string, unknown>;
  version: ProductionTuningVersion;
  diff: ProductionTuningDiff[];
}> {
  if (!nonEmpty(input.version_id) || !nonEmpty(input.created_at)) {
    throw new TypeError("Tuning version identity is required");
  }
  if (
    input.controls.some((control) => !validTuningControlPath(control.path)) ||
    new Set(input.controls.map((control) => control.path)).size !==
      input.controls.length
  ) {
    throw new TypeError("Tuning controls must have unique safe paths");
  }
  const controlsByPath = new Map(
    input.controls.map((control) => [control.path, control]),
  );
  const unexpected = Object.keys(input.values).filter(
    (path) => !controlsByPath.has(path),
  );
  if (unexpected.length > 0) {
    throw new TypeError(
      `Unsupported tuning controls: ${unexpected.join(", ")}`,
    );
  }
  const values: Record<string, ProductionTuningValue> = {};
  for (const control of input.controls) {
    const value = input.values[control.path];
    if (!validTuningValue(control, value)) {
      throw new TypeError(`Invalid tuning value: ${control.path}`);
    }
    values[control.path] = cloneTuningValue(value);
  }
  validateTuningRelations(values);

  const previousVersion = productionTuningVersion(input.previous_patch);
  const previousValues = productionTuningDraft({
    base_config: input.base_config,
    patch: input.previous_patch,
    controls: input.controls,
  });
  const diff: ProductionTuningDiff[] = input.controls.flatMap((control) => {
    const previous = previousValues[control.path];
    const next = values[control.path];
    return sameTuningValue(previous, next)
      ? []
      : [
          {
            path: control.path,
            previous_value:
              previous === undefined ? null : cloneTuningValue(previous),
            next_value: cloneTuningValue(next),
          },
        ];
  });
  const valuesSha256 = await sha256Text(canonicalJson(values));
  const history = previousVersion
    ? [
        ...previousVersion.history,
        {
          version_id: previousVersion.version_id,
          created_at: previousVersion.created_at,
          values_sha256: previousVersion.values_sha256,
          values: Object.fromEntries(
            Object.entries(previousVersion.values).map(([path, value]) => [
              path,
              cloneTuningValue(value),
            ]),
          ),
        },
      ]
    : [];
  const version: ProductionTuningVersion = {
    schema_version: PRODUCTION_TUNING_PATCH_VERSION,
    version_id: input.version_id,
    parent_version_id: previousVersion?.version_id ?? null,
    created_at: input.created_at,
    values_sha256: valuesSha256,
    values: Object.fromEntries(
      Object.entries(values).map(([path, value]) => [
        path,
        cloneTuningValue(value),
      ]),
    ),
    history,
  };
  const patch: Record<string, unknown> = {};
  for (const control of input.controls) {
    const baseValue = valueAtPath(input.base_config, control.path);
    const value = values[control.path];
    if (!sameTuningValue(baseValue, value))
      setValueAtPath(patch, control.path, cloneTuningValue(value));
  }
  setValueAtPath(patch, "metadata.production_tuning", version);
  return { patch, version, diff };
}

export function isProductionTrialSettings(
  value: unknown,
): value is ProductionTrialSettings {
  return (
    isRecord(value) &&
    nonEmpty(value.base_config_name) &&
    nonNegativeInteger(value.start_frame) &&
    positiveInteger(value.max_frames) &&
    typeof value.enable_postprocess === "boolean" &&
    typeof value.enable_follow_cam === "boolean" &&
    isRecord(value.tuning_patch) &&
    (() => {
      try {
        canonicalJson(value.tuning_patch);
        return true;
      } catch {
        return false;
      }
    })()
  );
}

export function createProductionTrialState(
  settings: ProductionTrialSettings = {
    base_config_name: "default.yaml",
    start_frame: 0,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: {},
  },
): ProductionTrialState {
  if (!isProductionTrialSettings(settings)) {
    throw new TypeError("Invalid production trial settings");
  }
  return {
    settings: {
      ...settings,
      tuning_patch: cloneJsonObject(settings.tuning_patch),
    },
    attempts: [],
    active_run_id: null,
    pending_submission: null,
    accepted: null,
  };
}

function requireTrialInputs(
  source: SourceSignature,
  calibration: ProductionCalibrationDraft,
  settings: ProductionTrialSettings,
): asserts calibration is ProductionCalibrationDraft & {
  source_resolution: NonNullable<
    ProductionCalibrationDraft["source_resolution"]
  >;
  polygon_digest: string;
} {
  if (
    !nonEmpty(source.path) ||
    !nonEmpty(source.modified_at) ||
    !finite(source.size_bytes)
  ) {
    throw new TypeError("A valid source signature is required");
  }
  if (!isProductionTrialSettings(settings)) {
    throw new TypeError("Invalid production trial settings");
  }
  if (
    !calibration.source_resolution ||
    !sha256String(calibration.polygon_digest) ||
    !calibrationIsComplete(calibration, source.path)
  ) {
    throw new TypeError("Completed calibration evidence is required");
  }
  if (
    !validatePolygon(
      calibration.approved_polygon,
      calibration.source_resolution,
    ).valid
  ) {
    throw new TypeError("Approved polygon is invalid");
  }
  for (const exclusion of calibration.exclusions) {
    if (!validatePolygon(exclusion, calibration.source_resolution).valid) {
      throw new TypeError("Exclusion polygon is invalid");
    }
  }
}

export function buildProductionTrialIntent(input: {
  workflow_id: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  settings: ProductionTrialSettings;
}): ProductionTrialIntent {
  requireTrialInputs(input.source, input.calibration, input.settings);
  if (!nonEmpty(input.workflow_id))
    throw new TypeError("workflow_id is required");
  return {
    schema_version: PRODUCTION_TRIAL_METADATA_VERSION,
    purpose: "production_trial_intent",
    workflow_id: input.workflow_id,
    source_signature: { ...input.source },
    calibration_digest: input.calibration.polygon_digest,
    approved_polygon: clonePolygon(input.calibration.approved_polygon),
    exclusions: input.calibration.exclusions.map(clonePolygon),
    base_config_name: input.settings.base_config_name,
    start_frame: input.settings.start_frame,
    max_frames: input.settings.max_frames,
    enable_postprocess: input.settings.enable_postprocess,
    enable_follow_cam: input.settings.enable_follow_cam,
    tuning_patch: cloneJsonObject(input.settings.tuning_patch),
  };
}

function polygonBounds(points: FieldPoint[]): [number, number, number, number] {
  return [
    Math.min(...points.map(([x]) => x)),
    Math.min(...points.map(([, y]) => y)),
    Math.max(...points.map(([x]) => x)),
    Math.max(...points.map(([, y]) => y)),
  ];
}

function protectedTrialPatch(input: {
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  enable_postprocess: boolean;
  enable_follow_cam: boolean;
  start_frame: number;
  max_frames: number;
  output_dir_name: string;
  machine_note: Record<string, unknown>;
}): Record<string, unknown> {
  const points = clonePolygon(input.calibration.approved_polygon);
  const exclusions = input.calibration.exclusions.map(clonePolygon);
  return {
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
    postprocess: { enabled: input.enable_postprocess },
    follow_cam: { enabled: input.enable_follow_cam },
    runtime: {
      start_frame: input.start_frame,
      max_frames: input.max_frames,
    },
    metadata: {
      production_workflow: {
        ...input.machine_note,
        source_signature: { ...input.source },
        output_dir_name: input.output_dir_name,
      },
    },
  };
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

export async function buildProductionTrialSubmission(input: {
  workflow_id: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  settings: ProductionTrialSettings;
  parent_run_id: string | null;
  legacy_restart_run_id?: string | null;
  submission_id: string;
  output_id: string;
  generation: number;
  created_at: string;
}): Promise<ProductionTrialSubmission> {
  if (!nonEmpty(input.submission_id) || !nonEmpty(input.output_id)) {
    throw new TypeError("submission_id and output_id are required");
  }
  if (!positiveInteger(input.generation)) {
    throw new TypeError("generation must be a positive integer");
  }
  if (input.parent_run_id === null && input.generation !== 1) {
    throw new TypeError("root production trials must use generation 1");
  }
  if (
    input.legacy_restart_run_id !== undefined &&
    input.legacy_restart_run_id !== null &&
    (!nonEmpty(input.legacy_restart_run_id) ||
      input.parent_run_id !== null ||
      input.generation !== 1)
  ) {
    throw new TypeError(
      "legacy trial restarts require a named generation-1 root",
    );
  }
  const intent = buildProductionTrialIntent(input);
  const intent_sha256 = await sha256Text(canonicalJson(intent));
  const output_dir_name = `production_trial_${input.output_id}`;
  const machineNote = {
    schema_version: PRODUCTION_TRIAL_METADATA_VERSION,
    purpose: "production_trial",
    workflow_id: input.workflow_id,
    submission_id: input.submission_id,
    output_id: input.output_id,
    generation: input.generation,
    calibration_digest: intent.calibration_digest,
    intent_sha256,
    start_frame: input.settings.start_frame,
    max_frames: input.settings.max_frames,
    enable_postprocess: input.settings.enable_postprocess,
    enable_follow_cam: input.settings.enable_follow_cam,
    ...(input.legacy_restart_run_id
      ? { legacy_restart_run_id: input.legacy_restart_run_id }
      : {}),
  };
  const protectedPatch = protectedTrialPatch({
    source: input.source,
    calibration: input.calibration,
    enable_postprocess: input.settings.enable_postprocess,
    enable_follow_cam: input.settings.enable_follow_cam,
    start_frame: input.settings.start_frame,
    max_frames: input.settings.max_frames,
    output_dir_name,
    machine_note: machineNote,
  });
  const request: CreateRunRequest = {
    config_name: input.settings.base_config_name,
    input_video: input.source.path,
    parent_run_id: input.parent_run_id,
    output_dir_name,
    config_patch: deepMergeProductionPatch(
      input.settings.tuning_patch,
      protectedPatch,
    ),
    enable_postprocess: input.settings.enable_postprocess,
    enable_follow_cam: input.settings.enable_follow_cam,
    start_frame: input.settings.start_frame,
    max_frames: input.settings.max_frames,
    pipeline_mode: "standard",
    notes: canonicalJson(machineNote),
  };
  const request_sha256 = await sha256Text(canonicalJson(request));
  return {
    intent,
    pending: {
      generation: input.generation,
      submission_id: input.submission_id,
      output_id: input.output_id,
      intent_sha256,
      request_sha256,
      request,
      created_at: input.created_at,
    },
  };
}

export function setPendingProductionTrial(
  state: ProductionTrialState,
  pending: ProductionTrialPendingSubmission,
): ProductionTrialState {
  return { ...state, pending_submission: pending };
}

export function appendProductionTrialAttempt(
  state: ProductionTrialState,
  input: {
    run: Pick<RunRecord, "run_id" | "status">;
    pending: ProductionTrialPendingSubmission;
    observed_at: string;
  },
): ProductionTrialState {
  const existing = state.attempts.find(
    (attempt) =>
      attempt.run_id === input.run.run_id ||
      attempt.submission_id === input.pending.submission_id,
  );
  if (existing) {
    if (
      existing.run_id !== input.run.run_id ||
      existing.generation !== input.pending.generation ||
      existing.submission_id !== input.pending.submission_id ||
      existing.request_sha256 !== input.pending.request_sha256
    ) {
      throw new Error("Conflicting trial attempt identity");
    }
    return {
      ...state,
      active_run_id:
        input.run.status === "queued" || input.run.status === "running"
          ? input.run.run_id
          : state.active_run_id === input.run.run_id
            ? null
            : state.active_run_id,
      pending_submission:
        state.pending_submission?.submission_id === input.pending.submission_id
          ? null
          : state.pending_submission,
    };
  }
  if (
    state.pending_submission?.generation !== input.pending.generation ||
    state.pending_submission.submission_id !== input.pending.submission_id ||
    state.pending_submission.request_sha256 !== input.pending.request_sha256
  ) {
    throw new Error("Stale trial submission response");
  }
  const attempt: ProductionTrialAttempt = {
    run_id: input.run.run_id,
    generation: input.pending.generation,
    submission_id: input.pending.submission_id,
    parent_run_id: input.pending.request.parent_run_id ?? null,
    intent_sha256: input.pending.intent_sha256,
    request_sha256: input.pending.request_sha256,
    request: input.pending.request,
    created_at: input.pending.created_at,
    last_observed: {
      status: input.run.status,
      observed_at: input.observed_at,
      evidence_generation: null,
    },
  };
  return {
    ...state,
    attempts: [...state.attempts, attempt],
    active_run_id:
      input.run.status === "queued" || input.run.status === "running"
        ? input.run.run_id
        : null,
    pending_submission:
      state.pending_submission?.submission_id === input.pending.submission_id
        ? null
        : state.pending_submission,
  };
}

export function observeProductionTrialRun(
  state: ProductionTrialState,
  input: {
    run_id: string;
    status: RunRecord["status"];
    observed_at: string;
    evidence_generation?: string | null;
  },
): ProductionTrialState {
  const attemptIndex = state.attempts.findIndex(
    (attempt) => attempt.run_id === input.run_id,
  );
  if (attemptIndex < 0) {
    return state;
  }
  const currentStatus = state.attempts[attemptIndex].last_observed.status;
  const becomesActive = input.status === "queued" || input.status === "running";
  if (
    ((currentStatus === "completed" ||
      currentStatus === "failed" ||
      currentStatus === "cancelled") &&
      becomesActive) ||
    (becomesActive && attemptIndex !== state.attempts.length - 1)
  ) {
    return state;
  }
  return {
    ...state,
    attempts: state.attempts.map((attempt) =>
      attempt.run_id === input.run_id
        ? {
            ...attempt,
            last_observed: {
              status: input.status,
              observed_at: input.observed_at,
              evidence_generation:
                input.evidence_generation ??
                attempt.last_observed.evidence_generation,
            },
          }
        : attempt,
    ),
    active_run_id:
      input.status === "queued" || input.status === "running"
        ? input.run_id
        : state.active_run_id === input.run_id
          ? null
          : state.active_run_id,
  };
}

export function trialMachineNote(
  notes: string | null | undefined,
): Record<string, unknown> | null {
  if (!notes) return null;
  try {
    const parsed: unknown = JSON.parse(notes);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function requestContextMatches(
  request: CreateRunRequest,
  expected: {
    workflow_id: string;
    source: SourceSignature;
    calibration: ProductionCalibrationDraft;
    submission_id: string;
    intent_sha256: string;
  },
): boolean {
  const note = trialMachineNote(request.notes);
  if (
    note?.schema_version !== PRODUCTION_TRIAL_METADATA_VERSION ||
    note.purpose !== "production_trial" ||
    note.workflow_id !== expected.workflow_id ||
    note.submission_id !== expected.submission_id ||
    note.calibration_digest !== expected.calibration.polygon_digest ||
    note.intent_sha256 !== expected.intent_sha256 ||
    !nonEmpty(note.output_id) ||
    request.input_video !== expected.source.path ||
    !isRecord(request.config_patch)
  ) {
    return false;
  }
  const outputDirName = `production_trial_${note.output_id}`;
  if (request.output_dir_name !== outputDirName) return false;
  const protectedPatch = protectedTrialPatch({
    source: expected.source,
    calibration: expected.calibration,
    enable_postprocess: Boolean(request.enable_postprocess),
    enable_follow_cam: Boolean(request.enable_follow_cam),
    start_frame: Number(request.start_frame),
    max_frames: Number(request.max_frames),
    output_dir_name: outputDirName,
    machine_note: note,
  });
  return recursiveSubset(protectedPatch, request.config_patch);
}

function requestMatchesSettings(
  request: CreateRunRequest,
  settings: ProductionTrialSettings,
  context: {
    workflow_id: string;
    source: SourceSignature;
    calibration: ProductionCalibrationDraft;
    submission_id: string;
    intent_sha256: string;
  },
): boolean {
  if (
    request.config_name !== settings.base_config_name ||
    request.start_frame !== settings.start_frame ||
    request.max_frames !== settings.max_frames ||
    request.enable_postprocess !== settings.enable_postprocess ||
    request.enable_follow_cam !== settings.enable_follow_cam ||
    !requestContextMatches(request, context)
  ) {
    return false;
  }
  const note = trialMachineNote(request.notes);
  if (!note || !nonEmpty(note.output_id)) return false;
  const expectedPatch = deepMergeProductionPatch(
    settings.tuning_patch,
    protectedTrialPatch({
      source: context.source,
      calibration: context.calibration,
      enable_postprocess: settings.enable_postprocess,
      enable_follow_cam: settings.enable_follow_cam,
      start_frame: settings.start_frame,
      max_frames: settings.max_frames,
      output_dir_name: `production_trial_${note.output_id}`,
      machine_note: note,
    }),
  );
  try {
    return canonicalJson(request.config_patch) === canonicalJson(expectedPatch);
  } catch {
    return false;
  }
}

export function productionTrialMatchesContext(
  state: ProductionTrialState,
  context: {
    workflow_id: string;
    source: SourceSignature;
    calibration: ProductionCalibrationDraft;
  },
): boolean {
  if (
    !isProductionTrialState(state) ||
    (state.accepted !== null && !productionTrialAcceptanceIsValid(state)) ||
    !calibrationIsComplete(context.calibration, context.source.path)
  ) {
    return false;
  }
  for (const attempt of state.attempts) {
    if (
      !requestContextMatches(attempt.request, {
        ...context,
        submission_id: attempt.submission_id,
        intent_sha256: attempt.intent_sha256,
      })
    ) {
      return false;
    }
  }
  if (
    state.pending_submission &&
    !requestMatchesSettings(state.pending_submission.request, state.settings, {
      ...context,
      submission_id: state.pending_submission.submission_id,
      intent_sha256: state.pending_submission.intent_sha256,
    })
  ) {
    return false;
  }
  if (state.accepted) {
    const acceptedAttempt = state.attempts.find(
      (attempt) => attempt.run_id === state.accepted?.run_id,
    );
    if (
      !acceptedAttempt ||
      !requestMatchesSettings(acceptedAttempt.request, state.settings, {
        ...context,
        submission_id: acceptedAttempt.submission_id,
        intent_sha256: acceptedAttempt.intent_sha256,
      })
    ) {
      return false;
    }
  }
  return true;
}

export function reconcilePendingProductionTrial(
  state: ProductionTrialState,
  input: {
    workflow_id: string;
    expected_generation: number;
    runs: Array<
      Pick<
        RunRecord,
        | "run_id"
        | "source"
        | "status"
        | "notes"
        | "config_name"
        | "input_video"
        | "parent_run_id"
        | "modules_enabled"
      >
    >;
    observed_at: string;
  },
): ProductionTrialState {
  const pending = state.pending_submission;
  if (!pending || pending.generation !== input.expected_generation)
    return state;
  const baseConfigName = pending.request.config_name;
  if (!nonEmpty(baseConfigName)) return state;
  const pendingNote = trialMachineNote(pending.request.notes);
  const expectedRunId = `production_trial_${pending.output_id}`;
  const expectedConfigName = materializedProductionTrialConfigName(
    baseConfigName,
    expectedRunId,
  );
  const pendingContractMatches =
    pending.request.output_dir_name === expectedRunId &&
    pending.request.pipeline_mode === "standard" &&
    baseConfigName === state.settings.base_config_name &&
    pending.request.start_frame === state.settings.start_frame &&
    pending.request.max_frames === state.settings.max_frames &&
    pending.request.enable_postprocess === state.settings.enable_postprocess &&
    pending.request.enable_follow_cam === state.settings.enable_follow_cam &&
    pendingNote?.schema_version === PRODUCTION_TRIAL_METADATA_VERSION &&
    pendingNote.purpose === "production_trial" &&
    pendingNote.workflow_id === input.workflow_id &&
    pendingNote.submission_id === pending.submission_id &&
    pendingNote.output_id === pending.output_id &&
    pendingNote.generation === pending.generation &&
    pendingNote.intent_sha256 === pending.intent_sha256 &&
    sha256String(pendingNote.calibration_digest) &&
    pendingNote.start_frame === pending.request.start_frame &&
    pendingNote.max_frames === pending.request.max_frames &&
    pendingNote.enable_postprocess === pending.request.enable_postprocess &&
    pendingNote.enable_follow_cam === pending.request.enable_follow_cam;
  if (!pendingContractMatches) return state;
  const matches = input.runs.filter((candidate) => {
    return (
      candidate.run_id === expectedRunId &&
      candidate.source === "api" &&
      candidate.notes === pending.request.notes &&
      candidate.config_name === expectedConfigName &&
      candidate.input_video === pending.request.input_video &&
      candidate.parent_run_id === pending.request.parent_run_id &&
      candidate.modules_enabled?.postprocess ===
        pending.request.enable_postprocess &&
      candidate.modules_enabled?.follow_cam ===
        pending.request.enable_follow_cam
    );
  });
  const run = matches.length === 1 ? matches[0] : null;
  return run
    ? appendProductionTrialAttempt(state, {
        run,
        pending,
        observed_at: input.observed_at,
      })
    : state;
}

/** Mirrors ApiService._materialize_run_config for a run carrying config_patch. */
export function materializedProductionTrialConfigName(
  baseConfigName: string,
  runId: string,
): string {
  const leaf = baseConfigName.replaceAll("\\", "/").split("/").at(-1) ?? "";
  const extensionIndex = leaf.lastIndexOf(".");
  const stem = extensionIndex > 0 ? leaf.slice(0, extensionIndex) : leaf;
  return `generated/${stem}_field_setup_${runId}.yaml`;
}

function artifactMatches(
  artifacts: readonly ArtifactSummary[],
  name: string,
): ArtifactSummary | null {
  const matches = artifacts.filter((artifact) => artifact.name === name);
  if (matches.length !== 1) return null;
  const [artifact] = matches;
  return artifact.exists &&
    finite(artifact.size_bytes) &&
    artifact.size_bytes > 0
    ? artifact
    : null;
}

function topLevelVideoCandidates(
  artifacts: readonly ArtifactSummary[],
): ArtifactSummary[] {
  return artifacts.filter(
    (artifact) =>
      artifact.kind === "video" &&
      artifact.exists &&
      finite(artifact.size_bytes) &&
      artifact.size_bytes > 0 &&
      !artifact.name.includes("/") &&
      !artifact.name.includes("\\"),
  );
}

export function selectProductionTrialVideo(
  artifacts: readonly ArtifactSummary[],
  enableFollowCam: boolean,
): ArtifactSummary | null {
  const candidates = topLevelVideoCandidates(artifacts);
  if (enableFollowCam) {
    const preferred = candidates.filter(
      (item) => item.name === "follow_cam.mp4",
    );
    if (preferred.length === 1) return preferred[0];
    if (preferred.length > 1) return null;
  }
  const annotated = candidates.filter((item) => item.name === "annotated.mp4");
  if (annotated.length === 1) return annotated[0];
  if (annotated.length > 1) return null;
  return candidates.length === 1 ? candidates[0] : null;
}

function validAudit(
  report: BallAuditReport | null,
  raw: Record<string, number> | null,
  cleaned: Record<string, number> | null,
  enablePostprocess: boolean,
): report is BallAuditReport {
  const summary = report?.summary;
  const sources = report?.sources;
  const tracklets = report?.tracklets;
  const events = report?.review_events;
  const rawSource = sources?.find((source) => source.name === "raw");
  const cleanedSource = sources?.find((source) => source.name === "cleaned");
  return Boolean(
    report?.schema_version === "1.0" &&
    summary &&
    positiveInteger(summary.frame_count) &&
    nonNegativeInteger(summary.tracklet_count) &&
    nonNegativeInteger(summary.suspicious_tracklet_count) &&
    nonNegativeInteger(summary.review_event_count) &&
    nonNegativeInteger(summary.lost_gap_count) &&
    (summary.max_step_px == null || finite(summary.max_step_px)) &&
    Array.isArray(sources) &&
    Array.isArray(tracklets) &&
    Array.isArray(events) &&
    summary.source_count === sources.length &&
    summary.tracklet_count === tracklets.length &&
    summary.suspicious_tracklet_count ===
      tracklets.filter((tracklet) => (tracklet.flags?.length ?? 0) > 0)
        .length &&
    summary.review_event_count === events.length &&
    summary.lost_gap_count ===
      events.filter((event) => event.type === "lost_gap").length &&
    raw &&
    summary.frame_count === raw.frame_count &&
    rawSource?.path === "ball_track.csv" &&
    rawSource.row_count === raw.frame_count &&
    (enablePostprocess
      ? cleaned &&
        cleanedSource?.path === "ball_track.cleaned.csv" &&
        cleanedSource.row_count === cleaned.frame_count
      : cleanedSource === undefined),
  );
}

const REQUIRED_METRIC_KEYS = [
  "frame_count",
  "detected",
  "predicted",
  "lost",
  "detected_ratio",
  "predicted_ratio",
  "lost_ratio",
] as const;

const RUN_STATUSES = new Set([
  "queued",
  "running",
  "completed",
  "failed",
  "cancelled",
]);

function ratioMatches(
  count: number,
  frameCount: number,
  ratio: number,
): boolean {
  return (
    Math.abs(ratio - Math.round((count / frameCount) * 10_000) / 10_000) <=
    0.0001
  );
}

function validTrackStats(value: unknown): value is Record<string, number> {
  if (!isRecord(value) || !positiveInteger(value.frame_count)) return false;
  if (!REQUIRED_METRIC_KEYS.every((key) => finite(value[key]))) return false;
  const detected = Number(value.detected);
  const predicted = Number(value.predicted);
  const lost = Number(value.lost);
  const detectedRatio = Number(value.detected_ratio);
  const predictedRatio = Number(value.predicted_ratio);
  const lostRatio = Number(value.lost_ratio);
  const counts = [detected, predicted, lost];
  const ratios = [detectedRatio, predictedRatio, lostRatio];
  return (
    counts.every(nonNegativeInteger) &&
    counts.reduce((sum, count) => sum + count, 0) === value.frame_count &&
    ratios.every((ratio) => ratio >= 0 && ratio <= 1) &&
    ratioMatches(detected, value.frame_count, detectedRatio) &&
    ratioMatches(predicted, value.frame_count, predictedRatio) &&
    ratioMatches(lost, value.frame_count, lostRatio) &&
    (value.longest_lost_streak === undefined ||
      nonNegativeInteger(value.longest_lost_streak)) &&
    (value.false_positive_island_count === undefined ||
      nonNegativeInteger(value.false_positive_island_count))
  );
}

function sameTrackStats(left: unknown, right: unknown): boolean {
  if (!validTrackStats(left) || !validTrackStats(right)) return false;
  return REQUIRED_METRIC_KEYS.every(
    (key) => Math.abs(left[key] - right[key]) <= 0.0001,
  );
}

function validMetrics(
  value: unknown,
  runStats: unknown,
  enablePostprocess: boolean,
): value is Record<string, unknown> {
  if (
    !isRecord(value) ||
    value.schema_version !== "1.0" ||
    !nonEmpty(value.generated_at) ||
    !isRecord(value.tracks) ||
    !isRecord(runStats) ||
    !sameTrackStats(value.tracks.raw, runStats.raw)
  ) {
    return false;
  }
  return enablePostprocess
    ? sameTrackStats(value.tracks.cleaned, runStats.cleaned)
    : value.tracks.cleaned === undefined;
}

function validTrackCsv(value: unknown, stats: unknown): value is string {
  if (typeof value !== "string" || !validTrackStats(stats)) return false;
  const lines = value.replace(/^\uFEFF/, "").split(/\r?\n/);
  while (lines.at(-1)?.trim() === "") lines.pop();
  const [header, ...rows] = lines;
  if (
    header !== "Frame,X,Y,Confidence,Status" ||
    rows.length !== stats.frame_count
  )
    return false;
  const frames = new Set<number>();
  const counts = { Detected: 0, Predicted: 0, Lost: 0 };
  for (const row of rows) {
    const columns = row.split(",");
    if (columns.length !== 5) return false;
    const [frameText, xText, yText, confidenceText, statusText] = columns;
    if (!/^\d+$/.test(frameText)) return false;
    const frame = Number(frameText);
    const confidence = Number(confidenceText);
    if (
      !Number.isSafeInteger(frame) ||
      frames.has(frame) ||
      !Number.isFinite(confidence) ||
      confidence < 0 ||
      confidence > 1 ||
      (statusText !== "Detected" &&
        statusText !== "Predicted" &&
        statusText !== "Lost")
    )
      return false;
    const coordinateValid = (text: string) =>
      text.trim() !== "" && Number.isFinite(Number(text));
    if (
      statusText === "Lost"
        ? !(
            (xText.trim() === "" && yText.trim() === "") ||
            (coordinateValid(xText) && coordinateValid(yText))
          )
        : !(coordinateValid(xText) && coordinateValid(yText))
    )
      return false;
    frames.add(frame);
    counts[statusText as keyof typeof counts] += 1;
  }
  return (
    counts.Detected === stats.detected &&
    counts.Predicted === stats.predicted &&
    counts.Lost === stats.lost
  );
}

function optionalFinite(value: unknown): number | null {
  return finite(value) ? value : null;
}

function qualityGateStatus(stats: Record<string, unknown>): string | null {
  const gate = stats.quality_gate;
  return isRecord(gate) && nonEmpty(gate.status) ? gate.status : null;
}

const TRIAL_SIGNAL_STATUSES = new Set([
  "insufficient_evidence",
  "retune_required",
  "acceptable",
]);
const TRIAL_FAILURE_CODES = new Set([
  "insufficient_evidence",
  "decode_failure",
  "no_raw_candidates",
  "all_candidates_class_rejected",
  "all_candidates_filtered",
  "no_tracklets",
  "all_lost",
  "wrong_or_noisy_candidates",
  "unstable_tracking",
  "acceptable",
]);
const TRIAL_FAILURE_SEVERITIES = new Set(["none", "high", "blocking"]);
const TRIAL_REQUIRED_THRESHOLDS = [
  "minimum_detected_ratio",
  "maximum_predicted_ratio",
  "maximum_lost_ratio",
  "maximum_longest_lost_streak",
  "maximum_false_positive_islands_per_100_frames",
  "maximum_suspicious_tracklet_ratio",
  "maximum_step_px",
  "maximum_follow_cam_pan_step_px",
  "maximum_follow_cam_pan_accel_px",
  "maximum_follow_cam_zoom_step_ratio",
  "maximum_ai_review_triggers_per_100_frames",
  "maximum_event_candidates_per_100_frames",
] as const;

function validTrialMatchingRules(value: unknown): boolean {
  return (
    isRecord(value) &&
    nonEmpty(value.stage_counter_reconciliation) &&
    nonEmpty(value.track_metric_scope) &&
    nonEmpty(value.follow_cam_scope) &&
    Array.isArray(value.required_visual_evidence) &&
    value.required_visual_evidence.length > 0 &&
    value.required_visual_evidence.every(nonEmpty) &&
    Array.isArray(value.required_integrity) &&
    value.required_integrity.length > 0 &&
    value.required_integrity.every(nonEmpty) &&
    nonEmpty(value.acceptance_contract)
  );
}

function validTrialThresholdProfile(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !nonEmpty(value.profile_id) ||
    !nonEmpty(value.version) ||
    !nonEmpty(value.algorithm_version) ||
    !validTrialMatchingRules(value.matching_rules) ||
    !sha256String(value.sha256) ||
    !isRecord(value.thresholds) ||
    !Object.values(value.thresholds).every(finite)
  ) {
    return false;
  }
  const thresholds = value.thresholds;
  return TRIAL_REQUIRED_THRESHOLDS.every((name) => finite(thresholds[name]));
}

function validCollectedCount(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !["collected", "not_collected", "invalid"].includes(String(value.status))
  ) {
    return false;
  }
  return value.status === "collected"
    ? nonNegativeInteger(value.value)
    : value.value === undefined || value.value === null;
}

function validDetectionStages(value: unknown): boolean {
  if (
    !isRecord(value) ||
    value.schema_version !== "2.0" ||
    !["complete", "invalid", "not_collected"].includes(
      String(value.coverage_status),
    ) ||
    !validCollectedCount(value.evaluated_frames) ||
    !validCollectedCount(value.detected_frames) ||
    !validCollectedCount(value.predicted_frames) ||
    !validCollectedCount(value.lost_frames) ||
    !validCollectedCount(value.raw_candidates) ||
    !validCollectedCount(value.class_mapped_candidates) ||
    !validCollectedCount(value.filtered_candidates) ||
    !validCollectedCount(value.selected_candidates) ||
    !validCollectedCount(value.tracklets) ||
    !isRecord(value.reconciliation) ||
    !["reconciled", "mismatch", "not_collected"].includes(
      String(value.reconciliation.status),
    )
  ) {
    return false;
  }
  if (
    value.reconciliation.reason_codes !== undefined &&
    (!Array.isArray(value.reconciliation.reason_codes) ||
      !value.reconciliation.reason_codes.every(nonEmpty))
  ) {
    return false;
  }
  return (
    isRecord(value.rejection_reasons) &&
    Object.values(value.rejection_reasons).every(nonNegativeInteger)
  );
}

const TRIAL_DIAGNOSTIC_STATUSES = new Set([
  "collected",
  "not_collected",
  "invalid",
]);
const TRACK_DIAGNOSTIC_METRICS = [
  "frame_count",
  "detected",
  "predicted",
  "lost",
  "detected_ratio",
  "predicted_ratio",
  "lost_ratio",
  "longest_lost_streak",
  "false_positive_island_count",
  "max_step_px",
] as const;

function validDiagnosticObservation(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !TRIAL_DIAGNOSTIC_STATUSES.has(String(value.status))
  ) {
    return false;
  }
  return value.status === "collected"
    ? finite(value.value) && value.value >= 0
    : value.value === null;
}

function validTrackDiagnostics(value: unknown): boolean {
  return (
    isRecord(value) &&
    TRIAL_DIAGNOSTIC_STATUSES.has(String(value.status)) &&
    TRACK_DIAGNOSTIC_METRICS.every((metric) =>
      validDiagnosticObservation(value[metric]),
    )
  );
}

function validRejectionReasonDiagnostics(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !TRIAL_DIAGNOSTIC_STATUSES.has(String(value.status))
  ) {
    return false;
  }
  return value.status === "collected"
    ? isRecord(value.value) &&
        Object.values(value.value).every(nonNegativeInteger)
    : value.value === null;
}

function validTrialDiagnostics(value: unknown): boolean {
  return (
    isRecord(value) &&
    validTrackDiagnostics(value.raw_track) &&
    validTrackDiagnostics(value.cleaned_track) &&
    validRejectionReasonDiagnostics(value.rejection_reasons) &&
    validDiagnosticObservation(value.ai_review_trigger_count) &&
    validDiagnosticObservation(value.ai_review_triggers_per_100_frames) &&
    validDiagnosticObservation(value.event_candidate_count) &&
    validDiagnosticObservation(value.event_candidates_per_100_frames) &&
    isRecord(value.follow_cam) &&
    TRIAL_DIAGNOSTIC_STATUSES.has(String(value.follow_cam.status)) &&
    validDiagnosticObservation(value.follow_cam.max_pan_step_px) &&
    validDiagnosticObservation(value.follow_cam.max_pan_accel_px) &&
    validDiagnosticObservation(value.follow_cam.max_zoom_step_ratio)
  );
}

export function isTrialSignalGateV2(
  value: unknown,
): value is ProductionTrialSignalGateV2 {
  if (
    !isRecord(value) ||
    value.schema_version !== "2.0" ||
    !TRIAL_SIGNAL_STATUSES.has(String(value.status)) ||
    ![
      "coverage_complete",
      "evidence_available",
      "trajectory_acceptable",
      "signal_acceptable",
      "acceptance_metrics_complete",
      "acceptance_contract_complete",
      "quality_acceptable",
    ].every((key) => typeof value[key] === "boolean") ||
    value.operator_confirmation_required !== true ||
    (value.reason_codes !== undefined &&
      (!Array.isArray(value.reason_codes) ||
        !value.reason_codes.every(nonEmpty))) ||
    !isRecord(value.failure_classification) ||
    !TRIAL_FAILURE_CODES.has(String(value.failure_classification.code)) ||
    !TRIAL_FAILURE_SEVERITIES.has(
      String(value.failure_classification.severity),
    ) ||
    !nonEmpty(value.failure_classification.summary) ||
    !nonEmpty(value.failure_classification.recommended_action) ||
    !validTrialThresholdProfile(value.threshold_profile) ||
    !(
      value.stage_counts === null || validDetectionStages(value.stage_counts)
    ) ||
    !isRecord(value.trajectory) ||
    !validTrialDiagnostics(value.diagnostics) ||
    !isRecord(value.evidence) ||
    !Object.values(value.evidence).every(nonEmpty)
  ) {
    return false;
  }
  return true;
}

function collectedPositiveStage(
  stages: ProductionTrialSignalGateV2["stage_counts"],
  name:
    | "evaluated_frames"
    | "raw_candidates"
    | "class_mapped_candidates"
    | "filtered_candidates"
    | "selected_candidates"
    | "tracklets",
): boolean {
  if (!stages) return false;
  const count = stages[name];
  return (
    count.status === "collected" &&
    nonNegativeInteger(count.value) &&
    count.value > 0
  );
}

function collectedStage(
  stages: ProductionTrialSignalGateV2["stage_counts"],
  name: "detected_frames" | "predicted_frames" | "lost_frames",
): boolean {
  if (!stages) return false;
  const count = stages[name];
  return count.status === "collected" && nonNegativeInteger(count.value);
}

function reconciledDetectionStages(
  stages: ProductionTrialSignalGateV2["stage_counts"],
): boolean {
  if (!stages) return false;
  const names = [
    "evaluated_frames",
    "detected_frames",
    "predicted_frames",
    "lost_frames",
    "raw_candidates",
    "class_mapped_candidates",
    "filtered_candidates",
    "selected_candidates",
    "tracklets",
  ] as const;
  const counts = Object.fromEntries(
    names.map((name) => [
      name,
      stages[name].status === "collected" &&
      nonNegativeInteger(stages[name].value)
        ? stages[name].value
        : null,
    ]),
  ) as Record<(typeof names)[number], number | null>;
  if (Object.values(counts).some((count) => count === null)) return false;
  return (
    counts.detected_frames! + counts.predicted_frames! + counts.lost_frames! ===
      counts.evaluated_frames &&
    counts.selected_candidates === counts.detected_frames &&
    counts.class_mapped_candidates! <= counts.raw_candidates! &&
    counts.filtered_candidates! <= counts.class_mapped_candidates! &&
    counts.selected_candidates! <= counts.filtered_candidates! &&
    counts.tracklets! <= counts.selected_candidates!
  );
}

export function productionTrialSignalGateAcceptable(
  value: unknown,
): value is ProductionTrialSignalGateV2 {
  if (!isTrialSignalGateV2(value)) return false;
  const evidence = value.evidence;
  return (
    value.status === "acceptable" &&
    value.coverage_complete === true &&
    value.evidence_available === true &&
    value.trajectory_acceptable === true &&
    value.signal_acceptable === true &&
    value.acceptance_metrics_complete === true &&
    value.acceptance_contract_complete === true &&
    value.quality_acceptable === true &&
    value.failure_classification.code === "acceptable" &&
    value.failure_classification.severity === "none" &&
    value.stage_counts?.coverage_status === "complete" &&
    value.stage_counts.reconciliation.status === "reconciled" &&
    collectedPositiveStage(value.stage_counts, "evaluated_frames") &&
    collectedStage(value.stage_counts, "detected_frames") &&
    collectedStage(value.stage_counts, "predicted_frames") &&
    collectedStage(value.stage_counts, "lost_frames") &&
    collectedPositiveStage(value.stage_counts, "raw_candidates") &&
    collectedPositiveStage(value.stage_counts, "class_mapped_candidates") &&
    collectedPositiveStage(value.stage_counts, "filtered_candidates") &&
    collectedPositiveStage(value.stage_counts, "selected_candidates") &&
    collectedPositiveStage(value.stage_counts, "tracklets") &&
    reconciledDetectionStages(value.stage_counts) &&
    evidence.wide_context === "available" &&
    evidence.tight_crop === "available" &&
    (evidence.follow_cam === "available" ||
      evidence.follow_cam === "not_applicable") &&
    (evidence.follow_cam_action_retention === "complete" ||
      evidence.follow_cam_action_retention === "not_applicable") &&
    evidence.scale_strata === "complete" &&
    evidence.lighting_strata === "complete" &&
    evidence.attack_transition_windows === "complete" &&
    evidence.media_integrity === "complete" &&
    evidence.identity_binding === "complete"
  );
}

function resolveTrialSignalGate(
  ...values: unknown[]
): ProductionTrialSignalGateV2 | null {
  const present = values.filter(
    (value) => value !== undefined && value !== null,
  );
  if (present.some((value) => !isTrialSignalGateV2(value))) return null;
  const gates = present as ProductionTrialSignalGateV2[];
  if (gates.length === 0) return null;
  const identity = canonicalJson(gates[0]);
  return gates.every((gate) => canonicalJson(gate) === identity)
    ? gates[0]
    : null;
}

export function assessProductionTrialEvidence(input: {
  run: Pick<
    RunRecord,
    | "run_id"
    | "status"
    | "input_video"
    | "config_name"
    | "stats"
    | "notes"
    | "trial_signal_gate_v2"
  >;
  artifacts: readonly ArtifactSummary[];
  manifest: unknown;
  metrics: unknown;
  audit: BallAuditReport | null;
  raw_csv: unknown;
  cleaned_csv: unknown;
  readable_artifact_names: readonly string[];
  enable_postprocess: boolean;
  enable_follow_cam: boolean;
  video_loaded: boolean;
  trial_signal_gate_v2?: unknown;
}): ProductionTrialEvidenceResult {
  const reasons: string[] = [];
  if (input.run.status !== "completed") reasons.push("run_not_completed");
  const requiredNames = [
    "run_manifest.json",
    "metrics_report.json",
    "ball_track.csv",
    "ball_audit.json",
    ...(input.enable_postprocess ? ["ball_track.cleaned.csv"] : []),
  ];
  const requiredArtifacts = requiredNames.flatMap((name) => {
    const artifact = artifactMatches(input.artifacts, name);
    if (!artifact) reasons.push(`artifact_unavailable:${name}`);
    return artifact ? [artifact] : [];
  });
  for (const name of requiredNames) {
    if (!input.readable_artifact_names.includes(name)) {
      reasons.push(`artifact_unreadable:${name}`);
    }
  }
  if (
    !isRecord(input.manifest) ||
    input.manifest.schema_version !== "1.0" ||
    input.manifest.run_id !== input.run.run_id ||
    input.manifest.input_video !== input.run.input_video ||
    input.manifest.config_name !== input.run.config_name ||
    input.manifest.status !== input.run.status ||
    input.manifest.notes !== (input.run.notes ?? null)
  ) {
    reasons.push("manifest_mismatch");
  }
  const stats = input.run.stats;
  const gate = resolveTrialSignalGate(
    input.trial_signal_gate_v2,
    input.run.trial_signal_gate_v2,
    isRecord(stats) ? stats.trial_signal_gate_v2 : undefined,
    isRecord(input.metrics) ? input.metrics.trial_signal_gate_v2 : undefined,
  );
  const raw = isRecord(stats) ? stats.raw : null;
  if (!validTrackStats(raw)) reasons.push("raw_stats_unreadable");
  const cleaned = isRecord(stats) ? stats.cleaned : null;
  if (input.enable_postprocess && !validTrackStats(cleaned)) {
    reasons.push("cleaned_stats_unreadable");
  }
  if (!validMetrics(input.metrics, stats, input.enable_postprocess)) {
    reasons.push("metrics_unreadable");
  }
  if (!validTrackCsv(input.raw_csv, raw)) reasons.push("raw_csv_unreadable");
  if (input.enable_postprocess && !validTrackCsv(input.cleaned_csv, cleaned)) {
    reasons.push("cleaned_csv_unreadable");
  }
  if (
    !validAudit(
      input.audit,
      validTrackStats(raw) ? raw : null,
      validTrackStats(cleaned) ? cleaned : null,
      input.enable_postprocess,
    )
  ) {
    reasons.push("audit_unreadable");
  }
  const video = selectProductionTrialVideo(
    input.artifacts,
    input.enable_follow_cam,
  );
  if (!video) reasons.push("video_unavailable");
  else if (!input.video_loaded) reasons.push("video_not_loaded");
  if (reasons.length > 0 || !video || !input.audit || !validTrackStats(raw)) {
    return { ready: false, reasons };
  }
  return {
    ready: true,
    video,
    required_artifacts: requiredArtifacts,
    quality: {
      frame_count: raw.frame_count,
      detected: raw.detected,
      predicted: raw.predicted,
      lost: raw.lost,
      detected_ratio: raw.detected_ratio,
      predicted_ratio: raw.predicted_ratio,
      lost_ratio: raw.lost_ratio,
      longest_lost_streak: optionalFinite(raw.longest_lost_streak),
      false_positive_island_count: optionalFinite(
        raw.false_positive_island_count,
      ),
      max_step_px: optionalFinite(raw.max_step_px),
      audit_tracklet_count: input.audit.summary.tracklet_count,
      audit_suspicious_tracklet_count:
        input.audit.summary.suspicious_tracklet_count,
      audit_review_event_count: input.audit.summary.review_event_count,
      audit_lost_gap_count: input.audit.summary.lost_gap_count,
      quality_gate_status: isRecord(stats) ? qualityGateStatus(stats) : null,
      trial_signal_gate_v2: gate,
    },
  };
}

export async function productionTrialEvidenceGeneration(input: {
  run_id: string;
  intent_sha256: string;
  request_sha256: string;
  artifacts: readonly ArtifactSummary[];
  stats: unknown;
  manifest: unknown;
  metrics: unknown;
  audit: BallAuditReport;
  raw_csv: string;
  cleaned_csv: string | null;
  selected_video: ArtifactSummary;
  video_metadata: {
    duration: number | null;
    width: number;
    height: number;
  };
}): Promise<string> {
  const acceptedNames = new Set([
    "run_manifest.json",
    "metrics_report.json",
    "ball_track.csv",
    "ball_audit.json",
    ...(input.cleaned_csv === null ? [] : ["ball_track.cleaned.csv"]),
    input.selected_video.name,
  ]);
  const artifactSnapshot = input.artifacts
    .filter((artifact) => acceptedNames.has(artifact.name))
    .map((artifact) => ({
      name: artifact.name,
      path: artifact.path,
      kind: artifact.kind,
      exists: artifact.exists,
      size_bytes: artifact.size_bytes ?? null,
      content_type: artifact.content_type ?? null,
    }))
    .sort((left, right) =>
      `${left.name}\u0000${left.path}`.localeCompare(
        `${right.name}\u0000${right.path}`,
      ),
    );
  return sha256Text(
    canonicalJson({
      run_id: input.run_id,
      intent_sha256: input.intent_sha256,
      request_sha256: input.request_sha256,
      artifacts: artifactSnapshot,
      stats: input.stats ?? null,
      manifest: input.manifest,
      metrics: input.metrics,
      audit: input.audit,
      raw_csv_sha256: await sha256Text(input.raw_csv),
      cleaned_csv_sha256:
        input.cleaned_csv === null ? null : await sha256Text(input.cleaned_csv),
      selected_video: {
        name: input.selected_video.name,
        path: input.selected_video.path,
        size_bytes: input.selected_video.size_bytes ?? null,
        content_type: input.selected_video.content_type ?? null,
      },
      video_metadata: input.video_metadata,
    }),
  );
}

export function productionTrialEvidenceSnapshotIdentity(input: {
  run_id: string;
  intent_sha256: string;
  request_sha256: string;
  query_revisions: {
    run: number;
    artifacts: number;
    manifest: number;
    metrics: number;
    audit: number;
    raw_csv: number;
    cleaned_csv: number | null;
  };
  raw_csv_length: number;
  cleaned_csv_length: number | null;
  selected_video: {
    name: string;
    path: string;
    size_bytes: number | null;
  };
  video_metadata: {
    duration: number | null;
    width: number;
    height: number;
  };
}): string {
  return canonicalJson(input);
}

export function acceptProductionTrial(
  state: ProductionTrialState,
  input: {
    run: Pick<RunRecord, "run_id" | "status">;
    current_intent_sha256: string;
    readiness: ProductionTrialReadinessSummary;
    accepted_at: string;
  },
): ProductionTrialState {
  const attempt = state.attempts.at(-1);
  const gate = input.readiness.quality.trial_signal_gate_v2;
  const confirmation = input.readiness.operator_visual_confirmation;
  if (
    !attempt ||
    attempt.run_id !== input.run.run_id ||
    input.run.status !== "completed" ||
    attempt.intent_sha256 !== input.current_intent_sha256 ||
    input.readiness.run_id !== attempt.run_id ||
    input.readiness.request_sha256 !== attempt.request_sha256 ||
    !sha256String(input.readiness.evidence_generation) ||
    !productionTrialSignalGateAcceptable(gate) ||
    !confirmation ||
    confirmation.confirmed !== true ||
    !nonEmpty(confirmation.confirmed_at) ||
    confirmation.evidence_generation !== input.readiness.evidence_generation ||
    confirmation.threshold_profile_sha256 !== gate.threshold_profile.sha256
  ) {
    throw new Error("Trial is not eligible for acceptance");
  }
  const accepted: ProductionTrialState = {
    ...state,
    active_run_id: null,
    attempts: state.attempts.map((candidate) =>
      candidate.run_id === attempt.run_id
        ? {
            ...candidate,
            last_observed: {
              ...candidate.last_observed,
              evidence_generation: input.readiness.evidence_generation,
            },
          }
        : candidate,
    ),
    accepted: {
      run_id: attempt.run_id,
      intent_sha256: attempt.intent_sha256,
      request_sha256: attempt.request_sha256,
      accepted_at: input.accepted_at,
      readiness: input.readiness,
    },
  };
  if (!productionTrialAcceptanceIsValid(accepted)) {
    throw new Error("Trial is not eligible for acceptance");
  }
  return accepted;
}

export function invalidateProductionTrialAcceptance(
  state: ProductionTrialState,
): ProductionTrialState {
  return { ...state, accepted: null };
}

export function nextProductionTrialGeneration(
  state: ProductionTrialState | null,
): number {
  return (
    Math.max(
      state?.pending_submission?.generation ?? 0,
      ...(state?.attempts.map((attempt) => attempt.generation) ?? [0]),
    ) + 1
  );
}

const TERMINAL_TRIAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function productionTrialSubmissionLineage(
  state: ProductionTrialState,
  authoritativeRun: Pick<
    RunRecord,
    "run_id" | "status" | "config_sha256"
  > | null,
): ProductionTrialSubmissionLineage | null {
  const latest = state.attempts.at(-1);
  if (!latest) {
    return {
      state,
      parent_run_id: null,
      generation: 1,
      legacy_restart_run_id: null,
      base_config_locked: false,
    };
  }
  if (
    !authoritativeRun ||
    authoritativeRun.run_id !== latest.run_id ||
    !TERMINAL_TRIAL_STATUSES.has(authoritativeRun.status)
  ) {
    return null;
  }
  if (sha256String(authoritativeRun.config_sha256)) {
    return {
      state,
      parent_run_id: latest.run_id,
      generation: nextProductionTrialGeneration(state),
      legacy_restart_run_id: null,
      base_config_locked: true,
    };
  }
  return {
    state: createProductionTrialState(state.settings),
    parent_run_id: null,
    generation: 1,
    legacy_restart_run_id: latest.run_id,
    base_config_locked: true,
  };
}

function validTrialRequestSnapshot(
  value: unknown,
  expected: {
    submission_id: string;
    output_id?: string;
    parent_run_id?: string | null;
  },
): value is CreateRunRequest {
  if (!isRecord(value) || !nonEmpty(value.notes)) return false;
  const note = trialMachineNote(value.notes);
  if (
    note?.purpose !== "production_trial" ||
    note.submission_id !== expected.submission_id ||
    (expected.output_id !== undefined &&
      note.output_id !== expected.output_id) ||
    (expected.output_id !== undefined &&
      value.output_dir_name !== `production_trial_${expected.output_id}`) ||
    (expected.parent_run_id !== undefined &&
      (value.parent_run_id ?? null) !== expected.parent_run_id) ||
    value.pipeline_mode !== "standard" ||
    !nonEmpty(value.config_name) ||
    !nonEmpty(value.input_video) ||
    !nonNegativeInteger(value.start_frame) ||
    !positiveInteger(value.max_frames) ||
    typeof value.enable_postprocess !== "boolean" ||
    typeof value.enable_follow_cam !== "boolean" ||
    !isRecord(value.config_patch)
  ) {
    return false;
  }
  try {
    canonicalJson(value);
    return true;
  } catch {
    return false;
  }
}

function validQualitySummary(
  value: unknown,
): value is ProductionTrialQualitySignals {
  if (!isRecord(value)) return false;
  for (const key of [
    "frame_count",
    "detected",
    "predicted",
    "lost",
    "detected_ratio",
    "predicted_ratio",
    "lost_ratio",
    "audit_tracklet_count",
    "audit_suspicious_tracklet_count",
    "audit_review_event_count",
    "audit_lost_gap_count",
  ]) {
    if (!finite(value[key])) return false;
  }
  return (
    validTrackStats(value) &&
    nonNegativeInteger(value.audit_tracklet_count) &&
    nonNegativeInteger(value.audit_suspicious_tracklet_count) &&
    nonNegativeInteger(value.audit_review_event_count) &&
    nonNegativeInteger(value.audit_lost_gap_count) &&
    (value.longest_lost_streak === null ||
      nonNegativeInteger(value.longest_lost_streak)) &&
    (value.false_positive_island_count === null ||
      nonNegativeInteger(value.false_positive_island_count)) &&
    (value.max_step_px === null || finite(value.max_step_px)) &&
    (value.quality_gate_status === null ||
      nonEmpty(value.quality_gate_status)) &&
    (value.trial_signal_gate_v2 === undefined ||
      value.trial_signal_gate_v2 === null ||
      isTrialSignalGateV2(value.trial_signal_gate_v2))
  );
}

function validVisualConfirmation(
  value: unknown,
  readiness: Record<string, unknown>,
): boolean {
  if (
    !isRecord(value) ||
    value.confirmed !== true ||
    !nonEmpty(value.confirmed_at) ||
    !sha256String(value.evidence_generation) ||
    !sha256String(value.threshold_profile_sha256) ||
    value.evidence_generation !== readiness.evidence_generation
  ) {
    return false;
  }
  const quality = readiness.quality;
  const gate = isRecord(quality) ? quality.trial_signal_gate_v2 : null;
  return (
    isTrialSignalGateV2(gate) &&
    value.threshold_profile_sha256 === gate.threshold_profile.sha256
  );
}

export function productionTrialAcceptanceIsValid(
  state: ProductionTrialState,
): state is ProductionTrialState & {
  accepted: ProductionTrialAcceptance;
} {
  const accepted = state.accepted;
  const attempt = state.attempts.at(-1);
  if (!accepted || !attempt || accepted.run_id !== attempt.run_id) return false;
  const readiness = accepted.readiness;
  return Boolean(
    attempt.last_observed.status === "completed" &&
    accepted.intent_sha256 === attempt.intent_sha256 &&
    accepted.request_sha256 === attempt.request_sha256 &&
    nonEmpty(accepted.accepted_at) &&
    attempt.last_observed.evidence_generation ===
      readiness.evidence_generation &&
    readiness.run_id === accepted.run_id &&
    readiness.request_sha256 === accepted.request_sha256 &&
    sha256String(readiness.evidence_generation) &&
    nonEmpty(readiness.verified_at) &&
    nonEmpty(readiness.video_artifact_name) &&
    Array.isArray(readiness.artifact_names) &&
    productionTrialArtifactContract({
      enable_postprocess: Boolean(attempt.request.enable_postprocess),
      video_artifact_name: readiness.video_artifact_name,
      artifact_names: readiness.artifact_names,
    }).matches === true &&
    validQualitySummary(readiness.quality) &&
    productionTrialSignalGateAcceptable(
      readiness.quality.trial_signal_gate_v2,
    ) &&
    validVisualConfirmation(
      readiness.operator_visual_confirmation,
      readiness as unknown as Record<string, unknown>,
    ) &&
    state.active_run_id === null &&
    state.pending_submission === null,
  );
}

export function isProductionTrialState(
  value: unknown,
): value is ProductionTrialState {
  if (!isRecord(value) || !isProductionTrialSettings(value.settings))
    return false;
  if (!Array.isArray(value.attempts)) return false;
  const attempts = value.attempts as unknown[];
  const ids = new Set<string>();
  const submissions = new Set<string>();
  let previousGeneration = 0;
  for (const [index, candidate] of attempts.entries()) {
    const previousRunId =
      index === 0 || !isRecord(attempts[index - 1])
        ? null
        : String((attempts[index - 1] as Record<string, unknown>).run_id);
    if (
      !isRecord(candidate) ||
      !nonEmpty(candidate.run_id) ||
      !positiveInteger(candidate.generation) ||
      candidate.generation <= previousGeneration ||
      !nonEmpty(candidate.submission_id) ||
      ids.has(candidate.run_id) ||
      submissions.has(candidate.submission_id) ||
      !(
        candidate.parent_run_id === null || nonEmpty(candidate.parent_run_id)
      ) ||
      !sha256String(candidate.intent_sha256) ||
      !sha256String(candidate.request_sha256) ||
      !validTrialRequestSnapshot(candidate.request, {
        submission_id: candidate.submission_id,
        parent_run_id: previousRunId,
      }) ||
      !nonEmpty(candidate.created_at) ||
      !isRecord(candidate.last_observed) ||
      !RUN_STATUSES.has(String(candidate.last_observed.status)) ||
      !nonEmpty(candidate.last_observed.observed_at) ||
      !(
        candidate.last_observed.evidence_generation === null ||
        sha256String(candidate.last_observed.evidence_generation)
      )
    ) {
      return false;
    }
    if ((candidate.request.parent_run_id ?? null) !== candidate.parent_run_id) {
      return false;
    }
    ids.add(candidate.run_id);
    submissions.add(candidate.submission_id);
    previousGeneration = candidate.generation;
  }
  if (value.active_run_id !== null && !ids.has(String(value.active_run_id))) {
    return false;
  }
  const activeAttempts = attempts.filter((candidate) => {
    if (!isRecord(candidate) || !isRecord(candidate.last_observed))
      return false;
    return (
      candidate.last_observed.status === "queued" ||
      candidate.last_observed.status === "running"
    );
  });
  if (
    activeAttempts.length > 1 ||
    (value.active_run_id === null && activeAttempts.length !== 0) ||
    (value.active_run_id !== null &&
      (activeAttempts.length !== 1 ||
        activeAttempts[0] !== attempts.at(-1) ||
        String(
          (activeAttempts[0] as Record<string, unknown> | undefined)?.run_id,
        ) !== value.active_run_id))
  ) {
    return false;
  }
  if (value.active_run_id !== null) {
    const active = attempts.find(
      (candidate) =>
        isRecord(candidate) && candidate.run_id === value.active_run_id,
    );
    const activeObservation = isRecord(active) ? active.last_observed : null;
    if (
      !isRecord(activeObservation) ||
      (activeObservation.status !== "queued" &&
        activeObservation.status !== "running")
    ) {
      return false;
    }
  }
  if (value.pending_submission !== null) {
    const pending = value.pending_submission;
    if (
      !isRecord(pending) ||
      !positiveInteger(pending.generation) ||
      pending.generation <= previousGeneration ||
      !nonEmpty(pending.submission_id) ||
      submissions.has(pending.submission_id) ||
      !nonEmpty(pending.output_id) ||
      !sha256String(pending.intent_sha256) ||
      !sha256String(pending.request_sha256) ||
      !validTrialRequestSnapshot(pending.request, {
        submission_id: pending.submission_id,
        output_id: pending.output_id,
        parent_run_id:
          attempts.length === 0 || !isRecord(attempts[attempts.length - 1])
            ? null
            : String(
                (attempts[attempts.length - 1] as Record<string, unknown>)
                  .run_id,
              ),
      }) ||
      !nonEmpty(pending.created_at)
    ) {
      return false;
    }
    const latest = attempts.at(-1);
    if (
      isRecord(latest) &&
      isRecord(latest.last_observed) &&
      (latest.last_observed.status === "queued" ||
        latest.last_observed.status === "running")
    ) {
      return false;
    }
  }
  if (value.accepted !== null) {
    if (
      !productionTrialAcceptanceIsValid(
        value as unknown as ProductionTrialState,
      )
    ) {
      return false;
    }
  }
  return true;
}
