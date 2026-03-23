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

function roiLabel(roi: [number, number, number, number]) {
  const [x1, y1, x2, y2] = roi;
  return `${x1}, ${y1} -> ${x2}, ${y2}`;
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

function updateSuggestionShape(current: FieldSuggestion, nextFieldPolygon: FieldPoint[]): FieldSuggestion {
  const expandedPolygon = deriveExpandedPolygon(nextFieldPolygon, current.frame_width, current.frame_height);
  const nextFieldRoi = polygonBounds(nextFieldPolygon);
  const nextExpandedRoi = polygonBounds(expandedPolygon);
  return {
    ...current,
    accepted: false,
    field_polygon: nextFieldPolygon,
    expanded_polygon: expandedPolygon,
    field_roi: nextFieldRoi,
    expanded_roi: nextExpandedRoi,
    config_patch: buildConfigPatch(nextFieldPolygon, expandedPolygon),
  };
}

function polygonPath(points: FieldPoint[], previewBounds: [number, number, number, number]) {
  const [previewX1, previewY1] = previewBounds;
  return points.map((point) => `${point[0] - previewX1},${point[1] - previewY1}`).join(" ");
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
  const { copy } = useI18n();
  const [draft, setDraft] = useState<FieldSuggestion | null>(suggestion);

  useEffect(() => {
    setDraft(suggestion);
  }, [suggestion]);

  const activeSuggestion = draft;
  const hasSuggestion = Boolean(activeSuggestion);
  const previewBounds = activeSuggestion?.preview_bounds ?? [0, 0, 1, 1];
  const previewWidth = Math.max(1, previewBounds[2] - previewBounds[0]);
  const previewHeight = Math.max(1, previewBounds[3] - previewBounds[1]);

  function applyAdjustment(nextSuggestion: FieldSuggestion | null) {
    if (!nextSuggestion) {
      return;
    }
    setDraft(nextSuggestion);
    onUpdate(nextSuggestion);
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

      {activeSuggestion ? (
        <>
          <div className="field-preview-shell">
            <img src={activeSuggestion.preview_data_url} alt={copy.workspace.fieldPreviewAlt} />
            <svg
              className="field-preview-overlay"
              viewBox={`0 0 ${previewWidth} ${previewHeight}`}
              preserveAspectRatio="none"
              aria-hidden="true"
            >
              <polygon
                className="field-polygon expanded"
                points={polygonPath(activeSuggestion.expanded_polygon, previewBounds)}
              />
              <polygon className="field-polygon field" points={polygonPath(activeSuggestion.field_polygon, previewBounds)} />
            </svg>
          </div>

          <div className="field-meta-grid">
            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldFrame}</p>
              <strong>
                {formatClock(activeSuggestion.frame_time_seconds)} · {activeSuggestion.sample_index}/{activeSuggestion.sample_count}
              </strong>
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
              <strong className="mono">{roiLabel(activeSuggestion.field_roi)}</strong>
              <p className="muted">{Math.round(activeSuggestion.field_coverage * 100)}%</p>
            </div>

            <div className="detail-block compact-detail">
              <p className="meta-label">{copy.workspace.fieldExpandedBox}</p>
              <strong className="mono">{roiLabel(activeSuggestion.expanded_roi)}</strong>
              <p className="muted mono">{activeSuggestion.source}</p>
            </div>
          </div>

          <div className="field-adjust-row">
            <span className="meta-label">{copy.workspace.fieldAdjustTitle}</span>
            <div className="field-adjust-actions">
              <button
                type="button"
                className="chip-button"
                onClick={() => applyAdjustment(updateSuggestionShape(activeSuggestion, scalePolygon(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, 0.96, 0.98)))}
              >
                {copy.workspace.fieldAdjustTighter}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() => applyAdjustment(updateSuggestionShape(activeSuggestion, scalePolygon(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, 1.04, 1.02)))}
              >
                {copy.workspace.fieldAdjustWider}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() => applyAdjustment(updateSuggestionShape(activeSuggestion, nudgeTop(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, -Math.max(6, activeSuggestion.frame_height * 0.02))))}
              >
                {copy.workspace.fieldAdjustRaise}
              </button>
              <button
                type="button"
                className="chip-button"
                onClick={() => applyAdjustment(updateSuggestionShape(activeSuggestion, nudgeTop(activeSuggestion.field_polygon, activeSuggestion.frame_width, activeSuggestion.frame_height, Math.max(6, activeSuggestion.frame_height * 0.02))))}
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
