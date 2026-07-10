import { useState } from "react";
import type {
  BroadcastReviewAction,
  BroadcastReviewCandidate,
  BroadcastReviewWindow,
  BroadcastReviewWindowsResponse,
} from "@workspace/api-client-react";
import { AlertCircle, CheckCircle2, ImageOff } from "lucide-react";

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
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { BroadcastReviewDecision } from "@/lib/broadcastWorkflow";

export type { BroadcastReviewDecision } from "@/lib/broadcastWorkflow";

export type BroadcastNoiseSubtype = NonNullable<
  BroadcastReviewAction["noise_subtype"]
>;

export interface BroadcastReviewStepLabels {
  title: string;
  description: string;
  ready: string;
  needsReview: string;
  reason: string;
  noReviewRequired: string;
  noCandidates: string;
  window: string;
  priority: string;
  frames: string;
  duration: string;
  seconds: string;
  candidate: string;
  frame: string;
  boundingBox: string;
  detector: string;
  classifier: string;
  modelDecision: string;
  reasons: string;
  noReasons: string;
  confirmBall: string;
  rejectNoise: string;
  markUnknown: string;
  noiseSubtype: string;
  chooseNoiseSubtype: string;
  montageUnavailable: string;
  montageUnavailableDescription: string;
  retryMontage: string;
  montageAlt: string;
  decisionsComplete: string;
  decisionsRemaining: string;
  submit: string;
  submitting: string;
  continueWithoutReview: string;
  continuingWithoutReview: string;
}

export interface BroadcastReviewStepProps {
  response: BroadcastReviewWindowsResponse;
  /** Candidate IDs mapped only to URLs already resolved and validated by the caller. */
  montageUrlsByCandidateId: Readonly<Record<string, string>>;
  decisions: readonly BroadcastReviewDecision[];
  onDecisionsChange: (decisions: BroadcastReviewDecision[]) => void;
  onSubmit?: (decisions: BroadcastReviewDecision[]) => void;
  isSubmitting?: boolean;
  disabled?: boolean;
  error?: string | null;
  labels?: Partial<BroadcastReviewStepLabels>;
  noiseSubtypeLabels?: Partial<Record<BroadcastNoiseSubtype, string>>;
}

const DEFAULT_LABELS: BroadcastReviewStepLabels = {
  title: "Review uncertain ball candidates",
  description:
    "Inspect the verified evidence and make one explicit decision for every candidate.",
  ready: "Ready",
  needsReview: "Needs review",
  reason: "Reason",
  noReviewRequired:
    "The evidence-bound queue contains no candidates. Continue to publish an empty review decision artifact and recompute.",
  noCandidates: "No review candidates are available.",
  window: "Review window",
  priority: "Priority",
  frames: "Frames",
  duration: "Duration",
  seconds: "seconds",
  candidate: "Candidate",
  frame: "Frame",
  boundingBox: "Bounding box",
  detector: "Detection",
  classifier: "Classification",
  modelDecision: "Model decision",
  reasons: "Reasons",
  noReasons: "No decision reasons provided",
  confirmBall: "Confirm ball",
  rejectNoise: "Reject as noise",
  markUnknown: "Mark unknown",
  noiseSubtype: "Noise subtype",
  chooseNoiseSubtype: "Choose a noise subtype",
  montageUnavailable: "Verified montage unavailable",
  montageUnavailableDescription:
    "This candidate cannot be submitted until its evidence URL is resolved and validated.",
  retryMontage: "Retry evidence",
  montageAlt: "Verified candidate review montage",
  decisionsComplete: "All candidate decisions are complete.",
  decisionsRemaining: "candidate decisions remaining",
  submit: "Submit review decisions",
  submitting: "Submitting review decisions…",
  continueWithoutReview: "Continue without manual review",
  continuingWithoutReview: "Continuing…",
};

const DEFAULT_NOISE_SUBTYPE_LABELS: Record<BroadcastNoiseSubtype, string> = {
  player_body_or_shoe: "Player body or shoe",
  field_line_or_mark: "Field line or mark",
  sideline_or_spare_ball: "Sideline or spare ball",
  equipment_or_background: "Equipment or background",
  lighting_shadow_or_blur: "Lighting, shadow, or blur",
};

const NOISE_SUBTYPES = Object.keys(
  DEFAULT_NOISE_SUBTYPE_LABELS,
) as BroadcastNoiseSubtype[];

function percentage(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function bboxText(candidate: BroadcastReviewCandidate): string {
  return candidate.bbox.map((value) => Number(value.toFixed(2))).join(", ");
}

function decisionIsComplete(
  decision: BroadcastReviewDecision | undefined,
): boolean {
  if (!decision) return false;
  if (decision.action === "reject_noise") return decision.noise_subtype != null;
  if (
    decision.action === "confirm_ball" ||
    decision.action === "mark_unknown"
  ) {
    return decision.noise_subtype == null;
  }
  return false;
}

function montageUrlFor(
  urls: Readonly<Record<string, string>>,
  candidateId: string,
): string | null {
  if (!Object.prototype.hasOwnProperty.call(urls, candidateId)) return null;
  const value = urls[candidateId];
  return typeof value === "string" && value.trim() ? value : null;
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

interface BoundMontageMedia {
  identity: string;
  requestUrl: string;
}

function appendEvidenceCacheKey(url: string, cacheKey: string): string {
  if (/^(?:blob|data):/i.test(url)) return url;
  const hashIndex = url.indexOf("#");
  const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
  const fragment = hashIndex >= 0 ? url.slice(hashIndex) : "";
  const separator = base.includes("?") ? "&" : "?";
  return `${base}${separator}broadcast_evidence=${encodeURIComponent(cacheKey)}${fragment}`;
}

function boundMontageMediaFor(
  response: BroadcastReviewWindowsResponse,
  candidate: BroadcastReviewCandidate,
  montageUrl: string,
): BoundMontageMedia | null {
  const queueSha256 = response.queue_sha256?.trim() ?? "";
  const candidateFingerprint = candidate.candidate_fingerprint.trim();
  const evidenceSha256 = candidate.evidence.sha256.trim();
  const datasetVersion = candidate.evidence.dataset_version.trim();
  const montageSha256 =
    candidate.evidence.artifacts.review_montage.sha256.trim();
  if (
    !SHA256_PATTERN.test(queueSha256) ||
    !SHA256_PATTERN.test(candidateFingerprint) ||
    !SHA256_PATTERN.test(evidenceSha256) ||
    !SHA256_PATTERN.test(datasetVersion) ||
    !SHA256_PATTERN.test(montageSha256)
  ) {
    return null;
  }
  const identity = JSON.stringify([
    response.run_id,
    queueSha256,
    candidate.candidate_id,
    candidateFingerprint,
    candidate.evidence.sample_id,
    evidenceSha256,
    datasetVersion,
    candidate.evidence.artifacts.review_montage.path,
    montageSha256,
    montageUrl,
  ]);
  return {
    identity,
    requestUrl: appendEvidenceCacheKey(
      montageUrl,
      `${queueSha256}.${montageSha256}`,
    ),
  };
}

function orderedDecisions(
  items: readonly BroadcastReviewWindow[],
  decisions: readonly BroadcastReviewDecision[],
): BroadcastReviewDecision[] {
  const byCandidate = new Map(
    decisions.map((decision) => [decision.candidate_id, decision]),
  );
  return items.flatMap(
    (item) =>
      item.candidates?.flatMap((candidate) => {
        const decision = byCandidate.get(candidate.candidate_id);
        return decision ? [decision] : [];
      }) ?? [],
  );
}

export function BroadcastReviewStep({
  response,
  montageUrlsByCandidateId,
  decisions,
  onDecisionsChange,
  onSubmit,
  isSubmitting = false,
  disabled = false,
  error,
  labels: labelOverrides,
  noiseSubtypeLabels: noiseSubtypeLabelOverrides,
}: BroadcastReviewStepProps) {
  const labels = { ...DEFAULT_LABELS, ...labelOverrides };
  const [mediaStates, setMediaStates] = useState<
    Record<string, "loaded" | "failed">
  >({});
  const noiseSubtypeLabels = {
    ...DEFAULT_NOISE_SUBTYPE_LABELS,
    ...noiseSubtypeLabelOverrides,
  };
  const items = response.items ?? [];
  const candidates = items.flatMap((item) => item.candidates ?? []);
  const decisionByCandidate = new Map(
    decisions.map((decision) => [decision.candidate_id, decision]),
  );
  const completeCount = candidates.filter((candidate) => {
    const decision = decisionByCandidate.get(candidate.candidate_id);
    const montageUrl = montageUrlFor(
      montageUrlsByCandidateId,
      candidate.candidate_id,
    );
    const media =
      montageUrl == null
        ? null
        : boundMontageMediaFor(response, candidate, montageUrl);
    return (
      media != null &&
      mediaStates[media.identity] === "loaded" &&
      decisionIsComplete(decision)
    );
  }).length;
  const isComplete =
    response.status === "ready" &&
    candidates.length > 0 &&
    completeCount === candidates.length;
  const remainingCount = candidates.length - completeCount;
  const zeroCandidateReady =
    response.status === "ready" &&
    response.review_item_count === 0 &&
    items.length === 0 &&
    candidates.length === 0;

  function updateDecision(
    candidate: BroadcastReviewCandidate,
    action: BroadcastReviewAction["action"],
    noiseSubtype?: BroadcastNoiseSubtype,
  ) {
    if (disabled || isSubmitting) return;
    const nextDecision: BroadcastReviewDecision = {
      candidate_id: candidate.candidate_id,
      action,
      ...(noiseSubtype != null ? { noise_subtype: noiseSubtype } : {}),
    };
    const nextByCandidate = new Map(decisionByCandidate);
    nextByCandidate.set(candidate.candidate_id, nextDecision);
    onDecisionsChange(orderedDecisions(items, [...nextByCandidate.values()]));
  }

  return (
    <Card data-testid="broadcast-review-step">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>{labels.title}</CardTitle>
          <Badge
            variant={response.status === "ready" ? "secondary" : "default"}
          >
            {response.status === "ready" ? labels.ready : labels.needsReview}
          </Badge>
        </div>
        <CardDescription>{labels.description}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{labels.needsReview}</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {response.reason && (
          <Alert>
            <AlertCircle />
            <AlertTitle>{labels.reason}</AlertTitle>
            <AlertDescription>{response.reason}</AlertDescription>
          </Alert>
        )}

        {response.status === "ready" && candidates.length === 0 ? (
          <Alert>
            <CheckCircle2 />
            <AlertTitle>{labels.ready}</AlertTitle>
            <AlertDescription>{labels.noReviewRequired}</AlertDescription>
          </Alert>
        ) : candidates.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            {labels.noCandidates}
          </p>
        ) : (
          items.map((item, itemIndex) => (
            <section
              key={item.review_item_id}
              className="space-y-4 rounded-xl border bg-muted/20 p-4"
              aria-labelledby={`broadcast-review-window-${item.review_item_id}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3
                    id={`broadcast-review-window-${item.review_item_id}`}
                    className="font-semibold"
                  >
                    {labels.window} {itemIndex + 1}
                  </h3>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {labels.frames} {item.start_frame}–{item.end_frame} ·{" "}
                    {labels.duration} {item.duration_seconds.toFixed(2)}{" "}
                    {labels.seconds}
                  </p>
                </div>
                <Badge variant="outline">
                  {labels.priority}: {item.priority}
                </Badge>
              </div>

              {(item.candidates ?? []).map((candidate, candidateIndex) => {
                const decision = decisionByCandidate.get(
                  candidate.candidate_id,
                );
                const montageUrl = montageUrlFor(
                  montageUrlsByCandidateId,
                  candidate.candidate_id,
                );
                const media =
                  montageUrl == null
                    ? null
                    : boundMontageMediaFor(response, candidate, montageUrl);
                const mediaLoaded =
                  media != null && mediaStates[media.identity] === "loaded";
                const mediaFailed =
                  media != null && mediaStates[media.identity] === "failed";
                const controlsDisabled =
                  disabled || isSubmitting || !mediaLoaded;
                const radioName = `${item.review_item_id}-${candidate.candidate_id}`;

                return (
                  <article
                    key={candidate.candidate_id}
                    className="grid gap-4 rounded-lg border bg-card p-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(18rem,0.9fr)]"
                  >
                    <div className="space-y-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h4 className="font-medium">
                          {labels.candidate} {candidateIndex + 1}
                        </h4>
                        <Badge variant="outline">
                          {candidate.selective_decision}
                        </Badge>
                      </div>

                      {media && !mediaFailed ? (
                        <img
                          key={media.identity}
                          src={media.requestUrl}
                          alt={`${labels.montageAlt}: ${candidate.candidate_id}`}
                          className="max-h-[28rem] w-full rounded-md border bg-muted object-contain"
                          loading="lazy"
                          onLoad={() =>
                            setMediaStates((current) => ({
                              ...current,
                              [media.identity]: "loaded",
                            }))
                          }
                          onError={() =>
                            setMediaStates((current) => ({
                              ...current,
                              [media.identity]: "failed",
                            }))
                          }
                        />
                      ) : (
                        <Alert variant="destructive">
                          <ImageOff />
                          <AlertTitle>{labels.montageUnavailable}</AlertTitle>
                          <AlertDescription className="space-y-3">
                            {labels.montageUnavailableDescription}
                            {mediaFailed && media && (
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() =>
                                  setMediaStates((current) => {
                                    const next = { ...current };
                                    delete next[media.identity];
                                    return next;
                                  })
                                }
                              >
                                {labels.retryMontage}
                              </Button>
                            )}
                          </AlertDescription>
                        </Alert>
                      )}

                      <dl className="grid gap-2 text-sm sm:grid-cols-2">
                        <div>
                          <dt className="text-muted-foreground">
                            {labels.frame}
                          </dt>
                          <dd className="font-mono">{candidate.frame_index}</dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground">
                            {labels.boundingBox}
                          </dt>
                          <dd className="font-mono">[{bboxText(candidate)}]</dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground">
                            {labels.detector}
                          </dt>
                          <dd>
                            {candidate.detector_source} ·{" "}
                            {percentage(candidate.detector_confidence)}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground">
                            {labels.classifier}
                          </dt>
                          <dd>
                            {candidate.predicted_label} ·{" "}
                            {percentage(candidate.prediction_confidence)}
                          </dd>
                        </div>
                        <div className="sm:col-span-2">
                          <dt className="text-muted-foreground">
                            {labels.modelDecision}
                          </dt>
                          <dd>{candidate.selective_decision}</dd>
                        </div>
                      </dl>

                      <div>
                        <p className="text-sm text-muted-foreground">
                          {labels.reasons}
                        </p>
                        {candidate.decision_reasons?.length ? (
                          <div className="mt-1 flex flex-wrap gap-1.5">
                            {candidate.decision_reasons.map((reason) => (
                              <Badge key={reason} variant="secondary">
                                {reason}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <p className="mt-1 text-sm">{labels.noReasons}</p>
                        )}
                      </div>
                    </div>

                    <fieldset className="space-y-4" disabled={controlsDisabled}>
                      <legend className="text-sm font-medium">
                        {labels.modelDecision}
                      </legend>
                      <RadioGroup
                        value={decision?.action}
                        onValueChange={(
                          action: BroadcastReviewAction["action"],
                        ) =>
                          updateDecision(
                            candidate,
                            action,
                            action === "reject_noise"
                              ? (decision?.noise_subtype ?? undefined)
                              : undefined,
                          )
                        }
                        className="mt-2 gap-3"
                        aria-label={`${labels.candidate} ${candidate.candidate_id}`}
                      >
                        {(
                          [
                            ["confirm_ball", labels.confirmBall],
                            ["reject_noise", labels.rejectNoise],
                            ["mark_unknown", labels.markUnknown],
                          ] as const
                        ).map(([value, text]) => (
                          <div
                            key={value}
                            className="flex items-center gap-2 rounded-md border p-3"
                          >
                            <RadioGroupItem
                              value={value}
                              id={`${radioName}-${value}`}
                            />
                            <Label
                              htmlFor={`${radioName}-${value}`}
                              className="flex-1 cursor-pointer"
                            >
                              {text}
                            </Label>
                          </div>
                        ))}
                      </RadioGroup>

                      {decision?.action === "reject_noise" && (
                        <div className="space-y-2">
                          <Label htmlFor={`${radioName}-noise-subtype`}>
                            {labels.noiseSubtype}
                          </Label>
                          <Select
                            value={decision.noise_subtype ?? ""}
                            onValueChange={(
                              noiseSubtype: BroadcastNoiseSubtype,
                            ) =>
                              updateDecision(
                                candidate,
                                "reject_noise",
                                noiseSubtype,
                              )
                            }
                            disabled={controlsDisabled}
                          >
                            <SelectTrigger id={`${radioName}-noise-subtype`}>
                              <SelectValue
                                placeholder={labels.chooseNoiseSubtype}
                              />
                            </SelectTrigger>
                            <SelectContent>
                              {NOISE_SUBTYPES.map((subtype) => (
                                <SelectItem key={subtype} value={subtype}>
                                  {noiseSubtypeLabels[subtype]}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      )}
                    </fieldset>
                  </article>
                );
              })}
            </section>
          ))
        )}
      </CardContent>

      {(candidates.length > 0 || zeroCandidateReady) && (
        <CardFooter className="flex flex-wrap justify-between gap-3 border-t pt-4">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {zeroCandidateReady
              ? labels.noReviewRequired
              : isComplete
                ? labels.decisionsComplete
                : `${remainingCount} ${labels.decisionsRemaining}`}
          </p>
          {onSubmit && (
            <Button
              type="button"
              onClick={() =>
                onSubmit(
                  zeroCandidateReady ? [] : orderedDecisions(items, decisions),
                )
              }
              disabled={
                (!isComplete && !zeroCandidateReady) || disabled || isSubmitting
              }
            >
              {zeroCandidateReady
                ? isSubmitting
                  ? labels.continuingWithoutReview
                  : labels.continueWithoutReview
                : isSubmitting
                  ? labels.submitting
                  : labels.submit}
            </Button>
          )}
        </CardFooter>
      )}
    </Card>
  );
}
