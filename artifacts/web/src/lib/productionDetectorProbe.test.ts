import { describe, expect, it } from "vitest";

import {
  buildDetectorProbeRequest,
  detectorProbeCatalogView,
  detectorProbeCreateEnvelope,
  detectorProbeJobId,
  detectorProbeJobView,
  detectorProbeRecoveryEligible,
  detectorProbeStorageKey,
} from "./productionDetectorProbe";
import {
  detectorProbeCatalogFixture,
  detectorProbeDescriptorFixture,
  detectorProbeImportedCatalogFixture,
  detectorProbeJobFixture,
} from "@/test/detectorProbeFixtures";

const sha = (character: string) => character.repeat(64);

function legacyStrictCatalogFixture() {
  return {
    schema_version: "1.0",
    artifact_type: "ball_detector_development_v1",
    models: [
      {
        descriptor: {
          model_id: "official-coco-yolo11n",
          version: "yolo11n-coco-v8.4.0",
          display_name: "Official YOLO11n",
          architecture_family: "yolo11",
          descriptor_sha256: sha("b"),
          weights: { sha256: sha("c") },
          source: {
            project: "Ultralytics assets",
            version: "v8.4.0",
            asset_release: "v8.4.0",
            weight_url:
              "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt",
            acquisition_method: "pinned local download",
            access_requirement: "pinned_local_file",
          },
          licenses: {
            dataset: {
              name: "COCO",
              spdx_id: "CC-BY-4.0",
              reviewed: true,
              approved_for_local_probe: true,
            },
            model: {
              name: "Ultralytics",
              spdx_id: "AGPL-3.0",
              reviewed: true,
              approved_for_local_probe: true,
            },
            runtime: {
              name: "Ultralytics",
              spdx_id: "AGPL-3.0",
              reviewed: true,
              approved_for_local_probe: true,
            },
            deployment: {
              name: "Review required",
              spdx_id: "LicenseRef-Review",
              reviewed: true,
              approved_for_local_probe: true,
            },
          },
          egress: {
            frames_leave_local_machine: false,
            destination: null,
            operator_consent: "not_required",
          },
          lifecycle_state: "unverified",
        },
        availability: {
          status: "available",
          reason_codes: [] as string[],
          observations: {
            file: { status: "pass", reason: "file_passed" },
            digest: { status: "pass", reason: "digest_passed" },
            class_map: { status: "pass", reason: "class_map_passed" },
            license: { status: "pass", reason: "license_passed" },
            runtime_load: {
              status: "pass",
              reason: "runtime_load_passed",
              installed_runtime: {
                ultralytics: "8.4.31",
                sahi: "0.11.36",
                torch: "2.7.1",
              },
            },
          },
        },
        selectable_for_probe: true,
        qualification: {
          trial_eligible: false,
          source_segment_qualified: false,
          camera_qualified: false,
        },
      },
    ],
    profiles: [
      {
        profile_id: "official-coco-yolo11n-direct",
        model_id: "official-coco-yolo11n",
        model_version: "yolo11n-coco-v8.4.0",
        schema_version: "1.0",
        version: "detector-probe-profile-v1",
        profile_sha256: sha("d"),
        mode: "direct",
        settings: {
          confidence_threshold: 0.05,
          image_size: 1280,
          top_k: 5,
        },
        availability: { status: "available", reason_codes: [] as string[] },
        selectable_for_probe: true,
        recommended: true,
      },
    ],
    catalog_findings: [] as Array<Record<string, unknown>>,
  };
}

function legacyStrictJobFixture() {
  const profileResult = (profileId: string, digestCharacter: string) => ({
    profile_id: profileId,
    profile_sha256: sha(digestCharacter),
    status: "completed",
    raw_overlay_artifact_url: `/api/v1/detector-probes/probe-1/artifacts/raw-overlay-000000007-${profileId}`,
    raw_overlay_sha256: sha(digestCharacter === "5" ? "3" : "4"),
    raw_overlay_size_bytes: 90,
    raw_candidates: [
      {
        frame_index: 7,
        bbox_source_px: [10, 20, 18, 28],
        confidence: 0.8,
        class_name: "ball",
        checkpoint_class_name: "sports ball",
        source: "direct",
        coordinate_reason: "direct_source_coordinates",
        merge_reason: "retained_top_k",
      },
    ],
    display_candidate: {
      frame_index: 7,
      bbox_source_px: [10, 20, 18, 28],
      confidence: 0.8,
      class_name: "ball",
      checkpoint_class_name: "sports ball",
      source: "direct",
      coordinate_reason: "direct_source_coordinates",
      merge_reason: "retained_top_k",
    },
    latency_ms: 1.25,
    candidate_count: 1,
    top_k: 5,
    filter_reasons: { duplicate_suppressed_iou: 2 },
    failure_code: null,
  });
  return {
    schema_version: "1.0",
    artifact_type: "detector_probe_job",
    job_id: "probe-1",
    request_sha256: sha("1"),
    status: "ready",
    stage: "ready",
    progress: { completed: 2, total: 2 },
    retry_from_job_id: null,
    frozen_request: {
      parent_trial_id: "trial-1",
      source_sha256: sha("a"),
      source_width: 5120,
      source_height: 1440,
      tracking_contract_sha256: sha("b"),
      base_config_relative_path: "config/base.yaml",
      base_config_sha256: sha("c"),
      effective_config_relative_path: "config/generated/trial.yaml",
      effective_config_sha256: sha("e"),
      trial_intent_sha256: sha("f"),
      tuning_patch_binding: {
        state: "versioned",
        schema_version: "1.0",
        version_id: "tuning-v2",
        parent_version_id: "tuning-v1",
        values_sha256: sha("7"),
      },
      tuning_patch_sha256: sha("d"),
      profile_ids: ["profile-a", "profile-b"],
      profile_sha256s: { "profile-a": sha("5"), "profile-b": sha("6") },
      frame_indices: [7],
      top_k: 5,
    },
    frozen_profiles: [
      { profile_id: "profile-a", profile_sha256: sha("5") },
      { profile_id: "profile-b", profile_sha256: sha("6") },
    ],
    report: {
      schema_version: "1.0",
      artifact_type: "detector_probe_report",
      job_id: "probe-1",
      request_sha256: sha("1"),
      top_k: 5,
      source: {
        sha256: sha("a"),
        width: 5120,
        height: 1440,
        tracking_contract_sha256: sha("b"),
      },
      lineage: {
        parent_trial_id: "trial-1",
        base_config_relative_path: "config/base.yaml",
        base_config_sha256: sha("c"),
        effective_config_relative_path: "config/generated/trial.yaml",
        effective_config_sha256: sha("e"),
        trial_intent_sha256: sha("f"),
        tuning_patch_binding: {
          state: "versioned",
          schema_version: "1.0",
          version_id: "tuning-v2",
          parent_version_id: "tuning-v1",
          values_sha256: sha("7"),
        },
        tuning_patch_sha256: sha("d"),
        profile_sha256s: { "profile-a": sha("5"), "profile-b": sha("6") },
        retry_from_job_id: null,
      },
      frames: [
        {
          frame_index: 7,
          source_width: 5120,
          source_height: 1440,
          source_artifact_url:
            "/api/v1/detector-probes/probe-1/artifacts/source-frame-000000007",
          source_frame_sha256: sha("2"),
          source_frame_size_bytes: 100,
          profile_results: [
            profileResult("profile-a", "5"),
            profileResult("profile-b", "6"),
          ],
        },
      ],
    },
  };
}

function strictCatalogFixture() {
  const fixture = detectorProbeCatalogFixture();
  const descriptor = {
    ...detectorProbeDescriptorFixture(
      "official-coco-yolo11n",
      sha("b"),
      sha("c"),
    ),
    version: "yolo11n-coco-v8.4.0",
    display_name: "Official YOLO11n",
  };
  return {
    ...fixture,
    models: [
      {
        ...fixture.models[0],
        descriptor,
      },
    ],
    profiles: [
      {
        ...fixture.profiles[0],
        profile_id: "official-coco-yolo11n-direct",
        model_id: "official-coco-yolo11n",
        model_version: descriptor.version,
        model_descriptor_sha256: descriptor.descriptor_sha256,
        profile_sha256: sha("d"),
      },
    ],
    catalog_findings: [] as Array<Record<string, unknown>>,
  };
}

function strictJobFixture() {
  return detectorProbeJobFixture("probe-1", "ready", null, {
    frameIndices: [7],
    zeroCandidates: false,
  });
}

function bindExecutionBundles(job: ReturnType<typeof strictJobFixture>) {
  for (const bundle of [
    job.frozen_request.execution_bundle,
    job.report.lineage.execution_bundle,
  ]) {
    Object.assign(bundle, {
      code_commit: "a".repeat(40),
      code_commit_status: "bound",
      code_commit_reason: null,
      code_commit_blob_files: structuredClone(bundle.code_bundle_files),
      code_commit_blob_bundle_sha256: sha("7"),
      code_commit_binding_kind: "exact_or_crlf_to_lf_commit_blob",
    });
  }
}

function publicFindingFixture(findingId: string) {
  return {
    finding_id: findingId,
    display_name: "Public soccer-ball candidate",
    source: {
      project: findingId,
      version: "3",
      url: "https://universe.roboflow.com/example/model/3",
    },
    architecture_family: "yolo11n",
    access: {
      method: "roboflow_hosted_or_account_export",
      account_or_plan_required: "unverified",
      local_weights_validated: false,
    },
    licenses: Object.fromEntries(
      ["dataset", "model", "runtime", "deployment"].map((kind) => [
        kind,
        { status: "review_required", approved_for_local_probe: false },
      ]),
    ),
    egress: {
      frames_leave_local_machine: "unknown_until_access_method_selected",
      destination: null,
      operator_consent: "required_before_external_inference",
    },
    selectable: false,
    availability: {
      status: "unavailable",
      reason_codes: ["exact_weight_access_not_validated"],
    },
  };
}

describe("production detector probe lineage", () => {
  it("opens recovery only for the current completed all-lost authoritative trial", () => {
    const eligible = {
      monitoredRunId: "trial-1",
      diagnosisRunId: "trial-1",
      authoritativeRun: { runId: "trial-1", status: "completed" },
      gate: {
        status: "retune_required",
        coverageComplete: true,
        failureCode: "no_raw_candidates",
        coverageStatus: "complete",
        reconciliationStatus: "reconciled",
        evaluatedFrames: { status: "collected", value: 300 },
        lostFrames: { status: "collected", value: 300 },
        rawCandidates: { status: "collected", value: 0 },
      },
    };

    expect(detectorProbeRecoveryEligible(eligible)).toBe(true);
    expect(
      detectorProbeRecoveryEligible({
        ...eligible,
        gate: {
          ...eligible.gate,
          failureCode: "all_candidates_class_rejected",
          rawCandidates: { status: "collected", value: 12 },
        },
      }),
    ).toBe(true);
  });

  it.each([
    { authoritativeRun: { runId: "trial-old", status: "completed" } },
    { authoritativeRun: { runId: "trial-1", status: "running" } },
    { diagnosisRunId: "trial-old" },
    { gate: null },
    { gate: { status: "acceptable" } },
    { gate: { coverageComplete: false } },
    { gate: { coverageStatus: "partial" } },
    { gate: { reconciliationStatus: "mismatch" } },
    { gate: { failureCode: "decode_failure" } },
    { gate: { failureCode: "unknown_detector_failure" } },
    { gate: { evaluatedFrames: { status: "not_collected", value: null } } },
    { gate: { evaluatedFrames: { status: "collected", value: 0 } } },
    { gate: { lostFrames: { status: "collected", value: 299 } } },
    { gate: { rawCandidates: { status: "collected", value: 1 } } },
    { gate: { rawCandidates: { status: "not_collected", value: null } } },
    {
      gate: {
        failureCode: "all_candidates_filtered",
        rawCandidates: { status: "collected", value: -1 },
      },
    },
  ])("does not open recovery for stale or incomplete evidence %#", (patch) => {
    const gate = {
      status: "retune_required",
      coverageComplete: true,
      failureCode: "no_raw_candidates",
      coverageStatus: "complete",
      reconciliationStatus: "reconciled",
      evaluatedFrames: { status: "collected", value: 300 },
      lostFrames: { status: "collected", value: 300 },
      rawCandidates: { status: "collected", value: 0 },
      ...(patch.gate ?? {}),
    };
    expect(
      detectorProbeRecoveryEligible({
        monitoredRunId: "trial-1",
        diagnosisRunId: patch.diagnosisRunId ?? "trial-1",
        authoritativeRun: {
          runId: "trial-1",
          status: "completed",
          ...patch.authoritativeRun,
        },
        gate: patch.gate === null ? null : gate,
      }),
    ).toBe(false);
  });

  it("sends only the parent trial and exact bounded profile/frame intent", () => {
    const request = buildDetectorProbeRequest({
      parentTrialId: "trial-1",
      profileIds: ["profile-b", "profile-a"],
      frameIndices: [30, 10, 20],
    });

    expect(request).toMatchObject({
      parent_trial_id: "trial-1",
      profile_ids: ["profile-a", "profile-b"],
      frame_indices: [10, 20, 30],
      top_k: 5,
    });
    expect(JSON.stringify(request)).not.toContain("model_path");
    expect(JSON.stringify(request)).not.toContain("relative_path");
    expect(JSON.stringify(request)).not.toContain("source_sha256");
  });

  it.each([
    { profileIds: ["only-one"] },
    { profileIds: ["profile-a", "profile-a"] },
    {
      profileIds: Array.from({ length: 7 }, (_, index) => `profile-${index}`),
    },
    { frameIndices: [] },
    { frameIndices: [1, 1] },
    { parentTrialId: " trial-1" },
    { profileIds: ["profile-a", "bad/profile"] },
    { retryFromJobId: "../probe-0" },
  ])("rejects unbounded probe input %#", (patch) => {
    expect(() =>
      buildDetectorProbeRequest({
        parentTrialId: "trial-1",
        profileIds: ["profile-a", "profile-b"],
        frameIndices: [1],
        ...patch,
      }),
    ).toThrow();
  });

  it("accepts only a safe server-issued job ID from the create envelope", () => {
    expect(detectorProbeJobId({ job_id: "probe-job-1" })).toBe("probe-job-1");
    expect(() => detectorProbeJobId({ job_id: "../probe-job-1" })).toThrow();
    expect(() => detectorProbeJobId({ job_id: "probe%2Fjob" })).toThrow();
  });

  it("binds the create response digest and control URLs to its safe job identity", () => {
    expect(
      detectorProbeCreateEnvelope({
        job_id: "probe-job-1",
        request_sha256: sha("a"),
        status: "queued",
        status_url: "/api/v1/detector-probes/probe-job-1",
        cancel_url: "/api/v1/detector-probes/probe-job-1/cancel",
        retry_from_job_id: null,
      }),
    ).toEqual({
      jobId: "probe-job-1",
      requestSha256: sha("a"),
      status: "queued",
      retryFromJobId: null,
    });
    expect(() =>
      detectorProbeCreateEnvelope({
        job_id: "probe-job-1",
        request_sha256: sha("a"),
        status: "queued",
        status_url: "/api/v1/detector-probes/other-job",
        cancel_url: "/api/v1/detector-probes/probe-job-1/cancel",
        retry_from_job_id: null,
      }),
    ).toThrow();
    expect(() =>
      detectorProbeCreateEnvelope({
        job_id: "probe-job-1",
        request_sha256: sha("a"),
        status: "queued",
        status_url: "/api/v1/detector-probes/probe-job-1",
        cancel_url: "/api/v1/detector-probes/probe-job-1/cancel",
        retry_from_job_id: "probe-job-1",
      }),
    ).toThrow(/cannot reference itself/i);
  });

  it("isolates recovered jobs by safe workflow and authoritative parent trial", () => {
    expect(detectorProbeStorageKey("workflow-1", "trial-1")).toBe(
      "football-tracking.production-detector-probe.v1.workflow-1.trial-1",
    );
    expect(() => detectorProbeStorageKey("workflow/1", "trial-1")).toThrow();
    expect(() => detectorProbeStorageKey("workflow-1", "../trial-1")).toThrow();
  });
});

describe("production detector probe API mapping", () => {
  it("accepts a non-null server-local media-integrity evidence path", () => {
    const job = strictJobFixture();
    job.report.frames[0].media_integrity.path = "frames/7.integrity.json";

    expect(detectorProbeJobView(job).frames[0]).toMatchObject({
      frameIndex: 7,
      mediaIntegrityClean: true,
    });
  });

  it("shows a valid trusted imported model as blocked and never probe-selectable", () => {
    const models = detectorProbeCatalogView(
      detectorProbeImportedCatalogFixture(),
    );

    expect(models).toEqual([
      expect.objectContaining({
        modelId: "trusted-imported-ball-model",
        displayName: "Trusted imported ball model",
        availability: "blocked",
        availabilityReason: "server_validation_required",
        lifecycle: "unverified",
        profiles: [],
      }),
    ]);
  });

  it("keeps legal same-ID model versions distinct and binds profiles to the exact version", () => {
    const catalog = strictCatalogFixture();
    const imported = detectorProbeImportedCatalogFixture().models[0];
    imported.descriptor.model_id = "official-coco-yolo11n";
    imported.descriptor.version = "trusted-v2";
    imported.descriptor.model_version = "trusted-v2";
    catalog.models.push(imported as (typeof catalog.models)[number]);

    const models = detectorProbeCatalogView(catalog);

    expect(models).toHaveLength(2);
    expect(
      models.find((model) => model.version === "yolo11n-coco-v8.4.0"),
    ).toMatchObject({
      modelId: "official-coco-yolo11n",
      profiles: [
        expect.objectContaining({
          profileId: "official-coco-yolo11n-direct",
          probeSelectable: true,
        }),
      ],
    });
    expect(
      models.find((model) => model.version === "trusted-v2"),
    ).toMatchObject({
      modelId: "official-coco-yolo11n",
      availability: "blocked",
      profiles: [],
    });
  });

  it("keeps public findings and imported models in independent ID namespaces", () => {
    const catalog = detectorProbeImportedCatalogFixture();
    catalog.catalog_findings.push(
      publicFindingFixture("trusted-imported-ball-model"),
    );

    const models = detectorProbeCatalogView(catalog);

    expect(models).toHaveLength(2);
    expect(models.map((model) => model.lifecycle)).toEqual([
      "unverified",
      "catalog_finding_only",
    ]);
    expect(models.map((model) => model.modelId)).toEqual([
      "trusted-imported-ball-model",
      "trusted-imported-ball-model",
    ]);
  });

  it("accepts backend-safe consecutive dots without accepting traversal", () => {
    const catalog = detectorProbeImportedCatalogFixture();
    catalog.models[0].descriptor.model_id = "camera..v2";
    catalog.models[0].descriptor.version = "weights..2026";
    catalog.models[0].descriptor.model_version = "weights..2026";

    expect(detectorProbeCatalogView(catalog)[0]).toMatchObject({
      modelId: "camera..v2",
      version: "weights..2026",
    });
    expect(detectorProbeStorageKey("workflow..1", "trial..1")).toContain(
      "workflow..1.trial..1",
    );
    expect(() => detectorProbeStorageKey("workflow/../1", "trial-1")).toThrow();
  });

  it.each(["checkpoint", "runtime_contract", "expected_input", "execution"])(
    "keeps built-in descriptor field %s mandatory",
    (field) => {
      const catalog = strictCatalogFixture();
      delete (catalog.models[0].descriptor as Record<string, unknown>)[field];
      expect(() => detectorProbeCatalogView(catalog)).toThrow();
    },
  );

  it.each([
    "runtime_contract",
    "expected_input",
    "memory_envelope",
    "import_manifest_sha256",
  ])("rejects trusted imported descriptors missing %s", (field) => {
    const catalog = detectorProbeImportedCatalogFixture();
    delete (catalog.models[0].descriptor as Record<string, unknown>)[field];
    expect(() => detectorProbeCatalogView(catalog)).toThrow();
  });

  it("rejects an imported model forged as available or qualified", () => {
    const available = detectorProbeImportedCatalogFixture();
    available.models[0].availability.status = "available";
    available.models[0].availability.reason_codes = [];
    available.models[0].selectable_for_probe = true;
    expect(() => detectorProbeCatalogView(available)).toThrow();

    const qualified = detectorProbeImportedCatalogFixture();
    qualified.models[0].qualification.trial_eligible = true;
    expect(() => detectorProbeCatalogView(qualified)).toThrow();
  });

  it("rejects any probe profile attached to a trusted imported model", () => {
    const catalog = detectorProbeImportedCatalogFixture();
    const profile = structuredClone(detectorProbeCatalogFixture().profiles[0]);
    profile.profile_id = "trusted-imported-ball-model-direct";
    profile.model_id = catalog.models[0].descriptor.model_id;
    profile.model_version = catalog.models[0].descriptor.version;
    profile.model_descriptor_sha256 =
      catalog.models[0].descriptor.descriptor_sha256;
    profile.settings.allowed_labels = ["football"];
    catalog.profiles.push(profile);

    expect(() => detectorProbeCatalogView(catalog)).toThrow();
  });

  it("maps exact catalog metadata while keeping unverified models probe-only", () => {
    const models = detectorProbeCatalogView(strictCatalogFixture());

    expect(models).toEqual([
      expect.objectContaining({
        modelId: "official-coco-yolo11n",
        version: "yolo11n-coco-v8.4.0",
        runtimeVersion: "ultralytics=8.4.31 · sahi=0.11.36 · torch=2.7.1",
        lifecycle: "unverified",
        trialEligible: false,
        weightsSha256: sha("c"),
        profiles: [
          expect.objectContaining({
            profileId: "official-coco-yolo11n-direct",
            confidenceThreshold: 0.05,
            tile: null,
            topK: 5,
            probeSelectable: true,
          }),
        ],
      }),
    ]);
  });

  it("maps inaccessible public candidates as honest unavailable findings with no profiles", () => {
    const catalog = strictCatalogFixture();
    catalog.catalog_findings.push({
      finding_id: "public-soccer-ball-yolo11n",
      display_name: "Roboflow soccer-ball-detection-s2sg3 version 3",
      source: {
        project: "soccer-ball-detection-s2sg3",
        version: "3",
        url: "https://universe.roboflow.com/example/model/3",
      },
      architecture_family: "yolo11n",
      access: {
        method: "roboflow_hosted_or_account_export",
        account_or_plan_required: "unverified",
        local_weights_validated: false,
      },
      licenses: Object.fromEntries(
        ["dataset", "model", "runtime", "deployment"].map((kind) => [
          kind,
          {
            status: "review_required",
            approved_for_local_probe: false,
          },
        ]),
      ),
      egress: {
        frames_leave_local_machine: "unknown_until_access_method_selected",
        destination: null,
        operator_consent: "required_before_external_inference",
      },
      selectable: false,
      availability: {
        status: "unavailable",
        reason_codes: [
          "exact_weight_access_not_validated",
          "license_review_incomplete",
        ],
      },
    });

    expect(detectorProbeCatalogView(catalog)[1]).toMatchObject({
      modelId: "public-soccer-ball-yolo11n",
      version: "3",
      weightsSha256: null,
      manifestSha256: null,
      lifecycle: "catalog_finding_only",
      availability: "unavailable",
      availabilityReason: expect.stringContaining(
        "exact_weight_access_not_validated",
      ),
      datasetLicense: "review_required",
      egress: {
        leavesDevice: null,
        consent: "required_before_external_inference",
      },
      profiles: [],
    });
  });

  it("maps same-frame source/raw overlay/display_candidate evidence", () => {
    const view = detectorProbeJobView(
      false
        ? {
            schema_version: "1.0",
            artifact_type: "detector_probe_job",
            job_id: "probe-1",
            request_sha256: sha("1"),
            status: "ready",
            stage: "ready",
            progress: { completed: 2, total: 2 },
            retry_from_job_id: null,
            frozen_request: {
              parent_trial_id: "trial-1",
              source_sha256: sha("a"),
              source_width: 5120,
              source_height: 1440,
              tracking_contract_sha256: sha("b"),
              base_config_relative_path: "config/base.yaml",
              base_config_sha256: sha("c"),
              effective_config_relative_path: "config/generated/trial.yaml",
              effective_config_sha256: sha("e"),
              trial_intent_sha256: sha("f"),
              tuning_patch_binding: {
                state: "versioned",
                schema_version: "1.0",
                version_id: "tuning-v2",
                parent_version_id: "tuning-v1",
                values_sha256: sha("7"),
              },
              tuning_patch_sha256: sha("d"),
              profile_ids: ["profile-a", "profile-b"],
              profile_sha256s: { "profile-a": sha("5"), "profile-b": sha("6") },
              frame_indices: [7],
              top_k: 5,
            },
            frozen_profiles: [
              { profile_id: "profile-a", profile_sha256: sha("5") },
              { profile_id: "profile-b", profile_sha256: sha("6") },
            ],
            report: {
              schema_version: "1.0",
              artifact_type: "detector_probe_report",
              job_id: "probe-1",
              request_sha256: sha("1"),
              top_k: 5,
              source: {
                sha256: sha("a"),
                width: 5120,
                height: 1440,
                tracking_contract_sha256: sha("b"),
              },
              lineage: {
                parent_trial_id: "trial-1",
                base_config_relative_path: "config/base.yaml",
                base_config_sha256: sha("c"),
                effective_config_relative_path: "config/generated/trial.yaml",
                effective_config_sha256: sha("e"),
                trial_intent_sha256: sha("f"),
                tuning_patch_binding: {
                  state: "versioned",
                  schema_version: "1.0",
                  version_id: "tuning-v2",
                  parent_version_id: "tuning-v1",
                  values_sha256: sha("7"),
                },
                tuning_patch_sha256: sha("d"),
                profile_sha256s: {
                  "profile-a": sha("5"),
                  "profile-b": sha("6"),
                },
                retry_from_job_id: null,
              },
              frames: [
                {
                  frame_index: 7,
                  source_width: 5120,
                  source_height: 1440,
                  source_artifact_url:
                    "/api/v1/detector-probes/probe-1/artifacts/source-frame-000000007",
                  source_frame_sha256: sha("2"),
                  source_frame_size_bytes: 100,
                  profile_results: ["profile-a", "profile-b"].map(
                    (profileId) => ({
                      profile_id: profileId,
                      profile_sha256: sha(
                        profileId === "profile-a" ? "5" : "6",
                      ),
                      status: "completed",
                      raw_overlay_artifact_url: `/api/v1/detector-probes/probe-1/artifacts/raw-overlay-000000007-${profileId}`,
                      raw_overlay_sha256: sha(
                        profileId === "profile-a" ? "3" : "4",
                      ),
                      raw_overlay_size_bytes: 90,
                      raw_candidates: [
                        {
                          frame_index: 7,
                          bbox_source_px: [10, 20, 18, 28],
                          confidence: 0.8,
                          class_name: "ball",
                          checkpoint_class_name: "sports ball",
                          source: "direct",
                          coordinate_reason: "direct_source_coordinates",
                          merge_reason: "retained_top_k",
                        },
                      ],
                      display_candidate: {
                        frame_index: 7,
                        bbox_source_px: [10, 20, 18, 28],
                        confidence: 0.8,
                        class_name: "ball",
                        checkpoint_class_name: "sports ball",
                        source: "direct",
                        coordinate_reason: "direct_source_coordinates",
                        merge_reason: "retained_top_k",
                      },
                      latency_ms: 1.25,
                      candidate_count: 1,
                      top_k: 5,
                      filter_reasons: { duplicate_suppressed_iou: 2 },
                      failure_code: null,
                    }),
                  ),
                },
              ],
            },
          }
        : strictJobFixture(),
    );

    expect(view.progressPercent).toBe(100);
    expect(view.parentTrialId).toBe("trial-1");
    expect(view.frameIndices).toEqual([7]);
    expect(view.noProfilesProducedCandidates).toBe(false);
    expect(view.frames[0]).toMatchObject({
      frameIndex: 7,
      sourceImageUrl: "/api/detector-probes/probe-1/artifacts/source-7",
      sourceSha256: sha("a"),
      sourceWidth: 5120,
      sourceHeight: 1440,
      profiles: [
        expect.objectContaining({
          profileId: "model-a-direct",
          overlayImageUrl:
            "/api/detector-probes/probe-1/artifacts/overlay-7-model-a-direct",
          displayCandidate: {
            x: 10,
            y: 20,
            width: 8,
            height: 8,
            confidence: 0.8,
            label: "ball",
          },
          topK: 5,
        }),
        expect.objectContaining({ profileId: "model-b-sahi" }),
      ],
    });
  });

  it("accepts an explicitly unavailable code bundle without inventing commit evidence", () => {
    const job = strictJobFixture();

    for (const bundle of [
      job.frozen_request.execution_bundle,
      job.report.lineage.execution_bundle,
    ]) {
      expect(bundle).toMatchObject({
        code_commit: null,
        code_commit_status: "unavailable",
        code_commit_reason: "repository_commit_unavailable",
        code_commit_blob_files: null,
        code_commit_blob_bundle_sha256: null,
        code_commit_binding_kind: null,
      });
    }
    expect(detectorProbeJobView(job).jobId).toBe(job.job_id);
  });

  it("accepts an honestly unbound code bundle without inventing a commit", () => {
    const job = strictJobFixture();
    for (const bundle of [
      job.frozen_request.execution_bundle,
      job.report.lineage.execution_bundle,
    ]) {
      Object.assign(bundle, {
        code_commit: null,
        code_commit_status: "unbound",
        code_commit_reason: "code_bundle_differs_from_commit",
        code_commit_blob_files: null,
        code_commit_blob_bundle_sha256: null,
        code_commit_binding_kind: null,
      });
    }

    expect(detectorProbeJobView(job).jobId).toBe(job.job_id);
  });

  it("accepts a bound commit only with the complete normalized blob bundle", () => {
    const job = strictJobFixture();
    bindExecutionBundles(job);

    expect(detectorProbeJobView(job).jobId).toBe(job.job_id);
  });

  it.each([
    "code_commit_blob_files",
    "code_commit_blob_bundle_sha256",
    "code_commit_binding_kind",
  ] as const)("rejects a missing required %s field", (field) => {
    const job = strictJobFixture();
    delete (job.frozen_request.execution_bundle as Record<string, unknown>)[
      field
    ];

    expect(() => detectorProbeJobView(job)).toThrow(/fields are invalid/i);
  });

  it.each([
    {
      name: "incomplete commit blob allowlist",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        bindExecutionBundles(job);
        delete job.frozen_request.execution_bundle.code_commit_blob_files![
          "football_tracking/detector.py"
        ];
      },
    },
    {
      name: "invalid commit blob digest",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        bindExecutionBundles(job);
        job.frozen_request.execution_bundle.code_commit_blob_files![
          "football_tracking/detector.py"
        ] = "latest";
      },
    },
    {
      name: "invalid commit blob aggregate digest",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        bindExecutionBundles(job);
        job.frozen_request.execution_bundle.code_commit_blob_bundle_sha256 =
          "latest";
      },
    },
    {
      name: "unknown commit binding kind",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        bindExecutionBundles(job);
        (
          job.frozen_request.execution_bundle as Record<string, unknown>
        ).code_commit_binding_kind = "working_tree_bytes";
      },
    },
    {
      name: "unbound status carrying commit blob files",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_status = "unbound";
        job.frozen_request.execution_bundle.code_commit_reason =
          "code_bundle_differs_from_commit";
        job.frozen_request.execution_bundle.code_commit_blob_files =
          structuredClone(
            job.frozen_request.execution_bundle.code_bundle_files,
          );
      },
    },
    {
      name: "unbound status carrying a commit blob aggregate",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_status = "unbound";
        job.frozen_request.execution_bundle.code_commit_reason =
          "code_bundle_differs_from_commit";
        job.frozen_request.execution_bundle.code_commit_blob_bundle_sha256 =
          sha("7");
      },
    },
    {
      name: "unbound status carrying a commit binding kind",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_status = "unbound";
        job.frozen_request.execution_bundle.code_commit_reason =
          "code_bundle_differs_from_commit";
        job.frozen_request.execution_bundle.code_commit_binding_kind =
          "exact_or_crlf_to_lf_commit_blob";
      },
    },
    {
      name: "unavailable status carrying commit blob files",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_blob_files =
          structuredClone(
            job.frozen_request.execution_bundle.code_bundle_files,
          );
      },
    },
    {
      name: "unavailable status carrying a commit blob aggregate",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_blob_bundle_sha256 =
          sha("7");
      },
    },
    {
      name: "unavailable status carrying a commit binding kind",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.execution_bundle.code_commit_binding_kind =
          "exact_or_crlf_to_lf_commit_blob";
      },
    },
  ])("rejects $name", ({ mutate }) => {
    const job = strictJobFixture();
    mutate(job);

    expect(() => detectorProbeJobView(job)).toThrow();
  });

  it.each([
    {
      name: "frozen source file identity",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.frozen_request.source_file_identity_sha256 = sha("8");
      },
    },
    {
      name: "report source file identity",
      mutate: (job: ReturnType<typeof strictJobFixture>) => {
        job.report.source.file_identity_sha256 = sha("8");
      },
    },
  ])("rejects tampered $name lineage", ({ mutate }) => {
    const job = strictJobFixture();
    mutate(job);
    expect(() => detectorProbeJobView(job)).toThrow(
      /source (identity|lineage)/i,
    );
  });

  it("does not classify a failed profile execution as a successful all-zero comparison", () => {
    const job = strictJobFixture();
    job.report.frames[0].profile_results =
      job.report.frames[0].profile_results.map((profile) => ({
        ...profile,
        status: "failed",
        failure_code: "runtime_load_failed",
        raw_candidates: [],
        display_candidate: null,
        candidate_count: 0,
      }));

    expect(detectorProbeJobView(job).noProfilesProducedCandidates).toBe(false);
  });

  it("accepts an explicitly absent tuning version while preserving its own binding digest", () => {
    const job = strictJobFixture();
    const absentBinding = {
      state: "absent",
      schema_version: "1.0",
      version_id: null,
      parent_version_id: null,
      values_sha256:
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    };
    Object.assign(job.frozen_request.tuning_patch_binding, absentBinding);
    Object.assign(job.report.lineage.tuning_patch_binding, absentBinding);

    expect(detectorProbeJobView(job).parentTrialId).toBe("trial-1");
  });
});

describe("production detector probe fail-closed mapping", () => {
  it.each<{
    name: string;
    mutate: (catalog: ReturnType<typeof strictCatalogFixture>) => void;
  }>([
    {
      name: "unknown model availability",
      mutate: (catalog) => {
        catalog.models[0].availability.status = "mystery";
      },
    },
    {
      name: "unknown profile availability",
      mutate: (catalog) => {
        catalog.profiles[0].availability.status = "mystery";
      },
    },
    {
      name: "unknown profile mode",
      mutate: (catalog) => {
        catalog.profiles[0].mode = "remote";
      },
    },
    {
      name: "unknown egress consent",
      mutate: (catalog) => {
        catalog.models[0].descriptor.egress.operator_consent = "implicit";
      },
    },
    {
      name: "non-positive input size",
      mutate: (catalog) => {
        catalog.profiles[0].settings.image_size = -1;
      },
    },
    {
      name: "invalid confidence threshold",
      mutate: (catalog) => {
        catalog.profiles[0].settings.confidence_threshold = 1.5;
      },
    },
    {
      name: "SAHI profile without exact tile settings",
      mutate: (catalog) => {
        catalog.profiles[0].mode = "sahi";
      },
    },
    {
      name: "non-five top-k",
      mutate: (catalog) => {
        catalog.profiles[0].settings.top_k = 10;
      },
    },
    {
      name: "mismatched model version",
      mutate: (catalog) => {
        catalog.profiles[0].model_version = "latest";
      },
    },
    {
      name: "missing exact profile version",
      mutate: (catalog) => {
        catalog.profiles[0].version = "";
      },
    },
    {
      name: "duplicate exact model identity",
      mutate: (catalog) => {
        catalog.models.push(structuredClone(catalog.models[0]));
      },
    },
    {
      name: "duplicate profile ID",
      mutate: (catalog) => {
        catalog.profiles.push(structuredClone(catalog.profiles[0]));
      },
    },
    {
      name: "orphan profile",
      mutate: (catalog) => {
        catalog.profiles[0].model_id = "missing-model";
      },
    },
    {
      name: "uppercase descriptor digest",
      mutate: (catalog) => {
        catalog.models[0].descriptor.descriptor_sha256 = sha("A");
      },
    },
    {
      name: "whitespace model ID",
      mutate: (catalog) => {
        catalog.models[0].descriptor.model_id = " model-a";
      },
    },
  ])("rejects $name", ({ mutate }) => {
    const catalog = strictCatalogFixture();
    mutate(catalog);
    expect(() => detectorProbeCatalogView(catalog)).toThrow();
  });

  it("keeps unreviewed or unapproved-egress profiles probe-unselectable", () => {
    const unreviewed = strictCatalogFixture();
    unreviewed.models[0].descriptor.licenses.model.reviewed = false;
    expect(detectorProbeCatalogView(unreviewed)[0].profiles[0]).toMatchObject({
      probeSelectable: false,
      unavailableReason: expect.stringContaining("licenses_not_reviewed"),
    });

    const external = strictCatalogFixture();
    Object.assign(external.models[0].descriptor.egress, {
      frames_leave_local_machine: true,
      destination: "https://api.example.invalid/inference",
      operator_consent: "required_not_granted",
    });
    expect(detectorProbeCatalogView(external)[0].profiles[0]).toMatchObject({
      probeSelectable: false,
      unavailableReason: expect.stringContaining("egress_not_approved"),
    });
  });

  it.each<{
    name: string;
    mutate: (job: ReturnType<typeof strictJobFixture>) => void;
  }>([
    {
      name: "unknown job status",
      mutate: (job) => {
        job.status = "partial";
      },
    },
    {
      name: "unknown profile status",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].status = "partial";
      },
    },
    {
      name: "non-five evidence top-k",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].top_k = 10;
      },
    },
    {
      name: "external source URL",
      mutate: (job) => {
        job.report.frames[0].source_artifact_url =
          "https://attacker.invalid/source.jpg";
      },
    },
    {
      name: "data overlay URL",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_artifact_url =
          "data:image/svg+xml,unsafe";
      },
    },
    {
      name: "javascript overlay URL",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_artifact_url =
          "javascript:alert(1)";
      },
    },
    {
      name: "query-bearing overlay URL",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_artifact_url +=
          "?redirect=https://attacker.invalid";
      },
    },
    {
      name: "encoded-path overlay URL",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_artifact_url =
          "/api/v1/detector-probes/probe-1/artifacts/raw%2Foverlay";
      },
    },
    {
      name: "negative candidate count",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].candidate_count = -1;
      },
    },
    {
      name: "candidate count below raw evidence",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].candidate_count = 0;
      },
    },
    {
      name: "display candidate outside raw evidence",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].display_candidate.bbox_source_px =
          [30, 40, 38, 48];
      },
    },
    {
      name: "completed profile without latency",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].latency_ms = null;
      },
    },
    {
      name: "failed profile without failure code",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].status = "failed";
      },
    },
    {
      name: "fractional filter count",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].filter_reasons.duplicate_suppressed_iou = 1.5;
      },
    },
    {
      name: "negative latency",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].latency_ms = -1;
      },
    },
    {
      name: "zero overlay size",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_size_bytes = 0;
      },
    },
    {
      name: "fractional frame index",
      mutate: (job) => {
        job.report.frames[0].frame_index = 7.5;
      },
    },
    {
      name: "zero source width",
      mutate: (job) => {
        job.report.frames[0].source_width = 0;
      },
    },
    {
      name: "box outside source dimensions",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].bbox_source_px =
          [10, 20, 6000, 28];
      },
    },
    {
      name: "candidate bound to a different frame",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].frame_index = 8;
      },
    },
    {
      name: "candidate without detector source",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].source = "";
      },
    },
    {
      name: "candidate with unsupported coordinate reason",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].coordinate_reason =
          "resized_preview_coordinates";
      },
    },
    {
      name: "candidate without checkpoint class",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].checkpoint_class_name =
          "";
      },
    },
    {
      name: "candidate with unsupported merge reason",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_candidates[0].merge_reason =
          "unmerged";
      },
    },
    {
      name: "display candidate with different evidence lineage",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].display_candidate.source =
          "sahi";
      },
    },
    {
      name: "progress beyond total",
      mutate: (job) => {
        job.progress.completed = 3;
      },
    },
    {
      name: "duplicate frozen profile",
      mutate: (job) => {
        job.frozen_request.profile_ids[1] = "profile-a";
      },
    },
    {
      name: "mismatched frozen frame",
      mutate: (job) => {
        job.frozen_request.frame_indices[0] = 8;
      },
    },
    {
      name: "non-five frozen top-k",
      mutate: (job) => {
        job.frozen_request.top_k = 4;
      },
    },
    {
      name: "unsafe frozen parent trial",
      mutate: (job) => {
        job.frozen_request.parent_trial_id = "../trial-1";
      },
    },
    {
      name: "frozen profile digest mismatch",
      mutate: (job) => {
        job.frozen_profiles[0].profile_sha256 = sha("7");
      },
    },
    {
      name: "frozen profile aggregate mismatch",
      mutate: (job) => {
        job.frozen_request.frozen_profiles_sha256 = sha("7");
      },
    },
    {
      name: "missing runtime observation evidence",
      mutate: (job) => {
        delete job.frozen_request.execution_bundle
          .runtime_observation_evidence_sha256s[
          job.frozen_request.profile_ids[0]
        ];
      },
    },
    {
      name: "incomplete code bundle allowlist",
      mutate: (job) => {
        delete job.frozen_request.execution_bundle.code_bundle_files[
          "football_tracking/detector.py"
        ];
      },
    },
    {
      name: "missing Pydantic version binding",
      mutate: (job) => {
        delete job.frozen_request.execution_bundle.execution_environment
          .pydantic_version;
      },
    },
    {
      name: "non-string Pydantic Core version binding",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.pydantic_core_version =
          2332;
      },
    },
    {
      name: "CPU environment claims GPU identity",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.gpu_name =
          "unexpected GPU";
      },
    },
    {
      name: "invalid decoder fingerprint",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.decoder_fingerprint_sha256 =
          "latest";
      },
    },
    {
      name: "non-fp32 frozen execution precision",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.precision =
          "fp16";
      },
    },
    {
      name: "non-string CUDA visibility",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.cuda_visible_devices = 7;
      },
    },
    {
      name: "invalid OpenCV FFmpeg observation",
      mutate: (job) => {
        job.frozen_request.execution_bundle.execution_environment.opencv_ffmpeg_enabled =
          "unknown";
      },
    },
    {
      name: "invalid bound code commit",
      mutate: (job) => {
        Object.assign(job.frozen_request.execution_bundle, {
          code_commit_status: "bound",
          code_commit: "not-a-commit",
          code_commit_reason: null,
        });
      },
    },
    {
      name: "unavailable code commit with a commit value",
      mutate: (job) => {
        job.frozen_request.execution_bundle.code_commit = "a".repeat(40);
      },
    },
    {
      name: "unbound code commit with unavailable reason",
      mutate: (job) => {
        Object.assign(job.frozen_request.execution_bundle, {
          code_commit_status: "unbound",
          code_commit: null,
          code_commit_reason: "repository_commit_unavailable",
        });
      },
    },
    {
      name: "unknown code commit status",
      mutate: (job) => {
        job.frozen_request.execution_bundle.code_commit_status = "dirty";
      },
    },
    {
      name: "execution bundle frozen-profile digest mismatch",
      mutate: (job) => {
        job.frozen_request.execution_bundle.frozen_profiles_sha256 = sha("7");
      },
    },
    {
      name: "runtime environment digest mismatch",
      mutate: (job) => {
        job.frozen_request.runtime_environment_sha256 = sha("7");
      },
    },
    {
      name: "missing frozen frame array",
      mutate: (job) => {
        Object.assign(job.frozen_request, { frame_indices: null });
      },
    },
    {
      name: "duplicate frozen frame indices",
      mutate: (job) => {
        job.frozen_request.frame_indices = [7, 7];
      },
    },
    {
      name: "missing frozen profile array",
      mutate: (job) => {
        Object.assign(job, { frozen_profiles: null });
      },
    },
    {
      name: "missing frozen profile bindings",
      mutate: (job) => {
        Object.assign(job.frozen_request, { profile_bindings: null });
      },
    },
    {
      name: "frozen profile binding mismatch",
      mutate: (job) => {
        job.frozen_request.profile_bindings[0].weights_sha256 = sha("7");
      },
    },
    {
      name: "progress total not bound to frozen work",
      mutate: (job) => {
        job.progress.total = 99;
      },
    },
    {
      name: "ready job claims cancellation capability",
      mutate: (job) => {
        job.can_cancel = true;
      },
    },
    {
      name: "frozen retry lineage mismatch",
      mutate: (job) => {
        Object.assign(job.frozen_request, {
          retry_from_job_id: "probe-parent",
        });
      },
    },
    {
      name: "job idempotency digest mismatch",
      mutate: (job) => {
        job.idempotency_key = sha("7");
      },
    },
    {
      name: "job control URL mismatch",
      mutate: (job) => {
        job.cancel_url = "/api/v1/detector-probes/other/cancel";
      },
    },
    {
      name: "missing frozen profile ID array",
      mutate: (job) => {
        Object.assign(job.frozen_request, { profile_ids: null });
      },
    },
    {
      name: "too few frozen profile IDs",
      mutate: (job) => {
        job.frozen_request.profile_ids = [job.frozen_request.profile_ids[0]];
      },
    },
    {
      name: "invalid report contract",
      mutate: (job) => {
        job.report.artifact_type = "partial_report";
      },
    },
    {
      name: "report job identity mismatch",
      mutate: (job) => {
        job.report.job_id = "probe-other";
      },
    },
    {
      name: "report source file identity mismatch",
      mutate: (job) => {
        job.report.source.file_identity_sha256 = sha("8");
      },
    },
    {
      name: "missing report frozen profiles",
      mutate: (job) => {
        Object.assign(job.report, { frozen_profiles: null });
      },
    },
    {
      name: "report frozen profile identity mismatch",
      mutate: (job) => {
        job.report.frozen_profiles[0].profile_sha256 = sha("7");
      },
    },
    {
      name: "ready job with incomplete work",
      mutate: (job) => {
        job.progress.completed = 1;
      },
    },
    {
      name: "decode dimensions mismatch",
      mutate: (job) => {
        job.report.decode.width = 1;
      },
    },
    {
      name: "missing decode verified-frame array",
      mutate: (job) => {
        Object.assign(job.report.decode, { verified_frame_indices: null });
      },
    },
    {
      name: "decode verified-frame mismatch",
      mutate: (job) => {
        job.report.decode.verified_frame_indices = [8];
      },
    },
    {
      name: "report execution precision mismatch",
      mutate: (job) => {
        job.report.execution.precision = "fp16";
      },
    },
    {
      name: "missing artifact manifest",
      mutate: (job) => {
        Object.assign(job.report, { artifacts: null });
      },
    },
    {
      name: "artifact manifest below its evidence bound",
      mutate: (job) => {
        job.report.artifacts = job.report.artifacts.slice(0, 2);
      },
    },
    {
      name: "duplicate artifact ID",
      mutate: (job) => {
        job.report.artifacts.push(structuredClone(job.report.artifacts[0]));
      },
    },
    {
      name: "non-image artifact media type",
      mutate: (job) => {
        job.report.artifacts[0].media_type = "text/html";
      },
    },
    {
      name: "missing report frame array",
      mutate: (job) => {
        Object.assign(job.report, { frames: null });
      },
    },
    {
      name: "missing frame profile result array",
      mutate: (job) => {
        Object.assign(job.report.frames[0], { profile_results: null });
      },
    },
    {
      name: "frame source dimensions mismatch",
      mutate: (job) => {
        job.report.frames[0].source_width = 1;
      },
    },
    {
      name: "frame decode position mismatch",
      mutate: (job) => {
        job.report.frames[0].decoded_frame_position = 8;
      },
    },
    {
      name: "media-integrity dimensions mismatch",
      mutate: (job) => {
        job.report.frames[0].media_integrity.width = 1;
      },
    },
    {
      name: "media-integrity ratio outside zero-to-one",
      mutate: (job) => {
        job.report.frames[0].media_integrity.texture_tile_ratio = 2;
      },
    },
    {
      name: "overlay evidence manifest mismatch",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].raw_overlay_sha256 = sha("9");
      },
    },
    {
      name: "overlay artifact referenced twice",
      mutate: (job) => {
        const first = job.report.frames[0].profile_results[0];
        const second = job.report.frames[0].profile_results[1];
        second.raw_overlay_artifact_url = first.raw_overlay_artifact_url;
        second.raw_overlay_sha256 = first.raw_overlay_sha256;
      },
    },
    {
      name: "frame profile digest differs from frozen profile",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].profile_sha256 = sha("7");
      },
    },
    {
      name: "source frame manifest mismatch",
      mutate: (job) => {
        job.report.frames[0].source_frame_sha256 = sha("9");
      },
    },
    {
      name: "ready report without frame evidence",
      mutate: (job) => {
        job.report.frames = [];
      },
    },
    {
      name: "artifact manifest with unreferenced evidence",
      mutate: (job) => {
        job.report.artifacts.push({
          ...structuredClone(job.report.artifacts[0]),
          artifact_id: "extra-unreferenced-artifact",
          relative_path: "frames/extra.jpg",
        });
      },
    },
    {
      name: "report execution bundle mismatch",
      mutate: (job) => {
        job.report.lineage.execution_bundle.installed_runtime.torch = "2.8.0";
      },
    },
    {
      name: "report execution device mismatch",
      mutate: (job) => {
        job.report.execution.device = "cuda:0";
      },
    },
    {
      name: "report request digest mismatch",
      mutate: (job) => {
        job.report.request_sha256 = sha("7");
      },
    },
    {
      name: "report source digest mismatch",
      mutate: (job) => {
        job.report.source.sha256 = sha("7");
      },
    },
    {
      name: "report parent trial mismatch",
      mutate: (job) => {
        job.report.lineage.parent_trial_id = "trial-2";
      },
    },
    {
      name: "report effective config digest mismatch",
      mutate: (job) => {
        job.report.lineage.effective_config_sha256 = sha("8");
      },
    },
    {
      name: "report base config path mismatch",
      mutate: (job) => {
        job.report.lineage.base_config_relative_path = "config/other.yaml";
      },
    },
    {
      name: "report effective config path mismatch",
      mutate: (job) => {
        job.report.lineage.effective_config_relative_path =
          "config/generated/other.yaml";
      },
    },
    {
      name: "report trial intent digest mismatch",
      mutate: (job) => {
        job.report.lineage.trial_intent_sha256 = sha("8");
      },
    },
    {
      name: "report tuning binding mismatch",
      mutate: (job) => {
        job.report.lineage.tuning_patch_binding.values_sha256 = sha("8");
      },
    },
    {
      name: "report tuning binding digest mismatch",
      mutate: (job) => {
        job.report.lineage.tuning_patch_sha256 = sha("8");
      },
    },
    {
      name: "invalid absent tuning version lineage",
      mutate: (job) => {
        job.frozen_request.tuning_patch_binding.version_id = "unexpected-v1";
      },
    },
    {
      name: "absent tuning does not bind the canonical empty patch",
      mutate: (job) => {
        Object.assign(job.frozen_request.tuning_patch_binding, {
          state: "absent",
          version_id: null,
          parent_version_id: null,
          values_sha256: sha("9"),
        });
      },
    },
    {
      name: "unknown tuning binding state",
      mutate: (job) => {
        job.frozen_request.tuning_patch_binding.state = "unknown";
      },
    },
    {
      name: "report profile digest mismatch",
      mutate: (job) => {
        job.report.lineage.profile_sha256s["profile-a"] = sha("7");
      },
    },
    {
      name: "self-referential retry",
      mutate: (job) => {
        job.retry_from_job_id = "probe-1";
      },
    },
    {
      name: "duplicate report frame",
      mutate: (job) => {
        job.report.frames.push(structuredClone(job.report.frames[0]));
      },
    },
    {
      name: "duplicate frame profile",
      mutate: (job) => {
        job.report.frames[0].profile_results[1].profile_id = "profile-a";
      },
    },
    {
      name: "orphan frame profile",
      mutate: (job) => {
        job.report.frames[0].profile_results[1].profile_id = "profile-c";
      },
    },
    {
      name: "invalid profile digest",
      mutate: (job) => {
        job.report.frames[0].profile_results[0].profile_sha256 = "latest";
      },
    },
    {
      name: "uppercase request digest",
      mutate: (job) => {
        job.request_sha256 = sha("A");
      },
    },
    {
      name: "whitespace job ID",
      mutate: (job) => {
        job.job_id = " probe-1";
      },
    },
    {
      name: "ready without report",
      mutate: (job) => {
        Object.assign(job, { report: null });
      },
    },
    {
      name: "failed with partial report",
      mutate: (job) => {
        job.status = "failed";
      },
    },
  ])("rejects $name", ({ mutate }) => {
    const job = strictJobFixture();
    mutate(job);
    expect(() => detectorProbeJobView(job)).toThrow();
  });
});
