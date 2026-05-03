import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatDateTime, historyCategory } from "@/lib/utils";
import { StatCard } from "@/components/StatCard";
import { RunRow } from "@/components/RunRow";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Loader2, Activity, CheckCircle2, AlertCircle, CopyPlus, Eye, Plus, Trash2 } from "lucide-react";
import type { ConfigListItem, RunRecord } from "@/lib/types";
import { useLanguage } from "@/contexts/LanguageContext";
import { useToast } from "@/hooks/use-toast";

export default function DashboardPage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [viewConfigOpen, setViewConfigOpen] = useState(false);
  const [viewConfigName, setViewConfigName] = useState<string | null>(null);
  const [createConfigOpen, setCreateConfigOpen] = useState(false);
  const [baseConfigName, setBaseConfigName] = useState("");
  const [outputConfigName, setOutputConfigName] = useState("");
  const [configToDelete, setConfigToDelete] = useState<ConfigListItem | null>(null);

  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5_000,
  });

  const { data: configs, isLoading: configsLoading } = useQuery({
    queryKey: ["configs"],
    queryFn: api.listConfigs,
    refetchInterval: 30_000,
  });

  const {
    data: configDetail,
    error: configDetailError,
    isLoading: configDetailLoading,
  } = useQuery({
    queryKey: ["config", viewConfigName],
    queryFn: () => api.getConfig(viewConfigName ?? ""),
    enabled: viewConfigOpen && !!viewConfigName,
  });

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 5_000,
  });

  const recentRuns = (runs ?? []).slice(0, 8);
  const activeRun = runs?.find((r) => r.status === "running" || r.status === "queued") ?? null;

  function refreshConfigQueries() {
    void queryClient.invalidateQueries({ queryKey: ["configs"] });
    void queryClient.invalidateQueries({ queryKey: ["health"] });
  }

  const deriveConfig = useMutation({
    mutationFn: api.deriveConfig,
    onSuccess: (detail) => {
      toast({ title: t.dashboard.configCreated, description: detail.name });
      setCreateConfigOpen(false);
      setOutputConfigName("");
      refreshConfigQueries();
    },
    onError: (err: Error) => {
      toast({ title: t.dashboard.configCreateFailed, description: err.message, variant: "destructive" });
    },
  });

  const deleteConfig = useMutation({
    mutationFn: api.deleteConfig,
    onSuccess: (result) => {
      toast({ title: t.dashboard.configDeleted, description: result.name });
      if (viewConfigName === result.name) setViewConfigOpen(false);
      setConfigToDelete(null);
      refreshConfigQueries();
    },
    onError: (err: Error) => {
      toast({ title: t.dashboard.configDeleteFailed, description: err.message, variant: "destructive" });
    },
  });

  function openCreateConfig(baseName?: string) {
    setBaseConfigName(baseName ?? configs?.[0]?.name ?? "");
    setOutputConfigName("");
    setCreateConfigOpen(true);
  }

  function openViewConfig(name: string) {
    setViewConfigName(name);
    setViewConfigOpen(true);
  }

  function handleCreateConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const baseName = baseConfigName.trim();
    const outputName = outputConfigName.trim();
    if (!baseName || !outputName) return;
    deriveConfig.mutate({
      base_config_name: baseName,
      output_name: outputName,
      patch: {},
    });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.dashboard.title}</h1>
        <p className="text-muted-foreground mt-1">{t.dashboard.subtitle}</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label={t.dashboard.backend}
          value={health?.status ?? "…"}
          detail={t.dashboard.backendDetail}
        />
        <StatCard
          label={t.dashboard.configs}
          value={String(health?.config_count ?? configs?.length ?? "…")}
          detail={t.dashboard.configsDetail}
        />
        <StatCard
          label={t.dashboard.runs}
          value={String(health?.run_count ?? runs?.length ?? "…")}
          detail={t.dashboard.runsDetail}
        />
        <StatCard
          label={t.dashboard.active}
          value={activeRun ? "1" : "0"}
          detail={activeRun ? activeRun.run_id : t.dashboard.activeNone}
        />
      </div>

      {/* Active run banner */}
      {activeRun && (
        <div className="flex items-start gap-3 rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-900/20 p-4">
          <Activity className="h-5 w-5 text-blue-500 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="font-medium text-sm text-blue-900 dark:text-blue-200">
              {t.dashboard.activeRunTitle}
            </p>
            <p className="text-xs font-mono text-blue-700 dark:text-blue-300 truncate mt-0.5">
              {activeRun.run_id}
            </p>
          </div>
          <StatusBadge status={activeRun.status} />
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Recent runs */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{t.dashboard.recentRuns}</CardTitle>
            <CardDescription>{t.dashboard.recentRunsDesc}</CardDescription>
          </CardHeader>
          <CardContent>
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" /> {t.dashboard.loadingRuns}
              </div>
            ) : !recentRuns.length ? (
              <p className="text-sm text-muted-foreground">{t.dashboard.noRuns}</p>
            ) : (
              <div className="space-y-2">
                {recentRuns.map((run) => (
                  <RunRow
                    key={run.run_id}
                    run={run}
                    selected={selectedRun?.run_id === run.run_id}
                    onClick={() => setSelectedRun(run)}
                  />
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Configs */}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <CardTitle className="text-base">{t.dashboard.availableConfigs}</CardTitle>
                <CardDescription>{t.dashboard.availableConfigsDesc}</CardDescription>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => openCreateConfig()}
                disabled={!configs?.length}
                data-testid="button-new-config"
              >
                <Plus className="h-4 w-4 mr-2" />
                {t.dashboard.newConfig}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {configsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" /> {t.dashboard.loadingConfigs}
              </div>
            ) : !configs?.length ? (
              <p className="text-sm text-muted-foreground">{t.dashboard.noConfigs}</p>
            ) : (
              <div className="space-y-2">
                {configs.map((cfg) => (
                  <div
                    key={cfg.name}
                    className="flex items-start gap-3 rounded-lg border bg-card px-3 py-2.5"
                    data-testid={`config-row-${cfg.name}`}
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">{cfg.name}</p>
                      {cfg.created_at && (
                        <p className="text-xs text-muted-foreground">{formatDateTime(cfg.created_at)}</p>
                      )}
                      <p className="text-xs font-mono text-muted-foreground truncate">{cfg.path}</p>
                    </div>
                    <div className="shrink-0 flex flex-col items-end gap-2">
                      <div className="flex flex-wrap justify-end gap-1.5">
                        <Badge variant={cfg.postprocess_enabled ? "default" : "secondary"} className="text-xs">
                          {cfg.postprocess_enabled ? t.baseline.cleanupOn : t.baseline.cleanupOff}
                        </Badge>
                        <Badge variant={cfg.follow_cam_enabled ? "default" : "secondary"} className="text-xs">
                          {cfg.follow_cam_enabled ? t.baseline.followCamOn : t.baseline.followCamOff}
                        </Badge>
                      </div>
                      <div className="flex gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          title={t.dashboard.viewConfig}
                          onClick={() => openViewConfig(cfg.name)}
                          data-testid={`button-view-config-${cfg.name}`}
                        >
                          <Eye className="h-4 w-4" />
                          <span className="sr-only">{t.dashboard.viewConfig}</span>
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          title={t.dashboard.duplicateConfig}
                          onClick={() => openCreateConfig(cfg.name)}
                          data-testid={`button-duplicate-config-${cfg.name}`}
                        >
                          <CopyPlus className="h-4 w-4" />
                          <span className="sr-only">{t.dashboard.duplicateConfig}</span>
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-destructive hover:text-destructive"
                          title={t.dashboard.deleteConfig}
                          onClick={() => setConfigToDelete(cfg)}
                          data-testid={`button-delete-config-${cfg.name}`}
                        >
                          <Trash2 className="h-4 w-4" />
                          <span className="sr-only">{t.dashboard.deleteConfig}</span>
                        </Button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Selected run detail */}
      {selectedRun && (
        <Card data-testid="card-selected-run">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-mono">{selectedRun.run_id}</CardTitle>
              <StatusBadge status={selectedRun.status} />
            </div>
            <CardDescription>{selectedRun.config_name ?? t.common.notAvailable}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div>
                <p className="text-xs text-muted-foreground">{t.dashboard.inputVideo}</p>
                <p className="font-mono text-xs truncate">{selectedRun.input_video ?? t.common.notAvailable}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{t.dashboard.category}</p>
                <p className="text-xs capitalize">{historyCategory(selectedRun)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{t.dashboard.created}</p>
                <p className="text-xs">{formatDateTime(selectedRun.created_at)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{t.dashboard.finished}</p>
                <p className="text-xs">{formatDateTime(selectedRun.completed_at)}</p>
              </div>
            </div>

            {selectedRun.artifacts.length > 0 && (
              <div>
                <p className="text-xs text-muted-foreground mb-1.5">{t.dashboard.artifacts}</p>
                <div className="flex flex-wrap gap-1.5">
                  {selectedRun.artifacts.map((a) => (
                    <Badge key={a.name} variant={a.exists ? "default" : "secondary"} className="text-xs">
                      {a.exists ? (
                        <CheckCircle2 className="h-3 w-3 mr-1" />
                      ) : (
                        <AlertCircle className="h-3 w-3 mr-1" />
                      )}
                      {a.name}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {selectedRun.error && (
              <div className="rounded-md bg-destructive/10 border border-destructive/20 p-2.5">
                <p className="text-xs text-destructive font-medium">{t.dashboard.error}</p>
                <p className="text-xs text-destructive/80 mt-0.5">{selectedRun.error}</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <Dialog open={viewConfigOpen} onOpenChange={setViewConfigOpen}>
        <DialogContent className="max-h-[85vh] max-w-4xl overflow-hidden">
          <DialogHeader>
            <DialogTitle>{viewConfigName ?? t.dashboard.viewConfig}</DialogTitle>
            {configDetail?.path && (
              <DialogDescription>
                {t.dashboard.configPath}: <span className="font-mono">{configDetail.path}</span>
              </DialogDescription>
            )}
          </DialogHeader>
          {configDetailLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t.dashboard.loadingConfig}
            </div>
          ) : configDetailError ? (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {configDetailError instanceof Error ? configDetailError.message : String(configDetailError)}
              </AlertDescription>
            </Alert>
          ) : configDetail ? (
            <div className="grid min-h-0 gap-3 overflow-y-auto pr-1 lg:grid-cols-2">
              <div className="min-w-0 space-y-2">
                <p className="text-sm font-medium">{t.dashboard.rawConfig}</p>
                <pre className="max-h-[56vh] overflow-auto rounded-md bg-muted p-3 text-xs">
                  {JSON.stringify(configDetail.raw, null, 2)}
                </pre>
              </div>
              <div className="min-w-0 space-y-2">
                <p className="text-sm font-medium">{t.dashboard.resolvedConfig}</p>
                <pre className="max-h-[56vh] overflow-auto rounded-md bg-muted p-3 text-xs">
                  {JSON.stringify(configDetail.resolved, null, 2)}
                </pre>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={createConfigOpen} onOpenChange={setCreateConfigOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.dashboard.newConfig}</DialogTitle>
            <DialogDescription>{t.dashboard.availableConfigsDesc}</DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={handleCreateConfig}>
            <div className="space-y-2">
              <Label htmlFor="select-base-config">{t.dashboard.baseConfig}</Label>
              <Select value={baseConfigName} onValueChange={setBaseConfigName}>
                <SelectTrigger id="select-base-config" data-testid="select-base-config">
                  <SelectValue placeholder={t.dashboard.baseConfig} />
                </SelectTrigger>
                <SelectContent>
                  {(configs ?? []).map((cfg) => (
                    <SelectItem key={cfg.name} value={cfg.name}>
                      {cfg.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="input-output-config-name">{t.dashboard.outputConfigName}</Label>
              <Input
                id="input-output-config-name"
                value={outputConfigName}
                onChange={(event) => setOutputConfigName(event.target.value)}
                placeholder={t.dashboard.outputConfigPlaceholder}
                data-testid="input-output-config-name"
              />
            </div>
            <DialogFooter>
              <Button
                type="submit"
                disabled={!baseConfigName || !outputConfigName.trim() || deriveConfig.isPending}
                data-testid="button-create-config"
              >
                {deriveConfig.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    {t.dashboard.creatingConfig}
                  </>
                ) : (
                  <>
                    <CopyPlus className="h-4 w-4 mr-2" />
                    {t.dashboard.createConfig}
                  </>
                )}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog open={!!configToDelete} onOpenChange={(open) => !open && setConfigToDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t.dashboard.deleteConfigTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {configToDelete ? t.dashboard.deleteConfigDesc(configToDelete.name) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteConfig.isPending}>{t.common.cancel}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteConfig.isPending}
              onClick={() => {
                if (configToDelete) deleteConfig.mutate(configToDelete.name);
              }}
              data-testid="button-confirm-delete-config"
            >
              {deleteConfig.isPending ? t.dashboard.deletingConfig : t.dashboard.deleteConfig}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
