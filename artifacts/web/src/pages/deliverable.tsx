import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useSearch } from "wouter";
import { api } from "@/lib/api";
import {
  buildLifecycleCandidateIndex,
  getRunLifecycle,
  lifecycleOperatorStateLabel,
  presentLifecycleCandidate,
  presentLifecycleSummary,
  type LifecycleCandidatePresentation,
  type LifecycleSummaryPresentation,
  type LifecycleTone,
} from "@/lib/aiLifecycle";
import { cn } from "@/lib/utils";
import type { AICandidateLifecycleCandidate, CreateRunRequest, EventCandidate, RunRecord } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Film, Clapperboard, AlertCircle, Loader2, CheckCircle2, Settings2, ArrowRight, Scissors, Target } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useLanguage } from "@/contexts/LanguageContext";

function eventCandidateCount(run: RunRecord): number | null {
  const summary = run.stats.event_candidates;
  if (summary === null || typeof summary !== "object") return null;

  const count = (summary as { candidate_count?: unknown }).candidate_count;
  return typeof count === "number" ? count : null;
}

function canRenderCandidateHighlights(run: RunRecord): boolean {
  const hasReportArtifact = run.artifacts.some((artifact) => artifact.exists && artifact.name === "event_candidates.json");
  if (!hasReportArtifact) return false;

  const count = eventCandidateCount(run);
  return count === null || count > 0;
}

function lifecycleToneClass(tone: LifecycleTone): string {
  switch (tone) {
    case "success":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "warning":
      return "border-amber-200 bg-amber-50 text-amber-700";
    case "danger":
      return "border-red-200 bg-red-50 text-red-700";
    case "pending":
      return "border-sky-200 bg-sky-50 text-sky-700";
    case "approved":
      return "border-blue-200 bg-blue-50 text-blue-700";
    case "info":
      return "border-slate-200 bg-slate-50 text-slate-700";
    case "muted":
    default:
      return "border-muted bg-muted/50 text-muted-foreground";
  }
}

function lifecycleStateText(
  labels: ReturnType<typeof useLanguage>["t"]["aiAnalysis"],
  presentation: LifecycleCandidatePresentation | LifecycleSummaryPresentation,
): string {
  return lifecycleOperatorStateLabel(presentation.state, labels.lifecycleStates);
}

function LifecycleBadge({
  labels,
  presentation,
}: {
  labels: ReturnType<typeof useLanguage>["t"]["aiAnalysis"];
  presentation: LifecycleCandidatePresentation | LifecycleSummaryPresentation;
}) {
  return (
    <Badge variant="outline" className={cn("max-w-full truncate", lifecycleToneClass(presentation.tone))}>
      {lifecycleStateText(labels, presentation)}
    </Badge>
  );
}

function draftHighlightLifecycleCandidate(candidate: EventCandidate): AICandidateLifecycleCandidate {
  return {
    candidate_id: candidate.id,
    problem_type: "highlight",
    improvement_ids: [],
    approval_ids: [],
    artifact_paths: [],
    stage: "review_only",
    comparison_status: "none",
    promotion_status: "not_promoted",
    resolution_status: "none",
    blocking_reasons: [],
  };
}

export default function DeliverablePage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [, setLocation] = useLocation();
  const routeSearch = useSearch();
  const requestedRunId =
    new URLSearchParams(routeSearch).get("run")?.trim() || null;

  const [selectedInput, setSelectedInput] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [enablePostprocess, setEnablePostprocess] = useState(true);
  const [renderFinal, setRenderFinal] = useState(true);
  const [drawBallMarker, setDrawBallMarker] = useState(false);
  const [drawFrameText, setDrawFrameText] = useState(false);
  const [selectedHighlightRunId, setSelectedHighlightRunId] = useState("");

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

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 5_000,
  });

  const highlightSourceRuns = useMemo(
    () =>
      (runs ?? []).filter(
        (run) => run.status === "completed" && run.source !== "highlight_render" && canRenderCandidateHighlights(run)
      ),
    [runs]
  );
  const selectedVideo = inputCatalog?.videos.find((video) => video.path === selectedInput) ?? null;
  const selectedCfg = configs?.find((config) => config.name === selectedConfig) ?? null;
  const selectedHighlightRun = highlightSourceRuns.find((run) => run.run_id === selectedHighlightRunId) ?? null;
  const requestedRunAvailable = requestedRunId
    ? highlightSourceRuns.some((run) => run.run_id === requestedRunId)
    : false;
  const selectedHighlightLifecycle = useMemo(() => getRunLifecycle(selectedHighlightRun), [selectedHighlightRun]);
  const selectedHighlightLifecycleIndex = useMemo(
    () => buildLifecycleCandidateIndex(selectedHighlightLifecycle),
    [selectedHighlightLifecycle],
  );
  const selectedHighlightLifecycleSummary = useMemo(
    () => presentLifecycleSummary(selectedHighlightLifecycle),
    [selectedHighlightLifecycle],
  );
  const canSubmit = !!selectedInput && !!selectedConfig;

  const eventCandidates = useQuery({
    queryKey: ["event-candidates", selectedHighlightRunId],
    queryFn: () => api.getEventCandidates(selectedHighlightRunId),
    enabled: !!selectedHighlightRunId,
    retry: false,
  });

  const candidates = useMemo(
    () => [...(eventCandidates.data?.candidates ?? [])].sort((a, b) => b.score - a.score),
    [eventCandidates.data?.candidates]
  );

  useEffect(() => {
    if (!selectedConfig || selectedInput) return;
    const config = configs?.find((item) => item.name === selectedConfig);
    if (config?.input_video) setSelectedInput(config.input_video);
  }, [configs, selectedConfig, selectedInput]);

  useEffect(() => {
    if (requestedRunId) {
      if (runs === undefined) return;
      setSelectedHighlightRunId(requestedRunAvailable ? requestedRunId : "");
      return;
    }
    if (!highlightSourceRuns.length) {
      if (selectedHighlightRunId) setSelectedHighlightRunId("");
      return;
    }
    if (!selectedHighlightRunId || !highlightSourceRuns.some((run) => run.run_id === selectedHighlightRunId)) {
      setSelectedHighlightRunId(highlightSourceRuns[0].run_id);
    }
  }, [
    highlightSourceRuns,
    requestedRunAvailable,
    requestedRunId,
    runs,
    selectedHighlightRunId,
  ]);

  const createFullDeliverable = useMutation({
    mutationFn: () => {
      const configPatch: CreateRunRequest["config_patch"] = renderFinal
        ? {
            follow_cam: {
              draw_ball_marker: drawBallMarker,
              draw_frame_text: drawFrameText,
              target_width: 1920,
              target_height: 1080,
            },
          }
        : undefined;

      return api.createRun({
        config_name: selectedConfig,
        input_video: selectedInput,
        enable_postprocess: enablePostprocess,
        enable_follow_cam: renderFinal,
        max_frames: null,
        config_patch: configPatch,
        notes: renderFinal ? "Full deliverable run · tracking + follow-cam render" : "Full tracking run",
      });
    },
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.deliverable.renderQueued, description: run.run_id });
    },
    onError: (err: Error) => {
      toast({ title: t.deliverable.renderFailed, description: err.message, variant: "destructive" });
    },
  });

  const renderHighlight = useMutation({
    mutationFn: ({ runId, candidate }: { runId: string; candidate: EventCandidate }) =>
      api.createHighlightRender(runId, {
        candidate_id: candidate.id,
        notes: `Highlight clip from ${runId} | ${candidate.id}`,
      }),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.deliverable.highlightQueued, description: run.run_id });
    },
    onError: (err: Error) => {
      toast({ title: t.deliverable.highlightFailed, description: err.message, variant: "destructive" });
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.deliverable.title}</h1>
        <p className="text-muted-foreground mt-1">{t.deliverable.subtitle}</p>
      </div>

      {requestedRunId && runs !== undefined && !requestedRunAvailable && (
        <Alert variant="destructive" role="alert">
          <AlertCircle className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            {t.deliverable.requestedRunNotFound(requestedRunId)}
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Clapperboard className="h-4 w-4 text-primary" />
              {t.deliverable.sourceVideo}
            </CardTitle>
            <CardDescription>{t.deliverable.sourceVideoDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {inputsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.deliverable.loadingVideos}
              </div>
            ) : !inputCatalog?.videos.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.noVideos}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedInput} onValueChange={setSelectedInput} data-testid="select-full-input">
                <SelectTrigger data-testid="trigger-full-input">
                  <SelectValue placeholder={t.deliverable.selectVideoPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {inputCatalog.videos.map((video) => (
                    <SelectItem key={video.path} value={video.path} data-testid={`option-full-input-${video.name}`}>
                      {video.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedVideo && (
              <div className="rounded-md bg-muted/50 p-3">
                <p className="text-xs font-medium truncate">{selectedVideo.name}</p>
                <p className="text-xs font-mono text-muted-foreground truncate">{selectedVideo.path}</p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Settings2 className="h-4 w-4 text-primary" />
              {t.deliverable.config}
            </CardTitle>
            <CardDescription>{t.deliverable.configDesc}</CardDescription>
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
                <AlertDescription>{t.deliverable.noConfigs}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedConfig} onValueChange={setSelectedConfig} data-testid="select-full-config">
                <SelectTrigger data-testid="trigger-full-config">
                  <SelectValue placeholder={t.deliverable.selectConfigPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {configs.map((config) => (
                    <SelectItem key={config.name} value={config.name} data-testid={`option-full-config-${config.name}`}>
                      {config.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedCfg && (
              <div className="rounded-md bg-muted/50 p-3 space-y-2">
                <p className="text-xs font-mono text-muted-foreground truncate">{selectedCfg.path}</p>
                <div className="flex flex-wrap gap-1.5">
                  <Badge variant={selectedCfg.postprocess_enabled ? "default" : "secondary"} className="text-xs">
                    {selectedCfg.postprocess_enabled ? t.baseline.cleanupOn : t.baseline.cleanupOff}
                  </Badge>
                  <Badge variant={selectedCfg.follow_cam_enabled ? "default" : "secondary"} className="text-xs">
                    {selectedCfg.follow_cam_enabled ? t.baseline.followCamOn : t.baseline.followCamOff}
                  </Badge>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Settings2 className="h-4 w-4 text-primary" />
              {t.deliverable.fullRunOptions}
            </CardTitle>
            <CardDescription>{t.deliverable.fullRunOptionsDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="switch-full-postprocess" className="font-medium">{t.deliverable.postprocess}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.postprocessDesc}</p>
              </div>
              <Switch
                id="switch-full-postprocess"
                checked={enablePostprocess}
                onCheckedChange={setEnablePostprocess}
                data-testid="switch-full-postprocess"
              />
            </div>
            <Separator />
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="switch-render-final" className="font-medium">{t.deliverable.renderFinal}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.renderFinalDesc}</p>
              </div>
              <Switch
                id="switch-render-final"
                checked={renderFinal}
                onCheckedChange={setRenderFinal}
                data-testid="switch-render-final"
              />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Film className="h-4 w-4 text-primary" />
              {t.deliverable.renderOptions}
            </CardTitle>
            <CardDescription>{t.deliverable.renderOptionsDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="switch-ball-marker" className="font-medium">{t.deliverable.ballMarker}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.ballMarkerDesc}</p>
              </div>
              <Switch
                id="switch-ball-marker"
                checked={drawBallMarker}
                onCheckedChange={setDrawBallMarker}
                disabled={!renderFinal}
                data-testid="switch-ball-marker"
              />
            </div>
            <Separator />
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="switch-frame-text" className="font-medium">{t.deliverable.frameText}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.frameTextDesc}</p>
              </div>
              <Switch
                id="switch-frame-text"
                checked={drawFrameText}
                onCheckedChange={setDrawFrameText}
                disabled={!renderFinal}
                data-testid="switch-frame-text"
              />
            </div>
            <Separator />
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs text-muted-foreground">{t.deliverable.width}</Label>
                <p className="font-mono text-sm font-medium">1920px</p>
              </div>
              <div>
                <Label className="text-xs text-muted-foreground">{t.deliverable.height}</Label>
                <p className="font-mono text-sm font-medium">1080px</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <div>
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Scissors className="h-4 w-4 text-primary" />
          {t.deliverable.highlightClips}
        </h2>
        <p className="text-sm text-muted-foreground mt-1">{t.deliverable.highlightClipsDesc}</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Clapperboard className="h-4 w-4 text-primary" />
              {t.deliverable.highlightSourceRun}
            </CardTitle>
            <CardDescription>{t.deliverable.highlightSourceRunDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.deliverable.loadingRuns}
              </div>
            ) : !highlightSourceRuns.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.noHighlightSourceRuns}</AlertDescription>
              </Alert>
            ) : (
              <Select
                value={selectedHighlightRunId}
                onValueChange={setSelectedHighlightRunId}
                data-testid="select-highlight-run"
              >
                <SelectTrigger data-testid="trigger-highlight-run">
                  <SelectValue placeholder={t.deliverable.selectRunPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {highlightSourceRuns.map((run) => (
                    <SelectItem key={run.run_id} value={run.run_id} data-testid={`option-highlight-run-${run.run_id}`}>
                      {run.run_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedHighlightRun && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1">
                <p className="text-xs font-mono truncate">{selectedHighlightRun.run_id}</p>
                <p className="text-xs text-muted-foreground truncate">{selectedHighlightRun.input_video ?? selectedHighlightRun.output_dir}</p>
                <div className="flex flex-wrap gap-1.5 pt-1">
                  <Badge variant="secondary">{t.deliverable.finalAiStatus}</Badge>
                  <LifecycleBadge labels={t.aiAnalysis} presentation={selectedHighlightLifecycleSummary} />
                  <Badge
                    variant={selectedHighlightLifecycleSummary.isPublishable ? "default" : "outline"}
                    className={selectedHighlightLifecycleSummary.isPublishable ? "" : "text-muted-foreground"}
                  >
                    {selectedHighlightLifecycleSummary.isPublishable
                      ? t.deliverable.finalAiPublishable
                      : t.deliverable.finalAiNotPublishable}
                  </Badge>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center justify-between gap-3 text-base">
              <span className="flex min-w-0 items-center gap-2">
                <Target className="h-4 w-4 shrink-0 text-primary" />
                <span className="truncate">{t.deliverable.eventCandidates}</span>
              </span>
              {eventCandidates.data && (
                <Badge variant="secondary" className="shrink-0">
                  {t.deliverable.candidateCount(eventCandidates.data.summary.candidate_count)}
                </Badge>
              )}
            </CardTitle>
            <CardDescription>{t.deliverable.eventCandidatesDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {!selectedHighlightRunId ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.noHighlightSourceRuns}</AlertDescription>
              </Alert>
            ) : eventCandidates.isLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.deliverable.loadingCandidates}
              </div>
            ) : eventCandidates.isError ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.candidatesUnavailable}</AlertDescription>
              </Alert>
            ) : !candidates.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.noCandidates}</AlertDescription>
              </Alert>
            ) : (
              <div className="space-y-2">
                {candidates.map((candidate) => {
                  const isRendering = renderHighlight.isPending && renderHighlight.variables?.candidate.id === candidate.id;
                  const linkedLifecycleCandidate = selectedHighlightLifecycleIndex.byCandidateId.get(candidate.id);
                  const lifecycleCandidate = linkedLifecycleCandidate ?? draftHighlightLifecycleCandidate(candidate);
                  const lifecyclePresentation = presentLifecycleCandidate(lifecycleCandidate);
                  return (
                    <div
                      key={candidate.id}
                      className="flex flex-col gap-3 rounded-md border p-3 sm:flex-row sm:items-center sm:justify-between"
                      data-testid={`event-candidate-${candidate.id}`}
                    >
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={candidate.type === "goal_candidate" ? "default" : "secondary"} className="text-xs">
                            {candidate.type.replace("_", " ")}
                          </Badge>
                          <span className="text-xs text-muted-foreground">
                            {t.deliverable.candidateFrames(candidate.start_frame, candidate.end_frame)}
                          </span>
                          <span className="text-xs font-medium tabular-nums">
                            {t.deliverable.candidateScore} {candidate.score.toFixed(2)}
                          </span>
                          <LifecycleBadge labels={t.aiAnalysis} presentation={lifecyclePresentation} />
                          {!linkedLifecycleCandidate && (
                            <Badge variant="outline" className="text-muted-foreground">
                              {t.deliverable.draftCandidate}
                            </Badge>
                          )}
                          {linkedLifecycleCandidate && !lifecyclePresentation.isPublishable && (
                            <Badge variant="outline" className="text-muted-foreground">
                              {t.deliverable.finalAiNotPublishable}
                            </Badge>
                          )}
                          {lifecyclePresentation.isPublishable && (
                            <Badge className="bg-emerald-600 text-white">
                              {t.deliverable.finalAiPublishable}
                            </Badge>
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground line-clamp-2">{candidate.reason}</p>
                      </div>
                      <Button
                        size="sm"
                        className="shrink-0"
                        onClick={() => renderHighlight.mutate({ runId: selectedHighlightRunId, candidate })}
                        disabled={renderHighlight.isPending}
                        data-testid={`button-render-highlight-${candidate.id}`}
                      >
                        {isRendering ? (
                          <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                        ) : (
                          <Scissors className="h-3.5 w-3.5 mr-1.5" />
                        )}
                        {t.deliverable.renderClip}
                      </Button>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {createFullDeliverable.isSuccess && (
        <Alert className="border-emerald-200 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          <AlertDescription className="text-emerald-700 dark:text-emerald-300 flex items-center gap-2 flex-wrap">
            <span>{createFullDeliverable.data?.run_id}</span>
            <button
              type="button"
              onClick={() => setLocation("/history")}
              className="underline font-medium inline-flex items-center gap-1 hover:opacity-80"
              data-testid="link-full-go-history"
            >
              {t.baseline.goToHistory}
              <ArrowRight className="h-3 w-3" />
            </button>
          </AlertDescription>
        </Alert>
      )}

      <div className="flex justify-end">
        <Button
          size="lg"
          onClick={() => createFullDeliverable.mutate()}
          disabled={!canSubmit || createFullDeliverable.isPending}
          data-testid="button-start-full-deliverable"
        >
          {createFullDeliverable.isPending ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              {t.deliverable.queuing}
            </>
          ) : (
            <>
              <Film className="h-4 w-4 mr-2" />
              {renderFinal ? t.deliverable.renderBtn : t.deliverable.runBtn}
            </>
          )}
        </Button>
      </div>
    </div>
  );
}
