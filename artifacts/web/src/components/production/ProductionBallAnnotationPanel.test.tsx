import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import type { ReviewProxyRepairJobView } from "@/lib/productionReviewProxyRepair";

import {
  ProductionBallAnnotationPanel,
  type BallAnnotationValueView,
  type BallAnnotationSessionView,
} from "./ProductionBallAnnotationPanel";

const decodeMockControl = vi.hoisted(() => ({ autoReady: true }));

vi.mock("./BallAnnotationCanvas", () => ({
  BallAnnotationCanvas: (props: {
    imageUrl: string;
    disabled: boolean;
    onImageDecodeStateChange: (state: "loading" | "ready" | "failed") => void;
    onGeometryChange: (geometry: {
      point: [number, number] | null;
      box: {
        left: number;
        top: number;
        right: number;
        bottom: number;
      } | null;
    }) => void;
    onClearGeometry: () => void;
    onUndo: () => void;
  }) => {
    useEffect(() => {
      props.onImageDecodeStateChange("loading");
      if (decodeMockControl.autoReady) {
        props.onImageDecodeStateChange("ready");
      }
    }, [props.imageUrl, props.onImageDecodeStateChange]);
    return (
      <div data-testid="mock-ball-canvas" data-image-url={props.imageUrl}>
        <button
          type="button"
          disabled={props.disabled}
          onClick={() =>
            props.onGeometryChange({ point: [100, 200], box: null })
          }
        >
          mock point
        </button>
        <button
          type="button"
          disabled={props.disabled}
          onClick={() =>
            props.onGeometryChange({
              point: [100, 200],
              box: { left: 90, top: 190, right: 110, bottom: 210 },
            })
          }
        >
          mock box
        </button>
        <button
          type="button"
          disabled={props.disabled}
          onClick={props.onClearGeometry}
        >
          mock clear geometry
        </button>
        <button type="button" disabled={props.disabled} onClick={props.onUndo}>
          mock undo draft
        </button>
        <button
          type="button"
          onClick={() => props.onImageDecodeStateChange("ready")}
        >
          mock decode ready
        </button>
        <button
          type="button"
          onClick={() => props.onImageDecodeStateChange("failed")}
        >
          mock decode failed
        </button>
      </div>
    );
  },
}));

afterEach(() => {
  decodeMockControl.autoReady = true;
});

const sha = (character: string) => character.repeat(64);

const session: BallAnnotationSessionView = {
  sessionId: "annotation-session-1",
  requestSha256: sha("a"),
  dataRole: "check",
  status: "annotating",
  stage: "human_annotation",
  source: {
    sourceId: "source-1",
    sourceSha256: sha("b"),
    width: 5_120,
    height: 1_440,
    frameCount: 10_000,
    fps: 20,
  },
  decode: {
    requestedMode: "sequential",
    effectiveMode: "sequential",
    positionVerification: "opencv_next_frame_index_with_0.25_tolerance",
  },
  lockedProfile: {
    profileId: "official-coco-yolo11s-sahi",
    profileSha256: sha("c"),
  },
  controlProfileId: "current-coco-yolov8n-direct",
  samplingManifestSha256: sha("d"),
  metricProfileId: "tiny_ball_feasibility_metric_v1",
  attemptFamilySha256: sha("1"),
  developmentPackageBinding: {
    sessionId: "development-session-1",
    packageSha256: sha("2"),
    attemptFamilySha256: sha("1"),
  },
  checkProbeJobId: "probe-check-1",
  checkProbeAuthority: { jobId: "probe-check-1", reportSha256: sha("3") },
  retryFromSessionId: null,
  retryLineage: null,
  errorCode: null,
  blockerCode: null,
  reviewProxyRepair: null,
  frames: [
    {
      frameIndex: 2_000,
      displayTimeSeconds: 100,
      decoderReportedPosMsec: 100_000,
      decoderTimeSeconds: 100,
      truePresentationTimestamp: {
        status: "not_collected",
        valueSeconds: null,
        method: null,
      },
      proxyBinding: null,
      temporalGroupId: "group-2000",
      sourceFrameSha256: sha("e"),
      annotationRevision: 0,
      annotationEtag: '"revision-0"',
      suggestedCandidates: [
        {
          candidateId: "candidate-one",
          bbox: { left: 3_820, top: 860, right: 3_833, bottom: 874 },
          confidence: 0.24,
          profileId: "official-coco-yolo11s-sahi",
          rank: 1,
          suggestionJobId: "probe-ready-1",
          suggestionSha256: sha("1"),
          decision: "pending",
        },
      ],
      currentAnnotation: null,
    },
    {
      frameIndex: 2_100,
      displayTimeSeconds: 105,
      decoderReportedPosMsec: 105_000,
      decoderTimeSeconds: 105,
      truePresentationTimestamp: {
        status: "not_collected",
        valueSeconds: null,
        method: null,
      },
      proxyBinding: null,
      temporalGroupId: "group-2100",
      sourceFrameSha256: sha("f"),
      annotationRevision: 1,
      annotationEtag: '"revision-1"',
      suggestedCandidates: [],
      currentAnnotation: {
        point: null,
        bbox: null,
        presence: "absent",
        visibility: "not_applicable",
        trainingUse: "excluded",
        annotationState: "confirmed",
        scaleStratum: "not_applicable",
        lightingTag: "shadow",
        motionOcclusionTags: [],
        provenance: "manual_human_annotation",
      },
    },
  ],
  progress: {
    annotatedFrames: 1,
    totalFrames: 2,
    unconfirmedSuggestions: 1,
    missingStrata: ["scale:far"],
  },
  finalPackage: null,
};

function repairJob(
  status: ReviewProxyRepairJobView["status"] = "running",
): ReviewProxyRepairJobView {
  const ready = status === "ready";
  const committing = status === "committing";
  const failed = status === "failed" || status === "blocked";
  return {
    repairId: "proxy-repair-1",
    attemptRootRepairId: "proxy-repair-1",
    attemptNumber: 1,
    retryFromRepairId: null,
    requestSha256: sha("4"),
    status,
    stage: ready ? "ready" : committing ? "proxy_ready" : status,
    presetId: "h264-cfr-720p-v1",
    eligibility: {
      eligible: true,
      action: "generate_verified_review_proxy",
      blockerCode: "review_proxy_required",
    },
    authority: {
      blockedSessionId: "annotation-blocked-1",
      blockedSessionRequestSha256: sha("5"),
      blockedSessionRecordSha256: sha("6"),
      parentProbeJobId: "probe-parent-17",
      developmentProbeJobIds: ["probe-parent-17"],
      parentProbeRequestSha256: sha("7"),
      parentProbeIntentSha256: sha("8"),
      parentProbeSemanticIntentSha256: sha("9"),
      parentProbeReportSha256: sha("8"),
      parentProbeResultManifestSha256: sha("9"),
      parentProbeRecordSha256: sha("a"),
      parentExecutionBundleSha256: sha("b"),
      parentRuntimeEnvironmentSha256: sha("c"),
      sourceFrameEvidenceSha256: sha("d"),
      sourceId: "source-1",
      sourceSha256: sha("b"),
      sourceFileIdentitySha256: sha("c"),
      sourceSizeBytes: 4096,
      sourceWidth: 5120,
      sourceHeight: 1440,
      sourceFrameCount: 100,
      sourceFps: 20,
      lockedProfileId: "official-coco-yolo11s-sahi",
      lockedProfileSha256: sha("d"),
      frameIndices: [10, 20],
      samplingManifestSha256: sha("e"),
      temporalGroupsSha256: sha("f"),
      candidateEvidenceSha256: sha("0"),
      replacementRequestAuthoritySha256: sha("1"),
    },
    progress: {
      stageCompleted: ready ? 6 : committing ? 1 : 0,
      stageTotal: 6,
      sourceFramesCompleted: ready || committing ? 100 : 50,
      sourceFramesTotal: 100,
      updatedAt: "2026-07-18T17:00:00Z",
    },
    canCancel: status === "queued" || status === "running",
    canRetry: failed || status === "cancelled",
    result: ready
      ? {
          proxy: {
            reviewProxyId: "review-proxy-1",
            reviewProxyManifestSha256: sha("1"),
            proxyMediaSha256: sha("2"),
            proxySizeBytes: 2048,
            proxyWidth: 2560,
            proxyHeight: 720,
            proxyFrameCount: 100,
            proxyFps: 20,
            mappingSha256: sha("3"),
            sampledArtifactCount: 2,
            encoderBindingSha256: sha("4"),
            repairExecutionBindingSha256: sha("5"),
            repairCodeBundleSha256: sha("6"),
            repairRuntimeSha256: sha("7"),
            repairDecoderFingerprintSha256: sha("8"),
          },
          childProbe: {
            jobId: "probe-child-18",
            requestSha256: sha("5"),
            intentSha256: sha("6"),
            semanticIntentSha256: sha("7"),
            resourceSha256: sha("8"),
            frozenProfilesSha256: sha("9"),
            reportSha256: sha("8"),
            resultManifestSha256: sha("9"),
            executionBundleSha256: sha("a"),
            runtimeEnvironmentSha256: sha("b"),
            continuationExecutionBindingSha256: sha("c"),
            continuationCodeBundleSha256: sha("d"),
            continuationRuntimeSha256: sha("e"),
            retryFromJobId: "probe-parent-17",
            retryKind: "review_proxy_decode_upgrade",
            statusUrl: "/api/v1/detector-probes/probe-child-18",
            reportUrl: "/api/v1/detector-probes/probe-child-18",
          },
          replacementSession: {
            sessionId: "annotation-replacement-1",
            requestSha256: sha("c"),
            status: "annotating",
            retryFromSessionId: "annotation-blocked-1",
            retryMode: "review_proxy_decode_upgrade",
            attemptFamilySha256: sha("d"),
            developmentProbeJobIds: ["probe-parent-17", "probe-child-18"],
            statusUrl:
              "/api/v1/ball-annotation-sessions/annotation-replacement-1",
          },
          parentProbeRecordSha256After: sha("a"),
        }
      : null,
    errorCode: failed ? "review_proxy_failed" : null,
    blockerCode: status === "blocked" ? "review_proxy_failed" : null,
    recoveryAction: failed ? "retry" : null,
    createdAt: "2026-07-18T16:59:00Z",
    updatedAt: "2026-07-18T17:00:00Z",
    statusUrl: "/api/v1/detector-review-proxy-repairs/proxy-repair-1",
    cancelUrl: "/api/v1/detector-review-proxy-repairs/proxy-repair-1/cancel",
    retryUrl: "/api/v1/detector-review-proxy-repairs/proxy-repair-1/retry",
  };
}

const repairCapability: NonNullable<
  BallAnnotationSessionView["reviewProxyRepair"]
> = {
  eligible: true,
  action: "generate_verified_review_proxy",
  createUrl: "/api/v1/detector-review-proxy-repairs",
  parentProbeJobId: "probe-parent-17",
  parentProbeReportSha256: sha("8"),
  parentProbeResultManifestSha256: sha("9"),
  parentProbeRecordSha256: sha("a"),
  blockedSessionRecordSha256: sha("b"),
};

function makePanelProps(
  overrides: Partial<
    React.ComponentProps<typeof ProductionBallAnnotationPanel>
  > = {},
) {
  const resolvedSession = overrides.session ?? session;
  const resolvedFrameOffset = overrides.activeFrameOffset ?? 0;
  const resolvedFrame = resolvedSession.frames[resolvedFrameOffset] ?? null;
  return {
    session: resolvedSession,
    activeFrameOffset: resolvedFrameOffset,
    frameImageUrl: "blob:verified-frame",
    frameImageState: "ready",
    frameImageIdentity: resolvedFrame
      ? `${resolvedSession.sessionId}:${resolvedFrame.frameIndex}:${resolvedFrame.sourceFrameSha256}`
      : null,
    operationState: "idle",
    operationError: null,
    onNavigate: vi.fn(),
    onSave: vi.fn(),
    onDelete: vi.fn(),
    onUndoSaved: vi.fn(),
    onStartPropagation: vi.fn(),
    onFinalize: vi.fn(),
    ...overrides,
  } satisfies React.ComponentProps<typeof ProductionBallAnnotationPanel>;
}

function renderPanel(
  overrides: Partial<
    React.ComponentProps<typeof ProductionBallAnnotationPanel>
  > = {},
) {
  const props = makePanelProps(overrides);
  render(
    <LanguageProvider>
      <ProductionBallAnnotationPanel {...props} />
    </LanguageProvider>,
  );
  return props;
}

describe("ProductionBallAnnotationPanel", () => {
  it("blocks every annotation mutation until browser decode succeeds and fails closed on decode error", async () => {
    decodeMockControl.autoReady = false;
    const user = userEvent.setup();
    const props = renderPanel();

    const point = await screen.findByRole("button", { name: "mock point" });
    const presence = screen.getByLabelText("Presence");
    const save = screen.getByRole("button", {
      name: "Save confirmed annotation",
    });
    const acceptSuggestion = screen.getByRole("button", {
      name: "Accept into draft",
    });
    expect(point).toBeDisabled();
    expect(presence).toBeDisabled();
    expect(save).toBeDisabled();
    expect(acceptSuggestion).toBeDisabled();
    expect(screen.getByText("Decoding verified source frame…")).toHaveAttribute(
      "role",
      "status",
    );

    await user.click(point);
    await user.click(save);
    await user.click(acceptSuggestion);
    expect(props.onSave).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "mock decode ready" }));
    await waitFor(() => expect(presence).toBeEnabled());
    await user.selectOptions(presence, "absent");
    await user.click(save);
    expect(props.onSave).toHaveBeenCalledOnce();

    await user.click(
      screen.getByRole("button", { name: "mock decode failed" }),
    );
    await waitFor(() => expect(presence).toBeDisabled());
    const decodeFailure = screen.getByText(
      "The verified frame could not be decoded by this browser; annotation and finalization are blocked.",
    );
    expect(decodeFailure.closest('[role="alert"]')).not.toBeNull();
    expect(point).toBeDisabled();
    expect(save).toBeDisabled();
    expect(acceptSuggestion).toBeDisabled();

    await user.click(point);
    await user.click(save);
    await user.click(acceptSuggestion);
    expect(props.onSave).toHaveBeenCalledOnce();
  });

  it("requires a fresh decode when the frame identity changes", async () => {
    decodeMockControl.autoReady = false;
    const user = userEvent.setup();
    const firstProps = makePanelProps();
    const rendered = render(
      <LanguageProvider>
        <ProductionBallAnnotationPanel {...firstProps} />
      </LanguageProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "mock decode ready" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Presence")).toBeEnabled(),
    );

    rendered.rerender(
      <LanguageProvider>
        <ProductionBallAnnotationPanel {...firstProps} activeFrameOffset={1} />
      </LanguageProvider>,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Presence")).toBeDisabled(),
    );
    expect(screen.getByText("Verifying source frame…")).toBeVisible();

    const nextFrame = firstProps.session.frames[1];
    if (!nextFrame) throw new Error("expected second annotation frame");
    rendered.rerender(
      <LanguageProvider>
        <ProductionBallAnnotationPanel
          {...firstProps}
          activeFrameOffset={1}
          frameImageIdentity={`${firstProps.session.sessionId}:${nextFrame.frameIndex}:${nextFrame.sourceFrameSha256}`}
        />
      </LanguageProvider>,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Presence")).toBeDisabled(),
    );
    expect(screen.getByText("Decoding verified source frame…")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "mock decode ready" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Presence")).toBeEnabled(),
    );
  });

  it("does not imply that development frames are training-authorized", () => {
    renderPanel({
      session: {
        ...session,
        dataRole: "development",
        controlProfileId: null,
      },
    });

    expect(
      screen.getByText(
        "Development frames are only for exploration and human calibration; they are not a blind check and never enter training or production truth automatically.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText(/may support later training/i),
    ).not.toBeInTheDocument();
  });

  it("keeps presence, visibility, and training use separate and check-only", async () => {
    const user = userEvent.setup();
    const props = renderPanel();

    expect(
      screen.getByText(
        "Data-isolated check frames are evaluation-only and can never train a detector.",
      ),
    ).toBeVisible();
    expect(
      screen.getByTestId("ball-annotation-workspace-governance"),
    ).toHaveTextContent(
      "One person may annotate development data and make local trial decisions, but their own work is not an independent production audit. / 一个人可以标注开发数据并作出本地试跑决定，但其本人完成的工作不构成独立生产审计。",
    );
    expect(
      screen.getByText("Direct source frame · no proxy binding"),
    ).toBeVisible();
    expect(screen.getByLabelText("Training use")).toHaveValue("excluded");
    expect(screen.getByLabelText("Training use")).toBeDisabled();
    expect(
      screen.getByText("Model suggestions and server decisions"),
    ).toBeVisible();
    expect(screen.getByText("Server decision: pending")).toBeVisible();
    expect(
      screen.getByText("Frame 2000 · display time 100.000 s"),
    ).toBeVisible();
    expect(screen.getByText("True presentation timestamp")).toBeVisible();
    expect(screen.getByText("not_collected")).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Presence"), "present");
    await user.selectOptions(screen.getByLabelText("Visibility"), "visible");
    await user.click(screen.getByRole("button", { name: "mock point" }));
    await user.click(screen.getByRole("button", { name: "mock box" }));
    await user.selectOptions(screen.getByLabelText("Scale stratum"), "far");
    await user.click(screen.getByRole("checkbox", { name: "airborne" }));
    await user.click(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    );

    expect(props.onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        presence: "present",
        visibility: "visible",
        trainingUse: "excluded",
        annotationState: "confirmed",
        point: [100, 200],
        bbox: { left: 90, top: 190, right: 110, bottom: 210 },
        motionOcclusionTags: ["airborne"],
      }),
    );
  });

  it("shows the exact review-proxy media and frame/time mapping", () => {
    renderPanel({
      session: {
        ...session,
        frames: [
          {
            ...session.frames[0],
            proxyBinding: {
              proxySha256: sha("4"),
              proxySizeBytes: 4_096,
              proxyWidth: 1_920,
              proxyHeight: 1_080,
              mapSha256: sha("5"),
              bindingSha256: sha("6"),
              sourceFrame: {
                frameIndex: 2_000,
                decoderReportedPosMsec: 100_000,
                sha256: sha("e"),
              },
              proxyFrame: {
                frameIndex: 2_000,
                decoderReportedPosMsec: 100_050,
                sha256: sha("7"),
              },
              mapTimeToleranceMsec: 2,
              declaredOffsetMsec: 50,
              observedOffsetMsec: 50,
              residualMsec: 0,
            },
          },
        ],
      },
    });

    const proxy = screen.getByRole("region", { name: "Review proxy binding" });
    expect(proxy).toHaveTextContent(`Proxy binding SHA-256 ${sha("6")}`);
    expect(proxy).toHaveTextContent(`Proxy media SHA-256 ${sha("4")}`);
    expect(proxy).toHaveTextContent(`Proxy map SHA-256 ${sha("5")}`);
    expect(proxy).toHaveTextContent(
      "Source frame 2000 @ 100000.000 ms → proxy frame 2000 @ 100050.000 ms",
    );
    expect(proxy).toHaveTextContent(
      "Declared offset 50.000 ms · observed 50.000 ms · residual 0.000 ms · tolerance 2.000 ms",
    );
  });

  it("renders uncollected proxy timing as unavailable and shows proxy authority", () => {
    renderPanel({
      session: {
        ...session,
        decode: {
          requestedMode: "direct",
          effectiveMode: "direct_verified",
          positionVerification: "verified_review_proxy_frame_index_mapping_v1",
        },
        frames: [
          {
            ...session.frames[0],
            decoderReportedPosMsec: null,
            decoderTimeSeconds: null,
            proxyBinding: {
              proxySha256: sha("4"),
              proxySizeBytes: 4_096,
              proxyWidth: 1_920,
              proxyHeight: 1_080,
              mapSha256: sha("5"),
              bindingSha256: sha("6"),
              sourceFrame: {
                frameIndex: 2_000,
                decoderReportedPosMsec: null,
                sha256: sha("e"),
              },
              proxyFrame: {
                frameIndex: 2_000,
                decoderReportedPosMsec: 100_050,
                sha256: sha("7"),
              },
              mapTimeToleranceMsec: 2,
              declaredOffsetMsec: 50,
              observedOffsetMsec: null,
              residualMsec: null,
            },
          },
        ],
      },
    });

    expect(
      screen.getByText("Decoder timing evidence").parentElement,
    ).toHaveTextContent("unavailable");
    expect(
      screen.getByText("Position verification").parentElement,
    ).toHaveTextContent(
      "verified_review_proxy_frame_index_mapping_v1 · direct → direct_verified",
    );
    const proxy = screen.getByRole("region", { name: "Review proxy binding" });
    expect(proxy).toHaveTextContent(
      "Source frame 2000 @ unavailable → proxy frame 2000 @ 100050.000 ms",
    );
    expect(proxy).toHaveTextContent(
      "Declared offset 50.000 ms · observed unavailable · residual unavailable · tolerance 2.000 ms",
    );
    expect(document.body).not.toHaveTextContent("NaN");
  });

  it("normalizes a development absent frame to confirmed background", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderPanel({ session: { ...session, dataRole: "development" }, onSave });
    await user.selectOptions(screen.getByLabelText("Presence"), "absent");
    expect(screen.getByLabelText("Visibility")).toHaveValue("not_applicable");
    expect(screen.getByLabelText("Training use")).toHaveValue("background");
    await user.click(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    );
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        point: null,
        bbox: null,
        presence: "absent",
        visibility: "not_applicable",
        trainingUse: "background",
      }),
    );
  });

  it("does not allow a development positive until a confirmed box exists", async () => {
    const user = userEvent.setup();
    renderPanel({ session: { ...session, dataRole: "development" } });
    await user.selectOptions(screen.getByLabelText("Presence"), "present");
    await user.click(screen.getByRole("button", { name: "mock point" }));
    expect(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    ).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "mock box" }));
    expect(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    ).toBeEnabled();
  });

  it("requires a human-confirmed box for a localizable check frame", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.selectOptions(screen.getByLabelText("Presence"), "present");
    await user.click(screen.getByRole("button", { name: "mock point" }));

    expect(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "Data-isolated check scoring requires a human-confirmed box. A point alone may seed development propagation, but it cannot score this unseen-frame check frame.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "mock box" }));
    expect(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    ).toBeEnabled();
  });

  it("normalizes cleared geometry to a valid unknown draft", async () => {
    const user = userEvent.setup();
    renderPanel({ session: { ...session, dataRole: "development" } });
    await user.selectOptions(screen.getByLabelText("Presence"), "present");
    await user.click(screen.getByRole("button", { name: "mock box" }));
    await user.click(
      screen.getByRole("button", { name: "mock clear geometry" }),
    );

    expect(screen.getByLabelText("Presence")).toHaveValue("unknown");
    expect(screen.getByLabelText("Visibility")).toHaveValue("unresolvable");
    expect(screen.getByLabelText("Training use")).toHaveValue("excluded");
    expect(screen.getByLabelText("Scale stratum")).toHaveValue(
      "not_applicable",
    );
    expect(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    ).toBeEnabled();
  });

  it("blocks finalization until detector and propagation suggestions are decided", () => {
    renderPanel({
      session: {
        ...session,
        progress: {
          ...session.progress,
          annotatedFrames: session.progress.totalFrames,
          unconfirmedSuggestions: 2,
          unconfirmedPropagationSuggestions: 1,
        },
      },
    });

    expect(
      screen.getByRole("button", {
        name: "Freeze and generate one-time report",
      }),
    ).toBeDisabled();
    expect(
      screen.getByText("Model suggestions and server decisions"),
    ).toBeVisible();
  });

  it("enables finalization only after every frame and suggestion is resolved", () => {
    renderPanel({
      session: {
        ...session,
        frames: session.frames.map((frame) => ({
          ...frame,
          suggestedCandidates: frame.suggestedCandidates.map((candidate) => ({
            ...candidate,
            decision: "accepted" as const,
          })),
        })),
        progress: {
          ...session.progress,
          annotatedFrames: session.progress.totalFrames,
          unconfirmedSuggestions: 0,
          unconfirmedPropagationSuggestions: 0,
        },
      },
    });

    expect(
      screen.getByRole("button", {
        name: "Freeze and generate one-time report",
      }),
    ).toBeEnabled();
    expect(screen.getByText("Server decision: accepted")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Accept into draft" }),
    ).not.toBeInTheDocument();
  });

  it("carries exact detector authority through accept, adjust, and ignore", async () => {
    const user = userEvent.setup();
    const accepted = renderPanel();

    await user.click(screen.getByRole("button", { name: "Accept into draft" }));
    await user.click(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    );
    expect(accepted.onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        bbox: { left: 3_820, top: 860, right: 3_833, bottom: 874 },
        provenance: "detector_candidate_human_confirmed",
      }),
      {
        action: "accept",
        kind: "detector_candidate",
        id: "candidate-one",
        jobId: "probe-ready-1",
        sha256: sha("1"),
      },
    );
  });

  it("keeps detector authority when the operator adjusts the geometry", async () => {
    const user = userEvent.setup();
    const adjusted = renderPanel();

    await user.click(
      screen.getByRole("button", { name: "Adjust before saving" }),
    );
    await user.click(screen.getByRole("button", { name: "mock box" }));
    await user.click(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    );
    expect(adjusted.onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        bbox: { left: 90, top: 190, right: 110, bottom: 210 },
        provenance: "detector_candidate_human_confirmed",
      }),
      {
        action: "accept",
        kind: "detector_candidate",
        id: "candidate-one",
        jobId: "probe-ready-1",
        sha256: sha("1"),
      },
    );
  });

  it("dismisses a detector candidate with its complete immutable binding", async () => {
    const user = userEvent.setup();
    const ignored = renderPanel();

    await user.click(screen.getByRole("button", { name: "Ignore suggestion" }));
    expect(ignored.onSave).toHaveBeenCalledWith(
      expect.objectContaining({ provenance: "suggestion_dismissed_manual" }),
      {
        action: "dismiss",
        kind: "detector_candidate",
        id: "candidate-one",
        jobId: "probe-ready-1",
        sha256: sha("1"),
      },
    );
  });

  it("requires an unchanged saved human-confirmed seed before propagation", async () => {
    const user = userEvent.setup();
    const onStartPropagation = vi.fn();
    const savedAnnotation: BallAnnotationValueView = {
      point: [100, 200] as [number, number],
      bbox: { left: 90, top: 190, right: 110, bottom: 210 },
      presence: "present" as const,
      visibility: "visible" as const,
      trainingUse: "positive" as const,
      annotationState: "confirmed" as const,
      scaleStratum: "far" as const,
      lightingTag: "bright_sun" as const,
      motionOcclusionTags: [],
      provenance: "manual_human_annotation",
    };
    renderPanel({
      session: {
        ...session,
        dataRole: "development",
        frames: [
          {
            ...session.frames[0],
            annotationRevision: 1,
            annotationEtag: '"revision-1"',
            currentAnnotation: savedAnnotation,
          },
        ],
      },
      onStartPropagation,
      propagationAvailable: true,
    });

    const propagate = screen.getByRole("button", {
      name: "Start short-window suggestion propagation",
    });
    expect(propagate).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("Lighting"), "shadow");
    expect(propagate).toBeDisabled();
    expect(
      screen.getByText("Save current changes before propagation."),
    ).toBeVisible();
    await user.click(propagate);
    expect(onStartPropagation).not.toHaveBeenCalled();
  });

  it("runs enabled propagation controls and draft/navigation handlers", async () => {
    const user = userEvent.setup();
    const onStartPropagation = vi.fn();
    const onNavigate = vi.fn();
    const savedAnnotation: BallAnnotationValueView = {
      point: [100, 200] as [number, number],
      bbox: { left: 90, top: 190, right: 110, bottom: 210 },
      presence: "present" as const,
      visibility: "visible" as const,
      trainingUse: "positive" as const,
      annotationState: "confirmed" as const,
      scaleStratum: "far" as const,
      lightingTag: "bright_sun" as const,
      motionOcclusionTags: ["airborne"],
      provenance: "manual_human_annotation",
    };
    renderPanel({
      session: {
        ...session,
        dataRole: "development",
        frames: [
          {
            ...session.frames[0],
            annotationRevision: 1,
            currentAnnotation: savedAnnotation,
          },
          session.frames[1],
        ],
      },
      propagationAvailable: true,
      onStartPropagation,
      onNavigate,
    });

    const radius = screen.getByLabelText("Propagation radius (frames)");
    await user.clear(radius);
    await user.type(radius, "1");
    await user.click(
      screen.getByRole("button", {
        name: "Start short-window suggestion propagation",
      }),
    );
    expect(onStartPropagation).toHaveBeenCalledWith(1);
    await user.click(screen.getByRole("button", { name: "Next frame" }));
    expect(onNavigate).toHaveBeenCalledWith(1);
    await user.selectOptions(screen.getByLabelText("Training use"), "excluded");
    await user.click(screen.getByRole("checkbox", { name: "airborne" }));
    await user.click(screen.getByRole("button", { name: "mock point" }));
    await user.click(screen.getByRole("button", { name: "mock undo draft" }));
    expect(screen.getByLabelText("Training use")).toHaveValue("excluded");
  });

  it("gates dirty-frame navigation and lets the operator stay or discard", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    renderPanel({ onNavigate });

    await user.selectOptions(screen.getByLabelText("Presence"), "absent");
    await user.click(screen.getByRole("button", { name: "Next frame" }));

    expect(onNavigate).not.toHaveBeenCalled();
    expect(
      screen.getByRole("alert", { name: "Unsaved frame changes" }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Stay on this frame" }),
    );
    expect(
      screen.queryByRole("alert", { name: "Unsaved frame changes" }),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next frame" }));
    await user.click(
      screen.getByRole("button", { name: "Discard draft and navigate" }),
    );
    expect(onNavigate).toHaveBeenCalledWith(1);
  });

  it("saves a dirty frame and navigates only after the saved revision returns", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const onSave = vi.fn();
    const props = makePanelProps({ onNavigate, onSave });
    const view = render(
      <LanguageProvider>
        <ProductionBallAnnotationPanel {...props} />
      </LanguageProvider>,
    );

    await user.selectOptions(screen.getByLabelText("Presence"), "absent");
    await user.click(screen.getByRole("button", { name: "Next frame" }));
    await user.click(screen.getByRole("button", { name: "Save and navigate" }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        presence: "absent",
        visibility: "not_applicable",
        trainingUse: "excluded",
      }),
    );
    expect(onNavigate).not.toHaveBeenCalled();

    const saved = onSave.mock.calls[0][0];
    view.rerender(
      <LanguageProvider>
        <ProductionBallAnnotationPanel
          {...props}
          session={{
            ...session,
            frames: [
              {
                ...session.frames[0],
                annotationRevision: 1,
                annotationEtag: '"revision-1"',
                currentAnnotation: saved,
              },
              session.frames[1],
            ],
            progress: {
              ...session.progress,
              annotatedFrames: 2,
            },
          }}
        />
      </LanguageProvider>,
    );
    await vi.waitFor(() => expect(onNavigate).toHaveBeenCalledWith(1));
  });

  it("hides propagation until a confirmable strong-revision contract is available", () => {
    renderPanel({ session: { ...session, dataRole: "development" } });
    expect(
      screen.queryByRole("button", {
        name: "Start short-window suggestion propagation",
      }),
    ).toBeNull();
    expect(screen.queryByLabelText("Propagation radius (frames)")).toBeNull();
  });

  it("keeps propagation results suggested-only and decides them with exact authority", async () => {
    const user = userEvent.setup();
    const props = renderPanel({
      propagationSuggestions: [
        {
          suggestionId: "suggestion-current",
          jobId: "propagation-job-one",
          suggestionSha256: sha("2"),
          frameIndex: 2_000,
          temporalGroupId: "group-2000",
          point: [98, 201],
          bbox: { left: 88, top: 191, right: 108, bottom: 211 },
          annotationState: "suggested",
          trainingUse: "excluded",
          provenance: "bounded_seed_copy_v1",
        },
      ],
    });

    expect(
      screen.getByRole("alert", { name: "Propagation result: suggested only" }),
    ).toHaveTextContent(
      "Frame 2000 · suggested only · excluded · manual confirmation required",
    );
    await user.click(
      screen.getAllByRole("button", { name: "Accept into draft" })[1],
    );
    await user.click(
      screen.getByRole("button", { name: "Save confirmed annotation" }),
    );
    expect(props.onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        bbox: { left: 88, top: 191, right: 108, bottom: 211 },
        provenance: "propagation_suggestion_human_confirmed",
      }),
      {
        action: "accept",
        kind: "propagation",
        id: "suggestion-current",
        jobId: "propagation-job-one",
        sha256: sha("2"),
      },
    );
  });

  it("shows and cancels a recoverable in-flight propagation job", async () => {
    const user = userEvent.setup();
    const onCancelPropagation = vi.fn();
    renderPanel({
      propagationJob: {
        jobId: "propagation-job-one",
        status: "waiting_probe",
        stage: "waiting_for_probe",
        pendingCount: 2,
        targetFrameIndices: [1_999, 2_001],
        errorCode: null,
      },
      onCancelPropagation,
    });

    expect(
      screen.getByRole("alert", { name: "Propagation job" }),
    ).toHaveTextContent(
      "propagation-job-one · waiting_probe · waiting_for_probe",
    );
    expect(screen.getByText("pending_human_confirmation=2")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Cancel propagation job" }),
    );
    expect(onCancelPropagation).toHaveBeenCalledOnce();
  });

  it("shows verified bindings, navigates, and offers append-only saved undo", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const onUndoSaved = vi.fn();
    renderPanel({ activeFrameOffset: 1, onNavigate, onUndoSaved });
    expect(screen.getByText(sha("f"))).toBeVisible();
    expect(screen.getByText("group-2100")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Previous frame" }));
    expect(onNavigate).toHaveBeenCalledWith(0);
    await user.click(
      screen.getByRole("button", { name: "Undo saved revision" }),
    );
    expect(onUndoSaved).toHaveBeenCalledWith(1);
  });

  it.each(["blocked", "finalized"] as const)(
    "disables mutation controls when the session is %s",
    (status) => {
      renderPanel({ session: { ...session, status } });
      expect(
        screen.getByRole("button", { name: "Save confirmed annotation" }),
      ).toBeDisabled();
      expect(screen.getByTestId("annotation-session-state")).toHaveTextContent(
        status,
      );
    },
  );

  it("shows the bounded repair CTA only for an eligible review-proxy blocker", async () => {
    const user = userEvent.setup();
    const onStartReviewProxyRepair = vi.fn();
    renderPanel({
      session: {
        ...session,
        status: "blocked",
        errorCode: "decode_integrity_failed",
        blockerCode: "review_proxy_required",
        reviewProxyRepair: repairCapability,
      },
      onStartReviewProxyRepair,
    });

    const button = screen.getByRole("button", {
      name: "Generate/repair review proxy",
    });
    expect(button).toHaveClass("min-h-11");
    expect(
      screen.getByText(
        /only repairs playable, frame-mapped review media.*independent production audit/i,
      ),
    ).toBeVisible();
    await user.click(button);
    expect(onStartReviewProxyRepair).toHaveBeenCalledOnce();
  });

  it("does not infer repair eligibility from a blocker alone", () => {
    renderPanel({
      session: {
        ...session,
        status: "blocked",
        blockerCode: "review_proxy_required",
      },
    });

    expect(
      screen.queryByRole("button", {
        name: "Generate/repair review proxy",
      }),
    ).not.toBeInTheDocument();
  });

  it("announces active repair progress and offers server-authorized cancellation", async () => {
    const user = userEvent.setup();
    const onCancelReviewProxyRepair = vi.fn();
    renderPanel({
      session: {
        ...session,
        status: "blocked",
        blockerCode: "review_proxy_required",
        reviewProxyRepair: repairCapability,
      },
      reviewProxyRepairJob: repairJob("running"),
      onCancelReviewProxyRepair,
    });

    const status = screen.getByTestId("review-proxy-repair-status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("running");
    expect(status).toHaveTextContent("50 / 100");
    const cancel = screen.getByRole("button", {
      name: "Cancel review-proxy repair",
    });
    expect(cancel).toHaveClass("min-h-11");
    await user.click(cancel);
    expect(onCancelReviewProxyRepair).toHaveBeenCalledWith("proxy-repair-1");
  });

  it("removes cancellation after commit and preserves ready provenance", () => {
    const committing = repairJob("committing");
    const committingView = renderPanel({
      reviewProxyRepairJob: committing,
    });
    expect(
      screen.queryByRole("button", {
        name: "Cancel review-proxy repair",
      }),
    ).not.toBeInTheDocument();
    cleanup();

    renderPanel({
      reviewProxyRepairJob: repairJob("ready"),
    });
    const provenance = screen.getByTestId("review-proxy-repair-provenance");
    expect(provenance).toHaveTextContent("probe-parent-17");
    expect(provenance).toHaveTextContent("probe-child-18");
    expect(provenance).toHaveTextContent("annotation-blocked-1");
    expect(provenance).toHaveTextContent("annotation-replacement-1");
    expect(provenance).toHaveTextContent(sha("1"));
    expect(provenance).toHaveTextContent(sha("2"));
    expect(provenance).toHaveTextContent(sha("3"));
    expect(provenance.querySelector(".break-all")).not.toBeNull();
  });

  it("distinguishes a server retry from reloading failed poll transport", async () => {
    const user = userEvent.setup();
    const onRetryReviewProxyRepair = vi.fn();
    const onReloadReviewProxyRepair = vi.fn();
    renderPanel({
      reviewProxyRepairJob: repairJob("failed"),
      reviewProxyRepairError: "Transport failed after the server response.",
      onRetryReviewProxyRepair,
      onReloadReviewProxyRepair,
    });

    expect(screen.getByText("review_proxy_failed")).toBeVisible();
    expect(screen.getByText("retry")).toBeVisible();
    expect(
      screen.getByText("Transport failed after the server response."),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Retry server repair" }),
    );
    expect(onRetryReviewProxyRepair).toHaveBeenCalledWith("proxy-repair-1");
    expect(onReloadReviewProxyRepair).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Reload repair status" }),
    );
    expect(onReloadReviewProxyRepair).toHaveBeenCalledWith("proxy-repair-1");
  });

  it("shows the server retry action only when canRetry is true", () => {
    renderPanel({
      reviewProxyRepairJob: repairJob("committing"),
      reviewProxyRepairError: "Poll transport failed.",
    });

    expect(
      screen.queryByRole("button", { name: "Retry server repair" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reload repair status" }),
    ).toBeVisible();
  });

  it("localizes the evaluation-only and unconfirmed-suggestion boundaries", () => {
    localStorage.setItem("app-language", "zh");
    renderPanel();
    expect(
      screen.getByText("数据隔离检查帧仅用于评估，绝不能训练检测器。"),
    ).toBeVisible();
    expect(screen.getByText("模型建议及服务端决定")).toBeVisible();
    expect(screen.getByText("服务端决定：待处理")).toBeVisible();
    expect(screen.getByText("保存确认标注")).toBeVisible();
  });
});
