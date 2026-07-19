import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLanguage } from "@/contexts/LanguageContext";
import type { SafeBrowserStorage } from "@/lib/browserStorage";
import { BALL_ANNOTATION_OPERATOR_GOVERNANCE } from "@/lib/ballAnnotationGovernance";
import {
  cancelBallPropagationJob,
  createBallAnnotationSession,
  createBallPropagationJob,
  finalizeBallAnnotationSession,
  getBallAnnotationResult,
  getBallAnnotationSession,
  getBallPropagationJob,
  getCustomFetchResponseMetadata,
  putBallAnnotation,
} from "@workspace/api-client-react";
import {
  ballAnnotationStorageKey,
  buildBallAnnotationMutation,
  buildBallAnnotationSessionRequest,
  buildBallPropagationRequest,
  fetchVerifiedBallAnnotationFrame,
  parseBallAnnotationFinalResult,
  parseBallAnnotationRevision,
  parseBallAnnotationSession,
  parseBallPropagationJob,
  type BallAnnotationApiValue,
  type BallPropagationCreateRequest,
  type BallAnnotationSessionCreateRequest,
  type ParsedBallAnnotationSession,
  type ParsedBallPropagationJob,
} from "@/lib/productionBallAnnotation";
import {
  cancelReviewProxyRepair,
  createReviewProxyRepair,
  getReviewProxyRepair,
  requireReviewProxyRepairRetryLineage,
  retryReviewProxyRepair,
  reviewProxyRepairUpdateIsCurrent,
  type ReviewProxyRepairJobView,
} from "@/lib/productionReviewProxyRepair";

import {
  ProductionBallAnnotationPanel,
  type BallAnnotationFrameView,
  type BallSuggestionDecision,
  type BallAnnotationValueView,
} from "./ProductionBallAnnotationPanel";
import { ProductionFeasibilityDashboard } from "./ProductionFeasibilityDashboard";

const ACTIVE_SESSION_STATUSES = new Set([
  "sampling_locked",
  "check_probe_queued",
  "check_probe_running",
  "check_probe_committing",
  "finalizing",
]);
const SCALE = ["near", "mid", "far"] as const;
const LIGHTING = [
  "bright_sun",
  "shadow",
  "backlight",
  "twilight",
  "artificial_light",
] as const;

interface StoredPendingCreate {
  schema_version: "1.0";
  artifact_type: "ball_annotation_pending_create";
  state: "pending_create";
  workflow_id: string;
  development_probe_job_ids: string[];
  locked_profile_id: string;
  request: BallAnnotationSessionCreateRequest;
}

interface StoredSessionPointer {
  schema_version: "1.0";
  artifact_type: "ball_annotation_session_pointer";
  state: "session_pointer";
  workflow_id: string;
  development_probe_job_ids: string[];
  locked_profile_id: string;
  session_id: string;
  data_role: "development" | "check";
  propagation_job?: {
    job_id: string | null;
    request: BallPropagationCreateRequest;
  };
  review_proxy_repair?: StoredReviewProxyRepair;
}

interface StoredReviewProxyRepair {
  repair_id: string | null;
  attempt_root_repair_id: string | null;
  attempt_number: number | null;
  retry_from_repair_id: string | null;
  blocked_session_id: string;
  request_sha256: string | null;
  parent_probe_job_id: string;
  parent_probe_record_sha256: string;
  blocked_session_record_sha256: string;
  child_probe_job_id: string | null;
  replacement_session_id: string | null;
}

type StoredRecovery = StoredPendingCreate | StoredSessionPointer;

export interface BallAnnotationLaunchRecovery {
  lockedProfileId: string;
  developmentProbeJobIds: string[];
}

function isSafeId(value: unknown): value is string {
  return (
    typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,119}$/.test(value)
  );
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function sameStrings(left: readonly string[], right: readonly string[]) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function parseStoredRecovery(
  raw: string | null,
  workflowId: string,
  developmentProbeJobIds: readonly string[],
): StoredRecovery | null {
  if (raw === null) return null;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Saved annotation recovery is invalid.");
  }
  const stored = value as Record<string, unknown>;
  const ids = Array.isArray(stored.development_probe_job_ids)
    ? stored.development_probe_job_ids
    : [];
  const canonicalExpected = [...developmentProbeJobIds].sort();
  if (
    stored.schema_version !== "1.0" ||
    stored.workflow_id !== workflowId ||
    !isSafeId(stored.locked_profile_id) ||
    ids.some((id) => !isSafeId(id))
  ) {
    throw new Error("Saved annotation recovery does not match this workflow.");
  }
  if (
    stored.state === "session_pointer" &&
    stored.artifact_type === "ball_annotation_session_pointer" &&
    isSafeId(stored.session_id) &&
    (stored.data_role === "development" || stored.data_role === "check") &&
    Object.keys(stored).sort().join("|") ===
      [
        "artifact_type",
        "data_role",
        "development_probe_job_ids",
        "locked_profile_id",
        ...(Object.prototype.hasOwnProperty.call(stored, "propagation_job")
          ? ["propagation_job"]
          : []),
        ...(Object.prototype.hasOwnProperty.call(stored, "review_proxy_repair")
          ? ["review_proxy_repair"]
          : []),
        "schema_version",
        "session_id",
        "state",
        "workflow_id",
      ]
        .sort()
        .join("|")
  ) {
    if ("propagation_job" in stored) {
      const recovery = stored.propagation_job;
      if (
        !recovery ||
        typeof recovery !== "object" ||
        Array.isArray(recovery)
      ) {
        throw new Error("Saved propagation recovery is invalid.");
      }
      const pointer = recovery as Record<string, unknown>;
      if (
        Object.keys(pointer).sort().join("|") !== "job_id|request" ||
        (pointer.job_id !== null && !isSafeId(pointer.job_id)) ||
        !pointer.request ||
        typeof pointer.request !== "object" ||
        Array.isArray(pointer.request)
      ) {
        throw new Error("Saved propagation recovery is invalid.");
      }
      const request = pointer.request as Record<string, unknown>;
      if (
        Object.keys(request).sort().join("|") !==
          "expected_seed_revision|mutation_id|radius_frames|seed_frame_index" ||
        !isSafeId(request.mutation_id) ||
        !Number.isSafeInteger(request.seed_frame_index) ||
        (request.seed_frame_index as number) < 0 ||
        !Number.isSafeInteger(request.expected_seed_revision) ||
        (request.expected_seed_revision as number) < 1 ||
        !Number.isSafeInteger(request.radius_frames) ||
        (request.radius_frames as number) < 1 ||
        (request.radius_frames as number) > 2
      ) {
        throw new Error("Saved propagation request is invalid.");
      }
    }
    let repair: StoredReviewProxyRepair | null = null;
    if ("review_proxy_repair" in stored) {
      const rawRepair = stored.review_proxy_repair;
      if (
        !rawRepair ||
        typeof rawRepair !== "object" ||
        Array.isArray(rawRepair)
      ) {
        throw new Error("Saved review-proxy repair recovery is invalid.");
      }
      const pointer = rawRepair as Record<string, unknown>;
      if (
        Object.keys(pointer).sort().join("|") !==
          [
            "attempt_number",
            "attempt_root_repair_id",
            "blocked_session_id",
            "blocked_session_record_sha256",
            "child_probe_job_id",
            "parent_probe_job_id",
            "parent_probe_record_sha256",
            "repair_id",
            "replacement_session_id",
            "request_sha256",
            "retry_from_repair_id",
          ]
            .sort()
            .join("|") ||
        (pointer.repair_id !== null && !isSafeId(pointer.repair_id)) ||
        (pointer.attempt_root_repair_id !== null &&
          !isSafeId(pointer.attempt_root_repair_id)) ||
        (pointer.attempt_number !== null &&
          (!Number.isSafeInteger(pointer.attempt_number) ||
            (pointer.attempt_number as number) < 1)) ||
        (pointer.retry_from_repair_id !== null &&
          !isSafeId(pointer.retry_from_repair_id)) ||
        !isSafeId(pointer.blocked_session_id) ||
        !isSafeId(pointer.parent_probe_job_id) ||
        (pointer.request_sha256 !== null &&
          !isSha256(pointer.request_sha256)) ||
        !isSha256(pointer.parent_probe_record_sha256) ||
        !isSha256(pointer.blocked_session_record_sha256) ||
        (pointer.child_probe_job_id !== null &&
          !isSafeId(pointer.child_probe_job_id)) ||
        (pointer.replacement_session_id !== null &&
          !isSafeId(pointer.replacement_session_id)) ||
        (pointer.child_probe_job_id === null) !==
          (pointer.replacement_session_id === null) ||
        (pointer.repair_id === null &&
          (pointer.request_sha256 !== null ||
            pointer.attempt_root_repair_id !== null ||
            pointer.attempt_number !== null ||
            pointer.retry_from_repair_id !== null ||
            pointer.child_probe_job_id !== null)) ||
        (pointer.repair_id !== null &&
          (pointer.request_sha256 === null ||
            pointer.attempt_root_repair_id === null ||
            pointer.attempt_number === null ||
            (pointer.attempt_number === 1 &&
              (pointer.attempt_root_repair_id !== pointer.repair_id ||
                pointer.retry_from_repair_id !== null)) ||
            ((pointer.attempt_number as number) > 1 &&
              (pointer.attempt_root_repair_id === pointer.repair_id ||
                pointer.retry_from_repair_id === null ||
                pointer.retry_from_repair_id === pointer.repair_id)))) ||
        (pointer.replacement_session_id === null
          ? stored.session_id !== pointer.blocked_session_id
          : stored.session_id !== pointer.blocked_session_id &&
            stored.session_id !== pointer.replacement_session_id)
      ) {
        throw new Error("Saved review-proxy repair recovery is invalid.");
      }
      repair = pointer as unknown as StoredReviewProxyRepair;
    }
    const canonicalStored = [...ids].sort() as string[];
    const exactProbeLineage = sameStrings(canonicalStored, canonicalExpected);
    const completedRepairExtension =
      repair !== null &&
      repair.child_probe_job_id !== null &&
      ids.length === canonicalExpected.length + 1 &&
      canonicalExpected.every((id) => ids.includes(id)) &&
      ids.at(-2) === repair.parent_probe_job_id &&
      ids.at(-1) === repair.child_probe_job_id;
    if (!exactProbeLineage && !completedRepairExtension) {
      throw new Error(
        "Saved annotation recovery does not match this workflow.",
      );
    }
    return stored as unknown as StoredSessionPointer;
  }
  if (
    stored.state === "pending_create" &&
    stored.artifact_type === "ball_annotation_pending_create" &&
    stored.request &&
    typeof stored.request === "object" &&
    !Array.isArray(stored.request) &&
    Object.keys(stored).length === 7
  ) {
    if (!sameStrings([...ids].sort() as string[], canonicalExpected)) {
      throw new Error(
        "Saved annotation recovery does not match this workflow.",
      );
    }
    return stored as unknown as StoredPendingCreate;
  }
  throw new Error("Saved annotation recovery schema is invalid.");
}

export function recoverBallAnnotationLaunch(
  storage: SafeBrowserStorage,
  workflowId: string,
  developmentProbeJobIds: readonly string[],
): BallAnnotationLaunchRecovery | null {
  const key = ballAnnotationStorageKey(workflowId, developmentProbeJobIds[0]);
  const stored = parseStoredRecovery(
    storage.getItem(key),
    workflowId,
    developmentProbeJobIds,
  );
  return stored
    ? {
        lockedProfileId: stored.locked_profile_id,
        developmentProbeJobIds: [...stored.development_probe_job_ids],
      }
    : null;
}

function hasNoStore(headers: Headers) {
  return (headers.get("Cache-Control") ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .includes("no-store");
}

function apiStatus(error: unknown) {
  if (!error || typeof error !== "object") return null;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function operationErrorMessage(error: unknown) {
  if (error && typeof error === "object") {
    const data = (error as { data?: unknown }).data;
    if (data && typeof data === "object") {
      const detail = (data as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail;
      if (detail && typeof detail === "object") {
        const message = (detail as { message?: unknown }).message;
        if (typeof message === "string") return message;
      }
    }
  }
  return error instanceof Error ? error.message : String(error);
}

function reviewRepairRecoveryFromJob(
  recovery: StoredReviewProxyRepair,
  job: ReviewProxyRepairJobView,
): StoredReviewProxyRepair {
  return {
    ...recovery,
    repair_id: job.repairId,
    attempt_root_repair_id: job.attemptRootRepairId,
    attempt_number: job.attemptNumber,
    retry_from_repair_id: job.retryFromRepairId,
    request_sha256: job.requestSha256,
    child_probe_job_id: job.result?.childProbe.jobId ?? null,
    replacement_session_id: job.result?.replacementSession.sessionId ?? null,
  };
}

function requireReviewRepairAuthority(
  job: ReviewProxyRepairJobView,
  recovery: StoredReviewProxyRepair,
  session: ParsedBallAnnotationSession | null,
) {
  if (
    job.authority.blockedSessionId !== recovery.blocked_session_id ||
    job.authority.blockedSessionRecordSha256 !==
      recovery.blocked_session_record_sha256 ||
    job.authority.parentProbeJobId !== recovery.parent_probe_job_id ||
    job.authority.parentProbeRecordSha256 !==
      recovery.parent_probe_record_sha256 ||
    (recovery.repair_id !== null && job.repairId !== recovery.repair_id) ||
    (recovery.attempt_root_repair_id !== null &&
      job.attemptRootRepairId !== recovery.attempt_root_repair_id) ||
    (recovery.attempt_number !== null &&
      job.attemptNumber !== recovery.attempt_number) ||
    job.retryFromRepairId !== recovery.retry_from_repair_id ||
    (recovery.request_sha256 !== null &&
      job.requestSha256 !== recovery.request_sha256) ||
    (recovery.replacement_session_id !== null &&
      (job.status !== "ready" ||
        job.result?.replacementSession.sessionId !==
          recovery.replacement_session_id ||
        job.result.childProbe.jobId !== recovery.child_probe_job_id))
  ) {
    throw new Error("Review-proxy repair authority does not match recovery.");
  }
  const capability = session?.view.reviewProxyRepair;
  if (
    capability &&
    (session?.view.sessionId !== recovery.blocked_session_id ||
      session.view.status !== "blocked" ||
      session.view.blockerCode !== "review_proxy_required" ||
      session.view.requestSha256 !==
        job.authority.blockedSessionRequestSha256 ||
      session.view.source.sourceId !== job.authority.sourceId ||
      session.view.source.sourceSha256 !== job.authority.sourceSha256 ||
      session.view.source.width !== job.authority.sourceWidth ||
      session.view.source.height !== job.authority.sourceHeight ||
      session.view.source.frameCount !== job.authority.sourceFrameCount ||
      session.view.source.fps !== job.authority.sourceFps ||
      session.view.lockedProfile.profileId !== job.authority.lockedProfileId ||
      session.view.lockedProfile.profileSha256 !==
        job.authority.lockedProfileSha256 ||
      session.view.samplingManifestSha256 !==
        job.authority.samplingManifestSha256 ||
      capability.parentProbeJobId !== job.authority.parentProbeJobId ||
      capability.parentProbeReportSha256 !==
        job.authority.parentProbeReportSha256 ||
      capability.parentProbeResultManifestSha256 !==
        job.authority.parentProbeResultManifestSha256 ||
      capability.parentProbeRecordSha256 !==
        job.authority.parentProbeRecordSha256 ||
      capability.blockedSessionRecordSha256 !==
        recovery.blocked_session_record_sha256)
  ) {
    throw new Error("Review-proxy repair capability authority changed.");
  }
}

function requireReplacementSession(
  parsed: ParsedBallAnnotationSession,
  job: ReviewProxyRepairJobView,
) {
  const result = job.result;
  if (!result) {
    throw new Error("Review-proxy repair result is unavailable.");
  }
  const expected = result.replacementSession;
  const proxy = result.proxy;
  if (
    parsed.view.sessionId !== expected.sessionId ||
    parsed.view.requestSha256 !== expected.requestSha256 ||
    parsed.view.status !== "annotating" ||
    parsed.view.dataRole !== "development" ||
    parsed.view.retryFromSessionId !== expected.retryFromSessionId ||
    parsed.view.retryLineage?.mode !== expected.retryMode ||
    parsed.view.retryLineage.previousSessionId !==
      expected.retryFromSessionId ||
    parsed.view.attemptFamilySha256 !== expected.attemptFamilySha256 ||
    parsed.view.source.sourceId !== job.authority.sourceId ||
    parsed.view.source.sourceSha256 !== job.authority.sourceSha256 ||
    parsed.view.source.width !== job.authority.sourceWidth ||
    parsed.view.source.height !== job.authority.sourceHeight ||
    parsed.view.source.frameCount !== job.authority.sourceFrameCount ||
    parsed.view.source.fps !== job.authority.sourceFps ||
    parsed.view.lockedProfile.profileId !== job.authority.lockedProfileId ||
    parsed.view.lockedProfile.profileSha256 !==
      job.authority.lockedProfileSha256 ||
    parsed.view.samplingManifestSha256 !==
      job.authority.samplingManifestSha256 ||
    !sameStrings(
      [...parsed.developmentProbeJobIds].sort(),
      [...expected.developmentProbeJobIds].sort(),
    ) ||
    parsed.view.frames.length !== proxy.sampledArtifactCount ||
    !sameStrings(
      parsed.view.frames.map((frame) => String(frame.frameIndex)),
      job.authority.frameIndices.map(String),
    ) ||
    parsed.view.frames.some(
      (frame) =>
        frame.proxyBinding === null ||
        frame.proxyBinding.proxySha256 !== proxy.proxyMediaSha256 ||
        frame.proxyBinding.proxySizeBytes !== proxy.proxySizeBytes ||
        frame.proxyBinding.proxyWidth !== proxy.proxyWidth ||
        frame.proxyBinding.proxyHeight !== proxy.proxyHeight ||
        frame.proxyBinding.mapSha256 !== proxy.mappingSha256 ||
        frame.proxyBinding.sourceFrame.frameIndex !== frame.frameIndex ||
        frame.proxyBinding.sourceFrame.sha256 !== frame.sourceFrameSha256,
    )
  ) {
    throw new Error("Replacement annotation session authority is invalid.");
  }
}

export function requireBallAnnotationResponseMetadata(value: unknown) {
  const metadata = getCustomFetchResponseMetadata(value);
  if (!metadata) {
    throw new Error("Annotation response metadata is missing.");
  }
  if (!hasNoStore(metadata.headers)) {
    throw new Error("Annotation response is missing no-store authority.");
  }
  return metadata;
}

function mutationId() {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `mutation-${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}`;
}

function apiAnnotation(value: BallAnnotationValueView): BallAnnotationApiValue {
  return {
    point_source_px: value.point
      ? { x: value.point[0], y: value.point[1] }
      : null,
    bbox_source_px: value.bbox,
    presence: value.presence,
    visibility: value.visibility,
    training_use: value.trainingUse,
    annotation_state: value.annotationState,
    scale_stratum: value.scaleStratum,
    lighting_tag: value.lightingTag,
    motion_occlusion_tags: value.motionOcclusionTags,
    provenance: value.provenance,
  };
}

type ApplicabilityState = Record<
  string,
  {
    status: "applicable" | "not_applicable";
    evidenceNote: string;
    quota: number;
    frameIntervalsText: string;
  }
>;

function initialApplicability(): ApplicabilityState {
  return Object.fromEntries(
    [...SCALE, ...LIGHTING].map((stratum) => [
      stratum,
      {
        status: "applicable",
        evidenceNote: "",
        quota: 0,
        frameIntervalsText: "",
      },
    ]),
  );
}

export function parseBallAnnotationFrameIntervals(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return [];
  const parts = trimmed.split(",");
  if (parts.length > 32) {
    throw new Error("At most 32 lighting frame intervals are allowed.");
  }
  return parts.map((part) => {
    const match = /^\s*(\d+)\s*-\s*(\d+)\s*$/.exec(part);
    if (!match) {
      throw new Error(
        "Lighting intervals must use start-end, separated by commas.",
      );
    }
    const startFrame = Number(match[1]);
    const endFrame = Number(match[2]);
    if (
      !Number.isSafeInteger(startFrame) ||
      !Number.isSafeInteger(endFrame) ||
      endFrame < startFrame
    ) {
      throw new Error("Lighting frame interval is invalid.");
    }
    return { startFrame, endFrame };
  });
}

function verifySuggestionDecision(
  frame: BallAnnotationFrameView,
  decision: BallSuggestionDecision,
) {
  const authority =
    decision.kind === "detector_candidate"
      ? frame.suggestedCandidates.find(
          (candidate) => candidate.candidateId === decision.id,
        )
      : (frame.propagationSuggestions ?? []).find(
          (suggestion) => suggestion.suggestionId === decision.id,
        );
  const jobId =
    authority && "suggestionJobId" in authority
      ? authority.suggestionJobId
      : authority?.jobId;
  const sha256 = authority?.suggestionSha256;
  const pending =
    authority && "decision" in authority
      ? authority.decision === "pending"
      : authority?.pendingHumanConfirmation !== false;
  if (
    !authority ||
    !pending ||
    jobId !== decision.jobId ||
    sha256 !== decision.sha256
  ) {
    throw new Error(
      "Suggestion evidence changed or is incomplete; reload before deciding.",
    );
  }
  return decision;
}

interface ProductionBallAnnotationControllerProps {
  workflowId: string;
  developmentProbeJobIds: string[];
  lockedProfileId: string;
  storage: SafeBrowserStorage;
  onStartNewDevelopmentBatch: () => void;
}

export function ProductionBallAnnotationController({
  workflowId,
  developmentProbeJobIds,
  lockedProfileId,
  storage,
  onStartNewDevelopmentBatch,
}: ProductionBallAnnotationControllerProps) {
  const { language } = useLanguage();
  const initialProbeIds = useMemo(
    () => [...developmentProbeJobIds].sort(),
    [developmentProbeJobIds],
  );
  const storageKey = useMemo(
    () => ballAnnotationStorageKey(workflowId, developmentProbeJobIds[0]),
    [developmentProbeJobIds, workflowId],
  );
  const [stored, setStored] = useState<StoredRecovery | null>(() => {
    try {
      return parseStoredRecovery(
        storage.getItem(storageKey),
        workflowId,
        initialProbeIds,
      );
    } catch {
      return null;
    }
  });
  const [recoveryError, setRecoveryError] = useState<string | null>(() => {
    try {
      parseStoredRecovery(
        storage.getItem(storageKey),
        workflowId,
        initialProbeIds,
      );
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  });
  const [activeProbeIds, setActiveProbeIds] = useState<string[]>(() =>
    stored ? [...stored.development_probe_job_ids] : initialProbeIds,
  );
  const probeIdentity = activeProbeIds.join("|");
  const [session, setSession] = useState<ParsedBallAnnotationSession | null>(
    null,
  );
  const [role, setRole] = useState<"development" | "check">(
    stored?.state === "session_pointer"
      ? stored.data_role
      : stored?.state === "pending_create"
        ? stored.request.data_role
        : "development",
  );
  const [preparingCheck, setPreparingCheck] = useState(false);
  const [targetFrameCount, setTargetFrameCount] = useState(20);
  const [operatorId, setOperatorId] = useState("local-operator");
  const [applicability, setApplicability] = useState(initialApplicability);
  const [activeFrameOffset, setActiveFrameOffset] = useState(0);
  const [frameImageUrl, setFrameImageUrl] = useState<string | null>(null);
  const [frameImageIdentity, setFrameImageIdentity] = useState<string | null>(
    null,
  );
  const [frameImageState, setFrameImageState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [operationState, setOperationState] = useState<
    "idle" | "saving" | "finalizing" | "propagating"
  >("idle");
  const [operationError, setOperationError] = useState<string | null>(null);
  const [dashboard, setDashboard] = useState<NonNullable<
    ReturnType<typeof parseBallAnnotationFinalResult>["dashboard"]
  > | null>(null);
  const operationRef = useRef(false);
  const sessionRefreshGenerationRef = useRef(0);
  const propagationRequestRef = useRef(false);
  const [propagationJob, setPropagationJob] =
    useState<ParsedBallPropagationJob | null>(null);
  const [reviewProxyRepairJob, setReviewProxyRepairJob] =
    useState<ReviewProxyRepairJobView | null>(null);
  const [reviewProxyRepairOperation, setReviewProxyRepairOperation] = useState<
    "idle" | "starting" | "cancelling" | "retrying" | "reloading"
  >("idle");
  const [reviewProxyRepairError, setReviewProxyRepairError] = useState<
    string | null
  >(null);
  const repairJobRef = useRef<ReviewProxyRepairJobView | null>(null);
  const repairGenerationRef = useRef(0);
  const repairAbortRef = useRef<AbortController | null>(null);
  const repairRecoveryAttemptRef = useRef<string | null>(null);
  const [repairPollCycle, setRepairPollCycle] = useState(0);

  const labels =
    language === "zh"
      ? {
          title: "小球逐帧验证",
          setup: "创建标注会话",
          setupDescription:
            "先声明该原片实际存在的尺度与光照。开发帧用于探索；数据隔离检查会另外抽取 20–50 个未见帧。",
          operator: "操作者标识",
          target: "未见帧检查帧数（20–50）",
          applicable: "适用",
          notApplicable: "不适用",
          evidence: "揭示前依据",
          startDevelopment: "开始开发帧标注",
          startCheck: "开始未见帧检查",
          resume: "恢复未完成的创建请求",
          prepareCheck: "开发标注完成，准备未见帧检查",
          newDevelopmentBatch: "开始新的开发证据批次",
          newDevelopmentBatchDescription:
            "当前未见帧检查已封存。请返回试跑设置，调整起始帧和帧数，然后完成一次新的有限试跑。旧的封存会话将保持不变。",
          fullVideoPreset: "设为单光照全片快捷方案",
          quota: "抽帧配额",
          intervals: "原片帧区间（例如 0-999, 1200-1400）",
          discard: "丢弃无效恢复记录",
          recovery: "恢复记录无效",
        }
      : {
          title: "Tiny-ball frame verification",
          setup: "Create annotation session",
          setupDescription:
            "Declare scale and lighting applicability before reveal. Development frames are exploratory; a data-isolated check draws 20–50 unseen frames.",
          operator: "Operator ID",
          target: "Unseen-frame check size (20–50)",
          applicable: "Applicable",
          notApplicable: "Not applicable",
          evidence: "Pre-reveal evidence",
          startDevelopment: "Start development annotation",
          startCheck: "Start unseen-frame check",
          resume: "Resume exact pending create",
          prepareCheck: "Development complete — prepare unseen-frame check",
          newDevelopmentBatch: "Start a new development evidence batch",
          newDevelopmentBatchDescription:
            "This unseen-frame check is sealed. Return to trial settings, adjust the start frame and frame count, then complete a new bounded trial. The existing sealed session will remain unchanged.",
          fullVideoPreset: "Use single-light full-video preset",
          quota: "Sampling quota",
          intervals: "Source-frame intervals (for example 0-999, 1200-1400)",
          discard: "Discard invalid recovery",
          recovery: "Invalid recovery record",
        };

  const expected = useMemo(
    () => ({
      dataRole: role,
      developmentProbeJobIds: activeProbeIds,
      lockedProfileId,
    }),
    [lockedProfileId, probeIdentity, role],
  );

  const refreshSession = useCallback(
    async (
      sessionId: string,
      expectedRole = role,
      expectedProbeIds = activeProbeIds,
    ) => {
      const generation = ++sessionRefreshGenerationRef.current;
      let raw: unknown;
      try {
        raw = await getBallAnnotationSession(sessionId, {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
        });
      } catch (error) {
        if (generation !== sessionRefreshGenerationRef.current) return null;
        throw error;
      }
      if (generation !== sessionRefreshGenerationRef.current) return null;
      requireBallAnnotationResponseMetadata(raw);
      const parsed = parseBallAnnotationSession(raw, {
        ...expected,
        dataRole: expectedRole,
        developmentProbeJobIds: expectedProbeIds,
      });
      setSession(parsed);
      setRole(parsed.view.dataRole);
      setActiveFrameOffset((offset) =>
        Math.min(offset, Math.max(0, parsed.view.frames.length - 1)),
      );
      return parsed;
    },
    [activeProbeIds, expected],
  );
  const activeFrame = session?.view.frames[activeFrameOffset] ?? null;
  const frameSessionId = session?.view.sessionId ?? null;
  const activeFrameIndex = activeFrame?.frameIndex ?? null;
  const activeFrameSha256 = activeFrame?.sourceFrameSha256 ?? null;

  useEffect(() => {
    if (stored?.state !== "session_pointer" || session) return;
    const replacementId =
      stored.review_proxy_repair?.replacement_session_id ?? stored.session_id;
    void refreshSession(
      replacementId,
      stored.data_role,
      stored.development_probe_job_ids,
    )
      .then((parsed) => {
        if (parsed === null) return;
        if (replacementId !== stored.session_id) {
          persist({ ...stored, session_id: replacementId });
        }
      })
      .catch((error) =>
        setOperationError(
          error instanceof Error ? error.message : String(error),
        ),
      );
  }, [refreshSession, session, stored]);

  useEffect(() => {
    if (!session || !ACTIVE_SESSION_STATUSES.has(session.view.status)) return;
    const timer = window.setInterval(() => {
      void refreshSession(session.view.sessionId, session.view.dataRole).catch(
        (error) =>
          setOperationError(
            error instanceof Error ? error.message : String(error),
          ),
      );
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [refreshSession, session]);

  useEffect(() => {
    if (
      frameSessionId === null ||
      activeFrameIndex === null ||
      activeFrameSha256 === null
    ) {
      setFrameImageUrl(null);
      setFrameImageIdentity(null);
      setFrameImageState("idle");
      return;
    }
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setFrameImageUrl(null);
    setFrameImageIdentity(null);
    setFrameImageState("loading");
    void fetchVerifiedBallAnnotationFrame({
      sessionId: frameSessionId,
      frameIndex: activeFrameIndex,
      expectedSha256: activeFrameSha256,
      signal: controller.signal,
    })
      .then((verified) => {
        if (controller.signal.aborted) {
          URL.revokeObjectURL(verified.objectUrl);
          return;
        }
        objectUrl = verified.objectUrl;
        setFrameImageUrl(objectUrl);
        setFrameImageIdentity(
          `${frameSessionId}:${activeFrameIndex}:${activeFrameSha256}`,
        );
        setFrameImageState("ready");
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setFrameImageIdentity(null);
          setFrameImageState("failed");
          setOperationError(
            error instanceof Error ? error.message : String(error),
          );
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [activeFrameIndex, activeFrameSha256, frameSessionId]);

  useEffect(() => {
    if (!session || session.view.status !== "finalized" || dashboard) return;
    void (async () => {
      const raw = await getBallAnnotationResult(session.view.sessionId, {
        cache: "no-store",
        headers: { "Cache-Control": "no-store" },
      });
      requireBallAnnotationResponseMetadata(raw);
      const parsed = parseBallAnnotationFinalResult(raw, session);
      setDashboard(parsed.dashboard);
    })().catch((error) =>
      setOperationError(error instanceof Error ? error.message : String(error)),
    );
  }, [dashboard, session]);

  function persist(next: StoredRecovery) {
    const serialized = JSON.stringify(next);
    storage.setItem(storageKey, serialized);
    if (!storage.isPersistent || storage.getItem(storageKey) !== serialized) {
      throw new Error("Persistent recovery storage is unavailable.");
    }
    setStored(next);
  }

  function persistReviewRepairRecovery(
    recovery: StoredReviewProxyRepair,
    options: {
      sessionId?: string;
      dataRole?: "development" | "check";
      developmentProbeJobIds?: string[];
    } = {},
  ) {
    const pointer = stored?.state === "session_pointer" ? stored : null;
    const sessionId =
      options.sessionId ?? session?.view.sessionId ?? pointer?.session_id;
    const dataRole =
      options.dataRole ?? session?.view.dataRole ?? pointer?.data_role;
    if (!sessionId || !dataRole) {
      throw new Error("Annotation session recovery is unavailable.");
    }
    persist({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: workflowId,
      development_probe_job_ids:
        options.developmentProbeJobIds ?? activeProbeIds,
      locked_profile_id: lockedProfileId,
      session_id: sessionId,
      data_role: dataRole,
      ...(pointer?.propagation_job
        ? { propagation_job: pointer.propagation_job }
        : {}),
      review_proxy_repair: recovery,
    });
  }

  async function adoptReadyReviewRepair(
    job: ReviewProxyRepairJobView,
    recovery: StoredReviewProxyRepair,
    generation: number,
    signal: AbortSignal,
  ) {
    const result = job.result;
    if (!result) {
      throw new Error("Review-proxy repair result is unavailable.");
    }
    const nextProbeIds = [...result.replacementSession.developmentProbeJobIds];
    const readyRecovery = reviewRepairRecoveryFromJob(recovery, job);

    // Persist the server-issued continuation before attempting to fetch it so a
    // refresh can resume without reconstructing a create request.
    persistReviewRepairRecovery(readyRecovery, {
      developmentProbeJobIds: nextProbeIds,
    });
    setActiveProbeIds(nextProbeIds);

    const raw = await getBallAnnotationSession(
      result.replacementSession.sessionId,
      {
        cache: "no-store",
        headers: { "Cache-Control": "no-store" },
        signal,
      },
    );
    if (signal.aborted || generation !== repairGenerationRef.current) {
      return;
    }
    requireBallAnnotationResponseMetadata(raw);
    const parsed = parseBallAnnotationSession(raw, {
      dataRole: "development",
      developmentProbeJobIds: nextProbeIds,
      lockedProfileId,
    });
    requireReplacementSession(parsed, job);
    persistReviewRepairRecovery(readyRecovery, {
      sessionId: parsed.view.sessionId,
      dataRole: "development",
      developmentProbeJobIds: nextProbeIds,
    });
    setSession(parsed);
    setRole("development");
    setActiveFrameOffset(0);
    setDashboard(null);
  }

  async function runReviewRepairRequest(
    action: "create" | "get" | "cancel" | "retry",
    recovery: StoredReviewProxyRepair,
    visibleOperation:
      | "starting"
      | "cancelling"
      | "retrying"
      | "reloading"
      | null,
  ) {
    const generation = repairGenerationRef.current + 1;
    repairGenerationRef.current = generation;
    repairAbortRef.current?.abort();
    const controller = new AbortController();
    repairAbortRef.current = controller;
    if (visibleOperation) setReviewProxyRepairOperation(visibleOperation);
    setReviewProxyRepairError(null);
    try {
      const next =
        action === "create"
          ? await createReviewProxyRepair(
              recovery.blocked_session_id,
              controller.signal,
            )
          : action === "cancel"
            ? await cancelReviewProxyRepair(
                recovery.repair_id!,
                controller.signal,
              )
            : action === "retry"
              ? await retryReviewProxyRepair(
                  recovery.repair_id!,
                  controller.signal,
                )
              : await getReviewProxyRepair(
                  recovery.repair_id!,
                  controller.signal,
                );
      if (
        controller.signal.aborted ||
        generation !== repairGenerationRef.current
      ) {
        return;
      }
      const current = repairJobRef.current;
      if (action === "retry") {
        if (!current) {
          throw new Error("Review-proxy repair retry authority is missing.");
        }
        requireReviewRepairAuthority(current, recovery, session);
        requireReviewProxyRepairRetryLineage(current, next);
        requireReviewRepairAuthority(
          next,
          reviewRepairRecoveryFromJob(recovery, next),
          session,
        );
      } else {
        requireReviewRepairAuthority(next, recovery, session);
        if (current && !reviewProxyRepairUpdateIsCurrent(current, next)) {
          return;
        }
      }
      repairJobRef.current = next;
      setReviewProxyRepairJob(next);
      const nextRecovery = reviewRepairRecoveryFromJob(recovery, next);
      const nextProbeIds =
        next.result?.replacementSession.developmentProbeJobIds ??
        activeProbeIds;
      persistReviewRepairRecovery(nextRecovery, {
        developmentProbeJobIds: [...nextProbeIds],
      });
      if (next.status === "ready") {
        await adoptReadyReviewRepair(
          next,
          nextRecovery,
          generation,
          controller.signal,
        );
      }
    } catch (error) {
      if (
        !controller.signal.aborted &&
        generation === repairGenerationRef.current
      ) {
        setReviewProxyRepairError(operationErrorMessage(error));
      }
    } finally {
      if (generation === repairGenerationRef.current) {
        if (repairAbortRef.current === controller) {
          repairAbortRef.current = null;
        }
        if (visibleOperation) setReviewProxyRepairOperation("idle");
      }
    }
  }

  function startReviewProxyRepair() {
    const capability = session?.view.reviewProxyRepair;
    if (
      !session ||
      session.view.status !== "blocked" ||
      session.view.blockerCode !== "review_proxy_required" ||
      !capability?.eligible ||
      capability.action !== "generate_verified_review_proxy" ||
      capability.createUrl !== "/api/v1/detector-review-proxy-repairs" ||
      reviewProxyRepairJob !== null ||
      reviewProxyRepairOperation !== "idle"
    ) {
      return;
    }
    const recovery: StoredReviewProxyRepair = {
      repair_id: null,
      attempt_root_repair_id: null,
      attempt_number: null,
      retry_from_repair_id: null,
      blocked_session_id: session.view.sessionId,
      request_sha256: null,
      parent_probe_job_id: capability.parentProbeJobId,
      parent_probe_record_sha256: capability.parentProbeRecordSha256,
      blocked_session_record_sha256: capability.blockedSessionRecordSha256,
      child_probe_job_id: null,
      replacement_session_id: null,
    };
    try {
      repairRecoveryAttemptRef.current = `create:${recovery.blocked_session_id}`;
      persistReviewRepairRecovery(recovery);
    } catch (error) {
      setReviewProxyRepairError(operationErrorMessage(error));
      return;
    }
    void runReviewRepairRequest("create", recovery, "starting");
  }

  function cancelActiveReviewProxyRepair(repairId: string) {
    const recovery =
      stored?.state === "session_pointer"
        ? stored.review_proxy_repair
        : undefined;
    if (
      !recovery?.repair_id ||
      recovery.repair_id !== repairId ||
      reviewProxyRepairJob?.repairId !== repairId ||
      !reviewProxyRepairJob.canCancel
    ) {
      return;
    }
    void runReviewRepairRequest("cancel", recovery, "cancelling");
  }

  function retryActiveReviewProxyRepair(repairId: string) {
    const recovery =
      stored?.state === "session_pointer"
        ? stored.review_proxy_repair
        : undefined;
    if (
      !recovery?.repair_id ||
      recovery.repair_id !== repairId ||
      reviewProxyRepairJob?.repairId !== repairId ||
      !reviewProxyRepairJob.canRetry ||
      reviewProxyRepairOperation !== "idle"
    ) {
      return;
    }
    void runReviewRepairRequest("retry", recovery, "retrying");
  }

  function reloadActiveReviewProxyRepair(repairId: string) {
    const recovery =
      stored?.state === "session_pointer"
        ? stored.review_proxy_repair
        : undefined;
    if (
      !recovery?.repair_id ||
      recovery.repair_id !== repairId ||
      reviewProxyRepairJob?.repairId !== repairId ||
      reviewProxyRepairError === null ||
      reviewProxyRepairOperation !== "idle"
    ) {
      return;
    }
    void runReviewRepairRequest("get", recovery, "reloading");
  }

  function requestFor(nextRole: "development" | "check") {
    const developmentBinding =
      nextRole === "check" &&
      session?.view.dataRole === "development" &&
      session.view.status === "finalized" &&
      session.view.finalPackage
        ? {
            sessionId: session.view.sessionId,
            packageSha256: session.view.finalPackage.packageSha256,
          }
        : null;
    return buildBallAnnotationSessionRequest({
      dataRole: nextRole,
      developmentProbeJobIds: activeProbeIds,
      lockedProfileId,
      targetFrameCount: nextRole === "check" ? targetFrameCount : null,
      operatorId,
      developmentPackageSessionId: developmentBinding?.sessionId ?? null,
      developmentPackageSha256: developmentBinding?.packageSha256 ?? null,
      strataApplicability: {
        scale: SCALE.map((stratum) => ({
          stratum,
          status: applicability[stratum].status,
          evidenceNote: applicability[stratum].evidenceNote,
        })),
        lighting: LIGHTING.map((stratum) => ({
          stratum,
          status: applicability[stratum].status,
          evidenceNote: applicability[stratum].evidenceNote,
          quota: nextRole === "check" ? applicability[stratum].quota : 0,
          frameIntervals:
            nextRole === "check"
              ? parseBallAnnotationFrameIntervals(
                  applicability[stratum].frameIntervalsText,
                )
              : [],
        })),
      },
    });
  }

  async function submitCreate(request: BallAnnotationSessionCreateRequest) {
    if (operationRef.current) return;
    operationRef.current = true;
    setOperationState("saving");
    setOperationError(null);
    try {
      const pending: StoredPendingCreate = {
        schema_version: "1.0",
        artifact_type: "ball_annotation_pending_create",
        state: "pending_create",
        workflow_id: workflowId,
        development_probe_job_ids: activeProbeIds,
        locked_profile_id: lockedProfileId,
        request,
      };
      persist(pending);
      const raw = await createBallAnnotationSession(request, {
        cache: "no-store",
        headers: {
          "Cache-Control": "no-store",
        },
      });
      requireBallAnnotationResponseMetadata(raw);
      const parsed = parseBallAnnotationSession(raw, {
        dataRole: request.data_role,
        developmentProbeJobIds: activeProbeIds,
        lockedProfileId,
      });
      persist({
        schema_version: "1.0",
        artifact_type: "ball_annotation_session_pointer",
        state: "session_pointer",
        workflow_id: workflowId,
        development_probe_job_ids: activeProbeIds,
        locked_profile_id: lockedProfileId,
        session_id: parsed.view.sessionId,
        data_role: parsed.view.dataRole,
      });
      setSession(parsed);
      setRole(parsed.view.dataRole);
      setPreparingCheck(false);
      setDashboard(null);
    } catch (error) {
      setOperationError(operationErrorMessage(error));
    } finally {
      operationRef.current = false;
      setOperationState("idle");
    }
  }

  async function mutate(
    operation: "set" | "delete" | "undo",
    annotation?: BallAnnotationValueView,
    undoRevision?: number,
    suggestionDecision?: BallSuggestionDecision,
  ) {
    const activeFrame = session?.view.frames[activeFrameOffset];
    if (!session || !activeFrame || operationRef.current) return;
    operationRef.current = true;
    setOperationState("saving");
    setOperationError(null);
    const id = mutationId();
    try {
      const request =
        operation === "set"
          ? buildBallAnnotationMutation({
              operation,
              mutationId: id,
              expectedRevision: activeFrame.annotationRevision,
              annotation: apiAnnotation(annotation!),
              suggestionDecision: suggestionDecision
                ? verifySuggestionDecision(activeFrame, suggestionDecision)
                : undefined,
            })
          : operation === "delete"
            ? buildBallAnnotationMutation({
                operation,
                mutationId: id,
                expectedRevision: activeFrame.annotationRevision,
              })
            : buildBallAnnotationMutation({
                operation,
                mutationId: id,
                expectedRevision: activeFrame.annotationRevision,
                undoRevision: undoRevision!,
              });
      let raw: unknown;
      try {
        raw = await putBallAnnotation(
          session.view.sessionId,
          activeFrame.frameIndex,
          request,
          {
            cache: "no-store",
            headers: {
              "Cache-Control": "no-store",
              "If-Match": activeFrame.annotationEtag,
            },
          },
        );
      } catch (error) {
        if (apiStatus(error) === 412) {
          await refreshSession(session.view.sessionId, session.view.dataRole);
          throw new Error(
            "This frame changed elsewhere. The latest revision was loaded; review it before saving again.",
          );
        }
        throw error;
      }
      const metadata = requireBallAnnotationResponseMetadata(raw);
      try {
        parseBallAnnotationRevision(raw, metadata.headers.get("ETag"), {
          sessionId: session.view.sessionId,
          frameIndex: activeFrame.frameIndex,
          mutationId: id,
          sourceWidth: session.view.source.width,
          sourceHeight: session.view.source.height,
          dataRole: session.view.dataRole,
          request,
          suggestionDecision,
        });
      } catch (error) {
        await refreshSession(session.view.sessionId, session.view.dataRole);
        throw error;
      }
      await refreshSession(session.view.sessionId, session.view.dataRole);
    } catch (error) {
      setOperationError(operationErrorMessage(error));
    } finally {
      operationRef.current = false;
      setOperationState("idle");
    }
  }

  async function finalize() {
    if (!session || operationRef.current) return;
    operationRef.current = true;
    setOperationState("finalizing");
    setOperationError(null);
    try {
      const raw = await finalizeBallAnnotationSession(
        session.view.sessionId,
        { mutation_id: mutationId() },
        {
          cache: "no-store",
          headers: {
            "Cache-Control": "no-store",
          },
        },
      );
      requireBallAnnotationResponseMetadata(raw);
      const parsedResult = parseBallAnnotationFinalResult(raw, session);
      setDashboard(parsedResult.dashboard);
      await refreshSession(session.view.sessionId, session.view.dataRole);
    } catch (error) {
      setOperationError(operationErrorMessage(error));
    } finally {
      operationRef.current = false;
      setOperationState("idle");
    }
  }

  function persistPropagationRecovery(
    recovery?: StoredSessionPointer["propagation_job"],
  ) {
    if (!session) throw new Error("Annotation session is unavailable.");
    persist({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: workflowId,
      development_probe_job_ids: activeProbeIds,
      locked_profile_id: lockedProfileId,
      session_id: session.view.sessionId,
      data_role: session.view.dataRole,
      ...(recovery ? { propagation_job: recovery } : {}),
      ...(stored?.state === "session_pointer" && stored.review_proxy_repair
        ? { review_proxy_repair: stored.review_proxy_repair }
        : {}),
    });
  }

  async function loadPropagation(
    recovery: NonNullable<StoredSessionPointer["propagation_job"]>,
    action: "create" | "get" | "cancel",
    showBusy: boolean,
  ) {
    if (!session || propagationRequestRef.current) return;
    const seedFrame = session.view.frames.find(
      (frame) => frame.frameIndex === recovery.request.seed_frame_index,
    );
    if (!seedFrame) {
      setOperationError("Propagation seed frame is no longer available.");
      return;
    }
    propagationRequestRef.current = true;
    if (showBusy) setOperationState("propagating");
    setOperationError(null);
    try {
      let raw: unknown;
      try {
        const requestOptions: RequestInit = {
          cache: "no-store",
          headers: {
            "Cache-Control": "no-store",
            ...(action === "create"
              ? { "If-Match": seedFrame.annotationEtag }
              : {}),
          },
        };
        raw =
          action === "create"
            ? await createBallPropagationJob(
                session.view.sessionId,
                recovery.request,
                requestOptions,
              )
            : action === "cancel"
              ? await cancelBallPropagationJob(
                  session.view.sessionId,
                  recovery.job_id!,
                  requestOptions,
                )
              : await getBallPropagationJob(
                  session.view.sessionId,
                  recovery.job_id!,
                  requestOptions,
                );
      } catch (error) {
        if (apiStatus(error) === 412) {
          await refreshSession(session.view.sessionId, session.view.dataRole);
          throw new Error(
            "The propagation seed changed. The latest frame was loaded; review it before retrying.",
          );
        }
        throw error;
      }
      requireBallAnnotationResponseMetadata(raw);
      const parsed = parseBallPropagationJob(raw, session, {
        jobId: recovery.job_id ?? undefined,
        mutationId: recovery.request.mutation_id,
        seedFrameIndex: recovery.request.seed_frame_index,
        expectedSeedRevision: recovery.request.expected_seed_revision,
        radiusFrames: recovery.request.radius_frames,
      });
      setPropagationJob(parsed);
      const active = ["queued", "waiting_probe", "committing"].includes(
        parsed.view.status,
      );
      persistPropagationRecovery(
        active
          ? {
              job_id: parsed.view.jobId,
              request: recovery.request,
            }
          : undefined,
      );
      if (!active) {
        await refreshSession(session.view.sessionId, session.view.dataRole);
      }
    } catch (error) {
      setOperationError(operationErrorMessage(error));
    } finally {
      propagationRequestRef.current = false;
      if (showBusy) setOperationState("idle");
    }
  }

  async function startPropagation(radiusFrames: number) {
    const seedFrame = session?.view.frames[activeFrameOffset];
    if (
      !session ||
      session.view.dataRole !== "development" ||
      !seedFrame?.currentAnnotation ||
      seedFrame.annotationRevision < 1 ||
      operationRef.current
    ) {
      return;
    }
    const request = buildBallPropagationRequest({
      mutationId: mutationId(),
      seedFrameIndex: seedFrame.frameIndex,
      radiusFrames,
      expectedSeedRevision: seedFrame.annotationRevision,
    });
    const recovery = { job_id: null, request };
    try {
      persistPropagationRecovery(recovery);
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : String(error));
      return;
    }
    await loadPropagation(recovery, "create", true);
  }

  async function cancelPropagation() {
    const recovery =
      stored?.state === "session_pointer" ? stored.propagation_job : undefined;
    if (!recovery?.job_id) return;
    await loadPropagation(recovery, "cancel", true);
  }

  useEffect(() => {
    const recovery =
      stored?.state === "session_pointer" ? stored.propagation_job : undefined;
    if (!session || !recovery || propagationJob) return;
    void loadPropagation(recovery, recovery.job_id ? "get" : "create", false);
  }, [propagationJob, session, stored]);

  useEffect(() => {
    const recovery =
      stored?.state === "session_pointer" ? stored.propagation_job : undefined;
    if (
      !session ||
      !recovery?.job_id ||
      !propagationJob ||
      !["queued", "waiting_probe", "committing"].includes(
        propagationJob.view.status,
      )
    ) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadPropagation(recovery, "get", false);
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [propagationJob, session, stored]);

  useEffect(() => {
    const recovery =
      stored?.state === "session_pointer"
        ? stored.review_proxy_repair
        : undefined;
    if (
      !session ||
      !recovery ||
      reviewProxyRepairJob ||
      repairAbortRef.current
    ) {
      return;
    }
    if (
      recovery.repair_id === null &&
      (session.view.sessionId !== recovery.blocked_session_id ||
        session.view.reviewProxyRepair === null)
    ) {
      return;
    }
    const attemptKey = `${recovery.repair_id ? "get" : "create"}:${
      recovery.repair_id ?? recovery.blocked_session_id
    }`;
    if (repairRecoveryAttemptRef.current === attemptKey) return;
    repairRecoveryAttemptRef.current = attemptKey;
    void runReviewRepairRequest(
      recovery.repair_id ? "get" : "create",
      recovery,
      null,
    );
  }, [reviewProxyRepairJob, session, stored]);

  useEffect(() => {
    const recovery =
      stored?.state === "session_pointer"
        ? stored.review_proxy_repair
        : undefined;
    if (
      !reviewProxyRepairJob ||
      !recovery?.repair_id ||
      reviewProxyRepairError !== null ||
      reviewProxyRepairOperation !== "idle" ||
      !["queued", "running", "committing"].includes(reviewProxyRepairJob.status)
    ) {
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      void runReviewRepairRequest("get", recovery, null).finally(() => {
        if (!disposed) setRepairPollCycle((cycle) => cycle + 1);
      });
    }, 1_000);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [
    repairPollCycle,
    reviewProxyRepairError,
    reviewProxyRepairJob,
    reviewProxyRepairOperation,
    stored,
  ]);

  useEffect(
    () => () => {
      repairGenerationRef.current += 1;
      repairAbortRef.current?.abort();
      repairAbortRef.current = null;
    },
    [],
  );

  if (recoveryError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>{labels.recovery}</AlertTitle>
        <AlertDescription className="space-y-3">
          <p>{recoveryError}</p>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              storage.removeItem(storageKey);
              setStored(null);
              setRecoveryError(null);
            }}
          >
            {labels.discard}
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  if (!session || preparingCheck) {
    const pending = stored?.state === "pending_create" ? stored : null;
    return (
      <Card className="min-w-0" data-testid="ball-annotation-setup">
        <CardHeader>
          <CardTitle>{labels.setup}</CardTitle>
          <CardDescription>{labels.setupDescription}</CardDescription>
        </CardHeader>
        <CardContent className="min-w-0 space-y-4">
          <div
            className="rounded-md border bg-muted/30 p-3 text-sm"
            data-testid="ball-annotation-setup-governance"
          >
            {BALL_ANNOTATION_OPERATOR_GOVERNANCE}
          </div>
          <div className="space-y-1">
            <Label htmlFor="ball-annotation-operator">{labels.operator}</Label>
            <Input
              id="ball-annotation-operator"
              value={operatorId}
              onChange={(event) => setOperatorId(event.target.value)}
            />
          </div>
          {role === "check" && (
            <div className="space-y-1">
              <Label htmlFor="ball-annotation-target">{labels.target}</Label>
              <Input
                id="ball-annotation-target"
                type="number"
                min={20}
                max={50}
                value={targetFrameCount}
                onChange={(event) =>
                  setTargetFrameCount(() => {
                    const nextTarget = Number(event.target.value);
                    const singleLighting = LIGHTING.filter(
                      (stratum) =>
                        applicability[stratum].status === "applicable" &&
                        applicability[stratum].frameIntervalsText.trim(),
                    );
                    if (singleLighting.length === 1) {
                      setApplicability((current) => ({
                        ...current,
                        [singleLighting[0]]: {
                          ...current[singleLighting[0]],
                          quota: nextTarget,
                        },
                      }));
                    }
                    return nextTarget;
                  })
                }
              />
            </div>
          )}
          <div className="grid min-w-0 gap-3 lg:grid-cols-2">
            {[...SCALE, ...LIGHTING].map((stratum) => (
              <fieldset
                key={stratum}
                className="min-w-0 space-y-2 rounded-md border p-3"
              >
                <legend className="px-1 font-mono text-sm">{stratum}</legend>
                <select
                  aria-label={`${stratum} applicability`}
                  className="min-h-9 w-full rounded-md border bg-background px-2"
                  value={applicability[stratum].status}
                  onChange={(event) =>
                    setApplicability((current) => ({
                      ...current,
                      [stratum]: {
                        ...current[stratum],
                        status: event.target.value as
                          | "applicable"
                          | "not_applicable",
                      },
                    }))
                  }
                >
                  <option value="applicable">{labels.applicable}</option>
                  <option value="not_applicable">{labels.notApplicable}</option>
                </select>
                <Input
                  aria-label={`${stratum} ${labels.evidence}`}
                  placeholder={labels.evidence}
                  value={applicability[stratum].evidenceNote}
                  onChange={(event) =>
                    setApplicability((current) => ({
                      ...current,
                      [stratum]: {
                        ...current[stratum],
                        evidenceNote: event.target.value,
                      },
                    }))
                  }
                />
                {role === "check" &&
                  LIGHTING.includes(stratum as (typeof LIGHTING)[number]) && (
                    <div className="space-y-2">
                      <Button
                        type="button"
                        variant="outline"
                        className="min-h-11 w-full whitespace-normal"
                        onClick={() => {
                          const endFrame = Math.max(
                            0,
                            (session?.view.source.frameCount ?? 1) - 1,
                          );
                          setApplicability((current) =>
                            Object.fromEntries(
                              Object.entries(current).map(([key, row]) =>
                                LIGHTING.includes(
                                  key as (typeof LIGHTING)[number],
                                )
                                  ? [
                                      key,
                                      {
                                        ...row,
                                        status:
                                          key === stratum
                                            ? "applicable"
                                            : "not_applicable",
                                        quota:
                                          key === stratum
                                            ? targetFrameCount
                                            : 0,
                                        frameIntervalsText:
                                          key === stratum
                                            ? `0-${endFrame}`
                                            : "",
                                      },
                                    ]
                                  : [key, row],
                              ),
                            ),
                          );
                        }}
                      >
                        {labels.fullVideoPreset}
                      </Button>
                      <Label htmlFor={`${stratum}-quota`}>{labels.quota}</Label>
                      <Input
                        id={`${stratum}-quota`}
                        aria-label={`${stratum} ${labels.quota}`}
                        type="number"
                        min={0}
                        max={50}
                        value={applicability[stratum].quota}
                        onChange={(event) =>
                          setApplicability((current) => ({
                            ...current,
                            [stratum]: {
                              ...current[stratum],
                              quota: Number(event.target.value),
                            },
                          }))
                        }
                      />
                      <Label htmlFor={`${stratum}-intervals`}>
                        {labels.intervals}
                      </Label>
                      <Input
                        id={`${stratum}-intervals`}
                        aria-label={`${stratum} ${labels.intervals}`}
                        value={applicability[stratum].frameIntervalsText}
                        onChange={(event) =>
                          setApplicability((current) => ({
                            ...current,
                            [stratum]: {
                              ...current[stratum],
                              frameIntervalsText: event.target.value,
                            },
                          }))
                        }
                      />
                    </div>
                  )}
              </fieldset>
            ))}
          </div>
          {operationError && (
            <Alert variant="destructive">
              <AlertDescription>{operationError}</AlertDescription>
            </Alert>
          )}
          <Button
            type="button"
            disabled={operationState !== "idle"}
            onClick={() => {
              try {
                void submitCreate(pending?.request ?? requestFor(role));
              } catch (error) {
                setOperationError(
                  error instanceof Error ? error.message : String(error),
                );
              }
            }}
          >
            {pending
              ? labels.resume
              : role === "check"
                ? labels.startCheck
                : labels.startDevelopment}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <section className="min-w-0 space-y-4" aria-label={labels.title}>
      <ProductionBallAnnotationPanel
        session={session.view}
        activeFrameOffset={activeFrameOffset}
        frameImageUrl={frameImageUrl}
        frameImageState={frameImageState}
        frameImageIdentity={frameImageIdentity}
        operationState={operationState}
        operationError={operationError}
        propagationSuggestions={activeFrame?.propagationSuggestions ?? []}
        propagationAvailable={session.view.dataRole === "development"}
        propagationJob={propagationJob?.view ?? null}
        reviewProxyRepairJob={reviewProxyRepairJob}
        reviewProxyRepairOperation={reviewProxyRepairOperation}
        reviewProxyRepairError={reviewProxyRepairError}
        onNavigate={setActiveFrameOffset}
        onSave={(annotation, decision) =>
          void mutate("set", annotation, undefined, decision)
        }
        onDelete={() => void mutate("delete")}
        onUndoSaved={(revision) => void mutate("undo", undefined, revision)}
        onStartPropagation={(radiusFrames) =>
          void startPropagation(radiusFrames)
        }
        onCancelPropagation={() => void cancelPropagation()}
        onStartReviewProxyRepair={startReviewProxyRepair}
        onCancelReviewProxyRepair={cancelActiveReviewProxyRepair}
        onRetryReviewProxyRepair={retryActiveReviewProxyRepair}
        onReloadReviewProxyRepair={reloadActiveReviewProxyRepair}
        onFinalize={() => void finalize()}
      />
      {dashboard && <ProductionFeasibilityDashboard view={dashboard} />}
      {session.view.dataRole === "check" &&
        session.view.status === "finalized" && (
          <Alert data-testid="new-development-evidence-batch">
            <AlertTitle>{labels.newDevelopmentBatch}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{labels.newDevelopmentBatchDescription}</p>
              <Button
                type="button"
                variant="outline"
                onClick={onStartNewDevelopmentBatch}
              >
                {labels.newDevelopmentBatch}
              </Button>
            </AlertDescription>
          </Alert>
        )}
      {session.view.dataRole === "development" &&
        session.view.status === "finalized" && (
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setRole("check");
              setPreparingCheck(true);
              const firstApplicable = LIGHTING.find(
                (stratum) => applicability[stratum].status === "applicable",
              );
              if (firstApplicable) {
                const endFrame = Math.max(
                  0,
                  session.view.source.frameCount - 1,
                );
                setApplicability((current) =>
                  Object.fromEntries(
                    Object.entries(current).map(([key, row]) =>
                      LIGHTING.includes(key as (typeof LIGHTING)[number])
                        ? [
                            key,
                            {
                              ...row,
                              status:
                                key === firstApplicable
                                  ? "applicable"
                                  : "not_applicable",
                              quota:
                                key === firstApplicable ? targetFrameCount : 0,
                              frameIntervalsText:
                                key === firstApplicable ? `0-${endFrame}` : "",
                            },
                          ]
                        : [key, row],
                    ),
                  ),
                );
              }
            }}
          >
            {labels.prepareCheck}
          </Button>
        )}
    </section>
  );
}
