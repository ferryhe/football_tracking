import { useEffect, useMemo, useState } from "react";
import { Loader2, Play, RotateCcw, Square } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";

export type DetectorProbeCatalogState = "loading" | "ready" | "failed";
export type DetectorProbeAvailability = "available" | "unavailable" | "blocked";
export type DetectorProbeJobStatus =
  | "queued"
  | "running"
  | "committing"
  | "ready"
  | "failed"
  | "cancelled"
  | "blocked";

export interface DetectorProbeEgressView {
  leavesDevice: boolean | null;
  destination: string | null;
  consent:
    | "not_required"
    | "granted"
    | "required_not_granted"
    | "required_before_external_inference"
    | "unknown";
}

export interface DetectorProbeProfileView {
  profileId: string;
  version: string;
  digest: string;
  mode: "direct" | "sahi";
  inputSize: number;
  confidenceThreshold: number;
  tile: {
    width: number;
    height: number;
    overlapWidthRatio: number;
    overlapHeightRatio: number;
  } | null;
  topK: number;
  probeSelectable: boolean;
  recommended?: boolean;
  unavailableReason?: string;
}

export interface DetectorProbeModelView {
  kind: "registered" | "catalog_finding";
  modelId: string;
  version: string;
  runtimeVersion: string;
  displayName: string;
  architectureFamily: string;
  sourceProject: string;
  sourceVersion: string;
  acquisitionMethod: string;
  accessRequirement: string;
  weightsSha256: string | null;
  manifestSha256: string | null;
  lifecycle: string;
  trialEligible: boolean;
  sourceSegmentQualified: boolean;
  cameraQualified: boolean;
  availability: DetectorProbeAvailability;
  availabilityReason?: string;
  datasetLicense: string;
  modelLicense: string;
  runtimeLicense: string;
  deploymentLicense: string;
  egress: DetectorProbeEgressView;
  profiles: DetectorProbeProfileView[];
}

export interface DetectorProbeBoxView {
  x: number;
  y: number;
  width: number;
  height: number;
  confidence: number;
  label: string;
}

export interface DetectorProbeProfileEvidenceView {
  profileId: string;
  profileSha256: string;
  status: "completed" | "failed" | "blocked";
  overlayImageUrl: string;
  overlaySha256: string;
  overlaySizeBytes: number;
  rawBoxes: DetectorProbeBoxView[];
  displayCandidate: DetectorProbeBoxView | null;
  latencyMs: number | null;
  candidateCount: number;
  topK: number;
  filterReasons: Record<string, number>;
  failureCode: string | null;
}

export interface DetectorProbeFrameEvidenceView {
  frameIndex: number;
  sourceImageUrl: string;
  sourceSha256: string;
  sourceSizeBytes: number;
  sourceWidth: number;
  sourceHeight: number;
  mediaIntegrityClean: boolean;
  mediaIntegrityReasons: string[];
  profiles: DetectorProbeProfileEvidenceView[];
}

export interface DetectorProbeJobView {
  jobId: string;
  parentTrialId: string;
  requestSha256: string;
  immutableIdentity: string;
  resultManifestSha256: string | null;
  status: DetectorProbeJobStatus;
  stage: string;
  progressPercent: number;
  selectedProfileIds: string[];
  frameIndices: number[];
  retryFromJobId: string | null;
  failureCode: string | null;
  recoveryAction: string | null;
  noProfilesProducedCandidates: boolean;
  frames: DetectorProbeFrameEvidenceView[];
}

export interface ProductionDetectorProbePanelProps {
  models: DetectorProbeModelView[];
  catalogState: DetectorProbeCatalogState;
  catalogError?: string | null;
  operationError?: string | null;
  recoveryError?: string | null;
  recoveryErrorKind?: "invalid_pointer" | "transport" | "integrity" | null;
  job: DetectorProbeJobView | null;
  mutationPending: boolean;
  actionsBlocked?: boolean;
  exactCreatePending?: boolean;
  onStart: (profileIds: string[]) => void;
  onCancel: (jobId: string) => void;
  onRetry: (request: { retryFromJobId: string; profileIds: string[] }) => void;
  onDiscardRecovery?: () => void;
  onRefreshRecovery?: () => void;
  onReloadCatalog?: () => void;
  onRetryCreate?: () => void;
}

function profileIdentity(models: readonly DetectorProbeModelView[]) {
  return models
    .flatMap((model) => model.profiles)
    .map(
      (profile) =>
        `${profile.profileId}:${profile.digest}:${profile.probeSelectable}`,
    )
    .sort()
    .join("|");
}

function initialProfileIds(models: readonly DetectorProbeModelView[]) {
  const selectable = models
    .filter((model) => model.availability === "available")
    .flatMap((model) =>
      model.profiles
        .filter((profile) => profile.probeSelectable)
        .map((profile) => ({ modelId: model.modelId, profile })),
    );
  const recommended = selectable.filter(({ profile }) => profile.recommended);
  const ordered = [
    ...recommended,
    ...selectable.filter(({ profile }) => !profile.recommended),
  ];
  const selected = ordered.slice(0, 1);
  for (const candidate of ordered) {
    if (selected.length >= 2) break;
    if (
      selected.some(
        ({ modelId, profile }) =>
          modelId === candidate.modelId ||
          profile.profileId === candidate.profile.profileId,
      )
    ) {
      continue;
    }
    selected.push(candidate);
  }
  for (const candidate of ordered) {
    if (selected.length >= 2) break;
    if (
      !selected.some(
        ({ profile }) => profile.profileId === candidate.profile.profileId,
      )
    ) {
      selected.push(candidate);
    }
  }
  return selected.map(({ profile }) => profile.profileId);
}

function clampProgress(value: number) {
  return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
}

function formatBox(box: DetectorProbeBoxView) {
  return `x=${box.x}, y=${box.y}, w=${box.width}, h=${box.height} · ${box.confidence.toFixed(3)} · ${box.label}`;
}

function detectorProbeLabels(language: "en" | "zh") {
  if (language === "zh") {
    return {
      title: "模型与有界探针对比",
      description:
        "在同一组原片帧上比较至少两个精确模型配置；这里只做有界探针，不会改写试跑参数或接受状态。",
      loading: "正在读取精确模型注册表…",
      unavailable: "精确模型注册表不可用，当前不能运行可信的模型对比。",
      selectAtLeastTwo: "请选择 2–6 个可用的精确配置。",
      run: "运行有界对比",
      running: "正在运行有界对比…",
      cancel: "取消对比",
      retry: "明确重试对比",
      newComparison: "以当前配置运行新的根对比",
      committing: "开始发布结果后不能取消。",
      localOnly: "仅本机运行；帧不会离开这台机器。",
      egress: "帧会发送到外部目的地",
      egressUnknown:
        "帧是否离开本机尚未确定；在明确访问方式并取得同意前不可选择。",
      registryIdentity: "模型身份",
      sourceIdentity: "来源 / 获取方式",
      access: "访问要求",
      profileIdentity: "配置身份",
      availability: "可用性",
      lifecycle: "生命周期",
      probeOnly: "仅限探针；不能用于接受试跑。",
      trialEligible: "可用于试跑",
      sourceQualified: "同源片段已验证",
      cameraQualified: "同机位已验证",
      licenses: "许可证",
      dataset: "数据集",
      model: "模型",
      runtime: "运行时",
      deployment: "部署",
      runtimeVersion: "已安装运行时版本",
      weightsSha: "权重 SHA-256",
      manifestSha: "清单 SHA-256",
      profileSha: "配置 SHA-256",
      confidence: "置信度",
      tile: "切片",
      evidenceProfileSha: "本次证据绑定的配置 SHA-256",
      notAcquired: "未获取",
      selected: "已选择",
      recommended: "推荐",
      sourceFrame: (frame: number) => `原片帧 ${frame}`,
      sourceSha: "原片帧 SHA-256",
      sourceDimensions: "原片尺寸",
      rawBoxes: "保留的 top-K 候选框（原片像素：x、y、w、h）",
      rawOverlay: (profileId: string, frame: number) =>
        `配置 ${profileId} 在原片帧 ${frame} 上的原始检测叠加图`,
      rawOverlaySha: "原始叠加图 SHA-256",
      bytes: "字节",
      noRawBoxes: "没有保留的候选框",
      displayCandidate: "展示候选",
      noDisplayCandidate: "没有展示候选",
      latency: "延迟",
      candidates: "候选数",
      filterReasons: "过滤原因",
      noFilterReasons: "没有记录过滤原因",
      progress: "检测探针进度",
      status: "状态",
      jobIdentity: "任务身份",
      requestSha: "请求 SHA-256",
      parentTrial: "父试跑",
      requestedFrames: "同帧请求",
      failure: "探针对比未完成",
      noRetainedCandidates: "本次有界对比中，所有已选配置均未保留任何候选框。",
      candidateCaveat:
        "证据图片全部验证后，如显示候选框，它们也只是未经人工确认的检测器输出，不代表已经找到足球；正确性由下一步标注验证确认。",
      t3Next:
        "下一步进入 20–50 帧可行性检查：在近、中、远距离和不同光照帧上指出或框出足球，再判断是否训练机位适配模型。",
      evidenceMissing:
        "就绪报告没有完整且成功执行的同帧证据，因此不能作为可信对比。请通过显式重试创建子任务。",
      evidenceImagesLoading:
        "正在验证所有原片帧和叠加图均可读取；完成前不会给出零候选或非零候选结论。",
      evidenceImagesFailed:
        "至少一张证据图片无法读取，因此本次结果不能作为可信结论。请明确重试。",
      serverFrames:
        "探索帧由后台从父试跑已冻结的跟踪契约中选择；所有配置严格比较同一组帧。",
      discardRecovery: "丢弃不可恢复的本地任务指针",
      discardRecoveryHelp:
        "这不会取消后台任务；它只清除当前浏览器中无法验证的恢复指针。",
      refreshRecovery: "重新读取当前任务",
      refreshRecoveryHelp:
        "已保留当前任务指针。重新读取成功前不会启动或替换后台任务。",
      reloadCatalog: "重新加载模型目录",
      retryCreate: "按原请求重试创建",
      previousFrame: "上一帧",
      nextFrame: "下一帧",
      framePosition: (current: number, total: number) =>
        `证据帧 ${current} / ${total}`,
      resultManifestSha: "结果清单 SHA-256",
    };
  }
  return {
    title: "Model and bounded probe comparison",
    description:
      "Compare at least two exact profiles on the same source frames. This bounded probe does not change trial tuning or acceptance.",
    loading: "Loading the exact model registry…",
    unavailable:
      "The exact model registry is unavailable, so a trustworthy comparison cannot run.",
    selectAtLeastTwo: "Select 2–6 available exact profiles.",
    run: "Run bounded comparison",
    running: "Running bounded comparison…",
    cancel: "Cancel comparison",
    retry: "Retry comparison",
    newComparison: "Run a new root comparison with this selection",
    committing: "The comparison cannot be cancelled after publication begins.",
    localOnly: "Local only; frames do not leave this machine.",
    egress: "Frames leave this machine for",
    egressUnknown:
      "Frame egress is not established; this finding remains unavailable until access and consent are explicit.",
    registryIdentity: "Model identity",
    sourceIdentity: "Source / acquisition",
    access: "Access requirement",
    profileIdentity: "Profile identity",
    availability: "Availability",
    lifecycle: "Lifecycle",
    probeOnly: "Probe only; not eligible for trial acceptance.",
    trialEligible: "Trial eligible",
    sourceQualified: "Source-segment qualified",
    cameraQualified: "Camera qualified",
    licenses: "Licenses",
    dataset: "Dataset",
    model: "Model",
    runtime: "Runtime",
    deployment: "Deployment",
    runtimeVersion: "Installed runtime version",
    weightsSha: "Weights SHA-256",
    manifestSha: "Manifest SHA-256",
    profileSha: "Profile SHA-256",
    confidence: "Confidence",
    tile: "Tile",
    evidenceProfileSha: "Evidence-bound profile SHA-256",
    notAcquired: "Not acquired",
    selected: "Selected",
    recommended: "Recommended",
    sourceFrame: (frame: number) => `Source frame ${frame}`,
    sourceSha: "Source frame SHA-256",
    sourceDimensions: "Source dimensions",
    rawBoxes: "Retained top-K candidate boxes (source px: x, y, w, h)",
    rawOverlay: (profileId: string, frame: number) =>
      `Raw detector overlay for ${profileId} on frame ${frame}`,
    rawOverlaySha: "Raw overlay SHA-256",
    bytes: "bytes",
    noRawBoxes: "No retained candidate boxes",
    displayCandidate: "Display candidate",
    noDisplayCandidate: "No display candidate",
    latency: "Latency",
    candidates: "Candidates",
    filterReasons: "Filter reasons",
    noFilterReasons: "No filter reasons recorded",
    progress: "Detector probe progress",
    status: "Status",
    jobIdentity: "Job identity",
    requestSha: "Request SHA-256",
    parentTrial: "Parent trial",
    requestedFrames: "Same-frame request",
    failure: "Probe comparison did not complete",
    noRetainedCandidates:
      "No selected profile produced retained candidate boxes in this bounded comparison.",
    candidateCaveat:
      "After every evidence image is verified, any displayed candidate boxes are still unverified detector output, not confirmation that the football was found. T3 annotation determines correctness.",
    t3Next:
      "Next, start the 20–50-frame feasibility check: point to or box the ball across near, medium, far, and varied-light frames before deciding whether to train a camera-adapted detector.",
    evidenceMissing:
      "The ready report is missing complete, successfully executed same-frame evidence and cannot be treated as a trustworthy comparison. Retry explicitly to create a child job.",
    evidenceImagesLoading:
      "Verifying that every source frame and overlay can be loaded. No zero/nonzero conclusion is shown until this completes.",
    evidenceImagesFailed:
      "At least one evidence image could not be loaded, so this result cannot support a trustworthy conclusion. Retry explicitly.",
    serverFrames:
      "The server selects exploratory frames from the parent trial's frozen tracking contract; every profile is compared on that exact same frame set.",
    discardRecovery: "Discard invalid local recovery pointer",
    discardRecoveryHelp:
      "This does not cancel a backend job; it only clears the recovery pointer that this browser cannot verify.",
    refreshRecovery: "Refresh current job",
    refreshRecoveryHelp:
      "The current job pointer is retained. No backend job will be started or replaced until refresh succeeds.",
    reloadCatalog: "Reload model registry",
    retryCreate: "Retry the exact create request",
    previousFrame: "Previous frame",
    nextFrame: "Next frame",
    framePosition: (current: number, total: number) =>
      `Evidence frame ${current} of ${total}`,
    resultManifestSha: "Result manifest SHA-256",
  };
}

function evidenceIsComplete(job: DetectorProbeJobView) {
  if (job.status !== "ready" || job.frames.length === 0) return false;
  const required = new Set(job.selectedProfileIds);
  if (required.size < 2 || required.size !== job.selectedProfileIds.length)
    return false;
  const frameIndices = new Set<number>();
  return job.frames.every((frame) => {
    if (
      frameIndices.has(frame.frameIndex) ||
      !frame.sourceImageUrl ||
      !frame.sourceSha256 ||
      frame.sourceSizeBytes <= 0 ||
      frame.sourceWidth <= 0 ||
      frame.sourceHeight <= 0
    )
      return false;
    frameIndices.add(frame.frameIndex);
    const observed = new Set(
      frame.profiles.map((profile) => profile.profileId),
    );
    return (
      observed.size === frame.profiles.length &&
      required.size === observed.size &&
      [...required].every((profileId) => observed.has(profileId)) &&
      frame.profiles.every(
        (profile) =>
          profile.status === "completed" &&
          /^[0-9a-f]{64}$/.test(profile.profileSha256) &&
          profile.overlayImageUrl &&
          profile.overlaySha256 &&
          profile.overlaySizeBytes > 0,
      ) &&
      frame.mediaIntegrityClean
    );
  });
}

export function ProductionDetectorProbePanel({
  models,
  catalogState,
  catalogError,
  operationError,
  recoveryError,
  recoveryErrorKind,
  job,
  mutationPending,
  actionsBlocked = false,
  exactCreatePending = false,
  onStart,
  onCancel,
  onRetry,
  onDiscardRecovery,
  onRefreshRecovery,
  onReloadCatalog,
  onRetryCreate,
}: ProductionDetectorProbePanelProps) {
  const { language } = useLanguage();
  const labels = detectorProbeLabels(language);
  const catalogIdentity = useMemo(() => profileIdentity(models), [models]);
  const selectableProfileIds = useMemo(
    () =>
      new Set(
        models
          .filter((model) => model.availability === "available")
          .flatMap((model) => model.profiles)
          .filter((profile) => profile.probeSelectable)
          .map((profile) => profile.profileId),
      ),
    [models],
  );
  const [selectedProfileIds, setSelectedProfileIds] = useState(() =>
    initialProfileIds(models),
  );
  const jobSelectionIdentity = job
    ? `${job.jobId}:${job.selectedProfileIds.join("|")}`
    : "";
  const evidenceJobIdentity = `${job?.jobId ?? "none"}:${job?.resultManifestSha256 ?? "none"}`;
  const [activeFrameOffset, setActiveFrameOffset] = useState(0);
  const activeFrameIndex = Math.min(
    activeFrameOffset,
    Math.max(0, (job?.frames.length ?? 1) - 1),
  );
  const activeFrame = job?.frames[activeFrameIndex] ?? null;
  const evidenceArtifactUrls = useMemo(
    () =>
      job?.status === "ready" && activeFrame
        ? [
            activeFrame.sourceImageUrl,
            ...activeFrame.profiles.map((profile) => profile.overlayImageUrl),
          ]
        : [],
    [activeFrame, job?.status],
  );
  const [loadedArtifactUrls, setLoadedArtifactUrls] = useState<Set<string>>(
    () => new Set(),
  );
  const [failedArtifactUrls, setFailedArtifactUrls] = useState<Set<string>>(
    () => new Set(),
  );

  useEffect(() => {
    setActiveFrameOffset(0);
    setLoadedArtifactUrls(new Set());
    setFailedArtifactUrls(new Set());
  }, [evidenceJobIdentity]);

  useEffect(() => {
    setSelectedProfileIds((current) => {
      const stillAvailable = current.filter((profileId) =>
        selectableProfileIds.has(profileId),
      );
      return stillAvailable.length > 0
        ? stillAvailable
        : initialProfileIds(models);
    });
  }, [catalogIdentity, models, selectableProfileIds]);

  useEffect(() => {
    if (!job || job.selectedProfileIds.length < 2) return;
    const availableJobProfiles = job.selectedProfileIds.filter((profileId) =>
      selectableProfileIds.has(profileId),
    );
    if (availableJobProfiles.length === job.selectedProfileIds.length) {
      setSelectedProfileIds(availableJobProfiles);
    }
  }, [jobSelectionIdentity, selectableProfileIds]);

  const active =
    job?.status === "queued" ||
    job?.status === "running" ||
    job?.status === "committing";
  const selectionValid =
    new Set(selectedProfileIds).size >= 2 &&
    new Set(selectedProfileIds).size <= 6 &&
    selectedProfileIds.every((profileId) =>
      selectableProfileIds.has(profileId),
    );
  const readyEvidenceComplete = job ? evidenceIsComplete(job) : false;
  const evidenceImagesFailed = evidenceArtifactUrls.some((url) =>
    failedArtifactUrls.has(url),
  );
  const evidenceImagesLoaded =
    evidenceArtifactUrls.length > 0 &&
    evidenceArtifactUrls.every((url) => loadedArtifactUrls.has(url)) &&
    !evidenceImagesFailed;
  const trustworthyReadyEvidence =
    readyEvidenceComplete && evidenceImagesLoaded;
  const sameSelectionAsJob = Boolean(
    job &&
    selectedProfileIds.length === job.selectedProfileIds.length &&
    selectedProfileIds.every((profileId) =>
      job.selectedProfileIds.includes(profileId),
    ),
  );
  const showStart =
    !job ||
    (job.status === "ready" &&
      !job.noProfilesProducedCandidates &&
      !sameSelectionAsJob);

  function toggleProfile(profileId: string, checked: boolean) {
    setSelectedProfileIds((current) =>
      checked
        ? [...new Set([...current, profileId])]
        : current.filter((candidate) => candidate !== profileId),
    );
  }

  function markArtifactLoaded(url: string) {
    setFailedArtifactUrls((current) => {
      if (!current.has(url)) return current;
      const next = new Set(current);
      next.delete(url);
      return next;
    });
    setLoadedArtifactUrls((current) => {
      if (current.has(url)) return current;
      return new Set(current).add(url);
    });
  }

  function markArtifactFailed(url: string) {
    setLoadedArtifactUrls((current) => {
      if (!current.has(url)) return current;
      const next = new Set(current);
      next.delete(url);
      return next;
    });
    setFailedArtifactUrls((current) => {
      if (current.has(url)) return current;
      return new Set(current).add(url);
    });
  }

  function repeatTerminalComparison() {
    if (!job) return;
    if (sameSelectionAsJob) {
      onRetry({
        retryFromJobId: job.jobId,
        profileIds: job.selectedProfileIds,
      });
      return;
    }
    onStart(selectedProfileIds);
  }

  return (
    <Card
      className="min-w-0 w-full"
      data-testid="production-detector-probe-panel"
      aria-labelledby="production-detector-probe-title"
    >
      <CardHeader>
        <CardTitle className="text-base">
          <h3 id="production-detector-probe-title">{labels.title}</h3>
        </CardTitle>
        <CardDescription>{labels.description}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0 space-y-5">
        <div role="status" aria-live="polite" aria-atomic="true">
          {catalogState === "loading" && (
            <p className="text-sm">
              <Loader2
                className="mr-2 inline h-4 w-4 animate-spin"
                aria-hidden="true"
              />
              {labels.loading}
            </p>
          )}
          {job && (
            <p className="text-sm">
              {labels.status}: <span className="font-mono">{job.status}</span>
              {job.stage ? ` · ${job.stage}` : ""}
            </p>
          )}
        </div>

        {catalogState === "failed" && (
          <Alert variant="destructive">
            <AlertTitle>{labels.unavailable}</AlertTitle>
            <AlertDescription className="space-y-3">
              {catalogError && <p>{catalogError}</p>}
              {onReloadCatalog && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={onReloadCatalog}
                >
                  {labels.reloadCatalog}
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        {operationError && (
          <Alert variant="destructive">
            <AlertTitle>{labels.failure}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{operationError}</p>
              {onRetryCreate && (
                <Button type="button" variant="outline" onClick={onRetryCreate}>
                  {labels.retryCreate}
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        {recoveryError && (
          <Alert variant="destructive">
            <AlertTitle>{labels.failure}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{recoveryError}</p>
              {recoveryErrorKind === "invalid_pointer" && onDiscardRecovery ? (
                <>
                  <p>{labels.discardRecoveryHelp}</p>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={onDiscardRecovery}
                  >
                    {labels.discardRecovery}
                  </Button>
                </>
              ) : (
                onRefreshRecovery && (
                  <>
                    <p>{labels.refreshRecoveryHelp}</p>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={onRefreshRecovery}
                    >
                      {labels.refreshRecovery}
                    </Button>
                  </>
                )
              )}
            </AlertDescription>
          </Alert>
        )}

        {catalogState === "ready" && !job && (
          <p className="text-xs text-muted-foreground">{labels.serverFrames}</p>
        )}

        {catalogState === "ready" && (
          <div className="grid min-w-0 gap-4 xl:grid-cols-2">
            {models.map((model) => (
              <article
                key={`${model.kind}:${model.modelId}:${model.version}`}
                className="min-w-0 space-y-3 rounded-lg border p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h4 className="font-semibold">{model.displayName}</h4>
                    <p className="break-all font-mono text-xs text-muted-foreground">
                      {model.modelId} · {model.version} ·{" "}
                      {model.architectureFamily}
                    </p>
                  </div>
                  <Badge
                    variant={
                      model.availability === "available"
                        ? "secondary"
                        : "destructive"
                    }
                  >
                    {model.availability}
                  </Badge>
                </div>

                <dl className="grid min-w-0 gap-2 text-xs sm:grid-cols-2">
                  <div className="min-w-0 rounded-md bg-muted p-2">
                    <dt>{labels.lifecycle}</dt>
                    <dd className="break-all font-mono">{model.lifecycle}</dd>
                  </div>
                  <div className="min-w-0 rounded-md bg-muted p-2">
                    <dt>{labels.availability}</dt>
                    <dd className="break-words font-mono">
                      {model.availability}
                    </dd>
                  </div>
                  <div className="min-w-0 rounded-md bg-muted p-2 sm:col-span-2">
                    <dt>{labels.weightsSha}</dt>
                    <dd className="break-all font-mono">
                      {model.weightsSha256 ?? labels.notAcquired}
                    </dd>
                  </div>
                  <div className="min-w-0 rounded-md bg-muted p-2 sm:col-span-2">
                    <dt>{labels.manifestSha}</dt>
                    <dd className="break-all font-mono">
                      {model.manifestSha256 ?? labels.notAcquired}
                    </dd>
                  </div>
                </dl>

                <dl className="space-y-1 text-xs">
                  <div>
                    <dt>{labels.sourceIdentity}</dt>
                    <dd className="break-words font-mono">
                      {model.sourceProject} · {model.sourceVersion} ·{" "}
                      {model.acquisitionMethod}
                    </dd>
                  </div>
                  <div>
                    <dt>{labels.access}</dt>
                    <dd className="break-words">{model.accessRequirement}</dd>
                  </div>
                </dl>

                {!model.trialEligible && (
                  <p className="text-xs font-medium text-amber-700 dark:text-amber-300">
                    {labels.probeOnly}
                  </p>
                )}
                <dl className="grid gap-1 text-xs sm:grid-cols-3">
                  <div>
                    <dt>{labels.trialEligible}</dt>
                    <dd className="font-mono">{String(model.trialEligible)}</dd>
                  </div>
                  <div>
                    <dt>{labels.sourceQualified}</dt>
                    <dd className="font-mono">
                      {String(model.sourceSegmentQualified)}
                    </dd>
                  </div>
                  <div>
                    <dt>{labels.cameraQualified}</dt>
                    <dd className="font-mono">
                      {String(model.cameraQualified)}
                    </dd>
                  </div>
                </dl>

                <div className="space-y-1 text-xs">
                  <p className="font-medium">{labels.licenses}</p>
                  <p>
                    {labels.dataset}: {model.datasetLicense}
                  </p>
                  <p>
                    {labels.model}: {model.modelLicense}
                  </p>
                  <p>
                    {labels.runtime}: {model.runtimeLicense}
                  </p>
                  <p>
                    {labels.deployment}: {model.deploymentLicense}
                  </p>
                  <p>
                    {labels.runtimeVersion}: {model.runtimeVersion}
                  </p>
                </div>

                <div className="text-xs">
                  {model.egress.leavesDevice === true ? (
                    <p>
                      {labels.egress}: {model.egress.destination ?? "—"} ·{" "}
                      {model.egress.consent}
                    </p>
                  ) : model.egress.leavesDevice === false ? (
                    <p>{labels.localOnly}</p>
                  ) : (
                    <p>
                      {labels.egressUnknown} · {model.egress.consent}
                    </p>
                  )}
                </div>

                {model.availabilityReason && (
                  <p className="text-xs text-destructive">
                    {model.availabilityReason}
                  </p>
                )}

                <div className="space-y-2">
                  {model.profiles.map((profile) => {
                    const inputId = `detector-profile-${profile.profileId}`;
                    const enabled =
                      model.availability === "available" &&
                      profile.probeSelectable;
                    return (
                      <div
                        key={profile.profileId}
                        className="flex min-w-0 items-start gap-3 rounded-md border p-3"
                      >
                        <Checkbox
                          id={inputId}
                          checked={selectedProfileIds.includes(
                            profile.profileId,
                          )}
                          disabled={
                            !enabled ||
                            Boolean(active) ||
                            mutationPending ||
                            actionsBlocked ||
                            (!selectedProfileIds.includes(profile.profileId) &&
                              selectedProfileIds.length >= 6)
                          }
                          onCheckedChange={(checked) =>
                            toggleProfile(profile.profileId, checked === true)
                          }
                        />
                        <Label
                          htmlFor={inputId}
                          className="min-w-0 cursor-pointer space-y-1"
                        >
                          <span className="block break-all font-mono text-xs">
                            {profile.profileId} · {profile.version}
                          </span>
                          <span className="block text-xs font-normal text-muted-foreground">
                            {profile.mode.toUpperCase()} · {profile.inputSize}px
                            · {labels.confidence} {profile.confidenceThreshold}{" "}
                            · top {profile.topK}
                          </span>
                          {profile.tile && (
                            <span className="block text-xs font-normal text-muted-foreground">
                              {labels.tile} {profile.tile.width} ×{" "}
                              {profile.tile.height} · overlap{" "}
                              {profile.tile.overlapWidthRatio} ×{" "}
                              {profile.tile.overlapHeightRatio}
                            </span>
                          )}
                          <span className="block break-all font-mono text-[11px] font-normal text-muted-foreground">
                            {labels.profileSha}: {profile.digest}
                          </span>
                          {profile.recommended && (
                            <Badge variant="outline">
                              {labels.recommended}
                            </Badge>
                          )}
                          {profile.unavailableReason && (
                            <span className="block text-xs font-normal text-destructive">
                              {profile.unavailableReason}
                            </span>
                          )}
                        </Label>
                      </div>
                    );
                  })}
                </div>
              </article>
            ))}
          </div>
        )}

        {catalogState === "ready" && !selectionValid && !active && (
          <Alert variant="destructive">
            <AlertDescription>{labels.selectAtLeastTwo}</AlertDescription>
          </Alert>
        )}

        {job && (
          <dl className="grid min-w-0 gap-2 text-xs sm:grid-cols-2">
            <div className="min-w-0 rounded-md border p-2">
              <dt>{labels.jobIdentity}</dt>
              <dd className="break-all font-mono">{job.jobId}</dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{labels.requestSha}</dt>
              <dd className="break-all font-mono">{job.requestSha256}</dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{labels.parentTrial}</dt>
              <dd className="break-all font-mono">{job.parentTrialId}</dd>
            </div>
            <div className="min-w-0 rounded-md border p-2">
              <dt>{labels.requestedFrames}</dt>
              <dd className="break-words font-mono">
                {job.frameIndices.join(", ")}
              </dd>
            </div>
            {job.resultManifestSha256 && (
              <div className="min-w-0 rounded-md border p-2 sm:col-span-2">
                <dt>{labels.resultManifestSha}</dt>
                <dd className="break-all font-mono">
                  {job.resultManifestSha256}
                </dd>
              </div>
            )}
          </dl>
        )}

        {active && job && (
          <div className="space-y-3">
            <Progress
              value={clampProgress(job.progressPercent)}
              aria-label={labels.progress}
              aria-valuenow={clampProgress(job.progressPercent)}
            />
            {job.status === "committing" && (
              <p className="text-xs text-muted-foreground">
                {labels.committing}
              </p>
            )}
            <Button
              type="button"
              variant="outline"
              onClick={() => onCancel(job.jobId)}
              disabled={
                job.status === "committing" ||
                mutationPending ||
                exactCreatePending
              }
            >
              <Square className="mr-2 h-4 w-4" aria-hidden="true" />
              {labels.cancel}
            </Button>
          </div>
        )}

        {job && ["failed", "blocked", "cancelled"].includes(job.status) && (
          <Alert variant="destructive">
            <AlertTitle>{labels.failure}</AlertTitle>
            <AlertDescription className="space-y-3">
              {job.failureCode && (
                <p className="font-mono">{job.failureCode}</p>
              )}
              {job.recoveryAction && <p>{job.recoveryAction}</p>}
              <Button
                type="button"
                variant="outline"
                onClick={repeatTerminalComparison}
                disabled={mutationPending || !selectionValid || actionsBlocked}
              >
                {sameSelectionAsJob ? (
                  <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
                ) : (
                  <Play className="mr-2 h-4 w-4" aria-hidden="true" />
                )}
                {sameSelectionAsJob ? labels.retry : labels.newComparison}
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {job?.status === "ready" && !readyEvidenceComplete && (
          <Alert variant="destructive">
            <AlertDescription className="space-y-3">
              <p>{labels.evidenceMissing}</p>
              <Button
                type="button"
                variant="outline"
                onClick={repeatTerminalComparison}
                disabled={mutationPending || !selectionValid || actionsBlocked}
              >
                {sameSelectionAsJob ? (
                  <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
                ) : (
                  <Play className="mr-2 h-4 w-4" aria-hidden="true" />
                )}
                {sameSelectionAsJob ? labels.retry : labels.newComparison}
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {job?.status === "ready" &&
          readyEvidenceComplete &&
          !evidenceImagesLoaded && (
            <Alert variant={evidenceImagesFailed ? "destructive" : "default"}>
              <AlertDescription className="space-y-3">
                <p>
                  {evidenceImagesFailed
                    ? labels.evidenceImagesFailed
                    : labels.evidenceImagesLoading}
                </p>
                {evidenceImagesFailed && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={repeatTerminalComparison}
                    disabled={
                      mutationPending || !selectionValid || actionsBlocked
                    }
                  >
                    {sameSelectionAsJob ? (
                      <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Play className="mr-2 h-4 w-4" aria-hidden="true" />
                    )}
                    {sameSelectionAsJob ? labels.retry : labels.newComparison}
                  </Button>
                )}
              </AlertDescription>
            </Alert>
          )}

        {job?.status === "ready" && readyEvidenceComplete && (
          <p className="rounded-lg border bg-muted px-4 py-3 text-sm">
            {labels.candidateCaveat}
          </p>
        )}

        {job?.status === "ready" && readyEvidenceComplete && activeFrame && (
          <div className="min-w-0 space-y-5">
            <nav
              className="flex flex-wrap items-center justify-between gap-3"
              aria-label={labels.framePosition(
                activeFrameIndex + 1,
                job.frames.length,
              )}
            >
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  setActiveFrameOffset((current) => Math.max(0, current - 1))
                }
                disabled={activeFrameIndex === 0}
              >
                {labels.previousFrame}
              </Button>
              <p
                role="status"
                aria-live="polite"
                className="text-sm font-medium"
              >
                {labels.framePosition(activeFrameIndex + 1, job.frames.length)}
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  setActiveFrameOffset((current) =>
                    Math.min(job.frames.length - 1, current + 1),
                  )
                }
                disabled={activeFrameIndex === job.frames.length - 1}
              >
                {labels.nextFrame}
              </Button>
            </nav>
            {[activeFrame].map((frame) => (
              <section
                key={frame.frameIndex}
                data-testid={`detector-probe-frame-${frame.frameIndex}`}
                className="min-w-0 space-y-3 rounded-lg border p-3"
                aria-labelledby={`detector-probe-frame-title-${frame.frameIndex}`}
              >
                <h4
                  id={`detector-probe-frame-title-${frame.frameIndex}`}
                  className="font-semibold"
                >
                  {labels.sourceFrame(frame.frameIndex)}
                </h4>
                <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
                  <div className="min-w-0 space-y-2">
                    <img
                      src={frame.sourceImageUrl}
                      alt={labels.sourceFrame(frame.frameIndex)}
                      loading="eager"
                      decoding="async"
                      aria-hidden={!trustworthyReadyEvidence}
                      style={
                        trustworthyReadyEvidence
                          ? undefined
                          : { visibility: "hidden" }
                      }
                      onLoad={() => markArtifactLoaded(frame.sourceImageUrl)}
                      onError={() => markArtifactFailed(frame.sourceImageUrl)}
                      className={
                        trustworthyReadyEvidence
                          ? "h-auto w-full rounded-md border object-contain"
                          : "pointer-events-none h-px w-px opacity-0"
                      }
                    />
                    <p className="text-xs">{labels.sourceSha}</p>
                    <p className="break-all font-mono text-xs">
                      {frame.sourceSha256}
                    </p>
                    <p className="font-mono text-xs">
                      {frame.sourceSizeBytes} {labels.bytes}
                    </p>
                    <p className="font-mono text-xs">
                      {labels.sourceDimensions}: {frame.sourceWidth} ×{" "}
                      {frame.sourceHeight}
                    </p>
                  </div>
                  <div className="grid min-w-0 gap-3 md:grid-cols-2">
                    {frame.profiles.map((profile) => (
                      <article
                        key={profile.profileId}
                        className="min-w-0 space-y-3 rounded-md bg-muted p-3 text-xs"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <h5 className="break-all font-mono font-semibold">
                            {profile.profileId}
                          </h5>
                          <Badge variant="outline">{profile.status}</Badge>
                        </div>
                        <div className="min-w-0">
                          <p>{labels.evidenceProfileSha}</p>
                          <p className="break-all font-mono">
                            {profile.profileSha256}
                          </p>
                        </div>
                        <div className="min-w-0 space-y-1">
                          <img
                            src={profile.overlayImageUrl}
                            alt={labels.rawOverlay(
                              profile.profileId,
                              frame.frameIndex,
                            )}
                            loading="eager"
                            decoding="async"
                            aria-hidden={!trustworthyReadyEvidence}
                            style={
                              trustworthyReadyEvidence
                                ? undefined
                                : { visibility: "hidden" }
                            }
                            onLoad={() =>
                              markArtifactLoaded(profile.overlayImageUrl)
                            }
                            onError={() =>
                              markArtifactFailed(profile.overlayImageUrl)
                            }
                            className={
                              trustworthyReadyEvidence
                                ? "h-auto w-full rounded-md border object-contain"
                                : "pointer-events-none h-px w-px opacity-0"
                            }
                          />
                          <p>{labels.rawOverlaySha}</p>
                          <p className="break-all font-mono">
                            {profile.overlaySha256}
                          </p>
                          <p className="font-mono">
                            {profile.overlaySizeBytes} {labels.bytes}
                          </p>
                        </div>
                        {trustworthyReadyEvidence && (
                          <>
                            <div>
                              <p className="font-medium">{labels.rawBoxes}</p>
                              {profile.rawBoxes.length === 0 ? (
                                <p>{labels.noRawBoxes}</p>
                              ) : (
                                <ol className="space-y-1">
                                  {profile.rawBoxes.map((box, index) => (
                                    <li
                                      key={`${index}:${formatBox(box)}`}
                                      className="break-words font-mono"
                                    >
                                      Retained {index + 1}: {formatBox(box)}
                                    </li>
                                  ))}
                                </ol>
                              )}
                            </div>
                            <div>
                              <p className="font-medium">
                                {labels.displayCandidate}
                              </p>
                              <p className="break-words font-mono">
                                {profile.displayCandidate
                                  ? formatBox(profile.displayCandidate)
                                  : labels.noDisplayCandidate}
                              </p>
                            </div>
                            <dl className="grid gap-2 sm:grid-cols-2">
                              <div>
                                <dt>{labels.latency}</dt>
                                <dd className="font-mono">
                                  {profile.latencyMs === null
                                    ? "—"
                                    : `${profile.latencyMs.toFixed(1)} ms`}
                                </dd>
                              </div>
                              <div>
                                <dt>{labels.candidates}</dt>
                                <dd className="font-mono">
                                  {profile.candidateCount} / top {profile.topK}
                                </dd>
                              </div>
                            </dl>
                            <div>
                              <p className="font-medium">
                                {labels.filterReasons}
                              </p>
                              {Object.keys(profile.filterReasons).length ===
                              0 ? (
                                <p>{labels.noFilterReasons}</p>
                              ) : (
                                <ul className="space-y-1 font-mono">
                                  {Object.entries(profile.filterReasons).map(
                                    ([reason, count]) => (
                                      <li key={reason}>
                                        {reason}: {count}
                                      </li>
                                    ),
                                  )}
                                </ul>
                              )}
                            </div>
                            {profile.failureCode && (
                              <p className="font-mono text-destructive">
                                {profile.failureCode}
                              </p>
                            )}
                          </>
                        )}
                      </article>
                    ))}
                  </div>
                </div>
              </section>
            ))}
          </div>
        )}

        {job?.status === "ready" &&
          job.noProfilesProducedCandidates &&
          trustworthyReadyEvidence && (
            <Alert variant="destructive">
              <AlertTitle>{labels.noRetainedCandidates}</AlertTitle>
              <AlertDescription className="space-y-3">
                <p>{labels.t3Next}</p>
                <Button
                  type="button"
                  variant="outline"
                  onClick={repeatTerminalComparison}
                  disabled={
                    mutationPending || !selectionValid || actionsBlocked
                  }
                >
                  {sameSelectionAsJob ? (
                    <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
                  ) : (
                    <Play className="mr-2 h-4 w-4" aria-hidden="true" />
                  )}
                  {sameSelectionAsJob ? labels.retry : labels.newComparison}
                </Button>
              </AlertDescription>
            </Alert>
          )}

        {catalogState === "ready" && !active && showStart && (
          <Button
            type="button"
            onClick={() => onStart(selectedProfileIds)}
            disabled={!selectionValid || mutationPending || actionsBlocked}
          >
            {mutationPending ? (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            ) : (
              <Play className="mr-2 h-4 w-4" aria-hidden="true" />
            )}
            {mutationPending ? labels.running : labels.run}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
