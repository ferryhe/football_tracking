import {
  ActivityIcon,
  CheckIcon,
  ClockIcon,
  FileIcon,
  LayersIcon,
  PlayIcon,
  SparkIcon,
  VideoIcon,
} from "../components/Icons";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, InputCatalog, RunRecord } from "../lib/types";

export type WorkspaceStage = "baseline" | "ai" | "delivery" | "history";

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
  onSelectRun: (run: RunRecord) => void;
  onSelectInput: (path: string) => void;
  onSelectConfig: (name: string) => void;
  onStartBaselineRun: () => Promise<void>;
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
  onSelectRun,
  onSelectInput,
  onSelectConfig,
  onStartBaselineRun,
}: WorkspacePageProps) {
  const { copy, formatDateTime, formatRunStatus } = useI18n();
  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = configs.find((item) => item.name === selectedConfigName) ?? null;
  const selectedScope = inferConfigScope(selectedConfig?.name);
  const stats = getTrackStats(selectedRun);
  const canLaunch = !loading && !launching && Boolean(selectedInputPath) && Boolean(selectedConfigName);
  const deliveryRuns = runs.filter((run) => run.status === "completed");

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

              <div className="info-block compact-block">
                <p className="meta-label">{copy.workspace.inputDirectory}</p>
                <p className="mono">{inputCatalog.root_dir || copy.common.unavailable}</p>
              </div>

              {selectedVideo ? (
                <div className="selection-summary-card">
                  <strong>{selectedVideo.name}</strong>
                  <p className="muted mono">{selectedVideo.path}</p>
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
                <article className="selection-summary-card config-summary-card">
                  <p className="meta-label">{copy.workspace.baselineSummaryTitle}</p>
                  <strong>{selectedConfig.name}</strong>
                  <div className="tag-row">
                    <span className="tag">
                      {copy.workspace.scopeLabel}: {scopeLabel(copy, selectedScope)}
                    </span>
                    <span className={`tag ${selectedConfig.postprocess_enabled ? "good" : ""}`}>{copy.workspace.cleanup}</span>
                    <span className={`tag ${selectedConfig.follow_cam_enabled ? "good" : ""}`}>{copy.workspace.followCam}</span>
                  </div>
                  <p className="muted mono">
                    {selectedConfig.detector_model_path ? formatPathTail(selectedConfig.detector_model_path) : copy.common.notAvailable}
                  </p>
                  <p className="muted mono">{selectedConfig.output_dir ?? copy.common.unavailable}</p>
                </article>
              ) : (
                <div className="empty-state">
                  <strong>{copy.workspace.noBaselineTitle}</strong>
                  <p className="muted">{copy.workspace.noBaselineBody}</p>
                </div>
              )}
            </section>
          </div>

          <div className="step-footer">
            <div className="info-block compact-block">
              <p className="meta-label">{copy.workspace.scopeLabel}</p>
              <p className="muted">{copy.workspace.baselineLoopHint}</p>
            </div>

            <div className="launch-actions">
              <p className="muted">{copy.workspace.launchCopy}</p>
              <button type="button" className="primary-button icon-button" onClick={onStartBaselineRun} disabled={!canLaunch}>
                <PlayIcon className="button-icon" />
                <span>{launching ? copy.workspace.launchStarting : copy.workspace.launchButton}</span>
              </button>
              {launchMessage ? <p className="notice-line">{launchMessage}</p> : null}
            </div>
          </div>
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

          {deliveryRuns.length ? (
            <div className="delivery-list">
              {deliveryRuns.map((run) => (
                <article key={run.run_id} className="delivery-row">
                  <div className="delivery-row-head">
                    <div className="title-row compact">
                      <CheckIcon className="section-icon tiny" />
                      <strong>{run.run_id}</strong>
                    </div>
                    <p className="muted mono">{run.config_name ?? copy.common.notAvailable}</p>
                  </div>

                  <div className="delivery-row-meta">
                    <div className="detail-block compact-detail">
                      <p className="meta-label">{copy.workspace.deliveryRanAt}</p>
                      <p>{formatDateTime(run.completed_at ?? run.started_at ?? run.created_at)}</p>
                    </div>
                    <div className="detail-block compact-detail">
                      <p className="meta-label">{copy.workspace.deliveryResultFolder}</p>
                      <p className="mono">{run.output_dir}</p>
                    </div>
                  </div>
                </article>
              ))}
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

  if (stage === "history") {
    return (
      <div className="page-stack">
        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <ClockIcon className="section-icon" />
              <div>
                <p className="eyebrow">{copy.workspace.queueEyebrow}</p>
                <h3>{copy.workspace.queueTitle}</h3>
                <p className="muted">{copy.workspace.queueSubtitle}</p>
              </div>
            </div>
          </div>

          {selectedRun ? (
            <div className="info-block compact-block">
              <p className="meta-label">{copy.workspace.currentFocus}</p>
              <strong>{selectedRun.run_id}</strong>
              <p className="muted mono">{selectedRun.config_name ?? copy.common.notAvailable}</p>
            </div>
          ) : null}

          {runs.length ? (
            <div className="run-list compact-list">
              {runs.map((run) => {
                const StatusIcon = runStatusIcon(run.status);
                return (
                  <button
                    type="button"
                    key={run.run_id}
                    className={`run-row ${selectedRun?.run_id === run.run_id ? "selected" : ""}`}
                    onClick={() => onSelectRun(run)}
                  >
                    <div className="run-row-lead">
                      <div className={`run-row-icon-shell ${run.status}`}>
                        <StatusIcon className="section-icon tiny" />
                      </div>
                      <div className="run-row-copy">
                        <strong>{run.run_id}</strong>
                        <p className="muted mono">{run.config_name ?? formatPathTail(run.output_dir)}</p>
                        <div className="run-chip-row">
                          <span className="tag">{formatDateTime(run.created_at)}</span>
                          <span className="tag">{formatRunStatus(run.status)}</span>
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="empty-state">
              <strong>{copy.workspace.noRunsTitle}</strong>
              <p className="muted">{copy.workspace.noRunsBody}</p>
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

        {selectedRun ? (
          <div className="focus-stack">
            <article className="summary-card spotlight-card icon-card">
              <ActivityIcon className="section-icon" />
              <p className="meta-label">{copy.workspace.currentFocus}</p>
              <strong>{selectedRun.run_id}</strong>
              <p className="muted">
                {formatRunStatus(selectedRun.status)} | {selectedRun.config_name ?? copy.common.notAvailable}
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
                <strong>{selectedRun.artifacts.length}</strong>
                <p className="muted">{copy.workspace.evidenceSubtitle}</p>
              </article>
              <article className="mini-stat icon-card">
                <ClockIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.lastEvent}</p>
                <strong>{formatDateTime(selectedRun.completed_at ?? selectedRun.started_at ?? selectedRun.created_at)}</strong>
                <p className="muted">{copy.common.refreshHint}</p>
              </article>
            </div>

            <div className="detail-grid">
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.inputVideo}</p>
                <p className="mono">{selectedRun.input_video ?? copy.common.notAvailable}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.outputDirectory}</p>
                <p className="mono">{selectedRun.output_dir}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.created}</p>
                <p>{formatDateTime(selectedRun.created_at)}</p>
              </div>
              <div className="detail-block">
                <p className="meta-label">{copy.workspace.completed}</p>
                <p>{selectedRun.completed_at ? formatDateTime(selectedRun.completed_at) : copy.common.stillRunning}</p>
              </div>
            </div>

            {selectedRun.error ? <div className="error-banner inline">{selectedRun.error}</div> : null}
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
