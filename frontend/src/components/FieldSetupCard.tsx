import { SparkIcon } from "./Icons";
import { useI18n } from "../lib/i18n";
import type { FieldSuggestion } from "../lib/types";

interface FieldSetupCardProps {
  suggestion: FieldSuggestion | null;
  loading: boolean;
  message: string | null;
  onGenerate: () => Promise<void>;
  onClear: () => void;
}

function formatClock(totalSeconds: number): string {
  const rounded = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(rounded / 60);
  const seconds = rounded % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function roiStyle(roi: [number, number, number, number], width: number, height: number) {
  const [x1, y1, x2, y2] = roi;
  return {
    left: `${(x1 / width) * 100}%`,
    top: `${(y1 / height) * 100}%`,
    width: `${((x2 - x1) / width) * 100}%`,
    height: `${((y2 - y1) / height) * 100}%`,
  };
}

function roiLabel(roi: [number, number, number, number]) {
  const [x1, y1, x2, y2] = roi;
  return `${x1}, ${y1} -> ${x2}, ${y2}`;
}

export function FieldSetupCard({ suggestion, loading, message, onGenerate, onClear }: FieldSetupCardProps) {
  const { copy } = useI18n();
  const hasSuggestion = Boolean(suggestion);

  return (
    <section className="field-setup-card">
      <div className="section-intro title-row">
        <SparkIcon className="section-icon" />
        <div>
          <h4>{copy.workspace.fieldSetupTitle}</h4>
          <p className="muted">{copy.workspace.fieldSetupSubtitle}</p>
        </div>
      </div>

      <div className="field-setup-actions">
        <button type="button" className="secondary-button icon-button" onClick={onGenerate} disabled={loading}>
          <SparkIcon className="button-icon" />
          <span>{loading ? copy.workspace.fieldGenerating : copy.workspace.fieldGenerate}</span>
        </button>
        {hasSuggestion ? (
          <button type="button" className="secondary-button" onClick={onClear} disabled={loading}>
            {copy.workspace.fieldClear}
          </button>
        ) : null}
      </div>

      {message ? <p className="notice-line">{message}</p> : null}

      {suggestion ? (
        <>
          <div className="field-preview-shell">
            <img src={suggestion.preview_data_url} alt={copy.workspace.fieldPreviewAlt} />
            <div
              className="field-overlay-box expanded"
              style={roiStyle(suggestion.expanded_roi, suggestion.frame_width, suggestion.frame_height)}
            />
            <div
              className="field-overlay-box field"
              style={roiStyle(suggestion.field_roi, suggestion.frame_width, suggestion.frame_height)}
            />
          </div>

          <div className="field-meta-grid">
            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldFrame}</p>
              <strong>
                {formatClock(suggestion.frame_time_seconds)} · {suggestion.sample_index}/{suggestion.sample_count}
              </strong>
              <p className="muted">
                {suggestion.confidence === "detected"
                  ? copy.workspace.fieldConfidenceDetected
                  : copy.workspace.fieldConfidenceFallback}
              </p>
            </div>

            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldFieldBox}</p>
              <strong className="mono">{roiLabel(suggestion.field_roi)}</strong>
              <p className="muted">{Math.round(suggestion.field_coverage * 100)}%</p>
            </div>

            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldExpandedBox}</p>
              <strong className="mono">{roiLabel(suggestion.expanded_roi)}</strong>
              <p className="muted mono">{suggestion.source}</p>
            </div>
          </div>

          <p className="notice-line subtle">{copy.workspace.fieldApplyHint}</p>
        </>
      ) : (
        <div className="empty-state compact-empty">
          <strong>{copy.workspace.fieldGenerate}</strong>
          <p className="muted">{copy.workspace.fieldEmptyBody}</p>
        </div>
      )}
    </section>
  );
}
