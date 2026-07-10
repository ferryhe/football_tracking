import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetArtifactUrl,
  getGetBroadcastReviewWindowsQueryKey,
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  getListRunsQueryKey,
  useCancelRun,
  useCreateRun,
  useGetBroadcastReviewWindows,
  useGetRun,
  useListArtifacts,
  useListRuns,
  useRecomputeBroadcastTrajectory,
  useRenderBroadcastHybrid,
  useSubmitBroadcastReviewActions,
  type ArtifactSummary,
  type BroadcastRenderRequest,
} from "@workspace/api-client-react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RadioTower,
  RefreshCw,
} from "lucide-react";
import { useLocation, useSearch } from "wouter";

import {
  BroadcastRenderStep,
  BROADCAST_DELIVERY_ARTIFACTS,
  type BroadcastDeliveryArtifact,
} from "@/components/broadcast/BroadcastRenderStep";
import {
  BroadcastReviewStep,
  type BroadcastReviewDecision,
} from "@/components/broadcast/BroadcastReviewStep";
import {
  BroadcastSetupStep,
  type BroadcastSetupInput,
} from "@/components/broadcast/BroadcastSetupStep";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLanguage } from "@/contexts/LanguageContext";
import {
  broadcastArtifactQueryIdentity,
  broadcastCancellationTarget,
  broadcastRecomputeRecoveryMode,
  buildBroadcastCreateRequest,
  localizeBroadcastWorkflowMessage,
  mergeBroadcastArtifacts,
  recoverBroadcastWorkflowRun,
  resolveBroadcastMontageArtifact,
  validateAndBuildBroadcastReviewActions,
  type BroadcastWorkflowStateName,
} from "@/lib/broadcastWorkflow";

const ACTIVE_STATES = new Set<BroadcastWorkflowStateName>([
  "tracking",
  "recomputing",
  "rendering",
]);

function runIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("run")?.trim();
  return value || null;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

function statusCode(error: unknown): number | null {
  if (!error || typeof error !== "object") return null;
  const value = (error as { status?: unknown }).status;
  return typeof value === "number" ? value : null;
}

function exactArtifact(
  artifacts: readonly ArtifactSummary[],
  name: string,
): ArtifactSummary | null {
  const matches = artifacts.filter(
    (artifact) =>
      artifact.exists &&
      artifact.name === name &&
      Number.isInteger(artifact.size_bytes) &&
      Number(artifact.size_bytes) >= 0,
  );
  return matches.length === 1 ? matches[0] : null;
}

async function sha256Artifact(
  runId: string,
  artifact: ArtifactSummary,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(getGetArtifactUrl(runId, artifact.name), {
    cache: "no-store",
    headers: { Accept: "application/octet-stream, application/json" },
    signal,
  });
  if (!response.ok)
    throw new Error(`HTTP ${response.status} while reading ${artifact.name}`);
  const bytes = await response.arrayBuffer();
  if (artifact.size_bytes != null && bytes.byteLength !== artifact.size_bytes) {
    throw new Error(
      `${artifact.name} changed while its review state was being recovered.`,
    );
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function stepIndex(state: BroadcastWorkflowStateName): number {
  if (state === "setup" || state === "tracking") return 0;
  if (state === "needs_review" || state === "recomputing") return 1;
  return 2;
}

export default function BroadcastPage() {
  const { language, t } = useLanguage();
  const [, navigate] = useLocation();
  const search = useSearch();
  const queryClient = useQueryClient();
  const runId = useMemo(() => runIdFromSearch(search), [search]);
  const [pollActive, setPollActive] = useState(Boolean(runId));
  const [pageError, setPageError] = useState<string | null>(null);
  const [reviewerId, setReviewerId] = useState("operator");
  const [recoveryNonce, setRecoveryNonce] = useState(0);
  const [recoveryAttemptState, setRecoveryAttemptState] = useState<
    "idle" | "pending" | "failed"
  >("idle");
  const [reviewDecisions, setReviewDecisions] = useState<
    BroadcastReviewDecision[]
  >([]);
  const recoveredArtifactRef = useRef<string | null>(null);
  const recoveryAttemptKeyRef = useRef<string | null>(null);

  const requestedRunQuery = useGetRun(runId ?? "", {
    query: {
      queryKey: getGetRunQueryKey(runId ?? ""),
      enabled: Boolean(runId),
      refetchInterval: pollActive ? 2_000 : false,
    },
  });
  const runsQuery = useListRuns({
    query: {
      queryKey: getListRunsQueryKey(),
      enabled: Boolean(runId),
      refetchInterval: pollActive ? 2_000 : false,
    },
  });

  const recovery = useMemo(
    () =>
      recoverBroadcastWorkflowRun(
        requestedRunQuery.data ?? runId,
        runsQuery.data ?? [],
      ),
    [requestedRunQuery.data, runId, runsQuery.data],
  );
  const parentRun = recovery.parentRun;
  const parentRunId = parentRun?.run_id ?? "";
  const parentRunIdRef = useRef(parentRunId);
  parentRunIdRef.current = parentRunId;
  const artifactQueryIdentity = useMemo(
    () =>
      broadcastArtifactQueryIdentity(
        recovery.state,
        parentRun?.broadcast?.status_generation,
      ),
    [parentRun?.broadcast?.status_generation, recovery.state],
  );

  const artifactsQuery = useListArtifacts(parentRunId, {
    query: {
      queryKey: [
        ...getListArtifactsQueryKey(parentRunId),
        artifactQueryIdentity.scope,
      ],
      enabled: Boolean(parentRunId),
      refetchInterval: pollActive ? 2_000 : false,
    },
  });
  const artifacts = useMemo(
    () => mergeBroadcastArtifacts(parentRun?.artifacts, artifactsQuery.data),
    [artifactsQuery.data, parentRun?.artifacts],
  );
  const reviewDecisionsArtifact = useMemo(
    () => exactArtifact(artifacts, "review_decisions.json"),
    [artifacts],
  );
  const recomputeRecoveryMode = broadcastRecomputeRecoveryMode(
    parentRun,
    reviewDecisionsArtifact !== null,
  );
  const reviewEnabled = Boolean(
    parentRunId && recovery.state === "needs_review",
  );
  const reviewQuery = useGetBroadcastReviewWindows(parentRunId, {
    query: {
      queryKey: getGetBroadcastReviewWindowsQueryKey(parentRunId),
      enabled: reviewEnabled,
      retry: false,
    },
  });

  const createRun = useCreateRun();
  const submitReview = useSubmitBroadcastReviewActions();
  const recompute = useRecomputeBroadcastTrajectory();
  const render = useRenderBroadcastHybrid();
  const cancel = useCancelRun();

  useEffect(() => {
    setPollActive(
      Boolean(runId) &&
        (ACTIVE_STATES.has(recovery.state) || recovery.pollRunIds.length > 0),
    );
  }, [recovery.pollRunIds.length, recovery.state, runId]);

  useEffect(() => {
    if (
      !parentRunId ||
      ACTIVE_STATES.has(recovery.state) ||
      recovery.state === "setup"
    ) {
      return;
    }
    void queryClient.invalidateQueries({
      queryKey: getListArtifactsQueryKey(parentRunId),
    });
  }, [parentRunId, queryClient, recovery.state]);

  useEffect(() => {
    setReviewDecisions([]);
    recoveredArtifactRef.current = null;
    recoveryAttemptKeyRef.current = null;
    setRecoveryAttemptState("idle");
  }, [parentRunId]);

  useEffect(() => {
    setReviewDecisions([]);
  }, [reviewQuery.data?.queue_sha256]);

  async function refreshWorkflow(extraRunId?: string) {
    const invalidations: Promise<unknown>[] = [
      queryClient.invalidateQueries({ queryKey: getListRunsQueryKey() }),
      queryClient.invalidateQueries({ queryKey: ["runs"] }),
      queryClient.invalidateQueries({ queryKey: ["health"] }),
    ];
    if (parentRunId) {
      invalidations.push(
        queryClient.invalidateQueries({
          queryKey: getGetRunQueryKey(parentRunId),
        }),
        queryClient.invalidateQueries({
          queryKey: getListArtifactsQueryKey(parentRunId),
        }),
        queryClient.invalidateQueries({
          queryKey: getGetBroadcastReviewWindowsQueryKey(parentRunId),
        }),
      );
    }
    if (runId && runId !== parentRunId) {
      invalidations.push(
        queryClient.invalidateQueries({ queryKey: getGetRunQueryKey(runId) }),
      );
    }
    if (extraRunId) {
      invalidations.push(
        queryClient.invalidateQueries({
          queryKey: getGetRunQueryKey(extraRunId),
        }),
      );
    }
    await Promise.all(invalidations);
  }

  async function handleSetup(input: BroadcastSetupInput) {
    setPageError(null);
    const request = buildBroadcastCreateRequest(input);
    if (!request.ok) {
      setPageError(
        request.messages
          .map((message) => localizeBroadcastWorkflowMessage(message, language))
          .join(" "),
      );
      return;
    }
    try {
      const created = await createRun.mutateAsync({ data: request.value });
      setPollActive(true);
      navigate(`/broadcast?run=${encodeURIComponent(created.run_id)}`);
    } catch (error) {
      setPageError(errorMessage(error, t.broadcast.startFailed));
    }
  }

  async function queueRecompute(reviewDecisionsSha256: string) {
    if (!parentRunId) return;
    const queued = await recompute.mutateAsync({
      runId: parentRunId,
      data: { review_decisions_sha256: reviewDecisionsSha256 },
    });
    setPollActive(true);
    await refreshWorkflow(queued.run_id);
  }

  async function handleRetryRecompute() {
    if (
      !parentRunId ||
      !reviewDecisionsArtifact ||
      (recomputeRecoveryMode !== "retry" &&
        recoveryAttemptState !== "failed") ||
      recoveryAttemptKeyRef.current !== null
    ) {
      return;
    }
    const retryRunId = parentRunId;
    const retryKey = `retry:${retryRunId}:${reviewDecisionsArtifact.size_bytes}`;
    recoveredArtifactRef.current = retryKey;
    recoveryAttemptKeyRef.current = retryKey;
    setRecoveryAttemptState("pending");
    setPageError(null);
    try {
      const digest = await sha256Artifact(retryRunId, reviewDecisionsArtifact);
      if (
        parentRunIdRef.current !== retryRunId ||
        recoveredArtifactRef.current !== retryKey
      ) {
        return;
      }
      await queueRecompute(digest);
      if (recoveryAttemptKeyRef.current === retryKey) {
        setRecoveryAttemptState("idle");
      }
    } catch (error) {
      if (parentRunIdRef.current !== retryRunId) return;
      if (recoveryAttemptKeyRef.current === retryKey) {
        setRecoveryAttemptState("failed");
      }
      if (statusCode(error) === 409) {
        recoveredArtifactRef.current = `blocked:${retryRunId}`;
        setPageError(t.broadcast.staleEvidence);
        await refreshWorkflow();
      } else {
        setPageError(errorMessage(error, t.broadcast.recomputeFailed));
      }
    } finally {
      if (recoveryAttemptKeyRef.current === retryKey) {
        recoveryAttemptKeyRef.current = null;
      }
    }
  }

  async function handleReviewSubmit(decisions: BroadcastReviewDecision[]) {
    if (!parentRunId || !reviewQuery.data) return;
    setPageError(null);
    const request = validateAndBuildBroadcastReviewActions(
      reviewQuery.data,
      decisions,
      reviewerId,
      new Date().toISOString(),
    );
    if (!request.ok) {
      setPageError(
        request.messages
          .map((message) => localizeBroadcastWorkflowMessage(message, language))
          .join(" "),
      );
      return;
    }
    let digest: string;
    try {
      const submitted = await submitReview.mutateAsync({
        runId: parentRunId,
        data: request.value,
      });
      const submittedDigest =
        submitted.details?.review_decisions_sha256?.trim();
      if (!submittedDigest || !/^[0-9a-f]{64}$/.test(submittedDigest)) {
        throw new Error(
          "The server did not return the evidence-bound review decision hash.",
        );
      }
      digest = submittedDigest;
    } catch (error) {
      if (statusCode(error) === 409) {
        recoveredArtifactRef.current = `blocked:${parentRunId}`;
        setPageError(t.broadcast.staleEvidence);
        await refreshWorkflow();
      } else {
        setPageError(errorMessage(error, t.broadcast.submitFailed));
      }
      return;
    }
    try {
      recoveredArtifactRef.current = `submitted:${digest}`;
      await queueRecompute(digest);
    } catch (error) {
      recoveryAttemptKeyRef.current = null;
      setRecoveryAttemptState("failed");
      await refreshWorkflow().catch(() => undefined);
      if (statusCode(error) === 409) {
        recoveredArtifactRef.current = `blocked:${parentRunId}`;
        setPageError(t.broadcast.staleEvidence);
      } else {
        setPageError(errorMessage(error, t.broadcast.recomputeFailed));
      }
    }
  }

  useEffect(() => {
    if (
      recomputeRecoveryMode !== "auto" ||
      recovery.state !== "needs_review" ||
      !parentRunId ||
      !reviewDecisionsArtifact ||
      submitReview.isPending ||
      recompute.isPending ||
      recoveryAttemptKeyRef.current !== null
    ) {
      return;
    }
    if (
      recoveredArtifactRef.current === `blocked:${parentRunId}` ||
      recoveredArtifactRef.current?.startsWith("submitted:")
    ) {
      return;
    }
    const recoveryKey = `auto:${parentRunId}:${reviewDecisionsArtifact.size_bytes}`;
    if (recoveredArtifactRef.current === recoveryKey) return;
    recoveredArtifactRef.current = recoveryKey;
    recoveryAttemptKeyRef.current = recoveryKey;
    setRecoveryAttemptState("pending");
    const controller = new AbortController();
    let queueStarted = false;
    void (async () => {
      try {
        const digest = await sha256Artifact(
          parentRunId,
          reviewDecisionsArtifact,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          parentRunIdRef.current !== parentRunId ||
          recoveredArtifactRef.current !== recoveryKey
        ) {
          return;
        }
        queueStarted = true;
        await queueRecompute(digest);
        if (recoveryAttemptKeyRef.current === recoveryKey) {
          setRecoveryAttemptState("idle");
        }
      } catch (error) {
        if (
          (!queueStarted && controller.signal.aborted) ||
          parentRunIdRef.current !== parentRunId ||
          recoveredArtifactRef.current !== recoveryKey
        ) {
          return;
        }
        if (recoveryAttemptKeyRef.current === recoveryKey) {
          setRecoveryAttemptState("failed");
        }
        if (statusCode(error) === 409) {
          recoveredArtifactRef.current = `blocked:${parentRunId}`;
          setPageError(t.broadcast.staleEvidence);
          await refreshWorkflow();
        } else {
          setPageError(errorMessage(error, t.broadcast.recomputeFailed));
        }
      } finally {
        if (recoveryAttemptKeyRef.current === recoveryKey) {
          recoveryAttemptKeyRef.current = null;
        }
      }
    })();
    return () => {
      controller.abort();
      if (!queueStarted && recoveryAttemptKeyRef.current === recoveryKey) {
        recoveryAttemptKeyRef.current = null;
        if (recoveredArtifactRef.current === recoveryKey) {
          recoveredArtifactRef.current = null;
        }
        setRecoveryAttemptState("idle");
      }
    };
  }, [
    parentRunId,
    recoveryNonce,
    recovery.state,
    recomputeRecoveryMode,
    recompute.isPending,
    reviewDecisionsArtifact?.exists,
    reviewDecisionsArtifact?.name,
    reviewDecisionsArtifact?.size_bytes,
    submitReview.isPending,
  ]);

  async function handleRender(request: BroadcastRenderRequest) {
    if (!parentRunId) return;
    setPageError(null);
    try {
      const queued = await render.mutateAsync({
        runId: parentRunId,
        data: request,
      });
      setPollActive(true);
      await refreshWorkflow(queued.run_id);
    } catch (error) {
      if (statusCode(error) === 409) {
        setPageError(t.broadcast.staleEvidence);
        await refreshWorkflow();
      } else {
        setPageError(errorMessage(error, t.broadcast.renderFailed));
      }
    }
  }

  async function handleCancel() {
    const targetId = broadcastCancellationTarget(recovery);
    if (!targetId) return;
    if (recovery.state === "recomputing" && parentRunId) {
      recoveredArtifactRef.current = `blocked:${parentRunId}`;
    }
    setPageError(null);
    try {
      await cancel.mutateAsync({ runId: targetId });
      await refreshWorkflow(targetId);
    } catch (error) {
      setPageError(errorMessage(error, t.broadcast.cancelFailed));
    }
  }

  const montageResolution = useMemo(() => {
    const urls: Record<string, string> = {};
    const messages: string[] = [];
    for (const item of reviewQuery.data?.items ?? []) {
      for (const candidate of item.candidates ?? []) {
        const resolved = resolveBroadcastMontageArtifact(artifacts, candidate);
        if (resolved.ok) {
          urls[candidate.candidate_id] = getGetArtifactUrl(
            parentRunId,
            resolved.value.name,
          );
        } else {
          messages.push(
            ...resolved.messages.map((message) =>
              localizeBroadcastWorkflowMessage(message, language),
            ),
          );
        }
      }
    }
    return { urls, messages };
  }, [artifacts, language, parentRunId, reviewQuery.data]);

  const localizedReviewResponse = useMemo(() => {
    if (!reviewQuery.data) return null;
    return {
      ...reviewQuery.data,
      reason: reviewQuery.data.reason
        ? localizeBroadcastWorkflowMessage(reviewQuery.data.reason, language)
        : reviewQuery.data.reason,
    };
  }, [language, reviewQuery.data]);

  const deliveryUrls = useMemo(() => {
    const result: Partial<Record<BroadcastDeliveryArtifact, string>> = {};
    if (!artifactQueryIdentity.deliveryReady || !artifactsQuery.isSuccess) {
      return result;
    }
    for (const name of BROADCAST_DELIVERY_ARTIFACTS) {
      const artifact = exactArtifact(artifactsQuery.data ?? [], name);
      if (artifact)
        result[name] = getGetArtifactUrl(parentRunId, artifact.name);
    }
    return result;
  }, [
    artifactQueryIdentity.deliveryReady,
    artifactsQuery.data,
    artifactsQuery.isSuccess,
    parentRunId,
  ]);

  const activeStep = stepIndex(recovery.state);
  const steps = [
    t.broadcast.setupStep,
    t.broadcast.reviewStep,
    t.broadcast.renderStep,
  ];
  const loading = Boolean(
    runId && !parentRun && (requestedRunQuery.isLoading || runsQuery.isLoading),
  );
  const queryError =
    requestedRunQuery.error ?? runsQuery.error ?? artifactsQuery.error;
  const combinedError =
    pageError ??
    (queryError ? errorMessage(queryError, t.broadcast.loadFailed) : null);
  const workflowMessages = Array.from(
    new Set(
      [
        ...recovery.messages.map((message) =>
          localizeBroadcastWorkflowMessage(message, language),
        ),
        ...(parentRun?.broadcast?.blocking_reasons ?? []).map((message) =>
          localizeBroadcastWorkflowMessage(message, language),
        ),
      ].filter(Boolean),
    ),
  );

  return (
    <div
      className="mx-auto max-w-6xl space-y-6"
      data-testid="broadcast-workflow-page"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <RadioTower className="h-6 w-6 text-primary" />
            <h1 className="text-2xl font-bold tracking-tight">
              {t.broadcast.title}
            </h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {t.broadcast.subtitle}
          </p>
        </div>
        {parentRun && (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline">{parentRun.run_id}</Badge>
            <Badge>{parentRun.broadcast?.status ?? parentRun.status}</Badge>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={recoveryAttemptState === "pending"}
              onClick={() => {
                recoveredArtifactRef.current = null;
                setRecoveryAttemptState("idle");
                setRecoveryNonce((value) => value + 1);
                void refreshWorkflow();
              }}
            >
              <RefreshCw className="mr-1.5 h-4 w-4" />
              {t.broadcast.refresh}
            </Button>
          </div>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        {steps.map((step, index) => (
          <div
            key={step}
            className={`rounded-lg border p-3 text-sm ${
              index === activeStep
                ? "border-primary bg-primary/5 font-medium"
                : index < activeStep
                  ? "border-emerald-300 bg-emerald-50 text-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-200"
                  : "text-muted-foreground"
            }`}
          >
            <span className="flex items-center gap-2">
              {index < activeStep ? <CheckCircle2 className="h-4 w-4" /> : null}
              {step}
            </span>
          </div>
        ))}
      </div>

      {combinedError && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{t.broadcast.loadFailed}</AlertTitle>
          <AlertDescription>{combinedError}</AlertDescription>
        </Alert>
      )}

      {workflowMessages.length > 0 && (
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{t.broadcast.blockingReasons}</AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-5">
              {workflowMessages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      {loading && (
        <Card>
          <CardContent className="flex items-center gap-3 py-10 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            {t.broadcast.loading}
          </CardContent>
        </Card>
      )}

      {!loading && recovery.state === "setup" && (
        <BroadcastSetupStep
          labels={t.broadcast.setup}
          onSubmit={(input) => void handleSetup(input)}
          isSubmitting={createRun.isPending}
          error={pageError}
        />
      )}

      {!loading && recovery.state === "tracking" && parentRun && (
        <Card>
          <CardHeader>
            <CardTitle>{t.broadcast.setupStep}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              {t.broadcast.trackingProgress}
            </div>
            <Button
              variant="outline"
              onClick={() => void handleCancel()}
              disabled={cancel.isPending}
            >
              {t.broadcast.cancel}
            </Button>
          </CardContent>
        </Card>
      )}

      {!loading && recovery.state === "needs_review" && parentRun && (
        <div className="space-y-4">
          {recomputeRecoveryMode === "auto" &&
            recoveryAttemptState !== "failed" && (
              <Card>
                <CardContent className="flex items-center gap-3 py-6 text-sm text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  {t.broadcast.recoveringDecisionHash}
                </CardContent>
              </Card>
            )}
          {(recomputeRecoveryMode === "retry" ||
            recoveryAttemptState === "failed") && (
            <Alert>
              <RefreshCw className="h-4 w-4" />
              <AlertTitle>{t.broadcast.retryRecompute}</AlertTitle>
              <AlertDescription className="space-y-3">
                <p>{t.broadcast.retryRecomputeDescription}</p>
                <Button
                  type="button"
                  onClick={() => void handleRetryRecompute()}
                  disabled={
                    recompute.isPending || recoveryAttemptState === "pending"
                  }
                >
                  {recompute.isPending || recoveryAttemptState === "pending"
                    ? t.broadcast.retryingRecompute
                    : t.broadcast.retryRecompute}
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {recomputeRecoveryMode === "none" && reviewQuery.isLoading && (
            <Card>
              <CardContent className="flex items-center gap-3 py-10">
                <Loader2 className="h-5 w-5 animate-spin" />
                {t.broadcast.loading}
              </CardContent>
            </Card>
          )}
          {recomputeRecoveryMode === "none" && reviewQuery.isError && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
              <AlertDescription>{t.broadcast.evidenceBlocked}</AlertDescription>
            </Alert>
          )}
          {recomputeRecoveryMode === "none" && localizedReviewResponse && (
            <>
              {montageResolution.messages.length > 0 && (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" />
                  <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
                  <AlertDescription>
                    {montageResolution.messages.join(" ")}
                  </AlertDescription>
                </Alert>
              )}
              <div className="max-w-sm space-y-2">
                <Label htmlFor="broadcast-reviewer-id">
                  {t.broadcast.reviewerId}
                </Label>
                <Input
                  id="broadcast-reviewer-id"
                  value={reviewerId}
                  onChange={(event) => setReviewerId(event.target.value)}
                  maxLength={200}
                  disabled={submitReview.isPending || recompute.isPending}
                />
              </div>
              <BroadcastReviewStep
                response={localizedReviewResponse}
                montageUrlsByCandidateId={montageResolution.urls}
                decisions={reviewDecisions}
                onDecisionsChange={setReviewDecisions}
                onSubmit={(decisions) => void handleReviewSubmit(decisions)}
                isSubmitting={submitReview.isPending || recompute.isPending}
                disabled={
                  montageResolution.messages.length > 0 ||
                  ((localizedReviewResponse.items ?? []).some(
                    (item) => (item.candidates ?? []).length > 0,
                  ) &&
                    !reviewerId.trim())
                }
                error={pageError}
                labels={t.broadcast.review}
                noiseSubtypeLabels={t.broadcast.noiseSubtypes}
              />
            </>
          )}
        </div>
      )}

      {!loading && recovery.state === "recomputing" && parentRun && (
        <Card>
          <CardHeader>
            <CardTitle>{t.broadcast.reviewStep}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              {t.broadcast.reviewSubmitted}
            </div>
            <Button
              variant="outline"
              onClick={() => void handleCancel()}
              disabled={cancel.isPending}
            >
              {t.broadcast.cancel}
            </Button>
          </CardContent>
        </Card>
      )}

      {!loading &&
        (recovery.state === "trajectory_ready" ||
          recovery.state === "rendering" ||
          recovery.state === "ready") &&
        parentRun && (
          <BroadcastRenderStep
            run={parentRun}
            operationRun={recovery.operationRun}
            trajectoryGenerationId={
              parentRun.broadcast?.trajectory_generation_id
            }
            artifactUrls={deliveryUrls}
            onRender={(request) => void handleRender(request)}
            isRendering={render.isPending || recovery.state === "rendering"}
            disabled={recovery.state === "ready"}
            error={pageError}
            onCancel={
              recovery.state === "rendering"
                ? () => void handleCancel()
                : undefined
            }
            labels={t.broadcast.render}
            artifactLabels={t.broadcast.artifacts}
          />
        )}

      {!loading &&
        (recovery.state === "failed" || recovery.state === "cancelled") && (
          <Card>
            <CardHeader>
              <CardTitle>
                {recovery.state === "failed"
                  ? t.broadcast.workflowFailed
                  : t.broadcast.workflowCancelled}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                {recovery.messages.join(" ") || parentRun?.error}
              </p>
              <Button onClick={() => navigate("/broadcast")}>
                {t.broadcast.startAnother}
              </Button>
            </CardContent>
          </Card>
        )}
    </div>
  );
}
