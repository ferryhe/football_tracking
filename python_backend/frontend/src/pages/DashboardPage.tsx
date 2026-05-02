import type { ConfigListItem, HealthResponse, RunRecord } from "../lib/types";
import { ArtifactList } from "../components/ArtifactList";
import { StatCard } from "../components/StatCard";

interface DashboardPageProps {
  health: HealthResponse | null;
  configs: ConfigListItem[];
  runs: RunRecord[];
  selectedRun: RunRecord | null;
  onSelectRun: (run: RunRecord) => void;
}

export function DashboardPage({ health, configs, runs, selectedRun, onSelectRun }: DashboardPageProps) {
  const recentRuns = runs.slice(0, 6);

  return (
    <div className="page-stack">
      <section className="hero-panel compact">
        <div>
          <p className="eyebrow">Control Room</p>
          <h3>Current baselines and active evidence</h3>
          <p className="muted">
            Start from the kept configs, keep one selected run in focus, and use the AI panel only after you have actual evidence.
          </p>
        </div>
        <div className="stats-row">
          <StatCard label="Backend" value={health?.status ?? "loading"} detail="FastAPI shell status" />
          <StatCard label="Configs" value={String(configs.length)} detail="Discovered from config/" />
          <StatCard label="Runs" value={String(runs.length)} detail="Registry + filesystem baselines" />
        </div>
      </section>

      <div className="content-grid two-up">
        <section className="panel">
          <div className="panel-header">
            <h3>Kept Configs</h3>
            <p className="muted">Use one of these as the base for the next run.</p>
          </div>
          <div className="simple-list">
            {configs.map((config) => (
              <article key={config.name} className="config-card compact">
                <div>
                  <strong>{config.name}</strong>
                  <p className="muted mono">{config.output_dir ?? "n/a"}</p>
                </div>
                <div className="tag-row">
                  <span className={`tag ${config.postprocess_enabled ? "good" : ""}`}>cleanup</span>
                  <span className={`tag ${config.follow_cam_enabled ? "good" : ""}`}>follow-cam</span>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h3>Recent Runs</h3>
            <p className="muted">Select one run and keep the rest in the background.</p>
          </div>
          <div className="run-list compact-list">
            {recentRuns.map((run) => (
              <button
                type="button"
                key={run.run_id}
                className={`run-row ${selectedRun?.run_id === run.run_id ? "selected" : ""}`}
                onClick={() => onSelectRun(run)}
              >
                <div>
                  <strong>{run.run_id}</strong>
                  <p className="muted mono">{run.config_name ?? run.output_dir}</p>
                </div>
                <span className={`status-dot ${run.status}`}>{run.status}</span>
              </button>
            ))}
          </div>
        </section>
      </div>

      {selectedRun ? (
        <section className="panel">
          <div className="panel-header">
            <h3>Selected Run Snapshot</h3>
            <p className="muted mono">{selectedRun.run_id}</p>
          </div>
          <div className="content-grid summary-grid">
            <div className="summary-card">
              <p className="meta-label">Config</p>
              <strong>{selectedRun.config_name ?? "n/a"}</strong>
              <p className="muted mono">{selectedRun.output_dir}</p>
            </div>
            <div className="summary-card">
              <p className="meta-label">Modules</p>
              <div className="tag-row">
                <span className={`tag ${selectedRun.modules_enabled.postprocess ? "good" : ""}`}>cleanup</span>
                <span className={`tag ${selectedRun.modules_enabled.follow_cam ? "good" : ""}`}>follow-cam</span>
              </div>
            </div>
            <div className="summary-card">
              <p className="meta-label">Artifacts</p>
              <strong>{selectedRun.artifacts.length}</strong>
              <p className="muted">Available for review</p>
            </div>
          </div>
          <div className="panel-divider" />
          <ArtifactList
            run={selectedRun}
            preferredNames={["follow_cam.mp4", "annotated.cleaned.mp4", "cleanup_report.json", "follow_cam_report.json"]}
          />
        </section>
      ) : null}
    </div>
  );
}
