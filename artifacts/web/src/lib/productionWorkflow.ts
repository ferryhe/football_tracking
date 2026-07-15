export const PRODUCTION_DRAFT_SCHEMA_VERSION = 1 as const;
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

export interface ProductionCalibrationEvidence {
  polygon_digest: string;
  confirmed_frame_ids: string[];
}

export interface ProductionTrialEvidence {
  latest_run_id: string;
  accepted_run_id: string | null;
}

export interface ProductionConfigEvidence {
  name: string;
  sha256: string;
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
  calibration: ProductionCalibrationEvidence | null;
  trial: ProductionTrialEvidence | null;
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
): value is ProductionCalibrationEvidence {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.polygon_digest) &&
    Array.isArray(value.confirmed_frame_ids) &&
    value.confirmed_frame_ids.every(isNonEmptyString)
  );
}

function isTrialEvidence(value: unknown): value is ProductionTrialEvidence {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.latest_run_id) &&
    (value.accepted_run_id === null || isNonEmptyString(value.accepted_run_id))
  );
}

function isConfigEvidence(value: unknown): value is ProductionConfigEvidence {
  if (!isRecord(value)) return false;
  return isNonEmptyString(value.name) && isSha256(value.sha256);
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
  return (
    value.schema_version === PRODUCTION_DRAFT_SCHEMA_VERSION &&
    isNonEmptyString(value.workflow_id) &&
    isNonEmptyString(value.created_at) &&
    isNonEmptyString(value.updated_at) &&
    typeof value.status === "string" &&
    DRAFT_STATUSES.has(value.status as ProductionDraftStatus) &&
    isNullable(value.source, isSourceSignature) &&
    isNullable(value.calibration, isCalibrationEvidence) &&
    isNullable(value.trial, isTrialEvidence) &&
    isNullable(value.confirmed_config, isConfigEvidence) &&
    isNullable(value.full_run, isFullRunEvidence) &&
    isNullable(value.verified_product, isProductEvidence)
  );
}

function migrateVersionZero(
  value: Record<string, unknown>,
): ProductionDraft | null {
  if (
    !isNonEmptyString(value.workflow_id) ||
    !isNonEmptyString(value.created_at) ||
    !isNonEmptyString(value.updated_at) ||
    !isNullable(value.source, isSourceSignature)
  ) {
    return null;
  }

  return {
    schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
    workflow_id: value.workflow_id,
    created_at: value.created_at,
    updated_at: value.updated_at,
    status: "active",
    source: value.source,
    calibration: null,
    trial: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

function hasConfirmedCalibration(draft: ProductionDraft): boolean {
  return Boolean(
    draft.calibration?.polygon_digest &&
    new Set(draft.calibration.confirmed_frame_ids).size >= 3,
  );
}

function hasAcceptedTrial(draft: ProductionDraft): boolean {
  return Boolean(draft.trial?.accepted_run_id);
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
  if (from === "source" || from === "calibration" || from === "trial") {
    updated.trial = null;
  }
  if (
    from === "source" ||
    from === "calibration" ||
    from === "trial" ||
    from === "config_confirmation"
  ) {
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
  if (value.schema_version === 0) {
    const migrated = migrateVersionZero(value);
    return migrated
      ? { status: "restored", draft: migrated, migrated: true }
      : { status: "corrupt", message: "Legacy draft is invalid." };
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
  try {
    storage.setItem(PRODUCTION_DRAFT_STORAGE_KEY, JSON.stringify(draft));
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
