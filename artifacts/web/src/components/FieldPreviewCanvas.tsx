import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight, Loader2, ImageOff } from "lucide-react";
import type { AISuggestion, FieldPreviewResponse } from "@/lib/types";
import { useLanguage } from "@/contexts/LanguageContext";

type Point = [number, number];
type Polygon = Point[];

interface AnnotationLayer {
  label: string;
  color: string;
  zones?: Polygon[];
  rect?: [number, number, number, number];
}

interface AnnotationLabels {
  exclusionZones: string;
  roi: string;
  fieldRoi: string;
  trackingRegion: string;
  ballRegion: string;
  filteringRoi: string;
  fieldCore: string;
  fieldBuffer: string;
}

function parsePatchAnnotations(
  patch: Record<string, unknown>,
  labels: AnnotationLabels,
): AnnotationLayer[] {
  const layers: AnnotationLayer[] = [];

  const toPolygon = (v: unknown): Polygon | null => {
    if (!Array.isArray(v) || v.length === 0) return null;
    // [x, y, w, h] rect
    if (
      v.length === 4 &&
      typeof v[0] === "number" &&
      typeof v[1] === "number"
    ) {
      const [x, y, x2, y2] = v as number[];
      // Treat as [x1, y1, x2, y2] (matches Python expanded_roi / field_roi tuples).
      return [
        [x, y],
        [x2, y],
        [x2, y2],
        [x, y2],
      ];
    }
    // List of [x, y] points
    if (Array.isArray(v[0]) && (v[0] as unknown[]).length === 2)
      return v as Polygon;
    return null;
  };

  const toPolygonList = (v: unknown): Polygon[] => {
    if (!Array.isArray(v)) return [];
    const first = v[0];
    if (Array.isArray(first) && Array.isArray(first[0])) {
      return (v as unknown[][]).map((z) => z as Polygon);
    }
    const single = toPolygon(v);
    return single ? [single] : [];
  };

  // Pull a nested zone array of objects with `points` (Python scene_bias shape).
  const collectNamedZones = (v: unknown): Polygon[] => {
    if (!Array.isArray(v)) return [];
    const out: Polygon[] = [];
    for (const entry of v) {
      if (entry && typeof entry === "object" && "points" in (entry as object)) {
        const poly = toPolygon((entry as { points: unknown }).points);
        if (poly) out.push(poly);
      }
    }
    return out;
  };

  const flatZoneKeys: [string, string, string][] = [
    ["exclusion_zones", "#ef4444", labels.exclusionZones],
    ["exclude_regions", "#ef4444", labels.exclusionZones],
    ["roi", "#3b82f6", labels.roi],
    ["field_roi", "#22c55e", labels.fieldRoi],
    ["expanded_roi", "#22c55e", labels.fieldRoi],
    ["tracking_region", "#f59e0b", labels.trackingRegion],
    ["ball_region", "#a855f7", labels.ballRegion],
  ];

  for (const [key, color, label] of flatZoneKeys) {
    if (!(key in patch)) continue;
    const val = patch[key];
    const polys = toPolygonList(val);
    if (polys.length > 0) layers.push({ label, color, zones: polys });
    else {
      const poly = toPolygon(val);
      if (poly) layers.push({ label, color, zones: [poly] });
    }
  }

  // Nested: filtering.roi (Python field-suggestion patch shape).
  const filtering = patch["filtering"];
  if (filtering && typeof filtering === "object") {
    const roi = (filtering as Record<string, unknown>)["roi"];
    const poly = toPolygon(roi);
    if (poly)
      layers.push({
        label: labels.filteringRoi,
        color: "#3b82f6",
        zones: [poly],
      });
  }

  // Nested: scene_bias.ground_zones[] / scene_bias.positive_rois[] (Python field-suggestion patch shape).
  const sceneBias = patch["scene_bias"];
  if (sceneBias && typeof sceneBias === "object") {
    const sb = sceneBias as Record<string, unknown>;
    const ground = collectNamedZones(sb["ground_zones"]);
    if (ground.length)
      layers.push({
        label: labels.fieldCore,
        color: "#22c55e",
        zones: ground,
      });
    const positive = collectNamedZones(sb["positive_rois"]);
    if (positive.length)
      layers.push({
        label: labels.fieldBuffer,
        color: "#84cc16",
        zones: positive,
      });
  }

  return layers;
}

function drawAnnotations(
  ctx: CanvasRenderingContext2D,
  layers: AnnotationLayer[],
  scaleX: number,
  scaleY: number,
) {
  for (const layer of layers) {
    ctx.strokeStyle = layer.color;
    ctx.fillStyle = layer.color + "33";
    ctx.lineWidth = 2.5;
    ctx.setLineDash([]);

    const polys = layer.zones ?? [];
    for (const poly of polys) {
      if (poly.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(poly[0][0] * scaleX, poly[0][1] * scaleY);
      for (let i = 1; i < poly.length; i++) {
        ctx.lineTo(poly[i][0] * scaleX, poly[i][1] * scaleY);
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = layer.color;
      ctx.font = "bold 12px Inter, sans-serif";
      ctx.fillText(
        layer.label,
        poly[0][0] * scaleX + 4,
        poly[0][1] * scaleY - 4,
      );
      ctx.fillStyle = layer.color + "33";
    }
  }
}

function clearCanvas(canvas: HTMLCanvasElement | null) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx?.clearRect(0, 0, canvas.width, canvas.height);
}

interface Props {
  inputVideo: string | null;
  suggestion?: AISuggestion | null;
  patch?: Record<string, unknown> | null;
  preview: FieldPreviewResponse | null;
  onPreviewChange: (p: FieldPreviewResponse) => void;
  onPreviewReadyChange?: (ready: boolean) => void;
  autoFetch?: boolean;
  navigationDisabled?: boolean;
}

export function FieldPreviewCanvas({
  inputVideo,
  suggestion,
  patch,
  preview,
  onPreviewChange,
  onPreviewReadyChange,
  autoFetch = true,
  navigationDisabled = false,
}: Props) {
  const { t } = useLanguage();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const inputVideoRef = useRef(inputVideo);
  const previewRef = useRef(preview);
  const previewReadyChangeRef = useRef(onPreviewReadyChange);
  const drawGenerationRef = useRef(0);
  inputVideoRef.current = inputVideo;
  previewRef.current = preview;
  previewReadyChangeRef.current = onPreviewReadyChange;

  const setDrawingReady = useCallback((ready: boolean) => {
    previewReadyChangeRef.current?.(ready);
  }, []);

  const invalidateDrawing = useCallback(() => {
    drawGenerationRef.current += 1;
    clearCanvas(canvasRef.current);
    setDrawingReady(false);
  }, [setDrawingReady]);

  const fetchPreview = useMutation({
    mutationFn: ({ video, idx }: { video: string; idx?: number }) =>
      api.captureFieldPreview(video, idx),
    onSuccess: (data, variables) => {
      const currentVideo = inputVideoRef.current;
      if (
        !currentVideo ||
        variables.video !== currentVideo ||
        data.input_video !== variables.video ||
        data.input_video !== currentVideo
      ) {
        return;
      }
      onPreviewChange(data);
    },
    onError: (_error, variables) => {
      if (variables.video === inputVideoRef.current) {
        invalidateDrawing();
      }
    },
  });

  useLayoutEffect(() => {
    invalidateDrawing();

    const canvas = canvasRef.current;
    if (!canvas || !preview || !inputVideo) return;
    if (preview.input_video !== inputVideo) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const generation = drawGenerationRef.current;
    const targetPreview = preview;
    const targetVideo = inputVideo;
    const isCurrentDraw = () =>
      drawGenerationRef.current === generation &&
      canvasRef.current === canvas &&
      previewRef.current === targetPreview &&
      inputVideoRef.current === targetVideo &&
      targetPreview.input_video === targetVideo;

    const img = new Image();
    img.onload = () => {
      if (!isCurrentDraw()) return;

      try {
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0);

        const activePatch = patch ?? suggestion?.patch ?? null;
        if (activePatch && Object.keys(activePatch).length > 0) {
          const layers = parsePatchAnnotations(activePatch, t.fieldPreview);
          const scaleX = canvas.width / targetPreview.frame_width;
          const scaleY = canvas.height / targetPreview.frame_height;
          drawAnnotations(ctx, layers, scaleX, scaleY);
        }
      } catch {
        if (isCurrentDraw()) {
          clearCanvas(canvas);
          setDrawingReady(false);
        }
        return;
      }

      if (isCurrentDraw()) setDrawingReady(true);
    };
    img.onerror = () => {
      if (isCurrentDraw()) {
        clearCanvas(canvas);
        setDrawingReady(false);
      }
    };
    img.src = targetPreview.preview_data_url;

    return () => {
      if (drawGenerationRef.current === generation) {
        drawGenerationRef.current += 1;
      }
      img.onload = null;
      img.onerror = null;
      clearCanvas(canvas);
      setDrawingReady(false);
    };
  }, [
    inputVideo,
    invalidateDrawing,
    patch,
    preview,
    setDrawingReady,
    suggestion,
    t.fieldPreview,
  ]);

  useEffect(() => {
    if (autoFetch && inputVideo && !preview) {
      invalidateDrawing();
      fetchPreview.mutate({ video: inputVideo });
    }
  }, [inputVideo, autoFetch, preview, invalidateDrawing]);

  const navigate = (delta: number) => {
    if (!preview || !inputVideo) return;
    // Backend uses 1-based sample_index in [1, sample_count]
    const next = Math.max(
      1,
      Math.min(preview.sample_count, preview.sample_index + delta),
    );
    if (next === preview.sample_index) return;
    invalidateDrawing();
    fetchPreview.mutate({ video: inputVideo, idx: next });
  };

  if (!inputVideo) {
    return (
      <div className="flex flex-col items-center justify-center h-40 rounded-lg border border-dashed text-muted-foreground gap-2">
        <ImageOff className="h-8 w-8 opacity-40" />
        <p className="text-sm">{t.fieldPreview.noVideo}</p>
      </div>
    );
  }

  const activePatch = patch ?? suggestion?.patch ?? null;
  const layers = activePatch
    ? parsePatchAnnotations(activePatch, t.fieldPreview)
    : [];

  return (
    <div className="space-y-3">
      <div className="relative rounded-lg overflow-hidden border bg-black">
        {fetchPreview.isPending && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-10">
            <Loader2 className="h-8 w-8 animate-spin text-white" />
          </div>
        )}
        {!preview && !fetchPreview.isPending && (
          <div className="flex items-center justify-center h-48 text-muted-foreground text-sm">
            {t.fieldPreview.loadingFrame}
          </div>
        )}
        <canvas
          ref={canvasRef}
          className="w-full h-auto block"
          data-testid="canvas-field-preview"
        />
      </div>

      {preview && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate(-1)}
            disabled={
              navigationDisabled ||
              preview.sample_index <= 1 ||
              fetchPreview.isPending
            }
            data-testid="button-prev-frame"
          >
            <ChevronLeft className="h-3.5 w-3.5 mr-1" />
            {t.fieldPreview.prev}
          </Button>

          <span className="tabular-nums">
            {t.fieldPreview.frame} {preview.sample_index} /{" "}
            {preview.sample_count}
            {" · "}
            {preview.frame_time_seconds.toFixed(1)}s{" · "}
            {preview.frame_width}×{preview.frame_height}
          </span>

          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate(1)}
            disabled={
              navigationDisabled ||
              preview.sample_index >= preview.sample_count ||
              fetchPreview.isPending
            }
            data-testid="button-next-frame"
          >
            {t.fieldPreview.next}
            <ChevronRight className="h-3.5 w-3.5 ml-1" />
          </Button>
        </div>
      )}

      {layers.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {layers.map((layer) => (
            <div
              key={layer.label}
              className="flex items-center gap-1.5 text-xs"
            >
              <span
                className="inline-block h-3 w-3 rounded-sm border"
                style={{
                  backgroundColor: layer.color + "55",
                  borderColor: layer.color,
                }}
              />
              <span>{layer.label}</span>
            </div>
          ))}
        </div>
      )}

      {fetchPreview.isError && (
        <p className="text-xs text-destructive">
          {fetchPreview.error?.message}
        </p>
      )}
    </div>
  );
}
