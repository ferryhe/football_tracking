import { useMemo, useRef, useState } from "react";
import {
  useListConfigs,
  useListInputVideos,
  useSuggestFieldSetup,
  type FieldPreviewResponse,
  type FieldSuggestionResponse,
} from "@workspace/api-client-react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Sparkles,
  Trash2,
} from "lucide-react";

import { FieldPreviewCanvas } from "@/components/FieldPreviewCanvas";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export type BroadcastSetupPoint = [number, number];
export type BroadcastSetupPolygon = BroadcastSetupPoint[];

export interface BroadcastSetupConfirmedFrame {
  frame_index: number;
  frame_width: number;
  frame_height: number;
  sample_index: number;
  frame_time_seconds: number;
}

export interface BroadcastSetupInput {
  inputVideo: string;
  configName: string;
  confirmedFrames: [
    BroadcastSetupConfirmedFrame,
    BroadcastSetupConfirmedFrame,
    BroadcastSetupConfirmedFrame,
  ];
  fieldPolygon: BroadcastSetupPolygon;
  exclusionPolygons: BroadcastSetupPolygon[];
  maxManualReviewWindows: number;
  configPatch?: Record<string, unknown>;
}

export type BroadcastSetupErrorField =
  | "inputVideo"
  | "configName"
  | "confirmedFrames"
  | "sourceResolution"
  | "fieldPolygon"
  | "exclusionPolygons"
  | "maxManualReviewWindows"
  | "suggestion";

export interface BroadcastSetupValidationError {
  field: BroadcastSetupErrorField;
  message: string;
}

export interface BroadcastSetupStepLabels {
  title: string;
  description: string;
  video: string;
  videoPlaceholder: string;
  config: string;
  configPlaceholder: string;
  catalogError: string;
  calibrationTitle: string;
  calibrationDescription: string;
  selectVideoFirst: string;
  confirmCurrentFrame: string;
  frameAlreadyConfirmed: string;
  threeFramesConfirmed: string;
  confirmedFrames: string;
  removeFrame: string;
  reconfirmNotice: string;
  suggestField: string;
  suggestingField: string;
  acceptSuggestion: string;
  suggestionUnavailable: string;
  suggestionFailed: (detail: string) => string;
  fieldPolygon: string;
  fieldPolygonHelp: string;
  exclusionPolygons: string;
  exclusionPolygonsHelp: string;
  reviewWindowLimit: string;
  reviewWindowHelp: string;
  selectVideoError: string;
  selectConfigError: string;
  confirmedFramesError: (count: number) => string;
  frameResolutionMismatch: (
    frameIndex: number,
    frameWidth: number,
    frameHeight: number,
    expectedWidth: number,
    expectedHeight: number,
  ) => string;
  sourceResolutionError: string;
  polygonInvalidJson: (label: string) => string;
  polygonMinPoints: (label: string) => string;
  polygonInvalidPoint: (label: string) => string;
  polygonOutOfBounds: (label: string, width: number, height: number) => string;
  polygonZeroArea: (label: string) => string;
  exclusionPolygonsInvalidJson: string;
  exclusionPolygonsNotArray: string;
  exclusionPolygonLabel: (index: number) => string;
  fieldPolygonRequired: string;
  exclusionPolygonsInvalid: string;
  reviewWindowInvalid: string;
  submit: string;
}

export interface BroadcastSetupStepProps {
  initialValue?: Partial<BroadcastSetupInput>;
  labels?: Partial<BroadcastSetupStepLabels>;
  isSubmitting?: boolean;
  disabled?: boolean;
  error?: string | null;
  onSubmit: (input: BroadcastSetupInput) => void;
  onError?: (error: BroadcastSetupValidationError | null) => void;
}

const DEFAULT_LABELS: BroadcastSetupStepLabels = {
  title: "Broadcast setup",
  description:
    "Choose the source and confirm its field calibration before starting the workflow.",
  video: "Input video",
  videoPlaceholder: "Select a video",
  config: "Tracking config",
  configPlaceholder: "Select a config",
  catalogError: "The video or config catalog could not be loaded.",
  calibrationTitle: "Three-frame calibration",
  calibrationDescription:
    "Browse the source and explicitly confirm three different actual frames. All three must use the same source resolution.",
  selectVideoFirst: "Select an input video to browse calibration frames.",
  confirmCurrentFrame: "Confirm current frame",
  frameAlreadyConfirmed: "This frame is confirmed",
  threeFramesConfirmed: "Three frames are already confirmed",
  confirmedFrames: "Confirmed actual frames",
  removeFrame: "Remove frame",
  reconfirmNotice:
    "Saved frame numbers are not pre-confirmed. Browse this source and confirm three frames again.",
  suggestField: "Suggest field polygon",
  suggestingField: "Finding field polygon…",
  acceptSuggestion: "Accept suggestion",
  suggestionUnavailable:
    "The suggestion did not contain a valid field polygon.",
  suggestionFailed: (detail) => `Could not suggest a field polygon: ${detail}`,
  fieldPolygon: "Field polygon",
  fieldPolygonHelp:
    "JSON array with at least three [x, y] points in source-frame pixels.",
  exclusionPolygons: "Exclusion polygons (optional)",
  exclusionPolygonsHelp:
    "JSON array of polygons, or leave blank. Each polygon needs at least three points.",
  reviewWindowLimit: "Maximum manual review windows",
  reviewWindowHelp: "Choose a limit from 1 to 30.",
  selectVideoError: "Select an input video.",
  selectConfigError: "Select a tracking config.",
  confirmedFramesError: (count) =>
    `Confirm exactly three different actual frames (${count}/3 confirmed).`,
  frameResolutionMismatch: (
    frameIndex,
    frameWidth,
    frameHeight,
    expectedWidth,
    expectedHeight,
  ) =>
    `Frame ${frameIndex} is ${frameWidth}×${frameHeight}; expected ${expectedWidth}×${expectedHeight}.`,
  sourceResolutionError:
    "All confirmed frames must use the same source resolution.",
  polygonInvalidJson: (label) => `${label} must be valid JSON.`,
  polygonMinPoints: (label) => `${label} must contain at least three points.`,
  polygonInvalidPoint: (label) =>
    `${label} points must be finite [x, y] number pairs.`,
  polygonOutOfBounds: (label, width, height) =>
    `${label} contains a point outside ${width}×${height}.`,
  polygonZeroArea: (label) => `${label} must have non-zero area.`,
  exclusionPolygonsInvalidJson: "Exclusion polygons must be valid JSON.",
  exclusionPolygonsNotArray: "Exclusion polygons must be an array of polygons.",
  exclusionPolygonLabel: (index) => `Exclusion polygon ${index}`,
  fieldPolygonRequired: "A field polygon is required.",
  exclusionPolygonsInvalid: "Exclusion polygons are invalid.",
  reviewWindowInvalid: "Choose a review-window limit from 1 to 30.",
  submit: "Confirm setup",
};

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parsePoint(value: unknown): BroadcastSetupPoint | null {
  if (
    !Array.isArray(value) ||
    value.length !== 2 ||
    typeof value[0] !== "number" ||
    typeof value[1] !== "number" ||
    !Number.isFinite(value[0]) ||
    !Number.isFinite(value[1])
  ) {
    return null;
  }
  return [value[0], value[1]];
}

function parsePolygon(
  text: string,
  label: string,
  labels: BroadcastSetupStepLabels,
  resolution?: [number, number],
): { value?: BroadcastSetupPolygon; error?: string } {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { error: labels.polygonInvalidJson(label) };
  }

  if (!Array.isArray(raw) || raw.length < 3) {
    return { error: labels.polygonMinPoints(label) };
  }

  const points: BroadcastSetupPolygon = [];
  for (const rawPoint of raw) {
    const point = parsePoint(rawPoint);
    if (!point) {
      return { error: labels.polygonInvalidPoint(label) };
    }
    if (
      resolution &&
      (point[0] < 0 ||
        point[1] < 0 ||
        point[0] >= resolution[0] ||
        point[1] >= resolution[1])
    ) {
      return {
        error: labels.polygonOutOfBounds(label, resolution[0], resolution[1]),
      };
    }
    points.push(point);
  }

  const doubledArea = points.reduce((area, point, index) => {
    const next = points[(index + 1) % points.length];
    return area + point[0] * next[1] - next[0] * point[1];
  }, 0);
  if (Math.abs(doubledArea) <= 1e-9) {
    return { error: labels.polygonZeroArea(label) };
  }

  return { value: points };
}

function parseExclusionPolygons(
  text: string,
  labels: BroadcastSetupStepLabels,
  resolution?: [number, number],
): { value?: BroadcastSetupPolygon[]; error?: string } {
  if (!text.trim()) return { value: [] };

  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { error: labels.exclusionPolygonsInvalidJson };
  }

  if (!Array.isArray(raw)) {
    return { error: labels.exclusionPolygonsNotArray };
  }

  const polygons: BroadcastSetupPolygon[] = [];
  for (let index = 0; index < raw.length; index += 1) {
    const result = parsePolygon(
      formatJson(raw[index]),
      labels.exclusionPolygonLabel(index + 1),
      labels,
      resolution,
    );
    if (result.error || !result.value) return { error: result.error };
    polygons.push(result.value);
  }
  return { value: polygons };
}

export function BroadcastSetupStep({
  initialValue,
  labels: labelOverrides,
  isSubmitting = false,
  disabled = false,
  error,
  onSubmit,
  onError,
}: BroadcastSetupStepProps) {
  const labels = useMemo(
    () => ({ ...DEFAULT_LABELS, ...labelOverrides }),
    [labelOverrides],
  );
  const [selectedVideo, setSelectedVideo] = useState(
    initialValue?.inputVideo ?? "",
  );
  const [selectedConfig, setSelectedConfig] = useState(
    initialValue?.configName ?? "",
  );
  const selectionRef = useRef({
    video: initialValue?.inputVideo ?? "",
    config: initialValue?.configName ?? "",
    frameIndex: null as number | null,
  });
  const [preview, setPreview] = useState<FieldPreviewResponse | null>(null);
  const [previewReady, setPreviewReady] = useState(false);
  const [confirmedFrames, setConfirmedFrames] = useState<
    BroadcastSetupConfirmedFrame[]
  >([]);
  const [pendingSuggestion, setPendingSuggestion] =
    useState<FieldSuggestionResponse | null>(null);
  const [fieldPolygonText, setFieldPolygonText] = useState(() =>
    initialValue?.fieldPolygon ? formatJson(initialValue.fieldPolygon) : "",
  );
  const [exclusionPolygonsText, setExclusionPolygonsText] = useState(() =>
    initialValue?.exclusionPolygons?.length
      ? formatJson(initialValue.exclusionPolygons)
      : "",
  );
  const [configPatch, setConfigPatch] = useState<
    Record<string, unknown> | undefined
  >(initialValue?.configPatch);
  const [reviewWindowLimit, setReviewWindowLimit] = useState(() => {
    const initial = initialValue?.maxManualReviewWindows;
    return String(initial && initial >= 1 && initial <= 30 ? initial : 30);
  });
  const [validationError, setValidationError] =
    useState<BroadcastSetupValidationError | null>(null);
  const controlsDisabled = disabled || isSubmitting;

  const inputs = useListInputVideos();
  const configs = useListConfigs();
  const suggestField = useSuggestFieldSetup({
    mutation: {
      onSuccess: (suggestion, variables) => {
        const selection = selectionRef.current;
        if (
          variables.data.input_video !== selection.video ||
          (variables.data.config_name ?? "") !== selection.config ||
          variables.data.frame_index !== selection.frameIndex
        ) {
          return;
        }
        setPendingSuggestion(suggestion);
        setValidationError(null);
        onError?.(null);
      },
    },
  });

  const previewPatch = useMemo(() => {
    const field = parsePolygon(
      fieldPolygonText,
      labels.fieldPolygon,
      labels,
    ).value;
    const exclusions = parseExclusionPolygons(
      exclusionPolygonsText,
      labels,
    ).value;
    const suggestedField = pendingSuggestion?.field_polygon;
    const patch: Record<string, unknown> = {};
    if (field) patch.field_roi = field;
    else if (suggestedField && suggestedField.length >= 3)
      patch.field_roi = suggestedField;
    if (exclusions?.length) patch.exclusion_zones = exclusions;
    return Object.keys(patch).length ? patch : null;
  }, [exclusionPolygonsText, fieldPolygonText, labels, pendingSuggestion]);

  const isCurrentFrameConfirmed = Boolean(
    preview &&
    confirmedFrames.some((frame) => frame.frame_index === preview.frame_index),
  );

  function reportError(field: BroadcastSetupErrorField, message: string) {
    const nextError = { field, message };
    setValidationError(nextError);
    onError?.(nextError);
  }

  function clearError() {
    if (!validationError) return;
    setValidationError(null);
    onError?.(null);
  }

  function handleVideoChange(value: string) {
    selectionRef.current.video = value;
    selectionRef.current.frameIndex = null;
    setSelectedVideo(value);
    setPreviewReady(false);
    setPreview(null);
    setConfirmedFrames([]);
    setPendingSuggestion(null);
    setFieldPolygonText("");
    setExclusionPolygonsText("");
    setConfigPatch(undefined);
    suggestField.reset();
    clearError();
  }

  function handleConfigChange(value: string) {
    selectionRef.current.config = value;
    setSelectedConfig(value);
    setPendingSuggestion(null);
    setConfigPatch(undefined);
    suggestField.reset();
    clearError();
  }

  function confirmCurrentFrame() {
    if (
      !preview ||
      !previewReady ||
      isCurrentFrameConfirmed ||
      confirmedFrames.length >= 3
    )
      return;
    const sourceResolution = confirmedFrames[0]
      ? [confirmedFrames[0].frame_width, confirmedFrames[0].frame_height]
      : null;
    if (
      sourceResolution &&
      (preview.frame_width !== sourceResolution[0] ||
        preview.frame_height !== sourceResolution[1])
    ) {
      reportError(
        "sourceResolution",
        labels.frameResolutionMismatch(
          preview.frame_index,
          preview.frame_width,
          preview.frame_height,
          sourceResolution[0],
          sourceResolution[1],
        ),
      );
      return;
    }

    setConfirmedFrames((current) =>
      [
        ...current,
        {
          frame_index: preview.frame_index,
          frame_width: preview.frame_width,
          frame_height: preview.frame_height,
          sample_index: preview.sample_index,
          frame_time_seconds: preview.frame_time_seconds,
        },
      ].sort((left, right) => left.frame_index - right.frame_index),
    );
    clearError();
  }

  function removeConfirmedFrame(frameIndex: number) {
    setConfirmedFrames((current) =>
      current.filter((frame) => frame.frame_index !== frameIndex),
    );
    clearError();
  }

  function handlePreviewChange(nextPreview: FieldPreviewResponse) {
    selectionRef.current.frameIndex = nextPreview.frame_index;
    setPreviewReady(false);
    setPreview(nextPreview);
    setPendingSuggestion(null);
  }

  function requestSuggestion() {
    if (!selectedVideo || !selectedConfig || !preview || !previewReady) return;
    suggestField.mutate({
      data: {
        input_video: selectedVideo,
        config_name: selectedConfig,
        frame_index: preview.frame_index,
      },
    });
  }

  function acceptSuggestion() {
    const fieldPolygon = pendingSuggestion?.field_polygon;
    if (!fieldPolygon || fieldPolygon.length < 3) {
      reportError("suggestion", labels.suggestionUnavailable);
      return;
    }
    setFieldPolygonText(formatJson(fieldPolygon));
    setConfigPatch(pendingSuggestion?.config_patch);
    setPendingSuggestion(null);
    clearError();
  }

  function submitSetup() {
    if (!selectedVideo) {
      reportError("inputVideo", labels.selectVideoError);
      return;
    }
    if (!selectedConfig) {
      reportError("configName", labels.selectConfigError);
      return;
    }
    if (confirmedFrames.length !== 3) {
      reportError(
        "confirmedFrames",
        labels.confirmedFramesError(confirmedFrames.length),
      );
      return;
    }

    const firstFrame = confirmedFrames[0];
    const sourceResolution: [number, number] = [
      firstFrame.frame_width,
      firstFrame.frame_height,
    ];
    if (
      confirmedFrames.some(
        (frame) =>
          frame.frame_width !== sourceResolution[0] ||
          frame.frame_height !== sourceResolution[1],
      )
    ) {
      reportError("sourceResolution", labels.sourceResolutionError);
      return;
    }

    const field = parsePolygon(
      fieldPolygonText,
      labels.fieldPolygon,
      labels,
      sourceResolution,
    );
    if (field.error || !field.value) {
      reportError("fieldPolygon", field.error ?? labels.fieldPolygonRequired);
      return;
    }

    const exclusions = parseExclusionPolygons(
      exclusionPolygonsText,
      labels,
      sourceResolution,
    );
    if (exclusions.error || !exclusions.value) {
      reportError(
        "exclusionPolygons",
        exclusions.error ?? labels.exclusionPolygonsInvalid,
      );
      return;
    }

    const reviewLimit = Number(reviewWindowLimit);
    if (!Number.isInteger(reviewLimit) || reviewLimit < 1 || reviewLimit > 30) {
      reportError("maxManualReviewWindows", labels.reviewWindowInvalid);
      return;
    }

    clearError();
    onSubmit({
      inputVideo: selectedVideo,
      configName: selectedConfig,
      confirmedFrames: [
        confirmedFrames[0],
        confirmedFrames[1],
        confirmedFrames[2],
      ],
      fieldPolygon: field.value,
      exclusionPolygons: exclusions.value,
      maxManualReviewWindows: reviewLimit,
      ...(configPatch ? { configPatch } : {}),
    });
  }

  const catalogError = inputs.isError || configs.isError;
  const currentFrameButtonLabel = isCurrentFrameConfirmed
    ? labels.frameAlreadyConfirmed
    : confirmedFrames.length >= 3
      ? labels.threeFramesConfirmed
      : labels.confirmCurrentFrame;

  return (
    <div className="space-y-4" data-testid="broadcast-setup-step">
      <Card>
        <CardHeader>
          <CardTitle>{labels.title}</CardTitle>
          <CardDescription>{labels.description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {catalogError && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {labels.catalogError}{" "}
                {inputs.error
                  ? errorMessage(inputs.error)
                  : errorMessage(configs.error)}
              </AlertDescription>
            </Alert>
          )}

          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="broadcast-input-video">{labels.video}</Label>
              <Select
                value={selectedVideo}
                onValueChange={handleVideoChange}
                disabled={controlsDisabled || inputs.isLoading}
              >
                <SelectTrigger
                  id="broadcast-input-video"
                  data-testid="select-broadcast-video"
                >
                  <SelectValue placeholder={labels.videoPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {(inputs.data?.videos ?? []).map((video) => (
                    <SelectItem key={video.path} value={video.path}>
                      {video.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="broadcast-config">{labels.config}</Label>
              <Select
                value={selectedConfig}
                onValueChange={handleConfigChange}
                disabled={controlsDisabled || configs.isLoading}
              >
                <SelectTrigger
                  id="broadcast-config"
                  data-testid="select-broadcast-config"
                >
                  <SelectValue placeholder={labels.configPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {(configs.data ?? []).map((config) => (
                    <SelectItem key={config.name} value={config.name}>
                      {config.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{labels.calibrationTitle}</CardTitle>
          <CardDescription>{labels.calibrationDescription}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {Boolean(initialValue?.confirmedFrames?.length) && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{labels.reconfirmNotice}</AlertDescription>
            </Alert>
          )}

          {!selectedVideo ? (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{labels.selectVideoFirst}</AlertDescription>
            </Alert>
          ) : (
            <FieldPreviewCanvas
              inputVideo={selectedVideo}
              patch={previewPatch}
              preview={preview}
              onPreviewChange={handlePreviewChange}
              onPreviewReadyChange={setPreviewReady}
              navigationDisabled={suggestField.isPending}
            />
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              onClick={confirmCurrentFrame}
              disabled={
                controlsDisabled ||
                !preview ||
                !previewReady ||
                isCurrentFrameConfirmed ||
                confirmedFrames.length >= 3
              }
              data-testid="button-confirm-broadcast-frame"
            >
              <CheckCircle2 className="mr-1.5 h-4 w-4" />
              {currentFrameButtonLabel}
            </Button>
            <span className="text-sm text-muted-foreground tabular-nums">
              {confirmedFrames.length}/3
            </span>
          </div>

          <div className="space-y-2">
            <Label>{labels.confirmedFrames}</Label>
            {confirmedFrames.length === 0 ? (
              <p className="text-sm text-muted-foreground">0/3</p>
            ) : (
              <div className="grid gap-2 md:grid-cols-3">
                {confirmedFrames.map((frame) => (
                  <div
                    key={frame.frame_index}
                    className="flex items-center justify-between rounded-md border p-2 text-sm"
                    data-testid={`confirmed-frame-${frame.frame_index}`}
                  >
                    <span className="tabular-nums">
                      #{frame.frame_index} · {frame.frame_width}×
                      {frame.frame_height} ·{" "}
                      {frame.frame_time_seconds.toFixed(1)}s
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => removeConfirmedFrame(frame.frame_index)}
                      disabled={controlsDisabled}
                      aria-label={`${labels.removeFrame} ${frame.frame_index}`}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={requestSuggestion}
              disabled={
                controlsDisabled ||
                !preview ||
                !previewReady ||
                !selectedConfig ||
                suggestField.isPending
              }
              data-testid="button-suggest-broadcast-field"
            >
              {suggestField.isPending ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              ) : (
                <Sparkles className="mr-1.5 h-4 w-4" />
              )}
              {suggestField.isPending
                ? labels.suggestingField
                : labels.suggestField}
            </Button>
            {pendingSuggestion && (
              <Button
                type="button"
                onClick={acceptSuggestion}
                disabled={
                  controlsDisabled || !pendingSuggestion.field_polygon?.length
                }
                data-testid="button-accept-broadcast-field"
              >
                {labels.acceptSuggestion}
              </Button>
            )}
          </div>

          {suggestField.isError && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {labels.suggestionFailed(errorMessage(suggestField.error))}
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="broadcast-field-polygon">
              {labels.fieldPolygon}
            </Label>
            <Textarea
              id="broadcast-field-polygon"
              value={fieldPolygonText}
              onChange={(event) => {
                setFieldPolygonText(event.target.value);
                setConfigPatch(undefined);
                clearError();
              }}
              placeholder="[[0, 0], [1919, 0], [1919, 1079], [0, 1079]]"
              rows={7}
              disabled={controlsDisabled}
              className="font-mono text-xs"
              data-testid="textarea-broadcast-field-polygon"
            />
            <p className="text-xs text-muted-foreground">
              {labels.fieldPolygonHelp}
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="broadcast-exclusion-polygons">
              {labels.exclusionPolygons}
            </Label>
            <Textarea
              id="broadcast-exclusion-polygons"
              value={exclusionPolygonsText}
              onChange={(event) => {
                setExclusionPolygonsText(event.target.value);
                setConfigPatch(undefined);
                clearError();
              }}
              placeholder="[[[0, 0], [100, 0], [100, 100]]]"
              rows={5}
              disabled={controlsDisabled}
              className="font-mono text-xs"
              data-testid="textarea-broadcast-exclusion-polygons"
            />
            <p className="text-xs text-muted-foreground">
              {labels.exclusionPolygonsHelp}
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="space-y-2">
            <Label htmlFor="broadcast-review-window-limit">
              {labels.reviewWindowLimit}
            </Label>
            <Select
              value={reviewWindowLimit}
              onValueChange={(value) => {
                setReviewWindowLimit(value);
                clearError();
              }}
              disabled={controlsDisabled}
            >
              <SelectTrigger
                id="broadcast-review-window-limit"
                className="w-32"
                data-testid="select-broadcast-review-window-limit"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Array.from({ length: 30 }, (_, index) =>
                  String(index + 1),
                ).map((value) => (
                  <SelectItem key={value} value={value}>
                    {value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {labels.reviewWindowHelp}
            </p>
          </div>

          {(validationError || error) && (
            <Alert variant="destructive" data-testid="broadcast-setup-error">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {validationError?.message ?? error}
              </AlertDescription>
            </Alert>
          )}

          <Button
            type="button"
            onClick={submitSetup}
            disabled={controlsDisabled}
            data-testid="button-submit-broadcast-setup"
          >
            {isSubmitting && (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
            )}
            {labels.submit}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
