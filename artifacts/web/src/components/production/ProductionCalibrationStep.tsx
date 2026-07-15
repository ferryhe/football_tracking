import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  useCaptureFieldPreview,
  useSuggestFieldSetup,
  type FieldPreviewResponse,
} from "@workspace/api-client-react";
import {
  ChevronLeft,
  ChevronRight,
  Loader2,
  Sparkles,
  Trash2,
  Undo2,
} from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLanguage } from "@/contexts/LanguageContext";
import {
  parseCoordinate,
  polygonSha256,
  validatePolygon,
  type FieldPoint,
  type FieldResolution,
} from "@/lib/fieldGeometry";
import {
  addConfirmedCalibrationFrame,
  createEmptyCalibration,
  mergePolygonDigestIfCurrent,
  resolutionsMatch,
  type ProductionCalibrationDraft,
} from "@/lib/productionCalibration";
import type { SourceSignature } from "@/lib/productionWorkflow";

const FieldPolygonEditor = lazy(() => import("./FieldPolygonEditor"));

interface ProductionCalibrationStepProps {
  source: SourceSignature;
  calibration: ProductionCalibrationDraft | null;
  onChange: (calibration: ProductionCalibrationDraft) => void;
  onUsabilityChange?: (usable: boolean) => void;
}

type CoordinateErrors = Record<string, string>;
type CoordinateDrafts = Record<string, string>;

function resolutionFromPreview(preview: FieldPreviewResponse): FieldResolution {
  return { width: preview.frame_width, height: preview.frame_height };
}

function pointsText(points: FieldPoint[]): string {
  return points.length
    ? points.map(([x, y], index) => `${index + 1}. (${x}, ${y})`).join(" · ")
    : "—";
}

function nextKeyboardPoint(
  index: number,
  resolution: FieldResolution,
): FieldPoint {
  const positions: Array<[number, number]> = [
    [0.2, 0.2],
    [0.8, 0.2],
    [0.8, 0.8],
    [0.2, 0.8],
  ];
  const [x, y] = positions[index % positions.length];
  return [
    Math.min(resolution.width - 1, Math.round(resolution.width * x)),
    Math.min(resolution.height - 1, Math.round(resolution.height * y)),
  ];
}

export function ProductionCalibrationStep({
  source,
  calibration,
  onChange,
  onUsabilityChange,
}: ProductionCalibrationStepProps) {
  const { t } = useLanguage();
  const previewMutation = useCaptureFieldPreview();
  const suggestionMutation = useSuggestFieldSetup();
  const [preview, setPreview] = useState<FieldPreviewResponse | null>(null);
  const [previewRequestPending, setPreviewRequestPending] = useState(false);
  const [imageReady, setImageReady] = useState(false);
  const [overlayReady, setOverlayReady] = useState(false);
  const [displaySize, setDisplaySize] = useState<FieldResolution>({
    width: 640,
    height: 360,
  });
  const [selectedVertex, setSelectedVertex] = useState<number | null>(null);
  const [history, setHistory] = useState<FieldPoint[][]>([]);
  const [coordinateDrafts, setCoordinateDrafts] = useState<CoordinateDrafts>(
    {},
  );
  const [coordinateErrors, setCoordinateErrors] = useState<CoordinateErrors>(
    {},
  );
  const [statusMessage, setStatusMessage] = useState<string>("");
  const [requestError, setRequestError] = useState<string | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const sourcePathRef = useRef(source.path);
  const calibrationRef = useRef(calibration);
  const previewGenerationRef = useRef(0);
  const acceptedPreviewGenerationRef = useRef(0);
  const suggestionGenerationRef = useRef(0);
  const polygonGenerationRef = useRef(0);
  sourcePathRef.current = source.path;
  calibrationRef.current = calibration;

  const publishCalibration = useCallback(
    (next: ProductionCalibrationDraft) => {
      calibrationRef.current = next;
      onChange(next);
    },
    [onChange],
  );

  const approved = calibration?.approved_polygon ?? [];
  const sourceResolution = preview
    ? resolutionFromPreview(preview)
    : (calibration?.source_resolution ?? null);
  const validation = sourceResolution
    ? validatePolygon(approved, sourceResolution)
    : { valid: false as const, reason: "too_few_points" as const };
  const hasCoordinateErrors = Object.keys(coordinateErrors).length > 0;
  const hasUncommittedCoordinates = approved.some(
    ([x, y], index) =>
      coordinateDrafts[`${index}-x`] !== String(x) ||
      coordinateDrafts[`${index}-y`] !== String(y),
  );
  const coordinateInputsResolved =
    !hasCoordinateErrors && !hasUncommittedCoordinates;
  const previewPending = previewRequestPending || previewMutation.isPending;
  const previewIsCurrent = Boolean(
    preview &&
    acceptedPreviewGenerationRef.current === previewGenerationRef.current,
  );
  const calibrationResolutionMismatch = Boolean(
    preview &&
    calibration?.source_resolution &&
    !resolutionsMatch(
      resolutionFromPreview(preview),
      calibration.source_resolution,
    ),
  );

  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const updateSize = () => {
      const rect = wrapper.getBoundingClientRect();
      const width = wrapper.clientWidth || rect.width;
      const height = wrapper.clientHeight || rect.height;
      if (width > 0 && height > 0) {
        setDisplaySize({ width, height });
      }
    };
    updateSize();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateSize);
    observer.observe(wrapper);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const next: CoordinateDrafts = {};
    approved.forEach(([x, y], index) => {
      next[`${index}-x`] = String(x);
      next[`${index}-y`] = String(y);
    });
    setCoordinateDrafts(next);
    setCoordinateErrors({});
  }, [JSON.stringify(approved)]);

  const acceptPreview = useCallback(
    (
      data: FieldPreviewResponse,
      generation: number,
      expectedSampleIndex?: number,
    ) => {
      if (
        generation !== previewGenerationRef.current ||
        data.input_video !== sourcePathRef.current ||
        (expectedSampleIndex !== undefined &&
          data.sample_index !== expectedSampleIndex)
      ) {
        return;
      }
      const resolution = resolutionFromPreview(data);
      suggestionGenerationRef.current += 1;
      acceptedPreviewGenerationRef.current = generation;
      setPreview(data);
      setImageReady(false);
      setOverlayReady(false);
      setRequestError(null);
      const current = calibrationRef.current ?? createEmptyCalibration();
      if (!current.source_resolution) {
        publishCalibration({ ...current, source_resolution: resolution });
      }
    },
    [publishCalibration],
  );

  const loadPreview = useCallback(
    async (sampleIndex?: number) => {
      const generation = ++previewGenerationRef.current;
      suggestionGenerationRef.current += 1;
      setPreviewRequestPending(true);
      setRequestError(null);
      setImageReady(false);
      setOverlayReady(false);
      try {
        const data = await previewMutation.mutateAsync({
          data: { input_video: source.path, sample_index: sampleIndex },
        });
        acceptPreview(data, generation, sampleIndex);
      } catch (error) {
        if (generation === previewGenerationRef.current) {
          setRequestError(
            error instanceof Error ? error.message : String(error),
          );
        }
      } finally {
        if (generation === previewGenerationRef.current) {
          setPreviewRequestPending(false);
        }
      }
    },
    [acceptPreview, previewMutation, source.path],
  );

  useEffect(() => {
    previewGenerationRef.current += 1;
    suggestionGenerationRef.current += 1;
    polygonGenerationRef.current += 1;
    acceptedPreviewGenerationRef.current = 0;
    setPreview(null);
    setPreviewRequestPending(false);
    setHistory([]);
    setSelectedVertex(null);
    setStatusMessage("");
    void loadPreview(1);
    return () => {
      previewGenerationRef.current += 1;
      suggestionGenerationRef.current += 1;
      polygonGenerationRef.current += 1;
    };
  }, [source.path, source.size_bytes, source.modified_at]);

  async function requestSuggestion() {
    if (
      !preview ||
      previewPending ||
      !previewIsCurrent ||
      calibrationResolutionMismatch
    )
      return;
    const generation = ++suggestionGenerationRef.current;
    const expectedPreview = preview;
    setRequestError(null);
    try {
      const data = await suggestionMutation.mutateAsync({
        data: {
          input_video: source.path,
          frame_index: expectedPreview.frame_index,
        },
      });
      if (
        generation !== suggestionGenerationRef.current ||
        sourcePathRef.current !== source.path ||
        data.input_video !== source.path ||
        data.frame_index !== expectedPreview.frame_index ||
        data.frame_width !== expectedPreview.frame_width ||
        data.frame_height !== expectedPreview.frame_height
      ) {
        return;
      }
      const polygon =
        data.field_polygon ?? data.calibration?.image_points ?? [];
      const current = calibrationRef.current ?? createEmptyCalibration();
      publishCalibration({
        ...current,
        source_resolution: resolutionFromPreview(expectedPreview),
        suggestion: {
          source_path: source.path,
          source: data.source,
          confidence: data.confidence,
          field_coverage: data.field_coverage,
          source_resolution: resolutionFromPreview(expectedPreview),
          frame_index: data.frame_index,
          polygon: polygon.map(([x, y]) => [x, y]),
        },
      });
      setStatusMessage(t.production.suggestionReady);
    } catch (error) {
      if (generation === suggestionGenerationRef.current) {
        setRequestError(error instanceof Error ? error.message : String(error));
      }
    }
  }

  function commitApproved(points: FieldPoint[], remember = true) {
    if (!sourceResolution || calibrationResolutionMismatch) return;
    if (
      resolutionsMatch(
        calibrationRef.current?.source_resolution ?? null,
        sourceResolution,
      ) &&
      JSON.stringify(calibrationRef.current?.approved_polygon ?? []) ===
        JSON.stringify(points) &&
      Boolean(calibrationRef.current?.polygon_digest)
    ) {
      return;
    }
    if (remember) setHistory((current) => [...current, approved]);
    const generation = ++polygonGenerationRef.current;
    const current = calibrationRef.current ?? createEmptyCalibration();
    const hadConfirmedFrames = current.confirmed_frames.length > 0;
    const provisional: ProductionCalibrationDraft = {
      ...current,
      source_resolution: sourceResolution,
      approved_polygon: points.map(([x, y]) => [x, y]),
      polygon_digest: null,
      confirmed_frames: [],
    };
    publishCalibration(provisional);
    setSelectedVertex(null);
    if (hadConfirmedFrames) {
      setStatusMessage(t.production.framesClearedAfterEdit);
    }
    if (!validatePolygon(points, sourceResolution).valid) return;
    void polygonSha256(points, sourceResolution)
      .then((digest) => {
        if (
          generation === polygonGenerationRef.current &&
          sourcePathRef.current === source.path
        ) {
          const merged = mergePolygonDigestIfCurrent(
            calibrationRef.current,
            points,
            sourceResolution,
            digest,
          );
          if (merged) publishCalibration(merged);
        }
      })
      .catch(() => {
        if (generation === polygonGenerationRef.current) {
          setRequestError(t.production.polygonDigestFailed);
        }
      });
  }

  function undo() {
    const previous = history.at(-1);
    if (!previous) return;
    setHistory((current) => current.slice(0, -1));
    commitApproved(previous, false);
  }

  function updateCoordinate(index: number, axis: "x" | "y", value: string) {
    if (calibrationResolutionMismatch) return;
    const key = `${index}-${axis}`;
    setCoordinateDrafts((current) => ({ ...current, [key]: value }));
    if (!sourceResolution) return;
    const result = parseCoordinate(
      value,
      axis === "x" ? sourceResolution.width : sourceResolution.height,
    );
    if (!result.ok) {
      setCoordinateErrors((current) => ({
        ...current,
        [key]:
          result.reason === "not_a_number"
            ? t.production.coordinateNumberError
            : t.production.coordinateBoundsError(
                axis === "x" ? sourceResolution.width : sourceResolution.height,
              ),
      }));
      return;
    }
    setCoordinateErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  function commitCoordinate(index: number, axis: "x" | "y") {
    if (!sourceResolution || calibrationResolutionMismatch) return;
    const key = `${index}-${axis}`;
    const result = parseCoordinate(
      coordinateDrafts[key] ?? "",
      axis === "x" ? sourceResolution.width : sourceResolution.height,
    );
    if (!result.ok) return;
    const approvedValue = approved[index][axis === "x" ? 0 : 1];
    if (result.value === approvedValue) {
      setCoordinateDrafts((current) => ({
        ...current,
        [key]: String(approvedValue),
      }));
      setCoordinateErrors((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      return;
    }
    const points = approved.map((point) => [...point] as FieldPoint);
    points[index][axis === "x" ? 0 : 1] = result.value;
    commitApproved(points);
  }

  function confirmCurrentFrame() {
    if (
      !preview ||
      !sourceResolution ||
      !calibration?.polygon_digest ||
      previewPending ||
      !previewIsCurrent ||
      calibrationResolutionMismatch ||
      !coordinateInputsResolved
    )
      return;
    const result = addConfirmedCalibrationFrame(calibration, {
      preview,
      source_path: source.path,
      source_resolution: sourceResolution,
      polygon_digest: calibration.polygon_digest,
      overlay_ready: imageReady && overlayReady,
      polygon_valid: validation.valid,
    });
    if (!result.ok) {
      setStatusMessage(t.production.confirmFrameError[result.reason]);
      return;
    }
    publishCalibration(result.calibration);
    setStatusMessage(
      t.production.frameConfirmed(result.calibration.confirmed_frames.length),
    );
  }

  const previewResolutionMatches = Boolean(
    sourceResolution &&
    calibration?.source_resolution &&
    resolutionsMatch(sourceResolution, calibration.source_resolution),
  );
  const frameAlreadyConfirmed = Boolean(
    preview &&
    calibration?.confirmed_frames.some(
      (frame) => frame.frame_index === preview.frame_index,
    ),
  );
  const confirmEnabled = Boolean(
    preview &&
    previewIsCurrent &&
    !previewPending &&
    !calibrationResolutionMismatch &&
    coordinateInputsResolved &&
    imageReady &&
    overlayReady &&
    validation.valid &&
    calibration?.polygon_digest &&
    previewResolutionMatches &&
    !frameAlreadyConfirmed &&
    (calibration?.confirmed_frames.length ?? 0) < 3,
  );
  const confidenceLabel = calibration?.suggestion
    ? t.production.suggestionConfidence[calibration.suggestion.confidence]
    : "";

  useEffect(() => {
    onUsabilityChange?.(
      Boolean(
        previewIsCurrent &&
        !previewPending &&
        !calibrationResolutionMismatch &&
        coordinateInputsResolved &&
        imageReady &&
        overlayReady,
      ),
    );
  }, [
    calibrationResolutionMismatch,
    coordinateInputsResolved,
    imageReady,
    onUsabilityChange,
    overlayReady,
    previewIsCurrent,
    previewPending,
  ]);

  useEffect(
    () => () => {
      onUsabilityChange?.(false);
    },
    [onUsabilityChange],
  );

  return (
    <div className="space-y-5" data-testid="production-calibration-step">
      <p className="break-all text-xs text-foreground/75">
        {t.production.calibrationSource}:{" "}
        <span className="font-mono">{source.path}</span>
      </p>

      <div
        ref={wrapperRef}
        className="relative aspect-video w-full overflow-hidden rounded-lg border bg-black"
        data-testid="calibration-preview"
      >
        {preview ? (
          <img
            key={`${source.path}-${preview.frame_index}`}
            src={preview.preview_data_url}
            alt={t.production.previewAlt(preview.frame_index)}
            className="absolute inset-0 h-full w-full object-contain"
            onLoad={() => setImageReady(true)}
            onError={() => setImageReady(false)}
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-white/80">
            {previewPending ? (
              <Loader2
                className="h-7 w-7 animate-spin"
                aria-label={t.common.loading}
              />
            ) : (
              t.production.previewUnavailable
            )}
          </div>
        )}
        {preview && sourceResolution && !calibrationResolutionMismatch && (
          <Suspense fallback={null}>
            <FieldPolygonEditor
              key={`${source.path}-${preview.frame_index}`}
              displaySize={displaySize}
              sourceResolution={sourceResolution}
              suggestion={calibration?.suggestion?.polygon ?? []}
              approved={approved}
              selectedVertex={selectedVertex}
              onSelectVertex={setSelectedVertex}
              onChange={commitApproved}
              onReadyChange={setOverlayReady}
            />
          </Suspense>
        )}
      </div>

      {preview && (
        <div className="flex flex-col gap-2 text-xs text-foreground/75 sm:flex-row sm:items-center sm:justify-between">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void loadPreview(preview.sample_index - 1)}
            disabled={preview.sample_index <= 1 || previewPending}
          >
            <ChevronLeft className="mr-1 h-4 w-4" aria-hidden="true" />
            {t.production.previousFrame}
          </Button>
          <span
            className="text-center tabular-nums"
            data-testid="calibration-frame-meta"
          >
            {t.production.frameMeta(
              preview.sample_index,
              preview.sample_count,
              preview.frame_index,
              preview.frame_width,
              preview.frame_height,
            )}
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void loadPreview(preview.sample_index + 1)}
            disabled={
              preview.sample_index >= preview.sample_count || previewPending
            }
          >
            {t.production.nextFrame}
            <ChevronRight className="ml-1 h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      )}

      {preview && preview.sample_count < 3 && (
        <Alert variant="destructive">
          <AlertDescription>
            {t.production.tooFewDistinctFrames}
          </AlertDescription>
        </Alert>
      )}
      {calibrationResolutionMismatch && (
        <Alert variant="destructive">
          <AlertDescription>
            {t.production.calibrationResolutionChanged}
          </AlertDescription>
        </Alert>
      )}
      {requestError && (
        <Alert variant="destructive">
          <AlertDescription>{requestError}</AlertDescription>
        </Alert>
      )}

      <section className="space-y-3" aria-labelledby="system-suggestion-title">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="system-suggestion-title" className="text-sm font-semibold">
            {t.production.systemSuggestion}
          </h3>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void requestSuggestion()}
            disabled={
              !preview ||
              previewPending ||
              !previewIsCurrent ||
              calibrationResolutionMismatch ||
              suggestionMutation.isPending
            }
          >
            {suggestionMutation.isPending ? (
              <Loader2
                className="mr-1 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            ) : (
              <Sparkles className="mr-1 h-4 w-4" aria-hidden="true" />
            )}
            {calibration?.suggestion
              ? t.production.regenerateSuggestion
              : t.production.requestSuggestion}
          </Button>
          {calibration?.suggestion && (
            <Button
              type="button"
              size="sm"
              onClick={() => commitApproved(calibration.suggestion!.polygon)}
              disabled={calibrationResolutionMismatch}
            >
              {t.production.adoptSuggestion}
            </Button>
          )}
        </div>
        {calibration?.suggestion ? (
          <div className="space-y-2 rounded-md border border-dashed border-yellow-500 p-3 text-xs">
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">{confidenceLabel}</Badge>
              <span>
                {t.production.suggestionSource}: {calibration.suggestion.source}
              </span>
              <span>
                {t.production.fieldCoverage}:{" "}
                {Math.round(calibration.suggestion.field_coverage * 100)}%
              </span>
            </div>
            <p
              className="break-words font-mono"
              data-testid="suggested-coordinates"
            >
              {pointsText(calibration.suggestion.polygon)}
            </p>
          </div>
        ) : (
          <p className="text-xs text-foreground/75">
            {t.production.noSuggestion}
          </p>
        )}
      </section>

      <section className="space-y-3" aria-labelledby="approved-polygon-title">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="approved-polygon-title" className="text-sm font-semibold">
            {t.production.approvedPolygon}
          </h3>
          {sourceResolution && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() =>
                commitApproved([
                  ...approved,
                  nextKeyboardPoint(approved.length, sourceResolution),
                ])
              }
              disabled={calibrationResolutionMismatch}
            >
              {t.production.addPoint}
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={undo}
            disabled={calibrationResolutionMismatch || history.length === 0}
          >
            <Undo2 className="mr-1 h-4 w-4" aria-hidden="true" />
            {t.production.undo}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => commitApproved([])}
            disabled={calibrationResolutionMismatch || approved.length === 0}
          >
            {t.production.clearPolygon}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            onClick={() => {
              if (selectedVertex === null) return;
              commitApproved(
                approved.filter((_, index) => index !== selectedVertex),
              );
            }}
            disabled={calibrationResolutionMismatch || selectedVertex === null}
          >
            <Trash2 className="mr-1 h-4 w-4" aria-hidden="true" />
            {t.production.deleteSelectedPoint}
          </Button>
        </div>
        <p
          className="break-words font-mono text-xs"
          data-testid="approved-coordinates"
        >
          {pointsText(approved)}
        </p>
        <p className="text-xs text-foreground/75">
          {
            t.production.polygonValidation[
              validation.valid ? "valid" : validation.reason
            ]
          }
        </p>
        <div
          className="space-y-2"
          role="group"
          aria-label={t.production.coordinateEditor}
        >
          {approved.map((_, index) => (
            <div
              key={index}
              className="grid grid-cols-[auto_1fr_1fr_auto] items-start gap-2"
            >
              <span className="pt-2 text-xs font-semibold">{index + 1}</span>
              {(["x", "y"] as const).map((axis) => {
                const key = `${index}-${axis}`;
                const error = coordinateErrors[key];
                return (
                  <div key={axis}>
                    <label htmlFor={`approved-${key}`} className="sr-only">
                      {t.production.pointCoordinate(index + 1, axis)}
                    </label>
                    <Input
                      id={`approved-${key}`}
                      inputMode="decimal"
                      disabled={calibrationResolutionMismatch}
                      value={coordinateDrafts[key] ?? ""}
                      onChange={(event) =>
                        updateCoordinate(index, axis, event.target.value)
                      }
                      onBlur={() => commitCoordinate(index, axis)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          commitCoordinate(index, axis);
                        }
                      }}
                      aria-invalid={Boolean(error)}
                      aria-describedby={
                        error ? `approved-${key}-error` : undefined
                      }
                      className="font-mono"
                    />
                    {error && (
                      <p
                        id={`approved-${key}-error`}
                        className="mt-1 text-xs text-destructive"
                        role="alert"
                      >
                        {error}
                      </p>
                    )}
                  </div>
                );
              })}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() =>
                  commitApproved(approved.filter((_, item) => item !== index))
                }
                disabled={calibrationResolutionMismatch}
                aria-label={t.production.deletePoint(index + 1)}
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-3" aria-labelledby="three-frame-title">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 id="three-frame-title" className="text-sm font-semibold">
              {t.production.threeFrameVerification}
            </h3>
            <p className="text-xs text-foreground/75">
              {t.production.confirmedFrames(
                calibration?.confirmed_frames.length ?? 0,
              )}
            </p>
          </div>
          <Button
            type="button"
            onClick={confirmCurrentFrame}
            disabled={!confirmEnabled}
          >
            {frameAlreadyConfirmed
              ? t.production.frameAlreadyConfirmed
              : t.production.confirmCurrentFrame}
          </Button>
        </div>
        <ol className="space-y-1 text-xs">
          {calibration?.confirmed_frames.map((frame) => (
            <li key={frame.frame_index}>
              {t.production.confirmedFrameDetail(
                frame.frame_index,
                frame.source_resolution.width,
                frame.source_resolution.height,
              )}
            </li>
          ))}
        </ol>
      </section>

      <p className="sr-only" role="status" aria-live="polite">
        {statusMessage}
      </p>
    </div>
  );
}
