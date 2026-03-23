import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";

import { AIPanel } from "../components/AIPanel";
import { ActivityIcon, ClockIcon, FileIcon, PlayIcon, SparkIcon, VideoIcon } from "../components/Icons";
import { LanguageToggle } from "../components/LanguageToggle";
import { api } from "../lib/api";
import { sortConfigsByCreatedAt } from "../lib/configs";
import { useI18n } from "../lib/i18n";
import type { AssetGroup, ConfigListItem, FieldPreview, FieldSuggestion, HealthResponse, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage, type WorkspaceStage } from "../pages/WorkspacePage";

interface StageTab {
  key: WorkspaceStage;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  title: string;
  description: string;
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

function configHasFieldSetup(raw: Record<string, unknown>): boolean {
  const filtering = (raw.filtering as Record<string, unknown> | undefined) ?? {};
  const sceneBias = (raw.scene_bias as Record<string, unknown> | undefined) ?? {};
  const groundZones = Array.isArray(sceneBias.ground_zones) ? sceneBias.ground_zones : [];
  const positiveRois = Array.isArray(sceneBias.positive_rois) ? sceneBias.positive_rois : [];
  return Boolean(filtering.roi) || groundZones.length > 0 || positiveRois.length > 0;
}

function supportsFollowCamRender(run: RunRecord): boolean {
  return (
    run.status === "completed" &&
    Boolean(run.input_video) &&
    Boolean(run.config_name || run.config_path) &&
    run.artifacts.some((artifact) => artifact.name === "ball_track.csv" || artifact.name === "ball_track.cleaned.csv")
  );
}

export function App() {
  const { copy } = useI18n();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [inputCatalog, setInputCatalog] = useState<InputCatalog>({ root_dir: "", videos: [] });
  const [configs, setConfigs] = useState<ConfigListItem[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [assetGroups, setAssetGroups] = useState<AssetGroup[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [selectedInputPath, setSelectedInputPath] = useState<string>("");
  const [selectedConfigName, setSelectedConfigName] = useState<string>("real_first_run.yaml");
  const [stage, setStage] = useState<WorkspaceStage>("baseline");
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
  const [fieldPreviews, setFieldPreviews] = useState<Record<string, FieldPreview>>({});
  const [fieldSuggestions, setFieldSuggestions] = useState<Record<string, FieldSuggestion>>({});
  const [fieldLoading, setFieldLoading] = useState(false);
  const [fieldMessage, setFieldMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refreshHealth(): Promise<HealthResponse> {
    const nextHealth = await api.getHealth();
    setHealth(nextHealth);
    return nextHealth;
  }

  async function refreshConfigs(): Promise<ConfigListItem[]> {
    const nextConfigs = sortConfigsByCreatedAt(await api.listConfigs());
    setConfigs(nextConfigs);
    return nextConfigs;
  }

  async function refreshRuns(): Promise<RunRecord[]> {
    const nextRuns = await api.listRuns();
    setRuns(nextRuns);
    return nextRuns;
  }

  async function refreshAssetGroups(): Promise<AssetGroup[]> {
    const nextGroups = await api.listAssetGroups();
    setAssetGroups(nextGroups);
    return nextGroups;
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
        const [healthData, inputData, configData, runData, assetGroupData] = await Promise.all([
          api.getHealth(),
          api.listInputs(),
          api.listConfigs(),
          api.listRuns(),
          api.listAssetGroups(),
        ]);
        if (cancelled) {
          return;
        }
        setHealth(healthData);
        setInputCatalog(inputData);
        const sortedConfigData = sortConfigsByCreatedAt(configData);
        setConfigs(sortedConfigData);
        setRuns(runData);
        setAssetGroups(assetGroupData);
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

        setSelectedConfigName((current) => pickPreferredConfigName(sortedConfigData, runData, nextSelectedInput, current));
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

  useEffect(() => {
    if (!selectedInputPath || !selectedConfigName) {
      return;
    }
    setFieldSuggestions((current) => {
      const existing = current[selectedInputPath];
      if (!existing?.source.startsWith("config:")) {
        return current;
      }
      if (existing.source === `config:${selectedConfigName}`) {
        return current;
      }
      const next = { ...current };
      delete next[selectedInputPath];
      return next;
    });
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
  const orderedConfigs = useMemo(() => sortConfigsByCreatedAt(configs), [configs]);

  const selectedVideo = inputCatalog.videos.find((item) => item.path === selectedInputPath) ?? null;
  const selectedConfig = orderedConfigs.find((item) => item.name === selectedConfigName) ?? null;
  const activeFieldPreview = selectedInputPath ? fieldPreviews[selectedInputPath] ?? null : null;
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
    setSelectedConfigName(pickPreferredConfigName(orderedConfigs, orderedRuns, path));
  }

  async function syncCreatedRun(createdRun: RunRecord) {
    const runData = await refreshRuns();
    await refreshAssetGroups();
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

  async function handleCaptureFieldPreview() {
    if (!selectedInputPath) {
      return;
    }
    setFieldLoading(true);
    setFieldMessage(null);
    try {
      const nextSampleIndex =
        activeFieldPreview && activeFieldPreview.sample_count > 1
          ? (activeFieldPreview.sample_index % activeFieldPreview.sample_count) + 1
          : undefined;
      const preview = await api.captureFieldPreview({
        input_video: selectedInputPath,
        sample_index: nextSampleIndex,
      });
      setFieldPreviews((current) => ({
        ...current,
        [selectedInputPath]: preview,
      }));
      setFieldSuggestions((current) => {
        const next = { ...current };
        delete next[selectedInputPath];
        return next;
      });
      setFieldMessage(copy.workspace.fieldPreviewReadyMessage);
    } catch (caughtError) {
      setFieldMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setFieldLoading(false);
    }
  }

  async function handleLoadFieldFromConfig() {
    if (!selectedInputPath || !selectedConfigName || !activeFieldPreview) {
      return;
    }
    setFieldLoading(true);
    setFieldMessage(null);
    try {
      const detail = await api.getConfig(selectedConfigName);
      if (!configHasFieldSetup(detail.raw)) {
        setFieldMessage(copy.workspace.fieldConfigMissing);
        return;
      }
      const suggestion = await api.suggestFieldSetup({
        input_video: selectedInputPath,
        config_name: selectedConfigName,
        frame_index: activeFieldPreview.frame_index,
      });
      setFieldSuggestions((current) => ({
        ...current,
        [selectedInputPath]: { ...suggestion, accepted: true },
      }));
      setFieldMessage(copy.workspace.fieldLoadedFromConfig);
    } catch (caughtError) {
      setFieldMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setFieldLoading(false);
    }
  }

  async function handleGenerateFieldSuggestion() {
    if (!selectedInputPath || !activeFieldPreview) {
      return;
    }
    setFieldLoading(true);
    setFieldMessage(null);
    try {
      const suggestion = await api.suggestFieldSetup({
        input_video: selectedInputPath,
        frame_index: activeFieldPreview.frame_index,
      });
      setFieldSuggestions((current) => ({
        ...current,
        [selectedInputPath]: { ...suggestion, accepted: false },
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

  function handleUpdateFieldSuggestion(nextSuggestion: FieldSuggestion) {
    if (!selectedInputPath) {
      return;
    }
    setFieldSuggestions((current) => ({
      ...current,
      [selectedInputPath]: nextSuggestion,
    }));
  }

  function handleAcceptFieldSuggestion(nextSuggestion: FieldSuggestion) {
    if (!selectedInputPath) {
      return;
    }
    setFieldSuggestions((current) => ({
      ...current,
      [selectedInputPath]: { ...nextSuggestion, accepted: true },
    }));
    setFieldMessage(copy.workspace.fieldAcceptedMessage);
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
        config_patch: activeFieldSuggestion?.accepted ? activeFieldSuggestion.config_patch : undefined,
        enable_postprocess: true,
        enable_follow_cam: true,
        notes: `Workspace baseline run for ${selectedVideo?.name ?? "selected input"}${
          activeFieldSuggestion?.accepted ? " | field setup applied" : ""
        }`,
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
    setStage("history");
  }

  async function handleCreateFollowCamRender(
    runId: string,
    options: {
      prefer_cleaned_track: boolean;
      draw_ball_marker: boolean;
      draw_frame_text: boolean;
    },
  ) {
    const createdRun = await api.createFollowCamRender(runId, options);
    await syncCreatedRun(createdRun);
    setStage("history");
    return createdRun;
  }

  async function handleDeleteInputVideo(name: string) {
    await api.deleteInput(name);
    const [nextInputs] = await Promise.all([refreshInputs(), refreshHealth(), refreshAssetGroups()]);
    const nextSelectedInput =
      selectedInputPath && nextInputs.videos.some((item) => item.path === selectedInputPath)
        ? selectedInputPath
        : (nextInputs.videos[0]?.path ?? "");
    setSelectedInputPath(nextSelectedInput);
    setSelectedConfigName((current) => pickPreferredConfigName(orderedConfigs, orderedRuns, nextSelectedInput, current));
  }

  async function handleDeleteConfig(name: string) {
    await api.deleteConfig(name);
    const [nextConfigs] = await Promise.all([refreshConfigs(), refreshHealth(), refreshAssetGroups()]);
    setSelectedConfigName((current) => pickPreferredConfigName(nextConfigs, orderedRuns, selectedInputPath, current === name ? "" : current));
  }

  async function handleDeleteRunOutput(runId: string) {
    await api.deleteRunOutput(runId);
    const [nextRuns] = await Promise.all([refreshRuns(), refreshHealth(), refreshAssetGroups()]);
    setSelectedRun((current) => {
      if (current?.run_id === runId) {
        return nextRuns[0] ?? null;
      }
      return current ? nextRuns.find((item) => item.run_id === current.run_id) ?? null : nextRuns[0] ?? null;
    });
  }

  const stageTabs = useMemo<StageTab[]>(
    () => [
      {
        key: "baseline",
        icon: PlayIcon,
        title: copy.workspace.flowRunTitle,
        description: `${copy.workspace.selectEyebrow}: ${copy.workspace.flowRunDetail}`,
        state: stage === "baseline" ? "current" : focusedRun ? "complete" : "upcoming",
      },
      {
        key: "ai",
        icon: SparkIcon,
        title: copy.workspace.flowAiTitle,
        description: `${copy.workspace.focusEyebrow}: ${copy.workspace.flowAiDetail}`,
        state: stage === "ai" ? "current" : aiFocusedRun ? "complete" : "upcoming",
      },
      {
        key: "deliverable",
        icon: VideoIcon,
        title: copy.workspace.flowDeliverableTitle,
        description: `${copy.workspace.deliverableEyebrow}: ${copy.workspace.flowDeliverableDetail}`,
        state:
          stage === "deliverable"
            ? "current"
            : orderedRuns.some((run) => supportsFollowCamRender(run))
              ? "complete"
              : "upcoming",
      },
      {
        key: "history",
        icon: ClockIcon,
        title: copy.workspace.flowHistoryTitle,
        description: `${copy.workspace.historyEyebrow}: ${copy.workspace.flowHistoryDetail}`,
        state: stage === "history" ? "current" : latestHistoryRun ? "complete" : "upcoming",
      },
    ],
    [aiFocusedRun, copy, focusedRun, latestHistoryRun, orderedRuns, stage],
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
        {stageTabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              type="button"
              key={tab.key}
              className={`step-tab ${stage === tab.key ? "active" : ""} ${tab.state}`}
              onClick={() => setStage(tab.key)}
              title={tab.description}
              aria-label={`${tab.title}. ${tab.description}`}
            >
              <div className="step-tab-icon-shell">
                <Icon className="section-icon" />
              </div>
              <div className="step-tab-copy simple">
                <strong>{tab.title}</strong>
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
            configs={orderedConfigs}
            assetGroups={assetGroups}
            runs={orderedRuns}
            selectedRun={stage === "ai" ? aiFocusedRun : focusedRun}
            selectedInputPath={selectedInputPath}
            selectedConfigName={selectedConfigName}
            loading={loading}
            launching={launching}
            launchMessage={launchMessage}
            fieldPreview={activeFieldPreview}
            fieldSuggestion={activeFieldSuggestion}
            fieldLoading={fieldLoading}
            fieldMessage={fieldMessage}
            canLoadFieldFromConfig={Boolean(selectedConfig)}
            canStartBaseline={!loading && !launching && Boolean(selectedInputPath) && Boolean(selectedConfig)}
            onSelectRun={handleSelectRun}
            onSelectInput={handleSelectInput}
            onSelectConfig={setSelectedConfigName}
            onCaptureFieldPreview={handleCaptureFieldPreview}
            onLoadFieldFromConfig={handleLoadFieldFromConfig}
            onGenerateFieldSuggestion={handleGenerateFieldSuggestion}
            onClearFieldSuggestion={handleClearFieldSuggestion}
            onUpdateFieldSuggestion={handleUpdateFieldSuggestion}
            onAcceptFieldSuggestion={handleAcceptFieldSuggestion}
            onStartBaselineRun={handleStartBaselineRun}
            onCreateFollowCamRender={handleCreateFollowCamRender}
            onDeleteInputVideo={handleDeleteInputVideo}
            onDeleteConfig={handleDeleteConfig}
            onDeleteRunOutput={handleDeleteRunOutput}
          />
        </section>

        {stage === "ai" ? (
          <aside className="assistant-column">
            <AIPanel
              run={aiFocusedRun}
              configs={orderedConfigs}
              targetInputVideo={selectedInputPath || undefined}
              onConfigDerived={async () => {
                await refreshConfigs();
                await refreshInputs();
                await refreshAssetGroups();
              }}
              onRunCreated={handleAssistantRunCreated}
            />
          </aside>
        ) : null}
      </div>
    </div>
  );
}
