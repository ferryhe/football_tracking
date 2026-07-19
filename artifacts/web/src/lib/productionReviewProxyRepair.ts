const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const SAFE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,119}$/;
const REPAIR_BASE_URL = "/api/detector-review-proxy-repairs";

export type ReviewProxyRepairStatus =
  | "queued"
  | "running"
  | "committing"
  | "ready"
  | "failed"
  | "blocked"
  | "cancelled";

export type ReviewProxyRepairStage =
  | "proxy_queued"
  | "queued"
  | "running"
  | "verifying_source"
  | "transcoding"
  | "independent_verification"
  | "recovered_after_restart"
  | "proxy_committing"
  | "proxy_ready"
  | "continuation_intent"
  | "child_probe_ready"
  | "replacement_session_ready"
  | "groups_published"
  | "ready"
  | "failed"
  | "blocked"
  | "cancelled";

export interface ReviewProxyRepairJobView {
  repairId: string;
  attemptRootRepairId: string;
  attemptNumber: number;
  retryFromRepairId: string | null;
  requestSha256: string;
  status: ReviewProxyRepairStatus;
  stage: ReviewProxyRepairStage;
  presetId: "h264-cfr-720p-v1";
  eligibility: {
    eligible: true;
    action: "generate_verified_review_proxy";
    blockerCode: "review_proxy_required";
  };
  authority: {
    blockedSessionId: string;
    blockedSessionRequestSha256: string;
    blockedSessionRecordSha256: string;
    parentProbeJobId: string;
    developmentProbeJobIds: string[];
    parentProbeRequestSha256: string;
    parentProbeIntentSha256: string;
    parentProbeSemanticIntentSha256: string;
    parentProbeReportSha256: string;
    parentProbeResultManifestSha256: string;
    parentProbeRecordSha256: string;
    parentExecutionBundleSha256: string;
    parentRuntimeEnvironmentSha256: string;
    sourceFrameEvidenceSha256: string;
    sourceId: string;
    sourceSha256: string;
    sourceFileIdentitySha256: string;
    sourceSizeBytes: number;
    sourceWidth: number;
    sourceHeight: number;
    sourceFrameCount: number;
    sourceFps: number;
    lockedProfileId: string;
    lockedProfileSha256: string;
    frameIndices: number[];
    samplingManifestSha256: string;
    temporalGroupsSha256: string;
    candidateEvidenceSha256: string;
    replacementRequestAuthoritySha256: string;
  };
  progress: {
    stageCompleted: number;
    stageTotal: number;
    sourceFramesCompleted: number;
    sourceFramesTotal: number;
    updatedAt: string;
  };
  canCancel: boolean;
  canRetry: boolean;
  result: {
    proxy: {
      reviewProxyId: string;
      reviewProxyManifestSha256: string;
      proxyMediaSha256: string;
      proxySizeBytes: number;
      proxyWidth: 2560;
      proxyHeight: 720;
      proxyFrameCount: number;
      proxyFps: number;
      mappingSha256: string;
      sampledArtifactCount: number;
      encoderBindingSha256: string;
      repairExecutionBindingSha256: string;
      repairCodeBundleSha256: string;
      repairRuntimeSha256: string;
      repairDecoderFingerprintSha256: string;
    };
    childProbe: {
      jobId: string;
      requestSha256: string;
      intentSha256: string;
      semanticIntentSha256: string;
      resourceSha256: string;
      frozenProfilesSha256: string;
      reportSha256: string;
      resultManifestSha256: string;
      executionBundleSha256: string;
      runtimeEnvironmentSha256: string;
      continuationExecutionBindingSha256: string;
      continuationCodeBundleSha256: string;
      continuationRuntimeSha256: string;
      retryFromJobId: string;
      retryKind: "review_proxy_decode_upgrade";
      statusUrl: string;
      reportUrl: string;
    };
    replacementSession: {
      sessionId: string;
      requestSha256: string;
      status: "annotating";
      retryFromSessionId: string;
      retryMode: "review_proxy_decode_upgrade";
      attemptFamilySha256: string;
      developmentProbeJobIds: string[];
      statusUrl: string;
    };
    parentProbeRecordSha256After: string;
  } | null;
  errorCode: string | null;
  blockerCode: string | null;
  recoveryAction: "retry" | "resume" | null;
  createdAt: string;
  updatedAt: string;
  statusUrl: string;
  cancelUrl: string;
  retryUrl: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(`${label} schema is invalid.`);
  }
}

function safeId(value: unknown, label: string) {
  if (typeof value !== "string" || !SAFE_ID_PATTERN.test(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function nullableSafeId(value: unknown, label: string) {
  return value === null ? null : safeId(value, label);
}

function sha256(value: unknown, label: string) {
  if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function integer(value: unknown, label: string, minimum = 0) {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    throw new Error(`${label} is invalid.`);
  }
  return value as number;
}

function finite(value: unknown, label: string, minimum = -Infinity) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function text(value: unknown, label: string) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function nullableText(value: unknown, label: string) {
  return value === null ? null : text(value, label);
}

function booleanValue(value: unknown, label: string) {
  if (typeof value !== "boolean") {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function timestamp(value: unknown, label: string) {
  const parsed = text(value, label);
  if (!Number.isFinite(Date.parse(parsed))) {
    throw new Error(`${label} is invalid.`);
  }
  return parsed;
}

function statusValue(value: unknown): ReviewProxyRepairStatus {
  if (
    value !== "queued" &&
    value !== "running" &&
    value !== "committing" &&
    value !== "ready" &&
    value !== "failed" &&
    value !== "blocked" &&
    value !== "cancelled"
  ) {
    throw new Error("Review-proxy repair status is invalid.");
  }
  return value;
}

const STAGE_RANKS: Record<ReviewProxyRepairStage, number> = {
  proxy_queued: 0,
  queued: 0,
  running: 0,
  verifying_source: 0,
  transcoding: 0,
  independent_verification: 0,
  recovered_after_restart: 0,
  proxy_committing: 0,
  failed: 0,
  blocked: 0,
  cancelled: 0,
  proxy_ready: 1,
  continuation_intent: 2,
  child_probe_ready: 3,
  replacement_session_ready: 4,
  groups_published: 5,
  ready: 6,
};

const RETRYABLE_FAILURE_CODES = new Set([
  "cancelled",
  "continuation_child_plan_changed",
  "repair_execution_binding_changed",
  "review_proxy_child_terminal",
  "review_proxy_continuation_failed",
  "review_proxy_failed",
  "review_proxy_worker_died",
  "review_proxy_worker_timeout",
  "service_shutting_down",
]);

function stageValue(value: unknown): ReviewProxyRepairStage {
  if (typeof value !== "string" || !(value in STAGE_RANKS)) {
    throw new Error("Review-proxy repair stage is invalid.");
  }
  return value as ReviewProxyRepairStage;
}

function recoveryActionValue(value: unknown): "retry" | "resume" | null {
  if (value === null || value === "retry" || value === "resume") {
    return value;
  }
  throw new Error("Repair recovery action is invalid.");
}

function parseAuthority(value: unknown): ReviewProxyRepairJobView["authority"] {
  const authority = record(value, "Review-proxy repair authority");
  exactKeys(
    authority,
    [
      "blocked_session_id",
      "blocked_session_request_sha256",
      "blocked_session_record_sha256",
      "parent_probe_job_id",
      "development_probe_job_ids",
      "parent_probe_request_sha256",
      "parent_probe_intent_sha256",
      "parent_probe_semantic_intent_sha256",
      "parent_probe_report_sha256",
      "parent_probe_result_manifest_sha256",
      "parent_probe_record_sha256",
      "parent_execution_bundle_sha256",
      "parent_runtime_environment_sha256",
      "source_frame_evidence_sha256",
      "source_id",
      "source_sha256",
      "source_file_identity_sha256",
      "source_size_bytes",
      "source_width",
      "source_height",
      "source_frame_count",
      "source_fps",
      "locked_profile_id",
      "locked_profile_sha256",
      "frame_indices",
      "sampling_manifest_sha256",
      "temporal_groups_sha256",
      "candidate_evidence_sha256",
      "replacement_request_authority_sha256",
    ],
    "Review-proxy repair authority",
  );
  const sourceFrameCount = integer(
    authority.source_frame_count,
    "Repair source frame count",
    1,
  );
  if (!Array.isArray(authority.frame_indices)) {
    throw new Error("Repair frame indices are invalid.");
  }
  const frameIndices = authority.frame_indices.map((value) =>
    integer(value, "Repair frame index"),
  );
  if (!Array.isArray(authority.development_probe_job_ids)) {
    throw new Error("Repair development probe lineage is invalid.");
  }
  const developmentProbeJobIds = authority.development_probe_job_ids.map(
    (value) => safeId(value, "Repair development probe job ID"),
  );
  const parentProbeJobId = safeId(
    authority.parent_probe_job_id,
    "Parent probe job ID",
  );
  if (
    frameIndices.length === 0 ||
    frameIndices.length > 50 ||
    frameIndices.some(
      (frameIndex, index) =>
        frameIndex >= sourceFrameCount ||
        (index > 0 && frameIndex <= frameIndices[index - 1]),
    )
  ) {
    throw new Error("Repair frame indices are invalid.");
  }
  if (
    developmentProbeJobIds.length === 0 ||
    developmentProbeJobIds.length > 7 ||
    new Set(developmentProbeJobIds).size !== developmentProbeJobIds.length ||
    developmentProbeJobIds.at(-1) !== parentProbeJobId
  ) {
    throw new Error("Repair development probe lineage is invalid.");
  }
  return {
    blockedSessionId: safeId(
      authority.blocked_session_id,
      "Blocked annotation session ID",
    ),
    blockedSessionRequestSha256: sha256(
      authority.blocked_session_request_sha256,
      "Blocked session request SHA-256",
    ),
    blockedSessionRecordSha256: sha256(
      authority.blocked_session_record_sha256,
      "Blocked session record SHA-256",
    ),
    parentProbeJobId,
    developmentProbeJobIds,
    parentProbeRequestSha256: sha256(
      authority.parent_probe_request_sha256,
      "Parent probe request SHA-256",
    ),
    parentProbeIntentSha256: sha256(
      authority.parent_probe_intent_sha256,
      "Parent probe intent SHA-256",
    ),
    parentProbeSemanticIntentSha256: sha256(
      authority.parent_probe_semantic_intent_sha256,
      "Parent probe semantic intent SHA-256",
    ),
    parentProbeReportSha256: sha256(
      authority.parent_probe_report_sha256,
      "Parent probe report SHA-256",
    ),
    parentProbeResultManifestSha256: sha256(
      authority.parent_probe_result_manifest_sha256,
      "Parent probe result manifest SHA-256",
    ),
    parentProbeRecordSha256: sha256(
      authority.parent_probe_record_sha256,
      "Parent probe record SHA-256",
    ),
    parentExecutionBundleSha256: sha256(
      authority.parent_execution_bundle_sha256,
      "Parent execution bundle SHA-256",
    ),
    parentRuntimeEnvironmentSha256: sha256(
      authority.parent_runtime_environment_sha256,
      "Parent runtime environment SHA-256",
    ),
    sourceFrameEvidenceSha256: sha256(
      authority.source_frame_evidence_sha256,
      "Source frame evidence SHA-256",
    ),
    sourceId: safeId(authority.source_id, "Repair source ID"),
    sourceSha256: sha256(authority.source_sha256, "Repair source SHA-256"),
    sourceFileIdentitySha256: sha256(
      authority.source_file_identity_sha256,
      "Repair source identity SHA-256",
    ),
    sourceSizeBytes: integer(
      authority.source_size_bytes,
      "Repair source size",
      1,
    ),
    sourceWidth: integer(authority.source_width, "Repair source width", 1),
    sourceHeight: integer(authority.source_height, "Repair source height", 1),
    sourceFrameCount,
    sourceFps: finite(
      authority.source_fps,
      "Repair source FPS",
      Number.MIN_VALUE,
    ),
    lockedProfileId: safeId(
      authority.locked_profile_id,
      "Repair locked profile ID",
    ),
    lockedProfileSha256: sha256(
      authority.locked_profile_sha256,
      "Repair locked profile SHA-256",
    ),
    frameIndices,
    samplingManifestSha256: sha256(
      authority.sampling_manifest_sha256,
      "Repair sampling manifest SHA-256",
    ),
    temporalGroupsSha256: sha256(
      authority.temporal_groups_sha256,
      "Repair temporal groups SHA-256",
    ),
    candidateEvidenceSha256: sha256(
      authority.candidate_evidence_sha256,
      "Repair candidate evidence SHA-256",
    ),
    replacementRequestAuthoritySha256: sha256(
      authority.replacement_request_authority_sha256,
      "Replacement request authority SHA-256",
    ),
  };
}

function parseResult(
  value: unknown,
  authority: ReviewProxyRepairJobView["authority"],
): NonNullable<ReviewProxyRepairJobView["result"]> {
  const result = record(value, "Review-proxy repair result");
  exactKeys(
    result,
    [
      "proxy",
      "child_probe",
      "replacement_session",
      "parent_probe_record_sha256_after",
    ],
    "Review-proxy repair result",
  );
  const proxy = record(result.proxy, "Review-proxy media result");
  exactKeys(
    proxy,
    [
      "review_proxy_id",
      "review_proxy_manifest_sha256",
      "proxy_media_sha256",
      "proxy_size_bytes",
      "proxy_width",
      "proxy_height",
      "proxy_frame_count",
      "proxy_fps",
      "mapping_sha256",
      "sampled_artifact_count",
      "encoder_binding_sha256",
      "repair_execution_binding_sha256",
      "repair_code_bundle_sha256",
      "repair_runtime_sha256",
      "repair_decoder_fingerprint_sha256",
    ],
    "Review-proxy media result",
  );
  const child = record(result.child_probe, "Review-proxy child probe");
  exactKeys(
    child,
    [
      "job_id",
      "request_sha256",
      "intent_sha256",
      "semantic_intent_sha256",
      "resource_sha256",
      "frozen_profiles_sha256",
      "report_sha256",
      "result_manifest_sha256",
      "execution_bundle_sha256",
      "runtime_environment_sha256",
      "continuation_execution_binding_sha256",
      "continuation_code_bundle_sha256",
      "continuation_runtime_sha256",
      "retry_from_job_id",
      "retry_kind",
      "status_url",
      "report_url",
    ],
    "Review-proxy child probe",
  );
  const replacement = record(
    result.replacement_session,
    "Review-proxy replacement session",
  );
  exactKeys(
    replacement,
    [
      "session_id",
      "request_sha256",
      "status",
      "retry_from_session_id",
      "retry_mode",
      "attempt_family_sha256",
      "development_probe_job_ids",
      "status_url",
    ],
    "Review-proxy replacement session",
  );
  if (!Array.isArray(replacement.development_probe_job_ids)) {
    throw new Error("Replacement development probe lineage is invalid.");
  }
  const developmentProbeJobIds = replacement.development_probe_job_ids.map(
    (value) => safeId(value, "Replacement development probe job ID"),
  );
  if (
    developmentProbeJobIds.length < 2 ||
    developmentProbeJobIds.length > 8 ||
    new Set(developmentProbeJobIds).size !== developmentProbeJobIds.length
  ) {
    throw new Error("Replacement development probe lineage is invalid.");
  }
  const parentProbeRecordSha256After = sha256(
    result.parent_probe_record_sha256_after,
    "Parent probe record SHA-256 after repair",
  );
  const childJobId = safeId(child.job_id, "Review-proxy child probe job ID");
  const childRetryFromJobId = safeId(
    child.retry_from_job_id,
    "Review-proxy child parent job ID",
  );
  const replacementSessionId = safeId(
    replacement.session_id,
    "Replacement session ID",
  );
  const retryFromSessionId = safeId(
    replacement.retry_from_session_id,
    "Replacement parent session ID",
  );
  const childStatusUrl = text(child.status_url, "Child probe status URL");
  const childReportUrl = text(child.report_url, "Child probe report URL");
  const replacementStatusUrl = text(
    replacement.status_url,
    "Replacement session status URL",
  );
  const proxyFrameCount = integer(
    proxy.proxy_frame_count,
    "Review-proxy frame count",
    1,
  );
  const proxyFps = finite(
    proxy.proxy_fps,
    "Review-proxy FPS",
    Number.MIN_VALUE,
  );
  const sampledArtifactCount = integer(
    proxy.sampled_artifact_count,
    "Review-proxy sampled artifact count",
    1,
  );
  if (
    proxy.proxy_width !== 2560 ||
    proxy.proxy_height !== 720 ||
    proxyFrameCount !== authority.sourceFrameCount ||
    Math.abs(proxyFps - authority.sourceFps) > 1e-9 ||
    sampledArtifactCount !== authority.frameIndices.length ||
    child.retry_kind !== "review_proxy_decode_upgrade" ||
    replacement.status !== "annotating" ||
    replacement.retry_mode !== "review_proxy_decode_upgrade" ||
    parentProbeRecordSha256After !== authority.parentProbeRecordSha256 ||
    childRetryFromJobId !== authority.parentProbeJobId ||
    retryFromSessionId !== authority.blockedSessionId ||
    developmentProbeJobIds.length !==
      authority.developmentProbeJobIds.length + 1 ||
    authority.developmentProbeJobIds.some(
      (jobId, index) => developmentProbeJobIds[index] !== jobId,
    ) ||
    developmentProbeJobIds.at(-1) !== childJobId ||
    childStatusUrl !== `/api/v1/detector-probes/${childJobId}` ||
    childReportUrl !== childStatusUrl ||
    replacementStatusUrl !==
      `/api/v1/ball-annotation-sessions/${replacementSessionId}`
  ) {
    throw new Error("Review-proxy repair continuation authority is invalid.");
  }
  return {
    proxy: {
      reviewProxyId: safeId(proxy.review_proxy_id, "Review-proxy ID"),
      reviewProxyManifestSha256: sha256(
        proxy.review_proxy_manifest_sha256,
        "Review-proxy manifest SHA-256",
      ),
      proxyMediaSha256: sha256(
        proxy.proxy_media_sha256,
        "Review-proxy media SHA-256",
      ),
      proxySizeBytes: integer(proxy.proxy_size_bytes, "Review-proxy size", 1),
      proxyWidth: 2560,
      proxyHeight: 720,
      proxyFrameCount,
      proxyFps,
      mappingSha256: sha256(
        proxy.mapping_sha256,
        "Review-proxy mapping SHA-256",
      ),
      sampledArtifactCount,
      encoderBindingSha256: sha256(
        proxy.encoder_binding_sha256,
        "Review-proxy encoder binding SHA-256",
      ),
      repairExecutionBindingSha256: sha256(
        proxy.repair_execution_binding_sha256,
        "Repair execution binding SHA-256",
      ),
      repairCodeBundleSha256: sha256(
        proxy.repair_code_bundle_sha256,
        "Repair code bundle SHA-256",
      ),
      repairRuntimeSha256: sha256(
        proxy.repair_runtime_sha256,
        "Repair runtime SHA-256",
      ),
      repairDecoderFingerprintSha256: sha256(
        proxy.repair_decoder_fingerprint_sha256,
        "Repair decoder fingerprint SHA-256",
      ),
    },
    childProbe: {
      jobId: childJobId,
      requestSha256: sha256(child.request_sha256, "Child request SHA-256"),
      intentSha256: sha256(child.intent_sha256, "Child intent SHA-256"),
      semanticIntentSha256: sha256(
        child.semantic_intent_sha256,
        "Child semantic intent SHA-256",
      ),
      resourceSha256: sha256(child.resource_sha256, "Child resource SHA-256"),
      frozenProfilesSha256: sha256(
        child.frozen_profiles_sha256,
        "Child frozen profiles SHA-256",
      ),
      reportSha256: sha256(child.report_sha256, "Child report SHA-256"),
      resultManifestSha256: sha256(
        child.result_manifest_sha256,
        "Child result manifest SHA-256",
      ),
      executionBundleSha256: sha256(
        child.execution_bundle_sha256,
        "Child execution bundle SHA-256",
      ),
      runtimeEnvironmentSha256: sha256(
        child.runtime_environment_sha256,
        "Child runtime environment SHA-256",
      ),
      continuationExecutionBindingSha256: sha256(
        child.continuation_execution_binding_sha256,
        "Continuation execution binding SHA-256",
      ),
      continuationCodeBundleSha256: sha256(
        child.continuation_code_bundle_sha256,
        "Continuation code bundle SHA-256",
      ),
      continuationRuntimeSha256: sha256(
        child.continuation_runtime_sha256,
        "Continuation runtime SHA-256",
      ),
      retryFromJobId: childRetryFromJobId,
      retryKind: "review_proxy_decode_upgrade",
      statusUrl: childStatusUrl,
      reportUrl: childReportUrl,
    },
    replacementSession: {
      sessionId: replacementSessionId,
      requestSha256: sha256(
        replacement.request_sha256,
        "Replacement session request SHA-256",
      ),
      status: "annotating",
      retryFromSessionId,
      retryMode: "review_proxy_decode_upgrade",
      attemptFamilySha256: sha256(
        replacement.attempt_family_sha256,
        "Replacement attempt family SHA-256",
      ),
      developmentProbeJobIds,
      statusUrl: replacementStatusUrl,
    },
    parentProbeRecordSha256After,
  };
}

export function parseReviewProxyRepairJob(
  value: unknown,
): ReviewProxyRepairJobView {
  const job = record(value, "Review-proxy repair job");
  exactKeys(
    job,
    [
      "schema_version",
      "artifact_type",
      "repair_id",
      "attempt_root_repair_id",
      "attempt_number",
      "retry_from_repair_id",
      "idempotency_key",
      "request_sha256",
      "status",
      "stage",
      "preset_id",
      "eligibility",
      "authority",
      "progress",
      "can_cancel",
      "can_retry",
      "result",
      "error_code",
      "blocker_code",
      "recovery_action",
      "created_at",
      "updated_at",
      "status_url",
      "cancel_url",
      "retry_url",
    ],
    "Review-proxy repair job",
  );
  if (
    job.schema_version !== "1.0" ||
    job.artifact_type !== "detector_review_proxy_repair_job" ||
    job.preset_id !== "h264-cfr-720p-v1"
  ) {
    throw new Error("Review-proxy repair identity is invalid.");
  }
  const repairId = safeId(job.repair_id, "Review-proxy repair ID");
  const attemptRootRepairId = safeId(
    job.attempt_root_repair_id,
    "Review-proxy repair attempt root ID",
  );
  const attemptNumber = integer(
    job.attempt_number,
    "Review-proxy repair attempt number",
    1,
  );
  const retryFromRepairId = nullableSafeId(
    job.retry_from_repair_id,
    "Review-proxy retry parent ID",
  );
  if (
    (attemptNumber === 1 &&
      (attemptRootRepairId !== repairId || retryFromRepairId !== null)) ||
    (attemptNumber > 1 &&
      (attemptRootRepairId === repairId ||
        retryFromRepairId === null ||
        retryFromRepairId === repairId))
  ) {
    throw new Error("Review-proxy repair retry lineage is invalid.");
  }
  const requestSha256 = sha256(job.request_sha256, "Repair request SHA-256");
  if (sha256(job.idempotency_key, "Repair idempotency key") !== requestSha256) {
    throw new Error("Repair idempotency authority is invalid.");
  }
  const status = statusValue(job.status);
  const stage = stageValue(job.stage);
  const eligibility = record(job.eligibility, "Repair eligibility");
  exactKeys(
    eligibility,
    ["eligible", "action", "blocker_code"],
    "Repair eligibility",
  );
  if (
    eligibility.eligible !== true ||
    eligibility.action !== "generate_verified_review_proxy" ||
    eligibility.blocker_code !== "review_proxy_required"
  ) {
    throw new Error("Repair eligibility is invalid.");
  }
  const authority = parseAuthority(job.authority);
  const progress = record(job.progress, "Review-proxy repair progress");
  exactKeys(
    progress,
    [
      "stage_completed",
      "stage_total",
      "source_frames_completed",
      "source_frames_total",
      "updated_at",
    ],
    "Review-proxy repair progress",
  );
  const stageCompleted = integer(
    progress.stage_completed,
    "Repair stage completed",
  );
  const stageTotal = integer(progress.stage_total, "Repair stage total");
  const sourceFramesCompleted = integer(
    progress.source_frames_completed,
    "Repair source frames completed",
  );
  const sourceFramesTotal = integer(
    progress.source_frames_total,
    "Repair source frames total",
    1,
  );
  const progressUpdatedAt = timestamp(
    progress.updated_at,
    "Repair progress updated time",
  );
  const updatedAt = timestamp(job.updated_at, "Repair updated time");
  const stageRank = STAGE_RANKS[stage];
  if (
    stageTotal !== 6 ||
    stageCompleted !== stageRank ||
    progressUpdatedAt !== updatedAt ||
    sourceFramesCompleted > sourceFramesTotal ||
    sourceFramesTotal !== authority.sourceFrameCount ||
    (stageRank >= 1 && sourceFramesCompleted !== sourceFramesTotal)
  ) {
    throw new Error("Repair progress authority is invalid.");
  }
  const statusStageValid =
    (status === "queued" &&
      ["proxy_queued", "queued", "recovered_after_restart"].includes(stage)) ||
    (status === "running" &&
      [
        "queued",
        "running",
        "verifying_source",
        "transcoding",
        "independent_verification",
        "recovered_after_restart",
      ].includes(stage)) ||
    (status === "committing" &&
      (stage === "proxy_committing" || (stageRank >= 1 && stageRank <= 5))) ||
    (status === "ready" && stage === "ready") ||
    (status === "failed" &&
      (stage === "failed" || (stageRank >= 1 && stageRank <= 5))) ||
    (status === "blocked" &&
      (stage === "blocked" || (stageRank >= 1 && stageRank <= 5))) ||
    (status === "cancelled" && stage === "cancelled");
  if (!statusStageValid) {
    throw new Error("Repair status and stage authority is invalid.");
  }
  const canCancel = booleanValue(job.can_cancel, "Repair cancellation state");
  if (canCancel !== (status === "queued" || status === "running")) {
    throw new Error("Repair cancellation authority is invalid.");
  }
  const canRetry = booleanValue(job.can_retry, "Repair retry state");
  const result =
    job.result === null ? null : parseResult(job.result, authority);
  if ((status === "ready") !== (result !== null)) {
    throw new Error("Repair result lifecycle is invalid.");
  }
  const errorCode = nullableText(job.error_code, "Repair error code");
  const blockerCode = nullableText(job.blocker_code, "Repair blocker code");
  const recoveryAction = recoveryActionValue(job.recovery_action);
  const active =
    status === "queued" || status === "running" || status === "committing";
  const terminalFailure = status === "failed" || status === "blocked";
  const retryableFailure = Boolean(
    terminalFailure &&
    stageRank <= 2 &&
    errorCode !== null &&
    RETRYABLE_FAILURE_CODES.has(errorCode),
  );
  const expectedCanRetry = status === "cancelled" || retryableFailure;
  const expectedRecoveryAction =
    terminalFailure && stageRank >= 3 && stageRank <= 5
      ? "resume"
      : retryableFailure
        ? "retry"
        : null;
  if (
    canRetry !== expectedCanRetry ||
    recoveryAction !== expectedRecoveryAction ||
    (active &&
      (result !== null || errorCode !== null || blockerCode !== null)) ||
    (status === "ready" &&
      (errorCode !== null ||
        blockerCode !== null ||
        recoveryAction !== null ||
        canCancel ||
        canRetry)) ||
    (status === "cancelled" &&
      (errorCode !== null ||
        blockerCode !== null ||
        recoveryAction !== null ||
        result !== null ||
        canCancel)) ||
    (terminalFailure &&
      (errorCode === null ||
        (status === "failed" && blockerCode !== null) ||
        (status === "blocked" && blockerCode !== errorCode)))
  ) {
    throw new Error("Repair failure lifecycle is invalid.");
  }
  const statusUrl = text(job.status_url, "Repair status URL");
  const cancelUrl = text(job.cancel_url, "Repair cancel URL");
  const retryUrl = text(job.retry_url, "Repair retry URL");
  const expectedStatusUrl = `/api/v1/detector-review-proxy-repairs/${repairId}`;
  if (
    statusUrl !== expectedStatusUrl ||
    cancelUrl !== `${expectedStatusUrl}/cancel` ||
    retryUrl !== `${expectedStatusUrl}/retry`
  ) {
    throw new Error("Repair control URLs are invalid.");
  }
  return {
    repairId,
    attemptRootRepairId,
    attemptNumber,
    retryFromRepairId,
    requestSha256,
    status,
    stage,
    presetId: "h264-cfr-720p-v1",
    eligibility: {
      eligible: true,
      action: "generate_verified_review_proxy",
      blockerCode: "review_proxy_required",
    },
    authority,
    progress: {
      stageCompleted,
      stageTotal,
      sourceFramesCompleted,
      sourceFramesTotal,
      updatedAt: progressUpdatedAt,
    },
    canCancel,
    canRetry,
    result,
    errorCode,
    blockerCode,
    recoveryAction,
    createdAt: timestamp(job.created_at, "Repair created time"),
    updatedAt,
    statusUrl,
    cancelUrl,
    retryUrl,
  };
}

const STATUS_ORDER: Record<ReviewProxyRepairStatus, number> = {
  queued: 0,
  running: 1,
  committing: 2,
  ready: 3,
  failed: 3,
  blocked: 3,
  cancelled: 3,
};

const TERMINAL = new Set<ReviewProxyRepairStatus>([
  "ready",
  "failed",
  "blocked",
  "cancelled",
]);

export function reviewProxyRepairUpdateIsCurrent(
  current: ReviewProxyRepairJobView,
  next: ReviewProxyRepairJobView,
) {
  if (
    current.repairId !== next.repairId ||
    current.attemptRootRepairId !== next.attemptRootRepairId ||
    current.attemptNumber !== next.attemptNumber ||
    current.retryFromRepairId !== next.retryFromRepairId ||
    current.requestSha256 !== next.requestSha256 ||
    JSON.stringify(current.authority) !== JSON.stringify(next.authority)
  ) {
    throw new Error("Review-proxy repair authority changed between polls.");
  }
  const currentTime = Date.parse(current.updatedAt);
  const nextTime = Date.parse(next.updatedAt);
  if (nextTime < currentTime) return false;
  if (TERMINAL.has(current.status)) {
    return JSON.stringify(current) === JSON.stringify(next);
  }
  if (STATUS_ORDER[next.status] < STATUS_ORDER[current.status]) return false;
  if (
    next.progress.sourceFramesCompleted <
      current.progress.sourceFramesCompleted ||
    next.progress.stageCompleted < current.progress.stageCompleted
  ) {
    return false;
  }
  return true;
}

export function requireReviewProxyRepairRetryLineage(
  current: ReviewProxyRepairJobView,
  next: ReviewProxyRepairJobView,
) {
  if (
    !current.canRetry ||
    next.attemptRootRepairId !== current.attemptRootRepairId ||
    next.attemptNumber !== current.attemptNumber + 1 ||
    next.retryFromRepairId !== current.repairId ||
    next.requestSha256 === current.requestSha256 ||
    next.presetId !== current.presetId ||
    JSON.stringify(next.eligibility) !== JSON.stringify(current.eligibility) ||
    JSON.stringify(next.authority) !== JSON.stringify(current.authority)
  ) {
    throw new Error("Review-proxy repair retry lineage is invalid.");
  }
}

export class ReviewProxyRepairRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ReviewProxyRepairRequestError";
    this.status = status;
    this.code = code;
  }
}

function reviewProxyRepairResponseError(status: number, value: unknown) {
  const envelope =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  const detail = envelope?.detail;
  if (typeof detail === "string") return new Error(detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const typed = detail as Record<string, unknown>;
    try {
      exactKeys(typed, ["code", "message"], "Review-proxy repair error");
      return new ReviewProxyRepairRequestError(
        status,
        text(typed.code, "Review-proxy repair error code"),
        text(typed.message, "Review-proxy repair error message"),
      );
    } catch {
      // Malformed or expanded error payloads do not become trusted typed data.
    }
  }
  return new Error(`Review-proxy repair request failed (${status}).`);
}

async function repairRequest(
  url: string,
  init: RequestInit,
): Promise<ReviewProxyRepairJobView> {
  const response = await fetch(url, {
    ...init,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Cache-Control": "no-store",
      ...init.headers,
    },
  });
  const raw = await response.text();
  let value: unknown = null;
  if (raw.trim()) {
    try {
      value = JSON.parse(raw);
    } catch {
      throw new Error("Review-proxy repair response is not valid JSON.");
    }
  }
  if (!response.ok) {
    throw reviewProxyRepairResponseError(response.status, value);
  }
  const cacheControl = response.headers.get("Cache-Control") ?? "";
  if (
    !cacheControl
      .split(",")
      .some((directive) => directive.trim().toLowerCase() === "no-store")
  ) {
    throw new Error(
      "Review-proxy repair response is missing no-store authority.",
    );
  }
  return parseReviewProxyRepairJob(value);
}

export function createReviewProxyRepair(
  blockedSessionId: string,
  signal?: AbortSignal,
) {
  return repairRequest(REPAIR_BASE_URL, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      blocked_session_id: safeId(
        blockedSessionId,
        "Blocked annotation session ID",
      ),
    }),
  });
}

export function getReviewProxyRepair(repairId: string, signal?: AbortSignal) {
  const id = safeId(repairId, "Review-proxy repair ID");
  return repairRequest(`${REPAIR_BASE_URL}/${id}`, {
    method: "GET",
    signal,
  });
}

export function cancelReviewProxyRepair(
  repairId: string,
  signal?: AbortSignal,
) {
  const id = safeId(repairId, "Review-proxy repair ID");
  return repairRequest(`${REPAIR_BASE_URL}/${id}/cancel`, {
    method: "POST",
    signal,
  });
}

export function retryReviewProxyRepair(repairId: string, signal?: AbortSignal) {
  const id = safeId(repairId, "Review-proxy repair ID");
  return repairRequest(`${REPAIR_BASE_URL}/${id}/retry`, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}
