import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn, formatBytes, formatDateTime, runMoment, statusBadgeClass } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sparkles,
  Brain,
  AlertCircle,
  Loader2,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Film,
  CopyPlus,
  FileText,
  ListChecks,
  Play,
  RotateCcw,
  Save,
  ShieldCheck,
  Wand2,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { FieldPreviewCanvas } from "@/components/FieldPreviewCanvas";
import type { Translations } from "@/lib/i18n";
import type {
  AIApprovedActionsArtifact,
  AIApprovedAction,
  AIExplainResponse,
  AIHighlightAdjustment,
  AIImprovementItem,
  AIImproveReportArtifact,
  AIImproveApprovalResponse,
  AIImproveResponse,
  AISuggestion,
  ArtifactSummary,
  FieldPreviewResponse,
} from "@/lib/types";
import { useLanguage } from "@/contexts/LanguageContext";

function encodeArtifactPath(name: string): string {
  return name.split("/").map((segment) => encodeURIComponent(segment)).join("/");
}

function artifactUrl(runId: string, artifact: ArtifactSummary): string {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeArtifactPath(artifact.name)}`;
}

function pickPlaybackArtifact(artifacts: ArtifactSummary[]): ArtifactSummary | null {
  const videos = artifacts.filter((artifact) => artifact.exists && artifact.kind === "video");
  return (
    videos.find((artifact) => /\.(web|browser|h264)\.mp4$/i.test(artifact.name)) ??
    videos.find((artifact) => artifact.name === "annotated.cleaned.mp4") ??
    videos.find((artifact) => artifact.name === "annotated.mp4") ??
    videos.find((artifact) => artifact.name.toLowerCase().includes("follow")) ??
    videos[0] ??
    null
  );
}

type AIAnalysisLabels = Translations["aiAnalysis"];
type ImprovementGroupKey = "missingBall" | "noise" | "cameraMotion" | "highlightBoundary";

const EXECUTABLE_RERUN_ACTIONS = new Set(["targeted_rerun"]);
const MISSING_BALL_ACTIONS = new Set(["targeted_rerun", "localize_ball_roi"]);
const HIGHLIGHT_ACTIONS = new Set(["adjust_highlight_window", "render_suggested_highlight"]);
const FOLLOW_CAM_ACTIONS = new Set([
  "adjust_follow_cam",
  "tracking_rerun_before_follow_cam",
  "human_review_camera_motion",
]);

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function isFilledRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length > 0;
}

function artifactByName(run: { artifacts: ArtifactSummary[] } | null, name: string): ArtifactSummary | null {
  return run?.artifacts.find((artifact) => artifact.exists && artifact.name === name) ?? null;
}

function frameWindowLabel(item: {
  start_frame?: number | null;
  end_frame?: number | null;
  rerun_scope?: { start_frame: number; end_frame: number } | null;
  suggested_window?: { start_frame: number; end_frame: number } | null;
}): string {
  const window = item.rerun_scope ?? item.suggested_window;
  if (window) return `${window.start_frame}-${window.end_frame}`;
  if (item.start_frame != null && item.end_frame != null) return `${item.start_frame}-${item.end_frame}`;
  return "n/a";
}

function classifyImprovement(item: AIImprovementItem): ImprovementGroupKey {
  const action = item.recommended_action;
  const area = item.area.toLowerCase();
  const tags = (item.failure_tags ?? []).join(" ").toLowerCase();
  if (
    FOLLOW_CAM_ACTIONS.has(action) ||
    area.includes("camera") ||
    tags.includes("camera") ||
    !!item.camera_motion_event_id
  ) {
    return "cameraMotion";
  }
  if (
    HIGHLIGHT_ACTIONS.has(action) ||
    area.includes("highlight") ||
    tags.includes("highlight") ||
    tags.includes("post_roll") ||
    !!item.candidate_id
  ) {
    return "highlightBoundary";
  }
  if (MISSING_BALL_ACTIONS.has(action) || area.includes("missing") || tags.includes("ball_lost") || !!item.likely_ball_region) {
    return "missingBall";
  }
  return "noise";
}

function approvalForImprovement(
  approval: AIImproveApprovalResponse | undefined,
  improvementId: string,
): AIApprovedAction | null {
  return approval?.approved_actions.find((action) => action.improvement_id === improvementId) ?? null;
}

function summaryFlagEntries(summary: Record<string, unknown>): [string, string][] {
  return Object.entries(summary)
    .filter(([key, value]) => key !== "artifacts" && (typeof value === "boolean" || typeof value === "number" || typeof value === "string"))
    .map(([key, value]) => [key, String(value)]);
}

function artifactEntries(summary: AIImproveApprovalResponse["summary"]): [string, NonNullable<AIImproveApprovalResponse["summary"]["artifacts"]>[string]][] {
  return Object.entries(summary.artifacts ?? {}).filter(([, artifact]) => !!artifact?.name || !!artifact?.path);
}

function actionCountMap(actions: AIApprovedAction[]): Record<string, number> {
  return actions.reduce<Record<string, number>>((counts, action) => {
    counts[action.approved_action] = (counts[action.approved_action] ?? 0) + 1;
    return counts;
  }, {});
}

function normalizeApprovalArtifact(
  artifact: AIApprovedActionsArtifact,
  run: { run_id: string; artifacts: ArtifactSummary[] } | null,
): AIImproveApprovalResponse {
  const approvedActionsArtifact = artifactByName(run, "ai_improvement_approved_actions.json");
  const configPatchArtifact = artifactByName(run, "ai_improvement_approved_config_patch.json");
  const followCamArtifact = artifactByName(run, "follow_cam_rerender_plan.json");
  const counts = actionCountMap(artifact.approved_actions ?? []);
  const requiresTrackingRerun = Boolean(counts.tracking_rerun_before_follow_cam);
  const requiresHighRecallRerun = Boolean(counts.targeted_rerun);
  const requiresHighlightRender = Boolean(counts.adjust_highlight_window || counts.render_suggested_highlight);
  const requiresFollowCamRerender = Boolean(followCamArtifact && counts.adjust_follow_cam && !requiresTrackingRerun);
  const configPatchCount = (artifact.approved_actions ?? []).filter((action) => isFilledRecord(action.config_patch)).length;

  return {
    schema_version: artifact.schema_version,
    generated_at: artifact.generated_at,
    run_id: artifact.run_id,
    source_report: artifact.source_report,
    approved_by: artifact.approved_by,
    artifact_name: approvedActionsArtifact?.name ?? "ai_improvement_approved_actions.json",
    artifact_path: approvedActionsArtifact?.path ?? "",
    config_patch_artifact_name: configPatchArtifact?.name ?? null,
    config_patch_artifact_path: configPatchArtifact?.path ?? null,
    follow_cam_rerender_plan_artifact_name: followCamArtifact?.name ?? null,
    follow_cam_rerender_plan_artifact_path: followCamArtifact?.path ?? null,
    approved_actions: artifact.approved_actions ?? [],
    summary: {
      approved_action_count: artifact.approved_actions?.length ?? 0,
      approved_action_counts: counts,
      targeted_rerun_count: counts.targeted_rerun ?? 0,
      config_patch_count: configPatchCount,
      highlight_action_count: (counts.adjust_highlight_window ?? 0) + (counts.render_suggested_highlight ?? 0),
      follow_cam_action_count: (counts.adjust_follow_cam ?? 0) + (counts.tracking_rerun_before_follow_cam ?? 0),
      requires_execution: Boolean(
        requiresHighRecallRerun ||
          requiresTrackingRerun ||
          requiresFollowCamRerender ||
          requiresHighlightRender ||
          configPatchArtifact,
      ),
      requires_high_recall_rerun: requiresHighRecallRerun,
      requires_tracking_rerun: requiresTrackingRerun,
      requires_follow_cam_rerender: requiresFollowCamRerender,
      requires_highlight_render: requiresHighlightRender,
      artifacts: {
        approved_actions: {
          name: approvedActionsArtifact?.name ?? "ai_improvement_approved_actions.json",
          path: approvedActionsArtifact?.path ?? null,
          exists: Boolean(approvedActionsArtifact),
        },
        config_patch: {
          name: configPatchArtifact?.name ?? null,
          path: configPatchArtifact?.path ?? null,
          exists: Boolean(configPatchArtifact),
        },
        follow_cam_rerender_plan: {
          name: followCamArtifact?.name ?? null,
          path: followCamArtifact?.path ?? null,
          exists: Boolean(followCamArtifact),
        },
      },
    },
    warnings: artifact.warnings ?? [],
  };
}

function normalizeImprovementReportArtifact(
  artifact: AIImproveReportArtifact,
  sourceArtifact: ArtifactSummary | null,
): AIImproveResponse {
  return {
    summary: artifact.summary ?? {},
    artifact_name: sourceArtifact?.name ?? "ai_improvement_report.json",
    artifact_path: sourceArtifact?.path ?? "",
    improvements: artifact.improvements ?? [],
    highlight_adjustments: artifact.highlight_adjustments ?? [],
  };
}

function approvalsByImprovement(
  approval: AIImproveApprovalResponse | null,
  allowedImprovementIds?: ReadonlySet<string>,
): Record<string, AIImproveApprovalResponse> {
  const mapped: Record<string, AIImproveApprovalResponse> = {};
  if (!approval) return mapped;
  for (const action of approval.approved_actions ?? []) {
    if (allowedImprovementIds && !allowedImprovementIds.has(action.improvement_id)) continue;
    mapped[action.improvement_id] = approval;
  }
  return mapped;
}

function FieldRow({ label, value }: { label: string; value: string | number | null | undefined }) {
  if (value == null || value === "") return null;
  return (
    <div className="min-w-0">
      <p className="text-[11px] uppercase text-muted-foreground">{label}</p>
      <p className="truncate font-mono text-xs">{value}</p>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-56 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed">
      <code>{formatJson(value)}</code>
    </pre>
  );
}

function ApprovalPanel({
  labels,
  approval,
  action,
  onQueueRerun,
  onRenderHighlight,
  queuePending,
  renderPending,
}: {
  labels: AIAnalysisLabels;
  approval: AIImproveApprovalResponse;
  action: AIApprovedAction | null;
  onQueueRerun: (action: AIApprovedAction, artifactName: string) => void;
  onRenderHighlight: (action: AIApprovedAction) => void;
  queuePending: boolean;
  renderPending: boolean;
}) {
  const artifactName = approval.summary.artifacts?.approved_actions?.name ?? approval.artifact_name;
  const flags = summaryFlagEntries(approval.summary);
  const artifacts = artifactEntries(approval.summary);
  const canQueueRerun = !!action && EXECUTABLE_RERUN_ACTIONS.has(action.approved_action) && !!artifactName;
  const canRenderHighlight = !!action && HIGHLIGHT_ACTIONS.has(action.approved_action);
  const isFollowCam = !!action && FOLLOW_CAM_ACTIONS.has(action.approved_action);

  return (
    <div className="rounded-md border bg-emerald-50/60 p-3 space-y-3">
      <div className="flex items-start gap-2">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
        <div className="min-w-0">
          <p className="text-sm font-medium">{labels.approvedIntent}</p>
          <p className="text-xs text-muted-foreground">{labels.approvedIntentDesc}</p>
        </div>
      </div>

      {action && (
        <div className="grid gap-2 sm:grid-cols-3">
          <FieldRow label={labels.approvalId} value={action.approval_id} />
          <FieldRow label={labels.actionId} value={action.approved_action} />
          <FieldRow label={labels.frameWindow} value={frameWindowLabel(action)} />
        </div>
      )}

      {flags.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.summaryFlags}</p>
          <div className="flex flex-wrap gap-1.5">
            {flags.map(([key, value]) => (
              <Badge key={key} variant="outline" className="max-w-full truncate font-mono">
                {key}: {value}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {artifacts.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-xs font-medium">{labels.artifacts}</p>
          {artifacts.map(([key, artifact]) => (
            <div key={key} className="rounded-md bg-background/80 p-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={artifact.exists ? "default" : "secondary"}>{key}</Badge>
                {artifact.name && <span className="font-mono">{artifact.name}</span>}
              </div>
              {artifact.path && <p className="mt-1 truncate font-mono text-muted-foreground">{artifact.path}</p>}
            </div>
          ))}
        </div>
      )}

      {approval.config_patch_artifact_name && (
        <p className="text-xs text-muted-foreground">
          {labels.configPatchApprovedArtifact}: <span className="font-mono">{approval.config_patch_artifact_name}</span>
        </p>
      )}

      {approval.follow_cam_rerender_plan_artifact_name && (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">
            {labels.followCamPlanArtifact}:{" "}
            <span className="font-mono">{approval.follow_cam_rerender_plan_artifact_name}</span>
          </p>
          <p className="text-xs text-muted-foreground">{labels.followCamNoAutoRender}</p>
        </div>
      )}

      {action && isFilledRecord(action.config_patch) && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.configPatchAdvisory}</p>
          <JsonBlock value={action.config_patch} />
        </div>
      )}

      {action?.follow_cam_rerender_plan && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.followCamPlanArtifact}</p>
          <JsonBlock value={action.follow_cam_rerender_plan} />
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {canQueueRerun && action && (
          <Button
            type="button"
            size="sm"
            onClick={() => onQueueRerun(action, artifactName)}
            disabled={queuePending}
            data-testid={`button-queue-approved-rerun-${action.approval_id}`}
          >
            {queuePending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {labels.queueingApprovedRerun}
              </>
            ) : (
              <>
                <Play className="mr-2 h-4 w-4" />
                {labels.queueApprovedRerun}
              </>
            )}
          </Button>
        )}
        {canRenderHighlight && action && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => onRenderHighlight(action)}
            disabled={renderPending}
            data-testid={`button-render-approved-highlight-${action.approval_id}`}
          >
            {renderPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {labels.renderingApprovedHighlight}
              </>
            ) : (
              <>
                <Film className="mr-2 h-4 w-4" />
                {labels.renderApprovedHighlight}
              </>
            )}
          </Button>
        )}
        {isFollowCam && (
          <Badge variant="outline" className="px-2 py-1 text-xs">
            {labels.followCamNoAutoRender}
          </Badge>
        )}
      </div>

      {approval.warnings && approval.warnings.length > 0 && (
        <ul className="space-y-1 text-xs text-muted-foreground">
          {approval.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ImprovementItemPanel({
  labels,
  item,
  approval,
  approvedAction,
  approvePending,
  onApprove,
  onQueueRerun,
  onRenderHighlight,
  queuePending,
  renderPending,
}: {
  labels: AIAnalysisLabels;
  item: AIImprovementItem;
  approval?: AIImproveApprovalResponse;
  approvedAction: AIApprovedAction | null;
  approvePending: boolean;
  onApprove: (id: string) => void;
  onQueueRerun: (action: AIApprovedAction, artifactName: string) => void;
  onRenderHighlight: (action: AIApprovedAction) => void;
  queuePending: boolean;
  renderPending: boolean;
}) {
  const evidence = item.evidence ?? [];
  const sourcePacket = typeof item.source_packet_id === "string" ? item.source_packet_id : null;
  const visualReview = typeof item.visual_review_id === "string" ? item.visual_review_id : null;
  const hasConfigPatch = isFilledRecord(item.config_patch);
  const hasEvidencePayload = isFilledRecord(item.evidence_payload);
  const hasProvenance = isFilledRecord(item.provenance);

  return (
    <div className="rounded-md border p-3 space-y-3" data-testid={`ai-improvement-${item.id}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="secondary" className="font-mono">{item.id}</Badge>
            {item.priority && <Badge variant="outline">{item.priority}</Badge>}
            <Badge>{item.recommended_action}</Badge>
          </div>
          {item.diagnosis && <p className="text-sm text-muted-foreground">{item.diagnosis}</p>}
        </div>
        <Button
          type="button"
          size="sm"
          variant={approvedAction ? "outline" : "default"}
          onClick={() => onApprove(item.id)}
          disabled={approvePending || !!approvedAction}
          data-testid={`button-approve-improvement-${item.id}`}
        >
          {approvePending ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {labels.approvingSuggestion}
            </>
          ) : approvedAction ? (
            <>
              <CheckCircle2 className="mr-2 h-4 w-4" />
              {labels.approvedIntent}
            </>
          ) : (
            <>
              <ShieldCheck className="mr-2 h-4 w-4" />
              {labels.approveSuggestion}
            </>
          )}
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <FieldRow label="area" value={item.area} />
        <FieldRow label="recommended_action" value={item.recommended_action} />
        <FieldRow label={labels.frameWindow} value={frameWindowLabel(item)} />
        <FieldRow label={labels.confidence} value={item.confidence != null ? item.confidence.toFixed(2) : "n/a"} />
        <FieldRow label={labels.sourcePacket} value={sourcePacket} />
        <FieldRow label={labels.visualReview} value={visualReview} />
        <FieldRow label="root_cause_module" value={item.root_cause_module} />
        <FieldRow label="candidate_id" value={item.candidate_id} />
      </div>

      {!!item.failure_tags?.length && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.failureTags}</p>
          <div className="flex flex-wrap gap-1.5">
            {item.failure_tags.map((tag) => (
              <Badge key={tag} variant="outline">{tag}</Badge>
            ))}
          </div>
        </div>
      )}

      {(item.likely_ball_region || item.local_search_roi || item.suggested_window || item.follow_cam_rerender_plan) && (
        <div className="grid gap-2 lg:grid-cols-2">
          {item.likely_ball_region && (
            <div>
              <p className="mb-1 text-xs font-medium">{labels.likelyBallRegion}</p>
              <JsonBlock value={item.likely_ball_region} />
            </div>
          )}
          {item.local_search_roi && (
            <div>
              <p className="mb-1 text-xs font-medium">{labels.localSearchRoi}</p>
              <JsonBlock value={item.local_search_roi} />
            </div>
          )}
          {item.suggested_window && (
            <div>
              <p className="mb-1 text-xs font-medium">{labels.suggestedWindow}</p>
              <JsonBlock value={item.suggested_window} />
            </div>
          )}
          {item.follow_cam_rerender_plan && (
            <div>
              <p className="mb-1 text-xs font-medium">{labels.followCamPlanArtifact}</p>
              <JsonBlock value={item.follow_cam_rerender_plan} />
            </div>
          )}
        </div>
      )}

      {evidence.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.evidence}</p>
          <JsonBlock value={evidence} />
        </div>
      )}

      {hasEvidencePayload && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.evidencePayload}</p>
          <JsonBlock value={item.evidence_payload} />
        </div>
      )}

      {hasProvenance && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.provenance}</p>
          <JsonBlock value={item.provenance} />
        </div>
      )}

      {hasConfigPatch && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.configPatchAdvisory}</p>
          <JsonBlock value={item.config_patch} />
        </div>
      )}

      {approval && (
        <ApprovalPanel
          labels={labels}
          approval={approval}
          action={approvedAction}
          onQueueRerun={onQueueRerun}
          onRenderHighlight={onRenderHighlight}
          queuePending={queuePending}
          renderPending={renderPending}
        />
      )}
    </div>
  );
}

function HighlightAdjustmentPanel({ labels, adjustment }: { labels: AIAnalysisLabels; adjustment: AIHighlightAdjustment }) {
  const boundaryWarnings = [...(adjustment.boundary_warnings ?? []), ...(adjustment.warnings ?? [])];
  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="secondary" className="font-mono">{adjustment.candidate_id}</Badge>
        {adjustment.clip_action && <Badge>{adjustment.clip_action}</Badge>}
        {adjustment.confidence != null && <Badge variant="outline">{labels.confidence}: {adjustment.confidence.toFixed(2)}</Badge>}
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <FieldRow label={labels.coreWindow} value={adjustment.core_window ? frameWindowLabel({ suggested_window: adjustment.core_window }) : null} />
        <FieldRow label={labels.renderWindow} value={adjustment.render_window ? frameWindowLabel({ suggested_window: adjustment.render_window }) : null} />
        <FieldRow label={labels.currentWindow} value={frameWindowLabel({ suggested_window: adjustment.current_window })} />
        <FieldRow label={labels.suggestedWindow} value={frameWindowLabel({ suggested_window: adjustment.suggested_window })} />
        <FieldRow label={labels.reason} value={adjustment.reason} />
      </div>
      {adjustment.buffer_policy && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.bufferPolicy}</p>
          <JsonBlock value={adjustment.buffer_policy} />
        </div>
      )}
      {boundaryWarnings.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium">{labels.boundaryWarnings}</p>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {boundaryWarnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function AIAnalysisPage() {
  const { t, language } = useLanguage();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedRunId, setSelectedRunId] = useState("");
  const [objective, setObjective] = useState("");
  const [showPatch, setShowPatch] = useState(false);
  const [suggestion, setSuggestion] = useState<AISuggestion | null>(null);
  const [outputConfigName, setOutputConfigName] = useState("");
  const [selectedConfigName, setSelectedConfigName] = useState("");
  const [configText, setConfigText] = useState("");
  const [configExplanation, setConfigExplanation] = useState<AIExplainResponse | null>(null);
  const [showFullConfigExplanation, setShowFullConfigExplanation] = useState(false);
  const [fieldPreview, setFieldPreview] = useState<FieldPreviewResponse | null>(null);
  const [improvementReport, setImprovementReport] = useState<AIImproveResponse | null>(null);
  const [approvalsByImprovementId, setApprovalsByImprovementId] = useState<Record<string, AIImproveApprovalResponse>>({});

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 10_000,
  });

  const { data: configs, isLoading: configsLoading } = useQuery({
    queryKey: ["configs"],
    queryFn: api.listConfigs,
    refetchInterval: 30_000,
  });

  const analysableRuns = (runs ?? []).filter((r) => r.status === "completed" || r.status === "failed");
  const selectedRun = analysableRuns.find((r) => r.run_id === selectedRunId) ?? null;
  const playbackArtifact = selectedRun ? pickPlaybackArtifact(selectedRun.artifacts) : null;
  const playbackUrl = selectedRun && playbackArtifact ? artifactUrl(selectedRun.run_id, playbackArtifact) : null;
  const existingImprovementReportArtifact = artifactByName(selectedRun, "ai_improvement_report.json");
  const existingApprovalActionsArtifact = artifactByName(selectedRun, "ai_improvement_approved_actions.json");

  const {
    data: configDetail,
    error: configDetailError,
    isLoading: configDetailLoading,
    refetch: refetchConfig,
  } = useQuery({
    queryKey: ["config", selectedConfigName],
    queryFn: () => api.getConfig(selectedConfigName),
    enabled: !!selectedConfigName,
  });

  const existingImprovementReport = useQuery({
    queryKey: ["artifact-json", selectedRunId, "ai_improvement_report.json"],
    queryFn: async () => {
      const artifact = await api.getRunArtifactJson<AIImproveReportArtifact>(
        selectedRunId,
        "ai_improvement_report.json",
      );
      return normalizeImprovementReportArtifact(artifact, existingImprovementReportArtifact);
    },
    enabled: !!selectedRunId && !!existingImprovementReportArtifact,
    retry: false,
  });

  const existingApprovalActions = useQuery({
    queryKey: ["artifact-json", selectedRunId, "ai_improvement_approved_actions.json"],
    queryFn: async () => {
      const artifact = await api.getRunArtifactJson<AIApprovedActionsArtifact>(
        selectedRunId,
        "ai_improvement_approved_actions.json",
      );
      return normalizeApprovalArtifact(artifact, selectedRun);
    },
    enabled: !!selectedRunId && !!existingApprovalActionsArtifact,
    retry: false,
  });

  useEffect(() => {
    if (selectedRun?.config_name) setSelectedConfigName(selectedRun.config_name);
  }, [selectedRun?.config_name]);

  useEffect(() => {
    if (!selectedConfigName && configs?.length) setSelectedConfigName(configs[0].name);
  }, [configs, selectedConfigName]);

  useEffect(() => {
    if (configDetail) setConfigText(configDetail.text);
  }, [configDetail]);

  useEffect(() => {
    setConfigExplanation(null);
    setShowFullConfigExplanation(false);
  }, [selectedConfigName]);

  useEffect(() => {
    if (existingImprovementReport.data) setImprovementReport(existingImprovementReport.data);
  }, [existingImprovementReport.data]);

  const currentImprovementIds = useMemo(
    () => new Set((improvementReport?.improvements ?? []).map((item) => item.id)),
    [improvementReport],
  );

  useEffect(() => {
    if (existingApprovalActions.data) {
      setApprovalsByImprovementId(approvalsByImprovement(existingApprovalActions.data, currentImprovementIds));
    }
  }, [currentImprovementIds, existingApprovalActions.data]);

  // Reset preview when run changes
  function handleRunChange(id: string) {
    setSelectedRunId(id);
    setFieldPreview(null);
    setSuggestion(null);
    setOutputConfigName("");
    setImprovementReport(null);
    setApprovalsByImprovementId({});
  }

  const recommend = useMutation({
    mutationFn: (params: { runId: string; objective?: string; language: string }) =>
      api.aiRecommend({ run_id: params.runId, objective: params.objective, language: params.language }),
    onSuccess: (data, params) => {
      if (params.runId !== selectedRunId) return;
      setSuggestion(data);
      setOutputConfigName(data.output_name_suggestion ?? "");
      toast({ title: t.aiAnalysis.recommendationReady });
    },
    onError: (err: Error, params) => {
      if (params.runId !== selectedRunId) return;
      toast({ title: t.aiAnalysis.recommendationFailed, description: err.message, variant: "destructive" });
    },
  });

  const improve = useMutation({
    mutationFn: (params: { runId: string; objective?: string; language: string }) =>
      api.aiImprove({ run_id: params.runId, objective: params.objective, language: params.language }),
    onSuccess: (data, params) => {
      if (params.runId !== selectedRunId) return;
      setImprovementReport(data);
      setApprovalsByImprovementId(
        approvalsByImprovement(
          existingApprovalActions.data ?? null,
          new Set(data.improvements.map((item) => item.id)),
        ),
      );
      toast({ title: t.aiAnalysis.improvementReady });
    },
    onError: (err: Error, params) => {
      if (params.runId !== selectedRunId) return;
      toast({ title: t.aiAnalysis.improvementFailed, description: err.message, variant: "destructive" });
    },
  });

  const approveImprovement = useMutation({
    mutationFn: (params: { runId: string; improvementIds: string[] }) =>
      api.approveAIImprovements(params.runId, {
        improvement_ids: params.improvementIds,
        approved_by: "operator-ui",
      }),
    onSuccess: (data, params) => {
      if (params.runId !== selectedRunId) return;
      setApprovalsByImprovementId(approvalsByImprovement(data, currentImprovementIds));
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      toast({ title: t.aiAnalysis.approvalReady, description: data.artifact_name });
    },
    onError: (err: Error, params) => {
      if (params.runId !== selectedRunId) return;
      toast({ title: t.aiAnalysis.approvalFailed, description: err.message, variant: "destructive" });
    },
  });

  const queueApprovedRerun = useMutation({
    mutationFn: (params: { runId: string; action: AIApprovedAction; artifactName: string }) =>
      api.createRun({
        parent_run_id: params.runId,
        approved_action_ids: [params.action.approval_id],
        approved_actions_artifact_name: params.artifactName,
        notes: `operator-ui queued approved AI improvement action ${params.action.approval_id}`,
      }),
    onSuccess: (run, params) => {
      if (params.runId !== selectedRunId) return;
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      toast({ title: t.aiAnalysis.approvedRerunQueued, description: run.run_id });
    },
    onError: (err: Error, params) => {
      if (params.runId !== selectedRunId) return;
      toast({ title: t.aiAnalysis.approvedRerunFailed, description: err.message, variant: "destructive" });
    },
  });

  const renderApprovedHighlight = useMutation({
    mutationFn: (params: { runId: string; action: AIApprovedAction }) =>
      api.createHighlightRender(params.runId, {
        approved_action_id: params.action.approval_id,
        notes: `operator-ui rendered approved AI highlight action ${params.action.approval_id}`,
      }),
    onSuccess: (run, params) => {
      if (params.runId !== selectedRunId) return;
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      toast({ title: t.aiAnalysis.highlightRenderQueued, description: run.run_id });
    },
    onError: (err: Error, params) => {
      if (params.runId !== selectedRunId) return;
      toast({ title: t.aiAnalysis.highlightRenderFailed, description: err.message, variant: "destructive" });
    },
  });

  const saveConfig = useMutation({
    mutationFn: () => {
      if (!selectedRun?.config_name || !suggestion) {
        throw new Error(t.aiAnalysis.noConfigPatch);
      }
      if (!suggestion.patch || Object.keys(suggestion.patch).length === 0) {
        throw new Error(t.aiAnalysis.noConfigPatch);
      }
      if (configDirty) {
        throw new Error(t.aiAnalysis.unsavedConfigConflict);
      }
      if (selectedConfigName !== selectedRun.config_name) {
        throw new Error(t.aiAnalysis.configBaseMismatch);
      }
      return api.deriveConfig({
        base_config_name: selectedConfigName,
        output_name: outputConfigName.trim() || suggestion.output_name_suggestion || "tuned_config",
        patch: suggestion.patch,
      });
    },
    onSuccess: (detail) => {
      void queryClient.invalidateQueries({ queryKey: ["configs"] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.aiAnalysis.configSaved, description: detail.name });
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configSaveFailed, description: err.message, variant: "destructive" });
    },
  });

  const saveConfigFile = useMutation({
    mutationFn: () => api.updateConfig(selectedConfigName, { content: configText }),
    onSuccess: (detail) => {
      setConfigText(detail.text);
      void queryClient.invalidateQueries({ queryKey: ["configs"] });
      void queryClient.invalidateQueries({ queryKey: ["config", detail.name] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      toast({ title: t.aiAnalysis.configFileSaved, description: detail.name });
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configFileSaveFailed, description: err.message, variant: "destructive" });
    },
  });

  const explainConfig = useMutation({
    mutationFn: () => api.aiExplain({ config_name: selectedConfigName, language }),
    onSuccess: (data) => {
      setConfigExplanation(data);
      setShowFullConfigExplanation(false);
    },
    onError: (err: Error) => {
      toast({ title: t.aiAnalysis.configExplanationFailed, description: err.message, variant: "destructive" });
    },
  });

  const configDirty = !!configDetail && configText !== configDetail.text;
  const aiSaveBlockedByEditor =
    configDirty || (!!selectedRun?.config_name && selectedConfigName !== selectedRun.config_name);
  const canSaveConfig =
    !!selectedRun?.config_name &&
    !!suggestion?.patch &&
    Object.keys(suggestion.patch).length > 0 &&
    !aiSaveBlockedByEditor &&
    !saveConfig.isPending;
  const canSaveConfigFile = !!selectedConfigName && configDirty && !saveConfigFile.isPending;
  const configExplanationPreviewLimit = 8;
  const visibleConfigEvidence = configExplanation
    ? showFullConfigExplanation
      ? configExplanation.evidence
      : configExplanation.evidence.slice(0, configExplanationPreviewLimit)
    : [];
  const hiddenConfigEvidenceCount = configExplanation
    ? Math.max(0, configExplanation.evidence.length - visibleConfigEvidence.length)
    : 0;
  const groupedImprovements = useMemo(() => {
    const groups: Record<ImprovementGroupKey, AIImprovementItem[]> = {
      missingBall: [],
      noise: [],
      cameraMotion: [],
      highlightBoundary: [],
    };
    for (const item of improvementReport?.improvements ?? []) {
      groups[classifyImprovement(item)].push(item);
    }
    return groups;
  }, [improvementReport]);
  const improvementGroups: Array<{ key: ImprovementGroupKey; label: string; items: AIImprovementItem[] }> = [
    { key: "missingBall", label: t.aiAnalysis.groupMissingBall, items: groupedImprovements.missingBall },
    { key: "noise", label: t.aiAnalysis.groupNoise, items: groupedImprovements.noise },
    { key: "cameraMotion", label: t.aiAnalysis.groupCameraMotion, items: groupedImprovements.cameraMotion },
    { key: "highlightBoundary", label: t.aiAnalysis.groupHighlightBoundary, items: groupedImprovements.highlightBoundary },
  ];
  const approvedImprovementIds = useMemo(
    () => Object.keys(approvalsByImprovementId),
    [approvalsByImprovementId],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t.aiAnalysis.title}</h1>
        <p className="text-muted-foreground mt-1">{t.aiAnalysis.subtitle}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Run selector */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Brain className="h-4 w-4 text-primary" />
              {t.aiAnalysis.selectRun}
            </CardTitle>
            <CardDescription>{t.aiAnalysis.selectRunDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {runsLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t.aiAnalysis.loadingRuns}
              </div>
            ) : !analysableRuns.length ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.aiAnalysis.noRuns}</AlertDescription>
              </Alert>
            ) : (
              <Select value={selectedRunId} onValueChange={handleRunChange} data-testid="select-ai-run">
                <SelectTrigger data-testid="trigger-ai-run">
                  <SelectValue placeholder={t.aiAnalysis.selectRunPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {analysableRuns.map((r) => (
                    <SelectItem key={r.run_id} value={r.run_id} data-testid={`option-ai-run-${r.run_id}`}>
                      {r.run_id} · {r.status}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {selectedRun && (
              <div className="rounded-md bg-muted/50 p-3 space-y-1.5">
                <div className="flex items-center gap-2">
                  <span className={cn("text-xs font-medium px-2 py-0.5 rounded-full", statusBadgeClass(selectedRun.status))}>
                    {selectedRun.status}
                  </span>
                  <span className="text-xs text-muted-foreground">{formatDateTime(runMoment(selectedRun))}</span>
                </div>
                <p className="text-xs font-mono text-muted-foreground truncate">{selectedRun.output_dir}</p>
                {selectedRun.input_video && (
                  <p className="text-xs text-muted-foreground truncate">{selectedRun.input_video}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Objective */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4 text-primary" />
              {t.aiAnalysis.objective}
            </CardTitle>
            <CardDescription>{t.aiAnalysis.objectiveDesc}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Label htmlFor="input-objective" className="sr-only">{t.aiAnalysis.objective}</Label>
            <Textarea
              id="input-objective"
              placeholder={t.aiAnalysis.objectivePlaceholder}
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              rows={4}
              data-testid="input-objective"
              className="resize-none"
            />
            <Button
              onClick={() =>
                recommend.mutate({
                  runId: selectedRunId,
                  objective: objective.trim() || undefined,
                  language,
                })
              }
              disabled={!selectedRunId || recommend.isPending}
              className="w-full"
              data-testid="button-ai-recommend"
            >
              {recommend.isPending ? (
                <><Loader2 className="h-4 w-4 mr-2 animate-spin" />{t.aiAnalysis.analysing}</>
              ) : (
                <><Sparkles className="h-4 w-4 mr-2" />{t.aiAnalysis.getRecommendation}</>
              )}
            </Button>
          </CardContent>
        </Card>
      </div>

      <Card data-testid="card-ai-improvement-workflow">
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <ListChecks className="h-4 w-4 text-primary" />
                {t.aiAnalysis.improvementWorkflow}
              </CardTitle>
              <CardDescription>{t.aiAnalysis.improvementWorkflowDesc}</CardDescription>
            </div>
            <Button
              type="button"
              onClick={() =>
                improve.mutate({
                  runId: selectedRunId,
                  objective: objective.trim() || undefined,
                  language,
                })
              }
              disabled={!selectedRunId || improve.isPending}
              data-testid="button-ai-improve"
              className="shrink-0"
            >
              {improve.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t.aiAnalysis.reviewingImprovement}
                </>
              ) : (
                <>
                  <Wand2 className="h-4 w-4 mr-2" />
                  {t.aiAnalysis.getImprovementReview}
                </>
              )}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <Alert>
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{t.aiAnalysis.advisoryOnlyHint}</AlertDescription>
          </Alert>

          {improvementReport && (
            <div className="space-y-4">
              <div className="rounded-md border bg-muted/30 p-3 space-y-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="text-sm font-medium">{t.aiAnalysis.advisoryReport}</p>
                    <p className="text-xs text-muted-foreground">
                      {t.aiAnalysis.reportArtifact}: <span className="font-mono">{improvementReport.artifact_name}</span>
                    </p>
                  </div>
                  <Badge variant="outline" className="w-fit font-mono">
                    {improvementReport.improvements.length} improvements
                  </Badge>
                </div>
                <JsonBlock value={improvementReport.summary} />
                <p className="truncate text-xs font-mono text-muted-foreground">{improvementReport.artifact_path}</p>
              </div>

              {improvementReport.improvements.length === 0 ? (
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{t.aiAnalysis.noImprovements}</AlertDescription>
                </Alert>
              ) : (
                <div className="space-y-4">
                  {improvementGroups.map((group) => (
                    <section key={group.key} className="space-y-2">
                      <div className="flex items-center justify-between gap-3">
                        <h2 className="text-sm font-semibold">{group.label}</h2>
                        <Badge variant="outline">{group.items.length}</Badge>
                      </div>
                      {group.items.length > 0 ? (
                        <div className="space-y-3">
                          {group.items.map((item) => {
                            const approval = approvalsByImprovementId[item.id];
                            const approvedAction = approvalForImprovement(approval, item.id);
                            return (
                              <ImprovementItemPanel
                                key={item.id}
                                labels={t.aiAnalysis}
                                item={item}
                                approval={approval}
                                approvedAction={approvedAction}
                                approvePending={approveImprovement.isPending}
                                onApprove={(id) =>
                                  approveImprovement.mutate({
                                    runId: selectedRunId,
                                    improvementIds: Array.from(new Set([...approvedImprovementIds, id])),
                                  })
                                }
                                onQueueRerun={(action, artifactName) =>
                                  queueApprovedRerun.mutate({ runId: selectedRunId, action, artifactName })
                                }
                                onRenderHighlight={(action) =>
                                  renderApprovedHighlight.mutate({ runId: selectedRunId, action })
                                }
                                queuePending={queueApprovedRerun.isPending}
                                renderPending={renderApprovedHighlight.isPending}
                              />
                            );
                          })}
                        </div>
                      ) : (
                        <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                          {t.common.noData}
                        </p>
                      )}
                    </section>
                  ))}
                </div>
              )}

              {improvementReport.highlight_adjustments.length > 0 && (
                <section className="space-y-2">
                  <h2 className="text-sm font-semibold">{t.aiAnalysis.highlightAdjustments}</h2>
                  <div className="space-y-3">
                    {improvementReport.highlight_adjustments.map((adjustment) => (
                      <HighlightAdjustmentPanel
                        key={`${adjustment.candidate_id}-${adjustment.suggested_window.start_frame}-${adjustment.suggested_window.end_frame}`}
                        labels={t.aiAnalysis}
                        adjustment={adjustment}
                      />
                    ))}
                  </div>
                </section>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card data-testid="card-config-file-editor">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4 text-primary" />
            {t.aiAnalysis.configFile}
          </CardTitle>
          <CardDescription>{t.aiAnalysis.configFileDesc}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {configsLoading ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t.aiAnalysis.loadingConfigs}
            </div>
          ) : !configs?.length ? (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{t.dashboard.noConfigs}</AlertDescription>
            </Alert>
          ) : (
            <Select value={selectedConfigName} onValueChange={setSelectedConfigName} data-testid="select-editor-config">
              <SelectTrigger data-testid="trigger-editor-config">
                <SelectValue placeholder={t.aiAnalysis.selectConfig} />
              </SelectTrigger>
              <SelectContent>
                {configs.map((config) => (
                  <SelectItem key={config.name} value={config.name} data-testid={`option-editor-config-${config.name}`}>
                    {config.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}

          {configDetail?.path && (
            <p className="text-xs text-muted-foreground truncate">
              {t.aiAnalysis.configFilePath}: <span className="font-mono">{configDetail.path}</span>
            </p>
          )}

          {configDirty && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{t.aiAnalysis.unsavedConfigHint}</AlertDescription>
            </Alert>
          )}

          {configDetailError ? (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {configDetailError instanceof Error ? configDetailError.message : t.aiAnalysis.configLoadFailed}
              </AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-2">
              <Textarea
                value={configText}
                onChange={(event) => setConfigText(event.target.value)}
                disabled={!selectedConfigName || configDetailLoading}
                placeholder={configDetailLoading ? t.aiAnalysis.loadingConfig : ""}
                className="min-h-[360px] resize-y font-mono text-xs leading-relaxed"
                spellCheck={false}
                data-testid="textarea-config-yaml"
              />
              <div className="flex flex-wrap justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => explainConfig.mutate()}
                  disabled={!selectedConfigName || configDirty || explainConfig.isPending}
                  data-testid="button-explain-config"
                >
                  {explainConfig.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      {t.aiAnalysis.explainingConfig}
                    </>
                  ) : (
                    <>
                      <Brain className="h-4 w-4 mr-2" />
                      {t.aiAnalysis.explainConfig}
                    </>
                  )}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    if (configDetail) setConfigText(configDetail.text);
                    void refetchConfig();
                  }}
                  disabled={!selectedConfigName || configDetailLoading || saveConfigFile.isPending}
                  data-testid="button-reload-config-file"
                >
                  <RotateCcw className="h-4 w-4 mr-2" />
                  {t.aiAnalysis.reloadConfigFile}
                </Button>
                <Button
                  type="button"
                  onClick={() => saveConfigFile.mutate()}
                  disabled={!canSaveConfigFile}
                  data-testid="button-save-config-file"
                >
                  {saveConfigFile.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      {t.aiAnalysis.savingConfigFile}
                    </>
                  ) : (
                    <>
                      <Save className="h-4 w-4 mr-2" />
                      {t.aiAnalysis.saveConfigFile}
                    </>
                  )}
                </Button>
              </div>
              {configExplanation && (
                <div className="rounded-md border bg-muted/40 p-3 space-y-2" data-testid="config-explanation">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium">{t.aiAnalysis.configExplanation}</p>
                    {configExplanation.evidence.length > configExplanationPreviewLimit && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 shrink-0"
                        onClick={() => setShowFullConfigExplanation((open) => !open)}
                        data-testid="button-toggle-config-explanation"
                      >
                        {showFullConfigExplanation ? (
                          <>
                            <ChevronUp className="h-4 w-4 mr-1.5" />
                            {t.aiAnalysis.collapseConfigExplanation}
                          </>
                        ) : (
                          <>
                            <ChevronDown className="h-4 w-4 mr-1.5" />
                            {t.aiAnalysis.expandConfigExplanation(hiddenConfigEvidenceCount)}
                          </>
                        )}
                      </Button>
                    )}
                  </div>
                  <p className="text-sm text-muted-foreground">{configExplanation.summary}</p>
                  {visibleConfigEvidence.length > 0 && (
                    <ul className="space-y-1">
                      {visibleConfigEvidence.map((item, index) => (
                        <li key={index} className="flex gap-2 text-xs text-muted-foreground">
                          <span className="text-primary">•</span>
                          <span>{item}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Result Video */}
      {selectedRun && (
        <Card data-testid="card-result-video">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Film className="h-4 w-4 text-primary" />
              {t.aiAnalysis.resultVideo}
            </CardTitle>
            <CardDescription>
              {playbackArtifact ? t.aiAnalysis.resultVideoDesc : t.aiAnalysis.noResultVideo}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {playbackArtifact && playbackUrl ? (
              <>
                <div className="overflow-hidden rounded-md border bg-black">
                  <video
                    key={`${selectedRun.run_id}-${playbackArtifact.name}`}
                    className="block max-h-[70vh] w-full bg-black"
                    controls
                    preload="metadata"
                    data-testid="video-run-artifact"
                  >
                    <source src={playbackUrl} type={playbackArtifact.content_type ?? "video/mp4"} />
                  </video>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">{playbackArtifact.name}</span>
                  <span>{formatBytes(playbackArtifact.size_bytes)}</span>
                </div>
              </>
            ) : selectedRun.input_video ? (
              <>
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{t.aiAnalysis.fieldPreviewFallback}</AlertDescription>
                </Alert>
                <FieldPreviewCanvas
                  inputVideo={selectedRun.input_video}
                  suggestion={suggestion}
                  preview={fieldPreview}
                  onPreviewChange={setFieldPreview}
                />
              </>
            ) : (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{t.aiAnalysis.noInputVideo}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      )}

      {/* AI Suggestion Results */}
      {suggestion && (
        <Card data-testid="card-ai-suggestion">
          <CardHeader className="pb-3">
            <div className="flex items-start gap-3">
              <CheckCircle2 className="h-5 w-5 text-emerald-500 mt-0.5 shrink-0" />
              <div>
                <CardTitle className="text-base">{suggestion.title}</CardTitle>
                <CardDescription className="mt-1">{suggestion.diagnosis}</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="text-sm font-medium mb-1">{t.aiAnalysis.recommendation}</p>
              <p className="text-sm text-muted-foreground">{suggestion.recommendation}</p>
            </div>

            {suggestion.expected_tradeoff && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-1">{t.aiAnalysis.expectedTradeoff}</p>
                  <p className="text-sm text-muted-foreground">{suggestion.expected_tradeoff}</p>
                </div>
              </>
            )}

            {suggestion.evidence.length > 0 && (
              <>
                <Separator />
                <div>
                  <p className="text-sm font-medium mb-2">{t.aiAnalysis.evidence}</p>
                  <ul className="space-y-1">
                    {suggestion.evidence.map((e, i) => (
                      <li key={i} className="text-sm text-muted-foreground flex gap-2">
                        <span className="text-primary mt-0.5">•</span>
                        {e}
                      </li>
                    ))}
                  </ul>
                </div>
              </>
            )}

            {suggestion.patch_preview.length > 0 && (
              <>
                <Separator />
                <div>
                  <button
                    type="button"
                    onClick={() => setShowPatch((p) => !p)}
                    className="flex items-center gap-2 text-sm font-medium hover:text-primary transition-colors"
                    data-testid="button-toggle-patch"
                  >
                    {t.aiAnalysis.configPatchPreview}
                    {showPatch ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                  </button>
                  {showPatch && (
                    <pre className="mt-2 rounded-md bg-muted p-3 text-xs font-mono overflow-x-auto">
                      {suggestion.patch_preview.join("\n")}
                    </pre>
                  )}
                </div>
              </>
            )}

            {(suggestion.output_name_suggestion || selectedRun?.config_name) && (
              <div className="rounded-md bg-accent/50 p-3 space-y-2">
                <Label htmlFor="input-tuned-config-name" className="text-xs text-muted-foreground">
                  {t.aiAnalysis.suggestedOutputName}
                </Label>
                {aiSaveBlockedByEditor && (
                  <Alert>
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>
                      {configDirty ? t.aiAnalysis.unsavedConfigConflict : t.aiAnalysis.configBaseMismatch}
                    </AlertDescription>
                  </Alert>
                )}
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    id="input-tuned-config-name"
                    value={outputConfigName}
                    onChange={(event) => setOutputConfigName(event.target.value)}
                    placeholder={suggestion.output_name_suggestion ?? "tuned_config"}
                    className="font-mono text-sm"
                    data-testid="input-tuned-config-name"
                  />
                  <Button
                    type="button"
                    onClick={() => saveConfig.mutate()}
                    disabled={!canSaveConfig}
                    data-testid="button-save-tuned-config"
                  >
                    {saveConfig.isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        {t.aiAnalysis.savingConfig}
                      </>
                    ) : (
                      <>
                        <CopyPlus className="h-4 w-4 mr-2" />
                        {t.aiAnalysis.saveConfig}
                      </>
                    )}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
