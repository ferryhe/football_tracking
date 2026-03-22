import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { ConfigListItem, RunRecord } from "../lib/types";

interface RunsPageProps {
  configs: ConfigListItem[];
  runs: RunRecord[];
  loading: boolean;
  onRunCreated: (run: RunRecord) => Promise<void> | void;
}

export function RunsPage({ configs, runs, loading, onRunCreated }: RunsPageProps) {
  const [configName, setConfigName] = useState<string>("real_v24_full_postclean.yaml");
  const [startFrame, setStartFrame] = useState("0");
  const [maxFrames, setMaxFrames] = useState("");
  const [enablePostprocess, setEnablePostprocess] = useState(true);
  const [enableFollowCam, setEnableFollowCam] = useState(true);
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!configs.length) {
      return;
    }
    if (!configs.some((config) => config.name === configName)) {
      setConfigName(configs[0].name);
    }
  }, [configs, configName]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage(null);
    try {
      const created = await api.createRun({
        config_name: configName,
        start_frame: Number(startFrame || 0),
        max_frames: maxFrames ? Number(maxFrames) : null,
        enable_postprocess: enablePostprocess,
        enable_follow_cam: enableFollowCam,
        notes,
      });
      setMessage(`Run ${created.run_id} accepted.`);
      await onRunCreated(created);
    } catch (caughtError) {
      setMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page-grid two-column">
      <section className="panel">
        <div className="panel-header">
          <h3>Launch Run</h3>
        </div>
        <form className="form-stack" onSubmit={handleSubmit}>
          <label>
            <span>Config</span>
            <select value={configName} onChange={(event) => setConfigName(event.target.value)}>
              {configs.map((config) => (
                <option key={config.name} value={config.name}>
                  {config.name}
                </option>
              ))}
            </select>
          </label>

          <div className="form-grid">
            <label>
              <span>Start frame</span>
              <input value={startFrame} onChange={(event) => setStartFrame(event.target.value)} />
            </label>
            <label>
              <span>Max frames</span>
              <input value={maxFrames} onChange={(event) => setMaxFrames(event.target.value)} placeholder="empty = full" />
            </label>
          </div>

          <div className="toggle-row">
            <label className="toggle-card">
              <input type="checkbox" checked={enablePostprocess} onChange={(event) => setEnablePostprocess(event.target.checked)} />
              <span>Enable cleanup</span>
            </label>
            <label className="toggle-card">
              <input type="checkbox" checked={enableFollowCam} onChange={(event) => setEnableFollowCam(event.target.checked)} />
              <span>Enable follow-cam</span>
            </label>
          </div>

          <label>
            <span>Notes</span>
            <textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={4} />
          </label>

          <button className="primary-button" type="submit" disabled={loading || submitting}>
            {submitting ? "Starting..." : "Start Run"}
          </button>
          {message ? <p className="muted">{message}</p> : null}
        </form>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Run Queue</h3>
        </div>
        <div className="run-list">
          {runs.map((run) => (
            <div key={run.run_id} className="run-row static">
              <div>
                <strong>{run.run_id}</strong>
                <p className="muted mono">{run.config_name ?? run.output_dir}</p>
              </div>
              <span className={`status-dot ${run.status}`}>{run.status}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
