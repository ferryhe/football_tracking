import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/utils";
import type { CreateRunRequest } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Play, Video, Layers, AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useLanguage } from "@/contexts/LanguageContext";

export default function BaselinePage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const [selectedInput, setSelectedInput] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [enablePostprocess, setEnablePostprocess] = useState(true);
  const [enableFollowCam, setEnableFollowCam] = useState(false);

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

  const createRun = useMutation({
    mutationFn: (req: CreateRunRequest) => api.createRun(req),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      toast({ title: t.baseline.runQueued, description: t.baseline.runQueuedDesc(run.run_id) });
    },
    onError: (err: Error) => {
      toast({ title: t.baseline.runFailed, description: err.message, variant: "destructive" });
    },
  });

  const selectedVideo = inputCatalog?.videos.find((v) => v.path === selectedInput) ?? null;
  const selectedCfg = configs?.find((c) => c.name === selectedConfig) ?? null;
  const canSubmit = !!selectedInput && !!selectedConfig && !createRun.isPending;

  function handleSubmit() {
    if (!canSubmit) return;
    createRun.mutate({
      config_name: selectedConfig,
      input_video: selectedInput,
      enable_postprocess: enablePostprocess,
      enable_follow_cam: enableFollowCam,
    });
  }

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
        </CardContent>
      </Card>

      {createRun.isSuccess && (
        <Alert className="border-emerald-200 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          <AlertDescription className="text-emerald-700 dark:text-emerald-300">
            {t.baseline.runQueuedDesc(createRun.data?.run_id ?? "")}
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
        <Button size="lg" onClick={handleSubmit} disabled={!canSubmit} data-testid="button-start-run">
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
