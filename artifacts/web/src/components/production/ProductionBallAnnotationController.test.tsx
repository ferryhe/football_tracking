import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getBallAnnotationSession } from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";
import type { SafeBrowserStorage } from "@/lib/browserStorage";
import type {
  ParsedBallAnnotationSession,
  ParsedBallPropagationJob,
} from "@/lib/productionBallAnnotation";
import type { ReviewProxyRepairJobView } from "@/lib/productionReviewProxyRepair";
import {
  ballAnnotationSessionFixture,
  refreshBallAnnotationProgress,
} from "@/test/ballAnnotationFixtures";

import {
  parseBallAnnotationFrameIntervals,
  ProductionBallAnnotationController,
  recoverBallAnnotationLaunch,
  requireBallAnnotationResponseMetadata,
} from "./ProductionBallAnnotationController";

const mocks = vi.hoisted(() => ({
  fetchFrame: vi.fn(),
  parseSession: vi.fn(),
  parseRevision: vi.fn(),
  parseFinal: vi.fn(),
  parsePropagation: vi.fn(),
  createRepair: vi.fn(),
  getRepair: vi.fn(),
  cancelRepair: vi.fn(),
  retryRepair: vi.fn(),
}));

vi.mock("@/lib/productionBallAnnotation", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/productionBallAnnotation")>();
  return {
    ...actual,
    fetchVerifiedBallAnnotationFrame: mocks.fetchFrame,
    parseBallAnnotationSession: mocks.parseSession,
    parseBallAnnotationRevision: mocks.parseRevision,
    parseBallAnnotationFinalResult: mocks.parseFinal,
    parseBallPropagationJob: mocks.parsePropagation,
  };
});

vi.mock("@/lib/productionReviewProxyRepair", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/productionReviewProxyRepair")>();
  return {
    ...actual,
    createReviewProxyRepair: mocks.createRepair,
    getReviewProxyRepair: mocks.getRepair,
    cancelReviewProxyRepair: mocks.cancelRepair,
    retryReviewProxyRepair: mocks.retryRepair,
  };
});

vi.mock("./ProductionBallAnnotationPanel", () => ({
  ProductionBallAnnotationPanel: (props: any) => {
    const frame = props.session.frames[props.activeFrameOffset];
    const annotation = (provenance: string) => ({
      point: [100, 100],
      bbox: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      trainingUse: "positive",
      annotationState: "confirmed",
      scaleStratum: "far",
      lightingTag: "bright_sun",
      motionOcclusionTags: [],
      provenance,
    });
    const candidate = frame?.suggestedCandidates?.[0];
    const propagation = frame?.propagationSuggestions?.[0];
    return (
      <section data-testid="annotation-panel">
        <p>frame {props.session.frames[props.activeFrameOffset]?.frameIndex}</p>
        <p>session stage {props.session.stage}</p>
        <p>image {props.frameImageUrl ?? props.frameImageState}</p>
        <p>image identity {props.frameImageIdentity ?? "none"}</p>
        {props.operationError && <p role="alert">{props.operationError}</p>}
        <button type="button" onClick={() => props.onNavigate(1)}>
          next test frame
        </button>
        <button
          type="button"
          onClick={() => props.onSave(annotation("manual_human_annotation"))}
        >
          save test annotation
        </button>
        <button type="button" onClick={props.onFinalize}>
          finalize test session
        </button>
        <button type="button" onClick={props.onDelete}>
          delete saved annotation
        </button>
        <button type="button" onClick={() => props.onUndoSaved(1)}>
          undo saved annotation
        </button>
        <button type="button" onClick={() => props.onStartPropagation(2)}>
          start hidden propagation hook
        </button>
        <button type="button" onClick={props.onCancelPropagation}>
          cancel hidden propagation hook
        </button>
        {props.session.reviewProxyRepair && (
          <button type="button" onClick={props.onStartReviewProxyRepair}>
            start review proxy repair hook
          </button>
        )}
        {props.reviewProxyRepairJob && (
          <p>
            repair {props.reviewProxyRepairJob.status}{" "}
            {props.reviewProxyRepairJob.progress.sourceFramesCompleted}
          </p>
        )}
        {props.reviewProxyRepairJob?.canCancel && (
          <button
            type="button"
            onClick={() =>
              props.onCancelReviewProxyRepair(
                props.reviewProxyRepairJob.repairId,
              )
            }
          >
            cancel review proxy repair hook
          </button>
        )}
        {props.reviewProxyRepairJob?.canRetry && (
          <button
            type="button"
            onClick={() =>
              props.onRetryReviewProxyRepair(
                props.reviewProxyRepairJob.repairId,
              )
            }
          >
            retry review proxy repair hook
          </button>
        )}
        {props.reviewProxyRepairJob && props.reviewProxyRepairError && (
          <button
            type="button"
            onClick={() =>
              props.onReloadReviewProxyRepair(
                props.reviewProxyRepairJob.repairId,
              )
            }
          >
            reload review proxy repair hook
          </button>
        )}
        {props.reviewProxyRepairError && (
          <p role="alert">{props.reviewProxyRepairError}</p>
        )}
        {candidate && <p>candidate decision {candidate.decision}</p>}
        {candidate?.decision === "pending" && (
          <>
            <button
              type="button"
              onClick={() =>
                props.onSave(annotation("detector_candidate_human_confirmed"), {
                  action: "accept",
                  kind: "detector_candidate",
                  id: candidate.candidateId,
                  jobId: candidate.suggestionJobId,
                  sha256: candidate.suggestionSha256,
                })
              }
            >
              accept exact detector suggestion
            </button>
            <button
              type="button"
              onClick={() =>
                props.onSave(annotation("detector_candidate_human_confirmed"), {
                  action: "accept",
                  kind: "detector_candidate",
                  id: candidate.candidateId,
                  jobId: "tampered-job",
                  sha256: candidate.suggestionSha256,
                })
              }
            >
              accept tampered detector suggestion
            </button>
            <button
              type="button"
              onClick={() =>
                props.onSave(annotation("detector_candidate_human_confirmed"), {
                  action: "accept",
                  kind: "detector_candidate",
                  id: candidate.candidateId,
                  jobId: candidate.suggestionJobId,
                })
              }
            >
              accept incomplete detector suggestion
            </button>
          </>
        )}
        {propagation && (
          <button
            type="button"
            onClick={() =>
              props.onSave(annotation("suggestion_dismissed_manual"), {
                action: "dismiss",
                kind: "propagation",
                id: propagation.suggestionId,
                jobId: propagation.jobId,
                sha256: propagation.suggestionSha256,
              })
            }
          >
            dismiss exact propagation suggestion
          </button>
        )}
        <p>propagation {String(props.propagationAvailable)}</p>
      </section>
    );
  },
}));

vi.mock("./ProductionFeasibilityDashboard", () => ({
  ProductionFeasibilityDashboard: () => <p>strict dashboard rendered</p>,
}));

const sha = (character: string) => character.repeat(64);

function parsedSession(
  role: "development" | "check" = "development",
  status: ParsedBallAnnotationSession["view"]["status"] = "annotating",
): ParsedBallAnnotationSession {
  return {
    developmentProbeJobIds: ["probe-ready-1"],
    samplingManifestSha256: sha("1"),
    targetFrameCount: role === "check" ? 20 : 2,
    view: {
      sessionId: "annotation-session-1",
      requestSha256: sha("2"),
      dataRole: role,
      status,
      stage: status,
      source: {
        sourceId: "source-1",
        sourceSha256: sha("3"),
        width: 5120,
        height: 1440,
        frameCount: 100,
        fps: 20,
      },
      decode: {
        requestedMode: "sequential",
        effectiveMode: "sequential",
        positionVerification: "opencv_next_frame_index_with_0.25_tolerance",
      },
      lockedProfile: {
        profileId: "official-coco-yolo11s-sahi",
        profileSha256: sha("4"),
      },
      controlProfileId: "control-yolov8n",
      samplingManifestSha256: sha("1"),
      metricProfileId: "tiny_ball_feasibility_metric_v1",
      attemptFamilySha256: sha("a"),
      developmentPackageBinding:
        role === "check"
          ? {
              sessionId: "development-session-1",
              packageSha256: sha("b"),
              attemptFamilySha256: sha("a"),
            }
          : null,
      checkProbeJobId: role === "check" ? "check-probe-1" : null,
      checkProbeAuthority:
        role === "check"
          ? { jobId: "check-probe-1", reportSha256: sha("c") }
          : null,
      retryFromSessionId: null,
      retryLineage: null,
      errorCode: null,
      blockerCode: null,
      reviewProxyRepair: null,
      frames: [10, 20].map((frameIndex, index) => ({
        frameIndex,
        displayTimeSeconds: index,
        decoderReportedPosMsec: index * 1_000,
        decoderTimeSeconds: index,
        truePresentationTimestamp: {
          status: "not_collected",
          valueSeconds: null,
          method: null,
        },
        proxyBinding: null,
        temporalGroupId: sha(index ? "6" : "5"),
        sourceFrameSha256: sha(index ? "8" : "7"),
        annotationRevision: 0,
        annotationEtag: `"${sha(index ? "a" : "9")}"`,
        suggestedCandidates: [],
        currentAnnotation: null,
      })),
      progress: {
        annotatedFrames: 0,
        totalFrames: 2,
        unconfirmedSuggestions: 0,
        missingStrata: [],
      },
      finalPackage: status === "finalized" ? { packageSha256: sha("b") } : null,
    },
  };
}

function parsedPropagationJob(
  status: ParsedBallPropagationJob["view"]["status"] = "queued",
): ParsedBallPropagationJob {
  return {
    sessionId: "annotation-session-1",
    mutationId: "propagation-mutation-1",
    seedFrameIndex: 10,
    expectedSeedRevision: 1,
    radiusFrames: 2,
    intentSha256: sha("f"),
    view: {
      jobId: "propagation-job-1",
      status,
      stage: status,
      pendingCount: status === "ready" ? 2 : 0,
      targetFrameIndices: [8, 9, 11, 12],
      errorCode: null,
    },
  };
}

function blockedReviewProxySession(): ParsedBallAnnotationSession {
  const parsed = parsedSession("development", "blocked");
  parsed.view.stage = "review_proxy_required";
  parsed.view.frames = [];
  parsed.view.progress = {
    annotatedFrames: 0,
    totalFrames: 0,
    unconfirmedSuggestions: 0,
    missingStrata: [],
  };
  parsed.targetFrameCount = 0;
  parsed.view.errorCode = "review_media_decode_unavailable";
  parsed.view.blockerCode = "review_proxy_required";
  parsed.view.reviewProxyRepair = {
    eligible: true,
    action: "generate_verified_review_proxy",
    createUrl: "/api/v1/detector-review-proxy-repairs",
    parentProbeJobId: "probe-ready-1",
    parentProbeReportSha256: sha("5"),
    parentProbeResultManifestSha256: sha("6"),
    parentProbeRecordSha256: sha("7"),
    blockedSessionRecordSha256: sha("8"),
  };
  return parsed;
}

function replacementReviewProxySession(): ParsedBallAnnotationSession {
  const parsed = parsedSession("development", "annotating");
  parsed.developmentProbeJobIds = ["probe-ready-1", "probe-proxy-1"];
  parsed.view.sessionId = "annotation-session-2";
  parsed.view.requestSha256 = sha("9");
  parsed.view.attemptFamilySha256 = sha("a");
  parsed.view.retryFromSessionId = "annotation-session-1";
  parsed.view.retryLineage = {
    mode: "review_proxy_decode_upgrade",
    previousSessionId: "annotation-session-1",
    previousErrorCode: "review_media_decode_unavailable",
    previousBlockerCode: "review_proxy_required",
    previousLineageSha256: sha("b"),
    currentLineageSha256: sha("c"),
    samplingManifestSha256: sha("1"),
  };
  parsed.view.frames = parsed.view.frames.map((frame) => ({
    ...frame,
    proxyBinding: {
      proxySha256: sha("1"),
      proxySizeBytes: 5_000,
      proxyWidth: 2560,
      proxyHeight: 720,
      mapSha256: sha("2"),
      bindingSha256: sha("d"),
      sourceFrame: {
        frameIndex: frame.frameIndex,
        decoderReportedPosMsec: frame.decoderReportedPosMsec,
        sha256: frame.sourceFrameSha256,
      },
      proxyFrame: {
        frameIndex: frame.frameIndex,
        decoderReportedPosMsec:
          frame.decoderReportedPosMsec ?? frame.displayTimeSeconds * 1_000,
        sha256: sha("e"),
      },
      mapTimeToleranceMsec: 2,
      declaredOffsetMsec: 0,
      observedOffsetMsec: 0,
      residualMsec: 0,
    },
  }));
  return parsed;
}

function reviewProxyRepairJob(
  status: ReviewProxyRepairJobView["status"],
  options: {
    completed?: number;
    updatedAt?: string;
    repairId?: string;
    attemptRootRepairId?: string;
    attemptNumber?: number;
    retryFromRepairId?: string | null;
    requestSha256?: string;
  } = {},
): ReviewProxyRepairJobView {
  const repairId = options.repairId ?? "repair-1";
  const attemptNumber = options.attemptNumber ?? 1;
  const completed = options.completed ?? (status === "ready" ? 100 : 25);
  const updatedAt =
    options.updatedAt ??
    (status === "ready" ? "2026-07-18T20:00:02Z" : "2026-07-18T20:00:01Z");
  return {
    repairId,
    attemptRootRepairId:
      options.attemptRootRepairId ??
      (attemptNumber === 1 ? repairId : "repair-1"),
    attemptNumber,
    retryFromRepairId:
      options.retryFromRepairId ?? (attemptNumber === 1 ? null : "repair-1"),
    requestSha256:
      options.requestSha256 ?? sha(attemptNumber === 1 ? "d" : "e"),
    status,
    stage: status === "committing" ? "proxy_ready" : status,
    presetId: "h264-cfr-720p-v1",
    eligibility: {
      eligible: true,
      action: "generate_verified_review_proxy",
      blockerCode: "review_proxy_required",
    },
    authority: {
      blockedSessionId: "annotation-session-1",
      blockedSessionRequestSha256: sha("2"),
      blockedSessionRecordSha256: sha("8"),
      parentProbeJobId: "probe-ready-1",
      developmentProbeJobIds: ["probe-ready-1"],
      parentProbeRequestSha256: sha("3"),
      parentProbeIntentSha256: sha("4"),
      parentProbeSemanticIntentSha256: sha("5"),
      parentProbeReportSha256: sha("5"),
      parentProbeResultManifestSha256: sha("6"),
      parentProbeRecordSha256: sha("7"),
      parentExecutionBundleSha256: sha("8"),
      parentRuntimeEnvironmentSha256: sha("9"),
      sourceFrameEvidenceSha256: sha("a"),
      sourceId: "source-1",
      sourceSha256: sha("3"),
      sourceFileIdentitySha256: sha("4"),
      sourceSizeBytes: 10_000,
      sourceWidth: 5120,
      sourceHeight: 1440,
      sourceFrameCount: 100,
      sourceFps: 20,
      lockedProfileId: "official-coco-yolo11s-sahi",
      lockedProfileSha256: sha("4"),
      frameIndices: [10, 20],
      samplingManifestSha256: sha("1"),
      temporalGroupsSha256: sha("e"),
      candidateEvidenceSha256: sha("f"),
      replacementRequestAuthoritySha256: sha("0"),
    },
    progress: {
      stageCompleted: status === "ready" ? 6 : status === "committing" ? 1 : 0,
      stageTotal: 6,
      sourceFramesCompleted: completed,
      sourceFramesTotal: 100,
      updatedAt,
    },
    canCancel: status === "queued" || status === "running",
    canRetry:
      status === "failed" || status === "blocked" || status === "cancelled",
    result:
      status === "ready"
        ? {
            proxy: {
              reviewProxyId: "review-proxy-1",
              reviewProxyManifestSha256: sha("0"),
              proxyMediaSha256: sha("1"),
              proxySizeBytes: 5_000,
              proxyWidth: 2560,
              proxyHeight: 720,
              proxyFrameCount: 100,
              proxyFps: 20,
              mappingSha256: sha("2"),
              sampledArtifactCount: 2,
              encoderBindingSha256: sha("3"),
              repairExecutionBindingSha256: sha("4"),
              repairCodeBundleSha256: sha("5"),
              repairRuntimeSha256: sha("6"),
              repairDecoderFingerprintSha256: sha("7"),
            },
            childProbe: {
              jobId: "probe-proxy-1",
              requestSha256: sha("4"),
              intentSha256: sha("5"),
              semanticIntentSha256: sha("6"),
              resourceSha256: sha("7"),
              frozenProfilesSha256: sha("8"),
              reportSha256: sha("7"),
              resultManifestSha256: sha("8"),
              executionBundleSha256: sha("9"),
              runtimeEnvironmentSha256: sha("a"),
              continuationExecutionBindingSha256: sha("b"),
              continuationCodeBundleSha256: sha("c"),
              continuationRuntimeSha256: sha("d"),
              retryFromJobId: "probe-ready-1",
              retryKind: "review_proxy_decode_upgrade",
              statusUrl: "/api/v1/detector-probes/probe-proxy-1",
              reportUrl: "/api/v1/detector-probes/probe-proxy-1",
            },
            replacementSession: {
              sessionId: "annotation-session-2",
              requestSha256: sha("9"),
              status: "annotating",
              retryFromSessionId: "annotation-session-1",
              retryMode: "review_proxy_decode_upgrade",
              attemptFamilySha256: sha("a"),
              developmentProbeJobIds: ["probe-ready-1", "probe-proxy-1"],
              statusUrl:
                "/api/v1/ball-annotation-sessions/annotation-session-2",
            },
            parentProbeRecordSha256After: sha("7"),
          }
        : null,
    errorCode:
      status === "failed" || status === "blocked"
        ? "review_proxy_failed"
        : null,
    blockerCode: status === "blocked" ? "review_proxy_failed" : null,
    recoveryAction:
      status === "failed" || status === "blocked" ? "retry" : null,
    createdAt: "2026-07-18T20:00:00Z",
    updatedAt,
    statusUrl: `/api/v1/detector-review-proxy-repairs/${repairId}`,
    cancelUrl: `/api/v1/detector-review-proxy-repairs/${repairId}/cancel`,
    retryUrl: `/api/v1/detector-review-proxy-repairs/${repairId}/retry`,
  };
}

function reviewRepairPointer(repairId: string | null = null) {
  return JSON.stringify({
    schema_version: "1.0",
    artifact_type: "ball_annotation_session_pointer",
    state: "session_pointer",
    workflow_id: "workflow-1",
    development_probe_job_ids: ["probe-ready-1"],
    locked_profile_id: "official-coco-yolo11s-sahi",
    session_id: "annotation-session-1",
    data_role: "development",
    review_proxy_repair: {
      repair_id: repairId,
      attempt_root_repair_id: repairId,
      attempt_number: repairId ? 1 : null,
      retry_from_repair_id: null,
      blocked_session_id: "annotation-session-1",
      request_sha256: repairId ? sha("d") : null,
      parent_probe_job_id: "probe-ready-1",
      parent_probe_record_sha256: sha("7"),
      blocked_session_record_sha256: sha("8"),
      child_probe_job_id: null,
      replacement_session_id: null,
    },
  });
}

function memoryStorage(
  initial: Record<string, string> = {},
): SafeBrowserStorage {
  const values = new Map(Object.entries(initial));
  return {
    isPersistent: true,
    unavailableReason: null,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => void values.set(key, value),
    removeItem: (key) => void values.delete(key),
  };
}

function json(body: unknown, status = 200, extraHeaders: HeadersInit = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

function sessionPointer(role: "development" | "check" = "development") {
  return JSON.stringify({
    schema_version: "1.0",
    artifact_type: "ball_annotation_session_pointer",
    state: "session_pointer",
    workflow_id: "workflow-1",
    development_probe_job_ids: ["probe-ready-1"],
    locked_profile_id: "official-coco-yolo11s-sahi",
    session_id: "annotation-session-1",
    data_role: role,
  });
}

function renderController(
  storage = memoryStorage(),
  onStartNewDevelopmentBatch = vi.fn(),
) {
  return render(
    <LanguageProvider>
      <ProductionBallAnnotationController
        workflowId="workflow-1"
        developmentProbeJobIds={["probe-ready-1"]}
        lockedProfileId="official-coco-yolo11s-sahi"
        storage={storage}
        onStartNewDevelopmentBatch={onStartNewDevelopmentBatch}
      />
    </LanguageProvider>,
  );
}

async function fillApplicability(user: ReturnType<typeof userEvent.setup>) {
  for (const stratum of [
    "near",
    "mid",
    "far",
    "bright_sun",
    "shadow",
    "backlight",
    "twilight",
    "artificial_light",
  ]) {
    await user.type(
      screen.getByLabelText(`${stratum} Pre-reveal evidence`),
      `${stratum} exists`,
    );
  }
}

async function flushAsyncWork() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  mocks.fetchFrame.mockImplementation(
    ({ frameIndex }: { frameIndex: number }) =>
      Promise.resolve({
        objectUrl: `blob:frame-${frameIndex}`,
        contentSha256: sha("c"),
        etag: `"${sha("c")}"`,
        contentType: "image/jpeg",
        sizeBytes: 4,
      }),
  );
  mocks.parseSession.mockReturnValue(parsedSession());
  mocks.parseRevision.mockReturnValue({
    sessionId: "annotation-session-1",
    frameIndex: 10,
    revision: 1,
    operation: "set",
    annotationEtag: `"${sha("d")}"`,
  });
  mocks.parseFinal.mockReturnValue({
    packageSha256: sha("e"),
    reportSha256: sha("f"),
    dashboard: null,
  });
  mocks.parsePropagation.mockReturnValue(parsedPropagationJob());
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.removeItem("app-language");
});

describe("ProductionBallAnnotationController", () => {
  it("parses only bounded source-frame interval syntax", () => {
    expect(parseBallAnnotationFrameIntervals("")).toEqual([]);
    expect(parseBallAnnotationFrameIntervals("0-9, 20-29")).toEqual([
      { startFrame: 0, endFrame: 9 },
      { startFrame: 20, endFrame: 29 },
    ]);
    for (const invalid of [
      Array.from({ length: 33 }, () => "0-1").join(","),
      "not-an-interval",
      "10-9",
      "999999999999999999999-999999999999999999999",
    ]) {
      expect(() => parseBallAnnotationFrameIntervals(invalid)).toThrow();
    }
  });

  it("fails closed when a generated response has no transport metadata", () => {
    expect(() =>
      requireBallAnnotationResponseMetadata({ session: true }),
    ).toThrow(/metadata is missing/);
  });

  it("keeps concurrent generated response metadata bound to its own body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const id = String(input).endsWith("session-one") ? "one" : "two";
        await new Promise((resolve) =>
          window.setTimeout(resolve, id === "one" ? 5 : 0),
        );
        return json({ session_id: id }, 200, { "X-Transport-Identity": id });
      }),
    );

    const [one, two] = await Promise.all([
      getBallAnnotationSession("session-one"),
      getBallAnnotationSession("session-two"),
    ]);

    expect(
      requireBallAnnotationResponseMetadata(one).headers.get(
        "X-Transport-Identity",
      ),
    ).toBe("one");
    expect(
      requireBallAnnotationResponseMetadata(two).headers.get(
        "X-Transport-Identity",
      ),
    ).toBe("two");
  });

  it("renders the localized setup contract", () => {
    window.localStorage.setItem("app-language", "zh");
    renderController();
    expect(screen.getByText("创建标注会话")).toBeVisible();
    expect(screen.getByLabelText("操作者标识")).toHaveValue("local-operator");
    expect(
      screen.getByTestId("ball-annotation-setup-governance"),
    ).toHaveTextContent(
      "One person may annotate development data and make local trial decisions, but their own work is not an independent production audit. / 一个人可以标注开发数据并作出本地试跑决定，但其本人完成的工作不构成独立生产审计。",
    );
  });

  it("recovers only an exact workflow-bound session pointer", () => {
    const key = "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1";
    const pointer = {
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      session_id: "annotation-session-1",
      data_role: "development",
    };
    expect(
      recoverBallAnnotationLaunch(memoryStorage(), "workflow-1", [
        "probe-ready-1",
      ]),
    ).toBeNull();
    expect(
      recoverBallAnnotationLaunch(
        memoryStorage({ [key]: JSON.stringify(pointer) }),
        "workflow-1",
        ["probe-ready-1"],
      ),
    ).toEqual({
      lockedProfileId: "official-coco-yolo11s-sahi",
      developmentProbeJobIds: ["probe-ready-1"],
    });
    const repairedPointer = {
      ...pointer,
      development_probe_job_ids: ["probe-ready-1", "probe-proxy-1"],
      session_id: "annotation-session-2",
      review_proxy_repair: {
        repair_id: "repair-1",
        attempt_root_repair_id: "repair-1",
        attempt_number: 1,
        retry_from_repair_id: null,
        blocked_session_id: "annotation-session-1",
        request_sha256: sha("d"),
        parent_probe_job_id: "probe-ready-1",
        parent_probe_record_sha256: sha("7"),
        blocked_session_record_sha256: sha("8"),
        child_probe_job_id: "probe-proxy-1",
        replacement_session_id: "annotation-session-2",
      },
    };
    expect(
      recoverBallAnnotationLaunch(
        memoryStorage({ [key]: JSON.stringify(repairedPointer) }),
        "workflow-1",
        ["probe-ready-1"],
      ),
    ).toEqual({
      lockedProfileId: "official-coco-yolo11s-sahi",
      developmentProbeJobIds: ["probe-ready-1", "probe-proxy-1"],
    });

    for (const invalid of [
      "not-json",
      JSON.stringify([]),
      JSON.stringify({ ...pointer, workflow_id: "workflow-2" }),
      JSON.stringify({ ...pointer, development_probe_job_ids: "not-an-array" }),
      JSON.stringify({ ...pointer, development_probe_job_ids: ["../bad"] }),
      JSON.stringify({ ...pointer, session_id: "../bad" }),
      JSON.stringify({ ...pointer, unexpected: true }),
      JSON.stringify({ ...pointer, propagation_job: null }),
      JSON.stringify({
        ...pointer,
        propagation_job: { job_id: "../bad", request: {} },
      }),
      JSON.stringify({
        ...pointer,
        propagation_job: {
          job_id: null,
          request: {
            mutation_id: "propagation-mutation-1",
            seed_frame_index: 10,
            expected_seed_revision: 1,
            radius_frames: 3,
          },
        },
      }),
      JSON.stringify({
        ...pointer,
        state: "pending_create",
        artifact_type: "ball_annotation_pending_create",
      }),
      JSON.stringify({
        ...repairedPointer,
        review_proxy_repair: {
          ...repairedPointer.review_proxy_repair,
          parent_probe_record_sha256: sha("A"),
        },
      }),
      JSON.stringify({
        ...repairedPointer,
        review_proxy_repair: {
          ...repairedPointer.review_proxy_repair,
          attempt_root_repair_id: "repair-other",
        },
      }),
      JSON.stringify({
        ...repairedPointer,
        review_proxy_repair: {
          ...repairedPointer.review_proxy_repair,
          attempt_number: 2,
        },
      }),
      JSON.stringify({
        ...repairedPointer,
        development_probe_job_ids: ["probe-proxy-1", "probe-ready-1"],
      }),
      JSON.stringify({
        ...repairedPointer,
        review_proxy_repair: {
          ...repairedPointer.review_proxy_repair,
          child_probe_job_id: "forged-child",
        },
      }),
      JSON.stringify({
        ...repairedPointer,
        review_proxy_repair: {
          ...repairedPointer.review_proxy_repair,
          replacement_session_id: null,
        },
      }),
      JSON.stringify({
        ...repairedPointer,
        session_id: "forged-session",
      }),
    ]) {
      expect(() =>
        recoverBallAnnotationLaunch(
          memoryStorage({ [key]: invalid }),
          "workflow-1",
          ["probe-ready-1"],
        ),
      ).toThrow();
    }
  });

  it("shows and discards a corrupted recovery record", async () => {
    const key = "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1";
    const storage = memoryStorage({ [key]: "not-json" });
    const user = userEvent.setup();
    renderController(storage);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Invalid recovery record",
    );
    await user.click(
      screen.getByRole("button", { name: "Discard invalid recovery" }),
    );
    expect(storage.getItem(key)).toBeNull();
    expect(screen.getByTestId("ball-annotation-setup")).toBeVisible();
  });

  it("replays the exact pending create and surfaces bounded server errors", async () => {
    const request = {
      data_role: "development",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      target_frame_count: null,
      sampling_profile_id: "tiny_ball_temporal_groups_v1",
      metric_profile_id: "tiny_ball_feasibility_metric_v1",
      operator_id: "local-operator",
      strata_applicability: {
        scale: ["near", "mid", "far"].map((stratum) => ({
          stratum,
          status: "applicable",
          evidence_note: `${stratum} exists`,
        })),
        lighting: [
          "bright_sun",
          "shadow",
          "backlight",
          "twilight",
          "artificial_light",
        ].map((stratum) => ({
          stratum,
          status: "applicable",
          evidence_note: `${stratum} exists`,
          quota: 0,
          frame_intervals: [],
        })),
      },
      retry_from_session_id: null,
      development_package_session_id: null,
      development_package_sha256: null,
    };
    const pending = JSON.stringify({
      schema_version: "1.0",
      artifact_type: "ball_annotation_pending_create",
      state: "pending_create",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      request,
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: "create denied" }, 400))
      .mockResolvedValueOnce(json({ detail: { message: "still denied" } }, 409))
      .mockResolvedValueOnce(
        new Response("not-json", {
          status: 500,
          headers: { "Cache-Control": "no-store" },
        }),
      )
      .mockResolvedValueOnce(json({}, 400))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ session: true }), {
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(json({ session: true }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          pending,
      }),
    );
    const resume = screen.getByRole("button", {
      name: "Resume exact pending create",
    });
    await user.click(resume);
    expect(await screen.findByRole("alert")).toHaveTextContent("create denied");
    await user.click(resume);
    expect(await screen.findByRole("alert")).toHaveTextContent("still denied");
    await user.click(resume);
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 500");
    await user.click(resume);
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 400");
    await user.click(resume);
    expect(await screen.findByRole("alert")).toHaveTextContent("no-store");
    await user.click(resume);
    expect(await screen.findByTestId("annotation-panel")).toBeVisible();
    for (const call of fetchMock.mock.calls) {
      expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual(
        request,
      );
    }
  });

  it("keeps recovery visible when the saved session cannot refresh", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue("network offline"));
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "network offline",
    );
    expect(screen.getByTestId("ball-annotation-setup")).toBeVisible();
  });

  it("persists repair intent, polls monotonically, and enters only the server-issued replacement session", async () => {
    vi.useFakeTimers();
    try {
      const blocked = blockedReviewProxySession();
      const replacement = replacementReviewProxySession();
      mocks.parseSession
        .mockReturnValueOnce(blocked)
        .mockReturnValueOnce(replacement);
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValueOnce(json({ blocked: true }))
          .mockResolvedValueOnce(json({ replacement: true })),
      );
      const storage = memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      });
      mocks.createRepair.mockImplementation(async () => {
        const persisted = JSON.parse(
          storage.getItem(
            "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
          )!,
        );
        expect(persisted.review_proxy_repair).toEqual(
          expect.objectContaining({
            repair_id: null,
            blocked_session_id: "annotation-session-1",
            request_sha256: null,
          }),
        );
        return reviewProxyRepairJob("running");
      });
      mocks.getRepair.mockResolvedValue(reviewProxyRepairJob("ready"));
      renderController(storage);
      await flushAsyncWork();

      fireEvent.click(
        screen.getByRole("button", {
          name: "start review proxy repair hook",
        }),
      );
      await flushAsyncWork();
      expect(screen.getByText("repair running 25")).toBeVisible();
      expect(mocks.createRepair).toHaveBeenCalledWith(
        "annotation-session-1",
        expect.any(AbortSignal),
      );

      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();

      expect(mocks.getRepair).toHaveBeenCalledWith(
        "repair-1",
        expect.any(AbortSignal),
      );
      expect(screen.getByText("repair ready 100")).toBeVisible();
      expect(screen.getByText("frame 10")).toBeVisible();
      expect(mocks.parseSession).toHaveBeenLastCalledWith(expect.anything(), {
        dataRole: "development",
        developmentProbeJobIds: ["probe-ready-1", "probe-proxy-1"],
        lockedProfileId: "official-coco-yolo11s-sahi",
      });
      expect(
        vi
          .mocked(fetch)
          .mock.calls.filter(
            ([, init]) => (init as RequestInit | undefined)?.method === "POST",
          ),
      ).toHaveLength(0);
      const persisted = JSON.parse(
        storage.getItem(
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
        )!,
      );
      expect(persisted).toEqual(
        expect.objectContaining({
          session_id: "annotation-session-2",
          development_probe_job_ids: ["probe-ready-1", "probe-proxy-1"],
          review_proxy_repair: expect.objectContaining({
            repair_id: "repair-1",
            child_probe_job_id: "probe-proxy-1",
            replacement_session_id: "annotation-session-2",
          }),
        }),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("switches durable recovery to the exact retry attempt before polling its replacement", async () => {
    vi.useFakeTimers();
    try {
      mocks.parseSession
        .mockReturnValueOnce(blockedReviewProxySession())
        .mockReturnValueOnce(replacementReviewProxySession());
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValueOnce(json({ blocked: true }))
          .mockResolvedValueOnce(json({ replacement: true })),
      );
      const storage = memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      });
      const failed = reviewProxyRepairJob("failed");
      const retried = reviewProxyRepairJob("queued", {
        repairId: "repair-2",
        attemptRootRepairId: "repair-1",
        attemptNumber: 2,
        retryFromRepairId: "repair-1",
        requestSha256: sha("e"),
        completed: 0,
        updatedAt: "2026-07-18T20:00:02Z",
      });
      const ready = reviewProxyRepairJob("ready", {
        repairId: "repair-2",
        attemptRootRepairId: "repair-1",
        attemptNumber: 2,
        retryFromRepairId: "repair-1",
        requestSha256: sha("e"),
        updatedAt: "2026-07-18T20:00:03Z",
      });
      mocks.createRepair.mockResolvedValue(failed);
      mocks.retryRepair.mockResolvedValue(retried);
      mocks.getRepair.mockResolvedValue(ready);
      renderController(storage);
      await flushAsyncWork();

      fireEvent.click(
        screen.getByRole("button", { name: "start review proxy repair hook" }),
      );
      await flushAsyncWork();
      expect(screen.getByText("repair failed 25")).toBeVisible();
      fireEvent.click(
        screen.getByRole("button", { name: "retry review proxy repair hook" }),
      );
      await flushAsyncWork();

      expect(mocks.retryRepair).toHaveBeenCalledWith(
        "repair-1",
        expect.any(AbortSignal),
      );
      expect(
        JSON.parse(
          storage.getItem(
            "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
          )!,
        ).review_proxy_repair,
      ).toEqual(
        expect.objectContaining({
          repair_id: "repair-2",
          attempt_root_repair_id: "repair-1",
          attempt_number: 2,
          retry_from_repair_id: "repair-1",
          request_sha256: sha("e"),
        }),
      );

      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();
      expect(mocks.getRepair).toHaveBeenCalledWith(
        "repair-2",
        expect.any(AbortSignal),
      );
      expect(screen.getByText("repair ready 100")).toBeVisible();
      expect(screen.getByText("frame 10")).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("recovers an exact persisted retry attempt after refresh", async () => {
    const pointer = JSON.parse(reviewRepairPointer("repair-1"));
    pointer.review_proxy_repair = {
      ...pointer.review_proxy_repair,
      repair_id: "repair-2",
      attempt_root_repair_id: "repair-1",
      attempt_number: 2,
      retry_from_repair_id: "repair-1",
      request_sha256: sha("e"),
    };
    mocks.parseSession
      .mockReturnValueOnce(blockedReviewProxySession())
      .mockReturnValueOnce(replacementReviewProxySession());
    mocks.getRepair.mockResolvedValue(
      reviewProxyRepairJob("ready", {
        repairId: "repair-2",
        attemptRootRepairId: "repair-1",
        attemptNumber: 2,
        retryFromRepairId: "repair-1",
        requestSha256: sha("e"),
      }),
    );
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ blocked: true }))
        .mockResolvedValueOnce(json({ replacement: true })),
    );

    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          JSON.stringify(pointer),
      }),
    );

    await waitFor(() =>
      expect(mocks.getRepair).toHaveBeenCalledWith(
        "repair-2",
        expect.any(AbortSignal),
      ),
    );
    expect(await screen.findByText("repair ready 100")).toBeVisible();
    expect(screen.getByText("frame 10")).toBeVisible();
  });

  it("replays a persisted pre-response repair create without inventing new authority", async () => {
    mocks.parseSession.mockReturnValue(blockedReviewProxySession());
    mocks.createRepair.mockResolvedValue(reviewProxyRepairJob("queued"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ blocked: true })));
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        reviewRepairPointer(),
    });
    const rendered = renderController(storage);

    await screen.findByTestId("annotation-panel");
    await waitFor(() => expect(mocks.createRepair).toHaveBeenCalledTimes(1));
    expect(mocks.createRepair).toHaveBeenCalledWith(
      "annotation-session-1",
      expect.any(AbortSignal),
    );
    expect(
      JSON.parse(
        storage.getItem(
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
        )!,
      ).review_proxy_repair,
    ).toEqual(
      expect.objectContaining({
        repair_id: "repair-1",
        request_sha256: sha("d"),
      }),
    );
    rendered.unmount();
  });

  it("does not let an older active job erase a durably recovered ready continuation", async () => {
    const pointer = JSON.parse(reviewRepairPointer("repair-1"));
    pointer.development_probe_job_ids = ["probe-ready-1", "probe-proxy-1"];
    pointer.session_id = "annotation-session-2";
    pointer.review_proxy_repair.child_probe_job_id = "probe-proxy-1";
    pointer.review_proxy_repair.replacement_session_id = "annotation-session-2";
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        JSON.stringify(pointer),
    });
    mocks.parseSession.mockReturnValue(replacementReviewProxySession());
    mocks.getRepair.mockResolvedValue(reviewProxyRepairJob("running"));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ replacement: true })),
    );

    renderController(storage);

    expect(
      await screen.findByText(
        "Review-proxy repair authority does not match recovery.",
      ),
    ).toBeVisible();
    expect(
      JSON.parse(
        storage.getItem(
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
        )!,
      ).review_proxy_repair,
    ).toEqual(
      expect.objectContaining({
        child_probe_job_id: "probe-proxy-1",
        replacement_session_id: "annotation-session-2",
      }),
    );
  });

  it("prevents a slow poll from overwriting a newer cancellation", async () => {
    vi.useFakeTimers();
    try {
      let resolvePoll!: (job: ReviewProxyRepairJobView) => void;
      const slowPoll = new Promise<ReviewProxyRepairJobView>((resolve) => {
        resolvePoll = resolve;
      });
      let resolveCancel!: (job: ReviewProxyRepairJobView) => void;
      const slowCancel = new Promise<ReviewProxyRepairJobView>((resolve) => {
        resolveCancel = resolve;
      });
      mocks.parseSession.mockReturnValue(blockedReviewProxySession());
      mocks.createRepair.mockResolvedValue(
        reviewProxyRepairJob("running", {
          completed: 25,
          updatedAt: "2026-07-18T20:00:01Z",
        }),
      );
      mocks.getRepair.mockReturnValue(slowPoll);
      mocks.cancelRepair.mockReturnValue(slowCancel);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(json({ blocked: true })),
      );
      renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer(),
        }),
      );
      await flushAsyncWork();
      fireEvent.click(
        screen.getByRole("button", {
          name: "start review proxy repair hook",
        }),
      );
      await flushAsyncWork();
      await act(async () => {
        vi.advanceTimersByTime(1_000);
      });
      expect(mocks.getRepair).toHaveBeenCalledTimes(1);

      fireEvent.click(
        screen.getByRole("button", {
          name: "cancel review proxy repair hook",
        }),
      );
      resolvePoll(
        reviewProxyRepairJob("running", {
          completed: 50,
          updatedAt: "2026-07-18T20:00:02Z",
        }),
      );
      await flushAsyncWork();
      act(() => vi.advanceTimersByTime(5_000));
      await flushAsyncWork();
      expect(mocks.getRepair).toHaveBeenCalledTimes(1);
      expect(screen.getByText("repair running 25")).toBeVisible();

      resolveCancel(
        reviewProxyRepairJob("cancelled", {
          completed: 25,
          updatedAt: "2026-07-18T20:00:03Z",
        }),
      );
      await flushAsyncWork();
      expect(screen.getByText("repair cancelled 25")).toBeVisible();
      expect(screen.queryByText("repair running 50")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores a regressive queued poll and keeps the accepted running state", async () => {
    vi.useFakeTimers();
    try {
      mocks.parseSession.mockReturnValue(blockedReviewProxySession());
      mocks.createRepair.mockResolvedValue(
        reviewProxyRepairJob("running", {
          completed: 25,
          updatedAt: "2026-07-18T20:00:02Z",
        }),
      );
      mocks.getRepair.mockResolvedValue(
        reviewProxyRepairJob("queued", {
          completed: 0,
          updatedAt: "2026-07-18T20:00:01Z",
        }),
      );
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(json({ blocked: true })),
      );
      renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer(),
        }),
      );
      await flushAsyncWork();
      fireEvent.click(
        screen.getByRole("button", {
          name: "start review proxy repair hook",
        }),
      );
      await flushAsyncWork();
      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();
      expect(mocks.getRepair).toHaveBeenCalledTimes(1);
      expect(screen.getByText("repair running 25")).toBeVisible();
      expect(screen.queryByText("repair queued 0")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reloads a poll transport failure without creating a server retry", async () => {
    vi.useFakeTimers();
    try {
      mocks.parseSession.mockReturnValue(blockedReviewProxySession());
      mocks.createRepair.mockResolvedValue(
        reviewProxyRepairJob("running", {
          completed: 25,
          updatedAt: "2026-07-18T20:00:01Z",
        }),
      );
      mocks.getRepair
        .mockRejectedValueOnce(new Error("poll transport offline"))
        .mockResolvedValueOnce(
          reviewProxyRepairJob("running", {
            completed: 50,
            updatedAt: "2026-07-18T20:00:02Z",
          }),
        );
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(json({ blocked: true })),
      );
      renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer(),
        }),
      );
      await flushAsyncWork();
      fireEvent.click(
        screen.getByRole("button", { name: "start review proxy repair hook" }),
      );
      await flushAsyncWork();
      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "poll transport offline",
      );

      fireEvent.click(
        screen.getByRole("button", { name: "reload review proxy repair hook" }),
      );
      await flushAsyncWork();
      expect(mocks.getRepair).toHaveBeenCalledTimes(2);
      expect(mocks.retryRepair).not.toHaveBeenCalled();
      expect(screen.getByText("repair running 50")).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("creates from declared applicability, uses verified frames, and revokes object URLs", async () => {
    const user = userEvent.setup();
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ accepted: true }, 202)),
    );
    const rendered = renderController();
    await fillApplicability(user);
    await user.click(
      screen.getByRole("button", { name: "Start development annotation" }),
    );

    expect(await screen.findByTestId("annotation-panel")).toBeVisible();
    expect(await screen.findByText("image blob:frame-10")).toBeVisible();
    expect(
      screen.getByText(`image identity annotation-session-1:10:${sha("7")}`),
    ).toBeVisible();
    expect(screen.getByText("propagation true")).toBeVisible();
    const createCall = vi.mocked(fetch).mock.calls[0];
    expect(createCall[0]).toBe("/api/ball-annotation-sessions");
    expect(JSON.parse((createCall[1] as RequestInit).body as string)).toEqual(
      expect.objectContaining({
        data_role: "development",
        target_frame_count: null,
      }),
    );

    let resolveNextFrame!: (value: {
      objectUrl: string;
      contentSha256: string;
      etag: string;
      contentType: "image/jpeg";
      sizeBytes: number;
    }) => void;
    mocks.fetchFrame.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveNextFrame = resolve;
        }),
    );
    await user.click(screen.getByRole("button", { name: "next test frame" }));
    expect(await screen.findByText("image loading")).toBeVisible();
    expect(screen.getByText("image identity none")).toBeVisible();
    act(() =>
      resolveNextFrame({
        objectUrl: "blob:frame-20",
        contentSha256: sha("8"),
        etag: `"${sha("8")}"`,
        contentType: "image/jpeg",
        sizeBytes: 4,
      }),
    );
    expect(await screen.findByText("image blob:frame-20")).toBeVisible();
    expect(
      screen.getByText(`image identity annotation-session-1:20:${sha("8")}`),
    ).toBeVisible();
    expect(revoke).toHaveBeenCalledWith("blob:frame-10");
    rendered.unmount();
    expect(revoke).toHaveBeenCalledWith("blob:frame-20");
  });

  it("does not POST when durable pending-create storage is unavailable", async () => {
    const user = userEvent.setup();
    const storage: SafeBrowserStorage = {
      ...memoryStorage(),
      isPersistent: false,
      unavailableReason: "localStorage denied",
    };
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderController(storage);
    await fillApplicability(user);
    await user.click(
      screen.getByRole("button", { name: "Start development annotation" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Persistent recovery storage is unavailable",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps the exact pending request when pointer persistence fails after POST", async () => {
    const values = new Map<string, string>();
    let writes = 0;
    const storage: SafeBrowserStorage = {
      isPersistent: true,
      unavailableReason: null,
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => {
        writes += 1;
        if (writes === 1) values.set(key, value);
      },
      removeItem: (key) => void values.delete(key),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ accepted: true })));
    const user = userEvent.setup();
    renderController(storage);
    await fillApplicability(user);
    await user.click(
      screen.getByRole("button", { name: "Start development annotation" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Persistent recovery storage is unavailable",
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledOnce();
    const saved = JSON.parse([...values.values()][0]);
    expect(saved).toEqual(
      expect.objectContaining({
        artifact_type: "ball_annotation_pending_create",
        state: "pending_create",
      }),
    );
    expect(
      screen.getByRole("button", { name: "Resume exact pending create" }),
    ).toBeVisible();
  });

  it("sends strong If-Match and refreshes instead of overwriting a 412", async () => {
    const pointer = JSON.stringify({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      session_id: "annotation-session-1",
      data_role: "development",
    });
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1": pointer,
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(
          json({ detail: { code: "stale", message: "stale" } }, 412),
        )
        .mockResolvedValueOnce(json({ session: true })),
    );
    const user = userEvent.setup();
    renderController(storage);
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", { name: "save test annotation" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /latest revision was loaded/i,
    );
    const putCall = vi
      .mocked(fetch)
      .mock.calls.find(([, init]) => (init as RequestInit)?.method === "PUT")!;
    expect(
      new Headers((putCall[1] as RequestInit).headers).get("If-Match"),
    ).toBe(`"${sha("9")}"`);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3);
    expect(mocks.parseRevision).not.toHaveBeenCalled();
  });

  it("fails closed on an unbound mutation response and reloads authoritative state", async () => {
    mocks.parseRevision.mockImplementationOnce(() => {
      throw new Error(
        "Annotation revision does not match the mutation intent.",
      );
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(
          json({ revision: "tampered" }, 200, { ETag: `"${sha("d")}"` }),
        )
        .mockResolvedValueOnce(json({ session: "authoritative" })),
    );
    const user = userEvent.setup();
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");

    await user.click(
      screen.getByRole("button", { name: "save test annotation" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /does not match the mutation intent/i,
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3);
    expect(mocks.parseSession).toHaveBeenCalledTimes(2);
  });

  it("echoes exact detector and propagation authority in accepted and dismissed requests", async () => {
    const authority = parsedSession();
    authority.view.frames[0].suggestedCandidates = [
      {
        candidateId: "candidate-one",
        bbox: { left: 90, top: 90, right: 110, bottom: 110 },
        confidence: 0.8,
        profileId: "official-coco-yolo11s-sahi",
        rank: 1,
        suggestionJobId: "probe-ready-1",
        suggestionSha256: sha("6"),
        decision: "pending",
      },
    ];
    authority.view.frames[0].propagationSuggestions = [
      {
        suggestionId: "suggestion-one",
        jobId: "propagation-job-one",
        suggestionSha256: sha("7"),
        frameIndex: 10,
        temporalGroupId: sha("5"),
        point: [100, 100],
        bbox: { left: 90, top: 90, right: 110, bottom: 110 },
        annotationState: "suggested",
        trainingUse: "excluded",
        provenance: "tiny_ball_bounded_template_flow_v1",
        pendingHumanConfirmation: true,
      },
    ];
    mocks.parseSession.mockReturnValue(authority);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ session: true }))
      .mockResolvedValueOnce(
        json({ revision: true }, 200, { ETag: `"${sha("d")}"` }),
      )
      .mockResolvedValueOnce(json({ session: true }))
      .mockResolvedValueOnce(
        json({ revision: true }, 200, { ETag: `"${sha("e")}"` }),
      )
      .mockResolvedValueOnce(json({ session: true }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", {
        name: "accept exact detector suggestion",
      }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    await user.click(
      screen.getByRole("button", {
        name: "dismiss exact propagation suggestion",
      }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));

    const bodies = fetchMock.mock.calls
      .filter(([, init]) => (init as RequestInit)?.method === "PUT")
      .map(([, init]) => JSON.parse((init as RequestInit).body as string));
    expect(bodies).toEqual([
      expect.objectContaining({
        suggestion_kind: "detector_candidate",
        suggestion_id: "candidate-one",
        accepted_suggestion_job_id: "probe-ready-1",
        accepted_suggestion_sha256: sha("6"),
        dismissed_suggestion_kind: null,
      }),
      expect.objectContaining({
        suggestion_kind: null,
        dismissed_suggestion_kind: "propagation",
        dismissed_suggestion_id: "suggestion-one",
        dismissed_suggestion_job_id: "propagation-job-one",
        dismissed_suggestion_sha256: sha("7"),
      }),
    ]);
    expect(mocks.parseRevision).toHaveBeenNthCalledWith(
      1,
      expect.anything(),
      expect.anything(),
      expect.objectContaining({
        request: expect.objectContaining({
          operation: "set",
          expected_revision: 0,
          annotation: expect.objectContaining({
            point_source_px: { x: 100, y: 100 },
          }),
        }),
        suggestionDecision: expect.objectContaining({
          id: "candidate-one",
          jobId: "probe-ready-1",
          sha256: sha("6"),
        }),
      }),
    );
  });

  it("reloads a retained server-decided candidate and can continue to finalization", async () => {
    const actual = await vi.importActual<
      typeof import("@/lib/productionBallAnnotation")
    >("@/lib/productionBallAnnotation");
    const raw = ballAnnotationSessionFixture({
      profileId: "official-coco-yolo11s-sahi",
      jobId: "probe-ready-1",
      frameIndices: [10],
      sourceWidth: 5_120,
      sourceHeight: 1_440,
    });
    raw.frames[0].suggested_candidates = [
      {
        candidate_id: "candidate-one",
        profile_id: "official-coco-yolo11s-sahi",
        rank: 1,
        bbox_source_px: { left: 90, top: 90, right: 110, bottom: 110 },
        confidence: 0.8,
        annotation_state: "suggested",
        training_use: "excluded",
        truth_status: "unconfirmed_suggestion",
        suggestion_job_id: "probe-ready-1",
        suggestion_sha256: sha("6"),
        decision: "pending",
      },
    ];
    refreshBallAnnotationProgress(raw);
    mocks.parseSession.mockImplementation(actual.parseBallAnnotationSession);
    const methods: string[] = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        const url = String(input);
        methods.push(method);
        if (method === "PUT") {
          const body = JSON.parse(String(init?.body));
          expect(body).toMatchObject({
            suggestion_kind: "detector_candidate",
            suggestion_id: "candidate-one",
            accepted_suggestion_job_id: "probe-ready-1",
            accepted_suggestion_sha256: sha("6"),
          });
          raw.frames[0].annotation_revision = 1;
          raw.frames[0].annotation_etag = sha("d");
          raw.frames[0].current_annotation = body.annotation;
          raw.frames[0].suggested_candidates[0].decision = "accepted";
          refreshBallAnnotationProgress(raw);
          return json({ revision: true }, 200, { ETag: `"${sha("d")}"` });
        }
        if (method === "POST" && url.endsWith("/finalize")) {
          return json({ finalized: true });
        }
        return json(raw);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        sessionPointer(),
    });
    const user = userEvent.setup();
    const first = renderController(storage);

    expect(await screen.findByText("candidate decision pending")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "accept exact detector suggestion" }),
    );
    expect(
      await screen.findByText("candidate decision accepted"),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: "accept exact detector suggestion",
      }),
    ).not.toBeInTheDocument();
    expect(raw.progress.unconfirmed_suggestions).toBe(0);
    expect(raw.frames[0].suggested_candidates).toHaveLength(1);

    first.unmount();
    renderController(storage);
    expect(
      await screen.findByText("candidate decision accepted"),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "finalize test session" }),
    );
    await waitFor(() => expect(methods).toContain("POST"));
  });

  it("does not send a revision when suggestion authority is missing or tampered", async () => {
    const authority = parsedSession();
    authority.view.frames[0].suggestedCandidates = [
      {
        candidateId: "candidate-one",
        bbox: { left: 90, top: 90, right: 110, bottom: 110 },
        confidence: 0.8,
        profileId: "official-coco-yolo11s-sahi",
        rank: 1,
        suggestionJobId: "probe-ready-1",
        suggestionSha256: sha("6"),
        decision: "pending",
      },
    ];
    mocks.parseSession.mockReturnValue(authority);
    const fetchMock = vi.fn().mockResolvedValue(json({ session: true }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", {
        name: "accept tampered detector suggestion",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /evidence changed or is incomplete/i,
    );
    await user.click(
      screen.getByRole("button", {
        name: "accept incomplete detector suggestion",
      }),
    );
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(mocks.parseRevision).not.toHaveBeenCalled();
  });

  it("does not refetch or revoke the same verified JPEG after a session refresh", async () => {
    const pointer = JSON.stringify({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      session_id: "annotation-session-1",
      data_role: "development",
    });
    mocks.parseSession.mockImplementation(() => parsedSession());
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(
          json({ revision: true }, 200, { ETag: `"${sha("d")}"` }),
        )
        .mockResolvedValueOnce(json({ session: true })),
    );
    const user = userEvent.setup();
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1": pointer,
    });
    renderController(storage);
    expect(await screen.findByText("image blob:frame-10")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "save test annotation" }),
    );
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3));
    expect(mocks.fetchFrame).toHaveBeenCalledOnce();
    expect(revoke).not.toHaveBeenCalled();
  });

  it("starts, polls, and settles a recoverable propagation job", async () => {
    const authority = parsedSession();
    authority.view.frames[0].annotationRevision = 1;
    authority.view.frames[0].currentAnnotation = {
      point: [100, 100],
      bbox: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      trainingUse: "positive",
      annotationState: "confirmed",
      scaleStratum: "far",
      lightingTag: "bright_sun",
      motionOcclusionTags: [],
      provenance: "manual_human_annotation",
    };
    mocks.parseSession.mockReturnValue(authority);
    mocks.parsePropagation
      .mockReturnValueOnce(parsedPropagationJob("queued"))
      .mockReturnValueOnce(parsedPropagationJob("ready"));
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ session: true }))
      .mockResolvedValueOnce(json({ propagation: "queued" }))
      .mockResolvedValueOnce(json({ propagation: "ready" }))
      .mockResolvedValueOnce(json({ session: true }));
    vi.stubGlobal("fetch", fetchMock);
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        sessionPointer(),
    });
    const user = userEvent.setup();
    renderController(storage);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await user.click(
      screen.getByRole("button", {
        name: "start hidden propagation hook",
      }),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const create = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/propagation-jobs") &&
        (init as RequestInit)?.method === "POST",
    )!;
    expect(JSON.parse((create[1] as RequestInit).body as string)).toEqual(
      expect.objectContaining({
        seed_frame_index: 10,
        expected_seed_revision: 1,
        radius_frames: 2,
      }),
    );
    expect(
      new Headers((create[1] as RequestInit).headers).get("If-Match"),
    ).toBe(`"${sha("9")}"`);
    expect(
      storage.getItem(
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
      ),
    ).toContain('"job_id":"propagation-job-1"');

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4), {
      timeout: 3_000,
    });
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/propagation-jobs/propagation-job-1"),
      ),
    ).toBe(true);
    expect(
      storage.getItem(
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
      ),
    ).not.toContain("propagation_job");
  });

  it("replays a durable pending propagation create and can cancel an active job", async () => {
    const authority = parsedSession();
    authority.view.frames[0].annotationRevision = 1;
    authority.view.frames[0].currentAnnotation = {
      point: [100, 100],
      bbox: { left: 90, top: 90, right: 110, bottom: 110 },
      presence: "present",
      visibility: "visible",
      trainingUse: "positive",
      annotationState: "confirmed",
      scaleStratum: "far",
      lightingTag: "bright_sun",
      motionOcclusionTags: [],
      provenance: "manual_human_annotation",
    };
    mocks.parseSession.mockReturnValue(authority);
    mocks.parsePropagation
      .mockReturnValueOnce(parsedPropagationJob("queued"))
      .mockReturnValueOnce(parsedPropagationJob("cancelled"));
    const pendingPointer = JSON.stringify({
      ...JSON.parse(sessionPointer()),
      propagation_job: {
        job_id: null,
        request: {
          mutation_id: "propagation-mutation-1",
          seed_frame_index: 10,
          radius_frames: 2,
          expected_seed_revision: 1,
        },
      },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ session: true }))
      .mockResolvedValueOnce(json({ propagation: "queued" }))
      .mockResolvedValueOnce(json({ propagation: "cancelled" }))
      .mockResolvedValueOnce(json({ session: true }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        pendingPointer,
    });
    renderController(storage);
    await waitFor(() =>
      expect(
        storage.getItem(
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
        ),
      ).toContain('"job_id":"propagation-job-1"'),
    );
    const replay = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/propagation-jobs") &&
        (init as RequestInit)?.method === "POST",
    )!;
    expect(replay[0]).toBe(
      "/api/ball-annotation-sessions/annotation-session-1/propagation-jobs",
    );
    expect(JSON.parse((replay[1] as RequestInit).body as string)).toEqual({
      mutation_id: "propagation-mutation-1",
      seed_frame_index: 10,
      radius_frames: 2,
      expected_seed_revision: 1,
    });
    await user.click(
      screen.getByRole("button", {
        name: "cancel hidden propagation hook",
      }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/propagation-job-1/cancel"),
        ),
      ).toBe(true),
    );
  });

  it("fails closed when verified frame loading fails", async () => {
    mocks.fetchFrame.mockRejectedValueOnce(new Error("frame digest mismatch"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ session: true })));
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
        sessionPointer(),
    });
    renderController(storage);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "frame digest mismatch",
    );
    expect(screen.getByText("image failed")).toBeVisible();
  });

  it("revokes a verified frame that resolves after unmount", async () => {
    let resolveFrame!: (value: {
      objectUrl: string;
      contentSha256: string;
      etag: string;
      contentType: "image/jpeg";
      sizeBytes: number;
    }) => void;
    mocks.fetchFrame.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFrame = resolve;
      }),
    );
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ session: true })));
    const rendered = renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");
    rendered.unmount();
    resolveFrame({
      objectUrl: "blob:late-frame",
      contentSha256: sha("c"),
      etag: `"${sha("c")}"`,
      contentType: "image/jpeg",
      sizeBytes: 4,
    });
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:late-frame"));
  });

  it("polls only an active server-managed session and clears the timer", async () => {
    vi.useFakeTimers();
    try {
      mocks.parseSession.mockReturnValue(
        parsedSession("check", "sampling_locked"),
      );
      const fetchMock = vi.fn().mockResolvedValue(json({ session: true }));
      vi.stubGlobal("fetch", fetchMock);
      const rendered = renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer("check"),
        }),
      );
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fetchMock).toHaveBeenCalledOnce();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      rendered.unmount();
      await vi.advanceTimersByTimeAsync(2_000);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not let an older overlapping poll success or failure regress newer authority", async () => {
    vi.useFakeTimers();
    try {
      let resolveOlder!: (response: Response) => void;
      const olderResponse = new Promise<Response>((resolve) => {
        resolveOlder = resolve;
      });
      let rejectOlder!: (error: Error) => void;
      const olderFailure = new Promise<Response>((_resolve, reject) => {
        rejectOlder = reject;
      });
      const initial = parsedSession("check", "sampling_locked");
      initial.view.stage = "initial-authority";
      const older = parsedSession("check", "sampling_locked");
      older.view.stage = "older-authority";
      const newer = parsedSession("check", "sampling_locked");
      newer.view.stage = "newer-authority";
      const newest = parsedSession("check", "sampling_locked");
      newest.view.stage = "newest-authority";
      mocks.parseSession.mockImplementation((value: any) => {
        if (value.marker === "older") return older;
        if (value.marker === "newer") return newer;
        if (value.marker === "newest") return newest;
        return initial;
      });
      let sessionReads = 0;
      const fetchMock = vi.fn(
        async (_input: RequestInfo | URL, init?: RequestInit) => {
          if ((init?.method ?? "GET") === "PUT") {
            return json({ revision: true }, 200, { ETag: `"${sha("d")}"` });
          }
          sessionReads += 1;
          if (sessionReads === 1) return json({ marker: "initial" });
          if (sessionReads === 2) return olderResponse;
          if (sessionReads === 3) return json({ marker: "newer" });
          if (sessionReads === 4) return olderFailure;
          return json({ marker: "newest" });
        },
      );
      vi.stubGlobal("fetch", fetchMock);
      const rendered = renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer("check"),
        }),
      );
      await flushAsyncWork();
      expect(screen.getByText("session stage initial-authority")).toBeVisible();

      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();
      expect(sessionReads).toBe(2);

      fireEvent.click(
        screen.getByRole("button", { name: "save test annotation" }),
      );
      await flushAsyncWork();
      await flushAsyncWork();
      expect(screen.getByText("session stage newer-authority")).toBeVisible();

      resolveOlder(json({ marker: "older" }));
      await flushAsyncWork();
      expect(screen.getByText("session stage newer-authority")).toBeVisible();
      expect(
        screen.queryByText("session stage older-authority"),
      ).not.toBeInTheDocument();

      act(() => vi.advanceTimersByTime(1_000));
      await flushAsyncWork();
      expect(sessionReads).toBe(4);
      fireEvent.click(
        screen.getByRole("button", { name: "save test annotation" }),
      );
      await flushAsyncWork();
      await flushAsyncWork();
      expect(screen.getByText("session stage newest-authority")).toBeVisible();

      rejectOlder(new Error("stale poll transport failure"));
      await flushAsyncWork();
      expect(screen.getByText("session stage newest-authority")).toBeVisible();
      expect(
        screen.queryByText("stale poll transport failure"),
      ).not.toBeInTheDocument();
      rendered.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it("routes delete and saved-undo through separate revision mutations", async () => {
    const afterDelete = parsedSession();
    afterDelete.view.frames[0].annotationRevision = 1;
    afterDelete.view.frames[0].annotationEtag = `"${sha("d")}"`;
    mocks.parseSession
      .mockReturnValueOnce(parsedSession())
      .mockReturnValue(afterDelete);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(
          json({ revision: true }, 200, { ETag: `"${sha("d")}"` }),
        )
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(
          json({ revision: true }, 200, { ETag: `"${sha("e")}"` }),
        )
        .mockResolvedValueOnce(json({ session: true })),
    );
    const user = userEvent.setup();
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", { name: "delete saved annotation" }),
    );
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3));
    await user.click(
      screen.getByRole("button", { name: "undo saved annotation" }),
    );
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(5));

    const mutationBodies = vi
      .mocked(fetch)
      .mock.calls.filter(([, init]) => (init as RequestInit)?.method === "PUT")
      .map(([, init]) => JSON.parse((init as RequestInit).body as string));
    expect(mutationBodies).toEqual([
      expect.objectContaining({ operation: "delete", undo_revision: null }),
      expect.objectContaining({ operation: "undo", undo_revision: 1 }),
    ]);
    expect(mocks.parseRevision).toHaveBeenCalledTimes(2);
  });

  it("renders the parsed feasibility dashboard after finalization", async () => {
    const pointer = JSON.stringify({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      session_id: "annotation-session-1",
      data_role: "check",
    });
    const check = parsedSession("check");
    mocks.parseSession.mockReturnValue(check);
    mocks.parseFinal.mockReturnValue({
      packageSha256: sha("e"),
      reportSha256: sha("f"),
      dashboard: { status: "feasibility_failed" },
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(json({ result: true }))
        .mockResolvedValueOnce(json({ session: true })),
    );
    const user = userEvent.setup();
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1": pointer,
    });
    renderController(storage);
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", { name: "finalize test session" }),
    );
    expect(await screen.findByText("strict dashboard rendered")).toBeVisible();
    expect(mocks.parseFinal).toHaveBeenCalledOnce();
  });

  it.each([
    [
      "en",
      "Start a new development evidence batch",
      /adjust the start frame and frame count, then complete a new bounded trial/i,
    ],
    [
      "zh",
      "开始新的开发证据批次",
      /调整起始帧和帧数，然后完成一次新的有限试跑/,
    ],
  ] as const)(
    "offers a %s continuation CTA only after the check is finalized",
    async (language, buttonName, description) => {
      window.localStorage.setItem("app-language", language);
      mocks.parseSession.mockReturnValue(parsedSession("check", "finalized"));
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValueOnce(json({ session: true }))
          .mockResolvedValueOnce(json({ result: true })),
      );
      const onStartNewDevelopmentBatch = vi.fn();
      const storage = memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer("check"),
      });
      const sealedPointer = storage.getItem(
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
      );

      renderController(storage, onStartNewDevelopmentBatch);
      expect(await screen.findByText(description)).toBeVisible();
      await userEvent
        .setup()
        .click(screen.getByRole("button", { name: buttonName }));

      expect(onStartNewDevelopmentBatch).toHaveBeenCalledOnce();
      expect(
        storage.getItem(
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
        ),
      ).toBe(sealedPointer);
    },
  );

  it.each([
    ["development", "finalized"],
    ["check", "annotating"],
  ] as const)(
    "does not offer a new development batch for a %s session in %s status",
    async (role, status) => {
      mocks.parseSession.mockReturnValue(parsedSession(role, status));
      const responses = [json({ session: true })];
      if (status === "finalized") responses.push(json({ result: true }));
      vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation(() => responses.shift()),
      );
      renderController(
        memoryStorage({
          "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
            sessionPointer(role),
        }),
      );

      await screen.findByTestId("annotation-panel");
      expect(
        screen.queryByRole("button", {
          name: "Start a new development evidence batch",
        }),
      ).not.toBeInTheDocument();
    },
  );

  it("surfaces finalize and finalized-result failures without losing the session", async () => {
    const finalizeFetch = vi
      .fn()
      .mockResolvedValueOnce(json({ session: true }))
      .mockResolvedValueOnce(json({ detail: "finalize denied" }, 409));
    vi.stubGlobal("fetch", finalizeFetch);
    const user = userEvent.setup();
    const rendered = renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    await screen.findByTestId("annotation-panel");
    await user.click(
      screen.getByRole("button", { name: "finalize test session" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "finalize denied",
    );
    rendered.unmount();

    mocks.parseSession.mockReturnValue(
      parsedSession("development", "finalized"),
    );
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockRejectedValueOnce(new Error("sealed result unavailable")),
    );
    renderController(
      memoryStorage({
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1":
          sessionPointer(),
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "sealed result unavailable",
    );
  });

  it("moves from finalized development into a separate 20–50 check setup", async () => {
    const finalized = parsedSession("development", "finalized");
    mocks.parseSession
      .mockReturnValueOnce(finalized)
      .mockReturnValueOnce(parsedSession("check"));
    mocks.parseFinal.mockReturnValue({
      packageSha256: sha("e"),
      reportSha256: sha("f"),
      dashboard: null,
    });
    const pointer = JSON.stringify({
      schema_version: "1.0",
      artifact_type: "ball_annotation_session_pointer",
      state: "session_pointer",
      workflow_id: "workflow-1",
      development_probe_job_ids: ["probe-ready-1"],
      locked_profile_id: "official-coco-yolo11s-sahi",
      session_id: "annotation-session-1",
      data_role: "development",
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ session: true }))
        .mockResolvedValueOnce(json({ result: true }))
        .mockResolvedValueOnce(json({ check: true })),
    );
    const user = userEvent.setup();
    const storage = memoryStorage({
      "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1": pointer,
    });
    renderController(storage);
    await user.click(
      await screen.findByRole("button", {
        name: "start hidden propagation hook",
      }),
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Development complete — prepare unseen-frame check",
      }),
    );
    expect(
      screen.getByLabelText("Unseen-frame check size (20–50)"),
    ).toHaveValue(20);
    expect(
      screen.getByRole("button", { name: "Start unseen-frame check" }),
    ).toBeVisible();
    expect(screen.getByLabelText("bright_sun Sampling quota")).toHaveValue(20);
    expect(
      screen.getByLabelText(
        "bright_sun Source-frame intervals (for example 0-999, 1200-1400)",
      ),
    ).toHaveValue("0-99");
    expect(screen.getByLabelText("shadow applicability")).toHaveValue(
      "not_applicable",
    );
    await user.click(
      screen.getAllByRole("button", {
        name: "Use single-light full-video preset",
      })[0],
    );
    await user.clear(screen.getByLabelText("bright_sun Sampling quota"));
    await user.type(screen.getByLabelText("bright_sun Sampling quota"), "19");
    const brightSunIntervals = screen.getByLabelText(
      "bright_sun Source-frame intervals (for example 0-999, 1200-1400)",
    );
    await user.clear(brightSunIntervals);
    await user.type(brightSunIntervals, "0-49, 50-99");
    await fillApplicability(user);
    await user.clear(screen.getByLabelText("Operator ID"));
    await user.type(screen.getByLabelText("Operator ID"), "reviewer-one");
    await user.clear(screen.getByLabelText("Unseen-frame check size (20–50)"));
    await user.type(
      screen.getByLabelText("Unseen-frame check size (20–50)"),
      "25",
    );
    await user.selectOptions(
      screen.getByLabelText("near applicability"),
      "not_applicable",
    );
    expect(screen.getByLabelText("near applicability")).toHaveValue(
      "not_applicable",
    );
    await user.click(
      screen.getByRole("button", { name: "Start unseen-frame check" }),
    );
    await screen.findByTestId("annotation-panel");
    const post = vi
      .mocked(fetch)
      .mock.calls.find(([, init]) => (init as RequestInit)?.method === "POST")!;
    expect(JSON.parse((post[1] as RequestInit).body as string)).toEqual(
      expect.objectContaining({
        data_role: "check",
        target_frame_count: 25,
        development_package_session_id: "annotation-session-1",
        development_package_sha256: sha("b"),
        strata_applicability: expect.objectContaining({
          lighting: expect.arrayContaining([
            expect.objectContaining({
              stratum: "bright_sun",
              quota: 25,
              frame_intervals: [
                { start_frame: 0, end_frame: 49 },
                { start_frame: 50, end_frame: 99 },
              ],
            }),
          ]),
        }),
      }),
    );
    expect(
      storage.getItem(
        "football-tracking.ball-annotation.v1.workflow-1.probe-ready-1",
      ),
    ).toContain('"data_role":"check"');
  });
});
