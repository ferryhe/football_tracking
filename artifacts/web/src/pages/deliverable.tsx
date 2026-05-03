import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatDateTime, runMoment, isDeliverable } from "@/lib/utils";
import type { FollowCamRenderRequest } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Film, Clapperboard, AlertCircle, Loader2, CheckCircle2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { StatusBadge } from "@/components/StatusBadge";
import { useLanguage } from "@/contexts/LanguageContext";

export default function DeliverablePage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const [selectedRunId, setSelectedRunId] = useState("");
  const [options, setOptions] = useState<FollowCamRenderRequest>({
    prefer_cleaned_track: true,
    draw_ball_marker: false,
    draw_frame_text: false,
    target_width: 1920,
    target_height: 1080,
  });

  const { data: runs, isLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 10_000,
  });

  const renderableRuns = (runs ?? []).filter((r) => r.status === "completed" && !isDeliverable(r));
  const selectedRun = renderableRuns.find((r) => r.run_id === selectedRunId) ?? null;

  const createRender = useMutation({
    mutationFn: () => api.createFollowCamRender(selectedRunId, options),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
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
        {/* Run selector */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Clapperboard className="h-4 w-4 text-primary" />
              {t.deliverable.sourceRun}
            </CardTitle>
            <CardDescription>{t.deliverable.sourceRunDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {isLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.deliverable.loadingRuns}
              </div>
            ) : !renderableRuns.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.deliverable.noRuns}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedRunId} onValueChange={setSelectedRunId} data-testid="select-render-run">
                <SelectTrigger data-testid="trigger-render-run">
                  <SelectValue placeholder={t.deliverable.selectRunPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {renderableRuns.map((r) => (
                    <SelectItem key={r.run_id} value={r.run_id} data-testid={`option-render-run-${r.run_id}`}>
                      {r.run_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedRun && (
              <div className="rounded-md bg-muted/50 p-3 space-y-2">
                <div className="flex items-center gap-2">
                  <StatusBadge status={selectedRun.status} />
                  <span className="text-xs text-muted-foreground">{formatDateTime(runMoment(selectedRun))}</span>
                </div>
                {selectedRun.input_video && (
                  <p className="text-xs text-muted-foreground truncate">{selectedRun.input_video}</p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  {selectedRun.artifacts.map((a) => (
                    <Badge key={a.name} variant={a.exists ? "default" : "secondary"} className="text-xs">
                      {a.name}
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Render options */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Film className="h-4 w-4 text-primary" />
              {t.deliverable.renderOptions}
            </CardTitle>
            <CardDescription>{t.deliverable.renderOptionsDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="switch-prefer-cleaned" className="font-medium">{t.deliverable.preferCleaned}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.preferCleanedDesc}</p>
              </div>
              <Switch
                id="switch-prefer-cleaned"
                checked={options.prefer_cleaned_track}
                onCheckedChange={(v) => setOptions((o) => ({ ...o, prefer_cleaned_track: v }))}
                data-testid="switch-prefer-cleaned"
              />
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="switch-ball-marker" className="font-medium">{t.deliverable.ballMarker}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.ballMarkerDesc}</p>
              </div>
              <Switch
                id="switch-ball-marker"
                checked={options.draw_ball_marker}
                onCheckedChange={(v) => setOptions((o) => ({ ...o, draw_ball_marker: v }))}
                data-testid="switch-ball-marker"
              />
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="switch-frame-text" className="font-medium">{t.deliverable.frameText}</Label>
                <p className="text-xs text-muted-foreground mt-0.5">{t.deliverable.frameTextDesc}</p>
              </div>
              <Switch
                id="switch-frame-text"
                checked={options.draw_frame_text}
                onCheckedChange={(v) => setOptions((o) => ({ ...o, draw_frame_text: v }))}
                data-testid="switch-frame-text"
              />
            </div>
            <Separator />
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs text-muted-foreground">{t.deliverable.width}</Label>
                <p className="font-mono text-sm font-medium">{options.target_width}px</p>
              </div>
              <div>
                <Label className="text-xs text-muted-foreground">{t.deliverable.height}</Label>
                <p className="font-mono text-sm font-medium">{options.target_height}px</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {createRender.isSuccess && (
        <Alert className="border-emerald-200 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800">
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          <AlertDescription className="text-emerald-700 dark:text-emerald-300">
            {createRender.data?.run_id}
          </AlertDescription>
        </Alert>
      )}

      <div className="flex justify-end">
        <Button size="lg" onClick={() => createRender.mutate()} disabled={!selectedRunId || createRender.isPending} data-testid="button-start-render">
          {createRender.isPending ? (
            <><Loader2 className="h-4 w-4 mr-2 animate-spin" />{t.deliverable.queuing}</>
          ) : (
            <><Film className="h-4 w-4 mr-2" />{t.deliverable.renderBtn}</>
          )}
        </Button>
      </div>
    </div>
  );
}
