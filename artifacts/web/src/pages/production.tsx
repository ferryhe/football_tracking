import { useState } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { useLocation } from "wouter";
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
import {
  clearProductionDraft,
  createProductionDraft,
  loadProductionDraft,
  requiresDraftReplacementConfirmation,
  saveProductionDraft,
  sourceSignaturesMatch,
  updateProductionSource,
  type ProductionDraft,
  type ProductionDraftLoadResult,
  type SourceSignature,
} from "@/lib/productionWorkflow";

function queryErrorMessage(error: unknown): string | null {
  return error instanceof Error && error.message ? error.message : null;
}

type ProductionNotice =
  | "migrated"
  | "restored"
  | "savedLocally"
  | "sourceReset"
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
  const [notice, setNotice] = useState<ProductionNotice | null>(() => {
    if (!storage.isPersistent) return "storageFallback";
    if (initialLoad.status !== "restored") return null;
    return initialLoad.migrated ? "migrated" : "restored";
  });
  const [error, setError] = useState<ProductionError | null>(null);
  const [replacement, setReplacement] = useState<ProductionDraft | null>(null);
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

  function handleSourceChange(source: SourceSignature) {
    const resetsDownstream =
      draft.source !== null && !sourceSignaturesMatch(draft.source, source);
    const nextDraft = updateProductionSource(draft, source);
    setDraft(nextDraft);
    if (persist(nextDraft)) {
      setNotice(
        !storage.isPersistent
          ? "storageFallback"
          : resetsDownstream
            ? "sourceReset"
            : "savedLocally",
      );
    }
  }

  function handleSaveExit() {
    if (persist(draft)) setLocation("/");
  }

  function replaceWith(nextDraft: ProductionDraft) {
    const result = clearProductionDraft(storage);
    setDraft(nextDraft);
    setReplacement(null);
    setRecovery(null);
    setNotice(storage.isPersistent ? null : "storageFallback");
    setError(result.ok ? null : { code: "saveFailed", detail: result.message });
  }

  function handleStartNew() {
    const nextDraft = createProductionDraft();
    if (requiresDraftReplacementConfirmation(draft, nextDraft.workflow_id)) {
      setReplacement(nextDraft);
      return;
    }
    replaceWith(nextDraft);
  }

  function handleDiscardRecovery() {
    replaceWith(createProductionDraft());
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

  const noticeText = notice ? t.production[notice] : null;
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
        onSourceChange={handleSourceChange}
        onSaveExit={handleSaveExit}
        onStartNew={handleStartNew}
      />

      <AlertDialog
        open={replacement !== null}
        onOpenChange={(open) => {
          if (!open) setReplacement(null);
        }}
      >
        <AlertDialogContent>
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
