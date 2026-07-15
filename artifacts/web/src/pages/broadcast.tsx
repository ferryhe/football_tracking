import { useEffect, useMemo, useState } from "react";
import { useCreateRun } from "@workspace/api-client-react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RadioTower,
  RefreshCw,
} from "lucide-react";
import { useLocation, useSearch } from "wouter";

import { BroadcastRenderStep } from "@/components/broadcast/BroadcastRenderStep";
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
  useBroadcastWorkflowController,
  type BroadcastControllerError,
} from "@/hooks/useBroadcastWorkflowController";
import {
  buildBroadcastCreateRequest,
  localizeBroadcastWorkflowMessage,
  type BroadcastWorkflowStateName,
} from "@/lib/broadcastWorkflow";

function runIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("run")?.trim();
  return value || null;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

function controllerErrorMessage(
  error: BroadcastControllerError,
  fallback: Record<BroadcastControllerError["code"], string>,
): string {
  if (error.message) return error.message;
  return errorMessage(error.cause, fallback[error.code]);
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
  const runId = useMemo(() => runIdFromSearch(search), [search]);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [reviewerId, setReviewerId] = useState("operator");
  const [reviewDecisions, setReviewDecisions] = useState<
    BroadcastReviewDecision[]
  >([]);
  const createRun = useCreateRun();
  const controller = useBroadcastWorkflowController({
    parentRunId: runId,
    enabled: Boolean(runId),
    language,
  });
  const { recovery, parent: parentRun } = controller;
  const recomputeRecoveryMode = controller.review.recomputeRecoveryMode;
  const recoveryAttemptState = controller.review.recoveryAttemptState;

  useEffect(() => {
    setReviewDecisions([]);
  }, [controller.review.data?.queue_sha256]);

  async function handleSetup(input: BroadcastSetupInput) {
    setSetupError(null);
    const request = buildBroadcastCreateRequest(input);
    if (!request.ok) {
      setSetupError(
        request.messages
          .map((message) => localizeBroadcastWorkflowMessage(message, language))
          .join(" "),
      );
      return;
    }
    try {
      const created = await createRun.mutateAsync({ data: request.value });
      navigate(`/broadcast?run=${encodeURIComponent(created.run_id)}`);
    } catch (error) {
      setSetupError(errorMessage(error, t.broadcast.startFailed));
    }
  }

  const activeStep = stepIndex(recovery.state);
  const steps = [
    t.broadcast.setupStep,
    t.broadcast.reviewStep,
    t.broadcast.renderStep,
  ];
  const loading = controller.pending.initialLoad;
  const actionError = controller.errors.action
    ? controllerErrorMessage(controller.errors.action, {
        submitFailed: t.broadcast.submitFailed,
        recomputeFailed: t.broadcast.recomputeFailed,
        renderFailed: t.broadcast.renderFailed,
        cancelFailed: t.broadcast.cancelFailed,
        staleEvidence: t.broadcast.staleEvidence,
      })
    : null;
  const combinedError =
    setupError ??
    actionError ??
    (controller.errors.query
      ? errorMessage(controller.errors.query, t.broadcast.loadFailed)
      : null);
  const workflowMessages = controller.workflowMessages;
  const localizedReviewResponse = controller.review.localizedData;
  const montageResolution = {
    urls: controller.montage.urlsByCandidateId,
    messages: controller.montage.messages,
  };
  const deliveryUrls = controller.delivery.urls;

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
              onClick={() => void controller.actions.refresh()}
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
          error={setupError}
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
              onClick={() => void controller.actions.cancel()}
              disabled={controller.pending.cancel}
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
                  onClick={() => void controller.actions.retryRecompute()}
                  disabled={
                    controller.pending.recompute ||
                    recoveryAttemptState === "pending"
                  }
                >
                  {controller.pending.recompute ||
                  recoveryAttemptState === "pending"
                    ? t.broadcast.retryingRecompute
                    : t.broadcast.retryRecompute}
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {recomputeRecoveryMode === "none" && controller.review.isLoading && (
            <Card>
              <CardContent className="flex items-center gap-3 py-10">
                <Loader2 className="h-5 w-5 animate-spin" />
                {t.broadcast.loading}
              </CardContent>
            </Card>
          )}
          {recomputeRecoveryMode === "none" && controller.review.isError && (
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
                  disabled={controller.pending.review}
                />
              </div>
              <BroadcastReviewStep
                response={localizedReviewResponse}
                montageUrlsByCandidateId={montageResolution.urls}
                decisions={reviewDecisions}
                onDecisionsChange={setReviewDecisions}
                onSubmit={(decisions) =>
                  void controller.actions.submitReview(decisions, reviewerId)
                }
                isSubmitting={controller.pending.review}
                disabled={
                  montageResolution.messages.length > 0 ||
                  ((localizedReviewResponse.items ?? []).some(
                    (item) => (item.candidates ?? []).length > 0,
                  ) &&
                    !reviewerId.trim())
                }
                error={actionError}
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
              onClick={() => void controller.actions.cancel()}
              disabled={controller.pending.cancel}
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
            onRender={(request) => void controller.actions.render(request)}
            isRendering={
              controller.pending.render || recovery.state === "rendering"
            }
            disabled={recovery.state === "ready"}
            error={actionError}
            onCancel={
              recovery.state === "rendering"
                ? () => void controller.actions.cancel()
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
