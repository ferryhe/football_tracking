import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";

import { AIPanel } from "../components/AIPanel";
import { ActivityIcon, LayersIcon, SparkIcon, VideoIcon } from "../components/Icons";
import { LanguageToggle } from "../components/LanguageToggle";
import { api } from "../lib/api";
import { useI18n } from "../lib/i18n";
import type { ConfigListItem, HealthResponse, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage } from "../pages/WorkspacePage";

interface TopTile {
  key: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  label: string;
  value: string;
  detail: string;
  state: "complete" | "current" | "upcoming";
}

function formatPathTail(path: string | null | undefined): string {
  if (!path) {
    return "";
  }
  const pieces = path.split(/[\\/]/).filter(Boolean);
  return pieces.length ? pieces[pieces.length - 1] : path;
}

function formatVideoSize(sizeBytes: number): string {
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function App() {
  const { copy, formatDateTime } = useI18n();
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

  const topTiles = useMemo<TopTile[]>(
    () => [
      {
        key: "input",
        icon: VideoIcon,
        label: copy.header.tileInput,
        value: selectedVideo?.name ?? copy.header.tileInputEmpty,
        detail: selectedVideo
          ? `${formatDateTime(selectedVideo.modified_at)} | ${formatVideoSize(selectedVideo.size_bytes)}`
          : copy.workspace.inputSubtitle,
        state: selectedVideo ? "complete" : "current",
      },
      {
        key: "baseline",
        icon: LayersIcon,
        label: copy.header.tileBaseline,
        value: selectedConfig?.name ?? copy.header.tileBaselineEmpty,
        detail: selectedConfig?.output_dir ? formatPathTail(selectedConfig.output_dir) : copy.workspace.baselineSubtitle,
        state: selectedConfig ? "complete" : selectedVideo ? "current" : "upcoming",
      },
      {
        key: "evidence",
        icon: ActivityIcon,
        label: copy.header.tileEvidence,
        value: selectedRun?.run_id ?? copy.header.tileEvidenceEmpty,
        detail: selectedRun
          ? `${selectedRun.artifacts.length} ${copy.workspace.artifacts.toLowerCase()}`
          : copy.workspace.focusSubtitle,
        state: selectedRun ? "complete" : "upcoming",
      },
      {
        key: "ai",
        icon: SparkIcon,
        label: copy.header.tileAi,
        value: selectedRun ? copy.header.tileAiReady : copy.header.tileAiLocked,
        detail: selectedRun ? copy.ai.subtitle : copy.workspace.noFocusBody,
        state: selectedRun ? "current" : "upcoming",
      },
    ],
    [copy, formatDateTime, selectedConfig, selectedRun, selectedVideo],
  );

  return (
    <div className="workspace-app">
      <header className="topbar">
        <div className="topbar-copy-block">
          <p className="eyebrow">{copy.header.eyebrow}</p>
          <h1>{copy.header.title}</h1>
          <p className="muted topbar-copy">{copy.header.subtitle}</p>
        </div>
        <div className="header-meta">
          <div className={`status-pill ${health?.status === "ok" ? "ok" : "warn"}`}>
            <ActivityIcon className="section-icon tiny" />
            <span>{loading ? copy.common.loading : error ? copy.common.offline : copy.common.backendOk}</span>
          </div>
          <div className="header-chip">
            <span className="meta-label">{copy.header.activeTask}</span>
            <strong className="mono">{activeRun?.run_id ?? health?.active_run_id ?? copy.common.idle}</strong>
          </div>
          <div className="header-chip wide">
            <span className="meta-label">{copy.header.inputRoot}</span>
            <strong className="mono">{inputCatalog.root_dir || copy.common.unavailable}</strong>
          </div>
          <LanguageToggle />
        </div>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <section className="top-tile-strip" aria-label="Primary workspace status">
        {topTiles.map((tile) => {
          const Icon = tile.icon;
          return (
            <article key={tile.key} className={`top-tile ${tile.state}`}>
              <div className="top-tile-icon-shell">
                <Icon className="section-icon" />
              </div>
              <div className="top-tile-copy">
                <p className="meta-label">{tile.label}</p>
                <strong>{tile.value}</strong>
                <p className="muted">{tile.detail}</p>
              </div>
            </article>
          );
        })}
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
