import type {
  ArtifactSummary,
  AssetGroup,
  ConfigDetail,
  ConfigListItem,
  InputVideoItem,
  RunRecord,
} from "@workspace/api-client-react";

import {
  broadcastCancellationTarget,
  recoverBroadcastWorkflowRun,
} from "./broadcastWorkflow";
import { sha256Text } from "./productionTrial";

const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);
const ACTIVE_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "reconciling",
  "committing",
  "copying",
  "validating",
]);
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);
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
  configName: string | null;
  expectedConfigSha256: string | null;
  acceptedTrialRunId: string | null;
  acceptedTrialRequestSha256: string | null;
  configPatchSha256: string | null;
  calibrationDigest: string;
  sourceSignature: {
    path: string;
    size_bytes: number;
    modified_at: string;
  } | null;
}

export type ProductionCurrentConfigVerification =
  | { status: "not_reverified"; reason: "summary_only" }
  | { status: "verified_current"; sha256: string }
  | { status: "modified"; actualSha256: string }
  | { status: "lineage_mismatch" }
  | { status: "missing" }
  | { status: "error"; message: string };

export interface ProductionHistoryTimelineItem {
  run: RunRecord;
  kind: ProductionHistoryKind;
  parentRunId: string | null;
  externalParentRunId: string | null;
  lineageIssue:
    | "ambiguous_parent"
    | "missing_parent"
    | "identity_mismatch"
    | null;
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
  inputVideo: InputVideoItem | null;
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

export interface ProductionProductCacheSnapshot {
  status: "pending" | "error" | "success";
  artifacts?: readonly ArtifactSummary[];
}

export interface ProductionProductCounts {
  unverified: number;
  verified: number;
  unavailable: number;
}

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

/** Parse the machine-note schema only; callers must bind it to visible server context. */
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
      configName: null,
      expectedConfigSha256: null,
      acceptedTrialRunId: null,
      acceptedTrialRequestSha256: null,
      configPatchSha256: null,
      calibrationDigest: String(parsed.calibration_digest),
      sourceSignature: null,
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
      configName,
      expectedConfigSha256: String(parsed.expected_config_sha256),
      acceptedTrialRunId,
      acceptedTrialRequestSha256: String(parsed.accepted_trial_request_sha256),
      configPatchSha256: String(parsed.config_patch_sha256),
      calibrationDigest: String(parsed.calibration_digest),
      sourceSignature: {
        path: String(parsed.source_signature.path),
        size_bytes: Number(parsed.source_signature.size_bytes),
        modified_at: String(parsed.source_signature.modified_at),
      },
    };
  }
  return null;
}

function normalizedPath(value: string | null | undefined): string | null {
  const text = nonEmptyText(value);
  return text ? text.replaceAll("\\", "/").replace(/\/+$/, "") : null;
}

function pathBasename(value: string | null | undefined): string | null {
  return normalizedPath(value)?.split("/").at(-1) ?? null;
}

function configMatchesRun(
  run: RunRecord,
  groupPath: string,
  configs: readonly ConfigListItem[],
): boolean {
  const configName = nonEmptyText(run.config_name);
  const configPath = normalizedPath(run.config_path);
  if (!configName || !configPath) return false;
  return configs.some(
    (config) =>
      config.name === configName &&
      normalizedPath(config.path) === configPath &&
      normalizedPath(config.input_video) === groupPath,
  );
}

function claimsProductionIdentity(run: RunRecord): boolean {
  return (
    run.run_id.startsWith("production_trial_") ||
    run.run_id.startsWith("production_full_") ||
    run.source === "broadcast_hybrid"
  );
}

/**
 * Full-run creation is server-preflighted against metadata.production_workflow.
 * Trial notes have no equivalent public proof, so both note kinds remain
 * untrusted until every visible run/group/config identity below agrees.
 */
function bindProductionHistoryNote(
  run: RunRecord,
  groupPath: string | null,
  inputVideo: InputVideoItem | null,
  configs: readonly ConfigListItem[],
): { note: ProductionHistoryNote | null; identityMismatch: boolean } {
  const note = parseProductionHistoryNote(run.notes);
  if (!note) {
    return { note: null, identityMismatch: claimsProductionIdentity(run) };
  }
  const inputPath = normalizedPath(run.input_video);
  const canonicalGroupPath = normalizedPath(groupPath);
  const parsed = JSON.parse(run.notes!) as Record<string, unknown>;
  const outputId = nonEmptyText(parsed.output_id)!;
  const expectedRunId = `${
    note.purpose === "production_trial"
      ? "production_trial_"
      : "production_full_"
  }${outputId}`;
  const commonMatches =
    run.run_id === expectedRunId &&
    pathBasename(run.output_dir) === expectedRunId &&
    inputPath !== null &&
    inputPath === canonicalGroupPath;
  if (!commonMatches) return { note: null, identityMismatch: true };

  if (note.purpose === "production_trial") {
    return run.source === "api" && configMatchesRun(run, inputPath, configs)
      ? { note, identityMismatch: false }
      : { note: null, identityMismatch: true };
  }

  const sourceSignature = parsed.source_signature as Record<string, unknown>;
  const sourceMatches =
    inputVideo !== null &&
    normalizedPath(inputVideo.path) === inputPath &&
    Number(sourceSignature.size_bytes) === inputVideo.size_bytes &&
    nonEmptyText(sourceSignature.modified_at) === inputVideo.modified_at;
  const fullMatches =
    run.source === "broadcast_hybrid" &&
    nonEmptyText(run.parent_run_id) === note.acceptedTrialRunId &&
    normalizedPath(String(sourceSignature.path)) === inputPath &&
    sourceMatches &&
    nonEmptyText(run.config_name) === note.configName &&
    pathBasename(run.config_path) === pathBasename(note.configName);
  return fullMatches
    ? { note, identityMismatch: false }
    : { note: null, identityMismatch: true };
}

export function productionCurrentConfigVerificationKey(
  note: ProductionHistoryNote | null,
  run: RunRecord,
): readonly [string, string, string, Record<string, unknown>] | null {
  if (
    note?.purpose !== "production_full" ||
    !note.configName ||
    !note.expectedConfigSha256 ||
    !note.acceptedTrialRunId ||
    !note.acceptedTrialRequestSha256 ||
    !note.configPatchSha256 ||
    !note.sourceSignature ||
    !run.config_path
  ) {
    return null;
  }
  return [
    "production-history",
    "config",
    note.configName,
    {
      run_id: run.run_id,
      config_path: run.config_path,
      expected_config_sha256: note.expectedConfigSha256,
      workflow_id: note.workflowId,
      accepted_trial_run_id: note.acceptedTrialRunId,
      accepted_trial_request_sha256: note.acceptedTrialRequestSha256,
      config_patch_sha256: note.configPatchSha256,
      calibration_digest: note.calibrationDigest,
      source_signature: { ...note.sourceSignature },
    },
  ];
}

export async function verifyProductionCurrentConfig(
  note: ProductionHistoryNote,
  run: RunRecord,
  detail: ConfigDetail | null,
): Promise<ProductionCurrentConfigVerification> {
  if (!detail) return { status: "missing" };
  if (
    note.purpose !== "production_full" ||
    !note.configName ||
    !note.expectedConfigSha256 ||
    !note.acceptedTrialRunId ||
    !note.acceptedTrialRequestSha256 ||
    !note.configPatchSha256 ||
    !note.sourceSignature
  ) {
    return { status: "lineage_mismatch" };
  }
  const actualSha256 = await sha256Text(detail.text);
  if (actualSha256 !== note.expectedConfigSha256) {
    return { status: "modified", actualSha256 };
  }
  const metadataRoot = isRecord(detail.raw.metadata)
    ? detail.raw.metadata
    : null;
  const metadata =
    metadataRoot && isRecord(metadataRoot.production_workflow)
      ? metadataRoot.production_workflow
      : null;
  const signature =
    metadata && isRecord(metadata.source_signature)
      ? metadata.source_signature
      : null;
  const identityMatches =
    detail.name === note.configName &&
    normalizedPath(detail.path) === normalizedPath(run.config_path) &&
    normalizedPath(detail.summary.input_video) ===
      normalizedPath(note.sourceSignature.path) &&
    metadata?.schema_version === "1.0" &&
    metadata.workflow_id === note.workflowId &&
    metadata.accepted_trial_run_id === note.acceptedTrialRunId &&
    metadata.calibration_digest === note.calibrationDigest &&
    metadata.trial_request_sha256 === note.acceptedTrialRequestSha256 &&
    metadata.patch_sha256 === note.configPatchSha256 &&
    sha256(metadata.trial_intent_sha256) &&
    normalizedPath(String(signature?.path)) ===
      normalizedPath(note.sourceSignature.path) &&
    Number(signature?.size_bytes) === note.sourceSignature.size_bytes &&
    nonEmptyText(signature?.modified_at) === note.sourceSignature.modified_at;
  return identityMatches
    ? { status: "verified_current", sha256: actualSha256 }
    : { status: "lineage_mismatch" };
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
  if (note?.purpose === "production_full") return "full";
  if (note?.purpose === "production_trial") return "trial";
  return "legacy";
}

function explicitLineageParent(
  run: RunRecord,
  note: ProductionHistoryNote | null,
): {
  parentRunId: string | null;
  ambiguous: boolean;
} {
  const candidates = new Set<string>();
  const direct = nonEmptyText(run.parent_run_id);
  const broadcastParent = nonEmptyText(run.broadcast?.parent_run_id);
  if (direct) candidates.add(direct);
  if (broadcastParent) candidates.add(broadcastParent);
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
  groupPath: string | null,
  inputVideo: InputVideoItem | null,
  configs: readonly ConfigListItem[],
): ProductionHistoryTimelineItem[] {
  const ids = new Set(runs.map((run) => run.run_id));
  return runs
    .map((run) => {
      const operation = run.broadcast?.operation;
      const binding =
        operation === "recompute" ||
        operation === "render" ||
        operation === "review_evidence_import"
          ? { note: null, identityMismatch: false }
          : bindProductionHistoryNote(run, groupPath, inputVideo, configs);
      const note = binding.note;
      const lineage = explicitLineageParent(run, note);
      const parentExists = lineage.parentRunId
        ? ids.has(lineage.parentRunId)
        : false;
      return {
        run,
        kind: historyKind(run, note),
        parentRunId: parentExists ? lineage.parentRunId : null,
        externalParentRunId:
          lineage.parentRunId && !parentExists ? lineage.parentRunId : null,
        lineageIssue: binding.identityMismatch
          ? "identity_mismatch"
          : lineage.ambiguous
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
    (item) => item.note?.purpose === "production_full",
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
  inputVideo: InputVideoItem | null;
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
    { run: RunRecord; paths: Set<string>; hasUnboundSource: boolean }
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
      inputVideo:
        source?.input_video &&
        normalizedPath(source.input_video.path) === normalizedPath(path)
          ? source.input_video
          : null,
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
    const sourcePath =
      source.is_unbound === true || source.input_video === null
        ? null
        : nonEmptyText(source.input_video?.path);
    ensureGroup(sourcePath, source);
    for (const config of source.configs ?? []) {
      ensureGroup(sourcePath, source).configs.push(config);
    }
    for (const run of source.runs ?? []) {
      const existing = runCandidates.get(run.run_id);
      if (existing) {
        if (sourcePath) existing.paths.add(sourcePath);
        else existing.hasUnboundSource = true;
      } else {
        runCandidates.set(run.run_id, {
          run,
          paths: new Set(sourcePath ? [sourcePath] : []),
          hasUnboundSource: sourcePath === null,
        });
      }
    }
  }

  for (const { run, paths, hasUnboundSource } of runCandidates.values()) {
    const canonicalPath =
      !hasUnboundSource && paths.size === 1 ? [...paths][0] : null;
    ensureGroup(canonicalPath).runs.push(run);
  }

  const aliasCounts = new Map<string, number>();
  for (const group of groupByKey.values()) {
    aliasCounts.set(
      group.candidateAlias,
      (aliasCounts.get(group.candidateAlias) ?? 0) + 1,
    );
  }

  const prepared = [...groupByKey.values()]
    .filter(
      (group) => !group.isUnbound || group.runs.length || group.configs.length,
    )
    .map((group) => {
      const timeline = buildTimeline(
        group.runs,
        group.path,
        group.inputVideo,
        group.configs,
      );
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
        inputVideo: group.inputVideo,
        lastActivityAt,
        isUnbound: group.isUnbound,
        configs: [...group.configs].sort((left, right) =>
          left.name.localeCompare(right.name),
        ),
        timeline,
        summary: summarize(timeline),
      } satisfies ProductionHistoryGroup;
    });

  const usedAliases = new Set<string>();
  const unique = [...prepared]
    .sort((left, right) => left.key.localeCompare(right.key))
    .map((group) => {
      const base = group.groupId;
      let groupId = base;
      let suffix = 1;
      while (usedAliases.has(groupId)) {
        groupId = `${base}--${suffix}`;
        suffix += 1;
      }
      usedAliases.add(groupId);
      return { ...group, groupId };
    });

  return unique.sort((left, right) => {
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

export function productionGroupProductCounts(
  group: ProductionHistoryGroup,
  lookup: (
    key: readonly [string, string, string, string],
  ) => ProductionProductCacheSnapshot | undefined,
): ProductionProductCounts {
  const counts: ProductionProductCounts = {
    unverified: 0,
    verified: 0,
    unavailable: 0,
  };
  for (const item of group.timeline) {
    if (!isReadyProductCandidate(item.run)) continue;
    const key = productionProductVerificationKey(item.run);
    if (!key) {
      counts.unavailable += 1;
      continue;
    }
    const cached = lookup(key);
    if (!cached || cached.status === "pending") {
      counts.unverified += 1;
      continue;
    }
    if (cached.status === "error") {
      counts.unavailable += 1;
      continue;
    }
    const classification = classifyProductionProduct(
      item.run,
      cached.artifacts ?? [],
    );
    if (classification.status === "verified") counts.verified += 1;
    else counts.unavailable += 1;
  }
  return counts;
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
    const target = broadcastCancellationTarget(
      recoverBroadcastWorkflowRun(run, groupRuns),
    );
    if (target) return target;
    const lineage = explicitLineageParent(run, null);
    if (
      run.source === "broadcast_review_evidence_import" &&
      run.broadcast?.operation === "review_evidence_import" &&
      !lineage.ambiguous &&
      !TERMINAL_RUN_STATUSES.has(run.status) &&
      (ACTIVE_RUN_STATUSES.has(run.status) ||
        ACTIVE_OPERATION_STATUSES.has(run.broadcast.operation_status ?? ""))
    ) {
      return run.run_id;
    }
    return null;
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
    const lineage = explicitLineageParent(candidate, null);
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
