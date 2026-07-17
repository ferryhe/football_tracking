import { describe, expect, it } from "vitest";

import type {
  ArtifactSummary,
  AssetGroup,
  ConfigDetail,
  RunRecord,
} from "@workspace/api-client-react";

import {
  buildProductionHistoryGroups,
  classifyProductionProduct,
  filterProductionHistoryGroups,
  isReadyProductCandidate,
  parseProductionHistoryNote,
  productionArtifactUrl,
  productionCurrentConfigVerificationKey,
  productionGroupProductCounts,
  productionHistoryCancellationTarget,
  productionHistoryDeletionBlocker,
  productionProductVerificationKey,
  verifyProductionCurrentConfig,
} from "./productionHistory";
import { sha256Text } from "./productionTrial";

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
      modified_at: "2026-07-14T09:00:00Z",
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
    configs: Array.from(
      new Map(
        runs
          .filter(
            (item) => item.config_name && item.config_path && item.input_video,
          )
          .map((item) => [
            item.config_name!,
            {
              name: item.config_name!,
              path: item.config_path!,
              created_at: "2026-07-14T09:30:00Z",
              input_video: item.input_video!,
              postprocess_enabled: true,
              follow_cam_enabled: false,
              exists: {},
            },
          ]),
      ).values(),
    ),
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
      configName: null,
      expectedConfigSha256: null,
      acceptedTrialRunId: null,
      acceptedTrialRequestSha256: null,
      configPatchSha256: null,
      calibrationDigest: HASH_A,
      sourceSignature: null,
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
      configName: "confirmed.yaml",
      expectedConfigSha256: HASH_B,
      acceptedTrialRunId: "trial-1",
      acceptedTrialRequestSha256: HASH_A,
      configPatchSha256: HASH_C,
      calibrationDigest: HASH_A,
      sourceSignature: {
        path: "C:/videos/match.mp4",
        size_bytes: 100,
        modified_at: "2026-07-14T09:00:00Z",
      },
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
    expect(projected.find((item) => item.isUnbound)?.timeline).toHaveLength(2);
  });

  it("keeps stale run and config paths in authoritative unbound groups", () => {
    const staleRun = run("stale-run", {
      input_video: "C:/stale/only-copy.mp4",
    });
    const staleConfig = {
      name: "stale.yaml",
      path: "config/stale.yaml",
      input_video: "C:/stale/only-copy.mp4",
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: {},
    };
    const projected = buildProductionHistoryGroups([
      group("server-unbound", "C:/server/claimed.mp4", [staleRun], {
        is_unbound: true,
        configs: [staleConfig],
      }),
      group(
        "null-source",
        null,
        [
          run("null-source-run", {
            input_video: "C:/stale/other.mp4",
          }),
        ],
        {
          configs: [{ ...staleConfig, name: "other.yaml" }],
        },
      ),
    ]);

    expect(projected).toHaveLength(1);
    expect(projected[0]).toMatchObject({
      key: "legacy:unbound",
      inputPath: null,
      isUnbound: true,
    });
    expect(projected[0].timeline.map((item) => item.run.run_id).sort()).toEqual(
      ["null-source-run", "stale-run"],
    );
    expect(projected[0].configs.map((config) => config.name).sort()).toEqual([
      "other.yaml",
      "stale.yaml",
    ]);
    expect(projected.some((item) => item.key.includes("stale"))).toBe(false);
  });

  it("uses the bound asset-group source instead of stale child paths", () => {
    const projected = buildProductionHistoryGroups([
      group(
        "bound",
        "C:/videos/canonical.mp4",
        [run("stale-child", { input_video: "C:/stale/child.mp4" })],
        {
          configs: [
            {
              name: "stale.yaml",
              path: "config/stale.yaml",
              input_video: "C:/stale/child.mp4",
              postprocess_enabled: true,
              follow_cam_enabled: false,
              exists: {},
            },
          ],
        },
      ),
    ]);

    expect(projected).toHaveLength(1);
    expect(projected[0].key).toBe("input:C:/videos/canonical.mp4");
    expect(projected[0].timeline[0].run.run_id).toBe("stale-child");
    expect(projected[0].configs[0].name).toBe("stale.yaml");
  });

  it("builds explicit trial/full/operation lineage without time guessing", () => {
    const trialId = "production_trial_trial-output";
    const fullId = "production_full_full-output";
    const trial = run(trialId, { notes: trialNote() });
    const unrelated = run("nearby", {
      created_at: "2026-07-14T10:02:00.001Z",
      notes: trialNote({ workflow_id: "other", output_id: "other" }),
    });
    const full = run(fullId, {
      source: "broadcast_hybrid",
      parent_run_id: trialId,
      config_name: "confirmed.yaml",
      config_path: "config/confirmed.yaml",
      notes: fullNote({ accepted_trial_run_id: trialId }),
      broadcast: { status: "ready", status_generation: HASH_A },
    });
    const render = run("render-1", {
      source: "broadcast_operation",
      parent_run_id: fullId,
      broadcast: {
        parent_run_id: fullId,
        operation: "render",
        operation_status: "completed",
      },
    });
    const recompute = run("recompute-1", {
      source: "broadcast_operation",
      parent_run_id: fullId,
      broadcast: {
        parent_run_id: fullId,
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

    expect(byId.get(trialId)?.kind).toBe("trial");
    expect(byId.get("nearby")?.parentRunId).toBeNull();
    expect(byId.get(fullId)?.parentRunId).toBe(trialId);
    expect(byId.get("render-1")).toMatchObject({
      kind: "render",
      parentRunId: fullId,
      lineageIssue: null,
    });
    expect(byId.get("recompute-1")?.kind).toBe("recompute");
    expect(projected.summary).toEqual({
      trialCount: 1,
      activeCount: 0,
      fullRunCount: 1,
      latestFullStatus: "ready",
      readyCandidateCount: 1,
      failedCount: 0,
      cancelledCount: 0,
    });
  });

  it("preserves server-bound full identity when the current config summary is missing", () => {
    const trialId = "production_trial_trial-output";
    const fullId = "production_full_full-output";
    const projected = buildProductionHistoryGroups([
      group(
        "match",
        "C:/videos/match.mp4",
        [
          run(trialId, { notes: trialNote() }),
          run(fullId, {
            source: "broadcast_hybrid",
            parent_run_id: trialId,
            config_name: "confirmed.yaml",
            config_path: "config/confirmed.yaml",
            notes: fullNote({ accepted_trial_run_id: trialId }),
          }),
        ],
        {
          configs: [
            {
              name: "default.yaml",
              path: "config/default.yaml",
              input_video: "C:/videos/match.mp4",
              postprocess_enabled: true,
              follow_cam_enabled: false,
              exists: {},
            },
          ],
        },
      ),
    ])[0];

    expect(
      projected.timeline.find((item) => item.run.run_id === fullId),
    ).toMatchObject({ kind: "full", lineageIssue: null });
    expect(projected.summary.fullRunCount).toBe(1);
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
      parentRunId: "parent",
      externalParentRunId: null,
      lineageIssue: "identity_mismatch",
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
    const firstTrialId = "production_trial_trial-1";
    const acceptedTrialId = "production_trial_trial-2";
    const laterTrialId = "production_trial_trial-3";
    const firstTrial = run(firstTrialId, {
      notes: trialNote({ output_id: "trial-1", generation: 1 }),
    });
    const acceptedTrial = run(acceptedTrialId, {
      parent_run_id: firstTrialId,
      notes: trialNote({ output_id: "trial-2", generation: 2 }),
    });
    const laterTrial = run(laterTrialId, {
      parent_run_id: acceptedTrialId,
      notes: trialNote({ output_id: "trial-3", generation: 3 }),
    });
    const products = [1, 2].map((generation) =>
      run(`production_full_full-${generation}`, {
        source: "broadcast_hybrid",
        parent_run_id: acceptedTrialId,
        config_name: "confirmed.yaml",
        config_path: "config/confirmed.yaml",
        notes: fullNote({
          output_id: `full-${generation}`,
          generation,
          accepted_trial_run_id: acceptedTrialId,
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
    ).toEqual([acceptedTrialId, acceptedTrialId]);
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

  it("binds machine notes to visible run, group, source, output, parent, and config identity", () => {
    const validTrialId = "production_trial_valid";
    const validTrial = run(validTrialId, {
      notes: trialNote({ output_id: "valid" }),
    });
    const invalid = [
      run("production_trial_free", { notes: "production trial, trust me" }),
      run("production_trial_malformed", { notes: "{" }),
      run("spoofed-id", { notes: trialNote({ output_id: "spoofed" }) }),
      run("production_trial_wrong-output", {
        output_dir: "C:/outputs/not-the-run-id",
        notes: trialNote({ output_id: "wrong-output" }),
      }),
      run("production_trial_wrong-source", {
        source: "scan",
        notes: trialNote({ output_id: "wrong-source" }),
      }),
      run("production_trial_wrong-input", {
        input_video: "C:/videos/other.mp4",
        notes: trialNote({ output_id: "wrong-input" }),
      }),
      run("production_trial_wrong-config", {
        config_name: "other.yaml",
        config_path: "config/other.yaml",
        notes: trialNote({ output_id: "wrong-config" }),
      }),
      run("production_full_wrong-parent", {
        source: "broadcast_hybrid",
        parent_run_id: "different-trial",
        config_name: "confirmed.yaml",
        config_path: "config/confirmed.yaml",
        notes: fullNote({
          output_id: "wrong-parent",
          accepted_trial_run_id: validTrialId,
        }),
      }),
      run("production_full_wrong-source", {
        source: "api",
        parent_run_id: validTrialId,
        config_name: "confirmed.yaml",
        config_path: "config/confirmed.yaml",
        notes: fullNote({
          output_id: "wrong-source",
          accepted_trial_run_id: validTrialId,
        }),
      }),
      run("production_full_wrong-source-size", {
        source: "broadcast_hybrid",
        parent_run_id: validTrialId,
        config_name: "confirmed.yaml",
        config_path: "config/confirmed.yaml",
        notes: fullNote({
          output_id: "wrong-source-size",
          accepted_trial_run_id: validTrialId,
          source_signature: {
            path: "C:/videos/match.mp4",
            size_bytes: 101,
            modified_at: "2026-07-14T09:00:00Z",
          },
        }),
      }),
      run("production_full_wrong-source-time", {
        source: "broadcast_hybrid",
        parent_run_id: validTrialId,
        config_name: "confirmed.yaml",
        config_path: "config/confirmed.yaml",
        notes: fullNote({
          output_id: "wrong-source-time",
          accepted_trial_run_id: validTrialId,
          source_signature: {
            path: "C:/videos/match.mp4",
            size_bytes: 100,
            modified_at: "2026-07-14T09:00:01Z",
          },
        }),
      }),
    ];
    const trustedConfig = {
      name: "default.yaml",
      path: "config/default.yaml",
      created_at: "2026-07-14T09:30:00Z",
      input_video: "C:/videos/match.mp4",
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: {},
    };
    const trustedFullConfig = {
      ...trustedConfig,
      name: "confirmed.yaml",
      path: "config/confirmed.yaml",
    };
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [validTrial, ...invalid], {
        configs: [trustedConfig, trustedFullConfig],
      }),
    ]);
    const items = projected.flatMap((item) => item.timeline);
    expect(items.find((item) => item.run.run_id === validTrialId)?.kind).toBe(
      "trial",
    );
    for (const item of items.filter(
      (entry) => entry.run.run_id !== validTrialId,
    )) {
      expect(item.note, item.run.run_id).toBeNull();
      expect(item.kind, item.run.run_id).toBe("legacy");
      expect(item.lineageIssue, item.run.run_id).toBe("identity_mismatch");
    }
  });

  it("globally disambiguates colliding hash suffixes deterministically", () => {
    const firstPath = "C:/video/svql0r61wzqd.mp4";
    const secondPath = "C:/video/wbelcve14ve9.mp4";
    const sources = [group("match", firstPath), group("match", secondPath)];
    const forward = buildProductionHistoryGroups(sources);
    const reverse = buildProductionHistoryGroups([...sources].reverse());
    const forwardByPath = new Map(
      forward.map((item) => [item.inputPath, item.groupId]),
    );
    const reverseByPath = new Map(
      reverse.map((item) => [item.inputPath, item.groupId]),
    );

    expect(new Set(forward.map((item) => item.groupId)).size).toBe(2);
    expect(forwardByPath).toEqual(reverseByPath);
    expect([...forwardByPath.values()].sort()).toEqual([
      "match--q3f3lv",
      "match--q3f3lv--1",
    ]);
  });
});

describe("current production configuration verification", () => {
  it("distinguishes current verified, modified, lineage-mismatched, and missing config", async () => {
    const text = "verified production config\n";
    const digest = await sha256Text(text);
    const acceptedTrialRunId = "production_trial_trial-output";
    const note = parseProductionHistoryNote(
      fullNote({
        expected_config_sha256: digest,
        accepted_trial_run_id: acceptedTrialRunId,
      }),
    )!;
    const fullRun = run("production_full_full-output", {
      source: "broadcast_hybrid",
      parent_run_id: acceptedTrialRunId,
      config_name: "confirmed.yaml",
      config_path: "config/confirmed.yaml",
      notes: fullNote({
        expected_config_sha256: digest,
        accepted_trial_run_id: acceptedTrialRunId,
      }),
    });
    const metadata = {
      schema_version: "1.0",
      workflow_id: note.workflowId,
      accepted_trial_run_id: acceptedTrialRunId,
      calibration_digest: note.calibrationDigest,
      source_signature: note.sourceSignature,
      trial_request_sha256: note.acceptedTrialRequestSha256,
      trial_intent_sha256: HASH_B,
      patch_sha256: note.configPatchSha256,
    };
    const detail: ConfigDetail = {
      name: "confirmed.yaml",
      path: "config/confirmed.yaml",
      text,
      raw: { metadata: { production_workflow: metadata } },
      resolved: {},
      summary: {
        name: "confirmed.yaml",
        path: "config/confirmed.yaml",
        input_video: "C:/videos/match.mp4",
        postprocess_enabled: true,
        follow_cam_enabled: false,
        exists: {},
      },
    };

    expect(
      productionCurrentConfigVerificationKey(note, detail.summary),
    ).toEqual([
      "production-history",
      "config",
      "confirmed.yaml",
      "config/confirmed.yaml",
      digest,
      "workflow-a",
      acceptedTrialRunId,
    ]);
    await expect(
      verifyProductionCurrentConfig(note, fullRun, detail),
    ).resolves.toEqual({ status: "verified_current", sha256: digest });
    await expect(
      verifyProductionCurrentConfig(note, fullRun, {
        ...detail,
        text: "locally modified\n",
      }),
    ).resolves.toMatchObject({ status: "modified" });
    await expect(
      verifyProductionCurrentConfig(note, fullRun, {
        ...detail,
        raw: {
          metadata: {
            production_workflow: {
              ...metadata,
              accepted_trial_run_id: "wrong-trial",
            },
          },
        },
      }),
    ).resolves.toEqual({ status: "lineage_mismatch" });
    await expect(
      verifyProductionCurrentConfig(note, fullRun, null),
    ).resolves.toEqual({ status: "missing" });
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

  it("derives product summary transitions only from generation-bound cache state", () => {
    const invalid = {
      ...ready,
      run_id: "invalid-generation",
      broadcast: { status: "ready", status_generation: "invalid" },
    };
    const projected = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [ready, invalid]),
    ])[0];
    expect(productionGroupProductCounts(projected, () => undefined)).toEqual({
      unverified: 1,
      verified: 0,
      unavailable: 1,
    });
    expect(
      productionGroupProductCounts(projected, () => ({
        status: "success",
        artifacts: [artifact("broadcast.mp4")],
      })),
    ).toEqual({ unverified: 0, verified: 1, unavailable: 1 });
    expect(
      productionGroupProductCounts(projected, () => ({
        status: "success",
        artifacts: [],
      })),
    ).toEqual({ unverified: 0, verified: 0, unavailable: 2 });
    expect(
      productionGroupProductCounts(projected, () => ({ status: "error" })),
    ).toEqual({ unverified: 0, verified: 0, unavailable: 2 });
    expect(
      productionGroupProductCounts(projected, () => ({ status: "pending" })),
    ).toEqual({ unverified: 1, verified: 0, unavailable: 1 });

    const changed = buildProductionHistoryGroups([
      group("match", "C:/videos/match.mp4", [
        {
          ...ready,
          broadcast: { status: "ready", status_generation: HASH_B },
        },
      ]),
    ])[0];
    expect(
      productionGroupProductCounts(changed, (key) =>
        key[3] === HASH_A
          ? { status: "success", artifacts: [artifact("broadcast.mp4")] }
          : undefined,
      ),
    ).toEqual({ unverified: 1, verified: 0, unavailable: 0 });
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

  it("safely falls back to an active review-evidence import child", () => {
    const parent = run("full", {
      source: "broadcast_hybrid",
      broadcast: { status: "needs_review" },
    });
    const child = run("review-import", {
      source: "broadcast_review_evidence_import",
      status: "running",
      completed_at: null,
      parent_run_id: "full",
      broadcast: {
        parent_run_id: "full",
        operation: "review_evidence_import",
        operation_status: "validating",
      },
    });
    expect(productionHistoryCancellationTarget(child, [parent, child])).toBe(
      "review-import",
    );
    expect(
      productionHistoryCancellationTarget({ ...child, status: "completed" }, [
        parent,
        child,
      ]),
    ).toBeNull();
    expect(
      productionHistoryCancellationTarget(
        {
          ...child,
          parent_run_id: "other",
        },
        [parent, child],
      ),
    ).toBeNull();
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
