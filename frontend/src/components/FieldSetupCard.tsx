import { useEffect, useState } from "react";

import { CheckIcon, SparkIcon } from "./Icons";
import { useI18n } from "../lib/i18n";
import type { FieldPoint, FieldSuggestion } from "../lib/types";

interface FieldSetupCardProps {
  suggestion: FieldSuggestion | null;
  loading: boolean;
  message: string | null;
  onGenerate: () => Promise<void>;
  onClear: () => void;
  onUpdate: (suggestion: FieldSuggestion) => void;
  onAccept: (suggestion: FieldSuggestion) => void;
}

function formatClock(totalSeconds: number): string {
  const rounded = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(rounded / 60);
  const seconds = rounded % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function polygonBounds(points: FieldPoint[]): [number, number, number, number] {
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function clampPoint(point: FieldPoint, frameWidth: number, frameHeight: number): FieldPoint {
  return [
    Math.max(0, Math.min(frameWidth, Math.round(point[0]))),
    Math.max(0, Math.min(frameHeight, Math.round(point[1]))),
  ];
}

function scalePolygon(points: FieldPoint[], frameWidth: number, frameHeight: number, scaleX: number, scaleY: number): FieldPoint[] {
  const [x1, y1, x2, y2] = polygonBounds(points);
  const centerX = (x1 + x2) / 2;
  const centerY = (y1 + y2) / 2;
  return points.map((point) =>
    clampPoint(
      [centerX + (point[0] - centerX) * scaleX, centerY + (point[1] - centerY) * scaleY],
      frameWidth,
      frameHeight,
    ),
  );
}

function nudgeTop(points: FieldPoint[], frameWidth: number, frameHeight: number, deltaY: number): FieldPoint[] {
  const [, y1, , y2] = polygonBounds(points);
  const centerY = (y1 + y2) / 2;
  return points.map((point) =>
    point[1] <= centerY ? clampPoint([point[0], point[1] + deltaY], frameWidth, frameHeight) : point,
  );
}

function deriveExpandedPolygon(points: FieldPoint[], frameWidth: number, frameHeight: number): FieldPoint[] {
  return scalePolygon(points, frameWidth, frameHeight, 1.08, 1.1);
}

function buildConfigPatch(fieldPolygon: FieldPoint[], expandedPolygon: FieldPoint[]) {
  const expandedRoi = polygonBounds(expandedPolygon);
  return {
    filtering: {
      roi: expandedRoi,
    },
    scene_bias: {
      enabled: true,
      ground_zones: [
        {
          name: "field_core",
          points: fieldPolygon.map((point) => [...point]),
        },
      ],
      positive_rois: [
        {
          name: "field_buffer",
          points: expandedPolygon.map((point) => [...point]),
        },
      ],
      dynamic_air_recovery: {
        enabled: true,
        edge_reentry_expand_x: expandedRoi[2] - expandedRoi[0],
        edge_reentry_expand_y: expandedRoi[3] - expandedRoi[1],
      },
    },
  };
}

function formatPointList(points: FieldPoint[]) {
  return points.map((point) => `${point[0]},${point[1]}`).join(" | ");
}

function parsePointList(input: string): FieldPoint[] | null {
  const trimmed = input.trim();
  if (!trimmed) {
    return null;
  }
  const matches = [...trimmed.matchAll(/(-?\d+)\s*,\s*(-?\d+)/g)];
  const remainder = trimmed.replace(/-?\d+\s*,\s*-?\d+/g, "").replace(/[|\s;]+/g, "");
  if (remainder.length > 0 || matches.length < 4) {
    return null;
  }
  return matches.map((match) => [Number(match[1]), Number(match[2])] as FieldPoint);
}

function updateSuggestionShape(
  current: FieldSuggestion,
  nextFieldPolygon: FieldPoint[],
  nextExpandedPolygon?: FieldPoint[],
): FieldSuggestion {
  const fieldPolygon = nextFieldPolygon.map((point) => clampPoint(point, current.frame_width, current.frame_height));
  const expandedPolygon = (nextExpandedPolygon ?? deriveExpandedPolygon(fieldPolygon, current.frame_width, current.frame_height)).map(
    (point) => clampPoint(point, current.frame_width, current.frame_height),
  );
  const nextFieldRoi = polygonBounds(fieldPolygon);
  const nextExpandedRoi = polygonBounds(expandedPolygon);
  return {
    ...current,
    accepted: false,
    field_polygon: fieldPolygon,
    expanded_polygon: expandedPolygon,
    field_roi: nextFieldRoi,
    expanded_roi: nextExpandedRoi,
    config_patch: buildConfigPatch(fieldPolygon, expandedPolygon),
  };
}

function polygonPath(points: FieldPoint[]) {
  return points.map((point) => `${point[0]},${point[1]}`).join(" ");
}

export function FieldSetupCard({
  suggestion,
  loading,
  message,
  onGenerate,
  onClear,
  onUpdate,
  onAccept,
}: FieldSetupCardProps) {
  const { copy, language } = useI18n();
  const [draft, setDraft] = useState<FieldSuggestion | null>(suggestion);
  const [fieldInput, setFieldInput] = useState("");
  const [expandedInput, setExpandedInput] = useState("");
  const [manualError, setManualError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(suggestion);
    setFieldInput(suggestion ? formatPointList(suggestion.field_polygon) : "");
    setExpandedInput(suggestion ? formatPointList(suggestion.expanded_polygon) : "");
    setManualError(null);
  }, [suggestion]);

  const activeSuggestion = draft;
  const hasSuggestion = Boolean(activeSuggestion);
  const previewWidth = activeSuggestion?.frame_width ?? 1;
  const previewHeight = activeSuggestion?.frame_height ?? 1;

  function pointSummary(count: number) {
    return language === "zh" ? `${count} 个点 / ${count * 2} 个值` : `${count} points / ${count * 2} values`;
  }

  function applyAdjustment(nextSuggestion: FieldSuggestion | null) {
    if (!nextSuggestion) {
      return;
    }
    setManualError(null);
    setDraft(nextSuggestion);
    setFieldInput(formatPointList(nextSuggestion.field_polygon));
    setExpandedInput(formatPointList(nextSuggestion.expanded_polygon));
    onUpdate(nextSuggestion);
  }

  function applyFieldInput() {
    if (!activeSuggestion) {
      return;
    }
    const parsed = parsePointList(fieldInput);
    if (!parsed) {
      setManualError(copy.workspace.fieldInputError);
      return;
    }
    applyAdjustment(updateSuggestionShape(activeSuggestion, parsed));
  }

  function applyExpandedInput() {
    if (!activeSuggestion) {
      return;
    }
    const parsed = parsePointList(expandedInput);
    if (!parsed) {
      setManualError(copy.workspace.fieldInputError);
      return;
    }
    applyAdjustment(updateSuggestionShape(activeSuggestion, activeSuggestion.field_polygon, parsed));
  }

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
          <div className="field-setup-actions">
            <button
              type="button"
              className="primary-button icon-button"
              onClick={() => {
                if (activeSuggestion) {
                  onAccept({ ...activeSuggestion, accepted: true });
                }
              }}
              disabled={loading}
            >
              <CheckIcon className="button-icon" />
              <span>{copy.workspace.fieldAccept}</span>
            </button>
            <button type="button" className="secondary-button" onClick={onClear} disabled={loading}>
              {copy.workspace.fieldClear}
            </button>
          </div>
        ) : null}
      </div>

      {message ? <p className="notice-line">{message}</p> : null}
      {manualError ? <p className="notice-line">{manualError}</p> : null}

      {activeSuggestion ? (
        <>
          <div className="field-preview-shell">
            <img src={activeSuggestion.preview_data_url} alt={copy.workspace.fieldPreviewAlt} />
            <svg
              className="field-preview-overlay"
              viewBox={`0 0 ${previewWidth} ${previewHeight}`}
              preserveAspectRatio="xMidYMid meet"
              aria-hidden="true"
            >
              <polygon className="field-polygon expanded" points={polygonPath(activeSuggestion.expanded_polygon)} />
              <polygon className="field-polygon field" points={polygonPath(activeSuggestion.field_polygon)} />
            </svg>
          </div>

          <div className="field-meta-grid">
            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldFrame}</p>
              <strong>
                {formatClock(activeSuggestion.frame_time_seconds)} | {activeSuggestion.sample_index}/{activeSuggestion.sample_count}
              </strong>
              <p className="muted">{activeSuggestion.frame_width} x {activeSuggestion.frame_height}</p>
              <p className="muted">
                {activeSuggestion.confidence === "config"
                  ? copy.workspace.fieldConfidenceConfig
                  : activeSuggestion.confidence === "detected"
                    ? copy.workspace.fieldConfidenceDetected
                    : copy.workspace.fieldConfidenceFallback}
              </p>
            </div>

            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldFieldBox}</p>
              <strong>{pointSummary(activeSuggestion.field_polygon.length)}</strong>
              <p className="muted">{copy.workspace.fieldPolygonInput}</p>
            </div>

            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldExpandedBox}</p>
              <strong>{pointSummary(activeSuggestion.expanded_polygon.length)}</strong>
              <p className="muted">{copy.workspace.fieldExpandedInput}</p>
              <p className="muted mono">{activeSuggestion.source}</p>
            </div>
          </div>

          <div className="field-manual-grid">
            <label className="form-label">
              <span className="meta-label">{copy.workspace.fieldPolygonInput}</span>
              <input
                className="mono"
                value={fieldInput}
                onChange={(event) => setFieldInput(event.target.value)}
                onBlur={applyFieldInput}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    applyFieldInput();
                  }
                }}
              />
            </label>
            <label className="form-label">
              <span className="meta-label">{copy.workspace.fieldExpandedInput}</span>
              <input
                className="mono"
                value={expandedInput}
                onChange={(event) => setExpandedInput(event.target.value)}
                onBlur={applyExpandedInput}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    applyExpandedInput();
                  }
                }}
              />
            </label>
          </div>
          <p className="muted field-manual-hint">{copy.workspace.fieldInputHint}</p>

          <div className="field-adjust-row">
            <span className="meta-label">{copy.workspace.fieldAdjustTitle}</span>
            <div className="field-adjust-actions">
              <button
                type="button"
                className="chip-button"
                onClick={() =>
                  applyAdjustment(
                    updateSuggestionShape(
                      activeSuggestion,
                      scalePolygon(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, 0.96, 0.98),
                    ),
                  )
                }
              >
                {copy.workspace.fieldAdjustTighter}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() =>
                  applyAdjustment(
                    updateSuggestionShape(
                      activeSuggestion,
                      scalePolygon(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, 1.04, 1.02),
                    ),
                  )
                }
              >
                {copy.workspace.fieldAdjustWider}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() =>
                  applyAdjustment(
                    updateSuggestionShape(
                      activeSuggestion,
                      nudgeTop(
                        activeSuggestion.field_polygon,
                        activeSuggestion.frame_width,
                        activeSuggestion.frame_height,
                        -Math.max(6, activeSuggestion.frame_height * 0.02),
                      ),
                    ),
                  )
                }
              >
                {copy.workspace.fieldAdjustRaise}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() =>
                  applyAdjustment(
                    updateSuggestionShape(
                      activeSuggestion,
                      nudgeTop(
                        activeSuggestion.field_polygon,
                        activeSuggestion.frame_width,
                        activeSuggestion.frame_height,
                        Math.max(6, activeSuggestion.frame_height * 0.02),
                      ),
                    ),
                  )
                }
              >
                {copy.workspace.fieldAdjustLower}
              </button>
            </div>
          </div>

          <p className={`notice-line subtle ${activeSuggestion.accepted ? "accepted" : ""}`}>
            {activeSuggestion.accepted ? copy.workspace.fieldAccepted : copy.workspace.fieldApplyHint}
          </p>
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
