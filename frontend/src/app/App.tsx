import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";

import { AIPanel } from "../components/AIPanel";
import { ActivityIcon, FileIcon, PlayIcon, SparkIcon, VideoIcon } from "../components/Icons";
import { LanguageToggle } from "../components/LanguageToggle";
import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, HealthResponse, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage } from "../pages/WorkspacePage";

interface WorkflowStep {
  key: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  title: string;
  detail: string;
  state: "complete" | "current" | "upcoming";
}

export function App() {
  const { copy } = useI18n();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [inputCatalog, setInputCatalog] = useState<InputCatalog>({ root_dir: "", videos: [] });
  const [configs, setConfigs] = useState<ConfigListItem[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [selectedInputPath, setSelectedInputPath] = useState<string>("");
  const [selectedConfigName, setSelectedConfigName] = useState<string>("real_v24_full_postclean.yaml");
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
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

  useEffect(() => {
    setLaunchMessage(null);
  }, [selectedConfigName, selectedInputPath]);

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

  async function handleStartBaselineRun() {
    setLaunching(true);
    setLaunchMessage(null);
    try {
      const createdRun = await api.createRun({
        config_name: selectedConfigName,
        input_video: selectedInputPath || undefined,
        enable_postprocess: true,
        enable_follow_cam: true,
        notes: `Workspace baseline run for ${selectedVideo?.name ?? "selected input"}`,
      });
      await handleRunCreated(createdRun);
      setLaunchMessage(copy.common.refreshHint);
    } catch (caughtError) {
      setLaunchMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setLaunching(false);
    }
  }

  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = configs.find((item) => item.name === selectedConfigName) ?? null;
  const activeRun = runs.find((item) => item.status === "running" || item.status === "queued") ?? null;
  const focusedRun = selectedRun ?? activeRun;

  const workflowSteps = useMemo<WorkflowStep[]>(
    () => [
      {
        key: "choose",
        icon: VideoIcon,
        title: copy.workspace.flowChooseTitle,
        detail: selectedVideo && selectedConfig ? `${selectedVideo.name} + ${selectedConfig.name}` : copy.workspace.flowChooseDetail,
        state: selectedVideo && selectedConfig ? "complete" : "current",
      },
      {
        key: "run",
        icon: PlayIcon,
        title: copy.workspace.flowRunTitle,
        detail: focusedRun ? focusedRun.run_id : copy.workspace.flowRunDetail,
        state: focusedRun ? "complete" : selectedVideo && selectedConfig ? "current" : "upcoming",
      },
      {
        key: "ai",
        icon: SparkIcon,
        title: copy.workspace.flowAiTitle,
        detail: focusedRun ? copy.ai.subtitle : copy.workspace.flowAiDetail,
        state: focusedRun ? "current" : "upcoming",
      },
      {
        key: "review",
        icon: FileIcon,
        title: copy.workspace.flowReviewTitle,
        detail: focusedRun ? copy.workspace.evidenceSubtitle : copy.workspace.flowReviewDetail,
        state: focusedRun ? "current" : "upcoming",
      },
    ],
    [copy, focusedRun, selectedConfig, selectedVideo],
  );

  return (
    <div className="workspace-app">
      <header className="topbar compact-topbar">
        <div className="topbar-copy-block">
          <p className="eyebrow">{copy.header.eyebrow}</p>
          <h1>{copy.header.title}</h1>
          <p className="muted topbar-copy">{copy.header.subtitle}</p>
        </div>
        <div className="topbar-actions">
          <div className={`status-pill compact ${health?.status === "ok" ? "ok" : "warn"}`}>
            <ActivityIcon className="section-icon tiny" />
            <span>{loading ? copy.common.loading : error ? copy.common.offline : copy.common.backendOk}</span>
          </div>
          <div className="header-chip compact">
            <span className="meta-label">{copy.header.activeTask}</span>
            <strong className="mono">{activeRun?.run_id ?? health?.active_run_id ?? copy.common.idle}</strong>
          </div>
          <LanguageToggle />
        </div>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <section className="workflow-strip" aria-label="Primary workflow">
        {workflowSteps.map((step, index) => {
          const Icon = step.icon;
          return (
            <article key={step.key} className={`workflow-step ${step.state}`}>
              <span className="workflow-marker">{index + 1}</span>
              <div className="workflow-copy">
                <div className="title-row compact">
                  <Icon className="section-icon tiny" />
                  <p className="meta-label">{step.title}</p>
                </div>
                <p className="muted">{step.detail}</p>
              </div>
            </article>
          );
        })}
      </section>

      <div className="workspace-layout">
        <section className="workspace-main">
          <WorkspacePage
            inputCatalog={inputCatalog}
            configs={configs}
            runs={runs}
            selectedRun={selectedRun}
            selectedInputPath={selectedInputPath}
            selectedConfigName={selectedConfigName}
            loading={loading}
            launching={launching}
            launchMessage={launchMessage}
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
