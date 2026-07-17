import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetConfigQueryKey,
  getGetBroadcastReviewEvidenceQueryKey,
  getGetBroadcastReviewWindowsQueryKey,
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  useCancelRun,
  useGetBroadcastReviewEvidence,
  useGetRun,
  useImportBroadcastReviewEvidence,
  useReconfirmBroadcastConfigLineage,
  type BroadcastReviewEvidenceBundleSummary,
  type BroadcastReviewEvidenceStateResponse,
  type RunRecord,
} from "@workspace/api-client-react";

import type {
  BroadcastReviewEvidenceBundleIdentity,
  BroadcastReviewEvidenceCapacity,
  BroadcastConfigLineageReconfirmationIdentity,
  BroadcastConfigLineageReconfirmationState,
  BroadcastReviewEvidenceState,
  BroadcastReviewEvidenceStepProps,
  BroadcastReviewEvidenceStatus,
} from "./BroadcastReviewEvidenceStep";

const ACTIVE_STATUSES = new Set<BroadcastReviewEvidenceStatus>([
  "queued",
  "copying",
  "validating",
  "committing",
]);

const TERMINAL_RETRY_STATUSES = new Set<BroadcastReviewEvidenceStatus>([
  "blocked",
  "failed",
  "cancelled",
]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export type BroadcastReviewEvidenceErrorKind =
  | "load"
  | "prepare"
  | "cancel"
  | "reconfirm";

export interface BroadcastReviewEvidenceControllerError {
  kind: BroadcastReviewEvidenceErrorKind;
  cause: unknown;
}

export interface BroadcastReviewEvidenceReadyIdentity {
  generationId: string | null;
  queueSha256: string | null;
}

export interface BroadcastReviewEvidenceControllerMessages {
  ambiguousBundleRecovery: string;
  insufficientCapacityRecovery: string;
  retryBundleUnavailableRecovery: string;
}

export interface UseBroadcastReviewEvidenceControllerOptions {
  runId: string;
  enabled: boolean;
  messages?: Partial<BroadcastReviewEvidenceControllerMessages>;
  formatRecoveryAction?: (action: string) => string;
}

export interface BroadcastReviewEvidenceHostContract {
  stepProps: Omit<BroadcastReviewEvidenceStepProps, "labels">;
  isLoading: boolean;
  isReady: boolean;
  readyIdentity: BroadcastReviewEvidenceReadyIdentity | null;
  error: BroadcastReviewEvidenceControllerError | null;
  refresh: () => Promise<void>;
}

const DEFAULT_MESSAGES: BroadcastReviewEvidenceControllerMessages = {
  ambiguousBundleRecovery:
    "Keep exactly one compatible bundle in the managed inbox, then refresh.",
  insufficientCapacityRecovery:
    "Free disk space or increase the per-attempt quota before preparing this bundle.",
  retryBundleUnavailableRecovery:
    "Restore the same compatible bundle identity before retrying this import.",
};

export function broadcastReviewEvidencePollingInterval(
  state: Pick<BroadcastReviewEvidenceStateResponse, "status"> | undefined,
): number | false {
  return state && ACTIVE_STATUSES.has(state.status) ? 2_000 : false;
}

function compatibleBundleIdentity(
  bundle: BroadcastReviewEvidenceBundleSummary,
): BroadcastReviewEvidenceBundleIdentity | null {
  const bundleId = bundle.bundle_id?.trim();
  const manifestSha256 = bundle.bundle_manifest_sha256?.trim();
  if (
    bundle.status !== "available" ||
    !bundleId ||
    !manifestSha256 ||
    !SHA256_PATTERN.test(manifestSha256)
  ) {
    return null;
  }
  return { bundleId, manifestSha256 };
}

function bundleCapacity(
  bundle: BroadcastReviewEvidenceBundleSummary | null | undefined,
): BroadcastReviewEvidenceCapacity | null {
  if (
    !bundle ||
    (bundle.total_size_bytes == null &&
      bundle.required_free_bytes == null &&
      bundle.available_free_bytes == null &&
      bundle.attempt_quota_bytes == null &&
      bundle.capacity_status == null)
  ) {
    return null;
  }
  return {
    totalSizeBytes: bundle.total_size_bytes,
    requiredFreeBytes: bundle.required_free_bytes,
    availableFreeBytes: bundle.available_free_bytes,
    attemptQuotaBytes: bundle.attempt_quota_bytes,
    status: bundle.capacity_status,
  };
}

function sameBundleIdentity(
  left: BroadcastReviewEvidenceBundleIdentity,
  right: BroadcastReviewEvidenceBundleIdentity,
): boolean {
  return (
    left.bundleId === right.bundleId &&
    left.manifestSha256 === right.manifestSha256
  );
}

function operationBundleIdentity(
  run: RunRecord | undefined,
): BroadcastReviewEvidenceBundleIdentity | null {
  const bundleId = run?.broadcast?.request?.bundle_id?.trim();
  const manifestSha256 =
    run?.broadcast?.request?.bundle_manifest_sha256?.trim();
  if (!bundleId || !manifestSha256 || !SHA256_PATTERN.test(manifestSha256)) {
    return null;
  }
  return { bundleId, manifestSha256 };
}

function cleanText(value: string | null | undefined): string | null {
  const cleaned = value?.trim();
  return cleaned || null;
}

function configLineageReconfirmationState(
  response: BroadcastReviewEvidenceStateResponse | undefined,
  runId: string,
): BroadcastConfigLineageReconfirmationState | null {
  const challenge = response?.config_lineage_reconfirmation;
  const targetRunId = cleanText(challenge?.target_run_id);
  const confirmedConfigName = cleanText(challenge?.confirmed_config_name);
  const confirmedTextSha256 = cleanText(challenge?.confirmed_text_sha256);
  const expectedObservedRawSha256 = cleanText(
    challenge?.expected_observed_raw_sha256,
  );
  const workflowBindings = challenge?.workflow_bindings;
  if (
    response?.status !== "blocked" ||
    response.blocker_code !==
      "confirmed_config_lineage_reconfirmation_required" ||
    response.recovery_action !== "reconfirm_production_config" ||
    targetRunId !== runId ||
    !confirmedConfigName ||
    !confirmedTextSha256 ||
    !SHA256_PATTERN.test(confirmedTextSha256) ||
    !expectedObservedRawSha256 ||
    !SHA256_PATTERN.test(expectedObservedRawSha256) ||
    typeof workflowBindings !== "object" ||
    workflowBindings === null ||
    Array.isArray(workflowBindings)
  ) {
    return null;
  }
  return {
    targetRunId,
    confirmedConfigName,
    confirmedTextSha256,
    expectedObservedRawSha256,
    workflowBindings,
  };
}

export function useBroadcastReviewEvidenceController({
  runId,
  enabled,
  messages: messageOverrides,
  formatRecoveryAction,
}: UseBroadcastReviewEvidenceControllerOptions): BroadcastReviewEvidenceHostContract {
  const queryClient = useQueryClient();
  const importMutation = useImportBroadcastReviewEvidence();
  const cancelMutation = useCancelRun();
  const reconfirmMutation = useReconfirmBroadcastConfigLineage();
  const mutationInFlightRef = useRef(false);
  const lastBundleRef = useRef<{
    runId: string;
    bundle: BroadcastReviewEvidenceBundleIdentity;
  } | null>(null);
  const readyInvalidationRef = useRef<string | null>(null);

  const evidenceQuery = useGetBroadcastReviewEvidence(runId, {
    query: {
      queryKey: getGetBroadcastReviewEvidenceQueryKey(runId),
      enabled: enabled && Boolean(runId),
      retry: false,
      refetchInterval: (query) =>
        broadcastReviewEvidencePollingInterval(query.state.data),
    },
  });
  const response = evidenceQuery.data;
  const activeJobId = enabled ? cleanText(response?.active_job_id) : null;
  const operationQuery = useGetRun(activeJobId ?? "", {
    query: {
      queryKey: getGetRunQueryKey(activeJobId ?? ""),
      enabled: enabled && Boolean(activeJobId),
      retry: false,
    },
  });

  const messages = { ...DEFAULT_MESSAGES, ...messageOverrides };
  const compatibleBundles = useMemo(
    () =>
      (response?.bundles ?? [])
        .map(compatibleBundleIdentity)
        .filter(
          (bundle): bundle is BroadcastReviewEvidenceBundleIdentity =>
            bundle !== null,
        ),
    [response?.bundles],
  );
  const uniqueCompatibleBundle =
    compatibleBundles.length === 1 ? compatibleBundles[0] : null;
  const childBundle = operationBundleIdentity(operationQuery.data);
  const locallySelectedBundle =
    lastBundleRef.current?.runId === runId
      ? lastBundleRef.current.bundle
      : null;
  const retainedBundle =
    childBundle ??
    locallySelectedBundle ??
    (activeJobId ? null : uniqueCompatibleBundle);
  const retainedBundleIsCompatible = Boolean(
    retainedBundle &&
    compatibleBundles.some((bundle) =>
      sameBundleIdentity(bundle, retainedBundle),
    ),
  );
  const alternativeBundles = retainedBundle
    ? compatibleBundles.filter(
        (bundle) => !sameBundleIdentity(bundle, retainedBundle),
      )
    : [];
  const uniqueAlternativeBundle =
    alternativeBundles.length === 1 ? alternativeBundles[0] : null;
  const uniqueAlternativeBundleSummary = uniqueAlternativeBundle
    ? response?.bundles?.find(
        (bundle) =>
          bundle.bundle_id?.trim() === uniqueAlternativeBundle.bundleId &&
          bundle.bundle_manifest_sha256?.trim() ===
            uniqueAlternativeBundle.manifestSha256,
      )
    : null;
  const uniqueAlternativeCapacity = bundleCapacity(
    uniqueAlternativeBundleSummary,
  );
  const preparableAlternativeBundle =
    uniqueAlternativeCapacity?.status === "insufficient"
      ? null
      : uniqueAlternativeBundle;

  const state = useMemo<BroadcastReviewEvidenceState>(() => {
    let status: BroadcastReviewEvidenceStatus =
      response?.status ?? "not_available";
    let blockerCode =
      cleanText(response?.blocker_code) ?? cleanText(response?.error_code);
    let recoveryAction = cleanText(response?.recovery_action);
    const stateBundle =
      status === "available" ? uniqueCompatibleBundle : retainedBundle;
    const stateBundleSummary = stateBundle
      ? response?.bundles?.find(
          (bundle) =>
            bundle.bundle_id?.trim() === stateBundle.bundleId &&
            bundle.bundle_manifest_sha256?.trim() ===
              stateBundle.manifestSha256,
        )
      : null;
    const capacity = bundleCapacity(stateBundleSummary);

    if (status === "available" && compatibleBundles.length !== 1) {
      if (compatibleBundles.length === 0) {
        status = "not_available";
      } else {
        status = "blocked";
        blockerCode = "ambiguous_compatible_review_evidence_bundles";
        recoveryAction = messages.ambiguousBundleRecovery;
      }
    }

    if (capacity?.status === "insufficient") {
      blockerCode =
        blockerCode ??
        cleanText(stateBundleSummary?.error_code) ??
        "insufficient_capacity";
      recoveryAction = recoveryAction ?? messages.insufficientCapacityRecovery;
    }

    if (
      TERMINAL_RETRY_STATUSES.has(status) &&
      response?.retryable === true &&
      !retainedBundleIsCompatible
    ) {
      recoveryAction = messages.retryBundleUnavailableRecovery;
    }

    const configLineageReconfirmation = configLineageReconfirmationState(
      response,
      runId,
    );
    return {
      status,
      bundle: stateBundle,
      alternativeBundle: TERMINAL_RETRY_STATUSES.has(status)
        ? preparableAlternativeBundle
        : null,
      stage: cleanText(response?.stage),
      progressPercent: response?.progress_percent,
      blockerCode,
      recoveryAction:
        recoveryAction && formatRecoveryAction
          ? formatRecoveryAction(recoveryAction)
          : recoveryAction,
      generationId: cleanText(response?.generation_id),
      queueSha256: cleanText(response?.queue_sha256),
      capacity,
      configLineageReconfirmation,
    };
  }, [
    compatibleBundles.length,
    formatRecoveryAction,
    messages.ambiguousBundleRecovery,
    messages.insufficientCapacityRecovery,
    messages.retryBundleUnavailableRecovery,
    response,
    retainedBundle,
    retainedBundleIsCompatible,
    runId,
    uniqueCompatibleBundle,
    preparableAlternativeBundle,
  ]);

  const invalidateProvisioningQueries = useCallback(
    async (
      operationRunId?: string | null,
      confirmedConfigName?: string | null,
    ) => {
      const invalidations: Promise<unknown>[] = [
        queryClient.invalidateQueries({
          queryKey: getGetBroadcastReviewEvidenceQueryKey(runId),
        }),
        queryClient.invalidateQueries({
          queryKey: getGetBroadcastReviewWindowsQueryKey(runId),
        }),
        queryClient.invalidateQueries({ queryKey: getGetRunQueryKey(runId) }),
        queryClient.invalidateQueries({
          queryKey: getListArtifactsQueryKey(runId),
        }),
      ];
      if (operationRunId) {
        invalidations.push(
          queryClient.invalidateQueries({
            queryKey: getGetRunQueryKey(operationRunId),
          }),
        );
      }
      if (confirmedConfigName) {
        invalidations.push(
          queryClient.invalidateQueries({
            queryKey: getGetConfigQueryKey(confirmedConfigName),
          }),
        );
      }
      await Promise.all(invalidations);
    },
    [queryClient, runId],
  );

  const readyGenerationId =
    state.status === "ready" ? (state.generationId ?? null) : null;
  const readyQueueSha256 =
    state.status === "ready" ? (state.queueSha256 ?? null) : null;
  const readyIdentity =
    enabled &&
    readyGenerationId !== null &&
    readyQueueSha256 !== null &&
    /^[0-9a-f]{64}$/.test(readyQueueSha256)
      ? {
          generationId: readyGenerationId,
          queueSha256: readyQueueSha256,
        }
      : null;
  const isReady = readyIdentity !== null;

  useEffect(() => {
    if (!isReady) return;
    const readyKey = `${runId}:${readyIdentity?.generationId ?? ""}:${readyIdentity?.queueSha256 ?? ""}`;
    if (readyInvalidationRef.current === readyKey) return;
    readyInvalidationRef.current = readyKey;
    void invalidateProvisioningQueries();
  }, [
    invalidateProvisioningQueries,
    isReady,
    readyIdentity?.generationId,
    readyIdentity?.queueSha256,
    runId,
  ]);

  const startImport = useCallback(
    async (
      bundle: BroadcastReviewEvidenceBundleIdentity,
      retryFromJobId?: string,
    ) => {
      if (!enabled || mutationInFlightRef.current) return;
      mutationInFlightRef.current = true;
      lastBundleRef.current = { runId, bundle };
      importMutation.reset();
      cancelMutation.reset();
      try {
        const imported = await importMutation.mutateAsync({
          runId,
          data: {
            bundle_id: bundle.bundleId,
            bundle_manifest_sha256: bundle.manifestSha256,
            ...(retryFromJobId ? { retry_from_job_id: retryFromJobId } : {}),
          },
        });
        await invalidateProvisioningQueries(imported.run_id);
      } catch {
        // The generated mutation retains the error for the host alert.
      } finally {
        mutationInFlightRef.current = false;
      }
    },
    [
      cancelMutation,
      enabled,
      importMutation,
      invalidateProvisioningQueries,
      runId,
    ],
  );

  const cancelImport = useCallback(async () => {
    if (
      !enabled ||
      !activeJobId ||
      state.status === "committing" ||
      mutationInFlightRef.current
    ) {
      return;
    }
    mutationInFlightRef.current = true;
    cancelMutation.reset();
    importMutation.reset();
    try {
      await cancelMutation.mutateAsync({ runId: activeJobId });
      await invalidateProvisioningQueries(activeJobId);
    } catch {
      // The generated mutation retains the error for the host alert.
    } finally {
      mutationInFlightRef.current = false;
    }
  }, [
    activeJobId,
    cancelMutation,
    enabled,
    importMutation,
    invalidateProvisioningQueries,
    state.status,
  ]);

  const reconfirmConfigLineage = useCallback(
    async (identities: BroadcastConfigLineageReconfirmationIdentity) => {
      const challenge = state.configLineageReconfirmation;
      const operatorId = identities.operatorId.trim();
      const reviewerId = identities.reviewerId.trim();
      if (
        !enabled ||
        !challenge ||
        !operatorId ||
        !reviewerId ||
        operatorId !== identities.operatorId ||
        reviewerId !== identities.reviewerId ||
        operatorId === reviewerId ||
        mutationInFlightRef.current
      ) {
        return;
      }
      mutationInFlightRef.current = true;
      reconfirmMutation.reset();
      importMutation.reset();
      cancelMutation.reset();
      try {
        await reconfirmMutation.mutateAsync({
          runId,
          data: {
            target_run_id: challenge.targetRunId,
            confirmed_config_name: challenge.confirmedConfigName,
            confirmed_text_sha256: challenge.confirmedTextSha256,
            expected_observed_raw_sha256: challenge.expectedObservedRawSha256,
            workflow_bindings: challenge.workflowBindings,
            operator_id: operatorId,
            reviewer_id: reviewerId,
          },
        });
        await invalidateProvisioningQueries(
          null,
          challenge.confirmedConfigName,
        );
      } catch {
        // Keep the mutation error and reload the authoritative stable blocker.
        try {
          await invalidateProvisioningQueries(
            null,
            challenge.confirmedConfigName,
          );
        } catch {
          // The original mutation error remains available to the host.
        }
      } finally {
        mutationInFlightRef.current = false;
      }
    },
    [
      cancelMutation,
      enabled,
      importMutation,
      invalidateProvisioningQueries,
      reconfirmMutation,
      runId,
      state.configLineageReconfirmation,
    ],
  );

  const canPrepare =
    state.status === "available" &&
    uniqueCompatibleBundle !== null &&
    state.capacity?.status !== "insufficient";
  const canCancel =
    ACTIVE_STATUSES.has(state.status) &&
    state.status !== "committing" &&
    response?.can_cancel !== false &&
    activeJobId !== null;
  const canRetry =
    TERMINAL_RETRY_STATUSES.has(state.status) &&
    response?.retryable === true &&
    activeJobId !== null &&
    retainedBundle !== null &&
    retainedBundleIsCompatible &&
    state.capacity?.status !== "insufficient";
  const canPrepareAlternative =
    TERMINAL_RETRY_STATUSES.has(state.status) &&
    preparableAlternativeBundle !== null;
  const mutationPending =
    importMutation.isPending ||
    cancelMutation.isPending ||
    reconfirmMutation.isPending;
  const mutationIsRetry = Boolean(
    importMutation.variables?.data.retry_from_job_id,
  );

  let error: BroadcastReviewEvidenceControllerError | null = null;
  if (evidenceQuery.error || operationQuery.error) {
    error = {
      kind: "load",
      cause: evidenceQuery.error ?? operationQuery.error,
    };
  } else if (importMutation.error) {
    error = { kind: "prepare", cause: importMutation.error };
  } else if (cancelMutation.error) {
    error = { kind: "cancel", cause: cancelMutation.error };
  } else if (reconfirmMutation.error) {
    error = { kind: "reconfirm", cause: reconfirmMutation.error };
  }

  return {
    stepProps: {
      state,
      onPrepare: canPrepare
        ? (bundle) => {
            if (sameBundleIdentity(bundle, uniqueCompatibleBundle)) {
              void startImport(bundle);
            }
          }
        : undefined,
      onCancel: canCancel
        ? () => {
            void cancelImport();
          }
        : undefined,
      onRetry: canRetry
        ? () => {
            void startImport(retainedBundle, activeJobId);
          }
        : undefined,
      onPrepareAlternative: canPrepareAlternative
        ? (bundle) => {
            if (sameBundleIdentity(bundle, preparableAlternativeBundle)) {
              void startImport(bundle);
            }
          }
        : undefined,
      onReconfirmConfigLineage: state.configLineageReconfirmation
        ? (identities) => {
            void reconfirmConfigLineage(identities);
          }
        : undefined,
      isPreparing: importMutation.isPending && !mutationIsRetry,
      isCancelling: cancelMutation.isPending,
      isRetrying: importMutation.isPending && mutationIsRetry,
      isReconfirming: reconfirmMutation.isPending,
      disabled: !enabled || mutationPending,
    },
    isLoading: enabled && evidenceQuery.isLoading,
    isReady,
    readyIdentity,
    error,
    refresh: async () => {
      if (!enabled || !runId) return;
      await evidenceQuery.refetch();
    },
  };
}
