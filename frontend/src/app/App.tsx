import { useEffect, useState } from "react";

import { AIPanel } from "../components/AIPanel";
import { api } from "../lib/api";
import type { ConfigListItem, HealthResponse, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage } from "../pages/WorkspacePage";

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "Waiting";
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

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [inputCatalog, setInputCatalog] = useState<InputCatalog>({ root_dir: "", videos: [] });
  const [configs, setConfigs] = useState<ConfigListItem[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [selectedInputPath, setSelectedInputPath] = useState<string>("");
  const [selectedConfigName, setSelectedConfigName] = useState<string>("real_v24_full_postclean.yaml");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  async function refreshInputs(): Promise<InputCatalog> {
    const nextInputs = await api.listInputs();
    setInputCatalog(nextInputs);
    return nextInputs;
  }

  useEffect(() => {
    let cancelled = false;

    async function load(showSpinner: boolean) {
      if (showSpinner) {
        setLoading(true);
      }
      try {
        const [healthData, inputData, configData, runData] = await Promise.all([
          api.getHealth(),
          api.listInputs(),
          api.listConfigs(),
          api.listRuns(),
        ]);
        if (cancelled) {
          return;
        }
        setHealth(healthData);
        setInputCatalog(inputData);
        setConfigs(configData);
        setRuns(runData);
        setError(null);

        setSelectedRun((current) => {
          if (current) {
            return runData.find((item) => item.run_id === current.run_id) ?? current;
          }
          return runData[0] ?? null;
        });

        setSelectedInputPath((current) => {
          if (current && inputData.videos.some((item) => item.path === current)) {
            return current;
          }
          const selectedRunInput = runData[0]?.input_video;
          if (selectedRunInput && inputData.videos.some((item) => item.path === selectedRunInput)) {
            return selectedRunInput;
          }
          return inputData.videos[0]?.path ?? "";
        });

        setSelectedConfigName((current) => {
          if (current && configData.some((item) => item.name === current)) {
            return current;
          }
          const preferred = configData.find((item) => item.name === "real_v24_full_postclean.yaml");
          return preferred?.name ?? configData[0]?.name ?? current;
        });
      } catch (caughtError) {
        if (!cancelled) {
          setError(caughtError instanceof Error ? caughtError.message : String(caughtError));
        }
      } finally {
        if (!cancelled && showSpinner) {
          setLoading(false);
        }
      }
    }

    void load(true);
    const intervalId = window.setInterval(() => {
      void load(false);
    }, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  function handleSelectRun(run: RunRecord) {
    setSelectedRun(run);
    if (run.input_video) {
      setSelectedInputPath(run.input_video);
    }
    if (run.config_name) {
      setSelectedConfigName(run.config_name);
    }
  }

  async function handleRunCreated(createdRun: RunRecord) {
    const runData = await refreshRuns();
    const matched = runData.find((item) => item.run_id === createdRun.run_id) ?? createdRun;
    setSelectedRun(matched);
    if (matched.input_video) {
      setSelectedInputPath(matched.input_video);
    }
    if (matched.config_name) {
      setSelectedConfigName(matched.config_name);
    }
  }

  async function handleStartBaselineRun(notes: string) {
    const createdRun = await api.createRun({
      config_name: selectedConfigName,
      input_video: selectedInputPath || undefined,
      enable_postprocess: true,
      enable_follow_cam: true,
      notes,
    });
    await handleRunCreated(createdRun);
  }

  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = configs.find((item) => item.name === selectedConfigName) ?? null;
  const activeRun = runs.find((item) => item.status === "running" || item.status === "queued") ?? null;
  const workflowSteps = [
    {
      title: "Input locked",
      detail: selectedVideo
        ? `${selectedVideo.name} · ${formatTimestamp(selectedVideo.modified_at)}`
        : "Choose one source clip from the discovered input list.",
      state: selectedVideo ? "complete" : "current",
    },
    {
      title: "Baseline ready",
      detail: selectedConfig ? selectedConfig.name : "Pick the kept config you want as the baseline.",
      state: selectedRun ? "complete" : selectedVideo && selectedConfig ? "current" : "upcoming",
    },
    {
      title: "Evidence bundle",
      detail: selectedRun
        ? `${selectedRun.run_id} · ${selectedRun.artifacts.length} artifacts in focus`
        : "Run one baseline and keep that evidence selected.",
      state: selectedRun ? "complete" : "upcoming",
    },
    {
      title: "AI iteration",
      detail: selectedRun ? "Recommendation console is ready to derive the next config." : "AI unlocks after a run is selected.",
      state: selectedRun ? "current" : "upcoming",
    },
  ] as const;

  return (
    <div className="workspace-app">
      <header className="topbar">
        <div>
          <p className="eyebrow">AI Native Workspace</p>
          <h1>Football Tracking Operator</h1>
          <p className="muted topbar-copy">
            Put a video in the input folder, pick it once, and let AI recommend the next run from actual evidence.
          </p>
        </div>
        <div className="header-meta">
          <div className={`status-pill ${health?.status === "ok" ? "ok" : "warn"}`}>
            {loading ? "Loading..." : error ? "Offline" : "Backend OK"}
          </div>
          <div className="header-chip">
            <span className="meta-label">Active task</span>
            <strong className="mono">{activeRun?.run_id ?? health?.active_run_id ?? "Idle"}</strong>
          </div>
          <div className="header-chip wide">
            <span className="meta-label">Input root</span>
            <strong className="mono">{inputCatalog.root_dir || "Input folder unavailable"}</strong>
          </div>
        </div>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <section className="workflow-strip" aria-label="Workflow stages">
        {workflowSteps.map((step, index) => (
          <article key={step.title} className={`workflow-step ${step.state}`}>
            <div className="workflow-marker">{index + 1}</div>
            <div className="workflow-copy">
              <p className="meta-label">Stage {index + 1}</p>
              <strong>{step.title}</strong>
              <p className="muted">{step.detail}</p>
            </div>
          </article>
        ))}
      </section>

      <div className="workspace-layout">
        <section className="workspace-main">
          <WorkspacePage
            health={health}
            inputCatalog={inputCatalog}
            configs={configs}
            runs={runs}
            selectedRun={selectedRun}
            selectedInputPath={selectedInputPath}
            selectedConfigName={selectedConfigName}
            loading={loading}
            onSelectRun={handleSelectRun}
            onSelectInput={setSelectedInputPath}
            onSelectConfig={setSelectedConfigName}
            onStartBaselineRun={handleStartBaselineRun}
          />
        </section>

        <aside className="assistant-column">
          <AIPanel
            run={selectedRun}
            configs={configs}
            targetInputVideo={selectedInputPath || undefined}
            onConfigDerived={async () => {
              await refreshConfigs();
              await refreshInputs();
            }}
            onRunCreated={handleRunCreated}
          />
        </aside>
      </div>
    </div>
  );
}
