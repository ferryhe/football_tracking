import { describe, expect, it } from "vitest";
import type { ArtifactSummary, RunRecord } from "@workspace/api-client-react";

import {
  assessBroadcastDelivery,
  isProductionProductEvidence,
  PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS,
} from "./broadcastDelivery";
import { canonicalJson, sha256Text } from "./productionTrial";

const NOW = "2026-07-15T17:00:00.000Z";
const VIDEO_SHA = "a".repeat(64);

function artifacts(qualitySize: number): ArtifactSummary[] {
  return PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.map((name, index) => ({
    name,
    path: `sealed/${name}`,
    kind: name.endsWith(".mp4") ? "video" : "file",
    exists: true,
    size_bytes:
      name === "broadcast_quality_report.json"
        ? qualitySize
        : name === "broadcast.mp4"
          ? 1_234
          : index + 10,
  }));
}

async function fixture(changes: Record<string, unknown> = {}) {
  const stable = {
    schema_version: "1.0",
    artifact_type: "broadcast_quality_report",
    status: "ready",
    blocking_reasons: [],
    limitations: ["source_audio_not_preserved"],
    lineage: { sources: {} },
    artifacts: {
      "broadcast.mp4": {
        status: "available",
        path: "broadcast.mp4",
        sha256: VIDEO_SHA,
        size_bytes: 1_234,
      },
    },
    final_bindings: {},
    capabilities: {},
    ...changes,
  };
  const generation = await sha256Text(canonicalJson(stable));
  const report = {
    ...stable,
    generated_at: NOW,
    status_generation: generation,
  };
  const text = canonicalJson(report);
  const qualityBytes = new TextEncoder().encode(text);
  const run: Pick<RunRecord, "run_id" | "source" | "status" | "broadcast"> = {
    run_id: "full-parent",
    source: "broadcast_hybrid",
    status: "completed",
    broadcast: { status: "ready", status_generation: generation },
  };
  return { stable, generation, report, text, qualityBytes, run };
}

describe("broadcast delivery evidence", () => {
  it("verifies the sealed exact artifact set and bound quality report", async () => {
    const ready = await fixture();
    const result = await assessBroadcastDelivery({
      run: ready.run,
      artifacts: artifacts(ready.qualityBytes.byteLength),
      quality_report_bytes: ready.qualityBytes,
      verified_at: NOW,
    });
    expect(result.status).toBe("verified");
    if (result.status !== "verified") return;
    expect(result.evidence).toMatchObject({
      run_id: "full-parent",
      artifact_name: "broadcast.mp4",
      artifact_size_bytes: 1_234,
      artifact_sha256: VIDEO_SHA,
      status_generation: ready.generation,
      verified_at: NOW,
    });
    expect(result.evidence.quality_report_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(result.limitations).toEqual(["source_audio_not_preserved"]);
    expect(isProductionProductEvidence(result.evidence)).toBe(true);
  });

  it.each([
    ["non-broadcast parent", { source: "api" }],
    ["active parent", { status: "running" }],
    ["non-ready facade", { broadcast: { status: "needs_review" } }],
    ["missing generation", { broadcast: { status: "ready" } }],
  ])("blocks %s", async (_label, runChange) => {
    const ready = await fixture();
    const result = await assessBroadcastDelivery({
      run: { ...ready.run, ...runChange } as never,
      artifacts: artifacts(ready.qualityBytes.byteLength),
      quality_report_bytes: ready.qualityBytes,
      verified_at: NOW,
    });
    expect(result.status).toBe("blocked");
  });

  it("rejects missing, duplicate, extra, or unavailable artifacts", async () => {
    const ready = await fixture();
    const valid = artifacts(ready.qualityBytes.byteLength);
    for (const invalid of [
      valid.slice(1),
      [...valid.slice(0, -1), valid[0]],
      [...valid, { ...valid[0], name: "extra.txt" }],
      valid.map((item) =>
        item.name === "broadcast.mp4" ? { ...item, exists: false } : item,
      ),
    ]) {
      await expect(
        assessBroadcastDelivery({
          run: ready.run,
          artifacts: invalid,
          quality_report_bytes: ready.qualityBytes,
          verified_at: NOW,
        }),
      ).resolves.toMatchObject({
        status: "blocked",
        reasons: ["artifact_set_mismatch"],
      });
    }
  });

  it("rejects unreadable, malformed, and size-mismatched reports", async () => {
    const ready = await fixture();
    await expect(
      assessBroadcastDelivery({
        run: ready.run,
        artifacts: artifacts(999),
        quality_report_bytes: ready.qualityBytes,
        verified_at: NOW,
      }),
    ).resolves.toMatchObject({
      status: "blocked",
      reasons: ["quality_size_mismatch"],
    });
    const invalidUtf8 = new Uint8Array([0xc3, 0x28]);
    await expect(
      assessBroadcastDelivery({
        run: ready.run,
        artifacts: artifacts(invalidUtf8.byteLength),
        quality_report_bytes: invalidUtf8,
        verified_at: NOW,
      }),
    ).resolves.toMatchObject({
      status: "blocked",
      reasons: ["quality_not_utf8"],
    });
    const malformed = new TextEncoder().encode("{");
    await expect(
      assessBroadcastDelivery({
        run: ready.run,
        artifacts: artifacts(malformed.byteLength),
        quality_report_bytes: malformed,
        verified_at: NOW,
      }),
    ).resolves.toMatchObject({
      status: "blocked",
      reasons: ["quality_not_json"],
    });
  });

  it("rejects stale generations, non-recomputing status digests, blockers, and video mismatches", async () => {
    const ready = await fixture();
    const changedReports = [
      { ...ready.report, status_generation: "b".repeat(64) },
      { ...ready.report, limitations: ["changed-after-hash"] },
      { ...ready.report, blocking_reasons: ["missing_broadcast_render"] },
      {
        ...ready.report,
        artifacts: {
          ...ready.report.artifacts,
          "broadcast.mp4": {
            ...ready.report.artifacts["broadcast.mp4"],
            size_bytes: 99,
          },
        },
      },
    ];
    for (const report of changedReports) {
      const value = new TextEncoder().encode(canonicalJson(report));
      const result = await assessBroadcastDelivery({
        run: ready.run,
        artifacts: artifacts(value.byteLength),
        quality_report_bytes: value,
        verified_at: NOW,
      });
      expect(result.status).toBe("blocked");
    }
  });

  it("rejects structurally forged product evidence", () => {
    expect(
      isProductionProductEvidence({
        run_id: "full-parent",
        artifact_name: "broadcast.mp4",
        artifact_size_bytes: 1,
        artifact_sha256: VIDEO_SHA,
        quality_report_sha256: "b".repeat(64),
        status_generation: "c".repeat(64),
        verified_at: NOW,
      }),
    ).toBe(true);
    expect(
      isProductionProductEvidence({
        run_id: "full-parent",
        artifact_name: "broadcast.mp4",
        artifact_size_bytes: -1,
        artifact_sha256: VIDEO_SHA,
        quality_report_sha256: "b".repeat(64),
        status_generation: "c".repeat(64),
        verified_at: NOW,
      }),
    ).toBe(false);
  });
});
