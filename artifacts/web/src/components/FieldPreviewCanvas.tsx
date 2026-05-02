import { useEffect, useRef, useCallback } from "react";
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

function parsePatchAnnotations(patch: Record<string, unknown>): AnnotationLayer[] {
  const layers: AnnotationLayer[] = [];

  const toPolygon = (v: unknown): Polygon | null => {
    if (!Array.isArray(v)) return null;
    if (v.length === 4 && typeof v[0] === "number") {
      const [x, y, w, h] = v as number[];
      return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
    }
    if (Array.isArray(v[0])) return v as Polygon;
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

  const zoneKeys: [string, string, string][] = [
    ["exclusion_zones", "#ef4444", "Exclusion zones"],
    ["exclude_regions", "#ef4444", "Exclusion zones"],
    ["roi", "#3b82f6", "ROI"],
    ["field_roi", "#22c55e", "Field ROI"],
    ["tracking_region", "#f59e0b", "Tracking region"],
    ["ball_region", "#a855f7", "Ball region"],
  ];

  for (const [key, color, label] of zoneKeys) {
    if (!(key in patch)) continue;
    const val = patch[key];
    const polys = toPolygonList(val);
    if (polys.length > 0) layers.push({ label, color, zones: polys });
    else {
      const poly = toPolygon(val);
      if (poly) layers.push({ label, color, zones: [poly] });
    }
  }

  return layers;
}

function drawAnnotations(
  ctx: CanvasRenderingContext2D,
  layers: AnnotationLayer[],
  scaleX: number,
  scaleY: number
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
      ctx.fillText(layer.label, poly[0][0] * scaleX + 4, poly[0][1] * scaleY - 4);
      ctx.fillStyle = layer.color + "33";
    }
  }
}

interface Props {
  inputVideo: string | null;
  suggestion: AISuggestion | null;
  preview: FieldPreviewResponse | null;
  onPreviewChange: (p: FieldPreviewResponse) => void;
}

export function FieldPreviewCanvas({ inputVideo, suggestion, preview, onPreviewChange }: Props) {
  const { t } = useLanguage();
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const fetchPreview = useMutation({
    mutationFn: ({ video, idx }: { video: string; idx?: number }) =>
      api.captureFieldPreview(video, idx),
    onSuccess: (data) => onPreviewChange(data),
  });

  const drawFrame = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !preview) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const img = new Image();
    img.onload = () => {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0);

      if (suggestion?.patch && Object.keys(suggestion.patch).length > 0) {
        const layers = parsePatchAnnotations(suggestion.patch);
        drawAnnotations(ctx, layers, 1, 1);
      }
    };
    img.src = preview.preview_data_url;
  }, [preview, suggestion]);

  useEffect(() => {
    drawFrame();
  }, [drawFrame]);

  useEffect(() => {
    if (inputVideo && !preview) {
      fetchPreview.mutate({ video: inputVideo });
    }
  }, [inputVideo]);

  const navigate = (delta: number) => {
    if (!preview || !inputVideo) return;
    const next = Math.max(0, Math.min(preview.sample_count - 1, preview.sample_index + delta));
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

  const layers = suggestion?.patch ? parsePatchAnnotations(suggestion.patch) : [];

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
            disabled={preview.sample_index === 0 || fetchPreview.isPending}
            data-testid="button-prev-frame"
          >
            <ChevronLeft className="h-3.5 w-3.5 mr-1" />
            {t.fieldPreview.prev}
          </Button>

          <span className="tabular-nums">
            {t.fieldPreview.frame} {preview.sample_index + 1} / {preview.sample_count}
            {" · "}
            {preview.frame_time_seconds.toFixed(1)}s
            {" · "}
            {preview.frame_width}×{preview.frame_height}
          </span>

          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate(1)}
            disabled={preview.sample_index >= preview.sample_count - 1 || fetchPreview.isPending}
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
            <div key={layer.label} className="flex items-center gap-1.5 text-xs">
              <span
                className="inline-block h-3 w-3 rounded-sm border"
                style={{ backgroundColor: layer.color + "55", borderColor: layer.color }}
              />
              <span>{layer.label}</span>
            </div>
          ))}
        </div>
      )}

      {fetchPreview.isError && (
        <p className="text-xs text-destructive">{fetchPreview.error?.message}</p>
      )}
    </div>
  );
}
