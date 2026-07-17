import { useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import type { RunRecord } from "@workspace/api-client-react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  Film,
  FolderOpen,
  Loader2,
  Search,
  Square,
  Trash2,
} from "lucide-react";

import { StatusBadge } from "@/components/StatusBadge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  buildProductionHistoryGroups,
  classifyProductionProduct,
  filterProductionHistoryGroups,
  isReadyProductCandidate,
  productionArtifactUrl,
  productionHistoryCancellationTarget,
  productionHistoryDeletionBlocker,
  productionProductVerificationKey,
  type ProductionHistoryFilter,
  type ProductionHistoryGroup,
  type ProductionHistoryTimelineItem,
} from "@/lib/productionHistory";
import { cn, formatDateTime, formatDuration } from "@/lib/utils";

export async function invalidateProductionHistoryCaches(
  queryClient: QueryClient,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["asset-groups"] }),
    queryClient.invalidateQueries({ queryKey: ["runs"] }),
    queryClient.invalidateQueries({
      queryKey: ["production-history", "artifact"],
    }),
    queryClient.invalidateQueries({
      queryKey: ["production-history", "product"],
    }),
  ]);
}

function runMoment(run: RunRecord): string | null {
  return run.completed_at ?? run.started_at ?? run.created_at ?? null;
}

function GroupRunProgress({ run }: { run: RunRecord }) {
  const { t } = useLanguage();
  if (!run.progress) return null;
  const percent = Math.max(0, Math.min(100, run.progress.percent ?? 0));
  return (
    <div
      className="mt-2 space-y-1.5"
      data-testid={`group-run-progress-${run.run_id}`}
    >
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{t.history.progressStage(run.progress.stage)}</span>
        <span className="font-medium tabular-nums text-foreground">
          {Math.round(percent)}%
        </span>
      </div>
      <Progress value={percent} />
      <p className="text-xs text-muted-foreground">
        {run.progress.eta_seconds != null
          ? `${t.history.etaLabel} ${formatDuration(run.progress.eta_seconds)}`
          : `${t.history.elapsedLabel} ${formatDuration(run.progress.elapsed_seconds)}`}
      </p>
    </div>
  );
}

function qualityText(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return String(value ?? "");
  }
  const record = value as Record<string, unknown>;
  const summary = record.summary ?? record.overall_status ?? record.status;
  return typeof summary === "string"
    ? summary
    : JSON.stringify(summary ?? value, null, 2).slice(0, 1_200);
}

function ProductEvidence({ run }: { run: RunRecord }) {
  const { t } = useLanguage();
  const verificationKey = productionProductVerificationKey(run);
  const generation = run.broadcast?.status_generation ?? "";
  const verification = useQuery({
    queryKey: verificationKey ?? [
      "production-history",
      "product",
      run.run_id,
      "invalid",
    ],
    queryFn: () => api.listRunArtifacts(run.run_id, generation),
    enabled: verificationKey !== null,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  const classification = classifyProductionProduct(run, verification.data);
  const quality = useQuery({
    queryKey: [
      "production-history",
      "artifact",
      run.run_id,
      generation,
      "broadcast_quality_report.json",
    ],
    queryFn: () =>
      api.getRunArtifactJson<unknown>(
        run.run_id,
        "broadcast_quality_report.json",
        generation,
      ),
    enabled:
      classification.status === "verified" && classification.quality !== null,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });

  if (verificationKey === null) {
    return (
      <Alert
        variant="destructive"
        data-testid={`product-generation-invalid-${run.run_id}`}
      >
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>
          {t.history.invalidProductGeneration}
        </AlertDescription>
      </Alert>
    );
  }
  if (verification.isPending) {
    return (
      <div
        className="flex items-center gap-2 text-sm text-muted-foreground"
        data-testid={`product-verifying-${run.run_id}`}
      >
        <Loader2 className="h-4 w-4 animate-spin" />
        {t.history.verifyingProduct}
      </div>
    );
  }
  if (verification.isError) {
    return (
      <Alert variant="destructive" data-testid={`product-error-${run.run_id}`}>
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>
          {t.history.productVerificationFailed}: {verification.error.message}
        </AlertDescription>
      </Alert>
    );
  }
  if (classification.status !== "verified") {
    return (
      <Alert
        variant="destructive"
        data-testid={`product-missing-${run.run_id}`}
      >
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>{t.history.missingProduct}</AlertDescription>
      </Alert>
    );
  }

  const limitations = run.broadcast?.limitations ?? [];
  return (
    <section
      className="space-y-4 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4"
      data-testid={`verified-product-${run.run_id}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-medium text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 className="h-4 w-4" />
          {t.history.verifiedProduct}
        </div>
        <span className="text-xs text-muted-foreground">
          {t.history.productCreated}: {formatDateTime(runMoment(run))}
        </span>
      </div>

      <Alert>
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>{t.history.operationalReleaseGate}</AlertDescription>
      </Alert>

      <video
        controls
        preload="metadata"
        className="aspect-video w-full rounded-md bg-black"
        src={productionArtifactUrl(run.run_id, "broadcast.mp4", generation)}
        data-testid={`product-preview-${run.run_id}`}
      />

      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <h4 className="text-sm font-medium">{t.history.qualitySummary}</h4>
          {classification.quality === null ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {t.history.qualityUnavailable}
            </p>
          ) : quality.isPending ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {t.common.loading}
            </p>
          ) : quality.isError || quality.data == null ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {t.history.qualityUnavailable}
            </p>
          ) : (
            <pre
              className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 text-xs"
              data-testid={`product-quality-${run.run_id}`}
            >
              {qualityText(quality.data)}
            </pre>
          )}
        </div>
        <div>
          <h4 className="text-sm font-medium">{t.history.limitations}</h4>
          {limitations.length ? (
            <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-muted-foreground">
              {limitations.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              {t.history.noLimitations}
            </p>
          )}
        </div>
      </div>

      <div>
        <h4 className="text-sm font-medium">{t.history.downloads}</h4>
        <div className="mt-2 flex flex-wrap gap-2">
          {classification.downloads.map((artifact) => (
            <Button key={artifact.name} asChild variant="outline" size="sm">
              <a
                href={productionArtifactUrl(
                  run.run_id,
                  artifact.name,
                  generation,
                )}
                download
                data-testid={`product-download-${run.run_id}-${artifact.name}`}
              >
                <Download className="mr-1.5 h-3.5 w-3.5" />
                {artifact.name}
              </a>
            </Button>
          ))}
        </div>
      </div>
    </section>
  );
}

function lineageLabel(item: ProductionHistoryTimelineItem): string | null {
  return item.parentRunId ?? item.externalParentRunId;
}

function TimelineRow({
  item,
  groupRuns,
  onCancel,
  onDelete,
}: {
  item: ProductionHistoryTimelineItem;
  groupRuns: readonly RunRecord[];
  onCancel: (runId: string) => void;
  onDelete: (runId: string) => void;
}) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(isReadyProductCandidate(item.run));
  const cancellationTarget = productionHistoryCancellationTarget(
    item.run,
    groupRuns,
  );
  const deletionBlocker = productionHistoryDeletionBlocker(item.run, groupRuns);
  const childCount = deletionBlocker?.startsWith("children:")
    ? Number(deletionBlocker.slice("children:".length))
    : 0;
  const lineage = lineageLabel(item);

  return (
    <div
      className="overflow-hidden rounded-lg border"
      data-testid={`timeline-run-${item.run.run_id}`}
    >
      <button
        type="button"
        className="flex w-full items-start gap-3 p-3 text-left hover:bg-muted/40"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        data-testid={`timeline-toggle-${item.run.run_id}`}
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-4 w-4" />
        ) : (
          <ChevronRight className="mt-0.5 h-4 w-4" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-medium">
              {item.run.run_id}
            </span>
            <Badge variant="outline">{item.kind}</Badge>
            {isReadyProductCandidate(item.run) && (
              <Badge variant="secondary">{t.history.productCandidates}</Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatDateTime(runMoment(item.run))}
            {lineage ? ` · ${t.history.lineage}: ${lineage}` : ""}
          </p>
          <GroupRunProgress run={item.run} />
        </div>
        <StatusBadge status={item.run.status} />
      </button>

      {open && (
        <div className="space-y-4 border-t bg-muted/10 p-4">
          <div className="grid gap-2 text-xs sm:grid-cols-2">
            <p>
              <span className="text-muted-foreground">
                {t.history.inputLabel}:{" "}
              </span>
              {item.run.input_video ?? t.common.notAvailable}
            </p>
            <p>
              <span className="text-muted-foreground">
                {t.history.outputDirLabel}:{" "}
              </span>
              {item.run.output_dir}
            </p>
            <p>
              <span className="text-muted-foreground">
                {t.history.configIdentity}:{" "}
              </span>
              {item.note?.configIdentity ??
                item.run.config_name ??
                t.common.notAvailable}
            </p>
            <p>
              <span className="text-muted-foreground">
                {t.history.statusGeneration}:{" "}
              </span>
              <span className="font-mono">
                {item.run.broadcast?.status_generation ?? t.common.notAvailable}
              </span>
            </p>
          </div>

          {item.lineageIssue && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {item.lineageIssue === "ambiguous_parent"
                  ? t.history.ambiguousLineage
                  : t.history.missingLineage}
              </AlertDescription>
            </Alert>
          )}

          {item.run.error && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{item.run.error}</AlertDescription>
            </Alert>
          )}

          {isReadyProductCandidate(item.run) && (
            <ProductEvidence run={item.run} />
          )}

          <div className="flex flex-wrap items-start gap-2">
            {cancellationTarget && (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="destructive"
                    size="sm"
                    data-testid={`group-cancel-${item.run.run_id}`}
                  >
                    <Square className="mr-1.5 h-3.5 w-3.5" />
                    {t.history.cancelRun}
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>
                      {t.history.cancelRunTitle}
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                      {t.history.cancelRunDesc}
                      <span className="mt-2 block font-mono text-xs text-foreground">
                        {cancellationTarget}
                      </span>
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>{t.common.cancel}</AlertDialogCancel>
                    <AlertDialogAction
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      onClick={() => onCancel(cancellationTarget)}
                      data-testid={`group-confirm-cancel-${item.run.run_id}`}
                    >
                      {t.history.cancelRunConfirm}
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}

            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={deletionBlocker !== null}
                  data-testid={`group-delete-${item.run.run_id}`}
                >
                  <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                  {t.history.deleteOutput}
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>
                    {t.history.deleteRunTitle}
                  </AlertDialogTitle>
                  <AlertDialogDescription>
                    {t.history.deleteRunDesc}
                    <span className="mt-2 block font-mono text-xs text-foreground">
                      {item.run.run_id}
                    </span>
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>{t.common.cancel}</AlertDialogCancel>
                  <AlertDialogAction
                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    onClick={() => onDelete(item.run.run_id)}
                    data-testid={`group-confirm-delete-${item.run.run_id}`}
                  >
                    {t.history.deleteRunConfirm}
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>

            {deletionBlocker && (
              <p
                className="w-full text-xs text-muted-foreground"
                data-testid={`group-delete-blocker-${item.run.run_id}`}
              >
                {deletionBlocker === "active_run"
                  ? t.history.deletionActive
                  : t.history.deletionChildren(childCount)}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function GroupDetail({
  group,
  onCancel,
  onDelete,
}: {
  group: ProductionHistoryGroup;
  onCancel: (runId: string) => void;
  onDelete: (runId: string) => void;
}) {
  const { t } = useLanguage();
  const runs = group.timeline.map((item) => item.run);
  return (
    <div
      className="space-y-3 border-t p-4"
      data-testid={`group-detail-${group.groupId}`}
    >
      <div>
        <h3 className="font-medium">{t.history.groupDetail}</h3>
        <p className="break-all text-xs text-muted-foreground">
          {group.inputPath ?? t.history.unbound}
        </p>
      </div>
      {group.timeline.map((item) => (
        <TimelineRow
          key={item.run.run_id}
          item={item}
          groupRuns={runs}
          onCancel={onCancel}
          onDelete={onDelete}
        />
      ))}
    </div>
  );
}

export function GroupedProductionHistory() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ProductionHistoryFilter>("all");
  const [search, setSearch] = useState("");
  const [openGroupKey, setOpenGroupKey] = useState<string | null>(null);

  const assetGroups = useQuery({
    queryKey: ["asset-groups"],
    queryFn: api.listAssetGroups,
    refetchInterval: (query) => {
      const groups = buildProductionHistoryGroups(query.state.data ?? []);
      return groups.some((group) => group.summary.activeCount > 0)
        ? 5_000
        : false;
    },
  });
  const groups = useMemo(
    () => buildProductionHistoryGroups(assetGroups.data ?? []),
    [assetGroups.data],
  );
  const filtered = useMemo(
    () => filterProductionHistoryGroups(groups, search, filter),
    [filter, groups, search],
  );

  const cancelRun = useMutation({
    mutationFn: (runId: string) => api.cancelRun(runId),
    onSuccess: async (cancelled, requestedId) => {
      await invalidateProductionHistoryCaches(queryClient);
      toast({
        title: t.history.cancelRunSuccess,
        description: cancelled.run_id,
      });
    },
    onError: (error: Error) =>
      toast({
        title: t.history.cancelRunFailed,
        description: error.message,
        variant: "destructive",
      }),
  });
  const deleteRun = useMutation({
    mutationFn: (runId: string) => api.deleteRunOutput(runId),
    onSuccess: async (_, runId) => {
      await invalidateProductionHistoryCaches(queryClient);
      toast({ title: t.history.deleteSuccess, description: runId });
    },
    onError: (error: Error) =>
      toast({
        title: t.history.deleteFailed,
        description: error.message,
        variant: "destructive",
      }),
  });

  const filters: {
    value: ProductionHistoryFilter;
    label: string;
    count: number;
  }[] = [
    { value: "all", label: t.history.all, count: groups.length },
    {
      value: "active",
      label: t.history.active,
      count: groups.filter((group) => group.summary.activeCount > 0).length,
    },
    {
      value: "ready",
      label: t.history.readyCandidates,
      count: groups.filter((group) => group.summary.readyCandidateCount > 0)
        .length,
    },
    {
      value: "failed",
      label: t.history.failed,
      count: groups.filter((group) => group.summary.failedCount > 0).length,
    },
    {
      value: "cancelled",
      label: t.history.cancelled,
      count: groups.filter((group) => group.summary.cancelledCount > 0).length,
    },
  ];

  return (
    <div className="space-y-6" data-testid="grouped-production-history">
      <div>
        <h1 className="text-2xl font-bold">{t.history.groupedTitle}</h1>
        <p className="mt-1 text-muted-foreground">
          {t.history.groupedSubtitle}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {filters.map((item) => (
          <button
            key={item.value}
            type="button"
            onClick={() => setFilter(item.value)}
            className={cn(
              "rounded-lg border p-3 text-left transition-colors",
              filter === item.value
                ? "border-primary bg-accent/60"
                : "bg-card hover:bg-muted/50",
            )}
            data-testid={`group-filter-${item.value}`}
          >
            <p className="text-xs uppercase tracking-wide text-muted-foreground">
              {item.label}
            </p>
            <p className="mt-0.5 text-2xl font-bold">{item.count}</p>
          </button>
        ))}
      </div>

      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={t.history.searchPlaceholder}
          className="pl-9"
          data-testid="group-search"
        />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">
            {t.history.groups(filtered.length)}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {assetGroups.isPending ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t.history.loadingRuns}
            </div>
          ) : assetGroups.isError ? (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{assetGroups.error.message}</AlertDescription>
            </Alert>
          ) : filtered.length === 0 ? (
            <div className="py-8 text-center text-muted-foreground">
              <p className="font-medium">{t.history.noRuns}</p>
              <p className="mt-1 text-sm">{t.history.noRunsHint}</p>
            </div>
          ) : (
            filtered.map((group) => {
              const open = openGroupKey === group.key;
              return (
                <div
                  key={group.key}
                  className="overflow-hidden rounded-lg border"
                  data-testid={`asset-group-${group.groupId}`}
                >
                  <button
                    type="button"
                    className="flex w-full items-start gap-3 p-4 text-left hover:bg-muted/40"
                    onClick={() => setOpenGroupKey(open ? null : group.key)}
                    aria-expanded={open}
                    data-testid={`asset-group-toggle-${group.groupId}`}
                  >
                    {open ? (
                      <ChevronDown className="mt-1 h-4 w-4" />
                    ) : (
                      <ChevronRight className="mt-1 h-4 w-4" />
                    )}
                    {group.isUnbound ? (
                      <FolderOpen className="mt-0.5 h-5 w-5 text-amber-500" />
                    ) : (
                      <Film className="mt-0.5 h-5 w-5 text-primary" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="font-medium">{group.title}</h2>
                        {group.isUnbound && (
                          <Badge variant="secondary">{t.history.unbound}</Badge>
                        )}
                      </div>
                      <p className="truncate text-xs text-muted-foreground">
                        {group.inputPath ?? t.history.unbound}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <span>
                          {t.history.trials}: {group.summary.trialCount}
                        </span>
                        <span>
                          {t.history.fullRuns}: {group.summary.fullRunCount}
                        </span>
                        {group.summary.latestFullStatus && (
                          <span>
                            {t.history.latestFullStatus}:{" "}
                            {group.summary.latestFullStatus}
                          </span>
                        )}
                        <span>
                          {t.history.active}: {group.summary.activeCount}
                        </span>
                        <span>
                          {t.history.productCandidates}:{" "}
                          {group.summary.readyCandidateCount}
                        </span>
                        <span>
                          {t.history.failed}: {group.summary.failedCount}
                        </span>
                      </div>
                    </div>
                    <span className="hidden text-xs text-muted-foreground sm:block">
                      {formatDateTime(group.lastActivityAt)}
                    </span>
                  </button>
                  {open && (
                    <GroupDetail
                      group={group}
                      onCancel={(runId) => cancelRun.mutate(runId)}
                      onDelete={(runId) => deleteRun.mutate(runId)}
                    />
                  )}
                </div>
              );
            })
          )}
        </CardContent>
      </Card>
    </div>
  );
}
