import { useRef, useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Link, useLocation, useSearch } from "wouter";
import { useListInputVideos } from "@workspace/api-client-react";

import { ProductionWorkspace } from "@/components/production/ProductionWorkspace";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useLanguage } from "@/contexts/LanguageContext";
import {
  createSafeBrowserStorage,
  type SafeBrowserStorage,
} from "@/lib/browserStorage";
import { runIdFromSearch } from "@/lib/productionCutover";
import { productionFullRunRequiresStop } from "@/lib/productionBroadcast";
import {
  clearProductionDraft,
  createProductionDraft,
  invalidateProductionDraft,
  loadProductionDraft,
  productionTrialRequiresStop,
  requiresDraftReplacementConfirmation,
  saveProductionDraft,
  sourceSignaturesMatch,
  updateConfirmedProductionConfig,
  updatePendingConfigConfirmation,
  updateProductionCalibration,
  updateProductionFullRun,
  updateProductionSource,
  updateProductionTrial,
  updateVerifiedProductionProduct,
  type ProductionDraft,
  type ProductionDraftLoadResult,
  type ProductionFullRunState,
  type ProductionProductEvidence,
  type SourceSignature,
} from "@/lib/productionWorkflow";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import type {
  ProductionConfigEvidence,
  ProductionPendingConfigConfirmation,
} from "@/lib/productionConfigFreeze";
import {
  canonicalJson,
  type ProductionTrialState,
} from "@/lib/productionTrial";

function sameSnapshot(left: unknown, right: unknown): boolean {
  try {
    return canonicalJson(left) === canonicalJson(right);
  } catch {
    return false;
  }
}

function queryErrorMessage(error: unknown): string | null {
  return error instanceof Error && error.message ? error.message : null;
}

type ProductionNotice =
  | "migrated"
  | "restored"
  | "savedLocally"
  | "sourceReset"
  | "activeTrialMustStop"
  | "pendingTrialMustReconcile"
  | "activeFullRunMustStop"
  | "pendingFullRunMustReconcile"
  | "storageFallback";

interface ProductionError {
  code: "saveFailed";
  detail: string;
}

interface ProductionPageProps {
  storage?: SafeBrowserStorage;
}

export function ProductionPageContent({
  storage: storageOverride,
}: ProductionPageProps = {}) {
  const { t } = useLanguage();
  const [, setLocation] = useLocation();
  const search = useSearch();
  const requestedRunId = runIdFromSearch(search);
  const migratedFrom = new URLSearchParams(search).get("from");
  const [storage] = useState(
    () => storageOverride ?? createSafeBrowserStorage(),
  );
  const [initialLoad] = useState<ProductionDraftLoadResult>(() =>
    loadProductionDraft(storage),
  );
  const [recovery, setRecovery] = useState<ProductionDraftLoadResult | null>(
    () =>
      initialLoad.status === "corrupt" ||
      initialLoad.status === "unsupported" ||
      initialLoad.status === "unavailable"
        ? initialLoad
        : null,
  );
  const [draft, setDraft] = useState<ProductionDraft>(() =>
    initialLoad.status === "restored"
      ? initialLoad.draft
      : createProductionDraft(),
  );
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [notice, setNotice] = useState<ProductionNotice | null>(() => {
    if (!storage.isPersistent) return "storageFallback";
    if (initialLoad.status !== "restored") return null;
    return initialLoad.migrated ? "migrated" : "restored";
  });
  const [error, setError] = useState<ProductionError | null>(null);
  const [replacement, setReplacement] = useState<ProductionDraft | null>(null);
  const startNewButtonRef = useRef<HTMLButtonElement>(null);
  const inputs = useListInputVideos();
  const videos: SourceSignature[] = (inputs.data?.videos ?? []).map(
    (video) => ({
      path: video.path,
      size_bytes: video.size_bytes,
      modified_at: video.modified_at,
    }),
  );

  const catalogSource = draft.source
    ? (videos.find((video) => video.path === draft.source?.path) ?? null)
    : null;
  const sourceIssue = draft.source
    ? !catalogSource
      ? "missing"
      : sourceSignaturesMatch(draft.source, catalogSource)
        ? null
        : "changed"
    : null;

  function persist(nextDraft: ProductionDraft): boolean {
    const result = saveProductionDraft(storage, nextDraft);
    if (!result.ok) {
      setError({ code: "saveFailed", detail: result.message });
      return false;
    }
    setError(null);
    return true;
  }

  function commitDraft(
    updater: (current: ProductionDraft) => ProductionDraft,
  ): boolean {
    const nextDraft = updater(draftRef.current);
    if (!persist(nextDraft)) return false;
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    setNotice(storage.isPersistent ? "savedLocally" : "storageFallback");
    return true;
  }

  function blockActiveWorkDiscard(): boolean {
    if (productionFullRunRequiresStop(draftRef.current.full_run)) {
      setNotice(
        draftRef.current.full_run?.pending_submission
          ? "pendingFullRunMustReconcile"
          : "activeFullRunMustStop",
      );
      return true;
    }
    if (!productionTrialRequiresStop(draftRef.current.trial)) return false;
    setNotice(
      draftRef.current.trial?.pending_submission
        ? "pendingTrialMustReconcile"
        : "activeTrialMustStop",
    );
    return true;
  }

  function handleSourceChange(source: SourceSignature) {
    if (blockActiveWorkDiscard()) return;
    const resetsDownstream =
      draftRef.current.source !== null &&
      !sourceSignaturesMatch(draftRef.current.source, source);
    if (commitDraft((current) => updateProductionSource(current, source))) {
      setNotice(
        !storage.isPersistent
          ? "storageFallback"
          : resetsDownstream
            ? "sourceReset"
            : "savedLocally",
      );
    }
  }

  function handleCalibrationChange(calibration: ProductionCalibrationDraft) {
    if (blockActiveWorkDiscard()) return;
    commitDraft((current) => updateProductionCalibration(current, calibration));
  }

  function handleTrialChange(
    trial: ProductionTrialState,
    expected: ProductionTrialState | null,
  ): boolean {
    if (!sameSnapshot(draftRef.current.trial, expected)) return false;
    return commitDraft((current) => updateProductionTrial(current, trial));
  }

  function handlePendingConfigChange(
    pending: ProductionPendingConfigConfirmation | null,
    expected: ProductionPendingConfigConfirmation | null,
    expectedAcceptedRunId: string,
  ): boolean {
    if (
      !sameSnapshot(draftRef.current.pending_config_confirmation, expected) ||
      draftRef.current.trial?.accepted?.run_id !== expectedAcceptedRunId
    )
      return false;
    return commitDraft((current) =>
      updatePendingConfigConfirmation(current, pending),
    );
  }

  function handleConfirmedConfigChange(
    confirmed: ProductionConfigEvidence,
    expectedPending: ProductionPendingConfigConfirmation,
  ): boolean {
    if (
      !sameSnapshot(
        draftRef.current.pending_config_confirmation,
        expectedPending,
      )
    )
      return false;
    return commitDraft((current) =>
      updateConfirmedProductionConfig(current, confirmed),
    );
  }

  function handleInvalidate(from: "calibration"): boolean {
    if (blockActiveWorkDiscard()) return false;
    return commitDraft((current) => invalidateProductionDraft(current, from));
  }

  function handleSaveExit() {
    if (persist(draftRef.current)) setLocation("/history");
  }

  function replaceWith(nextDraft: ProductionDraft) {
    if (blockActiveWorkDiscard()) return;
    const result = clearProductionDraft(storage);
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    setReplacement(null);
    setRecovery(null);
    setNotice(storage.isPersistent ? null : "storageFallback");
    setError(result.ok ? null : { code: "saveFailed", detail: result.message });
  }

  function handleStartNew() {
    if (blockActiveWorkDiscard()) return;
    const nextDraft = createProductionDraft();
    if (
      requiresDraftReplacementConfirmation(
        draftRef.current,
        nextDraft.workflow_id,
      )
    ) {
      setReplacement(nextDraft);
      return;
    }
    replaceWith(nextDraft);
  }

  function handleDiscardRecovery() {
    replaceWith(createProductionDraft());
  }

  function handleFullRunChange(
    fullRun: ProductionFullRunState,
    expectedRevision: number,
  ): boolean {
    const currentRevision = draftRef.current.full_run?.revision ?? 0;
    if (currentRevision !== expectedRevision) return false;
    return commitDraft((current) =>
      updateProductionFullRun(current, fullRun, expectedRevision),
    );
  }

  function handlePersistCurrent(expectedRevision: number): boolean {
    if ((draftRef.current.full_run?.revision ?? 0) !== expectedRevision) {
      return false;
    }
    return persist(draftRef.current);
  }

  function handleVerifiedProduct(
    product: ProductionProductEvidence,
    expectedRevision: number,
  ): boolean {
    if ((draftRef.current.full_run?.revision ?? 0) !== expectedRevision) {
      return false;
    }
    return commitDraft((current) =>
      updateVerifiedProductionProduct(current, product, expectedRevision),
    );
  }

  function handleParentRunIdChange(runId: string) {
    setLocation(`/production?run=${encodeURIComponent(runId)}`, {
      replace: true,
    });
  }

  if (recovery) {
    const description =
      recovery.status === "corrupt"
        ? t.production.corruptDraft
        : recovery.status === "unsupported"
          ? t.production.unsupportedDraft(recovery.version)
          : t.production.unavailableStorage;
    return (
      <section
        className="mx-auto max-w-2xl"
        aria-labelledby="production-recovery-title"
      >
        <Card>
          <CardHeader>
            <CardTitle>
              <h1
                id="production-recovery-title"
                className="flex items-center gap-2 text-xl"
              >
                <AlertTriangle
                  className="h-5 w-5 text-destructive"
                  aria-hidden="true"
                />
                {t.production.recoveryTitle}
              </h1>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Alert variant="destructive">
              <AlertDescription>{description}</AlertDescription>
            </Alert>
            <Button type="button" onClick={handleDiscardRecovery}>
              {t.production.discardDraft}
            </Button>
          </CardContent>
        </Card>
      </section>
    );
  }

  if (requestedRunId && !draft.full_run?.current_run_id) {
    return (
      <section
        className="mx-auto max-w-2xl"
        aria-labelledby="production-url-conflict-title"
      >
        <Alert variant="destructive" data-testid="production-full-run-error">
          <AlertTitle id="production-url-conflict-title">
            {t.production.fullUrlConflictTitle}
          </AlertTitle>
          <AlertDescription className="space-y-3">
            <p>{t.production.fullUrlConflict}</p>
            <Link
              className="font-medium underline"
              href={`/history?run=${encodeURIComponent(requestedRunId)}&from=production`}
            >
              {t.production.fullOpenHistory}
            </Link>
          </AlertDescription>
        </Alert>
      </section>
    );
  }

  if (inputs.isLoading) {
    return (
      <div className="flex min-h-64 items-center justify-center" role="status">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
        {t.production.loadingSources}
      </div>
    );
  }

  if (inputs.isError) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <Alert variant="destructive">
          <AlertTitle>{t.production.loadSourcesFailed}</AlertTitle>
          {queryErrorMessage(inputs.error) && (
            <AlertDescription>
              {queryErrorMessage(inputs.error)}
            </AlertDescription>
          )}
        </Alert>
        <Button
          type="button"
          variant="outline"
          onClick={() => void inputs.refetch()}
        >
          {t.production.retry}
        </Button>
      </div>
    );
  }

  if (videos.length === 0) {
    return (
      <Alert className="mx-auto max-w-2xl">
        <AlertDescription>{t.production.noSources}</AlertDescription>
      </Alert>
    );
  }

  const migrationNotice =
    migratedFrom === "baseline"
      ? t.production.baselineMigrated
      : migratedFrom === "broadcast"
        ? t.production.broadcastMigrated
        : null;
  const noticeText = [migrationNotice, notice ? t.production[notice] : null]
    .filter(Boolean)
    .join(" ");
  const errorText = error
    ? `${t.production[error.code]} ${error.detail}`
    : null;

  return (
    <>
      <ProductionWorkspace
        key={draft.workflow_id}
        draft={draft}
        videos={videos}
        sourceIssue={sourceIssue}
        notice={noticeText}
        error={errorText}
        requestedRunId={requestedRunId}
        onSourceChange={handleSourceChange}
        onCalibrationChange={handleCalibrationChange}
        onTrialChange={handleTrialChange}
        onPendingConfigChange={handlePendingConfigChange}
        onConfirmedConfigChange={handleConfirmedConfigChange}
        onFullRunChange={handleFullRunChange}
        onPersistCurrent={handlePersistCurrent}
        onVerifiedProduct={handleVerifiedProduct}
        onParentRunIdChange={handleParentRunIdChange}
        onInvalidate={handleInvalidate}
        onSaveExit={handleSaveExit}
        onStartNew={handleStartNew}
        startNewButtonRef={startNewButtonRef}
      />

      <AlertDialog
        open={replacement !== null}
        onOpenChange={(open) => {
          if (!open) setReplacement(null);
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            startNewButtonRef.current?.focus();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{t.production.replaceTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {t.production.replaceDescription}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t.production.keepDraft}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (replacement) replaceWith(replacement);
              }}
            >
              {t.production.replaceDraft}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export default function ProductionPage() {
  return <ProductionPageContent />;
}
