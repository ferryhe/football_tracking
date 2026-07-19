import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";
import type {
  AnnotationBox,
  AnnotationPoint,
} from "@/lib/ballAnnotationGeometry";
import { BALL_ANNOTATION_OPERATOR_GOVERNANCE } from "@/lib/ballAnnotationGovernance";
import type { ReviewProxyRepairJobView } from "@/lib/productionReviewProxyRepair";

import type { BallAnnotationImageDecodeState } from "./BallAnnotationCanvas";

export const REVIEW_PROXY_REPAIR_BOUNDARY =
  "This only repairs playable, frame-mapped review media; it does not confirm the football, create truth, or constitute an independent production audit. / 这只修复可播放、可逐帧映射的复核媒体；不会确认足球位置，不会生成真值，也不构成独立生产审核。";

const BallAnnotationCanvas = lazy(() =>
  import("./BallAnnotationCanvas").then((module) => ({
    default: module.BallAnnotationCanvas,
  })),
);

export type BallPresence = "present" | "absent" | "unknown";
export type BallVisibility =
  | "visible"
  | "partial"
  | "unresolvable"
  | "not_applicable";
export type BallTrainingUse = "positive" | "background" | "excluded";
export type BallAnnotationState = "suggested" | "confirmed";
export type BallMotionOcclusionTag =
  | "ground"
  | "airborne"
  | "motion_blurred"
  | "occluded"
  | "reappearance"
  | "stationary";
export type BallAnnotationProvenance =
  | "manual_human_annotation"
  | "detector_candidate_human_confirmed"
  | "propagation_suggestion_human_confirmed"
  | "suggestion_dismissed_manual";

export interface BallAnnotationValueView {
  point: AnnotationPoint | null;
  bbox: AnnotationBox | null;
  presence: BallPresence;
  visibility: BallVisibility;
  trainingUse: BallTrainingUse;
  annotationState: BallAnnotationState;
  scaleStratum: "near" | "mid" | "far" | "not_applicable";
  lightingTag:
    | "bright_sun"
    | "shadow"
    | "backlight"
    | "twilight"
    | "artificial_light"
    | "not_applicable";
  motionOcclusionTags: BallMotionOcclusionTag[];
  provenance: BallAnnotationProvenance;
}

export interface BallAnnotationFrameView {
  frameIndex: number;
  displayTimeSeconds: number;
  decoderReportedPosMsec: number | null;
  decoderTimeSeconds: number | null;
  truePresentationTimestamp: {
    status: "not_collected";
    valueSeconds: null;
    method: null;
  };
  proxyBinding: {
    proxySha256: string;
    proxySizeBytes: number;
    proxyWidth: number;
    proxyHeight: number;
    mapSha256: string;
    bindingSha256: string;
    sourceFrame: {
      frameIndex: number;
      decoderReportedPosMsec: number | null;
      sha256: string;
    };
    proxyFrame: {
      frameIndex: number;
      decoderReportedPosMsec: number;
      sha256: string;
    };
    mapTimeToleranceMsec: number;
    declaredOffsetMsec: number;
    observedOffsetMsec: number | null;
    residualMsec: number | null;
  } | null;
  temporalGroupId: string;
  sourceFrameSha256: string;
  annotationRevision: number;
  annotationEtag: string;
  suggestedCandidates: Array<{
    candidateId: string;
    bbox: AnnotationBox;
    confidence: number;
    profileId: string;
    rank: number;
    suggestionJobId: string;
    suggestionSha256: string;
    decision: "pending" | "accepted" | "dismissed";
  }>;
  currentAnnotation: BallAnnotationValueView | null;
  frameRole?: "primary_sample" | "propagation_target";
  primarySample?: boolean;
  propagationSuggestions?: BallPropagationSuggestionView[];
}

export interface BallAnnotationSessionView {
  sessionId: string;
  requestSha256: string;
  dataRole: "development" | "check";
  status:
    | "sampling_locked"
    | "check_probe_queued"
    | "check_probe_running"
    | "check_probe_committing"
    | "annotating"
    | "finalizing"
    | "finalized"
    | "failed"
    | "cancelled"
    | "blocked";
  stage: string;
  source: {
    sourceId: string;
    sourceSha256: string;
    width: number;
    height: number;
    frameCount: number;
    fps: number;
  };
  decode: {
    requestedMode: "sequential" | "preroll" | "direct";
    effectiveMode:
      | "sequential"
      | "preroll_verified"
      | "direct_verified"
      | "sequential_fallback";
    positionVerification:
      | "opencv_next_frame_index_with_0.25_tolerance"
      | "verified_review_proxy_frame_index_mapping_v1";
  };
  lockedProfile: {
    profileId: string;
    profileSha256: string;
  };
  controlProfileId: string | null;
  samplingManifestSha256: string;
  metricProfileId: string;
  attemptFamilySha256: string;
  developmentPackageBinding: {
    sessionId: string;
    packageSha256: string;
    attemptFamilySha256: string;
  } | null;
  checkProbeJobId: string | null;
  checkProbeAuthority: {
    jobId: string;
    reportSha256: string;
  } | null;
  retryFromSessionId: string | null;
  retryLineage: {
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
  } | null;
  errorCode: string | null;
  blockerCode: string | null;
  reviewProxyRepair: {
    eligible: true;
    action: "generate_verified_review_proxy";
    createUrl: "/api/v1/detector-review-proxy-repairs";
    parentProbeJobId: string;
    parentProbeReportSha256: string;
    parentProbeResultManifestSha256: string;
    parentProbeRecordSha256: string;
    blockedSessionRecordSha256: string;
  } | null;
  frames: BallAnnotationFrameView[];
  progress: {
    annotatedFrames: number;
    totalFrames: number;
    unconfirmedSuggestions: number;
    missingStrata: string[];
    primaryAnnotatedFrames?: number;
    primaryTotalFrames?: number;
    supplementalAnnotatedFrames?: number;
    supplementalTotalFrames?: number;
    unconfirmedPropagationSuggestions?: number;
  };
  finalPackage: { packageSha256: string } | null;
}

export interface BallPropagationSuggestionView {
  suggestionId: string;
  jobId: string;
  suggestionSha256: string;
  frameIndex: number;
  temporalGroupId: string;
  point: AnnotationPoint | null;
  bbox: AnnotationBox | null;
  annotationState: "suggested";
  trainingUse: "excluded";
  provenance: string;
  selfCheck?: {
    matchScore: number;
    backwardMatchScore: number;
    forwardBackwardErrorPx: number;
    stepDisplacementPx: number;
  };
  pendingHumanConfirmation?: boolean;
  confirmationRevision?: number | null;
  decision?: "pending" | "confirmed" | "dismissed";
}

export interface BallSuggestionDecision {
  action: "accept" | "dismiss";
  kind: "detector_candidate" | "propagation";
  id: string;
  jobId: string;
  sha256: string;
}

export interface BallPropagationJobView {
  jobId: string;
  status:
    | "queued"
    | "waiting_probe"
    | "committing"
    | "ready"
    | "failed"
    | "blocked"
    | "cancelled";
  stage: string;
  pendingCount: number;
  targetFrameIndices: number[];
  errorCode: string | null;
}

interface ProductionBallAnnotationPanelProps {
  session: BallAnnotationSessionView;
  activeFrameOffset: number;
  frameImageUrl: string | null;
  frameImageState: "idle" | "loading" | "ready" | "failed";
  frameImageIdentity: string | null;
  operationState: "idle" | "saving" | "finalizing" | "propagating";
  operationError: string | null;
  propagationSuggestions?: BallPropagationSuggestionView[];
  propagationAvailable?: boolean;
  propagationJob?: BallPropagationJobView | null;
  reviewProxyRepairJob?: ReviewProxyRepairJobView | null;
  reviewProxyRepairOperation?:
    | "idle"
    | "starting"
    | "cancelling"
    | "retrying"
    | "reloading";
  reviewProxyRepairError?: string | null;
  onNavigate: (offset: number) => void;
  onSave: (
    annotation: BallAnnotationValueView,
    decision?: BallSuggestionDecision,
  ) => void;
  onDelete: () => void;
  onUndoSaved: (revision: number) => void;
  onStartPropagation: (radiusFrames: number) => void;
  onCancelPropagation?: () => void;
  onStartReviewProxyRepair?: () => void;
  onCancelReviewProxyRepair?: (repairId: string) => void;
  onRetryReviewProxyRepair?: (repairId: string) => void;
  onReloadReviewProxyRepair?: (repairId: string) => void;
  onReviewSuggestion?: (frameIndex: number, suggestionId: string) => void;
  onFinalize: () => void;
}

function panelLabels(language: "en" | "zh") {
  return language === "zh"
    ? {
        title: "逐帧点/框标注",
        description: "坐标始终使用原片像素；模型建议不会自动变为真值。",
        checkOnly: "数据隔离检查帧仅用于评估，绝不能训练检测器。",
        developmentOnly:
          "开发帧只用于探索和人工校准；不是盲测，也不会自动进入训练或生产真值。",
        previous: "上一帧",
        next: "下一帧",
        unsavedTitle: "当前帧有未保存修改",
        unsavedDescription:
          "保存后会自动继续；也可以丢弃修改并切帧，或留在当前帧。",
        saveAndNavigate: "保存并切换",
        discardAndNavigate: "丢弃修改并切换",
        stayOnFrame: "留在当前帧",
        frame: (index: number, displayTime: number) =>
          `帧 ${index} · 显示时间 ${displayTime.toFixed(3)} 秒`,
        displayTimeNotice: "显示时间仅由帧号 ÷ FPS 推算，不代表原片权威时间。",
        decoderTiming: "解码器时间证据",
        unavailableTiming: "不可用",
        truePresentationTimestamp: "原片真实显示时间戳",
        proxyBinding: "复核代理绑定",
        directSource: "原片直连帧 · 无代理绑定",
        proxyBindingSha: "代理绑定 SHA-256",
        proxySha: "代理媒体 SHA-256",
        proxyMapSha: "代理映射 SHA-256",
        proxyFrameMapping: (
          sourceFrame: number,
          sourceTime: number | null,
          proxyFrame: number,
          proxyTime: number,
        ) =>
          `原片帧 ${sourceFrame} @ ${sourceTime === null ? "不可用" : `${sourceTime.toFixed(3)} ms`} → 代理帧 ${proxyFrame} @ ${proxyTime.toFixed(3)} ms`,
        proxyTimeMapping: (
          declared: number,
          observed: number | null,
          residual: number | null,
          tolerance: number,
        ) =>
          `声明偏移 ${declared.toFixed(3)} ms · 观测 ${observed === null ? "不可用" : `${observed.toFixed(3)} ms`} · 残差 ${residual === null ? "不可用" : `${residual.toFixed(3)} ms`} · 容差 ${tolerance.toFixed(3)} ms`,
        timingVerification: "位置验证",
        verifiedFrame: "已校验原片帧",
        loadingFrame: "正在校验原片帧…",
        failedFrame: "帧内容校验失败，禁止标注。",
        decodingFrame: "正在解码已校验原片帧…",
        decodeFailedTitle: "原片帧解码失败",
        decodeFailed: "浏览器无法解码已校验的原片帧；禁止标注和完成本次会话。",
        presence: "是否出现",
        visibility: "可见性",
        training: "训练用途",
        scale: "尺度层次",
        lighting: "光照",
        motion: "运动/遮挡状态",
        present: "出现",
        absent: "确认缺席",
        unknown: "未知",
        visible: "可见",
        partial: "部分遮挡",
        unresolvable: "无法定位",
        notApplicable: "不适用",
        positive: "正样本",
        background: "背景",
        excluded: "排除",
        suggestion: "模型建议及服务端决定",
        canvasLoading: "正在加载标注画布…",
        suggestionWarning: "必须人工绘制或确认自己的框；不能直接把建议当真值。",
        candidate: (rank: number, confidence: number) =>
          `候选 #${rank} · 置信度 ${confidence.toFixed(3)}`,
        candidateCoordinates: (
          x: number,
          y: number,
          left: number,
          top: number,
          right: number,
          bottom: number,
        ) =>
          `中心 X ${x.toFixed(1)} / Y ${y.toFixed(1)} · L ${left.toFixed(1)} / T ${top.toFixed(1)} / R ${right.toFixed(1)} / B ${bottom.toFixed(1)}`,
        acceptSuggestion: "接受到草稿",
        adjustSuggestion: "调整后保存",
        ignoreSuggestion: "忽略建议",
        candidateDecision: (decision: "pending" | "accepted" | "dismissed") =>
          `服务端决定：${decision === "pending" ? "待处理" : decision === "accepted" ? "已接受" : "已忽略"}`,
        selectedSuggestion: "已选择建议；保存时将记录完整建议审计。",
        save: "保存确认标注",
        delete: "追加删除修订",
        undoSaved: "撤销已保存修订",
        propagate: "启动短窗口建议传播",
        radius: "传播半径（帧）",
        propagationNeedsSeed: "请先保存一个含球心或框的人工确认标注。",
        propagationHasChanges: "请先保存当前修改，再启动传播。",
        propagationResult: "传播结果：仅为建议",
        propagationSuggestion: (frameIndex: number) =>
          `帧 ${frameIndex} · 仅为建议 · 已排除 · 必须人工确认`,
        propagationJob: "传播任务",
        cancelPropagation: "取消传播任务",
        reviewSuggestion: "查看并处理",
        finalize: "锁定并生成一次性报告",
        invalid:
          "当前字段组合不合法；未知/不可定位不能带坐标，正样本必须有确认框。",
        checkBoxRequired:
          "数据隔离检查评分必须使用人工确认框。点只能用于开发传播的种子，不能为本未见帧检查帧计分。",
        progress: (done: number, total: number) =>
          `${done} / ${total} 帧已标注`,
        repairTitle: "审核代理修复",
        repairAction: "生成/修复审核代理",
        repairStarting: "正在启动审核代理修复…",
        repairCancel: "取消审核代理修复",
        repairRetry: "重试服务端修复任务",
        repairRetrying: "正在创建新的服务端修复尝试…",
        repairReload: "重新加载修复状态",
        repairReloading: "正在重新加载修复状态…",
        repairCommit: "正在发布已验证结果；此时不能取消。",
        repairProgress: (completed: number, total: number) =>
          `${completed} / ${total} 个原片帧已处理`,
        repairFailure: "审核代理修复未完成",
        repairProvenance: "审核代理修复来源与结果",
        repairParent: "不可变父探针",
        repairChild: "审核代理子探针",
        repairBlockedSession: "原阻塞会话",
        repairReplacementSession: "新标注会话",
        repairParentDigest: "父记录修复前/后 SHA-256",
        repairProxyManifest: "审核代理清单 SHA-256",
        repairProxyMedia: "代理媒体 SHA-256",
        repairMapping: "逐帧映射 SHA-256",
      }
    : {
        title: "Point/box annotation workspace",
        description:
          "Coordinates stay in source pixels; model suggestions never become truth automatically.",
        checkOnly:
          "Data-isolated check frames are evaluation-only and can never train a detector.",
        developmentOnly:
          "Development frames are only for exploration and human calibration; they are not a blind check and never enter training or production truth automatically.",
        previous: "Previous frame",
        next: "Next frame",
        unsavedTitle: "Unsaved frame changes",
        unsavedDescription:
          "Save to continue automatically, discard the draft and navigate, or stay on this frame.",
        saveAndNavigate: "Save and navigate",
        discardAndNavigate: "Discard draft and navigate",
        stayOnFrame: "Stay on this frame",
        frame: (index: number, displayTime: number) =>
          `Frame ${index} · display time ${displayTime.toFixed(3)} s`,
        displayTimeNotice:
          "Display time is frame index ÷ FPS only; it is not authoritative source timing.",
        decoderTiming: "Decoder timing evidence",
        unavailableTiming: "unavailable",
        truePresentationTimestamp: "True presentation timestamp",
        proxyBinding: "Review proxy binding",
        directSource: "Direct source frame · no proxy binding",
        proxyBindingSha: "Proxy binding SHA-256",
        proxySha: "Proxy media SHA-256",
        proxyMapSha: "Proxy map SHA-256",
        proxyFrameMapping: (
          sourceFrame: number,
          sourceTime: number | null,
          proxyFrame: number,
          proxyTime: number,
        ) =>
          `Source frame ${sourceFrame} @ ${sourceTime === null ? "unavailable" : `${sourceTime.toFixed(3)} ms`} → proxy frame ${proxyFrame} @ ${proxyTime.toFixed(3)} ms`,
        proxyTimeMapping: (
          declared: number,
          observed: number | null,
          residual: number | null,
          tolerance: number,
        ) =>
          `Declared offset ${declared.toFixed(3)} ms · observed ${observed === null ? "unavailable" : `${observed.toFixed(3)} ms`} · residual ${residual === null ? "unavailable" : `${residual.toFixed(3)} ms`} · tolerance ${tolerance.toFixed(3)} ms`,
        timingVerification: "Position verification",
        verifiedFrame: "Verified source frame",
        loadingFrame: "Verifying source frame…",
        failedFrame:
          "Frame content verification failed; annotation is blocked.",
        decodingFrame: "Decoding verified source frame…",
        decodeFailedTitle: "Source-frame decode failed",
        decodeFailed:
          "The verified frame could not be decoded by this browser; annotation and finalization are blocked.",
        presence: "Presence",
        visibility: "Visibility",
        training: "Training use",
        scale: "Scale stratum",
        lighting: "Lighting",
        motion: "Motion / occlusion state",
        present: "Present",
        absent: "Confirmed absent",
        unknown: "Unknown",
        visible: "Visible",
        partial: "Partial",
        unresolvable: "Unresolvable",
        notApplicable: "Not applicable",
        positive: "Positive",
        background: "Background",
        excluded: "Excluded",
        suggestion: "Model suggestions and server decisions",
        canvasLoading: "Loading annotation canvas…",
        suggestionWarning:
          "Draw or confirm your own box; a suggestion cannot be promoted as truth by display alone.",
        candidate: (rank: number, confidence: number) =>
          `Candidate #${rank} · confidence ${confidence.toFixed(3)}`,
        candidateCoordinates: (
          x: number,
          y: number,
          left: number,
          top: number,
          right: number,
          bottom: number,
        ) =>
          `center X ${x.toFixed(1)} / Y ${y.toFixed(1)} · L ${left.toFixed(1)} / T ${top.toFixed(1)} / R ${right.toFixed(1)} / B ${bottom.toFixed(1)}`,
        acceptSuggestion: "Accept into draft",
        adjustSuggestion: "Adjust before saving",
        ignoreSuggestion: "Ignore suggestion",
        candidateDecision: (decision: "pending" | "accepted" | "dismissed") =>
          `Server decision: ${decision}`,
        selectedSuggestion:
          "Suggestion selected; saving records its full audit binding.",
        save: "Save confirmed annotation",
        delete: "Append deletion revision",
        undoSaved: "Undo saved revision",
        propagate: "Start short-window suggestion propagation",
        radius: "Propagation radius (frames)",
        propagationNeedsSeed:
          "Save a human-confirmed point or box before propagation.",
        propagationHasChanges: "Save current changes before propagation.",
        propagationResult: "Propagation result: suggested only",
        propagationSuggestion: (frameIndex: number) =>
          `Frame ${frameIndex} · suggested only · excluded · manual confirmation required`,
        propagationJob: "Propagation job",
        cancelPropagation: "Cancel propagation job",
        reviewSuggestion: "Review and decide",
        finalize: "Freeze and generate one-time report",
        invalid:
          "This field combination is invalid. Unknown/unresolvable items cannot carry coordinates, and positives require a confirmed box.",
        checkBoxRequired:
          "Data-isolated check scoring requires a human-confirmed box. A point alone may seed development propagation, but it cannot score this unseen-frame check frame.",
        progress: (done: number, total: number) =>
          `${done} / ${total} frames annotated`,
        repairTitle: "Review-proxy repair",
        repairAction: "Generate/repair review proxy",
        repairStarting: "Starting review-proxy repair…",
        repairCancel: "Cancel review-proxy repair",
        repairRetry: "Retry server repair",
        repairRetrying: "Creating a new server repair attempt…",
        repairReload: "Reload repair status",
        repairReloading: "Reloading repair status…",
        repairCommit:
          "Publishing verified results; cancellation is unavailable.",
        repairProgress: (completed: number, total: number) =>
          `${completed} / ${total} source frames processed`,
        repairFailure: "Review-proxy repair did not complete",
        repairProvenance: "Review-proxy repair provenance",
        repairParent: "Immutable parent probe",
        repairChild: "Review-proxy child probe",
        repairBlockedSession: "Blocked source session",
        repairReplacementSession: "Replacement annotation session",
        repairParentDigest: "Parent record before/after SHA-256",
        repairProxyManifest: "Review-proxy manifest SHA-256",
        repairProxyMedia: "Proxy media SHA-256",
        repairMapping: "Frame mapping SHA-256",
      };
}

const EMPTY_ANNOTATION: BallAnnotationValueView = {
  point: null,
  bbox: null,
  presence: "unknown",
  visibility: "unresolvable",
  trainingUse: "excluded",
  annotationState: "confirmed",
  scaleStratum: "not_applicable",
  lightingTag: "bright_sun",
  motionOcclusionTags: [],
  provenance: "manual_human_annotation",
};

function validAnnotation(
  annotation: BallAnnotationValueView,
  role: "development" | "check",
) {
  if (role === "check" && annotation.trainingUse !== "excluded") return false;
  if (annotation.presence === "unknown") {
    return (
      ["unresolvable", "not_applicable"].includes(annotation.visibility) &&
      annotation.trainingUse === "excluded" &&
      annotation.scaleStratum === "not_applicable" &&
      annotation.point === null &&
      annotation.bbox === null
    );
  }
  if (annotation.presence === "absent") {
    return (
      annotation.visibility === "not_applicable" &&
      annotation.point === null &&
      annotation.bbox === null &&
      annotation.scaleStratum === "not_applicable" &&
      (annotation.trainingUse === "background" ||
        annotation.trainingUse === "excluded")
    );
  }
  if (annotation.visibility === "unresolvable") {
    return (
      annotation.trainingUse === "excluded" &&
      annotation.scaleStratum === "not_applicable" &&
      annotation.point === null &&
      annotation.bbox === null
    );
  }
  if (
    !["visible", "partial"].includes(annotation.visibility) ||
    !annotation.point ||
    annotation.scaleStratum === "not_applicable" ||
    annotation.trainingUse === "background"
  ) {
    return false;
  }
  if (role === "check" && annotation.bbox === null) return false;
  return annotation.trainingUse !== "positive" || annotation.bbox !== null;
}

function sameAnnotation(
  left: BallAnnotationValueView,
  right: BallAnnotationValueView,
) {
  const samePoint =
    left.point === right.point ||
    (left.point !== null &&
      right.point !== null &&
      left.point[0] === right.point[0] &&
      left.point[1] === right.point[1]);
  const sameBox =
    left.bbox === right.bbox ||
    (left.bbox !== null &&
      right.bbox !== null &&
      left.bbox.left === right.bbox.left &&
      left.bbox.top === right.bbox.top &&
      left.bbox.right === right.bbox.right &&
      left.bbox.bottom === right.bbox.bottom);
  return (
    samePoint &&
    sameBox &&
    left.presence === right.presence &&
    left.visibility === right.visibility &&
    left.trainingUse === right.trainingUse &&
    left.annotationState === right.annotationState &&
    left.scaleStratum === right.scaleStratum &&
    left.lightingTag === right.lightingTag &&
    left.provenance === right.provenance &&
    left.motionOcclusionTags.length === right.motionOcclusionTags.length &&
    left.motionOcclusionTags.every(
      (tag, index) => tag === right.motionOcclusionTags[index],
    )
  );
}

export function ProductionBallAnnotationPanel({
  session,
  activeFrameOffset,
  frameImageUrl,
  frameImageState,
  frameImageIdentity,
  operationState,
  operationError,
  propagationSuggestions = [],
  propagationAvailable = false,
  propagationJob = null,
  reviewProxyRepairJob = null,
  reviewProxyRepairOperation = "idle",
  reviewProxyRepairError = null,
  onNavigate,
  onSave,
  onDelete,
  onUndoSaved,
  onStartPropagation,
  onCancelPropagation = () => undefined,
  onStartReviewProxyRepair = () => undefined,
  onCancelReviewProxyRepair = () => undefined,
  onRetryReviewProxyRepair = () => undefined,
  onReloadReviewProxyRepair = () => undefined,
  onReviewSuggestion = () => undefined,
  onFinalize,
}: ProductionBallAnnotationPanelProps) {
  const { language } = useLanguage();
  const text = panelLabels(language);
  const frame = session.frames[activeFrameOffset] ?? null;
  const frameIdentity = frame
    ? `${session.sessionId}:${frame.frameIndex}:${frame.annotationRevision}`
    : "missing";
  const expectedFrameImageIdentity = frame
    ? `${session.sessionId}:${frame.frameIndex}:${frame.sourceFrameSha256}`
    : null;
  const verifiedFrameReady =
    frameImageState === "ready" &&
    frameImageUrl !== null &&
    expectedFrameImageIdentity !== null &&
    frameImageIdentity === expectedFrameImageIdentity;
  const frameDecodeKey =
    verifiedFrameReady && frameImageUrl && frameImageIdentity
      ? `${frameImageIdentity}:${frameImageUrl}`
      : null;
  const [draft, setDraft] = useState<BallAnnotationValueView>(EMPTY_ANNOTATION);
  const [history, setHistory] = useState<BallAnnotationValueView[]>([]);
  const [viewport, setViewport] = useState({ zoom: 1, pan: { x: 0, y: 0 } });
  const [propagationRadius, setPropagationRadius] = useState(2);
  const [pendingNavigation, setPendingNavigation] = useState<{
    offset: number;
    sourceFrameIndex: number;
  } | null>(null);
  const [navigationSaveRequested, setNavigationSaveRequested] = useState(false);
  const [suggestionDecision, setSuggestionDecision] =
    useState<BallSuggestionDecision | null>(null);
  const [selectedSuggestionBox, setSelectedSuggestionBox] =
    useState<AnnotationBox | null>(null);
  const canvasContainerRef = useRef<HTMLDivElement | null>(null);
  const [canvasWidth, setCanvasWidth] = useState(320);
  const [frameDecode, setFrameDecode] = useState<{
    key: string;
    state: BallAnnotationImageDecodeState;
  } | null>(null);
  const frameDecodeState = frameDecodeKey
    ? frameDecode?.key === frameDecodeKey
      ? frameDecode.state
      : "loading"
    : "idle";
  const handleImageDecodeStateChange = useCallback(
    (state: BallAnnotationImageDecodeState) => {
      if (!frameDecodeKey) return;
      setFrameDecode({ key: frameDecodeKey, state });
    },
    [frameDecodeKey],
  );

  useEffect(() => {
    setDraft(frame?.currentAnnotation ?? EMPTY_ANNOTATION);
    setHistory([]);
    setViewport({ zoom: 1, pan: { x: 0, y: 0 } });
    setSuggestionDecision(null);
    setSelectedSuggestionBox(null);
  }, [frameIdentity]);

  useEffect(() => {
    const element = canvasContainerRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      setCanvasWidth(Math.max(1, Math.min(960, entry.contentRect.width)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [frameIdentity, frameImageState]);

  const terminal = ["finalized", "failed", "cancelled", "blocked"].includes(
    session.status,
  );
  const busy = operationState !== "idle";
  const mutationBlocked =
    terminal ||
    busy ||
    session.status !== "annotating" ||
    !verifiedFrameReady ||
    frameDecodeState !== "ready";
  const valid = validAnnotation(draft, session.dataRole);
  const checkLocalizableNeedsBox =
    session.dataRole === "check" &&
    draft.presence === "present" &&
    ["visible", "partial"].includes(draft.visibility) &&
    draft.point !== null &&
    draft.bbox === null;
  const progress =
    session.progress.totalFrames > 0
      ? (session.progress.annotatedFrames / session.progress.totalFrames) * 100
      : 0;
  const firstSuggestion =
    selectedSuggestionBox ??
    frame?.suggestedCandidates.find(
      (candidate) => candidate.decision === "pending",
    )?.bbox ??
    null;
  const savedAnnotation = frame?.currentAnnotation ?? null;
  const baselineAnnotation = savedAnnotation ?? EMPTY_ANNOTATION;
  const hasSavedConfirmedSeed =
    savedAnnotation?.annotationState === "confirmed" &&
    Boolean(savedAnnotation.point || savedAnnotation.bbox);
  const hasUnsavedChanges =
    frame !== null && !sameAnnotation(draft, baselineAnnotation);
  const propagationBlockedReason =
    !propagationAvailable || session.dataRole !== "development"
      ? null
      : !hasSavedConfirmedSeed
        ? text.propagationNeedsSeed
        : hasUnsavedChanges
          ? text.propagationHasChanges
          : null;
  const repairCtaVisible =
    session.reviewProxyRepair?.eligible === true &&
    session.status === "blocked" &&
    session.blockerCode === "review_proxy_required" &&
    reviewProxyRepairJob === null;
  const repairVisible =
    repairCtaVisible ||
    reviewProxyRepairJob !== null ||
    reviewProxyRepairError !== null;
  const repairProgress = reviewProxyRepairJob
    ? (reviewProxyRepairJob.progress.sourceFramesCompleted /
        reviewProxyRepairJob.progress.sourceFramesTotal) *
      100
    : 0;

  function replaceDraft(next: BallAnnotationValueView) {
    setHistory((current) => [...current, draft]);
    setDraft(next);
  }

  function requestNavigation(offset: number) {
    if (!frame || !hasUnsavedChanges) {
      onNavigate(offset);
      return;
    }
    setNavigationSaveRequested(false);
    setPendingNavigation({ offset, sourceFrameIndex: frame.frameIndex });
  }

  function discardAndNavigate() {
    if (!pendingNavigation) return;
    const offset = pendingNavigation.offset;
    setNavigationSaveRequested(false);
    setPendingNavigation(null);
    onNavigate(offset);
  }

  function saveDraft() {
    if (pendingNavigation) setNavigationSaveRequested(true);
    const annotation = {
      ...draft,
      annotationState: "confirmed",
      provenance: suggestionDecision
        ? draft.provenance
        : "manual_human_annotation",
    } as const;
    if (suggestionDecision) {
      onSave(annotation, suggestionDecision);
      return;
    }
    onSave(annotation);
  }

  function loadSuggestion(
    kind: BallSuggestionDecision["kind"],
    id: string,
    jobId: string,
    sha256: string,
    point: AnnotationPoint | null,
    bbox: AnnotationBox | null,
  ) {
    const canonicalPoint = bbox
      ? ([(bbox.left + bbox.right) / 2, (bbox.top + bbox.bottom) / 2] as const)
      : point;
    setSuggestionDecision({ action: "accept", kind, id, jobId, sha256 });
    setSelectedSuggestionBox(bbox);
    replaceDraft({
      ...draft,
      point: canonicalPoint ? [canonicalPoint[0], canonicalPoint[1]] : null,
      bbox,
      presence: "present",
      visibility: "visible",
      trainingUse: session.dataRole === "check" ? "excluded" : "positive",
      annotationState: "confirmed",
      scaleStratum:
        draft.scaleStratum === "not_applicable" ? "far" : draft.scaleStratum,
      provenance:
        kind === "detector_candidate"
          ? "detector_candidate_human_confirmed"
          : "propagation_suggestion_human_confirmed",
    });
  }

  function dismissSuggestion(
    kind: BallSuggestionDecision["kind"],
    id: string,
    jobId: string,
    sha256: string,
  ) {
    const annotation = valid
      ? draft
      : {
          ...EMPTY_ANNOTATION,
          lightingTag: draft.lightingTag,
        };
    onSave(
      {
        ...annotation,
        annotationState: "confirmed",
        provenance: "suggestion_dismissed_manual",
      },
      { action: "dismiss", kind, id, jobId, sha256 },
    );
  }

  useEffect(() => {
    if (!pendingNavigation) return;
    if (
      !frame ||
      frame.frameIndex !== pendingNavigation.sourceFrameIndex ||
      activeFrameOffset === pendingNavigation.offset
    ) {
      setNavigationSaveRequested(false);
      setPendingNavigation(null);
      return;
    }
    if (
      busy ||
      hasUnsavedChanges ||
      !navigationSaveRequested ||
      operationError !== null
    )
      return;
    const offset = pendingNavigation.offset;
    setNavigationSaveRequested(false);
    setPendingNavigation(null);
    onNavigate(offset);
  }, [
    activeFrameOffset,
    busy,
    frame,
    hasUnsavedChanges,
    navigationSaveRequested,
    onNavigate,
    operationError,
    pendingNavigation,
  ]);

  function changePresence(presence: BallPresence) {
    if (presence === "unknown") {
      replaceDraft({
        ...draft,
        point: null,
        bbox: null,
        presence,
        visibility: "unresolvable",
        trainingUse: "excluded",
        scaleStratum: "not_applicable",
      });
    } else if (presence === "absent") {
      replaceDraft({
        ...draft,
        point: null,
        bbox: null,
        presence,
        visibility: "not_applicable",
        trainingUse: session.dataRole === "check" ? "excluded" : "background",
        scaleStratum: "not_applicable",
      });
    } else {
      replaceDraft({
        ...draft,
        presence,
        visibility: "visible",
        trainingUse: session.dataRole === "check" ? "excluded" : "positive",
        scaleStratum:
          draft.scaleStratum === "not_applicable" ? "far" : draft.scaleStratum,
      });
    }
  }

  function changeVisibility(visibility: BallVisibility) {
    if (visibility === "unresolvable") {
      replaceDraft({
        ...draft,
        point: null,
        bbox: null,
        presence: "present",
        visibility,
        trainingUse: "excluded",
        scaleStratum: "not_applicable",
      });
    } else {
      replaceDraft({ ...draft, visibility });
    }
  }

  const statusVariant = terminal ? "destructive" : "outline";

  return (
    <Card className="min-w-0" data-testid="ball-annotation-panel">
      <CardHeader>
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <CardTitle>{text.title}</CardTitle>
          <Badge variant={statusVariant} data-testid="annotation-session-state">
            {session.status}
          </Badge>
        </div>
        <CardDescription>{text.description}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0 space-y-5">
        <Alert>
          <AlertDescription>
            {session.dataRole === "check"
              ? text.checkOnly
              : text.developmentOnly}
          </AlertDescription>
        </Alert>
        <div
          className="rounded-md border bg-muted/30 p-3 text-sm"
          data-testid="ball-annotation-workspace-governance"
        >
          {BALL_ANNOTATION_OPERATOR_GOVERNANCE}
        </div>
        {repairVisible && (
          <section
            className="min-w-0 space-y-3 rounded-lg border p-4"
            aria-label={text.repairTitle}
            data-testid="review-proxy-repair-panel"
          >
            <h3 className="font-semibold">{text.repairTitle}</h3>
            <p className="text-sm text-muted-foreground">
              {REVIEW_PROXY_REPAIR_BOUNDARY}
            </p>
            {repairCtaVisible && (
              <Button
                type="button"
                className="min-h-11 w-full whitespace-normal sm:w-auto"
                disabled={reviewProxyRepairOperation !== "idle"}
                onClick={onStartReviewProxyRepair}
              >
                {reviewProxyRepairOperation === "starting" ? (
                  <Loader2
                    className="h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                ) : null}
                {reviewProxyRepairOperation === "starting"
                  ? text.repairStarting
                  : text.repairAction}
              </Button>
            )}
            {reviewProxyRepairJob && (
              <div
                className="min-w-0 space-y-3"
                aria-live="polite"
                aria-atomic="true"
                data-testid="review-proxy-repair-status"
              >
                <p className="break-words font-mono text-sm">
                  {reviewProxyRepairJob.status} · {reviewProxyRepairJob.stage}
                </p>
                <p className="text-sm">
                  {text.repairProgress(
                    reviewProxyRepairJob.progress.sourceFramesCompleted,
                    reviewProxyRepairJob.progress.sourceFramesTotal,
                  )}
                </p>
                <Progress
                  value={repairProgress}
                  aria-label={text.repairTitle}
                  aria-valuenow={repairProgress}
                />
                {reviewProxyRepairJob.status === "committing" && (
                  <p className="text-sm text-muted-foreground">
                    {text.repairCommit}
                  </p>
                )}
                {reviewProxyRepairJob.canCancel && (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11 w-full whitespace-normal sm:w-auto"
                    disabled={reviewProxyRepairOperation !== "idle"}
                    onClick={() =>
                      onCancelReviewProxyRepair(reviewProxyRepairJob.repairId)
                    }
                  >
                    {reviewProxyRepairOperation === "cancelling" && (
                      <Loader2
                        className="h-4 w-4 animate-spin"
                        aria-hidden="true"
                      />
                    )}
                    {text.repairCancel}
                  </Button>
                )}
                {reviewProxyRepairJob.canRetry && (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11 w-full whitespace-normal sm:w-auto"
                    disabled={reviewProxyRepairOperation !== "idle"}
                    onClick={() =>
                      onRetryReviewProxyRepair(reviewProxyRepairJob.repairId)
                    }
                  >
                    {reviewProxyRepairOperation === "retrying" && (
                      <Loader2
                        className="h-4 w-4 animate-spin"
                        aria-hidden="true"
                      />
                    )}
                    {reviewProxyRepairOperation === "retrying"
                      ? text.repairRetrying
                      : text.repairRetry}
                  </Button>
                )}
                {reviewProxyRepairError && (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11 w-full whitespace-normal sm:w-auto"
                    disabled={reviewProxyRepairOperation !== "idle"}
                    onClick={() =>
                      onReloadReviewProxyRepair(reviewProxyRepairJob.repairId)
                    }
                  >
                    {reviewProxyRepairOperation === "reloading" && (
                      <Loader2
                        className="h-4 w-4 animate-spin"
                        aria-hidden="true"
                      />
                    )}
                    {reviewProxyRepairOperation === "reloading"
                      ? text.repairReloading
                      : text.repairReload}
                  </Button>
                )}
                {["failed", "blocked"].includes(
                  reviewProxyRepairJob.status,
                ) && (
                  <Alert variant="destructive">
                    <AlertTitle>{text.repairFailure}</AlertTitle>
                    <AlertDescription className="space-y-1">
                      {reviewProxyRepairJob.errorCode && (
                        <p className="break-all font-mono">
                          {reviewProxyRepairJob.errorCode}
                        </p>
                      )}
                      {reviewProxyRepairJob.blockerCode && (
                        <p className="break-all font-mono">
                          {reviewProxyRepairJob.blockerCode}
                        </p>
                      )}
                      {reviewProxyRepairJob.recoveryAction && (
                        <p>{reviewProxyRepairJob.recoveryAction}</p>
                      )}
                    </AlertDescription>
                  </Alert>
                )}
                {reviewProxyRepairJob.result && (
                  <section
                    className="min-w-0 space-y-2 rounded-md border p-3 text-xs"
                    aria-label={text.repairProvenance}
                    data-testid="review-proxy-repair-provenance"
                  >
                    <h4 className="font-semibold">{text.repairProvenance}</h4>
                    <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
                      <div className="min-w-0">
                        <dt>{text.repairParent}</dt>
                        <dd className="break-all font-mono">
                          {reviewProxyRepairJob.authority.parentProbeJobId}
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt>{text.repairChild}</dt>
                        <dd className="break-all font-mono">
                          {reviewProxyRepairJob.result.childProbe.jobId}
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt>{text.repairBlockedSession}</dt>
                        <dd className="break-all font-mono">
                          {reviewProxyRepairJob.authority.blockedSessionId}
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt>{text.repairReplacementSession}</dt>
                        <dd className="break-all font-mono">
                          {
                            reviewProxyRepairJob.result.replacementSession
                              .sessionId
                          }
                        </dd>
                      </div>
                      <div className="min-w-0 sm:col-span-2">
                        <dt>{text.repairParentDigest}</dt>
                        <dd className="break-all font-mono">
                          {
                            reviewProxyRepairJob.authority
                              .parentProbeRecordSha256
                          }{" "}
                          /{" "}
                          {
                            reviewProxyRepairJob.result
                              .parentProbeRecordSha256After
                          }
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt>{text.repairProxyManifest}</dt>
                        <dd className="break-all font-mono">
                          {
                            reviewProxyRepairJob.result.proxy
                              .reviewProxyManifestSha256
                          }
                        </dd>
                      </div>
                      <div className="min-w-0">
                        <dt>{text.repairProxyMedia}</dt>
                        <dd className="break-all font-mono">
                          {reviewProxyRepairJob.result.proxy.proxyMediaSha256}
                        </dd>
                      </div>
                      <div className="min-w-0 sm:col-span-2">
                        <dt>{text.repairMapping}</dt>
                        <dd className="break-all font-mono">
                          {reviewProxyRepairJob.result.proxy.mappingSha256}
                        </dd>
                      </div>
                    </dl>
                  </section>
                )}
              </div>
            )}
            {reviewProxyRepairError && (
              <Alert variant="destructive">
                <AlertDescription>{reviewProxyRepairError}</AlertDescription>
              </Alert>
            )}
          </section>
        )}

        <div className="space-y-2">
          <p role="status" className="text-sm font-medium">
            {text.progress(
              session.progress.annotatedFrames,
              session.progress.totalFrames,
            )}
          </p>
          <Progress value={progress} />
        </div>

        <nav className="flex flex-wrap items-center justify-between gap-2">
          <Button
            type="button"
            variant="outline"
            aria-label={text.previous}
            disabled={activeFrameOffset <= 0 || busy}
            onClick={() => requestNavigation(activeFrameOffset - 1)}
          >
            <ChevronLeft aria-hidden="true" />
            {text.previous}
          </Button>
          {frame && (
            <div className="text-center">
              <p className="font-medium">
                {text.frame(frame.frameIndex, frame.displayTimeSeconds)}
              </p>
              <p className="text-xs text-muted-foreground">
                {text.displayTimeNotice}
              </p>
            </div>
          )}
          <Button
            type="button"
            variant="outline"
            aria-label={text.next}
            disabled={activeFrameOffset >= session.frames.length - 1 || busy}
            onClick={() => requestNavigation(activeFrameOffset + 1)}
          >
            {text.next}
            <ChevronRight aria-hidden="true" />
          </Button>
        </nav>

        {pendingNavigation && (
          <Alert aria-label={text.unsavedTitle}>
            <AlertTitle>{text.unsavedTitle}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{text.unsavedDescription}</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={saveDraft}
                  disabled={mutationBlocked || !valid}
                >
                  {text.saveAndNavigate}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="destructive"
                  onClick={discardAndNavigate}
                  disabled={busy}
                >
                  {text.discardAndNavigate}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setNavigationSaveRequested(false);
                    setPendingNavigation(null);
                  }}
                  disabled={busy}
                >
                  {text.stayOnFrame}
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        {frame && (
          <dl className="grid min-w-0 gap-2 text-xs sm:grid-cols-2">
            <div className="min-w-0 rounded-md border p-2">
              <dt>source frame SHA-256</dt>
              <dd className="break-all font-mono">{frame.sourceFrameSha256}</dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>temporal group</dt>
              <dd className="break-all font-mono">{frame.temporalGroupId}</dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>revision / ETag</dt>
              <dd className="break-all font-mono">
                {frame.annotationRevision} · {frame.annotationEtag}
              </dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>locked profile</dt>
              <dd className="break-all font-mono">
                {session.lockedProfile.profileId}
              </dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{text.decoderTiming}</dt>
              <dd className="break-all font-mono">
                {frame.decoderReportedPosMsec === null ||
                frame.decoderTimeSeconds === null ? (
                  text.unavailableTiming
                ) : (
                  <>
                    {frame.decoderReportedPosMsec.toFixed(3)} ms ·{" "}
                    {frame.decoderTimeSeconds.toFixed(6)} s
                  </>
                )}
              </dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{text.timingVerification}</dt>
              <dd className="break-all font-mono">
                {session.decode.positionVerification} ·{" "}
                {session.decode.requestedMode} → {session.decode.effectiveMode}
              </dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{text.truePresentationTimestamp}</dt>
              <dd className="break-all font-mono">
                {frame.truePresentationTimestamp.status}
              </dd>
            </div>
          </dl>
        )}
        {frame && (
          <section
            className="min-w-0 space-y-2 rounded-md border p-3 text-xs"
            aria-label={text.proxyBinding}
          >
            <h4 className="font-semibold">{text.proxyBinding}</h4>
            {frame.proxyBinding === null ? (
              <p>{text.directSource}</p>
            ) : (
              <div className="min-w-0 space-y-1">
                <p className="break-all font-mono">
                  <strong>{text.proxyBindingSha}</strong>{" "}
                  {frame.proxyBinding.bindingSha256}
                </p>
                <p className="break-all font-mono">
                  <strong>{text.proxySha}</strong>{" "}
                  {frame.proxyBinding.proxySha256}
                </p>
                <p className="break-all font-mono">
                  <strong>{text.proxyMapSha}</strong>{" "}
                  {frame.proxyBinding.mapSha256}
                </p>
                <p className="break-words font-mono">
                  {text.proxyFrameMapping(
                    frame.proxyBinding.sourceFrame.frameIndex,
                    frame.proxyBinding.sourceFrame.decoderReportedPosMsec,
                    frame.proxyBinding.proxyFrame.frameIndex,
                    frame.proxyBinding.proxyFrame.decoderReportedPosMsec,
                  )}
                </p>
                <p className="break-words font-mono">
                  {text.proxyTimeMapping(
                    frame.proxyBinding.declaredOffsetMsec,
                    frame.proxyBinding.observedOffsetMsec,
                    frame.proxyBinding.residualMsec,
                    frame.proxyBinding.mapTimeToleranceMsec,
                  )}
                </p>
              </div>
            )}
          </section>
        )}

        {frame?.suggestedCandidates.length ? (
          <section
            className="space-y-2"
            aria-labelledby="detector-candidates-title"
          >
            <Alert>
              <AlertTitle id="detector-candidates-title">
                {text.suggestion}
              </AlertTitle>
              <AlertDescription>{text.suggestionWarning}</AlertDescription>
            </Alert>
            <ul className="space-y-2">
              {frame.suggestedCandidates.map((candidate) => {
                const x = (candidate.bbox.left + candidate.bbox.right) / 2;
                const y = (candidate.bbox.top + candidate.bbox.bottom) / 2;
                return (
                  <li
                    key={candidate.candidateId}
                    className="min-w-0 space-y-2 rounded-md border p-3"
                  >
                    <p className="font-medium">
                      {text.candidate(candidate.rank, candidate.confidence)}
                    </p>
                    <p className="break-words font-mono text-xs">
                      {text.candidateCoordinates(
                        x,
                        y,
                        candidate.bbox.left,
                        candidate.bbox.top,
                        candidate.bbox.right,
                        candidate.bbox.bottom,
                      )}
                    </p>
                    <p className="break-all font-mono text-xs text-muted-foreground">
                      {candidate.profileId} · {candidate.candidateId}
                    </p>
                    <Badge
                      variant={
                        candidate.decision === "pending"
                          ? "secondary"
                          : "outline"
                      }
                    >
                      {text.candidateDecision(candidate.decision)}
                    </Badge>
                    {candidate.decision === "pending" && (
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          onClick={() =>
                            loadSuggestion(
                              "detector_candidate",
                              candidate.candidateId,
                              candidate.suggestionJobId,
                              candidate.suggestionSha256,
                              [x, y],
                              candidate.bbox,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.acceptSuggestion}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            loadSuggestion(
                              "detector_candidate",
                              candidate.candidateId,
                              candidate.suggestionJobId,
                              candidate.suggestionSha256,
                              [x, y],
                              candidate.bbox,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.adjustSuggestion}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            dismissSuggestion(
                              "detector_candidate",
                              candidate.candidateId,
                              candidate.suggestionJobId,
                              candidate.suggestionSha256,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.ignoreSuggestion}
                        </Button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {suggestionDecision?.action === "accept" && (
          <p
            role="status"
            className="text-sm text-amber-700 dark:text-amber-300"
          >
            {text.selectedSuggestion}
          </p>
        )}

        {propagationSuggestions.length > 0 && (
          <Alert aria-label={text.propagationResult}>
            <AlertTitle>{text.propagationResult}</AlertTitle>
            <AlertDescription>
              <ul className="space-y-1">
                {propagationSuggestions.map((suggestion) => (
                  <li
                    key={suggestion.suggestionId}
                    className="space-y-2 rounded-md border p-2"
                  >
                    <p>
                      {text.propagationSuggestion(suggestion.frameIndex)} ·{" "}
                      <span className="font-mono">
                        {suggestion.temporalGroupId}
                      </span>
                    </p>
                    {suggestion.pendingHumanConfirmation !== false && (
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          onClick={() =>
                            loadSuggestion(
                              "propagation",
                              suggestion.suggestionId,
                              suggestion.jobId,
                              suggestion.suggestionSha256,
                              suggestion.point,
                              suggestion.bbox,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.acceptSuggestion}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            loadSuggestion(
                              "propagation",
                              suggestion.suggestionId,
                              suggestion.jobId,
                              suggestion.suggestionSha256,
                              suggestion.point,
                              suggestion.bbox,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.adjustSuggestion}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            dismissSuggestion(
                              "propagation",
                              suggestion.suggestionId,
                              suggestion.jobId,
                              suggestion.suggestionSha256,
                            )
                          }
                          disabled={mutationBlocked}
                        >
                          {text.ignoreSuggestion}
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        {(frameImageState === "loading" ||
          (frameImageState === "ready" && !verifiedFrameReady)) && (
          <p role="status">
            <Loader2
              className="mr-2 inline h-4 w-4 animate-spin"
              aria-hidden="true"
            />
            {text.loadingFrame}
          </p>
        )}
        {frameImageState === "failed" && (
          <Alert variant="destructive">
            <AlertDescription>{text.failedFrame}</AlertDescription>
          </Alert>
        )}
        {verifiedFrameReady && frameDecodeState === "loading" && (
          <p role="status">{text.decodingFrame}</p>
        )}
        {verifiedFrameReady && frameDecodeState === "failed" && (
          <Alert variant="destructive">
            <AlertTitle>{text.decodeFailedTitle}</AlertTitle>
            <AlertDescription>{text.decodeFailed}</AlertDescription>
          </Alert>
        )}
        {frame && verifiedFrameReady && frameImageUrl && (
          <section aria-label={text.verifiedFrame}>
            <div ref={canvasContainerRef} className="min-w-0 max-w-full">
              <Suspense fallback={<p role="status">{text.canvasLoading}</p>}>
                <BallAnnotationCanvas
                  key={frameDecodeKey}
                  sourceSize={{
                    width: session.source.width,
                    height: session.source.height,
                  }}
                  displaySize={{
                    width: canvasWidth,
                    height: Math.max(220, Math.min(540, canvasWidth * 0.5625)),
                  }}
                  imageUrl={frameImageUrl}
                  point={draft.point}
                  box={draft.bbox}
                  suggestion={firstSuggestion}
                  zoom={viewport.zoom}
                  pan={viewport.pan}
                  disabled={mutationBlocked}
                  canUndo={history.length > 0}
                  onGeometryChange={({ point, box: bbox }) =>
                    replaceDraft({ ...draft, point, bbox })
                  }
                  onClearGeometry={() =>
                    replaceDraft({
                      ...draft,
                      point: null,
                      bbox: null,
                      presence: "unknown",
                      visibility: "unresolvable",
                      trainingUse: "excluded",
                      scaleStratum: "not_applicable",
                    })
                  }
                  onUndo={() => {
                    const previous = history.at(-1);
                    if (!previous) return;
                    setDraft(previous);
                    setHistory((current) => current.slice(0, -1));
                  }}
                  onViewportChange={setViewport}
                  onImageDecodeStateChange={handleImageDecodeStateChange}
                />
              </Suspense>
            </div>
          </section>
        )}

        <fieldset disabled={mutationBlocked} className="min-w-0 space-y-3">
          <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <div className="min-w-0 space-y-1">
              <Label htmlFor="ball-presence">{text.presence}</Label>
              <select
                id="ball-presence"
                className="min-h-9 w-full rounded-md border bg-background px-2"
                value={draft.presence}
                onChange={(event) =>
                  changePresence(event.target.value as BallPresence)
                }
              >
                <option value="present">{text.present}</option>
                <option value="absent">{text.absent}</option>
                <option value="unknown">{text.unknown}</option>
              </select>
            </div>
            <div className="min-w-0 space-y-1">
              <Label htmlFor="ball-visibility">{text.visibility}</Label>
              <select
                id="ball-visibility"
                className="min-h-9 w-full rounded-md border bg-background px-2"
                value={draft.visibility}
                onChange={(event) =>
                  changeVisibility(event.target.value as BallVisibility)
                }
              >
                <option value="visible">{text.visible}</option>
                <option value="partial">{text.partial}</option>
                <option value="unresolvable">{text.unresolvable}</option>
                <option value="not_applicable">{text.notApplicable}</option>
              </select>
            </div>
            <div className="min-w-0 space-y-1">
              <Label htmlFor="ball-training-use">{text.training}</Label>
              <select
                id="ball-training-use"
                className="min-h-9 w-full rounded-md border bg-background px-2"
                value={draft.trainingUse}
                disabled={session.dataRole === "check" || mutationBlocked}
                onChange={(event) =>
                  replaceDraft({
                    ...draft,
                    trainingUse: event.target.value as BallTrainingUse,
                  })
                }
              >
                <option value="positive">{text.positive}</option>
                <option value="background">{text.background}</option>
                <option value="excluded">{text.excluded}</option>
              </select>
            </div>
            <div className="min-w-0 space-y-1">
              <Label htmlFor="ball-scale">{text.scale}</Label>
              <select
                id="ball-scale"
                className="min-h-9 w-full rounded-md border bg-background px-2"
                value={draft.scaleStratum}
                onChange={(event) =>
                  replaceDraft({
                    ...draft,
                    scaleStratum: event.target
                      .value as BallAnnotationValueView["scaleStratum"],
                  })
                }
              >
                <option value="near">near</option>
                <option value="mid">mid</option>
                <option value="far">far</option>
                <option value="not_applicable">{text.notApplicable}</option>
              </select>
            </div>
            <div className="min-w-0 space-y-1">
              <Label htmlFor="ball-lighting">{text.lighting}</Label>
              <select
                id="ball-lighting"
                className="min-h-9 w-full rounded-md border bg-background px-2"
                value={draft.lightingTag}
                onChange={(event) =>
                  replaceDraft({
                    ...draft,
                    lightingTag: event.target
                      .value as BallAnnotationValueView["lightingTag"],
                  })
                }
              >
                {[
                  "bright_sun",
                  "shadow",
                  "backlight",
                  "twilight",
                  "artificial_light",
                  "not_applicable",
                ].map((lighting) => (
                  <option key={lighting} value={lighting}>
                    {lighting}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">{text.motion}</legend>
            <div className="flex flex-wrap gap-3">
              {(
                [
                  "ground",
                  "airborne",
                  "motion_blurred",
                  "occluded",
                  "reappearance",
                  "stationary",
                ] as const
              ).map((tag) => {
                const inputId = `ball-motion-${tag}`;
                return (
                  <div key={tag} className="flex items-center gap-2">
                    <Checkbox
                      id={inputId}
                      checked={draft.motionOcclusionTags.includes(tag)}
                      onCheckedChange={(checked) =>
                        replaceDraft({
                          ...draft,
                          motionOcclusionTags:
                            checked === true
                              ? [...draft.motionOcclusionTags, tag].sort()
                              : draft.motionOcclusionTags.filter(
                                  (candidate) => candidate !== tag,
                                ),
                        })
                      }
                    />
                    <Label htmlFor={inputId}>{tag}</Label>
                  </div>
                );
              })}
            </div>
          </fieldset>
        </fieldset>

        {!valid && (
          <p role="alert" className="text-sm text-destructive">
            {checkLocalizableNeedsBox ? text.checkBoxRequired : text.invalid}
          </p>
        )}
        {operationError && (
          <Alert variant="destructive">
            <AlertDescription>{operationError}</AlertDescription>
          </Alert>
        )}

        <div className="flex min-w-0 flex-wrap gap-2">
          <Button
            type="button"
            onClick={saveDraft}
            disabled={mutationBlocked || !valid}
          >
            {operationState === "saving" && (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            )}
            {text.save}
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={onDelete}
            disabled={mutationBlocked || !frame?.currentAnnotation}
          >
            {text.delete}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => frame && onUndoSaved(frame.annotationRevision)}
            disabled={mutationBlocked || !frame || frame.annotationRevision < 1}
          >
            {text.undoSaved}
          </Button>
        </div>

        {propagationAvailable && (
          <div className="flex min-w-0 flex-wrap items-end gap-2">
            <div className="space-y-1">
              <Label htmlFor="propagation-radius">{text.radius}</Label>
              <Input
                id="propagation-radius"
                type="number"
                min={1}
                max={2}
                value={propagationRadius}
                onChange={(event) =>
                  setPropagationRadius(Number(event.target.value))
                }
                disabled={mutationBlocked || session.dataRole !== "development"}
                className="w-24"
              />
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={() => onStartPropagation(propagationRadius)}
              disabled={
                mutationBlocked ||
                session.dataRole !== "development" ||
                !hasSavedConfirmedSeed ||
                hasUnsavedChanges ||
                !Number.isInteger(propagationRadius) ||
                propagationRadius < 1 ||
                propagationRadius > 2
              }
            >
              {text.propagate}
            </Button>
          </div>
        )}
        {propagationJob && (
          <Alert aria-label={text.propagationJob}>
            <AlertTitle>{text.propagationJob}</AlertTitle>
            <AlertDescription className="space-y-2">
              <p className="break-all font-mono text-xs">
                {propagationJob.jobId} · {propagationJob.status} ·{" "}
                {propagationJob.stage}
              </p>
              <p className="text-sm">
                pending_human_confirmation={propagationJob.pendingCount}
              </p>
              {["queued", "waiting_probe", "committing"].includes(
                propagationJob.status,
              ) &&
                onCancelPropagation && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={onCancelPropagation}
                    disabled={mutationBlocked}
                  >
                    {text.cancelPropagation}
                  </Button>
                )}
            </AlertDescription>
          </Alert>
        )}
        {propagationBlockedReason && (
          <p className="text-sm text-muted-foreground">
            {propagationBlockedReason}
          </p>
        )}

        <Button
          type="button"
          onClick={onFinalize}
          disabled={
            mutationBlocked ||
            session.progress.annotatedFrames !== session.progress.totalFrames ||
            session.progress.unconfirmedSuggestions > 0 ||
            (session.progress.unconfirmedPropagationSuggestions ?? 0) > 0
          }
        >
          {operationState === "finalizing" && (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
          )}
          {text.finalize}
        </Button>
      </CardContent>
    </Card>
  );
}
