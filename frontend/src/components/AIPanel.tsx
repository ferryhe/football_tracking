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
import { FileIcon, PlayIcon, SparkIcon, WandIcon } from "./Icons";

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
    suggestionTitle: string;
    suggestionEmpty: string;
    objectiveDetails: string;
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
    explainHint: "Pick the focused run above first, then trigger AI only when you need an explanation.",
    explanationTitle: "AI explanation",
    explanationEmpty: "No explanation yet. Pick a run in step 2, then click explain.",
    suggestionTitle: "Suggested next config",
    suggestionEmpty: "The first suggestion will appear after the explanation finishes.",
    objectiveDetails: "Adjust objective",
    updateButton: "Update suggestion",
    updateHint: "Edit the objective only when you want a different direction.",
    runHint: "When the suggested config looks right, start the next task.",
    readyMessage: "Explanation finished and the first suggestion is ready.",
    updatedMessage: "Suggestion updated.",
    noPatchReady: "No AI patch is ready yet.",
  },
  zh: {
    explainButton: "\u89e3\u91ca\u5df2\u9009\u7ed3\u679c",
    explainHint: "\u5148\u5728\u4e0a\u9762\u9009\u597d\u7126\u70b9 run\uff0c\u771f\u6b63\u9700\u8981 AI \u89e3\u91ca\u65f6\u518d\u624b\u52a8\u89e6\u53d1\u3002",
    explanationTitle: "AI \u89e3\u91ca",
    explanationEmpty: "\u8fd8\u6ca1\u6709 AI \u89e3\u91ca\u3002\u5148\u5728\u7b2c\u4e8c\u6b65\u9009\u4e00\u4e2a run\uff0c\u7136\u540e\u70b9\u51fb\u89e3\u91ca\u3002",
    suggestionTitle: "\u5efa\u8bae\u7684\u65b0\u914d\u7f6e",
    suggestionEmpty: "\u89e3\u91ca\u5b8c\u6210\u540e\uff0c\u7b2c\u4e00\u7248\u5efa\u8bae\u4f1a\u51fa\u73b0\u5728\u8fd9\u91cc\u3002",
    objectiveDetails: "\u8c03\u6574\u76ee\u6807",
    updateButton: "\u66f4\u65b0\u5efa\u8bae",
    updateHint: "\u53ea\u6709\u60f3\u6362\u65b9\u5411\u65f6\uff0c\u624d\u9700\u8981\u6539\u4e0b\u9762\u7684\u76ee\u6807\u3002",
    runHint: "\u786e\u8ba4\u65b0\u914d\u7f6e\u6ca1\u95ee\u9898\u540e\uff0c\u5c31\u53ef\u4ee5\u76f4\u63a5\u542f\u52a8\u4e0b\u4e00\u4e2a\u4efb\u52a1\u3002",
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
    setLastDerivedConfig(null);
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

  const nextConfigName = diffPreview?.output_name ?? suggestion?.outputNameSuggestion ?? "-";

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

        <div className="assistant-context compact-context">
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

        <div className="assistant-actions compact-actions">
          <button type="button" className="secondary-button icon-button" onClick={handleExplain} disabled={!run || loading}>
            <SparkIcon className="button-icon" />
            <span>{panelCopy.explainButton}</span>
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

        <p className="notice-line subtle">{panelCopy.explainHint}</p>
        {activityLabel ? <p className="notice-line subtle">{activityLabel}</p> : null}
        {statusMessage ? <p className="notice-line">{statusMessage}</p> : null}

        <div className="signal-card">
          <p className="meta-label">{panelCopy.explanationTitle}</p>
          <p className="assistant-summary-copy">{explanation?.summary ?? panelCopy.explanationEmpty}</p>
        </div>

        <div className="signal-card">
          <div className="meta-row">
            <span className="meta-label">{panelCopy.suggestionTitle}</span>
            <strong className="mono">{nextConfigName}</strong>
          </div>
          {suggestion ? (
            <>
              <p className="assistant-reco">{suggestion.recommendation}</p>
              <p className="muted">{suggestion.expected_tradeoff}</p>
              <p className="muted">{panelCopy.runHint}</p>
            </>
          ) : (
            <p className="assistant-summary-copy">{panelCopy.suggestionEmpty}</p>
          )}
          {lastDerivedConfig ? (
            <p className="muted mono">
              {copy.ai.latestDerived}: {lastDerivedConfig.name}
            </p>
          ) : null}
        </div>
      </section>

      <details className="assistant-card detail-card">
        <summary>{panelCopy.objectiveDetails}</summary>
        <p className="muted">{panelCopy.updateHint}</p>

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

        <div className="assistant-actions compact-actions">
          <button
            type="button"
            className="secondary-button icon-button"
            onClick={handleRefreshSuggestion}
            disabled={!run || !explanation || loading}
          >
            <SparkIcon className="button-icon" />
            <span>{panelCopy.updateButton}</span>
          </button>
        </div>
      </details>

      {suggestion ? (
        <details className="assistant-card detail-card">
          <summary>{copy.ai.evidencePatch}</summary>
          <p className="muted detail-lead">{suggestion.diagnosis}</p>
          <ul className="flat-list evidence-list">
            {suggestion.evidence.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <pre className="code-block compact patch-block">{(suggestion.patchPreview ?? []).join("\n")}</pre>
        </details>
      ) : null}

      {diffPreview ? (
        <details className="assistant-card detail-card">
          <summary>{copy.ai.configDiff}</summary>
          <p className="muted mono">{diffPreview.base_config_name}</p>
          <p className="muted mono">generated/{diffPreview.output_name}.yaml</p>
          <pre className="code-block compact patch-block">{diffPreview.patch_preview.join("\n")}</pre>
        </details>
      ) : null}
    </div>
  );
}
