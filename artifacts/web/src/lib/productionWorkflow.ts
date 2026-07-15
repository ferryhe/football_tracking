import {
  calibrationIsComplete,
  resolutionsMatch,
  type ProductionCalibrationDraft,
  type ProductionCalibrationFrame,
  type ProductionCalibrationSuggestion,
} from "./productionCalibration";
import type { FieldPoint, FieldResolution } from "./fieldGeometry";
import {
  isProductionTrialState,
  productionTrialMatchesContext,
  type ProductionTrialState,
} from "./productionTrial";
import {
  isProductionConfigEvidence,
  isProductionPendingConfigConfirmation,
  type ProductionConfigEvidence,
  type ProductionPendingConfigConfirmation,
} from "./productionConfigFreeze";

export const PRODUCTION_DRAFT_SCHEMA_VERSION = 3 as const;
export const PRODUCTION_DRAFT_STORAGE_KEY =
  "football-tracking.production-draft.v1";

export type ProductionDraftStatus = "active" | "completed" | "archived";

export type ProductionWorkflowStage =
  | "source"
  | "calibration"
  | "trial"
  | "config_confirmation"
  | "full_tracking"
  | "review"
  | "recomputing"
  | "trajectory_ready"
  | "rendering"
  | "ready"
  | "failed"
  | "cancelled";

export type ProductionUserStage =
  | "source"
  | "calibration"
  | "trial"
  | "full_tracking"
  | "ready";

export interface SourceSignature {
  path: string;
  size_bytes: number;
  modified_at: string;
}

export type ProductionFullRunStatus =
  | "queued"
  | "running"
  | "needs_review"
  | "recomputing"
  | "trajectory_ready"
  | "rendering"
  | "ready"
  | "failed"
  | "cancelled";

export interface ProductionFullRunEvidence {
  run_id: string;
  status: ProductionFullRunStatus;
}

export interface ProductionProductEvidence {
  run_id: string;
  artifact_name: "broadcast.mp4";
  status_generation: string;
}

export interface ProductionDraft {
  schema_version: typeof PRODUCTION_DRAFT_SCHEMA_VERSION;
  workflow_id: string;
  created_at: string;
  updated_at: string;
  status: ProductionDraftStatus;
  source: SourceSignature | null;
  calibration: ProductionCalibrationDraft | null;
  trial: ProductionTrialState | null;
  pending_config_confirmation: ProductionPendingConfigConfirmation | null;
  confirmed_config: ProductionConfigEvidence | null;
  full_run: ProductionFullRunEvidence | null;
  verified_product: ProductionProductEvidence | null;
}

export interface DerivedProductionWorkflow {
  stage: ProductionWorkflowStage;
  user_stage: ProductionUserStage;
  delivery_blocked: boolean;
}

export type ProductionDraftLoadResult =
  | { status: "empty" }
  | { status: "restored"; draft: ProductionDraft; migrated: boolean }
  | { status: "corrupt"; message: string }
  | { status: "unsupported"; version: number }
  | { status: "unavailable"; message: string };

export type ProductionDraftStorageResult =
  | { ok: true }
  | { ok: false; message: string };

type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

const FULL_RUN_STATUSES = new Set<ProductionFullRunStatus>([
  "queued",
  "running",
  "needs_review",
  "recomputing",
  "trajectory_ready",
  "rendering",
  "ready",
  "failed",
  "cancelled",
]);

const DRAFT_STATUSES = new Set<ProductionDraftStatus>([
  "active",
  "completed",
  "archived",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f\d]{64}$/i.test(value);
}

function isFieldPoint(value: unknown): value is FieldPoint {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    value.every(
      (coordinate) =>
        typeof coordinate === "number" && Number.isFinite(coordinate),
    )
  );
}

function isFieldResolution(value: unknown): value is FieldResolution {
  return (
    isRecord(value) &&
    typeof value.width === "number" &&
    Number.isFinite(value.width) &&
    value.width > 0 &&
    typeof value.height === "number" &&
    Number.isFinite(value.height) &&
    value.height > 0
  );
}

function isPointList(value: unknown): value is FieldPoint[] {
  return Array.isArray(value) && value.every(isFieldPoint);
}

function isCalibrationSuggestion(
  value: unknown,
): value is ProductionCalibrationSuggestion {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.source_path) &&
    isNonEmptyString(value.source) &&
    (value.confidence === "config" ||
      value.confidence === "detected" ||
      value.confidence === "fallback") &&
    typeof value.field_coverage === "number" &&
    Number.isFinite(value.field_coverage) &&
    value.field_coverage >= 0 &&
    value.field_coverage <= 1 &&
    isFieldResolution(value.source_resolution) &&
    typeof value.frame_index === "number" &&
    Number.isInteger(value.frame_index) &&
    value.frame_index >= 0 &&
    isPointList(value.polygon)
  );
}

function isCalibrationFrame(
  value: unknown,
): value is ProductionCalibrationFrame {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.input_video) &&
    typeof value.frame_index === "number" &&
    Number.isInteger(value.frame_index) &&
    value.frame_index >= 0 &&
    typeof value.frame_time_seconds === "number" &&
    Number.isFinite(value.frame_time_seconds) &&
    value.frame_time_seconds >= 0 &&
    typeof value.sample_index === "number" &&
    Number.isInteger(value.sample_index) &&
    value.sample_index >= 0 &&
    isFieldResolution(value.source_resolution) &&
    isSha256(value.polygon_digest)
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isSourceSignature(value: unknown): value is SourceSignature {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.path) &&
    typeof value.size_bytes === "number" &&
    Number.isFinite(value.size_bytes) &&
    value.size_bytes >= 0 &&
    isNonEmptyString(value.modified_at)
  );
}

function isCalibrationEvidence(
  value: unknown,
): value is ProductionCalibrationDraft {
  if (!isRecord(value)) return false;
  return (
    isNullable(value.source_resolution, isFieldResolution) &&
    isNullable(value.suggestion, isCalibrationSuggestion) &&
    isPointList(value.approved_polygon) &&
    Array.isArray(value.exclusions) &&
    value.exclusions.every(isPointList) &&
    (value.polygon_digest === null || isSha256(value.polygon_digest)) &&
    Array.isArray(value.confirmed_frames) &&
    value.confirmed_frames.every(isCalibrationFrame)
  );
}

function isFullRunEvidence(value: unknown): value is ProductionFullRunEvidence {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.run_id) &&
    typeof value.status === "string" &&
    FULL_RUN_STATUSES.has(value.status as ProductionFullRunStatus)
  );
}

function isProductEvidence(value: unknown): value is ProductionProductEvidence {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.run_id) &&
    value.artifact_name === "broadcast.mp4" &&
    isSha256(value.status_generation)
  );
}

function isNullable<T>(
  value: unknown,
  predicate: (candidate: unknown) => candidate is T,
): value is T | null {
  return value === null || predicate(value);
}

function isProductionDraft(value: unknown): value is ProductionDraft {
  if (!isRecord(value)) return false;
  if (
    !(
      value.schema_version === PRODUCTION_DRAFT_SCHEMA_VERSION &&
      isNonEmptyString(value.workflow_id) &&
      isNonEmptyString(value.created_at) &&
      isNonEmptyString(value.updated_at) &&
      typeof value.status === "string" &&
      DRAFT_STATUSES.has(value.status as ProductionDraftStatus) &&
      isNullable(value.source, isSourceSignature) &&
      isNullable(value.calibration, isCalibrationEvidence) &&
      isNullable(value.trial, isProductionTrialState) &&
      isNullable(
        value.pending_config_confirmation,
        isProductionPendingConfigConfirmation,
      ) &&
      isNullable(value.confirmed_config, isProductionConfigEvidence) &&
      isNullable(value.full_run, isFullRunEvidence) &&
      isNullable(value.verified_product, isProductEvidence)
    )
  )
    return false;

  const source = value.source;
  const calibration = value.calibration;
  const trial = value.trial;
  const pendingConfig = value.pending_config_confirmation;
  const confirmedConfig = value.confirmed_config;
  const fullRun = value.full_run;
  const product = value.verified_product;
  if (calibration !== null && source === null) return false;
  if (
    trial !== null &&
    (!source ||
      !calibration ||
      !calibrationIsComplete(calibration, source.path) ||
      !productionTrialMatchesContext(trial, {
        workflow_id: value.workflow_id,
        source,
        calibration,
      }))
  )
    return false;
  if (pendingConfig !== null) {
    if (
      !trial?.accepted ||
      pendingConfig.workflow_id !== value.workflow_id ||
      pendingConfig.accepted_trial_run_id !== trial.accepted.run_id ||
      pendingConfig.trial_intent_sha256 !== trial.accepted.intent_sha256 ||
      pendingConfig.trial_request_sha256 !== trial.accepted.request_sha256 ||
      pendingConfig.calibration_digest !== calibration?.polygon_digest ||
      !sourceSignaturesMatch(pendingConfig.source_signature, source)
    )
      return false;
  }
  if (confirmedConfig !== null) {
    if (
      !trial?.accepted ||
      confirmedConfig.workflow_id !== value.workflow_id ||
      confirmedConfig.accepted_trial_run_id !== trial.accepted.run_id ||
      confirmedConfig.trial_intent_sha256 !== trial.accepted.intent_sha256 ||
      confirmedConfig.trial_request_sha256 !== trial.accepted.request_sha256 ||
      confirmedConfig.calibration_digest !== calibration?.polygon_digest ||
      !sourceSignaturesMatch(confirmedConfig.source_signature, source)
    )
      return false;
  }
  if (fullRun !== null && confirmedConfig === null) return false;
  if (
    product !== null &&
    (fullRun === null || product.run_id !== fullRun.run_id)
  ) {
    return false;
  }
  return true;
}

function migrateV0OrV1Draft(
  value: Record<string, unknown>,
): ProductionDraft | null {
  if (
    !isNonEmptyString(value.workflow_id) ||
    !isNonEmptyString(value.created_at) ||
    !isNonEmptyString(value.updated_at) ||
    !isNullable(value.source, isSourceSignature) ||
    (value.schema_version === 1 &&
      (typeof value.status !== "string" ||
        !DRAFT_STATUSES.has(value.status as ProductionDraftStatus)))
  ) {
    return null;
  }

  return {
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    workflow_id: value.workflow_id,
    created_at: value.created_at,
    updated_at: value.updated_at,
    status:
      value.schema_version === 1
        ? (value.status as ProductionDraftStatus)
        : "active",
    source: value.source,
    calibration: null,
    trial: null,
    pending_config_confirmation: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

function migrateV2Draft(
  value: Record<string, unknown>,
): ProductionDraft | null {
  if (
    !isNonEmptyString(value.workflow_id) ||
    !isNonEmptyString(value.created_at) ||
    !isNonEmptyString(value.updated_at) ||
    !isNullable(value.source, isSourceSignature) ||
    typeof value.status !== "string" ||
    !DRAFT_STATUSES.has(value.status as ProductionDraftStatus)
  ) {
    return null;
  }
  const calibration =
    value.source && isCalibrationEvidence(value.calibration)
      ? value.calibration
      : null;
  return {
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    workflow_id: value.workflow_id,
    created_at: value.created_at,
    updated_at: value.updated_at,
    status: "active",
    source: value.source,
    calibration,
    trial: null,
    pending_config_confirmation: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

function hasConfirmedCalibration(draft: ProductionDraft): boolean {
  return Boolean(
    draft.source && calibrationIsComplete(draft.calibration, draft.source.path),
  );
}

function hasAcceptedTrial(draft: ProductionDraft): boolean {
  return Boolean(draft.trial?.accepted?.run_id);
}

function hasConfirmedConfig(draft: ProductionDraft): boolean {
  return Boolean(draft.confirmed_config?.name && draft.confirmed_config.sha256);
}

function hasVerifiedProduct(draft: ProductionDraft): boolean {
  return Boolean(
    draft.full_run?.status === "ready" &&
    draft.verified_product?.run_id === draft.full_run.run_id &&
    draft.verified_product.artifact_name === "broadcast.mp4" &&
    isSha256(draft.verified_product.status_generation),
  );
}

export interface ProductionIdCrypto {
  randomUUID?: () => string;
  getRandomValues: (bytes: Uint8Array) => Uint8Array;
}

export function createProductionWorkflowId(
  source: ProductionIdCrypto = globalThis.crypto as ProductionIdCrypto,
): string {
  if (typeof source.randomUUID === "function") return source.randomUUID();

  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

export function createProductionDraft(
  now = new Date().toISOString(),
  workflowId: string = createProductionWorkflowId(),
): ProductionDraft {
  return {
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    workflow_id: workflowId,
    created_at: now,
    updated_at: now,
    status: "active",
    source: null,
    calibration: null,
    trial: null,
    pending_config_confirmation: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

export function sourceSignaturesMatch(
  left: SourceSignature | null,
  right: SourceSignature | null,
): boolean {
  return Boolean(
    left &&
    right &&
    left.path === right.path &&
    left.size_bytes === right.size_bytes &&
    left.modified_at === right.modified_at,
  );
}

export function deriveProductionWorkflow(
  draft: ProductionDraft,
): DerivedProductionWorkflow {
  if (!draft.source) {
    return { stage: "source", user_stage: "source", delivery_blocked: false };
  }
  if (!hasConfirmedCalibration(draft)) {
    return {
      stage: "calibration",
      user_stage: "calibration",
      delivery_blocked: false,
    };
  }
  if (!hasAcceptedTrial(draft)) {
    return { stage: "trial", user_stage: "trial", delivery_blocked: false };
  }
  if (!hasConfirmedConfig(draft)) {
    return {
      stage: "config_confirmation",
      user_stage: "trial",
      delivery_blocked: false,
    };
  }
  if (!draft.full_run) {
    return {
      stage: "full_tracking",
      user_stage: "full_tracking",
      delivery_blocked: false,
    };
  }

  switch (draft.full_run.status) {
    case "queued":
    case "running":
      return {
        stage: "full_tracking",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
    case "needs_review":
      return {
        stage: "review",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
    case "recomputing":
      return {
        stage: "recomputing",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
    case "trajectory_ready":
      return {
        stage: "trajectory_ready",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
    case "rendering":
      return {
        stage: "rendering",
        user_stage: "ready",
        delivery_blocked: false,
      };
    case "ready":
      return hasVerifiedProduct(draft)
        ? { stage: "ready", user_stage: "ready", delivery_blocked: false }
        : { stage: "rendering", user_stage: "ready", delivery_blocked: true };
    case "failed":
      return {
        stage: "failed",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
    case "cancelled":
      return {
        stage: "cancelled",
        user_stage: "full_tracking",
        delivery_blocked: false,
      };
  }
}

export function canEnterProductionStage(
  draft: ProductionDraft,
  stage: ProductionWorkflowStage,
): boolean {
  switch (stage) {
    case "source":
      return true;
    case "calibration":
      return draft.source !== null;
    case "trial":
      return hasConfirmedCalibration(draft);
    case "config_confirmation":
      return hasConfirmedCalibration(draft) && hasAcceptedTrial(draft);
    case "full_tracking":
      return (
        hasConfirmedCalibration(draft) &&
        hasAcceptedTrial(draft) &&
        hasConfirmedConfig(draft)
      );
    case "review":
      return draft.full_run?.status === "needs_review";
    case "recomputing":
      return draft.full_run?.status === "recomputing";
    case "trajectory_ready":
      return draft.full_run?.status === "trajectory_ready";
    case "rendering":
      return (
        draft.full_run?.status === "rendering" ||
        draft.full_run?.status === "ready"
      );
    case "ready":
      return hasVerifiedProduct(draft);
    case "failed":
      return draft.full_run?.status === "failed";
    case "cancelled":
      return draft.full_run?.status === "cancelled";
  }
}

export function invalidateProductionDraft(
  draft: ProductionDraft,
  from:
    | "source"
    | "calibration"
    | "trial"
    | "config_confirmation"
    | "full_tracking",
  now = new Date().toISOString(),
): ProductionDraft {
  const updated: ProductionDraft = {
    ...draft,
    updated_at: now,
    status: "active",
  };

  if (from === "source") updated.source = null;
  if (from === "source" || from === "calibration") updated.calibration = null;
  if (from === "source" || from === "calibration") {
    updated.trial = null;
  } else if (from === "trial" && updated.trial) {
    updated.trial = {
      ...updated.trial,
      pending_submission: null,
      active_run_id: null,
      accepted: null,
    };
  }
  if (
    from === "source" ||
    from === "calibration" ||
    from === "trial" ||
    from === "config_confirmation"
  ) {
    updated.pending_config_confirmation = null;
    updated.confirmed_config = null;
  }
  updated.full_run = null;
  updated.verified_product = null;
  return updated;
}

export function updateProductionSource(
  draft: ProductionDraft,
  source: SourceSignature,
  now = new Date().toISOString(),
): ProductionDraft {
  if (sourceSignaturesMatch(draft.source, source)) return draft;
  return {
    ...invalidateProductionDraft(draft, "calibration", now),
    source,
  };
}

export function updateProductionCalibration(
  draft: ProductionDraft,
  calibration: ProductionCalibrationDraft,
  now = new Date().toISOString(),
): ProductionDraft {
  const approvedChanged =
    draft.calibration?.polygon_digest !== calibration.polygon_digest ||
    !resolutionsMatch(
      draft.calibration?.source_resolution ?? null,
      calibration.source_resolution,
    ) ||
    JSON.stringify(draft.calibration?.approved_polygon ?? []) !==
      JSON.stringify(calibration.approved_polygon) ||
    JSON.stringify(draft.calibration?.exclusions ?? []) !==
      JSON.stringify(calibration.exclusions);
  return {
    ...draft,
    updated_at: now,
    status: "active",
    calibration,
    trial: approvedChanged ? null : draft.trial,
    pending_config_confirmation: approvedChanged
      ? null
      : draft.pending_config_confirmation,
    confirmed_config: approvedChanged ? null : draft.confirmed_config,
    full_run: approvedChanged ? null : draft.full_run,
    verified_product: approvedChanged ? null : draft.verified_product,
  };
}

export function productionTrialRequiresStop(
  trial: ProductionTrialState | null,
): boolean {
  if (!trial) return false;
  return (
    trial.pending_submission !== null ||
    (typeof trial.active_run_id === "string" &&
      trial.active_run_id.trim().length > 0) ||
    trial.attempts.some(
      (attempt) =>
        attempt.last_observed.status === "queued" ||
        attempt.last_observed.status === "running",
    )
  );
}

export function updateProductionTrial(
  draft: ProductionDraft,
  trial: ProductionTrialState,
  now = new Date().toISOString(),
): ProductionDraft {
  if (!isProductionTrialState(trial)) {
    throw new TypeError("Invalid production trial state");
  }
  const acceptanceChanged =
    draft.trial?.accepted?.run_id !== trial.accepted?.run_id ||
    draft.trial?.accepted?.intent_sha256 !== trial.accepted?.intent_sha256 ||
    draft.trial?.accepted?.readiness.evidence_generation !==
      trial.accepted?.readiness.evidence_generation;
  const candidate: ProductionDraft = {
    ...draft,
    updated_at: now,
    status: "active",
    trial,
    pending_config_confirmation: acceptanceChanged
      ? null
      : draft.pending_config_confirmation,
    confirmed_config: acceptanceChanged ? null : draft.confirmed_config,
    full_run: acceptanceChanged ? null : draft.full_run,
    verified_product: acceptanceChanged ? null : draft.verified_product,
  };
  if (!isProductionDraft(candidate)) {
    throw new TypeError("Production trial does not match the current draft");
  }
  return candidate;
}

export function updatePendingConfigConfirmation(
  draft: ProductionDraft,
  pending: ProductionPendingConfigConfirmation | null,
  now = new Date().toISOString(),
): ProductionDraft {
  if (pending !== null && !isProductionPendingConfigConfirmation(pending)) {
    throw new TypeError("Invalid pending configuration confirmation");
  }
  const candidate: ProductionDraft = {
    ...draft,
    updated_at: now,
    pending_config_confirmation: pending,
    confirmed_config: pending ? null : draft.confirmed_config,
    full_run: pending ? null : draft.full_run,
    verified_product: pending ? null : draft.verified_product,
  };
  if (!isProductionDraft(candidate)) {
    throw new TypeError(
      "Pending configuration does not match the current draft",
    );
  }
  return candidate;
}

export function updateConfirmedProductionConfig(
  draft: ProductionDraft,
  confirmedConfig: ProductionConfigEvidence,
  now = new Date().toISOString(),
): ProductionDraft {
  if (!isProductionConfigEvidence(confirmedConfig)) {
    throw new TypeError("Invalid confirmed configuration evidence");
  }
  const candidate: ProductionDraft = {
    ...draft,
    updated_at: now,
    status: "active",
    pending_config_confirmation: null,
    confirmed_config: confirmedConfig,
    full_run: null,
    verified_product: null,
  };
  if (!isProductionDraft(candidate)) {
    throw new TypeError(
      "Confirmed configuration does not match the current draft",
    );
  }
  return candidate;
}

export function loadProductionDraft(
  storage: DraftStorage,
): ProductionDraftLoadResult {
  let raw: string | null;
  try {
    raw = storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY);
  } catch (error) {
    return { status: "unavailable", message: errorMessage(error) };
  }
  if (raw === null) return { status: "empty" };

  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    return { status: "corrupt", message: errorMessage(error) };
  }
  if (!isRecord(value) || typeof value.schema_version !== "number") {
    return { status: "corrupt", message: "Draft schema is invalid." };
  }
  if (value.schema_version > PRODUCTION_DRAFT_SCHEMA_VERSION) {
    return { status: "unsupported", version: value.schema_version };
  }
  if (value.schema_version === 0 || value.schema_version === 1) {
    const migrated = migrateV0OrV1Draft(value);
    return migrated
      ? { status: "restored", draft: migrated, migrated: true }
      : { status: "corrupt", message: "Legacy draft is invalid." };
  }
  if (value.schema_version === 2) {
    const migrated = migrateV2Draft(value);
    return migrated
      ? { status: "restored", draft: migrated, migrated: true }
      : { status: "corrupt", message: "Version 2 draft is invalid." };
  }
  if (!isProductionDraft(value)) {
    return { status: "corrupt", message: "Draft data is invalid." };
  }
  return { status: "restored", draft: value, migrated: false };
}

export function saveProductionDraft(
  storage: DraftStorage,
  draft: ProductionDraft,
): ProductionDraftStorageResult {
  if (!isProductionDraft(draft)) {
    return { ok: false, message: "Draft data is invalid." };
  }
  try {
    storage.setItem(
      PRODUCTION_DRAFT_STORAGE_KEY,
      JSON.stringify(draft, (key, value) =>
        key === "preview_data_url" ? undefined : value,
      ),
    );
    return { ok: true };
  } catch (error) {
    return { ok: false, message: errorMessage(error) };
  }
}

export function clearProductionDraft(
  storage: DraftStorage,
): ProductionDraftStorageResult {
  try {
    storage.removeItem(PRODUCTION_DRAFT_STORAGE_KEY);
    return { ok: true };
  } catch (error) {
    return { ok: false, message: errorMessage(error) };
  }
}

export function requiresDraftReplacementConfirmation(
  current: ProductionDraft,
  incomingWorkflowId: string,
): boolean {
  if (
    current.workflow_id === incomingWorkflowId ||
    current.status !== "active"
  ) {
    return false;
  }
  return Boolean(
    current.source ||
    current.calibration ||
    current.trial ||
    current.confirmed_config ||
    current.full_run ||
    current.verified_product,
  );
}

export type ProductionHistoryOpenAction =
  | "resume_current"
  | "confirm_replace"
  | "open_requested";

export function productionHistoryOpenAction(
  current: ProductionDraft,
  requestedWorkflowId: string,
): ProductionHistoryOpenAction {
  if (current.workflow_id === requestedWorkflowId) return "resume_current";
  return requiresDraftReplacementConfirmation(current, requestedWorkflowId)
    ? "confirm_replace"
    : "open_requested";
}
