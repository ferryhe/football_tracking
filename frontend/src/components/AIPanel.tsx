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
  onConfigDerived: () => Promise<ConfigListItem[]> | Promise<void> | void;
  onRunCreated: (run: RunRecord) => Promise<void> | void;
}

export function AIPanel({ run, configs, onConfigDerived, onRunCreated }: AIPanelProps) {
  const [objective, setObjective] = useState("stabilize follow-cam without making it sluggish");
  const [explanation, setExplanation] = useState<AIExplainResponse | null>(null);
  const [suggestion, setSuggestion] = useState<AssistantSuggestion | null>(null);
  const [diffPreview, setDiffPreview] = useState<AIConfigDiffResponse | null>(null);
  const [lastDerivedConfig, setLastDerivedConfig] = useState<ConfigDetail | null>(null);
  const [deriveStatus, setDeriveStatus] = useState<string | null>(null);
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
          setLoading(false);
        }
      }
    }
    loadExplain();
    return () => {
      cancelled = true;
    };
  }, [run, objective]);

  async function handleRecommend() {
    if (!run) {
      return;
    }
    setLoading(true);
    setDeriveStatus(null);
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
      setDeriveStatus(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
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

  async function handleDerive() {
    setLoading(true);
    setDeriveStatus(null);
    try {
      const derived = await deriveCurrentConfig();
      if (derived) {
        setDeriveStatus(`Derived config saved: ${derived.name}`);
      }
    } catch (caughtError) {
      setDeriveStatus(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setLoading(false);
    }
  }

  async function handleDeriveAndRun() {
    if (!run) {
      return;
    }
    setLoading(true);
    setDeriveStatus(null);
    try {
      const derived = await deriveCurrentConfig();
      if (!derived) {
        throw new Error("No derived config is available.");
      }
      const createdRun = await api.createRun({
        config_name: derived.name,
        input_video: run.input_video ?? undefined,
        enable_postprocess: run.modules_enabled.postprocess ?? true,
        enable_follow_cam: run.modules_enabled.follow_cam ?? true,
        notes: `AI objective: ${objective}`,
      });
      setDeriveStatus(`Derived ${derived.name} and started run ${createdRun.run_id}.`);
      await onRunCreated(createdRun);
    } catch (caughtError) {
      setDeriveStatus(caughtError instanceof Error ? caughtError.message : String(caughtError));
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="right-rail">
      <div className="assistant-header">
        <p className="eyebrow">AI Native</p>
        <h3>Operator Assistant</h3>
        <p className="muted">
          The assistant is grounded in live run evidence and can now derive a config and immediately launch a new run.
        </p>
      </div>

      <div className="assistant-context">
        <p className="meta-label">Selected run</p>
        <p className="mono">{run?.run_id ?? "none"}</p>
        <p className="meta-label">Config</p>
        <p className="mono">{run?.config_name ?? "n/a"}</p>
        <p className="meta-label">Known configs</p>
        <p>{configs.length}</p>
      </div>

      <div className="assistant-card">
        <label className="assistant-form-label">
          <span>Objective</span>
          <textarea rows={4} value={objective} onChange={(event) => setObjective(event.target.value)} />
        </label>
        <div className="assistant-actions">
          <button type="button" className="primary-button" onClick={handleRecommend} disabled={!run || loading}>
            {loading ? "Thinking..." : "Recommend Patch"}
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleDerive}
            disabled={!run || !suggestion || !diffPreview || loading}
          >
            Derive Config
          </button>
          <button
            type="button"
            className="accent-button"
            onClick={handleDeriveAndRun}
            disabled={!run || !suggestion || !diffPreview || loading}
          >
            Derive and Run
          </button>
        </div>
        {deriveStatus ? <p className="muted">{deriveStatus}</p> : null}
        {lastDerivedConfig ? <p className="muted mono">latest: {lastDerivedConfig.name}</p> : null}
      </div>

      <div className="assistant-stack">
        <section className="assistant-card">
          <h4>Explain</h4>
          <p>{explanation?.summary ?? "Select a run to load grounded evidence."}</p>
          {explanation?.evidence?.length ? (
            <ul className="flat-list">
              {explanation.evidence.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </section>

        {suggestion ? (
          <section className="assistant-card">
            <h4>{suggestion.title}</h4>
            <p>{suggestion.diagnosis}</p>
            <p className="assistant-reco">{suggestion.recommendation}</p>
            <p className="muted">{suggestion.expected_tradeoff}</p>
            <div>
              <p className="meta-label">Patch preview</p>
              <pre className="code-block">{(suggestion.patchPreview ?? []).join("\n")}</pre>
            </div>
            <div>
              <p className="meta-label">Evidence</p>
              <ul className="flat-list">
                {suggestion.evidence.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </section>
        ) : null}

        {diffPreview ? (
          <section className="assistant-card">
            <h4>Config Diff</h4>
            <p className="muted mono">{diffPreview.base_config_name}</p>
            <p className="muted mono">generated/{diffPreview.output_name}.yaml</p>
            <pre className="code-block">{diffPreview.patch_preview.join("\n")}</pre>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
