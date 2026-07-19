import { pythonCanonicalSha256Sync } from "./canonicalSha256";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const SAFE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,119}$/;
const STRONG_ETAG_PATTERN = /^"[0-9a-f]{64}"$/;
const MAX_FRAME_BYTES = 32 * 1024 * 1024;
const SESSION_RESPONSE_KEYS = [
  "schema_version",
  "artifact_type",
  "session_id",
  "idempotency_key",
  "request_sha256",
  "data_role",
  "status",
  "stage",
  "source",
  "lineage",
  "locked_profile",
  "control_profile_id",
  "control_profile",
  "sampling_profile_id",
  "metric_profile_id",
  "metric_profile_sha256",
  "sampling_manifest",
  "operator_id",
  "applicable_scale_strata",
  "applicable_lighting_strata",
  "retry_from_session_id",
  "retry_lineage",
  "attempt_family_sha256",
  "development_package_binding",
  "check_probe_job_id",
  "check_probe_authority",
  "frames",
  "final_package",
  "error_code",
  "blocker_code",
  "review_proxy_repair",
  "created_at",
  "updated_at",
  "progress",
] as const;
const SESSION_INPUT_KEYS = [
  "dataRole",
  "developmentPackageSessionId",
  "developmentPackageSha256",
  "developmentProbeJobIds",
  "lockedProfileId",
  "operatorId",
  "retryFromSessionId",
  "strataApplicability",
  "targetFrameCount",
] as const;

const SCALE_STRATA = ["near", "mid", "far"] as const;
const FEASIBILITY_METRIC_PROFILE_SHA256 =
  "50320c9d6186d844e5f533193f3cc767bed9682a5c0c2c42ab17ccbf59169595";
const LIGHTING_STRATA = [
  "bright_sun",
  "shadow",
  "backlight",
  "twilight",
  "artificial_light",
] as const;
const MOTION_OCCLUSION_TAGS = [
  "ground",
  "airborne",
  "motion_blurred",
  "occluded",
  "reappearance",
  "stationary",
] as const;
const ANNOTATION_PROVENANCE = [
  "manual_human_annotation",
  "detector_candidate_human_confirmed",
  "propagation_suggestion_human_confirmed",
  "suggestion_dismissed_manual",
] as const;
const REVIEW_PROXY_FLOAT_PATHS = [
  "$.source_frame.decoder_reported_pos_msec",
  "$.proxy_frame.cfr_time_msec",
  "$.map_time_tolerance_msec",
  "$.declared_offset_msec",
  "$.time_mapping.declared_offset_msec",
  "$.time_mapping.observed_offset_msec",
  "$.time_mapping.residual_msec",
  "$.time_mapping.tolerance_msec",
] as const;
type StratumStatus = "applicable" | "not_applicable";
type StratumApplicabilityInput<T extends string> = {
  stratum: T;
  status: StratumStatus;
  evidenceNote: string;
};
type StratumApplicabilityRequest<T extends string> = {
  stratum: T;
  status: StratumStatus;
  evidence_note: string;
};
type FrameIntervalInput = { startFrame: number; endFrame: number };
type LightingApplicabilityInput = StratumApplicabilityInput<
  (typeof LIGHTING_STRATA)[number]
> & {
  quota: number;
  frameIntervals: FrameIntervalInput[];
};
type LightingApplicabilityRequest = StratumApplicabilityRequest<
  (typeof LIGHTING_STRATA)[number]
> & {
  quota: number;
  frame_intervals: Array<{ start_frame: number; end_frame: number }>;
};

export interface BallAnnotationSessionRequestInput {
  dataRole: "development" | "check";
  developmentProbeJobIds: string[];
  lockedProfileId: string;
  targetFrameCount?: number | null;
  operatorId: string;
  strataApplicability: {
    scale: Array<StratumApplicabilityInput<(typeof SCALE_STRATA)[number]>>;
    lighting: LightingApplicabilityInput[];
  };
  retryFromSessionId?: string | null;
  developmentPackageSessionId?: string | null;
  developmentPackageSha256?: string | null;
}

export interface BallAnnotationSessionCreateRequest {
  data_role: "development" | "check";
  development_probe_job_ids: string[];
  locked_profile_id: string;
  target_frame_count: number | null;
  sampling_profile_id: "tiny_ball_temporal_groups_v1";
  metric_profile_id: "tiny_ball_feasibility_metric_v1";
  operator_id: string;
  strata_applicability: {
    scale: Array<StratumApplicabilityRequest<(typeof SCALE_STRATA)[number]>>;
    lighting: LightingApplicabilityRequest[];
  };
  retry_from_session_id: string | null;
  development_package_session_id: string | null;
  development_package_sha256: string | null;
}

export interface BallAnnotationApiValue {
  point_source_px: { x: number; y: number } | null;
  bbox_source_px: {
    left: number;
    top: number;
    right: number;
    bottom: number;
  } | null;
  presence: "present" | "absent" | "unknown";
  visibility: "visible" | "partial" | "unresolvable" | "not_applicable";
  training_use: "positive" | "background" | "excluded";
  annotation_state: "suggested" | "confirmed";
  scale_stratum: "near" | "mid" | "far" | "not_applicable";
  lighting_tag: (typeof LIGHTING_STRATA)[number] | "not_applicable";
  motion_occlusion_tags: Array<(typeof MOTION_OCCLUSION_TAGS)[number]>;
  provenance: (typeof ANNOTATION_PROVENANCE)[number];
}

type BallAnnotationMutationInput =
  | {
      operation: "set";
      mutationId: string;
      expectedRevision: number;
      annotation: BallAnnotationApiValue;
      suggestionDecision?: {
        action: "accept" | "dismiss";
        kind: "detector_candidate" | "propagation";
        id: string;
        jobId: string;
        sha256: string;
      };
    }
  | {
      operation: "delete";
      mutationId: string;
      expectedRevision: number;
    }
  | {
      operation: "undo";
      mutationId: string;
      expectedRevision: number;
      undoRevision: number;
    };

export interface BallAnnotationMutationRequest {
  mutation_id: string;
  expected_revision: number;
  operation: "set" | "delete" | "undo";
  undo_revision: number | null;
  annotation: BallAnnotationApiValue | null;
  suggestion_kind: "detector_candidate" | "propagation" | null;
  suggestion_id: string | null;
  accepted_suggestion_job_id: string | null;
  accepted_suggestion_sha256: string | null;
  dismissed_suggestion_kind: "detector_candidate" | "propagation" | null;
  dismissed_suggestion_id: string | null;
  dismissed_suggestion_job_id: string | null;
  dismissed_suggestion_sha256: string | null;
}

export interface ParsedBallAnnotationSession {
  view: import("@/components/production/ProductionBallAnnotationPanel").BallAnnotationSessionView;
  developmentProbeJobIds: string[];
  developmentProbeDigestMaps?: Record<string, Record<string, string>>;
  runtimeEnvironmentSha256?: string;
  operatorId?: string;
  applicableScaleStrata?: string[];
  applicableLightingStrata?: string[];
  samplingManifestSha256: string;
  targetFrameCount: number;
}

export interface ParsedBallAnnotationRevision {
  sessionId: string;
  frameIndex: number;
  revision: number;
  operation: "set" | "delete" | "undo";
  annotationEtag: string;
}

export interface ParsedBallAnnotationFinalResult {
  packageSha256: string;
  reportSha256: string;
  dashboard:
    | import("@/components/production/ProductionFeasibilityDashboard").FeasibilityDashboardView
    | null;
}

export interface BallPropagationCreateRequest {
  mutation_id: string;
  seed_frame_index: number;
  radius_frames: number;
  expected_seed_revision: number;
}

export interface ParsedBallPropagationJob {
  view: import("@/components/production/ProductionBallAnnotationPanel").BallPropagationJobView;
  sessionId: string;
  mutationId: string;
  seedFrameIndex: number;
  expectedSeedRevision: number;
  radiusFrames: number;
  intentSha256: string;
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

function exactAllowedKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  allowed: readonly string[],
  label: string,
) {
  const actual = Object.keys(value);
  const actualKeys = new Set(actual);
  const allowedKeys = new Set(allowed);
  if (
    required.some((key) => !actualKeys.has(key)) ||
    actual.some((key) => !allowedKeys.has(key))
  ) {
    throw new Error(`${label} schema is invalid.`);
  }
}

function oneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  label: string,
): T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value as T[number];
}

function finiteNumber(value: unknown, label: string, minimum = 0) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
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

function nullableSafeId(value: unknown, label: string) {
  return value === null ? null : safeId(value, label);
}

function stringValue(value: unknown, label: string) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function parsePoint(value: unknown, label: string) {
  if (value === null) return null;
  const point = record(value, label);
  exactKeys(point, ["x", "y"], label);
  return [
    finiteNumber(point.x, `${label} x`),
    finiteNumber(point.y, `${label} y`),
  ] as [number, number];
}

function parseBox(value: unknown, label: string) {
  const box = record(value, label);
  exactKeys(box, ["left", "top", "right", "bottom"], label);
  const result = {
    left: finiteNumber(box.left, `${label} left`),
    top: finiteNumber(box.top, `${label} top`),
    right: finiteNumber(box.right, `${label} right`),
    bottom: finiteNumber(box.bottom, `${label} bottom`),
  };
  if (result.right <= result.left || result.bottom <= result.top) {
    throw new Error(`${label} has invalid bounds.`);
  }
  return result;
}

function parseTruePresentationTimestamp(value: unknown, label: string) {
  const timestamp = record(value, label);
  exactKeys(timestamp, ["status", "value_seconds", "method"], label);
  if (
    timestamp.status !== "not_collected" ||
    timestamp.value_seconds !== null ||
    timestamp.method !== null
  ) {
    throw new Error(`${label} is invalid.`);
  }
  return {
    status: "not_collected" as const,
    valueSeconds: null,
    method: null,
  };
}

function parseProxyBinding(
  value: unknown,
  context: {
    frameIndex: number;
    sourceFrameSha256: string;
    sourceTimingStatus: "observed" | "not_collected";
    decoderReportedPosMsec: number | null;
  },
) {
  if (value === null) return null;
  const binding = record(value, "Review proxy binding");
  exactKeys(
    binding,
    [
      "schema_version",
      "artifact_type",
      "proxy",
      "map_sha256",
      "source_frame",
      "proxy_frame",
      "map_time_tolerance_msec",
      "declared_offset_msec",
      "time_mapping",
      "binding_sha256",
    ],
    "Review proxy binding",
  );
  const proxy = record(binding.proxy, "Review proxy media");
  exactKeys(
    proxy,
    ["sha256", "size_bytes", "width", "height"],
    "Review proxy media",
  );
  const rawSourceFrame = record(
    binding.source_frame,
    "Review proxy source frame",
  );
  exactKeys(
    rawSourceFrame,
    ["frame_index", "timing_status", "decoder_reported_pos_msec", "sha256"],
    "Review proxy source frame",
  );
  const sourceTimingStatus = oneOf(
    rawSourceFrame.timing_status,
    ["observed", "not_collected"] as const,
    "Review proxy source timing status",
  );
  const sourceDecoderReportedPosMsec =
    rawSourceFrame.decoder_reported_pos_msec === null
      ? null
      : finiteNumber(
          rawSourceFrame.decoder_reported_pos_msec,
          "Review proxy source decoder position",
          Number.NEGATIVE_INFINITY,
        );
  const sourceFrame = {
    frameIndex: integer(
      rawSourceFrame.frame_index,
      "Review proxy source frame index",
    ),
    decoderReportedPosMsec: sourceDecoderReportedPosMsec,
    sha256: sha256(rawSourceFrame.sha256, "Review proxy source frame SHA-256"),
  };
  const rawProxyFrame = record(binding.proxy_frame, "Review proxy frame");
  exactKeys(
    rawProxyFrame,
    ["frame_index", "timing_basis", "cfr_time_msec", "sha256"],
    "Review proxy frame",
  );
  const proxyFrame = {
    frameIndex: integer(rawProxyFrame.frame_index, "Review proxy frame index"),
    decoderReportedPosMsec: finiteNumber(
      rawProxyFrame.cfr_time_msec,
      "Review proxy CFR position",
      Number.NEGATIVE_INFINITY,
    ),
    sha256: sha256(rawProxyFrame.sha256, "Review proxy frame SHA-256"),
  };
  const timeMapping = record(binding.time_mapping, "Review proxy time mapping");
  exactKeys(
    timeMapping,
    [
      "method",
      "source_timing_status",
      "proxy_timing_basis",
      "declared_offset_msec",
      "observed_offset_msec",
      "residual_msec",
      "tolerance_msec",
    ],
    "Review proxy time mapping",
  );
  const mapTimeToleranceMsec = finiteNumber(
    binding.map_time_tolerance_msec,
    "Review proxy map tolerance",
  );
  const declaredOffsetMsec = finiteNumber(
    binding.declared_offset_msec,
    "Review proxy declared offset",
    Number.NEGATIVE_INFINITY,
  );
  const observedOffsetMsec =
    timeMapping.observed_offset_msec === null
      ? null
      : finiteNumber(
          timeMapping.observed_offset_msec,
          "Review proxy observed offset",
          Number.NEGATIVE_INFINITY,
        );
  const residualMsec =
    timeMapping.residual_msec === null
      ? null
      : finiteNumber(
          timeMapping.residual_msec,
          "Review proxy residual",
          Number.NEGATIVE_INFINITY,
        );
  const timeToleranceMsec = finiteNumber(
    timeMapping.tolerance_msec,
    "Review proxy time tolerance",
  );
  const mappedObservedOffset =
    sourceDecoderReportedPosMsec === null
      ? null
      : proxyFrame.decoderReportedPosMsec - sourceDecoderReportedPosMsec;
  const observedTiming = sourceTimingStatus === "observed";
  if (
    binding.schema_version !== "1.0" ||
    binding.artifact_type !== "ball_review_proxy_frame_binding" ||
    rawProxyFrame.timing_basis !== "verified_cfr_frame_index_time_v1" ||
    timeMapping.proxy_timing_basis !== "verified_cfr_frame_index_time_v1" ||
    timeMapping.source_timing_status !== sourceTimingStatus ||
    sourceTimingStatus !== context.sourceTimingStatus ||
    (observedTiming
      ? timeMapping.method !== "explicit_per_frame_decoder_pos_msec_map_v1" ||
        sourceDecoderReportedPosMsec === null ||
        observedOffsetMsec === null ||
        residualMsec === null ||
        context.decoderReportedPosMsec === null
      : timeMapping.method !== "exact_frame_index_to_verified_proxy_cfr_v1" ||
        sourceDecoderReportedPosMsec !== null ||
        observedOffsetMsec !== null ||
        residualMsec !== null ||
        context.decoderReportedPosMsec !== null) ||
    sourceFrame.frameIndex !== context.frameIndex ||
    proxyFrame.frameIndex !== context.frameIndex ||
    sourceFrame.sha256 !== context.sourceFrameSha256 ||
    (observedTiming &&
      Math.abs(
        (sourceDecoderReportedPosMsec as number) -
          (context.decoderReportedPosMsec as number),
      ) > mapTimeToleranceMsec) ||
    Math.abs(timeToleranceMsec - mapTimeToleranceMsec) > 1e-9 ||
    Math.abs(
      finiteNumber(
        timeMapping.declared_offset_msec,
        "Review proxy mapped declared offset",
        Number.NEGATIVE_INFINITY,
      ) - declaredOffsetMsec,
    ) > 1e-9 ||
    (observedTiming &&
      (Math.abs(
        (observedOffsetMsec as number) - (mappedObservedOffset as number),
      ) > 1e-9 ||
        Math.abs(
          (residualMsec as number) -
            ((mappedObservedOffset as number) - declaredOffsetMsec),
        ) > 1e-9 ||
        Math.abs(residualMsec as number) > mapTimeToleranceMsec))
  ) {
    throw new Error("Review proxy frame/time authority is invalid.");
  }
  const bindingSha256 = sha256(
    binding.binding_sha256,
    "Review proxy binding SHA-256",
  );
  const bindingCore = {
    schema_version: binding.schema_version,
    artifact_type: binding.artifact_type,
    proxy: {
      sha256: sha256(proxy.sha256, "Review proxy media SHA-256"),
      size_bytes: integer(proxy.size_bytes, "Review proxy media size", 1),
      width: integer(proxy.width, "Review proxy width", 1),
      height: integer(proxy.height, "Review proxy height", 1),
    },
    map_sha256: sha256(binding.map_sha256, "Review proxy map SHA-256"),
    source_frame: binding.source_frame,
    proxy_frame: binding.proxy_frame,
    map_time_tolerance_msec: mapTimeToleranceMsec,
    declared_offset_msec: declaredOffsetMsec,
    time_mapping: binding.time_mapping,
  };
  if (computeReviewProxyBindingSha256(bindingCore) !== bindingSha256) {
    throw new Error("Review proxy binding digest is invalid.");
  }
  return {
    proxySha256: bindingCore.proxy.sha256,
    proxySizeBytes: bindingCore.proxy.size_bytes,
    proxyWidth: bindingCore.proxy.width,
    proxyHeight: bindingCore.proxy.height,
    mapSha256: bindingCore.map_sha256,
    bindingSha256,
    sourceFrame,
    proxyFrame,
    mapTimeToleranceMsec,
    declaredOffsetMsec,
    observedOffsetMsec,
    residualMsec,
  };
}

export function computeReviewProxyBindingSha256(value: unknown) {
  return pythonCanonicalSha256Sync(value, REVIEW_PROXY_FLOAT_PATHS);
}

function parseAnnotation(
  value: unknown,
  label: string,
  context: {
    width: number;
    height: number;
    dataRole: "development" | "check";
  },
) {
  if (value === null) return null;
  const annotation = record(value, label);
  exactKeys(
    annotation,
    [
      "point_source_px",
      "bbox_source_px",
      "presence",
      "visibility",
      "training_use",
      "annotation_state",
      "scale_stratum",
      "lighting_tag",
      "motion_occlusion_tags",
      "provenance",
    ],
    label,
  );
  const tags = Array.isArray(annotation.motion_occlusion_tags)
    ? annotation.motion_occlusion_tags.map((tag) =>
        oneOf(tag, MOTION_OCCLUSION_TAGS, `${label} motion tag`),
      )
    : (() => {
        throw new Error(`${label} motion tags are invalid.`);
      })();
  if (tags.length > 6 || new Set(tags).size !== tags.length) {
    throw new Error(`${label} motion tags are invalid.`);
  }
  const result = {
    point: parsePoint(annotation.point_source_px, `${label} point`),
    bbox:
      annotation.bbox_source_px === null
        ? null
        : parseBox(annotation.bbox_source_px, `${label} box`),
    presence: oneOf(
      annotation.presence,
      ["present", "absent", "unknown"] as const,
      `${label} presence`,
    ),
    visibility: oneOf(
      annotation.visibility,
      ["visible", "partial", "unresolvable", "not_applicable"] as const,
      `${label} visibility`,
    ),
    trainingUse: oneOf(
      annotation.training_use,
      ["positive", "background", "excluded"] as const,
      `${label} training use`,
    ),
    annotationState: oneOf(
      annotation.annotation_state,
      ["suggested", "confirmed"] as const,
      `${label} state`,
    ),
    scaleStratum: oneOf(
      annotation.scale_stratum,
      ["near", "mid", "far", "not_applicable"] as const,
      `${label} scale`,
    ),
    lightingTag: oneOf(
      annotation.lighting_tag,
      [...LIGHTING_STRATA, "not_applicable"] as const,
      `${label} lighting`,
    ),
    motionOcclusionTags: tags,
    provenance: oneOf(
      annotation.provenance,
      ANNOTATION_PROVENANCE,
      `${label} provenance`,
    ),
  };
  if (
    result.point &&
    (result.point[0] >= context.width || result.point[1] >= context.height)
  ) {
    throw new Error(`${label} point is outside the source frame.`);
  }
  if (
    result.bbox &&
    (result.bbox.right > context.width || result.bbox.bottom > context.height)
  ) {
    throw new Error(`${label} box is outside the source frame.`);
  }
  if (result.point && result.bbox) {
    const centerX = (result.bbox.left + result.bbox.right) / 2;
    const centerY = (result.bbox.top + result.bbox.bottom) / 2;
    if (
      Math.abs(result.point[0] - centerX) > 0.5 ||
      Math.abs(result.point[1] - centerY) > 0.5
    ) {
      throw new Error(`${label} point and box centers are inconsistent.`);
    }
    result.point = [centerX, centerY];
  } else if (!result.point && result.bbox) {
    result.point = [
      (result.bbox.left + result.bbox.right) / 2,
      (result.bbox.top + result.bbox.bottom) / 2,
    ];
  }
  if (
    (context.dataRole === "check" && result.trainingUse !== "excluded") ||
    (result.annotationState === "suggested" &&
      result.trainingUse !== "excluded")
  ) {
    throw new Error(`${label} violates the training boundary.`);
  }
  if (result.presence === "absent") {
    if (
      result.point ||
      result.bbox ||
      result.visibility !== "not_applicable" ||
      !["background", "excluded"].includes(result.trainingUse) ||
      result.scaleStratum !== "not_applicable"
    ) {
      throw new Error(`${label} has invalid absent semantics.`);
    }
  } else if (result.presence === "unknown") {
    if (
      result.point ||
      result.bbox ||
      !["unresolvable", "not_applicable"].includes(result.visibility) ||
      result.trainingUse !== "excluded" ||
      result.scaleStratum !== "not_applicable"
    ) {
      throw new Error(`${label} has invalid unknown semantics.`);
    }
  } else if (result.visibility === "unresolvable") {
    if (
      result.point ||
      result.bbox ||
      result.trainingUse !== "excluded" ||
      result.scaleStratum !== "not_applicable"
    ) {
      throw new Error(`${label} has invalid unresolvable semantics.`);
    }
  } else if (
    !["visible", "partial"].includes(result.visibility) ||
    !result.point ||
    result.scaleStratum === "not_applicable" ||
    result.trainingUse === "background" ||
    (context.dataRole === "check" && !result.bbox) ||
    (result.trainingUse === "positive" &&
      (result.annotationState !== "confirmed" || !result.bbox))
  ) {
    throw new Error(`${label} has invalid localizable semantics.`);
  }
  return result;
}

function safeId(value: unknown, label: string) {
  if (typeof value !== "string" || !SAFE_ID_PATTERN.test(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

function exactInputKeys(value: object) {
  const actual = Object.keys(value).sort();
  const expected = SESSION_INPUT_KEYS.filter(
    (key) =>
      ![
        "developmentPackageSessionId",
        "developmentPackageSha256",
        "retryFromSessionId",
        "targetFrameCount",
      ].includes(key) || key in value,
  ).sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(
      "Ball annotation session input contains client authority fields.",
    );
  }
}

function normalizeLightingRows(
  rows: unknown,
  dataRole: "development" | "check",
  targetFrameCount: number | null,
): LightingApplicabilityRequest[] {
  if (
    !Array.isArray(rows) ||
    rows.length !== LIGHTING_STRATA.length ||
    rows.some(
      (row) =>
        !row ||
        typeof row !== "object" ||
        Array.isArray(row) ||
        Object.keys(row).sort().join("|") !==
          "evidenceNote|frameIntervals|quota|status|stratum",
    )
  ) {
    throw new Error("Every lighting stratum requires sampling evidence.");
  }
  const typedRows = rows as LightingApplicabilityInput[];
  const byStratum = new Map(typedRows.map((row) => [row.stratum, row]));
  if (
    byStratum.size !== LIGHTING_STRATA.length ||
    LIGHTING_STRATA.some((stratum) => !byStratum.has(stratum))
  ) {
    throw new Error("lighting strata are incomplete or duplicated.");
  }
  const normalized = LIGHTING_STRATA.map((stratum) => {
    const row = byStratum.get(stratum);
    if (
      !row ||
      !["applicable", "not_applicable"].includes(row.status) ||
      typeof row.evidenceNote !== "string" ||
      row.evidenceNote.trim().length < 3 ||
      row.evidenceNote.length > 500 ||
      !Number.isSafeInteger(row.quota) ||
      row.quota < 0 ||
      row.quota > 50 ||
      !Array.isArray(row.frameIntervals) ||
      row.frameIntervals.length > 32
    ) {
      throw new Error("lighting applicability evidence is invalid.");
    }
    const frameIntervals = row.frameIntervals.map((interval) => {
      if (
        !interval ||
        typeof interval !== "object" ||
        Array.isArray(interval) ||
        Object.keys(interval).sort().join("|") !== "endFrame|startFrame" ||
        !Number.isSafeInteger(interval.startFrame) ||
        interval.startFrame < 0 ||
        !Number.isSafeInteger(interval.endFrame) ||
        interval.endFrame < interval.startFrame
      ) {
        throw new Error("lighting frame interval is invalid.");
      }
      return {
        start_frame: interval.startFrame,
        end_frame: interval.endFrame,
      };
    });
    if (
      (dataRole === "development" &&
        (row.quota !== 0 || frameIntervals.length !== 0)) ||
      (row.status === "not_applicable" &&
        (row.quota !== 0 || frameIntervals.length !== 0)) ||
      (dataRole === "check" &&
        row.status === "applicable" &&
        (row.quota < 3 || frameIntervals.length === 0))
    ) {
      throw new Error(
        "lighting quota and intervals do not match applicability.",
      );
    }
    return {
      stratum,
      status: row.status,
      evidence_note: row.evidenceNote,
      quota: row.quota,
      frame_intervals: frameIntervals,
    };
  });
  if (!normalized.some((row) => row.status === "applicable")) {
    throw new Error("At least one lighting stratum must be applicable.");
  }
  if (
    dataRole === "check" &&
    normalized.reduce((total, row) => total + row.quota, 0) !== targetFrameCount
  ) {
    throw new Error("Check lighting quotas must sum to target frame count.");
  }
  return normalized;
}

function normalizeStrataRows<const T extends readonly string[]>(
  rows: unknown,
  allowed: T,
  dimension: string,
): Array<StratumApplicabilityRequest<T[number]>> {
  if (
    !Array.isArray(rows) ||
    rows.length !== allowed.length ||
    rows.some(
      (row) =>
        !row ||
        typeof row !== "object" ||
        Array.isArray(row) ||
        Object.keys(row).sort().join("|") !== "evidenceNote|status|stratum",
    )
  ) {
    throw new Error(`Every ${dimension} stratum requires evidence.`);
  }
  const typedRows = rows as Array<StratumApplicabilityInput<T[number]>>;
  const byStratum = new Map(typedRows.map((row) => [row.stratum, row]));
  if (
    byStratum.size !== allowed.length ||
    allowed.some((stratum) => !byStratum.has(stratum))
  ) {
    throw new Error(`${dimension} strata are incomplete or duplicated.`);
  }
  const normalized = allowed.map((stratum) => {
    const row = byStratum.get(stratum);
    if (
      !row ||
      !["applicable", "not_applicable"].includes(row.status) ||
      typeof row.evidenceNote !== "string" ||
      row.evidenceNote.trim().length < 3 ||
      row.evidenceNote.length > 500
    ) {
      throw new Error(`${dimension} applicability evidence is invalid.`);
    }
    return {
      stratum,
      status: row.status,
      evidence_note: row.evidenceNote,
    };
  });
  if (!normalized.some((row) => row.status === "applicable")) {
    throw new Error(`At least one ${dimension} stratum must be applicable.`);
  }
  return normalized;
}

export function buildBallAnnotationSessionRequest(
  input: BallAnnotationSessionRequestInput,
): BallAnnotationSessionCreateRequest {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Ball annotation session input is invalid.");
  }
  exactInputKeys(input);
  if (input.dataRole !== "development" && input.dataRole !== "check") {
    throw new Error("Ball annotation data role is invalid.");
  }
  if (
    !Array.isArray(input.developmentProbeJobIds) ||
    input.developmentProbeJobIds.length < 1 ||
    input.developmentProbeJobIds.length > 8
  ) {
    throw new Error("One to eight ready development probe jobs are required.");
  }
  const probeJobIds = input.developmentProbeJobIds.map((jobId) =>
    safeId(jobId, "Development probe job ID"),
  );
  if (new Set(probeJobIds).size !== probeJobIds.length) {
    throw new Error("Development probe job IDs contain duplicates.");
  }
  if (
    input.dataRole === "development" &&
    input.targetFrameCount !== undefined &&
    input.targetFrameCount !== null
  ) {
    throw new Error(
      "Development sessions use the actual revealed T2 frame count.",
    );
  }
  if (
    input.dataRole === "development" &&
    input.retryFromSessionId !== undefined &&
    input.retryFromSessionId !== null
  ) {
    throw new Error("Only a blocked check session can be retried.");
  }
  if (
    input.dataRole === "development" &&
    (input.developmentPackageSessionId != null ||
      input.developmentPackageSha256 != null)
  ) {
    throw new Error("Development sessions cannot bind a development package.");
  }
  if (
    input.dataRole === "check" &&
    (input.developmentPackageSessionId == null ||
      input.developmentPackageSha256 == null)
  ) {
    throw new Error("Check sessions require a finalized development package.");
  }
  if (
    input.dataRole === "check" &&
    (!Number.isSafeInteger(input.targetFrameCount) ||
      (input.targetFrameCount ?? 0) < 20 ||
      (input.targetFrameCount ?? 0) > 50)
  ) {
    throw new Error("Target frame count must be between 20 and 50.");
  }
  return {
    data_role: input.dataRole,
    development_probe_job_ids: probeJobIds.sort(),
    locked_profile_id: safeId(input.lockedProfileId, "Locked profile ID"),
    target_frame_count:
      input.dataRole === "development" ? null : input.targetFrameCount!,
    sampling_profile_id: "tiny_ball_temporal_groups_v1",
    metric_profile_id: "tiny_ball_feasibility_metric_v1",
    operator_id: safeId(input.operatorId, "Operator ID"),
    strata_applicability: {
      scale: normalizeStrataRows(
        input.strataApplicability?.scale,
        SCALE_STRATA,
        "scale",
      ),
      lighting: normalizeLightingRows(
        input.strataApplicability?.lighting,
        input.dataRole,
        input.dataRole === "check" ? input.targetFrameCount! : null,
      ),
    },
    retry_from_session_id:
      input.retryFromSessionId === undefined ||
      input.retryFromSessionId === null
        ? null
        : safeId(input.retryFromSessionId, "Retry session ID"),
    development_package_session_id:
      input.dataRole === "check"
        ? safeId(
            input.developmentPackageSessionId,
            "Development package session ID",
          )
        : null,
    development_package_sha256:
      input.dataRole === "check"
        ? sha256(input.developmentPackageSha256, "Development package SHA-256")
        : null,
  };
}

export function buildBallAnnotationMutation(
  input: BallAnnotationMutationInput,
): BallAnnotationMutationRequest {
  const mutationId = safeId(input.mutationId, "Annotation mutation ID");
  if (
    !Number.isSafeInteger(input.expectedRevision) ||
    input.expectedRevision < 0
  ) {
    throw new Error("Expected annotation revision is invalid.");
  }
  if (input.operation === "set") {
    if (!input.annotation || typeof input.annotation !== "object") {
      throw new Error("Set mutation requires an annotation.");
    }
    const decision = input.suggestionDecision;
    if (
      decision &&
      (typeof decision !== "object" ||
        Array.isArray(decision) ||
        Object.keys(decision).sort().join("|") !==
          "action|id|jobId|kind|sha256")
    ) {
      throw new Error("Suggestion decision authority is invalid.");
    }
    const action = decision?.action ?? null;
    const kind = decision
      ? oneOf(
          decision.kind,
          ["detector_candidate", "propagation"] as const,
          "Suggestion kind",
        )
      : null;
    const suggestionId = decision ? safeId(decision.id, "Suggestion ID") : null;
    const suggestionJobId = decision
      ? safeId(decision.jobId, "Suggestion job ID")
      : null;
    const suggestionSha256 = decision
      ? sha256(decision.sha256, "Suggestion SHA-256")
      : null;
    if (decision && action !== "accept" && action !== "dismiss") {
      throw new Error("Suggestion decision action is invalid.");
    }
    const expectedProvenance =
      action === "accept" && kind === "detector_candidate"
        ? "detector_candidate_human_confirmed"
        : action === "accept" && kind === "propagation"
          ? "propagation_suggestion_human_confirmed"
          : action === "dismiss"
            ? "suggestion_dismissed_manual"
            : "manual_human_annotation";
    if (
      input.annotation.annotation_state !== "confirmed" ||
      input.annotation.provenance !== expectedProvenance
    ) {
      throw new Error(
        "Annotation state or provenance conflicts with suggestion decision.",
      );
    }
    return {
      mutation_id: mutationId,
      expected_revision: input.expectedRevision,
      operation: "set",
      undo_revision: null,
      annotation: input.annotation,
      suggestion_kind: action === "accept" ? kind : null,
      suggestion_id: action === "accept" ? suggestionId : null,
      accepted_suggestion_job_id: action === "accept" ? suggestionJobId : null,
      accepted_suggestion_sha256: action === "accept" ? suggestionSha256 : null,
      dismissed_suggestion_kind: action === "dismiss" ? kind : null,
      dismissed_suggestion_id: action === "dismiss" ? suggestionId : null,
      dismissed_suggestion_job_id:
        action === "dismiss" ? suggestionJobId : null,
      dismissed_suggestion_sha256:
        action === "dismiss" ? suggestionSha256 : null,
    };
  }
  if (input.operation === "delete") {
    return {
      mutation_id: mutationId,
      expected_revision: input.expectedRevision,
      operation: "delete",
      undo_revision: null,
      annotation: null,
      suggestion_kind: null,
      suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
    };
  }
  if (
    !Number.isSafeInteger(input.undoRevision) ||
    input.undoRevision < 1 ||
    input.undoRevision > input.expectedRevision
  ) {
    throw new Error("Undo revision is invalid.");
  }
  return {
    mutation_id: mutationId,
    expected_revision: input.expectedRevision,
    operation: "undo",
    undo_revision: input.undoRevision,
    annotation: null,
    suggestion_kind: null,
    suggestion_id: null,
    accepted_suggestion_job_id: null,
    accepted_suggestion_sha256: null,
    dismissed_suggestion_kind: null,
    dismissed_suggestion_id: null,
    dismissed_suggestion_job_id: null,
    dismissed_suggestion_sha256: null,
  };
}

export function buildBallPropagationRequest(input: {
  mutationId: string;
  seedFrameIndex: number;
  radiusFrames: number;
  expectedSeedRevision: number;
}): BallPropagationCreateRequest {
  if (
    !input ||
    typeof input !== "object" ||
    Array.isArray(input) ||
    Object.keys(input).sort().join("|") !==
      "expectedSeedRevision|mutationId|radiusFrames|seedFrameIndex"
  ) {
    throw new Error("Propagation request authority is invalid.");
  }
  const seedFrameIndex = integer(
    input.seedFrameIndex,
    "Propagation seed frame",
  );
  const radiusFrames = integer(input.radiusFrames, "Propagation radius", 1);
  const expectedSeedRevision = integer(
    input.expectedSeedRevision,
    "Propagation seed revision",
    1,
  );
  if (radiusFrames > 2) {
    throw new Error("Propagation radius must be one or two frames.");
  }
  return {
    mutation_id: safeId(input.mutationId, "Propagation mutation ID"),
    seed_frame_index: seedFrameIndex,
    radius_frames: radiusFrames,
    expected_seed_revision: expectedSeedRevision,
  };
}

export function ballAnnotationStorageKey(
  workflowId: string,
  developmentProbeJobId: string,
) {
  return `football-tracking.ball-annotation.v1.${safeId(
    workflowId,
    "Workflow ID",
  )}.${safeId(developmentProbeJobId, "Development probe job ID")}`;
}

function parseProfile(value: unknown, label: string) {
  const profile = record(value, label);
  exactKeys(
    profile,
    [
      "profile_id",
      "profile_sha256",
      "model_id",
      "model_version",
      "model_descriptor_sha256",
      "weights_sha256",
    ],
    label,
  );
  return {
    profileId: safeId(profile.profile_id, `${label} ID`),
    profileSha256: sha256(profile.profile_sha256, `${label} SHA-256`),
  };
}

const MANIFEST_KEYS = [
  "schema_version",
  "artifact_type",
  "profile_id",
  "selection_profile_id",
  "scale_stratification_mode",
  "lighting_stratification_mode",
  "selection_seed_sha256",
  "candidate_universe_sha256",
  "candidate_universe_start_frame",
  "candidate_universe_end_frame",
  "selection_authority",
  "candidate_universe_authority",
  "metric_profile_id",
  "metric_profile_sha256",
  "data_role",
  "target_frame_count",
  "frame_indices",
  "groups",
  "excluded_development_groups",
  "locked_before_probe",
  "source_sha256",
  "locked_profile_id",
  "locked_profile_sha256",
  "strata_applicability",
  "manifest_sha256",
] as const;

function parseLineage(value: unknown) {
  const lineage = record(value, "Annotation lineage");
  exactKeys(
    lineage,
    [
      "parent_trial_id",
      "development_probe_job_ids",
      "development_probe_report_sha256s",
      "development_probe_result_manifest_sha256s",
      "development_probe_execution_bundle_sha256s",
      "development_probe_frozen_profiles_sha256s",
      "decode",
      "runtime_environment_sha256",
    ],
    "Annotation lineage",
  );
  safeId(lineage.parent_trial_id, "Parent trial ID");
  const runtimeEnvironmentSha256 = sha256(
    lineage.runtime_environment_sha256,
    "Runtime environment SHA-256",
  );
  const decode = record(lineage.decode, "Decode authority");
  exactKeys(
    decode,
    [
      "width",
      "height",
      "frame_count",
      "fps",
      "requested_decode_mode",
      "effective_decode_mode",
      "position_verification",
    ],
    "Decode authority",
  );
  const normalizedDecode = {
    width: integer(decode.width, "Decode width", 1),
    height: integer(decode.height, "Decode height", 1),
    frame_count: integer(decode.frame_count, "Decode frame count", 1),
    fps: finiteNumber(decode.fps, "Decode FPS", Number.MIN_VALUE),
    requested_decode_mode: oneOf(
      decode.requested_decode_mode,
      ["sequential", "preroll", "direct"] as const,
      "Requested decode mode",
    ),
    effective_decode_mode: oneOf(
      decode.effective_decode_mode,
      [
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
      ] as const,
      "Effective decode mode",
    ),
    position_verification: oneOf(
      decode.position_verification,
      [
        "opencv_next_frame_index_with_0.25_tolerance",
        "verified_review_proxy_frame_index_mapping_v1",
      ] as const,
      "Decode position verification",
    ),
  };
  if (!Array.isArray(lineage.development_probe_job_ids)) {
    throw new Error("Annotation development lineage is invalid.");
  }
  const jobIds = lineage.development_probe_job_ids.map((id) =>
    safeId(id, "Development probe job ID"),
  );
  if (jobIds.length < 1 || new Set(jobIds).size !== jobIds.length) {
    throw new Error("Annotation development lineage is invalid.");
  }
  const digestMaps: Record<string, Record<string, string>> = {};
  for (const [field, label] of [
    ["development_probe_report_sha256s", "Development report map"],
    [
      "development_probe_result_manifest_sha256s",
      "Development result manifest map",
    ],
    [
      "development_probe_execution_bundle_sha256s",
      "Development execution bundle map",
    ],
    [
      "development_probe_frozen_profiles_sha256s",
      "Development frozen profiles map",
    ],
  ] as const) {
    const digests = record(lineage[field], label);
    exactKeys(digests, jobIds, label);
    digestMaps[field] = Object.fromEntries(
      jobIds.map((jobId) => [
        jobId,
        sha256(digests[jobId], `${label} SHA-256`),
      ]),
    );
  }
  return {
    jobIds,
    decode: normalizedDecode,
    digestMaps,
    runtimeEnvironmentSha256,
  };
}

function parseApplicability(value: unknown) {
  const applicability = record(value, "Strata applicability");
  exactKeys(applicability, ["scale", "lighting"], "Strata applicability");
  const applicable: {
    scale: string[];
    lighting: string[];
    lightingQuota: number;
  } = {
    scale: [],
    lighting: [],
    lightingQuota: 0,
  };
  for (const [dimension, allowed] of [
    ["scale", SCALE_STRATA],
    ["lighting", LIGHTING_STRATA],
  ] as const) {
    const rows = applicability[dimension];
    if (!Array.isArray(rows) || rows.length !== allowed.length) {
      throw new Error(`${dimension} applicability is invalid.`);
    }
    const seen = new Set<string>();
    rows.forEach((rawRow) => {
      const row = record(rawRow, `${dimension} applicability row`);
      exactKeys(
        row,
        dimension === "lighting"
          ? ["stratum", "status", "evidence", "quota", "frame_intervals"]
          : ["stratum", "status", "evidence"],
        `${dimension} applicability row`,
      );
      const stratum = oneOf(
        row.stratum,
        allowed,
        `${dimension} applicability stratum`,
      );
      seen.add(stratum);
      const status = oneOf(
        row.status,
        ["applicable", "not_applicable"] as const,
        `${dimension} applicability status`,
      );
      const evidence = record(row.evidence, `${dimension} evidence`);
      exactKeys(
        evidence,
        ["declared_before_reveal", "note", "evidence_sha256"],
        `${dimension} evidence`,
      );
      if (
        evidence.declared_before_reveal !== true ||
        stringValue(evidence.note, `${dimension} evidence note`).trim().length <
          3
      ) {
        throw new Error(`${dimension} evidence is invalid.`);
      }
      sha256(evidence.evidence_sha256, `${dimension} evidence SHA-256`);
      if (dimension === "lighting") {
        const quota = integer(row.quota, "Lighting quota");
        applicable.lightingQuota += quota;
        if (
          !Array.isArray(row.frame_intervals) ||
          row.frame_intervals.length > 32
        ) {
          throw new Error("lighting frame intervals are invalid.");
        }
        const intervals = row.frame_intervals.map((rawInterval) => {
          const interval = record(rawInterval, "Lighting frame interval");
          exactKeys(
            interval,
            ["start_frame", "end_frame"],
            "Lighting frame interval",
          );
          const startFrame = integer(
            interval.start_frame,
            "Lighting interval start",
          );
          const endFrame = integer(interval.end_frame, "Lighting interval end");
          if (endFrame < startFrame) {
            throw new Error("lighting frame interval is invalid.");
          }
          return { startFrame, endFrame };
        });
        if (
          (status === "not_applicable" && (quota !== 0 || intervals.length)) ||
          (quota === 0 && intervals.length)
        ) {
          throw new Error("lighting quota conflicts with applicability.");
        }
      }
      if (status === "applicable") applicable[dimension].push(stratum);
    });
    if (seen.size !== allowed.length) {
      throw new Error(`${dimension} applicability is duplicated.`);
    }
  }
  return applicable;
}

function parseTemporalGroup(
  value: unknown,
  sourceSha256: string,
  label: string,
) {
  const group = record(value, label);
  exactKeys(
    group,
    [
      "group_id",
      "profile_id",
      "source_sha256",
      "seed_frame_index",
      "start_frame",
      "end_frame",
      "derivative_family",
      "canonical_moment_id",
      "derivative_family_id",
      "ancestry_profile",
      "frame_index",
      "pre_reveal_lighting_stratum",
    ],
    label,
  );
  const groupId = sha256(group.group_id, `${label} ID`);
  const frameIndex = integer(group.frame_index, `${label} frame index`);
  const seedFrameIndex = integer(
    group.seed_frame_index,
    `${label} seed frame index`,
  );
  const startFrame = integer(group.start_frame, `${label} start frame`);
  const endFrame = integer(group.end_frame, `${label} end frame`);
  if (
    group.profile_id !== "tiny_ball_temporal_groups_v1" ||
    group.source_sha256 !== sourceSha256 ||
    seedFrameIndex !== frameIndex ||
    startFrame > frameIndex ||
    endFrame < frameIndex ||
    group.derivative_family_id !== groupId ||
    group.ancestry_profile !==
      "source-proxy-crop-tile-propagation-closure-v1" ||
    !Array.isArray(group.derivative_family) ||
    group.derivative_family.length !== 2 ||
    group.derivative_family[0] !== startFrame ||
    group.derivative_family[1] !== endFrame
  ) {
    throw new Error(`${label} authority is invalid.`);
  }
  sha256(group.canonical_moment_id, `${label} canonical moment ID`);
  if (
    group.pre_reveal_lighting_stratum !== null &&
    !LIGHTING_STRATA.includes(group.pre_reveal_lighting_stratum as never)
  ) {
    throw new Error(`${label} lighting stratum is invalid.`);
  }
  return { frameIndex, groupId, startFrame, endFrame };
}

function parseCheckAuthority(value: unknown, expectedJobId: string | null) {
  if (value === null) return null;
  const authority = record(value, "Check probe authority");
  exactKeys(
    authority,
    [
      "job_id",
      "request_sha256",
      "intent_sha256",
      "result_manifest_sha256",
      "report_sha256",
      "parent_trial_id",
      "runtime_environment_sha256",
      "execution_bundle_sha256",
      "frozen_profiles_sha256",
      "locked_profile",
      "control_profile",
    ],
    "Check probe authority",
  );
  const jobId = safeId(authority.job_id, "Check authority job ID");
  if (jobId !== expectedJobId) {
    throw new Error("Check probe authority does not match its job.");
  }
  safeId(authority.parent_trial_id, "Check parent trial ID");
  for (const field of [
    "request_sha256",
    "intent_sha256",
    "result_manifest_sha256",
    "report_sha256",
    "runtime_environment_sha256",
    "execution_bundle_sha256",
    "frozen_profiles_sha256",
  ] as const) {
    sha256(authority[field], `Check authority ${field}`);
  }
  return {
    jobId,
    reportSha256: sha256(
      authority.report_sha256,
      "Check authority report SHA-256",
    ),
    lockedProfile: parseProfile(
      authority.locked_profile,
      "Check locked profile",
    ),
    controlProfile: parseProfile(
      authority.control_profile,
      "Check control profile",
    ),
  };
}

function parseDevelopmentPackageBinding(value: unknown) {
  if (value === null) return null;
  const binding = record(value, "Development package binding");
  exactKeys(
    binding,
    ["session_id", "package_sha256", "attempt_family_sha256"],
    "Development package binding",
  );
  return {
    sessionId: safeId(binding.session_id, "Development package session ID"),
    packageSha256: sha256(
      binding.package_sha256,
      "Development package SHA-256",
    ),
    attemptFamilySha256: sha256(
      binding.attempt_family_sha256,
      "Development package attempt family SHA-256",
    ),
  };
}

function parsePropagationSuggestion(
  value: unknown,
  context: {
    frameIndex: number;
    temporalGroupId: string;
    sourceFrameSha256: string;
    sourceSha256: string;
    width: number;
    height: number;
    propagationJobIds: string[];
  },
) {
  const suggestion = record(value, "Propagation suggestion");
  exactKeys(
    suggestion,
    [
      "suggestion_id",
      "frame_index",
      "temporal_group_id",
      "temporal_group",
      "point_source_px",
      "bbox_source_px",
      "presence",
      "visibility",
      "training_use",
      "annotation_state",
      "provenance",
      "source_frame_sha256",
      "self_check",
      "suggestion_job_id",
      "suggestion_sha256",
      "pending_human_confirmation",
      "human_confirmation",
      "human_decision",
    ],
    "Propagation suggestion",
  );
  const suggestionId = safeId(suggestion.suggestion_id, "Suggestion ID");
  const suggestionJobId = safeId(
    suggestion.suggestion_job_id,
    "Suggestion job ID",
  );
  const suggestionSha256 = sha256(
    suggestion.suggestion_sha256,
    "Suggestion SHA-256",
  );
  if (
    suggestion.frame_index !== context.frameIndex ||
    suggestion.temporal_group_id !== context.temporalGroupId ||
    suggestion.source_frame_sha256 !== context.sourceFrameSha256 ||
    suggestion.presence !== "present" ||
    !["visible", "partial"].includes(suggestion.visibility as string) ||
    suggestion.training_use !== "excluded" ||
    suggestion.annotation_state !== "suggested" ||
    suggestion.provenance !== "tiny_ball_bounded_template_flow_v1"
  ) {
    throw new Error(
      "Propagation suggestion truth or source binding is invalid.",
    );
  }
  const group = record(suggestion.temporal_group, "Inherited temporal group");
  exactKeys(
    group,
    [
      "group_id",
      "profile_id",
      "source_sha256",
      "seed_frame_index",
      "start_frame",
      "end_frame",
      "derivative_family",
      "canonical_moment_id",
      "derivative_family_id",
      "ancestry_profile",
      "derivative",
      "derivative_binding_sha256",
    ],
    "Inherited temporal group",
  );
  const derivative = record(group.derivative, "Propagation derivative");
  exactKeys(
    derivative,
    ["artifact_type", "artifact_id", "inheritance_rule"],
    "Propagation derivative",
  );
  const groupId = sha256(group.group_id, "Inherited group ID");
  safeId(derivative.artifact_id, "Propagation derivative artifact ID");
  const startFrame = integer(group.start_frame, "Inherited group start");
  const endFrame = integer(group.end_frame, "Inherited group end");
  if (
    groupId !== context.temporalGroupId ||
    group.profile_id !== "tiny_ball_temporal_groups_v1" ||
    group.source_sha256 !== context.sourceSha256 ||
    group.derivative_family_id !== groupId ||
    group.ancestry_profile !==
      "source-proxy-crop-tile-propagation-closure-v1" ||
    startFrame > context.frameIndex ||
    endFrame < context.frameIndex ||
    !Array.isArray(group.derivative_family) ||
    group.derivative_family.length !== 2 ||
    group.derivative_family[0] !== startFrame ||
    group.derivative_family[1] !== endFrame ||
    derivative.artifact_type !== "propagation" ||
    !context.propagationJobIds.includes(suggestionJobId) ||
    derivative.inheritance_rule !== "inherit-source-group-without-regrouping-v1"
  ) {
    throw new Error("Propagation temporal lineage is invalid.");
  }
  integer(group.seed_frame_index, "Propagation seed frame");
  sha256(group.canonical_moment_id, "Canonical moment ID");
  sha256(group.derivative_binding_sha256, "Derivative binding SHA-256");
  let point = parsePoint(suggestion.point_source_px, "Suggestion point");
  const bbox =
    suggestion.bbox_source_px === null
      ? null
      : parseBox(suggestion.bbox_source_px, "Suggestion box");
  if (
    (point && (point[0] >= context.width || point[1] >= context.height)) ||
    (bbox && (bbox.right > context.width || bbox.bottom > context.height)) ||
    (!point && !bbox)
  ) {
    throw new Error("Propagation suggestion geometry is invalid.");
  }
  if (bbox) {
    const center: [number, number] = [
      (bbox.left + bbox.right) / 2,
      (bbox.top + bbox.bottom) / 2,
    ];
    if (
      point &&
      (Math.abs(point[0] - center[0]) > 0.5 ||
        Math.abs(point[1] - center[1]) > 0.5)
    ) {
      throw new Error("Propagation suggestion point and box disagree.");
    }
    point = center;
  }
  const selfCheck = record(suggestion.self_check, "Propagation self-check");
  exactKeys(
    selfCheck,
    [
      "match_score",
      "backward_match_score",
      "forward_backward_error_px",
      "step_displacement_px",
    ],
    "Propagation self-check",
  );
  const matchScore = finiteNumber(
    selfCheck.match_score,
    "Propagation match score",
    -1,
  );
  const backwardMatchScore = finiteNumber(
    selfCheck.backward_match_score,
    "Propagation backward score",
    -1,
  );
  if (matchScore > 1 || backwardMatchScore > 1) {
    throw new Error("Propagation self-check score is invalid.");
  }
  const pending = suggestion.pending_human_confirmation;
  if (typeof pending !== "boolean") {
    throw new Error("Propagation confirmation state is invalid.");
  }
  let confirmationRevision: number | null = null;
  let decision: "pending" | "confirmed" | "dismissed" = "pending";
  if (pending) {
    if (
      suggestion.human_confirmation !== null ||
      suggestion.human_decision !== null
    ) {
      throw new Error("Pending propagation suggestion claims human truth.");
    }
  } else if (suggestion.human_confirmation !== null) {
    if (suggestion.human_decision !== null) {
      throw new Error("Propagation suggestion has conflicting decisions.");
    }
    const confirmation = record(
      suggestion.human_confirmation,
      "Propagation human confirmation",
    );
    exactKeys(
      confirmation,
      [
        "revision_id",
        "revision",
        "operator_id",
        "center_error_px",
        "iou",
        "corrected",
        "confirmed_at",
      ],
      "Propagation human confirmation",
    );
    safeId(confirmation.revision_id, "Confirmation revision ID");
    confirmationRevision = integer(
      confirmation.revision,
      "Confirmation revision",
      1,
    );
    safeId(confirmation.operator_id, "Confirmation operator ID");
    finiteNumber(confirmation.center_error_px, "Confirmation center error");
    if (
      confirmation.iou !== null &&
      finiteNumber(confirmation.iou, "Confirmation IoU") > 1
    ) {
      throw new Error("Confirmation IoU is invalid.");
    }
    if (typeof confirmation.corrected !== "boolean") {
      throw new Error("Confirmation correction flag is invalid.");
    }
    stringValue(confirmation.confirmed_at, "Confirmation time");
    decision = "confirmed";
  } else {
    const humanDecision = record(
      suggestion.human_decision,
      "Propagation human decision",
    );
    exactKeys(
      humanDecision,
      ["decision", "revision_id", "revision", "operator_id", "decided_at"],
      "Propagation human decision",
    );
    if (humanDecision.decision !== "dismissed_manual_annotation") {
      throw new Error("Propagation human decision is invalid.");
    }
    safeId(humanDecision.revision_id, "Decision revision ID");
    confirmationRevision = integer(
      humanDecision.revision,
      "Decision revision",
      1,
    );
    safeId(humanDecision.operator_id, "Decision operator ID");
    stringValue(humanDecision.decided_at, "Decision time");
    decision = "dismissed";
  }
  return {
    suggestionId,
    jobId: suggestionJobId,
    suggestionSha256,
    frameIndex: context.frameIndex,
    temporalGroupId: context.temporalGroupId,
    point,
    bbox,
    annotationState: "suggested" as const,
    trainingUse: "excluded" as const,
    provenance: "tiny_ball_bounded_template_flow_v1",
    selfCheck: {
      matchScore,
      backwardMatchScore,
      forwardBackwardErrorPx: finiteNumber(
        selfCheck.forward_backward_error_px,
        "Forward-backward error",
      ),
      stepDisplacementPx: finiteNumber(
        selfCheck.step_displacement_px,
        "Step displacement",
      ),
    },
    pendingHumanConfirmation: pending,
    confirmationRevision,
    decision,
  };
}

export function parseBallPropagationJob(
  value: unknown,
  session: ParsedBallAnnotationSession,
  expected?: {
    jobId?: string;
    mutationId?: string;
    seedFrameIndex?: number;
    expectedSeedRevision?: number;
    radiusFrames?: number;
  },
): ParsedBallPropagationJob {
  const job = record(value, "Propagation job");
  exactKeys(
    job,
    [
      "schema_version",
      "artifact_type",
      "job_id",
      "session_id",
      "intent_sha256",
      "mutation_id",
      "seed_frame_index",
      "expected_seed_revision",
      "radius_frames",
      "seed_binding",
      "target_frame_indices",
      "tracker_profile",
      "neighbor_probe_job_id",
      "status",
      "stage",
      "frame_results",
      "summary",
      "suggestions",
      "error_code",
      "neighbor_probe_cancel_status",
      "neighbor_probe_cancel_error_code",
      "created_at",
      "updated_at",
      "status_url",
      "cancel_url",
    ],
    "Propagation job",
  );
  if (
    job.schema_version !== "1.0" ||
    job.artifact_type !== "ball_propagation_job"
  ) {
    throw new Error("Propagation job identity is invalid.");
  }
  const jobId = safeId(job.job_id, "Propagation job ID");
  const sessionId = safeId(job.session_id, "Propagation session ID");
  const mutationId = safeId(job.mutation_id, "Propagation mutation ID");
  const seedFrameIndex = integer(
    job.seed_frame_index,
    "Propagation seed frame",
  );
  const expectedSeedRevision = integer(
    job.expected_seed_revision,
    "Propagation seed revision",
    1,
  );
  const radiusFrames = integer(job.radius_frames, "Propagation radius", 1);
  const intentSha256 = sha256(job.intent_sha256, "Propagation intent SHA-256");
  if (
    radiusFrames > 2 ||
    sessionId !== session.view.sessionId ||
    (expected?.jobId !== undefined && expected.jobId !== jobId) ||
    (expected?.mutationId !== undefined &&
      expected.mutationId !== mutationId) ||
    (expected?.seedFrameIndex !== undefined &&
      expected.seedFrameIndex !== seedFrameIndex) ||
    (expected?.expectedSeedRevision !== undefined &&
      expected.expectedSeedRevision !== expectedSeedRevision) ||
    (expected?.radiusFrames !== undefined &&
      expected.radiusFrames !== radiusFrames)
  ) {
    throw new Error("Propagation job does not match its request authority.");
  }
  const seedBinding = record(job.seed_binding, "Propagation seed binding");
  exactKeys(
    seedBinding,
    [
      "frame_index",
      "annotation_revision",
      "annotation_etag",
      "annotation_sha256",
      "source_frame_sha256",
      "temporal_group_id",
      "sampling_manifest_sha256",
      "tracker_profile_sha256",
    ],
    "Propagation seed binding",
  );
  const seedFrame = session.view.frames.find(
    (frame) => frame.frameIndex === seedFrameIndex,
  );
  if (
    !seedFrame ||
    seedBinding.frame_index !== seedFrameIndex ||
    seedBinding.annotation_revision !== expectedSeedRevision ||
    seedBinding.source_frame_sha256 !== seedFrame.sourceFrameSha256 ||
    seedBinding.temporal_group_id !== seedFrame.temporalGroupId ||
    seedBinding.sampling_manifest_sha256 !== session.samplingManifestSha256
  ) {
    throw new Error("Propagation seed binding is invalid.");
  }
  for (const field of [
    "annotation_etag",
    "annotation_sha256",
    "tracker_profile_sha256",
  ] as const) {
    sha256(seedBinding[field], `Propagation seed ${field}`);
  }
  const tracker = record(job.tracker_profile, "Propagation tracker profile");
  exactKeys(
    tracker,
    [
      "profile_id",
      "version",
      "radius_frames_max",
      "search_radius_source_px",
      "minimum_match_score",
      "minimum_backward_match_score",
      "maximum_forward_backward_error_px",
      "profile_sha256",
    ],
    "Propagation tracker profile",
  );
  if (
    tracker.profile_id !== "tiny_ball_bounded_template_flow_v1" ||
    tracker.version !== "1.0" ||
    tracker.radius_frames_max !== 2 ||
    tracker.search_radius_source_px !== 24 ||
    tracker.profile_sha256 !== seedBinding.tracker_profile_sha256
  ) {
    throw new Error("Propagation tracker authority is invalid.");
  }
  const targetFrameIndices = Array.isArray(job.target_frame_indices)
    ? job.target_frame_indices.map((frameIndex) =>
        integer(frameIndex, "Propagation target frame"),
      )
    : [];
  if (
    targetFrameIndices.length < 1 ||
    targetFrameIndices.length > 4 ||
    new Set(targetFrameIndices).size !== targetFrameIndices.length ||
    targetFrameIndices.some(
      (frameIndex) =>
        frameIndex === seedFrameIndex ||
        Math.abs(frameIndex - seedFrameIndex) > radiusFrames,
    )
  ) {
    throw new Error("Propagation target frames are invalid.");
  }
  const status = oneOf(
    job.status,
    [
      "queued",
      "waiting_probe",
      "committing",
      "ready",
      "failed",
      "blocked",
      "cancelled",
    ] as const,
    "Propagation status",
  );
  if (
    job.status_url !==
      `/api/v1/ball-annotation-sessions/${sessionId}/propagation-jobs/${jobId}` ||
    job.cancel_url !==
      `/api/v1/ball-annotation-sessions/${sessionId}/propagation-jobs/${jobId}/cancel` ||
    !Array.isArray(job.frame_results) ||
    job.frame_results.length > 4 ||
    !Array.isArray(job.suggestions) ||
    job.suggestions.length > 4
  ) {
    throw new Error("Propagation lifecycle authority is invalid.");
  }
  const suggestions = job.suggestions.map((suggestion) => {
    const rawSuggestion = record(suggestion, "Propagation job suggestion");
    const frameIndex = integer(
      rawSuggestion.frame_index,
      "Propagation suggestion frame",
    );
    return parsePropagationSuggestion(suggestion, {
      frameIndex,
      temporalGroupId: sha256(
        rawSuggestion.temporal_group_id,
        "Propagation suggestion temporal group",
      ),
      sourceFrameSha256: sha256(
        rawSuggestion.source_frame_sha256,
        "Propagation suggestion source frame",
      ),
      sourceSha256: session.view.source.sourceSha256,
      width: session.view.source.width,
      height: session.view.source.height,
      propagationJobIds: [jobId],
    });
  });
  if (
    suggestions.some(
      (suggestion) => !targetFrameIndices.includes(suggestion.frameIndex),
    )
  ) {
    throw new Error("Propagation suggestions are outside target authority.");
  }
  let pendingCount = suggestions.filter(
    (suggestion) => suggestion.pendingHumanConfirmation,
  ).length;
  if (job.summary !== null) {
    const summary = record(job.summary, "Propagation summary");
    if (
      typeof summary.pending_human_confirmation !== "boolean" ||
      integer(
        summary.pending_human_confirmation_count,
        "Pending propagation count",
      ) !== pendingCount
    ) {
      throw new Error("Propagation summary is inconsistent.");
    }
  } else if (status === "ready") {
    throw new Error("Ready propagation job is missing its summary.");
  } else {
    pendingCount = 0;
  }
  const errorCode =
    job.error_code === null
      ? null
      : stringValue(job.error_code, "Propagation error code");
  if (
    (["failed", "blocked"].includes(status) && errorCode === null) ||
    (!["failed", "blocked"].includes(status) && errorCode !== null)
  ) {
    throw new Error("Propagation error lifecycle is invalid.");
  }
  return {
    sessionId,
    mutationId,
    seedFrameIndex,
    expectedSeedRevision,
    radiusFrames,
    intentSha256,
    view: {
      jobId,
      status,
      stage: stringValue(job.stage, "Propagation stage"),
      pendingCount,
      targetFrameIndices,
      errorCode,
    },
  };
}

/** Parse server authority into the deliberately smaller annotation UI view. */
export function parseBallAnnotationSession(
  value: unknown,
  expected?: {
    dataRole?: "development" | "check";
    developmentProbeJobIds?: readonly string[];
    lockedProfileId?: string;
  },
): ParsedBallAnnotationSession {
  const session = record(value, "Ball annotation session");
  exactKeys(
    session,
    Object.prototype.hasOwnProperty.call(session, "review_proxy_repair")
      ? SESSION_RESPONSE_KEYS
      : SESSION_RESPONSE_KEYS.filter((key) => key !== "review_proxy_repair"),
    "Ball annotation session",
  );
  if (
    session.schema_version !== "1.0" ||
    session.artifact_type !== "ball_annotation_session"
  ) {
    throw new Error("Ball annotation session identity is invalid.");
  }
  const sessionId = safeId(session.session_id, "Annotation session ID");
  const dataRole = oneOf(
    session.data_role,
    ["development", "check"] as const,
    "Annotation data role",
  );
  const status = oneOf(
    session.status,
    [
      "sampling_locked",
      "check_probe_queued",
      "check_probe_running",
      "check_probe_committing",
      "annotating",
      "finalizing",
      "blocked",
      "finalized",
    ] as const,
    "Annotation session status",
  );
  const source = record(session.source, "Annotation source");
  exactKeys(
    source,
    [
      "source_id",
      "sha256",
      "file_identity_sha256",
      "size_bytes",
      "width",
      "height",
      "frame_count",
      "tracking_contract_sha256",
      "relative_path",
      "tracking_contract_relative_path",
      "fps",
    ],
    "Annotation source",
  );
  const sourceWidth = integer(source.width, "Source width", 1);
  const sourceHeight = integer(source.height, "Source height", 1);
  integer(source.size_bytes, "Source size", 1);
  integer(source.frame_count, "Source frame count", 1);
  const sourceFps = finiteNumber(source.fps, "Source FPS", Number.MIN_VALUE);
  sha256(source.file_identity_sha256, "Source identity SHA-256");
  sha256(source.tracking_contract_sha256, "Tracking contract SHA-256");
  stringValue(source.relative_path, "Source relative path");
  stringValue(
    source.tracking_contract_relative_path,
    "Tracking contract relative path",
  );
  const lockedProfile = parseProfile(session.locked_profile, "Locked profile");
  const controlProfile =
    session.control_profile === null
      ? null
      : parseProfile(session.control_profile, "Control profile");
  const controlProfileId = nullableSafeId(
    session.control_profile_id,
    "Control profile ID",
  );
  if (
    (controlProfile === null) !== (controlProfileId === null) ||
    (controlProfile && controlProfile.profileId !== controlProfileId) ||
    controlProfileId === null ||
    controlProfileId === lockedProfile.profileId
  ) {
    throw new Error("Control profile binding is invalid.");
  }
  const {
    jobIds: developmentProbeJobIds,
    decode,
    digestMaps: developmentDigestMaps,
    runtimeEnvironmentSha256,
  } = parseLineage(session.lineage);
  if (
    decode.width !== sourceWidth ||
    decode.height !== sourceHeight ||
    decode.frame_count !== source.frame_count ||
    decode.fps !== source.fps
  ) {
    throw new Error("Decode authority does not match the source binding.");
  }
  const manifest = record(session.sampling_manifest, "Sampling manifest");
  exactKeys(manifest, MANIFEST_KEYS, "Sampling manifest");
  const manifestSha256 = sha256(
    manifest.manifest_sha256,
    "Sampling manifest SHA-256",
  );
  if (
    manifest.schema_version !== "1.0" ||
    manifest.artifact_type !== "ball_annotation_sampling_manifest" ||
    manifest.profile_id !== "tiny_ball_temporal_groups_v1" ||
    manifest.metric_profile_id !== "tiny_ball_feasibility_metric_v1" ||
    manifest.metric_profile_sha256 !== FEASIBILITY_METRIC_PROFILE_SHA256 ||
    manifest.data_role !== dataRole ||
    manifest.source_sha256 !== source.sha256 ||
    manifest.locked_profile_id !== lockedProfile.profileId ||
    manifest.locked_profile_sha256 !== lockedProfile.profileSha256
  ) {
    throw new Error("Sampling manifest authority is invalid.");
  }
  const selectionProfileId = oneOf(
    manifest.selection_profile_id,
    [
      "development_probe_frames_v1",
      "tiny_ball_temporal_block_hash_v1",
    ] as const,
    "Sampling selection profile",
  );
  if (
    manifest.scale_stratification_mode !== "post_reveal_support_gate_only" ||
    manifest.lighting_stratification_mode !==
      (dataRole === "check"
        ? "predeclared_frame_intervals_and_quota_v1"
        : "not_applicable_development_evidence") ||
    selectionProfileId !==
      (dataRole === "check"
        ? "tiny_ball_temporal_block_hash_v1"
        : "development_probe_frames_v1") ||
    (dataRole === "development") !== (manifest.selection_authority === null) ||
    (dataRole === "development") !==
      (manifest.candidate_universe_authority === null)
  ) {
    throw new Error("Sampling selection authority is invalid.");
  }
  sha256(manifest.selection_seed_sha256, "Sampling selection seed");
  sha256(manifest.candidate_universe_sha256, "Candidate universe SHA-256");
  const universeStart = integer(
    manifest.candidate_universe_start_frame,
    "Candidate universe start",
  );
  const universeEnd = integer(
    manifest.candidate_universe_end_frame,
    "Candidate universe end",
  );
  if (
    universeEnd < universeStart ||
    universeEnd >= (source.frame_count as number)
  ) {
    throw new Error("Candidate universe bounds are invalid.");
  }
  if (dataRole === "check") {
    const selection = record(
      manifest.selection_authority,
      "Sampling selection authority",
    );
    exactKeys(
      selection,
      [
        "schema_version",
        "artifact_type",
        "attempt_family_sha256",
        "development_package_sha256",
        "source_sha256",
        "locked_profile_id",
        "locked_profile_sha256",
        "sampling_profile_id",
        "metric_profile_id",
        "metric_profile_sha256",
        "target_frame_count",
        "scale_applicability",
        "lighting_applicability",
      ],
      "Sampling selection authority",
    );
    const universe = record(
      manifest.candidate_universe_authority,
      "Candidate universe authority",
    );
    exactKeys(
      universe,
      [
        "schema_version",
        "artifact_type",
        "source_sha256",
        "start_frame",
        "end_frame",
        "candidate_frame_count",
        "grouping_profile_id",
        "selection_profile_id",
        "lighting_strata",
        "excluded_temporal_groups",
      ],
      "Candidate universe authority",
    );
  }
  const targetFrameCount = integer(
    manifest.target_frame_count,
    "Sampling target frame count",
    1,
  );
  if (
    (dataRole === "check" &&
      (targetFrameCount < 20 || targetFrameCount > 50)) ||
    typeof manifest.locked_before_probe !== "boolean" ||
    manifest.locked_before_probe !== (dataRole === "check")
  ) {
    throw new Error("Sampling role boundary is invalid.");
  }
  sha256(manifest.metric_profile_sha256, "Manifest metric profile SHA-256");
  const manifestApplicability = parseApplicability(
    manifest.strata_applicability,
  );
  if (
    manifestApplicability.lightingQuota !==
    (dataRole === "check" ? targetFrameCount : 0)
  ) {
    throw new Error("Sampling lighting quota does not match its target.");
  }
  if (
    !Array.isArray(manifest.frame_indices) ||
    !Array.isArray(manifest.groups)
  ) {
    throw new Error("Sampling frame authority is invalid.");
  }
  const manifestFrameIndices = manifest.frame_indices.map((frameIndex) =>
    integer(frameIndex, "Sampling frame index"),
  );
  if (
    manifestFrameIndices.length !== targetFrameCount ||
    manifestFrameIndices.some(
      (frameIndex, index) =>
        index > 0 && frameIndex <= manifestFrameIndices[index - 1],
    ) ||
    manifest.groups.length !== targetFrameCount ||
    !Array.isArray(manifest.excluded_development_groups)
  ) {
    throw new Error("Sampling frame authority is invalid.");
  }
  const manifestGroups = manifest.groups.map((group, index) =>
    parseTemporalGroup(
      group,
      source.sha256 as string,
      `Sampling group ${index}`,
    ),
  );
  if (
    manifestGroups.some(
      (group, index) => group.frameIndex !== manifestFrameIndices[index],
    )
  ) {
    throw new Error("Sampling groups do not match their frame set.");
  }
  const excludedDevelopmentGroups = manifest.excluded_development_groups.map(
    (group, index) =>
      parseTemporalGroup(
        group,
        source.sha256 as string,
        `Excluded development group ${index}`,
      ),
  );
  if (dataRole === "development" && excludedDevelopmentGroups.length !== 0) {
    throw new Error("Development sampling cannot exclude itself.");
  }
  if (dataRole === "check") {
    const selection = manifest.selection_authority as Record<string, unknown>;
    const universe = manifest.candidate_universe_authority as Record<
      string,
      unknown
    >;
    const selectionScale = boundedCollection(
      selection.scale_applicability,
      "Sampling selection scale authority",
      SCALE_STRATA.length,
      SCALE_STRATA.length,
    );
    const selectionLighting = boundedCollection(
      selection.lighting_applicability,
      "Sampling selection lighting authority",
      LIGHTING_STRATA.length,
      LIGHTING_STRATA.length,
    );
    selectionScale.forEach((rawRow, index) => {
      const row = record(rawRow, `Sampling selection scale ${index}`);
      exactKeys(
        row,
        ["stratum", "status"],
        `Sampling selection scale ${index}`,
      );
      if (row.stratum !== SCALE_STRATA[index]) {
        throw new Error("Sampling selection scale authority is not canonical.");
      }
    });
    selectionLighting.forEach((rawRow, index) => {
      const row = record(rawRow, `Sampling selection lighting ${index}`);
      exactKeys(
        row,
        ["stratum", "status", "quota", "frame_intervals"],
        `Sampling selection lighting ${index}`,
      );
      if (row.stratum !== LIGHTING_STRATA[index]) {
        throw new Error(
          "Sampling selection lighting authority is not canonical.",
        );
      }
    });
    const applicability = record(
      manifest.strata_applicability,
      "Sampling manifest applicability",
    );
    const expectedSelectionScale = (applicability.scale as unknown[]).map(
      (rawRow) => {
        const row = record(rawRow, "Sampling scale authority");
        return { stratum: row.stratum, status: row.status };
      },
    );
    const expectedSelectionLighting = (applicability.lighting as unknown[]).map(
      (rawRow) => {
        const row = record(rawRow, "Sampling lighting authority");
        return {
          stratum: row.stratum,
          status: row.status,
          quota: row.quota,
          frame_intervals: row.frame_intervals,
        };
      },
    );
    if (
      selection.schema_version !== "1.0" ||
      selection.artifact_type !==
        "ball_annotation_sampling_selection_authority" ||
      selection.source_sha256 !== source.sha256 ||
      selection.locked_profile_id !== lockedProfile.profileId ||
      selection.locked_profile_sha256 !== lockedProfile.profileSha256 ||
      selection.sampling_profile_id !== manifest.profile_id ||
      selection.metric_profile_id !== manifest.metric_profile_id ||
      selection.metric_profile_sha256 !== manifest.metric_profile_sha256 ||
      selection.target_frame_count !== targetFrameCount ||
      !sameCanonicalValue(selectionScale, expectedSelectionScale) ||
      !sameCanonicalValue(selectionLighting, expectedSelectionLighting) ||
      authorityCanonicalSha256(selection, "Sampling selection authority") !==
        manifest.selection_seed_sha256
    ) {
      throw new Error(
        "Sampling selection authority does not bind the manifest.",
      );
    }
    if (!Array.isArray(universe.lighting_strata)) {
      throw new Error("Candidate universe lighting authority is invalid.");
    }
    universe.lighting_strata.forEach((rawRow, index) => {
      const row = record(rawRow, `Candidate universe lighting ${index}`);
      exactKeys(
        row,
        ["stratum", "quota", "frame_intervals"],
        `Candidate universe lighting ${index}`,
      );
    });
    if (!Array.isArray(universe.excluded_temporal_groups)) {
      throw new Error("Candidate universe exclusions are invalid.");
    }
    universe.excluded_temporal_groups.forEach((rawRow, index) => {
      const row = record(rawRow, `Candidate universe exclusion ${index}`);
      exactKeys(
        row,
        [
          "group_id",
          "profile_id",
          "source_sha256",
          "seed_frame_index",
          "start_frame",
          "end_frame",
          "derivative_family",
          "canonical_moment_id",
          "derivative_family_id",
          "ancestry_profile",
        ],
        `Candidate universe exclusion ${index}`,
      );
    });
    const expectedUniverseLighting = (applicability.lighting as unknown[])
      .map((rawRow) => record(rawRow, "Sampling lighting authority"))
      .filter((row) => row.quota !== 0)
      .map(({ stratum, quota, frame_intervals }) => ({
        stratum,
        quota,
        frame_intervals,
      }));
    if (
      universe.schema_version !== "1.0" ||
      universe.artifact_type !== "ball_annotation_candidate_universe" ||
      universe.source_sha256 !== source.sha256 ||
      universe.start_frame !== universeStart ||
      universe.end_frame !== universeEnd ||
      universe.candidate_frame_count !== universeEnd - universeStart + 1 ||
      universe.grouping_profile_id !== "tiny_ball_temporal_groups_v1" ||
      universe.selection_profile_id !== "tiny_ball_temporal_block_hash_v1" ||
      !sameCanonicalValue(universe.lighting_strata, expectedUniverseLighting) ||
      authorityCanonicalSha256(universe, "Candidate universe authority") !==
        manifest.candidate_universe_sha256
    ) {
      throw new Error(
        "Candidate universe authority does not bind the manifest.",
      );
    }
  }
  const canonicalManifest = structuredClone(manifest);
  delete canonicalManifest.manifest_sha256;
  if (canonicalManifest.selection_authority === null) {
    delete canonicalManifest.selection_authority;
  }
  if (canonicalManifest.candidate_universe_authority === null) {
    delete canonicalManifest.candidate_universe_authority;
  }
  for (const field of ["groups", "excluded_development_groups"] as const) {
    canonicalManifest[field] = (canonicalManifest[field] as unknown[]).map(
      (rawGroup) => {
        const group = { ...record(rawGroup, "Sampling temporal group") };
        if (group.pre_reveal_lighting_stratum === null) {
          delete group.pre_reveal_lighting_stratum;
        }
        return group;
      },
    );
  }
  if (
    authorityCanonicalSha256(canonicalManifest, "Sampling manifest") !==
    manifestSha256
  ) {
    throw new Error("Sampling manifest digest does not match its contents.");
  }
  sha256(session.idempotency_key, "Session idempotency key");
  const requestSha256 = sha256(
    session.request_sha256,
    "Session request SHA-256",
  );
  const metricProfileSha256 = sha256(
    session.metric_profile_sha256,
    "Metric profile SHA-256",
  );
  const operatorId = safeId(session.operator_id, "Operator ID");
  stringValue(session.stage, "Annotation stage");
  stringValue(session.created_at, "Annotation created time");
  stringValue(session.updated_at, "Annotation updated time");
  if (
    session.sampling_profile_id !== "tiny_ball_temporal_groups_v1" ||
    session.metric_profile_id !== "tiny_ball_feasibility_metric_v1" ||
    metricProfileSha256 !== manifest.metric_profile_sha256
  ) {
    throw new Error("Annotation metric or sampling profile is invalid.");
  }
  const applicableScales = Array.isArray(session.applicable_scale_strata)
    ? session.applicable_scale_strata.map((item) =>
        oneOf(item, SCALE_STRATA, "Applicable scale stratum"),
      )
    : [];
  const applicableLighting = Array.isArray(session.applicable_lighting_strata)
    ? session.applicable_lighting_strata.map((item) =>
        oneOf(item, LIGHTING_STRATA, "Applicable lighting stratum"),
      )
    : [];
  if (
    applicableScales.length < 1 ||
    applicableLighting.length < 1 ||
    new Set(applicableScales).size !== applicableScales.length ||
    new Set(applicableLighting).size !== applicableLighting.length
  ) {
    throw new Error("Applicable strata are invalid.");
  }
  if (
    applicableScales.length !== manifestApplicability.scale.length ||
    applicableScales.some(
      (stratum, index) => stratum !== manifestApplicability.scale[index],
    ) ||
    applicableLighting.length !== manifestApplicability.lighting.length ||
    applicableLighting.some(
      (stratum, index) => stratum !== manifestApplicability.lighting[index],
    )
  ) {
    throw new Error("Applicable strata do not match their frozen evidence.");
  }
  const retryFromSessionId = nullableSafeId(
    session.retry_from_session_id,
    "Retry session ID",
  );
  const attemptFamilySha256 = sha256(
    session.attempt_family_sha256,
    "Attempt family SHA-256",
  );
  const developmentPackageBinding = parseDevelopmentPackageBinding(
    session.development_package_binding,
  );
  if (
    (dataRole === "development") !== (developmentPackageBinding === null) ||
    (developmentPackageBinding &&
      developmentPackageBinding.attemptFamilySha256 !== attemptFamilySha256)
  ) {
    throw new Error("Development package authority is invalid.");
  }
  if ((retryFromSessionId === null) !== (session.retry_lineage === null)) {
    throw new Error("Retry lineage is incomplete.");
  }
  let retryLineage: {
    mode:
      | "same_authority"
      | "worker_runtime_reexecution"
      | "review_proxy_decode_upgrade";
    previousSessionId: string;
    previousErrorCode: string | null;
    previousBlockerCode: string | null;
    previousLineageSha256: string;
    currentLineageSha256: string;
    samplingManifestSha256: string;
  } | null = null;
  if (session.retry_lineage !== null) {
    const retry = record(session.retry_lineage, "Retry lineage");
    exactKeys(
      retry,
      [
        "mode",
        "previous_session_id",
        "previous_error_code",
        "previous_blocker_code",
        "previous_lineage_sha256",
        "current_lineage_sha256",
        "sampling_manifest_sha256",
      ],
      "Retry lineage",
    );
    const mode = oneOf(
      retry.mode,
      [
        "same_authority",
        "worker_runtime_reexecution",
        "review_proxy_decode_upgrade",
      ] as const,
      "Retry mode",
    );
    const previousSessionId = safeId(
      retry.previous_session_id,
      "Previous retry session ID",
    );
    const samplingManifestSha256 = sha256(
      retry.sampling_manifest_sha256,
      "Retry sampling manifest SHA-256",
    );
    if (
      previousSessionId !== retryFromSessionId ||
      samplingManifestSha256 !== manifestSha256
    ) {
      throw new Error("Retry lineage authority is invalid.");
    }
    retryLineage = {
      mode,
      previousSessionId,
      previousErrorCode:
        retry.previous_error_code === null
          ? null
          : stringValue(retry.previous_error_code, "Previous retry error code"),
      previousBlockerCode:
        retry.previous_blocker_code === null
          ? null
          : stringValue(
              retry.previous_blocker_code,
              "Previous retry blocker code",
            ),
      previousLineageSha256: sha256(
        retry.previous_lineage_sha256,
        "Previous retry lineage SHA-256",
      ),
      currentLineageSha256: sha256(
        retry.current_lineage_sha256,
        "Current retry lineage SHA-256",
      ),
      samplingManifestSha256,
    };
  }
  const checkProbeJobId = nullableSafeId(
    session.check_probe_job_id,
    "Check probe job ID",
  );
  const checkAuthority = parseCheckAuthority(
    session.check_probe_authority,
    checkProbeJobId,
  );
  if (
    dataRole === "development" &&
    retryLineage !== null &&
    retryLineage.mode !== "review_proxy_decode_upgrade"
  ) {
    throw new Error("Development retry is not a review-proxy upgrade.");
  }
  if (
    (dataRole === "development" &&
      (checkProbeJobId !== null || checkAuthority !== null)) ||
    (checkAuthority !== null &&
      (checkAuthority.lockedProfile.profileId !== lockedProfile.profileId ||
        checkAuthority.controlProfile.profileId !== controlProfileId)) ||
    ([
      "check_probe_queued",
      "check_probe_running",
      "check_probe_committing",
    ].includes(status) &&
      checkProbeJobId === null) ||
    (dataRole === "check" &&
      ["annotating", "finalizing", "finalized"].includes(status) &&
      checkAuthority === null)
  ) {
    throw new Error("Check probe lifecycle authority is invalid.");
  }
  const errorCode =
    session.error_code === null
      ? null
      : stringValue(session.error_code, "Session error code");
  const blockerCode =
    session.blocker_code === null
      ? null
      : stringValue(session.blocker_code, "Session blocker code");
  if (
    (status === "blocked" && blockerCode === null) ||
    (status !== "blocked" && blockerCode !== null) ||
    (status !== "blocked" && errorCode !== null)
  ) {
    throw new Error("Session error lifecycle is invalid.");
  }
  const rawRepairCapability = Object.prototype.hasOwnProperty.call(
    session,
    "review_proxy_repair",
  )
    ? session.review_proxy_repair
    : null;
  let reviewProxyRepair: {
    eligible: true;
    action: "generate_verified_review_proxy";
    createUrl: "/api/v1/detector-review-proxy-repairs";
    parentProbeJobId: string;
    parentProbeReportSha256: string;
    parentProbeResultManifestSha256: string;
    parentProbeRecordSha256: string;
    blockedSessionRecordSha256: string;
  } | null = null;
  if (rawRepairCapability !== null) {
    const capability = record(
      rawRepairCapability,
      "Review-proxy repair capability",
    );
    exactKeys(
      capability,
      [
        "eligible",
        "action",
        "create_url",
        "parent_probe_job_id",
        "parent_probe_report_sha256",
        "parent_probe_result_manifest_sha256",
        "parent_probe_record_sha256",
        "blocked_session_record_sha256",
      ],
      "Review-proxy repair capability",
    );
    const parentProbeJobId = safeId(
      capability.parent_probe_job_id,
      "Repair parent probe job ID",
    );
    const parentProbeReportSha256 = sha256(
      capability.parent_probe_report_sha256,
      "Repair parent probe report SHA-256",
    );
    const parentProbeResultManifestSha256 = sha256(
      capability.parent_probe_result_manifest_sha256,
      "Repair parent probe result manifest SHA-256",
    );
    if (
      capability.eligible !== true ||
      capability.action !== "generate_verified_review_proxy" ||
      capability.create_url !== "/api/v1/detector-review-proxy-repairs" ||
      dataRole !== "development" ||
      status !== "blocked" ||
      blockerCode !== "review_proxy_required" ||
      retryFromSessionId !== null ||
      parentProbeJobId !== developmentProbeJobIds.at(-1) ||
      parentProbeReportSha256 !==
        developmentDigestMaps.development_probe_report_sha256s?.[
          parentProbeJobId
        ] ||
      parentProbeResultManifestSha256 !==
        developmentDigestMaps.development_probe_result_manifest_sha256s?.[
          parentProbeJobId
        ]
    ) {
      throw new Error("Review-proxy repair capability authority is invalid.");
    }
    reviewProxyRepair = {
      eligible: true,
      action: "generate_verified_review_proxy",
      createUrl: "/api/v1/detector-review-proxy-repairs",
      parentProbeJobId,
      parentProbeReportSha256,
      parentProbeResultManifestSha256,
      parentProbeRecordSha256: sha256(
        capability.parent_probe_record_sha256,
        "Repair parent probe record SHA-256",
      ),
      blockedSessionRecordSha256: sha256(
        capability.blocked_session_record_sha256,
        "Repair blocked session record SHA-256",
      ),
    };
  }
  const rawFrames = Array.isArray(session.frames) ? session.frames : null;
  if (!rawFrames || rawFrames.length > 70) {
    throw new Error("Annotation frames are invalid.");
  }
  const frames = rawFrames.map((rawFrame, offset) => {
    const frame = record(rawFrame, `Annotation frame ${offset}`);
    exactKeys(
      frame,
      [
        "frame_index",
        "source_frame_sha256",
        "source_frame_size_bytes",
        "suggested_candidates",
        "source_timing_status",
        "decoder_reported_pos_msec",
        "decoder_time_seconds",
        "display_time_seconds",
        "true_presentation_timestamp",
        "proxy_binding",
        "temporal_group_id",
        "frame_url",
        "annotation_revision",
        "annotation_etag",
        "current_annotation",
        "frame_role",
        "primary_sample",
        "propagation_job_ids",
        "propagation_suggestions",
      ],
      `Annotation frame ${offset}`,
    );
    const frameIndex = integer(frame.frame_index, "Frame index");
    if (
      frame.frame_url !==
      `/api/v1/ball-annotation-sessions/${sessionId}/frames/${frameIndex}`
    ) {
      throw new Error("Frame authority URL is invalid.");
    }
    const rawCandidates = Array.isArray(frame.suggested_candidates)
      ? frame.suggested_candidates
      : null;
    if (!rawCandidates || rawCandidates.length > 5) {
      throw new Error("Frame suggestions are invalid.");
    }
    const suggestedCandidates = rawCandidates.map((rawCandidate, index) => {
      const candidate = record(rawCandidate, `Frame candidate ${index}`);
      exactKeys(
        candidate,
        [
          "candidate_id",
          "profile_id",
          "rank",
          "bbox_source_px",
          "confidence",
          "annotation_state",
          "training_use",
          "truth_status",
          "suggestion_job_id",
          "suggestion_sha256",
          "decision",
        ],
        `Frame candidate ${index}`,
      );
      safeId(candidate.candidate_id, "Candidate ID");
      if (
        candidate.annotation_state !== "suggested" ||
        candidate.training_use !== "excluded" ||
        candidate.truth_status !== "unconfirmed_suggestion"
      ) {
        throw new Error("Candidate truth boundary is invalid.");
      }
      const rank = integer(candidate.rank, "Candidate rank", 1);
      const confidence = finiteNumber(candidate.confidence, "Confidence");
      const decision = oneOf(
        candidate.decision,
        ["pending", "accepted", "dismissed"] as const,
        "Candidate decision",
      );
      if (rank > 5 || confidence > 1) {
        throw new Error("Candidate score is invalid.");
      }
      return {
        candidateId: safeId(candidate.candidate_id, "Candidate ID"),
        bbox: parseBox(candidate.bbox_source_px, "Candidate box"),
        confidence,
        profileId: safeId(candidate.profile_id, "Candidate profile ID"),
        rank,
        suggestionJobId: safeId(
          candidate.suggestion_job_id,
          "Candidate suggestion job ID",
        ),
        suggestionSha256: sha256(
          candidate.suggestion_sha256,
          "Candidate suggestion SHA-256",
        ),
        decision,
      };
    });
    if (
      suggestedCandidates.some(
        (candidate) =>
          !(
            dataRole === "check" && checkProbeJobId
              ? [checkProbeJobId]
              : developmentProbeJobIds
          ).includes(candidate.suggestionJobId),
      )
    ) {
      throw new Error(
        "Candidate suggestion job is outside development authority.",
      );
    }
    const currentAnnotation = parseAnnotation(
      frame.current_annotation,
      `Annotation frame ${frameIndex}`,
      { width: sourceWidth, height: sourceHeight, dataRole },
    );
    const sourceFrameSha256 = sha256(
      frame.source_frame_sha256,
      "Source frame SHA-256",
    );
    const annotationEtag = sha256(frame.annotation_etag, "Annotation ETag");
    const temporalGroupId = sha256(
      frame.temporal_group_id,
      "Temporal group ID",
    );
    const frameRole = oneOf(
      frame.frame_role,
      ["primary_sample", "propagation_target"] as const,
      "Frame role",
    );
    if (
      typeof frame.primary_sample !== "boolean" ||
      frame.primary_sample !== (frameRole === "primary_sample")
    ) {
      throw new Error("Frame role and primary-sample flag disagree.");
    }
    if (
      !Array.isArray(frame.propagation_job_ids) ||
      frame.propagation_job_ids.length > 20 ||
      !Array.isArray(frame.propagation_suggestions) ||
      frame.propagation_suggestions.length > 20
    ) {
      throw new Error("Frame propagation authority is invalid.");
    }
    const propagationJobIds = frame.propagation_job_ids.map((id) =>
      safeId(id, "Propagation job ID"),
    );
    if (
      new Set(propagationJobIds).size !== propagationJobIds.length ||
      (frameRole === "propagation_target" && propagationJobIds.length === 0) ||
      (frame.propagation_suggestions.length > 0 &&
        propagationJobIds.length === 0)
    ) {
      throw new Error("Frame propagation lineage is invalid.");
    }
    const propagationSuggestions = frame.propagation_suggestions.map(
      (suggestion) =>
        parsePropagationSuggestion(suggestion, {
          frameIndex,
          temporalGroupId,
          sourceFrameSha256,
          sourceSha256: source.sha256 as string,
          width: sourceWidth,
          height: sourceHeight,
          propagationJobIds,
        }),
    );
    if (
      new Set(propagationSuggestions.map((item) => item.suggestionId)).size !==
      propagationSuggestions.length
    ) {
      throw new Error("Propagation suggestion identities are duplicated.");
    }
    if (
      propagationSuggestions.some(
        (suggestion) =>
          !suggestion.pendingHumanConfirmation &&
          (currentAnnotation?.annotationState !== "confirmed" ||
            suggestion.confirmationRevision !== frame.annotation_revision),
      )
    ) {
      throw new Error("Confirmed propagation truth is not revision-bound.");
    }
    integer(frame.source_frame_size_bytes, "Source frame size", 1);
    const sourceTimingStatus = oneOf(
      frame.source_timing_status,
      ["observed", "not_collected"] as const,
      "Source timing status",
    );
    const decoderReportedPosMsec =
      frame.decoder_reported_pos_msec === null
        ? null
        : finiteNumber(
            frame.decoder_reported_pos_msec,
            "Decoder reported position",
            Number.NEGATIVE_INFINITY,
          );
    const decoderTimeSeconds =
      frame.decoder_time_seconds === null
        ? null
        : finiteNumber(
            frame.decoder_time_seconds,
            "Decoder time",
            Number.NEGATIVE_INFINITY,
          );
    const displayTimeSeconds = finiteNumber(
      frame.display_time_seconds,
      "Display time",
    );
    const truePresentationTimestamp = parseTruePresentationTimestamp(
      frame.true_presentation_timestamp,
      "True presentation timestamp",
    );
    const proxyBinding = parseProxyBinding(frame.proxy_binding, {
      frameIndex,
      sourceFrameSha256,
      sourceTimingStatus,
      decoderReportedPosMsec,
    });
    if (
      (sourceTimingStatus === "observed"
        ? decoderReportedPosMsec === null ||
          decoderTimeSeconds === null ||
          Math.abs(decoderTimeSeconds - decoderReportedPosMsec / 1_000) > 1e-6
        : decoderReportedPosMsec !== null ||
          decoderTimeSeconds !== null ||
          proxyBinding === null) ||
      Math.abs(displayTimeSeconds - frameIndex / sourceFps) > 1e-6
    ) {
      throw new Error("Frame timing evidence is inconsistent.");
    }
    return {
      frameIndex,
      sourceTimingStatus,
      decoderReportedPosMsec,
      decoderTimeSeconds,
      displayTimeSeconds,
      truePresentationTimestamp,
      proxyBinding,
      temporalGroupId,
      sourceFrameSha256,
      annotationRevision: integer(
        frame.annotation_revision,
        "Annotation revision",
      ),
      annotationEtag: `"${annotationEtag}"`,
      suggestedCandidates,
      currentAnnotation,
      frameRole,
      primarySample: frame.primary_sample,
      propagationSuggestions,
    };
  });
  if (
    frames.some(
      (frame, index) =>
        index > 0 && frame.frameIndex <= frames[index - 1].frameIndex,
    )
  ) {
    throw new Error("Annotation frames are not strictly ordered.");
  }
  const primaryFrames = frames.filter((frame) => frame.primarySample);
  const supplementalFrames = frames.filter((frame) => !frame.primarySample);
  if (
    primaryFrames.some(
      (frame, index) =>
        frame.temporalGroupId !== manifestGroups[index]?.groupId,
    )
  ) {
    throw new Error(
      "Frame temporal groups do not match the sampling manifest.",
    );
  }
  if (
    ["annotating", "finalizing", "finalized"].includes(status) &&
    (manifestFrameIndices.length !== primaryFrames.length ||
      manifestFrameIndices.some(
        (frameIndex, index) => frameIndex !== primaryFrames[index].frameIndex,
      ))
  ) {
    throw new Error("Session frames do not match the frozen manifest.");
  }
  const progress = record(session.progress, "Annotation progress");
  exactKeys(
    progress,
    [
      "annotated_frames",
      "total_frames",
      "unconfirmed_suggestions",
      "primary_annotated_frames",
      "primary_total_frames",
      "supplemental_annotated_frames",
      "supplemental_total_frames",
      "unconfirmed_propagation_suggestions",
    ],
    "Annotation progress",
  );
  const annotatedFrames = integer(
    progress.annotated_frames,
    "Annotated frames",
  );
  const totalFrames = integer(progress.total_frames, "Total frames");
  const unconfirmedSuggestions = integer(
    progress.unconfirmed_suggestions,
    "Unconfirmed suggestions",
  );
  const primaryAnnotatedFrames = integer(
    progress.primary_annotated_frames,
    "Primary annotated frames",
  );
  const primaryTotalFrames = integer(
    progress.primary_total_frames,
    "Primary total frames",
  );
  const supplementalAnnotatedFrames = integer(
    progress.supplemental_annotated_frames,
    "Supplemental annotated frames",
  );
  const supplementalTotalFrames = integer(
    progress.supplemental_total_frames,
    "Supplemental total frames",
  );
  const unconfirmedPropagationSuggestions = integer(
    progress.unconfirmed_propagation_suggestions,
    "Unconfirmed propagation suggestions",
  );
  if (
    totalFrames !== frames.length ||
    annotatedFrames !==
      frames.filter((frame) => frame.currentAnnotation).length ||
    unconfirmedSuggestions !==
      frames.reduce(
        (total, frame) =>
          total +
          frame.suggestedCandidates.filter(
            (candidate) => candidate.decision === "pending",
          ).length,
        0,
      ) ||
    primaryTotalFrames !== primaryFrames.length ||
    primaryAnnotatedFrames !==
      primaryFrames.filter((frame) => frame.currentAnnotation).length ||
    supplementalTotalFrames !== supplementalFrames.length ||
    supplementalAnnotatedFrames !==
      supplementalFrames.filter((frame) => frame.currentAnnotation).length ||
    unconfirmedPropagationSuggestions !==
      frames.reduce(
        (total, frame) =>
          total +
          frame.propagationSuggestions.filter(
            (suggestion) => suggestion.pendingHumanConfirmation,
          ).length,
        0,
      )
  ) {
    throw new Error("Annotation progress does not match the frozen frames.");
  }
  const blockedBeforeReveal =
    status === "blocked" &&
    blockerCode === "review_proxy_required" &&
    frames.length === 0;
  if (
    dataRole === "development" &&
    !blockedBeforeReveal &&
    targetFrameCount !== primaryFrames.length
  ) {
    throw new Error("Development target does not match revealed T2 frames.");
  }
  if (
    dataRole === "development" &&
    retryLineage?.mode === "review_proxy_decode_upgrade" &&
    frames.some((frame) => frame.proxyBinding === null)
  ) {
    throw new Error(
      "Development review-proxy retry is missing proxy bindings.",
    );
  }
  if (expected?.dataRole !== undefined && expected.dataRole !== dataRole) {
    throw new Error("Annotation role does not match the create intent.");
  }
  if (
    expected?.lockedProfileId !== undefined &&
    expected.lockedProfileId !== lockedProfile.profileId
  ) {
    throw new Error("Locked profile does not match the create intent.");
  }
  if (
    expected?.developmentProbeJobIds !== undefined &&
    (expected.developmentProbeJobIds.length !== developmentProbeJobIds.length ||
      [...expected.developmentProbeJobIds]
        .sort()
        .some((id, index) => id !== [...developmentProbeJobIds].sort()[index]))
  ) {
    throw new Error("Development probes do not match the create intent.");
  }
  let finalPackage: { packageSha256: string } | null = null;
  if (session.final_package !== null) {
    const summary = record(session.final_package, "Final package");
    exactKeys(
      summary,
      [
        "result_url",
        "manifest_sha256",
        "package_sha256",
        "report_sha256",
        "status",
      ],
      "Final package",
    );
    if (
      summary.result_url !==
      `/api/v1/ball-annotation-sessions/${sessionId}/result`
    ) {
      throw new Error("Final result authority URL is invalid.");
    }
    sha256(summary.manifest_sha256, "Final manifest SHA-256");
    sha256(summary.report_sha256, "Final report SHA-256");
    oneOf(
      summary.status,
      [
        "not_applicable",
        "insufficient_evidence",
        "feasibility_failed",
        "feasibility_passed",
      ] as const,
      "Final report status",
    );
    finalPackage = {
      packageSha256: sha256(summary.package_sha256, "Final package SHA-256"),
    };
  }
  if (
    (status === "finalized" && finalPackage === null) ||
    (status !== "finalized" && finalPackage !== null)
  ) {
    throw new Error("Final package lifecycle is invalid.");
  }
  return {
    developmentProbeJobIds,
    developmentProbeDigestMaps: developmentDigestMaps,
    runtimeEnvironmentSha256,
    operatorId,
    applicableScaleStrata: applicableScales,
    applicableLightingStrata: applicableLighting,
    samplingManifestSha256: manifestSha256,
    targetFrameCount,
    view: {
      sessionId,
      requestSha256,
      dataRole,
      status,
      stage: session.stage as string,
      source: {
        sourceId: safeId(source.source_id, "Source ID"),
        sourceSha256: sha256(source.sha256, "Source SHA-256"),
        width: sourceWidth,
        height: sourceHeight,
        frameCount: integer(source.frame_count, "Source frame count", 1),
        fps: sourceFps,
      },
      decode: {
        requestedMode: decode.requested_decode_mode,
        effectiveMode: decode.effective_decode_mode,
        positionVerification: decode.position_verification,
      },
      lockedProfile,
      controlProfileId,
      samplingManifestSha256: manifestSha256,
      metricProfileId: "tiny_ball_feasibility_metric_v1",
      attemptFamilySha256,
      developmentPackageBinding,
      checkProbeJobId,
      checkProbeAuthority: checkAuthority
        ? {
            jobId: checkAuthority.jobId,
            reportSha256: checkAuthority.reportSha256,
          }
        : null,
      retryFromSessionId,
      retryLineage,
      errorCode,
      blockerCode,
      reviewProxyRepair,
      frames,
      progress: {
        annotatedFrames,
        totalFrames,
        unconfirmedSuggestions,
        missingStrata: [],
        primaryAnnotatedFrames,
        primaryTotalFrames,
        supplementalAnnotatedFrames,
        supplementalTotalFrames,
        unconfirmedPropagationSuggestions,
      },
      finalPackage,
    },
  };
}

export function parseBallAnnotationRevision(
  value: unknown,
  responseEtag: string | null,
  expected: {
    sessionId: string;
    frameIndex: number;
    mutationId: string;
    sourceWidth: number;
    sourceHeight: number;
    dataRole: "development" | "check";
    request: BallAnnotationMutationRequest;
    suggestionDecision?: {
      action: "accept" | "dismiss";
      kind: "detector_candidate" | "propagation";
      id: string;
      jobId: string;
      sha256: string;
    };
  },
): ParsedBallAnnotationRevision {
  const revision = record(value, "Annotation revision");
  exactKeys(
    revision,
    [
      "schema_version",
      "artifact_type",
      "revision_id",
      "session_id",
      "frame_index",
      "revision",
      "operation",
      "mutation_id",
      "expected_revision",
      "supersedes_revision",
      "undo_revision",
      "accepted_suggestion_kind",
      "accepted_suggestion_id",
      "accepted_suggestion_job_id",
      "accepted_suggestion_sha256",
      "dismissed_suggestion_kind",
      "dismissed_suggestion_id",
      "dismissed_suggestion_job_id",
      "dismissed_suggestion_sha256",
      "effective_annotation",
      "operator_id",
      "annotation_etag",
      "created_at",
    ],
    "Annotation revision",
  );
  if (
    revision.schema_version !== "1.0" ||
    revision.artifact_type !== "ball_annotation_revision"
  ) {
    throw new Error("Annotation revision identity is invalid.");
  }
  const sessionId = safeId(revision.session_id, "Revision session ID");
  const frameIndex = integer(revision.frame_index, "Revision frame index");
  const mutationId = safeId(revision.mutation_id, "Revision mutation ID");
  const operation = oneOf(
    revision.operation,
    ["set", "delete", "undo"] as const,
    "Revision operation",
  );
  const expectedRevision = integer(
    revision.expected_revision,
    "Expected revision",
  );
  const request = expected.request;
  if (
    sessionId !== expected.sessionId ||
    frameIndex !== expected.frameIndex ||
    mutationId !== expected.mutationId ||
    request.mutation_id !== expected.mutationId ||
    operation !== request.operation ||
    expectedRevision !== request.expected_revision ||
    revision.undo_revision !== request.undo_revision ||
    (operation === "set" &&
      pythonCanonicalSha256Sync(
        revision.effective_annotation,
        annotationAuthorityFloatPaths(),
      ) !==
        pythonCanonicalSha256Sync(
          request.annotation,
          annotationAuthorityFloatPaths(),
        ))
  ) {
    throw new Error("Annotation revision does not match the mutation intent.");
  }
  safeId(revision.revision_id, "Revision ID");
  safeId(revision.operator_id, "Revision operator ID");
  stringValue(revision.created_at, "Revision created time");
  if (revision.supersedes_revision !== null) {
    integer(revision.supersedes_revision, "Superseded revision", 1);
  }
  if (revision.undo_revision !== null) {
    integer(revision.undo_revision, "Undo revision", 1);
  }
  const parseSuggestionTuple = (
    prefix: "accepted" | "dismissed",
  ): [
    "detector_candidate" | "propagation" | null,
    string | null,
    string | null,
    string | null,
  ] => {
    const kindValue = revision[`${prefix}_suggestion_kind`];
    const idValue = revision[`${prefix}_suggestion_id`];
    const jobValue = revision[`${prefix}_suggestion_job_id`];
    const digestValue = revision[`${prefix}_suggestion_sha256`];
    return [
      kindValue === null
        ? null
        : oneOf(
            kindValue,
            ["detector_candidate", "propagation"] as const,
            `${prefix} suggestion kind`,
          ),
      idValue === null ? null : safeId(idValue, `${prefix} suggestion ID`),
      jobValue === null
        ? null
        : safeId(jobValue, `${prefix} suggestion job ID`),
      digestValue === null
        ? null
        : sha256(digestValue, `${prefix} suggestion digest`),
    ];
  };
  const acceptedBinding = parseSuggestionTuple("accepted");
  const dismissedBinding = parseSuggestionTuple("dismissed");
  for (const binding of [acceptedBinding, dismissedBinding]) {
    if (
      binding.some((item) => item !== null) !==
      binding.every((item) => item !== null)
    ) {
      throw new Error("Revision suggestion lineage is incomplete.");
    }
  }
  if (
    acceptedBinding.some((item) => item !== null) &&
    dismissedBinding.some((item) => item !== null)
  ) {
    throw new Error("Revision suggestion decisions conflict.");
  }
  const decision = expected.suggestionDecision;
  const expectedSuggestionBinding = decision
    ? [
        oneOf(
          decision.kind,
          ["detector_candidate", "propagation"] as const,
          "Expected suggestion kind",
        ),
        safeId(decision.id, "Expected suggestion ID"),
        safeId(decision.jobId, "Expected suggestion job ID"),
        sha256(decision.sha256, "Expected suggestion digest"),
      ]
    : [null, null, null, null];
  const responseSuggestionBinding =
    decision?.action === "dismiss" ? dismissedBinding : acceptedBinding;
  if (
    (decision?.action === "dismiss" ? acceptedBinding : dismissedBinding).some(
      (item) => item !== null,
    ) ||
    responseSuggestionBinding.some(
      (item, index) => item !== expectedSuggestionBinding[index],
    )
  ) {
    throw new Error("Revision suggestion lineage does not match the mutation.");
  }
  const annotation = parseAnnotation(
    revision.effective_annotation,
    "Effective annotation",
    {
      width: expected.sourceWidth,
      height: expected.sourceHeight,
      dataRole: expected.dataRole,
    },
  );
  const expectedProvenance = decision
    ? decision.action === "dismiss"
      ? "suggestion_dismissed_manual"
      : decision.kind === "detector_candidate"
        ? "detector_candidate_human_confirmed"
        : "propagation_suggestion_human_confirmed"
    : operation === "set"
      ? "manual_human_annotation"
      : null;
  if (
    (operation === "set") !== (annotation !== null) ||
    (annotation && annotation.provenance !== expectedProvenance)
  ) {
    throw new Error(
      "Revision provenance does not match its suggestion decision.",
    );
  }
  const digest = sha256(revision.annotation_etag, "Revision ETag");
  const annotationEtag = `"${digest}"`;
  if (responseEtag !== annotationEtag) {
    throw new Error("Revision response ETag does not match its body.");
  }
  return {
    sessionId,
    frameIndex,
    revision: integer(revision.revision, "Revision number", 1),
    operation,
    annotationEtag,
  };
}

function parseRawMetric(
  value: unknown,
  label: string,
  intervalKey?: "one_sided_95_lower" | "one_sided_95_upper",
) {
  const metric = record(value, label);
  exactKeys(
    metric,
    intervalKey
      ? ["raw", "point_estimate", intervalKey]
      : ["raw", "point_estimate"],
    label,
  );
  const raw = record(metric.raw, `${label} raw counts`);
  exactKeys(raw, ["numerator", "denominator"], `${label} raw counts`);
  const numerator = integer(raw.numerator, `${label} numerator`);
  const denominator = integer(raw.denominator, `${label} denominator`);
  const pointEstimate = finiteNumber(metric.point_estimate, `${label} point`);
  const interval = intervalKey
    ? finiteNumber(metric[intervalKey], `${label} interval`)
    : null;
  const expectedPoint = denominator === 0 ? 0 : numerator / denominator;
  const expectedInterval =
    intervalKey === "one_sided_95_lower"
      ? feasibilityWilsonLower(numerator, denominator)
      : intervalKey === "one_sided_95_upper"
        ? feasibilityHoeffdingUpper(expectedPoint, denominator)
        : null;
  if (
    (intervalKey === "one_sided_95_lower" && numerator > denominator) ||
    (intervalKey === "one_sided_95_upper" && numerator > 5 * denominator) ||
    Math.abs(pointEstimate - expectedPoint) > 1e-12 ||
    (expectedInterval !== null &&
      (interval === null || Math.abs(interval - expectedInterval) > 1e-12))
  ) {
    throw new Error(`${label} estimates do not match raw counts.`);
  }
  return { numerator, denominator, pointEstimate, interval };
}

function feasibilityWilsonLower(successes: number, total: number) {
  if (total <= 0) return 0;
  const z = 1.6448536269514722;
  const point = successes / total;
  const denominator = 1 + (z * z) / total;
  const center = point + (z * z) / (2 * total);
  const spread =
    z * Math.sqrt((point * (1 - point) + (z * z) / (4 * total)) / total);
  return Math.max(0, (center - spread) / denominator);
}

function feasibilityHoeffdingUpper(point: number, total: number) {
  if (total <= 0) return 5;
  const radius = 5 * Math.sqrt(Math.log(1 / 0.05) / (2 * total));
  return Math.min(5, point + radius);
}

function parseAuthorizations(value: unknown, passed: boolean) {
  const authorizations = record(value, "Feasibility authorizations");
  exactKeys(
    authorizations,
    [
      "may_expand_to_100_300_boxes",
      "trial_eligible",
      "source_segment_qualified",
      "camera_qualified",
      "production_approved",
      "full_run_authorized",
    ],
    "Feasibility authorizations",
  );
  if (
    authorizations.may_expand_to_100_300_boxes !== passed ||
    [
      "trial_eligible",
      "source_segment_qualified",
      "camera_qualified",
      "production_approved",
      "full_run_authorized",
    ].some((field) => authorizations[field] !== false)
  ) {
    throw new Error("Feasibility authorizations exceed the allowed boundary.");
  }
}

function parseDatasetExpansionEligibility(value: unknown) {
  const eligibility = record(value, "Dataset expansion eligibility");
  exactKeys(
    eligibility,
    ["eligible", "reasons", "validation_evidence"],
    "Dataset expansion eligibility",
  );
  if (
    typeof eligibility.eligible !== "boolean" ||
    !Array.isArray(eligibility.reasons) ||
    eligibility.reasons.some(
      (reason) =>
        ![
          "check_role_is_evaluation_only",
          "pending_suggestion_decisions",
          "no_localizable_positive_seed",
        ].includes(reason as string),
    )
  ) {
    throw new Error("Dataset expansion eligibility is invalid.");
  }
  const evidence = record(
    eligibility.validation_evidence,
    "Dataset expansion validation evidence",
  );
  exactKeys(
    evidence,
    [
      "all_frames_human_confirmed",
      "all_primary_roles_complete",
      "all_supplemental_roles_complete",
      "exact_frame_media_sha256",
      "frame_evidence_sha256",
      "localizable_positive_seed_count",
      "pending_detector_candidate_count",
      "pending_propagation_suggestion_count",
      "pending_suggestion_decision_count",
      "revision_chain_sha256",
    ],
    "Dataset expansion validation evidence",
  );
  for (const field of [
    "all_frames_human_confirmed",
    "all_primary_roles_complete",
    "all_supplemental_roles_complete",
  ] as const) {
    if (evidence[field] !== true) {
      throw new Error("Dataset expansion validation flags are invalid.");
    }
  }
  for (const field of [
    "exact_frame_media_sha256",
    "frame_evidence_sha256",
    "revision_chain_sha256",
  ] as const) {
    sha256(evidence[field], `Dataset expansion ${field}`);
  }
  const pendingDetectorCandidateCount = integer(
    evidence.pending_detector_candidate_count,
    "Pending detector candidate count",
  );
  const pendingPropagationSuggestionCount = integer(
    evidence.pending_propagation_suggestion_count,
    "Pending propagation suggestion count",
  );
  const pendingSuggestionDecisionCount = integer(
    evidence.pending_suggestion_decision_count,
    "Pending suggestion decision count",
  );
  const localizablePositiveSeedCount = integer(
    evidence.localizable_positive_seed_count,
    "Localizable positive seed count",
  );
  const reasons = eligibility.reasons as string[];
  const canonicalReasons = [
    "check_role_is_evaluation_only",
    "pending_suggestion_decisions",
    "no_localizable_positive_seed",
  ].filter((reason) => reasons.includes(reason));
  const hasCheckReason = reasons.includes("check_role_is_evaluation_only");
  if (
    reasons.length !== new Set(reasons).size ||
    !sameOrderedValues(reasons, canonicalReasons) ||
    eligibility.eligible !== (reasons.length === 0) ||
    reasons.includes("no_localizable_positive_seed") !==
      (localizablePositiveSeedCount === 0 && !hasCheckReason)
  ) {
    throw new Error("Dataset expansion eligibility reasons are invalid.");
  }
  if (
    reasons.includes("pending_suggestion_decisions") !==
      pendingSuggestionDecisionCount > 0 ||
    pendingSuggestionDecisionCount !==
      pendingDetectorCandidateCount + pendingPropagationSuggestionCount ||
    pendingDetectorCandidateCount !== 0 ||
    pendingPropagationSuggestionCount !== 0 ||
    pendingSuggestionDecisionCount !== 0
  ) {
    throw new Error(
      "Final evidence cannot contain pending suggestion decisions.",
    );
  }
  return {
    eligible: eligibility.eligible,
    reasons,
    exactFrameMediaSha256: evidence.exact_frame_media_sha256 as string,
    frameEvidenceSha256: evidence.frame_evidence_sha256 as string,
    revisionChainSha256: evidence.revision_chain_sha256 as string,
    localizablePositiveSeedCount,
    pendingDetectorCandidateCount,
    pendingPropagationSuggestionCount,
    pendingSuggestionDecisionCount,
  };
}

function parseFinalAnnotations(
  value: unknown,
  session: ParsedBallAnnotationSession,
) {
  if (!Array.isArray(value)) {
    throw new Error("Effective annotations are invalid.");
  }
  const seen = new Set<number>();
  return value.map((rawValue, index) => {
    const row = record(rawValue, `Effective annotation ${index}`);
    const frameIndex = integer(row.frame_index, "Effective frame index");
    if (seen.has(frameIndex)) {
      throw new Error("Effective annotation frames are duplicated.");
    }
    seen.add(frameIndex);
    const { frame_index: _frameIndex, ...payload } = row;
    const annotation = parseAnnotation(
      payload,
      `Effective annotation ${frameIndex}`,
      {
        width: session.view.source.width,
        height: session.view.source.height,
        dataRole: session.view.dataRole,
      },
    );
    if (!annotation || annotation.annotationState !== "confirmed") {
      throw new Error("Final annotation is not human-confirmed.");
    }
    return { frameIndex, annotation };
  });
}

function authorityCanonicalSha256(value: unknown, label: string) {
  try {
    return pythonCanonicalSha256Sync(value, []);
  } catch {
    throw new Error(`${label} is not canonically serializable.`);
  }
}

function sameCanonicalValue(left: unknown, right: unknown) {
  return (
    authorityCanonicalSha256(left, "Authority value") ===
    authorityCanonicalSha256(right, "Authority value")
  );
}

function sameOrderedValues(
  left: readonly unknown[],
  right: readonly unknown[],
) {
  return (
    left.length === right.length &&
    left.every((item, index) => item === right[index])
  );
}

function parseSessionRequestAuthority(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageValue: Record<string, unknown>;
    packageManifest: Record<string, unknown>;
    packageDevelopmentBinding: ReturnType<
      typeof parseDevelopmentPackageBinding
    >;
  },
) {
  const authority = record(value, "Session request authority");
  exactKeys(
    authority,
    [
      "schema_version",
      "artifact_type",
      "session_id",
      "request_sha256",
      "normalized_request",
      "authority_sha256",
    ],
    "Session request authority",
  );
  const normalized = record(
    authority.normalized_request,
    "Normalized session request",
  );
  exactKeys(
    normalized,
    [
      "data_role",
      "development_probe_job_ids",
      "locked_profile_id",
      "target_frame_count",
      "sampling_profile_id",
      "metric_profile_id",
      "operator_id",
      "strata_applicability",
      "applicable_scale_strata",
      "applicable_lighting_strata",
      "retry_from_session_id",
      "development_package_session_id",
      "development_package_sha256",
    ],
    "Normalized session request",
  );
  const requestSha256 = sha256(
    authority.request_sha256,
    "Session request authority SHA-256",
  );
  const authoritySha256 = sha256(
    authority.authority_sha256,
    "Session request authority envelope SHA-256",
  );
  const authorityBody = { ...authority };
  delete authorityBody.authority_sha256;
  if (
    authority.schema_version !== "1.0" ||
    authority.artifact_type !== "ball_annotation_session_request_authority" ||
    authority.session_id !== context.session.view.sessionId ||
    requestSha256 !== context.session.view.requestSha256 ||
    authorityCanonicalSha256(normalized, "Normalized session request") !==
      requestSha256 ||
    authorityCanonicalSha256(authorityBody, "Session request authority") !==
      authoritySha256
  ) {
    throw new Error(
      "Session request authority digest or session binding is invalid.",
    );
  }
  const expectedPrefix = `annotation-${requestSha256.slice(0, 16)}-`;
  const suffix = context.session.view.sessionId.slice(expectedPrefix.length);
  if (
    !context.session.view.sessionId.startsWith(expectedPrefix) ||
    !/^[0-9a-f]{12}$/.test(suffix)
  ) {
    throw new Error("Session request authority does not bind the session ID.");
  }
  if (!Array.isArray(normalized.development_probe_job_ids)) {
    throw new Error("Session request selection is invalid.");
  }
  const developmentProbeJobIds = normalized.development_probe_job_ids.map(
    (jobId) => safeId(jobId, "Session request development probe job ID"),
  );
  const sortedJobIds = [...developmentProbeJobIds].sort();
  const applicability = parseApplicability(normalized.strata_applicability);
  const applicableScales = Array.isArray(normalized.applicable_scale_strata)
    ? normalized.applicable_scale_strata.map((item) =>
        oneOf(item, SCALE_STRATA, "Session request scale stratum"),
      )
    : [];
  const applicableLighting = Array.isArray(
    normalized.applicable_lighting_strata,
  )
    ? normalized.applicable_lighting_strata.map((item) =>
        oneOf(item, LIGHTING_STRATA, "Session request lighting stratum"),
      )
    : [];
  const developmentSessionId =
    normalized.development_package_session_id === null
      ? null
      : safeId(
          normalized.development_package_session_id,
          "Session request development package session ID",
        );
  const developmentPackageSha256 =
    normalized.development_package_sha256 === null
      ? null
      : sha256(
          normalized.development_package_sha256,
          "Session request development package SHA-256",
        );
  const retryFromSessionId = nullableSafeId(
    normalized.retry_from_session_id,
    "Session request retry session ID",
  );
  const dataRole = context.session.view.dataRole;
  if (
    normalized.data_role !== dataRole ||
    !sameOrderedValues(developmentProbeJobIds, sortedJobIds) ||
    !sameOrderedValues(
      developmentProbeJobIds,
      [...context.session.developmentProbeJobIds].sort(),
    ) ||
    normalized.locked_profile_id !==
      context.session.view.lockedProfile.profileId ||
    normalized.operator_id !== context.session.operatorId ||
    normalized.operator_id !== context.packageValue.operator_id ||
    normalized.sampling_profile_id !== "tiny_ball_temporal_groups_v1" ||
    normalized.metric_profile_id !== context.session.view.metricProfileId ||
    retryFromSessionId !== context.session.view.retryFromSessionId ||
    !sameCanonicalValue(
      normalized.strata_applicability,
      context.packageManifest.strata_applicability,
    ) ||
    !sameOrderedValues(applicableScales, applicability.scale) ||
    !sameOrderedValues(applicableLighting, applicability.lighting) ||
    !sameOrderedValues(
      applicableScales,
      context.session.applicableScaleStrata ?? [],
    ) ||
    !sameOrderedValues(
      applicableLighting,
      context.session.applicableLightingStrata ?? [],
    )
  ) {
    throw new Error(
      "Session request selection differs from the final package.",
    );
  }
  if (
    dataRole === "development"
      ? normalized.target_frame_count !== null ||
        developmentSessionId !== null ||
        developmentPackageSha256 !== null ||
        context.packageDevelopmentBinding !== null
      : integer(
          normalized.target_frame_count,
          "Session request target frame count",
          20,
        ) !== context.session.targetFrameCount ||
        context.session.targetFrameCount > 50 ||
        developmentSessionId !== context.packageDevelopmentBinding?.sessionId ||
        developmentPackageSha256 !==
          context.packageDevelopmentBinding?.packageSha256
  ) {
    throw new Error("Session request role authority is invalid.");
  }
  return { requestSha256, normalized };
}

const DETECTOR_PROBE_AUTHORITY_KEYS = [
  "schema_version",
  "artifact_type",
  "job_id",
  "request_sha256",
  "intent_sha256",
  "semantic_intent_sha256",
  "resource_sha256",
  "frozen_profiles_sha256",
  "execution_bundle_sha256",
  "runtime_environment_sha256",
  "retry_from_job_id",
  "retry_kind",
  "frozen_request",
  "frozen_profiles",
  "probe_report_sha256",
  "probe_result_manifest_sha256",
  "probe_report",
  "probe_result_manifest",
  "probe_job_record",
  "canonical_job_record_sha256",
  "audit_anchor_kind",
  "job_record_authority_sha256",
] as const;

const DETECTOR_RESOURCE_FIELDS = [
  "parent_trial_id",
  "source_id",
  "source_sha256",
  "source_file_identity_sha256",
  "tracking_contract_sha256",
  "base_config_relative_path",
  "base_config_sha256",
  "effective_config_relative_path",
  "effective_config_sha256",
  "trial_intent_sha256",
  "tuning_patch_sha256",
] as const;

const DETECTOR_JOB_RECORD_REQUIRED_KEYS = [
  "schema_version",
  "artifact_type",
  "job_id",
  "request_sha256",
  "intent_sha256",
  "semantic_intent_sha256",
  "status",
  "frozen_request",
  "frozen_profiles",
  "retry_from_job_id",
  "report",
  "result_manifest_sha256",
] as const;

const DETECTOR_JOB_RECORD_ALLOWED_KEYS = [
  ...DETECTOR_JOB_RECORD_REQUIRED_KEYS,
  "retry_kind",
  "resource_sha256",
  "frozen_profiles_sha256",
  "idempotency_key",
  "stage",
  "progress",
  "error_code",
  "blocker_code",
  "recovery_action",
  "created_at",
  "updated_at",
  "status_url",
  "cancel_url",
  "can_cancel",
] as const;

const DETECTOR_REPORT_REQUIRED_KEYS = [
  "schema_version",
  "artifact_type",
  "job_id",
  "request_sha256",
  "source",
  "lineage",
  "frozen_profiles",
  "frames",
  "artifacts",
  "report_sha256",
] as const;

const DETECTOR_REPORT_ALLOWED_KEYS = [
  ...DETECTOR_REPORT_REQUIRED_KEYS,
  "top_k",
  "decode",
  "review_proxy_manifest",
  "execution",
  "created_at",
] as const;

function parseFrozenDetectorProfile(value: unknown, label: string) {
  const profile = record(value, label);
  const descriptor = record(profile.model_descriptor, `${label} descriptor`);
  const weights = record(descriptor.weights, `${label} weights`);
  return {
    profile_id: safeId(profile.profile_id, `${label} profile ID`),
    profile_sha256: sha256(profile.profile_sha256, `${label} profile SHA-256`),
    model_id: safeId(profile.model_id, `${label} model ID`),
    model_version: stringValue(profile.model_version, `${label} model version`),
    model_descriptor_sha256: sha256(
      profile.model_descriptor_sha256,
      `${label} model descriptor SHA-256`,
    ),
    weights_sha256: sha256(weights.sha256, `${label} weights SHA-256`),
  };
}

function detectorReportFloatPaths(
  value: Record<string, unknown>,
  root = "$",
  currentSchema = false,
) {
  const paths: string[] = [];
  const decode =
    value.decode && typeof value.decode === "object"
      ? (value.decode as Record<string, unknown>)
      : null;
  if (decode) {
    paths.push(`${root}.decode.fps`);
    if (Array.isArray(decode.frame_timing_observations)) {
      decode.frame_timing_observations.forEach((_, index) => {
        paths.push(
          `${root}.decode.frame_timing_observations[${index}].decoder_reported_pos_msec`,
        );
      });
    }
  }
  if (Array.isArray(value.frames)) {
    value.frames.forEach((rawFrame, frameIndex) => {
      if (!rawFrame || typeof rawFrame !== "object") return;
      const frame = rawFrame as Record<string, unknown>;
      if (frame.decoder_reported_pos_msec !== null) {
        paths.push(`${root}.frames[${frameIndex}].decoder_reported_pos_msec`);
      }
      if (currentSchema) {
        for (const field of [
          "mean_luma",
          "std_luma",
          "texture_tile_ratio",
          "dominant_color_ratio",
        ]) {
          paths.push(`${root}.frames[${frameIndex}].media_integrity.${field}`);
        }
      }
      if (!Array.isArray(frame.profile_results)) return;
      frame.profile_results.forEach((rawProfile, profileIndex) => {
        if (!rawProfile || typeof rawProfile !== "object") return;
        const profile = rawProfile as Record<string, unknown>;
        if (currentSchema && profile.latency_ms !== null) {
          paths.push(
            `${root}.frames[${frameIndex}].profile_results[${profileIndex}].latency_ms`,
          );
        }
        for (const field of ["display_candidate", "raw_candidates"] as const) {
          const candidates =
            field === "display_candidate"
              ? profile[field] === null
                ? []
                : [profile[field]]
              : Array.isArray(profile[field])
                ? profile[field]
                : [];
          candidates.forEach((rawCandidate, candidateIndex) => {
            if (!rawCandidate || typeof rawCandidate !== "object") return;
            const candidate = rawCandidate as Record<string, unknown>;
            const candidateRoot =
              field === "display_candidate"
                ? `${root}.frames[${frameIndex}].profile_results[${profileIndex}].display_candidate`
                : `${root}.frames[${frameIndex}].profile_results[${profileIndex}].raw_candidates[${candidateIndex}]`;
            if (Array.isArray(candidate.bbox_source_px)) {
              candidate.bbox_source_px.forEach((_, coordinateIndex) => {
                paths.push(
                  `${candidateRoot}.bbox_source_px[${coordinateIndex}]`,
                );
              });
            }
            paths.push(`${candidateRoot}.confidence`);
          });
        }
      });
    });
  }
  const proxy = value.review_proxy_manifest;
  if (proxy && typeof proxy === "object") {
    const mappings = (proxy as Record<string, unknown>).mappings;
    paths.push(
      ...reviewProxyManifestFloatPaths(
        Array.isArray(mappings) ? mappings.length : 0,
        `${root}.review_proxy_manifest`,
      ),
    );
  }
  return paths;
}

function detectorJobFloatPaths(
  job: Record<string, unknown>,
  root: string,
  currentSchema: boolean,
) {
  const report = record(job.report, "Detector job report");
  return detectorReportFloatPaths(report, `${root}.report`, currentSchema);
}

function parseDetectorProbeAuthorities(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageValue: Record<string, unknown>;
    packageSource: Record<string, unknown>;
    packageProfile: Record<string, unknown>;
    packageControlProfile: Record<string, unknown>;
    packageManifest: Record<string, unknown>;
    packageLineage: ReturnType<typeof parseLineage>;
    packageCheckAuthority: Record<string, unknown> | null;
  },
) {
  if (!Array.isArray(value) || value.length > 8) {
    throw new Error(
      "Detector probe authority collection is outside its bound.",
    );
  }
  const authorities = value.map((rawAuthority, index) => {
    const label = `Detector probe authority ${index}`;
    const authority = record(rawAuthority, label);
    exactKeys(authority, DETECTOR_PROBE_AUTHORITY_KEYS, label);
    const jobId = safeId(authority.job_id, `${label} job ID`);
    const requestSha256 = sha256(
      authority.request_sha256,
      `${label} request SHA-256`,
    );
    const intentSha256 = sha256(
      authority.intent_sha256,
      `${label} intent SHA-256`,
    );
    const semanticIntentSha256 =
      authority.semantic_intent_sha256 === null
        ? null
        : sha256(
            authority.semantic_intent_sha256,
            `${label} semantic intent SHA-256`,
          );
    const resourceSha256 = sha256(
      authority.resource_sha256,
      `${label} resource SHA-256`,
    );
    const frozenProfilesSha256 = sha256(
      authority.frozen_profiles_sha256,
      `${label} frozen profiles SHA-256`,
    );
    const executionBundleSha256 = sha256(
      authority.execution_bundle_sha256,
      `${label} execution bundle SHA-256`,
    );
    const runtimeEnvironmentSha256 = sha256(
      authority.runtime_environment_sha256,
      `${label} runtime environment SHA-256`,
    );
    const reportSha256 = sha256(
      authority.probe_report_sha256,
      `${label} report SHA-256`,
    );
    const resultManifestSha256 = sha256(
      authority.probe_result_manifest_sha256,
      `${label} result manifest SHA-256`,
    );
    const canonicalJobRecordSha256 = sha256(
      authority.canonical_job_record_sha256,
      `${label} canonical job record SHA-256`,
    );
    const jobRecordAuthoritySha256 = sha256(
      authority.job_record_authority_sha256,
      `${label} job record authority SHA-256`,
    );
    const retryFromJobId = nullableSafeId(
      authority.retry_from_job_id,
      `${label} retry job ID`,
    );
    const retryKind =
      authority.retry_kind === null
        ? null
        : stringValue(authority.retry_kind, `${label} retry kind`);
    const auditAnchorKind = oneOf(
      authority.audit_anchor_kind,
      ["audited_t2_legacy", "embedded_job_record"] as const,
      `${label} audit anchor kind`,
    );
    const frozenRequest = record(
      authority.frozen_request,
      `${label} frozen request`,
    );
    if (!Array.isArray(authority.frozen_profiles)) {
      throw new Error(`${label} frozen profiles are invalid.`);
    }
    const frozenProfiles = authority.frozen_profiles.map((profile, offset) =>
      parseFrozenDetectorProfile(profile, `${label} frozen profile ${offset}`),
    );
    const profileIds = frozenProfiles.map((profile) => profile.profile_id);
    if (
      frozenProfiles.length < 2 ||
      frozenProfiles.length > 6 ||
      new Set(profileIds).size !== profileIds.length ||
      authorityCanonicalSha256(frozenRequest, `${label} frozen request`) !==
        requestSha256 ||
      authorityCanonicalSha256(
        authority.frozen_profiles,
        `${label} frozen profiles`,
      ) !== frozenProfilesSha256
    ) {
      throw new Error(
        `${label} detector request/profile authority is invalid.`,
      );
    }
    const intent = { ...frozenRequest };
    delete intent.retry_from_job_id;
    const resource = Object.fromEntries(
      DETECTOR_RESOURCE_FIELDS.map((field) => {
        if (!Object.prototype.hasOwnProperty.call(frozenRequest, field)) {
          throw new Error(`${label} resource authority is incomplete.`);
        }
        return [field, frozenRequest[field]];
      }),
    );
    if (
      authorityCanonicalSha256(intent, `${label} intent`) !== intentSha256 ||
      authorityCanonicalSha256(resource, `${label} resource`) !==
        resourceSha256 ||
      frozenRequest.execution_bundle_sha256 !== executionBundleSha256 ||
      frozenRequest.runtime_environment_sha256 !== runtimeEnvironmentSha256 ||
      (frozenRequest.retry_from_job_id ?? null) !== retryFromJobId ||
      (frozenRequest.retry_kind ?? null) !== retryKind ||
      frozenRequest.parent_trial_id !==
        record(context.packageValue.lineage, "Package lineage")
          .parent_trial_id ||
      frozenRequest.source_id !== context.packageSource.source_id ||
      frozenRequest.source_sha256 !== context.packageSource.sha256 ||
      frozenRequest.source_file_identity_sha256 !==
        context.packageSource.file_identity_sha256 ||
      frozenRequest.source_width !== context.packageSource.width ||
      frozenRequest.source_height !== context.packageSource.height ||
      frozenRequest.source_frame_count !== context.packageSource.frame_count ||
      frozenRequest.source_size_bytes !== context.packageSource.size_bytes ||
      frozenRequest.tracking_contract_sha256 !==
        context.packageSource.tracking_contract_sha256 ||
      (frozenRequest.annotation_sampling_manifest_sha256 !== null &&
        frozenRequest.annotation_sampling_manifest_sha256 !==
          context.packageManifest.manifest_sha256)
    ) {
      throw new Error(
        `${label} detector resource/session authority is invalid.`,
      );
    }
    const report = record(authority.probe_report, `${label} report`);
    exactAllowedKeys(
      report,
      DETECTOR_REPORT_REQUIRED_KEYS,
      DETECTOR_REPORT_ALLOWED_KEYS,
      `${label} report`,
    );
    const reportLineage = record(report.lineage, `${label} report lineage`);
    const resultManifest = record(
      authority.probe_result_manifest,
      `${label} result manifest`,
    );
    exactKeys(
      resultManifest,
      [
        "schema_version",
        "artifact_type",
        "job_id",
        "request_sha256",
        "frozen_profiles_sha256",
        "execution_bundle_sha256",
        "runtime_environment_sha256",
        "source_file_identity_sha256",
        "report_content_sha256",
        "artifacts",
        "report_file_sha256",
        "report_file_size_bytes",
      ],
      `${label} result manifest`,
    );
    const jobRecord = record(
      authority.probe_job_record,
      `${label} immutable job record`,
    );
    exactAllowedKeys(
      jobRecord,
      DETECTOR_JOB_RECORD_REQUIRED_KEYS,
      DETECTOR_JOB_RECORD_ALLOWED_KEYS,
      `${label} immutable job record`,
    );
    const currentJobSchema = auditAnchorKind === "embedded_job_record";
    const jobFloatPaths = detectorJobFloatPaths(
      jobRecord,
      "$",
      currentJobSchema,
    );
    const authorityBody = { ...authority };
    delete authorityBody.job_record_authority_sha256;
    const authorityFloatPaths = [
      ...detectorReportFloatPaths(report, "$.probe_report", currentJobSchema),
      ...detectorJobFloatPaths(
        jobRecord,
        "$.probe_job_record",
        currentJobSchema,
      ),
    ];
    if (
      pythonCanonicalSha256Sync(jobRecord, jobFloatPaths) !==
        canonicalJobRecordSha256 ||
      pythonCanonicalSha256Sync(authorityBody, authorityFloatPaths) !==
        jobRecordAuthoritySha256 ||
      report.schema_version !== "1.0" ||
      report.artifact_type !== "detector_probe_report" ||
      report.job_id !== jobId ||
      report.request_sha256 !== requestSha256 ||
      report.report_sha256 !== reportSha256 ||
      !sameCanonicalValue(report.frozen_profiles, authority.frozen_profiles) ||
      reportLineage.intent_sha256 !== intentSha256 ||
      (reportLineage.semantic_intent_sha256 ?? null) !== semanticIntentSha256 ||
      reportLineage.frozen_profiles_sha256 !== frozenProfilesSha256 ||
      reportLineage.execution_bundle_sha256 !== executionBundleSha256 ||
      reportLineage.runtime_environment_sha256 !== runtimeEnvironmentSha256 ||
      resultManifest.schema_version !== "1.0" ||
      resultManifest.artifact_type !== "detector_probe_result_manifest" ||
      resultManifest.job_id !== jobId ||
      resultManifest.request_sha256 !== requestSha256 ||
      resultManifest.frozen_profiles_sha256 !== frozenProfilesSha256 ||
      resultManifest.execution_bundle_sha256 !== executionBundleSha256 ||
      resultManifest.runtime_environment_sha256 !== runtimeEnvironmentSha256 ||
      resultManifest.report_content_sha256 !== reportSha256 ||
      !sameCanonicalValue(resultManifest.artifacts, report.artifacts) ||
      jobRecord.schema_version !== "1.0" ||
      jobRecord.artifact_type !== "detector_probe_job" ||
      jobRecord.status !== "ready" ||
      jobRecord.job_id !== jobId ||
      jobRecord.request_sha256 !== requestSha256 ||
      jobRecord.intent_sha256 !== intentSha256 ||
      (jobRecord.semantic_intent_sha256 ?? null) !== semanticIntentSha256 ||
      (jobRecord.retry_from_job_id ?? null) !== retryFromJobId ||
      (jobRecord.retry_kind ?? null) !== retryKind ||
      jobRecord.result_manifest_sha256 !== resultManifestSha256 ||
      !sameCanonicalValue(jobRecord.frozen_request, frozenRequest) ||
      !sameCanonicalValue(
        jobRecord.frozen_profiles,
        authority.frozen_profiles,
      ) ||
      !sameCanonicalValue(jobRecord.report, report)
    ) {
      throw new Error(`${label} immutable detector job authority is invalid.`);
    }
    return {
      raw: authority,
      jobId,
      requestSha256,
      intentSha256,
      reportSha256,
      resultManifestSha256,
      executionBundleSha256,
      runtimeEnvironmentSha256,
      frozenProfilesSha256,
      retryFromJobId,
      retryKind,
      report,
      resultManifest,
      frozenProfiles,
    };
  });
  const actualJobIds = authorities.map((authority) => authority.jobId);
  if (new Set(actualJobIds).size !== actualJobIds.length) {
    throw new Error("Detector probe authority jobs are duplicated.");
  }
  let expectedJobIds: string[];
  if (context.session.view.dataRole === "development") {
    expectedJobIds = context.packageLineage.jobIds;
  } else {
    const current = authorities.at(-1);
    const currentLineage = current?.report.lineage;
    const reviewProxyUpgrade =
      currentLineage && typeof currentLineage === "object"
        ? (currentLineage as Record<string, unknown>).review_proxy_upgrade
        : null;
    expectedJobIds =
      reviewProxyUpgrade == null
        ? [context.session.view.checkProbeJobId as string]
        : [
            current?.retryFromJobId as string,
            context.session.view.checkProbeJobId as string,
          ];
    if (
      reviewProxyUpgrade != null &&
      (current?.retryFromJobId === null ||
        current?.retryKind !== "review_proxy_decode_upgrade")
    ) {
      throw new Error("Detector check proxy retry authority is invalid.");
    }
  }
  if (!sameOrderedValues(actualJobIds, expectedJobIds)) {
    throw new Error(
      "Detector probe authorities differ from exact package lineage.",
    );
  }
  const byJob = new Map(
    authorities.map((authority) => [authority.jobId, authority]),
  );
  if (context.session.view.dataRole === "development") {
    for (const [mapField, authorityField] of [
      ["development_probe_report_sha256s", "reportSha256"],
      ["development_probe_result_manifest_sha256s", "resultManifestSha256"],
      ["development_probe_execution_bundle_sha256s", "executionBundleSha256"],
      ["development_probe_frozen_profiles_sha256s", "frozenProfilesSha256"],
    ] as const) {
      const packageMap = context.packageLineage.digestMaps[mapField];
      const sessionMap = context.session.developmentProbeDigestMaps?.[mapField];
      if (
        !sameCanonicalValue(packageMap, sessionMap) ||
        actualJobIds.some(
          (jobId) => packageMap[jobId] !== byJob.get(jobId)?.[authorityField],
        )
      ) {
        throw new Error(
          "Detector probe authority differs from exact lineage digests.",
        );
      }
    }
  } else {
    const current = authorities.at(-1);
    const check = context.packageCheckAuthority;
    if (
      !current ||
      !check ||
      check.job_id !== current.jobId ||
      check.request_sha256 !== current.requestSha256 ||
      check.intent_sha256 !== current.intentSha256 ||
      check.report_sha256 !== current.reportSha256 ||
      check.result_manifest_sha256 !== current.resultManifestSha256 ||
      check.execution_bundle_sha256 !== current.executionBundleSha256 ||
      check.runtime_environment_sha256 !== current.runtimeEnvironmentSha256 ||
      check.frozen_profiles_sha256 !== current.frozenProfilesSha256
    ) {
      throw new Error(
        "Detector authority differs from the active check authority.",
      );
    }
  }
  const expectedProfiles = new Map(
    authorities[0]?.frozenProfiles.map((profile) => [
      profile.profile_id,
      profile,
    ]) ?? [],
  );
  const canonicalProfileMap = (
    profiles: Array<ReturnType<typeof parseFrozenDetectorProfile>>,
  ) =>
    Object.fromEntries(
      profiles
        .map((profile) => [profile.profile_id, profile] as const)
        .sort(([left], [right]) => left.localeCompare(right)),
    );
  const expectedProfileMap = canonicalProfileMap([
    ...expectedProfiles.values(),
  ]);
  if (
    authorities.length === 0 ||
    authorities.some(
      (authority) =>
        !sameCanonicalValue(
          canonicalProfileMap(authority.frozenProfiles),
          expectedProfileMap,
        ),
    ) ||
    !sameCanonicalValue(
      expectedProfiles.get(context.packageProfile.profile_id as string),
      context.packageProfile,
    ) ||
    !sameCanonicalValue(
      expectedProfiles.get(context.packageControlProfile.profile_id as string),
      context.packageControlProfile,
    )
  ) {
    throw new Error(
      "Detector frozen profiles do not bind package profile selection.",
    );
  }
  return { authorities, byJob };
}

const SEALED_REVISION_KEYS = [
  "schema_version",
  "artifact_type",
  "revision_id",
  "session_id",
  "frame_index",
  "revision",
  "operation",
  "mutation_id",
  "mutation_sha256",
  "expected_revision",
  "supersedes_revision",
  "undo_revision",
  "accepted_suggestion_kind",
  "accepted_suggestion_id",
  "accepted_suggestion_job_id",
  "accepted_suggestion_sha256",
  "dismissed_suggestion_kind",
  "dismissed_suggestion_id",
  "dismissed_suggestion_job_id",
  "dismissed_suggestion_sha256",
  "previous_effective_annotation",
  "effective_annotation",
  "operator_id",
  "annotation_etag",
  "created_at",
] as const;

function boundedCollection(
  value: unknown,
  label: string,
  minimum: number,
  maximum?: number,
) {
  if (
    !Array.isArray(value) ||
    value.length < minimum ||
    (maximum !== undefined && value.length > maximum)
  ) {
    throw new Error(`${label} collection is outside its public bound.`);
  }
  return value;
}

function parseSealedRevisionChain(
  value: unknown,
  session: ParsedBallAnnotationSession,
) {
  const revisions = boundedCollection(value, "Revision chain", 1);
  const revisionsByFrame = new Map<number, Array<Record<string, unknown>>>();
  const effectiveByFrame = new Map<number, Record<string, unknown> | null>();
  const revisionsByNumber = new Map<
    number,
    Map<number, Record<string, unknown>>
  >();
  const revisionsById = new Map<string, Record<string, unknown>>();
  revisions.forEach((rawRevision, index) => {
    const label = `Sealed revision ${index}`;
    const revision = record(rawRevision, label);
    exactKeys(revision, SEALED_REVISION_KEYS, label);
    const frameIndex = integer(revision.frame_index, `${label} frame index`);
    if (frameIndex >= session.view.source.frameCount) {
      throw new Error(`${label} frame index is outside the source.`);
    }
    const revisionNumber = integer(revision.revision, `${label} number`, 1);
    const expectedRevision = integer(
      revision.expected_revision,
      `${label} expected revision`,
    );
    const operation = oneOf(
      revision.operation,
      ["set", "delete", "undo"] as const,
      `${label} operation`,
    );
    const previousEffective =
      revision.previous_effective_annotation === null
        ? null
        : record(
            revision.previous_effective_annotation,
            `${label} previous effective annotation`,
          );
    const effective =
      revision.effective_annotation === null
        ? null
        : record(revision.effective_annotation, `${label} annotation`);
    parseAnnotation(previousEffective, `${label} previous annotation`, {
      width: session.view.source.width,
      height: session.view.source.height,
      dataRole: session.view.dataRole,
    });
    parseAnnotation(effective, `${label} effective annotation`, {
      width: session.view.source.width,
      height: session.view.source.height,
      dataRole: session.view.dataRole,
    });
    const priorEffective = effectiveByFrame.get(frameIndex) ?? null;
    const priorDigest =
      priorEffective === null
        ? null
        : pythonCanonicalSha256Sync(
            priorEffective,
            annotationAuthorityFloatPaths(),
          );
    const recordedPriorDigest =
      previousEffective === null
        ? null
        : pythonCanonicalSha256Sync(
            previousEffective,
            annotationAuthorityFloatPaths(),
          );
    if (
      revision.schema_version !== "1.0" ||
      revision.artifact_type !== "ball_annotation_revision" ||
      revision.session_id !== session.view.sessionId ||
      revision.operator_id !== session.operatorId ||
      expectedRevision !== revisionNumber - 1 ||
      (revisionNumber === 1
        ? revision.supersedes_revision !== null
        : revision.supersedes_revision !== revisionNumber - 1) ||
      priorDigest !== recordedPriorDigest ||
      (operation === "set" && effective === null) ||
      (operation === "delete" && effective !== null) ||
      (operation === "undo") !== (revision.undo_revision !== null)
    ) {
      throw new Error(`${label} session/revision authority is invalid.`);
    }
    const mutationId = safeId(revision.mutation_id, `${label} mutation ID`);
    const accepted = [
      revision.accepted_suggestion_id,
      revision.accepted_suggestion_job_id,
      revision.accepted_suggestion_sha256,
    ];
    const dismissed = [
      revision.dismissed_suggestion_id,
      revision.dismissed_suggestion_job_id,
      revision.dismissed_suggestion_sha256,
    ];
    const acceptedPresent = accepted.filter((item) => item !== null).length;
    const dismissedPresent = dismissed.filter((item) => item !== null).length;
    if (
      ![0, 3].includes(acceptedPresent) ||
      ![0, 3].includes(dismissedPresent) ||
      (acceptedPresent === 3 && dismissedPresent === 3) ||
      (acceptedPresent === 0) !==
        (revision.accepted_suggestion_kind === null) ||
      (dismissedPresent === 0) !== (revision.dismissed_suggestion_kind === null)
    ) {
      throw new Error(`${label} suggestion decision authority is invalid.`);
    }
    for (const [kind, values, decisionLabel] of [
      [revision.accepted_suggestion_kind, accepted, "accepted"],
      [revision.dismissed_suggestion_kind, dismissed, "dismissed"],
    ] as const) {
      if (values[0] === null) continue;
      oneOf(
        kind,
        ["detector_candidate", "propagation"] as const,
        `${label} ${decisionLabel} suggestion kind`,
      );
      safeId(values[0], `${label} ${decisionLabel} suggestion ID`);
      safeId(values[1], `${label} ${decisionLabel} suggestion job ID`);
      sha256(values[2], `${label} ${decisionLabel} suggestion SHA-256`);
    }
    let nextEffective = effective;
    if (operation === "delete") {
      if (effective !== null || acceptedPresent || dismissedPresent) {
        throw new Error(`${label} delete authority is invalid.`);
      }
    } else if (operation === "undo") {
      const undoRevision = integer(
        revision.undo_revision,
        `${label} undo revision`,
        1,
      );
      const undone = revisionsByNumber.get(frameIndex)?.get(undoRevision);
      if (undoRevision !== revisionNumber - 1 || !undone) {
        throw new Error(`${label} undo authority is invalid.`);
      }
      const restored = undone.previous_effective_annotation;
      const restoredDigest =
        restored === null
          ? null
          : pythonCanonicalSha256Sync(
              restored,
              annotationAuthorityFloatPaths(),
            );
      const effectiveDigest =
        effective === null
          ? null
          : pythonCanonicalSha256Sync(
              effective,
              annotationAuthorityFloatPaths(),
            );
      if (
        restoredDigest !== effectiveDigest ||
        acceptedPresent ||
        dismissedPresent
      ) {
        throw new Error(`${label} undo transition is invalid.`);
      }
      nextEffective = restored as Record<string, unknown> | null;
    }
    const mutationRequest = {
      mutation_id: mutationId,
      expected_revision: expectedRevision,
      operation,
      undo_revision: revision.undo_revision,
      annotation: operation === "set" ? effective : null,
      suggestion_kind: revision.accepted_suggestion_kind,
      suggestion_id: revision.accepted_suggestion_id,
      accepted_suggestion_job_id: revision.accepted_suggestion_job_id,
      accepted_suggestion_sha256: revision.accepted_suggestion_sha256,
      dismissed_suggestion_kind: revision.dismissed_suggestion_kind,
      dismissed_suggestion_id: revision.dismissed_suggestion_id,
      dismissed_suggestion_job_id: revision.dismissed_suggestion_job_id,
      dismissed_suggestion_sha256: revision.dismissed_suggestion_sha256,
    };
    const mutationAuthority = {
      session_id: session.view.sessionId,
      frame_index: frameIndex,
      request: mutationRequest,
    };
    if (
      revision.mutation_sha256 !==
      pythonCanonicalSha256Sync(
        mutationAuthority,
        annotationAuthorityFloatPaths("$.request.annotation"),
      )
    ) {
      throw new Error(`${label} mutation digest is invalid.`);
    }
    const expectedRevisionId = `revision-${authorityCanonicalSha256(
      {
        session_id: session.view.sessionId,
        frame_index: frameIndex,
        revision: revisionNumber,
      },
      `${label} identity`,
    ).slice(0, 24)}`;
    const expectedEtag = pythonCanonicalSha256Sync(
      {
        schema_version: "1.0",
        artifact_type: "ball_annotation_effective_revision",
        session_id: session.view.sessionId,
        frame_index: frameIndex,
        revision: revisionNumber,
        effective_annotation: nextEffective,
      },
      annotationAuthorityFloatPaths("$.effective_annotation"),
    );
    if (
      revision.revision_id !== expectedRevisionId ||
      revision.annotation_etag !== expectedEtag
    ) {
      throw new Error(`${label} identity or annotation ETag is invalid.`);
    }
    if (revisionsById.has(expectedRevisionId)) {
      throw new Error(`${label} identity is duplicated.`);
    }
    stringValue(revision.created_at, `${label} creation time`);
    const rows = revisionsByFrame.get(frameIndex) ?? [];
    if (rows.length + 1 !== revisionNumber) {
      throw new Error(`${label} is not canonically ordered by revision.`);
    }
    rows.push(revision);
    revisionsByFrame.set(frameIndex, rows);
    effectiveByFrame.set(frameIndex, nextEffective);
    const byNumber = revisionsByNumber.get(frameIndex) ?? new Map();
    byNumber.set(revisionNumber, revision);
    revisionsByNumber.set(frameIndex, byNumber);
    revisionsById.set(expectedRevisionId, revision);
  });
  return { revisions, revisionsByFrame, revisionsById, effectiveByFrame };
}

function parseImmutableFrameMedia(
  value: unknown,
  packageSource: Record<string, unknown>,
) {
  const media = boundedCollection(value, "Frame media", 1, 70).map(
    (rawMedia, index) => {
      const label = `Frame media ${index}`;
      const row = record(rawMedia, label);
      exactKeys(
        row,
        [
          "frame_index",
          "relative_path",
          "sha256",
          "size_bytes",
          "media_type",
          "width",
          "height",
        ],
        label,
      );
      const frameIndex = integer(row.frame_index, `${label} frame index`);
      if (
        row.relative_path !==
          `frames/${frameIndex.toString().padStart(9, "0")}.jpg` ||
        row.media_type !== "image/jpeg" ||
        integer(row.width, `${label} width`, 1) !== packageSource.width ||
        integer(row.height, `${label} height`, 1) !== packageSource.height
      ) {
        throw new Error(`${label} source binding is invalid.`);
      }
      return {
        raw: row,
        frameIndex,
        sha256: sha256(row.sha256, `${label} SHA-256`),
        sizeBytes: integer(row.size_bytes, `${label} size`, 1),
      };
    },
  );
  if (
    media.some(
      (row, index) =>
        index > 0 && row.frameIndex <= media[index - 1].frameIndex,
    )
  ) {
    throw new Error("Frame media is not canonically ordered.");
  }
  return media;
}

const ANNOTATION_AUTHORITY_FLOAT_PATHS = [
  "$.point_source_px.x",
  "$.point_source_px.y",
  "$.bbox_source_px.left",
  "$.bbox_source_px.top",
  "$.bbox_source_px.right",
  "$.bbox_source_px.bottom",
] as const;

function annotationAuthorityFloatPaths(root = "$") {
  return ANNOTATION_AUTHORITY_FLOAT_PATHS.map((path) =>
    path.replace("$", root),
  );
}

function revisionChainFloatPaths(
  revisions: readonly Record<string, unknown>[],
  root = "$",
) {
  return revisions.flatMap((_, index) => [
    ...annotationAuthorityFloatPaths(
      `${root}[${index}].previous_effective_annotation`,
    ),
    ...annotationAuthorityFloatPaths(`${root}[${index}].effective_annotation`),
  ]);
}

function timingBindingFloatPaths(timing: Record<string, unknown>, root = "$") {
  const paths = [
    `${root}.fps`,
    `${root}.display_time_seconds`,
    `${root}.true_presentation_timestamp.value_seconds`,
  ];
  if (timing.decoder_reported_pos_msec !== null) {
    paths.push(`${root}.decoder_reported_pos_msec`);
  }
  if (timing.decoder_time_seconds !== null) {
    paths.push(`${root}.decoder_time_seconds`);
  }
  const cross = timing.cross_decode_verification;
  if (cross && typeof cross === "object") {
    paths.push(`${root}.cross_decode_verification.tolerance_msec`);
    const observations = (cross as Record<string, unknown>).observations;
    if (Array.isArray(observations)) {
      observations.forEach((_, index) => {
        paths.push(
          `${root}.cross_decode_verification.observations[${index}].decoder_reported_pos_msec`,
        );
      });
    }
  }
  return paths;
}

function frameEvidenceFloatPaths(row: Record<string, unknown>, root = "$") {
  const timing = record(row.timing_binding, "Frame timing binding");
  const paths = timingBindingFloatPaths(timing, `${root}.timing_binding`);
  if (row.proxy_binding !== null) {
    paths.push(
      ...REVIEW_PROXY_FLOAT_PATHS.map((path) =>
        path.replace("$", `${root}.proxy_binding`),
      ),
    );
  }
  return paths;
}

function parseSealedFrameEvidence(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageSource: Record<string, unknown>;
    packageLineage: ReturnType<typeof parseLineage>;
    packageManifest: Record<string, unknown>;
    detectorAuthorities: ReturnType<typeof parseDetectorProbeAuthorities>;
    media: ReturnType<typeof parseImmutableFrameMedia>;
    revisions: ReturnType<typeof parseSealedRevisionChain>;
    rawAnnotations: Record<number, Record<string, unknown>>;
    supplementalFrameIndices: number[];
  },
) {
  const evidence = boundedCollection(value, "Frame evidence", 1, 70).map(
    (rawEvidence, index) => {
      const label = `Frame evidence ${index}`;
      const row = record(rawEvidence, label);
      exactKeys(
        row,
        [
          "schema_version",
          "artifact_type",
          "frame_index",
          "frame_role",
          "source",
          "source_frame_jpeg",
          "temporal_group",
          "probe_evidence",
          "timing_binding",
          "proxy_binding",
          "propagation_evidence",
          "effective_revision",
          "effective_annotation_sha256",
          "revision_chain_sha256",
          "frame_evidence_sha256",
        ],
        label,
      );
      const frameIndex = integer(row.frame_index, `${label} frame index`);
      const frameRole = oneOf(
        row.frame_role,
        ["primary", "supplemental"] as const,
        `${label} role`,
      );
      const source = record(row.source, `${label} source`);
      exactKeys(source, ["sha256", "width", "height"], `${label} source`);
      const jpeg = record(row.source_frame_jpeg, `${label} JPEG`);
      exactKeys(jpeg, ["sha256", "size_bytes", "media_type"], `${label} JPEG`);
      const media = context.media[index];
      const sourceFrameSha256 = sha256(
        jpeg.sha256,
        `${label} source frame SHA-256`,
      );
      const sourceFrameSize = integer(
        jpeg.size_bytes,
        `${label} source frame size`,
        1,
      );
      if (
        row.schema_version !== "1.0" ||
        row.artifact_type !== "ball_sealed_frame_evidence" ||
        source.sha256 !== context.packageSource.sha256 ||
        source.width !== context.packageSource.width ||
        source.height !== context.packageSource.height ||
        jpeg.media_type !== "image/jpeg" ||
        media?.frameIndex !== frameIndex ||
        media.sha256 !== sourceFrameSha256 ||
        media.sizeBytes !== sourceFrameSize ||
        (frameRole === "supplemental") !==
          context.supplementalFrameIndices.includes(frameIndex)
      ) {
        throw new Error(`${label} media/source authority is invalid.`);
      }
      const probe = record(row.probe_evidence, `${label} probe evidence`);
      exactKeys(
        probe,
        [
          "schema_version",
          "artifact_type",
          "probe_job_id",
          "probe_report_sha256",
          "probe_result_manifest_sha256",
          "artifact_id",
          "artifact_sha256",
          "artifact_size_bytes",
          "artifact_media_type",
          "binding_sha256",
        ],
        `${label} probe evidence`,
      );
      const probeJobId = safeId(probe.probe_job_id, `${label} probe job ID`);
      const detectorAuthority =
        context.detectorAuthorities.byJob.get(probeJobId);
      const probeReportSha256 = sha256(
        probe.probe_report_sha256,
        `${label} probe report SHA-256`,
      );
      const probeResultManifestSha256 = sha256(
        probe.probe_result_manifest_sha256,
        `${label} probe result manifest SHA-256`,
      );
      const propagation =
        row.propagation_evidence === null
          ? null
          : record(row.propagation_evidence, `${label} propagation evidence`);
      if (propagation !== null) {
        exactKeys(
          propagation,
          [
            "schema_version",
            "artifact_type",
            "propagation_job_id",
            "propagation_report_sha256",
            "neighbor_probe_job_id",
            "neighbor_probe_report_sha256",
            "neighbor_probe_result_manifest_sha256",
            "neighbor_artifact_id",
            "neighbor_artifact_sha256",
            "neighbor_artifact_size_bytes",
            "propagation_intent_sha256",
            "seed_binding_sha256",
            "tracker_profile_sha256",
            "propagation_frame_result_sha256",
            "suggestion_id",
            "suggestion_sha256",
            "temporal_group_derivative_binding_sha256",
            "binding_sha256",
          ],
          `${label} propagation evidence`,
        );
      }
      if (
        probe.schema_version !== "1.0" ||
        probe.artifact_type !== "ball_source_frame_probe_evidence" ||
        (frameRole === "primary"
          ? !detectorAuthority ||
            propagation !== null ||
            probeReportSha256 !== detectorAuthority.reportSha256 ||
            probeResultManifestSha256 !== detectorAuthority.resultManifestSha256
          : propagation === null ||
            propagation.schema_version !== "1.0" ||
            propagation.artifact_type !==
              "ball_supplemental_propagation_evidence" ||
            propagation.neighbor_probe_job_id !== probeJobId ||
            propagation.neighbor_probe_report_sha256 !== probeReportSha256 ||
            propagation.neighbor_probe_result_manifest_sha256 !==
              probeResultManifestSha256 ||
            propagation.neighbor_artifact_id !== probe.artifact_id ||
            propagation.neighbor_artifact_sha256 !== sourceFrameSha256 ||
            propagation.neighbor_artifact_size_bytes !== sourceFrameSize) ||
        probe.artifact_sha256 !== sourceFrameSha256 ||
        probe.artifact_size_bytes !== sourceFrameSize ||
        probe.artifact_media_type !== "image/jpeg"
      ) {
        throw new Error(`${label} probe authority is invalid.`);
      }
      safeId(probe.artifact_id, `${label} probe artifact ID`);
      sha256(probe.binding_sha256, `${label} probe binding SHA-256`);
      const timing = record(row.timing_binding, `${label} timing binding`);
      exactKeys(
        timing,
        [
          "schema_version",
          "artifact_type",
          "timing_profile_id",
          "timing_status",
          "source_sha256",
          "runtime_environment_sha256",
          "source_frame_jpeg_sha256",
          "frame_index",
          "decoded_frame_position",
          "fps",
          "effective_decode_mode",
          "decoder_reported_pos_msec",
          "decoder_time_seconds",
          "decoder_timing_observation_method",
          "display_time_seconds",
          "display_time_derivation",
          "true_presentation_timestamp",
          "position_verification",
          "cross_decode_verification",
          "timing_binding_sha256",
        ],
        `${label} timing binding`,
      );
      const timingStatus = oneOf(
        timing.timing_status,
        ["observed", "not_collected"] as const,
        `${label} timing status`,
      );
      const effectiveDecodeMode = oneOf(
        timing.effective_decode_mode,
        [
          "sequential",
          "preroll_verified",
          "direct_verified",
          "sequential_fallback",
        ] as const,
        `${label} effective decode mode`,
      );
      const decoderReportedPosMsec =
        timing.decoder_reported_pos_msec === null
          ? null
          : finiteNumber(
              timing.decoder_reported_pos_msec,
              `${label} decoder position`,
              Number.NEGATIVE_INFINITY,
            );
      const timingFps = finiteNumber(
        timing.fps,
        `${label} timing FPS`,
        Number.MIN_VALUE,
      );
      const decoderTimeSeconds =
        timing.decoder_time_seconds === null
          ? null
          : finiteNumber(
              timing.decoder_time_seconds,
              `${label} decoder time`,
              Number.NEGATIVE_INFINITY,
            );
      const displayTimeSeconds = finiteNumber(
        timing.display_time_seconds,
        `${label} display time`,
        0,
      );
      parseTruePresentationTimestamp(
        timing.true_presentation_timestamp,
        `${label} presentation timestamp`,
      );
      const crossDecode =
        timing.cross_decode_verification === null
          ? null
          : record(
              timing.cross_decode_verification,
              `${label} cross-decode verification`,
            );
      if (crossDecode !== null) {
        exactKeys(
          crossDecode,
          ["method", "tolerance_msec", "observations", "verification_sha256"],
          `${label} cross-decode verification`,
        );
        const toleranceMsec = finiteNumber(
          crossDecode.tolerance_msec,
          `${label} cross-decode tolerance`,
        );
        if (
          crossDecode.method !==
            "decoder_pos_msec_and_frame_digest_agreement_v1" ||
          toleranceMsec > 1_000 ||
          !Array.isArray(crossDecode.observations) ||
          crossDecode.observations.length < 2 ||
          crossDecode.observations.length > 4
        ) {
          throw new Error(`${label} cross-decode authority is invalid.`);
        }
        const observationModes = crossDecode.observations.map(
          (rawObservation, observationIndex) => {
            const observation = record(
              rawObservation,
              `${label} cross-decode observation ${observationIndex}`,
            );
            exactKeys(
              observation,
              [
                "effective_decode_mode",
                "decoded_frame_position",
                "decoder_reported_pos_msec",
                "source_frame_jpeg_sha256",
              ],
              `${label} cross-decode observation ${observationIndex}`,
            );
            const mode = oneOf(
              observation.effective_decode_mode,
              [
                "sequential",
                "preroll_verified",
                "direct_verified",
                "sequential_fallback",
              ] as const,
              `${label} cross-decode observation mode`,
            );
            const positionMsec = finiteNumber(
              observation.decoder_reported_pos_msec,
              `${label} cross-decode decoder position`,
              Number.NEGATIVE_INFINITY,
            );
            if (
              Math.abs(positionMsec) > 7 * 24 * 60 * 60 * 1_000 ||
              observation.decoded_frame_position !== frameIndex ||
              observation.source_frame_jpeg_sha256 !== sourceFrameSha256 ||
              decoderReportedPosMsec === null ||
              Math.abs(positionMsec - decoderReportedPosMsec) > toleranceMsec
            ) {
              throw new Error(
                `${label} cross-decode observations do not bind the frame.`,
              );
            }
            return mode;
          },
        );
        if (
          new Set(observationModes).size !== observationModes.length ||
          !sameOrderedValues(
            observationModes,
            [...observationModes].sort((left, right) =>
              left.localeCompare(right),
            ),
          ) ||
          !observationModes.includes(effectiveDecodeMode)
        ) {
          throw new Error(`${label} cross-decode modes are invalid.`);
        }
        const verificationBody = { ...crossDecode };
        delete verificationBody.verification_sha256;
        if (
          sha256(
            crossDecode.verification_sha256,
            `${label} cross-decode SHA-256`,
          ) !== crossDecode.verification_sha256 ||
          pythonCanonicalSha256Sync(
            verificationBody,
            timingBindingFloatPaths(
              { cross_decode_verification: crossDecode },
              "$",
            )
              .filter((path) => path.startsWith("$.cross_decode_verification."))
              .map((path) => path.replace("$.cross_decode_verification", "$")),
          ) !== crossDecode.verification_sha256
        ) {
          throw new Error(`${label} cross-decode digest is invalid.`);
        }
      }
      const timingBody = { ...timing };
      delete timingBody.timing_binding_sha256;
      if (
        timing.schema_version !== "1.0" ||
        timing.artifact_type !== "ball_source_frame_timing_binding" ||
        timing.timing_profile_id !==
          (timingStatus === "observed"
            ? "verified_decoder_pos_msec_after_frame_position_v1"
            : "source_pos_msec_not_collected_proxy_cfr_verified_v1") ||
        timing.frame_index !== frameIndex ||
        timing.decoded_frame_position !== frameIndex ||
        timing.source_sha256 !== context.packageSource.sha256 ||
        timing.source_frame_jpeg_sha256 !== sourceFrameSha256 ||
        timing.runtime_environment_sha256 !==
          context.packageLineage.runtimeEnvironmentSha256 ||
        effectiveDecodeMode !==
          context.packageLineage.decode.effective_decode_mode ||
        timingFps !== context.packageSource.fps ||
        decoderTimeSeconds !==
          (decoderReportedPosMsec === null
            ? null
            : decoderReportedPosMsec / 1_000) ||
        displayTimeSeconds !== frameIndex / timingFps ||
        timing.display_time_derivation !==
          "frame_index_divided_by_fps_for_display_only_not_source_pts" ||
        timing.position_verification !==
          (timingStatus === "observed"
            ? "opencv_next_frame_index_with_0.25_tolerance"
            : "verified_review_proxy_frame_index_mapping_v1") ||
        timing.decoder_timing_observation_method !==
          (timingStatus === "observed"
            ? "opencv_cap_prop_pos_msec_after_verified_frame_read"
            : null) ||
        (timingStatus === "not_collected") !==
          (decoderReportedPosMsec === null) ||
        (timingStatus === "not_collected" && crossDecode !== null) ||
        pythonCanonicalSha256Sync(
          timingBody,
          timingBindingFloatPaths(timing),
        ) !== timing.timing_binding_sha256
      ) {
        throw new Error(`${label} timing authority is invalid.`);
      }
      const parsedProxyBinding = parseProxyBinding(row.proxy_binding, {
        frameIndex,
        sourceFrameSha256,
        sourceTimingStatus: timingStatus,
        decoderReportedPosMsec,
      });
      const revisions = context.revisions.revisionsByFrame.get(frameIndex);
      const effectiveRevision = integer(
        row.effective_revision,
        `${label} effective revision`,
        1,
      );
      const rawAnnotation = context.rawAnnotations[frameIndex];
      const finalRevisionAnnotation = revisions?.at(-1)
        ?.effective_annotation as Record<string, unknown> | null | undefined;
      const finalRevisionDigest =
        finalRevisionAnnotation == null
          ? null
          : pythonCanonicalSha256Sync(
              finalRevisionAnnotation,
              ANNOTATION_AUTHORITY_FLOAT_PATHS,
            );
      const rawAnnotationPayload = rawAnnotation
        ? Object.fromEntries(
            Object.entries(rawAnnotation).filter(
              ([key]) => key !== "frame_index",
            ),
          )
        : null;
      const rawAnnotationDigest = rawAnnotationPayload
        ? pythonCanonicalSha256Sync(
            rawAnnotationPayload,
            ANNOTATION_AUTHORITY_FLOAT_PATHS,
          )
        : null;
      if (
        !revisions ||
        revisions.length !== effectiveRevision ||
        !rawAnnotation ||
        finalRevisionDigest !== rawAnnotationDigest ||
        pythonCanonicalSha256Sync(
          rawAnnotation,
          ANNOTATION_AUTHORITY_FLOAT_PATHS,
        ) !== row.effective_annotation_sha256
      ) {
        throw new Error(`${label} effective revision authority is invalid.`);
      }
      if (
        pythonCanonicalSha256Sync(
          revisions,
          revisionChainFloatPaths(revisions),
        ) !== row.revision_chain_sha256
      ) {
        throw new Error(`${label} revision chain digest is invalid.`);
      }
      const frameEvidenceBody = { ...row };
      delete frameEvidenceBody.frame_evidence_sha256;
      if (
        pythonCanonicalSha256Sync(
          frameEvidenceBody,
          frameEvidenceFloatPaths(frameEvidenceBody),
        ) !== row.frame_evidence_sha256
      ) {
        throw new Error(`${label} frame evidence digest is invalid.`);
      }
      return {
        raw: row,
        frameIndex,
        frameRole,
        probeJobId,
        probeReportSha256,
        probeResultManifestSha256,
        proxyBinding: parsedProxyBinding,
      };
    },
  );
  if (
    evidence.some(
      (row, index) =>
        index > 0 && row.frameIndex <= evidence[index - 1].frameIndex,
    )
  ) {
    throw new Error("Frame evidence is not canonically ordered.");
  }
  return evidence;
}

function reviewProxyManifestFloatPaths(mappingCount: number, root = "$") {
  return [
    `${root}.source.fps`,
    `${root}.proxy.fps`,
    `${root}.map_time_tolerance_msec`,
    `${root}.declared_offset_msec`,
    `${root}.coordinate_transform.scale_x`,
    `${root}.coordinate_transform.scale_y`,
    `${root}.coordinate_transform.source_origin[0]`,
    `${root}.coordinate_transform.source_origin[1]`,
    `${root}.coordinate_transform.proxy_origin[0]`,
    `${root}.coordinate_transform.proxy_origin[1]`,
    ...Array.from({ length: mappingCount }, (_, index) => [
      `${root}.mappings[${index}].source_decoder_pos_msec`,
      `${root}.mappings[${index}].proxy_cfr_time_msec`,
    ]).flat(),
  ];
}

function parseFrameReviewProxyAuthority(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageSource: Record<string, unknown>;
    detectorAuthorities: ReturnType<typeof parseDetectorProbeAuthorities>;
    frameEvidence: ReturnType<typeof parseSealedFrameEvidence>;
  },
) {
  const proxyRows = context.frameEvidence.filter(
    (row) => row.proxyBinding !== null,
  );
  if (value === null) {
    if (proxyRows.length > 0) {
      throw new Error("Proxy frame rows lack a frame proxy authority.");
    }
    if (
      context.session.view.dataRole === "check" &&
      context.frameEvidence.some(
        (row) => row.probeJobId !== context.session.view.checkProbeJobId,
      )
    ) {
      throw new Error(
        "Check frame evidence differs from its current probe authority.",
      );
    }
    return null;
  }
  const authority = record(value, "Frame review proxy authority");
  exactKeys(
    authority,
    [
      "probe_job_id",
      "probe_report_sha256",
      "probe_result_manifest_sha256",
      "probe_report",
      "probe_result_manifest",
      "review_proxy_manifest",
      "historical_probe_authority",
    ],
    "Frame review proxy authority",
  );
  const probeJobId = safeId(
    authority.probe_job_id,
    "Frame review proxy job ID",
  );
  const detector = context.detectorAuthorities.byJob.get(probeJobId);
  const reportSha256 = sha256(
    authority.probe_report_sha256,
    "Frame review proxy report SHA-256",
  );
  const resultManifestSha256 = sha256(
    authority.probe_result_manifest_sha256,
    "Frame review proxy result manifest SHA-256",
  );
  if (
    !detector ||
    reportSha256 !== detector.reportSha256 ||
    resultManifestSha256 !== detector.resultManifestSha256 ||
    !sameCanonicalValue(authority.probe_report, detector.report) ||
    !sameCanonicalValue(
      authority.probe_result_manifest,
      detector.resultManifest,
    )
  ) {
    throw new Error("Frame review proxy authority is unknown or copied.");
  }
  const report = record(authority.probe_report, "Frame review proxy report");
  const manifest = record(
    authority.review_proxy_manifest,
    "Frame review proxy manifest",
  );
  exactKeys(
    manifest,
    [
      "schema_version",
      "artifact_type",
      "source",
      "proxy",
      "decoder_fingerprint_sha256",
      "requested_decode_mode",
      "effective_decode_mode",
      "map_time_tolerance_msec",
      "declared_offset_msec",
      "coordinate_transform",
      "expected_frame_indices",
      "mappings",
      "mapping_sha256",
      "integrity_report_sha256",
      "manifest_sha256",
    ],
    "Frame review proxy manifest",
  );
  if (
    report.job_id !== probeJobId ||
    report.report_sha256 !== reportSha256 ||
    !sameCanonicalValue(report.review_proxy_manifest, manifest) ||
    manifest.schema_version !== "1.0" ||
    manifest.artifact_type !== "ball_review_proxy" ||
    !Array.isArray(manifest.expected_frame_indices) ||
    manifest.expected_frame_indices.length < 1 ||
    manifest.expected_frame_indices.length > 50 ||
    !Array.isArray(manifest.mappings) ||
    manifest.mappings.length !== manifest.expected_frame_indices.length
  ) {
    throw new Error("Frame review proxy report/manifest authority is invalid.");
  }
  const manifestSource = record(manifest.source, "Frame proxy source");
  const manifestProxy = record(manifest.proxy, "Frame proxy media");
  if (
    manifestSource.sha256 !== context.packageSource.sha256 ||
    manifestSource.file_identity_sha256 !==
      context.packageSource.file_identity_sha256 ||
    manifestSource.size_bytes !== context.packageSource.size_bytes ||
    manifestSource.width !== context.packageSource.width ||
    manifestSource.height !== context.packageSource.height ||
    manifestSource.frame_count !== context.packageSource.frame_count ||
    manifestSource.fps !== context.packageSource.fps
  ) {
    throw new Error("Frame review proxy source authority is invalid.");
  }
  sha256(
    manifest.decoder_fingerprint_sha256,
    "Frame proxy decoder fingerprint SHA-256",
  );
  const expectedFrameIndices = manifest.expected_frame_indices.map((item) =>
    integer(item, "Frame proxy expected frame index"),
  );
  if (
    new Set(expectedFrameIndices).size !== expectedFrameIndices.length ||
    !sameOrderedValues(
      expectedFrameIndices,
      [...expectedFrameIndices].sort((left, right) => left - right),
    ) ||
    !sameOrderedValues(
      expectedFrameIndices,
      proxyRows.map((row) => row.frameIndex),
    )
  ) {
    throw new Error("Frame proxy rows differ from current manifest authority.");
  }
  const normalizedMappings = manifest.mappings.map((rawMapping, index) => {
    const label = `Frame proxy mapping ${index}`;
    const mapping = record(rawMapping, label);
    exactKeys(
      mapping,
      [
        "source_frame_index",
        "source_timing_status",
        "source_decoder_pos_msec",
        "proxy_frame_index",
        "proxy_timing_basis",
        "proxy_cfr_time_msec",
        "source_frame_sha256",
        "proxy_frame_sha256",
        "media_integrity",
      ],
      label,
    );
    const frameIndex = integer(
      mapping.source_frame_index,
      `${label} source frame`,
    );
    const proxyFrameIndex = integer(
      mapping.proxy_frame_index,
      `${label} proxy frame`,
    );
    const timingStatus = oneOf(
      mapping.source_timing_status,
      ["observed", "not_collected"] as const,
      `${label} source timing status`,
    );
    const sourcePosition =
      mapping.source_decoder_pos_msec === null
        ? null
        : finiteNumber(
            mapping.source_decoder_pos_msec,
            `${label} source position`,
            Number.NEGATIVE_INFINITY,
          );
    const proxyPosition = finiteNumber(
      mapping.proxy_cfr_time_msec,
      `${label} proxy CFR position`,
      Number.NEGATIVE_INFINITY,
    );
    const integrity = record(mapping.media_integrity, `${label} integrity`);
    exactKeys(
      integrity,
      ["status", "gray", "low_information", "likely_corrupt"],
      `${label} integrity`,
    );
    const evidenceRow = proxyRows[index];
    const rawBinding = record(
      evidenceRow?.raw.proxy_binding,
      `${label} frame binding`,
    );
    const sourceFrame = record(
      rawBinding.source_frame,
      `${label} source binding`,
    );
    const proxyFrame = record(rawBinding.proxy_frame, `${label} proxy binding`);
    const proxyMedia = record(rawBinding.proxy, `${label} proxy media`);
    if (
      frameIndex !== expectedFrameIndices[index] ||
      proxyFrameIndex !== frameIndex ||
      (timingStatus === "not_collected") !== (sourcePosition === null) ||
      mapping.proxy_timing_basis !== "verified_cfr_frame_index_time_v1" ||
      integrity.status !== "ok" ||
      integrity.gray !== false ||
      integrity.low_information !== false ||
      integrity.likely_corrupt !== false ||
      evidenceRow?.probeJobId !== probeJobId ||
      evidenceRow.probeReportSha256 !== reportSha256 ||
      evidenceRow.probeResultManifestSha256 !== resultManifestSha256 ||
      rawBinding.map_sha256 !== manifest.mapping_sha256 ||
      !sameCanonicalValue(proxyMedia, {
        sha256: manifestProxy.sha256,
        size_bytes: manifestProxy.size_bytes,
        width: manifestProxy.width,
        height: manifestProxy.height,
      }) ||
      sourceFrame.frame_index !== frameIndex ||
      sourceFrame.timing_status !== timingStatus ||
      sourceFrame.decoder_reported_pos_msec !== sourcePosition ||
      sourceFrame.sha256 !== mapping.source_frame_sha256 ||
      proxyFrame.frame_index !== frameIndex ||
      proxyFrame.timing_basis !== mapping.proxy_timing_basis ||
      proxyFrame.cfr_time_msec !== proxyPosition ||
      proxyFrame.sha256 !== mapping.proxy_frame_sha256
    ) {
      throw new Error(`${label} differs from current frame authority.`);
    }
    sha256(mapping.source_frame_sha256, `${label} source SHA-256`);
    sha256(mapping.proxy_frame_sha256, `${label} proxy SHA-256`);
    return mapping;
  });
  const mappingFloatPaths = normalizedMappings.flatMap((_, index) => [
    `$[${index}].source_decoder_pos_msec`,
    `$[${index}].proxy_cfr_time_msec`,
  ]);
  const mappingSha256 = sha256(
    manifest.mapping_sha256,
    "Frame proxy mapping SHA-256",
  );
  const integrityReport = normalizedMappings.map((mapping) => ({
    frame_index: mapping.source_frame_index,
    ...record(mapping.media_integrity, "Frame proxy integrity"),
  }));
  const manifestSha256 = sha256(
    manifest.manifest_sha256,
    "Frame proxy manifest SHA-256",
  );
  const manifestBody = { ...manifest };
  delete manifestBody.manifest_sha256;
  if (
    pythonCanonicalSha256Sync(normalizedMappings, mappingFloatPaths) !==
      mappingSha256 ||
    authorityCanonicalSha256(
      integrityReport,
      "Frame proxy integrity report",
    ) !== manifest.integrity_report_sha256 ||
    pythonCanonicalSha256Sync(
      manifestBody,
      reviewProxyManifestFloatPaths(normalizedMappings.length),
    ) !== manifestSha256
  ) {
    throw new Error("Frame review proxy manifest digest is invalid.");
  }
  const historical = authority.historical_probe_authority;
  const upgrade = record(
    report.lineage,
    "Frame proxy report lineage",
  ).review_proxy_upgrade;
  if ((upgrade == null) !== (historical === null)) {
    throw new Error("Frame proxy historical authority shape is invalid.");
  }
  if (historical !== null) {
    const history = record(historical, "Historical probe authority");
    exactKeys(
      history,
      [
        "probe_job_id",
        "probe_report_sha256",
        "probe_result_manifest_sha256",
        "probe_report",
        "probe_result_manifest",
        "source_frame_evidence_sha256",
        "candidate_evidence_sha256",
      ],
      "Historical probe authority",
    );
    const historicalJobId = safeId(
      history.probe_job_id,
      "Historical probe job ID",
    );
    const historicalDetector =
      context.detectorAuthorities.byJob.get(historicalJobId);
    if (
      !historicalDetector ||
      history.probe_report_sha256 !== historicalDetector.reportSha256 ||
      history.probe_result_manifest_sha256 !==
        historicalDetector.resultManifestSha256 ||
      !sameCanonicalValue(history.probe_report, historicalDetector.report) ||
      !sameCanonicalValue(
        history.probe_result_manifest,
        historicalDetector.resultManifest,
      )
    ) {
      throw new Error("Historical frame proxy authority is unknown or copied.");
    }
    sha256(
      history.source_frame_evidence_sha256,
      "Historical source evidence SHA-256",
    );
    sha256(
      history.candidate_evidence_sha256,
      "Historical candidate evidence SHA-256",
    );
  }
  return { probeJobId, manifest };
}

const DETECTOR_CANDIDATE_KEYS = [
  "frame_index",
  "candidate_origin",
  "review_media",
  "candidate",
  "candidate_sha256",
  "decision",
] as const;
const DETECTOR_CANDIDATE_AUTHORITY_KEYS = [
  "candidate_id",
  "profile_id",
  "rank",
  "bbox_source_px",
  "confidence",
  "annotation_state",
  "training_use",
  "truth_status",
  "suggestion_job_id",
  "suggestion_sha256",
] as const;

function detectorCandidateFloatPaths(root: string) {
  return [
    `${root}.candidate.bbox_source_px.left`,
    `${root}.candidate.bbox_source_px.top`,
    `${root}.candidate.bbox_source_px.right`,
    `${root}.candidate.bbox_source_px.bottom`,
    `${root}.candidate.confidence`,
  ];
}

function detectorProfileResultFloatPaths(value: unknown, root: string) {
  if (!Array.isArray(value)) return [];
  const paths: string[] = [];
  value.forEach((rawProfile, profileIndex) => {
    if (!rawProfile || typeof rawProfile !== "object") return;
    const profile = rawProfile as Record<string, unknown>;
    if (profile.latency_ms !== null && profile.latency_ms !== undefined) {
      paths.push(`${root}[${profileIndex}].latency_ms`);
    }
    for (const field of ["display_candidate", "raw_candidates"] as const) {
      const candidates =
        field === "display_candidate"
          ? profile[field] === null || profile[field] === undefined
            ? []
            : [profile[field]]
          : Array.isArray(profile[field])
            ? profile[field]
            : [];
      candidates.forEach((rawCandidate, candidateIndex) => {
        if (!rawCandidate || typeof rawCandidate !== "object") return;
        const candidate = rawCandidate as Record<string, unknown>;
        const candidateRoot =
          field === "display_candidate"
            ? `${root}[${profileIndex}].display_candidate`
            : `${root}[${profileIndex}].raw_candidates[${candidateIndex}]`;
        if (Array.isArray(candidate.bbox_source_px)) {
          candidate.bbox_source_px.forEach((_, coordinateIndex) =>
            paths.push(`${candidateRoot}.bbox_source_px[${coordinateIndex}]`),
          );
        }
        paths.push(`${candidateRoot}.confidence`);
      });
    }
  });
  return paths;
}

function detectorCandidateEvidenceSha256(report: Record<string, unknown>) {
  const frames = boundedCollection(report.frames, "Detector report frames", 1);
  const payload = frames.map((rawFrame, index) => {
    const frame = record(rawFrame, `Detector report frame ${index}`);
    return {
      frame_index: frame.frame_index,
      profile_results: frame.profile_results,
    };
  });
  return pythonCanonicalSha256Sync(
    payload,
    payload.flatMap((row, index) =>
      detectorProfileResultFloatPaths(
        row.profile_results,
        `$[${index}].profile_results`,
      ),
    ),
  );
}

function expectedDetectorCandidates(
  authority: ReturnType<
    typeof parseDetectorProbeAuthorities
  >["authorities"][number],
  frameIndex: number,
  artifactId: string,
  profile: Record<string, unknown>,
  source: Record<string, unknown>,
) {
  const frames = boundedCollection(
    authority.report.frames,
    "Detector frames",
    1,
  );
  const matchingFrames = frames
    .map((frame, index) => record(frame, `Detector frame ${index}`))
    .filter((frame) => frame.frame_index === frameIndex);
  const artifacts = boundedCollection(
    authority.report.artifacts,
    "Detector artifacts",
    1,
  )
    .map((artifact, index) => record(artifact, `Detector artifact ${index}`))
    .filter((artifact) => artifact.artifact_id === artifactId);
  if (matchingFrames.length !== 1 || artifacts.length !== 1) {
    throw new Error("Detector candidate frame artifact authority is invalid.");
  }
  const frame = matchingFrames[0];
  const artifact = artifacts[0];
  if (
    frame.source_artifact_url !==
      `/api/v1/detector-probes/${authority.jobId}/artifacts/${artifactId}` ||
    artifact.media_type !== "image/jpeg" ||
    artifact.sha256 !== frame.source_frame_sha256 ||
    artifact.size_bytes !== frame.source_frame_size_bytes ||
    artifact.width !== frame.source_width ||
    artifact.height !== frame.source_height
  ) {
    throw new Error("Detector candidate source artifact authority is invalid.");
  }
  const profileResults = boundedCollection(
    frame.profile_results,
    "Detector profile results",
    1,
  )
    .map((result, index) => record(result, `Detector profile result ${index}`))
    .filter((result) => result.profile_id === profile.profile_id);
  if (
    profileResults.length !== 1 ||
    profileResults[0].profile_sha256 !== profile.profile_sha256
  ) {
    throw new Error("Detector locked-profile candidate authority is missing.");
  }
  const result = profileResults[0];
  const rawCandidates = Array.isArray(result.raw_candidates)
    ? result.raw_candidates
    : null;
  const filterReasons = record(
    result.filter_reasons,
    "Detector candidate filter reasons",
  );
  const candidateCount = integer(
    result.candidate_count,
    "Detector candidate count",
  );
  const duplicateCount = integer(
    filterReasons.duplicate_suppressed_iou ?? 0,
    "Detector duplicate count",
  );
  const topKLimited = integer(
    filterReasons.top_k_limit ?? 0,
    "Detector top-k count",
  );
  const deduplicatedCount = candidateCount - duplicateCount;
  if (
    result.status !== "completed" ||
    result.top_k !== 5 ||
    rawCandidates === null ||
    rawCandidates.length > 5 ||
    deduplicatedCount < 0 ||
    rawCandidates.length !== Math.min(deduplicatedCount, 5) ||
    topKLimited !== Math.max(0, deduplicatedCount - 5) ||
    !sameCanonicalValue(result.display_candidate, rawCandidates[0] ?? null)
  ) {
    throw new Error("Detector candidate accounting is invalid.");
  }
  return rawCandidates.map((rawCandidate, index) => {
    const raw = record(rawCandidate, `Detector raw candidate ${index}`);
    if (!Array.isArray(raw.bbox_source_px) || raw.bbox_source_px.length !== 4) {
      throw new Error("Detector raw candidate box is invalid.");
    }
    const coordinates = raw.bbox_source_px.map((coordinate, offset) =>
      finiteNumber(coordinate, `Detector raw candidate coordinate ${offset}`),
    );
    const confidence = finiteNumber(
      raw.confidence,
      "Detector raw candidate confidence",
    );
    const [left, top, right, bottom] = coordinates;
    if (
      confidence > 1 ||
      !(0 <= left && left < right && right <= (source.width as number)) ||
      !(0 <= top && top < bottom && bottom <= (source.height as number))
    ) {
      throw new Error("Detector raw candidate is outside source authority.");
    }
    const rank = index + 1;
    const candidateId = `suggestion-${pythonCanonicalSha256Sync(
      {
        frame_index: frameIndex,
        profile_id: profile.profile_id,
        rank,
        box: coordinates,
        confidence,
      },
      ["$.box[0]", "$.box[1]", "$.box[2]", "$.box[3]", "$.confidence"],
    ).slice(0, 24)}`;
    const core = {
      candidate_id: candidateId,
      profile_id: profile.profile_id,
      rank,
      bbox_source_px: { left, top, right, bottom },
      confidence,
      annotation_state: "suggested",
      training_use: "excluded",
      truth_status: "unconfirmed_suggestion",
    };
    const suggestionSha256 = pythonCanonicalSha256Sync(core, [
      "$.bbox_source_px.left",
      "$.bbox_source_px.top",
      "$.bbox_source_px.right",
      "$.bbox_source_px.bottom",
      "$.confidence",
    ]);
    return {
      ...core,
      suggestion_job_id: authority.jobId,
      suggestion_sha256: suggestionSha256,
    };
  });
}

function parseDetectorCandidateEvidence(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageValue: Record<string, unknown>;
    packageSource: Record<string, unknown>;
    packageProfile: Record<string, unknown>;
    detectorAuthorities: ReturnType<typeof parseDetectorProbeAuthorities>;
    frameEvidence: ReturnType<typeof parseSealedFrameEvidence>;
    revisions: ReturnType<typeof parseSealedRevisionChain>;
  },
) {
  const rows = boundedCollection(value, "Detector candidate evidence", 0);
  const frameEvidenceByIndex = new Map(
    context.frameEvidence.map((row) => [row.frameIndex, row]),
  );
  const candidates = new Map<string, Record<string, unknown>>();
  let previousSortKey: [number, number] | null = null;
  rows.forEach((rawRow, index) => {
    const label = `Detector candidate evidence ${index}`;
    const row = record(rawRow, label);
    exactKeys(row, DETECTOR_CANDIDATE_KEYS, label);
    const frameIndex = integer(row.frame_index, `${label} frame index`);
    if (frameIndex >= context.session.view.source.frameCount) {
      throw new Error(`${label} frame index is outside the source.`);
    }
    const origin = record(row.candidate_origin, `${label} origin`);
    const media = record(row.review_media, `${label} review media`);
    exactKeys(
      origin,
      [
        "probe_job_id",
        "probe_report_sha256",
        "probe_result_manifest_sha256",
        "source_artifact_id",
        "candidate_evidence_sha256",
      ],
      `${label} origin`,
    );
    exactKeys(
      media,
      [
        "probe_job_id",
        "probe_report_sha256",
        "probe_result_manifest_sha256",
        "source_artifact_id",
        "proxy_binding_sha256",
      ],
      `${label} review media`,
    );
    const originJobId = safeId(origin.probe_job_id, `${label} origin job ID`);
    const mediaJobId = safeId(media.probe_job_id, `${label} media job ID`);
    const originAuthority = context.detectorAuthorities.byJob.get(originJobId);
    const mediaAuthority = context.detectorAuthorities.byJob.get(mediaJobId);
    const evidenceRow = frameEvidenceByIndex.get(frameIndex);
    const probe = evidenceRow
      ? record(evidenceRow.raw.probe_evidence, `${label} frame probe`)
      : null;
    const expectedProxySha256 =
      !evidenceRow || evidenceRow.raw.proxy_binding === null
        ? null
        : pythonCanonicalSha256Sync(
            evidenceRow.raw.proxy_binding,
            REVIEW_PROXY_FLOAT_PATHS,
          );
    if (
      !originAuthority ||
      !mediaAuthority ||
      (context.session.view.dataRole === "development" &&
        originAuthority.raw.audit_anchor_kind !== "audited_t2_legacy") ||
      origin.probe_report_sha256 !== originAuthority.reportSha256 ||
      origin.probe_result_manifest_sha256 !==
        originAuthority.resultManifestSha256 ||
      media.probe_report_sha256 !== mediaAuthority.reportSha256 ||
      media.probe_result_manifest_sha256 !==
        mediaAuthority.resultManifestSha256 ||
      origin.candidate_evidence_sha256 !==
        detectorCandidateEvidenceSha256(originAuthority.report) ||
      !evidenceRow ||
      evidenceRow.frameRole !== "primary" ||
      probe?.probe_job_id !== mediaJobId ||
      probe.probe_report_sha256 !== mediaAuthority.reportSha256 ||
      probe.probe_result_manifest_sha256 !==
        mediaAuthority.resultManifestSha256 ||
      probe.artifact_id !== media.source_artifact_id ||
      origin.source_artifact_id !== media.source_artifact_id ||
      media.proxy_binding_sha256 !== expectedProxySha256
    ) {
      throw new Error(`${label} probe/frame authority is invalid.`);
    }
    const proxyAuthority =
      context.packageValue.frame_review_proxy_authority === null
        ? null
        : record(
            context.packageValue.frame_review_proxy_authority,
            "Frame proxy authority",
          );
    const historical =
      proxyAuthority?.historical_probe_authority === null || !proxyAuthority
        ? null
        : record(
            proxyAuthority.historical_probe_authority,
            "Historical detector authority",
          );
    if (
      historical === null
        ? originJobId !== mediaJobId ||
          origin.probe_report_sha256 !== media.probe_report_sha256 ||
          origin.probe_result_manifest_sha256 !==
            media.probe_result_manifest_sha256
        : originJobId !== historical.probe_job_id ||
          origin.probe_report_sha256 !== historical.probe_report_sha256 ||
          origin.probe_result_manifest_sha256 !==
            historical.probe_result_manifest_sha256 ||
          origin.candidate_evidence_sha256 !==
            historical.candidate_evidence_sha256 ||
          mediaJobId !== proxyAuthority?.probe_job_id ||
          media.probe_report_sha256 !== proxyAuthority.probe_report_sha256 ||
          media.probe_result_manifest_sha256 !==
            proxyAuthority.probe_result_manifest_sha256
    ) {
      throw new Error(`${label} detector provenance is invalid.`);
    }
    const candidate = record(row.candidate, `${label} candidate`);
    exactKeys(
      candidate,
      DETECTOR_CANDIDATE_AUTHORITY_KEYS,
      `${label} candidate`,
    );
    const candidateId = safeId(candidate.candidate_id, `${label} candidate ID`);
    const rank = integer(candidate.rank, `${label} rank`, 1);
    if (rank > 5) throw new Error(`${label} rank is invalid.`);
    const expectedCandidates = expectedDetectorCandidates(
      originAuthority,
      frameIndex,
      origin.source_artifact_id as string,
      context.packageProfile,
      context.packageSource,
    );
    const expectedCandidate = expectedCandidates[rank - 1];
    const candidateCore = { ...candidate };
    delete candidateCore.suggestion_job_id;
    delete candidateCore.suggestion_sha256;
    const candidateSha256 = sha256(
      row.candidate_sha256,
      `${label} candidate SHA-256`,
    );
    if (
      !expectedCandidate ||
      !sameCanonicalValue(candidate, expectedCandidate) ||
      candidate.suggestion_job_id !== originJobId ||
      candidate.suggestion_sha256 !== candidateSha256 ||
      pythonCanonicalSha256Sync(candidateCore, [
        "$.bbox_source_px.left",
        "$.bbox_source_px.top",
        "$.bbox_source_px.right",
        "$.bbox_source_px.bottom",
        "$.confidence",
      ]) !== candidateSha256
    ) {
      throw new Error(`${label} candidate authority is invalid.`);
    }
    const sortKey: [number, number] = [frameIndex, rank];
    if (
      previousSortKey &&
      (sortKey[0] < previousSortKey[0] ||
        (sortKey[0] === previousSortKey[0] && sortKey[1] < previousSortKey[1]))
    ) {
      throw new Error(
        "Detector candidate evidence is not canonically ordered.",
      );
    }
    previousSortKey = sortKey;
    const key = `${frameIndex}:${candidateId}`;
    if (candidates.has(key)) {
      throw new Error("Detector candidate identity is duplicated.");
    }
    const decision =
      row.decision === null ? null : record(row.decision, `${label} decision`);
    if (decision !== null) {
      exactKeys(
        decision,
        ["decision", "revision_id", "revision", "operator_id", "decided_at"],
        `${label} decision`,
      );
      const decisionKind = oneOf(
        decision.decision,
        ["accepted_human_annotation", "dismissed_manual_annotation"] as const,
        `${label} decision`,
      );
      const revisionId = safeId(
        decision.revision_id,
        `${label} decision revision ID`,
      );
      const revision = context.revisions.revisionsById.get(revisionId);
      const prefix =
        decisionKind === "accepted_human_annotation" ? "accepted" : "dismissed";
      if (
        !revision ||
        revision.frame_index !== frameIndex ||
        revision.revision !== decision.revision ||
        revision.operator_id !== decision.operator_id ||
        revision.created_at !== decision.decided_at ||
        revision[`${prefix}_suggestion_kind`] !== "detector_candidate" ||
        revision[`${prefix}_suggestion_id`] !== candidateId ||
        revision[`${prefix}_suggestion_job_id`] !== originJobId ||
        revision[`${prefix}_suggestion_sha256`] !== candidateSha256
      ) {
        throw new Error(`${label} decision lacks bound revision authority.`);
      }
    }
    candidates.set(key, row);
  });
  const expectedKeys = new Set<string>();
  for (const evidenceRow of context.frameEvidence) {
    if (evidenceRow.frameRole !== "primary") continue;
    const probe = record(
      evidenceRow.raw.probe_evidence,
      "Primary detector frame probe",
    );
    const proxyAuthority =
      context.packageValue.frame_review_proxy_authority === null
        ? null
        : record(
            context.packageValue.frame_review_proxy_authority,
            "Frame proxy authority",
          );
    const historical =
      evidenceRow.raw.proxy_binding !== null &&
      proxyAuthority?.historical_probe_authority
        ? record(
            proxyAuthority.historical_probe_authority,
            "Historical detector authority",
          )
        : null;
    const originJobId = (historical?.probe_job_id ??
      probe.probe_job_id) as string;
    const authority = context.detectorAuthorities.byJob.get(originJobId);
    if (!authority) throw new Error("Primary frame lacks detector authority.");
    for (const candidate of expectedDetectorCandidates(
      authority,
      evidenceRow.frameIndex,
      probe.artifact_id as string,
      context.packageProfile,
      context.packageSource,
    )) {
      expectedKeys.add(`${evidenceRow.frameIndex}:${candidate.candidate_id}`);
    }
  }
  if (
    candidates.size !== expectedKeys.size ||
    [...expectedKeys].some((key) => !candidates.has(key))
  ) {
    throw new Error("Detector candidate collection is incomplete or invented.");
  }
  const collectionSha256 = sha256(
    context.packageValue.detector_candidate_evidence_sha256,
    "Detector candidate collection SHA-256",
  );
  if (
    pythonCanonicalSha256Sync(
      rows,
      rows.flatMap((_, index) => detectorCandidateFloatPaths(`$[${index}]`)),
    ) !== collectionSha256
  ) {
    throw new Error("Detector candidate collection digest is invalid.");
  }
  return {
    rows,
    pendingCount: rows.filter(
      (row) => record(row, "Detector candidate evidence").decision === null,
    ).length,
  };
}

const PROPAGATION_REPORT_KEYS = [
  "schema_version",
  "artifact_type",
  "job_id",
  "session_id",
  "intent_sha256",
  "mutation_id",
  "seed_frame_index",
  "expected_seed_revision",
  "radius_frames",
  "seed_binding",
  "seed_binding_sha256",
  "target_frame_indices",
  "tracker_profile",
  "tracker_profile_sha256",
  "neighbor_probe_job_id",
  "neighbor_probe_report_sha256",
  "neighbor_probe_result_manifest_sha256",
  "frame_results",
  "suggestions",
  "summary",
  "decision_counts",
  "created_at",
  "updated_at",
  "report_sha256",
] as const;
const PROPAGATION_FRAME_RESULT_KEYS = [
  "frame_index",
  "direction",
  "status",
  "failure_code",
  "source_frame_sha256",
  "suggestion_id",
  "match_score",
  "backward_match_score",
  "forward_backward_error_px",
  "step_displacement_px",
  "pending_human_confirmation",
  "human_confirmation",
  "human_decision",
] as const;
const PROPAGATION_SUGGESTION_KEYS = [
  "suggestion_id",
  "frame_index",
  "temporal_group_id",
  "temporal_group",
  "point_source_px",
  "bbox_source_px",
  "presence",
  "visibility",
  "training_use",
  "annotation_state",
  "provenance",
  "source_frame_sha256",
  "self_check",
  "suggestion_job_id",
  "suggestion_sha256",
  "pending_human_confirmation",
  "human_confirmation",
  "human_decision",
] as const;

function canonicalPropagationRow(row: Record<string, unknown>) {
  const canonical = { ...row };
  for (const field of ["human_confirmation", "human_decision"] as const) {
    if (canonical[field] === null) delete canonical[field];
  }
  return canonical;
}

function canonicalPropagationReport(
  report: Record<string, unknown>,
  includeReportSha256: boolean,
) {
  const canonical = { ...report };
  if (!includeReportSha256) delete canonical.report_sha256;
  canonical.frame_results = boundedCollection(
    report.frame_results,
    "Propagation frame results",
    1,
    4,
  ).map((row, index) =>
    canonicalPropagationRow(record(row, `Propagation frame result ${index}`)),
  );
  canonical.suggestions = boundedCollection(
    report.suggestions,
    "Propagation suggestions",
    0,
    4,
  ).map((row, index) =>
    canonicalPropagationRow(record(row, `Propagation suggestion ${index}`)),
  );
  return canonical;
}

function propagationHumanFloatPaths(value: unknown, root: string) {
  if (!value || typeof value !== "object") return [];
  const audit = value as Record<string, unknown>;
  return audit.center_error_px === undefined
    ? []
    : [
        `${root}.center_error_px`,
        ...(audit.iou === null ? [] : [`${root}.iou`]),
      ];
}

function propagationReportFloatPaths(
  report: Record<string, unknown>,
  root = "$",
) {
  const paths = [
    `${root}.tracker_profile.minimum_match_score`,
    `${root}.tracker_profile.minimum_backward_match_score`,
    `${root}.tracker_profile.maximum_forward_backward_error_px`,
    `${root}.summary.self_check_coverage`,
  ];
  const summary = record(report.summary, "Propagation summary");
  if (summary.human_validated_center_error_px !== null) {
    paths.push(`${root}.summary.human_validated_center_error_px`);
  }
  if (summary.human_validated_iou !== null) {
    paths.push(`${root}.summary.human_validated_iou`);
  }
  (Array.isArray(report.frame_results) ? report.frame_results : [])
    .map(
      (row, index) =>
        [record(row, `Propagation frame result ${index}`), index] as const,
    )
    .forEach(([row, index]) => {
      for (const field of [
        "match_score",
        "backward_match_score",
        "forward_backward_error_px",
        "step_displacement_px",
      ]) {
        if (row[field] !== null) {
          paths.push(`${root}.frame_results[${index}].${field}`);
        }
      }
      paths.push(
        ...propagationHumanFloatPaths(
          row.human_confirmation,
          `${root}.frame_results[${index}].human_confirmation`,
        ),
      );
    });
  (Array.isArray(report.suggestions) ? report.suggestions : [])
    .map(
      (row, index) =>
        [record(row, `Propagation suggestion ${index}`), index] as const,
    )
    .forEach(([row, index]) => {
      const suggestionRoot = `${root}.suggestions[${index}]`;
      if (row.point_source_px !== null) {
        paths.push(
          `${suggestionRoot}.point_source_px.x`,
          `${suggestionRoot}.point_source_px.y`,
        );
      }
      if (row.bbox_source_px !== null) {
        paths.push(
          `${suggestionRoot}.bbox_source_px.left`,
          `${suggestionRoot}.bbox_source_px.top`,
          `${suggestionRoot}.bbox_source_px.right`,
          `${suggestionRoot}.bbox_source_px.bottom`,
        );
      }
      for (const field of [
        "match_score",
        "backward_match_score",
        "forward_backward_error_px",
        "step_displacement_px",
      ]) {
        paths.push(`${suggestionRoot}.self_check.${field}`);
      }
      paths.push(
        ...propagationHumanFloatPaths(
          row.human_confirmation,
          `${suggestionRoot}.human_confirmation`,
        ),
      );
    });
  return paths;
}

function parsePropagationHumanAudit(
  value: unknown,
  kind: "confirmation" | "decision",
  label: string,
) {
  if (value === null) return null;
  const audit = record(value, label);
  if (kind === "confirmation") {
    exactKeys(
      audit,
      [
        "revision_id",
        "revision",
        "operator_id",
        "center_error_px",
        "iou",
        "corrected",
        "confirmed_at",
      ],
      label,
    );
    finiteNumber(audit.center_error_px, `${label} center error`);
    if (audit.iou !== null) {
      const iou = finiteNumber(audit.iou, `${label} IoU`);
      if (iou > 1) throw new Error(`${label} IoU is invalid.`);
    }
    if (typeof audit.corrected !== "boolean") {
      throw new Error(`${label} correction state is invalid.`);
    }
    stringValue(audit.confirmed_at, `${label} time`);
  } else {
    exactKeys(
      audit,
      ["decision", "revision_id", "revision", "operator_id", "decided_at"],
      label,
    );
    if (audit.decision !== "dismissed_manual_annotation") {
      throw new Error(`${label} decision is invalid.`);
    }
    stringValue(audit.decided_at, `${label} time`);
  }
  safeId(audit.revision_id, `${label} revision ID`);
  integer(audit.revision, `${label} revision`, 1);
  safeId(audit.operator_id, `${label} operator ID`);
  return audit;
}

function parsePropagationReports(
  value: unknown,
  context: {
    session: ParsedBallAnnotationSession;
    packageValue: Record<string, unknown>;
    packageManifest: Record<string, unknown>;
    frameEvidence: ReturnType<typeof parseSealedFrameEvidence>;
    revisions: ReturnType<typeof parseSealedRevisionChain>;
  },
) {
  const rawReports = boundedCollection(value, "Propagation reports", 0, 20);
  if (context.session.view.dataRole === "check" && rawReports.length > 0) {
    throw new Error("Check packages cannot contain propagation reports.");
  }
  const reportsByJob = new Map<
    string,
    {
      raw: Record<string, unknown>;
      frameResults: Map<number, Record<string, unknown>>;
      suggestions: Map<string, Record<string, unknown>>;
    }
  >();
  let previousJobId: string | null = null;
  rawReports.forEach((rawReport, reportIndex) => {
    const label = `Propagation report ${reportIndex}`;
    const report = record(rawReport, label);
    exactKeys(report, PROPAGATION_REPORT_KEYS, label);
    const jobId = safeId(report.job_id, `${label} job ID`);
    if (previousJobId !== null && jobId <= previousJobId) {
      throw new Error("Propagation reports are not unique and ordered.");
    }
    previousJobId = jobId;
    const seedFrameIndex = integer(
      report.seed_frame_index,
      `${label} seed frame`,
    );
    const expectedSeedRevision = integer(
      report.expected_seed_revision,
      `${label} seed revision`,
      1,
    );
    const radiusFrames = integer(report.radius_frames, `${label} radius`, 1);
    if (
      radiusFrames > 2 ||
      report.schema_version !== "1.0" ||
      report.artifact_type !== "ball_propagation_report" ||
      report.session_id !== context.session.view.sessionId
    ) {
      throw new Error(`${label} identity is invalid.`);
    }
    const seed = record(report.seed_binding, `${label} seed binding`);
    exactKeys(
      seed,
      [
        "frame_index",
        "annotation_revision",
        "annotation_etag",
        "annotation_sha256",
        "source_frame_sha256",
        "temporal_group_id",
        "sampling_manifest_sha256",
        "tracker_profile_sha256",
      ],
      `${label} seed binding`,
    );
    const tracker = record(report.tracker_profile, `${label} tracker profile`);
    exactKeys(
      tracker,
      [
        "profile_id",
        "version",
        "radius_frames_max",
        "search_radius_source_px",
        "minimum_match_score",
        "minimum_backward_match_score",
        "maximum_forward_backward_error_px",
        "profile_sha256",
      ],
      `${label} tracker profile`,
    );
    const trackerBody = { ...tracker };
    delete trackerBody.profile_sha256;
    const trackerSha256 = sha256(
      report.tracker_profile_sha256,
      `${label} tracker SHA-256`,
    );
    for (const field of [
      "minimum_match_score",
      "minimum_backward_match_score",
    ] as const) {
      const score = finiteNumber(tracker[field], `${label} tracker ${field}`);
      if (score > 1) throw new Error(`${label} tracker profile is invalid.`);
    }
    finiteNumber(
      tracker.maximum_forward_backward_error_px,
      `${label} tracker error`,
      Number.MIN_VALUE,
    );
    if (
      tracker.profile_id !== "tiny_ball_bounded_template_flow_v1" ||
      tracker.version !== "1.0" ||
      tracker.radius_frames_max !== 2 ||
      tracker.search_radius_source_px !== 24 ||
      tracker.profile_sha256 !== trackerSha256 ||
      pythonCanonicalSha256Sync(trackerBody, [
        "$.minimum_match_score",
        "$.minimum_backward_match_score",
        "$.maximum_forward_backward_error_px",
      ]) !== trackerSha256 ||
      seed.frame_index !== seedFrameIndex ||
      seed.annotation_revision !== expectedSeedRevision ||
      seed.sampling_manifest_sha256 !==
        context.packageManifest.manifest_sha256 ||
      seed.tracker_profile_sha256 !== trackerSha256 ||
      pythonCanonicalSha256Sync(seed, []) !== report.seed_binding_sha256
    ) {
      throw new Error(`${label} seed/tracker authority is invalid.`);
    }
    const seedEvidence = context.frameEvidence.find(
      (row) => row.frameIndex === seedFrameIndex,
    );
    const seedRevisions =
      context.revisions.revisionsByFrame.get(seedFrameIndex);
    const seedRevision = seedRevisions?.[expectedSeedRevision - 1];
    const seedAnnotation = seedRevision?.effective_annotation;
    const seedAnnotationSha256 =
      seedAnnotation && typeof seedAnnotation === "object"
        ? pythonCanonicalSha256Sync(
            seedAnnotation,
            annotationAuthorityFloatPaths(),
          )
        : null;
    const seedGroup = seedEvidence
      ? record(seedEvidence.raw.temporal_group, `${label} seed temporal group`)
      : null;
    if (
      !seedEvidence ||
      seedEvidence.frameRole !== "primary" ||
      !seedRevision ||
      seedRevision.revision !== expectedSeedRevision ||
      seedRevision.annotation_etag !== seed.annotation_etag ||
      seedAnnotationSha256 !== seed.annotation_sha256 ||
      record(seedEvidence.raw.source_frame_jpeg, `${label} seed JPEG`)
        .sha256 !== seed.source_frame_sha256 ||
      seedGroup?.group_id !== seed.temporal_group_id
    ) {
      throw new Error(`${label} seed does not bind the sealed frame/revision.`);
    }
    const targetIndices = boundedCollection(
      report.target_frame_indices,
      `${label} targets`,
      1,
      4,
    ).map((frameIndex) => integer(frameIndex, `${label} target frame`));
    if (
      !sameOrderedValues(
        targetIndices,
        [...new Set(targetIndices)].sort((left, right) => left - right),
      )
    ) {
      throw new Error(`${label} target frames are invalid.`);
    }
    const intentBody = {
      session_id: context.session.view.sessionId,
      mutation_id: safeId(report.mutation_id, `${label} mutation ID`),
      seed_frame_index: seedFrameIndex,
      radius_frames: radiusFrames,
      expected_seed_revision: expectedSeedRevision,
      seed_binding: seed,
      target_frame_indices: targetIndices,
    };
    if (pythonCanonicalSha256Sync(intentBody, []) !== report.intent_sha256) {
      throw new Error(`${label} intent digest is invalid.`);
    }
    safeId(report.neighbor_probe_job_id, `${label} neighbor probe job ID`);
    sha256(
      report.neighbor_probe_report_sha256,
      `${label} neighbor report SHA-256`,
    );
    sha256(
      report.neighbor_probe_result_manifest_sha256,
      `${label} neighbor manifest SHA-256`,
    );
    const frameResults = new Map<number, Record<string, unknown>>();
    const successfulResults: Record<string, unknown>[] = [];
    boundedCollection(
      report.frame_results,
      `${label} frame results`,
      1,
      4,
    ).forEach((rawResult, resultIndex) => {
      const result = record(rawResult, `${label} frame result ${resultIndex}`);
      exactKeys(
        result,
        PROPAGATION_FRAME_RESULT_KEYS,
        `${label} frame result ${resultIndex}`,
      );
      const frameIndex = integer(result.frame_index, `${label} result frame`);
      if (frameIndex !== targetIndices[resultIndex]) {
        throw new Error(`${label} frame results differ from targets.`);
      }
      oneOf(
        result.direction,
        ["backward", "forward"] as const,
        `${label} result direction`,
      );
      const status = oneOf(
        result.status,
        ["success", "failed"] as const,
        `${label} result status`,
      );
      sha256(result.source_frame_sha256, `${label} result source SHA-256`);
      const confirmation = parsePropagationHumanAudit(
        result.human_confirmation,
        "confirmation",
        `${label} result confirmation`,
      );
      const decision = parsePropagationHumanAudit(
        result.human_decision,
        "decision",
        `${label} result decision`,
      );
      const decided = confirmation !== null || decision !== null;
      if (confirmation && decision) {
        throw new Error(`${label} result has conflicting human decisions.`);
      }
      if (status === "success") {
        safeId(result.suggestion_id, `${label} result suggestion ID`);
        for (const field of [
          "match_score",
          "backward_match_score",
          "forward_backward_error_px",
          "step_displacement_px",
        ] as const) {
          finiteNumber(
            result[field],
            `${label} result ${field}`,
            field.includes("score") ? -1 : 0,
          );
        }
        if (
          result.failure_code !== null ||
          result.pending_human_confirmation === decided
        ) {
          throw new Error(`${label} successful result state is invalid.`);
        }
        successfulResults.push(result);
      } else if (
        typeof result.failure_code !== "string" ||
        result.suggestion_id !== null ||
        result.pending_human_confirmation !== false ||
        decided
      ) {
        throw new Error(`${label} failed result state is invalid.`);
      }
      frameResults.set(frameIndex, result);
    });
    const suggestions = new Map<string, Record<string, unknown>>();
    boundedCollection(report.suggestions, `${label} suggestions`, 0, 4).forEach(
      (rawSuggestion, suggestionIndex) => {
        const suggestion = record(
          rawSuggestion,
          `${label} suggestion ${suggestionIndex}`,
        );
        exactKeys(
          suggestion,
          PROPAGATION_SUGGESTION_KEYS,
          `${label} suggestion ${suggestionIndex}`,
        );
        const suggestionId = safeId(
          suggestion.suggestion_id,
          `${label} suggestion ID`,
        );
        const frameIndex = integer(
          suggestion.frame_index,
          `${label} suggestion frame`,
        );
        const point = parsePoint(
          suggestion.point_source_px,
          `${label} suggestion point`,
        );
        const box =
          suggestion.bbox_source_px === null
            ? null
            : parseBox(suggestion.bbox_source_px, `${label} suggestion box`);
        const selfCheck = record(
          suggestion.self_check,
          `${label} suggestion self-check`,
        );
        exactKeys(
          selfCheck,
          [
            "match_score",
            "backward_match_score",
            "forward_backward_error_px",
            "step_displacement_px",
          ],
          `${label} suggestion self-check`,
        );
        for (const field of [
          "match_score",
          "backward_match_score",
          "forward_backward_error_px",
          "step_displacement_px",
        ] as const) {
          finiteNumber(
            selfCheck[field],
            `${label} suggestion ${field}`,
            field.includes("score") ? -1 : 0,
          );
        }
        const group = record(
          suggestion.temporal_group,
          `${label} suggestion temporal group`,
        );
        exactKeys(
          group,
          [
            "group_id",
            "profile_id",
            "source_sha256",
            "seed_frame_index",
            "start_frame",
            "end_frame",
            "derivative_family",
            "canonical_moment_id",
            "derivative_family_id",
            "ancestry_profile",
            "derivative",
            "derivative_binding_sha256",
          ],
          `${label} suggestion temporal group`,
        );
        const derivative = record(
          group.derivative,
          `${label} suggestion derivative`,
        );
        exactKeys(
          derivative,
          ["artifact_type", "artifact_id", "inheritance_rule"],
          `${label} suggestion derivative`,
        );
        const confirmation = parsePropagationHumanAudit(
          suggestion.human_confirmation,
          "confirmation",
          `${label} suggestion confirmation`,
        );
        const decision = parsePropagationHumanAudit(
          suggestion.human_decision,
          "decision",
          `${label} suggestion decision`,
        );
        const decided = confirmation !== null || decision !== null;
        const suggestionBody = { ...suggestion };
        for (const field of [
          "suggestion_job_id",
          "suggestion_sha256",
          "pending_human_confirmation",
          "human_confirmation",
          "human_decision",
        ]) {
          delete suggestionBody[field];
        }
        const suggestionFloatPaths = propagationReportFloatPaths({
          tracker_profile: {
            minimum_match_score: 0,
            minimum_backward_match_score: 0,
            maximum_forward_backward_error_px: 0,
          },
          summary: {
            self_check_coverage: 0,
            human_validated_center_error_px: null,
            human_validated_iou: null,
          },
          frame_results: [],
          suggestions: [suggestion],
        })
          .filter((path) => path.startsWith("$.suggestions[0]."))
          .map((path) => path.replace("$.suggestions[0]", "$"));
        const suggestionSha256 = sha256(
          suggestion.suggestion_sha256,
          `${label} suggestion SHA-256`,
        );
        if (
          point === null ||
          box === null ||
          suggestion.presence !== "present" ||
          !["visible", "partial"].includes(suggestion.visibility as string) ||
          suggestion.training_use !== "excluded" ||
          suggestion.annotation_state !== "suggested" ||
          suggestion.provenance !== "tiny_ball_bounded_template_flow_v1" ||
          suggestion.suggestion_job_id !== jobId ||
          suggestion.pending_human_confirmation === decided ||
          (confirmation !== null && decision !== null) ||
          group.group_id !== suggestion.temporal_group_id ||
          group.seed_frame_index !== seedFrameIndex ||
          group.source_sha256 !== context.session.view.source.sourceSha256 ||
          group.profile_id !== "tiny_ball_temporal_groups_v1" ||
          group.ancestry_profile !==
            "source-proxy-crop-tile-propagation-closure-v1" ||
          derivative.artifact_type !== "propagation" ||
          derivative.inheritance_rule !==
            "inherit-source-group-without-regrouping-v1" ||
          pythonCanonicalSha256Sync(suggestionBody, suggestionFloatPaths) !==
            suggestionSha256
        ) {
          throw new Error(`${label} suggestion authority is invalid.`);
        }
        const result = frameResults.get(frameIndex);
        if (
          !result ||
          result.status !== "success" ||
          result.suggestion_id !== suggestionId ||
          result.source_frame_sha256 !== suggestion.source_frame_sha256 ||
          result.pending_human_confirmation !==
            suggestion.pending_human_confirmation ||
          !sameCanonicalValue(
            result.human_confirmation,
            suggestion.human_confirmation,
          ) ||
          !sameCanonicalValue(result.human_decision, suggestion.human_decision)
        ) {
          throw new Error(`${label} suggestion differs from its frame result.`);
        }
        const humanAudit = confirmation ?? decision;
        if (humanAudit) {
          const revision = context.revisions.revisionsById.get(
            humanAudit.revision_id as string,
          );
          const prefix = confirmation ? "accepted" : "dismissed";
          const decidedAt = confirmation
            ? humanAudit.confirmed_at
            : humanAudit.decided_at;
          if (
            !revision ||
            revision.frame_index !== frameIndex ||
            revision.revision !== humanAudit.revision ||
            revision.operator_id !== humanAudit.operator_id ||
            revision.created_at !== decidedAt ||
            revision[`${prefix}_suggestion_kind`] !== "propagation" ||
            revision[`${prefix}_suggestion_id`] !== suggestionId ||
            revision[`${prefix}_suggestion_job_id`] !== jobId ||
            revision[`${prefix}_suggestion_sha256`] !== suggestionSha256
          ) {
            throw new Error(
              `${label} suggestion lacks bound revision authority.`,
            );
          }
        }
        if (suggestions.has(suggestionId)) {
          throw new Error(`${label} suggestion identity is duplicated.`);
        }
        suggestions.set(suggestionId, suggestion);
      },
    );
    const successfulSuggestionIds = new Set(
      successfulResults.map((result) => result.suggestion_id as string),
    );
    if (
      suggestions.size !== successfulSuggestionIds.size ||
      [...successfulSuggestionIds].some((id) => !suggestions.has(id))
    ) {
      throw new Error(`${label} suggestions do not match successful results.`);
    }
    const counts = record(report.decision_counts, `${label} decision counts`);
    exactKeys(counts, ["confirmed", "dismissed", "pending"], `${label} counts`);
    const confirmed = successfulResults.filter(
      (result) => result.human_confirmation !== null,
    ).length;
    const dismissed = successfulResults.filter(
      (result) => result.human_decision !== null,
    ).length;
    const pending = successfulResults.filter(
      (result) => result.pending_human_confirmation === true,
    ).length;
    const summary = record(report.summary, `${label} summary`);
    exactKeys(
      summary,
      [
        "attempted_by_direction",
        "succeeded_by_direction",
        "attempted_frame_count",
        "succeeded_frame_count",
        "self_check_coverage",
        "self_checked_max_safe_window_frames",
        "human_validated_frame_count",
        "human_dismissed_frame_count",
        "pending_human_confirmation_count",
        "human_validated_center_error_px",
        "human_validated_iou",
        "human_validated_safe_span_frames",
        "pending_human_confirmation",
      ],
      `${label} summary`,
    );
    exactKeys(
      record(summary.attempted_by_direction, `${label} attempted directions`),
      ["backward", "forward"],
      `${label} attempted directions`,
    );
    exactKeys(
      record(summary.succeeded_by_direction, `${label} succeeded directions`),
      ["backward", "forward"],
      `${label} succeeded directions`,
    );
    finiteNumber(summary.self_check_coverage, `${label} self-check coverage`);
    if (
      counts.confirmed !== confirmed ||
      counts.dismissed !== dismissed ||
      counts.pending !== pending ||
      summary.succeeded_frame_count !== successfulResults.length ||
      summary.human_validated_frame_count !== confirmed ||
      summary.human_dismissed_frame_count !== dismissed ||
      summary.pending_human_confirmation_count !== pending ||
      summary.pending_human_confirmation !== pending > 0
    ) {
      throw new Error(`${label} decision accounting is invalid.`);
    }
    const canonicalBody = canonicalPropagationReport(report, false);
    if (
      pythonCanonicalSha256Sync(
        canonicalBody,
        propagationReportFloatPaths(canonicalBody),
      ) !== report.report_sha256
    ) {
      throw new Error(`${label} digest is invalid.`);
    }
    reportsByJob.set(jobId, { raw: report, frameResults, suggestions });
  });
  const producingJobs = new Set<string>();
  for (const evidenceRow of context.frameEvidence) {
    if (evidenceRow.frameRole !== "supplemental") continue;
    const propagation = record(
      evidenceRow.raw.propagation_evidence,
      "Supplemental propagation evidence",
    );
    const jobId = safeId(
      propagation.propagation_job_id,
      "Supplemental propagation job ID",
    );
    producingJobs.add(jobId);
    const report = reportsByJob.get(jobId);
    const frameResult = report?.frameResults.get(evidenceRow.frameIndex);
    const suggestion =
      frameResult?.suggestion_id === null || !frameResult
        ? null
        : report?.suggestions.get(frameResult.suggestion_id as string);
    const resultCanonical = frameResult
      ? canonicalPropagationRow(frameResult)
      : null;
    const resultFloatPaths = frameResult
      ? propagationReportFloatPaths({
          tracker_profile: {
            minimum_match_score: 0,
            minimum_backward_match_score: 0,
            maximum_forward_backward_error_px: 0,
          },
          summary: {
            self_check_coverage: 0,
            human_validated_center_error_px: null,
            human_validated_iou: null,
          },
          frame_results: [frameResult],
          suggestions: [],
        })
          .filter((path) => path.startsWith("$.frame_results[0]."))
          .map((path) => path.replace("$.frame_results[0]", "$"))
      : [];
    const bindingBody = { ...propagation };
    delete bindingBody.binding_sha256;
    const temporalGroup = record(
      evidenceRow.raw.temporal_group,
      "Supplemental temporal group",
    );
    if (
      !report ||
      !frameResult ||
      propagation.propagation_report_sha256 !== report.raw.report_sha256 ||
      propagation.propagation_intent_sha256 !== report.raw.intent_sha256 ||
      propagation.seed_binding_sha256 !== report.raw.seed_binding_sha256 ||
      propagation.tracker_profile_sha256 !==
        report.raw.tracker_profile_sha256 ||
      propagation.neighbor_probe_job_id !== report.raw.neighbor_probe_job_id ||
      propagation.neighbor_probe_report_sha256 !==
        report.raw.neighbor_probe_report_sha256 ||
      propagation.neighbor_probe_result_manifest_sha256 !==
        report.raw.neighbor_probe_result_manifest_sha256 ||
      propagation.suggestion_id !== frameResult.suggestion_id ||
      propagation.suggestion_sha256 !== suggestion?.suggestion_sha256 ||
      propagation.temporal_group_derivative_binding_sha256 !==
        temporalGroup.derivative_binding_sha256 ||
      !resultCanonical ||
      pythonCanonicalSha256Sync(resultCanonical, resultFloatPaths) !==
        propagation.propagation_frame_result_sha256 ||
      pythonCanonicalSha256Sync(bindingBody, []) !== propagation.binding_sha256
    ) {
      throw new Error(
        "Supplemental frame changed its sealed propagation report.",
      );
    }
  }
  if (
    producingJobs.size !== reportsByJob.size ||
    [...reportsByJob.keys()].some((jobId) => !producingJobs.has(jobId))
  ) {
    throw new Error("Propagation reports include unreferenced producer jobs.");
  }
  const canonicalReports = rawReports.map((report) =>
    canonicalPropagationReport(record(report, "Propagation report"), true),
  );
  if (
    pythonCanonicalSha256Sync(
      canonicalReports,
      canonicalReports.flatMap((report, index) =>
        propagationReportFloatPaths(report, `$[${index}]`),
      ),
    ) !== context.packageValue.propagation_reports_sha256
  ) {
    throw new Error("Propagation report collection digest is invalid.");
  }
  return {
    canonicalReports,
    pendingCount: [...reportsByJob.values()].reduce(
      (total, report) =>
        total +
        integer(
          record(report.raw.decision_counts, "Propagation decision counts")
            .pending,
          "Pending propagation count",
        ),
      0,
    ),
  };
}

const MOTION_STRATA = [
  "none",
  "ground",
  "airborne",
  "motion_blurred",
  "occluded",
  "reappearance",
  "stationary",
] as const;

const FEASIBILITY_REPORT_FRAME_KEYS = [
  "frame_index",
  "presence",
  "metric_eligible",
  "scored_candidate_count",
  "raw_candidate_count",
  "top1_hit",
  "top5_hit",
  "candidate_diagnostics",
  "observed_lighting_tag",
  "frozen_lighting_stratum",
  "observed_scale_stratum",
  "derived_scale_stratum",
  "bbox_diagonal_source_px",
  "bbox_aspect_ratio",
  "motion_occlusion_tags",
  "diagnostic_codes",
] as const;

function feasibilityMetricProfileFloatPaths(root = "$") {
  return [
    `${root}.top1_recall_target`,
    `${root}.top5_recall_target`,
    `${root}.apparent_size_rule.plausible_diagonal_min_source_px`,
    `${root}.apparent_size_rule.far_max_source_height_divisor`,
    `${root}.apparent_size_rule.mid_max_source_height_divisor`,
    `${root}.apparent_size_rule.near_max_source_height_multiplier`,
    `${root}.apparent_size_rule.aspect_ratio_min`,
    `${root}.apparent_size_rule.aspect_ratio_max`,
    `${root}.matching_rule.minimum_radius_source_px`,
    `${root}.matching_rule.confirmed_box_diagonal_multiplier`,
    `${root}.matching_rule.source_height_cap_divisor`,
    `${root}.intervals.confidence`,
    `${root}.intervals.false_candidate_range[0]`,
    `${root}.intervals.false_candidate_range[1]`,
  ];
}

function feasibilityRawMetricFloatPaths(
  root: string,
  interval?: "one_sided_95_lower" | "one_sided_95_upper",
) {
  return [
    `${root}.point_estimate`,
    ...(interval ? [`${root}.${interval}`] : []),
  ];
}

function feasibilityStratumMetricFloatPaths(root: string) {
  return [
    ...feasibilityRawMetricFloatPaths(
      `${root}.top1_recall`,
      "one_sided_95_lower",
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.top5_recall`,
      "one_sided_95_lower",
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.false_candidates_per_evaluable_frame`,
      "one_sided_95_upper",
    ),
  ];
}

function checkFeasibilityReportFloatPaths(
  report: Record<string, unknown>,
  root = "$",
) {
  const paths = [
    ...feasibilityMetricProfileFloatPaths(`${root}.metric_profile`),
    ...[
      "plausible_diagonal_min_source_px",
      "far_diagonal_max_source_px",
      "mid_diagonal_max_source_px",
      "near_diagonal_max_source_px",
      "plausible_diagonal_max_source_px",
      "aspect_ratio_min",
      "aspect_ratio_max",
      "matching_radius_cap_source_px",
    ].map((key) => `${root}.computed_source_px_bounds.${key}`),
    ...feasibilityRawMetricFloatPaths(
      `${root}.metrics.top1_recall`,
      "one_sided_95_lower",
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.metrics.top5_recall`,
      "one_sided_95_lower",
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.metrics.false_candidates_per_evaluable_frame`,
      "one_sided_95_upper",
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.metrics.candidates_per_evaluable_frame`,
    ),
    ...feasibilityRawMetricFloatPaths(
      `${root}.metrics.raw_candidates_per_evaluable_frame`,
    ),
  ];
  for (const [family, keys] of [
    ["scale", SCALE_STRATA],
    ["lighting", LIGHTING_STRATA],
    ["motion_occlusion", MOTION_STRATA],
  ] as const) {
    keys.forEach((key) =>
      paths.push(
        ...feasibilityStratumMetricFloatPaths(
          `${root}.strata_metrics.${family}.${key}`,
        ),
      ),
    );
  }
  if (Array.isArray(report.frames)) {
    report.frames.forEach((rawFrame, frameIndex) => {
      const frameRoot = `${root}.frames[${frameIndex}]`;
      paths.push(
        `${frameRoot}.bbox_diagonal_source_px`,
        `${frameRoot}.bbox_aspect_ratio`,
      );
      if (
        rawFrame &&
        typeof rawFrame === "object" &&
        Array.isArray(
          (rawFrame as Record<string, unknown>).candidate_diagnostics,
        )
      ) {
        (
          (rawFrame as Record<string, unknown>)
            .candidate_diagnostics as unknown[]
        ).forEach((_, diagnosticIndex) => {
          const diagnosticRoot = `${frameRoot}.candidate_diagnostics[${diagnosticIndex}]`;
          paths.push(
            `${diagnosticRoot}.center_distance_source_px`,
            `${diagnosticRoot}.iou`,
            `${diagnosticRoot}.evaluation_radius_source_px`,
          );
        });
      }
    });
  }
  return paths;
}

function parseFeasibilityMetricProfile(value: unknown) {
  const profile = record(value, "Feasibility metric profile");
  exactKeys(
    profile,
    [
      "profile_id",
      "candidate_budget",
      "top1_recall_target",
      "top5_recall_target",
      "minimum_total_frames",
      "maximum_total_frames",
      "minimum_localizable_positives",
      "minimum_confirmed_absent",
      "minimum_applicable_stratum_positives",
      "exploratory_small_n_threshold",
      "apparent_size_rule",
      "matching_rule",
      "intervals",
    ],
    "Feasibility metric profile",
  );
  const apparent = record(
    profile.apparent_size_rule,
    "Feasibility apparent-size rule",
  );
  exactKeys(
    apparent,
    [
      "name",
      "plausible_diagonal_min_source_px",
      "far_max_source_height_divisor",
      "mid_max_source_height_divisor",
      "near_max_source_height_multiplier",
      "aspect_ratio_min",
      "aspect_ratio_max",
    ],
    "Feasibility apparent-size rule",
  );
  const matching = record(profile.matching_rule, "Feasibility matching rule");
  exactKeys(
    matching,
    [
      "name",
      "minimum_radius_source_px",
      "confirmed_box_diagonal_multiplier",
      "source_height_cap_divisor",
      "one_to_one",
    ],
    "Feasibility matching rule",
  );
  const intervals = record(profile.intervals, "Metric intervals");
  exactKeys(
    intervals,
    ["confidence", "recall", "false_candidates", "false_candidate_range"],
    "Metric intervals",
  );
  if (
    profile.profile_id !== "tiny_ball_feasibility_metric_v1" ||
    profile.candidate_budget !== 5 ||
    profile.top1_recall_target !== 0.6 ||
    profile.top5_recall_target !== 0.8 ||
    profile.minimum_total_frames !== 20 ||
    profile.maximum_total_frames !== 50 ||
    profile.minimum_localizable_positives !== 15 ||
    profile.minimum_confirmed_absent !== 5 ||
    profile.minimum_applicable_stratum_positives !== 3 ||
    profile.exploratory_small_n_threshold !== 10 ||
    apparent.name !== "source-height-bound-ball-diagonal-v1" ||
    apparent.plausible_diagonal_min_source_px !== 1 ||
    apparent.far_max_source_height_divisor !== 80 ||
    apparent.mid_max_source_height_divisor !== 40 ||
    apparent.near_max_source_height_multiplier !== 0.075 ||
    apparent.aspect_ratio_min !== 0.25 ||
    apparent.aspect_ratio_max !== 4 ||
    matching.name !== "confirmed-box-center-region-v1" ||
    matching.minimum_radius_source_px !== 4 ||
    matching.confirmed_box_diagonal_multiplier !== 0.75 ||
    matching.source_height_cap_divisor !== 45 ||
    matching.one_to_one !== true ||
    intervals.confidence !== 0.95 ||
    intervals.recall !== "one-sided-wilson-score-v1" ||
    intervals.false_candidates !== "bounded-hoeffding-upper-v1" ||
    !Array.isArray(intervals.false_candidate_range) ||
    intervals.false_candidate_range.length !== 2 ||
    intervals.false_candidate_range[0] !== 0 ||
    intervals.false_candidate_range[1] !== 5
  ) {
    throw new Error("Feasibility metric profile is invalid.");
  }
  return { profile, intervals };
}

function parseFeasibilitySourceBounds(value: unknown, sourceHeight: number) {
  const bounds = record(value, "Computed source bounds");
  exactKeys(
    bounds,
    [
      "source_height_px",
      "plausible_diagonal_min_source_px",
      "far_diagonal_max_source_px",
      "mid_diagonal_max_source_px",
      "near_diagonal_max_source_px",
      "plausible_diagonal_max_source_px",
      "aspect_ratio_min",
      "aspect_ratio_max",
      "matching_radius_cap_source_px",
    ],
    "Computed source bounds",
  );
  const observed = {
    sourceHeight: integer(bounds.source_height_px, "Computed source height", 1),
    plausibleMin: finiteNumber(
      bounds.plausible_diagonal_min_source_px,
      "Computed plausible minimum",
      Number.MIN_VALUE,
    ),
    farMax: finiteNumber(
      bounds.far_diagonal_max_source_px,
      "Computed far maximum",
      Number.MIN_VALUE,
    ),
    midMax: finiteNumber(
      bounds.mid_diagonal_max_source_px,
      "Computed mid maximum",
      Number.MIN_VALUE,
    ),
    nearMax: finiteNumber(
      bounds.near_diagonal_max_source_px,
      "Computed near maximum",
      Number.MIN_VALUE,
    ),
    plausibleMax: finiteNumber(
      bounds.plausible_diagonal_max_source_px,
      "Computed plausible maximum",
      Number.MIN_VALUE,
    ),
    aspectMin: finiteNumber(
      bounds.aspect_ratio_min,
      "Computed aspect minimum",
      Number.MIN_VALUE,
    ),
    aspectMax: finiteNumber(
      bounds.aspect_ratio_max,
      "Computed aspect maximum",
      Number.MIN_VALUE,
    ),
    radiusCap: finiteNumber(
      bounds.matching_radius_cap_source_px,
      "Computed radius cap",
      Number.MIN_VALUE,
    ),
  };
  const expected = {
    plausibleMin: 1,
    farMax: sourceHeight / 80,
    midMax: sourceHeight / 40,
    nearMax: sourceHeight * 0.075,
    plausibleMax: sourceHeight * 0.075,
    aspectMin: 0.25,
    aspectMax: 4,
    radiusCap: Math.max(4, sourceHeight / 45),
  };
  if (
    observed.sourceHeight !== sourceHeight ||
    Object.entries(expected).some(
      ([key, expectedValue]) =>
        Math.abs(observed[key as keyof typeof expected] - expectedValue) >
        1e-12,
    )
  ) {
    throw new Error("Computed source bounds differ from metric authority.");
  }
  return observed;
}

function parseCheckFeasibilityFrames(
  value: unknown,
  context: {
    primaryFrameIndices: number[];
    packageManifest: Record<string, unknown>;
    finalAnnotations: ReturnType<typeof parseFinalAnnotations>;
    bounds: ReturnType<typeof parseFeasibilitySourceBounds>;
  },
) {
  const rawFrames = boundedCollection(
    value,
    "Check feasibility frames",
    20,
    50,
  );
  const rawGroups = Array.isArray(context.packageManifest.groups)
    ? context.packageManifest.groups
    : [];
  const frozenLightingByFrame = new Map<number, string>();
  context.primaryFrameIndices.forEach((frameIndex, index) => {
    const group = record(rawGroups[index], `Package sampling group ${index}`);
    if (group.frame_index !== frameIndex) {
      throw new Error("Package sampling group frame authority is invalid.");
    }
    frozenLightingByFrame.set(
      frameIndex,
      oneOf(
        group.pre_reveal_lighting_stratum,
        LIGHTING_STRATA,
        "Frozen lighting stratum",
      ),
    );
  });
  const annotationByFrame = new Map(
    context.finalAnnotations.map((row) => [row.frameIndex, row.annotation]),
  );
  const parsed = rawFrames.map((rawFrame, index) => {
    const label = `Check feasibility frame ${index}`;
    const frame = record(rawFrame, label);
    exactKeys(frame, FEASIBILITY_REPORT_FRAME_KEYS, label);
    const frameIndex = integer(frame.frame_index, `${label} index`);
    const presence = oneOf(
      frame.presence,
      ["present", "absent", "unknown"] as const,
      `${label} presence`,
    );
    if (typeof frame.metric_eligible !== "boolean") {
      throw new Error(`${label} eligibility is invalid.`);
    }
    const scoredCandidateCount = integer(
      frame.scored_candidate_count,
      `${label} scored candidate count`,
    );
    const rawCandidateCount = integer(
      frame.raw_candidate_count,
      `${label} raw candidate count`,
    );
    if (scoredCandidateCount > 5 || rawCandidateCount < scoredCandidateCount) {
      throw new Error(`${label} candidate counts are invalid.`);
    }
    const top1Hit =
      frame.top1_hit === null || typeof frame.top1_hit === "boolean"
        ? frame.top1_hit
        : (() => {
            throw new Error(`${label} Top-1 state is invalid.`);
          })();
    const top5Hit =
      frame.top5_hit === null || typeof frame.top5_hit === "boolean"
        ? frame.top5_hit
        : (() => {
            throw new Error(`${label} Top-5 state is invalid.`);
          })();
    const bboxDiagonal =
      frame.bbox_diagonal_source_px === null
        ? null
        : finiteNumber(
            frame.bbox_diagonal_source_px,
            `${label} box diagonal`,
            Number.MIN_VALUE,
          );
    const bboxAspect =
      frame.bbox_aspect_ratio === null
        ? null
        : finiteNumber(
            frame.bbox_aspect_ratio,
            `${label} box aspect ratio`,
            Number.MIN_VALUE,
          );
    const observedLighting = oneOf(
      frame.observed_lighting_tag,
      [...LIGHTING_STRATA, "not_applicable"] as const,
      `${label} observed lighting`,
    );
    const frozenLighting = oneOf(
      frame.frozen_lighting_stratum,
      LIGHTING_STRATA,
      `${label} frozen lighting`,
    );
    const observedScale = oneOf(
      frame.observed_scale_stratum,
      [...SCALE_STRATA, "not_applicable"] as const,
      `${label} observed scale`,
    );
    const derivedScale =
      frame.derived_scale_stratum === null
        ? null
        : oneOf(
            frame.derived_scale_stratum,
            SCALE_STRATA,
            `${label} derived scale`,
          );
    if (!Array.isArray(frame.motion_occlusion_tags)) {
      throw new Error(`${label} motion tags are invalid.`);
    }
    const motionTags = frame.motion_occlusion_tags.map((tag) =>
      oneOf(tag, MOTION_OCCLUSION_TAGS, `${label} motion tag`),
    );
    if (
      motionTags.length > 6 ||
      motionTags.length !== new Set(motionTags).size
    ) {
      throw new Error(`${label} motion tags are invalid.`);
    }
    const expectedCodes: string[] = [];
    if (observedLighting !== frozenLighting) {
      expectedCodes.push(
        `lighting_stratum_mismatch:${frozenLighting}:${observedLighting}`,
      );
    }
    if ((bboxDiagonal === null) !== (bboxAspect === null)) {
      throw new Error(`${label} box diagnostics are incomplete.`);
    }
    let expectedDerivedScale: "near" | "mid" | "far" | null = null;
    if (bboxDiagonal === null) {
      if (
        derivedScale !== null ||
        (presence === "present" && observedScale !== "not_applicable") ||
        (presence !== "present" && observedScale !== "not_applicable")
      ) {
        throw new Error(`${label} scale diagnostics are invalid.`);
      }
    } else {
      if (presence !== "present" || observedScale === "not_applicable") {
        throw new Error(`${label} box diagnostics are invalid.`);
      }
      if (bboxDiagonal < context.bounds.plausibleMin) {
        expectedCodes.push("bbox_diagonal_below_minimum");
      }
      if (bboxDiagonal > context.bounds.plausibleMax) {
        expectedCodes.push("bbox_diagonal_above_maximum");
      }
      if (
        bboxAspect! < context.bounds.aspectMin ||
        bboxAspect! > context.bounds.aspectMax
      ) {
        expectedCodes.push("bbox_aspect_ratio_out_of_bounds");
      }
      if (
        bboxDiagonal >= context.bounds.plausibleMin &&
        bboxDiagonal <= context.bounds.plausibleMax + 1e-9
      ) {
        expectedDerivedScale =
          bboxDiagonal <= context.bounds.farMax + 1e-9
            ? "far"
            : bboxDiagonal <= context.bounds.midMax + 1e-9
              ? "mid"
              : "near";
      }
      if (derivedScale !== expectedDerivedScale) {
        throw new Error(`${label} derived scale is invalid.`);
      }
      if (
        expectedDerivedScale !== null &&
        observedScale !== expectedDerivedScale
      ) {
        expectedCodes.push(
          `scale_stratum_mismatch:${observedScale}:${expectedDerivedScale}`,
        );
      }
    }
    if (!Array.isArray(frame.diagnostic_codes)) {
      throw new Error(`${label} diagnostic codes are invalid.`);
    }
    const diagnosticCodes = frame.diagnostic_codes.map((code) =>
      stringValue(code, `${label} diagnostic code`),
    );
    if (
      diagnosticCodes.length > 8 ||
      diagnosticCodes.length !== new Set(diagnosticCodes).size ||
      !sameOrderedValues(diagnosticCodes, expectedCodes)
    ) {
      throw new Error(`${label} diagnostic codes are invalid.`);
    }
    const expectedEligible =
      diagnosticCodes.length === 0 &&
      (presence === "absent" ||
        (presence === "present" && bboxDiagonal !== null));
    if (frame.metric_eligible !== expectedEligible) {
      throw new Error(`${label} eligibility is invalid.`);
    }
    const rawDiagnostics = boundedCollection(
      frame.candidate_diagnostics,
      `${label} candidate diagnostics`,
      0,
      5,
    );
    const expectedRadius =
      presence === "present" && bboxDiagonal !== null
        ? Math.min(Math.max(4, bboxDiagonal * 0.75), context.bounds.radiusCap)
        : null;
    const matched = rawDiagnostics.map((rawDiagnostic, diagnosticIndex) => {
      const diagnosticLabel = `${label} candidate ${diagnosticIndex}`;
      const diagnostic = record(rawDiagnostic, diagnosticLabel);
      exactKeys(
        diagnostic,
        [
          "rank",
          "matched",
          "center_distance_source_px",
          "iou",
          "evaluation_radius_source_px",
        ],
        diagnosticLabel,
      );
      const rank = integer(diagnostic.rank, `${diagnosticLabel} rank`, 1);
      if (rank > 5 || typeof diagnostic.matched !== "boolean") {
        throw new Error(`${diagnosticLabel} state is invalid.`);
      }
      const centerDistance =
        diagnostic.center_distance_source_px === null
          ? null
          : finiteNumber(
              diagnostic.center_distance_source_px,
              `${diagnosticLabel} center distance`,
            );
      const iou =
        diagnostic.iou === null
          ? null
          : finiteNumber(diagnostic.iou, `${diagnosticLabel} IoU`);
      const radius =
        diagnostic.evaluation_radius_source_px === null
          ? null
          : finiteNumber(
              diagnostic.evaluation_radius_source_px,
              `${diagnosticLabel} radius`,
              Number.MIN_VALUE,
            );
      const measured = [centerDistance, iou, radius].filter(
        (measurement) => measurement !== null,
      ).length;
      if (
        (measured !== 0 && measured !== 3) ||
        (iou !== null && iou > 1) ||
        (measured === 0 && diagnostic.matched !== false) ||
        (measured === 3 &&
          (diagnostic.matched !== centerDistance! <= radius! ||
            expectedRadius === null ||
            Math.abs(radius! - expectedRadius) > 1e-12))
      ) {
        throw new Error(`${diagnosticLabel} measurements are invalid.`);
      }
      return { rank, matched: diagnostic.matched as boolean };
    });
    const expectedRanks = Array.from(
      { length: scoredCandidateCount },
      (_, rank) => rank + 1,
    );
    if (frame.metric_eligible) {
      if (
        presence === "unknown" ||
        !sameOrderedValues(
          matched.map((row) => row.rank),
          expectedRanks,
        ) ||
        top1Hit !== Boolean(matched[0]?.matched) ||
        top5Hit !== matched.some((row) => row.matched) ||
        (presence === "absent" && matched.some((row) => row.matched))
      ) {
        throw new Error(`${label} metric evidence is invalid.`);
      }
    } else if (top1Hit !== null || top5Hit !== null || matched.length !== 0) {
      throw new Error(`${label} ineligible metric evidence is invalid.`);
    }
    const annotation = annotationByFrame.get(frameIndex);
    const expectedFrozenLighting = frozenLightingByFrame.get(frameIndex);
    if (!annotation || !expectedFrozenLighting) {
      throw new Error(`${label} lacks package truth authority.`);
    }
    const expectedDiagonal = annotation.bbox
      ? Math.hypot(
          annotation.bbox.right - annotation.bbox.left,
          annotation.bbox.bottom - annotation.bbox.top,
        )
      : null;
    const expectedAspect = annotation.bbox
      ? (annotation.bbox.right - annotation.bbox.left) /
        (annotation.bbox.bottom - annotation.bbox.top)
      : null;
    if (
      presence !== annotation.presence ||
      observedLighting !== annotation.lightingTag ||
      observedScale !== annotation.scaleStratum ||
      frozenLighting !== expectedFrozenLighting ||
      !sameOrderedValues(motionTags, annotation.motionOcclusionTags) ||
      (expectedDiagonal === null
        ? bboxDiagonal !== null
        : bboxDiagonal === null ||
          Math.abs(bboxDiagonal - expectedDiagonal) > 1e-12) ||
      (expectedAspect === null
        ? bboxAspect !== null
        : bboxAspect === null || Math.abs(bboxAspect - expectedAspect) > 1e-12)
    ) {
      throw new Error(`${label} differs from package truth authority.`);
    }
    return {
      frameIndex,
      presence,
      metricEligible: frame.metric_eligible as boolean,
      scoredCandidateCount,
      rawCandidateCount,
      top1Hit: top1Hit as boolean | null,
      top5Hit: top5Hit as boolean | null,
      observedLighting,
      observedScale,
      motionTags,
      diagnosticCodes,
    };
  });
  if (
    !sameOrderedValues(
      parsed.map((frame) => frame.frameIndex),
      context.primaryFrameIndices,
    )
  ) {
    throw new Error("Check report frame set differs from sampling authority.");
  }
  return parsed;
}

function parseStratumPositiveCounts(
  value: unknown,
  keys: readonly string[],
  label: string,
) {
  const strata = record(value, label);
  exactKeys(strata, keys, label);
  return Object.fromEntries(
    keys.map((key) => {
      const metric = record(strata[key], `${label} ${key}`);
      exactKeys(
        metric,
        [
          "support",
          "top1_recall",
          "top5_recall",
          "candidate_totals",
          "false_candidates_per_evaluable_frame",
          "exploratory_small_n",
        ],
        `${label} ${key}`,
      );
      const support = record(metric.support, `${label} ${key} support`);
      exactKeys(
        support,
        ["confirmed_absent", "evaluable_frames", "localizable_positives"],
        `${label} ${key} support`,
      );
      const localizable = integer(
        support.localizable_positives,
        `${label} ${key} localizable positives`,
      );
      const absent = integer(
        support.confirmed_absent,
        `${label} ${key} confirmed absent`,
      );
      const evaluable = integer(
        support.evaluable_frames,
        `${label} ${key} evaluable frames`,
      );
      const top1 = parseRawMetric(
        metric.top1_recall,
        `${label} ${key} Top-1 recall`,
        "one_sided_95_lower",
      );
      const top5 = parseRawMetric(
        metric.top5_recall,
        `${label} ${key} Top-5 recall`,
        "one_sided_95_lower",
      );
      const falseCandidates = parseRawMetric(
        metric.false_candidates_per_evaluable_frame,
        `${label} ${key} false candidates`,
        "one_sided_95_upper",
      );
      const totals = record(
        metric.candidate_totals,
        `${label} ${key} candidate totals`,
      );
      exactKeys(
        totals,
        ["false", "scored", "raw"],
        `${label} ${key} candidate totals`,
      );
      const falseTotal = integer(totals.false, `${label} ${key} false total`);
      const scoredTotal = integer(
        totals.scored,
        `${label} ${key} scored total`,
      );
      const rawTotal = integer(totals.raw, `${label} ${key} raw total`);
      if (
        typeof metric.exploratory_small_n !== "boolean" ||
        evaluable !== localizable + absent ||
        top1.denominator !== localizable ||
        top5.denominator !== localizable ||
        top1.numerator > top5.numerator ||
        falseCandidates.denominator !== evaluable ||
        falseCandidates.numerator !== falseTotal ||
        falseTotal !== scoredTotal - top5.numerator ||
        falseTotal > scoredTotal ||
        scoredTotal > rawTotal ||
        metric.exploratory_small_n !== localizable < 10
      ) {
        throw new Error(`${label} ${key} metrics are inconsistent.`);
      }
      return [key, localizable];
    }),
  );
}

function canonicalSamplingManifestForPackage(value: Record<string, unknown>) {
  const manifest = structuredClone(value);
  if (manifest.selection_authority === null)
    delete manifest.selection_authority;
  if (manifest.candidate_universe_authority === null) {
    delete manifest.candidate_universe_authority;
  }
  for (const field of ["groups", "excluded_development_groups"] as const) {
    if (!Array.isArray(manifest[field])) continue;
    manifest[field] = manifest[field].map((rawGroup) => {
      const group = { ...record(rawGroup, "Sampling temporal group") };
      if (group.pre_reveal_lighting_stratum === null) {
        delete group.pre_reveal_lighting_stratum;
      }
      return group;
    });
  }
  return manifest;
}

function annotationPackageFloatPaths(packageValue: Record<string, unknown>) {
  const paths = ["$.source.fps", "$.lineage.decode.fps"];
  const detectorAuthorities = Array.isArray(
    packageValue.detector_probe_authorities,
  )
    ? packageValue.detector_probe_authorities
    : [];
  detectorAuthorities.forEach((rawAuthority, index) => {
    if (!rawAuthority || typeof rawAuthority !== "object") return;
    const authority = rawAuthority as Record<string, unknown>;
    const currentSchema = authority.audit_anchor_kind === "embedded_job_record";
    if (authority.probe_report && typeof authority.probe_report === "object") {
      paths.push(
        ...detectorReportFloatPaths(
          authority.probe_report as Record<string, unknown>,
          `$.detector_probe_authorities[${index}].probe_report`,
          currentSchema,
        ),
      );
    }
    if (
      authority.probe_job_record &&
      typeof authority.probe_job_record === "object"
    ) {
      paths.push(
        ...detectorJobFloatPaths(
          authority.probe_job_record as Record<string, unknown>,
          `$.detector_probe_authorities[${index}].probe_job_record`,
          currentSchema,
        ),
      );
    }
  });
  const proxy = packageValue.frame_review_proxy_authority;
  if (proxy && typeof proxy === "object") {
    const authority = proxy as Record<string, unknown>;
    if (authority.probe_report && typeof authority.probe_report === "object") {
      paths.push(
        ...detectorReportFloatPaths(
          authority.probe_report as Record<string, unknown>,
          "$.frame_review_proxy_authority.probe_report",
          true,
        ),
      );
    }
    const manifest = authority.review_proxy_manifest;
    if (manifest && typeof manifest === "object") {
      const mappings = (manifest as Record<string, unknown>).mappings;
      paths.push(
        ...reviewProxyManifestFloatPaths(
          Array.isArray(mappings) ? mappings.length : 0,
          "$.frame_review_proxy_authority.review_proxy_manifest",
        ),
      );
    }
    const historical = authority.historical_probe_authority;
    if (historical && typeof historical === "object") {
      const report = (historical as Record<string, unknown>).probe_report;
      if (report && typeof report === "object") {
        paths.push(
          ...detectorReportFloatPaths(
            report as Record<string, unknown>,
            "$.frame_review_proxy_authority.historical_probe_authority.probe_report",
            false,
          ),
        );
      }
    }
  }
  if (Array.isArray(packageValue.effective_annotations)) {
    packageValue.effective_annotations.forEach((_, index) =>
      paths.push(
        ...annotationAuthorityFloatPaths(`$.effective_annotations[${index}]`),
      ),
    );
  }
  if (Array.isArray(packageValue.revision_chain)) {
    paths.push(
      ...revisionChainFloatPaths(
        packageValue.revision_chain as Record<string, unknown>[],
        "$.revision_chain",
      ),
    );
  }
  if (Array.isArray(packageValue.frame_evidence)) {
    packageValue.frame_evidence.forEach((rawRow, index) => {
      const row = record(rawRow, `Frame evidence ${index}`);
      paths.push(...frameEvidenceFloatPaths(row, `$.frame_evidence[${index}]`));
    });
  }
  if (Array.isArray(packageValue.detector_candidate_evidence)) {
    packageValue.detector_candidate_evidence.forEach((_, index) =>
      paths.push(
        ...detectorCandidateFloatPaths(
          `$.detector_candidate_evidence[${index}]`,
        ),
      ),
    );
  }
  if (Array.isArray(packageValue.propagation_reports)) {
    packageValue.propagation_reports.forEach((rawReport, index) => {
      const report = record(rawReport, `Propagation report ${index}`);
      paths.push(
        ...propagationReportFloatPaths(
          report,
          `$.propagation_reports[${index}]`,
        ),
      );
    });
  }
  return paths;
}

/** Strictly project a sealed package/report; opaque or over-authorized output fails closed. */
export function parseBallAnnotationFinalResult(
  value: unknown,
  session: ParsedBallAnnotationSession,
): ParsedBallAnnotationFinalResult {
  const result = record(value, "Ball annotation final result");
  exactKeys(result, ["package", "feasibility_report"], "Final result");
  const packageValue = record(result.package, "Annotation package");
  exactKeys(
    packageValue,
    [
      "schema_version",
      "artifact_type",
      "session_id",
      "session_request_authority",
      "data_role",
      "source",
      "lineage",
      "detector_probe_authorities",
      "frame_review_proxy_authority",
      "operator_id",
      "locked_profile",
      "control_profile_id",
      "control_profile",
      "sampling_profile_id",
      "metric_profile_id",
      "metric_profile_sha256",
      "sampling_manifest",
      "attempt_family_sha256",
      "development_package_binding",
      "check_probe_job_id",
      "check_probe_authority",
      "effective_annotations",
      "revision_chain",
      "supplemental_frame_indices",
      "frame_evidence",
      "frame_evidence_sha256",
      "frame_media",
      "frame_media_sha256",
      "detector_candidate_evidence",
      "detector_candidate_evidence_sha256",
      "propagation_reports",
      "propagation_reports_sha256",
      "created_at",
      "training_eligible",
      "may_seed_dataset_expansion",
      "qualification_eligible",
      "pr4a_pr4b_truth_compatible",
      "dataset_expansion_eligibility",
      "package_sha256",
    ],
    "Annotation package",
  );
  const packageSource = record(packageValue.source, "Package source");
  const packageProfile = record(packageValue.locked_profile, "Package profile");
  const packageControlProfile = record(
    packageValue.control_profile,
    "Package control profile",
  );
  const packageManifest = record(
    packageValue.sampling_manifest,
    "Package sampling manifest",
  );
  const attemptFamilySha256 = sha256(
    packageValue.attempt_family_sha256,
    "Package attempt family SHA-256",
  );
  const packageDevelopmentBinding = parseDevelopmentPackageBinding(
    packageValue.development_package_binding,
  );
  const packageCheckAuthority =
    packageValue.check_probe_authority === null
      ? null
      : record(packageValue.check_probe_authority, "Package check authority");
  const eligibility = parseDatasetExpansionEligibility(
    packageValue.dataset_expansion_eligibility,
  );
  const packageLineage = parseLineage(packageValue.lineage);
  if (
    packageValue.schema_version !== "1.0" ||
    packageValue.artifact_type !== "ball_annotation_package" ||
    packageValue.session_id !== session.view.sessionId ||
    packageValue.data_role !== session.view.dataRole ||
    packageSource.sha256 !== session.view.source.sourceSha256 ||
    packageProfile.profile_id !== session.view.lockedProfile.profileId ||
    packageProfile.profile_sha256 !==
      session.view.lockedProfile.profileSha256 ||
    packageControlProfile.profile_id !== session.view.controlProfileId ||
    packageManifest.manifest_sha256 !== session.samplingManifestSha256 ||
    packageValue.control_profile_id !== session.view.controlProfileId ||
    packageValue.sampling_profile_id !== "tiny_ball_temporal_groups_v1" ||
    packageValue.metric_profile_id !== session.view.metricProfileId ||
    packageValue.metric_profile_sha256 !== FEASIBILITY_METRIC_PROFILE_SHA256 ||
    attemptFamilySha256 !== session.view.attemptFamilySha256 ||
    packageValue.check_probe_job_id !== session.view.checkProbeJobId ||
    packageValue.training_eligible !== false ||
    packageValue.qualification_eligible !== false ||
    packageValue.pr4a_pr4b_truth_compatible !== false ||
    packageValue.may_seed_dataset_expansion !== eligibility.eligible ||
    (session.view.dataRole === "check" && eligibility.eligible) ||
    !Array.isArray(packageValue.detector_candidate_evidence) ||
    !sameOrderedValues(packageLineage.jobIds, session.developmentProbeJobIds) ||
    packageLineage.runtimeEnvironmentSha256 !== session.runtimeEnvironmentSha256
  ) {
    throw new Error(
      "Annotation package identity or training boundary is invalid.",
    );
  }
  for (const field of [
    "metric_profile_sha256",
    "frame_evidence_sha256",
    "frame_media_sha256",
    "detector_candidate_evidence_sha256",
    "propagation_reports_sha256",
  ] as const) {
    sha256(packageValue[field], `Package ${field}`);
  }
  parseSessionRequestAuthority(packageValue.session_request_authority, {
    session,
    packageValue,
    packageManifest,
    packageDevelopmentBinding,
  });
  const detectorAuthorities = parseDetectorProbeAuthorities(
    packageValue.detector_probe_authorities,
    {
      session,
      packageValue,
      packageSource,
      packageProfile,
      packageControlProfile,
      packageManifest,
      packageLineage,
      packageCheckAuthority,
    },
  );
  boundedCollection(
    packageValue.effective_annotations,
    "Effective annotations",
    1,
    70,
  );
  const finalAnnotations = parseFinalAnnotations(
    packageValue.effective_annotations,
    session,
  );
  const supplementalFrameIndices = boundedCollection(
    packageValue.supplemental_frame_indices,
    "Supplemental frame indices",
    0,
    20,
  ).map((frameIndex) => integer(frameIndex, "Supplemental frame index"));
  if (
    !sameOrderedValues(
      supplementalFrameIndices,
      [...new Set(supplementalFrameIndices)].sort(
        (left, right) => left - right,
      ),
    )
  ) {
    throw new Error("Supplemental frame indices are not canonical.");
  }
  boundedCollection(
    packageValue.propagation_reports,
    "Propagation reports",
    0,
    20,
  );
  const revisions = parseSealedRevisionChain(
    packageValue.revision_chain,
    session,
  );
  const media = parseImmutableFrameMedia(
    packageValue.frame_media,
    packageSource,
  );
  const rawAnnotations = Object.fromEntries(
    boundedCollection(
      packageValue.effective_annotations,
      "Effective annotations",
      1,
      70,
    ).map((rawAnnotation) => {
      const annotation = record(
        rawAnnotation,
        "Effective annotation authority",
      );
      return [
        integer(annotation.frame_index, "Effective annotation frame index"),
        annotation,
      ];
    }),
  );
  const frameEvidence = parseSealedFrameEvidence(packageValue.frame_evidence, {
    session,
    packageSource,
    packageLineage,
    packageManifest,
    detectorAuthorities,
    media,
    revisions,
    rawAnnotations,
    supplementalFrameIndices,
  });
  if (
    authorityCanonicalSha256(
      packageValue.frame_media,
      "Frame media collection",
    ) !== packageValue.frame_media_sha256
  ) {
    throw new Error("Frame media collection digest is invalid.");
  }
  const rawFrameEvidence = packageValue.frame_evidence as Record<
    string,
    unknown
  >[];
  if (
    pythonCanonicalSha256Sync(
      rawFrameEvidence,
      rawFrameEvidence.flatMap((row, index) =>
        frameEvidenceFloatPaths(row, `$[${index}]`),
      ),
    ) !== packageValue.frame_evidence_sha256
  ) {
    throw new Error("Frame evidence collection digest is invalid.");
  }
  if (
    eligibility.exactFrameMediaSha256 !== packageValue.frame_media_sha256 ||
    eligibility.frameEvidenceSha256 !== packageValue.frame_evidence_sha256 ||
    pythonCanonicalSha256Sync(
      revisions.revisions,
      revisionChainFloatPaths(revisions.revisions),
    ) !== eligibility.revisionChainSha256
  ) {
    throw new Error("Dataset expansion evidence digest is invalid.");
  }
  parseFrameReviewProxyAuthority(packageValue.frame_review_proxy_authority, {
    session,
    packageSource,
    detectorAuthorities,
    frameEvidence,
  });
  const candidateEvidence = parseDetectorCandidateEvidence(
    packageValue.detector_candidate_evidence,
    {
      session,
      packageValue,
      packageSource,
      packageProfile,
      detectorAuthorities,
      frameEvidence,
      revisions,
    },
  );
  const propagationReports = parsePropagationReports(
    packageValue.propagation_reports,
    {
      session,
      packageValue,
      packageManifest,
      frameEvidence,
      revisions,
    },
  );
  const localizablePositiveSeedCount = finalAnnotations.filter(
    ({ annotation }) =>
      annotation.presence === "present" &&
      (annotation.point !== null || annotation.bbox !== null),
  ).length;
  const expectedEligibilityReasons = [
    ...(session.view.dataRole === "check"
      ? ["check_role_is_evaluation_only"]
      : []),
    ...(candidateEvidence.pendingCount + propagationReports.pendingCount > 0
      ? ["pending_suggestion_decisions"]
      : []),
    ...(session.view.dataRole === "development" &&
    localizablePositiveSeedCount === 0
      ? ["no_localizable_positive_seed"]
      : []),
  ];
  const expectedExpansionEligible =
    session.view.dataRole === "development" &&
    expectedEligibilityReasons.length === 0;
  if (
    eligibility.localizablePositiveSeedCount !== localizablePositiveSeedCount ||
    eligibility.pendingDetectorCandidateCount !==
      candidateEvidence.pendingCount ||
    eligibility.pendingPropagationSuggestionCount !==
      propagationReports.pendingCount ||
    eligibility.pendingSuggestionDecisionCount !==
      candidateEvidence.pendingCount + propagationReports.pendingCount ||
    eligibility.eligible !== expectedExpansionEligible ||
    !sameOrderedValues(eligibility.reasons, expectedEligibilityReasons)
  ) {
    throw new Error("Dataset expansion eligibility evidence is inconsistent.");
  }
  const primaryFrameIndices = Array.isArray(packageManifest.frame_indices)
    ? packageManifest.frame_indices.map((frameIndex) =>
        integer(frameIndex, "Package primary frame index"),
      )
    : [];
  const expectedFrameIndices = [
    ...primaryFrameIndices,
    ...supplementalFrameIndices,
  ].sort((left, right) => left - right);
  if (
    !sameOrderedValues(
      finalAnnotations.map((annotation) => annotation.frameIndex),
      expectedFrameIndices,
    ) ||
    !sameOrderedValues(
      media.map((row) => row.frameIndex),
      expectedFrameIndices,
    ) ||
    !sameOrderedValues(
      frameEvidence.map((row) => row.frameIndex),
      expectedFrameIndices,
    ) ||
    !sameOrderedValues(
      [...revisions.revisionsByFrame.keys()].sort(
        (left, right) => left - right,
      ),
      finalAnnotations.map((annotation) => annotation.frameIndex),
    )
  ) {
    throw new Error(
      "Final package frame collections do not bind sampled roles.",
    );
  }
  const packageSha256 = sha256(
    packageValue.package_sha256,
    "Annotation package SHA-256",
  );
  const canonicalPackageBody = structuredClone(packageValue);
  delete canonicalPackageBody.package_sha256;
  canonicalPackageBody.sampling_manifest = canonicalSamplingManifestForPackage(
    record(canonicalPackageBody.sampling_manifest, "Package sampling manifest"),
  );
  canonicalPackageBody.propagation_reports =
    propagationReports.canonicalReports;
  if (
    pythonCanonicalSha256Sync(
      canonicalPackageBody,
      annotationPackageFloatPaths(canonicalPackageBody),
    ) !== packageSha256
  ) {
    throw new Error("Annotation package digest is invalid.");
  }
  if (
    session.view.finalPackage &&
    session.view.finalPackage.packageSha256 !== packageSha256
  ) {
    throw new Error("Annotation package digest differs from the session.");
  }
  const sameDevelopmentBinding =
    packageDevelopmentBinding?.sessionId ===
      session.view.developmentPackageBinding?.sessionId &&
    packageDevelopmentBinding?.packageSha256 ===
      session.view.developmentPackageBinding?.packageSha256 &&
    packageDevelopmentBinding?.attemptFamilySha256 ===
      session.view.developmentPackageBinding?.attemptFamilySha256;
  if (
    session.view.dataRole === "development"
      ? packageDevelopmentBinding !== null || packageCheckAuthority !== null
      : !sameDevelopmentBinding || packageCheckAuthority === null
  ) {
    throw new Error("Final package role authority is invalid.");
  }

  const unknownFrames = finalAnnotations.filter(
    ({ annotation }) => annotation.presence === "unknown",
  ).length;
  const excludedFrames = finalAnnotations.filter(
    ({ annotation }) => annotation.trainingUse === "excluded",
  ).length;
  const absentFrames = finalAnnotations.filter(
    ({ annotation }) => annotation.presence === "absent",
  ).length;
  const localizableFrames = finalAnnotations.filter(
    ({ annotation }) =>
      annotation.presence === "present" && annotation.point !== null,
  ).length;
  const developmentScaleCounts = Object.fromEntries(
    SCALE_STRATA.map((key) => [
      key,
      finalAnnotations.filter(
        ({ annotation }) => annotation.scaleStratum === key,
      ).length,
    ]),
  );
  const developmentLightingCounts = Object.fromEntries(
    LIGHTING_STRATA.map((key) => [
      key,
      finalAnnotations.filter(
        ({ annotation }) => annotation.lightingTag === key,
      ).length,
    ]),
  );
  const developmentMotionCounts = Object.fromEntries(
    MOTION_STRATA.map((key) => [
      key,
      finalAnnotations.filter(({ annotation }) =>
        key === "none"
          ? annotation.motionOcclusionTags.length === 0
          : annotation.motionOcclusionTags.includes(key),
      ).length,
    ]),
  );

  const report = record(result.feasibility_report, "Feasibility report");
  if (session.view.dataRole === "development") {
    exactKeys(
      report,
      [
        "schema_version",
        "artifact_type",
        "session_id",
        "attempt_family_sha256",
        "development_package_binding",
        "status",
        "reason",
        "sealed_evidence",
        "authorizations",
        "report_sha256",
      ],
      "Development feasibility report",
    );
    const sealed = record(
      report.sealed_evidence,
      "Development sealed evidence",
    );
    exactKeys(
      sealed,
      [
        "annotation_package_sha256",
        "attempt_family_sha256",
        "check_probe_job_id",
        "check_probe_report_sha256",
        "dataset_expansion_eligibility",
        "sampling_manifest_sha256",
      ],
      "Development sealed evidence",
    );
    if (
      report.schema_version !== "1.0" ||
      report.artifact_type !== "ball_feasibility_report" ||
      report.session_id !== session.view.sessionId ||
      report.attempt_family_sha256 !== attemptFamilySha256 ||
      report.development_package_binding !== null ||
      report.status !== "not_applicable" ||
      report.reason !== "development_package_is_not_one_time_check_evidence" ||
      sealed.annotation_package_sha256 !== packageSha256 ||
      sealed.attempt_family_sha256 !== attemptFamilySha256 ||
      sealed.sampling_manifest_sha256 !== session.samplingManifestSha256 ||
      sealed.check_probe_job_id !== null ||
      sealed.check_probe_report_sha256 !== null
    ) {
      throw new Error("Development sealed evidence is invalid.");
    }
    const sealedEligibility = parseDatasetExpansionEligibility(
      sealed.dataset_expansion_eligibility,
    );
    parseAuthorizations(report.authorizations, false);
    const developmentReportBody = { ...report };
    delete developmentReportBody.report_sha256;
    const reportSha256 = sha256(report.report_sha256, "Report SHA-256");
    if (
      !sameCanonicalValue(sealedEligibility, eligibility) ||
      authorityCanonicalSha256(
        developmentReportBody,
        "Development feasibility report",
      ) !== reportSha256
    ) {
      throw new Error("Development feasibility report digest is invalid.");
    }
    return {
      packageSha256,
      reportSha256,
      dashboard: {
        status: "not_applicable",
        totalFrames: finalAnnotations.length,
        annotatedFrames: finalAnnotations.length,
        confirmedLocalizablePositiveFrames: localizableFrames,
        confirmedAbsentFrames: absentFrames,
        unknownFrames,
        excludedFrames,
        unconfirmedSuggestions: eligibility.pendingSuggestionDecisionCount,
        applicableScaleStrata: [],
        applicableLightingStrata: [],
        scalePositiveCounts: developmentScaleCounts,
        lightingPositiveCounts: developmentLightingCounts,
        motionPositiveCounts: developmentMotionCounts,
        missingStrata: [],
        reasonCodes: [report.reason as string],
        contradictions: [],
        requiresNewAttempt: false,
        datasetExpansionEligibility: eligibility,
        authorityGates: {
          developmentPackageBound: true,
          checkProbeBound: false,
          sealedEvidenceBound: true,
        },
        rawCounts: {
          top1Matches: 0,
          top5Matches: 0,
          evaluablePositives: 0,
          falseCandidates: 0,
          evaluableFrames: 0,
          rawCandidates: 0,
          candidateBudget: 5,
        },
        intervals: {
          method: "not_applicable_to_development_evidence",
          top1RecallLower: 0,
          top5RecallLower: 0,
          falseCandidatesPerFrameUpper: 0,
        },
        lockedAttempt: {
          sessionId: session.view.sessionId,
          profileId: session.view.lockedProfile.profileId,
          profileSha256: session.view.lockedProfile.profileSha256,
          metricProfileId: session.view.metricProfileId,
          dataRole: "development",
          revealed: false,
        },
      },
    };
  }

  exactKeys(
    report,
    [
      "schema_version",
      "artifact_type",
      "session_id",
      "attempt_family_sha256",
      "development_package_binding",
      "source_sha256",
      "locked_profile_id",
      "locked_profile_sha256",
      "sampling_manifest_sha256",
      "metric_profile",
      "metric_profile_sha256",
      "computed_source_px_bounds",
      "status",
      "support",
      "metrics",
      "strata_metrics",
      "frames",
      "contradictions",
      "resolution",
      "authorizations",
      "limitations",
      "sealed_evidence",
      "report_sha256",
    ],
    "Check feasibility report",
  );
  const status = oneOf(
    report.status,
    [
      "insufficient_evidence",
      "feasibility_failed",
      "feasibility_passed",
    ] as const,
    "Feasibility status",
  );
  const reportDevelopmentBinding = parseDevelopmentPackageBinding(
    report.development_package_binding,
  );
  if (
    report.schema_version !== "1.0" ||
    report.artifact_type !== "ball_feasibility_report" ||
    report.session_id !== session.view.sessionId ||
    report.attempt_family_sha256 !== attemptFamilySha256 ||
    reportDevelopmentBinding?.packageSha256 !==
      packageDevelopmentBinding?.packageSha256 ||
    report.source_sha256 !== session.view.source.sourceSha256 ||
    report.locked_profile_id !== session.view.lockedProfile.profileId ||
    report.locked_profile_sha256 !== session.view.lockedProfile.profileSha256 ||
    report.sampling_manifest_sha256 !== session.samplingManifestSha256 ||
    !Array.isArray(report.frames) ||
    !Array.isArray(report.limitations) ||
    report.limitations.some((item) => typeof item !== "string")
  ) {
    throw new Error("Feasibility report authority is invalid.");
  }
  const reportMetricProfileSha256 = sha256(
    report.metric_profile_sha256,
    "Report metric profile SHA-256",
  );
  const { profile, intervals } = parseFeasibilityMetricProfile(
    report.metric_profile,
  );
  if (
    pythonCanonicalSha256Sync(profile, feasibilityMetricProfileFloatPaths()) !==
      reportMetricProfileSha256 ||
    reportMetricProfileSha256 !== FEASIBILITY_METRIC_PROFILE_SHA256
  ) {
    throw new Error("Feasibility metric profile digest is invalid.");
  }
  const bounds = parseFeasibilitySourceBounds(
    report.computed_source_px_bounds,
    session.view.source.height,
  );
  const reportFrames = parseCheckFeasibilityFrames(report.frames, {
    primaryFrameIndices,
    packageManifest,
    finalAnnotations,
    bounds,
  });
  const eligibleReportFrames = reportFrames.filter(
    (frame) => frame.metricEligible,
  );
  const positiveReportFrames = eligibleReportFrames.filter(
    (frame) => frame.presence === "present",
  );
  const absentReportFrames = eligibleReportFrames.filter(
    (frame) => frame.presence === "absent",
  );
  const support = record(report.support, "Feasibility support");
  exactKeys(
    support,
    [
      "total_frames",
      "localizable_positives",
      "confirmed_absent",
      "excluded_or_unresolvable",
      "scale",
      "lighting",
      "applicable_scale_strata",
      "applicable_lighting_strata",
      "missing",
    ],
    "Feasibility support",
  );
  const totalFrames = integer(support.total_frames, "Support total frames");
  const localizable = integer(
    support.localizable_positives,
    "Localizable positives",
  );
  const absent = integer(support.confirmed_absent, "Confirmed absent");
  const excluded = integer(support.excluded_or_unresolvable, "Excluded frames");
  if (
    totalFrames !== reportFrames.length ||
    localizable !== positiveReportFrames.length ||
    absent !== absentReportFrames.length ||
    excluded !== reportFrames.length - eligibleReportFrames.length
  ) {
    throw new Error("Feasibility support counts are inconsistent.");
  }
  const scale = record(support.scale, "Scale support");
  const lighting = record(support.lighting, "Lighting support");
  exactKeys(scale, SCALE_STRATA, "Scale support");
  exactKeys(lighting, LIGHTING_STRATA, "Lighting support");
  const scaleCounts = Object.fromEntries(
    SCALE_STRATA.map((key) => [key, integer(scale[key], `Scale ${key}`)]),
  );
  const lightingCounts = Object.fromEntries(
    LIGHTING_STRATA.map((key) => [
      key,
      integer(lighting[key], `Lighting ${key}`),
    ]),
  );
  if (
    !Array.isArray(support.applicable_scale_strata) ||
    !Array.isArray(support.applicable_lighting_strata) ||
    !Array.isArray(support.missing) ||
    support.missing.some((item) => typeof item !== "string") ||
    !sameOrderedValues(
      support.applicable_scale_strata,
      session.applicableScaleStrata ?? [],
    ) ||
    !sameOrderedValues(
      support.applicable_lighting_strata,
      session.applicableLightingStrata ?? [],
    ) ||
    SCALE_STRATA.some(
      (key) =>
        scaleCounts[key] !==
        positiveReportFrames.filter((frame) => frame.observedScale === key)
          .length,
    ) ||
    LIGHTING_STRATA.some(
      (key) =>
        lightingCounts[key] !==
        positiveReportFrames.filter((frame) => frame.observedLighting === key)
          .length,
    )
  ) {
    throw new Error("Feasibility strata support is invalid.");
  }
  const metrics = record(report.metrics, "Feasibility metrics");
  exactKeys(
    metrics,
    [
      "top1_recall",
      "top5_recall",
      "false_candidates_per_evaluable_frame",
      "candidates_per_evaluable_frame",
      "raw_candidates_per_evaluable_frame",
    ],
    "Feasibility metrics",
  );
  const top1 = parseRawMetric(
    metrics.top1_recall,
    "Top-1 recall",
    "one_sided_95_lower",
  );
  const top5 = parseRawMetric(
    metrics.top5_recall,
    "Top-5 recall",
    "one_sided_95_lower",
  );
  const falseCandidates = parseRawMetric(
    metrics.false_candidates_per_evaluable_frame,
    "False candidates",
    "one_sided_95_upper",
  );
  const candidates = parseRawMetric(
    metrics.candidates_per_evaluable_frame,
    "Candidates",
  );
  const rawCandidates = parseRawMetric(
    metrics.raw_candidates_per_evaluable_frame,
    "Raw candidates",
  );
  const evaluableFrames = localizable + absent;
  const expectedTop1Matches = positiveReportFrames.filter(
    (frame) => frame.top1Hit,
  ).length;
  const expectedTop5Matches = positiveReportFrames.filter(
    (frame) => frame.top5Hit,
  ).length;
  const expectedScoredCandidates = eligibleReportFrames.reduce(
    (sum, frame) => sum + frame.scoredCandidateCount,
    0,
  );
  const expectedRawCandidates = eligibleReportFrames.reduce(
    (sum, frame) => sum + frame.rawCandidateCount,
    0,
  );
  const expectedFalseCandidates = eligibleReportFrames.reduce(
    (sum, frame) =>
      sum +
      (frame.presence === "present"
        ? frame.scoredCandidateCount - Number(Boolean(frame.top5Hit))
        : frame.scoredCandidateCount),
    0,
  );
  if (
    top1.denominator !== localizable ||
    top5.denominator !== localizable ||
    falseCandidates.denominator !== evaluableFrames ||
    candidates.denominator !== evaluableFrames ||
    rawCandidates.denominator !== evaluableFrames ||
    top1.numerator !== expectedTop1Matches ||
    top5.numerator !== expectedTop5Matches ||
    falseCandidates.numerator !== expectedFalseCandidates ||
    candidates.numerator !== expectedScoredCandidates ||
    rawCandidates.numerator !== expectedRawCandidates ||
    top1.numerator > top5.numerator ||
    candidates.numerator > rawCandidates.numerator ||
    candidates.numerator > 5 * evaluableFrames ||
    falseCandidates.numerator !== candidates.numerator - top5.numerator
  ) {
    throw new Error(
      "Feasibility metrics are inconsistent with frame evidence.",
    );
  }
  const strataMetrics = record(report.strata_metrics, "Strata metrics");
  exactKeys(
    strataMetrics,
    ["scale", "lighting", "motion_occlusion"],
    "Strata metrics",
  );
  const motionCounts = parseStratumPositiveCounts(
    strataMetrics.motion_occlusion,
    MOTION_STRATA,
    "Motion strata",
  );
  const scaleMetricCounts = parseStratumPositiveCounts(
    strataMetrics.scale,
    SCALE_STRATA,
    "Scale strata",
  );
  const lightingMetricCounts = parseStratumPositiveCounts(
    strataMetrics.lighting,
    LIGHTING_STRATA,
    "Lighting strata",
  );
  if (
    SCALE_STRATA.some(
      (key) =>
        scaleMetricCounts[key] !==
        positiveReportFrames.filter((frame) => frame.observedScale === key)
          .length,
    ) ||
    LIGHTING_STRATA.some(
      (key) =>
        lightingMetricCounts[key] !==
        positiveReportFrames.filter((frame) => frame.observedLighting === key)
          .length,
    ) ||
    MOTION_STRATA.some(
      (key) =>
        motionCounts[key] !==
        positiveReportFrames.filter((frame) =>
          key === "none"
            ? frame.motionTags.length === 0
            : frame.motionTags.includes(key),
        ).length,
    )
  ) {
    throw new Error("Feasibility strata metrics differ from frame evidence.");
  }
  const contradictions = Array.isArray(report.contradictions)
    ? report.contradictions.map((rawContradiction) => {
        const contradiction = record(rawContradiction, "Contradiction");
        exactKeys(
          contradiction,
          ["frame_index", "diagnostic_codes"],
          "Contradiction",
        );
        if (
          !Array.isArray(contradiction.diagnostic_codes) ||
          contradiction.diagnostic_codes.some(
            (code) => typeof code !== "string",
          )
        ) {
          throw new Error("Contradiction diagnostics are invalid.");
        }
        return {
          frameIndex: integer(contradiction.frame_index, "Contradiction frame"),
          diagnosticCodes: contradiction.diagnostic_codes as string[],
        };
      })
    : (() => {
        throw new Error("Contradictions are invalid.");
      })();
  const resolution = record(report.resolution, "Feasibility resolution");
  exactKeys(
    resolution,
    [
      "raw_annotation_plausibility_contradiction_count",
      "raw_lighting_mismatch_count",
      "raw_scale_mismatch_count",
      "reason_codes",
      "requires_new_attempt",
    ],
    "Feasibility resolution",
  );
  if (
    !Array.isArray(resolution.reason_codes) ||
    resolution.reason_codes.some((reason) => typeof reason !== "string") ||
    typeof resolution.requires_new_attempt !== "boolean"
  ) {
    throw new Error("Feasibility resolution is invalid.");
  }
  const expectedResolutionCounts = {
    raw_annotation_plausibility_contradiction_count: reportFrames.filter(
      (frame) =>
        frame.diagnosticCodes.some((code) =>
          [
            "bbox_diagonal_below_minimum",
            "bbox_diagonal_above_maximum",
            "bbox_aspect_ratio_out_of_bounds",
          ].includes(code),
        ),
    ).length,
    raw_scale_mismatch_count: reportFrames.reduce(
      (sum, frame) =>
        sum +
        frame.diagnosticCodes.filter((code) =>
          code.startsWith("scale_stratum_mismatch:"),
        ).length,
      0,
    ),
    raw_lighting_mismatch_count: reportFrames.reduce(
      (sum, frame) =>
        sum +
        frame.diagnosticCodes.filter((code) =>
          code.startsWith("lighting_stratum_mismatch:"),
        ).length,
      0,
    ),
  };
  const expectedResolutionReasons = [
    ...(expectedResolutionCounts.raw_annotation_plausibility_contradiction_count
      ? ["annotation_plausibility_contradiction"]
      : []),
    ...(expectedResolutionCounts.raw_scale_mismatch_count
      ? ["scale_strata_mismatch"]
      : []),
    ...(expectedResolutionCounts.raw_lighting_mismatch_count
      ? ["lighting_strata_mismatch"]
      : []),
  ];
  if (
    integer(
      resolution.raw_annotation_plausibility_contradiction_count,
      "Annotation plausibility contradiction count",
    ) !==
      expectedResolutionCounts.raw_annotation_plausibility_contradiction_count ||
    integer(resolution.raw_scale_mismatch_count, "Scale mismatch count") !==
      expectedResolutionCounts.raw_scale_mismatch_count ||
    integer(
      resolution.raw_lighting_mismatch_count,
      "Lighting mismatch count",
    ) !== expectedResolutionCounts.raw_lighting_mismatch_count ||
    !sameOrderedValues(resolution.reason_codes, expectedResolutionReasons) ||
    resolution.requires_new_attempt !== expectedResolutionReasons.length > 0 ||
    !sameCanonicalValue(
      contradictions,
      reportFrames
        .filter((frame) => frame.diagnosticCodes.length > 0)
        .map((frame) => ({
          frameIndex: frame.frameIndex,
          diagnosticCodes: frame.diagnosticCodes,
        })),
    )
  ) {
    throw new Error("Feasibility resolution differs from frame evidence.");
  }
  const applicableScaleStrata = support.applicable_scale_strata as string[];
  const applicableLightingStrata =
    support.applicable_lighting_strata as string[];
  const expectedMissing = [
    ...(reportFrames.length < 20 || reportFrames.length > 50
      ? ["total_frame_support"]
      : []),
    ...(localizable < 15 ? ["localizable_positive_support"] : []),
    ...(absent < 5 ? ["confirmed_absent_support"] : []),
    ...applicableScaleStrata.flatMap((key) =>
      (scaleCounts[key] ?? 0) < 3 ? [`scale:${key}`] : [],
    ),
    ...SCALE_STRATA.flatMap((key) =>
      !applicableScaleStrata.includes(key) && scaleCounts[key] > 0
        ? [`applicability_contradiction:scale:${key}`]
        : [],
    ),
    ...applicableLightingStrata.flatMap((key) =>
      (lightingCounts[key] ?? 0) < 3 ? [`lighting:${key}`] : [],
    ),
    ...LIGHTING_STRATA.flatMap((key) =>
      !applicableLightingStrata.includes(key) && lightingCounts[key] > 0
        ? [`applicability_contradiction:lighting:${key}`]
        : [],
    ),
    ...(expectedResolutionCounts.raw_annotation_plausibility_contradiction_count
      ? ["annotation_plausibility_contradiction"]
      : []),
    ...(expectedResolutionCounts.raw_scale_mismatch_count
      ? ["scale_strata_mismatch"]
      : []),
    ...(expectedResolutionCounts.raw_lighting_mismatch_count
      ? ["lighting_strata_mismatch"]
      : []),
  ];
  const expectedStatus =
    expectedMissing.length > 0
      ? "insufficient_evidence"
      : top1.pointEstimate >= 0.6 && top5.pointEstimate >= 0.8
        ? "feasibility_passed"
        : "feasibility_failed";
  if (
    !sameOrderedValues(support.missing as string[], expectedMissing) ||
    status !== expectedStatus
  ) {
    throw new Error("Feasibility status differs from recomputed evidence.");
  }
  parseAuthorizations(report.authorizations, status === "feasibility_passed");
  const sealed = record(report.sealed_evidence, "Check sealed evidence");
  exactKeys(
    sealed,
    [
      "annotation_package_sha256",
      "attempt_family_sha256",
      "development_annotation_session_id",
      "development_annotation_package_sha256",
      "dataset_expansion_eligibility",
      "sampling_lock_sha256",
      "sampling_manifest_sha256",
      "check_probe_job_id",
      "check_probe_report_sha256",
    ],
    "Check sealed evidence",
  );
  const packageCheckJobId = safeId(
    packageCheckAuthority?.job_id,
    "Package check authority job ID",
  );
  const packageCheckReportSha256 = sha256(
    packageCheckAuthority?.report_sha256,
    "Package check authority report SHA-256",
  );
  const sealedEligibility = parseDatasetExpansionEligibility(
    sealed.dataset_expansion_eligibility,
  );
  const sealedEvidenceBound =
    sealed.annotation_package_sha256 === packageSha256 &&
    sealed.attempt_family_sha256 === attemptFamilySha256 &&
    sealed.sampling_manifest_sha256 === session.samplingManifestSha256 &&
    sealed.development_annotation_session_id ===
      packageDevelopmentBinding?.sessionId &&
    sealed.development_annotation_package_sha256 ===
      packageDevelopmentBinding?.packageSha256 &&
    sealed.check_probe_job_id === packageCheckJobId &&
    sealed.check_probe_report_sha256 === packageCheckReportSha256 &&
    JSON.stringify(sealedEligibility) === JSON.stringify(eligibility);
  if (!sealedEvidenceBound) {
    throw new Error("Check sealed evidence authority is invalid.");
  }
  const checkProbeBound =
    packageCheckJobId === session.view.checkProbeAuthority?.jobId &&
    packageCheckReportSha256 === session.view.checkProbeAuthority?.reportSha256;
  if (!checkProbeBound) {
    throw new Error("Check probe authority differs from the active session.");
  }
  const expectedLimitations = [
    "one_time_directional_feasibility_only",
    "small_support_is_exploratory",
    "revealed_group_must_be_retired_for_all_profiles",
  ];
  if (
    !sameOrderedValues(report.limitations as unknown[], expectedLimitations)
  ) {
    throw new Error("Feasibility limitations are invalid.");
  }
  const checkReportBody = structuredClone(report);
  delete checkReportBody.report_sha256;
  const reportSha256 = sha256(report.report_sha256, "Report SHA-256");
  if (
    pythonCanonicalSha256Sync(
      checkReportBody,
      checkFeasibilityReportFloatPaths(checkReportBody),
    ) !== reportSha256
  ) {
    throw new Error("Check feasibility report digest is invalid.");
  }
  return {
    packageSha256,
    reportSha256,
    dashboard: {
      status,
      totalFrames,
      annotatedFrames: finalAnnotations.length,
      confirmedLocalizablePositiveFrames: localizable,
      confirmedAbsentFrames: absent,
      unknownFrames,
      excludedFrames: excluded,
      unconfirmedSuggestions: eligibility.pendingSuggestionDecisionCount,
      applicableScaleStrata: support.applicable_scale_strata as string[],
      applicableLightingStrata: support.applicable_lighting_strata as string[],
      scalePositiveCounts: scaleCounts,
      lightingPositiveCounts: lightingCounts,
      motionPositiveCounts: motionCounts,
      missingStrata: support.missing as string[],
      reasonCodes: resolution.reason_codes as string[],
      contradictions,
      requiresNewAttempt: resolution.requires_new_attempt,
      datasetExpansionEligibility: eligibility,
      authorityGates: {
        developmentPackageBound: sameDevelopmentBinding,
        checkProbeBound,
        sealedEvidenceBound,
      },
      rawCounts: {
        top1Matches: top1.numerator,
        top5Matches: top5.numerator,
        evaluablePositives: localizable,
        falseCandidates: falseCandidates.numerator,
        evaluableFrames,
        rawCandidates: rawCandidates.numerator,
        candidateBudget: integer(
          profile.candidate_budget,
          "Candidate budget",
          1,
        ),
      },
      intervals: {
        method: `${stringValue(intervals.recall, "Recall interval")} / ${stringValue(intervals.false_candidates, "False-candidate interval")}`,
        top1RecallLower: top1.interval!,
        top5RecallLower: top5.interval!,
        falseCandidatesPerFrameUpper: falseCandidates.interval!,
      },
      lockedAttempt: {
        sessionId: session.view.sessionId,
        profileId: session.view.lockedProfile.profileId,
        profileSha256: session.view.lockedProfile.profileSha256,
        metricProfileId: session.view.metricProfileId,
        dataRole: "check",
        revealed: true,
      },
    },
  };
}

function sha256(value: unknown, label: string) {
  if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value;
}

async function arrayBufferSha256(bytes: ArrayBuffer) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function readBoundedFrameBytes(response: Response, signal?: AbortSignal) {
  if (!response.body) {
    return response.arrayBuffer();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let aborted = signal?.aborted === true;
  const abort = () => {
    aborted = true;
    void reader.cancel(signal?.reason).catch(() => undefined);
  };
  signal?.addEventListener("abort", abort, { once: true });
  try {
    while (!aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value?.byteLength) continue;
      if (total + value.byteLength > MAX_FRAME_BYTES) {
        await reader.cancel("Frame response exceeded the bounded limit.");
        throw new Error("Frame response size is outside the bounded limit.");
      }
      chunks.push(value);
      total += value.byteLength;
    }
    if (aborted) {
      throw signal?.reason instanceof Error
        ? signal.reason
        : new DOMException("The operation was aborted.", "AbortError");
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes.buffer;
}

export async function fetchVerifiedBallAnnotationFrame({
  sessionId,
  frameIndex,
  expectedSha256,
  signal,
}: {
  sessionId: string;
  frameIndex: number;
  expectedSha256: string;
  signal?: AbortSignal;
}) {
  const normalizedSessionId = safeId(sessionId, "Annotation session ID");
  if (!Number.isSafeInteger(frameIndex) || frameIndex < 0) {
    throw new Error("Source frame index is invalid.");
  }
  const expectedDigest = sha256(expectedSha256, "Expected frame SHA-256");
  const response = await fetch(
    // Browser API paths intentionally use /api. The app proxy/reverse proxy
    // maps that public prefix to the backend's internal /api/v1 prefix.
    `/api/ball-annotation-sessions/${encodeURIComponent(
      normalizedSessionId,
    )}/frames/${frameIndex}`,
    {
      method: "GET",
      cache: "no-store",
      headers: { "Cache-Control": "no-store" },
      signal,
    },
  );
  if (!response.ok) {
    throw new Error(
      `Verified frame request failed with HTTP ${response.status}.`,
    );
  }
  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0];
  const cacheControl = response.headers.get("Cache-Control") ?? "";
  const etag = response.headers.get("ETag");
  const contentLengthHeader = response.headers.get("Content-Length");
  const headerDigest = sha256(
    response.headers.get("X-Content-SHA256"),
    "Frame response SHA-256",
  );
  const sourceFrameIndexHeader = response.headers.get("X-Source-Frame-Index");
  const sourceFrameIndex =
    sourceFrameIndexHeader !== null &&
    /^(0|[1-9]\d*)$/.test(sourceFrameIndexHeader)
      ? Number(sourceFrameIndexHeader)
      : null;
  if (
    contentType !== "image/jpeg" ||
    !cacheControl
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .includes("no-store") ||
    !etag ||
    !STRONG_ETAG_PATTERN.test(etag) ||
    etag !== `"${headerDigest}"` ||
    sourceFrameIndex === null ||
    !Number.isSafeInteger(sourceFrameIndex) ||
    sourceFrameIndex !== frameIndex
  ) {
    throw new Error("Frame response headers do not match the frozen frame.");
  }
  let declaredContentLength: number | null = null;
  if (contentLengthHeader !== null) {
    const contentLength = Number(contentLengthHeader);
    if (
      !/^\d+$/.test(contentLengthHeader) ||
      !Number.isSafeInteger(contentLength) ||
      contentLength < 1 ||
      contentLength > MAX_FRAME_BYTES
    ) {
      throw new Error("Frame response size is outside the bounded limit.");
    }
    declaredContentLength = contentLength;
  }
  const bytes = await readBoundedFrameBytes(response, signal);
  if (
    bytes.byteLength < 1 ||
    bytes.byteLength > MAX_FRAME_BYTES ||
    (declaredContentLength !== null &&
      declaredContentLength !== bytes.byteLength)
  ) {
    throw new Error("Frame response size is outside the bounded limit.");
  }
  const actualDigest = await arrayBufferSha256(bytes);
  if (actualDigest !== headerDigest || actualDigest !== expectedDigest) {
    throw new Error("Frame content digest does not match the frozen frame.");
  }
  const blob = new Blob([bytes], { type: contentType });
  return {
    objectUrl: URL.createObjectURL(blob),
    contentSha256: actualDigest,
    etag,
    contentType,
    sizeBytes: bytes.byteLength,
  };
}
