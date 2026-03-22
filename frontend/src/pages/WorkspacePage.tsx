import { useMemo, useState } from "react";

import { ArtifactList } from "../components/ArtifactList";
import { api } from "../lib/api";
import type { ConfigListItem, HealthResponse, InputCatalog, RunRecord } from "../lib/types";

interface WorkspacePageProps {
  health: HealthResponse | null;
  inputCatalog: InputCatalog;
  configs: ConfigListItem[];
  runs: RunRecord[];
  selectedRun: RunRecord | null;
  selectedInputPath: string;
  selectedConfigName: string;
  loading: boolean;
  onSelectRun: (run: RunRecord) => void;
  onSelectInput: (path: string) => void;
  onSelectConfig: (name: string) => void;
  onStartBaselineRun: (notes: string) => Promise<void>;
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

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "n/a";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatPathTail(path: string | null | undefined): string {
  if (!path) {
    return "n/a";
  }
  const pieces = path.split(/[\\/]/).filter(Boolean);
  return pieces.length ? pieces[pieces.length - 1] : path;
}

function formatStatusLabel(status: string): string {
  return status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, " ");
}

export function WorkspacePage({
  health,
  inputCatalog,
  configs,
  runs,
  selectedRun,
  selectedInputPath,
  selectedConfigName,
  loading,
  onSelectRun,
  onSelectInput,
  onSelectConfig,
  onStartBaselineRun,
}: WorkspacePageProps) {
  const [launching, setLaunching] = useState(false);
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
  const selectedVideo = useMemo(
    () => inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null,
    [inputCatalog.videos, selectedInputPath],
  );
  const selectedConfig = useMemo(
    () => configs.find((item) => item.name === selectedConfigName) ?? null,
    [configs, selectedConfigName],
  );
  const stats = getTrackStats(selectedRun);
  const activeRun = runs.find((item) => item.status === "running" || item.status === "queued") ?? null;
  const focusedRun = selectedRun ?? activeRun;

  async function handleStartRun() {
    setLaunching(true);
    setLaunchMessage(null);
    try {
      await onStartBaselineRun(`Workspace baseline run for ${selectedVideo?.name ?? "selected input"}`);
      setLaunchMessage("Run started.");
    } catch (caughtError) {
      setLaunchMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setLaunching(false);
    }
  }

  return (
    <div className="page-stack">
      <section className="hero-panel compact workspace-hero">
        <div>
          <p className="eyebrow">Operator Flow</p>
          <h3>Keep the loop visible without turning it into a dashboard wall</h3>
          <p className="muted">
            One selected input, one baseline lane, one evidence bundle, one AI console. Everything else stays secondary.
          </p>
        </div>
        <div className="stats-row">
          <article className="stat-card">
            <p className="meta-label">Input videos</p>
            <strong>{inputCatalog.videos.length}</strong>
            <p className="muted">Found under the input folder</p>
          </article>
          <article className="stat-card">
            <p className="meta-label">System pulse</p>
            <strong>{health?.status ?? "loading"}</strong>
            <p className="muted">{activeRun?.run_id ?? health?.active_run_id ?? "No active run"}</p>
          </article>
          <article className="stat-card">
            <p className="meta-label">Ready baselines</p>
            <strong>{configs.length}</strong>
            <p className="muted">Configs currently available for launch</p>
          </article>
        </div>
      </section>

      <section className="panel workflow-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Stage 1 + 2</p>
            <h3>Select the source and baseline</h3>
            <p className="muted">The cleanest AI loop starts from a single, explicit baseline run.</p>
          </div>
        </div>

        <div className="selector-grid">
          <section className="selector-column">
            <div className="section-intro">
              <h4>Input videos</h4>
              <p className="muted">Discovered from the input folder. Pick one clip and keep it fixed for the iteration.</p>
            </div>
            <div className="info-block">
              <p className="meta-label">Input directory</p>
              <p className="mono">{inputCatalog.root_dir || "Unavailable"}</p>
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
                        {isSelected ? <span className="choice-badge">Selected</span> : null}
                      </div>
                      <p className="muted mono">{formatPathTail(video.path)}</p>
                      <div className="choice-meta">
                        <span>{formatVideoSize(video.size_bytes)}</span>
                        <span>{formatTimestamp(video.modified_at)}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">
                <strong>No source videos found</strong>
                <p className="muted">Drop a supported clip into the input directory, then refresh the workspace.</p>
              </div>
            )}
          </section>

          <section className="selector-column">
            <div className="section-intro">
              <h4>Baseline configs</h4>
              <p className="muted">Use an existing kept config as the base. AI derives from evidence after this step.</p>
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
                        {isSelected ? <span className="choice-badge">Baseline</span> : null}
                      </div>
                      <p className="muted mono">{config.output_dir ?? "Output directory not resolved"}</p>
                      <div className="tag-row">
                        <span className={`tag ${config.postprocess_enabled ? "good" : ""}`}>cleanup</span>
                        <span className={`tag ${config.follow_cam_enabled ? "good" : ""}`}>follow-cam</span>
                        <span className="tag">{formatPathTail(config.detector_model_path)}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">
                <strong>No configs discovered</strong>
                <p className="muted">Add or regenerate configs under `config/` before starting a new baseline.</p>
              </div>
            )}
          </section>
        </div>

        <div className="launch-bar">
          <div className="launch-summary">
            <article className="launch-card">
              <p className="meta-label">Selected input</p>
              <strong>{selectedVideo?.name ?? "Choose a source video"}</strong>
              <p className="muted mono">{selectedVideo?.path ?? "Waiting for a selected input file."}</p>
            </article>
            <article className="launch-card">
              <p className="meta-label">Selected baseline</p>
              <strong>{selectedConfig?.name ?? "Choose a kept config"}</strong>
              <p className="muted mono">{selectedConfig?.output_dir ?? "Waiting for a selected config."}</p>
            </article>
          </div>

          <div className="launch-actions">
            <p className="muted">
              This launches a baseline run with cleanup and follow-cam enabled, so the AI console can work from actual
              artifacts instead of guesses.
            </p>
            <button
              type="button"
              className="primary-button"
              onClick={handleStartRun}
              disabled={loading || launching || !selectedInputPath || !selectedConfigName}
            >
              {launching ? "Starting..." : "Run Selected Video"}
            </button>
            {launchMessage ? <p className="notice-line">{launchMessage}</p> : null}
          </div>
        </div>
      </section>

      <div className="content-grid two-up">
        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Stage 3</p>
              <h3>Focused run</h3>
              <p className="muted">Keep one run selected. That evidence bundle becomes the source of truth for AI.</p>
            </div>
          </div>
          {focusedRun ? (
            <div className="focus-stack">
              <article className="summary-card spotlight-card">
                <p className="meta-label">Current focus</p>
                <strong>{focusedRun.run_id}</strong>
                <p className="muted">
                  {formatStatusLabel(focusedRun.status)} · {focusedRun.config_name ?? "Config pending"}
                </p>
              </article>

              <div className="mini-stat-grid">
                <article className="mini-stat">
                  <p className="meta-label">Detected</p>
                  <strong>{readNumber(stats, "detected")}</strong>
                  <p className="muted">Current focused track count</p>
                </article>
                <article className="mini-stat">
                  <p className="meta-label">Lost</p>
                  <strong>{readNumber(stats, "lost")}</strong>
                  <p className="muted">Frames marked as lost</p>
                </article>
                <article className="mini-stat">
                  <p className="meta-label">Artifacts</p>
                  <strong>{focusedRun.artifacts.length}</strong>
                  <p className="muted">Files available for review</p>
                </article>
                <article className="mini-stat">
                  <p className="meta-label">Last event</p>
                  <strong>{formatTimestamp(focusedRun.completed_at ?? focusedRun.started_at ?? focusedRun.created_at)}</strong>
                  <p className="muted">Most recent known run timestamp</p>
                </article>
              </div>

              <div className="detail-grid">
                <div className="detail-block">
                  <p className="meta-label">Input video</p>
                  <p className="mono">{focusedRun.input_video ?? "n/a"}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">Output directory</p>
                  <p className="mono">{focusedRun.output_dir}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">Created</p>
                  <p>{formatTimestamp(focusedRun.created_at)}</p>
                </div>
                <div className="detail-block">
                  <p className="meta-label">Completed</p>
                  <p>{focusedRun.completed_at ? formatTimestamp(focusedRun.completed_at) : "Still running or queued"}</p>
                </div>
              </div>

              {focusedRun.notes ? (
                <div className="info-block">
                  <p className="meta-label">Run notes</p>
                  <p className="muted">{focusedRun.notes}</p>
                </div>
              ) : null}

              {focusedRun.error ? <div className="error-banner inline">{focusedRun.error}</div> : null}
            </div>
          ) : (
            <div className="empty-state">
              <strong>No run in focus yet</strong>
              <p className="muted">Start a baseline run to populate the evidence lane and unlock the AI console.</p>
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Run Queue</p>
              <h3>Recent tasks</h3>
              <p className="muted">The queue stays short here. Deep logs and full reports remain secondary.</p>
            </div>
          </div>
          {activeRun ? (
            <div className="summary-card">
              <p className="meta-label">Running now</p>
              <strong>{activeRun.run_id}</strong>
              <p className="muted">{formatStatusLabel(activeRun.status)}</p>
            </div>
          ) : (
            <div className="summary-card">
              <p className="meta-label">Running now</p>
              <strong>Idle</strong>
              <p className="muted">No active task at the moment</p>
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
                      <span>{formatTimestamp(run.created_at)}</span>
                      <span>{run.artifacts.length} artifacts</span>
                    </div>
                  </div>
                  <span className={`status-dot ${run.status}`}>{formatStatusLabel(run.status)}</span>
                </button>
              ))
            ) : (
              <div className="empty-state">
                <strong>No runs yet</strong>
                <p className="muted">Launch the first baseline above to start building a reviewable history.</p>
              </div>
            )}
          </div>
        </section>
      </div>

      <section className="panel evidence-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Evidence Bundle</p>
            <h3>Latest outputs in one place</h3>
            <p className="muted">This is the material the AI reads before it recommends the next config.</p>
          </div>
        </div>

        {selectedRun ? (
          <>
            <div className="summary-grid evidence-summary-grid">
              <div className="summary-card">
                <p className="meta-label">Run</p>
                <strong>{selectedRun.run_id}</strong>
                <p className="muted mono">{selectedRun.config_name ?? "n/a"}</p>
              </div>
              <div className="summary-card">
                <p className="meta-label">Modules enabled</p>
                <div className="tag-row">
                  <span className={`tag ${selectedRun.modules_enabled.postprocess ? "good" : ""}`}>cleanup</span>
                  <span className={`tag ${selectedRun.modules_enabled.follow_cam ? "good" : ""}`}>follow-cam</span>
                </div>
                <p className="muted">Processing modules used for this run</p>
              </div>
              <div className="summary-card">
                <p className="meta-label">Artifacts ready</p>
                <strong>{selectedRun.artifacts.length}</strong>
                <p className="muted">Videos, reports, CSVs, and camera path samples</p>
              </div>
              <div className="summary-card">
                <p className="meta-label">Output folder</p>
                <strong>{formatPathTail(selectedRun.output_dir)}</strong>
                <p className="muted mono">{selectedRun.output_dir}</p>
              </div>
            </div>

            <div className="video-grid">
              <div className="video-card">
                <p className="meta-label">Follow-cam</p>
                <video controls src={api.artifactUrl(selectedRun.run_id, "follow_cam.mp4")} />
              </div>
              <div className="video-card">
                <p className="meta-label">Annotated cleaned</p>
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
            <strong>No evidence selected</strong>
            <p className="muted">Start or select a run to surface videos, reports, and artifacts here.</p>
          </div>
        )}
      </section>
    </div>
  );
}
