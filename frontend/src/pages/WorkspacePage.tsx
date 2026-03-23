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
import { FieldSetupCard } from "../components/FieldSetupCard";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, FieldPreview, FieldSuggestion, InputCatalog, RunRecord } from "../lib/types";

export type WorkspaceStage = "baseline" | "ai" | "delivery";

interface WorkspacePageProps {
  stage: WorkspaceStage;
  inputCatalog: InputCatalog;
  configs: ConfigListItem[];
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

function supportsFollowCamRender(run: RunRecord): boolean {
  return (
    run.status === "completed" &&
    Boolean(run.input_video) &&
    Boolean(run.config_name || run.config_path) &&
    run.artifacts.some((artifact) => artifact.name === "ball_track.csv" || artifact.name === "ball_track.cleaned.csv")
  );
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
            renderEyebrow: "Standalone deliverable task",
            renderTitle: "Create a 16:9 deliverable from history",
            renderSubtitle: "This does not rerun detector or baseline. It reuses the selected completed run and renders a clean deliverable.",
            renderSelect: "Source run",
            renderReady: "Runs ready for deliverable render",
            renderOutput: "Output",
            renderSource: "Source",
            renderClip: "Clip",
            renderUseCleaned: "Prefer cleaned track CSV",
            renderShowMarker: "Show ball marker",
            renderShowText: "Show frame text / annotation",
            renderDefaults: "Final deliverables default to a clean 16:9 frame with marker and annotation turned off.",
            renderButton: "Start 16:9 deliverable render",
            renderEmpty: "Complete at least one run with track CSVs before starting a standalone 16:9 render.",
            renderCreated: "Standalone deliverable task created",
            renderConfirm: "Start a new standalone 16:9 render task?",
            historySource: "Source run",
            historyModeBaseline: "Baseline",
            historyModeRender: "Deliverable",
            historyModeScan: "Scanned",
            manageEyebrow: "File management",
            manageTitle: "Video and config cleanup",
            manageSubtitle: "Remove videos and YAML configs you no longer need. Active runs stay protected.",
            manageSummary: "Clean up unused videos and YAML configs",
            manageVideos: "Input videos",
            manageConfigs: "Config files",
            manageDelete: "Delete",
            manageNoVideos: "No videos to manage.",
            manageNoConfigs: "No configs to manage.",
            manageDeleted: "Deleted",
            manageDeleteVideoConfirm: "Delete this input video?",
            manageDeleteConfigConfirm: "Delete this config file?",
          },
    [language],
  );
  const [renderRunId, setRenderRunId] = useState("");
  const [renderBusy, setRenderBusy] = useState(false);
  const [historyMessage, setHistoryMessage] = useState<string | null>(null);
  const [renderOptions, setRenderOptions] = useState({
    prefer_cleaned_track: true,
    draw_ball_marker: false,
    draw_frame_text: false,
  });
  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = configs.find((item) => item.name === selectedConfigName) ?? null;
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
  const activeRenderRun =
    renderableRuns.find((run) => run.run_id === renderRunId) ??
    (selectedRun && renderableRuns.some((run) => run.run_id === selectedRun.run_id) ? selectedRun : renderableRuns[0] ?? null);

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

  async function handleDeleteVideo(name: string) {
    if (typeof window !== "undefined" && !window.confirm(historyCopy.manageDeleteVideoConfirm)) {
      return;
    }
    setRenderBusy(true);
    setHistoryMessage(null);
    try {
      await onDeleteInputVideo(name);
      setHistoryMessage(`${historyCopy.manageDeleted}: ${name}`);
    } catch (caughtError) {
      setHistoryMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setRenderBusy(false);
    }
  }

  async function handleDeleteConfigClick(name: string) {
    if (typeof window !== "undefined" && !window.confirm(historyCopy.manageDeleteConfigConfirm)) {
      return;
    }
    setRenderBusy(true);
    setHistoryMessage(null);
    try {
      await onDeleteConfig(name);
      setHistoryMessage(`${historyCopy.manageDeleted}: ${name}`);
    } catch (caughtError) {
      setHistoryMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setRenderBusy(false);
    }
  }

  if (stage === "baseline") {
    return (
      <div className="page-stack">
        <section className="panel workflow-panel">
          <div className="panel-header">
            <div className="title-row">
              <PlayIcon className="section-icon" />
              <div>
                <p className="eyebrow">{copy.workspace.selectEyebrow}</p>
                <h3>{copy.workspace.selectTitle}</h3>
                <p className="muted">{copy.workspace.selectSubtitle}</p>
              </div>
            </div>
          </div>

          <div className="step-form-grid">
            <section className="step-form-section">
              <div className="section-intro title-row">
                <VideoIcon className="section-icon" />
                <div>
                  <h4>{copy.workspace.inputTitle}</h4>
                  <p className="muted">{copy.workspace.inputSubtitle}</p>
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
                    <strong>{selectedVideo.name}</strong>
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
                <div>
                  <h4>{copy.workspace.baselineTitle}</h4>
                  <p className="muted">{copy.workspace.baselineSubtitle}</p>
                </div>
              </div>

              <label className="form-label">
                <span className="meta-label">{copy.workspace.selectedBaseline}</span>
                <select value={selectedConfigName} onChange={(event) => onSelectConfig(event.target.value)} disabled={!configs.length}>
                  {configs.length ? null : <option value="">{copy.workspace.noBaselineTitle}</option>}
                  {configs.map((config) => (
                    <option key={config.name} value={config.name}>
                      {config.name}
                    </option>
                  ))}
                </select>
              </label>

              {selectedConfig ? (
                <article className="selection-summary-card compact-selection-card">
                  <strong>{selectedConfig.name}</strong>
                  <div className="tag-row">
                    <span className="tag">
                      {copy.workspace.scopeLabel}: {scopeLabel(copy, selectedScope)}
                    </span>
                    <span className={`tag ${selectedConfig.postprocess_enabled ? "good" : ""}`}>{copy.workspace.cleanup}</span>
                    <span className={`tag ${selectedConfig.follow_cam_enabled ? "good" : ""}`}>{copy.workspace.followCam}</span>
                  </div>
                  <p className="muted">{hasRunHistoryForInput ? copy.workspace.baselineReuseHint : copy.workspace.baselineDefaultHint}</p>
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
            <summary>{copy.workspace.selectionDetails}</summary>
            <p className="muted">{copy.workspace.selectionDetailsSubtitle}</p>
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

  if (stage === "delivery") {
    return (
      <div className="page-stack">
        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <FileIcon className="section-icon" />
              <div>
                <p className="eyebrow">{copy.workspace.deliveryEyebrow}</p>
                <h3>{copy.workspace.deliveryTitle}</h3>
                <p className="muted">{copy.workspace.deliverySubtitle}</p>
              </div>
            </div>
          </div>

          <div className="history-grid">
            <article className="assistant-card primary history-action-card">
              <div className="title-row">
                <VideoIcon className="section-icon" />
                <div>
                  <p className="eyebrow">{historyCopy.renderEyebrow}</p>
                  <h4>{historyCopy.renderTitle}</h4>
                  <p className="muted compact-lead">{historyCopy.renderSubtitle}</p>
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
                    <article className="summary-card compact-summary-card">
                      <p className="meta-label">{historyCopy.renderSource}</p>
                      <strong>{activeRenderRun?.run_id ?? copy.common.notAvailable}</strong>
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

                  <div className="option-toggle-grid compact-toggle-grid">
                    <label className="option-toggle">
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
                    </label>
                    <label className="option-toggle">
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
                    </label>
                    <label className="option-toggle">
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
                    </label>
                  </div>

                  <div className="render-footer">
                    <p className="notice-line subtle compact-notice">{historyCopy.renderDefaults}</p>
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

            <details className="panel resource-panel resource-panel-collapsed">
              <summary className="resource-summary">
                <div className="title-row">
                  <FolderIcon className="section-icon" />
                  <div>
                    <p className="eyebrow">{historyCopy.manageEyebrow}</p>
                    <h4>{historyCopy.manageTitle}</h4>
                    <p className="muted compact-lead">{historyCopy.manageSummary}</p>
                  </div>
                </div>
              </summary>

              <p className="muted">{historyCopy.manageSubtitle}</p>

              <div className="resource-grid">
                <section className="resource-list-card">
                  <div className="meta-row">
                    <span className="meta-label">{historyCopy.manageVideos}</span>
                    <strong>{inputCatalog.videos.length}</strong>
                  </div>
                  {inputCatalog.videos.length ? (
                    <div className="resource-list">
                      {inputCatalog.videos.map((video) => (
                        <article key={video.path} className="resource-row">
                          <div className="resource-copy">
                            <strong>{video.name}</strong>
                            <p className="muted mono">{formatVideoSize(video.size_bytes)} | {formatDateTime(video.modified_at)}</p>
                          </div>
                          <button
                            type="button"
                            className="secondary-button icon-button danger-button"
                            onClick={() => void handleDeleteVideo(video.name)}
                            disabled={renderBusy}
                          >
                            <TrashIcon className="button-icon" />
                            <span>{historyCopy.manageDelete}</span>
                          </button>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="muted">{historyCopy.manageNoVideos}</p>
                  )}
                </section>

                <section className="resource-list-card">
                  <div className="meta-row">
                    <span className="meta-label">{historyCopy.manageConfigs}</span>
                    <strong>{configs.length}</strong>
                  </div>
                  {configs.length ? (
                    <div className="resource-list">
                      {configs.map((config) => (
                        <article key={config.name} className="resource-row">
                          <div className="resource-copy">
                            <strong>{config.name}</strong>
                            <div className="tag-row">
                              <span className={`tag ${config.postprocess_enabled ? "good" : ""}`}>{copy.workspace.cleanup}</span>
                              <span className={`tag ${config.follow_cam_enabled ? "good" : ""}`}>{copy.workspace.followCam}</span>
                            </div>
                          </div>
                          <button
                            type="button"
                            className="secondary-button icon-button danger-button"
                            onClick={() => void handleDeleteConfigClick(config.name)}
                            disabled={renderBusy}
                          >
                            <TrashIcon className="button-icon" />
                            <span>{historyCopy.manageDelete}</span>
                          </button>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="muted">{historyCopy.manageNoConfigs}</p>
                  )}
                </section>
              </div>
            </details>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <ClockIcon className="section-icon" />
              <div>
                <p className="eyebrow">{copy.workspace.queueEyebrow}</p>
                <h3>{copy.workspace.deliveryTitle}</h3>
                <p className="muted">{copy.workspace.deliverySubtitle}</p>
              </div>
            </div>
          </div>

          {runs.length ? (
            <div className="delivery-list">
              {runs.map((run) => {
                const StatusIcon = runStatusIcon(run.status);
                const modeLabel =
                  run.source === "follow_cam_render"
                    ? historyCopy.historyModeRender
                    : run.source === "filesystem_scan"
                      ? historyCopy.historyModeScan
                      : historyCopy.historyModeBaseline;
                return (
                  <article key={run.run_id} className={`delivery-row ${activeRenderRun?.run_id === run.run_id ? "selected" : ""}`}>
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
        </section>
      </div>
    );
  }

  return (
    <div className="page-stack">
      <section className="panel">
        <div className="panel-header">
          <div className="title-row">
            <SparkIcon className="section-icon" />
            <div>
              <p className="eyebrow">{copy.workspace.focusEyebrow}</p>
              <h3>{copy.workspace.focusTitle}</h3>
              <p className="muted">{copy.workspace.focusSubtitle}</p>
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
              <p className="meta-label">{copy.workspace.currentFocus}</p>
              <strong>{aiSelectedRun?.run_id ?? copy.common.notAvailable}</strong>
              <p className="muted">
                {formatRunStatus(aiSelectedRun?.status ?? "queued")} | {aiSelectedRun?.config_name ?? copy.common.notAvailable}
              </p>
            </article>

            <div className="mini-stat-grid">
              <article className="mini-stat icon-card">
                <CheckIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.detected}</p>
                <strong>{readNumber(stats, "detected")}</strong>
                <p className="muted">{copy.workspace.focusSubtitle}</p>
              </article>
              <article className="mini-stat icon-card">
                <ActivityIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.lost}</p>
                <strong>{readNumber(stats, "lost")}</strong>
                <p className="muted">{copy.workspace.focusSubtitle}</p>
              </article>
              <article className="mini-stat icon-card">
                <FileIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.artifacts}</p>
                <strong>{aiSelectedRun?.artifacts.length ?? 0}</strong>
                <p className="muted">{copy.workspace.evidenceSubtitle}</p>
              </article>
              <article className="mini-stat icon-card">
                <ClockIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.lastEvent}</p>
                <strong>{formatDateTime(aiSelectedRun ? runMoment(aiSelectedRun) : null)}</strong>
                <p className="muted">{copy.common.refreshHint}</p>
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
                <p>{aiSelectedRun?.completed_at ? formatDateTime(aiSelectedRun.completed_at) : copy.common.stillRunning}</p>
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
