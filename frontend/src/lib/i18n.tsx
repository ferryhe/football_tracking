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
    chinese: "中文",
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
    eyebrow: "AI-native workspace",
    title: "Football Tracking Operator",
    subtitle: "Pick one clip, run one baseline, then let AI iterate from evidence.",
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
    missionEyebrow: "Mission control",
    missionTitle: "Pin the next pass at the top",
    missionSubtitle: "Selections, focus run, and launch stay synced here.",
    selectEyebrow: "Input + baseline",
    selectTitle: "Choose clip and config",
    selectSubtitle: "Keep the first step tight. One clip, one baseline, one clean launch.",
    inputTitle: "Input clips",
    inputSubtitle: "Source videos discovered under the input folder.",
    inputDirectory: "Input folder",
    noInputTitle: "No clips found",
    noInputBody: "Drop a supported video into `data/`, then refresh the workspace.",
    baselineTitle: "Baseline configs",
    baselineSubtitle: "Use a kept config as the starting point for the next run.",
    noBaselineTitle: "No configs found",
    noBaselineBody: "Add or regenerate configs under `config/` first.",
    selectedInput: "Selected clip",
    selectedBaseline: "Selected config",
    launchCopy: "Launch one baseline with cleanup and follow-cam enabled so AI works from real artifacts.",
    launchButton: "Run baseline",
    launchStarting: "Starting...",
    launchHint: "Selections stay synced with the top control deck.",
    focusEyebrow: "Focused run",
    focusTitle: "Keep one run in focus",
    focusSubtitle: "This evidence bundle is the source of truth for AI.",
    currentFocus: "Current focus",
    focusRun: "Focus run",
    aiLane: "AI lane",
    aiReady: "Grounded and ready",
    aiWaiting: "Need one run first",
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
    noFocusBody: "Start a baseline run to unlock the evidence lane.",
    queueEyebrow: "Queue",
    queueTitle: "Recent runs",
    queueSubtitle: "Keep the list short here. Deep logs stay secondary.",
    runningNow: "Running now",
    noRunsTitle: "No runs yet",
    noRunsBody: "Launch the first baseline above to start the loop.",
    evidenceEyebrow: "Evidence",
    evidenceTitle: "Output bundle",
    evidenceSubtitle: "Videos, reports, and files from the selected run.",
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
    eyebrow: "AI",
    title: "Plan the next config",
    subtitle: "AI stays grounded in the selected run, not free-form guessing.",
    stageEvidence: "Evidence",
    stageExplain: "Explain",
    stageRecommend: "Recommend",
    stageDerive: "Derive",
    stageEvidenceNone: "Pick one run first.",
    stageExplainNone: "Summary appears after a run is selected.",
    stageRecommendNone: "Ask AI for the next config.",
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
    buttonRun: "Run AI config",
    activityExplain: "Refreshing AI explanation...",
    activityRecommend: "Generating grounded recommendation...",
    activityRun: "Deriving config and launching run...",
    latestDerived: "Latest derived config",
    readoutTitle: "Readout",
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
    chinese: "中文",
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
    eyebrow: "AI 工作台",
    title: "足球跟踪控制台",
    subtitle: "先选一条视频，跑一版基线，再让 AI 基于证据继续迭代。",
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
    missionEyebrow: "顶部主控",
    missionTitle: "把下一次运行固定在顶部",
    missionSubtitle: "已选视频、已选基线、焦点 run 和启动动作都钉在这里。",
    selectEyebrow: "输入 + 基线",
    selectTitle: "先选视频和配置",
    selectSubtitle: "第一步尽量收紧：一条视频、一份基线、一次干净启动。",
    inputTitle: "输入视频",
    inputSubtitle: "自动发现输入目录下的源视频。",
    inputDirectory: "输入目录",
    noInputTitle: "还没有视频",
    noInputBody: "把支持的视频文件放进 `data/` 后再刷新。",
    baselineTitle: "基线配置",
    baselineSubtitle: "从保留配置里选一份，作为下一次运行的起点。",
    noBaselineTitle: "还没有配置",
    noBaselineBody: "请先在 `config/` 下添加或重新生成配置。",
    selectedInput: "已选视频",
    selectedBaseline: "已选配置",
    launchCopy: "这里会直接启动带 cleanup 和 follow-cam 的基线运行，方便 AI 基于真实产物继续迭代。",
    launchButton: "启动基线",
    launchStarting: "启动中...",
    launchHint: "这里的选择会和顶部主控区保持同步。",
    focusEyebrow: "焦点运行",
    focusTitle: "始终只盯一个 run",
    focusSubtitle: "这个证据包就是 AI 的唯一依据。",
    currentFocus: "当前焦点",
    focusRun: "焦点 run",
    aiLane: "AI 通道",
    aiReady: "证据齐了，可继续",
    aiWaiting: "先有一次 run 再说",
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
    noFocusBody: "先启动一版基线，才能打开证据通道。",
    queueEyebrow: "队列",
    queueTitle: "最近运行",
    queueSubtitle: "这里只保留短列表，详细日志放在次级层。",
    runningNow: "当前运行",
    noRunsTitle: "还没有运行记录",
    noRunsBody: "先在上面启动第一条基线。",
    evidenceEyebrow: "证据",
    evidenceTitle: "输出证据包",
    evidenceSubtitle: "选中 run 的视频、报告和文件都集中放在这里。",
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
    eyebrow: "AI",
    title: "规划下一版配置",
    subtitle: "AI 只基于选中的 run 工作，不做脱离证据的猜测。",
    stageEvidence: "证据",
    stageExplain: "解释",
    stageRecommend: "推荐",
    stageDerive: "生成",
    stageEvidenceNone: "先选一个 run。",
    stageExplainNone: "选中 run 后会自动生成摘要。",
    stageRecommendNone: "让 AI 推荐下一版配置。",
    stageDeriveNone: "这里会显示生成配置名。",
    evidenceRun: "证据 run",
    targetVideo: "目标视频",
    knownConfigs: "配置数",
    objective: "目标",
    presetSteady: "稳镜头",
    presetRecover: "找回球",
    presetClean: "更干净",
    buttonRecommend: "生成推荐",
    buttonThinking: "思考中...",
    buttonRun: "运行 AI 配置",
    activityExplain: "正在刷新 AI 解释...",
    activityRecommend: "正在生成基于证据的推荐...",
    activityRun: "正在生成配置并启动运行...",
    latestDerived: "最近生成配置",
    readoutTitle: "AI 读数",
    readoutSubtitle: "摘要、证据数量和下一版配置状态都放在这里。",
    readoutFallback: "选中 run 后，这里会显示 AI 摘要。",
    evidencePoints: "证据点",
    patchLines: "补丁行数",
    nextConfig: "下一配置",
    recommendation: "推荐结论",
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
