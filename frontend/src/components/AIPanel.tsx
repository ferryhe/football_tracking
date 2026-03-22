import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type {
  AIConfigDiffResponse,
  AIExplainResponse,
  AssistantSuggestion,
  ConfigDetail,
  ConfigListItem,
  RunRecord,
} from "../lib/types";

interface AIPanelProps {
  run: RunRecord | null;
  configs: ConfigListItem[];
  targetInputVideo?: string;
  onConfigDerived: () => Promise<ConfigListItem[]> | Promise<void> | void;
  onRunCreated: (run: RunRecord) => Promise<void> | void;
}

const OBJECTIVE_PRESETS = [
  {
    label: "Steadier camera",
    value: "Keep the camera motion steady, reduce whip pans, and preserve ball visibility during fast transitions.",
  },
  {
    label: "Recover lost ball",
    value: "Reduce lost-ball stretches and favor tracking choices that keep the ball in frame even if the crop becomes less aggressive.",
  },
  {
    label: "Cleaner output",
    value: "Prefer cleaner filtered tracks and smoother follow-cam behavior, even if that means a slightly more conservative crop.",
  },
] as const;

export function AIPanel({ run, configs, targetInputVideo, onConfigDerived, onRunCreated }: AIPanelProps) {
  const [objective, setObjective] = useState("Use the safest recommended config for this video and keep the camera motion steady.");
  const [explanation, setExplanation] = useState<AIExplainResponse | null>(null);
  const [suggestion, setSuggestion] = useState<AssistantSuggestion | null>(null);
  const [diffPreview, setDiffPreview] = useState<AIConfigDiffResponse | null>(null);
  const [lastDerivedConfig, setLastDerivedConfig] = useState<ConfigDetail | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [activityLabel, setActivityLabel] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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
      setActivityLabel("Refreshing AI explanation from the selected evidence.");
      try {
        const explain = await api.aiExplain({
          run_id: run.run_id,
          config_name: run.config_name,
          focus: objective,
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
  }, [run, objective]);

  async function handleRecommend() {
    if (!run) {
      return;
    }
    setLoading(true);
    setStatusMessage(null);
    setActivityLabel("Generating a grounded recommendation from the selected run.");
    try {
      const nextSuggestion = await api.aiRecommend({
        run_id: run.run_id,
        objective,
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
    setActivityLabel("Deriving the AI config and launching the next run.");
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
      setStatusMessage(`Started ${createdRun.run_id} from ${derived.name}.`);
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
      title: "Evidence selected",
      detail: run ? run.run_id : "Pick one run from the workspace first.",
      state: run ? "complete" : "current",
    },
    {
      title: "Current run explained",
      detail: explanation?.summary ? "AI summary is loaded and tied to the selected evidence." : "Summary appears automatically once a run is selected.",
      state: explanation ? "complete" : run ? "current" : "upcoming",
    },
    {
      title: "Recommendation prepared",
      detail: suggestion ? suggestion.outputNameSuggestion ?? "Patch preview is ready." : "Ask AI to recommend the next config.",
      state: suggestion ? "complete" : run ? "current" : "upcoming",
    },
    {
      title: "Derived config ready",
      detail: diffPreview ? `generated/${diffPreview.output_name}.yaml` : "The derived config name will appear here.",
      state: diffPreview ? "current" : "upcoming",
    },
  ] as const;

  return (
    <div className="assistant-shell">
      <section className="assistant-card primary">
        <div className="assistant-header">
          <p className="eyebrow">AI Console</p>
          <h3>Tell AI what to do</h3>
          <p className="muted">
            AI works from the selected run. Start a baseline run first, then let AI recommend and run the next config.
          </p>
        </div>

        <div className="assistant-stage-grid">
          {assistantStages.map((stage, index) => (
            <article key={stage.title} className={`assistant-stage ${stage.state}`}>
              <span className="assistant-stage-index">{index + 1}</span>
              <div>
                <p className="meta-label">{stage.title}</p>
                <p className="muted">{stage.detail}</p>
              </div>
            </article>
          ))}
        </div>

        <div className="assistant-context">
          <div className="meta-row">
            <span className="meta-label">Evidence run</span>
            <span className="mono">{run?.run_id ?? "none selected"}</span>
          </div>
          <div className="meta-row">
            <span className="meta-label">Target video</span>
            <span className="mono">{targetInputVideo ?? run?.input_video ?? "not selected"}</span>
          </div>
          <div className="meta-row">
            <span className="meta-label">Known configs</span>
            <strong>{configs.length}</strong>
          </div>
        </div>

        <label className="assistant-form-label">
          <span className="meta-label">Objective</span>
          <textarea rows={5} value={objective} onChange={(event) => setObjective(event.target.value)} />
        </label>

        <div className="preset-row" aria-label="Objective presets">
          {OBJECTIVE_PRESETS.map((preset) => (
            <button
              type="button"
              key={preset.label}
              className={`chip-button ${objective === preset.value ? "selected" : ""}`}
              onClick={() => setObjective(preset.value)}
            >
              {preset.label}
            </button>
          ))}
        </div>

        <div className="assistant-actions">
          <button type="button" className="secondary-button" onClick={handleRecommend} disabled={!run || loading}>
            {loading ? "Thinking..." : "AI Recommend"}
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={handleApplyAndRun}
            disabled={!run || !suggestion || !diffPreview || loading}
          >
            Use AI Recommended Config
          </button>
        </div>

        {activityLabel ? <p className="notice-line subtle">{activityLabel}</p> : null}
        {statusMessage ? <p className="notice-line">{statusMessage}</p> : null}
        {lastDerivedConfig ? <p className="muted mono">Latest derived config: {lastDerivedConfig.name}</p> : null}
      </section>

      <section className="assistant-card">
        <div className="panel-header">
          <div>
            <h3>AI Readout</h3>
            <p className="muted">Explanation, evidence count, and recommendation readiness stay visible here.</p>
          </div>
        </div>
        <p className="assistant-summary-copy">
          {explanation?.summary ?? "AI explanation will appear after you select a run."}
        </p>

        <div className="mini-stat-grid assistant-mini-grid">
          <article className="mini-stat">
            <p className="meta-label">Evidence points</p>
            <strong>{evidencePoints.length}</strong>
            <p className="muted">Grounding items referenced by AI</p>
          </article>
          <article className="mini-stat">
            <p className="meta-label">Patch lines</p>
            <strong>{patchLines.length}</strong>
            <p className="muted">Recommended YAML edits previewed so far</p>
          </article>
          <article className="mini-stat">
            <p className="meta-label">Next config</p>
            <strong className="mono">{diffPreview?.output_name ?? suggestion?.outputNameSuggestion ?? "-"}</strong>
            <p className="muted">Generated config target name</p>
          </article>
        </div>

        {suggestion ? (
          <div className="signal-card">
            <p className="meta-label">Recommendation</p>
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
          <summary>Show AI evidence and patch</summary>
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
          <summary>Show config diff</summary>
          <p className="muted mono">{diffPreview.base_config_name}</p>
          <p className="muted mono">generated/{diffPreview.output_name}.yaml</p>
          <pre className="code-block compact">{diffPreview.patch_preview.join("\n")}</pre>
        </details>
      ) : null}
    </div>
  );
}
