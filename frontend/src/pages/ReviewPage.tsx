import { useEffect, useState } from "react";

import { ArtifactList } from "../components/ArtifactList";
import { api } from "../lib/api";
import type { RunRecord } from "../lib/types";

interface ReviewPageProps {
  runs: RunRecord[];
  selectedRun: RunRecord | null;
  onSelectRun: (run: RunRecord) => void;
}

export function ReviewPage({ runs, selectedRun, onSelectRun }: ReviewPageProps) {
  const [cleanupReport, setCleanupReport] = useState<Record<string, unknown> | null>(null);
  const [followCamReport, setFollowCamReport] = useState<Record<string, unknown> | null>(null);
  const [cameraPathRows, setCameraPathRows] = useState<Record<string, string>[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!selectedRun) {
        setCleanupReport(null);
        setFollowCamReport(null);
        setCameraPathRows([]);
        return;
      }

      try {
        const [cleanup, followCam, cameraPath] = await Promise.allSettled([
          api.getCleanupReport(selectedRun.run_id),
          api.getFollowCamReport(selectedRun.run_id),
          api.getCameraPath(selectedRun.run_id, 12),
        ]);
        if (cancelled) {
          return;
        }
        setCleanupReport(cleanup.status === "fulfilled" ? cleanup.value : null);
        setFollowCamReport(followCam.status === "fulfilled" ? followCam.value : null);
        setCameraPathRows(cameraPath.status === "fulfilled" ? cameraPath.value.rows : []);
      } catch {
        if (!cancelled) {
          setCleanupReport(null);
          setFollowCamReport(null);
          setCameraPathRows([]);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [selectedRun]);

  return (
    <div className="page-stack">
      <section className="panel">
        <div className="panel-header">
          <h3>Run Selection</h3>
          <p className="muted">Keep one run in focus. The rest can stay collapsed in the queue or dashboard.</p>
        </div>
        <div className="run-list compact-list">
          {runs.map((run) => (
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
        <>
          <section className="panel">
            <div className="panel-header">
              <h3>Playback</h3>
              <p className="muted mono">{selectedRun.run_id}</p>
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
          </section>

          <div className="content-grid two-up">
            <section className="panel">
              <div className="panel-header">
                <h3>Artifacts</h3>
              </div>
              <ArtifactList
                run={selectedRun}
                preferredNames={[
                  "follow_cam.mp4",
                  "annotated.cleaned.mp4",
                  "annotated.mp4",
                  "cleanup_report.json",
                  "follow_cam_report.json",
                  "ball_track.cleaned.csv",
                ]}
              />
            </section>

            <section className="panel">
              <div className="panel-header">
                <h3>Reports</h3>
              </div>
              <div className="report-stack">
                <div className="report-card">
                  <p className="meta-label">Cleanup report</p>
                  <pre className="code-block compact">{cleanupReport ? JSON.stringify(cleanupReport, null, 2) : "not available"}</pre>
                </div>
                <div className="report-card">
                  <p className="meta-label">Follow-cam report</p>
                  <pre className="code-block compact">{followCamReport ? JSON.stringify(followCamReport, null, 2) : "not available"}</pre>
                </div>
              </div>
            </section>
          </div>

          <section className="panel">
            <div className="panel-header">
              <h3>Camera Path Preview</h3>
              <p className="muted">Small sample only. Full path stays in the artifact bundle.</p>
            </div>
            <div className="table-shell">
              <table>
                <thead>
                  <tr>
                    {cameraPathRows[0] ? Object.keys(cameraPathRows[0]).map((key) => <th key={key}>{key}</th>) : null}
                  </tr>
                </thead>
                <tbody>
                  {cameraPathRows.map((row, index) => (
                    <tr key={index}>
                      {Object.entries(row).map(([key, value]) => (
                        <td key={key}>{value}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : (
        <section className="panel">
          <p className="muted">Select a run to inspect playback, reports, and camera-path evidence.</p>
        </section>
      )}
    </div>
  );
}
