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
  const recentRuns = runs.slice(0, 5);

  return (
    <div className="page-grid">
      <section className="hero-panel">
        <div>
          <p className="eyebrow">Control Room</p>
          <h3>Current baselines and recent evidence</h3>
          <p className="muted">
            The UI is grounded in the same kept configs and outputs already stabilized in the backend.
          </p>
        </div>
        <div className="stats-row">
          <StatCard label="Backend" value={health?.status ?? "loading"} detail="FastAPI shell status" />
          <StatCard label="Configs" value={String(configs.length)} detail="Discovered from config/" />
          <StatCard label="Runs" value={String(runs.length)} detail="Registry + filesystem baselines" />
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Kept Configs</h3>
        </div>
        <div className="config-grid">
          {configs.map((config) => (
            <article key={config.name} className="config-card">
              <strong>{config.name}</strong>
              <p className="muted mono">{config.output_dir ?? "n/a"}</p>
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
        </div>
        <div className="run-list">
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

      {selectedRun ? (
        <section className="panel panel-span-2">
          <div className="panel-header">
            <h3>Selected Run Artifacts</h3>
          </div>
          <ArtifactList
            run={selectedRun}
            preferredNames={["follow_cam.mp4", "annotated.cleaned.mp4", "cleanup_report.json", "follow_cam_report.json"]}
          />
        </section>
      ) : null}
    </div>
  );
}
