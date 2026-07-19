import { afterEach, describe, expect, it, vi } from "vitest";

import ballAnnotationApiGolden from "../../../../test_fixtures/contracts/ball_annotation_api_golden.v1.json";
import {
  ballAnnotationSessionFixture,
  developmentFinalResultFixture,
  refreshBallAnnotationProgress,
} from "../test/ballAnnotationFixtures";

import {
  ballAnnotationStorageKey,
  buildBallAnnotationMutation,
  buildBallAnnotationSessionRequest,
  buildBallPropagationRequest,
  computeReviewProxyBindingSha256,
  fetchVerifiedBallAnnotationFrame,
  parseBallAnnotationFinalResult,
  parseBallAnnotationRevision,
  parseBallAnnotationSession,
  parseBallPropagationJob,
} from "./productionBallAnnotation";
import { pythonCanonicalSha256Sync } from "./canonicalSha256";

const sha = (character: string) => character.repeat(64);
const metricProfileSha256 =
  "50320c9d6186d844e5f533193f3cc767bed9682a5c0c2c42ab17ccbf59169595";

const annotationAuthorityFloatPaths = (root = "$") =>
  [
    "point_source_px.x",
    "point_source_px.y",
    "bbox_source_px.left",
    "bbox_source_px.top",
    "bbox_source_px.right",
    "bbox_source_px.bottom",
  ].map((path) => `${root}.${path}`);

function testTimingFloatPaths(timing: any, root: string) {
  const paths = [
    `${root}.fps`,
    `${root}.display_time_seconds`,
    `${root}.true_presentation_timestamp.value_seconds`,
  ];
  if (timing.decoder_reported_pos_msec !== null) {
    paths.push(`${root}.decoder_reported_pos_msec`);
  }
  if (timing.decoder_time_seconds !== null) {
    paths.push(`${root}.decoder_time_seconds`);
  }
  if (timing.cross_decode_verification) {
    paths.push(`${root}.cross_decode_verification.tolerance_msec`);
    timing.cross_decode_verification.observations.forEach(
      (_: unknown, index: number) =>
        paths.push(
          `${root}.cross_decode_verification.observations[${index}].decoder_reported_pos_msec`,
        ),
    );
  }
  return paths;
}

const proxyBindingFloatPaths = (root: string) =>
  [
    "source_frame.decoder_reported_pos_msec",
    "proxy_frame.cfr_time_msec",
    "map_time_tolerance_msec",
    "declared_offset_msec",
    "time_mapping.declared_offset_msec",
    "time_mapping.observed_offset_msec",
    "time_mapping.residual_msec",
    "time_mapping.tolerance_msec",
  ].map((path) => `${root}.${path}`);

function testFrameEvidenceFloatPaths(row: any, root: string) {
  return [
    ...testTimingFloatPaths(row.timing_binding, `${root}.timing_binding`),
    ...(row.proxy_binding
      ? proxyBindingFloatPaths(`${root}.proxy_binding`)
      : []),
  ];
}

function resealFrameEvidence(packageValue: any, rowIndex: number) {
  const row = packageValue.frame_evidence[rowIndex];
  const body = { ...row };
  delete body.frame_evidence_sha256;
  row.frame_evidence_sha256 = pythonCanonicalSha256Sync(
    body,
    testFrameEvidenceFloatPaths(body, "$"),
  );
  packageValue.frame_evidence_sha256 = pythonCanonicalSha256Sync(
    packageValue.frame_evidence,
    packageValue.frame_evidence.flatMap((item: any, index: number) =>
      testFrameEvidenceFloatPaths(item, `$[${index}]`),
    ),
  );
  packageValue.dataset_expansion_eligibility.validation_evidence.frame_evidence_sha256 =
    packageValue.frame_evidence_sha256;
}

function testDetectorReportFloatPaths(report: any, root: string) {
  const paths = [`${root}.decode.fps`];
  report.decode?.frame_timing_observations?.forEach(
    (_: unknown, index: number) =>
      paths.push(
        `${root}.decode.frame_timing_observations[${index}].decoder_reported_pos_msec`,
      ),
  );
  report.frames?.forEach((frame: any, frameIndex: number) => {
    if (frame.decoder_reported_pos_msec !== null) {
      paths.push(`${root}.frames[${frameIndex}].decoder_reported_pos_msec`);
    }
    for (const field of [
      "mean_luma",
      "std_luma",
      "texture_tile_ratio",
      "dominant_color_ratio",
    ]) {
      paths.push(`${root}.frames[${frameIndex}].media_integrity.${field}`);
    }
    frame.profile_results?.forEach((profile: any, profileIndex: number) => {
      if (profile.latency_ms !== null && profile.latency_ms !== undefined) {
        paths.push(
          `${root}.frames[${frameIndex}].profile_results[${profileIndex}].latency_ms`,
        );
      }
      const candidates = [
        ...(profile.display_candidate ? [profile.display_candidate] : []),
        ...(profile.raw_candidates ?? []),
      ];
      candidates.forEach((candidate: any, candidateIndex: number) => {
        const candidateRoot =
          candidateIndex === 0 && profile.display_candidate
            ? `${root}.frames[${frameIndex}].profile_results[${profileIndex}].display_candidate`
            : `${root}.frames[${frameIndex}].profile_results[${profileIndex}].raw_candidates[${
                candidateIndex - (profile.display_candidate ? 1 : 0)
              }]`;
        candidate.bbox_source_px?.forEach((_: unknown, index: number) =>
          paths.push(`${candidateRoot}.bbox_source_px[${index}]`),
        );
        paths.push(`${candidateRoot}.confidence`);
      });
    });
  });
  if (report.review_proxy_manifest) {
    const proxyRoot = `${root}.review_proxy_manifest`;
    paths.push(
      `${proxyRoot}.source.fps`,
      `${proxyRoot}.proxy.fps`,
      `${proxyRoot}.map_time_tolerance_msec`,
      `${proxyRoot}.declared_offset_msec`,
      `${proxyRoot}.coordinate_transform.scale_x`,
      `${proxyRoot}.coordinate_transform.scale_y`,
      `${proxyRoot}.coordinate_transform.source_origin[0]`,
      `${proxyRoot}.coordinate_transform.source_origin[1]`,
      `${proxyRoot}.coordinate_transform.proxy_origin[0]`,
      `${proxyRoot}.coordinate_transform.proxy_origin[1]`,
    );
    report.review_proxy_manifest.mappings.forEach(
      (_: unknown, index: number) => {
        paths.push(
          `${proxyRoot}.mappings[${index}].source_decoder_pos_msec`,
          `${proxyRoot}.mappings[${index}].proxy_cfr_time_msec`,
        );
      },
    );
  }
  return paths;
}

function resealDetectorAuthority(authority: any) {
  authority.audit_anchor_kind = "embedded_job_record";
  authority.probe_job_record.report = structuredClone(authority.probe_report);
  authority.canonical_job_record_sha256 = pythonCanonicalSha256Sync(
    authority.probe_job_record,
    testDetectorReportFloatPaths(authority.probe_job_record.report, "$.report"),
  );
  const body = { ...authority };
  delete body.job_record_authority_sha256;
  authority.job_record_authority_sha256 = pythonCanonicalSha256Sync(body, [
    ...testDetectorReportFloatPaths(authority.probe_report, "$.probe_report"),
    ...testDetectorReportFloatPaths(
      authority.probe_job_record.report,
      "$.probe_job_record.report",
    ),
  ]);
}

function testReviewProxyManifestFloatPaths(manifest: any, root = "$") {
  return [
    `${root}.source.fps`,
    `${root}.proxy.fps`,
    `${root}.map_time_tolerance_msec`,
    `${root}.declared_offset_msec`,
    `${root}.coordinate_transform.scale_x`,
    `${root}.coordinate_transform.scale_y`,
    `${root}.coordinate_transform.source_origin[0]`,
    `${root}.coordinate_transform.source_origin[1]`,
    `${root}.coordinate_transform.proxy_origin[0]`,
    `${root}.coordinate_transform.proxy_origin[1]`,
    ...manifest.mappings.flatMap((_: unknown, index: number) => [
      `${root}.mappings[${index}].source_decoder_pos_msec`,
      `${root}.mappings[${index}].proxy_cfr_time_msec`,
    ]),
  ];
}

function canonicalPropagationReportForTest(report: any) {
  const canonical = structuredClone(report);
  for (const field of ["frame_results", "suggestions"]) {
    canonical[field] = canonical[field].map((row: any) => {
      if (row.human_confirmation === null) delete row.human_confirmation;
      if (row.human_decision === null) delete row.human_decision;
      return row;
    });
  }
  return canonical;
}

function testPropagationFloatPaths(report: any, root: string) {
  const paths = [
    `${root}.tracker_profile.minimum_match_score`,
    `${root}.tracker_profile.minimum_backward_match_score`,
    `${root}.tracker_profile.maximum_forward_backward_error_px`,
    `${root}.summary.self_check_coverage`,
  ];
  if (report.summary.human_validated_center_error_px !== null) {
    paths.push(`${root}.summary.human_validated_center_error_px`);
  }
  if (report.summary.human_validated_iou !== null) {
    paths.push(`${root}.summary.human_validated_iou`);
  }
  report.frame_results.forEach((row: any, index: number) => {
    for (const field of [
      "match_score",
      "backward_match_score",
      "forward_backward_error_px",
      "step_displacement_px",
    ]) {
      if (row[field] !== null) {
        paths.push(`${root}.frame_results[${index}].${field}`);
      }
    }
    if (row.human_confirmation) {
      paths.push(
        `${root}.frame_results[${index}].human_confirmation.center_error_px`,
      );
      if (row.human_confirmation.iou !== null) {
        paths.push(`${root}.frame_results[${index}].human_confirmation.iou`);
      }
    }
  });
  report.suggestions.forEach((row: any, index: number) => {
    const suggestionRoot = `${root}.suggestions[${index}]`;
    if (row.point_source_px) {
      paths.push(
        `${suggestionRoot}.point_source_px.x`,
        `${suggestionRoot}.point_source_px.y`,
      );
    }
    if (row.bbox_source_px) {
      paths.push(
        `${suggestionRoot}.bbox_source_px.left`,
        `${suggestionRoot}.bbox_source_px.top`,
        `${suggestionRoot}.bbox_source_px.right`,
        `${suggestionRoot}.bbox_source_px.bottom`,
      );
    }
    for (const field of [
      "match_score",
      "backward_match_score",
      "forward_backward_error_px",
      "step_displacement_px",
    ]) {
      paths.push(`${suggestionRoot}.self_check.${field}`);
    }
    if (row.human_confirmation) {
      paths.push(`${suggestionRoot}.human_confirmation.center_error_px`);
      if (row.human_confirmation.iou !== null) {
        paths.push(`${suggestionRoot}.human_confirmation.iou`);
      }
    }
  });
  return paths;
}

function testPackageFloatPaths(packageValue: any) {
  const paths = ["$.source.fps", "$.lineage.decode.fps"];
  packageValue.detector_probe_authorities.forEach(
    (authority: any, index: number) => {
      paths.push(
        ...testDetectorReportFloatPaths(
          authority.probe_report,
          `$.detector_probe_authorities[${index}].probe_report`,
        ),
        ...testDetectorReportFloatPaths(
          authority.probe_job_record.report,
          `$.detector_probe_authorities[${index}].probe_job_record.report`,
        ),
      );
    },
  );
  const proxy = packageValue.frame_review_proxy_authority;
  if (proxy) {
    paths.push(
      ...testDetectorReportFloatPaths(
        proxy.probe_report,
        "$.frame_review_proxy_authority.probe_report",
      ),
      ...testReviewProxyManifestFloatPaths(
        proxy.review_proxy_manifest,
        "$.frame_review_proxy_authority.review_proxy_manifest",
      ),
    );
    if (proxy.historical_probe_authority) {
      paths.push(
        ...testDetectorReportFloatPaths(
          proxy.historical_probe_authority.probe_report,
          "$.frame_review_proxy_authority.historical_probe_authority.probe_report",
        ),
      );
    }
  }
  packageValue.effective_annotations.forEach((_: unknown, index: number) =>
    paths.push(
      ...annotationAuthorityFloatPaths(`$.effective_annotations[${index}]`),
    ),
  );
  paths.push(
    ...packageValue.revision_chain.flatMap((_: unknown, index: number) => [
      ...annotationAuthorityFloatPaths(
        `$.revision_chain[${index}].previous_effective_annotation`,
      ),
      ...annotationAuthorityFloatPaths(
        `$.revision_chain[${index}].effective_annotation`,
      ),
    ]),
    ...packageValue.frame_evidence.flatMap((row: any, index: number) =>
      testFrameEvidenceFloatPaths(row, `$.frame_evidence[${index}]`),
    ),
    ...packageValue.detector_candidate_evidence.flatMap(
      (_: unknown, index: number) => [
        `$.detector_candidate_evidence[${index}].candidate.bbox_source_px.left`,
        `$.detector_candidate_evidence[${index}].candidate.bbox_source_px.top`,
        `$.detector_candidate_evidence[${index}].candidate.bbox_source_px.right`,
        `$.detector_candidate_evidence[${index}].candidate.bbox_source_px.bottom`,
        `$.detector_candidate_evidence[${index}].candidate.confidence`,
      ],
    ),
    ...packageValue.propagation_reports.flatMap((report: any, index: number) =>
      testPropagationFloatPaths(report, `$.propagation_reports[${index}]`),
    ),
  );
  return paths;
}

function resealFinalResult(result: any, session: any) {
  for (const evidence of result.package.detector_candidate_evidence) {
    const frameEvidence = result.package.frame_evidence.find(
      (row: any) => row.frame_index === evidence.frame_index,
    );
    evidence.review_media.proxy_binding_sha256 = frameEvidence?.proxy_binding
      ? pythonCanonicalSha256Sync(
          frameEvidence.proxy_binding,
          proxyBindingFloatPaths("$"),
        )
      : null;
  }
  result.package.detector_candidate_evidence_sha256 = pythonCanonicalSha256Sync(
    result.package.detector_candidate_evidence,
    result.package.detector_candidate_evidence.flatMap(
      (_: unknown, index: number) => [
        `$[${index}].candidate.bbox_source_px.left`,
        `$[${index}].candidate.bbox_source_px.top`,
        `$[${index}].candidate.bbox_source_px.right`,
        `$[${index}].candidate.bbox_source_px.bottom`,
        `$[${index}].candidate.confidence`,
      ],
    ),
  );
  const packageBody = structuredClone(result.package);
  delete packageBody.package_sha256;
  if (packageBody.sampling_manifest.selection_authority === null) {
    delete packageBody.sampling_manifest.selection_authority;
  }
  if (packageBody.sampling_manifest.candidate_universe_authority === null) {
    delete packageBody.sampling_manifest.candidate_universe_authority;
  }
  for (const field of ["groups", "excluded_development_groups"]) {
    packageBody.sampling_manifest[field] = packageBody.sampling_manifest[
      field
    ].map((rawGroup: any) => {
      const group = { ...rawGroup };
      if (group.pre_reveal_lighting_stratum === null) {
        delete group.pre_reveal_lighting_stratum;
      }
      return group;
    });
  }
  packageBody.propagation_reports = packageBody.propagation_reports.map(
    canonicalPropagationReportForTest,
  );
  result.package.package_sha256 = pythonCanonicalSha256Sync(
    packageBody,
    testPackageFloatPaths(packageBody),
  );
  if (session.view.finalPackage) {
    session.view.finalPackage.packageSha256 = result.package.package_sha256;
  }
  if (result.feasibility_report.status === "not_applicable") {
    result.feasibility_report.sealed_evidence.annotation_package_sha256 =
      result.package.package_sha256;
    result.feasibility_report.sealed_evidence.dataset_expansion_eligibility =
      structuredClone(result.package.dataset_expansion_eligibility);
    const reportBody = { ...result.feasibility_report };
    delete reportBody.report_sha256;
    result.feasibility_report.report_sha256 = pythonCanonicalSha256Sync(
      reportBody,
      [],
    );
  }
}

const strataApplicability = {
  scale: [
    {
      stratum: "near" as const,
      status: "applicable" as const,
      evidenceNote: "Near-ball moments exist in the source.",
    },
    {
      stratum: "mid" as const,
      status: "applicable" as const,
      evidenceNote: "Mid-distance play is represented.",
    },
    {
      stratum: "far" as const,
      status: "applicable" as const,
      evidenceNote: "Far-ball panoramic play is represented.",
    },
  ],
  lighting: [
    {
      stratum: "bright_sun" as const,
      status: "applicable" as const,
      evidenceNote: "Bright sunlight is visible.",
      quota: 0,
      frameIntervals: [],
    },
    {
      stratum: "shadow" as const,
      status: "applicable" as const,
      evidenceNote: "Pitch shadows are visible.",
      quota: 0,
      frameIntervals: [],
    },
    {
      stratum: "backlight" as const,
      status: "not_applicable" as const,
      evidenceNote: "No backlit interval exists in this source.",
      quota: 0,
      frameIntervals: [],
    },
    {
      stratum: "twilight" as const,
      status: "not_applicable" as const,
      evidenceNote: "The match was not captured at twilight.",
      quota: 0,
      frameIntervals: [],
    },
    {
      stratum: "artificial_light" as const,
      status: "not_applicable" as const,
      evidenceNote: "No artificial lighting was used.",
      quota: 0,
      frameIntervals: [],
    },
  ],
};

function checkStrataApplicability(targetFrameCount: number) {
  const brightQuota = Math.ceil(targetFrameCount / 2);
  const shadowQuota = targetFrameCount - brightQuota;
  return {
    scale: strataApplicability.scale,
    lighting: strataApplicability.lighting.map((row) =>
      row.stratum === "bright_sun"
        ? {
            ...row,
            quota: brightQuota,
            frameIntervals: [{ startFrame: 0, endFrame: 249 }],
          }
        : row.stratum === "shadow"
          ? {
              ...row,
              quota: shadowQuota,
              frameIntervals: [{ startFrame: 250, endFrame: 499 }],
            }
          : row,
    ),
  };
}

function sessionFixture() {
  const profile = {
    profile_id: "official-coco-yolo11s-sahi",
    profile_sha256: sha("c"),
    model_id: "yolo11s",
    model_version: "11.0",
    model_descriptor_sha256: sha("d"),
    weights_sha256: sha("e"),
  };
  const controlProfile = {
    profile_id: "current-coco-yolov8n-direct",
    profile_sha256: sha("f"),
    model_id: "yolov8n",
    model_version: "8.0",
    model_descriptor_sha256: sha("7"),
    weights_sha256: sha("8"),
  };
  const value: any = {
    schema_version: "1.0",
    artifact_type: "ball_annotation_session",
    session_id: "annotation-session-1",
    idempotency_key: sha("1"),
    request_sha256: sha("2"),
    data_role: "development",
    status: "annotating",
    stage: "annotating",
    source: {
      source_id: "source-1",
      sha256: sha("3"),
      file_identity_sha256: sha("4"),
      size_bytes: 1234,
      width: 5120,
      height: 1440,
      frame_count: 500,
      tracking_contract_sha256: sha("5"),
      relative_path: "source.mp4",
      tracking_contract_relative_path: "tracking-contract.json",
      fps: 20,
    },
    lineage: {
      parent_trial_id: "trial-1",
      development_probe_job_ids: ["probe-ready-1"],
      development_probe_report_sha256s: { "probe-ready-1": sha("b") },
      development_probe_result_manifest_sha256s: {
        "probe-ready-1": sha("c"),
      },
      development_probe_execution_bundle_sha256s: {
        "probe-ready-1": sha("d"),
      },
      development_probe_frozen_profiles_sha256s: {
        "probe-ready-1": sha("e"),
      },
      decode: {
        width: 5120,
        height: 1440,
        frame_count: 500,
        fps: 20,
        requested_decode_mode: "sequential",
        effective_decode_mode: "sequential",
        position_verification: "opencv_next_frame_index_with_0.25_tolerance",
      },
      runtime_environment_sha256: sha("d"),
    },
    locked_profile: profile,
    control_profile_id: controlProfile.profile_id,
    control_profile: controlProfile,
    sampling_profile_id: "tiny_ball_temporal_groups_v1",
    metric_profile_id: "tiny_ball_feasibility_metric_v1",
    metric_profile_sha256: metricProfileSha256,
    sampling_manifest: {
      schema_version: "1.0",
      artifact_type: "ball_annotation_sampling_manifest",
      profile_id: "tiny_ball_temporal_groups_v1",
      selection_profile_id: "development_probe_frames_v1",
      scale_stratification_mode: "post_reveal_support_gate_only",
      lighting_stratification_mode: "not_applicable_development_evidence",
      selection_seed_sha256: sha("1"),
      candidate_universe_sha256: sha("2"),
      candidate_universe_start_frame: 0,
      candidate_universe_end_frame: 499,
      selection_authority: null,
      candidate_universe_authority: null,
      metric_profile_id: "tiny_ball_feasibility_metric_v1",
      metric_profile_sha256: metricProfileSha256,
      data_role: "development",
      target_frame_count: 1,
      frame_indices: [10],
      groups: [
        {
          group_id: sha("9"),
          profile_id: "tiny_ball_temporal_groups_v1",
          source_sha256: sha("3"),
          seed_frame_index: 10,
          start_frame: 8,
          end_frame: 12,
          derivative_family: [8, 12],
          canonical_moment_id: sha("0"),
          derivative_family_id: sha("9"),
          ancestry_profile: "source-proxy-crop-tile-propagation-closure-v1",
          frame_index: 10,
          pre_reveal_lighting_stratum: null,
        },
      ],
      excluded_development_groups: [],
      locked_before_probe: false,
      source_sha256: sha("3"),
      locked_profile_id: profile.profile_id,
      locked_profile_sha256: profile.profile_sha256,
      strata_applicability: {
        scale: ["near", "mid", "far"].map((stratum) => ({
          stratum,
          status: stratum === "far" ? "applicable" : "not_applicable",
          evidence: {
            declared_before_reveal: true,
            note: `${stratum} evidence`,
            evidence_sha256: sha("e"),
          },
        })),
        lighting: [
          "bright_sun",
          "shadow",
          "backlight",
          "twilight",
          "artificial_light",
        ].map((stratum) => ({
          stratum,
          status: stratum === "bright_sun" ? "applicable" : "not_applicable",
          quota: 0,
          frame_intervals: [],
          evidence: {
            declared_before_reveal: true,
            note: `${stratum} evidence`,
            evidence_sha256: sha("f"),
          },
        })),
      },
      manifest_sha256: "",
    },
    operator_id: "operator-one",
    applicable_scale_strata: ["far"],
    applicable_lighting_strata: ["bright_sun"],
    retry_from_session_id: null,
    retry_lineage: null,
    attempt_family_sha256: sha("2"),
    development_package_binding: null,
    check_probe_job_id: null,
    check_probe_authority: null,
    frames: [
      {
        frame_index: 10,
        source_frame_sha256: sha("8"),
        source_frame_size_bytes: 2048,
        suggested_candidates: [],
        source_timing_status: "observed",
        decoder_reported_pos_msec: 500,
        decoder_time_seconds: 0.5,
        display_time_seconds: 0.5,
        true_presentation_timestamp: {
          status: "not_collected",
          value_seconds: null,
          method: null,
        },
        proxy_binding: null,
        temporal_group_id: sha("9"),
        frame_url:
          "/api/v1/ball-annotation-sessions/annotation-session-1/frames/10",
        annotation_revision: 0,
        annotation_etag: sha("a"),
        current_annotation: null,
        frame_role: "primary_sample",
        primary_sample: true,
        propagation_job_ids: [],
        propagation_suggestions: [],
      },
    ],
    final_package: null,
    error_code: null,
    blocker_code: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:00:00Z",
    progress: {
      annotated_frames: 0,
      total_frames: 1,
      unconfirmed_suggestions: 0,
      primary_annotated_frames: 0,
      primary_total_frames: 1,
      supplemental_annotated_frames: 0,
      supplemental_total_frames: 0,
      unconfirmed_propagation_suggestions: 0,
    },
  };
  const canonicalManifest = structuredClone(value.sampling_manifest);
  delete canonicalManifest.manifest_sha256;
  delete canonicalManifest.selection_authority;
  delete canonicalManifest.candidate_universe_authority;
  canonicalManifest.groups = canonicalManifest.groups.map((group: any) => {
    const copy = { ...group };
    delete copy.pre_reveal_lighting_stratum;
    return copy;
  });
  value.sampling_manifest.manifest_sha256 = pythonCanonicalSha256Sync(
    canonicalManifest,
    [],
  );
  return value;
}

const indexedSha = (index: number) => index.toString(16).padStart(64, "0");

function sessionWithDetectorCandidate(
  decision: "pending" | "accepted" | "dismissed" = "pending",
) {
  const value: any = sessionFixture();
  value.frames[0].suggested_candidates = [
    {
      candidate_id: "candidate-one",
      profile_id: value.locked_profile.profile_id,
      rank: 1,
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      confidence: 0.9,
      annotation_state: "suggested",
      training_use: "excluded",
      truth_status: "unconfirmed_suggestion",
      suggestion_job_id: "probe-ready-1",
      suggestion_sha256: sha("1"),
      decision,
    },
  ];
  value.progress.unconfirmed_suggestions = decision === "pending" ? 1 : 0;
  return value;
}

function finalizedCheckSessionFixture() {
  const value: any = sessionFixture();
  const frameIndices = Array.from(
    { length: 20 },
    (_, index) => 10 + index * 10,
  );
  const groupFor = (frameIndex: number, index: number) => ({
    group_id: indexedSha(100 + index),
    profile_id: "tiny_ball_temporal_groups_v1",
    source_sha256: value.source.sha256,
    seed_frame_index: frameIndex,
    start_frame: Math.max(0, frameIndex - 2),
    end_frame: frameIndex + 2,
    derivative_family: [Math.max(0, frameIndex - 2), frameIndex + 2],
    canonical_moment_id: indexedSha(200 + index),
    derivative_family_id: indexedSha(100 + index),
    ancestry_profile: "source-proxy-crop-tile-propagation-closure-v1",
    frame_index: frameIndex,
    pre_reveal_lighting_stratum: "bright_sun",
  });
  const groups = frameIndices.map(groupFor);
  value.data_role = "check";
  value.status = "finalized";
  value.stage = "finalized";
  value.check_probe_job_id = "check-probe-one";
  value.check_probe_authority = {
    job_id: "check-probe-one",
    request_sha256: indexedSha(301),
    intent_sha256: indexedSha(302),
    result_manifest_sha256: indexedSha(303),
    report_sha256: indexedSha(304),
    parent_trial_id: "trial-1",
    runtime_environment_sha256: indexedSha(305),
    execution_bundle_sha256: indexedSha(306),
    frozen_profiles_sha256: indexedSha(307),
    locked_profile: value.locked_profile,
    control_profile: value.control_profile,
  };
  value.sampling_manifest.data_role = "check";
  value.sampling_manifest.target_frame_count = 20;
  value.sampling_manifest.frame_indices = frameIndices;
  value.sampling_manifest.groups = groups;
  value.sampling_manifest.excluded_development_groups = [
    sessionFixture().sampling_manifest.groups[0],
  ];
  value.sampling_manifest.locked_before_probe = true;
  value.sampling_manifest.selection_profile_id =
    "tiny_ball_temporal_block_hash_v1";
  value.sampling_manifest.lighting_stratification_mode =
    "predeclared_frame_intervals_and_quota_v1";
  value.sampling_manifest.selection_authority = { frozen: true };
  value.sampling_manifest.candidate_universe_authority = { frozen: true };
  value.sampling_manifest.strata_applicability.lighting =
    value.sampling_manifest.strata_applicability.lighting.map((row: any) =>
      row.stratum === "bright_sun"
        ? {
            ...row,
            quota: 20,
            frame_intervals: [{ start_frame: 0, end_frame: 499 }],
          }
        : row,
    );
  value.frames = frameIndices.map((frameIndex, index) => ({
    frame_index: frameIndex,
    source_frame_sha256: indexedSha(400 + index),
    source_frame_size_bytes: 2048 + index,
    suggested_candidates:
      index === 0
        ? [
            {
              candidate_id: "candidate-one",
              profile_id: value.locked_profile.profile_id,
              rank: 1,
              bbox_source_px: {
                left: 90,
                top: 90,
                right: 110,
                bottom: 110,
              },
              confidence: 0.9,
              annotation_state: "suggested",
              training_use: "excluded",
              truth_status: "unconfirmed_suggestion",
              suggestion_job_id: "check-probe-one",
              suggestion_sha256: indexedSha(650),
              decision: "pending",
            },
          ]
        : [],
    source_timing_status: "observed",
    decoder_reported_pos_msec: (frameIndex / value.source.fps) * 1_000,
    decoder_time_seconds: frameIndex / value.source.fps,
    display_time_seconds: frameIndex / value.source.fps,
    true_presentation_timestamp: {
      status: "not_collected",
      value_seconds: null,
      method: null,
    },
    proxy_binding: null,
    temporal_group_id: groups[index].group_id,
    frame_url: `/api/v1/ball-annotation-sessions/${value.session_id}/frames/${frameIndex}`,
    annotation_revision: 1,
    annotation_etag: indexedSha(500 + index),
    current_annotation: {
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: index === 1 ? "partial" : "visible",
      training_use: "excluded",
      annotation_state: "confirmed",
      scale_stratum: "far",
      lighting_tag: "bright_sun",
      motion_occlusion_tags: index === 1 ? ["motion_blurred"] : [],
      provenance: "manual_human_annotation",
    },
    frame_role: "primary_sample",
    primary_sample: true,
    propagation_job_ids: [],
    propagation_suggestions: [],
  }));
  value.progress = {
    annotated_frames: 20,
    total_frames: 20,
    unconfirmed_suggestions: 1,
    primary_annotated_frames: 20,
    primary_total_frames: 20,
    supplemental_annotated_frames: 0,
    supplemental_total_frames: 0,
    unconfirmed_propagation_suggestions: 0,
  };
  value.final_package = {
    result_url: `/api/v1/ball-annotation-sessions/${value.session_id}/result`,
    manifest_sha256: value.sampling_manifest.manifest_sha256,
    package_sha256: indexedSha(601),
    report_sha256: indexedSha(602),
    status: "feasibility_passed",
  };
  return value;
}

function checkFinalResultFixture(rawSession: any) {
  const rawMetric = (
    numerator: number,
    denominator: number,
    intervalKey?: "one_sided_95_lower" | "one_sided_95_upper",
  ) => ({
    raw: { numerator, denominator },
    point_estimate: denominator === 0 ? 0 : numerator / denominator,
    ...(intervalKey ? { [intervalKey]: 0.75 } : {}),
  });
  return {
    package: {
      schema_version: "1.0",
      artifact_type: "ball_annotation_package",
      session_id: rawSession.session_id,
      data_role: "check",
      source: rawSession.source,
      lineage: rawSession.lineage,
      operator_id: rawSession.operator_id,
      locked_profile: rawSession.locked_profile,
      sampling_manifest: rawSession.sampling_manifest,
      check_probe_job_id: rawSession.check_probe_job_id,
      check_probe_authority: rawSession.check_probe_authority,
      effective_annotations: rawSession.frames.map((frame: any) => ({
        frame_index: frame.frame_index,
      })),
      revision_chain: [],
      created_at: "2026-07-18T00:30:00Z",
      training_eligible: false,
      may_seed_dataset_expansion: false,
      qualification_eligible: false,
      pr4a_pr4b_truth_compatible: false,
      package_sha256: rawSession.final_package.package_sha256,
    },
    feasibility_report: {
      schema_version: "1.0",
      artifact_type: "ball_feasibility_report",
      session_id: rawSession.session_id,
      source_sha256: rawSession.source.sha256,
      locked_profile_id: rawSession.locked_profile.profile_id,
      locked_profile_sha256: rawSession.locked_profile.profile_sha256,
      sampling_manifest_sha256: rawSession.sampling_manifest.manifest_sha256,
      metric_profile: {
        profile_id: "tiny_ball_feasibility_metric_v1",
        candidate_budget: 5,
        intervals: {
          recall: "clopper-pearson-one-sided-95",
          false_candidates: "poisson-one-sided-95",
        },
      },
      metric_profile_sha256: rawSession.metric_profile_sha256,
      status: "feasibility_passed",
      support: {
        total_frames: 20,
        localizable_positives: 20,
        confirmed_absent: 0,
        excluded_or_unresolvable: 0,
        scale: { near: 0, mid: 0, far: 20 },
        lighting: {
          bright_sun: 20,
          shadow: 0,
          backlight: 0,
          twilight: 0,
          artificial_light: 0,
        },
        applicable_scale_strata: ["far"],
        applicable_lighting_strata: ["bright_sun"],
        missing: [],
      },
      metrics: {
        top1_recall: rawMetric(18, 20, "one_sided_95_lower"),
        top5_recall: rawMetric(20, 20, "one_sided_95_lower"),
        false_candidates_per_evaluable_frame: rawMetric(
          2,
          20,
          "one_sided_95_upper",
        ),
        candidates_per_evaluable_frame: rawMetric(20, 20),
        raw_candidates_per_evaluable_frame: rawMetric(24, 20),
      },
      frames: rawSession.frames.map((frame: any) => ({
        frame_index: frame.frame_index,
      })),
      authorizations: {
        may_expand_to_100_300_boxes: true,
        trial_eligible: false,
        source_segment_qualified: false,
        camera_qualified: false,
        production_approved: false,
        full_run_authorized: false,
      },
      limitations: ["One-time source/profile feasibility only."],
      sealed_evidence: {},
      report_sha256: rawSession.final_package.report_sha256,
    },
  };
}

function makeInternallyConsistentPositiveFeasibilityFrame(frame: any) {
  Object.assign(frame, {
    presence: "present",
    metric_eligible: true,
    scored_candidate_count: 1,
    raw_candidate_count: 1,
    top1_hit: false,
    top5_hit: false,
    bbox_diagonal_source_px: 1,
    bbox_aspect_ratio: 1,
    observed_scale_stratum: "near",
    derived_scale_stratum: "near",
    diagnostic_codes: [],
    candidate_diagnostics: [
      {
        rank: 1,
        matched: false,
        center_distance_source_px: 5,
        iou: 0,
        evaluation_radius_source_px: 4,
      },
    ],
  });
}

function makeIneligiblePositiveFeasibilityFrame(
  frame: any,
  values: {
    diagonal: number;
    aspect?: number;
    derivedScale?: "near" | "mid" | "far" | null;
    observedScale?: "near" | "mid" | "far";
    diagnosticCodes: string[];
  },
) {
  Object.assign(frame, {
    presence: "present",
    metric_eligible: false,
    scored_candidate_count: 0,
    raw_candidate_count: 0,
    top1_hit: null,
    top5_hit: null,
    bbox_diagonal_source_px: values.diagonal,
    bbox_aspect_ratio: values.aspect ?? 1,
    observed_scale_stratum: values.observedScale ?? "near",
    derived_scale_stratum: values.derivedScale ?? null,
    diagnostic_codes: values.diagnosticCodes,
    candidate_diagnostics: [],
  });
}

async function digest(bytes: Uint8Array) {
  const value = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(value), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function proxyBindingFor(frame: {
  frame_index: number;
  decoder_reported_pos_msec: number;
  source_frame_sha256: string;
}) {
  const sourcePosition = frame.decoder_reported_pos_msec;
  const proxyPosition = sourcePosition + 50;
  const binding = {
    schema_version: "1.0",
    artifact_type: "ball_review_proxy_frame_binding",
    proxy: {
      sha256: sha("4"),
      size_bytes: 4_096,
      width: 1_920,
      height: 1_080,
    },
    map_sha256: sha("5"),
    source_frame: {
      frame_index: frame.frame_index,
      timing_status: "observed",
      decoder_reported_pos_msec: sourcePosition,
      sha256: frame.source_frame_sha256,
    },
    proxy_frame: {
      frame_index: frame.frame_index,
      timing_basis: "verified_cfr_frame_index_time_v1",
      cfr_time_msec: proxyPosition,
      sha256: sha("6"),
    },
    map_time_tolerance_msec: 2,
    declared_offset_msec: 50,
    time_mapping: {
      method: "explicit_per_frame_decoder_pos_msec_map_v1",
      source_timing_status: "observed",
      proxy_timing_basis: "verified_cfr_frame_index_time_v1",
      declared_offset_msec: 50,
      observed_offset_msec: 50,
      residual_msec: 0,
      tolerance_msec: 2,
    },
  };
  return {
    ...binding,
    binding_sha256: computeReviewProxyBindingSha256(binding),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("ball annotation authority request builders", () => {
  it("builds only the allowlisted source-derived session intent", () => {
    expect(
      buildBallAnnotationSessionRequest({
        dataRole: "check",
        developmentProbeJobIds: ["probe-ready-2", "probe-ready-1"],
        lockedProfileId: "official-coco-yolo11s-sahi",
        targetFrameCount: 30,
        developmentPackageSessionId: "development-session-1",
        developmentPackageSha256: sha("a"),
        operatorId: "operator-one",
        strataApplicability: {
          scale: [...checkStrataApplicability(30).scale].reverse(),
          lighting: [...checkStrataApplicability(30).lighting].reverse(),
        },
      }),
    ).toEqual({
      data_role: "check",
      development_probe_job_ids: ["probe-ready-1", "probe-ready-2"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      target_frame_count: 30,
      sampling_profile_id: "tiny_ball_temporal_groups_v1",
      metric_profile_id: "tiny_ball_feasibility_metric_v1",
      operator_id: "operator-one",
      strata_applicability: {
        scale: strataApplicability.scale.map(({ evidenceNote, ...row }) => ({
          ...row,
          evidence_note: evidenceNote,
        })),
        lighting: checkStrataApplicability(30).lighting.map(
          ({ evidenceNote, frameIntervals, ...row }) => ({
            ...row,
            evidence_note: evidenceNote,
            frame_intervals: frameIntervals.map(({ startFrame, endFrame }) => ({
              start_frame: startFrame,
              end_frame: endFrame,
            })),
          }),
        ),
      },
      retry_from_session_id: null,
      development_package_session_id: "development-session-1",
      development_package_sha256: sha("a"),
    });
  });

  it("binds a development session to its actual revealed T2 frames without a fake 20-frame target", () => {
    expect(
      buildBallAnnotationSessionRequest({
        dataRole: "development",
        developmentProbeJobIds: ["probe-ready-1"],
        lockedProfileId: "official-coco-yolo11s-sahi",
        operatorId: "operator-one",
        strataApplicability,
      }),
    ).toEqual(
      expect.objectContaining({
        data_role: "development",
        target_frame_count: null,
        retry_from_session_id: null,
        development_package_session_id: null,
        development_package_sha256: null,
        strata_applicability: expect.objectContaining({
          lighting: expect.arrayContaining([
            expect.objectContaining({ quota: 0, frame_intervals: [] }),
          ]),
        }),
      }),
    );
  });

  it.each([
    { dataRole: "invalid" },
    { developmentProbeJobIds: ["probe-ready-1", "probe-ready-1"] },
    {
      developmentProbeJobIds: Array.from(
        { length: 9 },
        (_, index) => `probe-ready-${index}`,
      ),
    },
    { targetFrameCount: 51 },
    { targetFrameCount: 20.5 },
    { targetFrameCount: 19 },
    { dataRole: "development", targetFrameCount: 20 },
    { dataRole: "development", targetFrameCount: null },
    {
      dataRole: "development",
      targetFrameCount: null,
      retryFromSessionId: "blocked-check-session",
    },
    { developmentProbeJobIds: [] },
    { applicableScaleStrata: ["near", "mid", "far"] },
    {
      strataApplicability: {
        ...strataApplicability,
        scale: strataApplicability.scale.slice(0, 2),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        lighting: null,
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        lighting: checkStrataApplicability(20).lighting.map((row, index) =>
          index === 1 ? { ...row, stratum: "bright_sun" as const } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        lighting: checkStrataApplicability(20).lighting.map((row, index) =>
          index === 0
            ? {
                ...row,
                frameIntervals: [{ startFrame: 10, endFrame: 9 }],
              }
            : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        lighting: strataApplicability.lighting.map((row) => ({
          ...row,
          status: "not_applicable" as const,
        })),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        scale: strataApplicability.scale.map((row, index) =>
          index === 1 ? { ...row, stratum: "near" as const } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        scale: strataApplicability.scale.map((row, index) =>
          index === 0 ? { ...row, evidenceNote: "x" } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        lighting: strataApplicability.lighting.map((row, index) =>
          index === 0 ? { ...row, evidenceNote: "x" } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...strataApplicability,
        scale: strataApplicability.scale.map((row) => ({
          ...row,
          status: "not_applicable" as const,
        })),
      },
    },
    { source_sha256: sha("f") },
    { frame_indices: [1] },
    { check_probe_job_id: "forged" },
    { candidate_artifact_url: "/forged" },
  ])("rejects invalid or client-authoritative session input %j", (patch) => {
    const input = {
      dataRole: "check",
      developmentProbeJobIds: ["probe-ready-1"],
      lockedProfileId: "official-coco-yolo11s-sahi",
      targetFrameCount: 20,
      operatorId: "operator-one",
      developmentPackageSessionId: "development-session-1",
      developmentPackageSha256: sha("a"),
      strataApplicability: checkStrataApplicability(20),
      ...patch,
    };
    expect(() => buildBallAnnotationSessionRequest(input as never)).toThrow();
  });

  it("rejects a non-object session request", () => {
    expect(() => buildBallAnnotationSessionRequest(null as never)).toThrow(
      /input is invalid/,
    );
  });

  it.each([
    { developmentPackageSessionId: undefined },
    { developmentPackageSha256: undefined },
    { developmentPackageSha256: sha("A") },
    {
      strataApplicability: {
        ...checkStrataApplicability(20),
        lighting: checkStrataApplicability(20).lighting.map((row) =>
          row.stratum === "bright_sun" ? { ...row, quota: 9 } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...checkStrataApplicability(20),
        lighting: checkStrataApplicability(20).lighting.map((row) =>
          row.stratum === "bright_sun" ? { ...row, frameIntervals: [] } : row,
        ),
      },
    },
    {
      strataApplicability: {
        ...checkStrataApplicability(20),
        lighting: checkStrataApplicability(20).lighting.map((row) =>
          row.stratum === "backlight"
            ? {
                ...row,
                quota: 3,
                frameIntervals: [{ startFrame: 0, endFrame: 499 }],
              }
            : row,
        ),
      },
    },
  ])("rejects check authority or lighting sampling mismatch %j", (patch) => {
    const input = {
      dataRole: "check" as const,
      developmentProbeJobIds: ["probe-ready-1"],
      lockedProfileId: "official-coco-yolo11s-sahi",
      targetFrameCount: 20,
      operatorId: "operator-one",
      developmentPackageSessionId: "development-session-1",
      developmentPackageSha256: sha("a"),
      strataApplicability: checkStrataApplicability(20),
      ...patch,
    };
    expect(() => buildBallAnnotationSessionRequest(input)).toThrow();
  });

  it("builds set, delete, and append-only undo mutations with revision identity", () => {
    const annotation = {
      point_source_px: null,
      bbox_source_px: null,
      presence: "absent" as const,
      visibility: "not_applicable" as const,
      training_use: "excluded" as const,
      annotation_state: "confirmed" as const,
      scale_stratum: "not_applicable" as const,
      lighting_tag: "shadow" as const,
      motion_occlusion_tags: [] as string[],
      provenance: "manual_human_annotation",
    };
    expect(
      buildBallAnnotationMutation({
        operation: "set",
        mutationId: "mutation-one",
        expectedRevision: 2,
        annotation,
      }),
    ).toEqual({
      mutation_id: "mutation-one",
      expected_revision: 2,
      operation: "set",
      undo_revision: null,
      annotation,
      suggestion_kind: null,
      suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
    });
    expect(
      buildBallAnnotationMutation({
        operation: "delete",
        mutationId: "mutation-two",
        expectedRevision: 3,
      }).annotation,
    ).toBeNull();
    expect(
      buildBallAnnotationMutation({
        operation: "undo",
        mutationId: "mutation-three",
        expectedRevision: 4,
        undoRevision: 4,
      }).undo_revision,
    ).toBe(4);
  });

  it("builds only a bounded propagation request", () => {
    expect(
      buildBallPropagationRequest({
        mutationId: "propagation-mutation-one",
        seedFrameIndex: 40,
        radiusFrames: 2,
        expectedSeedRevision: 1,
      }),
    ).toEqual({
      mutation_id: "propagation-mutation-one",
      seed_frame_index: 40,
      radius_frames: 2,
      expected_seed_revision: 1,
    });
    for (const patch of [
      { radiusFrames: 0 },
      { radiusFrames: 3 },
      { radiusFrames: 1.5 },
      { expectedSeedRevision: 0 },
      { seedFrameIndex: -1 },
      { unexpected: true },
    ]) {
      expect(() =>
        buildBallPropagationRequest({
          mutationId: "propagation-mutation-one",
          seedFrameIndex: 40,
          radiusFrames: 2,
          expectedSeedRevision: 1,
          ...patch,
        } as never),
      ).toThrow();
    }
  });

  it("maps accepted and dismissed suggestion authority without dropping lineage", () => {
    const annotation = {
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present" as const,
      visibility: "visible" as const,
      training_use: "positive" as const,
      annotation_state: "confirmed" as const,
      scale_stratum: "far" as const,
      lighting_tag: "bright_sun" as const,
      motion_occlusion_tags: [] as string[],
      provenance: "detector_candidate_human_confirmed",
    };
    expect(
      buildBallAnnotationMutation({
        operation: "set",
        mutationId: "mutation-accept",
        expectedRevision: 0,
        annotation,
        suggestionDecision: {
          action: "accept",
          kind: "detector_candidate",
          id: "candidate-one",
          jobId: "probe-ready-1",
          sha256: sha("a"),
        },
      }),
    ).toEqual(
      expect.objectContaining({
        suggestion_kind: "detector_candidate",
        suggestion_id: "candidate-one",
        accepted_suggestion_job_id: "probe-ready-1",
        accepted_suggestion_sha256: sha("a"),
        dismissed_suggestion_kind: null,
        dismissed_suggestion_id: null,
        dismissed_suggestion_job_id: null,
        dismissed_suggestion_sha256: null,
      }),
    );
    expect(
      buildBallAnnotationMutation({
        operation: "set",
        mutationId: "mutation-dismiss",
        expectedRevision: 0,
        annotation: {
          ...annotation,
          provenance: "suggestion_dismissed_manual",
        },
        suggestionDecision: {
          action: "dismiss",
          kind: "propagation",
          id: "suggestion-one",
          jobId: "propagation-job-one",
          sha256: sha("b"),
        },
      }),
    ).toEqual(
      expect.objectContaining({
        suggestion_kind: null,
        suggestion_id: null,
        accepted_suggestion_job_id: null,
        accepted_suggestion_sha256: null,
        dismissed_suggestion_kind: "propagation",
        dismissed_suggestion_id: "suggestion-one",
        dismissed_suggestion_job_id: "propagation-job-one",
        dismissed_suggestion_sha256: sha("b"),
      }),
    );
  });

  it.each([
    { jobId: undefined },
    { sha256: undefined },
    { sha256: sha("A") },
    { action: "forged" },
    { unexpected: true },
  ])("rejects incomplete or tampered suggestion authority %j", (patch) => {
    expect(() =>
      buildBallAnnotationMutation({
        operation: "set",
        mutationId: "mutation-tampered",
        expectedRevision: 0,
        annotation: {
          point_source_px: { x: 100, y: 100 },
          bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
          presence: "present",
          visibility: "visible",
          training_use: "positive",
          annotation_state: "confirmed",
          scale_stratum: "far",
          lighting_tag: "bright_sun",
          motion_occlusion_tags: [],
          provenance: "detector_candidate_human_confirmed",
        },
        suggestionDecision: {
          action: "accept",
          kind: "detector_candidate",
          id: "candidate-one",
          jobId: "probe-ready-1",
          sha256: sha("a"),
          ...patch,
        },
      } as never),
    ).toThrow(/suggestion|authority|invalid/i);
  });

  it.each([
    {
      operation: "set",
      mutationId: "mutation-one",
      expectedRevision: -1,
      annotation: {},
    },
    {
      operation: "set",
      mutationId: "mutation-one",
      expectedRevision: 0,
      annotation: null,
    },
    {
      operation: "undo",
      mutationId: "mutation-one",
      expectedRevision: 2,
      undoRevision: 0,
    },
    {
      operation: "undo",
      mutationId: "mutation-one",
      expectedRevision: 2,
      undoRevision: 3,
    },
  ])("rejects invalid mutation identity %j", (input) => {
    expect(() => buildBallAnnotationMutation(input as never)).toThrow();
  });

  it("binds local recovery only to safe session IDs", () => {
    expect(ballAnnotationStorageKey("workflow-one", "probe-ready-1")).toBe(
      "football-tracking.ball-annotation.v1.workflow-one.probe-ready-1",
    );
    expect(() => ballAnnotationStorageKey("../escape", "probe")).toThrow();
  });
});

describe("strict ball annotation response parsing", () => {
  it("keeps the browser journey fixture aligned with the frozen contract", () => {
    const fixture = ballAnnotationSessionFixture({
      profileId: "official-coco-yolo11s-sahi",
    });
    for (const frame of fixture.frames) {
      frame.current_annotation = {
        point_source_px: null,
        bbox_source_px: null,
        presence: "absent",
        visibility: "not_applicable",
        training_use: "background",
        annotation_state: "confirmed",
        scale_stratum: "not_applicable",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: [],
        provenance: "manual_human_annotation",
      };
    }
    refreshBallAnnotationProgress(fixture);
    const parsed = parseBallAnnotationSession(fixture);

    expect(parsed.view.frames).toHaveLength(6);
    expect(
      parseBallAnnotationFinalResult(
        developmentFinalResultFixture(fixture),
        parsed,
      ).dashboard,
    ).toEqual(expect.objectContaining({ status: "not_applicable" }));
  });

  it("accepts a confirmed bbox-only positive as a localizable development seed", () => {
    const fixture = ballAnnotationSessionFixture({
      profileId: "official-coco-yolo11s-sahi",
    });
    for (const [index, frame] of fixture.frames.entries()) {
      frame.current_annotation =
        index === 0
          ? {
              point_source_px: null,
              bbox_source_px: {
                left: 10,
                top: 10,
                right: 14,
                bottom: 14,
              },
              presence: "present",
              visibility: "visible",
              training_use: "positive",
              annotation_state: "confirmed",
              scale_stratum: "far",
              lighting_tag: "bright_sun",
              motion_occlusion_tags: [],
              provenance: "manual_human_annotation",
            }
          : {
              point_source_px: null,
              bbox_source_px: null,
              presence: "absent",
              visibility: "not_applicable",
              training_use: "background",
              annotation_state: "confirmed",
              scale_stratum: "not_applicable",
              lighting_tag: "bright_sun",
              motion_occlusion_tags: [],
              provenance: "manual_human_annotation",
            };
    }
    refreshBallAnnotationProgress(fixture);
    const session = parseBallAnnotationSession(fixture);
    const finalResult = developmentFinalResultFixture(fixture);

    expect(
      parseBallAnnotationFinalResult(finalResult, session).dashboard,
    ).toEqual(
      expect.objectContaining({
        status: "not_applicable",
        confirmedLocalizablePositiveFrames: 1,
      }),
    );
    expect(
      finalResult.package.dataset_expansion_eligibility.validation_evidence
        .localizable_positive_seed_count,
    ).toBe(1);
  });

  it("parses the shared service-generated golden contract directly", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const development = parseBallAnnotationSession(golden.development_session);
    const checkActive = parseBallAnnotationSession(golden.check_session_active);
    const checkReady = parseBallAnnotationSession(golden.check_session_ready);
    expect(development.view.dataRole).toBe("development");
    expect(checkActive.view.status).toBe("check_probe_queued");
    expect(checkReady.view.frames).toHaveLength(20);
    expect(
      parseBallPropagationJob(golden.propagation_job, development).view,
    ).toEqual(
      expect.objectContaining({
        status: "ready",
        pendingCount: 4,
        targetFrameIndices: [38, 39, 41, 42],
      }),
    );

    const revision = golden.annotation_revision;
    const revisionRequest = buildBallAnnotationMutation({
      operation: "set",
      mutationId: revision.mutation_id,
      expectedRevision: revision.expected_revision,
      annotation: revision.effective_annotation,
      suggestionDecision: {
        action: "accept",
        kind: revision.accepted_suggestion_kind,
        id: revision.accepted_suggestion_id,
        jobId: revision.accepted_suggestion_job_id,
        sha256: revision.accepted_suggestion_sha256,
      },
    });
    expect(
      parseBallAnnotationRevision(revision, `"${revision.annotation_etag}"`, {
        sessionId: revision.session_id,
        frameIndex: revision.frame_index,
        mutationId: revision.mutation_id,
        sourceWidth: golden.development_session.source.width,
        sourceHeight: golden.development_session.source.height,
        dataRole: "development",
        request: revisionRequest,
        suggestionDecision: {
          action: "accept",
          kind: revision.accepted_suggestion_kind,
          id: revision.accepted_suggestion_id,
          jobId: revision.accepted_suggestion_job_id,
          sha256: revision.accepted_suggestion_sha256,
        },
      }).revision,
    ).toBe(revision.revision);
    expect(
      parseBallAnnotationFinalResult(
        golden.development_final_result,
        development,
      ).dashboard,
    ).toEqual(
      expect.objectContaining({
        status: "not_applicable",
        datasetExpansionEligibility: expect.objectContaining({
          eligible: true,
        }),
      }),
    );
    expect(
      parseBallAnnotationFinalResult(golden.check_final_result, checkReady)
        .dashboard,
    ).not.toBeNull();
  });

  it.each([
    [
      "selection authority unknown key",
      (session: any) => {
        session.sampling_manifest.selection_authority.unsealed = true;
      },
    ],
    [
      "candidate-universe authority unknown key",
      (session: any) => {
        session.sampling_manifest.candidate_universe_authority.unsealed = true;
      },
    ],
    [
      "sampling manifest digest",
      (session: any) => {
        session.sampling_manifest.manifest_sha256 = sha("7");
      },
    ],
    [
      "metric profile digest",
      (session: any) => {
        session.metric_profile_sha256 = sha("7");
      },
    ],
  ] as const)("rejects tampered session %s", (_label, mutate) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const session = structuredClone(golden.check_session_ready);
    mutate(session);

    expect(() => parseBallAnnotationSession(session)).toThrow(
      /sampling|selection|candidate|metric|manifest|digest|schema/i,
    );
  });

  it("projects a frozen server session without trusting its internal frame URL", () => {
    const parsed = parseBallAnnotationSession(sessionFixture(), {
      dataRole: "development",
      developmentProbeJobIds: ["probe-ready-1"],
      lockedProfileId: "official-coco-yolo11s-sahi",
    });

    expect(parsed.targetFrameCount).toBe(1);
    expect(parsed.view.controlProfileId).toBe("current-coco-yolov8n-direct");
    expect(parsed.view.frames[0]).toEqual(
      expect.objectContaining({
        frameIndex: 10,
        annotationEtag: `"${sha("a")}"`,
        truePresentationTimestamp: {
          status: "not_collected",
          valueSeconds: null,
          method: null,
        },
        proxyBinding: null,
      }),
    );
  });

  it("retains a server-owned review-proxy blocker without revealing frames", () => {
    const value = sessionFixture();
    value.status = "blocked";
    value.stage = "review_proxy_required";
    value.error_code = "decode_integrity_failed";
    value.blocker_code = "review_proxy_required";
    value.review_proxy_repair = {
      eligible: true,
      action: "generate_verified_review_proxy",
      create_url: "/api/v1/detector-review-proxy-repairs",
      parent_probe_job_id: "probe-ready-1",
      parent_probe_report_sha256:
        value.lineage.development_probe_report_sha256s["probe-ready-1"],
      parent_probe_result_manifest_sha256:
        value.lineage.development_probe_result_manifest_sha256s[
          "probe-ready-1"
        ],
      parent_probe_record_sha256: sha("3"),
      blocked_session_record_sha256: sha("4"),
    };
    value.frames = [];
    value.progress = {
      annotated_frames: 0,
      total_frames: 0,
      unconfirmed_suggestions: 0,
      primary_annotated_frames: 0,
      primary_total_frames: 0,
      supplemental_annotated_frames: 0,
      supplemental_total_frames: 0,
      unconfirmed_propagation_suggestions: 0,
    };

    expect(parseBallAnnotationSession(value).view).toEqual(
      expect.objectContaining({
        status: "blocked",
        errorCode: "decode_integrity_failed",
        blockerCode: "review_proxy_required",
        retryFromSessionId: null,
        retryLineage: null,
        reviewProxyRepair: {
          eligible: true,
          action: "generate_verified_review_proxy",
          createUrl: "/api/v1/detector-review-proxy-repairs",
          parentProbeJobId: "probe-ready-1",
          parentProbeReportSha256: sha("b"),
          parentProbeResultManifestSha256: sha("c"),
          parentProbeRecordSha256: sha("3"),
          blockedSessionRecordSha256: sha("4"),
        },
        frames: [],
      }),
    );
  });

  it("rejects a repair capability that does not bind the blocked parent", () => {
    const value = sessionFixture();
    value.status = "blocked";
    value.error_code = "decode_integrity_failed";
    value.blocker_code = "review_proxy_required";
    value.frames = [];
    value.progress = {
      annotated_frames: 0,
      total_frames: 0,
      unconfirmed_suggestions: 0,
      primary_annotated_frames: 0,
      primary_total_frames: 0,
      supplemental_annotated_frames: 0,
      supplemental_total_frames: 0,
      unconfirmed_propagation_suggestions: 0,
    };
    value.review_proxy_repair = {
      eligible: true,
      action: "generate_verified_review_proxy",
      create_url: "/api/v1/detector-review-proxy-repairs",
      parent_probe_job_id: "other-probe",
      parent_probe_report_sha256: sha("b"),
      parent_probe_result_manifest_sha256: sha("c"),
      parent_probe_record_sha256: sha("3"),
      blocked_session_record_sha256: sha("4"),
    };

    expect(() => parseBallAnnotationSession(value)).toThrow(/capability/i);
  });

  it("accepts and retains a verified development review-proxy retry", async () => {
    const value = sessionFixture();
    const parentJobId = value.lineage.development_probe_job_ids[0];
    const childJobId = "probe-ready-proxy-child";
    value.session_id = "annotation-session-proxy-child";
    value.frames[0].frame_url =
      "/api/v1/ball-annotation-sessions/annotation-session-proxy-child/frames/10";
    value.frames[0].proxy_binding = await proxyBindingFor(value.frames[0]);
    value.lineage.development_probe_job_ids = [parentJobId, childJobId];
    for (const field of [
      "development_probe_report_sha256s",
      "development_probe_result_manifest_sha256s",
      "development_probe_execution_bundle_sha256s",
      "development_probe_frozen_profiles_sha256s",
    ]) {
      value.lineage[field][childJobId] = sha("6");
    }
    value.retry_from_session_id = "annotation-session-blocked";
    value.retry_lineage = {
      mode: "review_proxy_decode_upgrade",
      previous_session_id: "annotation-session-blocked",
      previous_error_code: "decode_integrity_failed",
      previous_blocker_code: "review_proxy_required",
      previous_lineage_sha256: sha("1"),
      current_lineage_sha256: sha("2"),
      sampling_manifest_sha256: value.sampling_manifest.manifest_sha256,
    };

    const parsed = parseBallAnnotationSession(value, {
      dataRole: "development",
      developmentProbeJobIds: [parentJobId, childJobId],
      lockedProfileId: value.locked_profile.profile_id,
    });

    expect(parsed.view.retryFromSessionId).toBe("annotation-session-blocked");
    expect(parsed.view.retryLineage).toEqual({
      mode: "review_proxy_decode_upgrade",
      previousSessionId: "annotation-session-blocked",
      previousErrorCode: "decode_integrity_failed",
      previousBlockerCode: "review_proxy_required",
      previousLineageSha256: sha("1"),
      currentLineageSha256: sha("2"),
      samplingManifestSha256: value.sampling_manifest.manifest_sha256,
    });
  });

  it("projects the verified proxy decode authority without claiming OpenCV", async () => {
    const value = sessionFixture();
    value.frames[0].proxy_binding = await proxyBindingFor(value.frames[0]);
    value.lineage.decode.requested_decode_mode = "direct";
    value.lineage.decode.effective_decode_mode = "direct_verified";
    value.lineage.decode.position_verification =
      "verified_review_proxy_frame_index_mapping_v1";

    expect(parseBallAnnotationSession(value).view.decode).toEqual({
      requestedMode: "direct",
      effectiveMode: "direct_verified",
      positionVerification: "verified_review_proxy_frame_index_mapping_v1",
    });
  });

  it("keeps unavailable source timing nullable for a verified CFR proxy", async () => {
    const value = sessionFixture();
    const frame = value.frames[0];
    const binding = await proxyBindingFor(frame);
    frame.source_timing_status = "not_collected";
    frame.decoder_reported_pos_msec = null;
    frame.decoder_time_seconds = null;
    binding.source_frame.timing_status = "not_collected";
    binding.source_frame.decoder_reported_pos_msec = null;
    binding.time_mapping.method = "exact_frame_index_to_verified_proxy_cfr_v1";
    binding.time_mapping.source_timing_status = "not_collected";
    binding.time_mapping.observed_offset_msec = null;
    binding.time_mapping.residual_msec = null;
    const bindingBody = { ...binding };
    delete bindingBody.binding_sha256;
    binding.binding_sha256 = computeReviewProxyBindingSha256(bindingBody);
    frame.proxy_binding = binding;

    const parsed = parseBallAnnotationSession(value).view.frames[0];
    expect(parsed.decoderReportedPosMsec).toBeNull();
    expect(parsed.decoderTimeSeconds).toBeNull();
    expect(parsed.proxyBinding?.sourceFrame.decoderReportedPosMsec).toBeNull();
    expect(parsed.proxyBinding?.observedOffsetMsec).toBeNull();
    expect(parsed.proxyBinding?.residualMsec).toBeNull();
  });

  it("rejects a development retry that is not a verified proxy upgrade", () => {
    const value = sessionFixture();
    value.retry_from_session_id = "annotation-session-blocked";
    value.retry_lineage = {
      mode: "same_authority",
      previous_session_id: "annotation-session-blocked",
      previous_error_code: null,
      previous_blocker_code: "review_proxy_required",
      previous_lineage_sha256: sha("1"),
      current_lineage_sha256: sha("2"),
      sampling_manifest_sha256: value.sampling_manifest.manifest_sha256,
    };

    expect(() => parseBallAnnotationSession(value)).toThrow(/proxy|retry/i);
  });

  it("projects a hash-bound proxy frame mapping", async () => {
    const value = sessionFixture();
    value.frames[0].proxy_binding = await proxyBindingFor(value.frames[0]);

    const frame = parseBallAnnotationSession(value).view.frames[0];

    expect(frame.proxyBinding).toEqual({
      proxySha256: sha("4"),
      proxySizeBytes: 4_096,
      proxyWidth: 1_920,
      proxyHeight: 1_080,
      mapSha256: sha("5"),
      bindingSha256: value.frames[0].proxy_binding.binding_sha256,
      sourceFrame: {
        frameIndex: 10,
        decoderReportedPosMsec: 500,
        sha256: sha("8"),
      },
      proxyFrame: {
        frameIndex: 10,
        decoderReportedPosMsec: 550,
        sha256: sha("6"),
      },
      mapTimeToleranceMsec: 2,
      declaredOffsetMsec: 50,
      observedOffsetMsec: 50,
      residualMsec: 0,
    });
  });

  it("accepts the backend's Python-canonical proxy binding digest", () => {
    const value = sessionFixture();
    value.frames[0].proxy_binding = {
      schema_version: "1.0",
      artifact_type: "ball_review_proxy_frame_binding",
      proxy: {
        sha256: sha("4"),
        size_bytes: 4_096,
        width: 1_920,
        height: 1_080,
      },
      map_sha256: sha("5"),
      source_frame: {
        frame_index: 10,
        timing_status: "observed",
        decoder_reported_pos_msec: 500,
        sha256: sha("8"),
      },
      proxy_frame: {
        frame_index: 10,
        timing_basis: "verified_cfr_frame_index_time_v1",
        cfr_time_msec: 550,
        sha256: sha("6"),
      },
      map_time_tolerance_msec: 2,
      declared_offset_msec: 50,
      time_mapping: {
        method: "explicit_per_frame_decoder_pos_msec_map_v1",
        source_timing_status: "observed",
        proxy_timing_basis: "verified_cfr_frame_index_time_v1",
        declared_offset_msec: 50,
        observed_offset_msec: 50,
        residual_msec: 0,
        tolerance_msec: 2,
      },
      // Generated by the Python authority using canonical_sha256, where
      // schema-declared floats are serialized as 500.0, 50.0, and so on.
      binding_sha256:
        "c3fefa6f148245d6491f84d966df02010cf71e487f92da51672b74ead8486535",
    };

    expect(
      parseBallAnnotationSession(value).view.frames[0].proxyBinding,
    ).toEqual(
      expect.objectContaining({
        bindingSha256: value.frames[0].proxy_binding.binding_sha256,
      }),
    );
  });

  it("projects every real backend-generated proxy binding from the golden session", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;

    const parsed = parseBallAnnotationSession(golden.development_proxy_session);

    expect(parsed.view.frames).toHaveLength(6);
    expect(
      parsed.view.frames.every((frame) => frame.proxyBinding !== null),
    ).toBe(true);
    expect(parsed.view.frames[0]?.proxyBinding).toEqual(
      expect.objectContaining({
        proxySha256: sha("8"),
        mapSha256:
          "8094244a91c3019fa9a393598ebd6c06f46c682d71b3a1b3da908ef9a9a42829",
        bindingSha256:
          "a7978bf856a02605d8f6bd8b7e4de210b0575401cbbe87593de7914a92262d93",
        sourceFrame: expect.objectContaining({
          frameIndex: 0,
        }),
        proxyFrame: expect.objectContaining({
          frameIndex: 0,
          decoderReportedPosMsec: 0,
        }),
        declaredOffsetMsec: 0,
        mapTimeToleranceMsec: 0.1,
      }),
    );
    expect(
      parsed.view.frames[0]?.proxyBinding?.sourceFrame.decoderReportedPosMsec,
    ).toBeNull();
    expect(parsed.view.frames[0]?.proxyBinding?.observedOffsetMsec).toBeNull();
  });

  it.each([
    [
      "digest tamper",
      (binding: any) => {
        binding.proxy.sha256 = sha("9");
      },
    ],
    [
      "source-frame tamper",
      (binding: any) => {
        binding.source_frame.frame_index += 1;
      },
    ],
    [
      "timing-map tamper",
      (binding: any) => {
        binding.proxy_frame.cfr_time_msec += 10;
      },
    ],
    [
      "unknown field",
      (binding: any) => {
        binding.unsealed = true;
      },
    ],
  ])("rejects proxy binding %s", async (_label, tamper) => {
    const value = sessionFixture();
    value.frames[0].proxy_binding = await proxyBindingFor(value.frames[0]);
    tamper(value.frames[0].proxy_binding);

    expect(() => parseBallAnnotationSession(value)).toThrow(/proxy/i);
  });

  it.each(["accepted", "dismissed"] as const)(
    "retains a %s detector candidate while excluding it from pending progress",
    (decision) => {
      const value = sessionWithDetectorCandidate(decision);

      const parsed = parseBallAnnotationSession(value);

      expect(parsed.view.frames[0].suggestedCandidates).toEqual([
        expect.objectContaining({
          candidateId: "candidate-one",
          decision,
        }),
      ]);
      expect(parsed.view.progress.unconfirmedSuggestions).toBe(0);
    },
  );

  it.each([
    [
      "missing development control binding",
      (value: any) => {
        value.control_profile_id = null;
        value.control_profile = null;
      },
    ],
    [
      "control binding with the locked profile",
      (value: any) => {
        value.control_profile_id = value.locked_profile.profile_id;
        value.control_profile = value.locked_profile;
      },
    ],
    [
      "mismatched control identity",
      (value: any) => {
        value.control_profile.profile_id = "different-control";
      },
    ],
  ])("rejects %s", (_, mutate) => {
    const value = sessionFixture();
    mutate(value);
    expect(() => parseBallAnnotationSession(value)).toThrow(
      /Control profile binding/,
    );
  });

  it.each([
    [
      "unknown top-level authority",
      (value: any) => (value.frame_indices = [10]),
    ],
    ["forged frame URL", (value: any) => (value.frames[0].frame_url = "/evil")],
    ["mismatched progress", (value: any) => (value.progress.total_frames = 2)],
    [
      "training-shaped development target",
      (value: any) => (value.sampling_manifest.target_frame_count = 20),
    ],
  ])("fails closed on %s", (_, mutate) => {
    const value = sessionFixture();
    mutate(value);
    expect(() => parseBallAnnotationSession(value)).toThrow();
  });

  it.each([
    ["non-object response", () => null],
    ["non-string status", () => ({ ...sessionFixture(), status: 42 })],
    [
      "non-finite source FPS",
      () => {
        const value = sessionFixture();
        value.source.fps = Number.NaN;
        return value;
      },
    ],
    [
      "non-integer source width",
      () => {
        const value = sessionFixture();
        value.source.width = 1.5;
        return value;
      },
    ],
    [
      "empty source path",
      () => {
        const value = sessionFixture();
        value.source.relative_path = "";
        return value;
      },
    ],
    [
      "unverified decode position",
      () => {
        const value = sessionFixture();
        value.lineage.decode.position_verification = "best_effort";
        return value;
      },
    ],
    [
      "non-array development lineage",
      () => {
        const value = sessionFixture();
        value.lineage.development_probe_job_ids = null as any;
        return value;
      },
    ],
    [
      "duplicated development lineage",
      () => {
        const value = sessionFixture();
        value.lineage.development_probe_job_ids = [
          "probe-ready-1",
          "probe-ready-1",
        ];
        return value;
      },
    ],
    [
      "missing applicability rows",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.strata_applicability.scale = [];
        return value;
      },
    ],
    [
      "post-reveal applicability evidence",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.strata_applicability.scale[0].evidence.declared_before_reveal = false;
        return value;
      },
    ],
    [
      "duplicated applicability stratum",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.strata_applicability.scale[1].stratum = "near";
        return value;
      },
    ],
    [
      "invalid temporal group authority",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.groups[0].profile_id = "other-sampler";
        return value;
      },
    ],
    [
      "invalid pre-reveal lighting group",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.groups[0].pre_reveal_lighting_stratum =
          "indoor_neon";
        return value;
      },
    ],
    [
      "decode/source mismatch",
      () => {
        const value = sessionFixture();
        value.lineage.decode.width = 4096;
        return value;
      },
    ],
    [
      "manifest/profile mismatch",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.locked_profile_id = "other-profile";
        return value;
      },
    ],
    [
      "role-lock mismatch",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.locked_before_probe = true;
        return value;
      },
    ],
    [
      "non-array manifest frames",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.frame_indices = null as any;
        return value;
      },
    ],
    [
      "group/frame mismatch",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.frame_indices = [11];
        return value;
      },
    ],
    [
      "development self-exclusion",
      () => {
        const value = sessionFixture();
        value.sampling_manifest.excluded_development_groups = [
          value.sampling_manifest.groups[0],
        ];
        return value;
      },
    ],
    [
      "unknown sampling profile",
      () => {
        const value = sessionFixture();
        value.sampling_profile_id = "other-sampler";
        return value;
      },
    ],
    [
      "empty applicable strata",
      () => {
        const value = sessionFixture();
        value.applicable_scale_strata = [];
        return value;
      },
    ],
    [
      "applicability list/evidence mismatch",
      () => {
        const value = sessionFixture();
        value.applicable_scale_strata = ["near"];
        return value;
      },
    ],
    [
      "development check-probe leakage",
      () => {
        const value = sessionFixture();
        value.check_probe_job_id = "check-probe-one";
        return value;
      },
    ],
    [
      "invalid blocker lifecycle",
      () => {
        const value = sessionFixture();
        value.blocker_code = "blocked-without-status";
        return value;
      },
    ],
    [
      "non-array frames",
      () => {
        const value = sessionFixture();
        value.frames = null as any;
        return value;
      },
    ],
    [
      "missing true presentation timestamp status",
      () => {
        const value = sessionFixture();
        delete value.frames[0].true_presentation_timestamp;
        return value;
      },
    ],
    [
      "fabricated true presentation timestamp",
      () => {
        const value = sessionFixture();
        value.frames[0].true_presentation_timestamp = {
          status: "collected",
          value_seconds: 0.5,
          method: "container_pts",
        };
        return value;
      },
    ],
    [
      "too many detector candidates",
      () => {
        const value = sessionFixture();
        value.frames[0].suggested_candidates = Array.from(
          { length: 6 },
          () => ({}),
        );
        return value;
      },
    ],
    [
      "candidate truth authority",
      () => {
        const value = sessionFixture();
        value.frames[0].suggested_candidates = [
          {
            candidate_id: "candidate-one",
            profile_id: "official-coco-yolo11s-sahi",
            rank: 1,
            bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
            confidence: 0.9,
            annotation_state: "confirmed",
            training_use: "excluded",
            truth_status: "unconfirmed_suggestion",
            suggestion_job_id: "probe-ready-1",
            suggestion_sha256: sha("1"),
            decision: "pending",
          },
        ];
        return value;
      },
    ],
    [
      "candidate score overflow",
      () => {
        const value = sessionFixture();
        value.frames[0].suggested_candidates = [
          {
            candidate_id: "candidate-one",
            profile_id: "official-coco-yolo11s-sahi",
            rank: 6,
            bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
            confidence: 0.9,
            annotation_state: "suggested",
            training_use: "excluded",
            truth_status: "unconfirmed_suggestion",
            suggestion_job_id: "probe-ready-1",
            suggestion_sha256: sha("1"),
            decision: "pending",
          },
        ];
        return value;
      },
    ],
    [
      "candidate without an authoritative decision",
      () => {
        const value = sessionWithDetectorCandidate();
        delete value.frames[0].suggested_candidates[0].decision;
        return value;
      },
    ],
    [
      "candidate with an unknown authoritative decision",
      () => {
        const value = sessionWithDetectorCandidate();
        value.frames[0].suggested_candidates[0].decision = "reviewed";
        return value;
      },
    ],
    [
      "primary flag mismatch",
      () => {
        const value = sessionFixture();
        value.frames[0].primary_sample = false;
        return value;
      },
    ],
    [
      "non-array propagation jobs",
      () => {
        const value = sessionFixture();
        value.frames[0].propagation_job_ids = null as any;
        return value;
      },
    ],
    [
      "propagation target without lineage",
      () => {
        const value = sessionFixture();
        value.frames[0].frame_role = "propagation_target";
        value.frames[0].primary_sample = false;
        return value;
      },
    ],
    [
      "unordered duplicate frames",
      () => {
        const value = sessionFixture();
        value.frames.push({ ...value.frames[0] });
        value.progress.total_frames = 2;
        value.progress.primary_total_frames = 2;
        return value;
      },
    ],
    [
      "frame/manifest temporal mismatch",
      () => {
        const value = sessionFixture();
        value.frames[0].temporal_group_id = sha("1");
        return value;
      },
    ],
    [
      "revealed frame/manifest mismatch",
      () => {
        const value = sessionFixture();
        value.frames = [];
        value.progress.total_frames = 0;
        value.progress.primary_total_frames = 0;
        return value;
      },
    ],
    [
      "final result URL mismatch",
      () => {
        const value = sessionFixture();
        value.status = "finalized";
        value.final_package = {
          result_url: "/wrong",
          manifest_sha256: sha("1"),
          package_sha256: sha("2"),
          report_sha256: sha("3"),
          status: "not_applicable",
        } as any;
        return value;
      },
    ],
    [
      "non-final session with a final package",
      () => {
        const value = sessionFixture();
        value.final_package = {
          result_url:
            "/api/v1/ball-annotation-sessions/annotation-session-1/result",
          manifest_sha256: sha("1"),
          package_sha256: sha("2"),
          report_sha256: sha("3"),
          status: "not_applicable",
        } as any;
        return value;
      },
    ],
  ])("rejects corrupt session authority: %s", (_, makeValue) => {
    expect(() => parseBallAnnotationSession(makeValue())).toThrow();
  });

  it.each([
    ["role", { dataRole: "check" as const }],
    ["locked profile", { lockedProfileId: "other-profile" }],
    ["development lineage", { developmentProbeJobIds: ["other-probe"] }],
  ])("binds the response to the expected %s", (_, expected) => {
    expect(() =>
      parseBallAnnotationSession(sessionFixture(), expected),
    ).toThrow(/intent|profile|probes|role/i);
  });

  it("accepts the bounded finalizing status while the result is committing", () => {
    const value = sessionFixture();
    value.status = "finalizing";
    expect(parseBallAnnotationSession(value).view.status).toBe("finalizing");
  });

  it("accepts only source-bound, self-checked propagation suggestions as pending truth", () => {
    const value = sessionFixture();
    const suggestion: any = {
      suggestion_id: "suggestion-1",
      frame_index: 11,
      temporal_group_id: sha("9"),
      temporal_group: {
        group_id: sha("9"),
        profile_id: "tiny_ball_temporal_groups_v1",
        source_sha256: sha("3"),
        seed_frame_index: 10,
        start_frame: 8,
        end_frame: 12,
        derivative_family: [8, 12],
        canonical_moment_id: sha("0"),
        derivative_family_id: sha("9"),
        ancestry_profile: "source-proxy-crop-tile-propagation-closure-v1",
        derivative: {
          artifact_type: "propagation",
          artifact_id: "propagation-job-1",
          inheritance_rule: "inherit-source-group-without-regrouping-v1",
        },
        derivative_binding_sha256: sha("b"),
      },
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      training_use: "excluded",
      annotation_state: "suggested",
      provenance: "tiny_ball_bounded_template_flow_v1",
      source_frame_sha256: sha("b"),
      self_check: {
        match_score: 0.9,
        backward_match_score: 0.8,
        forward_backward_error_px: 0.4,
        step_displacement_px: 2,
      },
      suggestion_job_id: "propagation-job-1",
      suggestion_sha256: sha("d"),
      pending_human_confirmation: true,
      human_confirmation: null,
      human_decision: null,
    };
    (value.frames as any).push({
      frame_index: 11,
      source_frame_sha256: sha("b"),
      source_frame_size_bytes: 2048,
      suggested_candidates: [],
      source_timing_status: "observed",
      decoder_reported_pos_msec: 550,
      decoder_time_seconds: 0.55,
      display_time_seconds: 0.55,
      true_presentation_timestamp: {
        status: "not_collected",
        value_seconds: null,
        method: null,
      },
      proxy_binding: null,
      temporal_group_id: sha("9"),
      frame_url:
        "/api/v1/ball-annotation-sessions/annotation-session-1/frames/11",
      annotation_revision: 0,
      annotation_etag: sha("c"),
      current_annotation: null,
      frame_role: "propagation_target",
      primary_sample: false,
      propagation_job_ids: ["propagation-job-1"],
      propagation_suggestions: [suggestion],
    });
    value.progress.total_frames = 2;
    value.progress.supplemental_total_frames = 1;
    value.progress.unconfirmed_propagation_suggestions = 1;

    const parsed = parseBallAnnotationSession(value);
    expect(parsed.view.frames[1]).toEqual(
      expect.objectContaining({
        frameRole: "propagation_target",
        primarySample: false,
        propagationSuggestions: [
          expect.objectContaining({
            suggestionId: "suggestion-1",
            pendingHumanConfirmation: true,
            trainingUse: "excluded",
          }),
        ],
      }),
    );

    const targetFrame = value.frames[1] as any;
    suggestion.pending_human_confirmation = false;
    suggestion.human_confirmation = {
      revision_id: "revision-confirmed-1",
      revision: 1,
      operator_id: "operator-one",
      center_error_px: 0.5,
      iou: 0.9,
      corrected: true,
      confirmed_at: "2026-07-18T00:01:00Z",
    };
    targetFrame.annotation_revision = 1;
    targetFrame.annotation_etag = sha("e");
    targetFrame.current_annotation = {
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      training_use: "positive",
      annotation_state: "confirmed",
      scale_stratum: "far",
      lighting_tag: "bright_sun",
      motion_occlusion_tags: [],
      provenance: "propagation_suggestion_human_confirmed",
    };
    value.progress.annotated_frames = 1;
    value.progress.supplemental_annotated_frames = 1;
    value.progress.unconfirmed_propagation_suggestions = 0;
    expect(
      parseBallAnnotationSession(value).view.frames[1].propagationSuggestions,
    ).toEqual([
      expect.objectContaining({
        decision: "confirmed",
        confirmationRevision: 1,
        pendingHumanConfirmation: false,
      }),
    ]);

    suggestion.human_confirmation = null;
    suggestion.human_decision = {
      decision: "dismissed_manual_annotation",
      revision_id: "revision-dismissed-2",
      revision: 2,
      operator_id: "operator-one",
      decided_at: "2026-07-18T00:02:00Z",
    };
    targetFrame.annotation_revision = 2;
    targetFrame.annotation_etag = sha("f");
    targetFrame.current_annotation = {
      point_source_px: null,
      bbox_source_px: null,
      presence: "absent",
      visibility: "not_applicable",
      training_use: "background",
      annotation_state: "confirmed",
      scale_stratum: "not_applicable",
      lighting_tag: "bright_sun",
      motion_occlusion_tags: [],
      provenance: "suggestion_dismissed_manual",
    };
    expect(
      parseBallAnnotationSession(value).view.frames[1].propagationSuggestions,
    ).toEqual([
      expect.objectContaining({
        decision: "dismissed",
        confirmationRevision: 2,
        pendingHumanConfirmation: false,
      }),
    ]);

    suggestion.pending_human_confirmation = true;
    suggestion.human_decision = null;
    targetFrame.annotation_revision = 0;
    targetFrame.annotation_etag = sha("c");
    targetFrame.current_annotation = null;
    value.progress.annotated_frames = 0;
    value.progress.supplemental_annotated_frames = 0;
    value.progress.unconfirmed_propagation_suggestions = 1;

    suggestion.source_frame_sha256 = sha("d");
    expect(() => parseBallAnnotationSession(value)).toThrow(/source binding/);
    suggestion.source_frame_sha256 = sha("b");
    suggestion.self_check.match_score = 2;
    expect(() => parseBallAnnotationSession(value)).toThrow(/self-check/);
  });

  it.each([
    [
      "point outside the source",
      (annotation: any) => (annotation.point_source_px.x = 5120),
    ],
    [
      "box outside the source",
      (annotation: any) => (annotation.bbox_source_px.right = 5121),
    ],
    [
      "point and box centers disagree",
      (annotation: any) => (annotation.point_source_px.x = 120),
    ],
    [
      "box bounds are inverted",
      (annotation: any) => (annotation.bbox_source_px.right = 80),
    ],
    [
      "suggestion claims positive truth",
      (annotation: any) => (annotation.annotation_state = "suggested"),
    ],
    [
      "absent frame carries coordinates",
      (annotation: any) => {
        annotation.presence = "absent";
        annotation.visibility = "not_applicable";
        annotation.scale_stratum = "not_applicable";
      },
    ],
  ])("rejects semantic annotation corruption: %s", (_, mutate) => {
    const value = sessionFixture();
    const annotation = {
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      training_use: "positive",
      annotation_state: "confirmed",
      scale_stratum: "far",
      lighting_tag: "bright_sun",
      motion_occlusion_tags: [],
      provenance: "manual_human_annotation",
    };
    mutate(annotation);
    (value.frames[0] as any).current_annotation = annotation;
    value.progress.annotated_frames = 1;
    value.progress.primary_annotated_frames = 1;
    expect(() => parseBallAnnotationSession(value)).toThrow();
  });

  it.each([
    [
      "unknown excluded frame",
      {
        point_source_px: null,
        bbox_source_px: null,
        presence: "unknown",
        visibility: "unresolvable",
        training_use: "excluded",
        annotation_state: "confirmed",
        scale_stratum: "not_applicable",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: [],
        provenance: "manual_human_annotation",
      },
    ],
    [
      "present but unresolvable excluded frame",
      {
        point_source_px: null,
        bbox_source_px: null,
        presence: "present",
        visibility: "unresolvable",
        training_use: "excluded",
        annotation_state: "confirmed",
        scale_stratum: "not_applicable",
        lighting_tag: "shadow",
        motion_occlusion_tags: ["occluded"],
        provenance: "manual_human_annotation",
      },
    ],
    [
      "box-only development truth",
      {
        point_source_px: null,
        bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
        presence: "present",
        visibility: "partial",
        training_use: "positive",
        annotation_state: "confirmed",
        scale_stratum: "far",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: ["motion_blurred"],
        provenance: "manual_human_annotation",
      },
    ],
  ])("accepts internally consistent %s", (_, annotation) => {
    const value = sessionFixture();
    (value.frames[0] as any).current_annotation = annotation;
    value.progress.annotated_frames = 1;
    value.progress.primary_annotated_frames = 1;
    expect(
      parseBallAnnotationSession(value).view.frames[0].currentAnnotation,
    ).toBeTruthy();
  });

  it.each([
    [
      "non-array motion tags",
      (annotation: any) => (annotation.motion_occlusion_tags = null),
    ],
    [
      "duplicated motion tags",
      (annotation: any) =>
        (annotation.motion_occlusion_tags = ["occluded", "occluded"]),
    ],
    [
      "whitespace provenance",
      (annotation: any) => (annotation.provenance = " manual_human_annotation"),
    ],
    [
      "unknown geometry",
      (annotation: any) => {
        annotation.presence = "unknown";
        annotation.visibility = "unresolvable";
        annotation.training_use = "excluded";
        annotation.scale_stratum = "not_applicable";
      },
    ],
  ])("rejects malformed annotation detail: %s", (_, mutate) => {
    const value = sessionFixture();
    const annotation: any = {
      point_source_px: { x: 100, y: 100 },
      bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      training_use: "positive",
      annotation_state: "confirmed",
      scale_stratum: "far",
      lighting_tag: "bright_sun",
      motion_occlusion_tags: [],
      provenance: "manual_human_annotation",
    };
    mutate(annotation);
    value.frames[0].current_annotation = annotation;
    value.progress.annotated_frames = 1;
    value.progress.primary_annotated_frames = 1;
    expect(() => parseBallAnnotationSession(value)).toThrow();
  });

  it("requires a revision body and strong response ETag to name the same mutation", () => {
    const revision = {
      schema_version: "1.0",
      artifact_type: "ball_annotation_revision",
      revision_id: "revision-1",
      session_id: "annotation-session-1",
      frame_index: 10,
      revision: 1,
      operation: "delete",
      mutation_id: "mutation-1",
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
      effective_annotation: null,
      operator_id: "operator-one",
      annotation_etag: sha("b"),
      created_at: "2026-07-18T00:00:00Z",
    };
    const request = buildBallAnnotationMutation({
      operation: "delete",
      mutationId: "mutation-1",
      expectedRevision: 0,
    });
    expect(
      parseBallAnnotationRevision(revision, `"${sha("b")}"`, {
        sessionId: "annotation-session-1",
        frameIndex: 10,
        mutationId: "mutation-1",
        sourceWidth: 5120,
        sourceHeight: 1440,
        dataRole: "development",
        request,
      }),
    ).toEqual(
      expect.objectContaining({ revision: 1, annotationEtag: `"${sha("b")}"` }),
    );
    expect(() =>
      parseBallAnnotationRevision(revision, `"${sha("c")}"`, {
        sessionId: "annotation-session-1",
        frameIndex: 10,
        mutationId: "mutation-1",
        sourceWidth: 5120,
        sourceHeight: 1440,
        dataRole: "development",
        request,
      }),
    ).toThrow(/ETag/);
    for (const invalidEtag of [null, `W/"${sha("b")}"`]) {
      expect(() =>
        parseBallAnnotationRevision(revision, invalidEtag, {
          sessionId: "annotation-session-1",
          frameIndex: 10,
          mutationId: "mutation-1",
          sourceWidth: 5120,
          sourceHeight: 1440,
          dataRole: "development",
          request,
        }),
      ).toThrow(/ETag/);
    }
  });

  it("accepts only an exact suggestion identity, job, and authority digest binding", () => {
    const accepted = {
      schema_version: "1.0",
      artifact_type: "ball_annotation_revision",
      revision_id: "revision-accepted",
      session_id: "annotation-session-1",
      frame_index: 10,
      revision: 2,
      operation: "set",
      mutation_id: "mutation-accepted",
      expected_revision: 1,
      supersedes_revision: 1,
      undo_revision: null,
      accepted_suggestion_kind: "propagation",
      accepted_suggestion_id: "suggestion-one",
      accepted_suggestion_job_id: "propagation-job-one",
      accepted_suggestion_sha256: sha("6"),
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
      effective_annotation: {
        point_source_px: { x: 100, y: 100 },
        bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
        presence: "present",
        visibility: "visible",
        training_use: "positive",
        annotation_state: "confirmed",
        scale_stratum: "far",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: [],
        provenance: "propagation_suggestion_human_confirmed",
      },
      operator_id: "operator-one",
      annotation_etag: sha("b"),
      created_at: "2026-07-18T00:00:00Z",
    };
    const expected = {
      sessionId: "annotation-session-1",
      frameIndex: 10,
      mutationId: "mutation-accepted",
      sourceWidth: 5120,
      sourceHeight: 1440,
      dataRole: "development" as const,
      request: buildBallAnnotationMutation({
        operation: "set",
        mutationId: "mutation-accepted",
        expectedRevision: 1,
        annotation: accepted.effective_annotation,
        suggestionDecision: {
          action: "accept",
          kind: "propagation",
          id: "suggestion-one",
          jobId: "propagation-job-one",
          sha256: sha("6"),
        },
      }),
      suggestionDecision: {
        action: "accept" as const,
        kind: "propagation" as const,
        id: "suggestion-one",
        jobId: "propagation-job-one",
        sha256: sha("6"),
      },
    };

    expect(
      parseBallAnnotationRevision(accepted, `"${sha("b")}"`, expected),
    ).toEqual(expect.objectContaining({ revision: 2, operation: "set" }));

    for (const key of [
      "accepted_suggestion_kind",
      "accepted_suggestion_id",
      "accepted_suggestion_job_id",
      "accepted_suggestion_sha256",
    ] as const) {
      const missing = { ...accepted } as Record<string, unknown>;
      delete missing[key];
      expect(() =>
        parseBallAnnotationRevision(missing, `"${sha("b")}"`, expected),
      ).toThrow(/schema/);
    }
    expect(() =>
      parseBallAnnotationRevision(
        { ...accepted, unexpected: true },
        `"${sha("b")}"`,
        expected,
      ),
    ).toThrow(/schema/);

    for (const mismatch of [
      {
        ...expected,
        suggestionDecision: {
          ...expected.suggestionDecision,
          id: "suggestion-two",
        },
      },
      {
        ...expected,
        suggestionDecision: {
          ...expected.suggestionDecision,
          jobId: "propagation-job-two",
        },
      },
      {
        ...expected,
        suggestionDecision: {
          ...expected.suggestionDecision,
          sha256: sha("7"),
        },
      },
    ]) {
      expect(() =>
        parseBallAnnotationRevision(accepted, `"${sha("b")}"`, mismatch),
      ).toThrow(/suggestion lineage/);
    }
    expect(() =>
      parseBallAnnotationRevision(
        { ...accepted, accepted_suggestion_job_id: null },
        `"${sha("b")}"`,
        expected,
      ),
    ).toThrow(/suggestion lineage/);

    for (const [name, mutation] of [
      ["operation", { operation: "delete" }],
      ["expected revision", { expected_revision: 0 }],
      [
        "submitted annotation",
        {
          effective_annotation: {
            ...accepted.effective_annotation,
            point_source_px: { x: 101, y: 100 },
          },
        },
      ],
    ] as const) {
      expect(
        () =>
          parseBallAnnotationRevision(
            { ...accepted, ...mutation },
            `"${sha("b")}"`,
            expected,
          ),
        name,
      ).toThrow(/mutation intent/);
    }
  });

  it("rejects a check revision that has only a point and no human box", () => {
    const revision = {
      schema_version: "1.0",
      artifact_type: "ball_annotation_revision",
      revision_id: "revision-2",
      session_id: "annotation-session-1",
      frame_index: 10,
      revision: 2,
      operation: "set",
      mutation_id: "mutation-2",
      expected_revision: 1,
      supersedes_revision: 1,
      undo_revision: null,
      accepted_suggestion_kind: null,
      accepted_suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
      effective_annotation: {
        point_source_px: { x: 100, y: 100 },
        bbox_source_px: null,
        presence: "present",
        visibility: "visible",
        training_use: "excluded",
        annotation_state: "confirmed",
        scale_stratum: "far",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: [],
        provenance: "manual_human_annotation",
      },
      operator_id: "operator-one",
      annotation_etag: sha("b"),
      created_at: "2026-07-18T00:00:00Z",
    };
    expect(() =>
      parseBallAnnotationRevision(revision, `"${sha("b")}"`, {
        sessionId: "annotation-session-1",
        frameIndex: 10,
        mutationId: "mutation-2",
        sourceWidth: 5120,
        sourceHeight: 1440,
        dataRole: "check",
        request: buildBallAnnotationMutation({
          operation: "set",
          mutationId: "mutation-2",
          expectedRevision: 1,
          annotation: revision.effective_annotation,
        }),
      }),
    ).toThrow(/localizable/);
  });

  it("keeps a finalized development package non-training and non-check", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.development_session,
    );
    const finalResult = structuredClone(golden.development_final_result);
    expect(parseBallAnnotationFinalResult(finalResult, parsedSession)).toEqual(
      expect.objectContaining({
        packageSha256: finalResult.package.package_sha256,
        reportSha256: finalResult.feasibility_report.report_sha256,
        dashboard: expect.objectContaining({
          status: "not_applicable",
          datasetExpansionEligibility: expect.objectContaining({
            eligible: true,
          }),
        }),
      }),
    );
    finalResult.package.training_eligible = true;
    expect(() =>
      parseBallAnnotationFinalResult(finalResult, parsedSession),
    ).toThrow(/training boundary/);
  });

  it.each([
    "session_request_authority",
    "detector_probe_authorities",
    "frame_review_proxy_authority",
  ])("requires final package authority field %s", (field) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.development_session,
    );
    const result = structuredClone(golden.development_final_result);
    delete result.package[field];

    expect(() => parseBallAnnotationFinalResult(result, parsedSession)).toThrow(
      /schema|authority/i,
    );
  });

  it("rejects tampered and copied session-request authority", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const development = parseBallAnnotationSession(golden.development_session);
    const tampered = structuredClone(golden.development_final_result);
    tampered.package.session_request_authority.normalized_request.operator_id =
      "tampered-operator";
    expect(() => parseBallAnnotationFinalResult(tampered, development)).toThrow(
      /request authority|request digest/i,
    );

    const check = parseBallAnnotationSession(golden.check_session_ready);
    const copied = structuredClone(golden.check_final_result);
    copied.package.session_request_authority = structuredClone(
      golden.development_final_result.package.session_request_authority,
    );
    expect(() => parseBallAnnotationFinalResult(copied, check)).toThrow(
      /request authority|request selection/i,
    );
  });

  it("rejects tampered, copied, and unknown detector authority", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const development = parseBallAnnotationSession(golden.development_session);
    const tampered = structuredClone(golden.development_final_result);
    tampered.package.detector_probe_authorities[0].frozen_request.source_sha256 =
      sha("7");
    expect(() => parseBallAnnotationFinalResult(tampered, development)).toThrow(
      /detector.*request|detector.*authority/i,
    );

    const check = parseBallAnnotationSession(golden.check_session_ready);
    const copied = structuredClone(golden.check_final_result);
    copied.package.detector_probe_authorities = structuredClone(
      golden.development_final_result.package.detector_probe_authorities,
    );
    expect(() => parseBallAnnotationFinalResult(copied, check)).toThrow(
      /detector.*lineage|detector.*authority/i,
    );

    const unknown = structuredClone(golden.development_final_result);
    unknown.package.detector_probe_authorities[0].job_id = "unknown-probe";
    expect(() => parseBallAnnotationFinalResult(unknown, development)).toThrow(
      /detector.*lineage|detector.*authority/i,
    );
  });

  it.each(["probe_job_record", "probe_result_manifest"])(
    "rejects unknown detector raw-authority keys in %s",
    (field) => {
      const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
      const session = parseBallAnnotationSession(golden.development_session);
      const result = structuredClone(golden.development_final_result);
      const authority = result.package.detector_probe_authorities[0];
      authority[field].unsealed = true;
      resealDetectorAuthority(authority);

      expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
        /detector|manifest|job record|schema/i,
      );
    },
  );

  it("rejects a coherently rewritten raw detector request with stale sealed job digests", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const session = parseBallAnnotationSession(golden.development_session);
    const result = structuredClone(golden.development_final_result);
    const authority = result.package.detector_probe_authorities[0];
    const originalCanonicalJobSha256 = authority.canonical_job_record_sha256;
    const originalJobAuthoritySha256 = authority.job_record_authority_sha256;
    authority.frozen_request.base_config_relative_path =
      "config/coherently-rewritten.yaml";
    const requestSha256 = pythonCanonicalSha256Sync(
      authority.frozen_request,
      [],
    );
    const intent = { ...authority.frozen_request };
    delete intent.retry_from_job_id;
    const intentSha256 = pythonCanonicalSha256Sync(intent, []);
    const resourceFields = [
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
    ];
    authority.request_sha256 = requestSha256;
    authority.intent_sha256 = intentSha256;
    authority.resource_sha256 = pythonCanonicalSha256Sync(
      Object.fromEntries(
        resourceFields.map((field) => [field, authority.frozen_request[field]]),
      ),
      [],
    );
    authority.probe_report.request_sha256 = requestSha256;
    authority.probe_report.lineage.intent_sha256 = intentSha256;
    authority.probe_result_manifest.request_sha256 = requestSha256;
    authority.probe_job_record.request_sha256 = requestSha256;
    authority.probe_job_record.intent_sha256 = intentSha256;
    authority.probe_job_record.frozen_request = structuredClone(
      authority.frozen_request,
    );
    authority.probe_job_record.report = structuredClone(authority.probe_report);

    expect(authority.canonical_job_record_sha256).toBe(
      originalCanonicalJobSha256,
    );
    expect(authority.job_record_authority_sha256).toBe(
      originalJobAuthoritySha256,
    );
    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      /detector|report|job record|digest/i,
    );
  });

  it("binds every sealed frame to a current detector/proxy authority", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const development = parseBallAnnotationSession(golden.development_session);
    const unknownFrame = structuredClone(golden.development_final_result);
    unknownFrame.package.frame_evidence[0].probe_evidence.probe_job_id =
      "unknown-probe";
    expect(() =>
      parseBallAnnotationFinalResult(unknownFrame, development),
    ).toThrow(/frame.*authority|probe.*authority/i);

    const unknownProxy = structuredClone(golden.development_final_result);
    const detector = unknownProxy.package.detector_probe_authorities[0];
    unknownProxy.package.frame_review_proxy_authority = {
      probe_job_id: "unknown-proxy",
      probe_report_sha256: detector.probe_report_sha256,
      probe_result_manifest_sha256: detector.probe_result_manifest_sha256,
      probe_report: detector.probe_report,
      probe_result_manifest: detector.probe_result_manifest,
      review_proxy_manifest: {},
      historical_probe_authority: null,
    };
    expect(() =>
      parseBallAnnotationFinalResult(unknownProxy, development),
    ).toThrow(/proxy.*authority/i);
  });

  it.each([
    ["supplemental_frame_indices", 21],
    ["frame_evidence", 71],
    ["frame_media", 71],
    ["propagation_reports", 21],
  ] as const)("enforces the public %s bound", (field, count) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.development_session,
    );
    const result = structuredClone(golden.development_final_result);
    const seed = result.package[field][0] ?? 0;
    result.package[field] = Array.from({ length: count }, () =>
      structuredClone(seed),
    );

    expect(() => parseBallAnnotationFinalResult(result, parsedSession)).toThrow(
      /bound|collection|frames|invalid/i,
    );
  });

  it.each(["revision_chain", "frame_evidence", "frame_media"])(
    "requires non-empty final package %s",
    (field) => {
      const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
      const parsedSession = parseBallAnnotationSession(
        golden.development_session,
      );
      const result = structuredClone(golden.development_final_result);
      result.package[field] = [];

      expect(() =>
        parseBallAnnotationFinalResult(result, parsedSession),
      ).toThrow(/collection|frames|invalid/i);
    },
  );

  it.each([
    [
      "revision effective annotation",
      (packageValue: any) => {
        packageValue.revision_chain[0].effective_annotation.presence =
          "unknown";
      },
    ],
    [
      "frame revision-chain digest",
      (packageValue: any) => {
        packageValue.frame_evidence[0].revision_chain_sha256 = sha("7");
      },
    ],
    [
      "frame evidence digest",
      (packageValue: any) => {
        packageValue.frame_evidence[0].frame_evidence_sha256 = sha("7");
      },
    ],
    [
      "frame-media collection digest",
      (packageValue: any) => {
        packageValue.frame_media_sha256 = sha("7");
      },
    ],
    [
      "timing-binding unknown key",
      (packageValue: any) => {
        packageValue.frame_evidence[0].timing_binding.unsealed = true;
      },
    ],
  ] as const)("rejects tampered sealed %s", (_label, mutate) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const session = parseBallAnnotationSession(golden.development_session);
    const result = structuredClone(golden.development_final_result);
    mutate(result.package);

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      /revision|frame|timing|evidence|digest|annotation/i,
    );
  });

  it("projects a one-time check dashboard without granting production authority", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.check_session_ready,
    );
    const result = golden.check_final_result;
    const parsed = parseBallAnnotationFinalResult(result, parsedSession);
    expect(parsed).toEqual(
      expect.objectContaining({
        packageSha256: result.package.package_sha256,
        reportSha256: result.feasibility_report.report_sha256,
        dashboard: expect.objectContaining({
          status: "insufficient_evidence",
          totalFrames: 20,
          confirmedLocalizablePositiveFrames: 0,
          reasonCodes: ["lighting_strata_mismatch"],
          requiresNewAttempt: true,
          rawCounts: expect.objectContaining({
            top1Matches: 0,
            top5Matches: 0,
            candidateBudget: 5,
          }),
          authorityGates: {
            developmentPackageBound: true,
            checkProbeBound: true,
            sealedEvidenceBound: true,
          },
          lockedAttempt: expect.objectContaining({
            dataRole: "check",
            revealed: true,
          }),
        }),
      }),
    );

    const developmentSession = parseBallAnnotationSession(
      golden.development_session,
    );

    const crossDecodeResult = structuredClone(golden.development_final_result);
    const crossRow = crossDecodeResult.package.frame_evidence[0];
    const crossTiming = crossRow.timing_binding;
    const verificationBody = {
      method: "decoder_pos_msec_and_frame_digest_agreement_v1",
      tolerance_msec: 0.25,
      observations: ["direct_verified", crossTiming.effective_decode_mode].map(
        (mode) => ({
          effective_decode_mode: mode,
          decoded_frame_position: crossRow.frame_index,
          decoder_reported_pos_msec: crossTiming.decoder_reported_pos_msec,
          source_frame_jpeg_sha256: crossRow.source_frame_jpeg.sha256,
        }),
      ),
    };
    crossTiming.cross_decode_verification = {
      ...verificationBody,
      verification_sha256: pythonCanonicalSha256Sync(verificationBody, [
        "$.tolerance_msec",
        "$.observations[0].decoder_reported_pos_msec",
        "$.observations[1].decoder_reported_pos_msec",
      ]),
    };
    const crossTimingBody = { ...crossTiming };
    delete crossTimingBody.timing_binding_sha256;
    crossTiming.timing_binding_sha256 = pythonCanonicalSha256Sync(
      crossTimingBody,
      testTimingFloatPaths(crossTimingBody, "$"),
    );
    resealFrameEvidence(crossDecodeResult.package, 0);
    resealFinalResult(crossDecodeResult, developmentSession);
    expect(
      parseBallAnnotationFinalResult(crossDecodeResult, developmentSession)
        .dashboard.status,
    ).toBe("not_applicable");

    const revisionResult = structuredClone(golden.development_final_result);
    const firstRevision = revisionResult.package.revision_chain[0];
    const frameIndex = firstRevision.frame_index;
    const effective = structuredClone(firstRevision.effective_annotation);
    const revisionFor = (
      revision: number,
      operation: "set" | "delete" | "undo",
      previous: any,
      next: any,
    ) => {
      const mutationId = `coverage-mutation-${revision}`;
      const request = {
        mutation_id: mutationId,
        expected_revision: revision - 1,
        operation,
        undo_revision: operation === "undo" ? revision - 1 : null,
        annotation: operation === "set" ? next : null,
        suggestion_kind: null,
        suggestion_id: null,
        accepted_suggestion_job_id: null,
        accepted_suggestion_sha256: null,
        dismissed_suggestion_kind: null,
        dismissed_suggestion_id: null,
        dismissed_suggestion_job_id: null,
        dismissed_suggestion_sha256: null,
      };
      return {
        ...firstRevision,
        revision_id: `revision-${pythonCanonicalSha256Sync(
          {
            session_id: developmentSession.view.sessionId,
            frame_index: frameIndex,
            revision,
          },
          [],
        ).slice(0, 24)}`,
        revision,
        operation,
        mutation_id: mutationId,
        mutation_sha256: pythonCanonicalSha256Sync(
          {
            session_id: developmentSession.view.sessionId,
            frame_index: frameIndex,
            request,
          },
          annotationAuthorityFloatPaths("$.request.annotation"),
        ),
        expected_revision: revision - 1,
        supersedes_revision: revision - 1,
        undo_revision: operation === "undo" ? revision - 1 : null,
        accepted_suggestion_kind: null,
        accepted_suggestion_id: null,
        accepted_suggestion_job_id: null,
        accepted_suggestion_sha256: null,
        dismissed_suggestion_kind: null,
        dismissed_suggestion_id: null,
        dismissed_suggestion_job_id: null,
        dismissed_suggestion_sha256: null,
        previous_effective_annotation: previous,
        effective_annotation: next,
        annotation_etag: pythonCanonicalSha256Sync(
          {
            schema_version: "1.0",
            artifact_type: "ball_annotation_effective_revision",
            session_id: developmentSession.view.sessionId,
            frame_index: frameIndex,
            revision,
            effective_annotation: next,
          },
          annotationAuthorityFloatPaths("$.effective_annotation"),
        ),
      };
    };
    const secondRevision = revisionFor(2, "delete", effective, null);
    const undoRevision = revisionFor(3, "undo", null, effective);
    for (const revision of [secondRevision, undoRevision]) {
      revision.mutation_sha256 = pythonCanonicalSha256Sync(
        {
          session_id: developmentSession.view.sessionId,
          frame_index: frameIndex,
          request: {
            mutation_id: revision.mutation_id,
            expected_revision: revision.expected_revision,
            operation: revision.operation,
            undo_revision: revision.undo_revision,
            annotation:
              revision.operation === "set"
                ? revision.effective_annotation
                : null,
            suggestion_kind: revision.accepted_suggestion_kind,
            suggestion_id: revision.accepted_suggestion_id,
            accepted_suggestion_job_id: revision.accepted_suggestion_job_id,
            accepted_suggestion_sha256: revision.accepted_suggestion_sha256,
            dismissed_suggestion_kind: revision.dismissed_suggestion_kind,
            dismissed_suggestion_id: revision.dismissed_suggestion_id,
            dismissed_suggestion_job_id: revision.dismissed_suggestion_job_id,
            dismissed_suggestion_sha256: revision.dismissed_suggestion_sha256,
          },
        },
        annotationAuthorityFloatPaths("$.request.annotation"),
      );
    }
    revisionResult.package.revision_chain.splice(
      1,
      0,
      secondRevision,
      undoRevision,
    );
    const revisionRowIndex = revisionResult.package.frame_evidence.findIndex(
      (row: any) => row.frame_index === frameIndex,
    );
    const revisionRow = revisionResult.package.frame_evidence[revisionRowIndex];
    const frameRevisions = [firstRevision, secondRevision, undoRevision];
    const frameRevisionFloatPaths = frameRevisions.flatMap(
      (_: unknown, index: number) => [
        ...annotationAuthorityFloatPaths(
          `$[${index}].previous_effective_annotation`,
        ),
        ...annotationAuthorityFloatPaths(`$[${index}].effective_annotation`),
      ],
    );
    revisionRow.effective_revision = 3;
    revisionRow.revision_chain_sha256 = pythonCanonicalSha256Sync(
      frameRevisions,
      frameRevisionFloatPaths,
    );
    const rawEffective = revisionResult.package.effective_annotations.find(
      (annotation: any) => annotation.frame_index === frameIndex,
    );
    const { frame_index: _rawFrameIndex, ...rawEffectivePayload } =
      rawEffective;
    expect(
      pythonCanonicalSha256Sync(
        undoRevision.effective_annotation,
        annotationAuthorityFloatPaths(),
      ),
    ).toBe(
      pythonCanonicalSha256Sync(
        rawEffectivePayload,
        annotationAuthorityFloatPaths(),
      ),
    );
    expect(
      pythonCanonicalSha256Sync(rawEffective, annotationAuthorityFloatPaths()),
    ).toBe(revisionRow.effective_annotation_sha256);
    const allRevisionFloatPaths = revisionResult.package.revision_chain.flatMap(
      (_: unknown, index: number) => [
        ...annotationAuthorityFloatPaths(
          `$[${index}].previous_effective_annotation`,
        ),
        ...annotationAuthorityFloatPaths(`$[${index}].effective_annotation`),
      ],
    );
    revisionResult.package.dataset_expansion_eligibility.validation_evidence.revision_chain_sha256 =
      pythonCanonicalSha256Sync(
        revisionResult.package.revision_chain,
        allRevisionFloatPaths,
      );
    resealFrameEvidence(revisionResult.package, revisionRowIndex);
    resealFinalResult(revisionResult, developmentSession);
    expect(
      parseBallAnnotationFinalResult(revisionResult, developmentSession)
        .dashboard.status,
    ).toBe("not_applicable");

    const proxySessionFixture = ballAnnotationSessionFixture({
      profileId: "official-coco-yolo11s-sahi",
    });
    for (const frame of proxySessionFixture.frames) {
      frame.current_annotation = {
        point_source_px: null,
        bbox_source_px: null,
        presence: "absent",
        visibility: "not_applicable",
        training_use: "background",
        annotation_state: "confirmed",
        scale_stratum: "not_applicable",
        lighting_tag: "bright_sun",
        motion_occlusion_tags: [],
        provenance: "manual_human_annotation",
      };
    }
    refreshBallAnnotationProgress(proxySessionFixture);
    const proxySession = parseBallAnnotationSession(proxySessionFixture);
    const proxyResult = developmentFinalResultFixture(proxySessionFixture);
    const proxyPackage = proxyResult.package;
    const proxyRow = proxyPackage.frame_evidence[0];
    const detectorAuthority = proxyPackage.detector_probe_authorities[0];
    const sourcePosition = proxyRow.timing_binding.decoder_reported_pos_msec;
    const declaredOffsetMsec = 100;
    const proxyPosition = sourcePosition + declaredOffsetMsec;
    const proxySha256 = sha("9");
    const mapping = {
      source_frame_index: proxyRow.frame_index,
      source_timing_status: "observed",
      source_decoder_pos_msec: sourcePosition,
      proxy_frame_index: proxyRow.frame_index,
      proxy_timing_basis: "verified_cfr_frame_index_time_v1",
      proxy_cfr_time_msec: proxyPosition,
      source_frame_sha256: proxyRow.source_frame_jpeg.sha256,
      proxy_frame_sha256: proxySha256,
      media_integrity: {
        status: "ok",
        gray: false,
        low_information: false,
        likely_corrupt: false,
      },
    };
    const mappingSha256 = pythonCanonicalSha256Sync(
      [mapping],
      ["$[0].source_decoder_pos_msec", "$[0].proxy_cfr_time_msec"],
    );
    const integrityReportSha256 = pythonCanonicalSha256Sync(
      [{ frame_index: proxyRow.frame_index, ...mapping.media_integrity }],
      [],
    );
    const proxyManifestBody = {
      schema_version: "1.0",
      artifact_type: "ball_review_proxy",
      source: {
        sha256: proxyPackage.source.sha256,
        size_bytes: proxyPackage.source.size_bytes,
        width: proxyPackage.source.width,
        height: proxyPackage.source.height,
        fps: proxyPackage.source.fps,
        frame_count: proxyPackage.source.frame_count,
        codec: "h264",
        file_identity_sha256: proxyPackage.source.file_identity_sha256,
      },
      proxy: {
        sha256: proxySha256,
        size_bytes: 4096,
        width: proxyPackage.source.width,
        height: proxyPackage.source.height,
        fps: proxyPackage.source.fps,
        frame_count: proxyPackage.source.frame_count,
        codec: "h264",
      },
      decoder_fingerprint_sha256: sha("8"),
      requested_decode_mode: "sequential",
      effective_decode_mode: "sequential",
      map_time_tolerance_msec: 0.25,
      declared_offset_msec: declaredOffsetMsec,
      coordinate_transform: {
        kind: "uniform_source_to_proxy_scale_v1",
        scale_x: 1,
        scale_y: 1,
        source_origin: [0, 0],
        proxy_origin: [0, 0],
      },
      expected_frame_indices: [proxyRow.frame_index],
      mappings: [mapping],
      mapping_sha256: mappingSha256,
      integrity_report_sha256: integrityReportSha256,
    };
    const proxyManifest = {
      ...proxyManifestBody,
      manifest_sha256: pythonCanonicalSha256Sync(
        proxyManifestBody,
        testReviewProxyManifestFloatPaths(proxyManifestBody),
      ),
    };
    const proxyBindingBody = {
      schema_version: "1.0",
      artifact_type: "ball_review_proxy_frame_binding",
      proxy: {
        sha256: proxySha256,
        size_bytes: 4096,
        width: proxyPackage.source.width,
        height: proxyPackage.source.height,
      },
      map_sha256: mappingSha256,
      source_frame: {
        frame_index: proxyRow.frame_index,
        timing_status: "observed",
        decoder_reported_pos_msec: sourcePosition,
        sha256: proxyRow.source_frame_jpeg.sha256,
      },
      proxy_frame: {
        frame_index: proxyRow.frame_index,
        timing_basis: "verified_cfr_frame_index_time_v1",
        cfr_time_msec: proxyPosition,
        sha256: proxySha256,
      },
      map_time_tolerance_msec: 0.25,
      declared_offset_msec: declaredOffsetMsec,
      time_mapping: {
        method: "explicit_per_frame_decoder_pos_msec_map_v1",
        source_timing_status: "observed",
        proxy_timing_basis: "verified_cfr_frame_index_time_v1",
        declared_offset_msec: declaredOffsetMsec,
        observed_offset_msec: declaredOffsetMsec,
        residual_msec: 0,
        tolerance_msec: 0.25,
      },
    };
    proxyRow.proxy_binding = {
      ...proxyBindingBody,
      binding_sha256: computeReviewProxyBindingSha256(proxyBindingBody),
    };
    detectorAuthority.probe_report.review_proxy_manifest = proxyManifest;
    resealDetectorAuthority(detectorAuthority);
    proxyPackage.frame_review_proxy_authority = {
      probe_job_id: detectorAuthority.job_id,
      probe_report_sha256: detectorAuthority.probe_report_sha256,
      probe_result_manifest_sha256:
        detectorAuthority.probe_result_manifest_sha256,
      probe_report: structuredClone(detectorAuthority.probe_report),
      probe_result_manifest: structuredClone(
        detectorAuthority.probe_result_manifest,
      ),
      review_proxy_manifest: proxyManifest,
      historical_probe_authority: null,
    };
    resealFrameEvidence(proxyPackage, 0);
    resealFinalResult(proxyResult, proxySession);
    expect(
      parseBallAnnotationFinalResult(proxyResult, proxySession).dashboard
        .status,
    ).toBe("not_applicable");

    const noSourceTimingResult = structuredClone(proxyResult);
    const noTimingPackage = noSourceTimingResult.package;
    const noTimingRow = noTimingPackage.frame_evidence[0];
    const noTiming = noTimingRow.timing_binding;
    noTiming.timing_profile_id =
      "source_pos_msec_not_collected_proxy_cfr_verified_v1";
    noTiming.timing_status = "not_collected";
    noTiming.decoder_reported_pos_msec = null;
    noTiming.decoder_time_seconds = null;
    noTiming.decoder_timing_observation_method = null;
    noTiming.position_verification =
      "verified_review_proxy_frame_index_mapping_v1";
    noTiming.cross_decode_verification = null;
    const noTimingBody = { ...noTiming };
    delete noTimingBody.timing_binding_sha256;
    noTiming.timing_binding_sha256 = pythonCanonicalSha256Sync(
      noTimingBody,
      testTimingFloatPaths(noTimingBody, "$"),
    );
    const noTimingMapping =
      noTimingPackage.frame_review_proxy_authority.review_proxy_manifest
        .mappings[0];
    noTimingMapping.source_timing_status = "not_collected";
    noTimingMapping.source_decoder_pos_msec = null;
    const noTimingManifest =
      noTimingPackage.frame_review_proxy_authority.review_proxy_manifest;
    noTimingManifest.mapping_sha256 = pythonCanonicalSha256Sync(
      noTimingManifest.mappings,
      ["$[0].source_decoder_pos_msec", "$[0].proxy_cfr_time_msec"],
    );
    const noTimingManifestBody = { ...noTimingManifest };
    delete noTimingManifestBody.manifest_sha256;
    noTimingManifest.manifest_sha256 = pythonCanonicalSha256Sync(
      noTimingManifestBody,
      testReviewProxyManifestFloatPaths(noTimingManifestBody),
    );
    const noTimingBinding = noTimingRow.proxy_binding;
    noTimingBinding.map_sha256 = noTimingManifest.mapping_sha256;
    noTimingBinding.source_frame.timing_status = "not_collected";
    noTimingBinding.source_frame.decoder_reported_pos_msec = null;
    noTimingBinding.time_mapping.method =
      "exact_frame_index_to_verified_proxy_cfr_v1";
    noTimingBinding.time_mapping.source_timing_status = "not_collected";
    noTimingBinding.time_mapping.observed_offset_msec = null;
    noTimingBinding.time_mapping.residual_msec = null;
    const noTimingBindingBody = { ...noTimingBinding };
    delete noTimingBindingBody.binding_sha256;
    noTimingBinding.binding_sha256 =
      computeReviewProxyBindingSha256(noTimingBindingBody);
    const noTimingDetector = noTimingPackage.detector_probe_authorities[0];
    noTimingDetector.probe_report.review_proxy_manifest = noTimingManifest;
    resealDetectorAuthority(noTimingDetector);
    noTimingPackage.frame_review_proxy_authority.probe_report = structuredClone(
      noTimingDetector.probe_report,
    );
    noTimingPackage.frame_review_proxy_authority.review_proxy_manifest =
      noTimingManifest;
    resealFrameEvidence(noTimingPackage, 0);
    resealFinalResult(noSourceTimingResult, proxySession);
    expect(
      parseBallAnnotationFinalResult(noSourceTimingResult, proxySession)
        .dashboard.status,
    ).toBe("not_applicable");

    const sessionAuthorityAttacks = [
      (session: any) => {
        session.sampling_manifest.selection_authority.scale_applicability[0].stratum =
          "far";
      },
      (session: any) => {
        session.sampling_manifest.selection_authority.lighting_applicability[0].stratum =
          "shadow";
      },
      (session: any) => {
        session.sampling_manifest.selection_authority.source_sha256 = sha("9");
      },
      (session: any) => {
        session.sampling_manifest.candidate_universe_authority.lighting_strata =
          null;
      },
      (session: any) => {
        session.sampling_manifest.candidate_universe_authority.excluded_temporal_groups =
          null;
      },
      (session: any) => {
        session.sampling_manifest.candidate_universe_authority.source_sha256 =
          sha("9");
      },
    ];
    for (const attack of sessionAuthorityAttacks) {
      const attacked = structuredClone(golden.check_session_ready);
      attack(attacked);
      expect(() => parseBallAnnotationSession(attacked)).toThrow(
        /sampling|selection|candidate/i,
      );
    }

    const partialSuggestion = structuredClone(revisionResult);
    partialSuggestion.package.revision_chain[1].accepted_suggestion_id =
      "candidate-partial";
    expect(() =>
      parseBallAnnotationFinalResult(partialSuggestion, developmentSession),
    ).toThrow(/suggestion/i);
    const invalidDelete = structuredClone(revisionResult);
    invalidDelete.package.revision_chain[1].effective_annotation = effective;
    expect(() =>
      parseBallAnnotationFinalResult(invalidDelete, developmentSession),
    ).toThrow(/revision|delete/i);
    const invalidUndo = structuredClone(revisionResult);
    invalidUndo.package.revision_chain[2].undo_revision = 1;
    expect(() =>
      parseBallAnnotationFinalResult(invalidUndo, developmentSession),
    ).toThrow(/undo/i);

    const invalidCrossMethod = structuredClone(crossDecodeResult);
    invalidCrossMethod.package.frame_evidence[0].timing_binding.cross_decode_verification.method =
      "unknown";
    expect(() =>
      parseBallAnnotationFinalResult(invalidCrossMethod, developmentSession),
    ).toThrow(/cross-decode/i);
    const invalidCrossDigest = structuredClone(crossDecodeResult);
    invalidCrossDigest.package.frame_evidence[0].timing_binding.cross_decode_verification.verification_sha256 =
      sha("9");
    expect(() =>
      parseBallAnnotationFinalResult(invalidCrossDigest, developmentSession),
    ).toThrow(/cross-decode.*digest/i);
    const invalidTimingDigest = structuredClone(crossDecodeResult);
    invalidTimingDigest.package.frame_evidence[0].timing_binding.timing_binding_sha256 =
      sha("9");
    expect(() =>
      parseBallAnnotationFinalResult(invalidTimingDigest, developmentSession),
    ).toThrow(/timing/i);

    const crossDecodeAttacks: Array<[string, (timing: any) => void, RegExp]> = [
      [
        "invalid effective mode",
        (timing) => {
          timing.effective_decode_mode = "teleport";
        },
        /effective decode mode/i,
      ],
      [
        "effective mode differs from package lineage",
        (timing) => {
          timing.effective_decode_mode = "direct_verified";
        },
        /timing authority/i,
      ],
      [
        "duplicate modes",
        (timing) => {
          timing.cross_decode_verification.observations[1].effective_decode_mode =
            timing.cross_decode_verification.observations[0].effective_decode_mode;
        },
        /cross-decode.*modes/i,
      ],
      [
        "noncanonical mode order",
        (timing) => {
          timing.cross_decode_verification.observations.reverse();
        },
        /cross-decode.*modes/i,
      ],
      [
        "too few observations",
        (timing) => {
          timing.cross_decode_verification.observations.pop();
        },
        /cross-decode.*(?:observations|authority)/i,
      ],
      [
        "effective mode omitted",
        (timing) => {
          timing.cross_decode_verification.observations[1].effective_decode_mode =
            "sequential";
        },
        /cross-decode.*(?:effective mode|modes)/i,
      ],
      [
        "frame mismatch",
        (timing) => {
          timing.cross_decode_verification.observations[0].decoded_frame_position += 1;
        },
        /cross-decode.*observation/i,
      ],
      [
        "JPEG mismatch",
        (timing) => {
          timing.cross_decode_verification.observations[0].source_frame_jpeg_sha256 =
            sha("9");
        },
        /cross-decode.*observation/i,
      ],
      [
        "position outside tolerance",
        (timing) => {
          timing.cross_decode_verification.observations[0].decoder_reported_pos_msec += 1;
        },
        /cross-decode.*observation/i,
      ],
    ];
    for (const [name, mutate, expected] of crossDecodeAttacks) {
      const attacked = structuredClone(crossDecodeResult);
      const timing = attacked.package.frame_evidence[0].timing_binding;
      mutate(timing);
      const verification = timing.cross_decode_verification;
      const verificationBody = { ...verification };
      delete verificationBody.verification_sha256;
      verification.verification_sha256 = pythonCanonicalSha256Sync(
        verificationBody,
        [
          "$.tolerance_msec",
          ...verification.observations.map(
            (_: unknown, index: number) =>
              `$.observations[${index}].decoder_reported_pos_msec`,
          ),
        ],
      );
      const timingBody = { ...timing };
      delete timingBody.timing_binding_sha256;
      timing.timing_binding_sha256 = pythonCanonicalSha256Sync(
        timingBody,
        testTimingFloatPaths(timingBody, "$"),
      );
      resealFrameEvidence(attacked.package, 0);
      resealFinalResult(attacked, developmentSession);
      expect(
        () => parseBallAnnotationFinalResult(attacked, developmentSession),
        name,
      ).toThrow(expected);
    }

    const expectProxyManifestAttack = (
      mutate: (manifest: any) => void,
      expected: RegExp,
    ) => {
      const attacked = structuredClone(proxyResult);
      const attackedPackage = attacked.package;
      const manifest =
        attackedPackage.frame_review_proxy_authority.review_proxy_manifest;
      mutate(manifest);
      const detector = attackedPackage.detector_probe_authorities[0];
      detector.probe_report.review_proxy_manifest = structuredClone(manifest);
      resealDetectorAuthority(detector);
      attackedPackage.frame_review_proxy_authority.probe_report =
        structuredClone(detector.probe_report);
      attackedPackage.frame_review_proxy_authority.review_proxy_manifest =
        manifest;
      expect(() =>
        parseBallAnnotationFinalResult(attacked, proxySession),
      ).toThrow(expected);
    };
    expectProxyManifestAttack((manifest) => {
      manifest.artifact_type = "unknown";
    }, /report|manifest/i);
    expectProxyManifestAttack((manifest) => {
      manifest.source.sha256 = sha("7");
    }, /source/i);
    expectProxyManifestAttack((manifest) => {
      manifest.mappings[0].media_integrity.gray = true;
    }, /mapping/i);
    expectProxyManifestAttack((manifest) => {
      manifest.manifest_sha256 = sha("7");
    }, /digest/i);
    const historicalShapeAttack = structuredClone(proxyResult);
    historicalShapeAttack.package.frame_review_proxy_authority.historical_probe_authority =
      {};
    expect(() =>
      parseBallAnnotationFinalResult(historicalShapeAttack, proxySession),
    ).toThrow(/historical/i);
  });

  it("rejects a finalized check result bound to a different active probe", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.check_session_ready,
    );
    const mismatchedSession = {
      ...parsedSession,
      view: {
        ...parsedSession.view,
        checkProbeAuthority: {
          ...parsedSession.view.checkProbeAuthority!,
          reportSha256: sha("9"),
        },
      },
    };

    expect(() =>
      parseBallAnnotationFinalResult(
        golden.check_final_result,
        mismatchedSession,
      ),
    ).toThrow(/active session/);
  });

  it("rejects check sealed eligibility that differs from its package", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const parsedSession = parseBallAnnotationSession(
      golden.check_session_ready,
    );
    const result = structuredClone(golden.check_final_result);
    result.feasibility_report.sealed_evidence.dataset_expansion_eligibility.validation_evidence.exact_frame_media_sha256 =
      sha("9");

    expect(() => parseBallAnnotationFinalResult(result, parsedSession)).toThrow(
      /sealed evidence|eligibility/,
    );
  });

  it("rejects non-authoritative detector candidate rows and bindings", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const attacks: Array<
      [string, (result: Record<string, any>) => void, RegExp]
    > = [
      [
        "unknown candidate field",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate.unsealed = true;
        },
        /candidate.*schema/i,
      ],
      [
        "invented candidate row",
        (result) => {
          const invented = structuredClone(
            result.package.detector_candidate_evidence[0],
          );
          invented.candidate.candidate_id = "invented-candidate";
          result.package.detector_candidate_evidence.push(invented);
        },
        /candidate|invented|canonical/i,
      ],
      [
        "candidate digest",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_sha256 =
            sha("9");
        },
        /candidate authority|digest/i,
      ],
      [
        "collection digest",
        (result) => {
          result.package.detector_candidate_evidence_sha256 = sha("9");
        },
        /candidate collection digest/i,
      ],
      [
        "probe authority",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_origin.probe_report_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "out-of-range frame",
        (result) => {
          result.package.detector_candidate_evidence[0].frame_index =
            result.package.source.frame_count;
        },
        /outside the source/i,
      ],
      [
        "unknown origin job",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_origin.probe_job_id =
            "unknown-origin";
        },
        /probe|authority/i,
      ],
      [
        "unknown media job",
        (result) => {
          result.package.detector_candidate_evidence[0].review_media.probe_job_id =
            "unknown-media";
        },
        /probe|authority/i,
      ],
      [
        "origin result manifest",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_origin.probe_result_manifest_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "media report",
        (result) => {
          result.package.detector_candidate_evidence[0].review_media.probe_report_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "media result manifest",
        (result) => {
          result.package.detector_candidate_evidence[0].review_media.probe_result_manifest_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "inherited candidate evidence",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_origin.candidate_evidence_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "missing frame evidence",
        (result) => {
          result.package.detector_candidate_evidence[0].frame_index = 1;
        },
        /probe|authority/i,
      ],
      [
        "origin artifact",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate_origin.source_artifact_id =
            "other-source";
        },
        /probe|authority/i,
      ],
      [
        "proxy binding",
        (result) => {
          result.package.detector_candidate_evidence[0].review_media.proxy_binding_sha256 =
            sha("9");
        },
        /probe|authority/i,
      ],
      [
        "missing raw candidate rank",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate.rank = 5;
        },
        /candidate authority/i,
      ],
      [
        "rank above top-k",
        (result) => {
          result.package.detector_candidate_evidence[0].candidate.rank = 6;
        },
        /rank is invalid/i,
      ],
      [
        "pending decision",
        (result) => {
          result.package.detector_candidate_evidence[0].decision = null;
        },
        /pending|eligibility|collection digest/i,
      ],
      [
        "noncanonical row order",
        (result) => {
          result.package.detector_candidate_evidence.reverse();
        },
        /canonically ordered/i,
      ],
      [
        "adjacent duplicate row",
        (result) => {
          result.package.detector_candidate_evidence.splice(
            1,
            0,
            structuredClone(result.package.detector_candidate_evidence[0]),
          );
        },
        /canonically ordered|duplicated/i,
      ],
      [
        "accepted decision kind",
        (result) => {
          result.package.detector_candidate_evidence[0].decision.decision =
            "accepted_human_annotation";
        },
        /revision authority/i,
      ],
      [
        "decision operator",
        (result) => {
          result.package.detector_candidate_evidence[0].decision.operator_id =
            "other-operator";
        },
        /revision authority/i,
      ],
      [
        "decision time",
        (result) => {
          result.package.detector_candidate_evidence[0].decision.decided_at =
            "2026-07-18T01:00:00+00:00";
        },
        /revision authority/i,
      ],
      [
        "missing candidate row",
        (result) => {
          result.package.detector_candidate_evidence.pop();
        },
        /candidate collection.*incomplete|invented/i,
      ],
      [
        "revision binding",
        (result) => {
          const evidence = result.package.detector_candidate_evidence[0];
          evidence.decision.revision_id = result.package.revision_chain.find(
            (revision: any) => revision.frame_index !== evidence.frame_index,
          ).revision_id;
        },
        /revision authority/i,
      ],
    ];

    for (const [name, mutate, expected] of attacks) {
      const result = structuredClone(golden.development_final_result);
      const session = parseBallAnnotationSession(golden.development_session);
      mutate(result);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(expected);
    }
  });

  it("rejects non-authoritative propagation rows and bindings", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const attacks: Array<
      [string, (result: Record<string, any>) => void, RegExp]
    > = [
      [
        "unknown report field",
        (result) => {
          result.package.propagation_reports[0].unsealed = true;
        },
        /propagation report.*schema/i,
      ],
      [
        "unknown result field",
        (result) => {
          result.package.propagation_reports[0].frame_results[0].unsealed = true;
        },
        /frame result.*schema/i,
      ],
      [
        "report digest",
        (result) => {
          result.package.propagation_reports[0].report_sha256 = sha("9");
        },
        /propagation report.*digest/i,
      ],
      [
        "session binding",
        (result) => {
          result.package.propagation_reports[0].session_id = "other-session";
        },
        /propagation report.*identity/i,
      ],
      [
        "probe binding",
        (result) => {
          result.package.propagation_reports[0].neighbor_probe_report_sha256 =
            sha("9");
        },
        /propagation|probe|authority/i,
      ],
      [
        "revision binding",
        (result) => {
          const report = result.package.propagation_reports[0];
          const frameResult = report.frame_results[0];
          const revisionId = result.package.revision_chain.find(
            (revision: any) => revision.frame_index !== frameResult.frame_index,
          ).revision_id;
          frameResult.human_decision.revision_id = revisionId;
          report.suggestions.find(
            (suggestion: any) =>
              suggestion.frame_index === frameResult.frame_index,
          ).human_decision.revision_id = revisionId;
        },
        /revision authority/i,
      ],
      [
        "invented report row",
        (result) => {
          result.package.propagation_reports.push(
            structuredClone(result.package.propagation_reports[0]),
          );
        },
        /propagation reports.*unique|producer/i,
      ],
      [
        "collection digest",
        (result) => {
          result.package.propagation_reports_sha256 = sha("9");
        },
        /propagation report collection digest/i,
      ],
    ];

    for (const [name, mutate, expected] of attacks) {
      const result = structuredClone(golden.development_final_result);
      const session = parseBallAnnotationSession(golden.development_session);
      mutate(result);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(expected);
    }
  });

  it("rejects each sealed propagation semantic mismatch", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const confirmationFrom = (decision: any, iou: number | null = null) => ({
      revision_id: decision.revision_id,
      revision: decision.revision,
      operator_id: decision.operator_id,
      center_error_px: 0,
      iou,
      corrected: false,
      confirmed_at: decision.decided_at,
    });
    const mutations: Array<[string, (report: Record<string, any>) => void]> = [
      ["radius", (report) => (report.radius_frames = 3)],
      ["schema version", (report) => (report.schema_version = "2.0")],
      ["artifact type", (report) => (report.artifact_type = "other")],
      [
        "tracker profile id",
        (report) => (report.tracker_profile.profile_id = "other"),
      ],
      ["tracker version", (report) => (report.tracker_profile.version = "2.0")],
      [
        "tracker radius",
        (report) => (report.tracker_profile.radius_frames_max = 3),
      ],
      [
        "tracker search radius",
        (report) => (report.tracker_profile.search_radius_source_px = 25),
      ],
      [
        "tracker profile digest",
        (report) => (report.tracker_profile.profile_sha256 = sha("9")),
      ],
      [
        "tracker body digest",
        (report) => (report.tracker_profile.minimum_match_score = 0.6),
      ],
      [
        "tracker score range",
        (report) => (report.tracker_profile.minimum_match_score = 1.1),
      ],
      ["seed frame", (report) => (report.seed_binding.frame_index += 1)],
      [
        "seed revision",
        (report) => (report.seed_binding.annotation_revision += 1),
      ],
      [
        "seed sampling manifest",
        (report) => (report.seed_binding.sampling_manifest_sha256 = sha("9")),
      ],
      [
        "seed tracker",
        (report) => (report.seed_binding.tracker_profile_sha256 = sha("9")),
      ],
      ["seed digest", (report) => (report.seed_binding_sha256 = sha("9"))],
      [
        "seed annotation ETag",
        (report) => (report.seed_binding.annotation_etag = sha("9")),
      ],
      [
        "seed annotation digest",
        (report) => (report.seed_binding.annotation_sha256 = sha("9")),
      ],
      [
        "seed source frame",
        (report) => (report.seed_binding.source_frame_sha256 = sha("9")),
      ],
      [
        "seed temporal group",
        (report) => (report.seed_binding.temporal_group_id = sha("9")),
      ],
      [
        "duplicate target",
        (report) =>
          (report.target_frame_indices[1] = report.target_frame_indices[0]),
      ],
      ["reversed targets", (report) => report.target_frame_indices.reverse()],
      ["missing target", (report) => (report.target_frame_indices = [])],
      ["too many targets", (report) => report.target_frame_indices.push(43)],
      [
        "missing seed evidence",
        (report) => {
          report.seed_frame_index = 1;
          report.seed_binding.frame_index = 1;
          report.seed_binding_sha256 = pythonCanonicalSha256Sync(
            report.seed_binding,
            [],
          );
          report.intent_sha256 = pythonCanonicalSha256Sync(
            {
              session_id: report.session_id,
              mutation_id: report.mutation_id,
              seed_frame_index: report.seed_frame_index,
              radius_frames: report.radius_frames,
              expected_seed_revision: report.expected_seed_revision,
              seed_binding: report.seed_binding,
              target_frame_indices: report.target_frame_indices,
            },
            [],
          );
        },
      ],
      [
        "supplemental seed evidence",
        (report) => {
          report.seed_frame_index = 38;
          report.seed_binding.frame_index = 38;
          report.seed_binding_sha256 = pythonCanonicalSha256Sync(
            report.seed_binding,
            [],
          );
          report.intent_sha256 = pythonCanonicalSha256Sync(
            {
              session_id: report.session_id,
              mutation_id: report.mutation_id,
              seed_frame_index: report.seed_frame_index,
              radius_frames: report.radius_frames,
              expected_seed_revision: report.expected_seed_revision,
              seed_binding: report.seed_binding,
              target_frame_indices: report.target_frame_indices,
            },
            [],
          );
        },
      ],
      [
        "missing seed revision",
        (report) => {
          report.expected_seed_revision = 2;
          report.seed_binding.annotation_revision = 2;
          report.seed_binding_sha256 = pythonCanonicalSha256Sync(
            report.seed_binding,
            [],
          );
          report.intent_sha256 = pythonCanonicalSha256Sync(
            {
              session_id: report.session_id,
              mutation_id: report.mutation_id,
              seed_frame_index: report.seed_frame_index,
              radius_frames: report.radius_frames,
              expected_seed_revision: report.expected_seed_revision,
              seed_binding: report.seed_binding,
              target_frame_indices: report.target_frame_indices,
            },
            [],
          );
        },
      ],
      ["intent digest", (report) => (report.intent_sha256 = sha("9"))],
      ["result target", (report) => (report.frame_results[0].frame_index += 1)],
      [
        "result direction",
        (report) => (report.frame_results[0].direction = "sideways"),
      ],
      [
        "result status",
        (report) => (report.frame_results[0].status = "unknown"),
      ],
      [
        "result conflicting audits",
        (report) => {
          report.frame_results[0].human_confirmation = confirmationFrom(
            report.frame_results[0].human_decision,
          );
        },
      ],
      [
        "confirmation IoU range",
        (report) => {
          report.frame_results[0].human_confirmation = confirmationFrom(
            report.frame_results[0].human_decision,
            1.1,
          );
        },
      ],
      [
        "confirmation correction state",
        (report) => {
          report.frame_results[0].human_confirmation = {
            ...confirmationFrom(report.frame_results[0].human_decision),
            corrected: "false",
          };
        },
      ],
      [
        "invalid human decision",
        (report) => {
          report.frame_results[0].human_decision.decision =
            "accepted_human_annotation";
        },
      ],
      [
        "success failure code",
        (report) => (report.frame_results[0].failure_code = "lost"),
      ],
      [
        "success pending state",
        (report) => (report.frame_results[0].pending_human_confirmation = true),
      ],
      [
        "failed missing code",
        (report) => (report.frame_results[0].status = "failed"),
      ],
      [
        "failed suggestion",
        (report) => {
          report.frame_results[0].status = "failed";
          report.frame_results[0].failure_code = "lost";
        },
      ],
      [
        "failed pending",
        (report) => {
          const result = report.frame_results[0];
          result.status = "failed";
          result.failure_code = "lost";
          result.suggestion_id = null;
          result.pending_human_confirmation = true;
        },
      ],
      [
        "failed decision",
        (report) => {
          const result = report.frame_results[0];
          result.status = "failed";
          result.failure_code = "lost";
          result.suggestion_id = null;
        },
      ],
      [
        "suggestion point",
        (report) => (report.suggestions[0].point_source_px = null),
      ],
      [
        "suggestion box",
        (report) => (report.suggestions[0].bbox_source_px = null),
      ],
      [
        "suggestion presence",
        (report) => (report.suggestions[0].presence = "absent"),
      ],
      [
        "suggestion visibility",
        (report) => (report.suggestions[0].visibility = "hidden"),
      ],
      [
        "suggestion training use",
        (report) => (report.suggestions[0].training_use = "positive"),
      ],
      [
        "suggestion state",
        (report) => (report.suggestions[0].annotation_state = "confirmed"),
      ],
      [
        "suggestion provenance",
        (report) => (report.suggestions[0].provenance = "other"),
      ],
      [
        "suggestion job",
        (report) => (report.suggestions[0].suggestion_job_id = "other-job"),
      ],
      [
        "suggestion pending state",
        (report) => (report.suggestions[0].pending_human_confirmation = true),
      ],
      [
        "suggestion conflicting audits",
        (report) => {
          report.suggestions[0].human_confirmation = confirmationFrom(
            report.suggestions[0].human_decision,
          );
        },
      ],
      [
        "suggestion group",
        (report) => (report.suggestions[0].temporal_group.group_id = sha("9")),
      ],
      [
        "suggestion seed",
        (report) =>
          (report.suggestions[0].temporal_group.seed_frame_index += 1),
      ],
      [
        "suggestion source",
        (report) =>
          (report.suggestions[0].temporal_group.source_sha256 = sha("9")),
      ],
      [
        "suggestion group profile",
        (report) => (report.suggestions[0].temporal_group.profile_id = "other"),
      ],
      [
        "suggestion ancestry",
        (report) =>
          (report.suggestions[0].temporal_group.ancestry_profile = "other"),
      ],
      [
        "suggestion derivative type",
        (report) =>
          (report.suggestions[0].temporal_group.derivative.artifact_type =
            "other"),
      ],
      [
        "suggestion inheritance",
        (report) =>
          (report.suggestions[0].temporal_group.derivative.inheritance_rule =
            "other"),
      ],
      [
        "suggestion digest",
        (report) => (report.suggestions[0].suggestion_sha256 = sha("9")),
      ],
      [
        "result suggestion",
        (report) =>
          (report.frame_results[0].suggestion_id = "other-suggestion"),
      ],
      [
        "result source",
        (report) => (report.frame_results[0].source_frame_sha256 = sha("9")),
      ],
      [
        "result pending mismatch",
        (report) => {
          report.frame_results[0].human_decision = null;
          report.frame_results[0].pending_human_confirmation = true;
        },
      ],
      [
        "result audit mismatch",
        (report) => {
          report.frame_results[0].human_decision.decided_at =
            "2026-07-18T01:00:00+00:00";
        },
      ],
      [
        "duplicate suggestion identity",
        (report) => {
          report.suggestions[1] = structuredClone(report.suggestions[0]);
        },
      ],
      [
        "missing successful suggestion",
        (report) => {
          report.suggestions.pop();
        },
      ],
      [
        "confirmation audit",
        (report) => {
          const confirmation = confirmationFrom(
            report.frame_results[0].human_decision,
          );
          report.frame_results[0].human_decision = null;
          report.frame_results[0].human_confirmation = confirmation;
          report.suggestions[0].human_decision = null;
          report.suggestions[0].human_confirmation =
            structuredClone(confirmation);
        },
      ],
      [
        "confirmation IoU",
        (report) => {
          const confirmation = confirmationFrom(
            report.frame_results[0].human_decision,
            0.5,
          );
          report.frame_results[0].human_decision = null;
          report.frame_results[0].human_confirmation = confirmation;
          report.suggestions[0].human_decision = null;
          report.suggestions[0].human_confirmation =
            structuredClone(confirmation);
        },
      ],
      ["count confirmed", (report) => (report.decision_counts.confirmed += 1)],
      ["count dismissed", (report) => (report.decision_counts.dismissed -= 1)],
      ["count pending", (report) => (report.decision_counts.pending += 1)],
      [
        "summary success",
        (report) => (report.summary.succeeded_frame_count -= 1),
      ],
      [
        "summary confirmed",
        (report) => (report.summary.human_validated_frame_count += 1),
      ],
      [
        "summary dismissed",
        (report) => (report.summary.human_dismissed_frame_count -= 1),
      ],
      [
        "summary pending count",
        (report) => (report.summary.pending_human_confirmation_count += 1),
      ],
      [
        "summary pending state",
        (report) => (report.summary.pending_human_confirmation = true),
      ],
    ];

    for (const [name, mutate] of mutations) {
      const result = structuredClone(golden.development_final_result);
      const session = parseBallAnnotationSession(golden.development_session);
      mutate(result.package.propagation_reports[0]);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(
        /propagation|tracker|seed|target|intent|result|suggestion|accounting/i,
      );
    }
  }, 30_000);

  it("rejects propagation reports in a one-time check package", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const result = structuredClone(golden.check_final_result);
    const session = parseBallAnnotationSession(golden.check_session_ready);
    result.package.propagation_reports = structuredClone(
      golden.development_final_result.package.propagation_reports,
    );
    result.package.propagation_reports_sha256 =
      golden.development_final_result.package.propagation_reports_sha256;

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      /check packages cannot contain propagation/i,
    );
  });

  it("rejects supplemental propagation evidence rebound away from its report", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const fields = [
      "propagation_job_id",
      "propagation_report_sha256",
      "propagation_intent_sha256",
      "seed_binding_sha256",
      "tracker_profile_sha256",
      "neighbor_probe_job_id",
      "neighbor_probe_report_sha256",
      "neighbor_probe_result_manifest_sha256",
      "suggestion_id",
      "suggestion_sha256",
      "temporal_group_derivative_binding_sha256",
      "propagation_frame_result_sha256",
    ];
    for (const field of fields) {
      const result = structuredClone(golden.development_final_result);
      const session = parseBallAnnotationSession(golden.development_session);
      const rowIndex = result.package.frame_evidence.findIndex(
        (row: any) => row.frame_role === "supplemental",
      );
      const propagation =
        result.package.frame_evidence[rowIndex].propagation_evidence;
      propagation[field] = sha("9");
      const bindingBody = { ...propagation };
      delete bindingBody.binding_sha256;
      propagation.binding_sha256 = pythonCanonicalSha256Sync(bindingBody, []);
      resealFrameEvidence(result.package, rowIndex);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        field,
      ).toThrow(/propagation|supplemental|probe authority/i);
    }
  });

  it("recomputes package and role-specific report root digests", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;

    const stalePackage = structuredClone(golden.development_final_result);
    const packageSession = parseBallAnnotationSession(
      golden.development_session,
    );
    stalePackage.package.created_at = "2026-07-18T00:01:00+00:00";
    stalePackage.package.package_sha256 = sha("8");
    stalePackage.feasibility_report.sealed_evidence.annotation_package_sha256 =
      sha("8");
    const reportBody = { ...stalePackage.feasibility_report };
    delete reportBody.report_sha256;
    stalePackage.feasibility_report.report_sha256 = pythonCanonicalSha256Sync(
      reportBody,
      [],
    );
    expect(() =>
      parseBallAnnotationFinalResult(stalePackage, packageSession),
    ).toThrow(/package.*digest/i);

    const staleReport = structuredClone(golden.development_final_result);
    const reportSession = parseBallAnnotationSession(
      golden.development_session,
    );
    staleReport.feasibility_report.report_sha256 = sha("9");
    expect(() =>
      parseBallAnnotationFinalResult(staleReport, reportSession),
    ).toThrow(/feasibility report.*digest/i);

    const staleCheckReport = structuredClone(golden.check_final_result);
    const checkSession = parseBallAnnotationSession(golden.check_session_ready);
    staleCheckReport.feasibility_report.report_sha256 = sha("7");
    expect(() =>
      parseBallAnnotationFinalResult(staleCheckReport, checkSession),
    ).toThrow(/feasibility report.*digest/i);
  });

  it.each([
    [
      "empty frame evidence",
      (result: Record<string, any>) => {
        result.feasibility_report.frames = [];
      },
      /feasibility frames.*bound/i,
    ],
    [
      "out-of-authority frame",
      (result: Record<string, any>) => {
        result.feasibility_report.frames[0].frame_index = 999_999;
      },
      /package truth|frame set|sampling authority/i,
    ],
    [
      "unbound metric profile",
      (result: Record<string, any>) => {
        result.feasibility_report.metric_profile_sha256 = sha("9");
      },
      /metric profile digest/i,
    ],
    [
      "over-authorized status",
      (result: Record<string, any>) => {
        result.feasibility_report.status = "feasibility_passed";
        result.feasibility_report.authorizations.may_expand_to_100_300_boxes = true;
      },
      /status differs from recomputed evidence/i,
    ],
    [
      "erased missing support",
      (result: Record<string, any>) => {
        result.feasibility_report.support.missing = [];
      },
      /status differs from recomputed evidence/i,
    ],
  ])("rejects check report authority attack: %s", (_name, mutate, error) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const result = structuredClone(golden.check_final_result);
    const session = parseBallAnnotationSession(golden.check_session_ready);
    mutate(result);

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      error,
    );
  });

  it.each([
    [
      "metric profile",
      (report: Record<string, any>) => {
        report.metric_profile.unsealed = true;
      },
    ],
    [
      "computed bounds",
      (report: Record<string, any>) => {
        report.computed_source_px_bounds.unsealed = 1;
      },
    ],
    [
      "frame evidence",
      (report: Record<string, any>) => {
        report.frames[0].unsealed = true;
      },
    ],
  ])("forbids extra nested check report fields in %s", (_name, mutate) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const result = structuredClone(golden.check_final_result);
    const session = parseBallAnnotationSession(golden.check_session_ready);
    mutate(result.feasibility_report);

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      /schema is invalid/i,
    );
  });

  it("enforces canonical dataset expansion eligibility semantics", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const attacks: Array<
      [string, (result: Record<string, any>) => void, RegExp]
    > = [
      [
        "confirmation flag",
        (result) => {
          result.package.dataset_expansion_eligibility.validation_evidence.all_frames_human_confirmed = false;
        },
        /(?:eligibility|validation).*flags/i,
      ],
      [
        "eligibility without reasons",
        (result) => {
          result.package.dataset_expansion_eligibility.eligible = false;
          result.package.may_seed_dataset_expansion = false;
        },
        /eligibility.*reasons/i,
      ],
      [
        "noncanonical reasons",
        (result) => {
          const eligibility = result.package.dataset_expansion_eligibility;
          eligibility.eligible = false;
          eligibility.reasons = [
            "no_localizable_positive_seed",
            "check_role_is_evaluation_only",
          ];
          result.package.may_seed_dataset_expansion = false;
        },
        /eligibility.*reasons/i,
      ],
      [
        "role reason",
        (result) => {
          const eligibility = result.package.dataset_expansion_eligibility;
          eligibility.eligible = false;
          eligibility.reasons = ["check_role_is_evaluation_only"];
          result.package.may_seed_dataset_expansion = false;
        },
        /eligibility.*inconsistent/i,
      ],
      [
        "localizable count",
        (result) => {
          result.package.dataset_expansion_eligibility.validation_evidence.localizable_positive_seed_count += 1;
        },
        /eligibility.*inconsistent/i,
      ],
    ];

    for (const [name, mutate, expected] of attacks) {
      const result = structuredClone(golden.development_final_result);
      const session = parseBallAnnotationSession(golden.development_session);
      mutate(result);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(expected);
    }
  });

  it.each([
    ["extra in-range", 1, /frame collections.*sampled roles/i],
    ["out-of-range", 200, /revision.*outside the source/i],
  ])("rejects an %s revision frame", (_name, frameIndex, expected) => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const result = structuredClone(golden.development_final_result);
    const session = parseBallAnnotationSession(golden.development_session);
    const revision = structuredClone(result.package.revision_chain[0]);
    const mutationId = `extra-frame-${frameIndex}`;
    const annotation = revision.effective_annotation;
    const mutationRequest = {
      mutation_id: mutationId,
      expected_revision: 0,
      operation: "set",
      undo_revision: null,
      annotation,
      suggestion_kind: null,
      suggestion_id: null,
      accepted_suggestion_job_id: null,
      accepted_suggestion_sha256: null,
      dismissed_suggestion_kind: null,
      dismissed_suggestion_id: null,
      dismissed_suggestion_job_id: null,
      dismissed_suggestion_sha256: null,
    };
    revision.frame_index = frameIndex;
    revision.revision = 1;
    revision.mutation_id = mutationId;
    revision.mutation_sha256 = pythonCanonicalSha256Sync(
      {
        session_id: session.view.sessionId,
        frame_index: frameIndex,
        request: mutationRequest,
      },
      annotationAuthorityFloatPaths("$.request.annotation"),
    );
    revision.expected_revision = 0;
    revision.supersedes_revision = null;
    revision.undo_revision = null;
    revision.accepted_suggestion_kind = null;
    revision.accepted_suggestion_id = null;
    revision.accepted_suggestion_job_id = null;
    revision.accepted_suggestion_sha256 = null;
    revision.dismissed_suggestion_kind = null;
    revision.dismissed_suggestion_id = null;
    revision.dismissed_suggestion_job_id = null;
    revision.dismissed_suggestion_sha256 = null;
    revision.previous_effective_annotation = null;
    revision.revision_id = `revision-${pythonCanonicalSha256Sync(
      {
        session_id: session.view.sessionId,
        frame_index: frameIndex,
        revision: 1,
      },
      [],
    ).slice(0, 24)}`;
    revision.annotation_etag = pythonCanonicalSha256Sync(
      {
        schema_version: "1.0",
        artifact_type: "ball_annotation_effective_revision",
        session_id: session.view.sessionId,
        frame_index: frameIndex,
        revision: 1,
        effective_annotation: annotation,
      },
      annotationAuthorityFloatPaths("$.effective_annotation"),
    );
    result.package.revision_chain.push(revision);
    result.package.dataset_expansion_eligibility.validation_evidence.revision_chain_sha256 =
      pythonCanonicalSha256Sync(
        result.package.revision_chain,
        result.package.revision_chain.flatMap((_: unknown, index: number) => [
          ...annotationAuthorityFloatPaths(
            `$[${index}].previous_effective_annotation`,
          ),
          ...annotationAuthorityFloatPaths(`$[${index}].effective_annotation`),
        ]),
      );

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      expected,
    );
  });

  it.each([
    ["development", "development_session", "development_final_result"],
    ["check", "check_session_ready", "check_final_result"],
  ])(
    "rejects a coherently rebound %s final result with pending decisions",
    (role, sessionKey, resultKey) => {
      const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
      const parsedSession = parseBallAnnotationSession(golden[sessionKey]);
      const result = structuredClone(golden[resultKey]);
      for (const eligibility of [
        result.package.dataset_expansion_eligibility,
        result.feasibility_report.sealed_evidence.dataset_expansion_eligibility,
      ]) {
        eligibility.eligible = false;
        eligibility.reasons = [
          ...(role === "check" ? ["check_role_is_evaluation_only"] : []),
          "pending_suggestion_decisions",
        ];
        eligibility.validation_evidence.pending_detector_candidate_count = 1;
        eligibility.validation_evidence.pending_propagation_suggestion_count = 1;
        eligibility.validation_evidence.pending_suggestion_decision_count = 2;
      }
      result.package.package_sha256 = sha("8");
      result.package.may_seed_dataset_expansion = false;
      result.feasibility_report.sealed_evidence.annotation_package_sha256 =
        sha("8");
      result.feasibility_report.report_sha256 = sha("9");

      expect(() =>
        parseBallAnnotationFinalResult(result, parsedSession),
      ).toThrow(/pending/i);
    },
  );

  it("does not let an internally consistent positive metric row override absent human truth", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const result = structuredClone(golden.check_final_result);
    const session = parseBallAnnotationSession(golden.check_session_ready);
    makeInternallyConsistentPositiveFeasibilityFrame(
      result.feasibility_report.frames[0],
    );

    expect(() => parseBallAnnotationFinalResult(result, session)).toThrow(
      /differs from package truth authority/i,
    );
  });

  it("fails closed for malformed and self-contradictory per-frame metric evidence", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const attacks: Array<[string, (frame: any) => void, RegExp]> = [
      [
        "non-boolean eligibility",
        (frame) => {
          frame.metric_eligible = "true";
        },
        /eligibility is invalid/i,
      ],
      [
        "scored candidates over the frozen budget",
        (frame) => {
          frame.scored_candidate_count = 6;
          frame.raw_candidate_count = 6;
        },
        /candidate counts are invalid/i,
      ],
      [
        "raw count below scored count",
        (frame) => {
          frame.raw_candidate_count = 0;
        },
        /candidate counts are invalid/i,
      ],
      [
        "invalid Top-1 state",
        (frame) => {
          frame.top1_hit = "false";
        },
        /Top-1 state is invalid/i,
      ],
      [
        "invalid Top-5 state",
        (frame) => {
          frame.top5_hit = 0;
        },
        /Top-5 state is invalid/i,
      ],
      [
        "non-numeric diagonal",
        (frame) => {
          frame.bbox_diagonal_source_px = "1";
        },
        /box diagonal/i,
      ],
      [
        "non-numeric aspect ratio",
        (frame) => {
          frame.bbox_aspect_ratio = "1";
        },
        /box aspect ratio/i,
      ],
      [
        "unknown derived scale",
        (frame) => {
          frame.derived_scale_stratum = "close";
        },
        /derived scale/i,
      ],
      [
        "non-array motion tags",
        (frame) => {
          frame.motion_occlusion_tags = null;
        },
        /motion tags are invalid/i,
      ],
      [
        "duplicate motion tags",
        (frame) => {
          frame.motion_occlusion_tags = ["occluded", "occluded"];
        },
        /motion tags are invalid/i,
      ],
      [
        "partial box diagnostics",
        (frame) => {
          frame.bbox_diagonal_source_px = 1;
        },
        /box diagnostics are incomplete/i,
      ],
      [
        "derived scale without a box",
        (frame) => {
          frame.derived_scale_stratum = "near";
        },
        /scale diagnostics are invalid/i,
      ],
      [
        "present scale without a localizable box",
        (frame) => {
          frame.presence = "present";
          frame.observed_scale_stratum = "near";
        },
        /scale diagnostics are invalid/i,
      ],
      [
        "box attached to an absent frame",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.presence = "absent";
        },
        /box diagnostics are invalid/i,
      ],
      [
        "box without an observed scale",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.observed_scale_stratum = "not_applicable";
        },
        /box diagnostics are invalid/i,
      ],
      [
        "below-minimum diagonal carried as eligible",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 0.5,
            diagnosticCodes: ["bbox_diagonal_below_minimum"],
          });
        },
        /differs from package truth authority/i,
      ],
      [
        "above-maximum diagonal carried as eligible",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 3,
            diagnosticCodes: ["bbox_diagonal_above_maximum"],
          });
        },
        /differs from package truth authority/i,
      ],
      [
        "aspect ratio above the frozen bound",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 1,
            aspect: 5,
            derivedScale: "near",
            diagnosticCodes: ["bbox_aspect_ratio_out_of_bounds"],
          });
        },
        /differs from package truth authority/i,
      ],
      [
        "aspect ratio below the frozen bound",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 1,
            aspect: 0.1,
            derivedScale: "near",
            diagnosticCodes: ["bbox_aspect_ratio_out_of_bounds"],
          });
        },
        /differs from package truth authority/i,
      ],
      [
        "incorrect derived scale",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.derived_scale_stratum = "far";
        },
        /derived scale is invalid/i,
      ],
      [
        "observed and derived scale mismatch",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 1,
            derivedScale: "near",
            observedScale: "far",
            diagnosticCodes: ["scale_stratum_mismatch:far:near"],
          });
        },
        /differs from package truth authority/i,
      ],
      [
        "non-array diagnostic codes",
        (frame) => {
          frame.diagnostic_codes = null;
        },
        /diagnostic codes are invalid/i,
      ],
      [
        "duplicate diagnostic codes",
        (frame) => {
          frame.diagnostic_codes = ["unexpected", "unexpected"];
        },
        /diagnostic codes are invalid/i,
      ],
      [
        "invented diagnostic code",
        (frame) => {
          frame.diagnostic_codes = ["invented"];
        },
        /diagnostic codes are invalid/i,
      ],
      [
        "false eligibility for a confirmed absence",
        (frame) => {
          frame.metric_eligible = false;
          frame.top1_hit = null;
          frame.top5_hit = null;
          frame.scored_candidate_count = 0;
          frame.raw_candidate_count = 0;
          frame.candidate_diagnostics = [];
        },
        /eligibility is invalid/i,
      ],
      [
        "candidate rank above budget",
        (frame) => {
          frame.candidate_diagnostics[0].rank = 6;
        },
        /candidate 0 state is invalid/i,
      ],
      [
        "non-boolean candidate match",
        (frame) => {
          frame.candidate_diagnostics[0].matched = "false";
        },
        /candidate 0 state is invalid/i,
      ],
      [
        "partial candidate measurements",
        (frame) => {
          frame.candidate_diagnostics[0].center_distance_source_px = 1;
        },
        /candidate 0 measurements are invalid/i,
      ],
      [
        "IoU above one",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.candidate_diagnostics[0].iou = 2;
        },
        /candidate 0 measurements are invalid/i,
      ],
      [
        "match without measurements",
        (frame) => {
          frame.candidate_diagnostics[0].matched = true;
        },
        /candidate 0 measurements are invalid/i,
      ],
      [
        "match flag disagrees with measured radius",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.candidate_diagnostics[0].matched = true;
        },
        /candidate 0 measurements are invalid/i,
      ],
      [
        "measured radius differs from metric authority",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.candidate_diagnostics[0].evaluation_radius_source_px = 5;
        },
        /candidate 0 measurements are invalid/i,
      ],
      [
        "noncanonical candidate ranks",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.candidate_diagnostics[0].rank = 2;
        },
        /metric evidence is invalid/i,
      ],
      [
        "Top-1 hit disagrees with the first candidate",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.top1_hit = true;
        },
        /metric evidence is invalid/i,
      ],
      [
        "Top-5 hit disagrees with all candidates",
        (frame) => {
          makeInternallyConsistentPositiveFeasibilityFrame(frame);
          frame.top5_hit = true;
        },
        /metric evidence is invalid/i,
      ],
      [
        "ineligible frame retains scored evidence",
        (frame) => {
          makeIneligiblePositiveFeasibilityFrame(frame, {
            diagonal: 0.5,
            diagnosticCodes: ["bbox_diagonal_below_minimum"],
          });
          frame.top1_hit = false;
        },
        /ineligible metric evidence is invalid/i,
      ],
    ];

    for (const [name, mutate, expected] of attacks) {
      const result = structuredClone(golden.check_final_result);
      const session = parseBallAnnotationSession(golden.check_session_ready);
      mutate(result.feasibility_report.frames[0]);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(expected);
    }
  }, 30_000);

  it("fails closed at each aggregate feasibility authority boundary", () => {
    const golden = ballAnnotationApiGolden as unknown as Record<string, any>;
    const attacks: Array<
      [string, (result: Record<string, any>, session: any) => void, RegExp]
    > = [
      [
        "metric profile constants",
        (result) => {
          result.feasibility_report.metric_profile.top1_recall_target = 0.61;
        },
        /metric profile is invalid/i,
      ],
      [
        "computed source height",
        (result) => {
          result.feasibility_report.computed_source_px_bounds.source_height_px += 1;
        },
        /computed source bounds differ/i,
      ],
      [
        "frame order",
        (result) => {
          result.feasibility_report.frames.reverse();
        },
        /frame set differs from sampling authority/i,
      ],
      [
        "support totals",
        (result) => {
          result.feasibility_report.support.total_frames += 1;
        },
        /support counts are inconsistent/i,
      ],
      [
        "support list item type",
        (result) => {
          result.feasibility_report.support.missing = [1];
        },
        /strata support is invalid/i,
      ],
      [
        "aggregate candidate numerator",
        (result) => {
          const metric =
            result.feasibility_report.metrics.candidates_per_evaluable_frame;
          metric.raw.numerator += 1;
          metric.point_estimate = metric.raw.numerator / metric.raw.denominator;
        },
        /metrics are inconsistent with frame evidence/i,
      ],
      [
        "contradiction collection type",
        (result) => {
          result.feasibility_report.contradictions = null;
        },
        /contradictions are invalid/i,
      ],
      [
        "contradiction diagnostics type",
        (result) => {
          result.feasibility_report.contradictions[0].diagnostic_codes = null;
        },
        /contradiction diagnostics are invalid/i,
      ],
      [
        "resolution reason-code type",
        (result) => {
          result.feasibility_report.resolution.reason_codes = null;
        },
        /resolution is invalid/i,
      ],
      [
        "failed-report authorization",
        (result) => {
          result.feasibility_report.authorizations.trial_eligible = true;
        },
        /authorization/i,
      ],
      [
        "limitation item type",
        (result) => {
          result.feasibility_report.limitations = [1];
        },
        /report authority is invalid/i,
      ],
      [
        "limitation order",
        (result) => {
          result.feasibility_report.limitations.reverse();
        },
        /limitations are invalid/i,
      ],
      [
        "duplicate supplemental frame authority",
        (result) => {
          result.package.supplemental_frame_indices = [8, 8];
        },
        /supplemental frame indices are not canonical/i,
      ],
      [
        "frame evidence collection digest",
        (result) => {
          result.package.frame_evidence_sha256 = sha("9");
        },
        /frame evidence collection digest is invalid/i,
      ],
      [
        "eligibility evidence digest",
        (result) => {
          result.package.dataset_expansion_eligibility.validation_evidence.frame_evidence_sha256 =
            sha("9");
        },
        /dataset expansion evidence digest is invalid/i,
      ],
      [
        "session package pointer",
        (_result, session) => {
          session.view.finalPackage = { packageSha256: sha("9") };
        },
        /package digest differs from the session/i,
      ],
    ];

    for (const [name, mutate, expected] of attacks) {
      const result = structuredClone(golden.check_final_result);
      const session = parseBallAnnotationSession(golden.check_session_ready);
      mutate(result, session);
      expect(
        () => parseBallAnnotationFinalResult(result, session),
        name,
      ).toThrow(expected);
    }

    const developmentResult = structuredClone(golden.development_final_result);
    const developmentSession = parseBallAnnotationSession(
      golden.development_session,
    );
    developmentResult.feasibility_report.reason = "check evidence";
    expect(() =>
      parseBallAnnotationFinalResult(developmentResult, developmentSession),
    ).toThrow(/development sealed evidence is invalid/i);
  }, 30_000);
});

describe("verified ball annotation frame fetch", () => {
  it("checks server headers and content SHA before creating an object URL", async () => {
    const bytes = new Uint8Array([255, 216, 255, 217]);
    const contentSha = await digest(bytes);
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:verified-frame");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(bytes, {
        status: 200,
        headers: {
          "Content-Type": "image/jpeg",
          "Cache-Control": "no-store",
          ETag: `"${contentSha}"`,
          "X-Content-SHA256": contentSha,
          "X-Source-Frame-Index": "2000",
        },
      }),
    );

    const result = await fetchVerifiedBallAnnotationFrame({
      sessionId: "annotation-session-1",
      frameIndex: 2_000,
      expectedSha256: contentSha,
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/ball-annotation-sessions/annotation-session-1/frames/2000",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result).toEqual({
      objectUrl: "blob:verified-frame",
      contentSha256: contentSha,
      etag: `"${contentSha}"`,
      contentType: "image/jpeg",
      sizeBytes: 4,
    });
    expect(createObjectURL).toHaveBeenCalledOnce();
  });

  it("accepts source frame zero only when its header is explicitly present", async () => {
    const bytes = new Uint8Array([255, 216, 255, 217]);
    const contentSha = await digest(bytes);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:frame-zero");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(bytes, {
        headers: {
          "Content-Type": "image/jpeg",
          "Cache-Control": "no-store",
          ETag: `"${contentSha}"`,
          "X-Content-SHA256": contentSha,
          "X-Source-Frame-Index": "0",
        },
      }),
    );

    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 0,
        expectedSha256: contentSha,
      }),
    ).resolves.toEqual(expect.objectContaining({ contentSha256: contentSha }));
  });

  it.each([
    ["missing", undefined],
    ["blank", ""],
  ])(
    "rejects a %s source-frame-index header even for frame zero",
    async (_, value) => {
      const bytes = new Uint8Array([255, 216, 255, 217]);
      const contentSha = await digest(bytes);
      const headers = new Headers({
        "Content-Type": "image/jpeg",
        "Cache-Control": "no-store",
        ETag: `"${contentSha}"`,
        "X-Content-SHA256": contentSha,
      });
      if (value !== undefined) headers.set("X-Source-Frame-Index", value);
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(bytes, { headers }),
      );

      await expect(
        fetchVerifiedBallAnnotationFrame({
          sessionId: "annotation-session-1",
          frameIndex: 0,
          expectedSha256: contentSha,
        }),
      ).rejects.toThrow(/headers/);
    },
  );

  it.each([
    ["wrong content digest", { contentSha: sha("0") }],
    ["wrong source index", { frameIndexHeader: "2001" }],
    ["missing no-store", { cacheControl: "public" }],
    ["wrong media type", { contentType: "text/html" }],
    ["weak ETag", { etag: `W/"${sha("b")}"` }],
    ["wildcard ETag", { etag: "*" }],
    ["multiple ETags", { etag: `"${sha("b")}", "${sha("c")}"` }],
    ["unquoted ETag", { etag: sha("b") }],
    ["quoted non-digest ETag", { etag: '"frame-etag"' }],
    ["strong ETag not bound to content digest", { etag: `"${sha("b")}"` }],
  ])("fails closed on %s without creating an object URL", async (_, patch) => {
    const bytes = new Uint8Array([1, 2, 3]);
    const actualSha = await digest(bytes);
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:must-not-exist");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(bytes, {
        headers: {
          "Content-Type": patch.contentType ?? "image/jpeg",
          "Cache-Control": patch.cacheControl ?? "no-store",
          ETag: patch.etag ?? `"${actualSha}"`,
          "X-Content-SHA256": patch.contentSha ?? actualSha,
          "X-Source-Frame-Index": patch.frameIndexHeader ?? "2000",
        },
      }),
    );
    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: actualSha,
      }),
    ).rejects.toThrow();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("rejects an oversized declared frame before reading its body", async () => {
    const response = new Response(new Uint8Array([1]), {
      headers: {
        "Content-Type": "image/jpeg",
        "Content-Length": String(32 * 1024 * 1024 + 1),
        "Cache-Control": "no-store",
        ETag: `"${sha("a")}"`,
        "X-Content-SHA256": sha("a"),
        "X-Source-Frame-Index": "2000",
      },
    });
    const read = vi.spyOn(response, "arrayBuffer");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: sha("a"),
      }),
    ).rejects.toThrow("size");
    expect(read).not.toHaveBeenCalled();
  });

  it("rejects Content-Length that differs from the verified body", async () => {
    const bytes = new Uint8Array([1, 2, 3]);
    const actualSha = await digest(bytes);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(bytes, {
        headers: {
          "Content-Type": "image/jpeg",
          "Content-Length": "4",
          "Cache-Control": "no-store",
          ETag: `"${actualSha}"`,
          "X-Content-SHA256": actualSha,
          "X-Source-Frame-Index": "2000",
        },
      }),
    );

    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: actualSha,
      }),
    ).rejects.toThrow("size");
  });

  it.each([0, 32 * 1024 * 1024 + 1])(
    "rejects a %d-byte body after reading when Content-Length is absent",
    async (size) => {
      const response = new Response(null, {
        headers: {
          "Content-Type": "image/jpeg",
          "Cache-Control": "no-store",
          ETag: `"${sha("a")}"`,
          "X-Content-SHA256": sha("a"),
          "X-Source-Frame-Index": "2000",
        },
      });
      vi.spyOn(response, "arrayBuffer").mockResolvedValue(
        new ArrayBuffer(size),
      );
      vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
      await expect(
        fetchVerifiedBallAnnotationFrame({
          sessionId: "annotation-session-1",
          frameIndex: 2_000,
          expectedSha256: sha("a"),
        }),
      ).rejects.toThrow("size");
    },
  );

  it("accepts Content-Length exactly at the 32 MiB boundary", async () => {
    const response = new Response(null, {
      headers: {
        "Content-Type": "image/jpeg",
        "Content-Length": String(32 * 1024 * 1024),
        "Cache-Control": "no-store",
        ETag: `"${sha("a")}"`,
        "X-Content-SHA256": sha("a"),
        "X-Source-Frame-Index": "2000",
      },
    });
    vi.spyOn(response, "arrayBuffer").mockResolvedValue(
      new ArrayBuffer(32 * 1024 * 1024),
    );
    vi.spyOn(crypto.subtle, "digest").mockResolvedValue(
      new Uint8Array(32).fill(0xaa).buffer,
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:max-frame");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: sha("a"),
      }),
    ).resolves.toEqual(
      expect.objectContaining({
        objectUrl: "blob:max-frame",
        sizeBytes: 32 * 1024 * 1024,
      }),
    );
  });

  it("cancels one malicious undeclared chunk larger than 32 MiB", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(32 * 1024 * 1024 + 1));
      },
      cancel() {
        cancelled = true;
      },
    });
    const response = new Response(stream, {
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "no-store",
        ETag: `"${sha("a")}"`,
        "X-Content-SHA256": sha("a"),
        "X-Source-Frame-Index": "2000",
      },
    });
    const unboundedRead = vi.spyOn(response, "arrayBuffer");
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:must-not-exist");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: sha("a"),
      }),
    ).rejects.toThrow("size");
    expect(cancelled).toBe(true);
    expect(unboundedRead).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("cancels an undeclared streaming body as soon as it crosses 32 MiB", async () => {
    let pullCount = 0;
    let producedBytes = 0;
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pullCount += 1;
        if (pullCount <= 2) {
          const chunk = new Uint8Array(16 * 1024 * 1024);
          producedBytes += chunk.byteLength;
          controller.enqueue(chunk);
        } else {
          const chunk = new Uint8Array([1]);
          producedBytes += chunk.byteLength;
          controller.enqueue(chunk);
        }
      },
      cancel() {
        cancelled = true;
      },
    });
    const response = new Response(stream, {
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "no-store",
        ETag: `"${sha("a")}"`,
        "X-Content-SHA256": sha("a"),
        "X-Source-Frame-Index": "2000",
      },
    });
    const unboundedRead = vi.spyOn(response, "arrayBuffer");
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:must-not-exist");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

    await expect(
      fetchVerifiedBallAnnotationFrame({
        sessionId: "annotation-session-1",
        frameIndex: 2_000,
        expectedSha256: sha("a"),
      }),
    ).rejects.toThrow("size");

    // Web Streams may pre-pull one highWaterMark chunk, but cancellation must
    // bound that extra production rather than allow an unbounded body read.
    expect(pullCount).toBeGreaterThanOrEqual(3);
    expect(producedBytes).toBeLessThanOrEqual(32 * 1024 * 1024 + 2);
    expect(cancelled).toBe(true);
    expect(unboundedRead).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});
