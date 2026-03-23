import { useEffect, useState } from "react";

import { CheckIcon, FileIcon, PlayIcon, SparkIcon, WandIcon } from "./Icons";
import { TooltipBadge } from "./TooltipBadge";
import { useI18n } from "../lib/i18n";
import type { FieldPoint, FieldPreview, FieldSuggestion } from "../lib/types";

interface FieldSetupCardProps {
  preview: FieldPreview | null;
  suggestion: FieldSuggestion | null;
  loading: boolean;
  message: string | null;
  canLoadFromConfig: boolean;
  canStartBaseline: boolean;
  launching: boolean;
  launchMessage: string | null;
  onCapturePreview: () => Promise<void>;
  onLoadFromConfig: () => Promise<void>;
  onGenerate: () => Promise<void>;
  onClear: () => void;
  onUpdate: (suggestion: FieldSuggestion) => void;
  onAccept: (suggestion: FieldSuggestion) => void;
  onStartBaseline: () => Promise<void>;
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

function adjustExpandedGap(current: FieldSuggestion, factor: number): FieldSuggestion {
  const baseExpandedPolygon =
    current.expanded_polygon.length === current.field_polygon.length
      ? current.expanded_polygon
      : deriveExpandedPolygon(current.field_polygon, current.frame_width, current.frame_height);

  const nextExpandedPolygon = baseExpandedPolygon.map((point, index) => {
    const fieldPoint = current.field_polygon[Math.min(index, current.field_polygon.length - 1)];
    return clampPoint(
      [
        fieldPoint[0] + (point[0] - fieldPoint[0]) * factor,
        fieldPoint[1] + (point[1] - fieldPoint[1]) * factor,
      ],
      current.frame_width,
      current.frame_height,
    );
  });

  return updateSuggestionShape(current, current.field_polygon, nextExpandedPolygon);
}

export function buildConfigPatch(fieldPolygon: FieldPoint[], expandedPolygon: FieldPoint[]) {
  const expandedRoi = polygonBounds(expandedPolygon);
  const expandedWidth = Math.max(1, expandedRoi[2] - expandedRoi[0]);
  const expandedHeight = Math.max(1, expandedRoi[3] - expandedRoi[1]);
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
        edge_reentry_expand_x: expandedWidth,
        edge_reentry_expand_y: expandedHeight,
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
  preview,
  suggestion,
  loading,
  message,
  canLoadFromConfig,
  canStartBaseline,
  launching,
  launchMessage,
  onCapturePreview,
  onLoadFromConfig,
  onGenerate,
  onClear,
  onUpdate,
  onAccept,
  onStartBaseline,
}: FieldSetupCardProps) {
  const { copy } = useI18n();
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
  const activePreview = preview ?? activeSuggestion;
  const previewWidth = activePreview?.frame_width ?? 1;
  const previewHeight = activePreview?.frame_height ?? 1;
  const sourceFromConfig = Boolean(activeSuggestion?.source.startsWith("config:"));
  const sourceFromAi = Boolean(activeSuggestion && !sourceFromConfig);
  const suggestionAccepted = Boolean(activeSuggestion?.accepted);

  function pointSummary(count: number) {
    return `${count}`;
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
        <div className="title-with-tooltip">
          <div className="title-inline">
            <h4>{copy.workspace.fieldSetupTitle}</h4>
            <TooltipBadge label={`${copy.workspace.fieldSetupSubtitle} ${copy.workspace.fieldChooseSourceHint}`} />
          </div>
        </div>
      </div>

      <div className="field-action-grid">
        <button
          type="button"
          className={`field-action-button ${activePreview ? "complete" : "active"}`}
          onClick={onCapturePreview}
          disabled={loading}
          title={activePreview?.sample_count && activePreview.sample_count > 1 ? copy.workspace.fieldPreviewCycleHint : undefined}
        >
          <span className="field-action-index">1</span>
          <span className="field-action-icon">
            <SparkIcon className="button-icon" />
          </span>
          <span className="field-action-copy">
            <strong>{copy.workspace.fieldCapture}</strong>
          </span>
        </button>

        <button
          type="button"
          className={`field-action-button ${sourceFromConfig ? "complete" : activePreview ? "active" : ""}`}
          onClick={onLoadFromConfig}
          disabled={!activePreview || !canLoadFromConfig || loading}
          title={copy.workspace.fieldChooseSourceHint}
        >
          <span className="field-action-index">2</span>
          <span className="field-action-icon">
            <FileIcon className="button-icon" />
          </span>
          <span className="field-action-copy">
            <strong>{copy.workspace.fieldLoadConfig}</strong>
          </span>
        </button>

        <button
          type="button"
          className={`field-action-button ${sourceFromAi ? "complete" : activePreview ? "active" : ""}`}
          onClick={onGenerate}
          disabled={!activePreview || loading}
          title={copy.workspace.fieldChooseSourceHint}
        >
          <span className="field-action-index">3</span>
          <span className="field-action-icon">
            <WandIcon className="button-icon" />
          </span>
          <span className="field-action-copy">
            <strong>{copy.workspace.fieldGenerate}</strong>
          </span>
        </button>

        <button
          type="button"
          className={`field-action-button ${suggestionAccepted ? "complete" : activeSuggestion ? "active" : ""}`}
          onClick={() => {
            if (activeSuggestion) {
              onAccept({ ...activeSuggestion, accepted: true });
            }
          }}
          disabled={!activeSuggestion || loading}
          title={copy.workspace.fieldApplyHint}
        >
          <span className="field-action-index">4</span>
          <span className="field-action-icon">
            <CheckIcon className="button-icon" />
          </span>
          <span className="field-action-copy">
            <strong>{copy.workspace.fieldAccept}</strong>
          </span>
        </button>

        <button
          type="button"
          className="field-action-button primary"
          onClick={onStartBaseline}
          disabled={!canStartBaseline}
        >
          <span className="field-action-index">5</span>
          <span className="field-action-icon">
            <PlayIcon className="button-icon" />
          </span>
          <span className="field-action-copy">
            <strong>{launching ? copy.workspace.launchStarting : copy.workspace.launchButton}</strong>
          </span>
        </button>
      </div>

      {message ? <p className="notice-line">{message}</p> : null}
      {launchMessage ? <p className="notice-line">{launchMessage}</p> : null}
      {manualError ? <p className="notice-line">{manualError}</p> : null}

      {activePreview ? (
        <>
          <div className="field-preview-shell">
            <img src={activePreview.preview_data_url} alt={copy.workspace.fieldPreviewAlt} />
            {activeSuggestion ? (
              <svg
                className="field-preview-overlay"
                viewBox={`0 0 ${previewWidth} ${previewHeight}`}
                preserveAspectRatio="xMidYMid meet"
                aria-hidden="true"
              >
                <polygon className="field-polygon expanded" points={polygonPath(activeSuggestion.expanded_polygon)} />
                <polygon className="field-polygon field" points={polygonPath(activeSuggestion.field_polygon)} />
              </svg>
            ) : null}
          </div>

          {activeSuggestion ? (
            <>
              <div className="field-legend-row">
                <span className="field-legend-pill field" title={copy.workspace.fieldFieldTooltip}>
                  <span className="field-legend-line field" aria-hidden="true" />
                  <span>{copy.workspace.fieldFieldBox}</span>
                </span>
                <span className="field-legend-pill expanded" title={copy.workspace.fieldExpandedTooltip}>
                  <span className="field-legend-line expanded" aria-hidden="true" />
                  <span>{copy.workspace.fieldExpandedBox}</span>
                </span>
              </div>

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
                  <button type="button" className="chip-button" onClick={() => applyAdjustment(adjustExpandedGap(activeSuggestion, 0.9))}>
                    {copy.workspace.fieldAdjustGapIn}
                  </button>
                  <button type="button" className="chip-button" onClick={() => applyAdjustment(adjustExpandedGap(activeSuggestion, 1.12))}>
                    {copy.workspace.fieldAdjustGapOut}
                  </button>
                </div>
              </div>

              {activeSuggestion.accepted ? (
                <p className="notice-line subtle accepted">{copy.workspace.fieldAccepted}</p>
              ) : null}
            </>
          ) : null}

          <details className="detail-card field-manual-card">
            <summary className="detail-summary-inline">
              <span>{copy.workspace.fieldDetailsTitle}</span>
              <TooltipBadge label={copy.workspace.fieldInputHint} />
            </summary>

            <div className="field-meta-grid">
              <div className="detail-block compact-detail">
                <p className="meta-label">{copy.workspace.fieldFrame}</p>
                <strong>
                  {formatClock(activePreview.frame_time_seconds)} | {activePreview.sample_index}/{activePreview.sample_count}
                </strong>
                <p className="muted">
                  {activePreview.frame_width} x {activePreview.frame_height}
                </p>
                <p className="muted">{activeSuggestion ? copy.workspace.fieldOverlayReady : copy.workspace.fieldPreviewReady}</p>
              </div>

              <div className="detail-block compact-detail">
                <p className="meta-label">{copy.workspace.fieldFieldBox}</p>
                <strong>{activeSuggestion ? pointSummary(activeSuggestion.field_polygon.length) : copy.workspace.fieldNoOverlay}</strong>
                <p className="muted">{copy.workspace.fieldPolygonInput}</p>
              </div>

              <div className="detail-block compact-detail">
                <p className="meta-label">{copy.workspace.fieldExpandedBox}</p>
                <strong>{activeSuggestion ? pointSummary(activeSuggestion.expanded_polygon.length) : copy.workspace.fieldAwaitingSource}</strong>
                <p className="muted mono">{activeSuggestion?.source ?? copy.workspace.fieldAwaitingSource}</p>
              </div>
            </div>

            {activeSuggestion ? (
              <>
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
                <div className="field-detail-actions">
                  <button type="button" className="secondary-button" onClick={onClear} disabled={loading}>
                    {copy.workspace.fieldClear}
                  </button>
                </div>
              </>
            ) : null}
          </details>
        </>
      ) : (
        <div className="empty-state compact-empty">
          <strong>{copy.workspace.fieldCapture}</strong>
          <p className="muted">{copy.workspace.fieldEmptyBody}</p>
        </div>
      )}
    </section>
  );
}
