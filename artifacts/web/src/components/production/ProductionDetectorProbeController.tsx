import {
  getGetDetectorProbeQueryKey,
  getListDetectorModelsQueryKey,
  useCancelDetectorProbe,
  useCreateDetectorProbe,
  useGetDetectorProbe,
  useListDetectorModels,
} from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  createSafeBrowserStorage,
  type SafeBrowserStorage,
} from "@/lib/browserStorage";
import {
  buildDetectorProbeRequest,
  detectorProbeCatalogView,
  detectorProbeCreateEnvelope,
  detectorProbeJobId,
  detectorProbeJobView,
  detectorProbeStorageKey,
} from "@/lib/productionDetectorProbe";
import { ballAnnotationStorageKey } from "@/lib/productionBallAnnotation";

import { ProductionDetectorProbePanel } from "./ProductionDetectorProbePanel";
import {
  ProductionBallAnnotationController,
  recoverBallAnnotationLaunch,
  type BallAnnotationLaunchRecovery,
} from "./ProductionBallAnnotationController";

const NO_STORE_REQUEST = {
  cache: "no-store" as const,
  headers: { "Cache-Control": "no-store" },
};

const ACTIVE_STATUSES = new Set(["queued", "running", "committing"]);

interface ProductionDetectorProbeControllerProps {
  workflowId: string;
  parentTrialId: string;
  frameIndices?: number[];
  storageFactory?: () => SafeBrowserStorage;
  onStartNewDevelopmentBatch: () => void;
}

interface StoredProbeRecovery {
  state: "job_pointer";
  schema_version: "2.0";
  workflow_id: string;
  parent_trial_id: string;
  job_id: string;
  immutable_identity: string | null;
  expected: {
    request_sha256: string;
    profile_ids: string[];
    frame_indices: number[] | null;
    retry_from_job_id: string | null;
  };
}

type ProbeCreateIntent = ReturnType<typeof buildDetectorProbeRequest>;

interface StoredPendingCreate {
  state: "pending_create";
  schema_version: "1.0";
  artifact_type: "detector_probe_pending_create";
  workflow_id: string;
  parent_trial_id: string;
  request: ProbeCreateIntent;
}

interface ProbeRecoveryState {
  entry: StoredProbeRecovery | null;
  pendingCreate: StoredPendingCreate | null;
  error: string | null;
  pointerPresent: boolean;
}

interface PendingCreateRuntime extends StoredPendingCreate {
  generation: number;
  storageKey: string;
  serialized: string;
}

type RecoveryIssueKind = "invalid_pointer" | "transport" | "integrity";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const PENDING_CREATE_MESSAGE =
  "A detector-probe create request is awaiting an authoritative response. Resume that exact request before any other detector action.";
const PERSISTENT_STORAGE_ERROR =
  "Persistent browser recovery storage is unavailable. No detector-probe job was started.";
const PENDING_READBACK_ERROR =
  "The saved exact create intent could not be re-verified. No new POST was sent; an earlier create result may still be unresolved.";
const INVALID_ANNOTATION_CONTINUATION_ERROR =
  "The saved annotation continuation is invalid. Discard it before retrying or branching this detector-probe lineage.";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function recoveryState(
  value: string | null,
  workflowId: string,
  parentTrialId: string,
): ProbeRecoveryState {
  if (value === null) {
    return {
      entry: null,
      pendingCreate: null,
      error: null,
      pointerPresent: false,
    };
  }
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("stored recovery is not an object");
    }
    const candidate = parsed as Record<string, unknown>;
    if (candidate.state === "pending_create") {
      if (
        candidate.schema_version !== "1.0" ||
        candidate.artifact_type !== "detector_probe_pending_create" ||
        candidate.workflow_id !== workflowId ||
        candidate.parent_trial_id !== parentTrialId ||
        !candidate.request ||
        typeof candidate.request !== "object" ||
        Array.isArray(candidate.request) ||
        !sameValues(Object.keys(candidate).sort(), [
          "artifact_type",
          "parent_trial_id",
          "request",
          "schema_version",
          "state",
          "workflow_id",
        ])
      ) {
        throw new Error("stored pending create schema is invalid");
      }
      const request = candidate.request as Record<string, unknown>;
      if (!Array.isArray(request.profile_ids)) {
        throw new Error("stored pending create profiles are invalid");
      }
      const profileIds = request.profile_ids.map((profileId) => {
        if (typeof profileId !== "string") {
          throw new Error("stored pending create profile is invalid");
        }
        return profileId;
      });
      const frameIndices =
        request.frame_indices === undefined
          ? undefined
          : Array.isArray(request.frame_indices)
            ? request.frame_indices.map((frameIndex) => {
                if (typeof frameIndex !== "number") {
                  throw new Error("stored pending create frame is invalid");
                }
                return frameIndex;
              })
            : (() => {
                throw new Error("stored pending create frame set is invalid");
              })();
      const retryFromJobId =
        request.retry_from_job_id === undefined
          ? undefined
          : detectorProbeJobId({ job_id: request.retry_from_job_id });
      const normalized = buildDetectorProbeRequest({
        parentTrialId,
        profileIds,
        frameIndices,
        retryFromJobId,
      });
      if (
        request.parent_trial_id !== parentTrialId ||
        request.top_k !== 5 ||
        !sameValues(
          Object.keys(request).sort(),
          Object.keys(normalized).sort(),
        ) ||
        JSON.stringify(request) !== JSON.stringify(normalized)
      ) {
        throw new Error("stored pending create request is not canonical");
      }
      return {
        entry: null,
        pendingCreate: {
          state: "pending_create",
          schema_version: "1.0",
          artifact_type: "detector_probe_pending_create",
          workflow_id: workflowId,
          parent_trial_id: parentTrialId,
          request: normalized,
        },
        error: null,
        pointerPresent: true,
      };
    }
    if (
      candidate.state !== "job_pointer" ||
      candidate.schema_version !== "2.0" ||
      candidate.workflow_id !== workflowId ||
      candidate.parent_trial_id !== parentTrialId ||
      !candidate.expected ||
      typeof candidate.expected !== "object" ||
      Array.isArray(candidate.expected) ||
      !sameValues(Object.keys(candidate).sort(), [
        "expected",
        "immutable_identity",
        "job_id",
        "parent_trial_id",
        "schema_version",
        "state",
        "workflow_id",
      ])
    ) {
      throw new Error("stored recovery schema is invalid");
    }
    const expected = candidate.expected as Record<string, unknown>;
    if (
      !sameValues(Object.keys(expected).sort(), [
        "frame_indices",
        "profile_ids",
        "request_sha256",
        "retry_from_job_id",
      ])
    ) {
      throw new Error("stored recovery expected identity is invalid");
    }
    const jobId = detectorProbeJobId({ job_id: candidate.job_id });
    const retryFromJobId =
      expected.retry_from_job_id === null
        ? null
        : detectorProbeJobId({ job_id: expected.retry_from_job_id });
    const frameIndices =
      expected.frame_indices === null
        ? undefined
        : Array.isArray(expected.frame_indices)
          ? expected.frame_indices.map((frameIndex) => {
              if (typeof frameIndex !== "number") {
                throw new Error("stored frame index is invalid");
              }
              return frameIndex;
            })
          : (() => {
              throw new Error("stored frame set is invalid");
            })();
    if (!Array.isArray(expected.profile_ids)) {
      throw new Error("stored profile set is invalid");
    }
    const profileIds = expected.profile_ids.map((profileId) => {
      if (typeof profileId !== "string") {
        throw new Error("stored profile ID is invalid");
      }
      return profileId;
    });
    const normalized = buildDetectorProbeRequest({
      parentTrialId,
      profileIds,
      frameIndices,
      retryFromJobId: retryFromJobId ?? undefined,
    });
    if (
      normalized.profile_ids.some(
        (profileId, index) => profileId !== profileIds[index],
      ) ||
      (frameIndices !== undefined &&
        normalized.frame_indices?.some(
          (frameIndex, index) => frameIndex !== frameIndices[index],
        ))
    ) {
      throw new Error("stored recovery request is not canonical");
    }
    const requestSha256 = expected.request_sha256;
    if (
      typeof requestSha256 !== "string" ||
      !SHA256_PATTERN.test(requestSha256)
    ) {
      throw new Error("stored request digest is invalid");
    }
    const immutableIdentity = candidate.immutable_identity;
    if (
      immutableIdentity !== null &&
      (typeof immutableIdentity !== "string" || immutableIdentity === "")
    ) {
      throw new Error("stored immutable identity is invalid");
    }
    return {
      entry: {
        state: "job_pointer",
        schema_version: "2.0",
        workflow_id: workflowId,
        parent_trial_id: parentTrialId,
        job_id: jobId,
        immutable_identity: immutableIdentity,
        expected: {
          request_sha256: requestSha256,
          profile_ids: normalized.profile_ids,
          frame_indices: normalized.frame_indices ?? null,
          retry_from_job_id: retryFromJobId,
        },
      },
      pendingCreate: null,
      error: null,
      pointerPresent: true,
    };
  } catch {
    return {
      entry: null,
      pendingCreate: null,
      error:
        "The saved detector-probe recovery pointer is invalid. Discard it before starting a new comparison.",
      pointerPresent: true,
    };
  }
}

function sameValues(left: readonly string[], right: readonly string[]) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function sameFrames(left: readonly number[], right: readonly number[]) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

export function ProductionDetectorProbeController(
  props: ProductionDetectorProbeControllerProps,
) {
  const scopeKey = JSON.stringify([props.workflowId, props.parentTrialId]);
  return <ScopedProductionDetectorProbeController key={scopeKey} {...props} />;
}

function ScopedProductionDetectorProbeController({
  workflowId,
  parentTrialId,
  frameIndices,
  storageFactory = createSafeBrowserStorage,
  onStartNewDevelopmentBatch,
}: ProductionDetectorProbeControllerProps) {
  const queryClient = useQueryClient();
  const [storage] = useState(storageFactory);
  const [annotationLaunch, setAnnotationLaunch] =
    useState<BallAnnotationLaunchRecovery | null>(null);
  const [annotationRecoveryError, setAnnotationRecoveryError] = useState<
    string | null
  >(null);
  const storageKey = useMemo(
    () => detectorProbeStorageKey(workflowId, parentTrialId),
    [parentTrialId, workflowId],
  );
  const normalizedFrameIndices = useMemo(
    () =>
      frameIndices === undefined
        ? undefined
        : [...frameIndices].sort((left, right) => left - right),
    [frameIndices],
  );
  const frameIdentity = normalizedFrameIndices?.join(",") ?? "server-selected";
  const [recovery, setRecovery] = useState<ProbeRecoveryState>(() =>
    recoveryState(storage.getItem(storageKey), workflowId, parentTrialId),
  );
  const jobId = recovery.entry?.job_id ?? null;
  const [operationError, setOperationError] = useState<string | null>(() =>
    recovery.pendingCreate
      ? PENDING_CREATE_MESSAGE
      : storage.isPersistent
        ? null
        : PERSISTENT_STORAGE_ERROR,
  );
  const [mutationPending, setMutationPending] = useState(false);
  const mutationInFlightRef = useRef(false);
  const scopeRef = useRef({ storageKey, generation: 0 });
  if (scopeRef.current.storageKey !== storageKey) {
    scopeRef.current = {
      storageKey,
      generation: scopeRef.current.generation + 1,
    };
  }
  const pendingCreateRef = useRef<PendingCreateRuntime | null>(
    recovery.pendingCreate
      ? {
          ...recovery.pendingCreate,
          generation: 0,
          storageKey,
          serialized: JSON.stringify(recovery.pendingCreate),
        }
      : null,
  );
  const immutableIdentityRef = useRef<string | null>(
    recovery.entry?.immutable_identity ?? null,
  );
  const pointerPersistenceBlockedRef = useRef(false);

  useEffect(
    () => () => {
      scopeRef.current = {
        storageKey: scopeRef.current.storageKey,
        generation: scopeRef.current.generation + 1,
      };
    },
    [],
  );

  useEffect(() => {
    const { generation } = scopeRef.current;
    const nextRecovery = recoveryState(
      storage.getItem(storageKey),
      workflowId,
      parentTrialId,
    );
    immutableIdentityRef.current =
      nextRecovery.entry?.immutable_identity ?? null;
    pointerPersistenceBlockedRef.current = false;
    pendingCreateRef.current = nextRecovery.pendingCreate
      ? {
          ...nextRecovery.pendingCreate,
          generation,
          storageKey,
          serialized: JSON.stringify(nextRecovery.pendingCreate),
        }
      : null;
    setAnnotationRecoveryError(null);
    setRecovery(nextRecovery);
    setOperationError(
      nextRecovery.pendingCreate
        ? PENDING_CREATE_MESSAGE
        : storage.isPersistent
          ? null
          : PERSISTENT_STORAGE_ERROR,
    );
  }, [parentTrialId, storage, storageKey, workflowId]);

  const catalogQuery = useListDetectorModels({
    query: {
      queryKey: getListDetectorModelsQueryKey(),
      retry: false,
      staleTime: 0,
    },
    request: NO_STORE_REQUEST,
  });
  const jobQuery = useGetDetectorProbe(jobId ?? "", {
    query: {
      queryKey: getGetDetectorProbeQueryKey(jobId ?? ""),
      enabled: Boolean(jobId),
      retry: false,
      staleTime: 0,
      refetchInterval: (query) =>
        ACTIVE_STATUSES.has(query.state.data?.status ?? "") ? 1_000 : false,
    },
    request: NO_STORE_REQUEST,
  });
  const createMutation = useCreateDetectorProbe({ request: NO_STORE_REQUEST });
  const cancelMutation = useCancelDetectorProbe({ request: NO_STORE_REQUEST });

  const catalog = useMemo(() => {
    if (!catalogQuery.data) return { models: [], error: null };
    try {
      return {
        models: detectorProbeCatalogView(catalogQuery.data),
        error: null,
      };
    } catch (error) {
      return { models: [], error: errorText(error) };
    }
  }, [catalogQuery.data]);

  const mappedJob = useMemo(() => {
    if (!jobQuery.data || !jobId || !recovery.entry) {
      return { job: null, error: null };
    }
    try {
      const job = detectorProbeJobView(jobQuery.data);
      if (job.jobId !== jobId) {
        throw new Error(
          "The detector probe response does not match its requested job.",
        );
      }
      if (job.parentTrialId !== parentTrialId) {
        throw new Error(
          "The detector probe belongs to a different parent trial.",
        );
      }
      const expected = recovery.entry.expected;
      if (
        job.requestSha256 !== expected.request_sha256 ||
        !sameValues(job.selectedProfileIds, expected.profile_ids) ||
        job.retryFromJobId !== expected.retry_from_job_id ||
        (expected.frame_indices !== null &&
          !sameFrames(job.frameIndices, expected.frame_indices))
      ) {
        throw new Error(
          "The detector probe does not match the verified create intent.",
        );
      }
      if (
        immutableIdentityRef.current !== null &&
        immutableIdentityRef.current !== job.immutableIdentity
      ) {
        throw new Error(
          "The detector probe immutable identity changed between polls.",
        );
      }
      immutableIdentityRef.current = job.immutableIdentity;
      return { job, error: null };
    } catch (error) {
      return { job: null, error: errorText(error) };
    }
  }, [frameIdentity, jobId, jobQuery.data, parentTrialId, recovery.entry]);

  useEffect(() => {
    if (
      annotationLaunch ||
      !mappedJob.job ||
      mappedJob.job.status !== "ready"
    ) {
      return;
    }
    try {
      const recovered = recoverBallAnnotationLaunch(storage, workflowId, [
        mappedJob.job.jobId,
      ]);
      if (recovered) {
        if (
          !mappedJob.job.selectedProfileIds.includes(recovered.lockedProfileId)
        ) {
          throw new Error(
            "Saved annotation profile is outside probe authority.",
          );
        }
        setAnnotationLaunch(recovered);
      }
      setAnnotationRecoveryError(null);
    } catch {
      setAnnotationRecoveryError(INVALID_ANNOTATION_CONTINUATION_ERROR);
    }
  }, [annotationLaunch, mappedJob.job, storage, workflowId]);

  useEffect(() => {
    if (
      !mappedJob.job ||
      !recovery.entry ||
      recovery.entry.immutable_identity !== null
    ) {
      return;
    }
    const nextEntry: StoredProbeRecovery = {
      ...recovery.entry,
      immutable_identity: mappedJob.job.immutableIdentity,
    };
    if (!pointerPersistenceBlockedRef.current) {
      storage.setItem(storageKey, JSON.stringify(nextEntry));
    }
    setRecovery({
      entry: nextEntry,
      pendingCreate: null,
      error: null,
      pointerPresent: true,
    });
  }, [mappedJob.job, recovery.entry, storage, storageKey]);

  const catalogError =
    catalog.error ??
    (catalogQuery.isError ? errorText(catalogQuery.error) : null);
  const transportJobError = jobQuery.isError ? errorText(jobQuery.error) : null;
  const recoveryIssue: {
    kind: RecoveryIssueKind;
    message: string;
  } | null = annotationRecoveryError
    ? { kind: "invalid_pointer", message: annotationRecoveryError }
    : recovery.error
      ? { kind: "invalid_pointer", message: recovery.error }
      : mappedJob.error
        ? { kind: "integrity", message: mappedJob.error }
        : transportJobError
          ? { kind: "transport", message: transportJobError }
          : null;
  const catalogState =
    catalogError !== null
      ? "failed"
      : catalogQuery.isPending || !catalogQuery.data
        ? "loading"
        : "ready";
  const actionsBlocked = Boolean(
    annotationLaunch !== null ||
    annotationRecoveryError !== null ||
    !storage.isPersistent ||
    recovery.pendingCreate ||
    pendingCreateRef.current ||
    (recovery.pointerPresent &&
      (!jobId || jobQuery.isPending || jobQuery.isLoading || recoveryIssue)),
  );

  function isCurrentPending(pending: PendingCreateRuntime): boolean {
    return (
      pending.storageKey === scopeRef.current.storageKey &&
      pending.generation === scopeRef.current.generation
    );
  }

  function pendingReadbackIsExact(pending: PendingCreateRuntime): boolean {
    if (!storage.isPersistent || !isCurrentPending(pending)) return false;
    const readback = storage.getItem(pending.storageKey);
    if (!storage.isPersistent || readback !== pending.serialized) return false;
    const parsed = recoveryState(readback, workflowId, parentTrialId);
    return (
      parsed.error === null &&
      parsed.pendingCreate !== null &&
      JSON.stringify(parsed.pendingCreate) ===
        JSON.stringify({
          state: pending.state,
          schema_version: pending.schema_version,
          artifact_type: pending.artifact_type,
          workflow_id: pending.workflow_id,
          parent_trial_id: pending.parent_trial_id,
          request: pending.request,
        })
    );
  }

  function persistPendingCreate(
    request: ProbeCreateIntent,
  ): PendingCreateRuntime | null {
    if (!storage.isPersistent) {
      setOperationError(PERSISTENT_STORAGE_ERROR);
      return null;
    }
    const stored: StoredPendingCreate = {
      state: "pending_create",
      schema_version: "1.0",
      artifact_type: "detector_probe_pending_create",
      workflow_id: workflowId,
      parent_trial_id: parentTrialId,
      request,
    };
    const serialized = JSON.stringify(stored);
    const pending: PendingCreateRuntime = {
      ...stored,
      generation: scopeRef.current.generation,
      storageKey,
      serialized,
    };
    storage.setItem(storageKey, serialized);
    if (!pendingReadbackIsExact(pending)) {
      setOperationError(PERSISTENT_STORAGE_ERROR);
      return null;
    }
    pendingCreateRef.current = pending;
    setRecovery({
      entry: null,
      pendingCreate: stored,
      error: null,
      pointerPresent: true,
    });
    return pending;
  }

  function adoptJob(
    nextEntry: StoredProbeRecovery,
    pending: PendingCreateRuntime,
  ): boolean {
    if (!isCurrentPending(pending)) return false;
    immutableIdentityRef.current = null;
    const serialized = JSON.stringify(nextEntry);
    storage.setItem(storageKey, serialized);
    const readback = storage.getItem(storageKey);
    const parsed = recoveryState(readback, workflowId, parentTrialId);
    const persisted =
      storage.isPersistent &&
      readback === serialized &&
      parsed.error === null &&
      parsed.entry !== null;
    setRecovery({
      entry: nextEntry,
      pendingCreate: null,
      error: null,
      pointerPresent: true,
    });
    if (!persisted) {
      pointerPersistenceBlockedRef.current = true;
      storage.setItem(pending.storageKey, pending.serialized);
      pendingCreateRef.current = null;
      setOperationError(
        "The server reported an exact job, but its durable recovery pointer could not replace the saved create intent. This session will reconcile the returned job identity while preserving that exact intent for recovery.",
      );
      return false;
    }
    pointerPersistenceBlockedRef.current = false;
    pendingCreateRef.current = null;
    setOperationError(null);
    return true;
  }

  async function submitCreate(
    pending: NonNullable<typeof pendingCreateRef.current>,
  ): Promise<void> {
    if (mutationInFlightRef.current) return;
    if (!pendingReadbackIsExact(pending)) {
      setOperationError(PENDING_READBACK_ERROR);
      return;
    }
    mutationInFlightRef.current = true;
    setMutationPending(true);
    setOperationError(null);
    createMutation.reset();
    cancelMutation.reset();
    try {
      const { request } = pending;
      const retryFromJobId = request.retry_from_job_id ?? null;
      const created = await createMutation.mutateAsync({ data: request });
      const envelope = detectorProbeCreateEnvelope(created);
      if (!isCurrentPending(pending)) return;
      const nextJobId = envelope.jobId;
      if (retryFromJobId && nextJobId === retryFromJobId) {
        throw new Error("The detector probe retry did not create a child job.");
      }
      if (envelope.retryFromJobId !== (retryFromJobId ?? null)) {
        throw new Error(
          "The detector probe create response changed the requested retry lineage.",
        );
      }
      adoptJob(
        {
          state: "job_pointer",
          schema_version: "2.0",
          workflow_id: workflowId,
          parent_trial_id: parentTrialId,
          job_id: nextJobId,
          immutable_identity: null,
          expected: {
            request_sha256: envelope.requestSha256,
            profile_ids: request.profile_ids,
            frame_indices: request.frame_indices ?? null,
            retry_from_job_id: retryFromJobId,
          },
        },
        pending,
      );
      await queryClient.invalidateQueries({
        queryKey: getGetDetectorProbeQueryKey(nextJobId),
      });
    } catch (error) {
      if (isCurrentPending(pending)) {
        setOperationError(errorText(error));
      }
    } finally {
      mutationInFlightRef.current = false;
      setMutationPending(false);
    }
  }

  function start(
    profileIds: string[],
    retryFromJobId?: string,
    retryFrameIndices?: number[],
  ): void {
    if (annotationLaunch || annotationRecoveryError) {
      setOperationError(
        annotationRecoveryError ??
          "Detector-probe lineage is locked while its annotation continuation is active. Use the annotation session's server-authorized recovery action instead of creating an ordinary probe retry.",
      );
      return;
    }
    if (
      mutationInFlightRef.current ||
      pendingCreateRef.current ||
      recovery.pendingCreate
    )
      return;
    const request = buildDetectorProbeRequest({
      parentTrialId,
      profileIds,
      frameIndices: retryFrameIndices ?? normalizedFrameIndices,
      retryFromJobId,
    });
    const pending = persistPendingCreate(request);
    if (!pending) return;
    void submitCreate(pending);
  }

  function retrySameCreate(): void {
    const pending = pendingCreateRef.current;
    if (pending) void submitCreate(pending);
  }

  async function cancel(requestedJobId: string): Promise<void> {
    if (
      annotationRecoveryError !== null ||
      mutationInFlightRef.current ||
      pendingCreateRef.current !== null ||
      requestedJobId !== jobId ||
      mappedJob.job?.jobId !== requestedJobId ||
      mappedJob.job.status === "committing"
    ) {
      return;
    }
    mutationInFlightRef.current = true;
    setMutationPending(true);
    setOperationError(null);
    cancelMutation.reset();
    createMutation.reset();
    try {
      const cancelled = await cancelMutation.mutateAsync({
        jobId: requestedJobId,
      });
      const cancelledJobId = detectorProbeJobId(cancelled);
      if (cancelledJobId !== requestedJobId) {
        throw new Error(
          "The cancelled detector probe response has a different job identity.",
        );
      }
      queryClient.setQueryData(
        getGetDetectorProbeQueryKey(requestedJobId),
        cancelled,
      );
      await queryClient.invalidateQueries({
        queryKey: getGetDetectorProbeQueryKey(requestedJobId),
      });
    } catch (error) {
      setOperationError(errorText(error));
    } finally {
      mutationInFlightRef.current = false;
      setMutationPending(false);
    }
  }

  function retry(request: {
    retryFromJobId: string;
    profileIds: string[];
  }): void {
    if (annotationLaunch || annotationRecoveryError) {
      setOperationError(
        annotationRecoveryError ??
          "Detector-probe lineage is locked while its annotation continuation is active. Ordinary retry cannot replace that lineage.",
      );
      return;
    }
    if (
      pendingCreateRef.current !== null ||
      request.retryFromJobId !== jobId ||
      mappedJob.job?.jobId !== request.retryFromJobId ||
      ACTIVE_STATUSES.has(mappedJob.job.status)
    ) {
      setOperationError(
        "The detector probe retry no longer matches the current terminal job.",
      );
      return;
    }
    const canonicalProfiles = [...request.profileIds].sort();
    if (!sameValues(canonicalProfiles, mappedJob.job.selectedProfileIds)) {
      setOperationError(
        "An explicit retry must keep the exact frozen profile set. Start a new root comparison for a changed selection.",
      );
      return;
    }
    start(
      mappedJob.job.selectedProfileIds,
      request.retryFromJobId,
      mappedJob.job.frameIndices,
    );
  }

  function discardRecovery(): void {
    if (annotationRecoveryError !== null && jobId !== null) {
      const annotationKey = ballAnnotationStorageKey(workflowId, jobId);
      storage.removeItem(annotationKey);
      if (storage.getItem(annotationKey) !== null) {
        setOperationError(
          "The invalid annotation continuation could not be removed from durable storage. Detector-probe actions remain blocked.",
        );
        return;
      }
      setAnnotationRecoveryError(null);
      setOperationError(null);
      return;
    }
    if (recovery.error === null) return;
    storage.removeItem(storageKey);
    immutableIdentityRef.current = null;
    pointerPersistenceBlockedRef.current = false;
    pendingCreateRef.current = null;
    setRecovery({
      entry: null,
      pendingCreate: null,
      error: null,
      pointerPresent: false,
    });
    setOperationError(null);
  }

  return (
    <div className="min-w-0 space-y-6">
      <ProductionDetectorProbePanel
        models={catalog.models}
        catalogState={catalogState}
        catalogError={catalogError}
        operationError={operationError}
        recoveryError={recoveryIssue?.message ?? null}
        recoveryErrorKind={recoveryIssue?.kind ?? null}
        exactCreatePending={pendingCreateRef.current !== null}
        job={mappedJob.job}
        mutationPending={
          mutationPending ||
          createMutation.isPending ||
          cancelMutation.isPending
        }
        actionsBlocked={actionsBlocked}
        lineageLocked={annotationLaunch !== null}
        onStart={start}
        onCancel={(requestedJobId) => void cancel(requestedJobId)}
        onRetry={retry}
        onDiscardRecovery={discardRecovery}
        onRefreshRecovery={() => void jobQuery.refetch()}
        onReloadCatalog={() => void catalogQuery.refetch()}
        onRetryCreate={pendingCreateRef.current ? retrySameCreate : undefined}
        onStartDevelopmentAnnotation={(developmentJobId, profileId) =>
          annotationRecoveryError === null &&
          setAnnotationLaunch({
            developmentProbeJobIds: [developmentJobId],
            lockedProfileId: profileId,
          })
        }
      />
      {annotationLaunch && (
        <ProductionBallAnnotationController
          workflowId={workflowId}
          developmentProbeJobIds={annotationLaunch.developmentProbeJobIds}
          lockedProfileId={annotationLaunch.lockedProfileId}
          storage={storage}
          onStartNewDevelopmentBatch={onStartNewDevelopmentBatch}
        />
      )}
    </div>
  );
}
