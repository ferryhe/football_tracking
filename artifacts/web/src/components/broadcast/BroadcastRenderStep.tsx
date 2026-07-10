import { useState } from "react";
import type {
  BroadcastRenderRequest,
  BroadcastRunState,
  RunRecord,
} from "@workspace/api-client-react";
import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Download,
  Film,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";

export const BROADCAST_DELIVERY_ARTIFACTS = [
  "broadcast.mp4",
  "broadcast_quality_report.json",
  "camera_target.csv",
  "ball_track.v2.csv",
  "review_decisions.json",
  "action_track.csv",
  "candidate_classifications.jsonl",
  "ball_candidates.jsonl",
] as const;

export type BroadcastDeliveryArtifact =
  (typeof BROADCAST_DELIVERY_ARTIFACTS)[number];

export interface BroadcastRenderDimensions {
  target_width: number;
  target_height: number;
}

export interface BroadcastRenderStepLabels {
  title: string;
  description: string;
  trajectory: string;
  trajectoryReady: string;
  trajectoryMissing: string;
  renderOperation: string;
  delivery: string;
  ready: string;
  waiting: string;
  operationProgress: string;
  operationStatus: string;
  width: string;
  height: string;
  resolutionHint: string;
  startRender: string;
  rendering: string;
  cancel: string;
  video: string;
  downloads: string;
  limitations: string;
  blockingReasons: string;
  metadataWarnings: string;
  metadataConflict: string;
  metadataConflictDescription: string;
  deliveryUrlsUnavailable: string;
  deliveryUrlsUnavailableDescription: string;
  renderError: string;
}

export interface BroadcastRenderStepProps {
  run: RunRecord;
  trajectoryGenerationId?: string | null;
  operationRun?: RunRecord | null;
  operationStatus?: BroadcastRunState["operation_status"];
  artifactUrls?: Partial<Record<BroadcastDeliveryArtifact, string>>;
  targetWidth?: number;
  targetHeight?: number;
  onDimensionsChange?: (dimensions: BroadcastRenderDimensions) => void;
  onRender: (request: BroadcastRenderRequest) => void;
  isRendering?: boolean;
  disabled?: boolean;
  error?: string | null;
  onCancel?: () => void;
  labels?: Partial<BroadcastRenderStepLabels>;
  artifactLabels?: Partial<Record<BroadcastDeliveryArtifact, string>>;
}

const DEFAULT_LABELS: BroadcastRenderStepLabels = {
  title: "Render and deliver",
  description:
    "Render the reviewed trajectory, inspect the final video, and download its evidence.",
  trajectory: "Trajectory",
  trajectoryReady: "Ready",
  trajectoryMissing: "Waiting for a reviewed trajectory",
  renderOperation: "Render operation",
  delivery: "Delivery",
  ready: "Ready",
  waiting: "Waiting",
  operationProgress: "Operation progress",
  operationStatus: "Status",
  width: "Target width",
  height: "Target height",
  resolutionHint: "Allowed range: 320–7680 × 180–4320",
  startRender: "Render broadcast",
  rendering: "Rendering broadcast…",
  cancel: "Cancel operation",
  video: "Broadcast video",
  downloads: "Evidence downloads",
  limitations: "Known limitations",
  blockingReasons: "Blocking reasons",
  metadataWarnings: "Metadata warnings",
  metadataConflict: "Metadata conflict",
  metadataConflictDescription:
    "The final media and operation metadata disagree. Verify the quality report while the server reconciles the operation record.",
  deliveryUrlsUnavailable: "Verified delivery URLs unavailable",
  deliveryUrlsUnavailableDescription:
    "The run is ready, but the final media URLs have not passed client-side artifact resolution.",
  renderError: "Render failed",
};

const DEFAULT_ARTIFACT_LABELS: Record<BroadcastDeliveryArtifact, string> = {
  "broadcast.mp4": "Broadcast video",
  "broadcast_quality_report.json": "Quality report",
  "camera_target.csv": "Camera target",
  "ball_track.v2.csv": "Ball track",
  "review_decisions.json": "Review decisions",
  "action_track.csv": "Action track",
  "candidate_classifications.jsonl": "Candidate classifications",
  "ball_candidates.jsonl": "Ball candidates",
};

const TRAJECTORY_ID_PATTERN = /^trajectory-[0-9a-f]{24}$/;
const ACTIVE_OPERATION_STATUSES = new Set(["queued", "running", "committing"]);

function clampedProgress(value: number | null | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function verifiedUrl(
  urls: Partial<Record<BroadcastDeliveryArtifact, string>>,
  artifact: BroadcastDeliveryArtifact,
): string | null {
  const value = urls[artifact];
  return typeof value === "string" && value.trim() ? value : null;
}

export function BroadcastRenderStep({
  run,
  trajectoryGenerationId: trajectoryGenerationIdOverride,
  operationRun,
  operationStatus,
  artifactUrls = {},
  targetWidth,
  targetHeight,
  onDimensionsChange,
  onRender,
  isRendering = false,
  disabled = false,
  error,
  onCancel,
  labels: labelOverrides,
  artifactLabels: artifactLabelOverrides,
}: BroadcastRenderStepProps) {
  const labels = { ...DEFAULT_LABELS, ...labelOverrides };
  const artifactLabels = {
    ...DEFAULT_ARTIFACT_LABELS,
    ...artifactLabelOverrides,
  };
  const [internalWidth, setInternalWidth] = useState(targetWidth ?? 1920);
  const [internalHeight, setInternalHeight] = useState(targetHeight ?? 1080);
  const width = targetWidth ?? internalWidth;
  const height = targetHeight ?? internalHeight;
  const trajectoryGenerationId =
    trajectoryGenerationIdOverride ??
    run.broadcast?.trajectory_generation_id ??
    run.broadcast?.result?.trajectory_generation_id ??
    null;
  const trajectoryReady =
    typeof trajectoryGenerationId === "string" &&
    TRAJECTORY_ID_PATTERN.test(trajectoryGenerationId);
  const effectiveOperationStatus =
    operationStatus ??
    operationRun?.broadcast?.operation_status ??
    run.broadcast?.operation_status ??
    (isRendering ? "running" : null);
  const operationActive =
    isRendering ||
    (effectiveOperationStatus != null &&
      ACTIVE_OPERATION_STATUSES.has(effectiveOperationStatus)) ||
    operationRun?.status === "queued" ||
    operationRun?.status === "running";
  const parentReady = run.broadcast?.status === "ready";
  const videoUrl = verifiedUrl(artifactUrls, "broadcast.mp4");
  const ready = parentReady && videoUrl != null;
  const progress = clampedProgress(
    operationRun?.progress?.percent ??
      (effectiveOperationStatus === "completed" ? 100 : 0),
  );
  const dimensionsValid =
    Number.isInteger(width) &&
    width >= 320 &&
    width <= 7680 &&
    Number.isInteger(height) &&
    height >= 180 &&
    height <= 4320;
  const operationReportStatus =
    operationRun?.broadcast?.operation_report_status ??
    run.broadcast?.operation_report_status;
  const metadataConflict =
    effectiveOperationStatus === "metadata_conflict" ||
    operationReportStatus === "conflict" ||
    operationReportStatus === "missing_after_ready_commit";
  const metadataWarnings = Array.from(
    new Set([
      ...(run.broadcast?.metadata_warnings ?? []),
      ...(operationRun?.broadcast?.metadata_warnings ?? []),
    ]),
  );
  const renderError = error ?? operationRun?.error ?? null;
  const downloadableArtifacts = BROADCAST_DELIVERY_ARTIFACTS.flatMap(
    (artifact) => {
      const url = verifiedUrl(artifactUrls, artifact);
      return url ? [{ artifact, url }] : [];
    },
  );

  function updateWidth(nextWidth: number) {
    setInternalWidth(nextWidth);
    onDimensionsChange?.({ target_width: nextWidth, target_height: height });
  }

  function updateHeight(nextHeight: number) {
    setInternalHeight(nextHeight);
    onDimensionsChange?.({ target_width: width, target_height: nextHeight });
  }

  return (
    <Card data-testid="broadcast-render-step">
      <CardHeader>
        <CardTitle>{labels.title}</CardTitle>
        <CardDescription>{labels.description}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-lg border p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{labels.trajectory}</span>
              {trajectoryReady ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              ) : (
                <CircleDashed className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {trajectoryReady
                ? labels.trajectoryReady
                : labels.trajectoryMissing}
            </p>
          </div>
          <div className="rounded-lg border p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">
                {labels.renderOperation}
              </span>
              {operationActive ? (
                <LoaderCircle className="h-4 w-4 animate-spin text-primary" />
              ) : effectiveOperationStatus === "completed" ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              ) : (
                <CircleDashed className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {effectiveOperationStatus ?? labels.waiting}
            </p>
          </div>
          <div className="rounded-lg border p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{labels.delivery}</span>
              {ready ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              ) : (
                <CircleDashed className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {ready ? labels.ready : labels.waiting}
            </p>
          </div>
        </div>

        {renderError && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{labels.renderError}</AlertTitle>
            <AlertDescription>{renderError}</AlertDescription>
          </Alert>
        )}

        {metadataConflict && (
          <Alert variant="destructive">
            <TriangleAlert />
            <AlertTitle>{labels.metadataConflict}</AlertTitle>
            <AlertDescription>
              {labels.metadataConflictDescription}
              {operationReportStatus ? ` (${operationReportStatus})` : ""}
            </AlertDescription>
          </Alert>
        )}

        {metadataWarnings.length > 0 && (
          <Alert>
            <TriangleAlert />
            <AlertTitle>{labels.metadataWarnings}</AlertTitle>
            <AlertDescription>
              <ul className="list-disc space-y-1 pl-5">
                {metadataWarnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        {(run.broadcast?.blocking_reasons?.length ?? 0) > 0 && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{labels.blockingReasons}</AlertTitle>
            <AlertDescription>
              <ul className="list-disc space-y-1 pl-5">
                {run.broadcast?.blocking_reasons?.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}

        {operationActive && (
          <div className="space-y-2 rounded-lg border p-4">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="font-medium">{labels.operationProgress}</span>
              <Badge variant="secondary">
                {labels.operationStatus}:{" "}
                {effectiveOperationStatus ??
                  operationRun?.status ??
                  labels.waiting}
              </Badge>
            </div>
            <Progress value={progress} aria-label={labels.operationProgress} />
            <p className="text-right text-xs text-muted-foreground">
              {progress.toFixed(1)}%
            </p>
          </div>
        )}

        {!ready && (
          <div className="space-y-4 rounded-lg border p-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="broadcast-target-width">{labels.width}</Label>
                <Input
                  id="broadcast-target-width"
                  type="number"
                  min={320}
                  max={7680}
                  step={1}
                  value={width}
                  onChange={(event) => updateWidth(Number(event.target.value))}
                  disabled={disabled || operationActive}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="broadcast-target-height">{labels.height}</Label>
                <Input
                  id="broadcast-target-height"
                  type="number"
                  min={180}
                  max={4320}
                  step={1}
                  value={height}
                  onChange={(event) => updateHeight(Number(event.target.value))}
                  disabled={disabled || operationActive}
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {labels.resolutionHint}
            </p>
          </div>
        )}

        {parentReady && !videoUrl && (
          <Alert variant="destructive">
            <Film />
            <AlertTitle>{labels.deliveryUrlsUnavailable}</AlertTitle>
            <AlertDescription>
              {labels.deliveryUrlsUnavailableDescription}
            </AlertDescription>
          </Alert>
        )}

        {ready && videoUrl && (
          <section
            className="space-y-3"
            aria-labelledby="broadcast-delivery-video-title"
          >
            <h3 id="broadcast-delivery-video-title" className="font-semibold">
              {labels.video}
            </h3>
            <video
              src={videoUrl}
              controls
              preload="metadata"
              className="aspect-video w-full rounded-lg border bg-black"
            />
          </section>
        )}

        {ready && downloadableArtifacts.length > 0 && (
          <section
            className="space-y-3"
            aria-labelledby="broadcast-delivery-downloads-title"
          >
            <h3
              id="broadcast-delivery-downloads-title"
              className="font-semibold"
            >
              {labels.downloads}
            </h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {downloadableArtifacts.map(({ artifact, url }) => (
                <Button
                  key={artifact}
                  variant="outline"
                  asChild
                  className="justify-start"
                >
                  <a href={url} download={artifact}>
                    <Download />
                    {artifactLabels[artifact]}
                  </a>
                </Button>
              ))}
            </div>
          </section>
        )}

        {(run.broadcast?.limitations?.length ?? 0) > 0 && (
          <section
            className="rounded-lg border p-4"
            aria-labelledby="broadcast-limitations-title"
          >
            <h3 id="broadcast-limitations-title" className="font-semibold">
              {labels.limitations}
            </h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {run.broadcast?.limitations?.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          </section>
        )}
      </CardContent>

      {!ready && (
        <CardFooter className="justify-end gap-2 border-t pt-4">
          {operationActive && onCancel && (
            <Button
              type="button"
              variant="outline"
              onClick={onCancel}
              disabled={disabled}
            >
              {labels.cancel}
            </Button>
          )}
          <Button
            type="button"
            onClick={() =>
              trajectoryGenerationId &&
              onRender({
                trajectory_generation_id: trajectoryGenerationId,
                target_width: width,
                target_height: height,
              })
            }
            disabled={
              disabled ||
              isRendering ||
              operationActive ||
              metadataConflict ||
              !trajectoryReady ||
              !dimensionsValid
            }
          >
            {operationActive || isRendering
              ? labels.rendering
              : labels.startRender}
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
