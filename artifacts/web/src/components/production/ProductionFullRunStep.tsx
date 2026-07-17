import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import {
  getGetConfigQueryKey,
  getGetArtifactUrl,
  getGetHealthQueryKey,
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  getListInputVideosQueryKey,
  getListRunsQueryKey,
  useCancelRun,
  useCreateRun,
  useGetConfig,
  useGetHealth,
  useGetRun,
  useListArtifacts,
  useListInputVideos,
  useListRuns,
  type BroadcastRenderRequest,
  type RunRecord,
} from "@workspace/api-client-react";
import {
  CheckCircle2,
  Download,
  Loader2,
  Play,
  RefreshCw,
  Square,
} from "lucide-react";

import {
  BroadcastReviewStep,
  type BroadcastReviewDecision,
} from "@/components/broadcast/BroadcastReviewStep";
import { BroadcastRenderStep } from "@/components/broadcast/BroadcastRenderStep";
import { BroadcastReviewEvidenceStep } from "@/components/broadcast/BroadcastReviewEvidenceStep";
import { useBroadcastReviewEvidenceController } from "@/components/broadcast/useBroadcastReviewEvidenceController";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";
import { useBroadcastWorkflowController } from "@/hooks/useBroadcastWorkflowController";
import {
  assessBroadcastDelivery,
  PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS,
  type BroadcastDeliveryAssessment,
  type ProductionBroadcastDeliveryArtifact,
} from "@/lib/broadcastDelivery";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import {
  appendProductionFullRunAttempt,
  buildProductionFullRunSubmission,
  clearPendingProductionFullRun,
  createProductionFullRunState,
  nextProductionFullRunGeneration,
  observeProductionFullRun,
  productionFullRunAuthoritativeContextMatches,
  productionFullRunRequestHashesMatch,
  reconcilePendingProductionFullRun,
  setPendingProductionFullRun,
  type ProductionFullRunAttempt,
  type ProductionFullRunState,
  type ProductionFullRunStatus,
} from "@/lib/productionBroadcast";
import {
  verifyProductionConfigDetail,
  type ProductionConfigEvidence,
  type ProductionConfigVerification,
} from "@/lib/productionConfigFreeze";
import {
  canonicalJson,
  type ProductionTrialState,
} from "@/lib/productionTrial";
import type {
  ProductionProductEvidence,
  SourceSignature,
} from "@/lib/productionWorkflow";

const NO_STORE_REQUEST = {
  cache: "no-store" as const,
  headers: { "Cache-Control": "no-store" },
};

const ACTIVE_RENDER_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "reconciling",
  "committing",
]);

export interface ProductionFullRunStepProps {
  workflowId: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  trial: ProductionTrialState;
  confirmedConfig: ProductionConfigEvidence;
  fullRun: ProductionFullRunState | null;
  verifiedProduct: ProductionProductEvidence | null;
  requestedRunId: string | null;
  focusRequest?: number;
  onFullRunChange: (
    fullRun: ProductionFullRunState,
    expectedRevision: number,
  ) => boolean;
  onPersistCurrent: (expectedRevision: number) => boolean;
  onVerifiedProduct: (
    product: ProductionProductEvidence,
    expectedRevision: number,
  ) => boolean;
  onParentRunIdChange: (runId: string) => void;
}

function errorStatus(error: unknown): number | null {
  if (!error || typeof error !== "object" || !("status" in error)) return null;
  const value = Number((error as { status?: unknown }).status);
  return Number.isFinite(value) ? value : null;
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : String(error);
}

function currentAttempt(
  state: ProductionFullRunState | null,
): ProductionFullRunAttempt | null {
  if (!state?.current_run_id) return null;
  return (
    state.attempts.find((attempt) => attempt.run_id === state.current_run_id) ??
    null
  );
}

function sameState(
  left: ProductionFullRunState,
  right: ProductionFullRunState,
): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function authoritativeRenderOperationStatus(
  operation: RunRecord | null,
  parentRunId: string,
): string | null {
  if (
    !operation ||
    operation.parent_run_id !== parentRunId ||
    operation.source !== "broadcast_hybrid_render" ||
    operation.broadcast?.operation !== "render" ||
    (operation.broadcast.parent_run_id != null &&
      operation.broadcast.parent_run_id !== parentRunId)
  ) {
    return null;
  }
  return operation.broadcast.operation_status ?? operation.status;
}

type VerifiedDelivery = Extract<
  BroadcastDeliveryAssessment,
  { status: "verified" }
>;

type DeliveryState =
  | { status: "idle"; key: string }
  | { status: "validating"; key: string }
  | { status: "blocked"; key: string; reasons: string[] }
  | {
      status: "video" | "ready";
      key: string;
      assessment: VerifiedDelivery;
      urls: Record<ProductionBroadcastDeliveryArtifact, string>;
    };

function evidenceMatches(
  left: ProductionProductEvidence | null,
  right: ProductionProductEvidence,
): boolean {
  return Boolean(
    left &&
    left.run_id === right.run_id &&
    left.artifact_name === right.artifact_name &&
    left.artifact_size_bytes === right.artifact_size_bytes &&
    left.artifact_sha256 === right.artifact_sha256 &&
    left.quality_report_sha256 === right.quality_report_sha256 &&
    left.status_generation === right.status_generation,
  );
}

export function ProductionFullRunStep({
  workflowId,
  source,
  calibration,
  trial,
  confirmedConfig,
  fullRun,
  verifiedProduct,
  requestedRunId,
  focusRequest = 0,
  onFullRunChange,
  onPersistCurrent,
  onVerifiedProduct,
  onParentRunIdChange,
}: ProductionFullRunStepProps) {
  const { language, t } = useLanguage();
  const createRun = useCreateRun();
  const cancelRun = useCancelRun();
  const config = useGetConfig(confirmedConfig.name, {
    query: {
      queryKey: getGetConfigQueryKey(confirmedConfig.name),
      enabled: false,
      staleTime: 0,
      retry: false,
    },
    request: NO_STORE_REQUEST,
  });
  const health = useGetHealth({
    query: { queryKey: getGetHealthQueryKey(), enabled: false, retry: false },
    request: NO_STORE_REQUEST,
  });
  const inputCatalog = useListInputVideos({
    query: {
      queryKey: getListInputVideosQueryKey(),
      enabled: false,
      staleTime: 0,
      retry: false,
    },
    request: NO_STORE_REQUEST,
  });
  const acceptedTrialRunId = trial.accepted?.run_id ?? "";
  const acceptedTrialRun = useGetRun(acceptedTrialRunId, {
    query: {
      queryKey: getGetRunQueryKey(acceptedTrialRunId),
      enabled: false,
      staleTime: 0,
      retry: false,
    },
    request: NO_STORE_REQUEST,
  });
  const acceptedTrialArtifacts = useListArtifacts(
    acceptedTrialRunId,
    undefined,
    {
      query: {
        queryKey: getListArtifactsQueryKey(acceptedTrialRunId),
        enabled: false,
        staleTime: 0,
        retry: false,
      },
      request: NO_STORE_REQUEST,
    },
  );
  const attempt = currentAttempt(fullRun);
  const currentRunId = attempt?.run_id ?? "";
  const fullRunIdentityScope = canonicalJson(
    fullRun
      ? {
          pending: fullRun.pending_submission
            ? {
                request: fullRun.pending_submission.request,
                request_sha256: fullRun.pending_submission.request_sha256,
              }
            : null,
          attempts: fullRun.attempts.map((candidate) => ({
            run_id: candidate.run_id,
            request: candidate.request,
            request_sha256: candidate.request_sha256,
          })),
        }
      : null,
  );
  const requiresFullRunHashValidation = Boolean(
    fullRun && (fullRun.pending_submission || fullRun.attempts.length > 0),
  );
  const fullRunHashRef = useRef(fullRun);
  fullRunHashRef.current = fullRun;
  const [hashGate, setHashGate] = useState<{
    scope: string;
    status: "validating" | "valid" | "invalid";
  }>(() => ({
    scope: requiresFullRunHashValidation ? "" : fullRunIdentityScope,
    status: requiresFullRunHashValidation ? "validating" : "valid",
  }));
  const hashGateStatus =
    hashGate.scope === fullRunIdentityScope
      ? hashGate.status
      : requiresFullRunHashValidation
        ? "validating"
        : "valid";
  const fullRunHashValid = hashGateStatus === "valid";

  useEffect(() => {
    let cancelled = false;
    const state = fullRunHashRef.current;
    if (!state || !requiresFullRunHashValidation) {
      setHashGate({ scope: fullRunIdentityScope, status: "valid" });
      return () => {
        cancelled = true;
      };
    }
    setHashGate({ scope: fullRunIdentityScope, status: "validating" });
    void productionFullRunRequestHashesMatch(state).then((matches) => {
      if (!cancelled) {
        setHashGate({
          scope: fullRunIdentityScope,
          status: matches ? "valid" : "invalid",
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fullRunIdentityScope, requiresFullRunHashValidation]);

  const runs = useListRuns({
    query: {
      queryKey: getListRunsQueryKey(),
      enabled: !requiresFullRunHashValidation || fullRunHashValid,
      refetchInterval:
        fullRunHashValid && fullRun?.pending_submission ? 2_000 : false,
    },
    request: NO_STORE_REQUEST,
  });
  const requestedNeedsIdentityLookup = Boolean(
    requestedRunId && requestedRunId !== currentRunId,
  );
  const requestedRecord = requestedRunId
    ? ((runs.data ?? []).find((run) => run.run_id === requestedRunId) ?? null)
    : null;
  const requestedChildRecognized = Boolean(
    requestedNeedsIdentityLookup &&
    currentRunId &&
    requestedRecord?.parent_run_id === currentRunId,
  );
  const requestedIdentitySettled =
    runs.data !== undefined || Boolean(runs.isError);
  const urlConflict = Boolean(
    requestedNeedsIdentityLookup &&
    requestedIdentitySettled &&
    !requestedChildRecognized,
  );
  const routeIdentityAccepted =
    !requestedNeedsIdentityLookup || requestedChildRecognized;
  const remoteFullRunEnabled = Boolean(
    currentRunId && fullRunHashValid && routeIdentityAccepted,
  );
  const controller = useBroadcastWorkflowController({
    parentRunId: currentRunId || null,
    enabled: remoteFullRunEnabled,
    language,
  });
  const reviewEvidenceEnabled = Boolean(
    remoteFullRunEnabled &&
    currentRunId &&
    controller.parent?.run_id === currentRunId &&
    controller.parent.source === "broadcast_hybrid" &&
    controller.recovery.state === "needs_review",
  );
  const reviewEvidence = useBroadcastReviewEvidenceController({
    runId: reviewEvidenceEnabled ? currentRunId : "",
    enabled: reviewEvidenceEnabled,
    messages: {
      ambiguousBundleRecovery:
        t.broadcast.reviewEvidence.ambiguousBundleRecovery,
      insufficientCapacityRecovery:
        t.broadcast.reviewEvidence.insufficientCapacityRecovery,
      retryBundleUnavailableRecovery:
        t.broadcast.reviewEvidence.retryBundleUnavailableRecovery,
    },
    formatRecoveryAction: t.broadcast.reviewEvidence.recoveryAction,
  });
  const runQuery = useGetRun(currentRunId, {
    query: {
      queryKey: getGetRunQueryKey(currentRunId),
      enabled: remoteFullRunEnabled,
      refetchInterval: (query) => {
        const status = query.state.data?.status;
        return status === "queued" || status === "running" ? 2_000 : false;
      },
    },
    request: NO_STORE_REQUEST,
  });
  const [message, setMessage] = useState<string | null>(null);
  const [conflictRunId, setConflictRunId] = useState<string | null>(null);
  const [reviewerId, setReviewerId] = useState("operator");
  const [terminalTailConfirmed, setTerminalTailConfirmed] = useState(false);
  const [reviewDecisions, setReviewDecisions] = useState<
    BroadcastReviewDecision[]
  >([]);
  const [deliveryState, setDeliveryState] = useState<DeliveryState>({
    status: "idle",
    key: "",
  });
  const [videoChecks, setVideoChecks] = useState({
    key: "",
    metadata: false,
    canPlay: false,
  });
  const [deliveryRetryNonce, setDeliveryRetryNonce] = useState(0);
  const [fullCancelPending, setFullCancelPending] = useState(false);
  const startLockRef = useRef(false);
  const reconcileLockRef = useRef(false);
  const cancelLockRef = useRef(false);
  const reviewSubmitLockRef = useRef(false);
  const recomputeRetryLockRef = useRef(false);
  const renderLockRef = useRef(false);
  const renderBaselineOperationIdRef = useRef<string | null>(null);
  const renderActiveOperationIdRef = useRef<string | null>(null);
  const previousRecoveryStateRef = useRef(controller.recovery.state);
  const deliveryValidationRef = useRef<string | null>(null);
  const deliveryFinalizeRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const latestFullRunRef = useRef(fullRun);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const reconcileHeadingRef = useRef<HTMLHeadingElement>(null);
  const operationScope = useMemo(
    () =>
      canonicalJson({
        workflowId,
        source,
        calibration: calibration.polygon_digest,
        trial: trial.accepted?.run_id,
        config: confirmedConfig.sha256,
      }),
    [
      calibration.polygon_digest,
      confirmedConfig.sha256,
      source,
      trial,
      workflowId,
    ],
  );
  const operationScopeRef = useRef(operationScope);
  operationScopeRef.current = operationScope;
  latestFullRunRef.current = fullRun;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!fullRun?.pending_submission) startLockRef.current = false;
  }, [fullRun?.pending_submission]);

  useEffect(() => {
    setReviewDecisions([]);
    reviewSubmitLockRef.current = false;
  }, [controller.review.data?.queue_sha256]);

  useEffect(() => {
    const previousState = previousRecoveryStateRef.current;
    previousRecoveryStateRef.current = controller.recovery.state;
    const renderStatus = authoritativeRenderOperationStatus(
      controller.operation,
      currentRunId,
    );
    const operationChanged = Boolean(
      controller.operation &&
      controller.operation.run_id !== renderBaselineOperationIdRef.current,
    );
    const activeRender = Boolean(
      operationChanged &&
      renderStatus &&
      ACTIVE_RENDER_OPERATION_STATUSES.has(renderStatus),
    );
    if (
      renderLockRef.current &&
      controller.recovery.state === "rendering" &&
      activeRender &&
      controller.operation
    ) {
      renderActiveOperationIdRef.current = controller.operation.run_id;
    }
    const terminalRender = Boolean(
      controller.operation &&
      controller.operation.run_id === renderActiveOperationIdRef.current &&
      (renderStatus === "failed" || renderStatus === "cancelled"),
    );
    const returnedWithoutActiveRender = Boolean(
      renderActiveOperationIdRef.current &&
      previousState === "rendering" &&
      controller.recovery.state === "trajectory_ready" &&
      !activeRender,
    );
    if (controller.recovery.state !== "needs_review") {
      reviewSubmitLockRef.current = false;
      recomputeRetryLockRef.current = false;
    }
    if (
      controller.recovery.state !== "trajectory_ready" &&
      controller.recovery.state !== "rendering"
    ) {
      renderLockRef.current = false;
      renderBaselineOperationIdRef.current = null;
      renderActiveOperationIdRef.current = null;
    } else if (terminalRender || returnedWithoutActiveRender) {
      renderLockRef.current = false;
      renderBaselineOperationIdRef.current = null;
      renderActiveOperationIdRef.current = null;
    }
  }, [controller.operation, controller.recovery.state, currentRunId]);

  useEffect(() => {
    if (controller.errors.action) {
      reviewSubmitLockRef.current = false;
      recomputeRetryLockRef.current = false;
      renderLockRef.current = false;
      renderBaselineOperationIdRef.current = null;
      renderActiveOperationIdRef.current = null;
    }
  }, [controller.errors.action]);

  useEffect(() => {
    if (!focusRequest) return;
    if (
      attempt?.last_observed.workflow_state === "tracking" ||
      attempt?.last_observed.workflow_state === "recomputing" ||
      attempt?.last_observed.workflow_state === "rendering"
    ) {
      cancelButtonRef.current?.focus();
    } else {
      reconcileHeadingRef.current?.focus();
    }
  }, [attempt?.last_observed.workflow_state, focusRequest]);

  useEffect(() => {
    if (
      !fullRunHashValid ||
      !currentRunId ||
      !routeIdentityAccepted ||
      urlConflict
    )
      return;
    if (!requestedRunId || requestedRunId !== currentRunId) {
      onParentRunIdChange(currentRunId);
    }
  }, [
    currentRunId,
    fullRunHashValid,
    onParentRunIdChange,
    requestedRunId,
    routeIdentityAccepted,
    urlConflict,
  ]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const current = latestFullRunRef.current;
      if (
        !routeIdentityAccepted ||
        urlConflict ||
        !fullRunHashValid ||
        !current?.pending_submission ||
        !runs.data
      )
        return;
      const reconciled = await reconcilePendingProductionFullRun(current, {
        runs: runs.data,
        observed_at: new Date().toISOString(),
      });
      if (cancelled || sameState(reconciled, current)) return;
      if (onFullRunChange(reconciled, current.revision)) {
        latestFullRunRef.current = reconciled;
        startLockRef.current = false;
        if (reconciled.current_run_id) {
          onParentRunIdChange(reconciled.current_run_id);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    fullRunHashValid,
    onFullRunChange,
    onParentRunIdChange,
    routeIdentityAccepted,
    urlConflict,
    runs.data,
  ]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const current = latestFullRunRef.current;
      const run = runQuery.data;
      if (
        !routeIdentityAccepted ||
        urlConflict ||
        !fullRunHashValid ||
        !current ||
        !run ||
        run.run_id !== current.current_run_id
      )
        return;
      if (controller.parent?.run_id === current.current_run_id) return;
      const observed = await observeProductionFullRun(current, {
        run,
        observed_at: new Date().toISOString(),
      });
      if (cancelled || sameState(observed, current)) return;
      if (onFullRunChange(observed, current.revision)) {
        latestFullRunRef.current = observed;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    controller.parent?.run_id,
    fullRunHashValid,
    onFullRunChange,
    routeIdentityAccepted,
    urlConflict,
    runQuery.data,
  ]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const current = latestFullRunRef.current;
      const parent = controller.parent;
      if (
        !routeIdentityAccepted ||
        urlConflict ||
        !fullRunHashValid ||
        !current ||
        !parent ||
        parent.run_id !== current.current_run_id ||
        controller.recovery.state === "setup"
      ) {
        return;
      }
      const observed = await observeProductionFullRun(current, {
        run: parent,
        observed_at: new Date().toISOString(),
        recovery: {
          state: controller.recovery.state,
          operation_run: controller.operation,
        },
      });
      if (cancelled || sameState(observed, current)) return;
      if (onFullRunChange(observed, current.revision)) {
        latestFullRunRef.current = observed;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    controller.operation,
    controller.parent,
    controller.recovery.state,
    fullRunHashValid,
    onFullRunChange,
    routeIdentityAccepted,
    urlConflict,
  ]);

  function scopeIsCurrent(scope: string): boolean {
    return mountedRef.current && operationScopeRef.current === scope;
  }

  async function persistReconciled(
    base: ProductionFullRunState,
    records: readonly RunRecord[],
  ): Promise<ProductionFullRunState | null> {
    const reconciled = await reconcilePendingProductionFullRun(base, {
      runs: records,
      observed_at: new Date().toISOString(),
    });
    if (sameState(reconciled, base)) return null;
    if (!onFullRunChange(reconciled, base.revision)) return null;
    latestFullRunRef.current = reconciled;
    startLockRef.current = false;
    if (reconciled.current_run_id) {
      onParentRunIdChange(reconciled.current_run_id);
    }
    return reconciled;
  }

  async function verifyFreshStartContext(
    scope: string,
  ): Promise<ProductionConfigVerification | null> {
    const [freshConfig, freshInputs, freshTrialRun, freshTrialArtifacts] =
      await Promise.all([
        config.refetch(),
        inputCatalog.refetch(),
        acceptedTrialRun.refetch(),
        acceptedTrialArtifacts.refetch(),
      ]);
    if (!scopeIsCurrent(scope)) return null;
    if (freshConfig.isError || !freshConfig.data) {
      setMessage(t.production.fullConfigUnverifiable);
      return null;
    }
    if (
      freshInputs.isError ||
      freshTrialRun.isError ||
      freshTrialArtifacts.isError ||
      !freshInputs.data ||
      !freshTrialRun.data ||
      !freshTrialArtifacts.data ||
      !(await productionFullRunAuthoritativeContextMatches({
        source,
        trial,
        input_catalog: freshInputs.data,
        accepted_trial_run: freshTrialRun.data,
        accepted_trial_artifacts: freshTrialArtifacts.data,
      }))
    ) {
      if (scopeIsCurrent(scope)) {
        setMessage(t.production.fullPrerequisitesChanged);
      }
      return null;
    }
    if (!scopeIsCurrent(scope)) return null;
    const verification = await verifyProductionConfigDetail(
      confirmedConfig,
      freshConfig.data,
    );
    if (!scopeIsCurrent(scope)) return null;
    if (verification.status !== "verified") {
      setMessage(t.production.fullConfigChanged);
      return null;
    }
    return verification;
  }

  async function startFrom(
    base: ProductionFullRunState,
    verifiedConfig?: ProductionConfigVerification,
  ) {
    if (startLockRef.current || base.pending_submission) return;
    startLockRef.current = true;
    const scope = operationScopeRef.current;
    let pendingPersisted = false;
    setMessage(null);
    setConflictRunId(null);
    try {
      const verification =
        verifiedConfig ?? (await verifyFreshStartContext(scope));
      if (!verification || !scopeIsCurrent(scope)) return;
      const submission = await buildProductionFullRunSubmission({
        workflow_id: workflowId,
        source,
        calibration,
        trial,
        confirmed_config: confirmedConfig,
        config_verification: verification,
        submission_id: crypto.randomUUID(),
        output_id: crypto.randomUUID(),
        generation: nextProductionFullRunGeneration(base),
        created_at: new Date().toISOString(),
      });
      if (!scopeIsCurrent(scope)) return;
      const waiting = setPendingProductionFullRun(base, submission.pending);
      if (!onFullRunChange(waiting, base.revision)) {
        setMessage(t.production.fullDraftStale);
        return;
      }
      pendingPersisted = true;
      latestFullRunRef.current = waiting;

      const healthResult = await health.refetch();
      if (!scopeIsCurrent(scope)) return;
      if (
        healthResult.isError ||
        !healthResult.data ||
        healthResult.data.status !== "ok"
      ) {
        setMessage(t.production.fullHealthUnavailable);
        return;
      }
      if (healthResult.data.active_run_id) {
        const records = await runs.refetch();
        if (!scopeIsCurrent(scope)) return;
        if (await persistReconciled(waiting, records.data ?? [])) return;
        setConflictRunId(healthResult.data.active_run_id);
        setMessage(
          t.production.fullActiveConflict(healthResult.data.active_run_id),
        );
        return;
      }

      try {
        const created = await createRun.mutateAsync({
          data: submission.pending.request,
        });
        if (!scopeIsCurrent(scope)) return;
        const current = latestFullRunRef.current;
        if (
          !current?.pending_submission ||
          current.pending_submission.submission_id !==
            submission.pending.submission_id
        ) {
          return;
        }
        const appended = appendProductionFullRunAttempt(current, {
          run: created,
          pending: submission.pending,
          observed_at: new Date().toISOString(),
        });
        if (!onFullRunChange(appended, current.revision)) {
          setMessage(t.production.fullDraftStale);
          return;
        }
        latestFullRunRef.current = appended;
        startLockRef.current = false;
        onParentRunIdChange(appended.current_run_id!);
      } catch (error) {
        const records = await runs.refetch();
        if (!scopeIsCurrent(scope)) return;
        const current = latestFullRunRef.current;
        if (current?.pending_submission) {
          if (await persistReconciled(current, records.data ?? [])) return;
        }
        if (errorStatus(error) === 409) {
          const healthAfter = await health.refetch();
          if (!scopeIsCurrent(scope)) return;
          const occupying = healthAfter.data?.active_run_id ?? "unknown";
          setConflictRunId(occupying);
          setMessage(t.production.fullActiveConflict(occupying));
        } else {
          setMessage(
            `${t.production.fullSubmissionUncertain} ${errorText(error)}`,
          );
        }
      }
    } finally {
      if (!pendingPersisted) startLockRef.current = false;
    }
  }

  async function handleStart() {
    await startFrom(latestFullRunRef.current ?? createProductionFullRunState());
  }

  async function handleReconcileAndRetry() {
    if (!fullRunHashValid || reconcileLockRef.current) return;
    const current = latestFullRunRef.current;
    if (!current?.pending_submission) return;
    reconcileLockRef.current = true;
    setMessage(null);
    const scope = operationScopeRef.current;
    try {
      const [healthResult, runsResult] = await Promise.all([
        health.refetch(),
        runs.refetch(),
      ]);
      if (!scopeIsCurrent(scope)) return;
      if (
        healthResult.isError ||
        runsResult.isError ||
        !healthResult.data ||
        healthResult.data.status !== "ok" ||
        !runsResult.data
      ) {
        setMessage(t.production.fullHealthUnavailable);
        return;
      }
      if (await persistReconciled(current, runsResult.data)) return;
      if (healthResult.data.active_run_id) {
        setConflictRunId(healthResult.data.active_run_id);
        setMessage(
          t.production.fullActiveConflict(healthResult.data.active_run_id),
        );
        return;
      }
      const verification = await verifyFreshStartContext(scope);
      if (!verification || !scopeIsCurrent(scope)) return;
      const cleared = clearPendingProductionFullRun(
        current,
        current.pending_submission,
      );
      if (!onFullRunChange(cleared, current.revision)) {
        setMessage(t.production.fullDraftStale);
        return;
      }
      latestFullRunRef.current = cleared;
      startLockRef.current = false;
      await startFrom(cleared, verification);
    } finally {
      reconcileLockRef.current = false;
    }
  }

  async function handleCancel(kind: "full" | "workflow") {
    const current = latestFullRunRef.current;
    if (!fullRunHashValid || cancelLockRef.current || !current) return;
    cancelLockRef.current = true;
    if (kind === "full") setFullCancelPending(true);
    setMessage(null);
    try {
      if (!onPersistCurrent(current.revision)) {
        setMessage(t.production.fullDraftStale);
        return;
      }
      if (kind === "workflow") {
        await controller.actions.cancel();
        return;
      }
      const fullRunRecord =
        runQuery.data?.run_id === current.current_run_id
          ? runQuery.data
          : controller.parent?.run_id === current.current_run_id
            ? controller.parent
            : null;
      if (!fullRunRecord) return;
      const cancelled = await cancelRun.mutateAsync({
        runId: fullRunRecord.run_id,
      });
      const latest = latestFullRunRef.current;
      if (!latest) return;
      const observed = await observeProductionFullRun(latest, {
        run: cancelled,
        observed_at: new Date().toISOString(),
      });
      if (!sameState(observed, latest)) {
        onFullRunChange(observed, latest.revision);
      }
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      cancelLockRef.current = false;
      if (kind === "full" && mountedRef.current) setFullCancelPending(false);
    }
  }

  async function handleReviewSubmit(decisions: BroadcastReviewDecision[]) {
    if (
      !fullRunHashValid ||
      reviewSubmitLockRef.current ||
      !reviewEvidence.isReady ||
      !reviewEvidence.readyIdentity?.queueSha256 ||
      reviewEvidence.readyIdentity.queueSha256 !==
        controller.review.data?.queue_sha256
    ) {
      return;
    }
    reviewSubmitLockRef.current = true;
    const hasCandidates = (controller.review.data?.items ?? []).some(
      (item) => (item.candidates ?? []).length > 0,
    );
    const tailRequiresReview =
      controller.review.data?.terminal_tail_review?.status === "required";
    if (tailRequiresReview && !terminalTailConfirmed) return;
    await controller.actions.submitReview(
      decisions,
      hasCandidates || tailRequiresReview ? reviewerId : "",
    );
  }

  async function handleTerminalTailAccept() {
    if (
      !fullRunHashValid ||
      reviewSubmitLockRef.current ||
      !terminalTailConfirmed ||
      !reviewerId.trim()
    ) {
      return;
    }
    reviewSubmitLockRef.current = true;
    await controller.actions.acceptTerminalTailReview(reviewerId);
  }

  async function handleRetryRecompute() {
    if (!fullRunHashValid || recomputeRetryLockRef.current) return;
    recomputeRetryLockRef.current = true;
    await controller.actions.retryRecompute();
  }

  async function handleRender(request: BroadcastRenderRequest) {
    if (!fullRunHashValid || renderLockRef.current) return;
    renderLockRef.current = true;
    renderBaselineOperationIdRef.current = controller.operation?.run_id ?? null;
    renderActiveOperationIdRef.current = null;
    await controller.actions.render(request);
  }

  const controllerOwnsCurrent = Boolean(
    fullRunHashValid &&
    remoteFullRunEnabled &&
    currentRunId &&
    controller.parent?.run_id === currentRunId,
  );
  const workflowState = !fullRunHashValid
    ? null
    : controllerOwnsCurrent
      ? controller.recovery.state === "setup"
        ? (attempt?.last_observed.workflow_state ?? null)
        : controller.recovery.state
      : (attempt?.last_observed.workflow_state ??
        (fullRun?.pending_submission ? "tracking" : null));
  const fullRunStatusText =
    hashGateStatus === "validating" && fullRun
      ? t.production.fullIntegrityValidatingTitle
      : hashGateStatus === "invalid" && fullRun
        ? t.production.fullIntegrityInvalidTitle
        : workflowState
          ? t.production.fullStates[workflowState as ProductionFullRunStatus]
          : t.production.fullNotStarted;
  const active = workflowState === "tracking";
  const fullCancelBusy = fullCancelPending || cancelRun.isPending;
  const retryable = workflowState === "failed" || workflowState === "cancelled";
  const progress = controllerOwnsCurrent
    ? (controller.parent?.progress?.percent ?? 0)
    : (runQuery.data?.progress?.percent ?? 0);
  const reviewResponse = controller.review.localizedData;
  const reviewHasCandidates = (reviewResponse?.items ?? []).some(
    (item) => (item.candidates ?? []).length > 0,
  );
  const terminalTailReview = reviewResponse?.terminal_tail_review;
  const terminalTailRequiresReview = terminalTailReview?.status === "required";
  const terminalTailAccepted = terminalTailReview?.status === "accepted";
  const terminalTailInvalid = terminalTailReview?.status === "invalid";
  const reviewEvidenceQueueMatches = Boolean(
    reviewEvidence.isReady &&
    reviewEvidence.readyIdentity?.queueSha256 &&
    reviewResponse?.queue_sha256 &&
    reviewEvidence.readyIdentity.queueSha256 === reviewResponse.queue_sha256,
  );
  const reviewEvidenceQueueMismatch = Boolean(
    reviewEvidence.isReady &&
    reviewResponse?.status === "ready" &&
    !reviewEvidenceQueueMatches,
  );
  const selectiveReviewCanSubmit = Boolean(
    reviewEvidenceQueueMatches &&
    reviewResponse?.status === "ready" &&
    (reviewHasCandidates ||
      (reviewResponse.review_item_count === 0 &&
        (reviewResponse.items ?? []).length === 0)),
  );
  const terminalTailNeedsReviewer =
    terminalTailRequiresReview ||
    (selectiveReviewCanSubmit && reviewHasCandidates);
  const terminalTailNeedsStandaloneSubmit =
    terminalTailRequiresReview && !selectiveReviewCanSubmit;

  useEffect(() => {
    setTerminalTailConfirmed(false);
  }, [
    currentRunId,
    terminalTailReview?.evidence?.evidence_sha256,
    terminalTailReview?.status,
  ]);
  const controllerActionError = controller.errors.action
    ? (controller.errors.action.message ??
      errorText(
        controller.errors.action.cause ?? controller.errors.action.code,
      ))
    : null;
  const reviewEvidenceError = reviewEvidence.error
    ? errorText(
        reviewEvidence.error.cause ??
          (reviewEvidence.error.kind === "prepare"
            ? t.broadcast.reviewEvidence.prepareFailed
            : reviewEvidence.error.kind === "cancel"
              ? t.broadcast.reviewEvidence.cancelFailed
              : reviewEvidence.error.kind === "reconfirm"
                ? t.broadcast.reviewEvidence.reconfirmFailed
                : t.broadcast.reviewEvidence.loadFailed),
      )
    : null;
  const reviewEvidenceErrorTitle = reviewEvidence.error
    ? reviewEvidence.error.kind === "prepare"
      ? t.broadcast.reviewEvidence.prepareFailed
      : reviewEvidence.error.kind === "cancel"
        ? t.broadcast.reviewEvidence.cancelFailed
        : reviewEvidence.error.kind === "reconfirm"
          ? t.broadcast.reviewEvidence.reconfirmFailed
          : t.broadcast.reviewEvidence.loadFailed
    : null;
  const statusGeneration =
    controller.parent?.broadcast?.status_generation?.trim() ?? "";
  const deliveryKey = `${currentRunId}:${statusGeneration}:${controller.delivery.queryIdentity.scope}:${deliveryRetryNonce}`;

  useEffect(() => {
    if (
      workflowState !== "ready" ||
      !controllerOwnsCurrent ||
      !controller.parent
    ) {
      deliveryValidationRef.current = null;
      deliveryFinalizeRef.current = null;
      setDeliveryState((current) =>
        current.status === "idle" ? current : { status: "idle", key: "" },
      );
      return;
    }
    if (
      attempt?.last_observed.workflow_state !== "ready" ||
      attempt.last_observed.status_generation !== statusGeneration
    ) {
      setDeliveryState({ status: "validating", key: deliveryKey });
      return;
    }
    if (!controller.delivery.queryIdentity.deliveryReady) {
      setDeliveryState({
        status: "blocked",
        key: deliveryKey,
        reasons: ["invalid_status_generation"],
      });
      return;
    }
    if (!controller.delivery.listSucceeded) {
      setDeliveryState(
        controller.errors.query
          ? {
              status: "blocked",
              key: deliveryKey,
              reasons: ["artifact_list_unavailable"],
            }
          : { status: "validating", key: deliveryKey },
      );
      return;
    }
    const artifacts = controller.delivery.listedArtifacts ?? [];
    const urls = {} as Record<ProductionBroadcastDeliveryArtifact, string>;
    for (const name of PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS) {
      const expected = getGetArtifactUrl(controller.parent.run_id, name, {
        status_generation: statusGeneration,
      });
      if (controller.delivery.urls[name] !== expected) {
        setDeliveryState({
          status: "blocked",
          key: deliveryKey,
          reasons: ["artifact_set_mismatch"],
        });
        return;
      }
      urls[name] = expected;
    }
    if (deliveryValidationRef.current === deliveryKey) return;
    deliveryValidationRef.current = deliveryKey;
    deliveryFinalizeRef.current = null;
    setDeliveryState({ status: "validating", key: deliveryKey });
    const abort = new AbortController();
    let settled = false;
    void (async () => {
      try {
        const response = await fetch(urls["broadcast_quality_report.json"], {
          cache: "no-store",
          headers: {
            Accept: "application/json, application/octet-stream",
            "Cache-Control": "no-store",
          },
          signal: abort.signal,
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const qualityBytes = new Uint8Array(await response.arrayBuffer());
        if (
          abort.signal.aborted ||
          deliveryValidationRef.current !== deliveryKey
        ) {
          return;
        }
        const assessment = await assessBroadcastDelivery({
          run: controller.parent!,
          artifacts,
          quality_report_bytes: qualityBytes,
          verified_at: new Date().toISOString(),
        });
        if (
          abort.signal.aborted ||
          deliveryValidationRef.current !== deliveryKey
        ) {
          return;
        }
        settled = true;
        if (assessment.status === "blocked") {
          setDeliveryState({
            status: "blocked",
            key: deliveryKey,
            reasons: assessment.reasons,
          });
          return;
        }
        setVideoChecks({
          key: deliveryKey,
          metadata: false,
          canPlay: false,
        });
        setDeliveryState({
          status: "video",
          key: deliveryKey,
          assessment,
          urls,
        });
      } catch (error) {
        if (abort.signal.aborted) return;
        settled = true;
        setDeliveryState({
          status: "blocked",
          key: deliveryKey,
          reasons: [errorText(error)],
        });
      }
    })();
    return () => {
      abort.abort();
      if (!settled && deliveryValidationRef.current === deliveryKey) {
        deliveryValidationRef.current = null;
      }
    };
  }, [
    attempt?.last_observed.status_generation,
    attempt?.last_observed.workflow_state,
    controller.delivery.listSucceeded,
    controller.delivery.listedArtifacts,
    controller.delivery.queryIdentity.deliveryReady,
    controller.delivery.urls,
    controller.errors.query,
    controller.parent,
    controllerOwnsCurrent,
    deliveryKey,
    statusGeneration,
    workflowState,
  ]);

  useEffect(() => {
    if (
      !fullRunHashValid ||
      deliveryState.status !== "video" ||
      videoChecks.key !== deliveryState.key ||
      !videoChecks.metadata ||
      !videoChecks.canPlay ||
      deliveryFinalizeRef.current === deliveryState.key ||
      !fullRun
    ) {
      return;
    }
    deliveryFinalizeRef.current = deliveryState.key;
    const evidence = deliveryState.assessment.evidence;
    if (
      evidenceMatches(verifiedProduct, evidence) ||
      onVerifiedProduct(evidence, fullRun.revision)
    ) {
      setDeliveryState({ ...deliveryState, status: "ready" });
      return;
    }
    setDeliveryState({
      status: "blocked",
      key: deliveryState.key,
      reasons: [t.production.fullProductStale],
    });
  }, [
    deliveryState,
    fullRun,
    fullRunHashValid,
    onVerifiedProduct,
    t.production.fullProductStale,
    verifiedProduct,
    videoChecks,
  ]);

  function handleDeliveryRetry() {
    if (!fullRunHashValid) return;
    deliveryValidationRef.current = null;
    deliveryFinalizeRef.current = null;
    setDeliveryRetryNonce((value) => value + 1);
    void controller.actions.refresh();
  }

  function markVideoCheck(kind: "metadata" | "canPlay") {
    if (deliveryState.status !== "video") return;
    setVideoChecks((current) => ({
      ...(current.key === deliveryState.key
        ? current
        : { key: deliveryState.key, metadata: false, canPlay: false }),
      [kind]: true,
    }));
  }

  function blockVideo() {
    if (deliveryState.status !== "video") return;
    deliveryFinalizeRef.current = deliveryState.key;
    setDeliveryState({
      status: "blocked",
      key: deliveryState.key,
      reasons: ["broadcast_video_unplayable"],
    });
  }

  return (
    <div className="space-y-4" data-testid="production-full-run-step">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 ref={reconcileHeadingRef} tabIndex={-1} className="font-semibold">
          {t.production.fullRunTitle}
        </h3>
        <Badge
          data-testid="production-full-run-status"
          variant="secondary"
          role="status"
          aria-live={hashGateStatus === "invalid" && fullRun ? "off" : "polite"}
          aria-atomic="true"
        >
          {fullRunStatusText}
        </Badge>
      </div>

      {urlConflict && (
        <Alert variant="destructive" data-testid="production-full-run-error">
          <AlertTitle>{t.production.fullUrlConflictTitle}</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>{t.production.fullUrlConflict}</p>
            {requestedRunId && (
              <Link
                className="font-medium underline"
                href={`/history?run=${encodeURIComponent(requestedRunId)}&from=production`}
              >
                {t.production.fullOpenHistory}
              </Link>
            )}
          </AlertDescription>
        </Alert>
      )}

      {fullRun && hashGateStatus === "validating" && (
        <Alert
          data-testid="production-full-run-integrity-validating"
          role="presentation"
        >
          <AlertTitle>{t.production.fullIntegrityValidatingTitle}</AlertTitle>
          <AlertDescription>
            {t.production.fullIntegrityValidating}
          </AlertDescription>
        </Alert>
      )}

      {fullRun && hashGateStatus === "invalid" && (
        <Alert
          variant="destructive"
          data-testid="production-full-run-integrity-invalid"
        >
          <AlertTitle>{t.production.fullIntegrityInvalidTitle}</AlertTitle>
          <AlertDescription>
            {t.production.fullIntegrityInvalid}
          </AlertDescription>
        </Alert>
      )}

      {message && (
        <Alert variant="destructive" data-testid="production-full-run-error">
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      )}

      {conflictRunId && (
        <Link
          className="text-sm font-medium underline"
          href={`/history?run=${encodeURIComponent(conflictRunId)}&from=production`}
        >
          {t.production.fullOpenHistory}
        </Link>
      )}

      {controllerOwnsCurrent && controller.workflowMessages.length > 0 && (
        <Alert>
          <AlertDescription>
            <ul className="list-disc space-y-1 pl-5">
              {controller.workflowMessages.map((workflowMessage) => (
                <li key={workflowMessage}>{workflowMessage}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      {controllerOwnsCurrent &&
        controllerActionError &&
        workflowState !== "needs_review" &&
        workflowState !== "trajectory_ready" &&
        workflowState !== "rendering" && (
          <Alert variant="destructive" data-testid="production-full-run-error">
            <AlertDescription>{controllerActionError}</AlertDescription>
          </Alert>
        )}

      {routeIdentityAccepted && !urlConflict && !fullRun && (
        <Button
          type="button"
          data-testid="production-start-full-run"
          onClick={() => void handleStart()}
          disabled={createRun.isPending}
        >
          {createRun.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Play className="mr-2 h-4 w-4" aria-hidden="true" />
          )}
          {t.production.fullStart}
        </Button>
      )}

      {routeIdentityAccepted &&
        !urlConflict &&
        fullRunHashValid &&
        fullRun?.pending_submission &&
        !attempt && (
        <Alert>
          <AlertDescription className="space-y-3">
            <p>{t.production.fullSubmissionUncertain}</p>
            <Button
              type="button"
              data-testid="production-retry-full-run"
              variant="outline"
              onClick={() => void handleReconcileAndRetry()}
            >
              <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.fullReconcileRetry}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {routeIdentityAccepted && !urlConflict && active && attempt && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.production.fullTracking}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Progress value={progress} aria-label={t.production.fullProgress} />
            <p className="text-right text-sm">{progress.toFixed(1)}%</p>
            <Button
              ref={cancelButtonRef}
              type="button"
              variant="destructive"
              onClick={() => void handleCancel("full")}
              disabled={fullCancelBusy}
            >
              {fullCancelBusy ? (
                <Loader2
                  className="mr-2 h-4 w-4 animate-spin"
                  aria-hidden="true"
                />
              ) : (
                <Square className="mr-2 h-4 w-4" aria-hidden="true" />
              )}
              {t.production.fullCancel}
            </Button>
          </CardContent>
        </Card>
      )}

      {routeIdentityAccepted && !urlConflict && retryable && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {workflowState === "failed"
                ? t.production.fullFailed
                : t.production.fullCancelled}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Button
              type="button"
              data-testid="production-retry-full-run"
              onClick={() => void handleStart()}
            >
              <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.fullRetry}
            </Button>
          </CardContent>
        </Card>
      )}

      {workflowState === "needs_review" && controllerOwnsCurrent && (
        <div className="space-y-4">
          {controller.review.recomputeRecoveryMode === "auto" &&
            controller.review.recoveryAttemptState !== "failed" && (
              <Card>
                <CardContent className="flex items-center gap-3 py-6 text-sm text-muted-foreground">
                  <Loader2
                    className="h-5 w-5 animate-spin"
                    aria-hidden="true"
                  />
                  {t.broadcast.recoveringDecisionHash}
                </CardContent>
              </Card>
            )}
          {(controller.review.recomputeRecoveryMode === "retry" ||
            controller.review.recoveryAttemptState === "failed") && (
            <Alert>
              <AlertTitle>{t.broadcast.retryRecompute}</AlertTitle>
              <AlertDescription className="space-y-3">
                <p>{t.broadcast.retryRecomputeDescription}</p>
                <Button
                  type="button"
                  onClick={() => void handleRetryRecompute()}
                  disabled={
                    controller.pending.recompute ||
                    controller.review.recoveryAttemptState === "pending"
                  }
                >
                  {controller.pending.recompute ||
                  controller.review.recoveryAttemptState === "pending"
                    ? t.broadcast.retryingRecompute
                    : t.broadcast.retryRecompute}
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {controller.review.recomputeRecoveryMode === "none" && (
            <div className="flex justify-end">
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="production-review-evidence-refresh"
                disabled={
                  controller.pending.recovery ||
                  reviewEvidence.stepProps.isPreparing ||
                  reviewEvidence.stepProps.isCancelling ||
                  reviewEvidence.stepProps.isRetrying
                }
                onClick={() => {
                  void Promise.allSettled([
                    controller.actions.refresh(),
                    reviewEvidence.refresh(),
                  ]);
                }}
              >
                <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
                {t.broadcast.refresh}
              </Button>
            </div>
          )}
          {controller.review.recomputeRecoveryMode === "none" &&
            reviewEvidenceError && (
              <Alert
                variant="destructive"
                data-testid="production-review-evidence-error"
              >
                <AlertTitle>{reviewEvidenceErrorTitle}</AlertTitle>
                <AlertDescription>{reviewEvidenceError}</AlertDescription>
              </Alert>
            )}
          {controller.review.recomputeRecoveryMode === "none" &&
            reviewEvidence.isLoading && (
              <Card>
                <CardContent className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
                  <Loader2
                    className="h-5 w-5 animate-spin"
                    aria-hidden="true"
                  />
                  {t.broadcast.reviewEvidence.loading}
                </CardContent>
              </Card>
            )}
          {controller.review.recomputeRecoveryMode === "none" &&
            !reviewEvidence.isLoading && (
              <BroadcastReviewEvidenceStep
                {...reviewEvidence.stepProps}
                labels={t.broadcast.reviewEvidence}
              />
            )}
          {controller.review.recomputeRecoveryMode === "none" &&
            controller.review.isLoading && (
              <Card>
                <CardContent className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
                  <Loader2
                    className="h-5 w-5 animate-spin"
                    aria-hidden="true"
                  />
                  {t.broadcast.loading}
                </CardContent>
              </Card>
            )}
          {controller.review.recomputeRecoveryMode === "none" &&
            controller.review.isError && (
              <Alert variant="destructive">
                <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
                <AlertDescription>
                  {t.broadcast.evidenceBlocked}
                </AlertDescription>
              </Alert>
            )}
          {controller.review.recomputeRecoveryMode === "none" &&
            reviewResponse && (
              <>
                {reviewEvidenceQueueMismatch && (
                  <Alert
                    variant="destructive"
                    data-testid="production-review-evidence-stale"
                  >
                    <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
                    <AlertDescription>
                      {t.broadcast.staleEvidence}
                    </AlertDescription>
                  </Alert>
                )}
                {reviewResponse.status !== "ready" && reviewResponse.reason && (
                  <Alert>
                    <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
                    <AlertDescription>{reviewResponse.reason}</AlertDescription>
                  </Alert>
                )}
                {selectiveReviewCanSubmit &&
                  controller.montage.messages.length > 0 && (
                    <Alert variant="destructive">
                      <AlertTitle>{t.broadcast.reviewUnavailable}</AlertTitle>
                      <AlertDescription>
                        {controller.montage.messages.join(" ")}
                      </AlertDescription>
                    </Alert>
                  )}
                {terminalTailInvalid && (
                  <Alert
                    variant="destructive"
                    data-testid="production-terminal-tail-invalid"
                  >
                    <AlertTitle>
                      {t.production.fullTerminalTailInvalidTitle}
                    </AlertTitle>
                    <AlertDescription>
                      {terminalTailReview.reason ??
                        t.production.fullTerminalTailInvalid}
                    </AlertDescription>
                  </Alert>
                )}
                {(terminalTailRequiresReview || terminalTailAccepted) &&
                  terminalTailReview.evidence && (
                    <Alert data-testid="production-terminal-tail-review">
                      <AlertTitle>
                        {terminalTailAccepted
                          ? t.production.fullTerminalTailAcceptedTitle
                          : t.production.fullTerminalTailTitle}
                      </AlertTitle>
                      <AlertDescription className="space-y-3">
                        <p>
                          {t.production.fullTerminalTailDescription(
                            terminalTailReview.evidence.reported_frame_count,
                            terminalTailReview.evidence.verified_frame_count,
                            terminalTailReview.evidence.gap_frames,
                            terminalTailReview.evidence.gap_seconds,
                          )}
                        </p>
                        {terminalTailAccepted ? (
                          <p>
                            {t.production.fullTerminalTailAccepted(
                              terminalTailReview.reviewer_id ??
                                t.common.notAvailable,
                            )}
                          </p>
                        ) : (
                          <div className="flex items-start gap-2">
                            <Checkbox
                              id="production-terminal-tail-confirm"
                              checked={terminalTailConfirmed}
                              onCheckedChange={(checked) =>
                                setTerminalTailConfirmed(checked === true)
                              }
                              disabled={controller.pending.review}
                            />
                            <Label htmlFor="production-terminal-tail-confirm">
                              {t.production.fullTerminalTailConfirm}
                            </Label>
                          </div>
                        )}
                      </AlertDescription>
                    </Alert>
                  )}
                {terminalTailNeedsReviewer && (
                  <div className="max-w-sm space-y-2">
                    <Label htmlFor="production-reviewer-id">
                      {t.broadcast.reviewerId}
                    </Label>
                    <Input
                      id="production-reviewer-id"
                      value={reviewerId}
                      onChange={(event) => setReviewerId(event.target.value)}
                      maxLength={200}
                      disabled={controller.pending.review}
                    />
                  </div>
                )}
                {terminalTailNeedsStandaloneSubmit && (
                  <Button
                    type="button"
                    data-testid="production-terminal-tail-submit"
                    onClick={() => void handleTerminalTailAccept()}
                    disabled={
                      controller.pending.review ||
                      !reviewerId.trim() ||
                      !terminalTailConfirmed
                    }
                  >
                    {controller.pending.review
                      ? t.production.fullTerminalTailSubmitting
                      : t.production.fullTerminalTailSubmit}
                  </Button>
                )}
                {selectiveReviewCanSubmit && (
                  <BroadcastReviewStep
                    response={reviewResponse}
                    montageUrlsByCandidateId={
                      controller.montage.urlsByCandidateId
                    }
                    decisions={reviewDecisions}
                    onDecisionsChange={setReviewDecisions}
                    onSubmit={(decisions) => void handleReviewSubmit(decisions)}
                    isSubmitting={controller.pending.review}
                    disabled={
                      controller.montage.messages.length > 0 ||
                      terminalTailInvalid ||
                      (terminalTailNeedsReviewer && !reviewerId.trim()) ||
                      (terminalTailRequiresReview && !terminalTailConfirmed)
                    }
                    error={controllerActionError}
                    labels={t.broadcast.review}
                    noiseSubtypeLabels={t.broadcast.noiseSubtypes}
                  />
                )}
              </>
            )}
        </div>
      )}

      {workflowState === "recomputing" && controllerOwnsCurrent && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.production.fullStates.recomputing}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Progress
              value={controller.operation?.progress?.percent ?? 0}
              aria-label={t.production.fullProgress}
            />
            <p className="text-right text-sm">
              {(controller.operation?.progress?.percent ?? 0).toFixed(1)}%
            </p>
            <Button
              ref={cancelButtonRef}
              type="button"
              variant="destructive"
              onClick={() => void handleCancel("workflow")}
              disabled={controller.pending.cancel}
            >
              <Square className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.fullCancel}
            </Button>
          </CardContent>
        </Card>
      )}

      {(workflowState === "trajectory_ready" ||
        workflowState === "rendering") &&
        controllerOwnsCurrent &&
        controller.parent && (
          <BroadcastRenderStep
            run={controller.parent}
            operationRun={controller.operation}
            trajectoryGenerationId={
              controller.parent.broadcast?.trajectory_generation_id
            }
            artifactUrls={{}}
            onRender={(request) => void handleRender(request)}
            isRendering={
              controller.pending.render || workflowState === "rendering"
            }
            error={controllerActionError}
            onCancel={
              workflowState === "rendering"
                ? () => void handleCancel("workflow")
                : undefined
            }
            cancelButtonRef={cancelButtonRef}
            labels={t.broadcast.render}
            artifactLabels={t.broadcast.artifacts}
          />
        )}

      {workflowState === "ready" &&
        controllerOwnsCurrent &&
        (deliveryState.status === "idle" ||
          deliveryState.status === "validating") && (
          <Card>
            <CardContent className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
              {t.production.fullDeliveryValidating}
            </CardContent>
          </Card>
        )}

      {workflowState === "ready" &&
        controllerOwnsCurrent &&
        deliveryState.status === "blocked" && (
          <Alert
            variant="destructive"
            data-testid="production-delivery-blocked"
          >
            <AlertTitle>{t.production.fullDeliveryBlockedTitle}</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>{t.production.fullDeliveryBlocked}</p>
              <ul className="list-disc space-y-1 pl-5">
                {deliveryState.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
              <Button
                type="button"
                variant="outline"
                onClick={handleDeliveryRetry}
              >
                <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
                {t.production.fullDeliveryRetry}
              </Button>
            </AlertDescription>
          </Alert>
        )}

      {workflowState === "ready" &&
        controllerOwnsCurrent &&
        (deliveryState.status === "video" ||
          deliveryState.status === "ready") && (
          <Card
            data-testid={
              deliveryState.status === "ready"
                ? "production-product-ready"
                : undefined
            }
          >
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                {deliveryState.status === "ready" ? (
                  <CheckCircle2
                    className="h-5 w-5 text-emerald-600"
                    aria-hidden="true"
                  />
                ) : (
                  <Loader2
                    className="h-5 w-5 animate-spin"
                    aria-hidden="true"
                  />
                )}
                {deliveryState.status === "ready"
                  ? t.production.fullProductReady
                  : t.production.fullVideoValidating}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <p className="text-sm font-medium">
                {t.production.fullQualityVerified}
              </p>
              <video
                key={deliveryState.key}
                src={deliveryState.urls["broadcast.mp4"]}
                aria-label={t.broadcast.render.video}
                controls
                preload="metadata"
                className="aspect-video w-full rounded-lg border bg-black"
                onLoadedMetadata={() => markVideoCheck("metadata")}
                onCanPlay={() => markVideoCheck("canPlay")}
                onError={blockVideo}
              />

              <section aria-labelledby="production-product-limitations">
                <h4
                  id="production-product-limitations"
                  className="font-semibold"
                >
                  {t.broadcast.render.limitations}
                </h4>
                {deliveryState.assessment.limitations.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                    {deliveryState.assessment.limitations.map((limitation) => (
                      <li key={limitation}>{limitation}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-muted-foreground">
                    {t.production.fullNoLimitations}
                  </p>
                )}
              </section>

              {deliveryState.status === "ready" && (
                <section aria-labelledby="production-product-downloads">
                  <h4
                    id="production-product-downloads"
                    className="font-semibold"
                  >
                    {t.broadcast.render.downloads}
                  </h4>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.map((name) => (
                      <Button
                        key={name}
                        variant="outline"
                        asChild
                        className="justify-start"
                      >
                        <a href={deliveryState.urls[name]} download={name}>
                          <Download aria-hidden="true" />
                          {t.broadcast.artifacts[name]}
                        </a>
                      </Button>
                    ))}
                  </div>
                </section>
              )}
            </CardContent>
          </Card>
        )}
    </div>
  );
}
