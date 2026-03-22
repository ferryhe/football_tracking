import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { useI18n, type LanguageCode } from "../lib/i18n";
import type {
  AIConfigDiffResponse,
  AIExplainResponse,
  AssistantSuggestion,
  ConfigDetail,
  ConfigListItem,
  RunRecord,
} from "../lib/types";
import { ActivityIcon, FileIcon, LayersIcon, PlayIcon, SparkIcon, WandIcon } from "./Icons";

interface AIPanelProps {
  run: RunRecord | null;
  configs: ConfigListItem[];
  targetInputVideo?: string;
  onConfigDerived: () => Promise<ConfigListItem[]> | Promise<void> | void;
  onRunCreated: (run: RunRecord) => Promise<void> | void;
}

const OBJECTIVE_PRESETS: Record<LanguageCode, Record<"steady" | "recover" | "clean", string>> = {
  en: {
    steady: "Keep the camera motion steady, reduce whip pans, and preserve ball visibility during fast transitions.",
    recover: "Reduce lost-ball stretches and favor tracking choices that keep the ball in frame even if the crop becomes less aggressive.",
    clean: "Prefer cleaner filtered tracks and smoother follow-cam behavior, even if that means a slightly more conservative crop.",
  },
  zh: {
    steady: "让镜头移动更稳定，减少快速甩镜，并在高速转换里尽量保持球始终可见。",
    recover: "减少长时间丢球的区段，优先保证球尽量留在画面中，即便裁切需要更保守一些。",
    clean: "优先得到更干净的轨迹和更顺滑的跟随镜头，即便这意味着裁切策略要稍微保守一些。",
  },
};

export function AIPanel({ run, configs, targetInputVideo, onConfigDerived, onRunCreated }: AIPanelProps) {
  const { copy, language } = useI18n();
  const presetObjectives = OBJECTIVE_PRESETS[language];
  const [objective, setObjective] = useState<string>(presetObjectives.steady);
  const [explanation, setExplanation] = useState<AIExplainResponse | null>(null);
  const [suggestion, setSuggestion] = useState<AssistantSuggestion | null>(null);
  const [diffPreview, setDiffPreview] = useState<AIConfigDiffResponse | null>(null);
  const [lastDerivedConfig, setLastDerivedConfig] = useState<ConfigDetail | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [activityLabel, setActivityLabel] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const nextObjective =
      objective === OBJECTIVE_PRESETS.en.recover || objective === OBJECTIVE_PRESETS.zh.recover
        ? presetObjectives.recover
        : objective === OBJECTIVE_PRESETS.en.clean || objective === OBJECTIVE_PRESETS.zh.clean
          ? presetObjectives.clean
          : objective === OBJECTIVE_PRESETS.en.steady || objective === OBJECTIVE_PRESETS.zh.steady
            ? presetObjectives.steady
            : objective;
    if (nextObjective !== objective) {
      setObjective(nextObjective);
    }
  }, [language, objective, presetObjectives]);

  useEffect(() => {
    let cancelled = false;

    async function loadExplain() {
      if (!run) {
        setExplanation(null);
        setSuggestion(null);
        setDiffPreview(null);
        setLastDerivedConfig(null);
        return;
      }
      setLoading(true);
      setActivityLabel(copy.ai.activityExplain);
      try {
        const explain = await api.aiExplain({
          run_id: run.run_id,
          config_name: run.config_name,
          focus: objective,
          language,
        });
        if (!cancelled) {
          setExplanation(explain);
        }
      } catch (caughtError) {
        if (!cancelled) {
          setExplanation({
            summary: caughtError instanceof Error ? caughtError.message : String(caughtError),
            evidence: [],
          });
        }
      } finally {
        if (!cancelled) {
          setActivityLabel(null);
          setLoading(false);
        }
      }
    }

    void loadExplain();
    return () => {
      cancelled = true;
    };
  }, [copy.ai.activityExplain, language, objective, run]);

  async function handleRecommend() {
    if (!run) {
      return;
    }
    setLoading(true);
    setStatusMessage(null);
    setActivityLabel(copy.ai.activityRecommend);
    try {
      const nextSuggestion = await api.aiRecommend({
        run_id: run.run_id,
        objective,
        language,
      });
      setSuggestion(nextSuggestion);
      if (run.config_name) {
        const nextDiff = await api.aiConfigDiff({
          base_config_name: run.config_name,
          patch: nextSuggestion.patch ?? {},
          output_name: nextSuggestion.outputNameSuggestion,
        });
        setDiffPreview(nextDiff);
      }
    } catch (caughtError) {
      setStatusMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setActivityLabel(null);
      setLoading(false);
    }
  }

  async function deriveCurrentConfig(): Promise<ConfigDetail | null> {
    if (!run?.config_name || !suggestion?.patch || !diffPreview) {
      return null;
    }
    const derived = await api.deriveConfig({
      base_config_name: run.config_name,
      output_name: diffPreview.output_name,
      patch: suggestion.patch,
    });
    setLastDerivedConfig(derived);
    await onConfigDerived();
    return derived;
  }

  async function handleApplyAndRun() {
    if (!run) {
      return;
    }
    setLoading(true);
    setStatusMessage(null);
    setActivityLabel(copy.ai.activityRun);
    try {
      const derived = await deriveCurrentConfig();
      if (!derived) {
        throw new Error("No AI patch is ready yet.");
      }
      const createdRun = await api.createRun({
        config_name: derived.name,
        input_video: targetInputVideo ?? run.input_video ?? undefined,
        enable_postprocess: true,
        enable_follow_cam: true,
        notes: `AI objective: ${objective}`,
      });
      setStatusMessage(`${createdRun.run_id} | ${derived.name}`);
      await onRunCreated(createdRun);
    } catch (caughtError) {
      setStatusMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setActivityLabel(null);
      setLoading(false);
    }
  }

  const evidencePoints = explanation?.evidence ?? [];
  const patchLines = suggestion?.patchPreview ?? [];
  const assistantStages = [
    {
      title: copy.ai.stageEvidence,
      detail: run ? run.run_id : copy.ai.stageEvidenceNone,
      state: run ? "complete" : "current",
      icon: LayersIcon,
    },
    {
      title: copy.ai.stageExplain,
      detail: explanation?.summary ? explanation.summary : copy.ai.stageExplainNone,
      state: explanation ? "complete" : run ? "current" : "upcoming",
      icon: ActivityIcon,
    },
    {
      title: copy.ai.stageRecommend,
      detail: suggestion ? suggestion.outputNameSuggestion ?? diffPreview?.output_name ?? copy.ai.nextConfig : copy.ai.stageRecommendNone,
      state: suggestion ? "complete" : run ? "current" : "upcoming",
      icon: SparkIcon,
    },
    {
      title: copy.ai.stageDerive,
      detail: diffPreview ? `generated/${diffPreview.output_name}.yaml` : copy.ai.stageDeriveNone,
      state: diffPreview ? "current" : "upcoming",
      icon: FileIcon,
    },
  ] as const;

  return (
    <div className="assistant-shell">
      <section className="assistant-card primary">
        <div className="assistant-header title-row">
          <WandIcon className="section-icon" />
          <div>
            <p className="eyebrow">{copy.ai.eyebrow}</p>
            <h3>{copy.ai.title}</h3>
            <p className="muted">{copy.ai.subtitle}</p>
          </div>
        </div>

        <div className="assistant-stage-grid">
          {assistantStages.map((stage, index) => {
            const Icon = stage.icon;
            return (
              <article key={stage.title} className={`assistant-stage ${stage.state}`}>
                <span className="assistant-stage-index">{index + 1}</span>
                <div className="assistant-stage-copy">
                  <div className="title-row compact">
                    <Icon className="section-icon tiny" />
                    <p className="meta-label">{stage.title}</p>
                  </div>
                  <p className="muted">{stage.detail}</p>
                </div>
              </article>
            );
          })}
        </div>

        <div className="assistant-context">
          <div className="meta-row">
            <span className="meta-label">{copy.ai.evidenceRun}</span>
            <span className="mono">{run?.run_id ?? copy.common.noneSelected}</span>
          </div>
          <div className="meta-row">
            <span className="meta-label">{copy.ai.targetVideo}</span>
            <span className="mono">{targetInputVideo ?? run?.input_video ?? copy.common.noneSelected}</span>
          </div>
          <div className="meta-row">
            <span className="meta-label">{copy.ai.knownConfigs}</span>
            <strong>{configs.length}</strong>
          </div>
        </div>

        <label className="assistant-form-label">
          <span className="meta-label">{copy.ai.objective}</span>
          <textarea rows={5} value={objective} onChange={(event) => setObjective(event.target.value)} />
        </label>

        <div className="preset-row" aria-label={copy.ai.objective}>
          <button
            type="button"
            className={`chip-button ${objective === presetObjectives.steady ? "selected" : ""}`}
            onClick={() => setObjective(presetObjectives.steady)}
          >
            {copy.ai.presetSteady}
          </button>
          <button
            type="button"
            className={`chip-button ${objective === presetObjectives.recover ? "selected" : ""}`}
            onClick={() => setObjective(presetObjectives.recover)}
          >
            {copy.ai.presetRecover}
          </button>
          <button
            type="button"
            className={`chip-button ${objective === presetObjectives.clean ? "selected" : ""}`}
            onClick={() => setObjective(presetObjectives.clean)}
          >
            {copy.ai.presetClean}
          </button>
        </div>

        <div className="assistant-actions">
          <button type="button" className="secondary-button icon-button" onClick={handleRecommend} disabled={!run || loading}>
            <SparkIcon className="button-icon" />
            <span>{loading ? copy.ai.buttonThinking : copy.ai.buttonRecommend}</span>
          </button>
          <button
            type="button"
            className="primary-button icon-button"
            onClick={handleApplyAndRun}
            disabled={!run || !suggestion || !diffPreview || loading}
          >
            <PlayIcon className="button-icon" />
            <span>{copy.ai.buttonRun}</span>
          </button>
        </div>

        {activityLabel ? <p className="notice-line subtle">{activityLabel}</p> : null}
        {statusMessage ? <p className="notice-line">{statusMessage}</p> : null}
        {lastDerivedConfig ? (
          <p className="muted mono">
            {copy.ai.latestDerived}: {lastDerivedConfig.name}
          </p>
        ) : null}
      </section>

      <section className="assistant-card">
        <div className="panel-header title-row">
          <SparkIcon className="section-icon" />
          <div>
            <h3>{copy.ai.readoutTitle}</h3>
            <p className="muted">{copy.ai.readoutSubtitle}</p>
          </div>
        </div>
        <p className="assistant-summary-copy">{explanation?.summary ?? copy.ai.readoutFallback}</p>

        <div className="mini-stat-grid assistant-mini-grid">
          <article className="mini-stat icon-card">
            <LayersIcon className="section-icon" />
            <p className="meta-label">{copy.ai.evidencePoints}</p>
            <strong>{evidencePoints.length}</strong>
            <p className="muted">{copy.ai.readoutSubtitle}</p>
          </article>
          <article className="mini-stat icon-card">
            <FileIcon className="section-icon" />
            <p className="meta-label">{copy.ai.patchLines}</p>
            <strong>{patchLines.length}</strong>
            <p className="muted">{copy.ai.readoutSubtitle}</p>
          </article>
          <article className="mini-stat icon-card">
            <WandIcon className="section-icon" />
            <p className="meta-label">{copy.ai.nextConfig}</p>
            <strong className="mono">{diffPreview?.output_name ?? suggestion?.outputNameSuggestion ?? "-"}</strong>
            <p className="muted">{copy.ai.readoutSubtitle}</p>
          </article>
        </div>

        {suggestion ? (
          <div className="signal-card">
            <p className="meta-label">{copy.ai.recommendation}</p>
            <p className="assistant-reco">{suggestion.recommendation}</p>
            <p className="muted">{suggestion.expected_tradeoff}</p>
          </div>
        ) : null}

        {evidencePoints.length ? (
          <ul className="flat-list compact-evidence">
            {evidencePoints.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
      </section>

      {suggestion ? (
        <details className="assistant-card detail-card">
          <summary>{copy.ai.evidencePatch}</summary>
          <p className="muted">{suggestion.diagnosis}</p>
          <ul className="flat-list">
            {suggestion.evidence.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <pre className="code-block compact">{(suggestion.patchPreview ?? []).join("\n")}</pre>
        </details>
      ) : null}

      {diffPreview ? (
        <details className="assistant-card detail-card">
          <summary>{copy.ai.configDiff}</summary>
          <p className="muted mono">{diffPreview.base_config_name}</p>
          <p className="muted mono">generated/{diffPreview.output_name}.yaml</p>
          <pre className="code-block compact">{diffPreview.patch_preview.join("\n")}</pre>
        </details>
      ) : null}
    </div>
  );
}
