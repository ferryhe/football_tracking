import type {
  DetectorProbeBoxView,
  DetectorProbeJobView,
  DetectorProbeModelView,
  DetectorProbeProfileEvidenceView,
} from "@/components/production/ProductionDetectorProbePanel";
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const EMPTY_JSON_SHA256 =
  "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a";

export interface DetectorProbeCreateRequestView {
  parent_trial_id: string;
  profile_ids: string[];
  frame_indices?: number[];
  top_k: 5;
  retry_from_job_id?: string;
}

export interface DetectorProbeCreateEnvelopeView {
  jobId: string;
  requestSha256: string;
  status:
    | "queued"
    | "running"
    | "committing"
    | "ready"
    | "failed"
    | "cancelled"
    | "blocked";
  retryFromJobId: string | null;
}

interface BuildDetectorProbeRequestInput {
  parentTrialId: string;
  profileIds: string[];
  frameIndices?: number[];
  retryFromJobId?: string;
}

interface DetectorProbeRecoveryEligibilityInput {
  monitoredRunId: string;
  diagnosisRunId: string | null;
  authoritativeRun: {
    runId: string;
    status: string;
  } | null;
  gate: {
    status: string;
    coverageComplete: boolean;
    failureCode: string;
    coverageStatus: string | null;
    reconciliationStatus: string | null;
    evaluatedFrames: {
      status: string;
      value?: number | null;
    } | null;
    lostFrames: { status: string; value?: number | null } | null;
    rawCandidates: { status: string; value?: number | null } | null;
  } | null;
}

export function detectorProbeRecoveryEligible({
  monitoredRunId,
  diagnosisRunId,
  authoritativeRun,
  gate,
}: DetectorProbeRecoveryEligibilityInput): boolean {
  if (
    !authoritativeRun ||
    authoritativeRun.runId !== monitoredRunId ||
    authoritativeRun.status !== "completed" ||
    diagnosisRunId !== monitoredRunId ||
    !gate ||
    gate.status !== "retune_required" ||
    gate.coverageComplete !== true ||
    gate.coverageStatus !== "complete" ||
    gate.reconciliationStatus !== "reconciled" ||
    ![
      "no_raw_candidates",
      "all_candidates_class_rejected",
      "all_candidates_filtered",
      "no_tracklets",
      "all_lost",
      "wrong_or_noisy_candidates",
      "unstable_tracking",
    ].includes(gate.failureCode) ||
    gate.evaluatedFrames?.status !== "collected" ||
    gate.lostFrames?.status !== "collected"
  ) {
    return false;
  }
  const evaluated = gate.evaluatedFrames.value;
  const lost = gate.lostFrames.value;
  const rawCandidates = gate.rawCandidates?.value;
  const allLost =
    typeof evaluated === "number" &&
    typeof lost === "number" &&
    Number.isSafeInteger(evaluated) &&
    Number.isSafeInteger(lost) &&
    evaluated > 0 &&
    lost === evaluated &&
    gate.rawCandidates?.status === "collected" &&
    typeof rawCandidates === "number" &&
    Number.isSafeInteger(rawCandidates) &&
    rawCandidates >= 0;
  if (!allLost) return false;
  if (gate.failureCode !== "no_raw_candidates") return true;
  return rawCandidates === 0;
}

export function detectorProbeStorageKey(
  workflowId: string,
  parentTrialId: string,
): string {
  return `football-tracking.production-detector-probe.v1.${safeId(
    workflowId,
    "workflow ID",
  )}.${safeId(parentTrialId, "parent trial ID")}`;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is not an object`);
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
    throw new Error(`${label} fields are invalid`);
  }
}

function string(value: unknown, label: string) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${label} is missing`);
  }
  return value;
}

function safeId(value: unknown, label: string) {
  const candidate = string(value, label);
  if (
    candidate !== candidate.trim() ||
    candidate.length > 120 ||
    !/^[a-z0-9][a-z0-9._-]*$/.test(candidate)
  ) {
    throw new Error(`${label} is invalid`);
  }
  return candidate;
}

function relativePath(value: unknown, label: string) {
  const candidate = string(value, label);
  const segments = candidate.split("/");
  if (
    candidate.includes("\\") ||
    candidate.startsWith("/") ||
    /^[a-zA-Z]:/.test(candidate) ||
    segments.some((segment) => !segment || segment === "." || segment === "..")
  ) {
    throw new Error(`${label} is not a safe relative path`);
  }
  return candidate;
}

function canonicalJsonValue(value: unknown, label: string): unknown {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error(`${label} is not finite JSON`);
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) =>
      canonicalJsonValue(item, `${label}[${index}]`),
    );
  }
  const object = record(value, label);
  return Object.fromEntries(
    Object.keys(object)
      .sort()
      .map((key) => [key, canonicalJsonValue(object[key], `${label}.${key}`)]),
  );
}

function canonicalIdentity(value: unknown, label: string) {
  return JSON.stringify(canonicalJsonValue(value, label));
}

function finiteNumber(value: unknown, label: string) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label} is not finite`);
  }
  return value;
}

function nonNegativeNumber(value: unknown, label: string) {
  const candidate = finiteNumber(value, label);
  if (candidate < 0) throw new Error(`${label} is negative`);
  return candidate;
}

function nonNegativeInteger(value: unknown, label: string) {
  const candidate = nonNegativeNumber(value, label);
  if (!Number.isSafeInteger(candidate))
    throw new Error(`${label} is not an integer`);
  return candidate;
}

function positiveInteger(value: unknown, label: string) {
  const candidate = nonNegativeInteger(value, label);
  if (candidate === 0) throw new Error(`${label} is not positive`);
  return candidate;
}

function boolean(value: unknown, label: string) {
  if (typeof value !== "boolean") throw new Error(`${label} is not boolean`);
  return value;
}

function enumValue<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  label: string,
): T[number] {
  const candidate = string(value, label);
  if (!allowed.includes(candidate)) throw new Error(`${label} is invalid`);
  return candidate as T[number];
}

function artifactUrl(value: unknown, jobId: string, label: string) {
  const artifactId = probeArtifactId(value, jobId, label);
  return `/api/detector-probes/${encodeURIComponent(jobId)}/artifacts/${artifactId}`;
}

function probeArtifactId(value: unknown, jobId: string, label: string) {
  const candidate = string(value, label);
  const internalPrefix = `/api/v1/detector-probes/${encodeURIComponent(jobId)}/artifacts/`;
  const artifactId = candidate.slice(internalPrefix.length);
  if (
    !candidate.startsWith(internalPrefix) ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(artifactId) ||
    artifactId.includes("..")
  ) {
    throw new Error(`${label} is outside the probe artifact allowlist`);
  }
  return artifactId;
}

function sha256(value: unknown, label: string) {
  const candidate = string(value, label);
  if (!SHA256_PATTERN.test(candidate)) throw new Error(`${label} is invalid`);
  return candidate;
}

interface DetectorProbeTuningBinding {
  state: "absent" | "versioned";
  schemaVersion: "1.0";
  versionId: string | null;
  parentVersionId: string | null;
  valuesSha256: string;
}

function tuningBinding(
  value: unknown,
  label: string,
): DetectorProbeTuningBinding {
  const binding = record(value, label);
  const state = enumValue(
    binding.state,
    ["absent", "versioned"] as const,
    `${label} state`,
  );
  if (binding.schema_version !== "1.0") {
    throw new Error(`${label} schema version is invalid`);
  }
  const versionId =
    binding.version_id === null
      ? null
      : string(binding.version_id, `${label} version ID`);
  const parentVersionId =
    binding.parent_version_id === null
      ? null
      : string(binding.parent_version_id, `${label} parent version ID`);
  if (
    versionId !== null &&
    (versionId !== versionId.trim() || versionId.length > 120)
  ) {
    throw new Error(`${label} version ID is invalid`);
  }
  if (
    parentVersionId !== null &&
    (parentVersionId !== parentVersionId.trim() || parentVersionId.length > 120)
  ) {
    throw new Error(`${label} parent version ID is invalid`);
  }
  if (
    (state === "absent" &&
      (versionId !== null ||
        parentVersionId !== null ||
        binding.values_sha256 !== EMPTY_JSON_SHA256)) ||
    (state === "versioned" && versionId === null)
  ) {
    throw new Error(`${label} version lineage is inconsistent`);
  }
  return {
    state,
    schemaVersion: "1.0",
    versionId,
    parentVersionId,
    valuesSha256: sha256(binding.values_sha256, `${label} values SHA-256`),
  };
}

function sameTuningBinding(
  left: DetectorProbeTuningBinding,
  right: DetectorProbeTuningBinding,
) {
  return (
    left.state === right.state &&
    left.schemaVersion === right.schemaVersion &&
    left.versionId === right.versionId &&
    left.parentVersionId === right.parentVersionId &&
    left.valuesSha256 === right.valuesSha256
  );
}

export function buildDetectorProbeRequest({
  parentTrialId,
  profileIds,
  frameIndices,
  retryFromJobId,
}: BuildDetectorProbeRequestInput): DetectorProbeCreateRequestView {
  const normalizedParentTrialId = safeId(parentTrialId, "parent trial ID");
  profileIds.forEach((profileId) => safeId(profileId, "detector profile ID"));
  const uniqueProfiles = [...new Set(profileIds)].sort();
  if (uniqueProfiles.length !== profileIds.length) {
    throw new Error("detector profile IDs contain duplicates");
  }
  if (uniqueProfiles.length < 2 || uniqueProfiles.length > 6) {
    throw new Error("detector probe requires two to six exact profiles");
  }
  const request: DetectorProbeCreateRequestView = {
    parent_trial_id: normalizedParentTrialId,
    profile_ids: uniqueProfiles,
    top_k: 5,
  };
  if (frameIndices !== undefined) {
    const uniqueFrames = [...new Set(frameIndices)].sort(
      (left, right) => left - right,
    );
    if (uniqueFrames.length !== frameIndices.length) {
      throw new Error("detector probe frame indices contain duplicates");
    }
    if (
      uniqueFrames.length < 1 ||
      uniqueFrames.length > 50 ||
      uniqueFrames.some(
        (frameIndex) => !Number.isSafeInteger(frameIndex) || frameIndex < 0,
      )
    ) {
      throw new Error(
        "detector probe requires one to fifty valid frame indices",
      );
    }
    request.frame_indices = uniqueFrames;
  }
  if (retryFromJobId !== undefined) {
    request.retry_from_job_id = safeId(retryFromJobId, "retry job ID");
  }
  return request;
}

export function detectorProbeJobId(value: unknown): string {
  return safeId(
    record(value, "detector probe job envelope").job_id,
    "probe job ID",
  );
}

export function detectorProbeCreateEnvelope(
  value: unknown,
): DetectorProbeCreateEnvelopeView {
  const envelope = record(value, "detector probe create envelope");
  const jobId = safeId(envelope.job_id, "probe job ID");
  const expectedStatusUrl = `/api/v1/detector-probes/${jobId}`;
  if (
    string(envelope.status_url, "probe status URL") !== expectedStatusUrl ||
    string(envelope.cancel_url, "probe cancel URL") !==
      `${expectedStatusUrl}/cancel`
  ) {
    throw new Error("detector probe create URLs do not match the job ID");
  }
  const retryFromJobId =
    envelope.retry_from_job_id === null ||
    envelope.retry_from_job_id === undefined
      ? null
      : safeId(envelope.retry_from_job_id, "create retry job ID");
  if (retryFromJobId === jobId) {
    throw new Error("detector probe create retry cannot reference itself");
  }
  return {
    jobId,
    requestSha256: sha256(envelope.request_sha256, "create request SHA-256"),
    status: enumValue(
      envelope.status,
      [
        "queued",
        "running",
        "committing",
        "ready",
        "failed",
        "cancelled",
        "blocked",
      ] as const,
      "create job status",
    ),
    retryFromJobId,
  };
}

function license(value: unknown, kind: string) {
  const license = record(value, "license");
  const name = string(license.name, "license name");
  const spdx = string(license.spdx_id, "license SPDX ID");
  string(license.url, "license URL");
  return {
    label: spdx && spdx !== name ? `${name} (${spdx})` : name,
    reviewed: boolean(license.reviewed, `${kind} license reviewed`),
    approvedForLocalProbe: boolean(
      license.approved_for_local_probe,
      `${kind} license local-probe approval`,
    ),
  };
}

function optionalRuntimeVersion(value: unknown, name: string) {
  return value === null ? `${name}=missing` : `${name}=${string(value, name)}`;
}

function nullableString(value: unknown, label: string): string | null {
  return value === null ? null : string(value, label);
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${label} is not an array`);
  return value.map((item) => string(item, `${label} item`));
}

interface ParsedModelDescriptor {
  modelId: string;
  version: string;
  imported: boolean;
  descriptorSha256: string;
  weightsSha256: string;
  weightsSizeBytes: number;
  weightsRelativePath: string;
  classMap: Record<string, string>;
  lifecycle:
    | "unverified"
    | "feasibility_passed"
    | "development_candidate"
    | "source_segment_qualified"
    | "camera_qualified"
    | "retired";
}

function modelDescriptor(
  value: unknown,
  label = "detector model descriptor",
): ParsedModelDescriptor {
  const descriptor = record(value, label);
  if (
    string(descriptor.schema_version, `${label} schema version`) !== "1.0" ||
    descriptor.artifact_type !== "detector_model_descriptor"
  ) {
    throw new Error(`${label} contract is invalid`);
  }
  const modelId = safeId(descriptor.model_id, `${label} model ID`);
  const version = safeId(descriptor.version, `${label} version`);
  const modelVersion = string(
    descriptor.model_version,
    `${label} checkpoint model version`,
  );
  const imported =
    descriptor.import_manifest_sha256 !== null &&
    descriptor.import_manifest_sha256 !== undefined;
  string(descriptor.display_name, `${label} display name`);
  string(descriptor.architecture_family, `${label} architecture family`);
  const weights = record(descriptor.weights, `${label} weights`);
  if (imported) {
    exactKeys(
      weights,
      ["relative_path", "sha256", "size_bytes"],
      `${label} weights`,
    );
  }
  const weightsRelativePath = relativePath(
    weights.relative_path,
    `${label} weights relative path`,
  );
  const weightsSha256 = sha256(weights.sha256, `${label} weights SHA-256`);
  const weightsSizeBytes = positiveInteger(
    weights.size_bytes,
    `${label} weights size`,
  );
  const source = record(descriptor.source, `${label} source`);
  [
    "project",
    "version",
    "asset_release",
    "weight_url",
    "acquisition_method",
    "access_requirement",
  ].forEach((field) => string(source[field], `${label} source ${field}`));
  if (imported) {
    exactKeys(
      source,
      [
        "project",
        "version",
        "asset_release",
        "weight_url",
        "acquisition_method",
        "access_requirement",
      ],
      `${label} source`,
    );
  }
  const runtimeContract = record(
    descriptor.runtime_contract,
    `${label} runtime contract`,
  );
  if (imported) {
    if (descriptor.checkpoint !== null && descriptor.checkpoint !== undefined) {
      throw new Error(`${label} imported checkpoint is not allowed`);
    }
    if (
      runtimeContract.validation !== "server_validation_required" ||
      runtimeContract.arbitrary_executable_model_code_allowed !== false
    ) {
      throw new Error(`${label} imported runtime contract is invalid`);
    }
    exactKeys(
      runtimeContract,
      ["validation", "arbitrary_executable_model_code_allowed"],
      `${label} runtime contract`,
    );
  } else {
    const checkpoint = record(descriptor.checkpoint, `${label} checkpoint`);
    string(checkpoint.format_version, `${label} checkpoint format version`);
    string(checkpoint.created_date, `${label} checkpoint created date`);
    ["ultralytics", "sahi", "torch"].forEach((runtime) =>
      string(runtimeContract[runtime], `${label} ${runtime} contract`),
    );
  }
  const classNames = stringArray(
    descriptor.class_names,
    `${label} class names`,
  );
  const rawClassMap = record(descriptor.class_map, `${label} class map`);
  const classMap: Record<string, string> = {};
  Object.entries(rawClassMap).forEach(([key, mapped]) => {
    string(key, `${label} class-map key`);
    classMap[key] = string(mapped, `${label} class-map value`);
  });
  if (
    imported &&
    (classNames.length === 0 ||
      classNames.length > 200 ||
      new Set(classNames).size !== classNames.length ||
      Object.keys(classMap).length === 0 ||
      Object.entries(classMap).some(
        ([className, mapped]) =>
          !classNames.includes(className) || mapped !== "ball",
      ))
  ) {
    throw new Error(`${label} imported class map is invalid`);
  }
  if (
    !classNames.some(
      (className) =>
        classMap[className] === "ball" || className.toLowerCase() === "ball",
    )
  ) {
    throw new Error(`${label} has no bound ball class`);
  }
  const expectedInput = record(
    descriptor.expected_input,
    `${label} expected input`,
  );
  if (imported) {
    exactKeys(
      expectedInput,
      ["image_size", "precision", "device", "source_coordinate_space"],
      `${label} expected input`,
    );
    if (
      positiveInteger(expectedInput.image_size, `${label} image size`) > 8192 ||
      !["fp32", "fp16"].includes(
        string(expectedInput.precision, `${label} precision`),
      ) ||
      !["cpu", "cuda"].includes(string(expectedInput.device, `${label} device`))
    ) {
      throw new Error(`${label} imported expected input is invalid`);
    }
    if ((expectedInput.image_size as number) < 32) {
      throw new Error(`${label} imported image size is invalid`);
    }
  } else {
    positiveInteger(
      expectedInput.direct_image_size,
      `${label} direct image size`,
    );
    positiveInteger(expectedInput.sahi_slice_width, `${label} slice width`);
    positiveInteger(expectedInput.sahi_slice_height, `${label} slice height`);
  }
  if (expectedInput.source_coordinate_space !== "source_pixels_xyxy") {
    throw new Error(`${label} source coordinate contract is invalid`);
  }
  if (imported) {
    if (descriptor.execution !== null && descriptor.execution !== undefined) {
      throw new Error(`${label} imported execution is not allowed`);
    }
    const memoryEnvelope = record(
      descriptor.memory_envelope,
      `${label} memory envelope`,
    );
    exactKeys(
      memoryEnvelope,
      ["max_ram_mb", "max_vram_mb"],
      `${label} memory envelope`,
    );
    if (
      positiveInteger(memoryEnvelope.max_ram_mb, `${label} maximum RAM`) >
        262_144 ||
      nonNegativeInteger(memoryEnvelope.max_vram_mb, `${label} maximum VRAM`) >
        262_144
    ) {
      throw new Error(`${label} imported memory envelope is invalid`);
    }
  } else {
    const execution = record(descriptor.execution, `${label} execution`);
    string(execution.device, `${label} execution device`);
    string(execution.precision, `${label} execution precision`);
    const memoryEnvelope = record(
      execution.memory_envelope,
      `${label} memory envelope`,
    );
    positiveInteger(memoryEnvelope.max_ram_mb, `${label} maximum RAM`);
    positiveInteger(memoryEnvelope.max_vram_mb, `${label} maximum VRAM`);
  }
  const bindings = record(descriptor.bindings, `${label} bindings`);
  if (imported) {
    exactKeys(
      bindings,
      [
        "source_sha256",
        "temporal_group_sha256",
        "camera_profile_sha256",
        "evaluation_package_sha256",
        "threshold_profile_sha256",
        "code_commit",
        "environment_sha256",
      ],
      `${label} bindings`,
    );
  }
  [
    "source_sha256",
    "temporal_group_sha256",
    "camera_profile_sha256",
    "evaluation_package_sha256",
    "threshold_profile_sha256",
    "environment_sha256",
  ].forEach((field) => {
    if (bindings[field] !== null) sha256(bindings[field], `${label} ${field}`);
  });
  if (bindings.code_commit !== null) {
    string(bindings.code_commit, `${label} code commit`);
  }
  const licenses = record(descriptor.licenses, `${label} licenses`);
  if (imported) {
    exactKeys(
      licenses,
      ["dataset", "model", "runtime", "deployment"],
      `${label} licenses`,
    );
    ["dataset", "model", "runtime", "deployment"].forEach((kind) =>
      exactKeys(
        record(licenses[kind], `${label} ${kind} license`),
        ["name", "spdx_id", "url", "reviewed", "approved_for_local_probe"],
        `${label} ${kind} license`,
      ),
    );
  }
  const parsedLicenses = ["dataset", "model", "runtime", "deployment"].map(
    (kind) => license(licenses[kind], kind),
  );
  const egress = record(descriptor.egress, `${label} egress`);
  if (imported) {
    exactKeys(
      egress,
      ["frames_leave_local_machine", "destination", "operator_consent"],
      `${label} egress`,
    );
  }
  const leavesDevice = boolean(
    egress.frames_leave_local_machine,
    `${label} frame egress`,
  );
  const destination = nullableString(
    egress.destination,
    `${label} egress destination`,
  );
  const consent = enumValue(
    egress.operator_consent,
    ["not_required", "granted", "required_not_granted"] as const,
    `${label} egress consent`,
  );
  if (
    (leavesDevice && destination === null) ||
    (!leavesDevice && destination !== null)
  ) {
    throw new Error(`${label} egress binding is unsafe`);
  }
  const lifecycle = enumValue(
    descriptor.lifecycle_state,
    [
      "unverified",
      "feasibility_passed",
      "development_candidate",
      "source_segment_qualified",
      "camera_qualified",
      "retired",
    ] as const,
    `${label} lifecycle`,
  );
  const descriptorSha256 = sha256(
    descriptor.descriptor_sha256,
    `${label} SHA-256`,
  );
  if (imported) {
    sha256(descriptor.import_manifest_sha256, `${label} import manifest`);
    if (
      modelVersion !== version ||
      !["yolov8", "yolo11", "yolo26", "rfdetr", "onnx"].includes(
        string(descriptor.architecture_family, `${label} architecture family`),
      ) ||
      source.version !== source.asset_release ||
      source.weight_url !== "trusted-import://server-lineage-package" ||
      !["trusted_local_package", "server_lineage_package"].includes(
        String(source.acquisition_method),
      ) ||
      source.access_requirement !== "trusted_server_lineage_package" ||
      leavesDevice ||
      destination !== null ||
      consent !== "not_required" ||
      lifecycle !== "unverified" ||
      !parsedLicenses.every(
        (item) => item.reviewed && item.approvedForLocalProbe,
      )
    ) {
      throw new Error(`${label} imported security contract is invalid`);
    }
  }
  return {
    modelId,
    version,
    imported,
    descriptorSha256,
    weightsSha256,
    weightsSizeBytes,
    weightsRelativePath,
    classMap,
    lifecycle,
  };
}

interface ParsedProfileSettings {
  confidenceThreshold: number;
  inputSize: number;
  topK: 5;
  allowedLabels: string[];
  useHalf: boolean;
  tile: {
    width: number;
    height: number;
    overlapWidthRatio: number;
    overlapHeightRatio: number;
  } | null;
}

function profileSettings(
  value: unknown,
  mode: "direct" | "sahi",
  label = "detector profile settings",
): ParsedProfileSettings {
  const settings = record(value, label);
  const confidenceThreshold = finiteNumber(
    settings.confidence_threshold,
    `${label} confidence threshold`,
  );
  if (confidenceThreshold < 0 || confidenceThreshold > 1) {
    throw new Error(`${label} confidence threshold is invalid`);
  }
  const inputSize = positiveInteger(settings.image_size, `${label} image size`);
  const useHalf = boolean(settings.use_half, `${label} half precision`);
  const allowedLabels = stringArray(
    settings.allowed_labels,
    `${label} allowed labels`,
  );
  if (
    allowedLabels.length === 0 ||
    new Set(allowedLabels).size !== allowedLabels.length
  ) {
    throw new Error(`${label} allowed labels are invalid`);
  }
  if (finiteNumber(settings.top_k, `${label} top-k`) !== 5) {
    throw new Error(`${label} top-k is not fixed at five`);
  }
  const tile =
    mode === "sahi"
      ? {
          width: positiveInteger(settings.slice_width, `${label} slice width`),
          height: positiveInteger(
            settings.slice_height,
            `${label} slice height`,
          ),
          overlapWidthRatio: finiteNumber(
            settings.overlap_width_ratio,
            `${label} horizontal overlap`,
          ),
          overlapHeightRatio: finiteNumber(
            settings.overlap_height_ratio,
            `${label} vertical overlap`,
          ),
        }
      : null;
  if (
    tile &&
    (tile.overlapWidthRatio < 0 ||
      tile.overlapWidthRatio >= 1 ||
      tile.overlapHeightRatio < 0 ||
      tile.overlapHeightRatio >= 1)
  ) {
    throw new Error(`${label} tile overlap is invalid`);
  }
  for (const name of [
    "perform_standard_pred",
    "postprocess_type",
    "postprocess_match_metric",
    "postprocess_match_threshold",
  ]) {
    const item = settings[name];
    if (item === undefined || item === null) continue;
    if (name === "perform_standard_pred") boolean(item, `${label} ${name}`);
    else if (name === "postprocess_match_threshold") {
      const threshold = finiteNumber(item, `${label} ${name}`);
      if (threshold < 0 || threshold > 1)
        throw new Error(`${label} ${name} is invalid`);
    } else string(item, `${label} ${name}`);
  }
  return {
    confidenceThreshold,
    inputSize,
    topK: 5,
    tile,
    allowedLabels,
    useHalf,
  };
}

function profileAvailability(value: unknown, label: string) {
  const availability = record(value, label);
  const status = enumValue(
    availability.status,
    ["available", "unavailable", "blocked"] as const,
    `${label} status`,
  );
  const reasonCodes = stringArray(
    availability.reason_codes,
    `${label} reasons`,
  );
  if (status !== "available" && reasonCodes.length === 0) {
    throw new Error(`${label} has no reason`);
  }
  let runtimeLoadSmoke: boolean | null = null;
  if (availability.runtime !== null && availability.runtime !== undefined) {
    const runtime = record(availability.runtime, `${label} runtime`);
    string(runtime.name, `${label} runtime name`);
    if (runtime.installed_version !== null) {
      string(runtime.installed_version, `${label} runtime version`);
    }
    runtimeLoadSmoke = boolean(
      runtime.load_smoke,
      `${label} runtime load smoke`,
    );
  }
  if (status === "available" && runtimeLoadSmoke !== true) {
    throw new Error(`${label} lacks a successful runtime load smoke`);
  }
  return { status, reasonCodes, runtimeLoadSmoke };
}

export function detectorProbeCatalogView(
  value: unknown,
): DetectorProbeModelView[] {
  const catalog = record(value, "detector model catalog");
  if (
    catalog.schema_version !== "1.0" ||
    catalog.artifact_type !== "ball_detector_development_v1"
  ) {
    throw new Error("detector model catalog contract is invalid");
  }
  if (
    !Array.isArray(catalog.models) ||
    !Array.isArray(catalog.profiles) ||
    !Array.isArray(catalog.catalog_findings)
  ) {
    throw new Error("detector model catalog arrays are missing");
  }
  const modelIdentity = (modelId: string, version: string) =>
    `${modelId}:${version}`;
  const rawModelsByIdentity = new Map<string, Record<string, unknown>>();
  for (const rawModel of catalog.models) {
    const model = record(rawModel, "detector model");
    const descriptor = record(model.descriptor, "detector descriptor");
    const parsedDescriptor = modelDescriptor(descriptor);
    const identity = modelIdentity(
      parsedDescriptor.modelId,
      parsedDescriptor.version,
    );
    if (rawModelsByIdentity.has(identity)) {
      throw new Error("duplicate detector model identity");
    }
    rawModelsByIdentity.set(identity, model);
  }
  const profilesByModelIdentity = new Map<string, Record<string, unknown>[]>();
  const profileIds = new Set<string>();
  for (const rawProfile of catalog.profiles) {
    const profile = record(rawProfile, "detector profile");
    if (
      string(profile.schema_version, "profile schema version") !== "1.0" ||
      profile.artifact_type !== "detector_profile"
    ) {
      throw new Error("detector profile contract is invalid");
    }
    const modelId = safeId(profile.model_id, "profile model ID");
    const modelVersion = safeId(profile.model_version, "profile model version");
    const identity = modelIdentity(modelId, modelVersion);
    const profileId = safeId(profile.profile_id, "profile ID");
    if (!rawModelsByIdentity.has(identity)) {
      throw new Error("orphan detector profile");
    }
    if (profileIds.has(profileId))
      throw new Error("duplicate detector profile ID");
    profileIds.add(profileId);
    profilesByModelIdentity.set(identity, [
      ...(profilesByModelIdentity.get(identity) ?? []),
      profile,
    ]);
  }
  const registeredModels = [...rawModelsByIdentity.values()].map((model) => {
    const descriptor = record(model.descriptor, "detector descriptor");
    const parsedDescriptor = modelDescriptor(descriptor);
    const modelId = parsedDescriptor.modelId;
    const availability = record(model.availability, "model availability");
    const qualification = record(model.qualification, "model qualification");
    const source = record(descriptor.source, "model source");
    const licenses = record(descriptor.licenses, "model licenses");
    const egress = record(descriptor.egress, "model egress");
    const availabilityStatus = string(
      availability.status,
      "model availability status",
    );
    const normalizedAvailability = enumValue(
      availabilityStatus,
      ["available", "unavailable", "blocked"] as const,
      "model availability status",
    );
    if (!Array.isArray(availability.reason_codes)) {
      throw new Error("model availability reasons are missing");
    }
    const reasonCodes = availability.reason_codes.map((reason) =>
      string(reason, "model availability reason"),
    );
    if (normalizedAvailability !== "available" && reasonCodes.length === 0) {
      throw new Error("unavailable model has no reason");
    }
    if (
      parsedDescriptor.imported &&
      (normalizedAvailability !== "blocked" ||
        !reasonCodes.includes("server_validation_required"))
    ) {
      throw new Error("imported detector is not blocked for server validation");
    }
    const observations = record(
      availability.observations,
      "model availability observations",
    );
    const observationKeys = [
      "file",
      "digest",
      "class_map",
      "license",
      "runtime_load",
    ];
    const observationsPass = observationKeys.every((key) => {
      const observation = record(
        observations[key],
        `model availability observation ${key}`,
      );
      const status = enumValue(
        observation.status,
        ["pass", "fail", "not_run"] as const,
        `model availability observation ${key} status`,
      );
      string(
        observation.reason,
        `model availability observation ${key} reason`,
      );
      return status === "pass";
    });
    const runtimeObservation = record(
      observations.runtime_load,
      "runtime-load availability observation",
    );
    const installedRuntime =
      runtimeObservation.installed_runtime === null ||
      runtimeObservation.installed_runtime === undefined
        ? null
        : record(
            runtimeObservation.installed_runtime,
            "installed runtime observation",
          );
    const datasetLicense = license(licenses.dataset, "dataset");
    const modelLicense = license(licenses.model, "model");
    const runtimeLicense = license(licenses.runtime, "runtime");
    const deploymentLicense = license(licenses.deployment, "deployment");
    const licensesReviewed = [
      datasetLicense,
      modelLicense,
      runtimeLicense,
      deploymentLicense,
    ].every((item) => item.reviewed && item.approvedForLocalProbe);
    const leavesDevice = boolean(
      egress.frames_leave_local_machine,
      "frames leave local machine",
    );
    const consent = enumValue(
      egress.operator_consent,
      ["not_required", "granted", "required_not_granted"] as const,
      "operator egress consent",
    );
    const destination =
      egress.destination === null
        ? null
        : string(egress.destination, "egress destination");
    if (leavesDevice && destination === null) {
      throw new Error("external egress destination is missing");
    }
    if (!leavesDevice && destination !== null) {
      throw new Error("local model has an external egress destination");
    }
    const egressApproved = !leavesDevice || consent === "granted";
    const trialEligible = boolean(
      qualification.trial_eligible,
      "trial eligibility",
    );
    const sourceSegmentQualified = boolean(
      qualification.source_segment_qualified,
      "source-segment qualification",
    );
    const cameraQualified = boolean(
      qualification.camera_qualified,
      "camera qualification",
    );
    if (
      (cameraQualified && !sourceSegmentQualified) ||
      (sourceSegmentQualified && !trialEligible)
    ) {
      throw new Error("detector qualification lineage is not monotonic");
    }
    const modelSelectable = boolean(
      model.selectable_for_probe,
      "model probe selectability",
    );
    if (
      parsedDescriptor.imported &&
      (modelSelectable ||
        trialEligible ||
        sourceSegmentQualified ||
        cameraQualified)
    ) {
      throw new Error("imported detector cannot be selectable or qualified");
    }
    if (
      modelSelectable &&
      (normalizedAvailability !== "available" ||
        !observationsPass ||
        parsedDescriptor.lifecycle === "retired")
    ) {
      throw new Error("model probe selectability is not supported by evidence");
    }
    const modelProfiles = (
      profilesByModelIdentity.get(
        modelIdentity(modelId, parsedDescriptor.version),
      ) ?? []
    ).map((profile) => {
      if (
        string(profile.schema_version, "profile schema version") !== "1.0" ||
        profile.artifact_type !== "detector_profile"
      ) {
        throw new Error("detector profile contract is invalid");
      }
      const rawProfileAvailability = record(
        profile.availability,
        "profile availability",
      );
      const parsedAvailability = profileAvailability(
        rawProfileAvailability,
        "profile availability",
      );
      const profileStatus = parsedAvailability.status;
      const profileReasons = parsedAvailability.reasonCodes;
      const mode = enumValue(
        profile.mode,
        ["direct", "sahi"] as const,
        "profile mode",
      );
      const settings = profileSettings(profile.settings, mode);
      const modelVersion = safeId(
        profile.model_version,
        "profile model version",
      );
      const profileDescriptorSha = sha256(
        profile.model_descriptor_sha256,
        "profile model descriptor SHA-256",
      );
      if (
        safeId(profile.model_id, "profile model ID") !== modelId ||
        modelVersion !== parsedDescriptor.version ||
        profileDescriptorSha !== parsedDescriptor.descriptorSha256
      ) {
        throw new Error("profile model version does not match its descriptor");
      }
      if (
        settings.allowedLabels.some(
          (label) => parsedDescriptor.classMap[label] !== "ball",
        )
      ) {
        throw new Error(
          "profile allowed labels do not map to the descriptor ball class",
        );
      }
      const profileSelectable = boolean(
        profile.selectable_for_probe,
        "profile probe selectability",
      );
      if (
        profileSelectable &&
        (profileStatus !== "available" ||
          parsedAvailability.runtimeLoadSmoke !== true)
      ) {
        throw new Error(
          "profile probe selectability is not supported by runtime evidence",
        );
      }
      const probeSelectable =
        profileStatus === "available" &&
        normalizedAvailability === "available" &&
        modelSelectable &&
        profileSelectable &&
        observationsPass &&
        licensesReviewed &&
        egressApproved &&
        parsedAvailability.runtimeLoadSmoke === true;
      return {
        profileId: safeId(profile.profile_id, "profile ID"),
        version: string(profile.version, "profile version"),
        digest: sha256(profile.profile_sha256, "profile SHA-256"),
        mode,
        inputSize: settings.inputSize,
        confidenceThreshold: settings.confidenceThreshold,
        tile: settings.tile,
        topK: settings.topK,
        probeSelectable,
        recommended: boolean(profile.recommended, "profile recommendation"),
        unavailableReason: probeSelectable
          ? undefined
          : [
              ...profileReasons,
              ...(!licensesReviewed ? ["licenses_not_reviewed"] : []),
              ...(!egressApproved ? ["egress_not_approved"] : []),
            ].join(", "),
      };
    });
    if (parsedDescriptor.imported && modelProfiles.length !== 0) {
      throw new Error("imported detector cannot expose probe profiles");
    }
    return {
      kind: "registered" as const,
      modelId,
      version: string(descriptor.version, "model version"),
      runtimeVersion: [
        optionalRuntimeVersion(
          installedRuntime?.ultralytics ?? null,
          "ultralytics",
        ),
        optionalRuntimeVersion(installedRuntime?.sahi ?? null, "sahi"),
        optionalRuntimeVersion(installedRuntime?.torch ?? null, "torch"),
      ].join(" · "),
      displayName: string(descriptor.display_name, "model display name"),
      architectureFamily: string(
        descriptor.architecture_family,
        "architecture family",
      ),
      sourceProject: string(source.project, "source project"),
      sourceVersion: `${string(source.version, "source version")} · ${string(
        source.asset_release,
        "source asset release",
      )}`,
      acquisitionMethod: string(
        source.acquisition_method,
        "source acquisition method",
      ),
      accessRequirement:
        string(source.access_requirement, "source access requirement") +
        ` · ${string(source.weight_url, "source weight URL")}`,
      weightsSha256: parsedDescriptor.weightsSha256,
      manifestSha256: parsedDescriptor.descriptorSha256,
      lifecycle: parsedDescriptor.lifecycle,
      trialEligible,
      sourceSegmentQualified,
      cameraQualified,
      availability: normalizedAvailability,
      availabilityReason:
        availabilityStatus === "available" ? undefined : reasonCodes.join(", "),
      datasetLicense: datasetLicense.label,
      modelLicense: modelLicense.label,
      runtimeLicense: runtimeLicense.label,
      deploymentLicense: deploymentLicense.label,
      egress: {
        leavesDevice,
        destination,
        consent,
      },
      profiles: modelProfiles,
    };
  });
  const findingIds = new Set<string>();
  const findings: DetectorProbeModelView[] = catalog.catalog_findings.map(
    (rawFinding) => {
      const finding = record(rawFinding, "public model catalog finding");
      const findingId = safeId(finding.finding_id, "catalog finding ID");
      if (findingIds.has(findingId)) {
        throw new Error("duplicate public catalog finding ID");
      }
      findingIds.add(findingId);
      if (boolean(finding.selectable, "catalog finding selectability")) {
        throw new Error(
          "unacquired public catalog finding cannot be selectable",
        );
      }
      const source = record(finding.source, "catalog finding source");
      const access = record(finding.access, "catalog finding access");
      const availability = record(
        finding.availability,
        "catalog finding availability",
      );
      if (
        enumValue(
          availability.status,
          ["unavailable"] as const,
          "catalog finding availability status",
        ) !== "unavailable"
      ) {
        throw new Error("catalog finding availability is invalid");
      }
      if (!Array.isArray(availability.reason_codes)) {
        throw new Error("catalog finding reasons are missing");
      }
      const reasons = availability.reason_codes.map((reason) =>
        string(reason, "catalog finding reason"),
      );
      if (reasons.length === 0) {
        throw new Error("catalog finding has no unavailability reason");
      }
      const licenses = record(finding.licenses, "catalog finding licenses");
      const findingLicense = (kind: string) => {
        const item = record(licenses[kind], `catalog finding ${kind} license`);
        const status = enumValue(
          item.status,
          ["review_required", "unavailable", "unknown"] as const,
          `catalog finding ${kind} license status`,
        );
        if (
          boolean(item.approved_for_local_probe, `${kind} finding approval`)
        ) {
          throw new Error(
            "unreviewed catalog finding license cannot be approved",
          );
        }
        return status;
      };
      const egress = record(finding.egress, "catalog finding egress");
      enumValue(
        egress.frames_leave_local_machine,
        ["unknown_until_access_method_selected"] as const,
        "catalog finding egress state",
      );
      const consent = enumValue(
        egress.operator_consent,
        ["required_before_external_inference"] as const,
        "catalog finding consent",
      );
      const sourceVersion =
        source.version === null
          ? "unbound"
          : string(source.version, "catalog finding source version");
      return {
        kind: "catalog_finding" as const,
        modelId: findingId,
        version: sourceVersion,
        runtimeVersion: "not_acquired",
        displayName: string(finding.display_name, "catalog finding name"),
        architectureFamily: string(
          finding.architecture_family,
          "catalog finding architecture",
        ),
        sourceProject: string(source.project, "catalog finding project"),
        sourceVersion,
        acquisitionMethod: string(
          access.method,
          "catalog finding access method",
        ),
        accessRequirement: `${string(
          access.account_or_plan_required,
          "catalog finding account requirement",
        )} · local_weights_validated=${String(
          boolean(
            access.local_weights_validated,
            "catalog finding local-weight validation",
          ),
        )} · ${string(source.url, "catalog finding URL")}`,
        weightsSha256: null,
        manifestSha256: null,
        lifecycle: "catalog_finding_only",
        trialEligible: false,
        sourceSegmentQualified: false,
        cameraQualified: false,
        availability: "unavailable",
        availabilityReason: reasons.join(", "),
        datasetLicense: findingLicense("dataset"),
        modelLicense: findingLicense("model"),
        runtimeLicense: findingLicense("runtime"),
        deploymentLicense: findingLicense("deployment"),
        egress: {
          leavesDevice: null,
          destination:
            egress.destination === null
              ? null
              : string(
                  egress.destination,
                  "catalog finding egress destination",
                ),
          consent,
        },
        profiles: [],
      };
    },
  );
  return [...registeredModels, ...findings];
}

interface ParsedDetectorProbeCandidate {
  view: DetectorProbeBoxView;
  evidenceKey: string;
}

function boxView(
  value: unknown,
  frameIndex: number,
  sourceWidth: number,
  sourceHeight: number,
): ParsedDetectorProbeCandidate {
  const candidate = record(value, "detector candidate");
  const candidateFrameIndex = nonNegativeInteger(
    candidate.frame_index,
    "candidate frame index",
  );
  if (candidateFrameIndex !== frameIndex) {
    throw new Error("candidate is not bound to its source frame");
  }
  if (
    !Array.isArray(candidate.bbox_source_px) ||
    candidate.bbox_source_px.length !== 4
  ) {
    throw new Error("candidate source box is invalid");
  }
  const [left, top, right, bottom] = candidate.bbox_source_px.map(
    (coordinate) => finiteNumber(coordinate, "candidate coordinate"),
  );
  if (
    left < 0 ||
    top < 0 ||
    right <= left ||
    bottom <= top ||
    right > sourceWidth ||
    bottom > sourceHeight
  ) {
    throw new Error("candidate box is outside source coordinates");
  }
  const confidence = finiteNumber(candidate.confidence, "candidate confidence");
  if (confidence < 0 || confidence > 1) {
    throw new Error("candidate confidence is outside zero to one");
  }
  const className = enumValue(
    candidate.class_name,
    ["ball"] as const,
    "candidate class",
  );
  const checkpointClassName = string(
    candidate.checkpoint_class_name,
    "candidate checkpoint class",
  );
  const source = string(candidate.source, "candidate source");
  const coordinateReason = enumValue(
    candidate.coordinate_reason,
    ["direct_source_coordinates", "sahi_tile_offset_applied"] as const,
    "candidate coordinate reason",
  );
  const mergeReason = enumValue(
    candidate.merge_reason,
    ["retained_top_k"] as const,
    "candidate merge reason",
  );
  return {
    view: {
      x: left,
      y: top,
      width: right - left,
      height: bottom - top,
      confidence,
      label: className,
    },
    evidenceKey: JSON.stringify([
      candidateFrameIndex,
      left,
      top,
      right,
      bottom,
      confidence,
      className,
      checkpointClassName,
      source,
      coordinateReason,
      mergeReason,
    ]),
  };
}

function profileEvidenceView(
  value: unknown,
  jobId: string,
  frameIndex: number,
  sourceWidth: number,
  sourceHeight: number,
): DetectorProbeProfileEvidenceView {
  const profile = record(value, "probe profile evidence");
  const filterReasons = record(profile.filter_reasons, "probe filter reasons");
  const normalizedReasons: Record<string, number> = {};
  for (const [reason, count] of Object.entries(filterReasons)) {
    normalizedReasons[reason] = nonNegativeInteger(
      count,
      `filter reason ${reason}`,
    );
  }
  const rawCandidates = Array.isArray(profile.raw_candidates)
    ? profile.raw_candidates.map((candidate) =>
        boxView(candidate, frameIndex, sourceWidth, sourceHeight),
      )
    : (() => {
        throw new Error("raw candidates are missing");
      })();
  const topK = finiteNumber(profile.top_k, "top-k");
  if (topK !== 5) throw new Error("probe evidence top-k is not fixed at five");
  if (rawCandidates.length > topK) {
    throw new Error("raw candidates exceed the fixed top-k");
  }
  const candidateCount = nonNegativeInteger(
    profile.candidate_count,
    "candidate count",
  );
  if (candidateCount < rawCandidates.length) {
    throw new Error(
      "raw candidate evidence does not match the candidate count",
    );
  }
  const latencyMs =
    profile.latency_ms === null || profile.latency_ms === undefined
      ? null
      : nonNegativeNumber(profile.latency_ms, "probe latency");
  const status = enumValue(
    profile.status,
    ["completed", "failed", "blocked"] as const,
    "probe profile status",
  );
  const parsedDisplayCandidate =
    profile.display_candidate === null ||
    profile.display_candidate === undefined
      ? null
      : boxView(
          profile.display_candidate,
          frameIndex,
          sourceWidth,
          sourceHeight,
        );
  if (
    parsedDisplayCandidate &&
    !rawCandidates.some(
      (candidate) =>
        candidate.evidenceKey === parsedDisplayCandidate.evidenceKey,
    )
  ) {
    throw new Error("display candidate is not bound to the raw candidates");
  }
  const failureCode =
    profile.failure_code === null || profile.failure_code === undefined
      ? null
      : string(profile.failure_code, "probe profile failure code");
  if (status === "completed" && (latencyMs === null || failureCode !== null)) {
    throw new Error("completed profile evidence has invalid completion state");
  }
  if (status !== "completed" && failureCode === null) {
    throw new Error("failed profile evidence has no failure code");
  }
  return {
    profileId: safeId(profile.profile_id, "probe profile ID"),
    profileSha256: sha256(profile.profile_sha256, "probe profile SHA-256"),
    status,
    overlayImageUrl: artifactUrl(
      profile.raw_overlay_artifact_url,
      jobId,
      "raw overlay artifact URL",
    ),
    overlaySha256: sha256(profile.raw_overlay_sha256, "raw overlay SHA-256"),
    overlaySizeBytes: positiveInteger(
      profile.raw_overlay_size_bytes,
      "raw overlay size",
    ),
    rawBoxes: rawCandidates.map((candidate) => candidate.view),
    displayCandidate: parsedDisplayCandidate?.view ?? null,
    latencyMs,
    candidateCount,
    topK,
    filterReasons: normalizedReasons,
    failureCode,
  };
}

interface ParsedFrozenProfile {
  profileId: string;
  profileSha256: string;
  modelId: string;
  modelVersion: string;
  modelDescriptorSha256: string;
  weightsSha256: string;
  weightsSizeBytes: number;
  identity: string;
}

function frozenProfile(value: unknown, label: string): ParsedFrozenProfile {
  const profile = record(value, label);
  if (
    string(profile.schema_version, `${label} schema version`) !== "1.0" ||
    profile.artifact_type !== "detector_profile"
  ) {
    throw new Error(`${label} contract is invalid`);
  }
  const profileId = safeId(profile.profile_id, `${label} ID`);
  const modelId = safeId(profile.model_id, `${label} model ID`);
  const modelVersion = string(profile.model_version, `${label} model version`);
  const modelDescriptorSha256 = sha256(
    profile.model_descriptor_sha256,
    `${label} descriptor SHA-256`,
  );
  string(profile.version, `${label} version`);
  const mode = enumValue(
    profile.mode,
    ["direct", "sahi"] as const,
    `${label} mode`,
  );
  profileSettings(profile.settings, mode, `${label} settings`);
  const profileSha256 = sha256(profile.profile_sha256, `${label} SHA-256`);
  boolean(profile.recommended, `${label} recommendation`);
  const availability = profileAvailability(
    profile.availability,
    `${label} availability`,
  );
  const selectable = boolean(
    profile.selectable_for_probe,
    `${label} probe selectability`,
  );
  if (
    selectable &&
    (availability.status !== "available" ||
      availability.runtimeLoadSmoke !== true)
  ) {
    throw new Error(`${label} selectable runtime evidence is invalid`);
  }
  const descriptor = modelDescriptor(
    profile.model_descriptor,
    `${label} model descriptor`,
  );
  if (
    descriptor.modelId !== modelId ||
    descriptor.version !== modelVersion ||
    descriptor.descriptorSha256 !== modelDescriptorSha256
  ) {
    throw new Error(`${label} model descriptor lineage does not match`);
  }
  const identity = canonicalIdentity(profile, `${label} identity`);
  return {
    profileId,
    profileSha256,
    modelId,
    modelVersion,
    modelDescriptorSha256,
    weightsSha256: descriptor.weightsSha256,
    weightsSizeBytes: descriptor.weightsSizeBytes,
    identity,
  };
}

interface ParsedProfileBinding {
  profileId: string;
  profileSha256: string;
  modelId: string;
  modelVersion: string;
  modelDescriptorSha256: string;
  weightsSha256: string;
  weightsSizeBytes: number;
  identity: string;
}

function profileBinding(value: unknown, label: string): ParsedProfileBinding {
  const binding = record(value, label);
  const parsed = {
    profileId: safeId(binding.profile_id, `${label} profile ID`),
    profileSha256: sha256(binding.profile_sha256, `${label} profile SHA-256`),
    modelId: safeId(binding.model_id, `${label} model ID`),
    modelVersion: string(binding.model_version, `${label} model version`),
    modelDescriptorSha256: sha256(
      binding.model_descriptor_sha256,
      `${label} descriptor SHA-256`,
    ),
    weightsSha256: sha256(binding.weights_sha256, `${label} weights SHA-256`),
    weightsSizeBytes: positiveInteger(
      binding.weights_size_bytes,
      `${label} weights size`,
    ),
  };
  return { ...parsed, identity: JSON.stringify(Object.values(parsed)) };
}

function profileMatchesBinding(
  profile: ParsedFrozenProfile,
  binding: ParsedProfileBinding,
) {
  return (
    profile.profileId === binding.profileId &&
    profile.profileSha256 === binding.profileSha256 &&
    profile.modelId === binding.modelId &&
    profile.modelVersion === binding.modelVersion &&
    profile.modelDescriptorSha256 === binding.modelDescriptorSha256 &&
    profile.weightsSha256 === binding.weightsSha256 &&
    profile.weightsSizeBytes === binding.weightsSizeBytes
  );
}

const EXECUTION_CODE_BUNDLE_FILES = [
  "football_tracking/__init__.py",
  "football_tracking/ai_contracts.py",
  "football_tracking/ai_improvement_prompt_contract.py",
  "football_tracking/api/__init__.py",
  "football_tracking/api/schemas.py",
  "football_tracking/candidate_dataset.py",
  "football_tracking/config.py",
  "football_tracking/detector.py",
  "football_tracking/detector_candidate_contract.py",
  "football_tracking/detector_development_common.py",
  "football_tracking/detector_model_registry.py",
  "football_tracking/detector_probe.py",
  "football_tracking/detector_probe_runner.py",
  "football_tracking/detector_probe_worker.py",
  "football_tracking/media_integrity.py",
  "football_tracking/tracking_contracts.py",
  "football_tracking/types.py",
] as const;

interface ParsedExecutionBundle {
  identity: string;
  frozenProfilesSha256: string;
  runtimeEnvironmentSha256: string;
  device: "cpu" | "cuda:0";
  precision: "fp32";
}

function nullableInteger(value: unknown, label: string, positive = false) {
  if (value === null) return null;
  return positive
    ? positiveInteger(value, label)
    : nonNegativeInteger(value, label);
}

function executionBundle(
  value: unknown,
  profileIds: readonly string[],
  expectedFrozenProfilesSha256: string,
  label: string,
): ParsedExecutionBundle {
  const bundle = record(value, label);
  exactKeys(
    bundle,
    [
      "schema_version",
      "installed_runtime",
      "runtime_contract",
      "runtime_contract_sha256",
      "runtime_observation_evidence_sha256s",
      "execution_environment",
      "runtime_environment_sha256",
      "code_bundle_files",
      "code_bundle_sha256",
      "code_commit",
      "code_commit_status",
      "code_commit_reason",
      "code_commit_blob_files",
      "code_commit_blob_bundle_sha256",
      "code_commit_binding_kind",
      "frozen_profiles_sha256",
    ],
    label,
  );
  if (bundle.schema_version !== "1.0") {
    throw new Error(`${label} schema version is invalid`);
  }
  const runtimeNames = ["sahi", "torch", "ultralytics"] as const;
  for (const field of ["installed_runtime", "runtime_contract"] as const) {
    const values = record(bundle[field], `${label} ${field}`);
    exactKeys(values, runtimeNames, `${label} ${field}`);
    runtimeNames.forEach((name) =>
      string(values[name], `${label} ${field} ${name}`),
    );
  }
  sha256(bundle.runtime_contract_sha256, `${label} runtime contract SHA-256`);
  const observations = record(
    bundle.runtime_observation_evidence_sha256s,
    `${label} runtime observation digests`,
  );
  exactKeys(observations, profileIds, `${label} runtime observation digests`);
  profileIds.forEach((profileId) =>
    sha256(
      observations[profileId],
      `${label} runtime observation ${profileId}`,
    ),
  );

  const environment = record(
    bundle.execution_environment,
    `${label} execution environment`,
  );
  exactKeys(
    environment,
    [
      "device",
      "precision",
      "cuda_available",
      "cuda_device_count",
      "cuda_visible_devices",
      "cuda_compiled_version",
      "cudnn_version",
      "gpu_name",
      "gpu_compute_capability",
      "gpu_total_memory_bytes",
      "cuda_driver_version",
      "python_implementation",
      "python_version",
      "numpy_version",
      "opencv_version",
      "pydantic_version",
      "pydantic_core_version",
      "opencv_build_information_sha256",
      "opencv_ffmpeg_enabled",
      "decoder_fingerprint_sha256",
    ],
    `${label} execution environment`,
  );
  const device = enumValue(
    environment.device,
    ["cpu", "cuda:0"] as const,
    `${label} device`,
  );
  if (environment.precision !== "fp32") {
    throw new Error(`${label} precision is invalid`);
  }
  const cudaAvailable = boolean(
    environment.cuda_available,
    `${label} CUDA availability`,
  );
  const cudaDeviceCount = nonNegativeInteger(
    environment.cuda_device_count,
    `${label} CUDA device count`,
  );
  if (
    environment.cuda_visible_devices !== null &&
    typeof environment.cuda_visible_devices !== "string"
  ) {
    throw new Error(`${label} CUDA visibility is invalid`);
  }
  for (const field of [
    "cuda_compiled_version",
    "gpu_name",
    "gpu_compute_capability",
  ] as const) {
    if (environment[field] !== null) {
      string(environment[field], `${label} ${field}`);
    }
  }
  nullableInteger(environment.cudnn_version, `${label} cuDNN version`);
  nullableInteger(
    environment.gpu_total_memory_bytes,
    `${label} GPU total memory`,
    true,
  );
  nullableInteger(
    environment.cuda_driver_version,
    `${label} CUDA driver version`,
  );
  if (
    (device === "cuda:0") !== cudaAvailable ||
    (cudaAvailable ? cudaDeviceCount < 1 : cudaDeviceCount !== 0) ||
    (!cudaAvailable &&
      [
        environment.gpu_name,
        environment.gpu_compute_capability,
        environment.gpu_total_memory_bytes,
        environment.cuda_driver_version,
      ].some((item) => item !== null)) ||
    (cudaAvailable &&
      [
        environment.cuda_compiled_version,
        environment.cudnn_version,
        environment.gpu_name,
        environment.gpu_compute_capability,
        environment.gpu_total_memory_bytes,
      ].some((item) => item === null))
  ) {
    throw new Error(`${label} CUDA hardware binding is inconsistent`);
  }
  for (const field of [
    "python_implementation",
    "python_version",
    "numpy_version",
    "opencv_version",
    "pydantic_version",
    "pydantic_core_version",
  ] as const) {
    string(environment[field], `${label} ${field}`);
  }
  sha256(
    environment.opencv_build_information_sha256,
    `${label} OpenCV build SHA-256`,
  );
  if (
    environment.opencv_ffmpeg_enabled !== null &&
    typeof environment.opencv_ffmpeg_enabled !== "boolean"
  ) {
    throw new Error(`${label} OpenCV FFmpeg binding is invalid`);
  }
  sha256(
    environment.decoder_fingerprint_sha256,
    `${label} decoder fingerprint SHA-256`,
  );

  const codeFiles = record(bundle.code_bundle_files, `${label} code files`);
  exactKeys(codeFiles, EXECUTION_CODE_BUNDLE_FILES, `${label} code files`);
  EXECUTION_CODE_BUNDLE_FILES.forEach((path) =>
    sha256(codeFiles[path], `${label} ${path}`),
  );
  sha256(bundle.code_bundle_sha256, `${label} code bundle SHA-256`);
  const codeCommitStatus = enumValue(
    bundle.code_commit_status,
    ["bound", "unbound", "unavailable"] as const,
    `${label} code commit status`,
  );
  if (codeCommitStatus === "bound") {
    const commit = string(bundle.code_commit, `${label} code commit`);
    const commitBlobFiles = record(
      bundle.code_commit_blob_files,
      `${label} commit blob files`,
    );
    exactKeys(
      commitBlobFiles,
      EXECUTION_CODE_BUNDLE_FILES,
      `${label} commit blob files`,
    );
    EXECUTION_CODE_BUNDLE_FILES.forEach((path) =>
      sha256(commitBlobFiles[path], `${label} commit blob ${path}`),
    );
    sha256(
      bundle.code_commit_blob_bundle_sha256,
      `${label} commit blob bundle SHA-256`,
    );
    if (
      !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit) ||
      bundle.code_commit_reason !== null ||
      bundle.code_commit_binding_kind !== "exact_or_crlf_to_lf_commit_blob"
    ) {
      throw new Error(`${label} code commit binding is invalid`);
    }
  } else if (codeCommitStatus === "unbound") {
    if (
      bundle.code_commit !== null ||
      bundle.code_commit_reason !== "code_bundle_differs_from_commit" ||
      bundle.code_commit_blob_files !== null ||
      bundle.code_commit_blob_bundle_sha256 !== null ||
      bundle.code_commit_binding_kind !== null
    ) {
      throw new Error(`${label} unbound code commit binding is invalid`);
    }
  } else if (
    bundle.code_commit !== null ||
    bundle.code_commit_reason !== "repository_commit_unavailable" ||
    bundle.code_commit_blob_files !== null ||
    bundle.code_commit_blob_bundle_sha256 !== null ||
    bundle.code_commit_binding_kind !== null
  ) {
    throw new Error(`${label} unavailable code commit binding is invalid`);
  }
  const frozenProfilesSha256 = sha256(
    bundle.frozen_profiles_sha256,
    `${label} frozen profiles SHA-256`,
  );
  if (frozenProfilesSha256 !== expectedFrozenProfilesSha256) {
    throw new Error(`${label} frozen profile digest does not match the job`);
  }
  return {
    identity: canonicalIdentity(bundle, `${label} identity`),
    frozenProfilesSha256,
    runtimeEnvironmentSha256: sha256(
      bundle.runtime_environment_sha256,
      `${label} runtime environment SHA-256`,
    ),
    device,
    precision: "fp32",
  };
}

export function detectorProbeJobView(value: unknown): DetectorProbeJobView {
  const job = record(value, "detector probe job");
  if (
    job.schema_version !== "1.0" ||
    job.artifact_type !== "detector_probe_job"
  ) {
    throw new Error("detector probe job contract is invalid");
  }
  const jobId = safeId(job.job_id, "probe job ID");
  const requestSha256 = sha256(job.request_sha256, "probe request SHA-256");
  if (sha256(job.idempotency_key, "probe idempotency key") !== requestSha256) {
    throw new Error("probe idempotency key does not match the request");
  }
  const intentSha256 = sha256(job.intent_sha256, "probe intent SHA-256");
  const frozenProfilesSha256 = sha256(
    job.frozen_profiles_sha256,
    "frozen profiles SHA-256",
  );
  const createdAt = string(job.created_at, "probe creation time");
  string(job.updated_at, "probe update time");
  const expectedStatusUrl = `/api/v1/detector-probes/${jobId}`;
  if (
    string(job.status_url, "probe status URL") !== expectedStatusUrl ||
    string(job.cancel_url, "probe cancel URL") !== `${expectedStatusUrl}/cancel`
  ) {
    throw new Error("probe control URLs do not match the job ID");
  }
  const progress = record(job.progress, "detector probe progress");
  const completed = nonNegativeInteger(progress.completed, "completed work");
  const total = nonNegativeInteger(progress.total, "total work");
  string(progress.updated_at, "probe progress update time");
  if (completed > total) throw new Error("completed work exceeds total work");
  const frozenRequest = record(job.frozen_request, "frozen probe request");
  if (!Array.isArray(frozenRequest.profile_ids)) {
    throw new Error("frozen profile IDs are missing");
  }
  const selectedProfileIds = frozenRequest.profile_ids.map((profileId) =>
    safeId(profileId, "frozen profile ID"),
  );
  if (
    selectedProfileIds.length < 2 ||
    selectedProfileIds.length > 6 ||
    new Set(selectedProfileIds).size !== selectedProfileIds.length
  ) {
    throw new Error("frozen profile IDs are invalid");
  }
  const frozenRequestProfilesSha256 = sha256(
    frozenRequest.frozen_profiles_sha256,
    "frozen request profiles SHA-256",
  );
  if (frozenRequestProfilesSha256 !== frozenProfilesSha256) {
    throw new Error("frozen profile aggregate digest does not match the job");
  }
  const parsedExecutionBundle = executionBundle(
    frozenRequest.execution_bundle,
    selectedProfileIds,
    frozenProfilesSha256,
    "frozen execution bundle",
  );
  const executionBundleSha256 = sha256(
    frozenRequest.execution_bundle_sha256,
    "frozen execution bundle SHA-256",
  );
  const runtimeEnvironmentSha256 = sha256(
    frozenRequest.runtime_environment_sha256,
    "frozen runtime environment SHA-256",
  );
  if (
    runtimeEnvironmentSha256 !== parsedExecutionBundle.runtimeEnvironmentSha256
  ) {
    throw new Error("runtime environment digest does not match its bundle");
  }
  const parentTrialId = safeId(
    frozenRequest.parent_trial_id,
    "frozen parent trial ID",
  );
  const frozenSourceId = safeId(frozenRequest.source_id, "frozen source ID");
  const frozenSourceRelativePath = relativePath(
    frozenRequest.source_relative_path,
    "frozen source relative path",
  );
  const frozenSourceSha256 = sha256(
    frozenRequest.source_sha256,
    "frozen source SHA-256",
  );
  const frozenSourceFileIdentitySha256 = sha256(
    frozenRequest.source_file_identity_sha256,
    "frozen source file identity SHA-256",
  );
  const frozenSourceSizeBytes = positiveInteger(
    frozenRequest.source_size_bytes,
    "frozen source size",
  );
  const frozenSourceWidth = positiveInteger(
    frozenRequest.source_width,
    "frozen source width",
  );
  const frozenSourceHeight = positiveInteger(
    frozenRequest.source_height,
    "frozen source height",
  );
  const frozenSourceFrameCount = positiveInteger(
    frozenRequest.source_frame_count,
    "frozen source frame count",
  );
  const frozenTrackingContractRelativePath = relativePath(
    frozenRequest.tracking_contract_relative_path,
    "frozen tracking-contract relative path",
  );
  const frozenTrackingContractSha256 = sha256(
    frozenRequest.tracking_contract_sha256,
    "frozen tracking-contract SHA-256",
  );
  const frozenBaseConfigSha256 = sha256(
    frozenRequest.base_config_sha256,
    "frozen base-config SHA-256",
  );
  const frozenBaseConfigRelativePath = relativePath(
    frozenRequest.base_config_relative_path,
    "frozen base-config relative path",
  );
  const frozenEffectiveConfigSha256 = sha256(
    frozenRequest.effective_config_sha256,
    "frozen effective-config SHA-256",
  );
  const frozenEffectiveConfigRelativePath = relativePath(
    frozenRequest.effective_config_relative_path,
    "frozen effective-config relative path",
  );
  const frozenTrialIntentSha256 = sha256(
    frozenRequest.trial_intent_sha256,
    "frozen trial-intent SHA-256",
  );
  const frozenTuningBinding = tuningBinding(
    frozenRequest.tuning_patch_binding,
    "frozen tuning-patch binding",
  );
  const frozenTuningPatchSha256 = sha256(
    frozenRequest.tuning_patch_sha256,
    "frozen tuning-patch SHA-256",
  );
  if (!Array.isArray(frozenRequest.frame_indices)) {
    throw new Error("frozen frame indices are missing");
  }
  const frameIndices = frozenRequest.frame_indices.map((frameIndex) =>
    nonNegativeInteger(frameIndex, "frozen frame index"),
  );
  if (
    frameIndices.length < 1 ||
    frameIndices.length > 50 ||
    new Set(frameIndices).size !== frameIndices.length ||
    frameIndices.some(
      (frameIndex, index) => index > 0 && frameIndices[index - 1] >= frameIndex,
    ) ||
    frameIndices.at(-1)! >= frozenSourceFrameCount
  ) {
    throw new Error("frozen frame indices are invalid");
  }
  if (finiteNumber(frozenRequest.top_k, "frozen top-k") !== 5) {
    throw new Error("frozen top-k is not fixed at five");
  }
  const requestedDecodeMode = enumValue(
    frozenRequest.requested_decode_mode,
    ["sequential", "preroll", "direct"] as const,
    "frozen requested decode mode",
  );
  if (!Array.isArray(job.frozen_profiles)) {
    throw new Error("frozen profiles are missing");
  }
  const parsedFrozenProfiles = job.frozen_profiles.map((profile, index) =>
    frozenProfile(profile, `frozen profile ${index + 1}`),
  );
  const frozenProfileDigests = new Map(
    parsedFrozenProfiles.map((profile) => [
      profile.profileId,
      profile.profileSha256,
    ]),
  );
  if (
    frozenProfileDigests.size !== parsedFrozenProfiles.length ||
    parsedFrozenProfiles.length !== selectedProfileIds.length ||
    parsedFrozenProfiles.some(
      (profile, index) => profile.profileId !== selectedProfileIds[index],
    )
  ) {
    throw new Error("frozen profiles do not match the frozen request");
  }
  const frozenRequestProfileDigests = record(
    frozenRequest.profile_sha256s,
    "frozen request profile SHA-256s",
  );
  if (
    Object.keys(frozenRequestProfileDigests).length !==
      frozenProfileDigests.size ||
    selectedProfileIds.some(
      (profileId) =>
        sha256(
          frozenRequestProfileDigests[profileId],
          "frozen request profile SHA-256",
        ) !== frozenProfileDigests.get(profileId),
    )
  ) {
    throw new Error("frozen request profile digests do not match");
  }
  if (!Array.isArray(frozenRequest.profile_bindings)) {
    throw new Error("frozen request profile bindings are missing");
  }
  const parsedBindings = frozenRequest.profile_bindings.map((binding, index) =>
    profileBinding(binding, `frozen profile binding ${index + 1}`),
  );
  if (
    parsedBindings.length !== parsedFrozenProfiles.length ||
    parsedBindings.some(
      (binding, index) =>
        binding.profileId !== selectedProfileIds[index] ||
        !profileMatchesBinding(parsedFrozenProfiles[index], binding),
    )
  ) {
    throw new Error("frozen profile bindings do not match frozen profiles");
  }
  if (total !== frameIndices.length * selectedProfileIds.length) {
    throw new Error("probe progress total does not match the frozen work");
  }
  const status = enumValue(
    job.status,
    [
      "queued",
      "running",
      "committing",
      "ready",
      "failed",
      "cancelled",
      "blocked",
    ] as const,
    "probe job status",
  );
  if (
    boolean(job.can_cancel, "probe cancellation capability") !==
    ["queued", "running"].includes(status)
  ) {
    throw new Error("probe cancellation capability is inconsistent");
  }
  const retryFromJobId =
    job.retry_from_job_id === null || job.retry_from_job_id === undefined
      ? null
      : safeId(job.retry_from_job_id, "retry job ID");
  if (retryFromJobId === jobId) {
    throw new Error("probe retry cannot reference itself");
  }
  const frozenRetryFromJobId =
    frozenRequest.retry_from_job_id === null ||
    frozenRequest.retry_from_job_id === undefined
      ? null
      : safeId(frozenRequest.retry_from_job_id, "frozen retry job ID");
  if (frozenRetryFromJobId !== retryFromJobId) {
    throw new Error("probe retry lineage does not match the frozen request");
  }
  const resultManifestSha256 =
    job.result_manifest_sha256 === null ||
    job.result_manifest_sha256 === undefined
      ? null
      : sha256(job.result_manifest_sha256, "result manifest SHA-256");
  const report =
    job.report === null || job.report === undefined
      ? null
      : record(job.report, "detector probe report");
  if (
    report &&
    (report.schema_version !== "1.0" ||
      report.artifact_type !== "detector_probe_report")
  ) {
    throw new Error("detector probe report contract is invalid");
  }
  if (report) {
    if (
      safeId(report.job_id, "report job ID") !== jobId ||
      sha256(report.request_sha256, "report request SHA-256") !==
        requestSha256 ||
      finiteNumber(report.top_k, "report top-k") !== 5
    ) {
      throw new Error("detector probe report identity is invalid");
    }
    const source = record(report.source, "detector probe report source");
    if (
      safeId(source.source_id, "report source ID") !== frozenSourceId ||
      relativePath(source.relative_path, "report source relative path") !==
        frozenSourceRelativePath ||
      sha256(source.sha256, "report source SHA-256") !== frozenSourceSha256 ||
      sha256(
        source.file_identity_sha256,
        "report source file identity SHA-256",
      ) !== frozenSourceFileIdentitySha256 ||
      positiveInteger(source.size_bytes, "report source size") !==
        frozenSourceSizeBytes ||
      positiveInteger(source.width, "report source width") !==
        frozenSourceWidth ||
      positiveInteger(source.height, "report source height") !==
        frozenSourceHeight ||
      positiveInteger(source.frame_count, "report source frame count") !==
        frozenSourceFrameCount ||
      relativePath(
        source.tracking_contract_relative_path,
        "report tracking-contract relative path",
      ) !== frozenTrackingContractRelativePath ||
      sha256(
        source.tracking_contract_sha256,
        "report tracking-contract SHA-256",
      ) !== frozenTrackingContractSha256
    ) {
      throw new Error("detector probe report source lineage is invalid");
    }
    const lineage = record(report.lineage, "detector probe report lineage");
    const reportExecutionBundle = executionBundle(
      lineage.execution_bundle,
      selectedProfileIds,
      frozenProfilesSha256,
      "report execution bundle",
    );
    const reportTuningBinding = tuningBinding(
      lineage.tuning_patch_binding,
      "report tuning-patch binding",
    );
    const reportRetryFromJobId =
      lineage.retry_from_job_id === null ||
      lineage.retry_from_job_id === undefined
        ? null
        : safeId(lineage.retry_from_job_id, "report retry job ID");
    if (
      safeId(lineage.parent_trial_id, "report parent trial ID") !==
        parentTrialId ||
      relativePath(
        lineage.base_config_relative_path,
        "report base-config relative path",
      ) !== frozenBaseConfigRelativePath ||
      sha256(lineage.base_config_sha256, "report base-config SHA-256") !==
        frozenBaseConfigSha256 ||
      relativePath(
        lineage.effective_config_relative_path,
        "report effective-config relative path",
      ) !== frozenEffectiveConfigRelativePath ||
      sha256(
        lineage.effective_config_sha256,
        "report effective-config SHA-256",
      ) !== frozenEffectiveConfigSha256 ||
      sha256(lineage.trial_intent_sha256, "report trial-intent SHA-256") !==
        frozenTrialIntentSha256 ||
      sha256(lineage.tuning_patch_sha256, "report tuning-patch SHA-256") !==
        frozenTuningPatchSha256 ||
      sha256(
        lineage.frozen_profiles_sha256,
        "report frozen profiles SHA-256",
      ) !== frozenProfilesSha256 ||
      sha256(
        lineage.execution_bundle_sha256,
        "report execution bundle SHA-256",
      ) !== executionBundleSha256 ||
      sha256(
        lineage.runtime_environment_sha256,
        "report runtime environment SHA-256",
      ) !== runtimeEnvironmentSha256 ||
      reportExecutionBundle.identity !== parsedExecutionBundle.identity ||
      sha256(lineage.intent_sha256, "report intent SHA-256") !== intentSha256 ||
      !sameTuningBinding(reportTuningBinding, frozenTuningBinding) ||
      reportRetryFromJobId !== retryFromJobId
    ) {
      throw new Error("detector probe report lineage is invalid");
    }
    const reportProfileDigests = record(
      lineage.profile_sha256s,
      "report profile SHA-256s",
    );
    if (
      Object.keys(reportProfileDigests).length !== frozenProfileDigests.size ||
      selectedProfileIds.some(
        (profileId) =>
          sha256(reportProfileDigests[profileId], "report profile SHA-256") !==
          frozenProfileDigests.get(profileId),
      )
    ) {
      throw new Error("detector probe report profile lineage is invalid");
    }
    if (!Array.isArray(report.frozen_profiles)) {
      throw new Error("report frozen profiles are missing");
    }
    const reportFrozenProfiles = report.frozen_profiles.map((profile, index) =>
      frozenProfile(profile, `report frozen profile ${index + 1}`),
    );
    if (
      reportFrozenProfiles.length !== parsedFrozenProfiles.length ||
      reportFrozenProfiles.some(
        (profile, index) =>
          profile.identity !== parsedFrozenProfiles[index].identity,
      )
    ) {
      throw new Error("report frozen profiles do not match the job");
    }
  }
  if (
    (status === "ready") !== Boolean(report) ||
    (status === "ready") !== Boolean(resultManifestSha256)
  ) {
    throw new Error("probe report does not match terminal job status");
  }
  if (status === "ready" && completed !== total) {
    throw new Error("ready probe did not complete its frozen work");
  }
  let reportEffectiveDecodeMode:
    | "sequential"
    | "preroll_verified"
    | "direct_verified"
    | "sequential_fallback"
    | null = null;
  const reportArtifacts = new Map<
    string,
    {
      sha256: string;
      sizeBytes: number;
      width: number;
      height: number;
    }
  >();
  if (report) {
    string(report.created_at, "probe report creation time");
    sha256(report.report_sha256, "probe report SHA-256");
    const decode = record(report.decode, "probe report decode evidence");
    if (
      positiveInteger(decode.width, "decode width") !== frozenSourceWidth ||
      positiveInteger(decode.height, "decode height") !== frozenSourceHeight ||
      positiveInteger(decode.frame_count, "decode frame count") !==
        frozenSourceFrameCount ||
      enumValue(
        decode.requested_decode_mode,
        ["sequential", "preroll", "direct"] as const,
        "decode requested mode",
      ) !== requestedDecodeMode ||
      finiteNumber(decode.fps, "decode fps") <= 0 ||
      decode.position_verification !==
        "opencv_next_frame_index_with_0.25_tolerance"
    ) {
      throw new Error("probe decode evidence does not match the frozen source");
    }
    reportEffectiveDecodeMode = enumValue(
      decode.effective_decode_mode,
      [
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
      ] as const,
      "decode effective mode",
    );
    if (!Array.isArray(decode.verified_frame_indices)) {
      throw new Error("decode verified frame set is missing");
    }
    const verifiedFrames = decode.verified_frame_indices.map((frameIndex) =>
      nonNegativeInteger(frameIndex, "decode verified frame index"),
    );
    if (
      verifiedFrames.length !== frameIndices.length ||
      verifiedFrames.some(
        (frameIndex, index) => frameIndex !== frameIndices[index],
      )
    ) {
      throw new Error("decode verified frames do not match the frozen request");
    }
    const execution = record(report.execution, "probe report execution");
    const reportDevice = enumValue(
      execution.device,
      ["cpu", "cuda:0"] as const,
      "probe execution device",
    );
    if (
      reportDevice !== parsedExecutionBundle.device ||
      execution.precision !== parsedExecutionBundle.precision
    ) {
      throw new Error(
        "probe execution evidence does not match its frozen bundle",
      );
    }
    if (!Array.isArray(report.artifacts)) {
      throw new Error("probe report artifact manifest is missing");
    }
    if (report.artifacts.length < 3 || report.artifacts.length > 350) {
      throw new Error("probe report artifact manifest is out of bounds");
    }
    for (const rawArtifact of report.artifacts) {
      const artifact = record(rawArtifact, "probe report artifact");
      const artifactId = safeId(artifact.artifact_id, "artifact ID");
      if (reportArtifacts.has(artifactId)) {
        throw new Error("probe report contains a duplicate artifact ID");
      }
      relativePath(artifact.relative_path, "artifact relative path");
      if (artifact.media_type !== "image/jpeg") {
        throw new Error("probe artifact media type is invalid");
      }
      reportArtifacts.set(artifactId, {
        sha256: sha256(artifact.sha256, "artifact SHA-256"),
        sizeBytes: positiveInteger(artifact.size_bytes, "artifact size"),
        width: positiveInteger(artifact.width, "artifact width"),
        height: positiveInteger(artifact.height, "artifact height"),
      });
    }
  }
  const observedFrameIndices = new Set<number>();
  const referencedArtifactIds = new Set<string>();
  const frames = report
    ? (Array.isArray(report.frames)
        ? report.frames
        : (() => {
            throw new Error("probe report frames are missing");
          })()
      ).map((rawFrame) => {
        const frame = record(rawFrame, "probe frame evidence");
        if (!Array.isArray(frame.profile_results)) {
          throw new Error("probe frame profile results are missing");
        }
        const frameIndex = nonNegativeInteger(
          frame.frame_index,
          "probe frame index",
        );
        if (observedFrameIndices.has(frameIndex)) {
          throw new Error("probe report contains a duplicate frame");
        }
        observedFrameIndices.add(frameIndex);
        const sourceWidth = positiveInteger(
          frame.source_width,
          "source frame width",
        );
        const sourceHeight = positiveInteger(
          frame.source_height,
          "source frame height",
        );
        if (
          sourceWidth !== frozenSourceWidth ||
          sourceHeight !== frozenSourceHeight
        ) {
          throw new Error("probe frame source dimensions do not match");
        }
        if (
          enumValue(
            frame.requested_decode_mode,
            ["sequential", "preroll", "direct"] as const,
            "frame requested decode mode",
          ) !== requestedDecodeMode ||
          enumValue(
            frame.effective_decode_mode,
            [
              "sequential",
              "preroll_verified",
              "direct_verified",
              "sequential_fallback",
            ] as const,
            "frame effective decode mode",
          ) !== reportEffectiveDecodeMode ||
          nonNegativeInteger(
            frame.decoded_frame_position,
            "decoded frame position",
          ) !== frameIndex
        ) {
          throw new Error("probe frame decode evidence is invalid");
        }
        const integrity = record(
          frame.media_integrity,
          "probe frame media integrity",
        );
        if (integrity.path !== null) {
          string(integrity.path, "media-integrity path");
        }
        const integrityStatus = enumValue(
          integrity.status,
          ["ok", "unavailable"] as const,
          "media-integrity status",
        );
        if (
          nonNegativeInteger(integrity.width, "media-integrity width") !==
            sourceWidth ||
          nonNegativeInteger(integrity.height, "media-integrity height") !==
            sourceHeight
        ) {
          throw new Error("media-integrity dimensions do not match the frame");
        }
        nonNegativeNumber(integrity.mean_luma, "media-integrity mean luma");
        nonNegativeNumber(integrity.std_luma, "media-integrity luma spread");
        const textureTileRatio = finiteNumber(
          integrity.texture_tile_ratio,
          "media-integrity texture ratio",
        );
        const dominantColorRatio = finiteNumber(
          integrity.dominant_color_ratio,
          "media-integrity dominant-color ratio",
        );
        if (
          textureTileRatio < 0 ||
          textureTileRatio > 1 ||
          dominantColorRatio < 0 ||
          dominantColorRatio > 1
        ) {
          throw new Error("media-integrity ratios are invalid");
        }
        const gray = boolean(integrity.gray, "media-integrity gray flag");
        const lowInformation = boolean(
          integrity.low_information,
          "media-integrity low-information flag",
        );
        const likelyCorrupt = boolean(
          integrity.likely_corrupt,
          "media-integrity corrupt flag",
        );
        const mediaIntegrityReasons = stringArray(
          integrity.reasons,
          "media-integrity reasons",
        );
        const profiles = frame.profile_results.map((rawProfile) => {
          const profile = profileEvidenceView(
            rawProfile,
            jobId,
            frameIndex,
            sourceWidth,
            sourceHeight,
          );
          const rawProfileRecord = record(rawProfile, "probe profile evidence");
          const artifactId = probeArtifactId(
            rawProfileRecord.raw_overlay_artifact_url,
            jobId,
            "raw overlay artifact URL",
          );
          const artifact = reportArtifacts.get(artifactId);
          if (
            !artifact ||
            artifact.sha256 !== profile.overlaySha256 ||
            artifact.sizeBytes !== profile.overlaySizeBytes ||
            artifact.width !== sourceWidth ||
            artifact.height !== sourceHeight
          ) {
            throw new Error(
              "raw overlay evidence does not match the artifact manifest",
            );
          }
          if (referencedArtifactIds.has(artifactId)) {
            throw new Error("probe artifact is referenced more than once");
          }
          referencedArtifactIds.add(artifactId);
          return profile;
        });
        const observedProfileIds = profiles.map((profile) => profile.profileId);
        if (
          new Set(observedProfileIds).size !== observedProfileIds.length ||
          observedProfileIds.length !== selectedProfileIds.length ||
          !selectedProfileIds.every((profileId) =>
            observedProfileIds.includes(profileId),
          )
        ) {
          throw new Error(
            "probe frame profiles do not match the frozen request",
          );
        }
        if (
          profiles.some(
            (profile) =>
              frozenProfileDigests.get(profile.profileId) !==
              profile.profileSha256,
          )
        ) {
          throw new Error(
            "probe frame profile digests do not match the frozen profiles",
          );
        }
        const sourceArtifactId = probeArtifactId(
          frame.source_artifact_url,
          jobId,
          "source frame artifact URL",
        );
        const sourceSha256 = sha256(
          frame.source_frame_sha256,
          "source frame SHA-256",
        );
        const sourceSizeBytes = positiveInteger(
          frame.source_frame_size_bytes,
          "source frame size",
        );
        const sourceArtifact = reportArtifacts.get(sourceArtifactId);
        if (
          !sourceArtifact ||
          sourceArtifact.sha256 !== sourceSha256 ||
          sourceArtifact.sizeBytes !== sourceSizeBytes ||
          sourceArtifact.width !== sourceWidth ||
          sourceArtifact.height !== sourceHeight ||
          referencedArtifactIds.has(sourceArtifactId)
        ) {
          throw new Error(
            "source frame evidence does not match the artifact manifest",
          );
        }
        referencedArtifactIds.add(sourceArtifactId);
        return {
          frameIndex,
          sourceImageUrl: artifactUrl(
            frame.source_artifact_url,
            jobId,
            "source frame artifact URL",
          ),
          sourceSha256,
          sourceSizeBytes,
          sourceWidth,
          sourceHeight,
          mediaIntegrityClean:
            integrityStatus === "ok" &&
            !gray &&
            !lowInformation &&
            !likelyCorrupt,
          mediaIntegrityReasons,
          profiles,
        };
      })
    : [];
  if (status === "ready" && frames.length === 0) {
    throw new Error("ready probe report has no frame evidence");
  }
  if (
    report &&
    (frames.length !== frameIndices.length ||
      frames.some((frame, index) => frame.frameIndex !== frameIndices[index]))
  ) {
    throw new Error("probe report frames do not match the frozen request");
  }
  if (
    report &&
    (referencedArtifactIds.size !== reportArtifacts.size ||
      [...reportArtifacts.keys()].some(
        (artifactId) => !referencedArtifactIds.has(artifactId),
      ))
  ) {
    throw new Error(
      "probe artifact manifest does not exactly match referenced evidence",
    );
  }
  const profileEvidence = frames.flatMap((frame) => frame.profiles);
  const immutableIdentity = JSON.stringify([
    jobId,
    requestSha256,
    intentSha256,
    frozenProfilesSha256,
    createdAt,
    parentTrialId,
    frozenSourceId,
    frozenSourceRelativePath,
    frozenSourceSha256,
    frozenSourceFileIdentitySha256,
    frozenSourceSizeBytes,
    frozenSourceWidth,
    frozenSourceHeight,
    frozenSourceFrameCount,
    frozenTrackingContractRelativePath,
    frozenTrackingContractSha256,
    frozenBaseConfigRelativePath,
    frozenBaseConfigSha256,
    frozenEffectiveConfigRelativePath,
    frozenEffectiveConfigSha256,
    frozenTrialIntentSha256,
    frozenTuningBinding,
    frozenTuningPatchSha256,
    executionBundleSha256,
    runtimeEnvironmentSha256,
    parsedExecutionBundle.identity,
    selectedProfileIds,
    [...frozenProfileDigests.entries()],
    parsedBindings.map((binding) => binding.identity),
    parsedFrozenProfiles.map((profile) => profile.identity),
    frameIndices,
    requestedDecodeMode,
    retryFromJobId,
  ]);
  return {
    jobId,
    parentTrialId,
    requestSha256,
    immutableIdentity,
    resultManifestSha256,
    status,
    stage: string(job.stage, "probe job stage"),
    progressPercent: total > 0 ? (completed / total) * 100 : 0,
    selectedProfileIds,
    frameIndices,
    retryFromJobId,
    failureCode:
      typeof job.error_code === "string"
        ? job.error_code
        : typeof job.blocker_code === "string"
          ? job.blocker_code
          : null,
    recoveryAction:
      typeof job.recovery_action === "string" ? job.recovery_action : null,
    noProfilesProducedCandidates:
      profileEvidence.length > 0 &&
      profileEvidence.every(
        (profile) =>
          profile.status === "completed" &&
          profile.candidateCount === 0 &&
          profile.displayCandidate === null,
      ),
    frames,
  };
}
