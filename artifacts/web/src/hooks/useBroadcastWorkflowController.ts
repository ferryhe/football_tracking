import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetArtifactUrl,
  getGetBroadcastReviewWindowsQueryKey,
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  getListRunsQueryKey,
  useCancelRun,
  useGetBroadcastReviewWindows,
  useGetRun,
  useListArtifacts,
  useListRuns,
  useRecomputeBroadcastTrajectory,
  useRenderBroadcastHybrid,
  useSubmitBroadcastReviewActions,
  useSubmitBroadcastTerminalTailReview,
  type ArtifactSummary,
  type BroadcastRenderRequest,
  type BroadcastReviewWindowsResponse,
  type BroadcastTerminalTailReviewState,
} from "@workspace/api-client-react";

import {
  BROADCAST_DELIVERY_ARTIFACTS,
  type BroadcastDeliveryArtifact,
} from "@/components/broadcast/BroadcastRenderStep";
import type { BroadcastReviewDecision } from "@/components/broadcast/BroadcastReviewStep";
import {
  broadcastArtifactQueryIdentity,
  broadcastCancellationTarget,
  broadcastRecomputeRecoveryMode,
  localizeBroadcastWorkflowMessage,
  mergeBroadcastArtifacts,
  recoverBroadcastWorkflowRun,
  resolveBroadcastMontageArtifact,
  validateAndBuildBroadcastReviewActions,
  type BroadcastArtifactQueryIdentity,
  type BroadcastRecomputeRecoveryMode,
  type BroadcastWorkflowRecovery,
  type BroadcastWorkflowStateName,
} from "@/lib/broadcastWorkflow";
import type { Language } from "@/lib/i18n";

const NO_STORE_REQUEST = {
  cache: "no-store" as const,
  headers: { "Cache-Control": "no-store" },
};

const ACTIVE_STATES = new Set<BroadcastWorkflowStateName>([
  "tracking",
  "recomputing",
  "rendering",
]);

type ActionName = "review" | "recompute" | "render" | "cancel";

export type BroadcastControllerErrorCode =
  | "submitFailed"
  | "recomputeFailed"
  | "renderFailed"
  | "cancelFailed"
  | "staleEvidence";

export interface BroadcastControllerError {
  code: BroadcastControllerErrorCode;
  cause?: unknown;
  message?: string;
}

export interface BroadcastControllerReview {
  data: BroadcastReviewWindowsResponse | null;
  localizedData: BroadcastReviewWindowsResponse | null;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  decisionsArtifact: ArtifactSummary | null;
  recomputeRecoveryMode: BroadcastRecomputeRecoveryMode;
  recoveryAttemptState: "idle" | "pending" | "failed";
}

export interface BroadcastControllerMontage {
  urlsByCandidateId: Record<string, string>;
  messages: string[];
}

export interface BroadcastControllerDeliveryEvidence {
  queryIdentity: BroadcastArtifactQueryIdentity;
  listedArtifacts: ArtifactSummary[] | null;
  listSucceeded: boolean;
  urls: Partial<Record<BroadcastDeliveryArtifact, string>>;
}

export interface BroadcastControllerPending {
  initialLoad: boolean;
  review: boolean;
  recompute: boolean;
  render: boolean;
  cancel: boolean;
  recovery: boolean;
}

export interface BroadcastWorkflowController {
  recovery: BroadcastWorkflowRecovery;
  parent: BroadcastWorkflowRecovery["parentRun"];
  operation: BroadcastWorkflowRecovery["operationRun"];
  artifacts: ArtifactSummary[];
  review: BroadcastControllerReview;
  montage: BroadcastControllerMontage;
  delivery: BroadcastControllerDeliveryEvidence;
  workflowMessages: string[];
  pending: BroadcastControllerPending;
  errors: {
    action: BroadcastControllerError | null;
    query: unknown;
  };
  actions: {
    refresh: () => Promise<void>;
    acceptTerminalTailReview: (reviewerId: string) => Promise<void>;
    submitReview: (
      decisions: BroadcastReviewDecision[],
      reviewerId: string,
    ) => Promise<void>;
    retryRecompute: () => Promise<void>;
    render: (request: BroadcastRenderRequest) => Promise<void>;
    cancel: () => Promise<void>;
    clearError: () => void;
  };
}

export interface UseBroadcastWorkflowControllerInput {
  parentRunId: string | null;
  enabled: boolean;
  language: Language;
}

interface ScopeSnapshot {
  key: string;
  parentRunId: string;
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
    headers: {
      Accept: "application/octet-stream, application/json",
      "Cache-Control": "no-store",
    },
    signal,
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} while reading ${artifact.name}`);
  }
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

export function useBroadcastWorkflowController({
  parentRunId: requestedRunId,
  enabled,
  language,
}: UseBroadcastWorkflowControllerInput): BroadcastWorkflowController {
  const queryClient = useQueryClient();
  const [pollActive, setPollActive] = useState(
    enabled && Boolean(requestedRunId),
  );
  const [actionError, setActionError] =
    useState<BroadcastControllerError | null>(null);
  const [recoveryNonce, setRecoveryNonce] = useState(0);
  const [recoveryAttemptState, setRecoveryAttemptState] = useState<
    "idle" | "pending" | "failed"
  >("idle");
  const [locallyPending, setLocallyPending] = useState<
    Record<ActionName, boolean>
  >({ review: false, recompute: false, render: false, cancel: false });
  const mountedRef = useRef(true);
  const scopeRef = useRef("");
  const actionLocksRef = useRef(new Map<ActionName, symbol>());
  const recoveredArtifactRef = useRef<string | null>(null);
  const recoveryAttemptKeyRef = useRef<string | null>(null);

  const requestedRunQuery = useGetRun(requestedRunId ?? "", {
    query: {
      queryKey: getGetRunQueryKey(requestedRunId ?? ""),
      enabled: enabled && Boolean(requestedRunId),
      refetchInterval: pollActive ? 2_000 : false,
    },
    request: NO_STORE_REQUEST,
  });
  const runsQuery = useListRuns({
    query: {
      queryKey: getListRunsQueryKey(),
      enabled: enabled && Boolean(requestedRunId),
      refetchInterval: pollActive ? 2_000 : false,
    },
    request: NO_STORE_REQUEST,
  });
  const recovery = useMemo(
    () =>
      recoverBroadcastWorkflowRun(
        enabled ? (requestedRunQuery.data ?? requestedRunId) : null,
        runsQuery.data ?? [],
      ),
    [enabled, requestedRunId, requestedRunQuery.data, runsQuery.data],
  );
  const parent = recovery.parentRun;
  const parentId = parent?.run_id ?? "";
  const scopeKey = `${enabled ? "enabled" : "disabled"}:${requestedRunId ?? ""}:${parentId}`;
  if (scopeRef.current !== scopeKey) {
    scopeRef.current = scopeKey;
    actionLocksRef.current.clear();
  }

  const artifactQueryIdentity = useMemo(
    () =>
      broadcastArtifactQueryIdentity(
        recovery.state,
        parent?.broadcast?.status_generation,
      ),
    [parent?.broadcast?.status_generation, recovery.state],
  );
  const artifactParams = useMemo(
    () =>
      artifactQueryIdentity.deliveryReady
        ? { status_generation: parent?.broadcast?.status_generation }
        : undefined,
    [artifactQueryIdentity.deliveryReady, parent?.broadcast?.status_generation],
  );
  const artifactsQuery = useListArtifacts(parentId, artifactParams, {
    query: {
      queryKey: [
        ...getListArtifactsQueryKey(parentId, artifactParams),
        artifactQueryIdentity.scope,
      ],
      enabled: enabled && Boolean(parentId),
      refetchInterval: pollActive ? 2_000 : false,
    },
    request: NO_STORE_REQUEST,
  });
  const artifacts = useMemo(
    () => mergeBroadcastArtifacts(parent?.artifacts, artifactsQuery.data),
    [artifactsQuery.data, parent?.artifacts],
  );
  const reviewDecisionsArtifact = useMemo(
    () => exactArtifact(artifacts, "review_decisions.json"),
    [artifacts],
  );
  const reviewEnabled = Boolean(
    enabled && parentId && recovery.state === "needs_review",
  );
  const reviewQuery = useGetBroadcastReviewWindows(parentId, {
    query: {
      queryKey: getGetBroadcastReviewWindowsQueryKey(parentId),
      enabled: reviewEnabled,
      retry: false,
    },
    request: NO_STORE_REQUEST,
  });
  const terminalTailReview = reviewQuery.data?.terminal_tail_review;
  const terminalTailGateOpen =
    reviewEnabled &&
    reviewQuery.isSuccess &&
    terminalTailReview !== undefined &&
    (terminalTailReview.status === "not_required" ||
      terminalTailReview.status === "accepted");
  const qualifiedReviewQueueReady = Boolean(
    reviewQuery.data?.status === "ready" &&
      /^[0-9a-f]{64}$/.test(reviewQuery.data.queue_sha256?.trim() ?? ""),
  );
  const recomputeRecoveryMode =
    terminalTailGateOpen && qualifiedReviewQueueReady
    ? broadcastRecomputeRecoveryMode(parent, reviewDecisionsArtifact !== null)
    : "none";

  const submitReviewMutation = useSubmitBroadcastReviewActions();
  const submitTerminalTailReviewMutation =
    useSubmitBroadcastTerminalTailReview();
  const recomputeMutation = useRecomputeBroadcastTrajectory();
  const renderMutation = useRenderBroadcastHybrid();
  const cancelMutation = useCancelRun();
  const submitReviewAsync = submitReviewMutation.mutateAsync;
  const submitTerminalTailReviewAsync =
    submitTerminalTailReviewMutation.mutateAsync;
  const recomputeAsync = recomputeMutation.mutateAsync;
  const renderAsync = renderMutation.mutateAsync;
  const cancelAsync = cancelMutation.mutateAsync;

  const captureScope = useCallback(
    (): ScopeSnapshot => ({ key: scopeRef.current, parentRunId: parentId }),
    [parentId],
  );
  const scopeIsCurrent = useCallback(
    (scope: ScopeSnapshot): boolean =>
      mountedRef.current &&
      scope.key === scopeRef.current &&
      scope.parentRunId === parentId,
    [parentId],
  );
  const beginAction = useCallback((name: ActionName): symbol | null => {
    if (actionLocksRef.current.has(name)) return null;
    const token = Symbol(name);
    actionLocksRef.current.set(name, token);
    if (mountedRef.current) {
      setLocallyPending((current) => ({ ...current, [name]: true }));
    }
    return token;
  }, []);
  const finishAction = useCallback((name: ActionName, token: symbol) => {
    if (actionLocksRef.current.get(name) !== token) return;
    actionLocksRef.current.delete(name);
    if (mountedRef.current) {
      setLocallyPending((current) => ({ ...current, [name]: false }));
    }
  }, []);
  const setScopedError = useCallback(
    (scope: ScopeSnapshot, error: BroadcastControllerError | null) => {
      if (scopeIsCurrent(scope)) setActionError(error);
    },
    [scopeIsCurrent],
  );

  const refreshWorkflow = useCallback(
    async (extraRunId?: string, scope?: ScopeSnapshot) => {
      if (scope && !scopeIsCurrent(scope)) return;
      const invalidations: Promise<unknown>[] = [
        queryClient.invalidateQueries({ queryKey: getListRunsQueryKey() }),
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: ["health"] }),
      ];
      if (parentId) {
        invalidations.push(
          queryClient.invalidateQueries({
            queryKey: getGetRunQueryKey(parentId),
          }),
          queryClient.invalidateQueries({
            queryKey: getListArtifactsQueryKey(parentId),
          }),
          queryClient.invalidateQueries({
            queryKey: getGetBroadcastReviewWindowsQueryKey(parentId),
          }),
        );
      }
      if (requestedRunId && requestedRunId !== parentId) {
        invalidations.push(
          queryClient.invalidateQueries({
            queryKey: getGetRunQueryKey(requestedRunId),
          }),
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
    },
    [parentId, queryClient, requestedRunId, scopeIsCurrent],
  );

  const queueRecompute = useCallback(
    async (digest: string, scope: ScopeSnapshot) => {
      if (!scope.parentRunId || !scopeIsCurrent(scope)) return;
      const queued = await recomputeAsync({
        runId: scope.parentRunId,
        data: { review_decisions_sha256: digest },
      });
      if (!scopeIsCurrent(scope)) return;
      setPollActive(true);
      await refreshWorkflow(queued.run_id, scope);
    },
    [recomputeAsync, refreshWorkflow, scopeIsCurrent],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      actionLocksRef.current.clear();
    };
  }, []);

  useEffect(() => {
    setPollActive(
      enabled &&
        Boolean(requestedRunId) &&
        (ACTIVE_STATES.has(recovery.state) || recovery.pollRunIds.length > 0),
    );
  }, [enabled, recovery.pollRunIds.length, recovery.state, requestedRunId]);

  useEffect(() => {
    if (
      !enabled ||
      !parentId ||
      ACTIVE_STATES.has(recovery.state) ||
      recovery.state === "setup"
    ) {
      return;
    }
    void queryClient.invalidateQueries({
      queryKey: getListArtifactsQueryKey(parentId),
    });
  }, [enabled, parentId, queryClient, recovery.state]);

  useEffect(() => {
    setActionError(null);
    recoveredArtifactRef.current = null;
    recoveryAttemptKeyRef.current = null;
    setRecoveryAttemptState("idle");
    setLocallyPending({
      review: false,
      recompute: false,
      render: false,
      cancel: false,
    });
  }, [scopeKey]);

  const retryRecompute = useCallback(async () => {
    if (
      !parentId ||
      !reviewDecisionsArtifact ||
      (recomputeRecoveryMode !== "retry" &&
        recoveryAttemptState !== "failed") ||
      recoveryAttemptKeyRef.current !== null
    ) {
      return;
    }
    const token = beginAction("recompute");
    if (!token) return;
    const scope = captureScope();
    const retryKey = `retry:${parentId}:${reviewDecisionsArtifact.size_bytes}`;
    recoveredArtifactRef.current = retryKey;
    recoveryAttemptKeyRef.current = retryKey;
    setRecoveryAttemptState("pending");
    setScopedError(scope, null);
    try {
      const digest = await sha256Artifact(parentId, reviewDecisionsArtifact);
      if (!scopeIsCurrent(scope) || recoveredArtifactRef.current !== retryKey) {
        return;
      }
      await queueRecompute(digest, scope);
      if (scopeIsCurrent(scope) && recoveryAttemptKeyRef.current === retryKey) {
        setRecoveryAttemptState("idle");
      }
    } catch (error) {
      if (!scopeIsCurrent(scope)) return;
      if (recoveryAttemptKeyRef.current === retryKey) {
        setRecoveryAttemptState("failed");
      }
      if (statusCode(error) === 409) {
        recoveredArtifactRef.current = `blocked:${parentId}`;
        setScopedError(scope, { code: "staleEvidence", cause: error });
        await refreshWorkflow(undefined, scope);
      } else {
        setScopedError(scope, { code: "recomputeFailed", cause: error });
      }
    } finally {
      if (recoveryAttemptKeyRef.current === retryKey) {
        recoveryAttemptKeyRef.current = null;
      }
      finishAction("recompute", token);
    }
  }, [
    beginAction,
    captureScope,
    finishAction,
    parentId,
    queueRecompute,
    recomputeRecoveryMode,
    recoveryAttemptState,
    refreshWorkflow,
    reviewDecisionsArtifact,
    scopeIsCurrent,
    setScopedError,
  ]);

  const acceptTerminalTailReview = useCallback(
    async (reviewerId: string) => {
      if (!parentId || !reviewQuery.data) return;
      const scope = captureScope();
      const tailReview = reviewQuery.data.terminal_tail_review;
      if (tailReview?.status === "invalid") {
        setScopedError(scope, {
          code: "staleEvidence",
          message:
            tailReview.reason ??
            "The terminal-tail review evidence is invalid or stale.",
        });
        return;
      }
      if (tailReview?.status !== "required") return;
      const reviewer = reviewerId.trim();
      if (!reviewer) {
        setScopedError(scope, {
          code: "submitFailed",
          message:
            "Reviewer identity is required to accept the terminal-tail limitation.",
        });
        return;
      }
      const evidenceSha256 = tailReview.evidence?.evidence_sha256?.trim();
      if (!evidenceSha256 || !/^[0-9a-f]{64}$/.test(evidenceSha256)) {
        setScopedError(scope, {
          code: "staleEvidence",
          message:
            "The server did not provide hash-bound terminal-tail evidence.",
        });
        return;
      }
      const token = beginAction("review");
      if (!token) return;
      setScopedError(scope, null);
      try {
        const accepted = await submitTerminalTailReviewAsync({
          runId: parentId,
          data: {
            decision: "accept_terminal_shortfall",
            reviewer_id: reviewer,
            evidence_sha256: evidenceSha256,
          },
        });
        if (!scopeIsCurrent(scope)) return;
        const acknowledgementSha256 =
          accepted.details?.terminal_tail_review_sha256?.trim();
        if (
          !acknowledgementSha256 ||
          !/^[0-9a-f]{64}$/.test(acknowledgementSha256)
        ) {
          throw new Error(
            "The server did not return the terminal-tail acknowledgement hash.",
          );
        }
        await refreshWorkflow(undefined, scope);
      } catch (error) {
        if (!scopeIsCurrent(scope)) return;
        if (statusCode(error) === 409) {
          setScopedError(scope, { code: "staleEvidence", cause: error });
          await refreshWorkflow(undefined, scope);
        } else {
          setScopedError(scope, { code: "submitFailed", cause: error });
        }
      } finally {
        finishAction("review", token);
      }
    },
    [
      beginAction,
      captureScope,
      finishAction,
      parentId,
      refreshWorkflow,
      reviewQuery.data,
      scopeIsCurrent,
      setScopedError,
      submitTerminalTailReviewAsync,
    ],
  );

  const submitReview = useCallback(
    async (decisions: BroadcastReviewDecision[], reviewerId: string) => {
      if (!parentId || !reviewQuery.data) return;
      const scope = captureScope();
      const reviewer = reviewerId.trim();
      const tailReview: BroadcastTerminalTailReviewState =
        reviewQuery.data.terminal_tail_review ?? {
          status: "not_required",
          reason: null,
          evidence: null,
          decision: null,
          reviewer_id: null,
          reviewed_at: null,
          acknowledgement_sha256: null,
        };
      if (tailReview.status === "invalid") {
        setScopedError(scope, {
          code: "staleEvidence",
          message:
            tailReview.reason ??
            "The terminal-tail review evidence is invalid or stale.",
        });
        return;
      }
      if (tailReview.status === "required" && !reviewer) {
        setScopedError(scope, {
          code: "submitFailed",
          message: "Reviewer identity is required to accept the terminal-tail limitation.",
        });
        return;
      }
      const request = validateAndBuildBroadcastReviewActions(
        reviewQuery.data,
        decisions,
        reviewer,
        new Date().toISOString(),
      );
      if (!request.ok) {
        setScopedError(scope, {
          code: "submitFailed",
          message: request.messages
            .map((message) =>
              localizeBroadcastWorkflowMessage(message, language),
            )
            .join(" "),
        });
        return;
      }
      const token = beginAction("review");
      if (!token) return;
      setScopedError(scope, null);
      let digest: string;
      try {
        if (tailReview.status === "required") {
          const evidenceSha256 = tailReview.evidence?.evidence_sha256?.trim();
          if (!evidenceSha256 || !/^[0-9a-f]{64}$/.test(evidenceSha256)) {
            throw new Error(
              "The server did not provide hash-bound terminal-tail evidence.",
            );
          }
          const accepted = await submitTerminalTailReviewAsync({
            runId: parentId,
            data: {
              decision: "accept_terminal_shortfall",
              reviewer_id: reviewer,
              evidence_sha256: evidenceSha256,
            },
          });
          const acknowledgementSha256 =
            accepted.details?.terminal_tail_review_sha256?.trim();
          if (
            !acknowledgementSha256 ||
            !/^[0-9a-f]{64}$/.test(acknowledgementSha256)
          ) {
            throw new Error(
              "The server did not return the terminal-tail acknowledgement hash.",
            );
          }
        }
        const submitted = await submitReviewAsync({
          runId: parentId,
          data: request.value,
        });
        if (!scopeIsCurrent(scope)) {
          finishAction("review", token);
          return;
        }
        const submittedDigest =
          submitted.details?.review_decisions_sha256?.trim();
        if (!submittedDigest || !/^[0-9a-f]{64}$/.test(submittedDigest)) {
          throw new Error(
            "The server did not return the evidence-bound review decision hash.",
          );
        }
        digest = submittedDigest;
      } catch (error) {
        if (!scopeIsCurrent(scope)) {
          finishAction("review", token);
          return;
        }
        if (statusCode(error) === 409) {
          recoveredArtifactRef.current = `blocked:${parentId}`;
          setScopedError(scope, { code: "staleEvidence", cause: error });
          await refreshWorkflow(undefined, scope);
        } else {
          setScopedError(scope, { code: "submitFailed", cause: error });
        }
        finishAction("review", token);
        return;
      }
      try {
        recoveredArtifactRef.current = `submitted:${digest}`;
        await queueRecompute(digest, scope);
      } catch (error) {
        if (!scopeIsCurrent(scope)) return;
        recoveryAttemptKeyRef.current = null;
        setRecoveryAttemptState("failed");
        await refreshWorkflow(undefined, scope).catch(() => undefined);
        if (statusCode(error) === 409) {
          recoveredArtifactRef.current = `blocked:${parentId}`;
          setScopedError(scope, { code: "staleEvidence", cause: error });
        } else {
          setScopedError(scope, { code: "recomputeFailed", cause: error });
        }
      } finally {
        finishAction("review", token);
      }
    },
    [
      beginAction,
      captureScope,
      finishAction,
      language,
      parentId,
      queueRecompute,
      refreshWorkflow,
      reviewQuery.data,
      scopeIsCurrent,
      setScopedError,
      submitReviewAsync,
      submitTerminalTailReviewAsync,
    ],
  );

  useEffect(() => {
    if (
      recomputeRecoveryMode !== "auto" ||
      recovery.state !== "needs_review" ||
      !parentId ||
      !reviewDecisionsArtifact ||
      !terminalTailGateOpen ||
      submitReviewMutation.isPending ||
      submitTerminalTailReviewMutation.isPending ||
      recomputeMutation.isPending ||
      recoveryAttemptKeyRef.current !== null
    ) {
      return;
    }
    if (
      recoveredArtifactRef.current === `blocked:${parentId}` ||
      recoveredArtifactRef.current?.startsWith("submitted:")
    ) {
      return;
    }
    const recoveryKey = `auto:${parentId}:${reviewDecisionsArtifact.size_bytes}`;
    if (recoveredArtifactRef.current === recoveryKey) return;
    const token = beginAction("recompute");
    if (!token) return;
    const scope = captureScope();
    recoveredArtifactRef.current = recoveryKey;
    recoveryAttemptKeyRef.current = recoveryKey;
    setRecoveryAttemptState("pending");
    const controller = new AbortController();
    let queueStarted = false;
    void (async () => {
      try {
        const digest = await sha256Artifact(
          parentId,
          reviewDecisionsArtifact,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          !scopeIsCurrent(scope) ||
          recoveredArtifactRef.current !== recoveryKey
        ) {
          return;
        }
        queueStarted = true;
        await queueRecompute(digest, scope);
        if (
          scopeIsCurrent(scope) &&
          recoveryAttemptKeyRef.current === recoveryKey
        ) {
          setRecoveryAttemptState("idle");
        }
      } catch (error) {
        if (
          (!queueStarted && controller.signal.aborted) ||
          !scopeIsCurrent(scope) ||
          recoveredArtifactRef.current !== recoveryKey
        ) {
          return;
        }
        if (recoveryAttemptKeyRef.current === recoveryKey) {
          setRecoveryAttemptState("failed");
        }
        if (statusCode(error) === 409) {
          recoveredArtifactRef.current = `blocked:${parentId}`;
          setScopedError(scope, { code: "staleEvidence", cause: error });
          await refreshWorkflow(undefined, scope);
        } else {
          setScopedError(scope, { code: "recomputeFailed", cause: error });
        }
      } finally {
        if (recoveryAttemptKeyRef.current === recoveryKey) {
          recoveryAttemptKeyRef.current = null;
        }
        finishAction("recompute", token);
      }
    })();
    return () => {
      controller.abort();
      if (!queueStarted && recoveryAttemptKeyRef.current === recoveryKey) {
        recoveryAttemptKeyRef.current = null;
        if (recoveredArtifactRef.current === recoveryKey) {
          recoveredArtifactRef.current = null;
        }
        if (mountedRef.current && scopeIsCurrent(scope)) {
          setRecoveryAttemptState("idle");
        }
        finishAction("recompute", token);
      }
    };
  }, [
    beginAction,
    captureScope,
    finishAction,
    parentId,
    queueRecompute,
    recomputeMutation.isPending,
    recomputeRecoveryMode,
    recovery.state,
    recoveryNonce,
    refreshWorkflow,
    reviewDecisionsArtifact,
    scopeIsCurrent,
    setScopedError,
    submitReviewMutation.isPending,
    submitTerminalTailReviewMutation.isPending,
    terminalTailGateOpen,
  ]);

  const render = useCallback(
    async (request: BroadcastRenderRequest) => {
      if (!parentId) return;
      const token = beginAction("render");
      if (!token) return;
      const scope = captureScope();
      setScopedError(scope, null);
      try {
        const queued = await renderAsync({
          runId: parentId,
          data: request,
        });
        if (!scopeIsCurrent(scope)) return;
        setPollActive(true);
        await refreshWorkflow(queued.run_id, scope);
      } catch (error) {
        if (!scopeIsCurrent(scope)) return;
        if (statusCode(error) === 409) {
          setScopedError(scope, { code: "staleEvidence", cause: error });
          await refreshWorkflow(undefined, scope);
        } else {
          setScopedError(scope, { code: "renderFailed", cause: error });
        }
      } finally {
        finishAction("render", token);
      }
    },
    [
      beginAction,
      captureScope,
      finishAction,
      parentId,
      refreshWorkflow,
      renderAsync,
      scopeIsCurrent,
      setScopedError,
    ],
  );

  const cancel = useCallback(async () => {
    const targetId = broadcastCancellationTarget(recovery);
    if (!targetId) return;
    const token = beginAction("cancel");
    if (!token) return;
    const scope = captureScope();
    if (recovery.state === "recomputing" && parentId) {
      recoveredArtifactRef.current = `blocked:${parentId}`;
    }
    setScopedError(scope, null);
    try {
      await cancelAsync({ runId: targetId });
      if (!scopeIsCurrent(scope)) return;
      await refreshWorkflow(targetId, scope);
    } catch (error) {
      if (scopeIsCurrent(scope)) {
        setScopedError(scope, { code: "cancelFailed", cause: error });
      }
    } finally {
      finishAction("cancel", token);
    }
  }, [
    beginAction,
    cancelAsync,
    captureScope,
    finishAction,
    parentId,
    recovery,
    refreshWorkflow,
    scopeIsCurrent,
    setScopedError,
  ]);

  const refresh = useCallback(async () => {
    recoveredArtifactRef.current = null;
    recoveryAttemptKeyRef.current = null;
    if (mountedRef.current) {
      setRecoveryAttemptState("idle");
      setActionError(null);
      setRecoveryNonce((value) => value + 1);
    }
    await refreshWorkflow();
  }, [refreshWorkflow]);

  const montage = useMemo<BroadcastControllerMontage>(() => {
    const urlsByCandidateId: Record<string, string> = {};
    const messages: string[] = [];
    for (const item of reviewQuery.data?.items ?? []) {
      for (const candidate of item.candidates ?? []) {
        const resolved = resolveBroadcastMontageArtifact(artifacts, candidate);
        if (resolved.ok) {
          urlsByCandidateId[candidate.candidate_id] = getGetArtifactUrl(
            parentId,
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
    return { urlsByCandidateId, messages };
  }, [artifacts, language, parentId, reviewQuery.data]);

  const localizedReviewData = useMemo<BroadcastReviewWindowsResponse | null>(
    () =>
      reviewQuery.data
        ? {
            ...reviewQuery.data,
            reason: reviewQuery.data.reason
              ? localizeBroadcastWorkflowMessage(
                  reviewQuery.data.reason,
                  language,
                )
              : reviewQuery.data.reason,
          }
        : null,
    [language, reviewQuery.data],
  );

  const deliveryUrls = useMemo(() => {
    const urls: Partial<Record<BroadcastDeliveryArtifact, string>> = {};
    if (!artifactQueryIdentity.deliveryReady || !artifactsQuery.isSuccess) {
      return urls;
    }
    for (const name of BROADCAST_DELIVERY_ARTIFACTS) {
      const artifact = exactArtifact(artifactsQuery.data ?? [], name);
      if (artifact) {
        urls[name] = getGetArtifactUrl(parentId, artifact.name, artifactParams);
      }
    }
    return urls;
  }, [
    artifactQueryIdentity.deliveryReady,
    artifactsQuery.data,
    artifactsQuery.isSuccess,
    artifactParams,
    parentId,
  ]);

  const workflowMessages = useMemo(
    () =>
      Array.from(
        new Set(
          [
            ...recovery.messages.map((message) =>
              localizeBroadcastWorkflowMessage(message, language),
            ),
            ...(parent?.broadcast?.blocking_reasons ?? []).map((message) =>
              localizeBroadcastWorkflowMessage(message, language),
            ),
          ].filter(Boolean),
        ),
      ),
    [language, parent?.broadcast?.blocking_reasons, recovery.messages],
  );

  const queryError =
    requestedRunQuery.error ?? runsQuery.error ?? artifactsQuery.error;
  const pending: BroadcastControllerPending = {
    initialLoad: Boolean(
      enabled &&
      requestedRunId &&
      !parent &&
      (requestedRunQuery.isLoading || runsQuery.isLoading),
    ),
    review:
      locallyPending.review ||
      submitTerminalTailReviewMutation.isPending ||
      submitReviewMutation.isPending ||
      recomputeMutation.isPending,
    recompute: locallyPending.recompute || recomputeMutation.isPending,
    render: locallyPending.render || renderMutation.isPending,
    cancel: locallyPending.cancel || cancelMutation.isPending,
    recovery: recoveryAttemptState === "pending",
  };

  return {
    recovery,
    parent,
    operation: recovery.operationRun,
    artifacts,
    review: {
      data: reviewQuery.data ?? null,
      localizedData: localizedReviewData,
      isLoading: reviewQuery.isLoading,
      isError: reviewQuery.isError,
      error: reviewQuery.error,
      decisionsArtifact: reviewDecisionsArtifact,
      recomputeRecoveryMode,
      recoveryAttemptState,
    },
    montage,
    delivery: {
      queryIdentity: artifactQueryIdentity,
      listedArtifacts: artifactsQuery.data ?? null,
      listSucceeded: artifactsQuery.isSuccess,
      urls: deliveryUrls,
    },
    workflowMessages,
    pending,
    errors: { action: actionError, query: queryError },
    actions: {
      refresh,
      acceptTerminalTailReview,
      submitReview,
      retryRecompute,
      render,
      cancel,
      clearError: () => setActionError(null),
    },
  };
}
