import { useEffect, useMemo, useRef, useState, type Ref } from "react";
import {
  getGetArtifactQueryKey,
  getGetArtifactUrl,
  getGetBallAuditReportQueryKey,
  getGetConfigQueryKey,
  getGetConfigQueryOptions,
  getGetHealthQueryKey,
  getGetRunQueryKey,
  getGetTrialDiagnosisQueryKey,
  getGetProductionTrialTuningSchemaQueryKey,
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
  useGetTrialDiagnosis,
  useGetProductionTrialTuningSchema,
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
import { detectorProbeRecoveryEligible } from "@/lib/productionDetectorProbe";
import {
  acceptProductionTrial,
  appendProductionTrialAttempt,
  assessProductionTrialEvidence,
  buildProductionTrialIntent,
  buildProductionTrialSubmission,
  buildVersionedProductionTuningPatch,
  canonicalJson,
  createProductionTrialState,
  invalidateProductionTrialAcceptance,
  isProductionTrialState,
  observeProductionTrialRun,
  productionTrialArtifactContract,
  productionTrialEvidenceGeneration,
  productionTrialEvidenceSnapshotIdentity,
  productionTrialSignalGateAcceptable,
  productionTrialSubmissionLineage,
  productionTrialTuningSchema,
  productionTuningDraft,
  productionTuningHistory,
  productionTuningVersion,
  reconcilePendingProductionTrial,
  selectProductionTrialVideo,
  sha256Text,
  isTrialSignalGateV2,
  type ProductionTrialTuningControl,
  type ProductionTrialReadinessSummary,
  type ProductionTrialSettings,
  type ProductionTrialState,
  type TrialDiagnosticObservation,
  type ProductionTuningDiff,
  type ProductionTuningValue,
} from "@/lib/productionTrial";
import type { SourceSignature } from "@/lib/productionWorkflow";

import { ProductionDetectorProbeController } from "./ProductionDetectorProbeController";

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
  onReturnToFieldSetup: () => void;
  onPendingReconciledReturnToFieldSetup: () => void;
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

function tuningValueLabel(value: unknown) {
  if (value === undefined || value === null || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function tuningControlGroups(
  controls: readonly ProductionTrialTuningControl[],
) {
  const groups = new Map<string, ProductionTrialTuningControl[]>();
  for (const control of controls) {
    const group = groups.get(control.section) ?? [];
    group.push(control);
    groups.set(control.section, group);
  }
  return Array.from(groups.entries());
}

const TRACK_DIAGNOSTIC_KEYS = [
  "frame_count",
  "detected",
  "predicted",
  "lost",
  "detected_ratio",
  "predicted_ratio",
  "lost_ratio",
  "longest_lost_streak",
  "false_positive_island_count",
  "max_step_px",
] as const;

function diagnosticObservationValue(observation: TrialDiagnosticObservation) {
  return observation.status === "collected" && observation.value !== null
    ? observation.value
    : "—";
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
  onReturnToFieldSetup,
  onPendingReconciledReturnToFieldSetup,
  onUsabilityChange,
  stopButtonRef,
}: ProductionTrialStepProps) {
  const { language, t } = useLanguage();
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
  const tuningSchemaQuery = useGetProductionTrialTuningSchema({
    query: {
      queryKey: getGetProductionTrialTuningSchemaQueryKey(),
      staleTime: 60_000,
    },
  });

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
  const [visualConfirmed, setVisualConfirmed] = useState(false);
  const [tuningDraft, setTuningDraft] = useState<Record<string, unknown>>({});
  const [tuningDiff, setTuningDiff] = useState<ProductionTuningDiff[]>([]);
  const [tuningSaving, setTuningSaving] = useState(false);
  const [tuningMessage, setTuningMessage] = useState<string | null>(null);
  const [startRequestInFlight, setStartRequestInFlight] = useState(false);
  const [retryInFlight, setRetryInFlight] = useState(false);
  const [returnReconcileInFlight, setReturnReconcileInFlight] = useState(false);
  const [returnAfterPendingClear, setReturnAfterPendingClear] =
    useState<ProductionTrialState | null>(null);
  const startInFlightRef = useRef(false);
  const retryInFlightRef = useRef(false);
  const returnReconcileInFlightRef = useRef(false);
  const cancelInFlightRef = useRef(false);
  const acceptInFlightRef = useRef(false);
  const configInFlightRef = useRef(false);
  const configGenerationRef = useRef(pendingConfig?.generation ?? 0);
  const evidenceGenerationRef = useRef(0);
  const configVerificationGenerationRef = useRef(0);
  const currentEvidenceFingerprintRef = useRef<string | null>(null);
  const latestTrialRef = useRef(trial);
  const trialPropRef = useRef(trial);
  const operationEpochRef = useRef({ epoch: 0, active: false });
  const latestPendingConfigRef = useRef(pendingConfig);
  const latestConfirmedConfigRef = useRef(confirmedConfig);
  const unlockButtonRef = useRef<HTMLButtonElement>(null);
  const trialSettingsRef = useRef<HTMLFieldSetElement>(null);
  const trialStartFrameRef = useRef<HTMLInputElement>(null);
  const operationContext = canonicalJson({ workflowId, source, calibration });
  const operationContextRef = useRef(operationContext);

  operationContextRef.current = operationContext;
  if (trialPropRef.current !== trial) {
    trialPropRef.current = trial;
    latestTrialRef.current = trial;
  }
  latestPendingConfigRef.current = pendingConfig;
  latestConfirmedConfigRef.current = confirmedConfig;

  useEffect(() => {
    const epoch = operationEpochRef.current.epoch + 1;
    operationEpochRef.current = { epoch, active: true };
    return () => {
      if (operationEpochRef.current.epoch === epoch) {
        operationEpochRef.current = { epoch, active: false };
      }
    };
  }, []);

  useEffect(() => {
    if (
      !returnAfterPendingClear ||
      !trial ||
      !sameTrial(returnAfterPendingClear, trial)
    )
      return;
    setReturnAfterPendingClear(null);
    onPendingReconciledReturnToFieldSetup();
  }, [onPendingReconciledReturnToFieldSetup, returnAfterPendingClear, trial]);

  function currentOperationEpoch(): number | null {
    const current = operationEpochRef.current;
    return current.active ? current.epoch : null;
  }

  function operationEpochIsActive(epoch: number): boolean {
    const current = operationEpochRef.current;
    return current.active && current.epoch === epoch;
  }

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
  const latestAuthoritativeRun =
    (run?.run_id === latestAttempt?.run_id ? run : null) ??
    (runs.data ?? []).find(
      (candidate) => candidate.run_id === latestAttempt?.run_id,
    ) ??
    null;
  const submissionLineage = trial
    ? productionTrialSubmissionLineage(trial, latestAuthoritativeRun)
    : null;
  const localBaseConfigLineageLocked = Boolean(
    trial &&
    (trial.attempts.length > 0 ||
      trial.pending_submission ||
      trial.active_run_id ||
      trial.accepted),
  );
  const baseConfigLineageLocked =
    localBaseConfigLineageLocked ||
    submissionLineage?.base_config_locked === true;
  const authoritativeBaseConfigName =
    submissionLineage?.base_config_locked && latestAttempt?.request.config_name
      ? latestAttempt.request.config_name
      : null;

  useEffect(() => {
    if (!authoritativeBaseConfigName) return;
    setBaseConfig(authoritativeBaseConfigName);
    const current = latestTrialRef.current;
    if (
      !current ||
      current.settings.base_config_name === authoritativeBaseConfigName
    )
      return;
    const next: ProductionTrialState = {
      ...current,
      settings: {
        ...current.settings,
        base_config_name: authoritativeBaseConfigName,
      },
    };
    if (isProductionTrialState(next) && onTrialChange(next, current)) {
      latestTrialRef.current = next;
    }
  }, [authoritativeBaseConfigName, onTrialChange]);
  const diagnosisEnabled =
    run?.status === "completed" || run?.status === "failed";
  const diagnosisQuery = useGetTrialDiagnosis(monitoredRunId, {
    query: {
      queryKey: getGetTrialDiagnosisQueryKey(monitoredRunId),
      enabled: Boolean(monitoredRunId) && diagnosisEnabled,
      staleTime: 0,
    },
    request: NO_STORE_REQUEST,
  });
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
  const tuningBaseConfigQuery = useGetConfig(baseConfig, {
    query: {
      queryKey: getGetConfigQueryKey(baseConfig),
      enabled: Boolean(baseConfig) && !trial?.accepted,
      staleTime: 0,
      retry: false,
    },
  });
  const tuningSchema = useMemo(
    () => productionTrialTuningSchema(tuningSchemaQuery.data),
    [tuningSchemaQuery.data],
  );
  const tuningControls = useMemo(
    () => tuningSchema?.controls ?? [],
    [tuningSchema?.controls],
  );
  const fieldSetupAction = tuningSchema?.actions.find(
    (action) => action.action_code === "return_to_field_setup",
  );
  const tuningBaseConfig = tuningBaseConfigQuery.data?.resolved ?? null;
  const tuningPatch = trial?.settings.tuning_patch ?? {};
  const tuningSourceIdentity = useMemo(() => {
    try {
      return canonicalJson({
        base_config: tuningBaseConfig,
        patch: tuningPatch,
        controls: tuningControls,
      });
    } catch {
      return "invalid";
    }
  }, [tuningBaseConfig, tuningControls, tuningPatch]);
  const tuningCurrentValues = useMemo(
    () =>
      tuningBaseConfig
        ? productionTuningDraft({
            base_config: tuningBaseConfig,
            patch: tuningPatch,
            controls: tuningControls,
          })
        : {},
    [tuningBaseConfig, tuningControls, tuningPatch],
  );
  const tuningGroups = useMemo(
    () => tuningControlGroups(tuningControls),
    [tuningControls],
  );
  const tuningHistory = useMemo(
    () => productionTuningHistory(tuningPatch),
    [tuningPatch],
  );
  const currentTuningVersion = useMemo(
    () => productionTuningVersion(tuningPatch),
    [tuningPatch],
  );

  useEffect(() => {
    if (!tuningBaseConfig || tuningControls.length === 0) {
      setTuningDraft({});
      setTuningDiff([]);
      return;
    }
    setTuningDraft(
      productionTuningDraft({
        base_config: tuningBaseConfig,
        patch: tuningPatch,
        controls: tuningControls,
      }),
    );
    setTuningDiff([]);
    setTuningMessage(null);
  }, [tuningSourceIdentity]);

  useEffect(() => {
    if (!trial?.pending_submission || !runs.data) return;
    const expectedRunId = `production_trial_${trial.pending_submission.output_id}`;
    if (
      runs.data.filter((candidate) => candidate.run_id === expectedRunId)
        .length !== 1
    )
      return;
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
      trial_signal_gate_v2: diagnosisQuery.data?.trial_signal_gate_v2 ?? null,
    });
  }, [
    artifactsQuery.data,
    auditQuery.data,
    latestAttempt,
    manifestQuery.data,
    metricsQuery.data,
    rawTrackQuery.data,
    cleanedTrackQuery.data,
    diagnosisQuery.data,
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
  const diagnosisGateCandidate: unknown =
    diagnosisQuery.data?.trial_signal_gate_v2;
  const diagnosisGate = isTrialSignalGateV2(diagnosisGateCandidate)
    ? diagnosisGateCandidate
    : null;
  const diagnosisAcceptable =
    productionTrialSignalGateAcceptable(diagnosisGate);
  const detectorProbeRecovery = detectorProbeRecoveryEligible({
    monitoredRunId,
    diagnosisRunId: diagnosisQuery.data?.run_id ?? null,
    authoritativeRun: latestAuthoritativeRun
      ? {
          runId: latestAuthoritativeRun.run_id,
          status: latestAuthoritativeRun.status,
        }
      : null,
    gate: diagnosisGate
      ? {
          status: diagnosisGate.status,
          coverageComplete: diagnosisGate.coverage_complete,
          failureCode: diagnosisGate.failure_classification.code,
          coverageStatus: diagnosisGate.stage_counts?.coverage_status ?? null,
          reconciliationStatus:
            diagnosisGate.stage_counts?.reconciliation.status ?? null,
          evaluatedFrames: diagnosisGate.stage_counts?.evaluated_frames ?? null,
          lostFrames: diagnosisGate.stage_counts?.lost_frames ?? null,
          rawCandidates: diagnosisGate.stage_counts?.raw_candidates ?? null,
        }
      : null,
  });
  const visualConfirmationBinding =
    readiness && diagnosisAcceptable
      ? `${readiness.evidence_generation}:${diagnosisGate?.threshold_profile.sha256 ?? ""}`
      : null;

  useEffect(() => {
    setVisualConfirmed(false);
  }, [visualConfirmationBinding]);

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
      tuning_patch: latestTrialRef.current?.settings.tuning_patch ?? {},
    };
  }

  function updateSettings(change: Partial<ProductionTrialSettings>) {
    const current = latestTrialRef.current;
    if (
      !current ||
      current.accepted ||
      current.active_run_id ||
      current.pending_submission
    )
      return;
    const next = {
      ...current,
      settings: { ...current.settings, ...change },
      accepted: null,
    };
    if (onTrialChange(next, current)) latestTrialRef.current = next;
  }

  async function handleSaveTuning(rerun: boolean) {
    if (tuningSaving || running || latestTrialRef.current?.pending_submission)
      return;
    setTuningMessage(null);
    const settings = currentSettings();
    const expectedTrial = latestTrialRef.current;
    if (
      !settings ||
      !tuningBaseConfig ||
      tuningControls.length === 0 ||
      expectedTrial?.accepted ||
      expectedTrial?.active_run_id ||
      expectedTrial?.pending_submission
    ) {
      setTuningMessage(t.production.trialTuningUnavailable);
      return;
    }
    setTuningSaving(true);
    try {
      const result = await buildVersionedProductionTuningPatch({
        base_config: tuningBaseConfig,
        previous_patch: expectedTrial?.settings.tuning_patch ?? {},
        controls: tuningControls,
        values: tuningDraft,
        version_id: crypto.randomUUID(),
        created_at: new Date().toISOString(),
      });
      const latest = latestTrialRef.current;
      if (
        !sameSnapshot(latest, expectedTrial) ||
        latest?.accepted ||
        latest?.active_run_id ||
        latest?.pending_submission
      )
        return;
      const current = latest ?? createProductionTrialState(settings);
      const next: ProductionTrialState = {
        ...current,
        settings: { ...settings, tuning_patch: result.patch },
        accepted: null,
      };
      if (!onTrialChange(next, expectedTrial)) return;
      latestTrialRef.current = next;
      setTuningDiff(result.diff);
      setTuningMessage(t.production.trialTuningSaved(result.diff.length));
      if (rerun) {
        startInFlightRef.current = false;
        await handleStartTrial();
      }
    } catch (error) {
      setTuningMessage(
        `${t.production.trialTuningInvalid} ${errorText(error)}`,
      );
    } finally {
      setTuningSaving(false);
    }
  }

  function handleResetTuning() {
    if (
      latestTrialRef.current?.pending_submission ||
      !tuningBaseConfig ||
      tuningControls.length === 0
    )
      return;
    setTuningDraft(
      productionTuningDraft({
        base_config: tuningBaseConfig,
        patch: {},
        controls: tuningControls,
      }),
    );
    setTuningDiff([]);
    setTuningMessage(null);
  }

  function handleRestoreTuning(values: Record<string, ProductionTuningValue>) {
    if (latestTrialRef.current?.pending_submission) return;
    setTuningDraft(
      Object.fromEntries(
        tuningControls.map((control) => [
          control.path,
          values[control.path] ?? tuningCurrentValues[control.path],
        ]),
      ),
    );
    setTuningDiff([]);
    setTuningMessage(t.production.trialTuningRestored);
  }

  async function handleStartTrial() {
    const operationEpoch = currentOperationEpoch();
    const initialTrial = latestTrialRef.current;
    if (
      operationEpoch === null ||
      startInFlightRef.current ||
      initialTrial?.pending_submission ||
      initialTrial?.active_run_id ||
      initialTrial?.accepted
    )
      return;
    startInFlightRef.current = true;
    setStartRequestInFlight(true);
    setMessage(null);
    setConflictRunId(null);
    const expectedContext = operationContextRef.current;
    try {
      const settings = currentSettings();
      if (!settings) {
        setMessage(t.production.trialInvalidFrameRange);
        return;
      }
      const expectedTrial = latestTrialRef.current;
      const lineage = expectedTrial
        ? productionTrialSubmissionLineage(
            expectedTrial,
            latestAuthoritativeRun,
          )
        : productionTrialSubmissionLineage(
            createProductionTrialState(settings),
            null,
          );
      if (!lineage) {
        setMessage(t.production.trialLineageUnavailable);
        return;
      }
      const current = lineage.state;
      const generation = lineage.generation;
      const submission = await buildProductionTrialSubmission({
        workflow_id: workflowId,
        source,
        calibration,
        settings,
        parent_run_id: lineage.parent_run_id,
        legacy_restart_run_id: lineage.legacy_restart_run_id,
        submission_id: crypto.randomUUID(),
        output_id: crypto.randomUUID(),
        generation,
        created_at: new Date().toISOString(),
      });
      if (
        !operationEpochIsActive(operationEpoch) ||
        operationContextRef.current !== expectedContext ||
        !sameSnapshot(latestTrialRef.current, expectedTrial) ||
        latestTrialRef.current?.pending_submission ||
        latestTrialRef.current?.active_run_id ||
        latestTrialRef.current?.accepted
      )
        return;
      const pendingState: ProductionTrialState = {
        ...current,
        settings,
        pending_submission: submission.pending,
        accepted: null,
      };
      if (!onTrialChange(pendingState, expectedTrial)) return;
      if (!operationEpochIsActive(operationEpoch)) return;
      latestTrialRef.current = pendingState;

      const healthResult = await health.refetch();
      const pendingAfterHealth = latestTrialRef.current?.pending_submission;
      if (
        !operationEpochIsActive(operationEpoch) ||
        operationContextRef.current !== expectedContext ||
        pendingAfterHealth?.submission_id !==
          submission.pending.submission_id ||
        pendingAfterHealth?.request_sha256 !== submission.pending.request_sha256
      )
        return;
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
        if (!operationEpochIsActive(operationEpoch)) return;
        const created = await createRun.mutateAsync({
          data: submission.pending.request,
        });
        if (
          !operationEpochIsActive(operationEpoch) ||
          operationContextRef.current !== expectedContext
        )
          return;
        if (
          latestTrialRef.current?.pending_submission?.submission_id !==
            submission.pending.submission_id ||
          latestTrialRef.current?.pending_submission?.request_sha256 !==
            submission.pending.request_sha256
        )
          return;
        const next = appendProductionTrialAttempt(latestTrialRef.current, {
          run: created,
          pending: submission.pending,
          observed_at: new Date().toISOString(),
        });
        onTrialChange(next, pendingState);
      } catch (error) {
        if (!operationEpochIsActive(operationEpoch)) return;
        if (errorStatus(error) === 409) {
          const [healthAfter, runsAfter] = await Promise.all([
            health.refetch(),
            runs.refetch(),
          ]);
          if (
            !operationEpochIsActive(operationEpoch) ||
            operationContextRef.current !== expectedContext
          )
            return;
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
      if (operationEpochIsActive(operationEpoch)) {
        startInFlightRef.current = false;
        setStartRequestInFlight(false);
      }
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
    const operationEpoch = currentOperationEpoch();
    const current = latestTrialRef.current;
    if (
      operationEpoch === null ||
      retryInFlightRef.current ||
      returnReconcileInFlightRef.current ||
      startInFlightRef.current ||
      !current?.pending_submission ||
      current.active_run_id
    )
      return;
    retryInFlightRef.current = true;
    setRetryInFlight(true);
    try {
      setMessage(null);
      const expectedContext = operationContextRef.current;
      const [healthResult, runsResult] = await Promise.all([
        health.refetch(),
        runs.refetch(),
      ]);
      const latestPending = latestTrialRef.current?.pending_submission;
      if (
        !operationEpochIsActive(operationEpoch) ||
        operationContextRef.current !== expectedContext ||
        latestPending?.submission_id !==
          current.pending_submission.submission_id ||
        latestPending?.request_sha256 !==
          current.pending_submission.request_sha256
      )
        return;
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
      if (!operationEpochIsActive(operationEpoch)) return;
      latestTrialRef.current = cleared;
      startInFlightRef.current = false;
      await handleStartTrial();
    } finally {
      if (operationEpochIsActive(operationEpoch)) {
        retryInFlightRef.current = false;
        setRetryInFlight(false);
      }
    }
  }

  async function handleReconcileAndReturnToFieldSetup() {
    const operationEpoch = currentOperationEpoch();
    const current = latestTrialRef.current;
    if (
      operationEpoch === null ||
      returnReconcileInFlightRef.current ||
      retryInFlightRef.current ||
      startInFlightRef.current ||
      !current?.pending_submission ||
      current.active_run_id
    )
      return;
    returnReconcileInFlightRef.current = true;
    setReturnReconcileInFlight(true);
    setMessage(null);
    setConflictRunId(null);
    try {
      const expectedContext = operationContextRef.current;
      let healthResult: Awaited<ReturnType<typeof health.refetch>>;
      let runsResult: Awaited<ReturnType<typeof runs.refetch>>;
      try {
        [healthResult, runsResult] = await Promise.all([
          health.refetch(),
          runs.refetch(),
        ]);
      } catch {
        if (operationEpochIsActive(operationEpoch)) {
          setMessage(t.production.trialReconcileUnavailable);
        }
        return;
      }
      const latestPending = latestTrialRef.current?.pending_submission;
      if (
        !operationEpochIsActive(operationEpoch) ||
        operationContextRef.current !== expectedContext ||
        !sameSnapshot(latestPending, current.pending_submission)
      )
        return;
      if (
        healthResult.isError ||
        runsResult.isError ||
        !healthResult.data ||
        healthResult.data.status !== "ok" ||
        !runsResult.data
      ) {
        setMessage(t.production.trialReconcileUnavailable);
        return;
      }

      const expectedRunId = `production_trial_${current.pending_submission.output_id}`;
      const expectedRunRecords = runsResult.data.filter(
        (candidate) => candidate.run_id === expectedRunId,
      );
      const reconciled = reconcilePendingProductionTrial(current, {
        workflow_id: workflowId,
        expected_generation: current.pending_submission.generation,
        runs: runsResult.data,
        observed_at: new Date().toISOString(),
      });
      if (
        expectedRunRecords.length > 1 ||
        (expectedRunRecords.length === 1 && sameTrial(reconciled, current))
      ) {
        setConflictRunId(expectedRunId);
        setMessage(t.production.trialPendingIdentityConflict(expectedRunId));
        return;
      }
      if (!sameTrial(reconciled, current)) {
        if (onTrialChange(reconciled, current)) {
          latestTrialRef.current = reconciled;
        }
        return;
      }

      const listedActiveRun = runsResult.data.find(
        (candidate) =>
          candidate.status === "queued" || candidate.status === "running",
      );
      const conflictRunId =
        healthResult.data.active_run_id ?? listedActiveRun?.run_id ?? null;
      if (conflictRunId) {
        setConflictRunId(conflictRunId);
        setMessage(t.production.trialActiveConflict(conflictRunId));
        return;
      }

      const cleared: ProductionTrialState = {
        ...current,
        pending_submission: null,
      };
      if (!onTrialChange(cleared, current)) return;
      if (!operationEpochIsActive(operationEpoch)) return;
      latestTrialRef.current = cleared;
      startInFlightRef.current = false;
      setReturnAfterPendingClear(cleared);
    } finally {
      if (operationEpochIsActive(operationEpoch)) {
        returnReconcileInFlightRef.current = false;
        setReturnReconcileInFlight(false);
      }
    }
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
      readinessFingerprint !== fingerprint ||
      !visualConfirmed ||
      !diagnosisAcceptable ||
      !visualConfirmationBinding
    )
      return;
    acceptInFlightRef.current = true;
    setAcceptRefreshing(true);
    setMessage(null);
    try {
      const attempt = latestTrialRef.current.attempts.at(-1);
      const priorEvidenceGeneration = readiness.evidence_generation;
      const priorThresholdProfile = diagnosisGate?.threshold_profile.sha256;
      if (!attempt || attempt.run_id !== run.run_id) return;

      const [
        freshRunResult,
        freshArtifactsResult,
        freshManifestResult,
        freshMetricsResult,
        freshRawTrackResult,
        freshCleanedTrackResult,
        freshAuditResult,
        freshDiagnosisResult,
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
        diagnosisQuery.refetch(),
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
        freshDiagnosisResult.isError ||
        !freshRunResult.data ||
        !freshArtifactsResult.data ||
        !freshManifestResult.data ||
        !freshMetricsResult.data ||
        typeof freshRawTrackResult.data !== "string" ||
        (attempt.request.enable_postprocess &&
          typeof freshCleanedTrackResult.data !== "string") ||
        !freshAuditResult.data ||
        !freshDiagnosisResult.data
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
        trial_signal_gate_v2: freshDiagnosisResult.data.trial_signal_gate_v2,
      });
      const freshGate = freshDiagnosisResult.data.trial_signal_gate_v2;
      if (
        !freshEvidence.ready ||
        !freshEvidence.video ||
        !videoMediaMetadata ||
        !productionTrialSignalGateAcceptable(freshGate)
      ) {
        setReadiness(null);
        setReadinessFingerprint(null);
        setVisualConfirmed(false);
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
      if (freshGate.threshold_profile.sha256 !== priorThresholdProfile) {
        setReadiness(null);
        setReadinessFingerprint(null);
        setVisualConfirmed(false);
        setMessage(t.production.trialVisualConfirmationChanged);
        return;
      }
      const confirmedAt = new Date().toISOString();
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
        operator_visual_confirmation: {
          confirmed: true,
          confirmed_at: confirmedAt,
          evidence_generation: freshEvidenceGeneration,
          threshold_profile_sha256: freshGate.threshold_profile.sha256,
        },
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
        accepted_at: confirmedAt,
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
  const requestIntentLocked =
    locked || running || Boolean(trial?.pending_submission);
  const canStart =
    !locked &&
    !running &&
    !trial?.pending_submission &&
    !startRequestInFlight &&
    !retryInFlight;
  const showRetry = run?.status === "failed" || run?.status === "cancelled";
  const evidenceLoading =
    evidenceEnabled &&
    (artifactsQuery.isPending ||
      manifestQuery.isPending ||
      metricsQuery.isPending ||
      rawTrackQuery.isPending ||
      auditQuery.isPending ||
      diagnosisQuery.isPending ||
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
    Boolean(auditQuery.isFetching) ||
    Boolean(diagnosisQuery.isFetching);
  const evidenceReadyForAction =
    Boolean(readiness) &&
    readinessFingerprint === currentEvidenceFingerprint &&
    !evidenceRefreshing;
  const canAcceptTrial =
    evidenceReadyForAction && diagnosisAcceptable && visualConfirmed;

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

  function focusTrialSettings(): void {
    const settings = trialSettingsRef.current;
    if (!settings) return;
    if (locked && unlockButtonRef.current) {
      unlockButtonRef.current.focus({ preventScroll: true });
      unlockButtonRef.current.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      });
      return;
    }
    trialStartFrameRef.current?.focus({ preventScroll: true });
    settings.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }

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
        ref={trialSettingsRef}
        id="production-trial-settings"
        data-testid="production-trial-settings"
        className="scroll-mt-4 grid gap-4 sm:grid-cols-2"
        disabled={requestIntentLocked}
      >
        <legend className="sr-only">{t.production.stages.trial}</legend>
        <div className="space-y-2 sm:col-span-2">
          <Label htmlFor="trial-base-config">
            {t.production.trialBaseConfig}
          </Label>
          <select
            id="trial-base-config"
            value={baseConfig}
            disabled={baseConfigLineageLocked}
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
          {baseConfigLineageLocked && (
            <p className="text-xs text-muted-foreground">
              {t.production.trialBaseConfigLineageLocked}
            </p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="trial-start-frame">
            {t.production.trialStartFrame}
          </Label>
          <Input
            ref={trialStartFrameRef}
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

      {!locked && fieldSetupAction && (
        <Alert data-testid="trial-field-setup-action">
          <AlertTitle>{t.production.trialFieldSetupActionTitle}</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>{t.production.trialFieldSetupActionDescription}</p>
            <p className="font-mono text-xs text-muted-foreground">
              {fieldSetupAction.reason_code} · {fieldSetupAction.target_step}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={onReturnToFieldSetup}
              disabled={requestIntentLocked}
            >
              {t.production.trialReturnToFieldSetup}
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!locked && tuningBaseConfig && tuningControls.length > 0 && (
        <Card data-testid="trial-tuning-editor">
          <CardHeader>
            <CardTitle className="text-base">
              {t.production.trialTuningTitle}
            </CardTitle>
            <p className="text-sm text-muted-foreground">
              {t.production.trialTuningDescription}
            </p>
          </CardHeader>
          <CardContent className="space-y-5">
            <fieldset
              className="space-y-5"
              disabled={requestIntentLocked || tuningSaving}
            >
              <legend className="sr-only">
                {t.production.trialTuningTitle}
              </legend>
              {tuningGroups.map(([section, controls]) => (
                <section key={section} className="space-y-3">
                  <h3 className="text-sm font-semibold">
                    {t.production.trialTuningSection(section)}
                  </h3>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {controls.map((control) => {
                      const id = `trial-tuning-${control.path.replaceAll(".", "-")}`;
                      const currentValue = tuningCurrentValues[control.path];
                      const proposedValue = tuningDraft[control.path];
                      const changed = !sameSnapshot(
                        currentValue,
                        proposedValue,
                      );
                      const description =
                        language === "zh"
                          ? control.description_zh
                          : control.description;
                      return (
                        <div
                          key={control.path}
                          className={`space-y-2 rounded-md border p-3 ${changed ? "border-primary" : ""}`}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <Label htmlFor={id}>{control.path}</Label>
                            <Badge variant={changed ? "default" : "secondary"}>
                              {changed
                                ? t.production.trialTuningChanged
                                : t.production.trialTuningUnchanged}
                            </Badge>
                          </div>
                          <p className="text-xs text-muted-foreground">
                            {description}
                          </p>
                          {control.kind === "boolean" ? (
                            <div className="flex items-center gap-2">
                              <Checkbox
                                id={id}
                                checked={proposedValue === true}
                                onCheckedChange={(checked) =>
                                  setTuningDraft((current) => ({
                                    ...current,
                                    [control.path]: checked === true,
                                  }))
                                }
                              />
                              <span className="text-sm">
                                {t.production.trialTuningBoolean(
                                  proposedValue === true,
                                )}
                              </span>
                            </div>
                          ) : control.kind === "select" ? (
                            <select
                              id={id}
                              value={String(proposedValue ?? "")}
                              onChange={(event) =>
                                setTuningDraft((current) => ({
                                  ...current,
                                  [control.path]: event.target.value,
                                }))
                              }
                              className="flex h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            >
                              {(control.options ?? []).map((option) => (
                                <option key={option} value={option}>
                                  {option}
                                </option>
                              ))}
                            </select>
                          ) : control.kind === "multi_select" ? (
                            <select
                              id={id}
                              multiple
                              size={Math.min(
                                Math.max(control.options?.length ?? 2, 2),
                                5,
                              )}
                              value={
                                Array.isArray(proposedValue)
                                  ? proposedValue
                                  : []
                              }
                              onChange={(event) =>
                                setTuningDraft((current) => ({
                                  ...current,
                                  [control.path]: Array.from(
                                    event.target.selectedOptions,
                                    (option) => option.value,
                                  ),
                                }))
                              }
                              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                            >
                              {(control.options ?? []).map((option) => (
                                <option key={option} value={option}>
                                  {option}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <Input
                              id={id}
                              type="number"
                              value={String(proposedValue ?? "")}
                              min={control.minimum ?? undefined}
                              max={control.maximum ?? undefined}
                              step={control.step ?? undefined}
                              onChange={(event) =>
                                setTuningDraft((current) => ({
                                  ...current,
                                  [control.path]:
                                    event.target.value === ""
                                      ? ""
                                      : Number(event.target.value),
                                }))
                              }
                            />
                          )}
                          <p className="text-xs">
                            {t.production.trialTuningRange(
                              control.minimum,
                              control.maximum,
                              control.step,
                            )}
                          </p>
                          <p className="text-xs">
                            {t.production.trialTuningRuntime}:{" "}
                            {t.production.trialTuningRuntimeImpact(
                              control.runtime_impact,
                            )}
                          </p>
                          <p className="text-xs font-mono">
                            {t.production.trialTuningCurrent}:{" "}
                            {tuningValueLabel(currentValue)} ·{" "}
                            {t.production.trialTuningProposed}:{" "}
                            {tuningValueLabel(proposedValue)}
                          </p>
                        </div>
                      );
                    })}
                  </div>
                </section>
              ))}
            </fieldset>

            {tuningDiff.length > 0 && (
              <div
                data-testid="trial-tuning-diff"
                className="space-y-1 text-xs"
              >
                <p className="font-semibold">{t.production.trialTuningDiff}</p>
                <ul className="list-disc pl-5 font-mono">
                  {tuningDiff.map((item) => (
                    <li key={item.path}>
                      {item.path}: {tuningValueLabel(item.previous_value)} →{" "}
                      {tuningValueLabel(item.next_value)}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {currentTuningVersion && (
              <p className="text-xs font-mono break-all">
                {t.production.trialTuningVersion}:{" "}
                {currentTuningVersion.version_id}
              </p>
            )}
            {tuningHistory.length > 0 && (
              <details className="text-sm">
                <summary>{t.production.trialTuningHistory}</summary>
                <ol className="mt-2 space-y-2">
                  {tuningHistory.map((version) => (
                    <li
                      key={version.version_id}
                      className="flex items-center justify-between gap-2 rounded-md border p-2"
                    >
                      <span className="font-mono text-xs break-all">
                        {version.version_id}
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => handleRestoreTuning(version.values)}
                        disabled={requestIntentLocked || tuningSaving}
                      >
                        {t.production.trialTuningRestore}
                      </Button>
                    </li>
                  ))}
                </ol>
              </details>
            )}
            {tuningMessage && (
              <p role="status" className="text-sm">
                {tuningMessage}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleResetTuning}
                disabled={requestIntentLocked || tuningSaving}
              >
                {t.production.trialTuningReset}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => void handleSaveTuning(false)}
                disabled={requestIntentLocked || tuningSaving}
              >
                {t.production.trialTuningSave}
              </Button>
              <Button
                type="button"
                onClick={() => void handleSaveTuning(true)}
                disabled={requestIntentLocked || tuningSaving || !canStart}
              >
                {tuningSaving && (
                  <Loader2
                    className="mr-2 h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                )}
                {t.production.trialAdjustAndRerun}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
      {(tuningSchemaQuery.isError || tuningBaseConfigQuery.isError) &&
        !locked && (
          <Alert variant="destructive">
            <AlertDescription>
              {t.production.trialTuningUnavailable}
            </AlertDescription>
          </Alert>
        )}

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
        ) : trial?.pending_submission ? null : (
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

      {trial?.pending_submission && (
        <Alert>
          <AlertDescription className="space-y-3">
            <p>{t.production.trialSubmissionUncertain}</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void handleRetryPending()}
              disabled={
                startRequestInFlight ||
                retryInFlight ||
                returnReconcileInFlight ||
                running ||
                Boolean(trial.active_run_id)
              }
            >
              <RotateCcw className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.trialRetry}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void handleReconcileAndReturnToFieldSetup()}
              disabled={
                startRequestInFlight ||
                retryInFlight ||
                returnReconcileInFlight ||
                running ||
                Boolean(trial.active_run_id)
              }
            >
              {returnReconcileInFlight && (
                <Loader2
                  className="mr-2 h-4 w-4 animate-spin"
                  aria-hidden="true"
                />
              )}
              {t.production.trialReconcileAndReturn}
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

      {diagnosisEnabled && (
        <Card data-testid="trial-diagnosis">
          <CardHeader>
            <CardTitle className="flex items-center justify-between gap-3 text-base">
              <span>{t.production.trialDiagnosisTitle}</span>
              {diagnosisGate && (
                <Badge
                  variant={diagnosisAcceptable ? "default" : "destructive"}
                >
                  {t.production.trialDiagnosisStatus(diagnosisGate.status)}
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            {diagnosisQuery.isPending && (
              <p role="status">{t.production.trialDiagnosisLoading}</p>
            )}
            {diagnosisQuery.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  {t.production.trialDiagnosisUnavailable}
                </AlertDescription>
              </Alert>
            )}
            {!diagnosisQuery.isPending &&
              !diagnosisQuery.isError &&
              !diagnosisGate && (
                <Alert variant="destructive">
                  <AlertDescription>
                    {t.production.trialDiagnosisUnavailable}
                  </AlertDescription>
                </Alert>
              )}
            {diagnosisGate && (
              <>
                <div className="space-y-1">
                  <p
                    className="font-semibold"
                    data-testid="trial-diagnosis-code"
                  >
                    {t.production.trialDiagnosisSummary(
                      diagnosisGate.failure_classification.code,
                    )}
                    <span className="ml-2 font-mono text-xs text-muted-foreground">
                      {diagnosisGate.failure_classification.code}
                    </span>
                  </p>
                  <p>
                    {t.production.trialDiagnosisNext}:{" "}
                    {t.production.trialDiagnosisAction(
                      diagnosisGate.failure_classification.code,
                    )}
                  </p>
                  {diagnosisGate.reason_codes &&
                    diagnosisGate.reason_codes.length > 0 && (
                      <ul className="list-disc pl-5 text-xs">
                        {diagnosisGate.reason_codes.map((reason) => (
                          <li key={reason}>
                            {t.production.trialDiagnosisReason(reason)}{" "}
                            <span className="font-mono text-muted-foreground">
                              ({reason})
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                </div>

                {diagnosisGate.stage_counts && (
                  <div className="space-y-2">
                    <p className="font-semibold">
                      {t.production.trialDiagnosisStages}
                    </p>
                    <dl
                      className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4"
                      data-testid="trial-debug-status-counts"
                    >
                      {(
                        [
                          [
                            "evaluated_frames",
                            diagnosisGate.stage_counts.evaluated_frames,
                          ],
                          [
                            "detected_frames",
                            diagnosisGate.stage_counts.detected_frames,
                          ],
                          [
                            "predicted_frames",
                            diagnosisGate.stage_counts.predicted_frames,
                          ],
                          [
                            "lost_frames",
                            diagnosisGate.stage_counts.lost_frames,
                          ],
                        ] as const
                      ).map(([label, count]) => (
                        <div key={label} className="rounded-md border p-2">
                          <dt>{t.production.trialDiagnosisStage(label)}</dt>
                          <dd className="font-mono">
                            {count.value ?? "—"} ·{" "}
                            {t.production.trialDiagnosisCounterStatus(
                              count.status,
                            )}
                          </dd>
                        </div>
                      ))}
                    </dl>
                    <ol
                      className="flex flex-wrap items-stretch gap-2"
                      data-testid="trial-detection-stage-chain"
                      aria-label={t.production.trialDiagnosisStageChain}
                    >
                      {(
                        [
                          [
                            "raw_candidates",
                            diagnosisGate.stage_counts.raw_candidates,
                          ],
                          [
                            "class_mapped_candidates",
                            diagnosisGate.stage_counts.class_mapped_candidates,
                          ],
                          [
                            "filtered_candidates",
                            diagnosisGate.stage_counts.filtered_candidates,
                          ],
                          [
                            "selected_candidates",
                            diagnosisGate.stage_counts.selected_candidates,
                          ],
                          ["tracklets", diagnosisGate.stage_counts.tracklets],
                        ] as const
                      ).map(([label, count], index) => (
                        <li key={label} className="flex items-center gap-2">
                          {index > 0 && (
                            <span aria-hidden="true" className="text-primary">
                              →
                            </span>
                          )}
                          <dl className="h-full rounded-md border p-2">
                            <dt>{t.production.trialDiagnosisStage(label)}</dt>
                            <dd className="font-mono">
                              {count.value ?? "—"} ·{" "}
                              {t.production.trialDiagnosisCounterStatus(
                                count.status,
                              )}
                            </dd>
                          </dl>
                        </li>
                      ))}
                    </ol>
                    <div data-testid="trial-stage-rejection-reasons">
                      <p className="font-medium">
                        {t.production.trialDiagnosisRejectionReasons}
                      </p>
                      {Object.keys(
                        diagnosisGate.stage_counts.rejection_reasons ?? {},
                      ).length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          {t.production.trialDiagnosisNoRejections}
                        </p>
                      ) : (
                        <ul className="list-disc pl-5 text-xs">
                          {Object.entries(
                            diagnosisGate.stage_counts.rejection_reasons ?? {},
                          ).map(([reason, count]) => (
                            <li key={reason}>
                              {t.production.trialDiagnosisRejectionReason(
                                reason,
                              )}
                              : {count}{" "}
                              <span className="font-mono text-muted-foreground">
                                ({reason})
                              </span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>
                )}

                <div
                  className="space-y-3"
                  data-testid="trial-typed-diagnostics"
                >
                  <p className="font-semibold">
                    {t.production.trialTrajectoryComparison}
                  </p>
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse text-left text-xs">
                      <thead>
                        <tr>
                          <th className="border p-2">
                            {t.production.trialDiagnosticMetric}
                          </th>
                          <th className="border p-2">
                            {t.production.trialDiagnosticRaw}
                          </th>
                          <th className="border p-2">
                            {t.production.trialDiagnosticCleaned}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {TRACK_DIAGNOSTIC_KEYS.map((metric) => {
                          const raw =
                            diagnosisGate.diagnostics.raw_track[metric];
                          const cleaned =
                            diagnosisGate.diagnostics.cleaned_track[metric];
                          return (
                            <tr key={metric}>
                              <th className="border p-2 font-medium">
                                {t.production.trialDiagnosticMetricLabel(
                                  metric,
                                )}
                              </th>
                              {[raw, cleaned].map((observation, index) => (
                                <td
                                  key={index === 0 ? "raw" : "cleaned"}
                                  className="border p-2 font-mono"
                                >
                                  {diagnosticObservationValue(observation)} ·{" "}
                                  {t.production.trialDiagnosisCounterStatus(
                                    observation.status,
                                  )}
                                </td>
                              ))}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  <div className="grid gap-2 sm:grid-cols-2">
                    {(
                      [
                        [
                          "ai_review_trigger_count",
                          diagnosisGate.diagnostics.ai_review_trigger_count,
                        ],
                        [
                          "ai_review_triggers_per_100_frames",
                          diagnosisGate.diagnostics
                            .ai_review_triggers_per_100_frames,
                        ],
                        [
                          "event_candidate_count",
                          diagnosisGate.diagnostics.event_candidate_count,
                        ],
                        [
                          "event_candidates_per_100_frames",
                          diagnosisGate.diagnostics
                            .event_candidates_per_100_frames,
                        ],
                        [
                          "max_pan_step_px",
                          diagnosisGate.diagnostics.follow_cam.max_pan_step_px,
                        ],
                        [
                          "max_pan_accel_px",
                          diagnosisGate.diagnostics.follow_cam.max_pan_accel_px,
                        ],
                        [
                          "max_zoom_step_ratio",
                          diagnosisGate.diagnostics.follow_cam
                            .max_zoom_step_ratio,
                        ],
                      ] as const
                    ).map(([metric, observation]) => (
                      <dl key={metric} className="rounded-md border p-2">
                        <dt>
                          {t.production.trialDiagnosticMetricLabel(metric)}
                        </dt>
                        <dd className="font-mono">
                          {diagnosticObservationValue(observation)} ·{" "}
                          {t.production.trialDiagnosisCounterStatus(
                            observation.status,
                          )}
                        </dd>
                      </dl>
                    ))}
                  </div>

                  <div data-testid="trial-diagnostic-rejection-status">
                    <p className="font-medium">
                      {t.production.trialDiagnosisRejectionReasons}:{" "}
                      {t.production.trialDiagnosisCounterStatus(
                        diagnosisGate.diagnostics.rejection_reasons.status,
                      )}
                    </p>
                  </div>
                </div>

                <div className="space-y-2">
                  <p className="font-semibold">
                    {t.production.trialDiagnosisEvidence}
                  </p>
                  <dl className="grid gap-2 sm:grid-cols-2">
                    {Object.entries(diagnosisGate.evidence).map(
                      ([name, status]) => (
                        <div key={name} className="rounded-md border p-2">
                          <dt>
                            {t.production.trialDiagnosisEvidenceKey(name)}
                          </dt>
                          <dd className="font-mono">
                            {t.production.trialDiagnosisEvidenceStatus(status)}
                          </dd>
                        </div>
                      ),
                    )}
                  </dl>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {detectorProbeRecovery && latestAuthoritativeRun && (
        <ProductionDetectorProbeController
          workflowId={workflowId}
          parentTrialId={latestAuthoritativeRun.run_id}
          onStartNewDevelopmentBatch={focusTrialSettings}
        />
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
        diagnosisAcceptable &&
        !trial?.accepted && (
          <section
            className="space-y-3 rounded-md border p-4"
            aria-labelledby="trial-visual-confirmation-title"
          >
            <h3
              id="trial-visual-confirmation-title"
              className="text-sm font-semibold"
            >
              {t.production.trialVisualConfirmationTitle}
            </h3>
            <p className="text-sm text-muted-foreground">
              {t.production.trialVisualConfirmationDescription}
            </p>
            <div className="flex items-start gap-2">
              <Checkbox
                id="trial-visual-confirmation"
                checked={visualConfirmed}
                onCheckedChange={(checked) =>
                  setVisualConfirmed(checked === true)
                }
                disabled={evidenceRefreshing}
              />
              <Label htmlFor="trial-visual-confirmation">
                {t.production.trialVisualConfirmationLabel}
              </Label>
            </div>
            {visualConfirmed && (
              <Button
                type="button"
                onClick={() => void handleAccept()}
                disabled={!canAcceptTrial || evidenceRefreshing}
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
          </section>
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
