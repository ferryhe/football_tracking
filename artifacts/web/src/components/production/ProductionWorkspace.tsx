import { useState, type Ref } from "react";
import { Check, ChevronLeft, ChevronRight, Save } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  canEnterProductionStage,
  deriveProductionWorkflow,
  type ProductionDraft,
  type ProductionUserStage,
  type SourceSignature,
} from "@/lib/productionWorkflow";

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
  onSourceChange: (source: SourceSignature) => void;
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
  onSourceChange,
  onSaveExit,
  onStartNew,
  startNewButtonRef,
}: ProductionWorkspaceProps) {
  const { t } = useLanguage();
  const derived = deriveProductionWorkflow(draft);
  const [viewStage, setViewStage] = useState<ProductionUserStage>(() =>
    sourceIssue ? "source" : derived.user_stage,
  );
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
  const reachableStageIndex = sourceIssue ? 0 : derivedStageIndex;
  const requestedStageIndex = USER_STAGES.indexOf(viewStage);
  const currentIndex = Math.min(requestedStageIndex, reachableStageIndex);
  const effectiveStage = USER_STAGES[currentIndex];
  const nextStage = USER_STAGES[currentIndex + 1] ?? null;

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
    sourceIssue === null &&
    nextStage !== null &&
    currentIndex + 1 <= reachableStageIndex &&
    canEnterUserStage(nextStage);

  function handleSourceSelection(path: string) {
    const source = videos.find((video) => video.path === path);
    if (!source) return;
    onSourceChange(source);
    setViewStage("source");
  }

  function handleNext() {
    if (!canContinue || !nextStage) return;
    setViewStage(nextStage);
  }

  function handleBack() {
    if (currentIndex > 0) setViewStage(USER_STAGES[currentIndex - 1]);
  }

  function summaryDetail(stage: ProductionUserStage): string {
    switch (stage) {
      case "source":
        return draft.source
          ? `${draft.source.path} · ${formatBytes(draft.source.size_bytes)}`
          : t.common.notAvailable;
      case "calibration":
        return draft.calibration
          ? `${t.production.confirmedFrames(draft.calibration.confirmed_frame_ids.length)} · ${draft.calibration.polygon_digest}`
          : t.common.notAvailable;
      case "trial":
        return (
          [draft.trial?.accepted_run_id, draft.confirmed_config?.name]
            .filter(Boolean)
            .join(" · ") || t.common.notAvailable
        );
      case "full_tracking":
        return draft.full_run
          ? `${draft.full_run.run_id} · ${draft.full_run.status}`
          : t.common.notAvailable;
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
          <h2 className="text-lg">
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
            <Button type="button" variant="outline" onClick={handleBack}>
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
          onClick={onStartNew}
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
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
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
    </section>
  );
}
