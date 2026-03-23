import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";

import { AIPanel } from "../components/AIPanel";
import { ActivityIcon, FileIcon, PlayIcon, SparkIcon } from "../components/Icons";
import { LanguageToggle } from "../components/LanguageToggle";
import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, FieldSuggestion, HealthResponse, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage, type WorkspaceStage } from "../pages/WorkspacePage";

interface StageTab {
  key: WorkspaceStage;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  title: string;
  detail: string;
  state: "complete" | "current" | "upcoming";
}

function pickPreferredConfigName(configs: ConfigListItem[], runs: RunRecord[], inputPath: string, current = ""): string {
  const knownNames = new Set(configs.map((item) => item.name));
  if (current && knownNames.has(current)) {
    return current;
  }

  const latestRunForInput = runs.find((item) => item.input_video === inputPath && item.config_name && knownNames.has(item.config_name));
  if (latestRunForInput?.config_name) {
    return latestRunForInput.config_name;
  }

  for (const preferredName of ["real_first_run.yaml", "default.yaml", "real_v24_full_postclean.yaml"]) {
    if (knownNames.has(preferredName)) {
      return preferredName;
    }
  }

  return configs[0]?.name ?? current;
}

export function App() {
  const { copy, formatDateTime } = useI18n();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [inputCatalog, setInputCatalog] = useState<InputCatalog>({ root_dir: "", videos: [] });
  const [configs, setConfigs] = useState<ConfigListItem[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [selectedInputPath, setSelectedInputPath] = useState<string>("");
  const [selectedConfigName, setSelectedConfigName] = useState<string>("real_first_run.yaml");
  const [stage, setStage] = useState<WorkspaceStage>("baseline");
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
  const [fieldSuggestions, setFieldSuggestions] = useState<Record<string, FieldSuggestion>>({});
  const [fieldLoading, setFieldLoading] = useState(false);
  const [fieldMessage, setFieldMessage] = useState<string | null>(null);
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

        let nextSelectedInput = "";
        setSelectedInputPath((current) => {
          if (current && inputData.videos.some((item) => item.path === current)) {
            nextSelectedInput = current;
            return current;
          }
          const selectedRunInput = runData[0]?.input_video;
          if (selectedRunInput && inputData.videos.some((item) => item.path === selectedRunInput)) {
            nextSelectedInput = selectedRunInput;
            return selectedRunInput;
          }
          nextSelectedInput = inputData.videos[0]?.path ?? "";
          return nextSelectedInput;
        });

        setSelectedConfigName((current) => pickPreferredConfigName(configData, runData, nextSelectedInput, current));
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
    setFieldMessage(null);
  }, [selectedConfigName, selectedInputPath]);

  const orderedRuns = useMemo(
    () =>
      [...runs].sort((left, right) => {
        const leftTime = new Date(left.completed_at ?? left.started_at ?? left.created_at).getTime();
        const rightTime = new Date(right.completed_at ?? right.started_at ?? right.created_at).getTime();
        return rightTime - leftTime;
      }),
    [runs],
  );

  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = configs.find((item) => item.name === selectedConfigName) ?? null;
  const activeFieldSuggestion = selectedInputPath ? fieldSuggestions[selectedInputPath] ?? null : null;
  const activeRun = orderedRuns.find((item) => item.status === "running" || item.status === "queued") ?? null;
  const latestCompletedRun = orderedRuns.find((item) => item.status === "completed") ?? null;
  const latestHistoryRun = orderedRuns[0] ?? null;
  const focusedRun = selectedRun ?? activeRun ?? latestCompletedRun ?? orderedRuns[0] ?? null;
  const aiScopedRuns = useMemo(
    () => (selectedInputPath ? orderedRuns.filter((item) => item.input_video === selectedInputPath) : orderedRuns),
    [orderedRuns, selectedInputPath],
  );
  const aiFocusedRun =
    selectedRun && aiScopedRuns.some((item) => item.run_id === selectedRun.run_id) ? selectedRun : aiScopedRuns[0] ?? null;

  function handleSelectRun(run: RunRecord) {
    setSelectedRun(run);
    if (run.input_video) {
      setSelectedInputPath(run.input_video);
    }
    if (run.config_name) {
      setSelectedConfigName(run.config_name);
    }
  }

  function handleSelectInput(path: string) {
    setSelectedInputPath(path);
    setSelectedConfigName(pickPreferredConfigName(configs, orderedRuns, path));
  }

  async function syncCreatedRun(createdRun: RunRecord) {
    const runData = await refreshRuns();
    const matched = runData.find((item) => item.run_id === createdRun.run_id) ?? createdRun;
    setSelectedRun(matched);
    if (matched.input_video) {
      setSelectedInputPath(matched.input_video);
    }
    if (matched.config_name) {
      setSelectedConfigName(matched.config_name);
    }
    return matched;
  }

  async function handleGenerateFieldSuggestion() {
    if (!selectedInputPath) {
      return;
    }
    setFieldLoading(true);
    setFieldMessage(null);
    try {
      const suggestion = await api.suggestFieldSetup({ input_video: selectedInputPath });
      setFieldSuggestions((current) => ({
        ...current,
        [selectedInputPath]: suggestion,
      }));
      setFieldMessage(copy.workspace.fieldReadyMessage);
    } catch (caughtError) {
      setFieldMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setFieldLoading(false);
    }
  }

  function handleClearFieldSuggestion() {
    if (!selectedInputPath) {
      return;
    }
    setFieldSuggestions((current) => {
      const next = { ...current };
      delete next[selectedInputPath];
      return next;
    });
    setFieldMessage(null);
  }

  async function handleStartBaselineRun() {
    if (!selectedConfig) {
      setLaunchMessage(copy.workspace.noBaselineBody);
      return;
    }
    setLaunching(true);
    setLaunchMessage(null);
    try {
      const createdRun = await api.createRun({
        config_name: selectedConfigName,
        input_video: selectedInputPath || undefined,
        config_patch: activeFieldSuggestion?.config_patch,
        enable_postprocess: true,
        enable_follow_cam: true,
        notes: `Workspace baseline run for ${selectedVideo?.name ?? "selected input"}${activeFieldSuggestion ? " | field setup applied" : ""}`,
      });
      await syncCreatedRun(createdRun);
      setLaunchMessage(copy.common.refreshHint);
      setStage("ai");
    } catch (caughtError) {
      setLaunchMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setLaunching(false);
    }
  }

  async function handleAssistantRunCreated(createdRun: RunRecord) {
    await syncCreatedRun(createdRun);
    setStage("delivery");
  }

  const stageTabs = useMemo<StageTab[]>(
    () => [
      {
        key: "baseline",
        icon: PlayIcon,
        title: copy.workspace.flowRunTitle,
        detail: selectedVideo && selectedConfig ? `${selectedVideo.name} | ${selectedConfig.name}` : copy.workspace.flowChooseDetail,
        state: focusedRun ? "complete" : "current",
      },
      {
        key: "ai",
        icon: SparkIcon,
        title: copy.workspace.flowAiTitle,
        detail: aiFocusedRun ? aiFocusedRun.run_id : copy.workspace.flowAiDetail,
        state: stage === "ai" ? "current" : aiFocusedRun ? "complete" : "upcoming",
      },
      {
        key: "delivery",
        icon: FileIcon,
        title: copy.workspace.deliveryTitle,
        detail: latestHistoryRun
          ? `${latestHistoryRun.run_id} | ${formatDateTime(
              latestHistoryRun.completed_at ?? latestHistoryRun.started_at ?? latestHistoryRun.created_at,
            )}`
          : copy.workspace.deliverySubtitle,
        state: stage === "delivery" ? "current" : latestHistoryRun ? "complete" : "upcoming",
      },
    ],
    [aiFocusedRun, copy, focusedRun, formatDateTime, latestHistoryRun, selectedConfig, selectedVideo, stage],
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

      <section className="step-tabs" aria-label="Workflow steps">
        {stageTabs.map((tab, index) => {
          const Icon = tab.icon;
          return (
            <button
              type="button"
              key={tab.key}
              className={`step-tab ${stage === tab.key ? "active" : ""} ${tab.state}`}
              onClick={() => setStage(tab.key)}
            >
              <span className="step-tab-index">{index + 1}</span>
              <div className="step-tab-copy">
                <div className="title-row compact">
                  <Icon className="section-icon tiny" />
                  <p className="meta-label">{tab.title}</p>
                </div>
                <p className="muted">{tab.detail}</p>
              </div>
            </button>
          );
        })}
      </section>

      <div className={`workspace-layout stage-${stage}`}>
        <section className="workspace-main">
          <WorkspacePage
            stage={stage}
            inputCatalog={inputCatalog}
            configs={configs}
            runs={orderedRuns}
            selectedRun={stage === "ai" ? aiFocusedRun : focusedRun}
            selectedInputPath={selectedInputPath}
            selectedConfigName={selectedConfigName}
            loading={loading}
            launching={launching}
            launchMessage={launchMessage}
            fieldSuggestion={activeFieldSuggestion}
            fieldLoading={fieldLoading}
            fieldMessage={fieldMessage}
            onSelectRun={handleSelectRun}
            onSelectInput={handleSelectInput}
            onSelectConfig={setSelectedConfigName}
            onGenerateFieldSuggestion={handleGenerateFieldSuggestion}
            onClearFieldSuggestion={handleClearFieldSuggestion}
            onStartBaselineRun={handleStartBaselineRun}
          />
        </section>

        {stage === "ai" ? (
          <aside className="assistant-column">
            <AIPanel
              run={aiFocusedRun}
              configs={configs}
              targetInputVideo={selectedInputPath || undefined}
              onConfigDerived={async () => {
                await refreshConfigs();
                await refreshInputs();
              }}
              onRunCreated={handleAssistantRunCreated}
            />
          </aside>
        ) : null}
      </div>
    </div>
  );
}
