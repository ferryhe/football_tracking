import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { api } from "@/lib/api";
import type { CreateRunRequest } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Film, Clapperboard, AlertCircle, Loader2, CheckCircle2, Settings2, ArrowRight } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useLanguage } from "@/contexts/LanguageContext";

export default function DeliverablePage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [, setLocation] = useLocation();

  const [selectedInput, setSelectedInput] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [enablePostprocess, setEnablePostprocess] = useState(true);
  const [renderFinal, setRenderFinal] = useState(true);
  const [drawBallMarker, setDrawBallMarker] = useState(false);
  const [drawFrameText, setDrawFrameText] = useState(false);

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

  const selectedVideo = inputCatalog?.videos.find((video) => video.path === selectedInput) ?? null;
  const selectedCfg = configs?.find((config) => config.name === selectedConfig) ?? null;
  const canSubmit = !!selectedInput && !!selectedConfig;

  useEffect(() => {
    if (!selectedConfig || selectedInput) return;
    const config = configs?.find((item) => item.name === selectedConfig);
    if (config?.input_video) setSelectedInput(config.input_video);
  }, [configs, selectedConfig, selectedInput]);

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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.deliverable.title}</h1>
        <p className="text-muted-foreground mt-1">{t.deliverable.subtitle}</p>
      </div>

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
