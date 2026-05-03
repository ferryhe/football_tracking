import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn, formatBytes, formatDateTime, runMoment, statusBadgeClass } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sparkles,
  Brain,
  AlertCircle,
  Loader2,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Film,
  CopyPlus,
  FileText,
  RotateCcw,
  Save,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { FieldPreviewCanvas } from "@/components/FieldPreviewCanvas";
import type { AIExplainResponse, AISuggestion, ArtifactSummary, FieldPreviewResponse } from "@/lib/types";
import { useLanguage } from "@/contexts/LanguageContext";

function encodeArtifactPath(name: string): string {
  return name.split("/").map((segment) => encodeURIComponent(segment)).join("/");
}

function artifactUrl(runId: string, artifact: ArtifactSummary): string {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeArtifactPath(artifact.name)}`;
}

function pickPlaybackArtifact(artifacts: ArtifactSummary[]): ArtifactSummary | null {
  const videos = artifacts.filter((artifact) => artifact.exists && artifact.kind === "video");
  return (
    videos.find((artifact) => /\.(web|browser|h264)\.mp4$/i.test(artifact.name)) ??
    videos.find((artifact) => artifact.name === "annotated.cleaned.mp4") ??
    videos.find((artifact) => artifact.name === "annotated.mp4") ??
    videos.find((artifact) => artifact.name.toLowerCase().includes("follow")) ??
    videos[0] ??
    null
  );
}

export default function AIAnalysisPage() {
  const { t, language } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedRunId, setSelectedRunId] = useState("");
  const [objective, setObjective] = useState("");
  const [showPatch, setShowPatch] = useState(false);
  const [suggestion, setSuggestion] = useState<AISuggestion | null>(null);
  const [outputConfigName, setOutputConfigName] = useState("");
  const [selectedConfigName, setSelectedConfigName] = useState("");
  const [configText, setConfigText] = useState("");
  const [configExplanation, setConfigExplanation] = useState<AIExplainResponse | null>(null);
  const [showFullConfigExplanation, setShowFullConfigExplanation] = useState(false);
  const [fieldPreview, setFieldPreview] = useState<FieldPreviewResponse | null>(null);

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 10_000,
  });

  const { data: configs, isLoading: configsLoading } = useQuery({
    queryKey: ["configs"],
    queryFn: api.listConfigs,
    refetchInterval: 30_000,
  });

  const analysableRuns = (runs ?? []).filter((r) => r.status === "completed" || r.status === "failed");
  const selectedRun = analysableRuns.find((r) => r.run_id === selectedRunId) ?? null;
  const playbackArtifact = selectedRun ? pickPlaybackArtifact(selectedRun.artifacts) : null;
  const playbackUrl = selectedRun && playbackArtifact ? artifactUrl(selectedRun.run_id, playbackArtifact) : null;

  const {
    data: configDetail,
    error: configDetailError,
    isLoading: configDetailLoading,
    refetch: refetchConfig,
  } = useQuery({
    queryKey: ["config", selectedConfigName],
    queryFn: () => api.getConfig(selectedConfigName),
    enabled: !!selectedConfigName,
  });

  useEffect(() => {
    if (selectedRun?.config_name) setSelectedConfigName(selectedRun.config_name);
  }, [selectedRun?.config_name]);

  useEffect(() => {
    if (!selectedConfigName && configs?.length) setSelectedConfigName(configs[0].name);
  }, [configs, selectedConfigName]);

  useEffect(() => {
    if (configDetail) setConfigText(configDetail.text);
  }, [configDetail]);

  useEffect(() => {
    setConfigExplanation(null);
    setShowFullConfigExplanation(false);
  }, [selectedConfigName]);

  // Reset preview when run changes
  function handleRunChange(id: string) {
    setSelectedRunId(id);
    setFieldPreview(null);
    setSuggestion(null);
    setOutputConfigName("");
  }

  const recommend = useMutation({
    mutationFn: () =>
      api.aiRecommend({ run_id: selectedRunId, objective: objective.trim() || undefined, language }),
    onSuccess: (data) => {
      setSuggestion(data);
      setOutputConfigName(data.output_name_suggestion ?? "");
      toast({ title: t.aiAnalysis.recommendationReady });
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.recommendationFailed, description: err.message, variant: "destructive" });
    },
  });

  const saveConfig = useMutation({
    mutationFn: () => {
      if (!selectedRun?.config_name || !suggestion) {
        throw new Error(t.aiAnalysis.noConfigPatch);
      }
      if (!suggestion.patch || Object.keys(suggestion.patch).length === 0) {
        throw new Error(t.aiAnalysis.noConfigPatch);
      }
      if (configDirty) {
        throw new Error(t.aiAnalysis.unsavedConfigConflict);
      }
      if (selectedConfigName !== selectedRun.config_name) {
        throw new Error(t.aiAnalysis.configBaseMismatch);
      }
      return api.deriveConfig({
        base_config_name: selectedConfigName,
        output_name: outputConfigName.trim() || suggestion.output_name_suggestion || "tuned_config",
        patch: suggestion.patch,
      });
    },
    onSuccess: (detail) => {
      void queryClient.invalidateQueries({ queryKey: ["configs"] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.aiAnalysis.configSaved, description: detail.name });
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configSaveFailed, description: err.message, variant: "destructive" });
    },
  });

  const saveConfigFile = useMutation({
    mutationFn: () => api.updateConfig(selectedConfigName, { content: configText }),
    onSuccess: (detail) => {
      setConfigText(detail.text);
      void queryClient.invalidateQueries({ queryKey: ["configs"] });
      void queryClient.invalidateQueries({ queryKey: ["config", detail.name] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.aiAnalysis.configFileSaved, description: detail.name });
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configFileSaveFailed, description: err.message, variant: "destructive" });
    },
  });

  const explainConfig = useMutation({
    mutationFn: () => api.aiExplain({ config_name: selectedConfigName, language }),
    onSuccess: (data) => {
      setConfigExplanation(data);
      setShowFullConfigExplanation(false);
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configExplanationFailed, description: err.message, variant: "destructive" });
    },
  });

  const configDirty = !!configDetail && configText !== configDetail.text;
  const aiSaveBlockedByEditor =
    configDirty || (!!selectedRun?.config_name && selectedConfigName !== selectedRun.config_name);
  const canSaveConfig =
    !!selectedRun?.config_name &&
    !!suggestion?.patch &&
    Object.keys(suggestion.patch).length > 0 &&
    !aiSaveBlockedByEditor &&
    !saveConfig.isPending;
  const canSaveConfigFile = !!selectedConfigName && configDirty && !saveConfigFile.isPending;
  const configExplanationPreviewLimit = 8;
  const visibleConfigEvidence = configExplanation
    ? showFullConfigExplanation
      ? configExplanation.evidence
      : configExplanation.evidence.slice(0, configExplanationPreviewLimit)
    : [];
  const hiddenConfigEvidenceCount = configExplanation
    ? Math.max(0, configExplanation.evidence.length - visibleConfigEvidence.length)
    : 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.aiAnalysis.title}</h1>
        <p className="text-muted-foreground mt-1">{t.aiAnalysis.subtitle}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Run selector */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Brain className="h-4 w-4 text-primary" />
              {t.aiAnalysis.selectRun}
            </CardTitle>
            <CardDescription>{t.aiAnalysis.selectRunDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.aiAnalysis.loadingRuns}
              </div>
            ) : !analysableRuns.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.aiAnalysis.noRuns}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedRunId} onValueChange={handleRunChange} data-testid="select-ai-run">
                <SelectTrigger data-testid="trigger-ai-run">
                  <SelectValue placeholder={t.aiAnalysis.selectRunPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {analysableRuns.map((r) => (
                    <SelectItem key={r.run_id} value={r.run_id} data-testid={`option-ai-run-${r.run_id}`}>
                      {r.run_id} · {r.status}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedRun && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1.5">
                <div className="flex items-center gap-2">
                  <span className={cn("text-xs font-medium px-2 py-0.5 rounded-full", statusBadgeClass(selectedRun.status))}>
                    {selectedRun.status}
                  </span>
                  <span className="text-xs text-muted-foreground">{formatDateTime(runMoment(selectedRun))}</span>
                </div>
                <p className="text-xs font-mono text-muted-foreground truncate">{selectedRun.output_dir}</p>
                {selectedRun.input_video && (
                  <p className="text-xs text-muted-foreground truncate">{selectedRun.input_video}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Objective */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4 text-primary" />
              {t.aiAnalysis.objective}
            </CardTitle>
            <CardDescription>{t.aiAnalysis.objectiveDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Label htmlFor="input-objective" className="sr-only">{t.aiAnalysis.objective}</Label>
            <Textarea
              id="input-objective"
              placeholder={t.aiAnalysis.objectivePlaceholder}
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              rows={4}
              data-testid="input-objective"
              className="resize-none"
            />
            <Button
              onClick={() => recommend.mutate()}
              disabled={!selectedRunId || recommend.isPending}
              className="w-full"
              data-testid="button-ai-recommend"
            >
              {recommend.isPending ? (
                <><Loader2 className="h-4 w-4 mr-2 animate-spin" />{t.aiAnalysis.analysing}</>
              ) : (
                <><Sparkles className="h-4 w-4 mr-2" />{t.aiAnalysis.getRecommendation}</>
              )}
            </Button>
          </CardContent>
        </Card>
      </div>

      <Card data-testid="card-config-file-editor">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4 text-primary" />
            {t.aiAnalysis.configFile}
          </CardTitle>
          <CardDescription>{t.aiAnalysis.configFileDesc}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {configsLoading ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t.aiAnalysis.loadingConfigs}
            </div>
          ) : !configs?.length ? (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{t.dashboard.noConfigs}</AlertDescription>
            </Alert>
          ) : (
            <Select value={selectedConfigName} onValueChange={setSelectedConfigName} data-testid="select-editor-config">
              <SelectTrigger data-testid="trigger-editor-config">
                <SelectValue placeholder={t.aiAnalysis.selectConfig} />
              </SelectTrigger>
              <SelectContent>
                {configs.map((config) => (
                  <SelectItem key={config.name} value={config.name} data-testid={`option-editor-config-${config.name}`}>
                    {config.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}

          {configDetail?.path && (
            <p className="text-xs text-muted-foreground truncate">
              {t.aiAnalysis.configFilePath}: <span className="font-mono">{configDetail.path}</span>
            </p>
          )}

          {configDirty && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{t.aiAnalysis.unsavedConfigHint}</AlertDescription>
            </Alert>
          )}

          {configDetailError ? (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {configDetailError instanceof Error ? configDetailError.message : t.aiAnalysis.configLoadFailed}
              </AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-2">
              <Textarea
                value={configText}
                onChange={(event) => setConfigText(event.target.value)}
                disabled={!selectedConfigName || configDetailLoading}
                placeholder={configDetailLoading ? t.aiAnalysis.loadingConfig : ""}
                className="min-h-[360px] resize-y font-mono text-xs leading-relaxed"
                spellCheck={false}
                data-testid="textarea-config-yaml"
              />
              <div className="flex flex-wrap justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => explainConfig.mutate()}
                  disabled={!selectedConfigName || configDirty || explainConfig.isPending}
                  data-testid="button-explain-config"
                >
                  {explainConfig.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      {t.aiAnalysis.explainingConfig}
                    </>
                  ) : (
                    <>
                      <Brain className="h-4 w-4 mr-2" />
                      {t.aiAnalysis.explainConfig}
                    </>
                  )}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    if (configDetail) setConfigText(configDetail.text);
                    void refetchConfig();
                  }}
                  disabled={!selectedConfigName || configDetailLoading || saveConfigFile.isPending}
                  data-testid="button-reload-config-file"
                >
                  <RotateCcw className="h-4 w-4 mr-2" />
                  {t.aiAnalysis.reloadConfigFile}
                </Button>
                <Button
                  type="button"
                  onClick={() => saveConfigFile.mutate()}
                  disabled={!canSaveConfigFile}
                  data-testid="button-save-config-file"
                >
                  {saveConfigFile.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      {t.aiAnalysis.savingConfigFile}
                    </>
                  ) : (
                    <>
                      <Save className="h-4 w-4 mr-2" />
                      {t.aiAnalysis.saveConfigFile}
                    </>
                  )}
                </Button>
              </div>
              {configExplanation && (
                <div className="rounded-md border bg-muted/40 p-3 space-y-2" data-testid="config-explanation">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium">{t.aiAnalysis.configExplanation}</p>
                    {configExplanation.evidence.length > configExplanationPreviewLimit && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 shrink-0"
                        onClick={() => setShowFullConfigExplanation((open) => !open)}
                        data-testid="button-toggle-config-explanation"
                      >
                        {showFullConfigExplanation ? (
                          <>
                            <ChevronUp className="h-4 w-4 mr-1.5" />
                            {t.aiAnalysis.collapseConfigExplanation}
                          </>
                        ) : (
                          <>
                            <ChevronDown className="h-4 w-4 mr-1.5" />
                            {t.aiAnalysis.expandConfigExplanation(hiddenConfigEvidenceCount)}
                          </>
                        )}
                      </Button>
                    )}
                  </div>
                  <p className="text-sm text-muted-foreground">{configExplanation.summary}</p>
                  {visibleConfigEvidence.length > 0 && (
                    <ul className="space-y-1">
                      {visibleConfigEvidence.map((item, index) => (
                        <li key={index} className="flex gap-2 text-xs text-muted-foreground">
                          <span className="text-primary">•</span>
                          <span>{item}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Result Video */}
      {selectedRun && (
        <Card data-testid="card-result-video">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Film className="h-4 w-4 text-primary" />
              {t.aiAnalysis.resultVideo}
            </CardTitle>
            <CardDescription>
              {playbackArtifact ? t.aiAnalysis.resultVideoDesc : t.aiAnalysis.noResultVideo}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {playbackArtifact && playbackUrl ? (
              <>
                <div className="overflow-hidden rounded-md border bg-black">
                  <video
                    key={`${selectedRun.run_id}-${playbackArtifact.name}`}
                    className="block max-h-[70vh] w-full bg-black"
                    controls
                    preload="metadata"
                    data-testid="video-run-artifact"
                  >
                    <source src={playbackUrl} type={playbackArtifact.content_type ?? "video/mp4"} />
                  </video>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">{playbackArtifact.name}</span>
                  <span>{formatBytes(playbackArtifact.size_bytes)}</span>
                </div>
              </>
            ) : selectedRun.input_video ? (
              <>
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{t.aiAnalysis.fieldPreviewFallback}</AlertDescription>
                </Alert>
                <FieldPreviewCanvas
                  inputVideo={selectedRun.input_video}
                  suggestion={suggestion}
                  preview={fieldPreview}
                  onPreviewChange={setFieldPreview}
                />
              </>
            ) : (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.aiAnalysis.noInputVideo}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      )}

      {/* AI Suggestion Results */}
      {suggestion && (
        <Card data-testid="card-ai-suggestion">
          <CardHeader className="pb-3">
            <div className="flex items-start gap-3">
              <CheckCircle2 className="h-5 w-5 text-emerald-500 mt-0.5 shrink-0" />
              <div>
                <CardTitle className="text-base">{suggestion.title}</CardTitle>
                <CardDescription className="mt-1">{suggestion.diagnosis}</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="text-sm font-medium mb-1">{t.aiAnalysis.recommendation}</p>
              <p className="text-sm text-muted-foreground">{suggestion.recommendation}</p>
            </div>

            {suggestion.expected_tradeoff && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-1">{t.aiAnalysis.expectedTradeoff}</p>
                  <p className="text-sm text-muted-foreground">{suggestion.expected_tradeoff}</p>
                </div>
              </>
            )}

            {suggestion.evidence.length > 0 && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-2">{t.aiAnalysis.evidence}</p>
                  <ul className="space-y-1">
                    {suggestion.evidence.map((e, i) => (
                      <li key={i} className="text-sm text-muted-foreground flex gap-2">
                        <span className="text-primary mt-0.5">•</span>
                        {e}
                      </li>
                    ))}
                  </ul>
                </div>
              </>
            )}

            {suggestion.patch_preview.length > 0 && (
              <>
                <Separator />
                <div>
                  <button
                    type="button"
                    onClick={() => setShowPatch((p) => !p)}
                    className="flex items-center gap-2 text-sm font-medium hover:text-primary transition-colors"
                    data-testid="button-toggle-patch"
                  >
                    {t.aiAnalysis.configPatchPreview}
                    {showPatch ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                  </button>
                  {showPatch && (
                    <pre className="mt-2 rounded-md bg-muted p-3 text-xs font-mono overflow-x-auto">
                      {suggestion.patch_preview.join("\n")}
                    </pre>
                  )}
                </div>
              </>
            )}

            {(suggestion.output_name_suggestion || selectedRun?.config_name) && (
              <div className="rounded-md bg-accent/50 p-3 space-y-2">
                <Label htmlFor="input-tuned-config-name" className="text-xs text-muted-foreground">
                  {t.aiAnalysis.suggestedOutputName}
                </Label>
                {aiSaveBlockedByEditor && (
                  <Alert>
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>
                      {configDirty ? t.aiAnalysis.unsavedConfigConflict : t.aiAnalysis.configBaseMismatch}
                    </AlertDescription>
                  </Alert>
                )}
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    id="input-tuned-config-name"
                    value={outputConfigName}
                    onChange={(event) => setOutputConfigName(event.target.value)}
                    placeholder={suggestion.output_name_suggestion ?? "tuned_config"}
                    className="font-mono text-sm"
                    data-testid="input-tuned-config-name"
                  />
                  <Button
                    type="button"
                    onClick={() => saveConfig.mutate()}
                    disabled={!canSaveConfig}
                    data-testid="button-save-tuned-config"
                  >
                    {saveConfig.isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        {t.aiAnalysis.savingConfig}
                      </>
                    ) : (
                      <>
                        <CopyPlus className="h-4 w-4 mr-2" />
                        {t.aiAnalysis.saveConfig}
                      </>
                    )}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
