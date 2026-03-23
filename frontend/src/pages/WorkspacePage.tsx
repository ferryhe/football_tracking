import { useEffect, useMemo, useState } from "react";

import {
  ActivityIcon,
  CheckIcon,
  ClockIcon,
  FileIcon,
  FolderIcon,
  LayersIcon,
  PlayIcon,
  SparkIcon,
  TrashIcon,
  VideoIcon,
} from "../components/Icons";
import { ConfirmDeleteDialog } from "../components/ConfirmDeleteDialog";
import { FieldSetupCard } from "../components/FieldSetupCard";
import { TooltipBadge } from "../components/TooltipBadge";
import { sortConfigsByCreatedAt } from "../lib/configs";
import { useI18n } from "../lib/i18n";
import type { AssetGroup, ConfigListItem, FieldPreview, FieldSuggestion, InputCatalog, RunRecord } from "../lib/types";

export type WorkspaceStage = "baseline" | "ai" | "deliverable" | "history";
type HistoryCategory = "baseline" | "deliverable" | "failed";
type PendingDelete = {
  targetName: string;
  prompt: string;
  execute: () => Promise<void>;
};

interface WorkspacePageProps {
  stage: WorkspaceStage;
  inputCatalog: InputCatalog;
  configs: ConfigListItem[];
  assetGroups: AssetGroup[];
  runs: RunRecord[];
  selectedRun: RunRecord | null;
  selectedInputPath: string;
  selectedConfigName: string;
  loading: boolean;
  launching: boolean;
  launchMessage: string | null;
  fieldPreview: FieldPreview | null;
  fieldSuggestion: FieldSuggestion | null;
  fieldLoading: boolean;
  fieldMessage: string | null;
  canLoadFieldFromConfig: boolean;
  canStartBaseline: boolean;
  onSelectRun: (run: RunRecord) => void;
  onSelectInput: (path: string) => void;
  onSelectConfig: (name: string) => void;
  onCaptureFieldPreview: () => Promise<void>;
  onLoadFieldFromConfig: () => Promise<void>;
  onGenerateFieldSuggestion: () => Promise<void>;
  onClearFieldSuggestion: () => void;
  onUpdateFieldSuggestion: (suggestion: FieldSuggestion) => void;
  onAcceptFieldSuggestion: (suggestion: FieldSuggestion) => void;
  onStartBaselineRun: () => Promise<void>;
  onCreateFollowCamRender: (
    runId: string,
    options: {
      prefer_cleaned_track: boolean;
      draw_ball_marker: boolean;
      draw_frame_text: boolean;
    },
  ) => Promise<RunRecord>;
  onDeleteInputVideo: (name: string) => Promise<void>;
  onDeleteConfig: (name: string) => Promise<void>;
  onDeleteRunOutput: (runId: string) => Promise<void>;
}

function getTrackStats(run: RunRecord | null): Record<string, unknown> | null {
  if (!run) {
    return null;
  }
  const cleaned = run.stats.cleaned;
  if (cleaned && typeof cleaned === "object") {
    return cleaned as Record<string, unknown>;
  }
  const raw = run.stats.raw;
  if (raw && typeof raw === "object") {
    return raw as Record<string, unknown>;
  }
  return null;
}

function readNumber(stats: Record<string, unknown> | null, key: string): string {
  if (!stats) {
    return "-";
  }
  const value = stats[key];
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  return typeof value === "string" ? value : "-";
}

function formatVideoSize(sizeBytes: number): string {
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatPathTail(path: string | null | undefined): string {
  if (!path) {
    return "";
  }
  const pieces = path.split(/[\\/]/).filter(Boolean);
  return pieces.length ? pieces[pieces.length - 1] : path;
}

function formatParentPath(path: string | null | undefined): string {
  if (!path) {
    return "";
  }
  const normalized = path.replace(/[\\/]+$/, "");
  const separatorIndex = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return separatorIndex >= 0 ? normalized.slice(0, separatorIndex) : normalized;
}

function supportsFollowCamRender(run: RunRecord): boolean {
  return (
    run.status === "completed" &&
    Boolean(run.input_video) &&
    Boolean(run.config_name || run.config_path) &&
    run.artifacts.some((artifact) => artifact.name === "ball_track.csv" || artifact.name === "ball_track.cleaned.csv")
  );
}

function historyCategoryForRun(run: RunRecord): HistoryCategory {
  if (run.status === "failed") {
    return "failed";
  }
  if (run.source === "follow_cam_render") {
    return "deliverable";
  }
  return "baseline";
}

function runStatusIcon(status: string) {
  if (status === "completed") {
    return CheckIcon;
  }
  if (status === "running") {
    return PlayIcon;
  }
  if (status === "queued") {
    return ClockIcon;
  }
  return ActivityIcon;
}

function runMoment(run: RunRecord): string {
  return run.completed_at ?? run.started_at ?? run.created_at;
}

function inferConfigScope(configName: string | null | undefined): "full" | "partial" | "standard" {
  const value = configName?.toLowerCase() ?? "";
  if (/(sample|short|debug|preview|first|partial|quick)/.test(value)) {
    return "partial";
  }
  if (/(full|final|complete)/.test(value)) {
    return "full";
  }
  return "standard";
}

function scopeLabel(copy: ReturnType<typeof useI18n>["copy"], scope: "full" | "partial" | "standard"): string {
  if (scope === "full") {
    return copy.workspace.scopeFull;
  }
  if (scope === "partial") {
    return copy.workspace.scopePartial;
  }
  return copy.workspace.scopeStandard;
}

export function WorkspacePage({
  stage,
  inputCatalog,
  configs,
  assetGroups,
  runs,
  selectedRun,
  selectedInputPath,
  selectedConfigName,
  loading,
  launching,
  launchMessage,
  fieldPreview,
  fieldSuggestion,
  fieldLoading,
  fieldMessage,
  canLoadFieldFromConfig,
  canStartBaseline,
  onSelectRun,
  onSelectInput,
  onSelectConfig,
  onCaptureFieldPreview,
  onLoadFieldFromConfig,
  onGenerateFieldSuggestion,
  onClearFieldSuggestion,
  onUpdateFieldSuggestion,
  onAcceptFieldSuggestion,
  onStartBaselineRun,
  onCreateFollowCamRender,
  onDeleteInputVideo,
  onDeleteConfig,
  onDeleteRunOutput,
}: WorkspacePageProps) {
  const { copy, formatDateTime, formatRunStatus, language } = useI18n();
  const historyCopy = useMemo(
    () =>
      language === "zh"
        ? {
            renderEyebrow: "独立成品任务",
            renderTitle: "从历史 run 生成 16:9 成品",
            renderSubtitle: "这里不会重跑 detector 或基线，只复用已完成 run 的轨迹重新导出成品。",
            renderSelect: "来源 run",
            renderReady: "可用于成品裁剪的 run",
            renderOutput: "输出",
            renderSource: "来源",
            renderClip: "原视频",
            renderUseCleaned: "优先使用清洗后的轨迹",
            renderShowMarker: "显示球点标记",
            renderShowText: "显示文字标注",
            renderDefaults: "成品默认关闭标记和文字标注，只保留干净的 16:9 画面。",
            renderButton: "开始 16:9 成品裁剪",
            renderEmpty: "先完成至少一个包含轨迹 CSV 的 run，才能独立导出 16:9 成品。",
            renderCreated: "已创建独立成品任务",
            renderConfirm: "要开始新的 16:9 成品任务吗？",
            historySource: "来源 run",
            historyModeBaseline: "基线",
            historyModeRender: "成品",
            historyModeScan: "扫描",
            historyFilterBaseline: "Baseline",
            historyFilterDeliverable: "Deliverable",
            historyFilterFailed: "Failed",
            manageEyebrow: "资源管理",
            manageTitle: "视频和配置文件管理",
            manageSubtitle: "这里可以清理不再需要的输入视频和 YAML 配置。正在运行中的任务会被保护，不能删除。",
            manageSummary: "清理不用的视频和 YAML",
            manageVideos: "输入视频",
            manageConfigs: "配置文件",
            manageDelete: "删除",
            manageNoVideos: "没有可管理的视频。",
            manageNoConfigs: "没有可管理的配置。",
            manageDeleted: "已删除",
            manageDeleteVideoConfirm: "确定删除这个输入视频吗？",
            manageDeleteConfigConfirm: "确定删除这个配置文件吗？",
          }
        : {
            renderEyebrow: "Deliverable task",
            renderTitle: "Create a 16:9 deliverable from history",
            renderSubtitle: "This does not rerun detector or baseline. It reuses the selected completed run and renders a clean deliverable.",
            renderSelect: "Source run",
            renderReady: "Runs ready for deliverable render",
            renderOutput: "Output",
            renderOutputHint: "A new deliverable folder will be created under",
            renderSource: "Source",
            renderClip: "Clip",
            renderUseCleaned: "Prefer cleaned track CSV",
            renderShowMarker: "Show ball marker",
            renderShowText: "Show frame text / annotation",
            renderDefaults: "Final deliverables default to a clean 16:9 frame with marker and annotation turned off.",
            renderButton: "Start 16:9 deliverable render",
            renderEmpty: "Complete at least one run with track CSVs before starting a 16:9 deliverable render.",
            renderCreated: "Deliverable task created",
            renderConfirm: "Start a new 16:9 deliverable task?",
            historySource: "Source run",
            historyModeBaseline: "Baseline",
            historyModeRender: "Deliverable",
            historyModeScan: "Scanned",
            historyFilterBaseline: "Baseline",
            historyFilterDeliverable: "Deliverable",
            historyFilterFailed: "Failed",
            manageEyebrow: "File management",
            manageTitle: "Video and config cleanup",
            manageSubtitle: "Remove videos, YAML configs, and output folders you no longer need. Active runs stay protected.",
            manageSummary: "Clean up unused videos, YAML configs, and outputs",
            manageVideos: "Input videos",
            manageConfigs: "Config files",
            manageOutputs: "Output folders",
            manageDelete: "Delete",
            manageNoVideos: "No videos to manage.",
            manageNoConfigs: "No configs to manage.",
            manageNoOutputs: "No outputs to manage.",
            manageDeleted: "Deleted",
            manageDeleteVideoConfirm: "Delete this input video?",
            manageDeleteConfigConfirm: "Delete this config file?",
            manageDeleteOutputConfirm: "Delete this output folder?",
          },
    [language],
  );
  const [renderRunId, setRenderRunId] = useState("");
  const [historyFilter, setHistoryFilter] = useState<HistoryCategory>("baseline");
  const [renderBusy, setRenderBusy] = useState(false);
  const [historyMessage, setHistoryMessage] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
  const [deleteConfirmValue, setDeleteConfirmValue] = useState("");
  const renderOutputHint = language === "zh" ? "新的成品文件夹会创建在" : "A new deliverable folder will be created under";
  const manageOutputsLabel = language === "zh" ? "输出文件夹" : "Output folders";
  const manageNoOutputs = language === "zh" ? "没有可管理的输出。" : "No outputs to manage.";
  const manageDeleteOutputConfirm = language === "zh" ? "确定删除这个输出文件夹吗？" : "Delete this output folder?";
  const deleteDialogTitle = language === "zh" ? "确认删除" : "Confirm deletion";
  const deleteDialogTarget = language === "zh" ? "目标" : "Target";
  const deleteDialogInputLabel = language === "zh" ? "输入 DELETE 以继续" : "Type DELETE to continue";
  const deleteDialogCancel = language === "zh" ? "取消" : "Cancel";
  const deleteDialogConfirm = language === "zh" ? "确认删除" : "Confirm delete";
  const deleteDialogPhrase = "DELETE";
  const [renderOptions, setRenderOptions] = useState({
    prefer_cleaned_track: true,
    draw_ball_marker: false,
    draw_frame_text: false,
  });
  const uiCopy = useMemo(
    () =>
      language === "zh"
        ? {
            assetGroupsEyebrow: "分组资产",
            assetGroupsTitle: "按原视频分组管理",
            assetGroupsSubtitle: "一个原视频下面收口它的源文件、配置和输出；没有输入视频关联的旧内容放到 Unbound / Legacy。",
            groupSource: "Source",
            groupConfigs: "Configs",
            groupOutputs: "Outputs",
            groupRuns: "Runs",
            groupLastActivity: "最近活动",
            groupNoSource: "这个分组没有可管理的源视频。",
            groupNoConfigs: "这个分组还没有关联配置。",
            groupNoOutputs: "这个分组还没有输出目录。",
            groupPath: "路径",
            groupStatus: "状态",
            unboundGroupTitle: "Unbound / Legacy",
            scopeTooltip: "Scope 表示这个配置预期的运行范围或强度。",
            scopeStandardTooltip: "Standard 适合作为默认起点，速度和覆盖范围比较平衡。",
            scopePartialTooltip: "Partial 更偏快速试跑，只覆盖片段或更保守的范围。",
            scopeFullTooltip: "Full 更适合最终全量导出，通常更慢但覆盖更完整。",
            cleanupTooltip: "Cleanup 会在原始轨迹之后做清洗，去掉坏点并补平明显异常。",
            followCamTooltip: "Follow-cam 会根据轨迹生成跟随裁剪视频。",
            renderUseCleanedTooltip: "优先使用清洗后的 ball_track.cleaned.csv；没有时再回退到原始轨迹。",
            renderShowMarkerTooltip: "在成品视频里叠加球点标记。",
            renderShowTextTooltip: "在成品视频里叠加状态文字和帧标注。",
          }
        : {
            assetGroupsEyebrow: "Asset groups",
            assetGroupsTitle: "Manage assets by source clip",
            assetGroupsSubtitle: "Each source clip owns its source file, configs, and outputs. Items without a matched input clip stay under Unbound / Legacy.",
            groupSource: "Source",
            groupConfigs: "Configs",
            groupOutputs: "Outputs",
            groupRuns: "Runs",
            groupLastActivity: "Last activity",
            groupNoSource: "No source video is attached to this group.",
            groupNoConfigs: "No configs are linked to this group yet.",
            groupNoOutputs: "No output folders are linked to this group yet.",
            groupPath: "Path",
            groupStatus: "Status",
            unboundGroupTitle: "Unbound / Legacy",
            scopeTooltip: "Scope describes how broad or heavy this config is meant to be.",
            scopeStandardTooltip: "Standard is the balanced default starting point.",
            scopePartialTooltip: "Partial is a quicker or narrower probe pass.",
            scopeFullTooltip: "Full is better suited for final full-length delivery runs.",
            cleanupTooltip: "Cleanup postprocesses the raw track to remove bad points and smooth obvious breaks.",
            followCamTooltip: "Follow-cam renders the cropped tracking video from the selected track.",
            renderUseCleanedTooltip: "Prefer ball_track.cleaned.csv when it exists, then fall back to the raw track.",
            renderShowMarkerTooltip: "Overlay the ball marker on the deliverable video.",
            renderShowTextTooltip: "Overlay status text and frame annotations on the deliverable video.",
          },
    [language],
  );
  const orderedConfigs = useMemo(() => sortConfigsByCreatedAt(configs), [configs]);
  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = orderedConfigs.find((item) => item.name === selectedConfigName) ?? null;
  const selectedScope = inferConfigScope(selectedConfig?.name);
  const hasRunHistoryForInput = runs.some((run) => run.input_video === selectedInputPath);
  const aiRuns = selectedInputPath ? runs.filter((run) => run.input_video === selectedInputPath) : runs;
  const aiSelectedRun = selectedRun && aiRuns.some((run) => run.run_id === selectedRun.run_id) ? selectedRun : aiRuns[0] ?? null;
  const stats = getTrackStats(aiSelectedRun ?? selectedRun);
  const renderableRuns = useMemo(
    () =>
      runs.filter(
        (run) => supportsFollowCamRender(run) && inputCatalog.videos.some((item) => item.path === run.input_video),
      ),
    [inputCatalog.videos, runs],
  );
  const filteredHistoryRuns = useMemo(
    () => runs.filter((run) => historyCategoryForRun(run) === historyFilter),
    [historyFilter, runs],
  );
  const activeRenderRun =
    renderableRuns.find((run) => run.run_id === renderRunId) ??
    (selectedRun && renderableRuns.some((run) => run.run_id === selectedRun.run_id) ? selectedRun : renderableRuns[0] ?? null);
  const deliverableOutputRoot = formatParentPath(activeRenderRun?.output_dir);
  const aiCompletedMoment =
    aiSelectedRun?.completed_at ??
    (aiSelectedRun?.status === "completed" ? aiSelectedRun.started_at ?? aiSelectedRun.created_at : null);

  function scopeTooltipText(scope: "full" | "partial" | "standard"): string {
    if (scope === "full") {
      return `${uiCopy.scopeTooltip} ${uiCopy.scopeFullTooltip}`;
    }
    if (scope === "partial") {
      return `${uiCopy.scopeTooltip} ${uiCopy.scopePartialTooltip}`;
    }
    return `${uiCopy.scopeTooltip} ${uiCopy.scopeStandardTooltip}`;
  }

  useEffect(() => {
    if (!pendingDelete) {
      setDeleteConfirmValue("");
      return;
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPendingDelete(null);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [pendingDelete]);

  useEffect(() => {
    if (renderableRuns.some((run) => run.run_id === renderRunId)) {
      return;
    }
    if (selectedRun && renderableRuns.some((run) => run.run_id === selectedRun.run_id)) {
      setRenderRunId(selectedRun.run_id);
      return;
    }
    setRenderRunId(renderableRuns[0]?.run_id ?? "");
  }, [renderRunId, renderableRuns, selectedRun]);

  async function handleCreateDeliverableRender() {
    if (!activeRenderRun) {
      return;
    }
    if (typeof window !== "undefined" && !window.confirm(historyCopy.renderConfirm)) {
      return;
    }
    setRenderBusy(true);
    setHistoryMessage(null);
    try {
      const createdRun = await onCreateFollowCamRender(activeRenderRun.run_id, renderOptions);
      setRenderRunId(createdRun.run_id);
      setHistoryMessage(`${historyCopy.renderCreated}: ${createdRun.run_id}`);
    } catch (caughtError) {
      setHistoryMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setRenderBusy(false);
    }
  }

  function requestDelete(targetName: string, prompt: string, execute: () => Promise<void>) {
    setPendingDelete({ targetName, prompt, execute });
  }

  async function handleDeleteVideo(name: string) {
    requestDelete(name, historyCopy.manageDeleteVideoConfirm, async () => {
      await onDeleteInputVideo(name);
    });
  }

  async function handleConfirmDelete() {
    if (!pendingDelete || deleteConfirmValue.trim().toUpperCase() !== deleteDialogPhrase) {
      return;
    }
    setRenderBusy(true);
    setHistoryMessage(null);
    try {
      await pendingDelete.execute();
      setHistoryMessage(`${historyCopy.manageDeleted}: ${pendingDelete.targetName}`);
      setPendingDelete(null);
    } catch (caughtError) {
      setHistoryMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setRenderBusy(false);
    }
  }

  async function handleDeleteConfigClick(name: string) {
    requestDelete(name, historyCopy.manageDeleteConfigConfirm, async () => {
      await onDeleteConfig(name);
    });
  }

  async function handleDeleteRunOutputClick(runId: string) {
    requestDelete(runId, manageDeleteOutputConfirm, async () => {
      await onDeleteRunOutput(runId);
    });
  }

  if (stage === "baseline") {
    return (
      <div className="page-stack">
        <section className="panel workflow-panel">
          <div className="panel-header">
            <div className="title-row">
              <PlayIcon className="section-icon" />
              <div className="title-with-tooltip">
                <p className="eyebrow">{copy.workspace.selectEyebrow}</p>
                <div className="title-inline">
                  <h3>{copy.workspace.selectTitle}</h3>
                  <TooltipBadge label={copy.workspace.selectSubtitle} />
                </div>
              </div>
            </div>
          </div>

          <div className="step-form-grid">
            <section className="step-form-section">
              <div className="section-intro title-row">
                <VideoIcon className="section-icon" />
                <div className="title-with-tooltip">
                  <div className="title-inline">
                    <h4>{copy.workspace.inputTitle}</h4>
                    <TooltipBadge label={copy.workspace.inputSubtitle} />
                  </div>
                </div>
              </div>

              <label className="form-label">
                <span className="meta-label">{copy.workspace.selectedInput}</span>
                <select value={selectedInputPath} onChange={(event) => onSelectInput(event.target.value)} disabled={!inputCatalog.videos.length}>
                  {inputCatalog.videos.length ? null : <option value="">{copy.workspace.noInputTitle}</option>}
                  {inputCatalog.videos.map((video) => (
                    <option key={video.path} value={video.path}>
                      {video.name}
                    </option>
                  ))}
                </select>
              </label>

              {selectedVideo ? (
                <div className="selection-summary-card">
                  <div className="selection-summary-head">
                    <strong className="summary-title">{selectedVideo.name}</strong>
                    {inputCatalog.root_dir ? <span className="minor-path mono">({inputCatalog.root_dir})</span> : null}
                  </div>
                  <div className="tag-row">
                    <span className="tag">{formatVideoSize(selectedVideo.size_bytes)}</span>
                    <span className="tag">{formatDateTime(selectedVideo.modified_at)}</span>
                  </div>
                </div>
              ) : (
                <div className="empty-state">
                  <strong>{copy.workspace.noInputTitle}</strong>
                  <p className="muted">{copy.workspace.noInputBody}</p>
                </div>
              )}

              {selectedVideo ? (
                <FieldSetupCard
                  preview={fieldPreview}
                  suggestion={fieldSuggestion}
                  loading={fieldLoading}
                  message={fieldMessage}
                  canLoadFromConfig={canLoadFieldFromConfig}
                  canStartBaseline={canStartBaseline}
                  launching={launching}
                  launchMessage={launchMessage}
                  onCapturePreview={onCaptureFieldPreview}
                  onLoadFromConfig={onLoadFieldFromConfig}
                  onGenerate={onGenerateFieldSuggestion}
                  onClear={onClearFieldSuggestion}
                  onUpdate={onUpdateFieldSuggestion}
                  onAccept={onAcceptFieldSuggestion}
                  onStartBaseline={onStartBaselineRun}
                />
              ) : null}
            </section>

            <section className="step-form-section">
              <div className="section-intro title-row">
                <LayersIcon className="section-icon" />
                <div className="title-with-tooltip">
                  <div className="title-inline">
                    <h4>{copy.workspace.baselineTitle}</h4>
                    <TooltipBadge label={copy.workspace.baselineSubtitle} />
                  </div>
                </div>
              </div>

              <label className="form-label">
                <span className="meta-label">{copy.workspace.selectedBaseline}</span>
                <select value={selectedConfigName} onChange={(event) => onSelectConfig(event.target.value)} disabled={!orderedConfigs.length}>
                  {orderedConfigs.length ? null : <option value="">{copy.workspace.noBaselineTitle}</option>}
                  {orderedConfigs.map((config) => (
                    <option key={config.name} value={config.name}>
                      {config.created_at ? `${config.name} | ${formatDateTime(config.created_at)}` : config.name}
                    </option>
                  ))}
                </select>
              </label>

              {selectedConfig ? (
                <article className="selection-summary-card compact-selection-card">
                  <div className="selection-summary-head">
                    <strong className="summary-title">{selectedConfig.name}</strong>
                    <TooltipBadge label={hasRunHistoryForInput ? copy.workspace.baselineReuseHint : copy.workspace.baselineDefaultHint} />
                  </div>
                  <div className="tag-row">
                    {selectedConfig.created_at ? <span className="tag">{formatDateTime(selectedConfig.created_at)}</span> : null}
                    <span className="tag" title={scopeTooltipText(selectedScope)}>
                      {copy.workspace.scopeLabel}: {scopeLabel(copy, selectedScope)}
                    </span>
                    <span
                      className={`tag ${selectedConfig.postprocess_enabled ? "good" : ""}`}
                      title={uiCopy.cleanupTooltip}
                    >
                      {copy.workspace.cleanup}
                    </span>
                    <span
                      className={`tag ${selectedConfig.follow_cam_enabled ? "good" : ""}`}
                      title={uiCopy.followCamTooltip}
                    >
                      {copy.workspace.followCam}
                    </span>
                  </div>
                </article>
              ) : (
                <div className="empty-state">
                  <strong>{copy.workspace.noBaselineTitle}</strong>
                  <p className="muted">{copy.workspace.noBaselineBody}</p>
                </div>
              )}
            </section>
          </div>

          <details className="assistant-card detail-card selection-tail-card">
            <summary className="detail-summary-inline">
              <span>{copy.workspace.selectionDetails}</span>
              <TooltipBadge label={copy.workspace.selectionDetailsSubtitle} />
            </summary>
            {selectedVideo ? (
              <div className="detail-grid">
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.selectedInput}</p>
                  <p className="mono">{selectedVideo.path}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.inputDirectory}</p>
                  <p className="mono">{inputCatalog.root_dir || copy.common.unavailable}</p>
                </div>
              </div>
            ) : null}
            {selectedConfig ? (
              <div className="detail-grid">
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.selectedBaseline}</p>
                  <p className="mono">{selectedConfig.name}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.baselineSummaryTitle}</p>
                  <p className="mono">
                    {selectedConfig.detector_model_path ? formatPathTail(selectedConfig.detector_model_path) : copy.common.notAvailable}
                  </p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.outputDirectory}</p>
                  <p className="mono">{selectedConfig.output_dir ?? copy.common.unavailable}</p>
                </div>
              </div>
            ) : null}
          </details>
        </section>
      </div>
    );
  }

  if (stage === "deliverable") {
    return (
      <div className="page-stack">
        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <FileIcon className="section-icon" />
              <div className="title-with-tooltip">
                <p className="eyebrow">{copy.workspace.deliverableEyebrow}</p>
                <div className="title-inline">
                  <h3>{copy.workspace.deliverableTitle}</h3>
                  <TooltipBadge label={copy.workspace.deliverableSubtitle} />
                </div>
              </div>
            </div>
          </div>

          <div className="history-grid">
            <article className="assistant-card primary history-action-card">
              <div className="title-row">
                <VideoIcon className="section-icon" />
                <div className="title-with-tooltip">
                  <p className="eyebrow">{historyCopy.renderEyebrow}</p>
                  <div className="title-inline">
                    <h4>{historyCopy.renderTitle}</h4>
                    <TooltipBadge label={`${historyCopy.renderSubtitle} ${historyCopy.renderDefaults}`} />
                  </div>
                </div>
              </div>

              {renderableRuns.length ? (
                <>
                  <label className="form-label">
                    <span className="meta-label">{historyCopy.renderSelect}</span>
                    <select
                      value={activeRenderRun?.run_id ?? ""}
                      onChange={(event) => {
                        setRenderRunId(event.target.value);
                        const nextRun = renderableRuns.find((run) => run.run_id === event.target.value);
                        if (nextRun) {
                          onSelectRun(nextRun);
                        }
                      }}
                    >
                      {renderableRuns.map((run) => (
                        <option key={run.run_id} value={run.run_id}>
                          {`${formatDateTime(runMoment(run))} | ${run.run_id} | ${formatPathTail(run.input_video)}`}
                        </option>
                      ))}
                    </select>
                  </label>

                  <div className="render-summary-strip">
                    <article className="summary-card compact-summary-card source-summary-card">
                      <p className="meta-label">{historyCopy.renderSource}</p>
                      <p className="mono summary-value compact-source-value">{activeRenderRun?.run_id ?? copy.common.notAvailable}</p>
                    </article>
                    <article className="summary-card compact-summary-card">
                      <p className="meta-label">{historyCopy.renderClip}</p>
                      <strong>{formatPathTail(activeRenderRun?.input_video) || copy.common.notAvailable}</strong>
                    </article>
                    <article className="summary-card compact-summary-card">
                      <p className="meta-label">{historyCopy.renderOutput}</p>
                      <strong>16:9 clean</strong>
                    </article>
                  </div>

                  {deliverableOutputRoot ? (
                    <p className="muted compact-resource-note mono">{`${renderOutputHint}: ${deliverableOutputRoot}`}</p>
                  ) : null}

                  <div className="option-toggle-grid compact-toggle-grid">
                    <label className="option-toggle">
                      <span className="meta-inline">
                        <input
                          type="checkbox"
                          checked={renderOptions.prefer_cleaned_track}
                          onChange={(event) =>
                            setRenderOptions((current) => ({
                              ...current,
                              prefer_cleaned_track: event.target.checked,
                            }))
                          }
                        />
                        <span>{historyCopy.renderUseCleaned}</span>
                        <TooltipBadge label={uiCopy.renderUseCleanedTooltip} />
                      </span>
                    </label>
                    <label className="option-toggle">
                      <span className="meta-inline">
                        <input
                          type="checkbox"
                          checked={renderOptions.draw_ball_marker}
                          onChange={(event) =>
                            setRenderOptions((current) => ({
                              ...current,
                              draw_ball_marker: event.target.checked,
                            }))
                          }
                        />
                        <span>{historyCopy.renderShowMarker}</span>
                        <TooltipBadge label={uiCopy.renderShowMarkerTooltip} />
                      </span>
                    </label>
                    <label className="option-toggle">
                      <span className="meta-inline">
                        <input
                          type="checkbox"
                          checked={renderOptions.draw_frame_text}
                          onChange={(event) =>
                            setRenderOptions((current) => ({
                              ...current,
                              draw_frame_text: event.target.checked,
                            }))
                          }
                        />
                        <span>{historyCopy.renderShowText}</span>
                        <TooltipBadge label={uiCopy.renderShowTextTooltip} />
                      </span>
                    </label>
                  </div>

                  <div className="render-footer">
                    <button
                      type="button"
                      className="primary-button icon-button"
                      onClick={handleCreateDeliverableRender}
                      disabled={renderBusy}
                    >
                      <PlayIcon className="button-icon" />
                      <span>{historyCopy.renderButton}</span>
                    </button>
                  </div>
                </>
              ) : (
                <div className="empty-state">
                  <strong>{copy.workspace.deliveryEmptyTitle}</strong>
                  <p className="muted">{historyCopy.renderEmpty}</p>
                </div>
              )}

              {historyMessage ? <p className="notice-line">{historyMessage}</p> : null}
            </article>
          </div>
        </section>
      </div>
    );
  }

  if (stage === "history") {
    return (
      <div className="page-stack">
        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <ClockIcon className="section-icon" />
              <div className="title-with-tooltip">
                <p className="eyebrow">{copy.workspace.historyEyebrow}</p>
                <div className="title-inline">
                  <h3>{copy.workspace.historyTitle}</h3>
                  <TooltipBadge label={copy.workspace.historySubtitle} />
                </div>
              </div>
            </div>
          </div>

          <div className="history-filter-row" role="tablist" aria-label="History filter">
            <button
              type="button"
              className={`chip-button ${historyFilter === "baseline" ? "selected" : ""}`}
              onClick={() => setHistoryFilter("baseline")}
            >
              {historyCopy.historyFilterBaseline}
            </button>
            <button
              type="button"
              className={`chip-button ${historyFilter === "deliverable" ? "selected" : ""}`}
              onClick={() => setHistoryFilter("deliverable")}
            >
              {historyCopy.historyFilterDeliverable}
            </button>
            <button
              type="button"
              className={`chip-button ${historyFilter === "failed" ? "selected" : ""}`}
              onClick={() => setHistoryFilter("failed")}
            >
              {historyCopy.historyFilterFailed}
            </button>
          </div>

          {filteredHistoryRuns.length ? (
            <div className="delivery-list">
              {filteredHistoryRuns.map((run) => {
                const StatusIcon = runStatusIcon(run.status);
                const category = historyCategoryForRun(run);
                const modeLabel =
                  category === "deliverable"
                    ? historyCopy.historyModeRender
                    : category === "failed"
                      ? historyCopy.historyFilterFailed
                      : historyCopy.historyModeBaseline;
                return (
                  <article key={run.run_id} className={`delivery-row ${selectedRun?.run_id === run.run_id ? "selected" : ""}`}>
                    <div className="delivery-row-compact">
                      <div className="title-row compact delivery-row-id">
                        <StatusIcon className="section-icon tiny" />
                        <strong>{run.run_id}</strong>
                      </div>
                      <div className="tag-row">
                        <span className="tag">{formatRunStatus(run.status)}</span>
                        <span className="tag">{modeLabel}</span>
                        {run.parent_run_id ? <span className="tag">{`${historyCopy.historySource}: ${run.parent_run_id}`}</span> : null}
                      </div>
                      <p className="delivery-row-time">{formatDateTime(runMoment(run))}</p>
                      <p className="delivery-row-output mono">{run.output_dir}</p>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="empty-state">
              <strong>{copy.workspace.deliveryEmptyTitle}</strong>
              <p className="muted">{copy.workspace.deliveryEmptyBody}</p>
            </div>
          )}

          {historyMessage ? <p className="notice-line">{historyMessage}</p> : null}
        </section>

        <section className="panel history-management-panel">
          <div className="panel-header">
            <div className="title-row">
              <FolderIcon className="section-icon" />
              <div className="title-with-tooltip">
                <p className="eyebrow">{uiCopy.assetGroupsEyebrow}</p>
                <div className="title-inline">
                  <h4>{uiCopy.assetGroupsTitle}</h4>
                  <TooltipBadge label={uiCopy.assetGroupsSubtitle} />
                </div>
              </div>
            </div>
          </div>

          {assetGroups.length ? (
            <div className="asset-group-list">
              {assetGroups.map((group) => (
                <details
                  key={group.group_id}
                  className="asset-group-card"
                  open={group.is_unbound || group.input_video?.path === selectedInputPath}
                >
                  <summary className="asset-group-summary">
                    <div className="asset-group-copy">
                      <strong className="resource-title">{group.is_unbound ? uiCopy.unboundGroupTitle : group.title}</strong>
                      <div className="tag-row">
                        <span className="tag">{`${uiCopy.groupRuns}: ${group.run_count}`}</span>
                        <span className="tag">{`${uiCopy.groupConfigs}: ${group.config_count}`}</span>
                        <span className="tag">{`${uiCopy.groupOutputs}: ${group.output_count}`}</span>
                      </div>
                    </div>
                    <div className="asset-group-meta">
                      <span className="meta-label">{uiCopy.groupLastActivity}</span>
                      <strong>{formatDateTime(group.last_activity_at)}</strong>
                    </div>
                  </summary>

                  <div className="asset-group-grid">
                    <section className="resource-list-card asset-section">
                      <div className="meta-row">
                        <span className="meta-label">{uiCopy.groupSource}</span>
                        <strong>{group.input_video ? 1 : 0}</strong>
                      </div>
                      {group.input_video ? (
                        <details className="asset-entry">
                          <summary className="asset-entry-summary">
                            <div className="asset-entry-copy">
                              <strong className="resource-title">{group.input_video.name}</strong>
                              <div className="tag-row">
                                <span className="tag">{formatVideoSize(group.input_video.size_bytes)}</span>
                              </div>
                            </div>
                            <span className="asset-entry-time">{formatDateTime(group.input_video.modified_at)}</span>
                          </summary>
                          <div className="asset-entry-detail">
                            <p className="muted mono compact-resource-path">{group.input_video.path}</p>
                            <button
                              type="button"
                              className="secondary-button icon-button danger-button"
                              onClick={() => void handleDeleteVideo(group.input_video!.name)}
                              disabled={renderBusy}
                            >
                              <TrashIcon className="button-icon" />
                              <span>{historyCopy.manageDelete}</span>
                            </button>
                          </div>
                        </details>
                      ) : (
                        <p className="muted">{uiCopy.groupNoSource}</p>
                      )}
                    </section>

                    <section className="resource-list-card asset-section">
                      <div className="meta-row">
                        <span className="meta-label">{uiCopy.groupConfigs}</span>
                        <strong>{group.config_count}</strong>
                      </div>
                      {group.configs.length ? (
                        <div className="resource-list">
                          {group.configs.map((config) => (
                            <details key={config.name} className="asset-entry">
                              <summary className="asset-entry-summary">
                                <div className="asset-entry-copy">
                                  <strong className="resource-title">{config.name}</strong>
                                  <div className="tag-row">
                                    <span className="tag" title={scopeTooltipText(inferConfigScope(config.name))}>
                                      {copy.workspace.scopeLabel}: {scopeLabel(copy, inferConfigScope(config.name))}
                                    </span>
                                    <span
                                      className={`tag ${config.postprocess_enabled ? "good" : ""}`}
                                      title={uiCopy.cleanupTooltip}
                                    >
                                      {copy.workspace.cleanup}
                                    </span>
                                    <span
                                      className={`tag ${config.follow_cam_enabled ? "good" : ""}`}
                                      title={uiCopy.followCamTooltip}
                                    >
                                      {copy.workspace.followCam}
                                    </span>
                                  </div>
                                </div>
                                <span className="asset-entry-time">{formatDateTime(config.created_at)}</span>
                              </summary>
                              <div className="asset-entry-detail">
                                <p className="muted mono compact-resource-path">{config.path}</p>
                                {config.output_dir ? <p className="muted mono compact-resource-path">{config.output_dir}</p> : null}
                                <button
                                  type="button"
                                  className="secondary-button icon-button danger-button"
                                  onClick={() => void handleDeleteConfigClick(config.name)}
                                  disabled={renderBusy}
                                >
                                  <TrashIcon className="button-icon" />
                                  <span>{historyCopy.manageDelete}</span>
                                </button>
                              </div>
                            </details>
                          ))}
                        </div>
                      ) : (
                        <p className="muted">{uiCopy.groupNoConfigs}</p>
                      )}
                    </section>

                    <section className="resource-list-card asset-section">
                      <div className="meta-row">
                        <span className="meta-label">{uiCopy.groupOutputs}</span>
                        <strong>{group.output_count}</strong>
                      </div>
                      {group.outputs.length ? (
                        <div className="resource-list">
                          {group.outputs.map((run) => (
                            <details key={run.run_id} className="asset-entry">
                              <summary className="asset-entry-summary">
                                <div className="asset-entry-copy">
                                  <strong className="resource-title">{run.run_id}</strong>
                                  <div className="tag-row">
                                    <span className="tag">{formatRunStatus(run.status)}</span>
                                    <span className="tag">
                                      {run.source === "follow_cam_render" ? historyCopy.historyModeRender : historyCopy.historyModeBaseline}
                                    </span>
                                    {run.parent_run_id ? <span className="tag">{`${historyCopy.historySource}: ${run.parent_run_id}`}</span> : null}
                                  </div>
                                </div>
                                <span className="asset-entry-time">{formatDateTime(runMoment(run))}</span>
                              </summary>
                              <div className="asset-entry-detail">
                                <p className="muted mono compact-resource-path">{run.output_dir}</p>
                                <button
                                  type="button"
                                  className="secondary-button icon-button danger-button"
                                  onClick={() => void handleDeleteRunOutputClick(run.run_id)}
                                  disabled={renderBusy || run.status === "queued" || run.status === "running"}
                                >
                                  <TrashIcon className="button-icon" />
                                  <span>{historyCopy.manageDelete}</span>
                                </button>
                              </div>
                            </details>
                          ))}
                        </div>
                      ) : (
                        <p className="muted">{uiCopy.groupNoOutputs}</p>
                      )}
                    </section>
                  </div>
                </details>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <strong>{historyCopy.manageTitle}</strong>
              <p className="muted">{historyCopy.manageSubtitle}</p>
            </div>
          )}
        </section>

        <ConfirmDeleteDialog
          open={Boolean(pendingDelete)}
          title={deleteDialogTitle}
          message={pendingDelete?.prompt ?? ""}
          targetLabel={deleteDialogTarget}
          targetValue={pendingDelete?.targetName ?? ""}
          phrase={deleteDialogPhrase}
          inputValue={deleteConfirmValue}
          inputLabel={deleteDialogInputLabel}
          cancelLabel={deleteDialogCancel}
          confirmLabel={deleteDialogConfirm}
          busy={renderBusy}
          onInputChange={setDeleteConfirmValue}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void handleConfirmDelete()}
        />
      </div>
    );
  }

  return (
    <div className="page-stack">
      <section className="panel">
        <div className="panel-header">
          <div className="title-row">
            <SparkIcon className="section-icon" />
            <div className="title-with-tooltip">
              <p className="eyebrow">{copy.workspace.focusEyebrow}</p>
              <div className="title-inline">
                <h3>{copy.workspace.focusTitle}</h3>
                <TooltipBadge label={copy.workspace.focusSubtitle} />
              </div>
            </div>
          </div>
        </div>

        {aiRuns.length ? (
          <div className="focus-stack">
            <label className="form-label">
              <span className="meta-label">{copy.workspace.focusRun}</span>
              <select
                value={aiSelectedRun?.run_id ?? ""}
                onChange={(event) => {
                  const nextRun = aiRuns.find((item) => item.run_id === event.target.value);
                  if (nextRun) {
                    onSelectRun(nextRun);
                  }
                }}
              >
                {aiRuns.map((run) => (
                  <option key={run.run_id} value={run.run_id}>
                    {`${formatDateTime(runMoment(run))} | ${run.run_id} | ${formatRunStatus(run.status)}`}
                  </option>
                ))}
              </select>
            </label>

            <article className="summary-card spotlight-card icon-card">
              <ActivityIcon className="section-icon" />
              <span className="meta-inline">
                <span className="meta-label">{copy.workspace.currentFocus}</span>
                <TooltipBadge label={copy.workspace.focusSubtitle} />
              </span>
              <strong>{aiSelectedRun?.run_id ?? copy.common.notAvailable}</strong>
              <p className="muted">
                {formatRunStatus(aiSelectedRun?.status ?? "queued")} | {aiSelectedRun?.config_name ?? copy.common.notAvailable}
              </p>
            </article>

            <div className="mini-stat-grid">
              <article className="mini-stat icon-card">
                <CheckIcon className="section-icon" />
                <span className="meta-inline">
                  <span className="meta-label">{copy.workspace.detected}</span>
                  <TooltipBadge label={copy.workspace.focusSubtitle} />
                </span>
                <strong>{readNumber(stats, "detected")}</strong>
              </article>
              <article className="mini-stat icon-card">
                <ActivityIcon className="section-icon" />
                <span className="meta-inline">
                  <span className="meta-label">{copy.workspace.lost}</span>
                  <TooltipBadge label={copy.workspace.focusSubtitle} />
                </span>
                <strong>{readNumber(stats, "lost")}</strong>
              </article>
              <article className="mini-stat icon-card">
                <FileIcon className="section-icon" />
                <span className="meta-inline">
                  <span className="meta-label">{copy.workspace.artifacts}</span>
                  <TooltipBadge label={copy.workspace.evidenceSubtitle} />
                </span>
                <strong>{aiSelectedRun?.artifacts.length ?? 0}</strong>
              </article>
              <article className="mini-stat icon-card">
                <ClockIcon className="section-icon" />
                <span className="meta-inline">
                  <span className="meta-label">{copy.workspace.lastEvent}</span>
                  <TooltipBadge label={copy.common.refreshHint} />
                </span>
                <strong>{formatDateTime(aiSelectedRun ? runMoment(aiSelectedRun) : null)}</strong>
              </article>
            </div>

            <div className="detail-grid">
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.inputVideo}</p>
                <p className="mono">{aiSelectedRun?.input_video ?? copy.common.notAvailable}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.outputDirectory}</p>
                <p className="mono">{aiSelectedRun?.output_dir ?? copy.common.notAvailable}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.created}</p>
                <p>{formatDateTime(aiSelectedRun?.created_at)}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.completed}</p>
                <p>{aiCompletedMoment ? formatDateTime(aiCompletedMoment) : copy.common.stillRunning}</p>
              </div>
            </div>

            {aiSelectedRun?.error ? <div className="error-banner inline">{aiSelectedRun.error}</div> : null}
          </div>
        ) : (
          <div className="empty-state">
            <strong>{copy.workspace.noFocusTitle}</strong>
            <p className="muted">{copy.workspace.noFocusBody}</p>
          </div>
        )}
      </section>
    </div>
  );
}
