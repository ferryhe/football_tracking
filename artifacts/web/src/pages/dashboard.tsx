import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatDateTime, runMoment, historyCategory } from "@/lib/utils";
import { StatCard } from "@/components/StatCard";
import { RunRow } from "@/components/RunRow";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2, Activity, CheckCircle2, AlertCircle } from "lucide-react";
import { useState } from "react";
import type { RunRecord } from "@/lib/types";

export default function DashboardPage() {
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);

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

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 5_000,
  });

  const recentRuns = (runs ?? []).slice(0, 8);
  const activeRun = runs?.find((r) => r.status === "running" || r.status === "queued") ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-muted-foreground mt-1">Overview of system status and recent activity.</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Backend"
          value={health?.status ?? "…"}
          detail="FastAPI service"
        />
        <StatCard
          label="Configs"
          value={String(health?.config_count ?? configs?.length ?? "…")}
          detail="Available YAML configs"
        />
        <StatCard
          label="Runs"
          value={String(health?.run_count ?? runs?.length ?? "…")}
          detail="Total tracked runs"
        />
        <StatCard
          label="Active"
          value={activeRun ? "1" : "0"}
          detail={activeRun ? activeRun.run_id : "No active run"}
        />
      </div>

      {/* Active run banner */}
      {activeRun && (
        <div className="flex items-start gap-3 rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-900/20 p-4">
          <Activity className="h-5 w-5 text-blue-500 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="font-medium text-sm text-blue-900 dark:text-blue-200">
              Run in progress
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
            <CardTitle className="text-base">Recent Runs</CardTitle>
            <CardDescription>Latest tracking and render jobs</CardDescription>
          </CardHeader>
          <CardContent>
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : !recentRuns.length ? (
              <p className="text-sm text-muted-foreground">No runs yet. Start from the Baseline tab.</p>
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
            <CardTitle className="text-base">Available Configs</CardTitle>
            <CardDescription>YAML tracking configurations</CardDescription>
          </CardHeader>
          <CardContent>
            {configsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : !configs?.length ? (
              <p className="text-sm text-muted-foreground">No configs found in config/ directory.</p>
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
                    </div>
                    <div className="shrink-0 flex gap-1.5">
                      <Badge
                        variant={cfg.postprocess_enabled ? "default" : "secondary"}
                        className="text-xs"
                      >
                        Cleanup
                      </Badge>
                      <Badge
                        variant={cfg.follow_cam_enabled ? "default" : "secondary"}
                        className="text-xs"
                      >
                        Follow-cam
                      </Badge>
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
            <CardDescription>{selectedRun.config_name ?? "—"}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div>
                <p className="text-xs text-muted-foreground">Input video</p>
                <p className="font-mono text-xs truncate">{selectedRun.input_video ?? "—"}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Category</p>
                <p className="text-xs capitalize">{historyCategory(selectedRun)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Created</p>
                <p className="text-xs">{formatDateTime(selectedRun.created_at)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Finished</p>
                <p className="text-xs">{formatDateTime(selectedRun.completed_at)}</p>
              </div>
            </div>

            {selectedRun.artifacts.length > 0 && (
              <div>
                <p className="text-xs text-muted-foreground mb-1.5">Artifacts</p>
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
                <p className="text-xs text-destructive font-medium">Error</p>
                <p className="text-xs text-destructive/80 mt-0.5">{selectedRun.error}</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
