import type {
  ArtifactSummary,
  BroadcastCalibrationConfirmation,
  BroadcastReviewAction,
  BroadcastReviewActionsRequest,
  BroadcastReviewCandidate,
  BroadcastReviewWindowsResponse,
  CreateRunRequest,
  RunRecord,
} from "@workspace/api-client-react";

export type BroadcastWorkflowStateName =
  | "setup"
  | "tracking"
  | "needs_review"
  | "recomputing"
  | "trajectory_ready"
  | "rendering"
  | "ready"
  | "failed"
  | "cancelled";

export interface BroadcastWorkflowState {
  state: BroadcastWorkflowStateName;
  messages: string[];
  pollRunIds: string[];
}

export interface BroadcastWorkflowRecovery extends BroadcastWorkflowState {
  parentRun: RunRecord | null;
  operationRun: RunRecord | null;
}

export type BroadcastValidationResult<T> =
  | { ok: true; value: T; messages: [] }
  | { ok: false; value: null; messages: string[] };

export interface BroadcastConfirmedFrame {
  frame_index: number;
  frame_width: number;
  frame_height: number;
  sample_index: number;
  frame_time_seconds: number;
}

export interface BroadcastSetupInput {
  inputVideo: string;
  configName: string;
  confirmedFrames: readonly BroadcastConfirmedFrame[];
  fieldPolygon: readonly [number, number][];
  exclusionPolygons: readonly (readonly [number, number][])[];
  maxManualReviewWindows: number;
  configPatch?: CreateRunRequest["config_patch"];
}

export interface BroadcastReviewDecision {
  candidate_id: string;
  action: string;
  noise_subtype?: BroadcastReviewAction["noise_subtype"];
}

export type BroadcastRecomputeRecoveryMode = "none" | "auto" | "retry";

export interface BroadcastArtifactQueryIdentity {
  scope: string;
  deliveryReady: boolean;
}

export function broadcastArtifactQueryIdentity(
  state: BroadcastWorkflowStateName,
  statusGeneration: string | null | undefined,
): BroadcastArtifactQueryIdentity {
  if (state !== "ready") return { scope: "mutable", deliveryReady: false };
  const generation = statusGeneration?.trim() ?? "";
  if (!/^[0-9a-f]{64}$/.test(generation)) {
    return { scope: "ready:missing", deliveryReady: false };
  }
  return { scope: `ready:${generation}`, deliveryReady: true };
}

const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);
const ACTIVE_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "committing",
  "reconciling",
]);
const REVIEW_ACTIONS = new Set<BroadcastReviewAction["action"]>([
  "confirm_ball",
  "reject_noise",
  "mark_unknown",
]);
const NOISE_SUBTYPES = new Set<
  NonNullable<BroadcastReviewAction["noise_subtype"]>
>([
  "player_body_or_shoe",
  "field_line_or_mark",
  "sideline_or_spare_ball",
  "equipment_or_background",
  "lighting_shadow_or_blur",
]);

const ZH_BROADCAST_MESSAGES: Readonly<Record<string, string>> = {
  "Calibration confirmation is required.": "必须确认校准信息。",
  "Source resolution must contain two positive integer dimensions.":
    "源画面分辨率必须包含两个正整数。",
  "Exactly three non-negative, strictly increasing confirmation frames are required.":
    "必须提供三个非负且严格递增的确认帧。",
  "Exactly three frame confirmations are required.": "必须确认恰好三个帧。",
  "Exclusion polygons must be a list.": "排除区多边形必须是列表。",
  "Input video is required.": "必须选择输入视频。",
  "Config name is required.": "必须选择配置。",
  "Maximum manual review windows must be an integer between 1 and 30.":
    "人工复核窗口上限必须是 1 到 30 的整数。",
  "Confirmation frames must be a list.": "确认帧必须是列表。",
  "Field polygon must be a list.": "球场多边形必须是列表。",
  "Config patch must be an object.": "配置补丁必须是对象。",
  "Review item count does not match the returned review windows.":
    "复核条目数量与服务端返回的复核窗口不一致。",
  "Every review window must have a review_item_id.":
    "每个复核窗口都必须包含 review_item_id。",
  "Reviewer id must contain between 1 and 200 characters.":
    "复核人 ID 必须为 1 到 200 个字符。",
  "Review action timestamp must be a valid ISO-8601 value.":
    "复核操作时间必须是有效的 ISO-8601 时间。",
  "Every review decision must name a candidate.":
    "每个复核决定都必须指定候选。",
  "correct_trajectory is not supported by the broadcast trajectory solver.":
    "导播轨迹求解器不支持 correct_trajectory。",
  "Review candidate evidence is required.": "必须提供复核候选证据。",
  "Operation metadata reconciliation reported a conflict.":
    "操作元数据对账报告冲突。",
  "The server did not return the evidence-bound review decision hash.":
    "服务端未返回与证据绑定的复核决定哈希。",
  missing_qualified_selective_review_queue: "缺少合格的选择性复核队列。",
  invalid_or_stale_selective_review_evidence: "选择性复核证据无效或已过期。",
  invalid_selective_review_queue_items: "选择性复核队列条目无效。",
  invalid_selective_review_queue_window_count: "选择性复核窗口数量无效。",
};

export function localizeBroadcastWorkflowMessage(
  message: string,
  language: "en" | "zh",
): string {
  const text = message.trim();
  if (language !== "zh" || !text) return text;
  const exact = ZH_BROADCAST_MESSAGES[text];
  if (exact) return exact;

  const patterns: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
    [
      /^Run (.+) is not a broadcast_hybrid parent\.$/,
      (match) => `任务 ${match[1]} 不是 broadcast_hybrid 父任务。`,
    ],
    [
      /^Run (.+) has unsupported status (.+)\.$/,
      (match) => `任务 ${match[1]} 的状态 ${match[2]} 不受支持。`,
    ],
    [
      /^Operation (.+) (failed|was cancelled)\.$/,
      (match) =>
        `操作 ${match[1]}${match[2] === "failed" ? "失败" : "已取消"}。`,
    ],
    [
      /^Operation run (.+) has no supported broadcast operation\.$/,
      (match) => `操作任务 ${match[1]} 没有受支持的导播操作。`,
    ],
    [
      /^Completed broadcast parent (.+) has unsupported broadcast status (.+)\.$/,
      (match) => `已完成的导播父任务 ${match[1]} 状态 ${match[2]} 不受支持。`,
    ],
    [
      /^Run (.+) is no longer present in the server run list\.$/,
      (match) => `服务端任务列表中已不存在任务 ${match[1]}。`,
    ],
    [
      /^Run (.+) has no recoverable broadcast_hybrid parent\.$/,
      (match) => `任务 ${match[1]} 没有可恢复的 broadcast_hybrid 父任务。`,
    ],
    [
      /^Multiple active broadcast operations were returned; using latest run (.+)\.$/,
      (match) => `服务端返回多个活动导播操作；采用最新任务 ${match[1]}。`,
    ],
    [
      /^Last operation (.+) is absent from the server run list\.$/,
      (match) => `服务端任务列表暂未包含最近操作 ${match[1]}。`,
    ],
    [
      /^Confirmation (\d+) does not match its confirmed frame index\.$/,
      (match) => `第 ${match[1]} 个确认项与确认帧编号不一致。`,
    ],
    [
      /^Confirmation frame (.+) has a different source resolution\.$/,
      (match) => `确认帧 ${match[1]} 的源画面分辨率不同。`,
    ],
    [
      /^Confirmation (\d+) sample index must be a non-negative integer\.$/,
      (match) => `第 ${match[1]} 个确认项的采样编号必须是非负整数。`,
    ],
    [
      /^Confirmation (\d+) time must be a non-negative finite number\.$/,
      (match) => `第 ${match[1]} 个确认项的时间必须是非负有限数。`,
    ],
    [
      /^(Field polygon|Exclusion polygon (\d+)) must contain at least three points\.$/,
      (match) => `${polygonLabel(match)}必须至少包含三个点。`,
    ],
    [
      /^(Field polygon|Exclusion polygon (\d+)) points must contain finite x\/y coordinates\.$/,
      (match) => `${polygonLabel(match)}的 x/y 坐标必须是有限数。`,
    ],
    [
      /^(Field polygon|Exclusion polygon (\d+)) points must lie inside the source resolution\.$/,
      (match) => `${polygonLabel(match)}的点必须位于源画面分辨率内。`,
    ],
    [
      /^(Field polygon|Exclusion polygon (\d+)) must have non-zero area\.$/,
      (match) => `${polygonLabel(match)}的面积必须大于零。`,
    ],
    [
      /^Review windows are not ready(?:: (.+)|\.)$/,
      (match) =>
        match[1]
          ? `复核窗口尚未就绪：${localizeBroadcastWorkflowMessage(match[1], "zh")}`
          : "复核窗口尚未就绪。",
    ],
    [
      /^Review window (.+) contains a candidate without an id\.$/,
      (match) => `复核窗口 ${match[1]} 包含没有 ID 的候选。`,
    ],
    [
      /^Candidate (.+) appears in multiple review windows\.$/,
      (match) => `候选 ${match[1]} 出现在多个复核窗口中。`,
    ],
    [
      /^Candidate (.+) has more than one review decision\.$/,
      (match) => `候选 ${match[1]} 有多个复核决定。`,
    ],
    [
      /^Candidate (.+) has unsupported action (.+)\.$/,
      (match) => `候选 ${match[1]} 的操作 ${match[2]} 不受支持。`,
    ],
    [
      /^Candidate (.+) reject_noise requires a concrete noise subtype\.$/,
      (match) => `候选 ${match[1]} 选择 reject_noise 时必须指定噪声子类型。`,
    ],
    [
      /^Candidate (.+) may set noise_subtype only for reject_noise\.$/,
      (match) => `候选 ${match[1]} 仅可在 reject_noise 时设置 noise_subtype。`,
    ],
    [
      /^Candidate (.+) is missing a review decision\.$/,
      (match) => `候选 ${match[1]} 缺少复核决定。`,
    ],
    [
      /^Decision references candidate (.+), which is not in the review queue\.$/,
      (match) => `复核决定引用了不在复核队列中的候选 ${match[1]}。`,
    ],
    [
      /^Candidate (.+) has an unsafe montage path\.$/,
      (match) => `候选 ${match[1]} 的拼图路径不安全。`,
    ],
    [
      /^Candidate (.+) has an invalid montage size\.$/,
      (match) => `候选 ${match[1]} 的拼图大小无效。`,
    ],
    [
      /^Montage path (.+) is ambiguous in the run artifact allowlist\.$/,
      (match) => `拼图路径 ${match[1]} 在任务产物白名单中不唯一。`,
    ],
    [
      /^Montage path (.+) does not match its evidence-bound size\.$/,
      (match) => `拼图路径 ${match[1]} 的大小与证据绑定不一致。`,
    ],
    [
      /^Montage path (.+) has multiple suffix-and-size matches in the run artifact allowlist\.$/,
      (match) =>
        `拼图路径 ${match[1]} 在任务产物白名单中有多个后缀和大小匹配项。`,
    ],
    [
      /^Montage path (.+) is missing from the run artifact allowlist\.$/,
      (match) => `任务产物白名单中缺少拼图路径 ${match[1]}。`,
    ],
    [
      /^Operation metadata reconciliation reported (.+)\.$/,
      (match) => `操作元数据对账报告：${match[1]}。`,
    ],
  ];
  for (const [pattern, translate] of patterns) {
    const match = text.match(pattern);
    if (match) return translate(match);
  }
  return text;
}

function polygonLabel(match: RegExpMatchArray): string {
  return match[1] === "Field polygon"
    ? "球场多边形"
    : `第 ${match[2]} 个排除区多边形`;
}

export function deriveBroadcastWorkflowState(
  parentRun: RunRecord | null | undefined,
  operationRun: RunRecord | null = null,
): BroadcastWorkflowState {
  if (!parentRun) return { state: "setup", messages: [], pollRunIds: [] };

  const messages = runMessages(parentRun);
  if (parentRun.source !== "broadcast_hybrid") {
    return invalidState(
      messages,
      `Run ${parentRun.run_id} is not a broadcast_hybrid parent.`,
    );
  }
  if (parentRun.status === "failed") return terminalState("failed", messages);
  if (parentRun.status === "cancelled")
    return terminalState("cancelled", messages);
  if (ACTIVE_RUN_STATUSES.has(parentRun.status)) {
    return { state: "tracking", messages, pollRunIds: [parentRun.run_id] };
  }
  if (parentRun.status !== "completed") {
    return invalidState(
      messages,
      `Run ${parentRun.run_id} has unsupported status ${parentRun.status}.`,
    );
  }

  const broadcastStatus = cleanText(parentRun.broadcast?.status);
  if (broadcastStatus === "ready") {
    if (operationRun) {
      messages.push(...runMessages(operationRun));
      const lineageError = operationLineageError(parentRun, operationRun);
      if (lineageError) return invalidState(messages, lineageError);
      if (operationIsActive(operationRun)) {
        return {
          state: "ready",
          messages,
          pollRunIds: [parentRun.run_id, operationRun.run_id],
        };
      }
    } else {
      const lastOperation = parentRun.broadcast?.last_operation;
      if (
        lastOperation &&
        ACTIVE_OPERATION_STATUSES.has(lastOperation.status)
      ) {
        return {
          state: "ready",
          messages,
          pollRunIds: [parentRun.run_id, lastOperation.operation_run_id],
        };
      }
    }
    return { state: "ready", messages, pollRunIds: [] };
  }

  if (operationRun) {
    messages.push(...runMessages(operationRun));
    const lineageError = operationLineageError(parentRun, operationRun);
    if (lineageError) return invalidState(messages, lineageError);
    if (
      operationRun.status === "failed" ||
      operationRun.status === "cancelled"
    ) {
      if (!operationRun.error) {
        messages.push(
          `Operation ${operationRun.run_id} ${operationRun.status === "failed" ? "failed" : "was cancelled"}.`,
        );
      }
      const fallback = operationFallbackState(
        operationRun.broadcast?.operation,
      );
      if (fallback) {
        return {
          state: fallback,
          messages,
          pollRunIds: parentStillReferencesActiveOperation(
            parentRun,
            operationRun,
          )
            ? [parentRun.run_id, operationRun.run_id]
            : [],
        };
      }
      return invalidState(
        messages,
        `Operation run ${operationRun.run_id} has no supported broadcast operation.`,
      );
    }
    if (operationIsActive(operationRun)) {
      const state = operationState(operationRun.broadcast?.operation);
      if (!state) {
        return invalidState(
          messages,
          `Operation run ${operationRun.run_id} has no supported broadcast operation.`,
        );
      }
      return {
        state,
        messages,
        pollRunIds: [parentRun.run_id, operationRun.run_id],
      };
    }
    if (
      operationRun.status === "completed" &&
      parentStillReferencesActiveOperation(parentRun, operationRun)
    ) {
      const state = operationState(operationRun.broadcast?.operation);
      if (state) {
        return {
          state,
          messages,
          pollRunIds: [parentRun.run_id, operationRun.run_id],
        };
      }
    }
  } else {
    const lastOperation = parentRun.broadcast?.last_operation;
    if (
      lastOperation?.status === "failed" ||
      lastOperation?.status === "cancelled"
    ) {
      const fallback = operationFallbackState(lastOperation.operation);
      const fallbackMessages = appendMessage(messages, lastOperation.error);
      if (!lastOperation.error) {
        fallbackMessages.push(
          `Operation ${lastOperation.operation_run_id} ${lastOperation.status === "failed" ? "failed" : "was cancelled"}.`,
        );
      }
      if (fallback)
        return { state: fallback, messages: fallbackMessages, pollRunIds: [] };
    }
    if (lastOperation && ACTIVE_OPERATION_STATUSES.has(lastOperation.status)) {
      const state = operationState(lastOperation.operation);
      if (state) {
        return {
          state,
          messages,
          pollRunIds: [parentRun.run_id, lastOperation.operation_run_id],
        };
      }
    }
  }

  if (broadcastStatus === "needs_review") {
    return { state: "needs_review", messages, pollRunIds: [] };
  }
  if (broadcastStatus === "trajectory_ready") {
    return { state: "trajectory_ready", messages, pollRunIds: [] };
  }
  if (broadcastStatus === "failed") return terminalState("failed", messages);
  if (broadcastStatus === "cancelled")
    return terminalState("cancelled", messages);
  return invalidState(
    messages,
    `Completed broadcast parent ${parentRun.run_id} has unsupported broadcast status ${broadcastStatus ?? "missing"}.`,
  );
}

export function recoverBroadcastWorkflowRun(
  requested: string | RunRecord | null | undefined,
  listedRuns: readonly RunRecord[],
): BroadcastWorkflowRecovery {
  if (!requested)
    return withRuns(deriveBroadcastWorkflowState(null), null, null);

  const requestedRun =
    typeof requested === "string"
      ? (listedRuns.find((run) => run.run_id === requested) ?? null)
      : requested;
  if (!requestedRun) {
    return withRuns(
      invalidState(
        [],
        `Run ${String(requested)} is no longer present in the server run list.`,
      ),
      null,
      null,
    );
  }

  const runIndex = uniqueRunIndex([...listedRuns, requestedRun]);
  const indexedRequestedRun = runIndex.get(requestedRun.run_id) ?? requestedRun;
  const parentRun =
    indexedRequestedRun.source === "broadcast_hybrid"
      ? indexedRequestedRun
      : indexedRequestedRun.parent_run_id
        ? (runIndex.get(indexedRequestedRun.parent_run_id) ?? null)
        : null;
  if (!parentRun) {
    return withRuns(
      invalidState(
        [],
        `Run ${requestedRun.run_id} has no recoverable broadcast_hybrid parent.`,
      ),
      null,
      null,
    );
  }
  if (parentRun.source !== "broadcast_hybrid") {
    return withRuns(
      invalidState(
        [],
        `Run ${parentRun.run_id} is not a broadcast_hybrid parent.`,
      ),
      parentRun,
      null,
    );
  }

  const children = [...runIndex.values()]
    .filter((run) => operationLineageError(parentRun, run) === null)
    .sort(compareRunsNewestFirst);
  const activeChildren = children.filter(operationIsActive);
  const referencedId = cleanText(
    parentRun.broadcast?.last_operation?.operation_run_id,
  );
  const referencedChild = referencedId
    ? (children.find((run) => run.run_id === referencedId) ?? null)
    : null;
  const operationRun =
    activeChildren[0] ??
    referencedChild ??
    (referencedId ? null : (children[0] ?? null));
  const derived = deriveBroadcastWorkflowState(parentRun, operationRun);
  if (activeChildren.length > 1) {
    derived.messages.push(
      `Multiple active broadcast operations were returned; using latest run ${activeChildren[0].run_id}.`,
    );
  }
  if (referencedId && !referencedChild && activeChildren.length === 0) {
    derived.messages.push(
      `Last operation ${referencedId} is absent from the server run list.`,
    );
    const lastStatus = parentRun.broadcast?.last_operation?.status;
    if (
      parentRun.broadcast?.status === "ready" ||
      (lastStatus != null && ACTIVE_OPERATION_STATUSES.has(lastStatus))
    ) {
      derived.pollRunIds = [parentRun.run_id, referencedId];
    }
  }
  return withRuns(derived, parentRun, operationRun);
}

export function validateBroadcastCalibration(
  calibration: BroadcastCalibrationConfirmation | null | undefined,
  confirmedFrames?: readonly BroadcastConfirmedFrame[],
): BroadcastValidationResult<BroadcastCalibrationConfirmation> {
  const messages: string[] = [];
  if (!calibration) return failure("Calibration confirmation is required.");

  const resolution = calibration.source_resolution;
  if (
    !Array.isArray(resolution) ||
    resolution.length !== 2 ||
    !isPositiveInteger(resolution[0]) ||
    !isPositiveInteger(resolution[1])
  ) {
    messages.push(
      "Source resolution must contain two positive integer dimensions.",
    );
  }

  const frames = calibration.confirmed_sample_frames;
  if (
    !Array.isArray(frames) ||
    frames.length !== 3 ||
    !frames.every(isNonnegativeInteger) ||
    !(frames[0] < frames[1] && frames[1] < frames[2])
  ) {
    messages.push(
      "Exactly three non-negative, strictly increasing confirmation frames are required.",
    );
  }

  if (confirmedFrames) {
    if (confirmedFrames.length !== 3) {
      messages.push("Exactly three frame confirmations are required.");
    } else {
      for (let index = 0; index < confirmedFrames.length; index += 1) {
        const confirmation = confirmedFrames[index];
        if (confirmation.frame_index !== frames[index]) {
          messages.push(
            `Confirmation ${index + 1} does not match its confirmed frame index.`,
          );
        }
        if (
          !sameResolution(
            [confirmation.frame_width, confirmation.frame_height],
            resolution,
          )
        ) {
          messages.push(
            `Confirmation frame ${confirmation.frame_index} has a different source resolution.`,
          );
        }
      }
    }
  }

  if (
    isPositiveInteger(resolution?.[0]) &&
    isPositiveInteger(resolution?.[1])
  ) {
    validatePolygon(
      calibration.field_polygon,
      "Field polygon",
      resolution,
      messages,
    );
    const exclusions = calibration.exclusion_polygons ?? [];
    if (!Array.isArray(exclusions)) {
      messages.push("Exclusion polygons must be a list.");
    } else {
      exclusions.forEach((polygon, index) =>
        validatePolygon(
          polygon,
          `Exclusion polygon ${index + 1}`,
          resolution,
          messages,
        ),
      );
    }
  }

  return messages.length === 0
    ? success(calibration)
    : { ok: false, value: null, messages };
}

export function buildBroadcastCreateRequest(
  setup: BroadcastSetupInput,
): BroadcastValidationResult<CreateRunRequest> {
  const messages: string[] = [];
  const inputVideo = cleanText(setup.inputVideo);
  const configName = cleanText(setup.configName);
  if (!inputVideo) messages.push("Input video is required.");
  if (!configName) messages.push("Config name is required.");
  if (
    !isPositiveInteger(setup.maxManualReviewWindows) ||
    setup.maxManualReviewWindows > 30
  ) {
    messages.push(
      "Maximum manual review windows must be an integer between 1 and 30.",
    );
  }
  const confirmedFrames = Array.isArray(setup.confirmedFrames)
    ? setup.confirmedFrames
    : [];
  const fieldPolygon = Array.isArray(setup.fieldPolygon)
    ? setup.fieldPolygon
    : [];
  const exclusionPolygons = Array.isArray(setup.exclusionPolygons)
    ? setup.exclusionPolygons
    : [];
  if (!Array.isArray(setup.confirmedFrames))
    messages.push("Confirmation frames must be a list.");
  if (!Array.isArray(setup.fieldPolygon))
    messages.push("Field polygon must be a list.");
  if (!Array.isArray(setup.exclusionPolygons))
    messages.push("Exclusion polygons must be a list.");
  const firstFrame = confirmedFrames[0];
  const calibrationConfirmation: BroadcastCalibrationConfirmation | null =
    firstFrame
      ? {
          source_resolution: [firstFrame.frame_width, firstFrame.frame_height],
          confirmed_sample_frames: confirmedFrames.map(
            (frame) => frame.frame_index,
          ) as [number, number, number],
          field_polygon: fieldPolygon.map((point) => [point[0], point[1]]),
          exclusion_polygons: exclusionPolygons.map((polygon) =>
            Array.isArray(polygon)
              ? polygon.map((point) => [point[0], point[1]])
              : [],
          ),
        }
      : null;
  for (const [index, frame] of confirmedFrames.entries()) {
    if (!isNonnegativeInteger(frame.sample_index)) {
      messages.push(
        `Confirmation ${index + 1} sample index must be a non-negative integer.`,
      );
    }
    if (
      typeof frame.frame_time_seconds !== "number" ||
      !Number.isFinite(frame.frame_time_seconds) ||
      frame.frame_time_seconds < 0
    ) {
      messages.push(
        `Confirmation ${index + 1} time must be a non-negative finite number.`,
      );
    }
  }
  const calibration = validateBroadcastCalibration(
    calibrationConfirmation,
    confirmedFrames,
  );
  if (!calibration.ok) messages.push(...calibration.messages);
  const baseConfigPatch = setup.configPatch ?? {};
  if (!isRecord(baseConfigPatch))
    messages.push("Config patch must be an object.");
  if (
    messages.length > 0 ||
    !inputVideo ||
    !configName ||
    !calibration.ok ||
    !isRecord(baseConfigPatch)
  ) {
    return { ok: false, value: null, messages };
  }

  const preparedConfigPatch = buildBroadcastFieldConfigPatch(
    setup,
    baseConfigPatch,
  );
  const runtime = isRecord(preparedConfigPatch.runtime)
    ? preparedConfigPatch.runtime
    : {};
  const output = isRecord(preparedConfigPatch.output)
    ? preparedConfigPatch.output
    : {};
  const request: CreateRunRequest = {
    config_name: configName,
    input_video: inputVideo,
    config_patch: {
      ...preparedConfigPatch,
      runtime: { ...runtime, max_frames: null },
      output: { ...output, save_tracking_contract: true },
    },
    enable_follow_cam: false,
    start_frame: 0,
    max_frames: null,
    pipeline_mode: "broadcast_hybrid",
    calibration_confirmation: calibration.value,
    quality_profile: "stable_broadcast",
    max_manual_review_windows: setup.maxManualReviewWindows,
  };
  return success(request);
}

export function broadcastRecomputeRecoveryMode(
  parentRun: RunRecord | null | undefined,
  hasReviewDecisionsArtifact: boolean,
): BroadcastRecomputeRecoveryMode {
  if (
    !parentRun ||
    parentRun.source !== "broadcast_hybrid" ||
    parentRun.status !== "completed" ||
    parentRun.broadcast?.status !== "needs_review" ||
    !hasReviewDecisionsArtifact
  ) {
    return "none";
  }

  const lastOperation = parentRun.broadcast.last_operation;
  if (!lastOperation) return "auto";
  if (
    lastOperation.operation === "recompute" &&
    (lastOperation.status === "failed" || lastOperation.status === "cancelled")
  ) {
    return "retry";
  }
  return "none";
}

export function mergeBroadcastArtifacts(
  runArtifacts: readonly ArtifactSummary[] | null | undefined,
  listedArtifacts: readonly ArtifactSummary[] | null | undefined,
): ArtifactSummary[] {
  const byName = new Map<string, ArtifactSummary>();
  for (const artifact of listedArtifacts ?? []) {
    byName.set(artifact.name, artifact);
  }
  // RunRecord and its workflow status are one server snapshot, so it wins when
  // the independent artifact-list request returns an older value out of order.
  for (const artifact of runArtifacts ?? []) {
    byName.set(artifact.name, artifact);
  }
  return [...byName.values()].sort((left, right) =>
    left.name.localeCompare(right.name),
  );
}

export function broadcastCancellationTarget(
  recovery: BroadcastWorkflowRecovery,
): string | null {
  if (recovery.state === "tracking") {
    return recovery.parentRun?.run_id ?? null;
  }
  const expectedOperation =
    recovery.state === "recomputing"
      ? "recompute"
      : recovery.state === "rendering"
        ? "render"
        : null;
  if (!expectedOperation) return null;
  if (
    recovery.operationRun?.broadcast?.operation === expectedOperation &&
    operationIsActive(recovery.operationRun)
  ) {
    return recovery.operationRun.run_id;
  }
  const lastOperation = recovery.parentRun?.broadcast?.last_operation;
  if (
    lastOperation?.operation === expectedOperation &&
    ACTIVE_OPERATION_STATUSES.has(lastOperation.status)
  ) {
    return lastOperation.operation_run_id;
  }
  return null;
}

export function validateAndBuildBroadcastReviewActions(
  review: BroadcastReviewWindowsResponse,
  decisions: readonly BroadcastReviewDecision[],
  reviewerId: string,
  createdAt?: string | null,
): BroadcastValidationResult<BroadcastReviewActionsRequest> {
  const messages: string[] = [];
  const reviewer = cleanText(reviewerId);
  if (review.status !== "ready") {
    messages.push(
      `Review windows are not ready${review.reason ? `: ${review.reason}` : "."}`,
    );
  }
  const items = review.items ?? [];
  if (
    review.review_item_count != null &&
    review.review_item_count !== items.length
  ) {
    messages.push(
      "Review item count does not match the returned review windows.",
    );
  }

  const candidates = new Map<
    string,
    { reviewItemId: string; candidate: BroadcastReviewCandidate }
  >();
  for (const item of items) {
    const reviewItemId = cleanText(item.review_item_id);
    if (!reviewItemId) {
      messages.push("Every review window must have a review_item_id.");
      continue;
    }
    for (const candidate of item.candidates ?? []) {
      const candidateId = cleanText(candidate.candidate_id);
      if (!candidateId) {
        messages.push(
          `Review window ${reviewItemId} contains a candidate without an id.`,
        );
        continue;
      }
      if (candidates.has(candidateId)) {
        messages.push(
          `Candidate ${candidateId} appears in multiple review windows.`,
        );
        continue;
      }
      candidates.set(candidateId, { reviewItemId, candidate });
    }
  }
  if (candidates.size > 0) {
    if (!reviewer || reviewer.length > 200) {
      messages.push("Reviewer id must contain between 1 and 200 characters.");
    }
    if (createdAt != null && !isIsoTimestamp(createdAt)) {
      messages.push("Review action timestamp must be a valid ISO-8601 value.");
    }
  }

  const decisionsByCandidate = new Map<string, BroadcastReviewDecision>();
  for (const decision of decisions) {
    const candidateId = cleanText(decision.candidate_id);
    if (!candidateId) {
      messages.push("Every review decision must name a candidate.");
      continue;
    }
    if (decisionsByCandidate.has(candidateId)) {
      messages.push(
        `Candidate ${candidateId} has more than one review decision.`,
      );
      continue;
    }
    decisionsByCandidate.set(candidateId, decision);
    if (decision.action === "correct_trajectory") {
      messages.push(
        "correct_trajectory is not supported by the broadcast trajectory solver.",
      );
    } else if (
      !REVIEW_ACTIONS.has(decision.action as BroadcastReviewAction["action"])
    ) {
      messages.push(
        `Candidate ${candidateId} has unsupported action ${decision.action || "missing"}.`,
      );
    }
    if (decision.action === "reject_noise") {
      if (
        !decision.noise_subtype ||
        !NOISE_SUBTYPES.has(decision.noise_subtype)
      ) {
        messages.push(
          `Candidate ${candidateId} reject_noise requires a concrete noise subtype.`,
        );
      }
    } else if (decision.noise_subtype != null) {
      messages.push(
        `Candidate ${candidateId} may set noise_subtype only for reject_noise.`,
      );
    }
  }

  for (const candidateId of candidates.keys()) {
    if (!decisionsByCandidate.has(candidateId)) {
      messages.push(`Candidate ${candidateId} is missing a review decision.`);
    }
  }
  for (const candidateId of decisionsByCandidate.keys()) {
    if (!candidates.has(candidateId)) {
      messages.push(
        `Decision references candidate ${candidateId}, which is not in the review queue.`,
      );
    }
  }
  if (messages.length > 0 || (candidates.size > 0 && !reviewer))
    return { ok: false, value: null, messages };

  const actions: BroadcastReviewAction[] = [...candidates.entries()].map(
    ([candidateId, { reviewItemId }], index) => {
      const decision = decisionsByCandidate.get(candidateId)!;
      const action: BroadcastReviewAction = {
        action_id: `broadcast-review-${String(index + 1).padStart(4, "0")}`,
        review_item_id: reviewItemId,
        candidate_id: candidateId,
        reviewer_id: reviewer ?? "",
        action: decision.action as BroadcastReviewAction["action"],
      };
      if (createdAt != null) action.created_at = createdAt;
      if (decision.action === "reject_noise")
        action.noise_subtype = decision.noise_subtype;
      return action;
    },
  );
  return success({ actions });
}

export function resolveBroadcastMontageArtifact(
  artifacts: readonly ArtifactSummary[] | null | undefined,
  candidate:
    | Pick<BroadcastReviewCandidate, "candidate_id" | "evidence">
    | null
    | undefined,
): BroadcastValidationResult<ArtifactSummary> {
  if (!candidate) return failure("Review candidate evidence is required.");
  const descriptor = candidate.evidence?.artifacts?.review_montage;
  const expectedPath = safeRelativePath(descriptor?.path);
  if (!expectedPath)
    return failure(
      `Candidate ${candidate.candidate_id} has an unsafe montage path.`,
    );
  if (!isNonnegativeInteger(descriptor?.size_bytes)) {
    return failure(
      `Candidate ${candidate.candidate_id} has an invalid montage size.`,
    );
  }

  const available = (artifacts ?? []).filter(
    (artifact) =>
      artifact.exists === true && safeRelativePath(artifact.name) !== null,
  );
  const exactByName = available.filter(
    (artifact) => safeRelativePath(artifact.name) === expectedPath,
  );
  if (exactByName.length > 1) {
    return failure(
      `Montage path ${expectedPath} is ambiguous in the run artifact allowlist.`,
    );
  }
  if (exactByName.length === 1) {
    const exact = exactByName[0];
    if (exact.size_bytes !== descriptor.size_bytes) {
      return failure(
        `Montage path ${expectedPath} does not match its evidence-bound size.`,
      );
    }
    return success(exact);
  }

  const suffix = `/${expectedPath}`;
  const suffixMatches = available.filter((artifact) => {
    const name = safeRelativePath(artifact.name);
    return (
      name?.endsWith(suffix) === true &&
      artifact.size_bytes === descriptor.size_bytes
    );
  });
  if (suffixMatches.length === 1) return success(suffixMatches[0]);
  if (suffixMatches.length > 1) {
    return failure(
      `Montage path ${expectedPath} has multiple suffix-and-size matches in the run artifact allowlist.`,
    );
  }
  return failure(
    `Montage path ${expectedPath} is missing from the run artifact allowlist.`,
  );
}

function operationLineageError(
  parentRun: RunRecord,
  operationRun: RunRecord,
): string | null {
  const operation = operationRun.broadcast?.operation;
  if (operation !== "recompute" && operation !== "render")
    return `Run ${operationRun.run_id} is not a supported broadcast operation.`;
  if (operationRun.parent_run_id !== parentRun.run_id)
    return `Operation run ${operationRun.run_id} does not belong to parent ${parentRun.run_id}.`;
  if (
    operationRun.broadcast?.parent_run_id &&
    operationRun.broadcast.parent_run_id !== parentRun.run_id
  ) {
    return `Operation run ${operationRun.run_id} has conflicting parent lineage.`;
  }
  if (operationRun.source !== `broadcast_hybrid_${operation}`)
    return `Operation run ${operationRun.run_id} has a mismatched source.`;
  return null;
}

function operationState(
  operation: unknown,
): "recomputing" | "rendering" | null {
  if (operation === "recompute") return "recomputing";
  if (operation === "render") return "rendering";
  return null;
}

function operationIsActive(run: RunRecord): boolean {
  return (
    ACTIVE_RUN_STATUSES.has(run.status) ||
    ACTIVE_OPERATION_STATUSES.has(
      cleanText(run.broadcast?.operation_status) ?? "",
    )
  );
}

function parentStillReferencesActiveOperation(
  parentRun: RunRecord,
  operationRun: RunRecord,
): boolean {
  const lastOperation = parentRun.broadcast?.last_operation;
  return Boolean(
    lastOperation?.operation_run_id === operationRun.run_id &&
    lastOperation.operation === operationRun.broadcast?.operation &&
    ACTIVE_OPERATION_STATUSES.has(lastOperation.status),
  );
}

function operationFallbackState(
  operation: unknown,
): "needs_review" | "trajectory_ready" | null {
  if (operation === "recompute") return "needs_review";
  if (operation === "render") return "trajectory_ready";
  return null;
}

function runMessages(run: RunRecord): string[] {
  const messages: string[] = [];
  const error = cleanText(run.error);
  if (error) messages.push(error);
  for (const reason of run.broadcast?.blocking_reasons ?? []) {
    const text = cleanText(reason);
    if (text && !messages.includes(text)) messages.push(text);
  }
  for (const warning of run.broadcast?.metadata_warnings ?? []) {
    const text = cleanText(warning);
    if (text && !messages.includes(text)) messages.push(text);
  }
  const reportStatus = cleanText(run.broadcast?.operation_report_status);
  if (
    reportStatus === "missing_after_ready_commit" ||
    reportStatus === "conflict"
  ) {
    messages.push(
      `Operation metadata reconciliation reported ${reportStatus}.`,
    );
  }
  if (run.broadcast?.operation_status === "metadata_conflict") {
    messages.push("Operation metadata reconciliation reported a conflict.");
  }
  return messages;
}

function terminalState(
  state: "failed" | "cancelled",
  messages: string[],
): BroadcastWorkflowState {
  return { state, messages, pollRunIds: [] };
}

function invalidState(
  messages: string[],
  message: string,
): BroadcastWorkflowState {
  return { state: "failed", messages: [...messages, message], pollRunIds: [] };
}

function appendMessage(messages: string[], value: unknown): string[] {
  const text = cleanText(value);
  return text ? [...messages, text] : messages;
}

function withRuns(
  state: BroadcastWorkflowState,
  parentRun: RunRecord | null,
  operationRun: RunRecord | null,
): BroadcastWorkflowRecovery {
  return { ...state, parentRun, operationRun };
}

function uniqueRunIndex(runs: readonly RunRecord[]): Map<string, RunRecord> {
  const chronologyIndex = new Map<string, RunRecord>();
  for (const run of runs) {
    const existing = chronologyIndex.get(run.run_id);
    if (!existing || compareRunChronology(run, existing) > 0) {
      chronologyIndex.set(run.run_id, run);
    }
  }

  const index = new Map<string, RunRecord>();
  for (const run of runs) {
    const existing = index.get(run.run_id);
    if (
      !existing ||
      compareRunSnapshotProgress(run, existing, chronologyIndex) >= 0
    ) {
      index.set(run.run_id, run);
    }
  }
  return index;
}

function compareRunSnapshotProgress(
  left: RunRecord,
  right: RunRecord,
  chronologyIndex: ReadonlyMap<string, RunRecord>,
): number {
  const leftOperation = left.broadcast?.last_operation;
  const rightOperation = right.broadcast?.last_operation;
  const differentParentOperations = Boolean(
    left.source === "broadcast_hybrid" &&
    right.source === "broadcast_hybrid" &&
    leftOperation?.operation_run_id &&
    rightOperation?.operation_run_id &&
    leftOperation.operation_run_id !== rightOperation.operation_run_id,
  );
  if (differentParentOperations && leftOperation && rightOperation) {
    const leftActive = ACTIVE_OPERATION_STATUSES.has(leftOperation.status);
    const rightActive = ACTIVE_OPERATION_STATUSES.has(rightOperation.status);
    if (leftActive !== rightActive) return leftActive ? 1 : -1;

    const leftOperationRun = chronologyIndex.get(
      leftOperation.operation_run_id,
    );
    const rightOperationRun = chronologyIndex.get(
      rightOperation.operation_run_id,
    );
    if (
      leftOperationRun &&
      rightOperationRun &&
      operationLineageError(left, leftOperationRun) === null &&
      operationLineageError(right, rightOperationRun) === null
    ) {
      const chronologyDifference = compareRunChronology(
        leftOperationRun,
        rightOperationRun,
      );
      if (chronologyDifference !== 0) return chronologyDifference;
    }
  }
  const leftRank = runSnapshotProgress(left, !differentParentOperations);
  const rightRank = runSnapshotProgress(right, !differentParentOperations);
  for (let index = 0; index < leftRank.length; index += 1) {
    const difference = leftRank[index] - rightRank[index];
    if (difference !== 0) return difference;
  }
  return 0;
}

function runSnapshotProgress(
  run: RunRecord,
  includeOperationStatus: boolean,
): number[] {
  const runStatusRank: Record<string, number> = {
    queued: 0,
    running: 1,
    completed: 2,
    failed: 2,
    cancelled: 2,
  };
  const workflowStatusRank: Record<string, number> = {
    tracking: 0,
    needs_review: 1,
    trajectory_ready: 2,
    ready: 3,
    failed: 3,
    cancelled: 3,
  };
  const operationStatusRank: Record<string, number> = {
    queued: 0,
    running: 1,
    committing: 2,
    reconciling: 3,
    completed: 4,
    failed: 4,
    cancelled: 4,
    metadata_conflict: 4,
  };
  const operationStatus =
    cleanText(run.broadcast?.operation_status) ??
    cleanText(run.broadcast?.last_operation?.status) ??
    "";
  const completedAt = Date.parse(run.completed_at ?? "");
  return [
    runStatusRank[run.status] ?? -1,
    workflowStatusRank[cleanText(run.broadcast?.status) ?? ""] ?? -1,
    includeOperationStatus ? (operationStatusRank[operationStatus] ?? -1) : -1,
    Number.isFinite(completedAt) ? completedAt : 0,
    run.artifacts?.length ?? 0,
  ];
}

function compareRunsNewestFirst(left: RunRecord, right: RunRecord): number {
  const timestampDifference = runTimestamp(right) - runTimestamp(left);
  return timestampDifference || right.run_id.localeCompare(left.run_id);
}

function compareRunChronology(left: RunRecord, right: RunRecord): number {
  const createdDifference = runTimestamp(left) - runTimestamp(right);
  if (createdDifference !== 0) return createdDifference;
  return (
    optionalTimestamp(left.completed_at) - optionalTimestamp(right.completed_at)
  );
}

function runTimestamp(run: RunRecord): number {
  return optionalTimestamp(run.created_at);
}

function optionalTimestamp(value: string | null | undefined): number {
  const parsed = Date.parse(value ?? "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function buildBroadcastFieldConfigPatch(
  setup: BroadcastSetupInput,
  baseConfigPatch: Record<string, unknown>,
): Record<string, unknown> {
  const xs = setup.fieldPolygon.map(([x]) => x);
  const ys = setup.fieldPolygon.map(([, y]) => y);
  const bounds = [
    Math.min(...xs),
    Math.min(...ys),
    Math.max(...xs),
    Math.max(...ys),
  ];
  const centerX = (bounds[0] + bounds[2]) / 2;
  const centerY = (bounds[1] + bounds[3]) / 2;
  const sourceWidth = setup.confirmedFrames[0].frame_width;
  const sourceHeight = setup.confirmedFrames[0].frame_height;
  const expandedPolygon = setup.fieldPolygon.map(([x, y]) => [
    Math.max(
      0,
      Math.min(sourceWidth - 1, Math.round(centerX + (x - centerX) * 1.08)),
    ),
    Math.max(
      0,
      Math.min(sourceHeight - 1, Math.round(centerY + (y - centerY) * 1.1)),
    ),
  ]);
  const expandedXs = expandedPolygon.map(([x]) => x);
  const expandedYs = expandedPolygon.map(([, y]) => y);
  const expandedRoi = [
    Math.min(...expandedXs),
    Math.min(...expandedYs),
    Math.max(...expandedXs),
    Math.max(...expandedYs),
  ];
  const filtering = isRecord(baseConfigPatch.filtering)
    ? baseConfigPatch.filtering
    : {};
  const sceneBias = isRecord(baseConfigPatch.scene_bias)
    ? baseConfigPatch.scene_bias
    : {};
  const existingRoi = isFiniteRoi(filtering.roi) ? filtering.roi : expandedRoi;
  const existingPositiveRois = Array.isArray(sceneBias.positive_rois)
    ? sceneBias.positive_rois
    : [{ name: "field_buffer", points: expandedPolygon }];
  const dynamicAirRecovery = isRecord(sceneBias.dynamic_air_recovery)
    ? sceneBias.dynamic_air_recovery
    : {};

  return {
    ...baseConfigPatch,
    filtering: { ...filtering, roi: existingRoi },
    scene_bias: {
      ...sceneBias,
      enabled: true,
      ground_zones: [{ name: "field_core", points: setup.fieldPolygon }],
      positive_rois: existingPositiveRois,
      dynamic_air_recovery: {
        ...dynamicAirRecovery,
        enabled: true,
        edge_reentry_expand_x:
          dynamicAirRecovery.edge_reentry_expand_x ??
          Math.max(1, expandedRoi[2] - expandedRoi[0]),
        edge_reentry_expand_y:
          dynamicAirRecovery.edge_reentry_expand_y ??
          Math.max(1, expandedRoi[3] - expandedRoi[1]),
      },
    },
  };
}

function isFiniteRoi(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === 4 &&
    value.every(
      (coordinate) =>
        typeof coordinate === "number" && Number.isFinite(coordinate),
    )
  );
}

function validatePolygon(
  polygon: unknown,
  label: string,
  resolution: readonly [number, number],
  messages: string[],
): void {
  if (!Array.isArray(polygon) || polygon.length < 3) {
    messages.push(`${label} must contain at least three points.`);
    return;
  }
  let doubledArea = 0;
  for (let index = 0; index < polygon.length; index += 1) {
    const point = polygon[index];
    const next = polygon[(index + 1) % polygon.length];
    if (!isPoint(point) || !isPoint(next)) {
      messages.push(`${label} points must contain finite x/y coordinates.`);
      return;
    }
    if (
      point[0] < 0 ||
      point[0] >= resolution[0] ||
      point[1] < 0 ||
      point[1] >= resolution[1]
    ) {
      messages.push(`${label} points must lie inside the source resolution.`);
      return;
    }
    doubledArea += point[0] * next[1] - next[0] * point[1];
  }
  if (Math.abs(doubledArea) <= 1e-9)
    messages.push(`${label} must have non-zero area.`);
}

function isPoint(value: unknown): value is [number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === "number" &&
    Number.isFinite(value[0]) &&
    typeof value[1] === "number" &&
    Number.isFinite(value[1])
  );
}

function sameResolution(left: unknown, right: unknown): boolean {
  return (
    Array.isArray(left) &&
    Array.isArray(right) &&
    left.length === 2 &&
    right.length === 2 &&
    left[0] === right[0] &&
    left[1] === right[1]
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}

function isNonnegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isIsoTimestamp(value: string): boolean {
  return value.trim().length > 0 && Number.isFinite(Date.parse(value));
}

function cleanText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function safeRelativePath(value: unknown): string | null {
  const text = cleanText(value);
  if (
    !text ||
    text.includes("\\") ||
    text.startsWith("/") ||
    /^[a-zA-Z]:/.test(text)
  )
    return null;
  const parts = text.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) return null;
  return parts.join("/");
}

function success<T>(value: T): BroadcastValidationResult<T> {
  return { ok: true, value, messages: [] };
}

function failure<T>(message: string): BroadcastValidationResult<T> {
  return { ok: false, value: null, messages: [message] };
}
