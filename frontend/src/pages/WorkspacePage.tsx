import { ArtifactList } from "../components/ArtifactList";
import {
  ActivityIcon,
  CheckIcon,
  ClockIcon,
  FileIcon,
  FolderIcon,
  LayersIcon,
  PlayIcon,
  SparkIcon,
  VideoIcon,
} from "../components/Icons";
import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, InputCatalog, RunRecord } from "../lib/types";

interface WorkspacePageProps {
  inputCatalog: InputCatalog;
  configs: ConfigListItem[];
  runs: RunRecord[];
  selectedRun: RunRecord | null;
  selectedInputPath: string;
  selectedConfigName: string;
  onSelectRun: (run: RunRecord) => void;
  onSelectInput: (path: string) => void;
  onSelectConfig: (name: string) => void;
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

export function WorkspacePage({
  inputCatalog,
  configs,
  runs,
  selectedRun,
  selectedInputPath,
  selectedConfigName,
  onSelectRun,
  onSelectInput,
  onSelectConfig,
}: WorkspacePageProps) {
  const { copy, formatDateTime, formatRunStatus } = useI18n();
  const stats = getTrackStats(selectedRun);
  const activeRun = runs.find((item) => item.status === "running" || item.status === "queued") ?? null;
  const focusedRun = selectedRun ?? activeRun;

  return (
    <div className="page-stack">
      <section className="panel workflow-panel">
        <div className="panel-header">
          <div className="title-row">
            <FolderIcon className="section-icon" />
            <div>
              <p className="eyebrow">{copy.workspace.selectEyebrow}</p>
              <h3>{copy.workspace.selectTitle}</h3>
              <p className="muted">{copy.workspace.selectSubtitle}</p>
            </div>
          </div>
        </div>

        <div className="selector-grid">
          <section className="selector-column">
            <div className="section-intro title-row">
              <VideoIcon className="section-icon" />
              <div>
                <h4>{copy.workspace.inputTitle}</h4>
                <p className="muted">{copy.workspace.inputSubtitle}</p>
              </div>
            </div>
            <div className="info-block">
              <p className="meta-label">{copy.workspace.inputDirectory}</p>
              <p className="mono">{inputCatalog.root_dir || copy.common.unavailable}</p>
            </div>

            {inputCatalog.videos.length ? (
              <div className="choice-grid">
                {inputCatalog.videos.map((video) => {
                  const isSelected = video.path === selectedInputPath;
                  return (
                    <button
                      type="button"
                      key={video.path}
                      className={`choice-card ${isSelected ? "selected" : ""}`}
                      onClick={() => onSelectInput(video.path)}
                    >
                      <div className="choice-card-header">
                        <strong>{video.name}</strong>
                        {isSelected ? <span className="choice-badge">{copy.common.selected}</span> : null}
                      </div>
                      <p className="muted mono">{formatPathTail(video.path)}</p>
                      <div className="choice-meta">
                        <span>{formatVideoSize(video.size_bytes)}</span>
                        <span>{formatDateTime(video.modified_at)}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">
                <strong>{copy.workspace.noInputTitle}</strong>
                <p className="muted">{copy.workspace.noInputBody}</p>
              </div>
            )}
          </section>

          <section className="selector-column">
            <div className="section-intro title-row">
              <LayersIcon className="section-icon" />
              <div>
                <h4>{copy.workspace.baselineTitle}</h4>
                <p className="muted">{copy.workspace.baselineSubtitle}</p>
              </div>
            </div>

            {configs.length ? (
              <div className="choice-grid">
                {configs.map((config) => {
                  const isSelected = config.name === selectedConfigName;
                  return (
                    <button
                      type="button"
                      key={config.name}
                      className={`choice-card ${isSelected ? "selected" : ""}`}
                      onClick={() => onSelectConfig(config.name)}
                    >
                      <div className="choice-card-header">
                        <strong>{config.name}</strong>
                        {isSelected ? <span className="choice-badge">{copy.common.baseline}</span> : null}
                      </div>
                      <p className="muted mono">{config.output_dir ?? copy.common.unavailable}</p>
                      <div className="tag-row">
                        <span className={`tag ${config.postprocess_enabled ? "good" : ""}`}>{copy.workspace.cleanup}</span>
                        <span className={`tag ${config.follow_cam_enabled ? "good" : ""}`}>{copy.workspace.followCam}</span>
                        <span className="tag">{formatPathTail(config.detector_model_path) || copy.common.notAvailable}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">
                <strong>{copy.workspace.noBaselineTitle}</strong>
                <p className="muted">{copy.workspace.noBaselineBody}</p>
              </div>
            )}
          </section>
        </div>

        <p className="notice-line subtle">{copy.workspace.launchHint}</p>
      </section>

      <div className="content-grid two-up">
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
          {focusedRun ? (
            <div className="focus-stack">
              <article className="summary-card spotlight-card icon-card">
                <ActivityIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.currentFocus}</p>
                <strong>{focusedRun.run_id}</strong>
                <p className="muted">
                  {formatRunStatus(focusedRun.status)} | {focusedRun.config_name ?? copy.common.notAvailable}
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
                  <strong>{focusedRun.artifacts.length}</strong>
                  <p className="muted">{copy.workspace.evidenceSubtitle}</p>
                </article>
                <article className="mini-stat icon-card">
                  <ClockIcon className="section-icon" />
                  <p className="meta-label">{copy.workspace.lastEvent}</p>
                  <strong>{formatDateTime(focusedRun.completed_at ?? focusedRun.started_at ?? focusedRun.created_at)}</strong>
                  <p className="muted">{copy.common.refreshHint}</p>
                </article>
              </div>

              <div className="detail-grid">
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.inputVideo}</p>
                  <p className="mono">{focusedRun.input_video ?? copy.common.notAvailable}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.outputDirectory}</p>
                  <p className="mono">{focusedRun.output_dir}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.created}</p>
                  <p>{formatDateTime(focusedRun.created_at)}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">{copy.workspace.completed}</p>
                  <p>{focusedRun.completed_at ? formatDateTime(focusedRun.completed_at) : copy.common.stillRunning}</p>
                </div>
              </div>

              {focusedRun.notes ? (
                <div className="info-block">
                  <p className="meta-label">{copy.workspace.runNotes}</p>
                  <p className="muted">{focusedRun.notes}</p>
                </div>
              ) : null}

              {focusedRun.error ? <div className="error-banner inline">{focusedRun.error}</div> : null}
            </div>
          ) : (
            <div className="empty-state">
              <strong>{copy.workspace.noFocusTitle}</strong>
              <p className="muted">{copy.workspace.noFocusBody}</p>
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panel-header">
            <div className="title-row">
              <ActivityIcon className="section-icon" />
              <div>
                <p className="eyebrow">{copy.workspace.queueEyebrow}</p>
                <h3>{copy.workspace.queueTitle}</h3>
                <p className="muted">{copy.workspace.queueSubtitle}</p>
              </div>
            </div>
          </div>
          {activeRun ? (
            <div className="summary-card icon-card">
              <PlayIcon className="section-icon" />
              <p className="meta-label">{copy.workspace.runningNow}</p>
              <strong>{activeRun.run_id}</strong>
              <p className="muted">{formatRunStatus(activeRun.status)}</p>
            </div>
          ) : (
            <div className="summary-card icon-card">
              <ClockIcon className="section-icon" />
              <p className="meta-label">{copy.workspace.runningNow}</p>
              <strong>{copy.common.idle}</strong>
              <p className="muted">{copy.common.noActiveRun}</p>
            </div>
          )}

          <div className="run-list compact-list">
            {runs.length ? (
              runs.map((run) => (
                <button
                  type="button"
                  key={run.run_id}
                  className={`run-row ${selectedRun?.run_id === run.run_id ? "selected" : ""}`}
                  onClick={() => onSelectRun(run)}
                >
                  <div className="run-row-copy">
                    <strong>{run.run_id}</strong>
                    <p className="muted mono">{run.config_name ?? formatPathTail(run.output_dir)}</p>
                    <div className="run-meta">
                      <span>{formatDateTime(run.created_at)}</span>
                      <span>{run.artifacts.length}</span>
                    </div>
                  </div>
                  <span className={`status-dot ${run.status}`}>{formatRunStatus(run.status)}</span>
                </button>
              ))
            ) : (
              <div className="empty-state">
                <strong>{copy.workspace.noRunsTitle}</strong>
                <p className="muted">{copy.workspace.noRunsBody}</p>
              </div>
            )}
          </div>
        </section>
      </div>

      <section className="panel evidence-panel">
        <div className="panel-header">
          <div className="title-row">
            <FileIcon className="section-icon" />
            <div>
              <p className="eyebrow">{copy.workspace.evidenceEyebrow}</p>
              <h3>{copy.workspace.evidenceTitle}</h3>
              <p className="muted">{copy.workspace.evidenceSubtitle}</p>
            </div>
          </div>
        </div>

        {selectedRun ? (
          <>
            <div className="summary-grid evidence-summary-grid">
              <div className="summary-card icon-card">
                <ActivityIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.run}</p>
                <strong>{selectedRun.run_id}</strong>
                <p className="muted mono">{selectedRun.config_name ?? copy.common.notAvailable}</p>
              </div>
              <div className="summary-card icon-card">
                <LayersIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.modulesEnabled}</p>
                <div className="tag-row">
                  <span className={`tag ${selectedRun.modules_enabled.postprocess ? "good" : ""}`}>{copy.workspace.cleanup}</span>
                  <span className={`tag ${selectedRun.modules_enabled.follow_cam ? "good" : ""}`}>{copy.workspace.followCam}</span>
                </div>
                <p className="muted">{copy.workspace.evidenceSubtitle}</p>
              </div>
              <div className="summary-card icon-card">
                <FileIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.artifactsReady}</p>
                <strong>{selectedRun.artifacts.length}</strong>
                <p className="muted">{copy.workspace.evidenceSubtitle}</p>
              </div>
              <div className="summary-card icon-card">
                <FolderIcon className="section-icon" />
                <p className="meta-label">{copy.workspace.outputFolder}</p>
                <strong>{formatPathTail(selectedRun.output_dir)}</strong>
                <p className="muted mono">{selectedRun.output_dir}</p>
              </div>
            </div>

            <div className="video-grid">
              <div className="video-card">
                <p className="meta-label">{copy.workspace.followCamVideo}</p>
                <video controls src={api.artifactUrl(selectedRun.run_id, "follow_cam.mp4")} />
              </div>
              <div className="video-card">
                <p className="meta-label">{copy.workspace.cleanedVideo}</p>
                <video controls src={api.artifactUrl(selectedRun.run_id, "annotated.cleaned.mp4")} />
              </div>
            </div>

            <ArtifactList
              run={selectedRun}
              preferredNames={[
                "follow_cam.mp4",
                "annotated.cleaned.mp4",
                "ball_track.cleaned.csv",
                "cleanup_report.json",
                "follow_cam_report.json",
              ]}
            />
          </>
        ) : (
          <div className="empty-state">
            <strong>{copy.workspace.noEvidenceTitle}</strong>
            <p className="muted">{copy.workspace.noEvidenceBody}</p>
          </div>
        )}
      </section>
    </div>
  );
}
