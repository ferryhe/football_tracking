import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { api } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/utils";
import type { CreateRunRequest, FieldPreviewResponse, FieldSuggestionResponse } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Play, Video, Layers, AlertCircle, CheckCircle2, Loader2,
  Map, Sparkles, X, ArrowRight, Minimize2, Maximize2, ArrowUp, ArrowDown,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useLanguage } from "@/contexts/LanguageContext";
import { FieldPreviewCanvas } from "@/components/FieldPreviewCanvas";

type FieldPoint = [number, number];

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

function scalePolygon(
  points: FieldPoint[],
  frameWidth: number,
  frameHeight: number,
  scaleX: number,
  scaleY: number,
): FieldPoint[] {
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

function updateSuggestionShape(
  current: FieldSuggestionResponse,
  nextFieldPolygon: FieldPoint[],
  nextExpandedPolygon?: FieldPoint[],
): FieldSuggestionResponse {
  const fieldPolygon = nextFieldPolygon.map((point) => clampPoint(point, current.frame_width, current.frame_height));
  const expandedPolygon = (
    nextExpandedPolygon ?? deriveExpandedPolygon(fieldPolygon, current.frame_width, current.frame_height)
  ).map((point) => clampPoint(point, current.frame_width, current.frame_height));
  const fieldRoi = polygonBounds(fieldPolygon);
  const expandedRoi = polygonBounds(expandedPolygon);
  return {
    ...current,
    field_polygon: fieldPolygon,
    expanded_polygon: expandedPolygon,
    field_roi: fieldRoi,
    expanded_roi: expandedRoi,
    config_patch: buildConfigPatch(fieldPolygon, expandedPolygon),
  };
}

function adjustExpandedGap(current: FieldSuggestionResponse, factor: number): FieldSuggestionResponse {
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

function formatPointList(points: FieldPoint[]) {
  return points.map((point) => `${point[0]},${point[1]}`).join(" | ");
}

function parsePointList(input: string): FieldPoint[] | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const matches = [...trimmed.matchAll(/(-?\d+)\s*,\s*(-?\d+)/g)];
  const remainder = trimmed.replace(/-?\d+\s*,\s*-?\d+/g, "").replace(/[|\s;]+/g, "");
  if (remainder.length > 0 || matches.length < 4) return null;
  return matches.map((match) => [Number(match[1]), Number(match[2])] as FieldPoint);
}

export default function BaselinePage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [, setLocation] = useLocation();

  const [selectedInput, setSelectedInput] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [enablePostprocess, setEnablePostprocess] = useState(true);
  const [enableFollowCam, setEnableFollowCam] = useState(false);
  const [startFrame, setStartFrame] = useState<string>("");
  const [maxFrames, setMaxFrames] = useState<string>("200");

  const [fieldPreview, setFieldPreview] = useState<FieldPreviewResponse | null>(null);
  const [suggestion, setSuggestion] = useState<FieldSuggestionResponse | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [fieldInput, setFieldInput] = useState("");
  const [expandedInput, setExpandedInput] = useState("");
  const [manualError, setManualError] = useState<string | null>(null);

  // Reset field preview state when input video changes
  useEffect(() => {
    setFieldPreview(null);
    setSuggestion(null);
    setAccepted(false);
  }, [selectedInput]);

  // Invalidate accepted suggestion when the chosen config changes — it was generated
  // against a specific config and may not be appropriate for the new one.
  useEffect(() => {
    setSuggestion(null);
    setAccepted(false);
  }, [selectedConfig]);

  useEffect(() => {
    if (!suggestion) {
      setFieldInput("");
      setExpandedInput("");
      setManualError(null);
      return;
    }
    setFieldInput(formatPointList(suggestion.field_polygon));
    setExpandedInput(formatPointList(suggestion.expanded_polygon));
    setManualError(null);
  }, [suggestion]);

  const { data: inputCatalog, isLoading: inputsLoading } = useQuery({
    queryKey: ["inputs"],
    queryFn: api.listInputs,
    refetchInterval: 30_000,
  });

  const { data: configs, isLoading: configsLoading } = useQuery({
    queryKey: ["configs"],
    queryFn: api.listConfigs,
    refetchInterval: 30_000,
  });

  const fetchSuggestion = useMutation({
    mutationFn: () =>
      api.suggestFieldSetup({
        input_video: selectedInput,
        config_name: selectedConfig || undefined,
        frame_index: fieldPreview?.frame_index,
      }),
    onSuccess: (data) => {
      setSuggestion(data);
      setAccepted(false);
    },
    onError: (err: Error) => {
      toast({ title: t.baseline.suggestFailed, description: err.message, variant: "destructive" });
    },
  });

  const createRun = useMutation({
    mutationFn: (req: CreateRunRequest) => api.createRun(req),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.baseline.runQueued, description: t.baseline.runQueuedDesc(run.run_id) });
      // Auto-navigate to history so user sees the run progressing
      setTimeout(() => setLocation("/history"), 600);
    },
    onError: (err: Error) => {
      toast({ title: t.baseline.runFailed, description: err.message, variant: "destructive" });
    },
  });

  const selectedVideo = inputCatalog?.videos.find((v) => v.path === selectedInput) ?? null;
  const selectedCfg = configs?.find((c) => c.name === selectedConfig) ?? null;
  const canSubmit = !!selectedInput && !!selectedConfig && !createRun.isPending;

  // Validate frame-range fields
  const startFrameNum = startFrame.trim() === "" ? null : Math.max(0, parseInt(startFrame, 10) || 0);
  const maxFramesNum = maxFrames.trim() === "" ? null : Math.max(1, parseInt(maxFrames, 10) || 0);
  const frameRangeInvalid =
    (startFrame.trim() !== "" && Number.isNaN(parseInt(startFrame, 10))) ||
    (maxFrames.trim() !== "" && Number.isNaN(parseInt(maxFrames, 10)));

  function handleSubmit() {
    if (!canSubmit || frameRangeInvalid) return;
    createRun.mutate({
      config_name: selectedConfig,
      input_video: selectedInput,
      enable_postprocess: enablePostprocess,
      enable_follow_cam: enableFollowCam,
      start_frame: startFrameNum,
      max_frames: maxFramesNum,
      config_patch: accepted && suggestion ? suggestion.config_patch : undefined,
      notes:
        accepted && suggestion
          ? `Baseline run · field setup applied (${suggestion.confidence})`
          : undefined,
    });
  }

  function applySuggestionUpdate(nextSuggestion: FieldSuggestionResponse) {
    setSuggestion(nextSuggestion);
    setAccepted(false);
    setManualError(null);
  }

  function applyFieldInput() {
    if (!suggestion) return;
    const parsed = parsePointList(fieldInput);
    if (!parsed) {
      setManualError(t.baseline.fieldInputError);
      return;
    }
    applySuggestionUpdate(updateSuggestionShape(suggestion, parsed));
  }

  function applyExpandedInput() {
    if (!suggestion) return;
    const parsed = parsePointList(expandedInput);
    if (!parsed) {
      setManualError(t.baseline.fieldInputError);
      return;
    }
    applySuggestionUpdate(updateSuggestionShape(suggestion, suggestion.field_polygon, parsed));
  }

  const confidenceLabel = (c?: string) =>
    c === "config"
      ? t.baseline.suggestionFromConfig
      : c === "detected"
        ? t.baseline.suggestionDetected
        : t.baseline.suggestionFallback;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.baseline.title}</h1>
        <p className="text-muted-foreground mt-1">{t.baseline.subtitle}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Input Video */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Video className="h-4 w-4 text-primary" />
              {t.baseline.sourceVideo}
            </CardTitle>
            <CardDescription>{t.baseline.sourceVideoDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {inputsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.baseline.loadingVideos}
              </div>
            ) : !inputCatalog?.videos.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.baseline.noVideos}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedInput} onValueChange={setSelectedInput} data-testid="select-input-video">
                <SelectTrigger data-testid="trigger-input-video">
                  <SelectValue placeholder={t.baseline.selectVideo} />
                </SelectTrigger>
                <SelectContent>
                  {inputCatalog.videos.map((v) => (
                    <SelectItem key={v.path} value={v.path} data-testid={`option-video-${v.name}`}>
                      {v.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedVideo && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1.5">
                <p className="font-medium text-sm">{selectedVideo.name}</p>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="secondary">{formatBytes(selectedVideo.size_bytes)}</Badge>
                  <Badge variant="secondary">{formatDateTime(selectedVideo.modified_at)}</Badge>
                </div>
                <p className="text-xs text-muted-foreground font-mono truncate">{selectedVideo.path}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Config */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Layers className="h-4 w-4 text-primary" />
              {t.baseline.baselineConfig}
            </CardTitle>
            <CardDescription>{t.baseline.baselineConfigDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {configsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.baseline.loadingConfigs}
              </div>
            ) : !configs?.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.baseline.noConfigs}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedConfig} onValueChange={setSelectedConfig} data-testid="select-config">
                <SelectTrigger data-testid="trigger-config">
                  <SelectValue placeholder={t.baseline.selectConfig} />
                </SelectTrigger>
                <SelectContent>
                  {configs.map((c) => (
                    <SelectItem key={c.name} value={c.name} data-testid={`option-config-${c.name}`}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedCfg && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1.5">
                <p className="font-medium text-sm">{selectedCfg.name}</p>
                <div className="flex flex-wrap gap-2">
                  <Badge variant={selectedCfg.postprocess_enabled ? "default" : "secondary"}>
                    {selectedCfg.postprocess_enabled ? t.baseline.cleanupOn : t.baseline.cleanupOff}
                  </Badge>
                  <Badge variant={selectedCfg.follow_cam_enabled ? "default" : "secondary"}>
                    {selectedCfg.follow_cam_enabled ? t.baseline.followCamOn : t.baseline.followCamOff}
                  </Badge>
                </div>
                {selectedCfg.created_at && (
                  <p className="text-xs text-muted-foreground">{formatDateTime(selectedCfg.created_at)}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Field Setup — preview + AI suggestion (KEY baseline calibration step) */}
      <Card data-testid="card-field-setup">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Map className="h-4 w-4 text-primary" />
            {t.baseline.fieldSetup}
          </CardTitle>
          <CardDescription>{t.baseline.fieldSetupDesc}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {!selectedInput ? (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{t.baseline.fieldSelectVideoFirst}</AlertDescription>
            </Alert>
          ) : (
            <>
              <FieldPreviewCanvas
                inputVideo={selectedInput}
                patch={suggestion?.config_patch ?? null}
                preview={fieldPreview}
                onPreviewChange={setFieldPreview}
              />

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant={suggestion ? "outline" : "default"}
                  size="sm"
                  onClick={() => fetchSuggestion.mutate()}
                  disabled={!fieldPreview || fetchSuggestion.isPending}
                  data-testid="button-get-field-suggestion"
                >
                  {fetchSuggestion.isPending ? (
                    <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />{t.baseline.suggesting}</>
                  ) : (
                    <><Sparkles className="h-3.5 w-3.5 mr-1.5" />
                      {suggestion ? t.baseline.regenSuggestion : t.baseline.getFieldSuggestion}</>
                  )}
                </Button>

                {suggestion && !accepted && (
                  <Button
                    size="sm"
                    onClick={() => setAccepted(true)}
                    data-testid="button-accept-suggestion"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5 mr-1.5" />
                    {t.baseline.acceptSuggestion}
                  </Button>
                )}

                {suggestion && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => { setSuggestion(null); setAccepted(false); }}
                    data-testid="button-clear-suggestion"
                  >
                    <X className="h-3.5 w-3.5 mr-1.5" />
                    {t.baseline.clearSuggestion}
                  </Button>
                )}

                {suggestion && (
                  <div className="flex items-center gap-2 ml-auto text-xs">
                    <Badge variant={accepted ? "default" : "secondary"}>
                      {confidenceLabel(suggestion.confidence)}
                    </Badge>
                    <span className="text-muted-foreground">
                      {t.baseline.coverage}: {(suggestion.field_coverage * 100).toFixed(0)}%
                    </span>
                  </div>
                )}
              </div>

              {suggestion && (
                <div className="rounded-md border bg-muted/20 p-3 space-y-3" data-testid="field-adjustments">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-muted-foreground mr-1">
                      {t.baseline.fieldAdjust}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        applySuggestionUpdate(
                          updateSuggestionShape(
                            suggestion,
                            scalePolygon(suggestion.field_polygon, suggestion.frame_width, suggestion.frame_height, 0.96, 0.98),
                          ),
                        )
                      }
                      data-testid="button-field-tighter"
                    >
                      <Minimize2 className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldTighter}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        applySuggestionUpdate(
                          updateSuggestionShape(
                            suggestion,
                            scalePolygon(suggestion.field_polygon, suggestion.frame_width, suggestion.frame_height, 1.04, 1.02),
                          ),
                        )
                      }
                      data-testid="button-field-wider"
                    >
                      <Maximize2 className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldWider}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        applySuggestionUpdate(
                          updateSuggestionShape(
                            suggestion,
                            nudgeTop(
                              suggestion.field_polygon,
                              suggestion.frame_width,
                              suggestion.frame_height,
                              -Math.max(6, suggestion.frame_height * 0.02),
                            ),
                          ),
                        )
                      }
                      data-testid="button-field-raise"
                    >
                      <ArrowUp className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldRaiseTop}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        applySuggestionUpdate(
                          updateSuggestionShape(
                            suggestion,
                            nudgeTop(
                              suggestion.field_polygon,
                              suggestion.frame_width,
                              suggestion.frame_height,
                              Math.max(6, suggestion.frame_height * 0.02),
                            ),
                          ),
                        )
                      }
                      data-testid="button-field-lower"
                    >
                      <ArrowDown className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldLowerTop}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => applySuggestionUpdate(adjustExpandedGap(suggestion, 0.9))}
                      data-testid="button-field-buffer-in"
                    >
                      <Minimize2 className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldBufferIn}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => applySuggestionUpdate(adjustExpandedGap(suggestion, 1.12))}
                      data-testid="button-field-buffer-out"
                    >
                      <Maximize2 className="h-3.5 w-3.5 mr-1.5" />
                      {t.baseline.fieldBufferOut}
                    </Button>
                  </div>

                  <div className="grid gap-3 md:grid-cols-2">
                    <div>
                      <Label htmlFor="input-field-polygon" className="text-xs text-muted-foreground">
                        {t.baseline.fieldPolygon}
                      </Label>
                      <Input
                        id="input-field-polygon"
                        value={fieldInput}
                        onChange={(e) => setFieldInput(e.target.value)}
                        onBlur={applyFieldInput}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            applyFieldInput();
                          }
                        }}
                        className="mt-1 font-mono text-xs"
                        data-testid="input-field-polygon"
                      />
                    </div>
                    <div>
                      <Label htmlFor="input-expanded-polygon" className="text-xs text-muted-foreground">
                        {t.baseline.expandedPolygon}
                      </Label>
                      <Input
                        id="input-expanded-polygon"
                        value={expandedInput}
                        onChange={(e) => setExpandedInput(e.target.value)}
                        onBlur={applyExpandedInput}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            applyExpandedInput();
                          }
                        }}
                        className="mt-1 font-mono text-xs"
                        data-testid="input-expanded-polygon"
                      />
                    </div>
                  </div>

                  {manualError && <p className="text-xs text-destructive">{manualError}</p>}
                </div>
              )}

              {suggestion && (
                <Alert
                  className={
                    accepted
                      ? "border-emerald-200 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800"
                      : ""
                  }
                >
                  {accepted ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                  ) : (
                    <Sparkles className="h-4 w-4" />
                  )}
                  <AlertDescription
                    className={accepted ? "text-emerald-700 dark:text-emerald-300" : ""}
                  >
                    {accepted ? t.baseline.suggestionAccepted : t.baseline.suggestionPending}
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Run Options */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t.baseline.runOptions}</CardTitle>
          <CardDescription>{t.baseline.runOptionsDesc}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label htmlFor="switch-postprocess" className="font-medium">{t.baseline.postprocess}</Label>
              <p className="text-xs text-muted-foreground mt-0.5">{t.baseline.postprocessDesc}</p>
            </div>
            <Switch
              id="switch-postprocess"
              checked={enablePostprocess}
              onCheckedChange={setEnablePostprocess}
              data-testid="switch-postprocess"
            />
          </div>
          <Separator />
          <div className="flex items-center justify-between">
            <div>
              <Label htmlFor="switch-followcam" className="font-medium">{t.baseline.followCam}</Label>
              <p className="text-xs text-muted-foreground mt-0.5">{t.baseline.followCamDesc}</p>
            </div>
            <Switch
              id="switch-followcam"
              checked={enableFollowCam}
              onCheckedChange={setEnableFollowCam}
              data-testid="switch-followcam"
            />
          </div>
          <Separator />
          <div>
            <Label className="font-medium">{t.baseline.frameRange}</Label>
            <p className="text-xs text-muted-foreground mt-0.5 mb-3">{t.baseline.frameRangeDesc}</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor="input-start-frame" className="text-xs text-muted-foreground">
                  {t.baseline.startFrame}
                </Label>
                <Input
                  id="input-start-frame"
                  type="number"
                  min="0"
                  inputMode="numeric"
                  placeholder={t.baseline.startFramePlaceholder}
                  value={startFrame}
                  onChange={(e) => setStartFrame(e.target.value)}
                  data-testid="input-start-frame"
                  className="mt-1"
                />
              </div>
              <div>
                <Label htmlFor="input-max-frames" className="text-xs text-muted-foreground">
                  {t.baseline.maxFrames}
                </Label>
                <Input
                  id="input-max-frames"
                  type="number"
                  min="1"
                  inputMode="numeric"
                  placeholder={t.baseline.maxFramesPlaceholder}
                  value={maxFrames}
                  onChange={(e) => setMaxFrames(e.target.value)}
                  data-testid="input-max-frames"
                  className="mt-1"
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground mt-2">{t.baseline.frameRangeHint}</p>
          </div>
        </CardContent>
      </Card>

      {createRun.isSuccess && (
        <Alert className="border-emerald-200 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          <AlertDescription className="text-emerald-700 dark:text-emerald-300 flex items-center gap-2 flex-wrap">
            <span>{t.baseline.runQueuedDesc(createRun.data?.run_id ?? "")}</span>
            <button
              type="button"
              onClick={() => setLocation("/history")}
              className="underline font-medium inline-flex items-center gap-1 hover:opacity-80"
              data-testid="link-go-history"
            >
              {t.baseline.goToHistory}
              <ArrowRight className="h-3 w-3" />
            </button>
          </AlertDescription>
        </Alert>
      )}

      {createRun.isError && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{createRun.error?.message}</AlertDescription>
        </Alert>
      )}

      <div className="flex justify-end">
        <Button
          size="lg"
          onClick={handleSubmit}
          disabled={!canSubmit || frameRangeInvalid}
          data-testid="button-start-run"
        >
          {createRun.isPending ? (
            <><Loader2 className="h-4 w-4 mr-2 animate-spin" />{t.baseline.launching}</>
          ) : (
            <><Play className="h-4 w-4 mr-2" />{t.baseline.startBtn}</>
          )}
        </Button>
      </div>
    </div>
  );
}
