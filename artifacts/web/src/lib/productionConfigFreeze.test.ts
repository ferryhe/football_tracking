import { describe, expect, it } from "vitest";
import type { ConfigDetail, RunRecord } from "@workspace/api-client-react";

import type { ProductionCalibrationDraft } from "./productionCalibration";
import {
  buildProductionConfigConfirmation,
  finalizeProductionConfigConfirmation,
  isProductionConfigEvidence,
  isProductionPendingConfigConfirmation,
  expectedProductionConfigName,
  verifyProductionConfigDetail,
} from "./productionConfigFreeze";
import {
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  createProductionTrialState,
  setPendingProductionTrial,
  type ProductionTrialState,
} from "./productionTrial";
import type { SourceSignature } from "./productionWorkflow";
import { ACCEPTABLE_TRIAL_SIGNAL_GATE } from "../test/productionTrialFixtures";

const NOW = "2026-07-15T13:00:00.000Z";
const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 12_345,
  modified_at: "2026-07-15T00:00:00Z",
};
const DIGEST = "c".repeat(64);
const CALIBRATION: ProductionCalibrationDraft = {
  source_resolution: { width: 1_920, height: 1_080 },
  suggestion: null,
  approved_polygon: [
    [100, 200],
    [1_800, 150],
    [1_700, 950],
    [200, 900],
  ],
  exclusions: [
    [
      [300, 300],
      [400, 300],
      [400, 400],
    ],
  ],
  polygon_digest: DIGEST,
  confirmed_frames: [10, 20, 30].map((frame_index, sample_index) => ({
    input_video: SOURCE.path,
    frame_index,
    frame_time_seconds: frame_index / 25,
    sample_index,
    source_resolution: { width: 1_920, height: 1_080 },
    polygon_digest: DIGEST,
  })),
};

async function acceptedTrial(): Promise<ProductionTrialState> {
  const settings = {
    base_config_name: "configs/base.yaml",
    start_frame: 25,
    max_frames: 240,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: {
      detector: { confidence: 0.2 },
      output: { dir: "temporary", video_name: "trial.mp4" },
      runtime: { start_frame: 999, max_frames: 1, trial_stride: 4 },
      follow_cam: {
        enabled: true,
        legacy_render: true,
        preview_codec: "trial-only",
      },
      metadata: { old: true },
    },
  };
  const submission = await buildProductionTrialSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    settings,
    parent_run_id: null,
    submission_id: "trial-submission",
    output_id: "trial-output",
    generation: 1,
    created_at: NOW,
  });
  const observed = appendProductionTrialAttempt(
    setPendingProductionTrial(
      createProductionTrialState(settings),
      submission.pending,
    ),
    {
      run: { run_id: "trial-1", status: "completed" },
      pending: submission.pending,
      observed_at: NOW,
    },
  );
  return {
    ...observed,
    attempts: observed.attempts.map((attempt) => ({
      ...attempt,
      last_observed: {
        ...attempt.last_observed,
        evidence_generation: "e".repeat(64),
      },
    })),
    accepted: {
      run_id: "trial-1",
      intent_sha256: submission.pending.intent_sha256,
      request_sha256: submission.pending.request_sha256,
      accepted_at: NOW,
      readiness: {
        run_id: "trial-1",
        request_sha256: submission.pending.request_sha256,
        evidence_generation: "e".repeat(64),
        verified_at: NOW,
        video_artifact_name: "follow_cam.mp4",
        artifact_names: [
          "run_manifest.json",
          "metrics_report.json",
          "ball_track.csv",
          "ball_audit.json",
          "ball_track.cleaned.csv",
          "follow_cam.mp4",
        ],
        quality: {
          frame_count: 240,
          detected: 150,
          predicted: 60,
          lost: 30,
          detected_ratio: 0.625,
          predicted_ratio: 0.25,
          lost_ratio: 0.125,
          longest_lost_streak: 5,
          false_positive_island_count: 2,
          max_step_px: 32.5,
          audit_tracklet_count: 4,
          audit_suspicious_tracklet_count: 1,
          audit_review_event_count: 2,
          audit_lost_gap_count: 3,
          quality_gate_status: "warn",
          trial_signal_gate_v2: ACCEPTABLE_TRIAL_SIGNAL_GATE,
        },
        operator_visual_confirmation: {
          confirmed: true,
          confirmed_at: NOW,
          evidence_generation: "e".repeat(64),
          threshold_profile_sha256:
            ACCEPTABLE_TRIAL_SIGNAL_GATE.threshold_profile.sha256,
        },
      },
    },
  };
}

function detailFor(
  pending: Awaited<ReturnType<typeof buildProductionConfigConfirmation>>,
  changes: Partial<ConfigDetail> = {},
): ConfigDetail {
  return {
    name: expectedProductionConfigName(pending.output_name),
    path: `configs/generated/${pending.output_name}`,
    text: "input_video: data/match-a.mp4\nmetadata:\n  created_at: server\n",
    raw: pending.request.patch ?? {},
    resolved: pending.request.patch ?? {},
    summary: {
      name: expectedProductionConfigName(pending.output_name),
      path: `configs/generated/${pending.output_name}`,
      input_video: SOURCE.path,
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: { yaml: true },
    },
    ...changes,
  };
}

function mergedBaseDetailFor(
  pending: Awaited<ReturnType<typeof buildProductionConfigConfirmation>>,
): ConfigDetail {
  const detail = detailFor(pending);
  return {
    ...detail,
    raw: {
      ...(detail.raw as Record<string, unknown>),
      runtime: {
        use_gpu_if_available: true,
        opencv_threads: 2,
        ...((detail.raw.runtime as Record<string, unknown>) ?? {}),
      },
      follow_cam: {
        prefer_cleaned_track: true,
        target_width: 1_920,
        output_video_name: "follow_cam.stable.mp4",
        ...((detail.raw.follow_cam as Record<string, unknown>) ?? {}),
      },
      output: {
        video_name: "annotated.mp4",
        save_csv: true,
        ...((detail.raw.output as Record<string, unknown>) ?? {}),
      },
    },
  };
}

describe("production configuration freeze patch", () => {
  it("rejects accepted state whose persisted readiness omits a required artifact", async () => {
    const trial = await acceptedTrial();
    trial.accepted!.readiness.artifact_names =
      trial.accepted!.readiness.artifact_names.filter(
        (name) => name !== "ball_track.cleaned.csv",
      );
    await expect(
      buildProductionConfigConfirmation({
        workflow_id: "workflow-a",
        source: SOURCE,
        calibration: CALIBRATION,
        trial,
        output_id: "11111111-1111-4111-8111-111111111111",
        generation: 1,
        confirmed_at: NOW,
      }),
    ).rejects.toThrow(/accepted|artifact/i);
  });

  it.each([
    ["zero frames", { ...CALIBRATION, confirmed_frames: [] }],
    [
      "two frames",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.slice(0, 2),
      },
    ],
    [
      "duplicate frame",
      {
        ...CALIBRATION,
        confirmed_frames: [
          CALIBRATION.confirmed_frames[0],
          { ...CALIBRATION.confirmed_frames[0] },
          CALIBRATION.confirmed_frames[2],
        ],
      },
    ],
    [
      "wrong source",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1 ? { ...frame, input_video: "data/other.mp4" } : frame,
        ),
      },
    ],
    [
      "wrong digest",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1 ? { ...frame, polygon_digest: "d".repeat(64) } : frame,
        ),
      },
    ],
    [
      "wrong resolution",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1
            ? { ...frame, source_resolution: { width: 1_280, height: 720 } }
            : frame,
        ),
      },
    ],
  ])(
    "refuses to freeze with incomplete calibration evidence: %s",
    async (_label, calibration) => {
      await expect(
        buildProductionConfigConfirmation({
          workflow_id: "workflow-a",
          source: SOURCE,
          calibration,
          trial: await acceptedTrial(),
          output_id: "config-invalid",
          generation: 1,
          confirmed_at: NOW,
        }),
      ).rejects.toThrow();
    },
  );

  it("builds an exact persistent patch, removes trial-only execution/output settings, and adds full lineage", async () => {
    const trial = await acceptedTrial();
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial,
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    expect(pending.output_name).toBe(
      "production_workflow-a_11111111-1111-4111-8111-111111111111.yaml",
    );
    expect(pending.persistent_patch).toEqual({
      detector: { confidence: 0.2 },
      metadata: { old: true },
      input_video: SOURCE.path,
      filtering: { roi: [100, 150, 1_800, 950] },
      scene_bias: {
        enabled: true,
        ground_zones: [
          { name: "production_field", points: CALIBRATION.approved_polygon },
        ],
        negative_rois: [
          {
            name: "production_exclusion_1",
            points: CALIBRATION.exclusions[0],
          },
        ],
      },
      postprocess: { enabled: true },
      runtime: { start_frame: 0, max_frames: null },
      follow_cam: { enabled: false },
      output: { save_tracking_contract: true },
    });
    expect(pending.persistent_patch.output).toEqual({
      save_tracking_contract: true,
    });
    const metadata = (
      pending.request.patch?.metadata as Record<string, unknown>
    ).production_workflow as Record<string, unknown>;
    expect(metadata).toMatchObject({
      schema_version: "1.0",
      workflow_id: "workflow-a",
      base_config_name: "configs/base.yaml",
      accepted_trial_run_id: "trial-1",
      calibration_digest: DIGEST,
      source_signature: SOURCE,
      trial_request_sha256: trial.accepted?.request_sha256,
      trial_intent_sha256: trial.accepted?.intent_sha256,
      patch_sha256: pending.patch_sha256,
      confirmed_at: NOW,
    });
    expect(pending.request).toEqual({
      base_config_name: "configs/base.yaml",
      output_name: pending.output_name,
      patch: {
        ...pending.persistent_patch,
        metadata: { old: true, production_workflow: metadata },
      },
    });
    expect(isProductionPendingConfigConfirmation(pending)).toBe(true);
  });

  it("always uses a new UUID-safe name and rejects unsafe identifiers", async () => {
    const trial = await acceptedTrial();
    const first = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial,
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    const second = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial,
      output_id: "22222222-2222-4222-8222-222222222222",
      generation: 2,
      confirmed_at: NOW,
    });
    expect(first.output_name).not.toBe(second.output_name);
    await expect(
      buildProductionConfigConfirmation({
        workflow_id: "workflow-a",
        source: SOURCE,
        calibration: CALIBRATION,
        trial,
        output_id: "../overwrite",
        generation: 3,
        confirmed_at: NOW,
      }),
    ).rejects.toThrow();
  });
});

describe("configuration finalization and verification", () => {
  it("requires the backend canonical generated name and allows only additive raw fields", async () => {
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: await acceptedTrial(),
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    expect(expectedProductionConfigName(pending.output_name)).toBe(
      `generated/${pending.output_name}`,
    );
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detailFor(pending),
        name: pending.output_name,
      }),
    ).rejects.toThrow(/name/i);
    const detail = detailFor(pending);
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detail,
        raw: {
          server_default: { retained: true },
          ...(detail.raw as Record<string, unknown>),
        },
      }),
    ).resolves.toMatchObject({
      name: expectedProductionConfigName(pending.output_name),
    });
    await expect(
      finalizeProductionConfigConfirmation(
        pending,
        mergedBaseDetailFor(pending),
      ),
    ).resolves.toMatchObject({
      name: expectedProductionConfigName(pending.output_name),
    });
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detail,
        raw: {
          ...(detail.raw as Record<string, unknown>),
          filtering: { roi: [0, 0, 1, 1] },
        },
      }),
    ).rejects.toThrow(/patch|lineage/i);
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detail,
        raw: {
          ...(detail.raw as Record<string, unknown>),
          follow_cam: { enabled: false, legacy_render: true },
        },
      }),
    ).rejects.toThrow(/lineage/i);
  });

  it("hashes returned UTF-8 text and verifies exact raw lineage before finalizing", async () => {
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: await acceptedTrial(),
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    const detail = mergedBaseDetailFor(pending);
    const confirmed = await finalizeProductionConfigConfirmation(
      pending,
      detail,
    );
    expect(confirmed.name).toBe(detail.name);
    expect(confirmed.sha256).toBe(
      "e759a6280020920a84e362dd71825b90b6144fb8cab3e89b1a09dd6d20881459",
    );
    expect(confirmed.patch_sha256).toBe(pending.patch_sha256);
    expect(confirmed.accepted_trial_run_id).toBe("trial-1");
    expect(isProductionConfigEvidence(confirmed)).toBe(true);
    expect(isProductionConfigEvidence({ ...confirmed, patch: {} })).toBe(false);
    expect(
      isProductionConfigEvidence({
        ...confirmed,
        patch: {
          ...confirmed.patch,
          output: { save_tracking_contract: false },
        },
      }),
    ).toBe(false);
    expect(
      isProductionConfigEvidence({
        ...confirmed,
        patch: {
          ...confirmed.patch,
          metadata: {
            production_workflow: {
              ...((confirmed.patch.metadata as Record<string, unknown>)
                .production_workflow as Record<string, unknown>),
              workflow_id: "workflow-other",
            },
          },
        },
      }),
    ).toBe(false);
    await expect(
      verifyProductionConfigDetail(confirmed, detail),
    ).resolves.toEqual({ status: "verified", sha256: confirmed.sha256 });
  });

  it("rejects a persisted pending request with an empty or divergent execution patch", async () => {
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: await acceptedTrial(),
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    expect(isProductionPendingConfigConfirmation(pending)).toBe(true);
    expect(
      isProductionPendingConfigConfirmation({
        ...pending,
        request: { ...pending.request, patch: {} },
      }),
    ).toBe(false);
    const patch = structuredClone(pending.request.patch) as Record<
      string,
      unknown
    >;
    const metadata = patch.metadata as Record<string, unknown>;
    (
      metadata.production_workflow as Record<string, unknown>
    ).calibration_digest = "9".repeat(64);
    expect(
      isProductionPendingConfigConfirmation({
        ...pending,
        request: { ...pending.request, patch },
      }),
    ).toBe(false);
  });

  it.each([
    ["runtime", { start_frame: 0, max_frames: null, trial_stride: 4 }],
    ["follow_cam", { enabled: false, legacy_render: true }],
    ["output", { save_tracking_contract: true, video_name: "trial.mp4" }],
  ])(
    "fails closed when persisted %s invariants contain extra keys",
    async (section, forcedObject) => {
      const pending = await buildProductionConfigConfirmation({
        workflow_id: "workflow-a",
        source: SOURCE,
        calibration: CALIBRATION,
        trial: await acceptedTrial(),
        output_id: "11111111-1111-4111-8111-111111111111",
        generation: 1,
        confirmed_at: NOW,
      });
      const pendingWithRequestExtra = structuredClone(pending);
      (pendingWithRequestExtra.request.patch as Record<string, unknown>)[
        section
      ] = forcedObject;
      expect(
        isProductionPendingConfigConfirmation(pendingWithRequestExtra),
      ).toBe(false);

      const pendingWithPersistentExtra = structuredClone(pending);
      pendingWithPersistentExtra.persistent_patch[section] = forcedObject;
      expect(
        isProductionPendingConfigConfirmation(pendingWithPersistentExtra),
      ).toBe(false);

      const confirmed = await finalizeProductionConfigConfirmation(
        pending,
        detailFor(pending),
      );
      const evidenceWithExtra = structuredClone(confirmed);
      evidenceWithExtra.patch[section] = forcedObject;
      expect(isProductionConfigEvidence(evidenceWithExtra)).toBe(false);
    },
  );

  it("classifies missing, name, digest, lineage, and unverifiable configuration states", async () => {
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: await acceptedTrial(),
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    const detail = detailFor(pending);
    const confirmed = await finalizeProductionConfigConfirmation(
      pending,
      detail,
    );
    await expect(
      verifyProductionConfigDetail(confirmed, null),
    ).resolves.toEqual({
      status: "missing",
    });
    await expect(
      verifyProductionConfigDetail(confirmed, {
        ...detail,
        name: "wrong.yaml",
      }),
    ).resolves.toEqual({ status: "name_mismatch" });
    await expect(
      verifyProductionConfigDetail(confirmed, {
        ...detail,
        text: `${detail.text}tampered`,
      }),
    ).resolves.toEqual({ status: "digest_mismatch" });
    await expect(
      verifyProductionConfigDetail(confirmed, {
        ...detail,
        raw: {
          ...(detail.raw as Record<string, unknown>),
          metadata: {
            production_workflow: {
              ...((detail.raw.metadata as Record<string, unknown>)
                .production_workflow as Record<string, unknown>),
              accepted_trial_run_id: "trial-other",
            },
          },
        },
      }),
    ).resolves.toEqual({ status: "lineage_mismatch" });
    await expect(
      verifyProductionConfigDetail(confirmed, {
        ...detail,
        raw: {
          ...(detail.raw as Record<string, unknown>),
          follow_cam: { enabled: false, legacy_render: true },
        },
      }),
    ).resolves.toEqual({ status: "lineage_mismatch" });
    await expect(
      verifyProductionConfigDetail(confirmed, {
        ...detail,
        raw: {
          ...(detail.raw as Record<string, unknown>),
          output: { save_tracking_contract: false },
        },
      }),
    ).resolves.toEqual({ status: "lineage_mismatch" });
    await expect(
      verifyProductionConfigDetail(confirmed, [] as unknown as ConfigDetail),
    ).resolves.toEqual({ status: "unverifiable" });
  });

  it("refuses to finalize a derive response whose name or lineage was changed", async () => {
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: await acceptedTrial(),
      output_id: "11111111-1111-4111-8111-111111111111",
      generation: 1,
      confirmed_at: NOW,
    });
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detailFor(pending),
        name: "wrong.yaml",
      }),
    ).rejects.toThrow();
    const detail = detailFor(pending);
    await expect(
      finalizeProductionConfigConfirmation(pending, {
        ...detail,
        raw: { ...detail.raw, metadata: {} },
      }),
    ).rejects.toThrow();
  });
});
