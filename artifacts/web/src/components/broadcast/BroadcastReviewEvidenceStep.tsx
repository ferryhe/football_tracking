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
import { Progress } from "@/components/ui/progress";

export type BroadcastReviewEvidenceStatus =
  | "not_available"
  | "available"
  | "queued"
  | "copying"
  | "validating"
  | "committing"
  | "ready"
  | "blocked"
  | "failed"
  | "cancelled";

export interface BroadcastReviewEvidenceBundleIdentity {
  bundleId: string;
  manifestSha256: string;
}

export interface BroadcastReviewEvidenceState {
  status: BroadcastReviewEvidenceStatus;
  bundle?: BroadcastReviewEvidenceBundleIdentity | null;
  stage?: string | null;
  progressPercent?: number | null;
  blockerCode?: string | null;
  recoveryAction?: string | null;
  generationId?: string | null;
}

export interface BroadcastReviewEvidenceStepLabels {
  title: string;
  description: string;
  notAvailable: string;
  available: string;
  queued: string;
  copying: string;
  validating: string;
  committing: string;
  ready: string;
  blocked: string;
  failed: string;
  cancelled: string;
  bundleId: string;
  bundleManifest: string;
  stage: string;
  progress: string;
  blocker: string;
  recovery: string;
  generation: string;
  prepare: string;
  preparing: string;
  cancel: string;
  cancelling: string;
  cancelUnavailableDuringCommit: string;
  retry: string;
  retrying: string;
}

export interface BroadcastReviewEvidenceStepProps {
  state: BroadcastReviewEvidenceState;
  onPrepare?: (bundle: BroadcastReviewEvidenceBundleIdentity) => void;
  onCancel?: () => void;
  onRetry?: () => void;
  isPreparing?: boolean;
  isCancelling?: boolean;
  isRetrying?: boolean;
  disabled?: boolean;
  labels?: Partial<BroadcastReviewEvidenceStepLabels>;
}

const DEFAULT_LABELS: BroadcastReviewEvidenceStepLabels = {
  title: "Prepare review evidence",
  description:
    "Import and validate a qualified evidence bundle before reviewing uncertain candidates.",
  notAvailable: "No compatible evidence bundle",
  available: "Evidence bundle available",
  queued: "Evidence import queued",
  copying: "Copying evidence bundle",
  validating: "Validating evidence bundle",
  committing: "Committing evidence generation",
  ready: "Review evidence ready",
  blocked: "Evidence import blocked",
  failed: "Evidence import failed",
  cancelled: "Evidence import cancelled",
  bundleId: "Bundle ID",
  bundleManifest: "Bundle manifest",
  stage: "Stage",
  progress: "Evidence import progress",
  blocker: "Blocker",
  recovery: "Recovery action",
  generation: "Evidence generation",
  prepare: "Prepare review evidence",
  preparing: "Preparing review evidence…",
  cancel: "Cancel import",
  cancelling: "Cancelling import…",
  cancelUnavailableDuringCommit:
    "Cancellation is unavailable after commit starts.",
  retry: "Retry import",
  retrying: "Retrying import…",
};

const ACTIVE_STATUSES = new Set<BroadcastReviewEvidenceStatus>([
  "queued",
  "copying",
  "validating",
  "committing",
]);

const RETRYABLE_STATUSES = new Set<BroadcastReviewEvidenceStatus>([
  "blocked",
  "failed",
  "cancelled",
]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function progressValue(value: number | null | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function statusText(
  status: BroadcastReviewEvidenceStatus,
  labels: BroadcastReviewEvidenceStepLabels,
): string {
  const keys: Record<
    BroadcastReviewEvidenceStatus,
    keyof BroadcastReviewEvidenceStepLabels
  > = {
    not_available: "notAvailable",
    available: "available",
    queued: "queued",
    copying: "copying",
    validating: "validating",
    committing: "committing",
    ready: "ready",
    blocked: "blocked",
    failed: "failed",
    cancelled: "cancelled",
  };
  return labels[keys[status]];
}

export function BroadcastReviewEvidenceStep({
  state,
  onPrepare,
  onCancel,
  onRetry,
  isPreparing = false,
  isCancelling = false,
  isRetrying = false,
  disabled = false,
  labels: labelOverrides,
}: BroadcastReviewEvidenceStepProps) {
  const labels = { ...DEFAULT_LABELS, ...labelOverrides };
  const status = statusText(state.status, labels);
  const progress = progressValue(state.progressPercent);
  const active = ACTIVE_STATUSES.has(state.status);
  const retryable = RETRYABLE_STATUSES.has(state.status);
  const bundleValid = Boolean(
    state.bundle?.bundleId.trim() &&
    SHA256_PATTERN.test(state.bundle.manifestSha256.trim()),
  );
  const showFooter = state.status === "available" || active || retryable;

  return (
    <Card
      className="min-w-0 w-full overflow-hidden"
      data-testid="broadcast-review-evidence-step"
    >
      <CardHeader>
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-1.5">
            <CardTitle>
              <h2 className="text-base">{labels.title}</h2>
            </CardTitle>
            <CardDescription>{labels.description}</CardDescription>
          </div>
          <div
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className="shrink-0"
          >
            <Badge variant={state.status === "ready" ? "secondary" : "outline"}>
              {status}
            </Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="min-w-0 space-y-4">
        {state.bundle && (
          <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2">
            <div className="min-w-0">
              <dt className="text-muted-foreground">{labels.bundleId}</dt>
              <dd className="break-all font-mono">{state.bundle.bundleId}</dd>
            </div>
            <div className="min-w-0">
              <dt className="text-muted-foreground">{labels.bundleManifest}</dt>
              <dd className="break-all font-mono">
                {state.bundle.manifestSha256}
              </dd>
            </div>
          </dl>
        )}

        {active && (
          <div className="space-y-2">
            <div className="flex min-w-0 flex-col gap-1 text-sm sm:flex-row sm:items-center sm:justify-between">
              <span className="text-muted-foreground">{labels.stage}</span>
              <span className="break-all font-mono">
                {state.stage?.trim() || state.status}
              </span>
            </div>
            <Progress
              value={progress}
              aria-label={labels.progress}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress}
            />
            <p className="text-right text-sm tabular-nums">
              {progress.toFixed(1)}%
            </p>
          </div>
        )}

        {(state.blockerCode || state.recoveryAction) && (
          <dl className="min-w-0 space-y-3 rounded-lg border bg-muted/20 p-4 text-sm">
            {state.blockerCode && (
              <div className="min-w-0">
                <dt className="text-muted-foreground">{labels.blocker}</dt>
                <dd className="break-all font-mono">{state.blockerCode}</dd>
              </div>
            )}
            {state.recoveryAction && (
              <div className="min-w-0">
                <dt className="text-muted-foreground">{labels.recovery}</dt>
                <dd>{state.recoveryAction}</dd>
              </div>
            )}
          </dl>
        )}

        {state.status === "ready" && state.generationId && (
          <div className="min-w-0 text-sm">
            <p className="text-muted-foreground">{labels.generation}</p>
            <p className="break-all font-mono">{state.generationId}</p>
          </div>
        )}

        {state.status === "committing" && (
          <p className="text-sm text-muted-foreground">
            {labels.cancelUnavailableDuringCommit}
          </p>
        )}
      </CardContent>

      {showFooter && (
        <CardFooter className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-end">
          {state.status === "available" && (
            <Button
              type="button"
              onClick={() => state.bundle && onPrepare?.(state.bundle)}
              disabled={disabled || isPreparing || !bundleValid || !onPrepare}
            >
              {isPreparing ? labels.preparing : labels.prepare}
            </Button>
          )}
          {active && (
            <Button
              type="button"
              variant="outline"
              onClick={() => onCancel?.()}
              disabled={
                disabled ||
                isCancelling ||
                state.status === "committing" ||
                !onCancel
              }
            >
              {isCancelling ? labels.cancelling : labels.cancel}
            </Button>
          )}
          {retryable && (
            <Button
              type="button"
              onClick={() => onRetry?.()}
              disabled={disabled || isRetrying || !onRetry}
            >
              {isRetrying ? labels.retrying : labels.retry}
            </Button>
          )}
        </CardFooter>
      )}
    </Card>
  );
}
