import type {
  ArtifactSummary,
  AssetGroup,
  ConfigListItem,
  RunRecord,
} from "@workspace/api-client-react";

import {
  broadcastCancellationTarget,
  recoverBroadcastWorkflowRun,
} from "./broadcastWorkflow";

const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);
const ACTIVE_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "reconciling",
  "committing",
  "copying",
  "validating",
]);
const SHA256 = /^[0-9a-f]{64}$/;

export type ProductionHistoryKind =
  | "trial"
  | "full"
  | "recompute"
  | "render"
  | "failed"
  | "cancelled"
  | "legacy";

export type ProductionHistoryFilter =
  | "all"
  | "active"
  | "ready"
  | "failed"
  | "cancelled";

export interface ProductionHistoryNote {
  purpose: "production_trial" | "production_full";
  workflowId: string;
  generation: number;
  configIdentity: string | null;
  acceptedTrialRunId: string | null;
}

export interface ProductionHistoryTimelineItem {
  run: RunRecord;
  kind: ProductionHistoryKind;
  parentRunId: string | null;
  externalParentRunId: string | null;
  lineageIssue: "ambiguous_parent" | "missing_parent" | null;
  note: ProductionHistoryNote | null;
}

export interface ProductionHistorySummary {
  trialCount: number;
  activeCount: number;
  fullRunCount: number;
  latestFullStatus: string | null;
  readyCandidateCount: number;
  failedCount: number;
  cancelledCount: number;
}

export interface ProductionHistoryGroup {
  key: string;
  groupId: string;
  title: string;
  inputPath: string | null;
  lastActivityAt: string | null;
  isUnbound: boolean;
  configs: ConfigListItem[];
  timeline: ProductionHistoryTimelineItem[];
  summary: ProductionHistorySummary;
}

export type ProductionProductClassification =
  | { status: "candidate"; reason: "not_verified" }
  | {
      status: "unavailable";
      reason: "not_ready" | "missing_generation" | "missing_broadcast";
    }
  | {
      status: "verified";
      video: ArtifactSummary;
      quality: ArtifactSummary | null;
      downloads: ArtifactSummary[];
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function nonEmptyText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function sha256(value: unknown): value is string {
  return typeof value === "string" && SHA256.test(value);
}

function positiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

/**
 * Only server-produced production notes are accepted. The API validates these
 * notes against metadata.production_workflow before a production full run can
 * start; malformed/free-form notes stay untrusted and create no lineage edge.
 */
export function parseProductionHistoryNote(
  notes: string | null | undefined,
): ProductionHistoryNote | null {
  if (!notes) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(notes);
  } catch {
    return null;
  }
  if (
    !isRecord(parsed) ||
    parsed.schema_version !== "1.0" ||
    !positiveInteger(parsed.generation)
  ) {
    return null;
  }
  const workflowId = nonEmptyText(parsed.workflow_id);
  const submissionId = nonEmptyText(parsed.submission_id);
  const outputId = nonEmptyText(parsed.output_id);
  if (!workflowId || !submissionId || !outputId) return null;

  if (parsed.purpose === "production_trial") {
    if (
      !sha256(parsed.calibration_digest) ||
      !sha256(parsed.intent_sha256) ||
      !Number.isInteger(parsed.start_frame) ||
      Number(parsed.start_frame) < 0 ||
      !positiveInteger(parsed.max_frames) ||
      typeof parsed.enable_postprocess !== "boolean" ||
      typeof parsed.enable_follow_cam !== "boolean"
    ) {
      return null;
    }
    return {
      purpose: "production_trial",
      workflowId,
      generation: parsed.generation,
      configIdentity: null,
      acceptedTrialRunId: null,
    };
  }

  if (parsed.purpose === "production_full") {
    const acceptedTrialRunId = nonEmptyText(parsed.accepted_trial_run_id);
    const configName = nonEmptyText(parsed.confirmed_config_name);
    if (
      !acceptedTrialRunId ||
      !configName ||
      !sha256(parsed.accepted_trial_request_sha256) ||
      !sha256(parsed.expected_config_sha256) ||
      !sha256(parsed.config_patch_sha256) ||
      !sha256(parsed.calibration_digest) ||
      !isRecord(parsed.source_signature) ||
      !nonEmptyText(parsed.source_signature.path) ||
      typeof parsed.source_signature.size_bytes !== "number" ||
      !Number.isFinite(parsed.source_signature.size_bytes) ||
      parsed.source_signature.size_bytes < 0 ||
      !nonEmptyText(parsed.source_signature.modified_at)
    ) {
      return null;
    }
    return {
      purpose: "production_full",
      workflowId,
      generation: parsed.generation,
      configIdentity: `${configName}@${parsed.expected_config_sha256}`,
      acceptedTrialRunId,
    };
  }
  return null;
}

function timestamp(run: RunRecord): string | null {
  return run.completed_at ?? run.started_at ?? run.created_at ?? null;
}

function timestampNumber(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function stablePathHash(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function historyKind(
  run: RunRecord,
  note: ProductionHistoryNote | null,
): ProductionHistoryKind {
  if (run.status === "failed") return "failed";
  if (run.status === "cancelled") return "cancelled";
  if (run.broadcast?.operation === "recompute") return "recompute";
  if (run.broadcast?.operation === "render") return "render";
  if (run.source === "broadcast_hybrid" || note?.purpose === "production_full")
    return "full";
  if (note?.purpose === "production_trial") return "trial";
  return "legacy";
}

function explicitLineageParent(run: RunRecord): {
  parentRunId: string | null;
  ambiguous: boolean;
} {
  const candidates = new Set<string>();
  const direct = nonEmptyText(run.parent_run_id);
  const broadcastParent = nonEmptyText(run.broadcast?.parent_run_id);
  if (direct) candidates.add(direct);
  if (broadcastParent) candidates.add(broadcastParent);
  const note = parseProductionHistoryNote(run.notes);
  if (note?.purpose === "production_full" && note.acceptedTrialRunId) {
    candidates.add(note.acceptedTrialRunId);
  }
  if (candidates.size !== 1) {
    return { parentRunId: null, ambiguous: candidates.size > 1 };
  }
  return { parentRunId: [...candidates][0], ambiguous: false };
}

function buildTimeline(
  runs: readonly RunRecord[],
): ProductionHistoryTimelineItem[] {
  const ids = new Set(runs.map((run) => run.run_id));
  return runs
    .map((run) => {
      const note = parseProductionHistoryNote(run.notes);
      const lineage = explicitLineageParent(run);
      const parentExists = lineage.parentRunId
        ? ids.has(lineage.parentRunId)
        : false;
      return {
        run,
        kind: historyKind(run, note),
        parentRunId: parentExists ? lineage.parentRunId : null,
        externalParentRunId:
          lineage.parentRunId && !parentExists ? lineage.parentRunId : null,
        lineageIssue: lineage.ambiguous
          ? "ambiguous_parent"
          : lineage.parentRunId && !parentExists
            ? "missing_parent"
            : null,
        note,
      } satisfies ProductionHistoryTimelineItem;
    })
    .sort((left, right) => {
      const timeOrder =
        timestampNumber(timestamp(right.run)) -
        timestampNumber(timestamp(left.run));
      return timeOrder || left.run.run_id.localeCompare(right.run.run_id);
    });
}

export function isReadyProductCandidate(run: RunRecord): boolean {
  return (
    run.source === "broadcast_hybrid" &&
    run.status === "completed" &&
    run.broadcast?.status === "ready"
  );
}

function summarize(
  timeline: readonly ProductionHistoryTimelineItem[],
): ProductionHistorySummary {
  const fullRuns = timeline.filter(
    (item) =>
      item.run.source === "broadcast_hybrid" ||
      item.note?.purpose === "production_full",
  );
  return {
    trialCount: timeline.filter(
      (item) => item.note?.purpose === "production_trial",
    ).length,
    activeCount: timeline.filter((item) =>
      ACTIVE_RUN_STATUSES.has(item.run.status),
    ).length,
    fullRunCount: fullRuns.length,
    latestFullStatus:
      fullRuns[0]?.run.broadcast?.status ?? fullRuns[0]?.run.status ?? null,
    readyCandidateCount: timeline.filter((item) =>
      isReadyProductCandidate(item.run),
    ).length,
    failedCount: timeline.filter((item) => item.run.status === "failed").length,
    cancelledCount: timeline.filter((item) => item.run.status === "cancelled")
      .length,
  };
}

interface MutableGroup {
  key: string;
  path: string | null;
  title: string;
  candidateAlias: string;
  lastActivityAt: string | null;
  isUnbound: boolean;
  configs: ConfigListItem[];
  runs: RunRecord[];
}

function groupKey(path: string | null): string {
  return path ? `input:${path}` : "legacy:unbound";
}

/** Build one canonical client projection even if the server returns duplicate aliases or rows. */
export function buildProductionHistoryGroups(
  sourceGroups: readonly AssetGroup[],
): ProductionHistoryGroup[] {
  const groupByKey = new Map<string, MutableGroup>();
  const runCandidates = new Map<
    string,
    { run: RunRecord; paths: Set<string> }
  >();

  const ensureGroup = (
    path: string | null,
    source?: AssetGroup,
  ): MutableGroup => {
    const key = groupKey(path);
    const existing = groupByKey.get(key);
    if (existing) return existing;
    const created: MutableGroup = {
      key,
      path,
      title:
        source?.title ??
        (path
          ? path.replaceAll("\\", "/").split("/").at(-1) || path
          : "Unbound / Legacy"),
      candidateAlias:
        nonEmptyText(source?.group_id) ??
        (path ? "input-group" : "unbound-legacy"),
      lastActivityAt: source?.last_activity_at ?? null,
      isUnbound: !path,
      configs: [],
      runs: [],
    };
    groupByKey.set(key, created);
    return created;
  };

  for (const source of sourceGroups) {
    const sourcePath = nonEmptyText(source.input_video?.path);
    if (sourcePath) ensureGroup(sourcePath, source);
    for (const config of source.configs ?? []) {
      const configPath = nonEmptyText(config.input_video) ?? sourcePath;
      ensureGroup(
        configPath,
        configPath === sourcePath ? source : undefined,
      ).configs.push(config);
    }
    for (const run of source.runs ?? []) {
      const runPath = nonEmptyText(run.input_video) ?? sourcePath;
      const existing = runCandidates.get(run.run_id);
      if (existing) {
        if (runPath) existing.paths.add(runPath);
      } else {
        runCandidates.set(run.run_id, {
          run,
          paths: new Set(runPath ? [runPath] : []),
        });
      }
    }
  }

  for (const { run, paths } of runCandidates.values()) {
    const canonicalPath = paths.size === 1 ? [...paths][0] : null;
    ensureGroup(canonicalPath).runs.push(run);
  }

  const aliasCounts = new Map<string, number>();
  for (const group of groupByKey.values()) {
    aliasCounts.set(
      group.candidateAlias,
      (aliasCounts.get(group.candidateAlias) ?? 0) + 1,
    );
  }

  return [...groupByKey.values()]
    .filter(
      (group) => !group.isUnbound || group.runs.length || group.configs.length,
    )
    .map((group) => {
      const timeline = buildTimeline(group.runs);
      const runActivity = timeline[0] ? timestamp(timeline[0].run) : null;
      const lastActivityAt =
        timestampNumber(runActivity) > timestampNumber(group.lastActivityAt)
          ? runActivity
          : group.lastActivityAt;
      return {
        key: group.key,
        groupId:
          (aliasCounts.get(group.candidateAlias) ?? 0) > 1 && group.path
            ? `${group.candidateAlias}--${stablePathHash(group.path)}`
            : group.candidateAlias,
        title: group.title,
        inputPath: group.path,
        lastActivityAt,
        isUnbound: group.isUnbound,
        configs: [...group.configs].sort((left, right) =>
          left.name.localeCompare(right.name),
        ),
        timeline,
        summary: summarize(timeline),
      } satisfies ProductionHistoryGroup;
    })
    .sort((left, right) => {
      if (left.isUnbound !== right.isUnbound) return left.isUnbound ? 1 : -1;
      return (
        timestampNumber(right.lastActivityAt) -
          timestampNumber(left.lastActivityAt) ||
        left.key.localeCompare(right.key)
      );
    });
}

export function filterProductionHistoryGroups(
  groups: readonly ProductionHistoryGroup[],
  search: string,
  filter: ProductionHistoryFilter,
): ProductionHistoryGroup[] {
  const needle = search.trim().toLowerCase();
  return groups.filter((group) => {
    const matchesFilter =
      filter === "all" ||
      (filter === "active" && group.summary.activeCount > 0) ||
      (filter === "ready" && group.summary.readyCandidateCount > 0) ||
      (filter === "failed" && group.summary.failedCount > 0) ||
      (filter === "cancelled" && group.summary.cancelledCount > 0);
    if (!matchesFilter) return false;
    if (!needle) return true;
    return [
      group.title,
      group.inputPath ?? "",
      ...group.configs.map((config) => config.name),
      ...group.timeline.map((item) => item.run.run_id),
    ].some((value) => value.toLowerCase().includes(needle));
  });
}

export function classifyProductionProduct(
  run: RunRecord,
  artifacts?: readonly ArtifactSummary[],
): ProductionProductClassification {
  if (!isReadyProductCandidate(run)) {
    return { status: "unavailable", reason: "not_ready" };
  }
  if (!sha256(run.broadcast?.status_generation)) {
    return { status: "unavailable", reason: "missing_generation" };
  }
  if (artifacts === undefined) {
    return { status: "candidate", reason: "not_verified" };
  }
  const video = artifacts.find(
    (artifact) => artifact.name === "broadcast.mp4" && artifact.exists,
  );
  if (!video) {
    return { status: "unavailable", reason: "missing_broadcast" };
  }
  return {
    status: "verified",
    video,
    quality:
      artifacts.find(
        (artifact) =>
          artifact.name === "broadcast_quality_report.json" && artifact.exists,
      ) ?? null,
    downloads: artifacts
      .filter((artifact) => artifact.exists)
      .sort((left, right) => left.name.localeCompare(right.name)),
  };
}

export function productionProductVerificationKey(
  run: RunRecord,
): readonly [string, string, string, string] | null {
  const generation = run.broadcast?.status_generation;
  return isReadyProductCandidate(run) && sha256(generation)
    ? ["production-history", "product", run.run_id, generation]
    : null;
}

export function productionHistoryCancellationTarget(
  run: RunRecord,
  groupRuns: readonly RunRecord[],
): string | null {
  const isBroadcast =
    run.source === "broadcast_hybrid" ||
    Boolean(run.broadcast?.operation) ||
    Boolean(run.broadcast?.parent_run_id);
  if (isBroadcast) {
    return broadcastCancellationTarget(
      recoverBroadcastWorkflowRun(run, groupRuns),
    );
  }
  return ACTIVE_RUN_STATUSES.has(run.status) ? run.run_id : null;
}

export function productionHistoryDeletionBlocker(
  run: RunRecord,
  groupRuns: readonly RunRecord[],
): string | null {
  if (
    ACTIVE_RUN_STATUSES.has(run.status) ||
    ACTIVE_OPERATION_STATUSES.has(run.broadcast?.operation_status ?? "")
  ) {
    return "active_run";
  }
  const children = groupRuns.filter((candidate) => {
    const lineage = explicitLineageParent(candidate);
    return !lineage.ambiguous && lineage.parentRunId === run.run_id;
  });
  return children.length ? `children:${children.length}` : null;
}

export function productionArtifactUrl(
  runId: string,
  artifactName: string,
  statusGeneration: string,
): string {
  const encodedName = artifactName
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodedName}?status_generation=${encodeURIComponent(statusGeneration)}`;
}
