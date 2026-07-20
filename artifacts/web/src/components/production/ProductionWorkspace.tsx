import { useEffect, useRef, useState, type Ref } from "react";
import { Check, ChevronLeft, ChevronRight, Save } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useLanguage } from "@/contexts/LanguageContext";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import { productionFullRunRequiresStop } from "@/lib/productionBroadcast";
import type {
  ProductionConfigEvidence,
  ProductionPendingConfigConfirmation,
} from "@/lib/productionConfigFreeze";
import type { ProductionTrialState } from "@/lib/productionTrial";
import {
  canEnterProductionStage,
  deriveProductionWorkflow,
  productionTrialRequiresStop,
  type ProductionDraft,
  type ProductionFullRunState,
  type ProductionProductEvidence,
  type ProductionUserStage,
  type SourceSignature,
} from "@/lib/productionWorkflow";
import { ProductionCalibrationStep } from "./ProductionCalibrationStep";
import { ProductionFullRunStep } from "./ProductionFullRunStep";
import { ProductionTrialStep } from "./ProductionTrialStep";

const USER_STAGES: ProductionUserStage[] = [
  "source",
  "calibration",
  "trial",
  "full_tracking",
  "ready",
];

export interface ProductionWorkspaceProps {
  draft: ProductionDraft;
  videos: SourceSignature[];
  sourceIssue?: "missing" | "changed" | null;
  notice?: string | null;
  error?: string | null;
  requestedRunId?: string | null;
  onSourceChange: (source: SourceSignature) => void;
  onCalibrationChange: (calibration: ProductionCalibrationDraft) => void;
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
  onInvalidate: (from: "calibration") => boolean;
  onSaveExit: () => void;
  onStartNew: () => void;
  startNewButtonRef?: Ref<HTMLButtonElement>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_024 * 1_024) return `${(bytes / 1_024).toFixed(1)} KB`;
  return `${(bytes / (1_024 * 1_024)).toFixed(1)} MB`;
}

export function ProductionWorkspace({
  draft,
  videos,
  sourceIssue = null,
  notice = null,
  error = null,
  requestedRunId = null,
  onSourceChange,
  onCalibrationChange,
  onTrialChange,
  onPendingConfigChange,
  onConfirmedConfigChange,
  onFullRunChange,
  onPersistCurrent,
  onVerifiedProduct,
  onParentRunIdChange,
  onInvalidate,
  onSaveExit,
  onStartNew,
  startNewButtonRef,
}: ProductionWorkspaceProps) {
  const { t } = useLanguage();
  const derived = deriveProductionWorkflow(draft);
  const trialDiscardBlocked = productionTrialRequiresStop(draft.trial);
  const fullRunDiscardBlocked = productionFullRunRequiresStop(draft.full_run);
  const workDiscardBlocked = trialDiscardBlocked || fullRunDiscardBlocked;
  const pendingTrialNeedsReconcile = Boolean(draft.trial?.pending_submission);
  const pendingFullRunNeedsReconcile = Boolean(
    draft.full_run?.pending_submission,
  );
  const pendingSubmissionNeedsReconcile =
    pendingTrialNeedsReconcile || pendingFullRunNeedsReconcile;
  const pendingReconciliationExplanationId =
    "production-pending-reconciliation-explanation";
  const activeTrialNeedsStop =
    trialDiscardBlocked && !pendingTrialNeedsReconcile;
  const [viewStage, setViewStage] = useState<ProductionUserStage>(() =>
    fullRunDiscardBlocked
      ? "full_tracking"
      : trialDiscardBlocked
        ? "trial"
        : sourceIssue
          ? "source"
          : draft.confirmed_config && !draft.full_run
            ? "trial"
            : derived.user_stage,
  );
  const [calibrationPreviewUsable, setCalibrationPreviewUsable] =
    useState(false);
  const [trialUsable, setTrialUsable] = useState(false);
  const [pendingUpstreamEdit, setPendingUpstreamEdit] = useState<
    { kind: "calibration" } | { kind: "source"; source: SourceSignature } | null
  >(null);
  const [activeStopFocusRequest, setActiveStopFocusRequest] = useState(0);
  const [fullRunFocusRequest, setFullRunFocusRequest] = useState(0);
  const backButtonRef = useRef<HTMLButtonElement>(null);
  const sourceSelectRef = useRef<HTMLSelectElement>(null);
  const stopButtonRef = useRef<HTMLButtonElement>(null);
  const trialHeadingRef = useRef<HTMLHeadingElement>(null);
  const upstreamFocusTargetRef = useRef<"source" | "back">("back");
  const sourceInCatalog = videos.find(
    (video) => video.path === draft.source?.path,
  );

  const stageLabels: Record<ProductionUserStage, string> = {
    source: t.production.stages.source,
    calibration: t.production.stages.calibration,
    trial: t.production.stages.trial,
    full_tracking: t.production.stages.fullTracking,
    ready: t.production.stages.ready,
  };
  const stageDescriptions: Record<ProductionUserStage, string> = {
    source: t.production.sourceDescription,
    calibration: t.production.calibrationDescription,
    trial: t.production.trialDescription,
    full_tracking: t.production.fullTrackingDescription,
    ready: t.production.readyDescription,
  };
  const stagePlaceholders: Record<
    Exclude<ProductionUserStage, "source">,
    string
  > = {
    calibration: t.production.calibrationPending,
    trial: t.production.trialPending,
    full_tracking: t.production.fullTrackingPending,
    ready: t.production.readyPending,
  };

  const derivedStageIndex = USER_STAGES.indexOf(derived.user_stage);
  const reachableStageIndex = fullRunDiscardBlocked
    ? USER_STAGES.indexOf("full_tracking")
    : trialDiscardBlocked
      ? USER_STAGES.indexOf("trial")
      : sourceIssue
        ? 0
        : derivedStageIndex;
  const requestedStageIndex = USER_STAGES.indexOf(viewStage);
  const currentIndex = Math.min(requestedStageIndex, reachableStageIndex);
  const effectiveStage = USER_STAGES[currentIndex];
  const nextStage = USER_STAGES[currentIndex + 1] ?? null;

  useEffect(() => {
    if (!trialDiscardBlocked || activeStopFocusRequest === 0) return;
    if (effectiveStage === "trial") {
      (activeTrialNeedsStop
        ? stopButtonRef.current
        : trialHeadingRef.current
      )?.focus();
    }
  }, [
    activeStopFocusRequest,
    activeTrialNeedsStop,
    effectiveStage,
    trialDiscardBlocked,
  ]);

  function canEnterUserStage(stage: ProductionUserStage): boolean {
    switch (stage) {
      case "source":
        return true;
      case "calibration":
        return canEnterProductionStage(draft, "calibration");
      case "trial":
        return canEnterProductionStage(draft, "trial");
      case "full_tracking":
        return canEnterProductionStage(draft, "full_tracking");
      case "ready":
        return derived.user_stage === "ready";
    }
  }

  const canContinue =
    !workDiscardBlocked &&
    sourceIssue === null &&
    nextStage !== null &&
    currentIndex + 1 <= reachableStageIndex &&
    (effectiveStage !== "calibration" || calibrationPreviewUsable) &&
    (effectiveStage !== "trial" || trialUsable) &&
    canEnterUserStage(nextStage);

  function blockActiveWorkDiscard(): boolean {
    if (!workDiscardBlocked) return false;
    setPendingUpstreamEdit(null);
    if (fullRunDiscardBlocked) {
      setViewStage("full_tracking");
      setFullRunFocusRequest((request) => request + 1);
    } else {
      setViewStage("trial");
      setActiveStopFocusRequest((request) => request + 1);
    }
    return true;
  }

  function handleSourceSelection(path: string) {
    if (blockActiveWorkDiscard()) return;
    const source = videos.find((video) => video.path === path);
    if (!source) return;
    if (
      draft.trial?.attempts.length ||
      draft.trial?.pending_submission ||
      draft.confirmed_config ||
      draft.pending_config_confirmation ||
      draft.full_run
    ) {
      upstreamFocusTargetRef.current = "source";
      setPendingUpstreamEdit({ kind: "source", source });
      return;
    }
    applySourceSelection(source);
  }

  function applySourceSelection(source: SourceSignature) {
    setCalibrationPreviewUsable(false);
    onSourceChange(source);
    setViewStage("source");
  }

  function handleNext() {
    if (!canContinue || !nextStage) return;
    setViewStage(nextStage);
  }

  function handleBack() {
    if (currentIndex <= 0) return;
    if (blockActiveWorkDiscard()) return;
    if (
      effectiveStage === "trial" &&
      (draft.trial?.attempts.length ||
        draft.trial?.pending_submission ||
        draft.confirmed_config ||
        draft.pending_config_confirmation)
    ) {
      requestCalibrationEdit();
      return;
    }
    setViewStage(USER_STAGES[currentIndex - 1]);
  }

  function requestCalibrationEdit() {
    upstreamFocusTargetRef.current = "back";
    setPendingUpstreamEdit({ kind: "calibration" });
  }

  function summaryDetail(stage: ProductionUserStage): string {
    switch (stage) {
      case "source":
        return draft.source
          ? `${draft.source.path} · ${formatBytes(draft.source.size_bytes)}`
          : t.common.notAvailable;
      case "calibration":
        return draft.calibration
          ? `${t.production.confirmedFrames(draft.calibration.confirmed_frames.length)} · ${draft.calibration.polygon_digest ?? t.common.notAvailable}`
          : t.common.notAvailable;
      case "trial":
        return (
          [draft.trial?.accepted?.run_id, draft.confirmed_config?.name]
            .filter(Boolean)
            .join(" · ") || t.common.notAvailable
        );
      case "full_tracking":
        if (!draft.full_run?.current_run_id) return t.common.notAvailable;
        return `${draft.full_run.current_run_id} · ${
          draft.full_run.attempts.find(
            (attempt) => attempt.run_id === draft.full_run?.current_run_id,
          )?.last_observed.workflow_state ?? t.common.notAvailable
        }`;
      case "ready":
        return draft.verified_product?.artifact_name ?? t.common.notAvailable;
    }
  }

  function completedStageSummary(stage: ProductionUserStage) {
    return (
      <Card
        key={stage}
        data-testid={`completed-stage-${stage}`}
        className="shadow-none"
      >
        <CardContent className="flex items-center gap-3 p-4">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-primary">
            <Check className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">
              {stage === "source"
                ? t.production.sourceCompleted
                : stageLabels[stage]}
            </p>
            <p className="truncate font-mono text-xs text-foreground/75">
              {summaryDetail(stage)}
            </p>
          </div>
          <Badge variant="secondary">{t.production.completed}</Badge>
        </CardContent>
      </Card>
    );
  }

  const currentStepCard = (
    <Card data-testid={`production-step-${effectiveStage}`}>
      <CardHeader>
        <CardTitle>
          <h2
            ref={trialHeadingRef}
            tabIndex={effectiveStage === "trial" ? -1 : undefined}
            className="text-lg"
          >
            {effectiveStage === "source"
              ? t.production.sourceTitle
              : stageLabels[effectiveStage]}
          </h2>
        </CardTitle>
        <CardDescription className="text-foreground/75">
          {stageDescriptions[effectiveStage]}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {effectiveStage === "source" ? (
          <>
            {sourceIssue && (
              <Alert
                variant={sourceIssue === "missing" ? "destructive" : "default"}
              >
                <AlertTitle>
                  {sourceIssue === "missing"
                    ? t.production.sourceUnavailable
                    : t.production.sourceChanged}
                </AlertTitle>
                {sourceIssue === "changed" && sourceInCatalog && (
                  <AlertDescription className="mt-3">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        handleSourceSelection(sourceInCatalog.path)
                      }
                    >
                      {t.production.useCurrentSource}
                    </Button>
                  </AlertDescription>
                )}
              </Alert>
            )}
            <div className="space-y-2">
              <label
                htmlFor="production-source"
                className="text-sm font-medium"
              >
                {t.production.sourceLabel}
              </label>
              <select
                ref={sourceSelectRef}
                id="production-source"
                value={sourceInCatalog?.path ?? ""}
                onChange={(event) => handleSourceSelection(event.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                data-testid="production-source-select"
              >
                <option value="">{t.production.sourcePlaceholder}</option>
                {videos.map((video) => (
                  <option key={video.path} value={video.path}>
                    {video.path} · {formatBytes(video.size_bytes)}
                  </option>
                ))}
              </select>
              {!draft.source && (
                <p className="text-xs text-foreground/75">
                  {t.production.sourceRequired}
                </p>
              )}
            </div>
          </>
        ) : effectiveStage === "calibration" && draft.source ? (
          <ProductionCalibrationStep
            source={draft.source}
            calibration={draft.calibration}
            onChange={onCalibrationChange}
            onUsabilityChange={setCalibrationPreviewUsable}
          />
        ) : effectiveStage === "trial" && draft.source && draft.calibration ? (
          <ProductionTrialStep
            workflowId={draft.workflow_id}
            source={draft.source}
            calibration={draft.calibration}
            trial={draft.trial}
            pendingConfig={draft.pending_config_confirmation}
            confirmedConfig={draft.confirmed_config}
            onTrialChange={onTrialChange}
            onPendingConfigChange={onPendingConfigChange}
            onConfirmedConfigChange={onConfirmedConfigChange}
            onReturnToFieldSetup={handleBack}
            onPendingReconciledReturnToFieldSetup={requestCalibrationEdit}
            onUsabilityChange={setTrialUsable}
            stopButtonRef={stopButtonRef}
          />
        ) : (effectiveStage === "full_tracking" ||
            effectiveStage === "ready") &&
          draft.source &&
          draft.calibration &&
          draft.trial &&
          draft.confirmed_config ? (
          <ProductionFullRunStep
            workflowId={draft.workflow_id}
            source={draft.source}
            calibration={draft.calibration}
            trial={draft.trial}
            confirmedConfig={draft.confirmed_config}
            fullRun={draft.full_run}
            verifiedProduct={draft.verified_product}
            requestedRunId={requestedRunId}
            focusRequest={fullRunFocusRequest}
            onFullRunChange={onFullRunChange}
            onPersistCurrent={onPersistCurrent}
            onVerifiedProduct={onVerifiedProduct}
            onParentRunIdChange={onParentRunIdChange}
          />
        ) : (
          <Alert>
            <AlertDescription>
              {stagePlaceholders[effectiveStage]}
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
      <CardFooter className="flex flex-col-reverse gap-2 border-t pt-5 sm:flex-row sm:justify-between">
        <div>
          {currentIndex > 0 && (
            <Button
              ref={backButtonRef}
              type="button"
              variant="outline"
              onClick={handleBack}
              disabled={pendingSubmissionNeedsReconcile}
              aria-describedby={
                pendingSubmissionNeedsReconcile
                  ? pendingReconciliationExplanationId
                  : undefined
              }
            >
              <ChevronLeft className="mr-2 h-4 w-4" aria-hidden="true" />
              {t.production.back}
            </Button>
          )}
        </div>
        <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
          <Button type="button" variant="ghost" onClick={onSaveExit}>
            <Save className="mr-2 h-4 w-4" aria-hidden="true" />
            {t.production.saveExit}
          </Button>
          <Button
            type="button"
            onClick={handleNext}
            disabled={!canContinue}
            className="disabled:border-border disabled:bg-muted disabled:text-foreground disabled:opacity-100"
          >
            {t.production.next}
            <ChevronRight className="ml-2 h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </CardFooter>
    </Card>
  );

  return (
    <section
      className="mx-auto max-w-4xl space-y-5"
      aria-labelledby="production-title"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1
            id="production-title"
            className="text-2xl font-bold tracking-tight"
          >
            {t.production.title}
          </h1>
          <p className="mt-1 text-sm text-foreground/75">
            {t.production.subtitle}
          </p>
        </div>
        <Button
          ref={startNewButtonRef}
          type="button"
          variant="outline"
          disabled={pendingSubmissionNeedsReconcile}
          aria-describedby={
            pendingSubmissionNeedsReconcile
              ? pendingReconciliationExplanationId
              : undefined
          }
          onClick={() => {
            if (!blockActiveWorkDiscard()) onStartNew();
          }}
        >
          {t.production.startNew}
        </Button>
      </div>

      <p className="text-sm font-medium text-primary" aria-live="polite">
        {t.production.stepLabel(
          currentIndex + 1,
          USER_STAGES.length,
          stageLabels[effectiveStage],
        )}
      </p>

      {notice && (
        <Alert role="status" aria-live="polite">
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {pendingSubmissionNeedsReconcile && (
        <Alert>
          <AlertDescription id={pendingReconciliationExplanationId}>
            {pendingFullRunNeedsReconcile
              ? t.production.pendingFullRunMustReconcile
              : t.production.pendingTrialMustReconcile}
          </AlertDescription>
        </Alert>
      )}
      {workDiscardBlocked &&
        (activeStopFocusRequest > 0 || fullRunFocusRequest > 0) && (
          <Alert role="status">
            <AlertDescription>
              {fullRunDiscardBlocked
                ? draft.full_run?.pending_submission
                  ? t.production.pendingFullRunMustReconcile
                  : t.production.activeFullRunMustStop
                : pendingTrialNeedsReconcile
                  ? t.production.pendingTrialMustReconcile
                  : t.production.activeTrialMustStop}
            </AlertDescription>
          </Alert>
        )}

      <div className="space-y-4">
        {USER_STAGES.slice(0, reachableStageIndex + 1).map((stage, index) => {
          if (index === currentIndex) {
            return <div key={stage}>{currentStepCard}</div>;
          }
          return index < reachableStageIndex
            ? completedStageSummary(stage)
            : null;
        })}
      </div>

      <AlertDialog
        open={pendingUpstreamEdit !== null}
        onOpenChange={(open) => {
          if (!open) setPendingUpstreamEdit(null);
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (upstreamFocusTargetRef.current === "source") {
              sourceSelectRef.current?.focus();
            } else {
              backButtonRef.current?.focus();
            }
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t.production.upstreamUnlockTitle}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t.production.upstreamUnlockDescription}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>
              {t.production.upstreamKeepLocked}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                const pending = pendingUpstreamEdit;
                if (!pending) return;
                if (pending.kind === "source") {
                  applySourceSelection(pending.source);
                  setPendingUpstreamEdit(null);
                  return;
                }
                if (!onInvalidate("calibration")) {
                  event.preventDefault();
                  return;
                }
                setPendingUpstreamEdit(null);
                setCalibrationPreviewUsable(false);
                setTrialUsable(false);
                setViewStage("calibration");
              }}
            >
              {t.production.upstreamUnlockConfirm}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
