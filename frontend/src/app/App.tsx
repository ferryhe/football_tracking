import { NavLink, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { ConfigListItem, HealthResponse, RunRecord } from "../lib/types";
import { AIPanel } from "../components/AIPanel";
import { DashboardPage } from "../pages/DashboardPage";
import { ReviewPage } from "../pages/ReviewPage";
import { RunsPage } from "../pages/RunsPage";

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [configs, setConfigs] = useState<ConfigListItem[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();

  async function refreshConfigs(): Promise<ConfigListItem[]> {
    const nextConfigs = await api.listConfigs();
    setConfigs(nextConfigs);
    return nextConfigs;
  }

  async function refreshRuns(): Promise<RunRecord[]> {
    const nextRuns = await api.listRuns();
    setRuns(nextRuns);
    return nextRuns;
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [healthData, configData, runData] = await Promise.all([
          api.getHealth(),
          api.listConfigs(),
          api.listRuns(),
        ]);
        if (cancelled) {
          return;
        }
        setHealth(healthData);
        setConfigs(configData);
        setRuns(runData);

        const requestedRunId = searchParams.get("run");
        const nextRun = runData.find((item) => item.run_id === requestedRunId) ?? runData[0] ?? null;
        setSelectedRun(nextRun);
      } catch (caughtError) {
        if (!cancelled) {
          setError(caughtError instanceof Error ? caughtError.message : String(caughtError));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [searchParams]);

  function handleSelectRun(run: RunRecord) {
    setSelectedRun(run);
    if (location.pathname === "/review") {
      navigate(`/review?run=${encodeURIComponent(run.run_id)}`);
    }
  }

  async function handleRunCreated(createdRun: RunRecord) {
    const runData = await refreshRuns();
    const matched = runData.find((item) => item.run_id === createdRun.run_id) ?? createdRun;
    setSelectedRun(matched);
    navigate(`/review?run=${encodeURIComponent(matched.run_id)}`);
  }

  return (
    <div className="app-shell">
      <aside className="left-rail">
        <div className="brand-card">
          <p className="eyebrow">AI Native Studio</p>
          <h1>Football Tracking</h1>
          <p className="muted">
            Review runs, derive configs, and hand AI the evidence bundle instead of raw guesswork.
          </p>
        </div>

        <nav className="nav-stack">
          <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
            Dashboard
          </NavLink>
          <NavLink to="/runs" className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
            Runs
          </NavLink>
          <NavLink to="/review" className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
            Review
          </NavLink>
        </nav>

        <div className="meta-card">
          <p className="meta-label">API</p>
          <p className="mono">{api.baseUrl}</p>
          <p className="meta-label">Configs</p>
          <p>{health?.config_count ?? "-"}</p>
          <p className="meta-label">Runs</p>
          <p>{health?.run_count ?? "-"}</p>
        </div>
      </aside>

      <main className="main-stage">
        <header className="page-header">
          <div>
            <p className="eyebrow">Phase 1</p>
            <h2>Frontend Shell</h2>
          </div>
          <div className={`status-pill ${health?.status === "ok" ? "ok" : "warn"}`}>
            {loading ? "Loading..." : error ? "Offline" : "Backend OK"}
          </div>
        </header>

        {error ? <div className="error-banner">{error}</div> : null}

        <Routes>
          <Route
            path="/"
            element={
              <DashboardPage
                health={health}
                configs={configs}
                runs={runs}
                selectedRun={selectedRun}
                onSelectRun={handleSelectRun}
              />
            }
          />
          <Route
            path="/runs"
            element={
              <RunsPage configs={configs} runs={runs} loading={loading} onRunCreated={handleRunCreated} />
            }
          />
          <Route
            path="/review"
            element={
              <ReviewPage runs={runs} selectedRun={selectedRun} onSelectRun={handleSelectRun} />
            }
          />
        </Routes>
      </main>

      <AIPanel
        run={selectedRun}
        configs={configs}
        onConfigDerived={refreshConfigs}
        onRunCreated={handleRunCreated}
      />
    </div>
  );
}
