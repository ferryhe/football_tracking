import { describe, expect, it } from "vitest";

import type {
  ArtifactSummary,
  AssetGroup,
  RunRecord,
} from "@workspace/api-client-react";

import {
  buildProductionHistoryGroups,
  classifyProductionProduct,
  filterProductionHistoryGroups,
  isReadyProductCandidate,
  parseProductionHistoryNote,
  productionArtifactUrl,
  productionHistoryCancellationTarget,
  productionHistoryDeletionBlocker,
  productionProductVerificationKey,
} from "./productionHistory";

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);
const HASH_C = "c".repeat(64);

function trialNote(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_trial",
    workflow_id: "workflow-a",
    submission_id: "submission-trial",
    output_id: "trial-output",
    generation: 1,
    calibration_digest: HASH_A,
    intent_sha256: HASH_B,
    start_frame: 10,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: false,
    ...overrides,
  });
}

function fullNote(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_full",
    workflow_id: "workflow-a",
    submission_id: "submission-full",
    output_id: "full-output",
    generation: 1,
    accepted_trial_run_id: "trial-1",
    accepted_trial_request_sha256: HASH_A,
    confirmed_config_name: "confirmed.yaml",
    expected_config_sha256: HASH_B,
    config_patch_sha256: HASH_C,
    calibration_digest: HASH_A,
    source_signature: {
      path: "C:/videos/match.mp4",
      size_bytes: 100,
      modified_at: "2026-07-14T10:00:00Z",
    },
    ...overrides,
  });
}

function run(runId: string, overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: runId,
    source: "api",
    status: "completed",
    created_at: "2026-07-14T10:00:00Z",
    started_at: "2026-07-14T10:01:00Z",
    completed_at: "2026-07-14T10:02:00Z",
    config_name: "default.yaml",
    config_path: "config/default.yaml",
    input_video: "C:/videos/match.mp4",
    parent_run_id: null,
    output_dir: `C:/outputs/${runId}`,
    modules_enabled: {},
    artifacts: [],
    stats: {},
    broadcast: {},
    ai_candidate_lifecycle: {},
    progress: null,
    notes: null,
    error: null,
    ...overrides,
  };
}

function group(
  groupId: string,
  path: string | null,
  runs: RunRecord[] = [],
  overrides: Partial<AssetGroup> = {},
): AssetGroup {
  return {
    group_id: groupId,
    title: path?.split("/").at(-1) ?? "Unbound / Legacy",
    input_video: path
      ? {
          name: path.split("/").at(-1) ?? path,
          path,
          size_bytes: 100,
          modified_at: "2026-07-14T09:00:00Z",
        }
      : null,
    last_activity_at: "2026-07-14T09:00:00Z",
    run_count: runs.length,
    config_count: 0,
    output_count: runs.length,
    runs,
    configs: [],
    outputs: runs,
    is_unbound: !path,
    ...overrides,
  };
}

function artifact(name: string, exists = true): ArtifactSummary {
  return {
    name,
    path: `C:/outputs/${name}`,
    kind: name.endsWith(".mp4") ? "video" : "report",
    exists,
    size_bytes: exists ? 100 : null,
    content_type: name.endsWith(".mp4") ? "video/mp4" : "application/json",
  };
}

describe("parseProductionHistoryNote", () => {
  it("accepts only complete production trial notes", () => {
    expect(parseProductionHistoryNote(trialNote())).toEqual({
      purpose: "production_trial",
      workflowId: "workflow-a",
      generation: 1,
      configIdentity: null,
      acceptedTrialRunId: null,
    });
    for (const note of [
      null,
      "plain operator note",
      "[]",
      trialNote({ schema_version: "2.0" }),
      trialNote({ generation: 0 }),
      trialNote({ workflow_id: "" }),
      trialNote({ submission_id: null }),
      trialNote({ output_id: " " }),
      trialNote({ calibration_digest: "bad" }),
      trialNote({ intent_sha256: "bad" }),
      trialNote({ start_frame: -1 }),
      trialNote({ max_frames: false }),
      trialNote({ enable_postprocess: "yes" }),
      trialNote({ enable_follow_cam: null }),
      JSON.stringify({
        schema_version: "1.0",
        purpose: "other",
        generation: 1,
      }),
    ]) {
      expect(parseProductionHistoryNote(note)).toBeNull();
    }
  });

  it("accepts only complete production full notes", () => {
    expect(parseProductionHistoryNote(fullNote())).toEqual({
      purpose: "production_full",
      workflowId: "workflow-a",
      generation: 1,
      configIdentity: `confirmed.yaml@${HASH_B}`,
      acceptedTrialRunId: "trial-1",
    });
    for (const override of [
      { accepted_trial_run_id: "" },
      { confirmed_config_name: null },
      { accepted_trial_request_sha256: "bad" },
      { expected_config_sha256: "bad" },
      { config_patch_sha256: "bad" },
      { calibration_digest: "bad" },
      { source_signature: null },
      { source_signature: { path: "", size_bytes: 1, modified_at: "now" } },
      { source_signature: { path: "x", size_bytes: -1, modified_at: "now" } },
      { source_signature: { path: "x", size_bytes: 1, modified_at: "" } },
    ]) {
      expect(parseProductionHistoryNote(fullNote(override))).toBeNull();
    }
  });
});

describe("buildProductionHistoryGroups", () => {
  it("uses input path as identity and collision-checks group aliases", () => {
    const first = run("run-a", { input_video: "C:/one/match.mp4" });
    const second = run("run-b", { input_video: "C:/two/match.mp4" });
    const projected = buildProductionHistoryGroups([
      group("match", "C:/one/match.mp4", [first]),
      group("match", "C:/two/match.mp4", [second]),
    ]);

    expect(projected).toHaveLength(2);
    expect(new Set(projected.map((item) => item.key))).toEqual(
      new Set(["input:C:/one/match.mp4", "input:C:/two/match.mp4"]),
    );
    expect(new Set(projected.map((item) => item.groupId)).size).toBe(2);
    expect(projected.every((item) => item.groupId.startsWith("match--"))).toBe(
      true,
    );
  });

  it("assigns duplicate and conflicting rows exactly once", () => {
    const shared = run("shared", { input_video: "C:/one/match.mp4" });
    const conflict = run("conflict", { input_video: null });
    const projected = buildProductionHistoryGroups([
      group("one", "C:/one/match.mp4", [shared, conflict]),
      group("two", "C:/two/match.mp4", [shared, conflict]),
      group("empty", null, []),
    ]);
    const allIds = projected.flatMap((item) =>
      item.timeline.map((entry) => entry.run.run_id),
    );
    expect(allIds.filter((id) => id === "shared")).toHaveLength(1);
    expect(allIds.filter((id) => id === "conflict")).toHaveLength(1);
    expect(projected.find((item) => item.isUnbound)?.timeline).toHaveLength(1);
  });

  it("builds explicit trial/full/operation lineage without time guessing", () => {
    const trial = run("trial-1", { notes: trialNote() });
    const unrelated = run("nearby", {
      created_at: "2026-07-14T10:02:00.001Z",
      notes: trialNote({ workflow_id: "other", output_id: "other" }),
    });
    const full = run("full-1", {
      source: "broadcast_hybrid",
      parent_run_id: "trial-1",
      notes: fullNote(),
      broadcast: { status: "ready", status_generation: HASH_A },
    });
    const render = run("render-1", {
      source: "broadcast_operation",
      parent_run_id: "full-1",
      broadcast: {
        parent_run_id: "full-1",
        operation: "render",
        operation_status: "completed",
      },
    });
    const recompute = run("recompute-1", {
      source: "broadcast_operation",
      parent_run_id: "full-1",
      broadcast: {
        parent_run_id: "full-1",
        operation: "recompute",
        operation_status: "completed",
      },
    });
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [
        trial,
        unrelated,
        full,
        render,
        recompute,
      ]),
    ])[0];
    const byId = new Map(
      projected.timeline.map((item) => [item.run.run_id, item]),
    );

    expect(byId.get("trial-1")?.kind).toBe("trial");
    expect(byId.get("nearby")?.parentRunId).toBeNull();
    expect(byId.get("full-1")?.parentRunId).toBe("trial-1");
    expect(byId.get("render-1")).toMatchObject({
      kind: "render",
      parentRunId: "full-1",
      lineageIssue: null,
    });
    expect(byId.get("recompute-1")?.kind).toBe("recompute");
    expect(projected.summary).toEqual({
      trialCount: 2,
      activeCount: 0,
      fullRunCount: 1,
      latestFullStatus: "ready",
      readyCandidateCount: 1,
      failedCount: 0,
      cancelledCount: 0,
    });
  });

  it("fails closed on malformed, conflicting, and missing lineage", () => {
    const parent = run("parent");
    const malformed = run("malformed", {
      notes: fullNote({ expected_config_sha256: "bad" }),
    });
    const conflicting = run("conflicting", {
      parent_run_id: "parent",
      notes: fullNote({ accepted_trial_run_id: "different" }),
    });
    const missing = run("missing", { parent_run_id: "not-returned" });
    const failed = run("failed", { status: "failed" });
    const cancelled = run("cancelled", { status: "cancelled" });
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [
        parent,
        malformed,
        conflicting,
        missing,
        failed,
        cancelled,
      ]),
    ])[0];
    const byId = new Map(
      projected.timeline.map((item) => [item.run.run_id, item]),
    );

    expect(byId.get("malformed")).toMatchObject({
      kind: "legacy",
      parentRunId: null,
    });
    expect(byId.get("conflicting")).toMatchObject({
      parentRunId: null,
      externalParentRunId: null,
      lineageIssue: "ambiguous_parent",
    });
    expect(byId.get("missing")).toMatchObject({
      parentRunId: null,
      externalParentRunId: "not-returned",
      lineageIssue: "missing_parent",
    });
    expect(byId.get("failed")?.kind).toBe("failed");
    expect(byId.get("cancelled")?.kind).toBe("cancelled");
    expect(projected.summary.failedCount).toBe(1);
    expect(projected.summary.cancelledCount).toBe(1);
  });

  it("preserves an accepted middle trial with multiple full product versions", () => {
    const firstTrial = run("trial-1", {
      notes: trialNote({ output_id: "trial-1", generation: 1 }),
    });
    const acceptedTrial = run("trial-2", {
      parent_run_id: "trial-1",
      notes: trialNote({ output_id: "trial-2", generation: 2 }),
    });
    const laterTrial = run("trial-3", {
      parent_run_id: "trial-2",
      notes: trialNote({ output_id: "trial-3", generation: 3 }),
    });
    const products = [1, 2].map((generation) =>
      run(`full-${generation}`, {
        source: "broadcast_hybrid",
        parent_run_id: "trial-2",
        notes: fullNote({
          output_id: `full-${generation}`,
          generation,
          accepted_trial_run_id: "trial-2",
        }),
        broadcast: { status: "ready", status_generation: HASH_A },
      }),
    );
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [
        firstTrial,
        acceptedTrial,
        laterTrial,
        ...products,
      ]),
    ])[0];

    expect(
      projected.timeline
        .filter((item) => item.kind === "full")
        .map((item) => item.parentRunId),
    ).toEqual(["trial-2", "trial-2"]);
    expect(projected.summary).toMatchObject({
      trialCount: 3,
      fullRunCount: 2,
      readyCandidateCount: 2,
    });
  });

  it("keeps config-only groups, sorts display activity, and handles 1,000 runs", () => {
    const runs = Array.from({ length: 1_000 }, (_, index) =>
      run(`run-${index}`, {
        created_at: new Date(Date.UTC(2026, 6, 14, 10, 0, index)).toISOString(),
        started_at: null,
        completed_at: null,
      }),
    );
    const configOnly = group("config", "C:/videos/config.mp4", [], {
      configs: [
        {
          name: "z.yaml",
          path: "config/z.yaml",
          input_video: "C:/videos/config.mp4",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: {},
        },
        {
          name: "a.yaml",
          path: "config/a.yaml",
          input_video: "C:/videos/config.mp4",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: {},
        },
      ],
    });
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", runs),
      configOnly,
    ]);
    const runGroup = projected.find((item) => item.groupId === "match")!;
    expect(runGroup.timeline).toHaveLength(1_000);
    expect(new Set(runGroup.timeline.map((item) => item.run.run_id)).size).toBe(
      1_000,
    );
    expect(runGroup.timeline[0].run.run_id).toBe("run-999");
    expect(
      projected
        .find((item) => item.groupId === "config")
        ?.configs.map((item) => item.name),
    ).toEqual(["a.yaml", "z.yaml"]);
  });
});

describe("history filtering and product classification", () => {
  const active = run("active", { status: "running" });
  const ready = run("ready", {
    source: "broadcast_hybrid",
    broadcast: { status: "ready", status_generation: HASH_A },
  });
  const failed = run("failed", { status: "failed" });
  const cancelled = run("cancelled", { status: "cancelled" });
  const groups = buildProductionHistoryGroups([
    group("match", "C:/videos/match.mp4", [active, ready, failed, cancelled], {
      configs: [
        {
          name: "special.yaml",
          path: "config/special.yaml",
          input_video: "C:/videos/match.mp4",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: {},
        },
      ],
    }),
  ]);

  it("preserves search and all status filters", () => {
    expect(filterProductionHistoryGroups(groups, "", "all")).toHaveLength(1);
    expect(
      filterProductionHistoryGroups(groups, "MATCH", "active"),
    ).toHaveLength(1);
    expect(
      filterProductionHistoryGroups(groups, "special", "ready"),
    ).toHaveLength(1);
    expect(
      filterProductionHistoryGroups(groups, "failed", "failed"),
    ).toHaveLength(1);
    expect(
      filterProductionHistoryGroups(groups, "cancelled", "cancelled"),
    ).toHaveLength(1);
    expect(filterProductionHistoryGroups(groups, "missing", "all")).toEqual([]);
  });

  it("never promotes a candidate without generation-bound artifact verification", () => {
    expect(isReadyProductCandidate(ready)).toBe(true);
    expect(classifyProductionProduct(active)).toEqual({
      status: "unavailable",
      reason: "not_ready",
    });
    expect(
      classifyProductionProduct(
        { ...ready, broadcast: { status: "ready" } },
        [],
      ),
    ).toEqual({ status: "unavailable", reason: "missing_generation" });
    expect(classifyProductionProduct(ready)).toEqual({
      status: "candidate",
      reason: "not_verified",
    });
    expect(
      classifyProductionProduct(ready, [artifact("broadcast.mp4", false)]),
    ).toEqual({
      status: "unavailable",
      reason: "missing_broadcast",
    });
  });

  it("classifies verified products and exposes sorted downloads", () => {
    const artifacts = [
      artifact("tracking_contract.v2.json"),
      artifact("broadcast.mp4"),
      artifact("broadcast_quality_report.json"),
      artifact("missing.json", false),
    ];
    const classified = classifyProductionProduct(ready, artifacts);
    expect(classified.status).toBe("verified");
    if (classified.status !== "verified") throw new Error("expected product");
    expect(classified.video.name).toBe("broadcast.mp4");
    expect(classified.quality?.name).toBe("broadcast_quality_report.json");
    expect(classified.downloads.map((item) => item.name)).toEqual([
      "broadcast_quality_report.json",
      "broadcast.mp4",
      "tracking_contract.v2.json",
    ]);
    expect(
      classifyProductionProduct(ready, [artifact("broadcast.mp4")]),
    ).toMatchObject({ status: "verified", quality: null });
  });

  it("binds verification and artifact URLs to run plus status generation", () => {
    expect(productionProductVerificationKey(ready)).toEqual([
      "production-history",
      "product",
      "ready",
      HASH_A,
    ]);
    expect(productionProductVerificationKey(active)).toBeNull();
    expect(
      productionProductVerificationKey({
        ...ready,
        broadcast: { status: "ready", status_generation: "invalid" },
      }),
    ).toBeNull();
    expect(
      productionArtifactUrl("run one", "nested/report #1.json", HASH_A),
    ).toBe(
      `/api/runs/run%20one/artifacts/nested/report%20%231.json?status_generation=${HASH_A}`,
    );
  });
});

describe("safe history actions", () => {
  it("uses the active broadcast child selected by workflow recovery", () => {
    const parent = run("full", {
      source: "broadcast_hybrid",
      broadcast: {
        status: "trajectory_ready",
        last_operation: {
          operation_run_id: "render",
          operation: "render",
          status: "running",
        },
      },
    });
    const child = run("render", {
      source: "broadcast_operation",
      status: "running",
      parent_run_id: "full",
      broadcast: {
        parent_run_id: "full",
        operation: "render",
        operation_status: "running",
      },
    });
    expect(productionHistoryCancellationTarget(parent, [parent, child])).toBe(
      "render",
    );
    expect(productionHistoryCancellationTarget(child, [parent, child])).toBe(
      "render",
    );
  });

  it("cancels ordinary active runs and rejects terminal runs", () => {
    expect(
      productionHistoryCancellationTarget(
        run("trial", { status: "queued" }),
        [],
      ),
    ).toBe("trial");
    expect(productionHistoryCancellationTarget(run("done"), [])).toBeNull();
  });

  it("requires child-first deletion and blocks active work", () => {
    const parent = run("parent");
    const child = run("child", { parent_run_id: "parent" });
    const active = run("active", { status: "running" });
    const committing = run("committing", {
      broadcast: { operation_status: "committing" },
    });
    expect(productionHistoryDeletionBlocker(parent, [parent, child])).toBe(
      "children:1",
    );
    expect(productionHistoryDeletionBlocker(child, [parent, child])).toBeNull();
    expect(productionHistoryDeletionBlocker(active, [active])).toBe(
      "active_run",
    );
    expect(productionHistoryDeletionBlocker(committing, [committing])).toBe(
      "active_run",
    );
  });
});
