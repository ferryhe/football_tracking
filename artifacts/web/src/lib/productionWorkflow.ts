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
  productionTrialAcceptanceIsValid,
  productionTrialMatchesContext,
  type ProductionTrialState,
} from "./productionTrial";
import {
  isProductionConfigEvidence,
  isProductionPendingConfigConfirmation,
  type ProductionConfigEvidence,
  type ProductionPendingConfigConfirmation,
} from "./productionConfigFreeze";
import {
  isProductionFullRunState,
  productionFullRunMatchesContext,
  type ProductionFullRunAttempt,
  type ProductionFullRunState,
} from "./productionBroadcast";
import {
  isProductionProductEvidence,
  type ProductionProductEvidence,
} from "./broadcastDelivery";

export const PRODUCTION_DRAFT_SCHEMA_VERSION = 5 as const;
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

export type { ProductionFullRunState, ProductionProductEvidence };

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
  full_run: ProductionFullRunState | null;
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
      isNullable(value.full_run, isProductionFullRunState) &&
      isNullable(value.verified_product, isProductionProductEvidence)
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
  if (fullRun !== null) {
    if (
      !source ||
      !calibration ||
      !trial ||
      !confirmedConfig ||
      !productionFullRunMatchesContext(fullRun, {
        workflow_id: value.workflow_id,
        source,
        calibration,
        trial,
        confirmed_config: confirmedConfig,
      })
    ) {
      return false;
    }
  }
  if (product !== null && !productMatchesFullRun(product, fullRun))
    return false;
  if (value.status === "completed" && product === null) return false;
  if (value.status === "active" && product !== null) return false;
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
      value.schema_version === 1 && value.status === "archived"
        ? "archived"
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

function migrateV3Draft(
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

  const source = value.source;
  const calibration =
    source && isCalibrationEvidence(value.calibration)
      ? value.calibration
      : null;
  let trial: ProductionTrialState | null = null;
  let acceptancePreserved = false;
  if (
    source &&
    calibration &&
    calibrationIsComplete(calibration, source.path) &&
    isRecord(value.trial)
  ) {
    const rawTrial = value.trial;
    const strictTrial = isProductionTrialState(rawTrial)
      ? rawTrial
      : isProductionTrialState({ ...rawTrial, accepted: null })
        ? ({ ...rawTrial, accepted: null } as ProductionTrialState)
        : null;
    if (
      strictTrial &&
      productionTrialMatchesContext(strictTrial, {
        workflow_id: value.workflow_id,
        source,
        calibration,
      })
    ) {
      trial = strictTrial;
      acceptancePreserved = productionTrialAcceptanceIsValid(strictTrial);
    }
  }
  const hadUntrustedAcceptance =
    isRecord(value.trial) &&
    value.trial.accepted !== null &&
    value.trial.accepted !== undefined &&
    !acceptancePreserved;
  const confirmedConfig =
    acceptancePreserved &&
    trial?.accepted &&
    isProductionConfigEvidence(value.confirmed_config) &&
    value.confirmed_config.workflow_id === value.workflow_id &&
    value.confirmed_config.accepted_trial_run_id === trial.accepted.run_id &&
    value.confirmed_config.trial_intent_sha256 ===
      trial.accepted.intent_sha256 &&
    value.confirmed_config.trial_request_sha256 ===
      trial.accepted.request_sha256 &&
    value.confirmed_config.calibration_digest === calibration?.polygon_digest &&
    sourceSignaturesMatch(value.confirmed_config.source_signature, source)
      ? value.confirmed_config
      : null;
  const migrated: ProductionDraft = {
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    workflow_id: value.workflow_id,
    created_at: value.created_at,
    updated_at: value.updated_at,
    status:
      !hadUntrustedAcceptance && value.status === "archived"
        ? "archived"
        : "active",
    source,
    calibration,
    trial,
    pending_config_confirmation: null,
    confirmed_config: confirmedConfig,
    full_run: null,
    verified_product: null,
  };
  return isProductionDraft(migrated) ? migrated : null;
}

function migrateV4Draft(
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

  const source = value.source;
  const calibration =
    source && isCalibrationEvidence(value.calibration)
      ? value.calibration
      : value.calibration === null
        ? null
        : undefined;
  if (calibration === undefined) return null;

  let trial: ProductionTrialState | null = null;
  let acceptancePreserved = false;
  if (value.trial !== null) {
    if (
      !source ||
      !calibration ||
      !calibrationIsComplete(calibration, source.path) ||
      !isRecord(value.trial)
    ) {
      return null;
    }
    const rawTrial = value.trial;
    const strictTrial = isProductionTrialState(rawTrial)
      ? rawTrial
      : isProductionTrialState({ ...rawTrial, accepted: null })
        ? ({ ...rawTrial, accepted: null } as ProductionTrialState)
        : null;
    if (
      !strictTrial ||
      !productionTrialMatchesContext(strictTrial, {
        workflow_id: value.workflow_id,
        source,
        calibration,
      })
    ) {
      return null;
    }
    trial = strictTrial;
    acceptancePreserved = productionTrialAcceptanceIsValid(strictTrial);
  }

  if (!acceptancePreserved) {
    const migrated: ProductionDraft = {
      schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
      workflow_id: value.workflow_id,
      created_at: value.created_at,
      updated_at: value.updated_at,
      status: value.status === "archived" ? "archived" : "active",
      source,
      calibration,
      trial,
      pending_config_confirmation: null,
      confirmed_config: null,
      full_run: null,
      verified_product: null,
    };
    return isProductionDraft(migrated) ? migrated : null;
  }

  const migrated = {
    ...value,
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    trial,
  };
  return isProductionDraft(migrated) ? migrated : null;
}

function hasConfirmedCalibration(draft: ProductionDraft): boolean {
  return Boolean(
    draft.source && calibrationIsComplete(draft.calibration, draft.source.path),
  );
}

function hasAcceptedTrial(draft: ProductionDraft): boolean {
  return Boolean(draft.trial && productionTrialAcceptanceIsValid(draft.trial));
}

function hasConfirmedConfig(draft: ProductionDraft): boolean {
  return Boolean(draft.confirmed_config?.name && draft.confirmed_config.sha256);
}

function currentFullRunAttempt(
  fullRun: ProductionFullRunState | null,
): ProductionFullRunAttempt | null {
  if (!fullRun?.current_run_id) return null;
  return (
    fullRun.attempts.find(
      (attempt) => attempt.run_id === fullRun.current_run_id,
    ) ?? null
  );
}

function productMatchesFullRun(
  product: ProductionProductEvidence,
  fullRun: ProductionFullRunState | null,
): boolean {
  const current = currentFullRunAttempt(fullRun);
  return Boolean(
    current &&
    current.run_id === product.run_id &&
    current.last_observed.workflow_state === "ready" &&
    current.last_observed.status_generation === product.status_generation,
  );
}

function hasVerifiedProduct(draft: ProductionDraft): boolean {
  return Boolean(
    draft.verified_product &&
    productMatchesFullRun(draft.verified_product, draft.full_run),
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
  if (!draft.full_run || draft.full_run.pending_submission) {
    return {
      stage: "full_tracking",
      user_stage: "full_tracking",
      delivery_blocked: false,
    };
  }
  const current = currentFullRunAttempt(draft.full_run);
  if (!current) {
    return {
      stage: "full_tracking",
      user_stage: "full_tracking",
      delivery_blocked: false,
    };
  }

  switch (current.last_observed.workflow_state) {
    case "tracking":
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
  const current = currentFullRunAttempt(draft.full_run);
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
      return current?.last_observed.workflow_state === "needs_review";
    case "recomputing":
      return current?.last_observed.workflow_state === "recomputing";
    case "trajectory_ready":
      return current?.last_observed.workflow_state === "trajectory_ready";
    case "rendering":
      return (
        current?.last_observed.workflow_state === "rendering" ||
        current?.last_observed.workflow_state === "ready"
      );
    case "ready":
      return hasVerifiedProduct(draft);
    case "failed":
      return current?.last_observed.workflow_state === "failed";
    case "cancelled":
      return current?.last_observed.workflow_state === "cancelled";
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
  createWorkflowId: () => string = createProductionWorkflowId,
): ProductionDraft {
  const invalidatesWorkflowRoot = from === "source" || from === "calibration";
  const trialHasLineage = Boolean(
    draft.trial &&
    (draft.trial.attempts.length > 0 ||
      draft.trial.pending_submission ||
      draft.trial.active_run_id ||
      draft.trial.accepted),
  );
  const hasDownstreamLineage = Boolean(
    trialHasLineage ||
    draft.pending_config_confirmation ||
    draft.confirmed_config ||
    draft.full_run ||
    draft.verified_product,
  );
  const updated: ProductionDraft = {
    ...draft,
    workflow_id:
      invalidatesWorkflowRoot && hasDownstreamLineage
        ? createWorkflowId()
        : draft.workflow_id,
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
  createWorkflowId: () => string = createProductionWorkflowId,
): ProductionDraft {
  if (sourceSignaturesMatch(draft.source, source)) return draft;
  return {
    ...invalidateProductionDraft(draft, "calibration", now, createWorkflowId),
    source,
  };
}

export function updateProductionCalibration(
  draft: ProductionDraft,
  calibration: ProductionCalibrationDraft,
  now = new Date().toISOString(),
  createWorkflowId: () => string = createProductionWorkflowId,
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
  const current = approvedChanged
    ? invalidateProductionDraft(draft, "calibration", now, createWorkflowId)
    : draft;
  return {
    ...current,
    updated_at: now,
    status: "active",
    calibration,
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

function fullRunAttemptIdentityMatches(
  left: ProductionFullRunAttempt,
  right: ProductionFullRunAttempt,
): boolean {
  return (
    left.run_id === right.run_id &&
    left.generation === right.generation &&
    left.submission_id === right.submission_id &&
    left.parent_trial_run_id === right.parent_trial_run_id &&
    left.config_name === right.config_name &&
    left.config_sha256 === right.config_sha256 &&
    left.request_sha256 === right.request_sha256 &&
    left.created_at === right.created_at &&
    JSON.stringify(left.request) === JSON.stringify(right.request)
  );
}

function productionFullRunTransitionIsValid(
  previous: ProductionFullRunState | null,
  next: ProductionFullRunState,
): boolean {
  const prior: ProductionFullRunState = previous ?? {
    revision: 0,
    attempts: [],
    pending_submission: null,
    current_run_id: null,
  };
  if (next.revision !== prior.revision + 1) return false;
  if (next.attempts.length < prior.attempts.length) return false;
  if (
    prior.attempts.some(
      (attempt, index) =>
        !fullRunAttemptIdentityMatches(attempt, next.attempts[index]),
    )
  ) {
    return false;
  }

  if (prior.pending_submission) {
    if (
      next.pending_submission === null &&
      next.attempts.length === prior.attempts.length &&
      next.current_run_id === prior.current_run_id &&
      JSON.stringify(next.attempts) === JSON.stringify(prior.attempts)
    ) {
      return true;
    }
    if (
      next.pending_submission !== null ||
      next.attempts.length !== prior.attempts.length + 1
    ) {
      return false;
    }
    const appended = next.attempts.at(-1);
    return Boolean(
      appended &&
      appended.run_id === prior.pending_submission.expected_run_id &&
      appended.generation === prior.pending_submission.generation &&
      appended.submission_id === prior.pending_submission.submission_id &&
      appended.parent_trial_run_id ===
        prior.pending_submission.accepted_trial_run_id &&
      appended.config_name === prior.pending_submission.config_name &&
      appended.config_sha256 === prior.pending_submission.config_sha256 &&
      appended.request_sha256 === prior.pending_submission.request_sha256 &&
      next.current_run_id === appended.run_id,
    );
  }

  if (next.pending_submission) {
    const nextGeneration =
      Math.max(0, ...prior.attempts.map((attempt) => attempt.generation)) + 1;
    return (
      next.attempts.length === prior.attempts.length &&
      next.current_run_id === prior.current_run_id &&
      next.pending_submission.generation === nextGeneration
    );
  }

  if (
    next.attempts.length !== prior.attempts.length ||
    next.current_run_id !== prior.current_run_id
  ) {
    return false;
  }
  const changedObservations = prior.attempts.filter(
    (attempt, index) =>
      JSON.stringify(attempt.last_observed) !==
      JSON.stringify(next.attempts[index].last_observed),
  ).length;
  return changedObservations === 1;
}

export function updateProductionFullRun(
  draft: ProductionDraft,
  fullRun: ProductionFullRunState,
  expectedRevision: number,
  now = new Date().toISOString(),
): ProductionDraft {
  const currentRevision = draft.full_run?.revision ?? 0;
  if (expectedRevision !== currentRevision) {
    throw new TypeError("Production full-run revision conflict");
  }
  if (!isProductionFullRunState(fullRun)) {
    throw new TypeError("Invalid production full-run state");
  }
  if (!productionFullRunTransitionIsValid(draft.full_run, fullRun)) {
    throw new TypeError("Invalid production full-run transition");
  }
  if (
    !draft.source ||
    !draft.calibration ||
    !draft.trial ||
    !draft.confirmed_config ||
    !productionFullRunMatchesContext(fullRun, {
      workflow_id: draft.workflow_id,
      source: draft.source,
      calibration: draft.calibration,
      trial: draft.trial,
      confirmed_config: draft.confirmed_config,
    })
  ) {
    throw new TypeError("Production full-run state does not match the draft");
  }
  const verifiedProduct =
    draft.verified_product &&
    productMatchesFullRun(draft.verified_product, fullRun)
      ? draft.verified_product
      : null;
  const candidate: ProductionDraft = {
    ...draft,
    updated_at: now,
    status: verifiedProduct ? draft.status : "active",
    full_run: fullRun,
    verified_product: verifiedProduct,
  };
  if (!isProductionDraft(candidate)) {
    throw new TypeError("Production full-run state does not match the draft");
  }
  return candidate;
}

export function updateVerifiedProductionProduct(
  draft: ProductionDraft,
  product: ProductionProductEvidence,
  expectedFullRunRevision: number,
  now = new Date().toISOString(),
): ProductionDraft {
  if (!isProductionProductEvidence(product)) {
    throw new TypeError("Invalid production product evidence");
  }
  if (!draft.full_run || draft.full_run.revision !== expectedFullRunRevision) {
    throw new TypeError("Production full-run revision conflict");
  }
  if (!productMatchesFullRun(product, draft.full_run)) {
    throw new TypeError(
      "Production product does not match the current full run",
    );
  }
  const candidate: ProductionDraft = {
    ...draft,
    updated_at: now,
    status: "completed",
    verified_product: product,
  };
  if (!isProductionDraft(candidate)) {
    throw new TypeError("Production product does not match the current draft");
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
  if (value.schema_version === 3) {
    const migrated = migrateV3Draft(value);
    return migrated
      ? { status: "restored", draft: migrated, migrated: true }
      : { status: "corrupt", message: "Version 3 draft is invalid." };
  }
  if (value.schema_version === 4) {
    const migrated = migrateV4Draft(value);
    return migrated
      ? { status: "restored", draft: migrated, migrated: true }
      : { status: "corrupt", message: "Version 4 draft is invalid." };
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
