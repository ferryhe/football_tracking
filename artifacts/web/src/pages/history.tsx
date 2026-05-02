import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatDateTime, runMoment, historyCategory, isDeliverable } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/StatusBadge";
import { Loader2, AlertCircle, Trash2, Film, Crosshair, ChevronDown, ChevronRight, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";
import type { RunRecord } from "@/lib/types";
import { useLanguage } from "@/contexts/LanguageContext";

type FilterCategory = "all" | "baseline" | "deliverable" | "failed";

function RunDetailRow({ run, onDelete }: { run: RunRecord; onDelete: (id: string) => void }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const cat = historyCategory(run);

  return (
    <div className="border rounded-lg overflow-hidden" data-testid={`run-detail-${run.run_id}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-3 px-3 py-2.5 hover:bg-muted/50 transition-colors text-left"
        data-testid={`button-expand-${run.run_id}`}
      >
        <span className="text-muted-foreground shrink-0">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </span>
        <span className="shrink-0 text-muted-foreground">
          {cat === "deliverable" ? <Film className="h-4 w-4" /> : <Crosshair className="h-4 w-4" />}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium font-mono truncate">{run.run_id}</p>
          <p className="text-xs text-muted-foreground truncate">{run.config_name ?? t.common.notAvailable}</p>
        </div>
        <div className="shrink-0 flex items-center gap-2">
          <StatusBadge status={run.status} />
          <span className="text-xs text-muted-foreground hidden sm:block">
            {formatDateTime(runMoment(run))}
          </span>
        </div>
      </button>

      {open && (
        <div className="border-t px-3 py-3 bg-muted/20 space-y-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
            <div>
              <p className="text-xs text-muted-foreground">{t.history.inputLabel}</p>
              <p className="font-mono text-xs truncate">{run.input_video ?? t.common.notAvailable}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{t.history.outputDirLabel}</p>
              <p className="font-mono text-xs truncate">{run.output_dir}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{t.history.createdLabel}</p>
              <p className="text-xs">{formatDateTime(run.created_at)}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{t.history.completedLabel}</p>
              <p className="text-xs">{formatDateTime(run.completed_at)}</p>
            </div>
            {run.parent_run_id && (
              <div className="col-span-2">
                <p className="text-xs text-muted-foreground">{t.history.parentRunLabel}</p>
                <p className="font-mono text-xs">{run.parent_run_id}</p>
              </div>
            )}
          </div>

          {run.artifacts.length > 0 && (
            <div>
              <p className="text-xs text-muted-foreground mb-1.5">{t.history.artifactsLabel}</p>
              <div className="flex flex-wrap gap-1.5">
                {run.artifacts.map((a) => (
                  <Badge key={a.name} variant={a.exists ? "default" : "secondary"} className="text-xs" data-testid={`artifact-badge-${a.name}`}>
                    {a.name}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {run.error && (
            <Alert variant="destructive" className="py-2">
              <AlertCircle className="h-3 w-3" />
              <AlertDescription className="text-xs">{run.error}</AlertDescription>
            </Alert>
          )}

          {run.notes && (
            <p className="text-xs text-muted-foreground italic">{run.notes}</p>
          )}

          <Button
            variant="destructive"
            size="sm"
            onClick={() => onDelete(run.run_id)}
            disabled={run.status === "queued" || run.status === "running"}
            data-testid={`button-delete-run-${run.run_id}`}
          >
            <Trash2 className="h-3.5 w-3.5 mr-1.5" />
            {t.history.deleteOutput}
          </Button>
        </div>
      )}
    </div>
  );
}

export default function HistoryPage() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<FilterCategory>("all");
  const [search, setSearch] = useState("");

  const { data: runs, isLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 5_000,
  });

  const deleteOutput = useMutation({
    mutationFn: (runId: string) => api.deleteRunOutput(runId),
    onSuccess: (_, runId) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      toast({ title: t.history.deleteSuccess, description: runId });
    },
    onError: (err: Error) => {
      toast({ title: t.history.deleteFailed, description: err.message, variant: "destructive" });
    },
  });

  const filters: { value: FilterCategory; label: string }[] = [
    { value: "all", label: t.history.all },
    { value: "baseline", label: t.history.baseline },
    { value: "deliverable", label: t.history.deliverable },
    { value: "failed", label: t.history.failed },
  ];

  const filtered = (runs ?? []).filter((r) => {
    const matchFilter = filter === "all" || historyCategory(r) === filter;
    const matchSearch =
      !search ||
      r.run_id.toLowerCase().includes(search.toLowerCase()) ||
      (r.config_name ?? "").toLowerCase().includes(search.toLowerCase()) ||
      (r.input_video ?? "").toLowerCase().includes(search.toLowerCase());
    return matchFilter && matchSearch;
  });

  const counts = {
    all: runs?.length ?? 0,
    baseline: (runs ?? []).filter((r) => historyCategory(r) === "baseline").length,
    deliverable: (runs ?? []).filter((r) => isDeliverable(r)).length,
    failed: (runs ?? []).filter((r) => r.status === "failed").length,
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.history.title}</h1>
        <p className="text-muted-foreground mt-1">{t.history.subtitle}</p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {filters.map((f) => (
          <button
            key={f.value}
            type="button"
            onClick={() => setFilter(f.value)}
            className={cn(
              "rounded-lg border p-3 text-left transition-colors",
              filter === f.value ? "border-primary bg-accent/60" : "bg-card hover:bg-muted/50"
            )}
            data-testid={`filter-btn-${f.value}`}
          >
            <p className="text-xs text-muted-foreground uppercase tracking-wide">{f.label}</p>
            <p className="text-2xl font-bold mt-0.5">{counts[f.value]}</p>
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
        <Input
          placeholder={t.history.searchPlaceholder}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
          data-testid="input-search-runs"
        />
      </div>

      {/* List */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t.history.runs(filtered.length)}</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm py-4">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t.history.loadingRuns}
            </div>
          ) : !filtered.length ? (
            <div className="text-center py-8 text-muted-foreground">
              <p className="font-medium">{t.history.noRuns}</p>
              <p className="text-sm mt-1">{t.history.noRunsHint}</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filtered.map((run) => (
                <RunDetailRow key={run.run_id} run={run} onDelete={(id) => deleteOutput.mutate(id)} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
