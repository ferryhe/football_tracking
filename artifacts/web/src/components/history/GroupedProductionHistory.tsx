import { useEffect, useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import {
  getGetRunQueryKey,
  getGetHealthQueryKey,
  getListArtifactsQueryKey,
  getListAssetGroupsQueryKey,
  getListRunsQueryKey,
  type ArtifactSummary,
  type RunRecord,
} from "@workspace/api-client-react";
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
  productionCurrentConfigVerificationKey,
  productionHistoryCancellationTarget,
  productionHistoryDeletionBlocker,
  productionGroupProductCounts,
  productionProductVerificationKey,
  verifyProductionCurrentConfig,
  type ProductionHistoryFilter,
  type ProductionHistoryGroup,
  type ProductionHistoryTimelineItem,
} from "@/lib/productionHistory";
import { cn, formatBytes, formatDateTime, formatDuration } from "@/lib/utils";

export async function invalidateProductionHistoryCaches(
  queryClient: QueryClient,
  affectedRunIds: readonly string[] = [],
): Promise<void> {
  const runIds = [...new Set(affectedRunIds.filter(Boolean))];
  const generatedKeys = [
    getGetHealthQueryKey(),
    getListAssetGroupsQueryKey(),
    getListRunsQueryKey(),
    ...runIds.flatMap((runId) => [
      getGetRunQueryKey(runId),
      getListArtifactsQueryKey(runId),
    ]),
  ];
  const legacyAndHistoryKeys = [
    ["asset-groups"],
    ["health"],
    ["runs"],
    ...runIds.flatMap((runId) => [
      ["run", runId],
      ["artifacts", runId],
      ["artifact-json", runId],
      ["ai-improvement-status", runId],
      ["event-candidates", runId],
    ]),
    ["production-history", "artifact"],
    ["production-history", "product"],
  ];
  const isGeneratedRunQuery = (query: {
    queryKey: readonly unknown[];
  }): boolean => {
    const root = query.queryKey[0];
    return (
      typeof root === "string" &&
      (root === "/api/runs" || root.startsWith("/api/runs/"))
    );
  };

  await Promise.all([
    ...generatedKeys.map((queryKey) => queryClient.cancelQueries({ queryKey })),
    ...legacyAndHistoryKeys.map((queryKey) =>
      queryClient.cancelQueries({ queryKey }),
    ),
    queryClient.cancelQueries({ predicate: isGeneratedRunQuery }),
  ]);
  await Promise.all([
    ...generatedKeys.map((queryKey) =>
      queryClient.invalidateQueries({ queryKey }),
    ),
    ...legacyAndHistoryKeys.map((queryKey) =>
      queryClient.invalidateQueries({ queryKey }),
    ),
    queryClient.invalidateQueries({ predicate: isGeneratedRunQuery }),
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
      role="status"
      aria-live="polite"
      data-testid={`group-run-progress-${run.run_id}`}
    >
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{t.history.progressStage(run.progress.stage)}</span>
        <span className="font-medium tabular-nums text-foreground">
          {Math.round(percent)}%
        </span>
      </div>
      <Progress
        value={percent}
        aria-label={t.history.runProgressName(
          run.run_id,
          t.history.progressStage(run.progress.stage),
        )}
        aria-valuetext={`${Math.round(percent)}%`}
      />
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

function useProductCacheRevision(queryClient: QueryClient): number {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let mounted = true;
    let scheduled = false;
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      const key = event.query.queryKey;
      if (
        (event.type === "added" ||
          event.type === "removed" ||
          event.type === "updated") &&
        key[0] === "production-history" &&
        key[1] === "product" &&
        !scheduled
      ) {
        scheduled = true;
        queueMicrotask(() => {
          scheduled = false;
          if (mounted) setRevision((current) => current + 1);
        });
      }
    });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [queryClient]);
  return revision;
}

interface TargetActionRegistry {
  targets: Set<string>;
  listeners: Set<() => void>;
}

const targetActionRegistries = new WeakMap<QueryClient, TargetActionRegistry>();

function targetActionRegistry(queryClient: QueryClient): TargetActionRegistry {
  const existing = targetActionRegistries.get(queryClient);
  if (existing) return existing;
  const created = {
    targets: new Set<string>(),
    listeners: new Set<() => void>(),
  };
  targetActionRegistries.set(queryClient, created);
  return created;
}

function notifyTargetActionRegistry(registry: TargetActionRegistry): void {
  for (const listener of registry.listeners) listener();
}

function usePendingTargets(queryClient: QueryClient): ReadonlySet<string> {
  const [pending, setPending] = useState<ReadonlySet<string>>(
    () => new Set(targetActionRegistry(queryClient).targets),
  );
  useEffect(() => {
    const registry = targetActionRegistry(queryClient);
    const update = () => setPending(new Set(registry.targets));
    registry.listeners.add(update);
    update();
    return () => {
      registry.listeners.delete(update);
    };
  }, [queryClient]);
  return pending;
}

async function runQueryClientTargetAction(
  queryClient: QueryClient,
  target: string,
  action: () => Promise<unknown>,
): Promise<boolean> {
  const registry = targetActionRegistry(queryClient);
  if (registry.targets.has(target)) return false;
  registry.targets.add(target);
  notifyTargetActionRegistry(registry);
  try {
    await action();
  } catch {
    // useMutation owns the user-facing error toast.
  } finally {
    registry.targets.delete(target);
    notifyTargetActionRegistry(registry);
  }
  return true;
}

function groupProductCountsFromCache(
  queryClient: QueryClient,
  group: ProductionHistoryGroup,
) {
  return productionGroupProductCounts(group, (key) => {
    const state = queryClient.getQueryState<ArtifactSummary[]>(key);
    return state ? { status: state.status, artifacts: state.data } : undefined;
  });
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

function CurrentConfigEvidence({
  item,
}: {
  item: ProductionHistoryTimelineItem;
}) {
  const { t } = useLanguage();
  const note = item.note!;
  const verificationKey = productionCurrentConfigVerificationKey(
    note,
    item.run,
  );
  const verification = useQuery({
    queryKey: verificationKey ?? [
      "production-history",
      "config",
      item.run.run_id,
      "missing-summary",
    ],
    queryFn: async () =>
      verifyProductionCurrentConfig(
        note,
        item.run,
        await api.getConfig(note.configName!),
      ),
    enabled: verificationKey !== null,
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  const status = verification.isPending
    ? ({ status: "not_reverified", reason: "summary_only" } as const)
    : verification.isError
      ? /^404\b/.test(verification.error.message)
        ? ({ status: "missing" } as const)
        : ({
            status: "error",
            message: verification.error.message,
          } as const)
      : verification.data;
  const label =
    status?.status === "verified_current"
      ? t.history.currentConfigVerified
      : status?.status === "modified"
        ? t.history.currentConfigModified
        : status?.status === "lineage_mismatch"
          ? t.history.currentConfigLineageMismatch
          : status?.status === "missing"
            ? t.history.currentConfigMissing
            : status?.status === "error"
              ? t.history.currentConfigError
              : t.history.currentConfigVerifying;

  return (
    <section
      className="space-y-2 rounded-lg border p-3 text-sm"
      role={status?.status === "error" ? "alert" : "status"}
      aria-live={status?.status === "error" ? "assertive" : "polite"}
      data-testid={`current-config-status-${item.run.run_id}`}
    >
      <h4 className="font-medium">{t.history.currentSavedConfig}</h4>
      <p>{label}</p>
      <p className="text-xs text-muted-foreground">
        {t.history.historicalConfigIdentity}: {note.configIdentity}
      </p>
      {status?.status === "error" && (
        <p className="text-xs text-destructive">{status.message}</p>
      )}
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
  pendingTargets,
}: {
  item: ProductionHistoryTimelineItem;
  groupRuns: readonly RunRecord[];
  onCancel: (runId: string) => Promise<boolean>;
  onDelete: (runId: string) => Promise<boolean>;
  pendingTargets: ReadonlySet<string>;
}) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const cancellationTarget = productionHistoryCancellationTarget(
    item.run,
    groupRuns,
  );
  const cancellationPending =
    cancellationTarget !== null && pendingTargets.has(cancellationTarget);
  const deletionPending = pendingTargets.has(item.run.run_id);
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
                {t.history.historicalConfigIdentity}:{" "}
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
                  : item.lineageIssue === "identity_mismatch"
                    ? t.history.identityMismatchLineage
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

          {item.note?.purpose === "production_full" && (
            <CurrentConfigEvidence item={item} />
          )}

          <div className="flex flex-wrap items-start gap-2">
            {cancellationTarget && (
              <AlertDialog
                open={cancelDialogOpen}
                onOpenChange={(nextOpen) => {
                  if (!cancellationPending) setCancelDialogOpen(nextOpen);
                }}
              >
                <AlertDialogTrigger asChild>
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={cancellationPending}
                    data-testid={`group-cancel-${item.run.run_id}`}
                  >
                    {cancellationPending ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Square className="mr-1.5 h-3.5 w-3.5" />
                    )}
                    {cancellationPending
                      ? t.history.actionPending
                      : t.history.cancelRun}
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
                    <AlertDialogCancel disabled={cancellationPending}>
                      {t.common.cancel}
                    </AlertDialogCancel>
                    <AlertDialogAction
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      disabled={cancellationPending}
                      onClick={async (event) => {
                        event.preventDefault();
                        if (cancellationPending) return;
                        if (await onCancel(cancellationTarget)) {
                          setCancelDialogOpen(false);
                        }
                      }}
                      data-testid={`group-confirm-cancel-${item.run.run_id}`}
                    >
                      {cancellationPending && (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                      )}
                      {cancellationPending
                        ? t.history.actionPending
                        : t.history.cancelRunConfirm}
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}

            <AlertDialog
              open={deleteDialogOpen}
              onOpenChange={(nextOpen) => {
                if (!deletionPending) setDeleteDialogOpen(nextOpen);
              }}
            >
              <AlertDialogTrigger asChild>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={deletionBlocker !== null || deletionPending}
                  data-testid={`group-delete-${item.run.run_id}`}
                >
                  {deletionPending ? (
                    <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                  )}
                  {deletionPending
                    ? t.history.actionPending
                    : t.history.deleteOutput}
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
                  <AlertDialogCancel disabled={deletionPending}>
                    {t.common.cancel}
                  </AlertDialogCancel>
                  <AlertDialogAction
                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    disabled={deletionPending}
                    onClick={async (event) => {
                      event.preventDefault();
                      if (deletionPending) return;
                      if (await onDelete(item.run.run_id)) {
                        setDeleteDialogOpen(false);
                      }
                    }}
                    data-testid={`group-confirm-delete-${item.run.run_id}`}
                  >
                    {deletionPending && (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    )}
                    {deletionPending
                      ? t.history.actionPending
                      : t.history.deleteRunConfirm}
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
  pendingTargets,
}: {
  group: ProductionHistoryGroup;
  onCancel: (runId: string) => Promise<boolean>;
  onDelete: (runId: string) => Promise<boolean>;
  pendingTargets: ReadonlySet<string>;
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
      {group.inputVideo && (
        <section
          className="grid gap-2 rounded-lg border bg-muted/10 p-3 text-xs sm:grid-cols-2"
          data-testid={`group-source-metadata-${group.groupId}`}
        >
          <h4 className="font-medium sm:col-span-2">
            {t.history.originalMetadata}
          </h4>
          <p>
            <span className="text-muted-foreground">
              {t.history.fileName}:{" "}
            </span>
            {group.inputVideo.name}
          </p>
          <p>
            <span className="text-muted-foreground">
              {t.history.fileSize}:{" "}
            </span>
            {formatBytes(group.inputVideo.size_bytes)}
          </p>
          <p className="break-all sm:col-span-2">
            <span className="text-muted-foreground">
              {t.history.inputLabel}:{" "}
            </span>
            {group.inputVideo.path}
          </p>
          <p>
            <span className="text-muted-foreground">
              {t.history.modifiedAt}:{" "}
            </span>
            {formatDateTime(group.inputVideo.modified_at)}
          </p>
        </section>
      )}
      {group.configs.length > 0 && (
        <section
          className="space-y-2 rounded-lg border bg-muted/10 p-3"
          data-testid={`group-config-snapshots-${group.groupId}`}
        >
          <h4 className="text-sm font-medium">{t.history.configSnapshots}</h4>
          <p className="text-xs text-muted-foreground">
            {t.history.configSnapshotsSummaryOnly}
          </p>
          {group.configs.map((config) => (
            <div
              key={`${config.path}:${config.name}`}
              className="grid gap-1 border-t pt-2 text-xs first:border-t-0 first:pt-0 sm:grid-cols-2"
            >
              <p>
                <span className="text-muted-foreground">
                  {t.history.configIdentity}:{" "}
                </span>
                {config.name}
              </p>
              <p className="break-all">
                <span className="text-muted-foreground">
                  {t.history.configPath}:{" "}
                </span>
                {config.path}
              </p>
              <p className="break-all">
                <span className="text-muted-foreground">
                  {t.history.inputLabel}:{" "}
                </span>
                {config.input_video ?? t.common.notAvailable}
              </p>
              <p>
                <span className="text-muted-foreground">
                  {t.history.createdLabel}:{" "}
                </span>
                {formatDateTime(config.created_at)}
              </p>
            </div>
          ))}
        </section>
      )}
      {group.timeline.map((item) => (
        <TimelineRow
          key={item.run.run_id}
          item={item}
          groupRuns={runs}
          onCancel={onCancel}
          onDelete={onDelete}
          pendingTargets={pendingTargets}
        />
      ))}
    </div>
  );
}

export function GroupedProductionHistory() {
  const { t } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const productCacheRevision = useProductCacheRevision(queryClient);
  const [filter, setFilter] = useState<ProductionHistoryFilter>("all");
  const [search, setSearch] = useState("");
  const [openGroupKey, setOpenGroupKey] = useState<string | null>(null);
  const pendingTargets = usePendingTargets(queryClient);

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
  const productCountsByGroup = useMemo(
    () =>
      new Map(
        groups.map((group) => [
          group.key,
          groupProductCountsFromCache(queryClient, group),
        ]),
      ),
    [groups, productCacheRevision, queryClient],
  );

  const cancelRun = useMutation({
    mutationFn: (runId: string) => api.cancelRun(runId),
    onSuccess: async (cancelled, requestedId) => {
      await invalidateProductionHistoryCaches(queryClient, [
        requestedId,
        cancelled.run_id,
      ]);
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
      await invalidateProductionHistoryCaches(queryClient, [runId]);
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
              const productCounts = productCountsByGroup.get(group.key) ?? {
                unverified: 0,
                verified: 0,
                unavailable: 0,
              };
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
                        <span
                          data-testid={`group-products-ready-${group.groupId}`}
                        >
                          {t.history.productCandidates}:{" "}
                          {group.summary.readyCandidateCount}
                        </span>
                        <span
                          data-testid={`group-products-unverified-${group.groupId}`}
                        >
                          {t.history.unverifiedProducts}:{" "}
                          {productCounts.unverified}
                        </span>
                        <span
                          data-testid={`group-products-verified-${group.groupId}`}
                        >
                          {t.history.verifiedProducts}: {productCounts.verified}
                        </span>
                        <span
                          data-testid={`group-products-unavailable-${group.groupId}`}
                        >
                          {t.history.unavailableProducts}:{" "}
                          {productCounts.unavailable}
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
                      onCancel={(runId) =>
                        runQueryClientTargetAction(queryClient, runId, () =>
                          cancelRun.mutateAsync(runId),
                        )
                      }
                      onDelete={(runId) =>
                        runQueryClientTargetAction(queryClient, runId, () =>
                          deleteRun.mutateAsync(runId),
                        )
                      }
                      pendingTargets={pendingTargets}
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
