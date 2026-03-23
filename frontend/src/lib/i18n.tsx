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
    refreshHint: "Polling stays on in the background.",
  },
  header: {
    eyebrow: "AI workflow",
    title: "Football Tracking Operator",
    subtitle: "One manual baseline, then AI handles config tuning from evidence.",
    activeTask: "Active task",
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
    flowChooseTitle: "Choose clip + baseline",
    flowChooseDetail: "Human step. Pick the input clip and the starting config.",
    flowRunTitle: "Run one baseline",
    flowRunDetail: "Human step. Launch once to create the first evidence bundle.",
    flowAiTitle: "AI suggests changes",
    flowAiDetail: "AI reads the evidence, explains issues, and drafts the next config patch.",
    flowReviewTitle: "Review outputs",
    flowReviewDetail: "Human step. Check outputs, approve, or rerun.",
    selectEyebrow: "Manual lane",
    selectTitle: "Pick the clip and launch once",
    selectSubtitle: "This is the only place you touch input and baseline. Later tuning should come from AI.",
    inputTitle: "Input clips",
    inputSubtitle: "Pick the source video for the next baseline run.",
    fieldSetupTitle: "Field setup",
    fieldSetupSubtitle: "Generate one suggested field box under the selected clip, then confirm it before the first baseline.",
    fieldGenerate: "AI generate suggestion",
    fieldGenerating: "Generating...",
    fieldClear: "Clear",
    fieldPreviewAlt: "Suggested field overlay preview",
    fieldFrame: "Preview frame",
    fieldFieldBox: "Field box",
    fieldExpandedBox: "Expanded box",
    fieldConfidenceDetected: "Detected from the sampled frame",
    fieldConfidenceFallback: "Fallback safe box",
    fieldApplyHint: "If this preview looks reasonable, the next baseline run will apply it automatically.",
    fieldEmptyBody: "Generate one suggestion first, then inspect the overlay here.",
    fieldReadyMessage: "Field setup suggestion is ready for the next baseline run.",
    inputDirectory: "Input folder",
    noInputTitle: "No clips found",
    noInputBody: "Drop a supported video into `data/`, then refresh the workspace.",
    baselineTitle: "Baseline configs",
    baselineSubtitle: "Pick the baseline config AI will build from after the first run.",
    baselineSummaryTitle: "Selected config summary",
    baselineDefaultHint: "This clip has no run history yet, so a first-run or default config is the safest start.",
    baselineReuseHint: "This clip already has history, so reusing its latest config is a reasonable baseline.",
    scopeLabel: "Scope",
    scopeFull: "Full run",
    scopePartial: "Partial run",
    scopeStandard: "Standard",
    baselineLoopHint: "For the first loop, prefer a standard or partial config. Keep full-run configs for the final delivery pass.",
    noBaselineTitle: "No configs found",
    noBaselineBody: "If `real_first_run.yaml` or `default.yaml` exists it will be auto-used. If `config/` is truly empty, baseline stays disabled.",
    selectedInput: "Selected clip",
    selectedBaseline: "Selected config",
    launchCopy: "Run one baseline. After that, let AI suggest the config changes.",
    launchButton: "Run baseline",
    launchStarting: "Starting...",
    selectionDetails: "Current file and config",
    selectionDetailsSubtitle: "Keep the main surface simple. Open this only when you need the full path and config context.",
    launchHint: "Manual edits stop after the first run. Let AI handle later config suggestions.",
    focusEyebrow: "Current evidence",
    focusTitle: "Keep one run in focus",
    focusSubtitle: "Choose the related run here first. AI only explains the focused run.",
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
    noFocusTitle: "No run selected",
    noFocusBody: "Run the selected source video first to unlock related history here.",
    queueEyebrow: "History",
    queueTitle: "Conversion history",
    queueSubtitle: "Past runs live here. Use this tab only when AI should inspect older evidence.",
    deliveryEyebrow: "History",
    deliveryTitle: "All history",
    deliverySubtitle: "View every past run here, across all source videos.",
    deliveryEmptyTitle: "No history yet",
    deliveryEmptyBody: "Finish one run first, then the history list will show here.",
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
    allArtifacts: "Full file list",
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
    title: "AI suggests config changes",
    subtitle: "After one baseline run, AI reads evidence, explains issues, proposes a patch, and prepares the next config.",
    stageEvidence: "Evidence",
    stageExplain: "Explain",
    stageRecommend: "Recommend",
    stageDerive: "Derive",
    stageEvidenceNone: "Pick one run first.",
    stageExplainNone: "Summary appears after a run is selected.",
    stageRecommendNone: "Ask AI to draft the next config.",
    stageDeriveNone: "The generated config name shows here.",
    evidenceRun: "Evidence run",
    targetVideo: "Target clip",
    knownConfigs: "Configs",
    objective: "Objective",
    presetSteady: "Steady cam",
    presetRecover: "Recover ball",
    presetClean: "Cleaner result",
    buttonRecommend: "Recommend",
    buttonThinking: "Thinking...",
    buttonRun: "Run suggested config",
    activityExplain: "Refreshing AI explanation...",
    activityRecommend: "Generating grounded recommendation...",
    activityRun: "Deriving config and launching run...",
    latestDerived: "Latest derived config",
    readoutTitle: "AI readout",
    readoutSubtitle: "Summary, evidence count, and next-config readiness.",
    readoutFallback: "AI summary appears after you select a run.",
    evidencePoints: "Evidence",
    patchLines: "Patch lines",
    nextConfig: "Next config",
    recommendation: "Recommendation",
    evidencePatch: "Show AI evidence and patch",
    configDiff: "Show config diff",
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
    selected: "已选",
    baseline: "基线",
    noActiveRun: "暂无运行任务",
    stillRunning: "仍在运行或排队",
    refreshHint: "后台会持续轮询刷新。",
  },
  header: {
    eyebrow: "AI 工作流",
    title: "足球跟踪控制台",
    subtitle: "先手动跑一版基线，后面的配置迭代交给 AI 基于证据处理。",
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
    flowChooseTitle: "选择视频和基线",
    flowChooseDetail: "人工步骤。选择输入视频和起始配置。",
    flowRunTitle: "跑一版基线",
    flowRunDetail: "人工步骤。先跑一次，拿到第一份证据包。",
    flowAiTitle: "AI 建议改动",
    flowAiDetail: "AI 读取证据、解释问题，并给出下一版配置补丁。",
    flowReviewTitle: "检查输出",
    flowReviewDetail: "人工步骤。查看输出、决定接受还是重跑。",
    selectEyebrow: "人工操作",
    selectTitle: "先选视频，再启动一次",
    selectSubtitle: "这里只处理输入和首版基线。后续调参建议应交给 AI。",
    inputTitle: "输入视频",
    inputSubtitle: "选择下一次基线运行要用的源视频。",
    fieldSetupTitle: "球场设置",
    fieldSetupSubtitle: "在已选视频下先生成一份球场建议，确认后再跑第一次基线。",
    fieldGenerate: "AI 生成建议",
    fieldGenerating: "生成中...",
    fieldClear: "清除",
    fieldPreviewAlt: "球场建议预览图",
    fieldFrame: "预览帧",
    fieldFieldBox: "球场区域",
    fieldExpandedBox: "扩展区域",
    fieldConfidenceDetected: "基于抽帧自动检测",
    fieldConfidenceFallback: "使用保守默认框",
    fieldApplyHint: "如果这个预览看起来合理，下一次基线运行就会自动带上它。",
    fieldEmptyBody: "先点击生成建议，然后在这里确认预览。",
    fieldReadyMessage: "球场建议已生成，下一次基线运行会带上这个设置。",
    inputDirectory: "输入目录",
    noInputTitle: "还没有视频",
    noInputBody: "把支持的视频文件放进 `data/` 后再刷新。",
    baselineTitle: "基线配置",
    baselineSubtitle: "选择第一版起始配置，后续由 AI 在它基础上继续建议修改。",
    baselineSummaryTitle: "当前配置摘要",
    baselineDefaultHint: "这个视频还没有跑过，所以 first-run 或 default 配置是更稳的起点。",
    baselineReuseHint: "这个视频已经有历史，直接用它最近的配置作为基线更合适。",
    scopeLabel: "范围",
    scopeFull: "全量",
    scopePartial: "部分",
    scopeStandard: "标准",
    baselineLoopHint: "第一轮先用标准或部分配置更合适，全量配置更适合最终交付。",
    noBaselineTitle: "还没有配置",
    noBaselineBody: "如果存在 `real_first_run.yaml` 或 `default.yaml` 会自动选用；如果 `config/` 真是空的，就不能启动基线。",
    selectedInput: "已选视频",
    selectedBaseline: "已选配置",
    launchCopy: "先跑一版基线。之后的配置改动交给 AI 建议。",
    launchButton: "开始跑基线",
    launchStarting: "启动中...",
    selectionDetails: "当前文件和配置",
    selectionDetailsSubtitle: "首屏先保持简单，只有需要看完整路径和配置上下文时再打开这里。",
    launchHint: "第一版之后尽量不要手改参数，让 AI 来给建议。",
    focusEyebrow: "当前证据",
    focusTitle: "始终只盯一个 run",
    focusSubtitle: "先在这里选择相关 run，AI 只解释当前焦点 run。",
    currentFocus: "焦点 run",
    focusRun: "焦点 run",
    aiLane: "AI 角色",
    aiReady: "AI 可以继续建议下一版配置",
    aiWaiting: "先跑出一版基线",
    detected: "检测到",
    lost: "丢失",
    artifacts: "产物",
    lastEvent: "最近事件",
    inputVideo: "输入视频",
    outputDirectory: "输出目录",
    created: "创建时间",
    completed: "完成时间",
    runNotes: "备注",
    noFocusTitle: "还没有选中 run",
    noFocusBody: "先把当前原视频跑出一版结果，这里才会出现相关历史。",
    queueEyebrow: "历史",
    queueTitle: "转换历史",
    queueSubtitle: "过往运行都放这里。只有在你想让 AI 回看旧证据时才用这个 tab。",
    deliveryEyebrow: "历史",
    deliveryTitle: "全部历史",
    deliverySubtitle: "这里查看所有过往 run，不区分原视频。",
    deliveryEmptyTitle: "还没有历史记录",
    deliveryEmptyBody: "先完成一次运行，这里才会出现历史列表。",
    deliveryRanAt: "运行时间",
    deliveryResultFolder: "结果目录",
    runningNow: "当前运行",
    noRunsTitle: "还没有运行记录",
    noRunsBody: "先在上面启动第一版基线。",
    evidenceEyebrow: "输出",
    evidenceTitle: "输出证据包",
    evidenceSubtitle: "焦点 run 的视频、报告和文件都放在这里。",
    featuredOutputs: "重点产物",
    featuredOutputsSubtitle: "先打开主要交付产物。",
    allArtifacts: "完整文件列表",
    allArtifactsSubtitle: "这个 run 导出的全部文件。",
    openArtifact: "打开",
    run: "运行",
    modulesEnabled: "启用模块",
    artifactsReady: "可用产物",
    outputFolder: "输出文件夹",
    followCamVideo: "跟随镜头",
    cleanedVideo: "清洗后视频",
    noEvidenceTitle: "还没有证据",
    noEvidenceBody: "启动或选中一个 run 后，这里会显示输出内容。",
    cleanup: "清洗",
    followCam: "跟随镜头",
  },
  ai: {
    eyebrow: "AI 角色",
    title: "AI 负责建议配置改动",
    subtitle: "在第一版基线之后，AI 会读取证据、解释问题、提出补丁，并准备下一版配置。",
    stageEvidence: "证据",
    stageExplain: "解释",
    stageRecommend: "建议",
    stageDerive: "生成",
    stageEvidenceNone: "先选一个 run。",
    stageExplainNone: "选中 run 后会生成摘要。",
    stageRecommendNone: "让 AI 起草下一版配置。",
    stageDeriveNone: "这里会显示生成配置名。",
    evidenceRun: "证据 run",
    targetVideo: "目标视频",
    knownConfigs: "配置数",
    objective: "目标",
    presetSteady: "稳镜头",
    presetRecover: "找回球",
    presetClean: "更干净",
    buttonRecommend: "生成建议",
    buttonThinking: "思考中...",
    buttonRun: "运行建议配置",
    activityExplain: "正在刷新 AI 解释...",
    activityRecommend: "正在生成基于证据的建议...",
    activityRun: "正在生成配置并启动运行...",
    latestDerived: "最近生成配置",
    readoutTitle: "AI 读数",
    readoutSubtitle: "摘要、证据数量和下一版配置状态都放在这里。",
    readoutFallback: "选中 run 后，这里会显示 AI 摘要。",
    evidencePoints: "证据点",
    patchLines: "补丁行数",
    nextConfig: "下一配置",
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
