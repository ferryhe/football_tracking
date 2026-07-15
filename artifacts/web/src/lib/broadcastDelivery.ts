import type { ArtifactSummary, RunRecord } from "@workspace/api-client-react";

import { canonicalJson, sha256Text } from "./productionTrial";

export const PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS = [
  "broadcast.mp4",
  "broadcast_quality_report.json",
  "camera_target.csv",
  "ball_track.v2.csv",
  "review_decisions.json",
  "action_track.csv",
  "candidate_classifications.jsonl",
  "ball_candidates.jsonl",
] as const;

export type ProductionBroadcastDeliveryArtifact =
  (typeof PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS)[number];

export interface ProductionProductEvidence {
  run_id: string;
  artifact_name: "broadcast.mp4";
  artifact_size_bytes: number;
  artifact_sha256: string;
  quality_report_sha256: string;
  status_generation: string;
  verified_at: string;
}

export type BroadcastDeliveryBlockedReason =
  | "parent_not_ready"
  | "invalid_status_generation"
  | "artifact_set_mismatch"
  | "invalid_artifact"
  | "quality_size_mismatch"
  | "quality_not_utf8"
  | "quality_not_json"
  | "quality_schema_mismatch"
  | "quality_generation_mismatch"
  | "quality_generation_digest_mismatch"
  | "quality_blocked"
  | "video_binding_mismatch";

export type BroadcastDeliveryAssessment =
  | {
      status: "verified";
      evidence: ProductionProductEvidence;
      artifacts: ReadonlyMap<
        ProductionBroadcastDeliveryArtifact,
        ArtifactSummary
      >;
      limitations: string[];
    }
  | { status: "blocked"; reasons: BroadcastDeliveryBlockedReason[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function sha256String(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function validArtifact(artifact: ArtifactSummary): boolean {
  return (
    artifact.exists === true &&
    Number.isInteger(artifact.size_bytes) &&
    Number(artifact.size_bytes) >= 0 &&
    PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.includes(
      artifact.name as ProductionBroadcastDeliveryArtifact,
    )
  );
}

function artifactIndex(
  artifacts: readonly ArtifactSummary[],
): ReadonlyMap<ProductionBroadcastDeliveryArtifact, ArtifactSummary> | null {
  if (artifacts.length !== PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.length) {
    return null;
  }
  const byName = new Map<
    ProductionBroadcastDeliveryArtifact,
    ArtifactSummary
  >();
  for (const artifact of artifacts) {
    if (!validArtifact(artifact) || byName.has(artifact.name as never)) {
      return null;
    }
    byName.set(artifact.name as ProductionBroadcastDeliveryArtifact, artifact);
  }
  return PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.every((name) =>
    byName.has(name),
  )
    ? byName
    : null;
}

function bytes(value: ArrayBuffer | Uint8Array): Uint8Array {
  return value instanceof Uint8Array ? value : new Uint8Array(value);
}

function uniqueReasons(
  reasons: BroadcastDeliveryBlockedReason[],
): BroadcastDeliveryBlockedReason[] {
  return [...new Set(reasons)];
}

function blocked(
  ...reasons: BroadcastDeliveryBlockedReason[]
): BroadcastDeliveryAssessment {
  return { status: "blocked", reasons: uniqueReasons(reasons) };
}

export async function assessBroadcastDelivery(input: {
  run: Pick<RunRecord, "run_id" | "source" | "status" | "broadcast">;
  artifacts: readonly ArtifactSummary[];
  quality_report_bytes: ArrayBuffer | Uint8Array;
  verified_at: string;
}): Promise<BroadcastDeliveryAssessment> {
  const generation = input.run.broadcast?.status_generation;
  if (
    input.run.source !== "broadcast_hybrid" ||
    input.run.status !== "completed" ||
    input.run.broadcast?.status !== "ready"
  ) {
    return blocked("parent_not_ready");
  }
  if (!sha256String(generation)) {
    return blocked("invalid_status_generation");
  }
  const artifacts = artifactIndex(input.artifacts);
  if (!artifacts) return blocked("artifact_set_mismatch");
  const qualityArtifact = artifacts.get("broadcast_quality_report.json");
  const videoArtifact = artifacts.get("broadcast.mp4");
  if (!qualityArtifact || !videoArtifact) {
    return blocked("invalid_artifact");
  }
  const reportBytes = bytes(input.quality_report_bytes);
  if (reportBytes.byteLength !== qualityArtifact.size_bytes) {
    return blocked("quality_size_mismatch");
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(reportBytes);
  } catch {
    return blocked("quality_not_utf8");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return blocked("quality_not_json");
  }
  if (!isRecord(parsed)) return blocked("quality_not_json");
  const reasons: BroadcastDeliveryBlockedReason[] = [];
  if (
    parsed.schema_version !== "1.0" ||
    parsed.artifact_type !== "broadcast_quality_report" ||
    parsed.status !== "ready" ||
    !Array.isArray(parsed.blocking_reasons) ||
    !Array.isArray(parsed.limitations) ||
    !parsed.limitations.every(nonEmpty) ||
    !isRecord(parsed.artifacts)
  ) {
    reasons.push("quality_schema_mismatch");
  }
  if (parsed.status_generation !== generation) {
    reasons.push("quality_generation_mismatch");
  }
  if (
    Array.isArray(parsed.blocking_reasons) &&
    parsed.blocking_reasons.length > 0
  ) {
    reasons.push("quality_blocked");
  }
  const stable = { ...parsed };
  delete stable.generated_at;
  delete stable.status_generation;
  try {
    if ((await sha256Text(canonicalJson(stable))) !== generation) {
      reasons.push("quality_generation_digest_mismatch");
    }
  } catch {
    reasons.push("quality_schema_mismatch");
  }
  const videoBinding = isRecord(parsed.artifacts)
    ? parsed.artifacts["broadcast.mp4"]
    : null;
  if (
    !isRecord(videoBinding) ||
    videoBinding.status !== "available" ||
    videoBinding.path !== "broadcast.mp4" ||
    videoBinding.size_bytes !== videoArtifact.size_bytes ||
    !sha256String(videoBinding.sha256)
  ) {
    reasons.push("video_binding_mismatch");
  }
  if (reasons.length > 0) return blocked(...reasons);

  return {
    status: "verified",
    evidence: {
      run_id: input.run.run_id,
      artifact_name: "broadcast.mp4",
      artifact_size_bytes: Number(videoArtifact.size_bytes),
      artifact_sha256: String((videoBinding as Record<string, unknown>).sha256),
      quality_report_sha256: await sha256Text(text),
      status_generation: generation,
      verified_at: input.verified_at,
    },
    artifacts,
    limitations: [...(parsed.limitations as string[])],
  };
}

export function isProductionProductEvidence(
  value: unknown,
): value is ProductionProductEvidence {
  return Boolean(
    isRecord(value) &&
    nonEmpty(value.run_id) &&
    value.artifact_name === "broadcast.mp4" &&
    Number.isInteger(value.artifact_size_bytes) &&
    Number(value.artifact_size_bytes) >= 0 &&
    sha256String(value.artifact_sha256) &&
    sha256String(value.quality_report_sha256) &&
    sha256String(value.status_generation) &&
    nonEmpty(value.verified_at),
  );
}
