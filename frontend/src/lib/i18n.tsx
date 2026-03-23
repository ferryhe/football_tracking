import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";

const LANGUAGE_STORAGE_KEY = "football-tracking-language";

export type LanguageCode = "en" | "zh";

const enCopy = {
  common: {
    english: "EN",
    chinese: "中",
    loading: "Loading...",
    offline: "Offline",
    backendOk: "Backend OK",
    idle: "Idle",
    waiting: "Waiting",
    unavailable: "Unavailable",
    notAvailable: "n/a",
    noneSelected: "None selected",
    chooseOne: "Choose one",
    selected: "Selected",
    baseline: "Baseline",
    noActiveRun: "No active run",
    stillRunning: "Still running or queued",
    refreshHint: "Background refresh stays on automatically.",
  },
  header: {
    eyebrow: "AI workflow",
    title: "Football Tracking Operator",
    subtitle: "Run one baseline, then let AI explain evidence and tune the next config.",
    activeTask: "Current task",
    inputRoot: "Input root",
    language: "Language",
    tileInput: "Input",
    tileBaseline: "Baseline",
    tileEvidence: "Evidence",
    tileAi: "AI",
    tileInputEmpty: "Pick a clip",
    tileBaselineEmpty: "Pick a config",
    tileEvidenceEmpty: "Run once",
    tileAiLocked: "Need evidence",
    tileAiReady: "Ready",
  },
  workspace: {
    flowChooseTitle: "Choose clip + config",
    flowChooseDetail: "Pick the source video and the baseline YAML.",
    flowRunTitle: "Run Baseline",
    flowRunDetail: "Create the first evidence bundle.",
    flowAiTitle: "AI Analysis",
    flowAiDetail: "Explain issues and suggest the next config.",
    flowReviewTitle: "Review outputs",
    flowReviewDetail: "Check results and decide whether to loop again.",
    selectEyebrow: "Step 1",
    selectTitle: "Choose the clip and run one baseline",
    selectSubtitle: "Only the first run is manual. Later tuning should come from AI.",
    inputTitle: "Input clip",
    inputSubtitle: "Pick the source video for the next baseline run.",
    fieldSetupTitle: "Field setup",
    fieldSetupSubtitle:
      "Capture one preview frame first. After that, either load the current config shape or ask AI for a suggestion.",
    fieldCapture: "Capture preview",
    fieldLoadConfig: "Read config",
    fieldGenerate: "AI suggest",
    fieldGenerating: "Generating...",
    fieldClear: "Clear",
    fieldAccept: "Accept",
    fieldAccepted: "Accepted for the next baseline run.",
    fieldPreviewAlt: "Field setup preview",
    fieldFrame: "Preview frame",
    fieldFieldBox: "Field area",
    fieldExpandedBox: "Expanded area",
    fieldConfidenceConfig: "Loaded from the selected YAML",
    fieldConfidenceDetected: "Detected from the sampled frame",
    fieldConfidenceFallback: "Using the fallback trapezoid",
    fieldAdjustTitle: "Quick adjust",
    fieldAdjustTighter: "Tighter",
    fieldAdjustWider: "Wider",
    fieldAdjustRaise: "Raise top",
    fieldAdjustLower: "Lower top",
    fieldAdjustGapIn: "Closer buffer",
    fieldAdjustGapOut: "Wider buffer",
    fieldPolygonInput: "Field points",
    fieldExpandedInput: "Expanded points",
    fieldFieldTooltip: "Field is the main playable area used as the core ground zone.",
    fieldExpandedTooltip: "Expanded is the outer buffer used for ROI and recovery near the field edge.",
    fieldDetailsTitle: "Details and manual points",
    fieldInputHint: "Edit points as `x,y | x,y | ...`. Press Enter to apply.",
    fieldInputError: "Point input is invalid. Use at least 4 `x,y` pairs.",
    fieldApplyHint: "Accept this shape if it matches the playable field.",
    fieldEmptyBody: "Capture one preview frame first, then choose config or AI.",
    fieldPreviewReady: "Preview frame is fixed.",
    fieldOverlayReady: "Overlay is shown on this fixed frame.",
    fieldPreviewCycleHint: "If this frame is not good, click Capture preview again to switch to another sampled moment.",
    fieldNoOverlay: "No overlay yet",
    fieldAwaitingSource: "Waiting",
    fieldChooseSourceHint: "Choose `Read config` or `AI suggest` after the preview frame looks right.",
    fieldPreviewReadyMessage: "Preview frame captured. Next choose config or AI.",
    fieldReadyMessage: "Field suggestion is ready. Accept it to use it in the next baseline run.",
    fieldLoadedFromConfig: "Loaded field setup from the selected YAML.",
    fieldAcceptedMessage: "Field setup accepted. The next baseline run will apply it.",
    fieldConfigMissing: "The selected YAML does not contain field setup.",
    inputDirectory: "Input folder",
    noInputTitle: "No clips found",
    noInputBody: "Put a supported video into `data/`, then refresh the workspace.",
    baselineTitle: "Baseline config",
    baselineSubtitle: "Pick the starting YAML. After the preview is fixed, you can load its field shape here.",
    baselineSummaryTitle: "Selected config summary",
    baselineDefaultHint: "This clip has no history yet, so a first-run or default config is the safest start.",
    baselineReuseHint: "This clip already has history, so reusing a recent config is a reasonable baseline.",
    scopeLabel: "Scope",
    scopeFull: "Full",
    scopePartial: "Partial",
    scopeStandard: "Standard",
    baselineLoopHint: "For the first loop, use a standard or partial config. Keep full configs for the final delivery pass.",
    noBaselineTitle: "No configs found",
    noBaselineBody: "If `real_first_run.yaml` or `default.yaml` exists it will be used. If `config/` is empty, baseline stays disabled.",
    selectedInput: "Selected clip",
    selectedBaseline: "Selected config",
    launchCopy: "Run one baseline first. After that, let AI drive the next config changes.",
    launchButton: "Start baseline",
    launchStarting: "Starting...",
    selectionDetails: "File and config details",
    selectionDetailsSubtitle: "Keep the main surface simple. Open this only when you need full paths and config context.",
    launchHint: "Manual edits should stop after the first run.",
    focusEyebrow: "Step 2",
    focusTitle: "Choose one related run for AI",
    focusSubtitle: "Only runs created from the current source clip appear here.",
    currentFocus: "Focused run",
    focusRun: "Focus run",
    aiLane: "AI role",
    aiReady: "AI can suggest the next config",
    aiWaiting: "Run one baseline first",
    detected: "Detected",
    lost: "Lost",
    artifacts: "Artifacts",
    lastEvent: "Last event",
    inputVideo: "Input video",
    outputDirectory: "Output",
    created: "Created",
    completed: "Completed",
    runNotes: "Notes",
    noFocusTitle: "No related run yet",
    noFocusBody: "Run the selected source clip once to unlock AI analysis here.",
    queueEyebrow: "History",
    queueTitle: "Conversion history",
    queueSubtitle: "Older runs stay here when you need to inspect them.",
    deliveryEyebrow: "Step 3",
    deliveryTitle: "History",
    deliverySubtitle: "All past runs across every source clip.",
    deliveryEmptyTitle: "No history yet",
    deliveryEmptyBody: "Finish one run first, then history will appear here.",
    deliveryRanAt: "Ran at",
    deliveryResultFolder: "Result folder",
    runningNow: "Running now",
    noRunsTitle: "No runs yet",
    noRunsBody: "Launch the first baseline above to start the loop.",
    evidenceEyebrow: "Outputs",
    evidenceTitle: "Output bundle",
    evidenceSubtitle: "Videos, reports, and files from the focused run.",
    featuredOutputs: "Featured outputs",
    featuredOutputsSubtitle: "Open the main delivery artifacts first.",
    allArtifacts: "All artifacts",
    allArtifactsSubtitle: "Everything exported for this run.",
    openArtifact: "Open",
    run: "Run",
    modulesEnabled: "Modules",
    artifactsReady: "Artifacts ready",
    outputFolder: "Output folder",
    followCamVideo: "Follow-cam",
    cleanedVideo: "Cleaned video",
    noEvidenceTitle: "No evidence selected",
    noEvidenceBody: "Start or select a run to show outputs here.",
    cleanup: "Cleanup",
    followCam: "Follow-cam",
  },
  ai: {
    eyebrow: "AI role",
    title: "AI suggests the next config",
    subtitle: "Explain one selected run, draft the next config patch, then launch a new task.",
    stageEvidence: "Evidence",
    stageExplain: "Explain",
    stageRecommend: "Recommend",
    stageDerive: "Derive",
    stageEvidenceNone: "Pick one run first.",
    stageExplainNone: "The explanation appears after you trigger it.",
    stageRecommendNone: "Ask AI to draft the next config.",
    stageDeriveNone: "The next config name appears here.",
    evidenceRun: "Evidence run",
    targetVideo: "Target clip",
    knownConfigs: "Configs",
    objective: "Objective",
    presetSteady: "Steady cam",
    presetRecover: "Recover ball",
    presetClean: "Cleaner result",
    buttonRecommend: "Recommend",
    buttonThinking: "Thinking...",
    buttonRun: "Start next task",
    activityExplain: "Requesting AI explanation...",
    activityRecommend: "Generating grounded suggestion...",
    activityRun: "Creating config and starting the next run...",
    latestDerived: "Latest derived config",
    readoutTitle: "AI readout",
    readoutSubtitle: "Summary, evidence count, and next-config readiness.",
    readoutFallback: "AI summary appears after you explain a run.",
    evidencePoints: "Evidence",
    patchLines: "Patch lines",
    nextConfig: "Next config",
    recommendation: "Recommendation",
    evidencePatch: "View AI evidence and patch",
    configDiff: "View config diff",
  },
  artifactKinds: {
    video: "Video",
    csv: "CSV",
    json: "JSON",
    jsonl: "JSONL",
    file: "File",
  },
};

type Copy = typeof enCopy;

const zhCopy: Copy = {
  common: {
    english: "EN",
    chinese: "中",
    loading: "加载中...",
    offline: "离线",
    backendOk: "后端正常",
    idle: "空闲",
    waiting: "等待中",
    unavailable: "不可用",
    notAvailable: "暂无",
    noneSelected: "未选择",
    chooseOne: "请选择",
    selected: "已选择",
    baseline: "基线",
    noActiveRun: "当前没有任务",
    stillRunning: "仍在运行或排队中",
    refreshHint: "后台会自动持续刷新。",
  },
  header: {
    eyebrow: "AI 工作流",
    title: "足球跟踪控制台",
    subtitle: "先跑一版基线，再让 AI 根据证据解释问题并建议下一版配置。",
    activeTask: "当前任务",
    inputRoot: "输入目录",
    language: "语言",
    tileInput: "输入",
    tileBaseline: "基线",
    tileEvidence: "证据",
    tileAi: "AI",
    tileInputEmpty: "先选视频",
    tileBaselineEmpty: "先选配置",
    tileEvidenceEmpty: "先跑一次",
    tileAiLocked: "需要证据",
    tileAiReady: "可用",
  },
  workspace: {
    flowChooseTitle: "选择视频和配置",
    flowChooseDetail: "选择源视频和基线 YAML。",
    flowRunTitle: "跑基线",
    flowRunDetail: "先产出第一版证据。",
    flowAiTitle: "AI分析",
    flowAiDetail: "解释问题并建议下一版配置。",
    flowReviewTitle: "查看结果",
    flowReviewDetail: "查看结果，决定是否继续迭代。",
    selectEyebrow: "第一步",
    selectTitle: "选择视频并先跑一版基线",
    selectSubtitle: "只有第一轮需要人工操作，后续调参尽量交给 AI。",
    inputTitle: "输入视频",
    inputSubtitle: "选择下一次基线运行要使用的源视频。",
    fieldSetupTitle: "球场设置",
    fieldSetupSubtitle: "先固定一张预览截图，再选择读取当前配置或让 AI 生成建议。",
    fieldCapture: "截图预览",
    fieldLoadConfig: "读取配置",
    fieldGenerate: "AI生成建议",
    fieldGenerating: "生成中...",
    fieldClear: "清除",
    fieldAccept: "接受",
    fieldAccepted: "已接受，将用于下一次基线运行。",
    fieldPreviewAlt: "球场设置预览",
    fieldFrame: "预览帧",
    fieldFieldBox: "球场区域",
    fieldExpandedBox: "扩展区域",
    fieldConfidenceConfig: "来自当前所选 YAML",
    fieldConfidenceDetected: "根据抽帧自动识别",
    fieldConfidenceFallback: "使用默认梯形建议",
    fieldAdjustTitle: "快速调整",
    fieldAdjustTighter: "收紧",
    fieldAdjustWider: "放宽",
    fieldAdjustRaise: "上沿提高",
    fieldAdjustLower: "上沿下压",
    fieldAdjustGapIn: "缩小缓冲",
    fieldAdjustGapOut: "放大缓冲",
    fieldPolygonInput: "球场点位",
    fieldExpandedInput: "扩展点位",
    fieldFieldTooltip: "Field 是主要可用球场区域，会作为核心 ground zone 使用。",
    fieldExpandedTooltip: "Expanded 是球场外侧的缓冲区，主要用于 ROI 和边缘重找。",
    fieldDetailsTitle: "详情和手动点位",
    fieldInputHint: "按 `x,y | x,y | ...` 输入点位，回车后立即应用。",
    fieldInputError: "点位输入无效，至少需要 4 组 `x,y`。",
    fieldApplyHint: "如果这个形状符合真实球场，就先点击接受。",
    fieldEmptyBody: "先固定一张预览截图，然后再选择读取配置或 AI 建议。",
    fieldPreviewReady: "预览截图已固定。",
    fieldOverlayReady: "叠加标记正在使用这张固定截图。",
    fieldPreviewCycleHint: "如果这张截图不合适，再点一次“截图预览”就会切到另一张代表帧。",
    fieldNoOverlay: "还没有标记",
    fieldAwaitingSource: "等待中",
    fieldChooseSourceHint: "确认截图没问题后，再点“读取配置”或“AI生成建议”。",
    fieldPreviewReadyMessage: "预览截图已生成，下一步请选择读取配置或 AI 建议。",
    fieldReadyMessage: "球场建议已生成，接受后会用于下一次基线运行。",
    fieldLoadedFromConfig: "已从当前 YAML 载入球场设置。",
    fieldAcceptedMessage: "球场设置已接受，下一次基线运行会带上它。",
    fieldConfigMissing: "当前 YAML 里没有球场设置。",
    inputDirectory: "输入目录",
    noInputTitle: "没有找到视频",
    noInputBody: "把支持的视频放进 `data/` 后再刷新。",
    baselineTitle: "基线配置",
    baselineSubtitle: "选择起始 YAML。截图固定后，可以再读取它里面的球场设置。",
    baselineSummaryTitle: "当前配置摘要",
    baselineDefaultHint: "这个视频还没有历史记录，所以 first-run 或 default 配置更稳妥。",
    baselineReuseHint: "这个视频已经有历史记录，可以复用最近的配置作为起点。",
    scopeLabel: "范围",
    scopeFull: "全量",
    scopePartial: "部分",
    scopeStandard: "标准",
    baselineLoopHint: "第一轮优先用标准或部分配置，全量配置更适合最终交付。",
    noBaselineTitle: "没有找到配置",
    noBaselineBody: "如果存在 `real_first_run.yaml` 或 `default.yaml` 会优先使用；如果 `config/` 为空，就无法启动基线。",
    selectedInput: "已选视频",
    selectedBaseline: "已选配置",
    launchCopy: "先跑一版基线，后续配置修改交给 AI。",
    launchButton: "开始跑基线",
    launchStarting: "启动中...",
    selectionDetails: "文件和配置详情",
    selectionDetailsSubtitle: "主界面先保持简洁，只有需要看完整路径和配置上下文时再展开。",
    launchHint: "第一版之后尽量不要再手改参数。",
    focusEyebrow: "第二步",
    focusTitle: "选择一个相关结果给 AI 分析",
    focusSubtitle: "这里仅显示与当前源视频关联的运行历史。",
    currentFocus: "当前焦点 run",
    focusRun: "焦点 run",
    aiLane: "AI 角色",
    aiReady: "AI 可以继续建议下一版配置",
    aiWaiting: "先跑出一版基线",
    detected: "检测到",
    lost: "丢失",
    artifacts: "交付物",
    lastEvent: "最近时间",
    inputVideo: "输入视频",
    outputDirectory: "输出目录",
    created: "创建时间",
    completed: "完成时间",
    runNotes: "备注",
    noFocusTitle: "还没有相关结果",
    noFocusBody: "先把当前视频跑出一版结果，这里才会出现可分析的 run。",
    queueEyebrow: "历史",
    queueTitle: "转换历史",
    queueSubtitle: "只有在需要回看旧结果时才使用这里。",
    deliveryEyebrow: "第三步",
    deliveryTitle: "历史",
    deliverySubtitle: "查看所有视频的过往运行结果。",
    deliveryEmptyTitle: "还没有历史记录",
    deliveryEmptyBody: "先完成一次运行，这里才会出现历史列表。",
    deliveryRanAt: "运行时间",
    deliveryResultFolder: "结果目录",
    runningNow: "正在运行",
    noRunsTitle: "还没有运行记录",
    noRunsBody: "先在上面启动第一版基线。",
    evidenceEyebrow: "输出",
    evidenceTitle: "结果包",
    evidenceSubtitle: "焦点 run 的视频、报告和文件都放在这里。",
    featuredOutputs: "重点产物",
    featuredOutputsSubtitle: "优先打开主要交付物。",
    allArtifacts: "全部文件",
    allArtifactsSubtitle: "这个 run 导出的所有文件。",
    openArtifact: "打开",
    run: "运行",
    modulesEnabled: "启用模块",
    artifactsReady: "产物数量",
    outputFolder: "输出文件夹",
    followCamVideo: "跟随镜头",
    cleanedVideo: "清洗后视频",
    noEvidenceTitle: "还没有选择证据",
    noEvidenceBody: "启动或选择一个 run 后，这里会显示输出内容。",
    cleanup: "清洗",
    followCam: "跟随镜头",
  },
  ai: {
    eyebrow: "AI 角色",
    title: "AI 建议下一版配置",
    subtitle: "先解释一个选中的 run，再生成补丁并启动下一次任务。",
    stageEvidence: "证据",
    stageExplain: "解释",
    stageRecommend: "建议",
    stageDerive: "生成",
    stageEvidenceNone: "先选一个 run。",
    stageExplainNone: "触发解释后会显示结果。",
    stageRecommendNone: "让 AI 生成下一版配置建议。",
    stageDeriveNone: "这里会显示下一版配置名。",
    evidenceRun: "证据 run",
    targetVideo: "目标视频",
    knownConfigs: "配置数",
    objective: "目标",
    presetSteady: "稳定镜头",
    presetRecover: "减少丢球",
    presetClean: "结果更干净",
    buttonRecommend: "生成建议",
    buttonThinking: "思考中...",
    buttonRun: "开始新任务",
    activityExplain: "正在请求 AI 解释...",
    activityRecommend: "正在生成基于证据的建议...",
    activityRun: "正在生成配置并启动下一次运行...",
    latestDerived: "最近生成配置",
    readoutTitle: "AI 读数",
    readoutSubtitle: "摘要、证据数量和下一版配置状态。",
    readoutFallback: "触发解释后，这里会显示 AI 摘要。",
    evidencePoints: "证据点",
    patchLines: "补丁行数",
    nextConfig: "下一版配置",
    recommendation: "建议结论",
    evidencePatch: "查看 AI 证据和补丁",
    configDiff: "查看配置 diff",
  },
  artifactKinds: {
    video: "视频",
    csv: "CSV",
    json: "JSON",
    jsonl: "JSONL",
    file: "文件",
  },
};

const COPY: Record<LanguageCode, Copy> = {
  en: enCopy,
  zh: zhCopy,
};

function normalizeLanguage(value: string | null | undefined): LanguageCode | null {
  if (!value) {
    return null;
  }
  if (value === "en" || value.startsWith("en-")) {
    return "en";
  }
  if (value === "zh" || value.startsWith("zh-")) {
    return "zh";
  }
  return null;
}

export function detectPreferredLanguage(): LanguageCode {
  if (typeof window === "undefined") {
    return "en";
  }
  const stored = normalizeLanguage(window.localStorage.getItem(LANGUAGE_STORAGE_KEY));
  if (stored) {
    return stored;
  }
  return normalizeLanguage(window.navigator.language) ?? "en";
}

function formatDateTimeForLocale(language: LanguageCode, value: string | null | undefined): string {
  if (!value) {
    return COPY[language].common.waiting;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(language === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatRunStatusForLocale(language: LanguageCode, value: string): string {
  const labels: Record<LanguageCode, Record<string, string>> = {
    en: {
      queued: "Queued",
      running: "Running",
      completed: "Completed",
      failed: "Failed",
    },
    zh: {
      queued: "排队中",
      running: "运行中",
      completed: "已完成",
      failed: "失败",
    },
  };
  return labels[language][value] ?? value;
}

function formatArtifactKindForLocale(language: LanguageCode, kind: string): string {
  return COPY[language].artifactKinds[kind as keyof Copy["artifactKinds"]] ?? kind.toUpperCase();
}

interface I18nContextValue {
  language: LanguageCode;
  setLanguage: (nextLanguage: LanguageCode) => void;
  copy: Copy;
  formatDateTime: (value: string | null | undefined) => string;
  formatRunStatus: (value: string) => string;
  formatArtifactKind: (value: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: PropsWithChildren) {
  const [language, setLanguage] = useState<LanguageCode>(detectPreferredLanguage);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  }, [language]);

  const value = useMemo<I18nContextValue>(
    () => ({
      language,
      setLanguage,
      copy: COPY[language],
      formatDateTime: (input) => formatDateTimeForLocale(language, input),
      formatRunStatus: (input) => formatRunStatusForLocale(language, input),
      formatArtifactKind: (input) => formatArtifactKindForLocale(language, input),
    }),
    [language],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return context;
}
