export const detectorProbeSha = (character: string) => character.repeat(64);

export const DETECTOR_PROBE_FRAME_INDICES = [10, 30] as const;
export const DETECTOR_PROBE_PROFILE_IDS = [
  "model-a-direct",
  "model-b-sahi",
] as const;

const EMPTY_JSON_SHA256 =
  "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a";

function licenses() {
  return Object.fromEntries(
    ["dataset", "model", "runtime", "deployment"].map((kind) => [
      kind,
      {
        name: `${kind} license`,
        spdx_id: "LicenseRef-Test",
        url: `https://example.invalid/licenses/${kind}`,
        reviewed: true,
        approved_for_local_probe: true,
      },
    ]),
  );
}

export function detectorProbeDescriptorFixture(
  modelId: string,
  descriptorSha256: string,
  weightsSha256: string,
) {
  return {
    schema_version: "1.0",
    artifact_type: "detector_model_descriptor",
    model_id: modelId,
    version: "1.0.0",
    model_version: "11.0.0",
    display_name: modelId,
    architecture_family: "yolo11",
    weights: {
      relative_path: `weights/${modelId}.pt`,
      sha256: weightsSha256,
      size_bytes: 1024,
    },
    source: {
      project: "Ultralytics assets",
      version: "v8.4.0",
      asset_release: "v8.4.0",
      weight_url: `https://example.invalid/${modelId}.pt`,
      acquisition_method: "pinned_local_asset",
      access_requirement: "pinned_local_file",
    },
    checkpoint: { format_version: "8.2.100", created_date: "2024-09-25" },
    runtime_contract: {
      ultralytics: ">=8.3.0,<9",
      sahi: ">=0.11.22,<1",
      torch: ">=2,<3",
    },
    class_names: ["sports ball"],
    class_map: { "sports ball": "ball" },
    expected_input: {
      direct_image_size: 1280,
      sahi_slice_width: 1280,
      sahi_slice_height: 720,
      source_coordinate_space: "source_pixels_xyxy",
    },
    execution: {
      device: "auto",
      precision: "fp32",
      memory_envelope: { max_ram_mb: 8192, max_vram_mb: 8192 },
    },
    licenses: licenses(),
    egress: {
      frames_leave_local_machine: false,
      destination: null,
      operator_consent: "not_required",
    },
    lifecycle_state: "unverified",
    bindings: {
      source_sha256: null,
      temporal_group_sha256: null,
      camera_profile_sha256: null,
      evaluation_package_sha256: null,
      threshold_profile_sha256: null,
      code_commit: null,
      environment_sha256: null,
    },
    descriptor_sha256: descriptorSha256,
  };
}

function profileFixture(
  profileId: string,
  modelId: string,
  descriptorSha256: string,
  profileSha256: string,
  mode: "direct" | "sahi",
) {
  const settings = {
    confidence_threshold: mode === "direct" ? 0.05 : 0.03,
    image_size: mode === "direct" ? 1280 : 1536,
    use_half: false,
    allowed_labels: ["sports ball"],
    top_k: 5,
    ...(mode === "sahi"
      ? {
          slice_width: 640,
          slice_height: 640,
          overlap_width_ratio: 0.2,
          overlap_height_ratio: 0.2,
          perform_standard_pred: false,
          postprocess_type: "NMS",
          postprocess_match_metric: "IOS",
          postprocess_match_threshold: 0.5,
        }
      : {}),
  };
  return {
    schema_version: "1.0",
    artifact_type: "detector_profile",
    profile_id: profileId,
    version: "profile-v1",
    model_id: modelId,
    model_version: "1.0.0",
    model_descriptor_sha256: descriptorSha256,
    profile_sha256: profileSha256,
    mode,
    settings,
    recommended: true,
    availability: {
      status: "available",
      reason_codes: [] as string[],
      runtime: {
        name: mode === "direct" ? "ultralytics" : "sahi",
        installed_version: mode === "direct" ? "8.4.31" : "0.11.36",
        load_smoke: true,
      },
    },
    selectable_for_probe: true,
  };
}

export function detectorProbeCatalogFixture() {
  const descriptors = [
    detectorProbeDescriptorFixture(
      "model-a",
      detectorProbeSha("a"),
      detectorProbeSha("b"),
    ),
    detectorProbeDescriptorFixture(
      "model-b",
      detectorProbeSha("c"),
      detectorProbeSha("d"),
    ),
  ];
  const models = descriptors.map((descriptor) => ({
    descriptor,
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
    qualification: {
      trial_eligible: false,
      source_segment_qualified: false,
      camera_qualified: false,
    },
    selectable_for_probe: true,
  }));
  return {
    schema_version: "1.0",
    artifact_type: "ball_detector_development_v1",
    models,
    profiles: [
      profileFixture(
        DETECTOR_PROBE_PROFILE_IDS[0],
        "model-a",
        detectorProbeSha("a"),
        detectorProbeSha("5"),
        "direct",
      ),
      profileFixture(
        DETECTOR_PROBE_PROFILE_IDS[1],
        "model-b",
        detectorProbeSha("c"),
        detectorProbeSha("6"),
        "sahi",
      ),
    ],
    catalog_findings: [] as Array<Record<string, unknown>>,
  };
}

export function detectorProbeImportedCatalogFixture() {
  const builtin = detectorProbeCatalogFixture();
  const builtinDescriptor = builtin.models[0].descriptor;
  const descriptor = {
    schema_version: "1.0",
    artifact_type: "detector_model_descriptor",
    model_id: "trusted-imported-ball-model",
    version: "trusted-v1",
    model_version: "trusted-v1",
    display_name: "Trusted imported ball model",
    architecture_family: "yolo11",
    weights: {
      relative_path:
        "data/ball_detector_development_v1/models/trusted-imported-ball-model/trusted-v1/weights/imported.pt",
      sha256: detectorProbeSha("7"),
      size_bytes: 2048,
    },
    source: {
      project: "Trusted camera detector",
      version: "trusted-v1",
      asset_release: "trusted-v1",
      weight_url: "trusted-import://server-lineage-package",
      acquisition_method: "server_lineage_package",
      access_requirement: "trusted_server_lineage_package",
    },
    runtime_contract: {
      validation: "server_validation_required",
      arbitrary_executable_model_code_allowed: false,
    },
    class_names: ["football"],
    class_map: { football: "ball" },
    expected_input: {
      image_size: 1920,
      precision: "fp32",
      device: "cpu",
      source_coordinate_space: "source_pixels_xyxy",
    },
    memory_envelope: { max_ram_mb: 16384, max_vram_mb: 0 },
    licenses: builtinDescriptor.licenses,
    egress: {
      frames_leave_local_machine: false,
      destination: null,
      operator_consent: "not_required",
    },
    lifecycle_state: "unverified",
    bindings: builtinDescriptor.bindings,
    import_manifest_sha256: detectorProbeSha("8"),
    descriptor_sha256: detectorProbeSha("9"),
  };
  return {
    schema_version: "1.0",
    artifact_type: "ball_detector_development_v1",
    models: [
      {
        descriptor,
        availability: {
          status: "blocked",
          reason_codes: ["server_validation_required"],
          observations: {
            file: { status: "pass", reason: "content_addressed_import_copy" },
            digest: { status: "pass", reason: "import_digest_verified" },
            class_map: {
              status: "not_run",
              reason: "checkpoint_class_map_check_required",
            },
            license: {
              status: "pass",
              reason: "four_layer_license_metadata_complete",
            },
            runtime_load: {
              status: "not_run",
              reason: "server_validation_required",
              installed_runtime: {
                ultralytics: null,
                sahi: null,
                torch: null,
              },
            },
          },
        },
        qualification: {
          trial_eligible: false,
          source_segment_qualified: false,
          camera_qualified: false,
        },
        selectable_for_probe: false,
      },
    ],
    profiles: [] as Array<Record<string, unknown>>,
    catalog_findings: [] as Array<Record<string, unknown>>,
  };
}

function frozenProfiles() {
  const catalog = detectorProbeCatalogFixture();
  return catalog.profiles.map((profile, index) => ({
    ...profile,
    model_descriptor: catalog.models[index].descriptor,
  }));
}

function profileBinding(profile: ReturnType<typeof frozenProfiles>[number]) {
  return {
    profile_id: profile.profile_id,
    profile_sha256: profile.profile_sha256,
    model_id: profile.model_id,
    model_version: profile.model_version,
    model_descriptor_sha256: profile.model_descriptor_sha256,
    weights_sha256: profile.model_descriptor.weights.sha256,
    weights_size_bytes: profile.model_descriptor.weights.size_bytes,
  };
}

export function detectorProbeJobFixture(
  jobId: string,
  status:
    | "queued"
    | "running"
    | "committing"
    | "ready"
    | "failed"
    | "cancelled"
    | "blocked",
  retryFromJobId: string | null = null,
  options: { frameIndices?: number[]; zeroCandidates?: boolean } = {},
) {
  const profileIds = [...DETECTOR_PROBE_PROFILE_IDS];
  const frameIndices = options.frameIndices ?? [
    ...DETECTOR_PROBE_FRAME_INDICES,
  ];
  const profiles = frozenProfiles();
  const requestSha256 = detectorProbeSha("e");
  const intentSha256 = detectorProbeSha("f");
  const now = "2026-07-18T12:00:00Z";
  const tuningBinding = {
    state: "absent",
    schema_version: "1.0",
    version_id: null,
    parent_version_id: null,
    values_sha256: EMPTY_JSON_SHA256,
  };
  const frozenProfilesSha256 = detectorProbeSha("d");
  const executionEnvironment = {
    device: "cpu",
    precision: "fp32",
    cuda_available: false,
    cuda_device_count: 0,
    cuda_visible_devices: null,
    cuda_compiled_version: null,
    cudnn_version: null,
    gpu_name: null,
    gpu_compute_capability: null,
    gpu_total_memory_bytes: null,
    cuda_driver_version: null,
    python_implementation: "CPython",
    python_version: "3.11.9",
    numpy_version: "2.1.0",
    opencv_version: "4.10.0",
    pydantic_version: "2.11.7",
    pydantic_core_version: "2.33.2",
    opencv_build_information_sha256: detectorProbeSha("5"),
    opencv_ffmpeg_enabled: true,
    decoder_fingerprint_sha256: detectorProbeSha("6"),
  };
  const executionBundle = {
    schema_version: "1.0",
    installed_runtime: {
      sahi: "0.11.36",
      torch: "2.7.1+cpu",
      ultralytics: "8.4.31",
    },
    runtime_contract: {
      sahi: ">=0.11.22,<1",
      torch: ">=2,<3",
      ultralytics: ">=8.3.0,<9",
    },
    runtime_contract_sha256: detectorProbeSha("1"),
    runtime_observation_evidence_sha256s: Object.fromEntries(
      profileIds.map((profileId, index) => [
        profileId,
        detectorProbeSha(index === 0 ? "2" : "3"),
      ]),
    ),
    execution_environment: executionEnvironment,
    runtime_environment_sha256: detectorProbeSha("4"),
    code_bundle_files: Object.fromEntries(
      [
        "__init__.py",
        "ai_contracts.py",
        "ai_improvement_prompt_contract.py",
        "api/__init__.py",
        "api/schemas.py",
        "candidate_dataset.py",
        "config.py",
        "detector.py",
        "detector_candidate_contract.py",
        "detector_development_common.py",
        "detector_model_registry.py",
        "detector_probe.py",
        "detector_probe_runner.py",
        "detector_probe_worker.py",
        "media_integrity.py",
        "tracking_contracts.py",
        "types.py",
      ].map((name, index) => [
        `football_tracking/${name}`,
        detectorProbeSha("abcdef0123456789a"[index]),
      ]),
    ),
    code_bundle_sha256: detectorProbeSha("5"),
    code_commit: null,
    code_commit_status: "unavailable",
    code_commit_reason: "repository_commit_unavailable",
    frozen_profiles_sha256: frozenProfilesSha256,
  };
  const executionBundleSha256 = detectorProbeSha("6");
  const runtimeEnvironmentSha256 = executionBundle.runtime_environment_sha256;
  const frozenRequest = {
    parent_trial_id: "trial-1",
    source_id: "source-1",
    source_relative_path: "data/source.mp4",
    source_sha256: detectorProbeSha("1"),
    source_file_identity_sha256: detectorProbeSha("7"),
    source_size_bytes: 50_000,
    source_width: 5120,
    source_height: 1440,
    source_frame_count: 300,
    tracking_contract_relative_path: "outputs/trial-1/tracking-contract.json",
    tracking_contract_sha256: detectorProbeSha("2"),
    base_config_relative_path: "config/base.yaml",
    base_config_sha256: detectorProbeSha("3"),
    effective_config_relative_path: "config/generated/trial.yaml",
    effective_config_sha256: detectorProbeSha("4"),
    trial_intent_sha256: detectorProbeSha("9"),
    tuning_patch_binding: tuningBinding,
    tuning_patch_sha256: detectorProbeSha("0"),
    profile_ids: profileIds,
    frozen_profiles_sha256: frozenProfilesSha256,
    profile_sha256s: Object.fromEntries(
      profiles.map((profile) => [profile.profile_id, profile.profile_sha256]),
    ),
    profile_bindings: profiles.map(profileBinding),
    execution_bundle: executionBundle,
    execution_bundle_sha256: executionBundleSha256,
    runtime_environment_sha256: runtimeEnvironmentSha256,
    frame_indices: frameIndices,
    top_k: 5,
    requested_decode_mode: "preroll",
    ...(retryFromJobId ? { retry_from_job_id: retryFromJobId } : {}),
  };
  const artifactFor = (
    artifactId: string,
    relativePath: string,
    digest: string,
    sizeBytes: number,
  ) => ({
    artifact_id: artifactId,
    relative_path: relativePath,
    sha256: digest,
    size_bytes: sizeBytes,
    media_type: "image/jpeg",
    width: 5120,
    height: 1440,
  });
  const artifacts = frameIndices.flatMap((frameIndex, frameOffset) => {
    const sourceId = `source-${frameIndex}`;
    return [
      artifactFor(
        sourceId,
        `frames/${frameIndex}.jpg`,
        detectorProbeSha(frameOffset === 0 ? "a" : "b"),
        100,
      ),
      ...profiles.map((profile, profileOffset) => {
        const artifactId = `overlay-${frameIndex}-${profile.profile_id}`;
        return artifactFor(
          artifactId,
          `overlays/${frameIndex}-${profile.profile_id}.jpg`,
          detectorProbeSha(profileOffset === 0 ? "7" : "8"),
          90,
        );
      }),
    ];
  });
  const frames = frameIndices.map((frameIndex, frameOffset) => ({
    frame_index: frameIndex,
    source_width: 5120,
    source_height: 1440,
    requested_decode_mode: "preroll",
    effective_decode_mode: "preroll_verified",
    decoded_frame_position: frameIndex,
    media_integrity: {
      path: null,
      status: "ok",
      width: 5120,
      height: 1440,
      mean_luma: 80,
      std_luma: 10,
      texture_tile_ratio: 0.5,
      dominant_color_ratio: 0.5,
      gray: false,
      low_information: false,
      likely_corrupt: false,
      reasons: [] as string[],
    },
    source_artifact_url: `/api/v1/detector-probes/${jobId}/artifacts/source-${frameIndex}`,
    source_frame_sha256: detectorProbeSha(frameOffset === 0 ? "a" : "b"),
    source_frame_size_bytes: 100,
    profile_results: profiles.map((profile, profileOffset) => ({
      profile_id: profile.profile_id,
      profile_sha256: profile.profile_sha256,
      status: "completed",
      latency_ms: 2.5,
      candidate_count: options.zeroCandidates === false ? 1 : 0,
      top_k: 5,
      raw_candidates:
        options.zeroCandidates === false
          ? [
              {
                frame_index: frameIndex,
                bbox_source_px: [10, 20, 18, 28],
                confidence: 0.8,
                class_name: "ball",
                checkpoint_class_name: "sports ball",
                source: profile.mode === "direct" ? "yolo_direct" : "yolo_sahi",
                coordinate_reason:
                  profile.mode === "direct"
                    ? "direct_source_coordinates"
                    : "sahi_tile_offset_applied",
                merge_reason: "retained_top_k",
              },
            ]
          : [],
      display_candidate:
        options.zeroCandidates === false
          ? {
              frame_index: frameIndex,
              bbox_source_px: [10, 20, 18, 28],
              confidence: 0.8,
              class_name: "ball",
              checkpoint_class_name: "sports ball",
              source: profile.mode === "direct" ? "yolo_direct" : "yolo_sahi",
              coordinate_reason:
                profile.mode === "direct"
                  ? "direct_source_coordinates"
                  : "sahi_tile_offset_applied",
              merge_reason: "retained_top_k",
            }
          : null,
      filter_reasons: {},
      failure_code: null,
      raw_overlay_artifact_url: `/api/v1/detector-probes/${jobId}/artifacts/overlay-${frameIndex}-${profile.profile_id}`,
      raw_overlay_sha256: detectorProbeSha(profileOffset === 0 ? "7" : "8"),
      raw_overlay_size_bytes: 90,
    })),
  }));
  const report =
    status === "ready"
      ? {
          schema_version: "1.0",
          artifact_type: "detector_probe_report",
          job_id: jobId,
          request_sha256: requestSha256,
          source: {
            source_id: "source-1",
            relative_path: "data/source.mp4",
            sha256: detectorProbeSha("1"),
            file_identity_sha256: detectorProbeSha("7"),
            size_bytes: 50_000,
            width: 5120,
            height: 1440,
            frame_count: 300,
            tracking_contract_relative_path:
              "outputs/trial-1/tracking-contract.json",
            tracking_contract_sha256: detectorProbeSha("2"),
          },
          lineage: {
            parent_trial_id: "trial-1",
            base_config_relative_path: "config/base.yaml",
            base_config_sha256: detectorProbeSha("3"),
            effective_config_relative_path: "config/generated/trial.yaml",
            effective_config_sha256: detectorProbeSha("4"),
            trial_intent_sha256: detectorProbeSha("9"),
            tuning_patch_binding: tuningBinding,
            tuning_patch_sha256: detectorProbeSha("0"),
            profile_sha256s: frozenRequest.profile_sha256s,
            frozen_profiles_sha256: frozenProfilesSha256,
            execution_bundle: structuredClone(executionBundle),
            execution_bundle_sha256: executionBundleSha256,
            runtime_environment_sha256: runtimeEnvironmentSha256,
            intent_sha256: intentSha256,
            retry_from_job_id: retryFromJobId,
          },
          frozen_profiles: profiles,
          top_k: 5,
          frames,
          decode: {
            width: 5120,
            height: 1440,
            frame_count: 300,
            fps: 30,
            requested_decode_mode: "preroll",
            effective_decode_mode: "preroll_verified",
            verified_frame_indices: frameIndices,
            position_verification:
              "opencv_next_frame_index_with_0.25_tolerance",
          },
          execution: { device: "cpu", precision: "fp32" },
          artifacts,
          created_at: now,
          report_sha256: detectorProbeSha("c"),
        }
      : null;
  return {
    schema_version: "1.0",
    artifact_type: "detector_probe_job",
    job_id: jobId,
    idempotency_key: requestSha256,
    request_sha256: requestSha256,
    intent_sha256: intentSha256,
    frozen_profiles_sha256: frozenProfilesSha256,
    status,
    stage: status,
    progress: {
      completed: status === "ready" ? frameIndices.length * profiles.length : 0,
      total: frameIndices.length * profiles.length,
      updated_at: now,
    },
    frozen_request: frozenRequest,
    frozen_profiles: profiles,
    retry_from_job_id: retryFromJobId,
    error_code: status === "cancelled" ? "cancelled_by_operator" : null,
    blocker_code: status === "blocked" ? "runtime_blocked" : null,
    recovery_action:
      status === "cancelled" || status === "blocked"
        ? "Retry explicitly."
        : null,
    report,
    result_manifest_sha256: status === "ready" ? detectorProbeSha("9") : null,
    created_at: now,
    updated_at: now,
    status_url: `/api/v1/detector-probes/${jobId}`,
    cancel_url: `/api/v1/detector-probes/${jobId}/cancel`,
    can_cancel: status === "queued" || status === "running",
  };
}
