import { useEffect, useMemo, useRef, useState, type Ref } from "react";
import {
  getGetArtifactQueryKey,
  getGetArtifactUrl,
  getGetBallAuditReportQueryKey,
  getGetConfigQueryKey,
  getGetConfigQueryOptions,
  getGetHealthQueryKey,
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  getListRunsQueryKey,
  useCancelRun,
  useCreateRun,
  useDeriveConfig,
  useGetArtifact,
  useGetBallAuditReport,
  useGetConfig,
  useGetHealth,
  useGetRun,
  useListArtifacts,
  useListConfigs,
  useListRuns,
  type RunRecord,
} from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import {
  ExternalLink,
  Loader2,
  Lock,
  Play,
  RotateCcw,
  Square,
} from "lucide-react";

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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { useLanguage } from "@/contexts/LanguageContext";
import {
  buildProductionConfigConfirmation,
  expectedProductionConfigName,
  finalizeProductionConfigConfirmation,
  verifyProductionConfigDetail,
  type ProductionConfigEvidence,
  type ProductionConfigVerification,
  type ProductionPendingConfigConfirmation,
} from "@/lib/productionConfigFreeze";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import {
  acceptProductionTrial,
  appendProductionTrialAttempt,
  assessProductionTrialEvidence,
  buildProductionTrialIntent,
  buildProductionTrialSubmission,
  canonicalJson,
  createProductionTrialState,
  invalidateProductionTrialAcceptance,
  nextProductionTrialGeneration,
  observeProductionTrialRun,
  productionTrialArtifactContract,
  productionTrialEvidenceGeneration,
  productionTrialEvidenceSnapshotIdentity,
  reconcilePendingProductionTrial,
  selectProductionTrialVideo,
  sha256Text,
  type ProductionTrialReadinessSummary,
  type ProductionTrialSettings,
  type ProductionTrialState,
} from "@/lib/productionTrial";
import type { SourceSignature } from "@/lib/productionWorkflow";

const NO_STORE_REQUEST = {
  cache: "no-store" as const,
  headers: { "Cache-Control": "no-store" },
};

const NO_STORE_TEXT_REQUEST = {
  ...NO_STORE_REQUEST,
  responseType: "text" as const,
};

interface ProductionTrialStepProps {
  workflowId: string;
  source: SourceSignature;
  calibration: ProductionCalibrationDraft;
  trial: ProductionTrialState | null;
  pendingConfig: ProductionPendingConfigConfirmation | null;
  confirmedConfig: ProductionConfigEvidence | null;
  onTrialChange: (
    trial: ProductionTrialState,
    expected: ProductionTrialState | null,
  ) => boolean;
  onPendingConfigChange: (
    pending: ProductionPendingConfigConfirmation | null,
    expected: ProductionPendingConfigConfirmation | null,
    expectedAcceptedRunId: string,
  ) => boolean;
  onConfirmedConfigChange: (
    confirmed: ProductionConfigEvidence,
    expectedPending: ProductionPendingConfigConfirmation,
  ) => boolean;
  onUsabilityChange: (usable: boolean) => void;
  stopButtonRef?: Ref<HTMLButtonElement>;
}

function errorStatus(error: unknown): number | null {
  return typeof error === "object" && error !== null && "status" in error
    ? Number((error as { status?: unknown }).status) || null
    : null;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parseStrictInteger(value: string, minimum: number): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum ? parsed : null;
}

function sameTrial(
  left: ProductionTrialState,
  right: ProductionTrialState,
): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function sameSnapshot(left: unknown, right: unknown): boolean {
  try {
    return canonicalJson(left) === canonicalJson(right);
  } catch {
    return false;
  }
}

export function ProductionTrialStep({
  workflowId,
  source,
  calibration,
  trial,
  pendingConfig,
  confirmedConfig,
  onTrialChange,
  onPendingConfigChange,
  onConfirmedConfigChange,
  onUsabilityChange,
  stopButtonRef,
}: ProductionTrialStepProps) {
  const { t } = useLanguage();
  const configs = useListConfigs();
  const health = useGetHealth({
    query: { queryKey: getGetHealthQueryKey(), refetchInterval: 3_000 },
  });
  const runs = useListRuns({
    query: { queryKey: getListRunsQueryKey(), refetchInterval: 3_000 },
  });
  const createRun = useCreateRun();
  const cancelRun = useCancelRun();
  const deriveConfig = useDeriveConfig();
  const queryClient = useQueryClient();

  const initialSettings = trial?.settings ?? {
    base_config_name: "",
    start_frame: 0,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: {},
  };
  const [baseConfig, setBaseConfig] = useState(
    initialSettings.base_config_name,
  );
  const [startFrame, setStartFrame] = useState(
    String(initialSettings.start_frame),
  );
  const [maxFrames, setMaxFrames] = useState(
    String(initialSettings.max_frames),
  );
  const [postprocess, setPostprocess] = useState(
    initialSettings.enable_postprocess,
  );
  const [followCam, setFollowCam] = useState(initialSettings.enable_follow_cam);
  const [message, setMessage] = useState<string | null>(null);
  const [conflictRunId, setConflictRunId] = useState<string | null>(null);
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [videoMetadataLoaded, setVideoMetadataLoaded] = useState(false);
  const [videoCanPlay, setVideoCanPlay] = useState(false);
  const [videoMediaMetadata, setVideoMediaMetadata] = useState<{
    duration: number | null;
    width: number;
    height: number;
  } | null>(null);
  const [readiness, setReadiness] =
    useState<ProductionTrialReadinessSummary | null>(null);
  const [readinessFingerprint, setReadinessFingerprint] = useState<
    string | null
  >(null);
  const [configVerification, setConfigVerification] =
    useState<ProductionConfigVerification | null>(null);
  const [configDeriveActive, setConfigDeriveActive] = useState(false);
  const [acceptRefreshing, setAcceptRefreshing] = useState(false);
  const startInFlightRef = useRef(false);
  const cancelInFlightRef = useRef(false);
  const acceptInFlightRef = useRef(false);
  const configInFlightRef = useRef(false);
  const submissionGenerationRef = useRef(
    nextProductionTrialGeneration(trial) - 1,
  );
  const configGenerationRef = useRef(pendingConfig?.generation ?? 0);
  const evidenceGenerationRef = useRef(0);
  const configVerificationGenerationRef = useRef(0);
  const currentEvidenceFingerprintRef = useRef<string | null>(null);
  const latestTrialRef = useRef(trial);
  const latestPendingConfigRef = useRef(pendingConfig);
  const latestConfirmedConfigRef = useRef(confirmedConfig);
  const unlockButtonRef = useRef<HTMLButtonElement>(null);
  const operationContext = canonicalJson({ workflowId, source, calibration });
  const operationContextRef = useRef(operationContext);

  operationContextRef.current = operationContext;
  latestTrialRef.current = trial;
  latestPendingConfigRef.current = pendingConfig;
  latestConfirmedConfigRef.current = confirmedConfig;

  useEffect(() => {
    if (!baseConfig && configs.data?.[0]?.name) {
      setBaseConfig(configs.data[0].name);
    }
  }, [baseConfig, configs.data]);

  useEffect(() => {
    if (
      trial &&
      !trial.pending_submission &&
      !trial.active_run_id &&
      trial.attempts.at(-1)?.last_observed.status !== "queued" &&
      trial.attempts.at(-1)?.last_observed.status !== "running"
    ) {
      startInFlightRef.current = false;
    }
  }, [trial]);

  const latestAttempt = trial?.attempts.at(-1) ?? null;
  const monitoredRunId = trial?.active_run_id ?? latestAttempt?.run_id ?? "";
  const runQuery = useGetRun(monitoredRunId, {
    query: {
      queryKey: getGetRunQueryKey(monitoredRunId),
      enabled: Boolean(monitoredRunId),
      refetchInterval: (query) => {
        const status = query.state.data?.status;
        return status === "queued" || status === "running" ? 2_000 : false;
      },
    },
  });
  const run = runQuery.data ?? null;
  const evidenceEnabled = run?.status === "completed";
  const artifactsQuery = useListArtifacts(monitoredRunId, undefined, {
    query: {
      queryKey: getListArtifactsQueryKey(monitoredRunId),
      enabled: evidenceEnabled,
      staleTime: 0,
    },
    request: NO_STORE_REQUEST,
  });
  const manifestQuery = useGetArtifact(
    monitoredRunId,
    "run_manifest.json",
    undefined,
    {
      query: {
        queryKey: getGetArtifactQueryKey(monitoredRunId, "run_manifest.json"),
        enabled: evidenceEnabled,
        staleTime: 0,
      },
      request: NO_STORE_REQUEST,
    },
  );
  const metricsQuery = useGetArtifact(
    monitoredRunId,
    "metrics_report.json",
    undefined,
    {
      query: {
        queryKey: getGetArtifactQueryKey(monitoredRunId, "metrics_report.json"),
        enabled: evidenceEnabled,
        staleTime: 0,
      },
      request: NO_STORE_REQUEST,
    },
  );
  const rawTrackQuery = useGetArtifact(
    monitoredRunId,
    "ball_track.csv",
    undefined,
    {
      query: {
        queryKey: getGetArtifactQueryKey(monitoredRunId, "ball_track.csv"),
        enabled: evidenceEnabled,
        staleTime: 0,
      },
      request: NO_STORE_TEXT_REQUEST,
    },
  );
  const cleanedTrackQuery = useGetArtifact(
    monitoredRunId,
    "ball_track.cleaned.csv",
    undefined,
    {
      query: {
        queryKey: getGetArtifactQueryKey(
          monitoredRunId,
          "ball_track.cleaned.csv",
        ),
        enabled:
          evidenceEnabled && Boolean(latestAttempt?.request.enable_postprocess),
        staleTime: 0,
      },
      request: NO_STORE_TEXT_REQUEST,
    },
  );
  const auditQuery = useGetBallAuditReport(monitoredRunId, {
    query: {
      queryKey: getGetBallAuditReportQueryKey(monitoredRunId),
      enabled: evidenceEnabled,
      staleTime: 0,
    },
  });

  const configName =
    confirmedConfig?.name ??
    (pendingConfig
      ? expectedProductionConfigName(pendingConfig.output_name)
      : "");
  const configQuery = useGetConfig(configName, {
    query: {
      queryKey: getGetConfigQueryKey(configName),
      enabled: Boolean(configName) && !configDeriveActive,
      staleTime: 0,
      retry: false,
    },
  });

  useEffect(() => {
    if (!trial?.pending_submission || !runs.data) return;
    const reconciled = reconcilePendingProductionTrial(trial, {
      workflow_id: workflowId,
      expected_generation: trial.pending_submission.generation,
      runs: runs.data,
      observed_at: new Date().toISOString(),
    });
    if (!sameTrial(reconciled, trial)) onTrialChange(reconciled, trial);
  }, [onTrialChange, runs.data, trial, workflowId]);

  useEffect(() => {
    if (!run || !trial) return;
    const attempt = trial.attempts.find((item) => item.run_id === run.run_id);
    if (!attempt || attempt.last_observed.status === run.status) return;
    const next = observeProductionTrialRun(trial, {
      run_id: run.run_id,
      status: run.status,
      observed_at: new Date().toISOString(),
    });
    if (!sameTrial(next, trial)) onTrialChange(next, trial);
  }, [onTrialChange, run, trial]);

  const selectedVideo = useMemo(
    () =>
      selectProductionTrialVideo(
        artifactsQuery.data ?? [],
        Boolean(latestAttempt?.request.enable_follow_cam),
      ),
    [artifactsQuery.data, latestAttempt?.request.enable_follow_cam],
  );

  useEffect(() => {
    setVideoMetadataLoaded(false);
    setVideoCanPlay(false);
    setVideoMediaMetadata(null);
  }, [
    monitoredRunId,
    selectedVideo?.name,
    selectedVideo?.path,
    selectedVideo?.size_bytes,
    selectedVideo?.content_type,
  ]);

  const readableArtifactNames = useMemo(() => {
    const names: string[] = [];
    if (
      manifestQuery.data &&
      typeof manifestQuery.data === "object" &&
      !Array.isArray(manifestQuery.data)
    )
      names.push("run_manifest.json");
    if (
      metricsQuery.data &&
      typeof metricsQuery.data === "object" &&
      !Array.isArray(metricsQuery.data)
    )
      names.push("metrics_report.json");
    if (typeof rawTrackQuery.data === "string" && rawTrackQuery.data.trim()) {
      names.push("ball_track.csv");
    }
    if (auditQuery.data) names.push("ball_audit.json");
    if (
      typeof cleanedTrackQuery.data === "string" &&
      cleanedTrackQuery.data.trim()
    )
      names.push("ball_track.cleaned.csv");
    return names;
  }, [
    auditQuery.data,
    cleanedTrackQuery.data,
    manifestQuery.data,
    metricsQuery.data,
    rawTrackQuery.data,
  ]);

  const evidence = useMemo(() => {
    if (!run || !latestAttempt) return null;
    return assessProductionTrialEvidence({
      run,
      artifacts: artifactsQuery.data ?? [],
      manifest: manifestQuery.data,
      metrics: metricsQuery.data,
      audit: auditQuery.data ?? null,
      raw_csv: rawTrackQuery.data,
      cleaned_csv: cleanedTrackQuery.data,
      readable_artifact_names: readableArtifactNames,
      enable_postprocess: Boolean(latestAttempt.request.enable_postprocess),
      enable_follow_cam: Boolean(latestAttempt.request.enable_follow_cam),
      video_loaded: videoMetadataLoaded && videoCanPlay,
    });
  }, [
    artifactsQuery.data,
    auditQuery.data,
    latestAttempt,
    manifestQuery.data,
    metricsQuery.data,
    rawTrackQuery.data,
    cleanedTrackQuery.data,
    readableArtifactNames,
    run,
    videoCanPlay,
    videoMetadataLoaded,
  ]);

  const currentEvidenceFingerprint = useMemo(() => {
    if (
      !evidence?.ready ||
      !latestAttempt ||
      !run ||
      !videoMediaMetadata ||
      typeof rawTrackQuery.data !== "string" ||
      (latestAttempt.request.enable_postprocess &&
        typeof cleanedTrackQuery.data !== "string") ||
      !auditQuery.data
    ) {
      return null;
    }
    try {
      return productionTrialEvidenceSnapshotIdentity({
        run_id: run.run_id,
        intent_sha256: latestAttempt.intent_sha256,
        request_sha256: latestAttempt.request_sha256,
        query_revisions: {
          run: runQuery.dataUpdatedAt,
          artifacts: artifactsQuery.dataUpdatedAt,
          manifest: manifestQuery.dataUpdatedAt,
          metrics: metricsQuery.dataUpdatedAt,
          audit: auditQuery.dataUpdatedAt,
          raw_csv: rawTrackQuery.dataUpdatedAt,
          cleaned_csv: latestAttempt.request.enable_postprocess
            ? cleanedTrackQuery.dataUpdatedAt
            : null,
        },
        raw_csv_length: rawTrackQuery.data.length,
        cleaned_csv_length: latestAttempt.request.enable_postprocess
          ? String(cleanedTrackQuery.data).length
          : null,
        selected_video: {
          name: evidence.video.name,
          path: evidence.video.path,
          size_bytes: evidence.video.size_bytes ?? null,
        },
        video_metadata: videoMediaMetadata,
      });
    } catch {
      return null;
    }
  }, [
    artifactsQuery.data,
    auditQuery.data,
    cleanedTrackQuery.data,
    evidence,
    latestAttempt,
    manifestQuery.data,
    metricsQuery.data,
    metricsQuery.dataUpdatedAt,
    rawTrackQuery.data,
    rawTrackQuery.dataUpdatedAt,
    run,
    runQuery.dataUpdatedAt,
    videoMediaMetadata,
    artifactsQuery.dataUpdatedAt,
    auditQuery.dataUpdatedAt,
    cleanedTrackQuery.dataUpdatedAt,
    manifestQuery.dataUpdatedAt,
  ]);
  currentEvidenceFingerprintRef.current = currentEvidenceFingerprint;

  useEffect(() => {
    const generation = ++evidenceGenerationRef.current;
    setReadiness(null);
    setReadinessFingerprint(null);
    if (
      !run ||
      !latestAttempt ||
      !evidence?.ready ||
      !currentEvidenceFingerprint ||
      !videoMediaMetadata ||
      !auditQuery.data ||
      typeof rawTrackQuery.data !== "string"
    ) {
      return;
    }
    const fingerprint = currentEvidenceFingerprint;
    void productionTrialEvidenceGeneration({
      run_id: run.run_id,
      intent_sha256: latestAttempt.intent_sha256,
      request_sha256: latestAttempt.request_sha256,
      artifacts: artifactsQuery.data ?? [],
      stats: run.stats ?? null,
      manifest: manifestQuery.data,
      metrics: metricsQuery.data,
      audit: auditQuery.data,
      raw_csv: rawTrackQuery.data,
      cleaned_csv: latestAttempt.request.enable_postprocess
        ? String(cleanedTrackQuery.data)
        : null,
      selected_video: evidence.video,
      video_metadata: videoMediaMetadata,
    }).then((evidenceGeneration) => {
      if (
        generation !== evidenceGenerationRef.current ||
        currentEvidenceFingerprintRef.current !== fingerprint
      )
        return;
      setReadiness({
        run_id: run.run_id,
        request_sha256: latestAttempt.request_sha256,
        evidence_generation: evidenceGeneration,
        verified_at: new Date().toISOString(),
        video_artifact_name: evidence.video.name,
        artifact_names: productionTrialArtifactContract({
          enable_postprocess: Boolean(latestAttempt.request.enable_postprocess),
          video_artifact_name: evidence.video.name,
        }).required_names,
        quality: evidence.quality,
      });
      setReadinessFingerprint(fingerprint);
    });
  }, [
    artifactsQuery.data,
    auditQuery.data,
    cleanedTrackQuery.data,
    currentEvidenceFingerprint,
    evidence,
    latestAttempt,
    manifestQuery.data,
    metricsQuery.data,
    rawTrackQuery.data,
    run,
    videoMediaMetadata,
  ]);

  useEffect(() => {
    const generation = ++configVerificationGenerationRef.current;
    onUsabilityChange(false);
    if (confirmedConfig) {
      if (configQuery.isPending) {
        setConfigVerification(null);
        return;
      }
      if (configQuery.isError) {
        setConfigVerification({
          status:
            errorStatus(configQuery.error) === 404 ? "missing" : "unverifiable",
        });
        return;
      }
      if (!configQuery.data) return;
      void verifyProductionConfigDetail(confirmedConfig, configQuery.data).then(
        (result) => {
          if (generation !== configVerificationGenerationRef.current) return;
          setConfigVerification(result);
          onUsabilityChange(result.status === "verified");
        },
      );
      return;
    }
    setConfigVerification(null);
  }, [
    configQuery.data,
    configQuery.error,
    configQuery.isError,
    configQuery.isPending,
    confirmedConfig,
    onUsabilityChange,
  ]);

  useEffect(() => {
    if (!pendingConfig || confirmedConfig || !configQuery.data) return;
    const expectedGeneration = pendingConfig.generation;
    void finalizeProductionConfigConfirmation(pendingConfig, configQuery.data)
      .then((confirmed) => {
        if (
          latestPendingConfigRef.current?.generation !== expectedGeneration ||
          latestConfirmedConfigRef.current
        )
          return;
        onConfirmedConfigChange(confirmed, pendingConfig);
      })
      .catch(() => {
        // A pending result is intentionally left for explicit re-confirmation.
      });
  }, [
    configQuery.data,
    confirmedConfig,
    onConfirmedConfigChange,
    pendingConfig,
  ]);

  function currentSettings(): ProductionTrialSettings | null {
    const parsedStart = parseStrictInteger(startFrame, 0);
    const parsedMax = parseStrictInteger(maxFrames, 1);
    if (!baseConfig || parsedStart === null || parsedMax === null) return null;
    return {
      base_config_name: baseConfig,
      start_frame: parsedStart,
      max_frames: parsedMax,
      enable_postprocess: postprocess,
      enable_follow_cam: followCam,
      tuning_patch: trial?.settings.tuning_patch ?? {},
    };
  }

  function updateSettings(change: Partial<ProductionTrialSettings>) {
    const current = latestTrialRef.current;
    if (!current || current.accepted || current.active_run_id) return;
    onTrialChange(
      {
        ...current,
        settings: { ...current.settings, ...change },
        accepted: null,
      },
      current,
    );
  }

  async function handleStartTrial() {
    if (startInFlightRef.current) return;
    startInFlightRef.current = true;
    setMessage(null);
    setConflictRunId(null);
    let persistedSubmission = false;
    const expectedContext = operationContextRef.current;
    try {
      const settings = currentSettings();
      if (!settings) {
        setMessage(t.production.trialInvalidFrameRange);
        return;
      }
      const expectedTrial = latestTrialRef.current;
      const current = expectedTrial ?? createProductionTrialState(settings);
      const generation = Math.max(
        ++submissionGenerationRef.current,
        nextProductionTrialGeneration(current),
      );
      submissionGenerationRef.current = generation;
      const submission = await buildProductionTrialSubmission({
        workflow_id: workflowId,
        source,
        calibration,
        settings,
        parent_run_id: current.attempts.at(-1)?.run_id ?? null,
        submission_id: crypto.randomUUID(),
        output_id: crypto.randomUUID(),
        generation,
        created_at: new Date().toISOString(),
      });
      if (operationContextRef.current !== expectedContext) return;
      const pendingState: ProductionTrialState = {
        ...current,
        settings,
        pending_submission: submission.pending,
        accepted: null,
      };
      if (!onTrialChange(pendingState, expectedTrial)) return;
      persistedSubmission = true;
      latestTrialRef.current = pendingState;

      const healthResult = await health.refetch();
      if (operationContextRef.current !== expectedContext) return;
      if (
        healthResult.isError ||
        !healthResult.data ||
        healthResult.data.status !== "ok"
      ) {
        setMessage(t.production.trialHealthUnavailable);
        return;
      }
      const activeRunId = healthResult.data?.active_run_id ?? null;
      if (activeRunId) {
        setConflictRunId(activeRunId);
        setMessage(t.production.trialActiveConflict(activeRunId));
        return;
      }
      try {
        const created = await createRun.mutateAsync({
          data: submission.pending.request,
        });
        if (operationContextRef.current !== expectedContext) return;
        if (
          latestTrialRef.current?.pending_submission?.submission_id !==
          submission.pending.submission_id
        )
          return;
        const next = appendProductionTrialAttempt(latestTrialRef.current, {
          run: created,
          pending: submission.pending,
          observed_at: new Date().toISOString(),
        });
        onTrialChange(next, pendingState);
      } catch (error) {
        if (errorStatus(error) === 409) {
          const [healthAfter, runsAfter] = await Promise.all([
            health.refetch(),
            runs.refetch(),
          ]);
          if (operationContextRef.current !== expectedContext) return;
          const currentState = latestTrialRef.current;
          if (currentState) {
            const reconciled = reconcilePendingProductionTrial(currentState, {
              workflow_id: workflowId,
              expected_generation: generation,
              runs: runsAfter.data ?? [],
              observed_at: new Date().toISOString(),
            });
            if (!sameTrial(reconciled, currentState)) {
              onTrialChange(reconciled, currentState);
            }
          }
          const occupying = healthAfter.data?.active_run_id ?? "unknown";
          setConflictRunId(occupying);
          setMessage(t.production.trialActiveConflict(occupying));
        } else {
          setMessage(
            `${t.production.trialSubmissionUncertain} ${errorText(error)}`,
          );
        }
      }
    } finally {
      if (!persistedSubmission) startInFlightRef.current = false;
    }
  }

  async function handleCancel() {
    if (cancelInFlightRef.current || !run) return;
    cancelInFlightRef.current = true;
    try {
      const current = latestTrialRef.current;
      if (!current || !onTrialChange(current, current)) return;
      const cancelled = await cancelRun.mutateAsync({ runId: run.run_id });
      const next = observeProductionTrialRun(
        latestTrialRef.current ?? current,
        {
          run_id: cancelled.run_id,
          status: cancelled.status,
          observed_at: new Date().toISOString(),
        },
      );
      onTrialChange(next, current);
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      cancelInFlightRef.current = false;
    }
  }

  async function handleRetryPending() {
    const current = latestTrialRef.current;
    if (!current?.pending_submission || current.active_run_id) return;
    setMessage(null);
    const expectedContext = operationContextRef.current;
    const [healthResult, runsResult] = await Promise.all([
      health.refetch(),
      runs.refetch(),
    ]);
    if (operationContextRef.current !== expectedContext) return;
    if (
      healthResult.isError ||
      runsResult.isError ||
      !healthResult.data ||
      healthResult.data.status !== "ok" ||
      !runsResult.data
    ) {
      setMessage(t.production.trialHealthUnavailable);
      return;
    }
    const reconciled = reconcilePendingProductionTrial(current, {
      workflow_id: workflowId,
      expected_generation: current.pending_submission.generation,
      runs: runsResult.data,
      observed_at: new Date().toISOString(),
    });
    if (!sameTrial(reconciled, current)) {
      onTrialChange(reconciled, current);
      return;
    }
    if (healthResult.data.active_run_id) {
      setConflictRunId(healthResult.data.active_run_id);
      setMessage(
        t.production.trialActiveConflict(healthResult.data.active_run_id),
      );
      return;
    }
    const cleared = { ...current, pending_submission: null };
    if (!onTrialChange(cleared, current)) return;
    latestTrialRef.current = cleared;
    startInFlightRef.current = false;
    await handleStartTrial();
  }

  async function handleAccept() {
    const expectedContext = operationContextRef.current;
    const fingerprint = currentEvidenceFingerprintRef.current;
    if (
      acceptInFlightRef.current ||
      !run ||
      !readiness ||
      !latestTrialRef.current ||
      !fingerprint ||
      readinessFingerprint !== fingerprint
    )
      return;
    acceptInFlightRef.current = true;
    setAcceptRefreshing(true);
    setMessage(null);
    try {
      const attempt = latestTrialRef.current.attempts.at(-1);
      const priorEvidenceGeneration = readiness.evidence_generation;
      if (!attempt || attempt.run_id !== run.run_id) return;

      const [
        freshRunResult,
        freshArtifactsResult,
        freshManifestResult,
        freshMetricsResult,
        freshRawTrackResult,
        freshCleanedTrackResult,
        freshAuditResult,
      ] = await Promise.all([
        runQuery.refetch(),
        artifactsQuery.refetch(),
        manifestQuery.refetch(),
        metricsQuery.refetch(),
        rawTrackQuery.refetch(),
        attempt.request.enable_postprocess
          ? cleanedTrackQuery.refetch()
          : Promise.resolve({
              data: null,
              isError: false,
              dataUpdatedAt: cleanedTrackQuery.dataUpdatedAt,
            }),
        auditQuery.refetch(),
      ]);
      if (operationContextRef.current !== expectedContext) return;
      if (
        freshRunResult.isError ||
        freshArtifactsResult.isError ||
        freshManifestResult.isError ||
        freshMetricsResult.isError ||
        freshRawTrackResult.isError ||
        freshCleanedTrackResult.isError ||
        freshAuditResult.isError ||
        !freshRunResult.data ||
        !freshArtifactsResult.data ||
        !freshManifestResult.data ||
        !freshMetricsResult.data ||
        typeof freshRawTrackResult.data !== "string" ||
        (attempt.request.enable_postprocess &&
          typeof freshCleanedTrackResult.data !== "string") ||
        !freshAuditResult.data
      ) {
        setReadiness(null);
        setReadinessFingerprint(null);
        setMessage(t.production.trialEvidenceRefreshFailed);
        return;
      }

      const freshEvidence = assessProductionTrialEvidence({
        run: freshRunResult.data,
        artifacts: freshArtifactsResult.data,
        manifest: freshManifestResult.data,
        metrics: freshMetricsResult.data,
        audit: freshAuditResult.data,
        raw_csv: freshRawTrackResult.data,
        cleaned_csv: freshCleanedTrackResult.data,
        readable_artifact_names: [
          "run_manifest.json",
          "metrics_report.json",
          "ball_track.csv",
          "ball_audit.json",
          ...(attempt.request.enable_postprocess
            ? ["ball_track.cleaned.csv"]
            : []),
        ],
        enable_postprocess: Boolean(attempt.request.enable_postprocess),
        enable_follow_cam: Boolean(attempt.request.enable_follow_cam),
        video_loaded: videoMetadataLoaded && videoCanPlay,
      });
      if (!freshEvidence.ready || !freshEvidence.video || !videoMediaMetadata) {
        setReadiness(null);
        setReadinessFingerprint(null);
        setMessage(t.production.trialEvidenceRefreshFailed);
        return;
      }
      const freshFingerprint = productionTrialEvidenceSnapshotIdentity({
        run_id: freshRunResult.data.run_id,
        intent_sha256: attempt.intent_sha256,
        request_sha256: attempt.request_sha256,
        query_revisions: {
          run: freshRunResult.dataUpdatedAt,
          artifacts: freshArtifactsResult.dataUpdatedAt,
          manifest: freshManifestResult.dataUpdatedAt,
          metrics: freshMetricsResult.dataUpdatedAt,
          audit: freshAuditResult.dataUpdatedAt,
          raw_csv: freshRawTrackResult.dataUpdatedAt,
          cleaned_csv: attempt.request.enable_postprocess
            ? freshCleanedTrackResult.dataUpdatedAt
            : null,
        },
        raw_csv_length: freshRawTrackResult.data.length,
        cleaned_csv_length: attempt.request.enable_postprocess
          ? String(freshCleanedTrackResult.data).length
          : null,
        selected_video: {
          name: freshEvidence.video.name,
          path: freshEvidence.video.path,
          size_bytes: freshEvidence.video.size_bytes ?? null,
        },
        video_metadata: videoMediaMetadata,
      });
      const freshEvidenceGeneration = await productionTrialEvidenceGeneration({
        run_id: freshRunResult.data.run_id,
        intent_sha256: attempt.intent_sha256,
        request_sha256: attempt.request_sha256,
        artifacts: freshArtifactsResult.data,
        stats: freshRunResult.data.stats ?? null,
        manifest: freshManifestResult.data,
        metrics: freshMetricsResult.data,
        audit: freshAuditResult.data,
        raw_csv: freshRawTrackResult.data,
        cleaned_csv: attempt.request.enable_postprocess
          ? String(freshCleanedTrackResult.data)
          : null,
        selected_video: freshEvidence.video,
        video_metadata: videoMediaMetadata,
      });
      if (operationContextRef.current !== expectedContext) return;
      const currentAttempt = latestTrialRef.current?.attempts.at(-1);
      if (
        !currentAttempt ||
        currentAttempt.run_id !== attempt.run_id ||
        currentAttempt.request_sha256 !== attempt.request_sha256
      )
        return;
      if (freshEvidenceGeneration !== priorEvidenceGeneration) {
        setReadiness(null);
        setReadinessFingerprint(null);
        setMessage(t.production.trialEvidenceChanged);
        return;
      }
      const freshReadiness: ProductionTrialReadinessSummary = {
        run_id: freshRunResult.data.run_id,
        request_sha256: attempt.request_sha256,
        evidence_generation: freshEvidenceGeneration,
        verified_at: new Date().toISOString(),
        video_artifact_name: freshEvidence.video.name,
        artifact_names: productionTrialArtifactContract({
          enable_postprocess: Boolean(attempt.request.enable_postprocess),
          video_artifact_name: freshEvidence.video.name,
        }).required_names,
        quality: freshEvidence.quality,
      };
      setReadiness(freshReadiness);
      setReadinessFingerprint(freshFingerprint);
      const settings = currentSettings();
      if (!settings) return;
      const currentTrial = latestTrialRef.current;
      if (!currentTrial) return;
      const intent = buildProductionTrialIntent({
        workflow_id: workflowId,
        source,
        calibration,
        settings,
      });
      const intentDigest = await sha256Text(canonicalJson(intent));
      if (operationContextRef.current !== expectedContext) return;
      const next = acceptProductionTrial(currentTrial, {
        run: freshRunResult.data,
        current_intent_sha256: intentDigest,
        readiness: freshReadiness,
        accepted_at: new Date().toISOString(),
      });
      onTrialChange(next, currentTrial);
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      acceptInFlightRef.current = false;
      setAcceptRefreshing(false);
    }
  }

  async function handleConfirmConfig() {
    if (
      configInFlightRef.current ||
      !latestTrialRef.current?.accepted ||
      !readiness ||
      readiness.evidence_generation !==
        latestTrialRef.current.accepted.readiness.evidence_generation ||
      readinessFingerprint !== currentEvidenceFingerprintRef.current
    )
      return;
    configInFlightRef.current = true;
    setConfigDeriveActive(true);
    setMessage(null);
    const expectedContext = operationContextRef.current;
    try {
      const generation = ++configGenerationRef.current;
      const currentTrial = latestTrialRef.current;
      const expectedPending = latestPendingConfigRef.current;
      if (!currentTrial?.accepted) return;
      const pending = await buildProductionConfigConfirmation({
        workflow_id: workflowId,
        source,
        calibration,
        trial: currentTrial,
        output_id: crypto.randomUUID(),
        generation,
        confirmed_at: new Date().toISOString(),
      });
      if (operationContextRef.current !== expectedContext) return;
      if (
        !onPendingConfigChange(
          pending,
          expectedPending,
          currentTrial.accepted.run_id,
        )
      )
        return;
      latestPendingConfigRef.current = pending;
      latestConfirmedConfigRef.current = null;
      try {
        const detail = await deriveConfig.mutateAsync({
          data: pending.request,
        });
        if (
          operationContextRef.current !== expectedContext ||
          !latestPendingConfigRef.current ||
          !sameSnapshot(latestPendingConfigRef.current, pending)
        )
          return;
        const confirmed = await finalizeProductionConfigConfirmation(
          pending,
          detail,
        );
        if (onConfirmedConfigChange(confirmed, pending)) {
          latestConfirmedConfigRef.current = confirmed;
          latestPendingConfigRef.current = null;
          try {
            await queryClient.fetchQuery(
              getGetConfigQueryOptions(confirmed.name, {
                query: {
                  queryKey: getGetConfigQueryKey(confirmed.name),
                  staleTime: 0,
                  retry: false,
                },
              }),
            );
          } catch {
            // Verification state is rendered by the canonical GET query.
          }
        }
      } catch (error) {
        setMessage(
          `${t.production.configPendingUncertain} ${errorText(error)}`,
        );
      }
    } finally {
      configInFlightRef.current = false;
      setConfigDeriveActive(false);
    }
  }

  function handleUnlock() {
    const current = latestTrialRef.current;
    if (!current) return;
    const next = invalidateProductionTrialAcceptance(current);
    if (onTrialChange(next, current)) {
      setUnlockOpen(false);
      setConfigVerification(null);
      onUsabilityChange(false);
    }
  }

  const locked = Boolean(trial?.accepted);
  const running = run?.status === "queued" || run?.status === "running";
  const canStart = !locked && !running && !trial?.pending_submission;
  const showRetry = run?.status === "failed" || run?.status === "cancelled";
  const evidenceLoading =
    evidenceEnabled &&
    (artifactsQuery.isPending ||
      manifestQuery.isPending ||
      metricsQuery.isPending ||
      rawTrackQuery.isPending ||
      auditQuery.isPending ||
      (Boolean(latestAttempt?.request.enable_postprocess) &&
        cleanedTrackQuery.isPending));
  const evidenceRefreshing =
    acceptRefreshing ||
    Boolean(runQuery.isFetching) ||
    Boolean(artifactsQuery.isFetching) ||
    Boolean(manifestQuery.isFetching) ||
    Boolean(metricsQuery.isFetching) ||
    Boolean(rawTrackQuery.isFetching) ||
    Boolean(cleanedTrackQuery.isFetching) ||
    Boolean(auditQuery.isFetching);
  const evidenceReadyForAction =
    Boolean(readiness) &&
    readinessFingerprint === currentEvidenceFingerprint &&
    !evidenceRefreshing;

  const verificationMessage = (() => {
    switch (configVerification?.status) {
      case "verified":
        return t.production.configVerified;
      case "missing":
        return t.production.configMissing;
      case "digest_mismatch":
        return t.production.configDigestMismatch;
      case "lineage_mismatch":
      case "name_mismatch":
        return t.production.configLineageMismatch;
      case "unverifiable":
        return t.production.configUnverifiable;
      default:
        return confirmedConfig ? t.production.configVerifying : null;
    }
  })();

  return (
    <div className="space-y-5" data-testid="production-trial-step">
      {message && (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      )}
      {conflictRunId && (
        <a
          className="inline-flex items-center text-sm text-primary underline"
          href={`/history?run=${encodeURIComponent(conflictRunId)}`}
        >
          {t.production.trialViewRun}
          <ExternalLink className="ml-1 h-3 w-3" aria-hidden="true" />
        </a>
      )}

      <fieldset
        className="grid gap-4 sm:grid-cols-2"
        disabled={locked || running}
      >
        <legend className="sr-only">{t.production.stages.trial}</legend>
        <div className="space-y-2 sm:col-span-2">
          <Label htmlFor="trial-base-config">
            {t.production.trialBaseConfig}
          </Label>
          <select
            id="trial-base-config"
            value={baseConfig}
            onChange={(event) => {
              setBaseConfig(event.target.value);
              updateSettings({ base_config_name: event.target.value });
            }}
            className="flex h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
          >
            <option value="">{t.production.sourcePlaceholder}</option>
            {(configs.data ?? []).map((config) => (
              <option key={config.name} value={config.name}>
                {config.name}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="trial-start-frame">
            {t.production.trialStartFrame}
          </Label>
          <Input
            id="trial-start-frame"
            inputMode="numeric"
            value={startFrame}
            onChange={(event) => {
              setStartFrame(event.target.value);
              const value = parseStrictInteger(event.target.value, 0);
              if (value !== null) updateSettings({ start_frame: value });
            }}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="trial-max-frames">
            {t.production.trialMaxFrames}
          </Label>
          <Input
            id="trial-max-frames"
            inputMode="numeric"
            value={maxFrames}
            onChange={(event) => {
              setMaxFrames(event.target.value);
              const value = parseStrictInteger(event.target.value, 1);
              if (value !== null) updateSettings({ max_frames: value });
            }}
          />
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id="trial-postprocess"
            checked={postprocess}
            onCheckedChange={(checked) => {
              const value = checked === true;
              setPostprocess(value);
              updateSettings({ enable_postprocess: value });
            }}
          />
          <Label htmlFor="trial-postprocess">
            {t.production.trialPostprocess}
          </Label>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id="trial-follow-cam"
            checked={followCam}
            onCheckedChange={(checked) => {
              const value = checked === true;
              setFollowCam(value);
              updateSettings({ enable_follow_cam: value });
            }}
          />
          <Label htmlFor="trial-follow-cam">
            {t.production.trialFollowCam}
          </Label>
        </div>
      </fieldset>

      <div className="flex flex-wrap gap-2">
        {locked ? (
          <Button
            ref={unlockButtonRef}
            type="button"
            variant="outline"
            onClick={() => setUnlockOpen(true)}
          >
            <Lock className="mr-2 h-4 w-4" aria-hidden="true" />
            {t.production.trialEditSettings}
          </Button>
        ) : (
          <Button
            type="button"
            onClick={() => void handleStartTrial()}
            disabled={!canStart}
          >
            {createRun.isPending ? (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            ) : showRetry ? (
              <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
            ) : (
              <Play className="mr-2 h-4 w-4" aria-hidden="true" />
            )}
            {showRetry ? t.production.trialRetry : t.production.trialStart}
          </Button>
        )}
        {running && (
          <Button
            ref={stopButtonRef}
            type="button"
            variant="destructive"
            onClick={() => void handleCancel()}
            disabled={cancelRun.isPending}
          >
            <Square className="mr-2 h-4 w-4" aria-hidden="true" />
            {t.production.trialCancel}
          </Button>
        )}
      </div>

      {trial?.pending_submission && !run && (
        <Alert>
          <AlertDescription className="space-y-3">
            <p>{t.production.trialSubmissionUncertain}</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void handleRetryPending()}
            >
              <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.trialRetry}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {run && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between text-base">
              <span>{t.production.trialStatus}</span>
              <Badge data-testid="trial-run-status" variant="secondary">
                {t.production.trialStatusLabel(run.status)}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3" aria-live="polite">
            <p className="font-mono text-xs">{run.run_id}</p>
            {run.progress && (
              <>
                <p className="text-sm">{run.progress.stage}</p>
                <Progress
                  value={run.progress.percent ?? 0}
                  aria-label={`${run.progress.percent ?? 0}%`}
                />
              </>
            )}
            {run.error && (
              <p className="text-sm text-destructive">{run.error}</p>
            )}
          </CardContent>
        </Card>
      )}

      {trial && trial.attempts.length > 0 && (
        <section aria-labelledby="trial-attempts-title">
          <h3 id="trial-attempts-title" className="mb-2 text-sm font-semibold">
            {t.production.trialAttempts}
          </h3>
          <ol className="space-y-2">
            {trial.attempts.map((attempt) => (
              <li
                key={attempt.run_id}
                className="rounded-md border p-3 text-xs"
              >
                <span className="font-mono">{attempt.run_id}</span>
                <span className="ml-2">
                  {t.production.trialStatusLabel(attempt.last_observed.status)}
                </span>
                {attempt.parent_run_id && (
                  <span className="ml-2 font-mono">
                    {t.production.trialParent}: {attempt.parent_run_id}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {evidenceLoading && (
        <p role="status" className="text-sm">
          <Loader2
            className="mr-2 inline h-4 w-4 animate-spin"
            aria-hidden="true"
          />
          {t.production.trialEvidenceLoading}
        </p>
      )}
      {evidence && !evidence.ready && !evidenceLoading && (
        <Alert variant="destructive">
          <AlertTitle>{t.production.trialEvidenceBlocked}</AlertTitle>
          <AlertDescription>
            <ul className="mt-2 list-disc pl-5">
              {evidence.reasons.map((reason) => (
                <li key={reason}>{t.production.trialEvidenceReason(reason)}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      {selectedVideo && (
        <section aria-labelledby="trial-video-title" className="space-y-2">
          <h3 id="trial-video-title" className="text-sm font-semibold">
            {t.production.trialVideoEvidence}
          </h3>
          <video
            key={`${monitoredRunId}:${selectedVideo.name}:${selectedVideo.path}:${selectedVideo.size_bytes}:${selectedVideo.content_type}`}
            controls
            preload="metadata"
            className="aspect-video w-full rounded-md bg-black"
            onLoadedMetadata={(event) => {
              const media = event.currentTarget;
              setVideoMetadataLoaded(true);
              setVideoMediaMetadata({
                duration: Number.isFinite(media.duration)
                  ? media.duration
                  : null,
                width: media.videoWidth,
                height: media.videoHeight,
              });
            }}
            onCanPlay={() => setVideoCanPlay(true)}
            onError={() => {
              setVideoMetadataLoaded(false);
              setVideoCanPlay(false);
              setVideoMediaMetadata(null);
            }}
          >
            <source
              src={getGetArtifactUrl(monitoredRunId, selectedVideo.name)}
              type={selectedVideo.content_type ?? "video/mp4"}
            />
          </video>
        </section>
      )}
      {evidence?.ready && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t.production.trialQualitySignals}
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
            <p>
              {t.production.trialQuality.detected}:{" "}
              {(evidence.quality.detected_ratio * 100).toFixed(1)}%
            </p>
            <p>
              {t.production.trialQuality.predicted}:{" "}
              {(evidence.quality.predicted_ratio * 100).toFixed(1)}%
            </p>
            <p>
              {t.production.trialQuality.lost}:{" "}
              {(evidence.quality.lost_ratio * 100).toFixed(1)}%
            </p>
            <p>
              {t.production.trialQuality.longestLost}:{" "}
              {evidence.quality.longest_lost_streak ?? "—"}
            </p>
            <p>
              {t.production.trialQuality.falsePositiveIslands}:{" "}
              {evidence.quality.false_positive_island_count ?? "—"}
            </p>
            <p>
              {t.production.trialQuality.maxStep}:{" "}
              {evidence.quality.max_step_px ?? "—"}
            </p>
            <p>
              {t.production.trialQuality.tracklets}:{" "}
              {evidence.quality.audit_tracklet_count}
            </p>
            <p>
              {t.production.trialQuality.suspicious}:{" "}
              {evidence.quality.audit_suspicious_tracklet_count}
            </p>
            <p>
              {t.production.trialQuality.reviewEvents}:{" "}
              {evidence.quality.audit_review_event_count}
            </p>
            <p>
              {t.production.trialQuality.lostGaps}:{" "}
              {evidence.quality.audit_lost_gap_count}
            </p>
            {evidence.quality.quality_gate_status && (
              <p>
                {t.production.trialQuality.qualityGate}:{" "}
                {evidence.quality.quality_gate_status}
              </p>
            )}
          </CardContent>
        </Card>
      )}
      {evidenceReadyForAction && (
        <span
          className="sr-only"
          role="status"
          data-testid="trial-evidence-ready"
        >
          {t.production.trialEvidenceReady}
        </span>
      )}
      {readiness &&
        readinessFingerprint === currentEvidenceFingerprint &&
        !trial?.accepted && (
          <Button
            type="button"
            onClick={() => void handleAccept()}
            disabled={evidenceRefreshing}
          >
            {acceptRefreshing && (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            )}
            {t.production.trialAccept}
          </Button>
        )}
      {trial?.accepted && (
        <Alert>
          <AlertTitle>{t.production.trialAccepted}</AlertTitle>
          <AlertDescription className="font-mono text-xs">
            {trial.accepted.run_id}
          </AlertDescription>
        </Alert>
      )}

      {trial?.accepted && (
        <section
          className="space-y-3 border-t pt-4"
          aria-labelledby="config-confirm-title"
        >
          <h3 id="config-confirm-title" className="text-base font-semibold">
            {t.production.configSummary}
          </h3>
          {confirmedConfig && (
            <dl className="grid gap-2 text-xs sm:grid-cols-[auto_1fr]">
              <dt>{t.production.configName}</dt>
              <dd className="font-mono break-all">{confirmedConfig.name}</dd>
              <dt>{t.production.configDigest}</dt>
              <dd className="font-mono break-all">{confirmedConfig.sha256}</dd>
              <dt>{t.production.configPatchDigest}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.patch_sha256}
              </dd>
              <dt>{t.production.configAcceptedTrial}</dt>
              <dd className="font-mono">
                {confirmedConfig.accepted_trial_run_id}
              </dd>
              <dt>{t.production.configTrialIntent}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.trial_intent_sha256}
              </dd>
              <dt>{t.production.configTrialRequest}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.trial_request_sha256}
              </dd>
              <dt>{t.production.configBase}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.base_config_name}
              </dd>
              <dt>{t.production.configWorkflow}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.workflow_id}
              </dd>
              <dt>{t.production.configCalibration}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.calibration_digest}
              </dd>
              <dt>{t.production.configSource}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.source_signature.path}
              </dd>
              <dt>{t.production.configSourceSize}</dt>
              <dd>{confirmedConfig.source_signature.size_bytes}</dd>
              <dt>{t.production.configSourceModified}</dt>
              <dd className="font-mono break-all">
                {confirmedConfig.source_signature.modified_at}
              </dd>
              <dt>{t.production.configPatch}</dt>
              <dd>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-2 font-mono text-[11px]">
                  {JSON.stringify(confirmedConfig.patch, null, 2)}
                </pre>
              </dd>
            </dl>
          )}
          {verificationMessage && (
            <Alert
              variant={
                configVerification && configVerification.status !== "verified"
                  ? "destructive"
                  : "default"
              }
            >
              <AlertDescription aria-live="polite">
                {verificationMessage}
              </AlertDescription>
            </Alert>
          )}
          {pendingConfig && !confirmedConfig && (
            <p className="text-sm">{t.production.configPendingUncertain}</p>
          )}
          <Button
            type="button"
            onClick={() => void handleConfirmConfig()}
            disabled={deriveConfig.isPending || !evidenceReadyForAction}
          >
            {deriveConfig.isPending && (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin"
                aria-hidden="true"
              />
            )}
            {confirmedConfig || pendingConfig
              ? t.production.configReconfirm
              : t.production.configConfirm}
          </Button>
        </section>
      )}

      <AlertDialog open={unlockOpen} onOpenChange={setUnlockOpen}>
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            unlockButtonRef.current?.focus();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{t.production.trialUnlockTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {t.production.trialUnlockDescription}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>
              {t.production.trialKeepLocked}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleUnlock}>
              {t.production.trialUnlockConfirm}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
