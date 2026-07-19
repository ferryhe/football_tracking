import { pythonCanonicalSha256Sync } from "../lib/canonicalSha256";

const metricProfileSha256 =
  "50320c9d6186d844e5f533193f3cc767bed9682a5c0c2c42ab17ccbf59169595";

const sha = (value: string) => {
  let digest = 2166136261;
  for (const character of value) {
    digest = Math.imul(digest ^ character.charCodeAt(0), 16777619) >>> 0;
  }
  return digest.toString(16).padStart(8, "0").repeat(8);
};

const canonicalSha = (value: unknown, floatPaths: readonly string[] = []) =>
  pythonCanonicalSha256Sync(value, floatPaths);

const annotationFloatPaths = [
  "$.point_source_px.x",
  "$.point_source_px.y",
  "$.bbox_source_px.left",
  "$.bbox_source_px.top",
  "$.bbox_source_px.right",
  "$.bbox_source_px.bottom",
];

const nestedAnnotationFloatPaths = (root: string) =>
  annotationFloatPaths.map((path) => path.replace("$", root));

const timingFloatPaths = (root: string) => [
  `${root}.fps`,
  `${root}.decoder_reported_pos_msec`,
  `${root}.decoder_time_seconds`,
  `${root}.display_time_seconds`,
  `${root}.true_presentation_timestamp.value_seconds`,
];

const revisionFloatPaths = (revisions: readonly unknown[], root = "$") =>
  revisions.flatMap((_, index) => [
    ...nestedAnnotationFloatPaths(
      `${root}[${index}].previous_effective_annotation`,
    ),
    ...nestedAnnotationFloatPaths(`${root}[${index}].effective_annotation`),
  ]);

export function ballAnnotationSessionFixture({
  sessionId,
  jobId = "probe-e2e-ready",
  profileId,
  frameIndices = [10, 20, 30, 40, 50, 60],
  frameSha256 = sha("jpeg"),
  frameSizeBytes = 4,
  sourceWidth = 64,
  sourceHeight = 36,
}: {
  sessionId?: string;
  jobId?: string;
  profileId: string;
  frameIndices?: number[];
  frameSha256?: string;
  frameSizeBytes?: number;
  sourceWidth?: number;
  sourceHeight?: number;
}) {
  const sourceSha256 = sha("e2e-source");
  const profileSha256 = sha(`profile:${profileId}`);
  const groups = frameIndices.map((frameIndex) => {
    const groupId = sha(`group:${frameIndex}`);
    return {
      group_id: groupId,
      profile_id: "tiny_ball_temporal_groups_v1",
      source_sha256: sourceSha256,
      seed_frame_index: frameIndex,
      start_frame: Math.max(0, frameIndex - 30),
      end_frame: frameIndex + 30,
      derivative_family: [Math.max(0, frameIndex - 30), frameIndex + 30],
      canonical_moment_id: sha(`moment:${frameIndex}`),
      derivative_family_id: groupId,
      ancestry_profile: "source-proxy-crop-tile-propagation-closure-v1",
      frame_index: frameIndex,
      pre_reveal_lighting_stratum: null,
    };
  });
  const evidenceRows = (dimension: string, strata: readonly string[]) =>
    strata.map((stratum) => ({
      stratum,
      status: "applicable",
      evidence: {
        declared_before_reveal: true,
        note: `${stratum} exists in this source`,
        evidence_sha256: sha(`${dimension}:${stratum}:applicable`),
      },
      ...(dimension === "lighting" ? { quota: 0, frame_intervals: [] } : {}),
    }));
  const samplingManifest = {
    schema_version: "1.0",
    artifact_type: "ball_annotation_sampling_manifest",
    profile_id: "tiny_ball_temporal_groups_v1",
    selection_profile_id: "development_probe_frames_v1",
    scale_stratification_mode: "post_reveal_support_gate_only",
    lighting_stratification_mode: "not_applicable_development_evidence",
    selection_seed_sha256: sha("selection-seed"),
    candidate_universe_sha256: sha("candidate-universe"),
    candidate_universe_start_frame: 0,
    candidate_universe_end_frame: 99,
    selection_authority: null,
    candidate_universe_authority: null,
    metric_profile_id: "tiny_ball_feasibility_metric_v1",
    metric_profile_sha256: metricProfileSha256,
    data_role: "development",
    target_frame_count: frameIndices.length,
    frame_indices: frameIndices,
    groups,
    excluded_development_groups: [],
    locked_before_probe: false,
    source_sha256: sourceSha256,
    locked_profile_id: profileId,
    locked_profile_sha256: profileSha256,
    strata_applicability: {
      scale: evidenceRows("scale", ["near", "mid", "far"]),
      lighting: evidenceRows("lighting", [
        "bright_sun",
        "shadow",
        "backlight",
        "twilight",
        "artificial_light",
      ]),
    },
    manifest_sha256: "",
  };
  const canonicalSamplingManifest = { ...samplingManifest };
  delete (canonicalSamplingManifest as Partial<typeof samplingManifest>)
    .manifest_sha256;
  delete (canonicalSamplingManifest as Partial<typeof samplingManifest>)
    .selection_authority;
  delete (canonicalSamplingManifest as Partial<typeof samplingManifest>)
    .candidate_universe_authority;
  canonicalSamplingManifest.groups = canonicalSamplingManifest.groups.map(
    ({ pre_reveal_lighting_stratum: _lighting, ...group }) => group as any,
  );
  samplingManifest.manifest_sha256 = canonicalSha(canonicalSamplingManifest);
  const source = {
    source_id: "source-e2e",
    sha256: sourceSha256,
    file_identity_sha256: sha("source-identity"),
    size_bytes: 4096,
    width: sourceWidth,
    height: sourceHeight,
    frame_count: 100,
    tracking_contract_sha256: sha("tracking-contract"),
    relative_path: "data/match-a.mp4",
    tracking_contract_relative_path: "data/tracking-contract.json",
    fps: 20,
  };
  const lockedProfile = {
    profile_id: profileId,
    profile_sha256: profileSha256,
    model_id: "yolo11s",
    model_version: "11.0",
    model_descriptor_sha256: sha("model-descriptor"),
    weights_sha256: sha("weights"),
  };
  const controlProfile = {
    profile_id: "current-coco-yolov8n-direct",
    profile_sha256: sha("control-profile"),
    model_id: "yolov8n",
    model_version: "8.0",
    model_descriptor_sha256: sha("control-model-descriptor"),
    weights_sha256: sha("control-weights"),
  };
  const frozenProfiles = [controlProfile, lockedProfile].map((profile) => ({
    profile_id: profile.profile_id,
    profile_sha256: profile.profile_sha256,
    model_id: profile.model_id,
    model_version: profile.model_version,
    model_descriptor_sha256: profile.model_descriptor_sha256,
    model_descriptor: {
      weights: { sha256: profile.weights_sha256, size_bytes: 7 },
    },
  }));
  const operatorId = "local-operator";
  const normalizedRequest = {
    data_role: "development",
    development_probe_job_ids: [jobId],
    locked_profile_id: profileId,
    target_frame_count: null,
    sampling_profile_id: "tiny_ball_temporal_groups_v1",
    metric_profile_id: "tiny_ball_feasibility_metric_v1",
    operator_id: operatorId,
    strata_applicability: samplingManifest.strata_applicability,
    applicable_scale_strata: ["near", "mid", "far"],
    applicable_lighting_strata: [
      "bright_sun",
      "shadow",
      "backlight",
      "twilight",
      "artificial_light",
    ],
    retry_from_session_id: null,
    development_package_session_id: null,
    development_package_sha256: null,
  };
  const requestSha256 = canonicalSha(normalizedRequest);
  const resolvedSessionId =
    sessionId ?? `annotation-${requestSha256.slice(0, 16)}-000000000000`;
  const session: any = {
    schema_version: "1.0",
    artifact_type: "ball_annotation_session",
    session_id: resolvedSessionId,
    idempotency_key: sha("idempotency"),
    request_sha256: requestSha256,
    data_role: "development",
    status: "annotating",
    stage: "annotating",
    source,
    lineage: {
      parent_trial_id: "trial-e2e",
      development_probe_job_ids: [jobId],
      development_probe_report_sha256s: { [jobId]: sha("probe-report") },
      development_probe_result_manifest_sha256s: {
        [jobId]: sha("probe-result-manifest"),
      },
      development_probe_execution_bundle_sha256s: {
        [jobId]: sha("probe-execution-bundle"),
      },
      development_probe_frozen_profiles_sha256s: {
        [jobId]: canonicalSha(frozenProfiles),
      },
      decode: {
        width: sourceWidth,
        height: sourceHeight,
        frame_count: 100,
        fps: 20,
        requested_decode_mode: "sequential",
        effective_decode_mode: "sequential",
        position_verification: "opencv_next_frame_index_with_0.25_tolerance",
      },
      runtime_environment_sha256: sha("runtime"),
    },
    locked_profile: lockedProfile,
    control_profile_id: controlProfile.profile_id,
    control_profile: controlProfile,
    sampling_profile_id: "tiny_ball_temporal_groups_v1",
    metric_profile_id: "tiny_ball_feasibility_metric_v1",
    metric_profile_sha256: metricProfileSha256,
    sampling_manifest: samplingManifest,
    operator_id: operatorId,
    applicable_scale_strata: ["near", "mid", "far"],
    applicable_lighting_strata: [
      "bright_sun",
      "shadow",
      "backlight",
      "twilight",
      "artificial_light",
    ],
    retry_from_session_id: null,
    retry_lineage: null,
    attempt_family_sha256: sha("attempt-family"),
    development_package_binding: null,
    check_probe_job_id: null,
    check_probe_authority: null,
    frames: frameIndices.map((frameIndex, index) => ({
      frame_index: frameIndex,
      source_frame_sha256: frameSha256,
      source_frame_size_bytes: frameSizeBytes,
      suggested_candidates: [],
      source_timing_status: "observed",
      decoder_reported_pos_msec: (frameIndex / 20) * 1_000,
      decoder_time_seconds: frameIndex / 20,
      display_time_seconds: frameIndex / 20,
      true_presentation_timestamp: {
        status: "not_collected",
        value_seconds: null,
        method: null,
      },
      proxy_binding: null,
      temporal_group_id: groups[index].group_id,
      frame_url: `/api/v1/ball-annotation-sessions/${resolvedSessionId}/frames/${frameIndex}`,
      annotation_revision: 0,
      annotation_etag: sha(`etag:${frameIndex}:0`),
      current_annotation: null,
      frame_role: "primary_sample",
      primary_sample: true,
      propagation_job_ids: [],
      propagation_suggestions: [],
    })),
    final_package: null,
    error_code: null,
    blocker_code: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:00:00Z",
  };
  refreshBallAnnotationProgress(session);
  return session;
}

export function refreshBallAnnotationProgress(session: any) {
  const primary = session.frames.filter((frame: any) => frame.primary_sample);
  const supplemental = session.frames.filter(
    (frame: any) => frame.frame_role === "propagation_target",
  );
  session.progress = {
    annotated_frames: session.frames.filter(
      (frame: any) => frame.current_annotation !== null,
    ).length,
    total_frames: session.frames.length,
    unconfirmed_suggestions: session.frames.reduce(
      (total: number, frame: any) =>
        total +
        frame.suggested_candidates.filter(
          (candidate: any) => candidate.decision === "pending",
        ).length,
      0,
    ),
    primary_annotated_frames: primary.filter(
      (frame: any) => frame.current_annotation !== null,
    ).length,
    primary_total_frames: primary.length,
    supplemental_annotated_frames: supplemental.filter(
      (frame: any) => frame.current_annotation !== null,
    ).length,
    supplemental_total_frames: supplemental.length,
    unconfirmed_propagation_suggestions: session.frames.reduce(
      (total: number, frame: any) =>
        total +
        frame.propagation_suggestions.filter(
          (suggestion: any) => suggestion.pending_human_confirmation,
        ).length,
      0,
    ),
  };
  return session;
}

function frozenDetectorProfiles(session: any) {
  return [session.control_profile, session.locked_profile].map(
    (profile: any) => ({
      profile_id: profile.profile_id,
      profile_sha256: profile.profile_sha256,
      model_id: profile.model_id,
      model_version: profile.model_version,
      model_descriptor_sha256: profile.model_descriptor_sha256,
      model_descriptor: {
        weights: { sha256: profile.weights_sha256, size_bytes: 7 },
      },
    }),
  );
}

function detectorProbeAuthority(session: any) {
  const jobId = session.lineage.development_probe_job_ids[0];
  const frozenProfiles = frozenDetectorProfiles(session);
  const frozenRequest: Record<string, unknown> = {
    annotation_sampling_manifest_sha256: null,
    base_config_relative_path: "config/base.yaml",
    base_config_sha256: sha("base-config"),
    effective_config_relative_path: "config/effective.yaml",
    effective_config_sha256: sha("effective-config"),
    execution_bundle: { fixture: "browser" },
    execution_bundle_sha256:
      session.lineage.development_probe_execution_bundle_sha256s[jobId],
    frame_indices: session.sampling_manifest.frame_indices,
    frozen_profiles_sha256: canonicalSha(frozenProfiles),
    parent_trial_id: session.lineage.parent_trial_id,
    profile_bindings: frozenProfiles.map((profile: any) => ({
      profile_id: profile.profile_id,
      profile_sha256: profile.profile_sha256,
      model_id: profile.model_id,
      model_version: profile.model_version,
      model_descriptor_sha256: profile.model_descriptor_sha256,
      weights_sha256: profile.model_descriptor.weights.sha256,
      weights_size_bytes: profile.model_descriptor.weights.size_bytes,
    })),
    profile_ids: frozenProfiles.map((profile: any) => profile.profile_id),
    profile_sha256s: Object.fromEntries(
      frozenProfiles.map((profile: any) => [
        profile.profile_id,
        profile.profile_sha256,
      ]),
    ),
    requested_decode_mode: session.lineage.decode.requested_decode_mode,
    retry_from_job_id: null,
    runtime_environment_sha256: session.lineage.runtime_environment_sha256,
    source_file_identity_sha256: session.source.file_identity_sha256,
    source_frame_count: session.source.frame_count,
    source_height: session.source.height,
    source_id: session.source.source_id,
    source_relative_path: session.source.relative_path,
    source_sha256: session.source.sha256,
    source_size_bytes: session.source.size_bytes,
    source_width: session.source.width,
    top_k: 5,
    tracking_contract_relative_path:
      session.source.tracking_contract_relative_path,
    tracking_contract_sha256: session.source.tracking_contract_sha256,
    trial_intent_sha256: sha("trial-intent"),
    tuning_patch_binding: {
      schema_version: "1.0",
      state: "absent",
      version_id: null,
      parent_version_id: null,
      values_sha256: sha("tuning-values"),
    },
    tuning_patch_sha256: sha("tuning-patch"),
  };
  const requestSha256 = canonicalSha(frozenRequest);
  const intentRequest = { ...frozenRequest };
  delete intentRequest.retry_from_job_id;
  const intentSha256 = canonicalSha(intentRequest);
  const resource = Object.fromEntries(
    [
      "parent_trial_id",
      "source_id",
      "source_sha256",
      "source_file_identity_sha256",
      "tracking_contract_sha256",
      "base_config_relative_path",
      "base_config_sha256",
      "effective_config_relative_path",
      "effective_config_sha256",
      "trial_intent_sha256",
      "tuning_patch_sha256",
    ].map((field) => [field, frozenRequest[field]]),
  );
  const semanticIntentSha256 = sha("semantic-intent");
  const reportSha256 = session.lineage.development_probe_report_sha256s[jobId];
  const resultManifestSha256 =
    session.lineage.development_probe_result_manifest_sha256s[jobId];
  const artifacts = session.frames.map((frame: any) => ({
    artifact_id: `source-${frame.frame_index.toString().padStart(9, "0")}`,
    media_type: "image/jpeg",
    sha256: frame.source_frame_sha256,
    size_bytes: frame.source_frame_size_bytes,
    width: session.source.width,
    height: session.source.height,
  }));
  const frames = session.frames.map((frame: any) => ({
    frame_index: frame.frame_index,
    source_artifact_url: `/api/v1/detector-probes/${jobId}/artifacts/source-${frame.frame_index
      .toString()
      .padStart(9, "0")}`,
    source_frame_sha256: frame.source_frame_sha256,
    source_frame_size_bytes: frame.source_frame_size_bytes,
    source_width: session.source.width,
    source_height: session.source.height,
    profile_results: [
      {
        profile_id: session.locked_profile.profile_id,
        profile_sha256: session.locked_profile.profile_sha256,
        status: "completed",
        top_k: 5,
        candidate_count: 0,
        filter_reasons: {},
        raw_candidates: [],
        display_candidate: null,
      },
    ],
  }));
  const report = {
    schema_version: "1.0",
    artifact_type: "detector_probe_report",
    job_id: jobId,
    request_sha256: requestSha256,
    source: {
      file_identity_sha256: session.source.file_identity_sha256,
    },
    lineage: {
      intent_sha256: intentSha256,
      semantic_intent_sha256: semanticIntentSha256,
      frozen_profiles_sha256: canonicalSha(frozenProfiles),
      execution_bundle_sha256:
        session.lineage.development_probe_execution_bundle_sha256s[jobId],
      runtime_environment_sha256: session.lineage.runtime_environment_sha256,
    },
    frozen_profiles: frozenProfiles,
    artifacts,
    frames,
    report_sha256: reportSha256,
  };
  const resultManifest = {
    schema_version: "1.0",
    artifact_type: "detector_probe_result_manifest",
    job_id: jobId,
    request_sha256: requestSha256,
    frozen_profiles_sha256: canonicalSha(frozenProfiles),
    execution_bundle_sha256:
      session.lineage.development_probe_execution_bundle_sha256s[jobId],
    runtime_environment_sha256: session.lineage.runtime_environment_sha256,
    source_file_identity_sha256: session.source.file_identity_sha256,
    report_content_sha256: reportSha256,
    artifacts,
    report_file_sha256: sha("report-file"),
    report_file_size_bytes: 1,
  };
  const jobRecord = {
    schema_version: "1.0",
    artifact_type: "detector_probe_job",
    status: "ready",
    job_id: jobId,
    request_sha256: requestSha256,
    intent_sha256: intentSha256,
    semantic_intent_sha256: semanticIntentSha256,
    retry_from_job_id: null,
    result_manifest_sha256: resultManifestSha256,
    frozen_request: frozenRequest,
    frozen_profiles: frozenProfiles,
    report,
  };
  const authorityBody = {
    schema_version: "1.0",
    artifact_type: "detector_probe_job_authority",
    job_id: jobId,
    request_sha256: requestSha256,
    intent_sha256: intentSha256,
    semantic_intent_sha256: semanticIntentSha256,
    resource_sha256: canonicalSha(resource),
    frozen_profiles_sha256: canonicalSha(frozenProfiles),
    execution_bundle_sha256:
      session.lineage.development_probe_execution_bundle_sha256s[jobId],
    runtime_environment_sha256: session.lineage.runtime_environment_sha256,
    retry_from_job_id: null,
    retry_kind: null,
    frozen_request: frozenRequest,
    frozen_profiles: frozenProfiles,
    probe_report_sha256: reportSha256,
    probe_result_manifest_sha256: resultManifestSha256,
    probe_report: report,
    probe_result_manifest: resultManifest,
    probe_job_record: jobRecord,
    canonical_job_record_sha256: canonicalSha(jobRecord),
    audit_anchor_kind: "embedded_job_record",
  };
  return {
    ...authorityBody,
    job_record_authority_sha256: canonicalSha(authorityBody),
  };
}

export function developmentFinalResultFixture(session: any) {
  const detectorAuthority = detectorProbeAuthority(session);
  const normalizedRequest = {
    data_role: "development",
    development_probe_job_ids: [
      ...session.lineage.development_probe_job_ids,
    ].sort(),
    locked_profile_id: session.locked_profile.profile_id,
    target_frame_count: null,
    sampling_profile_id: session.sampling_profile_id,
    metric_profile_id: session.metric_profile_id,
    operator_id: session.operator_id,
    strata_applicability: session.sampling_manifest.strata_applicability,
    applicable_scale_strata: session.applicable_scale_strata,
    applicable_lighting_strata: session.applicable_lighting_strata,
    retry_from_session_id: session.retry_from_session_id,
    development_package_session_id: null,
    development_package_sha256: null,
  };
  const requestAuthorityBody = {
    schema_version: "1.0",
    artifact_type: "ball_annotation_session_request_authority",
    session_id: session.session_id,
    request_sha256: canonicalSha(normalizedRequest),
    normalized_request: normalizedRequest,
  };
  const sessionRequestAuthority = {
    ...requestAuthorityBody,
    authority_sha256: canonicalSha(requestAuthorityBody),
  };
  const effectiveAnnotations = session.frames.map((frame: any) => ({
    frame_index: frame.frame_index,
    ...frame.current_annotation,
  }));
  const revisionChain = session.frames.map((frame: any, index: number) => {
    const mutationId = `fixture-mutation-${index}`;
    const mutationRequest = {
      mutation_id: mutationId,
      expected_revision: 0,
      operation: "set",
      undo_revision: null,
      annotation: frame.current_annotation,
      suggestion_kind: null,
      suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
    };
    const revisionId = `revision-${canonicalSha({
      session_id: session.session_id,
      frame_index: frame.frame_index,
      revision: 1,
    }).slice(0, 24)}`;
    const annotationEtag = canonicalSha(
      {
        schema_version: "1.0",
        artifact_type: "ball_annotation_effective_revision",
        session_id: session.session_id,
        frame_index: frame.frame_index,
        revision: 1,
        effective_annotation: frame.current_annotation,
      },
      nestedAnnotationFloatPaths("$.effective_annotation"),
    );
    return {
      schema_version: "1.0",
      artifact_type: "ball_annotation_revision",
      revision_id: revisionId,
      session_id: session.session_id,
      frame_index: frame.frame_index,
      revision: 1,
      operation: "set",
      mutation_id: mutationId,
      mutation_sha256: canonicalSha(
        {
          session_id: session.session_id,
          frame_index: frame.frame_index,
          request: mutationRequest,
        },
        nestedAnnotationFloatPaths("$.request.annotation"),
      ),
      expected_revision: 0,
      supersedes_revision: null,
      undo_revision: null,
      accepted_suggestion_kind: null,
      accepted_suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
      previous_effective_annotation: null,
      effective_annotation: frame.current_annotation,
      operator_id: session.operator_id,
      annotation_etag: annotationEtag,
      created_at: "2026-07-18T00:09:00Z",
    };
  });
  const frameMedia = session.frames.map((frame: any) => ({
    frame_index: frame.frame_index,
    relative_path: `frames/${frame.frame_index
      .toString()
      .padStart(9, "0")}.jpg`,
    sha256: frame.source_frame_sha256,
    size_bytes: frame.source_frame_size_bytes,
    media_type: "image/jpeg",
    width: session.source.width,
    height: session.source.height,
  }));
  const frameEvidence = session.frames.map((frame: any, index: number) => {
    const group = session.sampling_manifest.groups[index];
    const {
      frame_index: _frameIndex,
      pre_reveal_lighting_stratum: _lighting,
      ...sealedGroup
    } = group;
    const timingBody = {
      schema_version: "1.0",
      artifact_type: "ball_source_frame_timing_binding",
      timing_profile_id: "verified_decoder_pos_msec_after_frame_position_v1",
      timing_status: "observed",
      source_sha256: session.source.sha256,
      runtime_environment_sha256: session.lineage.runtime_environment_sha256,
      source_frame_jpeg_sha256: frame.source_frame_sha256,
      frame_index: frame.frame_index,
      decoded_frame_position: frame.frame_index,
      fps: session.source.fps,
      effective_decode_mode: session.lineage.decode.effective_decode_mode,
      decoder_reported_pos_msec: frame.decoder_reported_pos_msec,
      decoder_time_seconds: frame.decoder_time_seconds,
      decoder_timing_observation_method:
        "opencv_cap_prop_pos_msec_after_verified_frame_read",
      display_time_seconds: frame.display_time_seconds,
      display_time_derivation:
        "frame_index_divided_by_fps_for_display_only_not_source_pts",
      true_presentation_timestamp: frame.true_presentation_timestamp,
      position_verification: "opencv_next_frame_index_with_0.25_tolerance",
      cross_decode_verification: null,
    };
    const evidenceBody = {
      schema_version: "1.0",
      artifact_type: "ball_sealed_frame_evidence",
      frame_index: frame.frame_index,
      frame_role: "primary",
      source: {
        sha256: session.source.sha256,
        width: session.source.width,
        height: session.source.height,
      },
      source_frame_jpeg: {
        sha256: frame.source_frame_sha256,
        size_bytes: frame.source_frame_size_bytes,
        media_type: "image/jpeg",
      },
      temporal_group: sealedGroup,
      probe_evidence: {
        schema_version: "1.0",
        artifact_type: "ball_source_frame_probe_evidence",
        probe_job_id: detectorAuthority.job_id,
        probe_report_sha256: detectorAuthority.probe_report_sha256,
        probe_result_manifest_sha256:
          detectorAuthority.probe_result_manifest_sha256,
        artifact_id: `source-${frame.frame_index.toString().padStart(9, "0")}`,
        artifact_sha256: frame.source_frame_sha256,
        artifact_size_bytes: frame.source_frame_size_bytes,
        artifact_media_type: "image/jpeg",
        binding_sha256: sha(`probe-binding:${index}`),
      },
      timing_binding: {
        ...timingBody,
        timing_binding_sha256: canonicalSha(timingBody, timingFloatPaths("$")),
      },
      proxy_binding: null,
      propagation_evidence: null,
      effective_revision: 1,
      effective_annotation_sha256: canonicalSha(
        effectiveAnnotations[index],
        annotationFloatPaths,
      ),
      revision_chain_sha256: canonicalSha(
        [revisionChain[index]],
        revisionFloatPaths([revisionChain[index]]),
      ),
    };
    return {
      ...evidenceBody,
      frame_evidence_sha256: canonicalSha(
        evidenceBody,
        timingFloatPaths("$.timing_binding"),
      ),
    };
  });
  const frameMediaSha256 = canonicalSha(frameMedia);
  const frameEvidenceSha256 = canonicalSha(
    frameEvidence,
    frameEvidence.flatMap((_: unknown, index: number) =>
      timingFloatPaths(`$[${index}].timing_binding`),
    ),
  );
  const revisionChainSha256 = canonicalSha(
    revisionChain,
    revisionFloatPaths(revisionChain),
  );
  const localizablePositiveCount = effectiveAnnotations.filter(
    (annotation: any) =>
      annotation.presence === "present" &&
      (annotation.point_source_px !== null ||
        annotation.bbox_source_px !== null),
  ).length;
  const datasetExpansionEligibility = {
    eligible: localizablePositiveCount > 0,
    reasons:
      localizablePositiveCount > 0 ? [] : ["no_localizable_positive_seed"],
    validation_evidence: {
      all_frames_human_confirmed: true,
      all_primary_roles_complete: true,
      all_supplemental_roles_complete: true,
      exact_frame_media_sha256: frameMediaSha256,
      frame_evidence_sha256: frameEvidenceSha256,
      localizable_positive_seed_count: localizablePositiveCount,
      pending_detector_candidate_count: 0,
      pending_propagation_suggestion_count: 0,
      pending_suggestion_decision_count: 0,
      revision_chain_sha256: revisionChainSha256,
    },
  };
  const packageValue = {
    schema_version: "1.0",
    artifact_type: "ball_annotation_package",
    session_id: session.session_id,
    session_request_authority: sessionRequestAuthority,
    data_role: "development",
    source: session.source,
    lineage: session.lineage,
    detector_probe_authorities: [detectorAuthority],
    frame_review_proxy_authority: null,
    operator_id: session.operator_id,
    locked_profile: session.locked_profile,
    control_profile_id: session.control_profile_id,
    control_profile: session.control_profile,
    sampling_profile_id: session.sampling_profile_id,
    metric_profile_id: session.metric_profile_id,
    metric_profile_sha256: session.metric_profile_sha256,
    sampling_manifest: session.sampling_manifest,
    attempt_family_sha256: session.attempt_family_sha256,
    development_package_binding: null,
    check_probe_job_id: null,
    check_probe_authority: null,
    effective_annotations: effectiveAnnotations,
    revision_chain: revisionChain,
    supplemental_frame_indices: [],
    frame_evidence: frameEvidence,
    frame_evidence_sha256: frameEvidenceSha256,
    frame_media: frameMedia,
    frame_media_sha256: frameMediaSha256,
    detector_candidate_evidence: [],
    detector_candidate_evidence_sha256: canonicalSha([]),
    propagation_reports: [],
    propagation_reports_sha256: canonicalSha([]),
    created_at: "2026-07-18T00:10:00Z",
    training_eligible: false,
    may_seed_dataset_expansion: datasetExpansionEligibility.eligible,
    qualification_eligible: false,
    pr4a_pr4b_truth_compatible: false,
    dataset_expansion_eligibility: datasetExpansionEligibility,
  };
  const canonicalPackage = structuredClone(packageValue);
  delete canonicalPackage.sampling_manifest.selection_authority;
  delete canonicalPackage.sampling_manifest.candidate_universe_authority;
  for (const field of ["groups", "excluded_development_groups"]) {
    canonicalPackage.sampling_manifest[field] =
      canonicalPackage.sampling_manifest[field].map((rawGroup: any) => {
        const group = { ...rawGroup };
        if (group.pre_reveal_lighting_stratum === null) {
          delete group.pre_reveal_lighting_stratum;
        }
        return group;
      });
  }
  const packageSha256 = canonicalSha(canonicalPackage, [
    "$.source.fps",
    "$.lineage.decode.fps",
    ...effectiveAnnotations.flatMap((_: unknown, index: number) =>
      nestedAnnotationFloatPaths(`$.effective_annotations[${index}]`),
    ),
    ...revisionFloatPaths(revisionChain, "$.revision_chain"),
    ...frameEvidence.flatMap((_: unknown, index: number) =>
      timingFloatPaths(`$.frame_evidence[${index}].timing_binding`),
    ),
  ]);
  const sealedPackage = { ...packageValue, package_sha256: packageSha256 };
  const reportBody = {
    schema_version: "1.0",
    artifact_type: "ball_feasibility_report",
    session_id: session.session_id,
    attempt_family_sha256: session.attempt_family_sha256,
    development_package_binding: null,
    status: "not_applicable",
    reason: "development_package_is_not_one_time_check_evidence",
    sealed_evidence: {
      annotation_package_sha256: packageSha256,
      attempt_family_sha256: session.attempt_family_sha256,
      sampling_manifest_sha256: session.sampling_manifest.manifest_sha256,
      check_probe_job_id: null,
      check_probe_report_sha256: null,
      dataset_expansion_eligibility: datasetExpansionEligibility,
    },
    authorizations: {
      may_expand_to_100_300_boxes: false,
      trial_eligible: false,
      source_segment_qualified: false,
      camera_qualified: false,
      production_approved: false,
      full_run_authorized: false,
    },
  };
  const report = {
    ...reportBody,
    report_sha256: canonicalSha(reportBody),
  };
  return { package: sealedPackage, feasibility_report: report };
}
