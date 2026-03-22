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
import { FileIcon, LayersIcon, PlayIcon, SparkIcon, WandIcon } from "./Icons";

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
    steady:
      "\u8ba9\u955c\u5934\u8fd0\u52a8\u66f4\u7a33\u5b9a\uff0c\u51cf\u5c11\u5feb\u901f\u7538\u955c\uff0c\u5e76\u5728\u8282\u594f\u53d8\u5316\u65f6\u5c3d\u91cf\u4fdd\u6301\u7403\u53ef\u89c1\u3002",
    recover:
      "\u51cf\u5c11\u4e22\u7403\u533a\u95f4\uff0c\u4f18\u5148\u4fdd\u8bc1\u7403\u7559\u5728\u753b\u9762\u5185\uff0c\u5373\u4f7f\u88c1\u5207\u9700\u8981\u66f4\u4fdd\u5b88\u4e00\u4e9b\u3002",
    clean:
      "\u4f18\u5148\u5f97\u5230\u66f4\u5e72\u51c0\u7684\u8f68\u8ff9\u548c\u66f4\u987a\u6ed1\u7684\u8ddf\u968f\u955c\u5934\uff0c\u5373\u4f7f\u8fd9\u610f\u5473\u7740\u88c1\u5207\u7b56\u7565\u8981\u7a0d\u5fae\u4fdd\u5b88\u4e00\u4e9b\u3002",
  },
};

const PANEL_COPY: Record<
  LanguageCode,
  {
    explainButton: string;
    explainHint: string;
    explanationTitle: string;
    explanationEmpty: string;
    suggestTitle: string;
    suggestHint: string;
    updateButton: string;
    updateHint: string;
    runHint: string;
    readyMessage: string;
    updatedMessage: string;
    noPatchReady: string;
  }
> = {
  en: {
    explainButton: "Explain selected run",
    explainHint: "Choose one finished run, then explain it manually so tokens are only spent when you actually need AI.",
    explanationTitle: "AI explanation",
    explanationEmpty: "No explanation yet. Pick a run in step 2, then click explain.",
    suggestTitle: "Suggested next config",
    suggestHint: "After the explanation finishes, AI creates the first config suggestion automatically.",
    updateButton: "Update suggestion",
    updateHint: "If you want a different direction, edit the objective and regenerate the suggestion.",
    runHint: "After the suggestion looks right, start the next task with the generated config.",
    readyMessage: "Explanation finished and the first suggestion is ready.",
    updatedMessage: "Suggestion updated.",
    noPatchReady: "No AI patch is ready yet.",
  },
  zh: {
    explainButton: "\u89e3\u91ca\u5df2\u9009\u7ed3\u679c",
    explainHint: "\u5148\u9009\u4e00\u4e2a\u5df2\u8dd1\u51fa\u6765\u7684 run\uff0c\u518d\u624b\u52a8\u70b9\u51fb\u89e3\u91ca\uff0c\u8fd9\u6837\u53ea\u5728\u771f\u6b63\u9700\u8981 AI \u65f6\u624d\u6d88\u8017 token\u3002",
    explanationTitle: "AI \u89e3\u91ca",
    explanationEmpty: "\u8fd8\u6ca1\u6709 AI \u89e3\u91ca\u3002\u5148\u5728\u7b2c\u4e8c\u6b65\u9009\u4e00\u4e2a run\uff0c\u7136\u540e\u70b9\u51fb\u89e3\u91ca\u3002",
    suggestTitle: "\u5efa\u8bae\u7684\u65b0\u914d\u7f6e",
    suggestHint: "\u89e3\u91ca\u5b8c\u6210\u540e\uff0cAI \u4f1a\u81ea\u52a8\u5148\u751f\u6210\u4e00\u7248\u65b0\u914d\u7f6e\u5efa\u8bae\u3002",
    updateButton: "\u66f4\u65b0\u5efa\u8bae",
    updateHint: "\u5982\u679c\u4f60\u60f3\u6362\u4e2a\u65b9\u5411\uff0c\u5148\u6539\u4e0b\u9762\u7684\u76ee\u6807\uff0c\u518d\u91cd\u65b0\u751f\u6210\u5efa\u8bae\u3002",
    runHint: "\u914d\u7f6e\u786e\u8ba4\u597d\u4e4b\u540e\uff0c\u5c31\u53ef\u4ee5\u76f4\u63a5\u542f\u52a8\u4e0b\u4e00\u4e2a\u4efb\u52a1\u3002",
    readyMessage: "\u89e3\u91ca\u5b8c\u6210\uff0c\u7b2c\u4e00\u7248\u5efa\u8bae\u5df2\u751f\u6210\u3002",
    updatedMessage: "\u5efa\u8bae\u5df2\u66f4\u65b0\u3002",
    noPatchReady: "\u8fd8\u6ca1\u6709\u53ef\u4ee5\u542f\u52a8\u7684 AI \u914d\u7f6e\u8865\u4e01\u3002",
  },
};

export function AIPanel({ run, configs, targetInputVideo, onConfigDerived, onRunCreated }: AIPanelProps) {
  const { copy, language } = useI18n();
  const presetObjectives = OBJECTIVE_PRESETS[language];
  const panelCopy = PANEL_COPY[language];
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
    setExplanation(null);
    setSuggestion(null);
    setDiffPreview(null);
    setStatusMessage(null);
    setActivityLabel(null);
  }, [run?.run_id]);

  async function buildSuggestion(targetRun: RunRecord) {
    const nextSuggestion = await api.aiRecommend({
      run_id: targetRun.run_id,
      objective,
      language,
    });
    setSuggestion(nextSuggestion);

    if (!targetRun.config_name) {
      setDiffPreview(null);
      return;
    }

    const nextDiff = await api.aiConfigDiff({
      base_config_name: targetRun.config_name,
      patch: nextSuggestion.patch ?? {},
      output_name: nextSuggestion.outputNameSuggestion,
    });
    setDiffPreview(nextDiff);
  }

  async function handleExplain() {
    if (!run) {
      return;
    }
    setLoading(true);
    setStatusMessage(null);
    setActivityLabel(copy.ai.activityExplain);
    try {
      const nextExplanation = await api.aiExplain({
        run_id: run.run_id,
        config_name: run.config_name,
        focus: objective,
        language,
      });
      setExplanation(nextExplanation);
      setActivityLabel(copy.ai.activityRecommend);
      await buildSuggestion(run);
      setStatusMessage(panelCopy.readyMessage);
    } catch (caughtError) {
      setStatusMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setActivityLabel(null);
      setLoading(false);
    }
  }

  async function handleRefreshSuggestion() {
    if (!run || !explanation) {
      return;
    }
    setLoading(true);
    setStatusMessage(null);
    setActivityLabel(copy.ai.activityRecommend);
    try {
      await buildSuggestion(run);
      setStatusMessage(panelCopy.updatedMessage);
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
        throw new Error(panelCopy.noPatchReady);
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

        <p className="notice-line subtle">{panelCopy.explainHint}</p>

        <button type="button" className="secondary-button icon-button" onClick={handleExplain} disabled={!run || loading}>
          <SparkIcon className="button-icon" />
          <span>{panelCopy.explainButton}</span>
        </button>

        {activityLabel ? <p className="notice-line subtle">{activityLabel}</p> : null}
        {statusMessage ? <p className="notice-line">{statusMessage}</p> : null}

        <div className="signal-card">
          <p className="meta-label">{panelCopy.explanationTitle}</p>
          <p className="assistant-summary-copy">{explanation?.summary ?? panelCopy.explanationEmpty}</p>
          {evidencePoints.length ? (
            <ul className="flat-list compact-evidence">
              {evidencePoints.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </section>

      <section className="assistant-card">
        <div className="panel-header title-row">
          <FileIcon className="section-icon" />
          <div>
            <h3>{panelCopy.suggestTitle}</h3>
            <p className="muted">{panelCopy.suggestHint}</p>
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

        <p className="muted">{panelCopy.updateHint}</p>

        <div className="assistant-actions">
          <button
            type="button"
            className="secondary-button icon-button"
            onClick={handleRefreshSuggestion}
            disabled={!run || !explanation || loading}
          >
            <SparkIcon className="button-icon" />
            <span>{panelCopy.updateButton}</span>
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

        <p className="muted">{panelCopy.runHint}</p>

        {suggestion ? (
          <div className="signal-card">
            <p className="meta-label">{copy.ai.recommendation}</p>
            <p className="assistant-reco">{suggestion.recommendation}</p>
            <p className="muted">{suggestion.expected_tradeoff}</p>
          </div>
        ) : null}

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

        {lastDerivedConfig ? (
          <p className="muted mono">
            {copy.ai.latestDerived}: {lastDerivedConfig.name}
          </p>
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
