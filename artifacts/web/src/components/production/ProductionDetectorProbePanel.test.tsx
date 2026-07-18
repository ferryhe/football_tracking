import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";

import {
  ProductionDetectorProbePanel,
  type DetectorProbeJobView,
  type DetectorProbeModelView,
} from "./ProductionDetectorProbePanel";

const sha = (character: string) => character.repeat(64);

const models: DetectorProbeModelView[] = [
  {
    kind: "registered",
    modelId: "official-coco-yolo11n",
    version: "v8.4.0:yolo11n.pt",
    runtimeVersion: "ultralytics==8.4.31",
    displayName: "Official COCO YOLO11n",
    architectureFamily: "YOLO11",
    sourceProject: "Ultralytics assets",
    sourceVersion: "v8.4.0",
    acquisitionMethod: "pinned local download",
    accessRequirement: "No account required",
    weightsSha256: sha("a"),
    manifestSha256: sha("b"),
    lifecycle: "unverified",
    trialEligible: false,
    sourceSegmentQualified: false,
    cameraQualified: false,
    availability: "available",
    datasetLicense: "COCO terms",
    modelLicense: "AGPL-3.0",
    runtimeLicense: "AGPL-3.0",
    deploymentLicense: "review_required",
    egress: { leavesDevice: false, destination: null, consent: "not_required" },
    profiles: [
      {
        profileId: "official-coco-yolo11n-direct",
        version: "1.0.0",
        digest: sha("c"),
        mode: "direct",
        inputSize: 1280,
        confidenceThreshold: 0.05,
        tile: null,
        topK: 5,
        probeSelectable: true,
        recommended: true,
      },
    ],
  },
  {
    kind: "registered",
    modelId: "official-coco-yolo11s",
    version: "v8.4.0:yolo11s.pt",
    runtimeVersion: "ultralytics==8.4.31",
    displayName: "Official COCO YOLO11s",
    architectureFamily: "YOLO11",
    sourceProject: "Ultralytics assets",
    sourceVersion: "v8.4.0",
    acquisitionMethod: "pinned local download",
    accessRequirement: "No account required",
    weightsSha256: sha("d"),
    manifestSha256: sha("e"),
    lifecycle: "unverified",
    trialEligible: false,
    sourceSegmentQualified: false,
    cameraQualified: false,
    availability: "available",
    datasetLicense: "COCO terms",
    modelLicense: "AGPL-3.0",
    runtimeLicense: "AGPL-3.0",
    deploymentLicense: "review_required",
    egress: { leavesDevice: false, destination: null, consent: "not_required" },
    profiles: [
      {
        profileId: "official-coco-yolo11s-sahi",
        version: "1.0.0",
        digest: sha("f"),
        mode: "sahi",
        inputSize: 1280,
        confidenceThreshold: 0.05,
        tile: {
          width: 1280,
          height: 720,
          overlapWidthRatio: 0.2,
          overlapHeightRatio: 0.2,
        },
        topK: 5,
        probeSelectable: true,
        recommended: true,
      },
    ],
  },
  {
    kind: "catalog_finding",
    modelId: "public-soccer-ball-yolo11n",
    version: "project-version-3",
    runtimeVersion: "unavailable",
    displayName: "Public soccer-ball YOLO11n",
    architectureFamily: "YOLO11",
    sourceProject: "Roboflow soccer-ball-detection-s2sg3",
    sourceVersion: "3",
    acquisitionMethod: "account-bound hosted project",
    accessRequirement:
      "Account/plan and license review required · https://universe.roboflow.com/example/model/3",
    weightsSha256: null,
    manifestSha256: null,
    lifecycle: "unverified",
    trialEligible: false,
    sourceSegmentQualified: false,
    cameraQualified: false,
    availability: "unavailable",
    availabilityReason: "Account-bound weights were not acquired.",
    datasetLicense: "review_required",
    modelLicense: "review_required",
    runtimeLicense: "unknown",
    deploymentLicense: "review_required",
    egress: {
      leavesDevice: null,
      destination: null,
      consent: "required_before_external_inference",
    },
    profiles: [],
  },
];

const readyJob: DetectorProbeJobView = {
  jobId: "probe-job-1",
  parentTrialId: "trial-1",
  requestSha256: sha("3"),
  immutableIdentity: "immutable-probe-job-1",
  resultManifestSha256: sha("7"),
  status: "ready",
  stage: "ready",
  progressPercent: 100,
  selectedProfileIds: [
    "official-coco-yolo11n-direct",
    "official-coco-yolo11s-sahi",
  ],
  frameIndices: [120],
  retryFromJobId: null,
  failureCode: null,
  recoveryAction: null,
  noProfilesProducedCandidates: false,
  frames: [
    {
      frameIndex: 120,
      sourceImageUrl:
        "/api/detector-probes/probe-job-1/artifacts/source-frame-000000120",
      sourceSha256: sha("4"),
      sourceSizeBytes: 42_000,
      sourceWidth: 5_120,
      sourceHeight: 1_440,
      mediaIntegrityClean: true,
      mediaIntegrityReasons: [],
      profiles: [
        {
          profileId: "official-coco-yolo11n-direct",
          profileSha256: sha("c"),
          status: "completed",
          overlayImageUrl:
            "/api/detector-probes/probe-job-1/artifacts/raw-overlay-120-yolo11n-direct",
          overlaySha256: sha("5"),
          overlaySizeBytes: 41_000,
          rawBoxes: [
            {
              x: 100,
              y: 80,
              width: 7,
              height: 7,
              confidence: 0.23,
              label: "sports ball",
            },
          ],
          displayCandidate: {
            x: 100,
            y: 80,
            width: 7,
            height: 7,
            confidence: 0.23,
            label: "sports ball",
          },
          latencyMs: 32.5,
          candidateCount: 1,
          topK: 5,
          filterReasons: { outside_field: 2 },
          failureCode: null,
        },
        {
          profileId: "official-coco-yolo11s-sahi",
          profileSha256: sha("f"),
          status: "completed",
          overlayImageUrl:
            "/api/detector-probes/probe-job-1/artifacts/raw-overlay-120-yolo11s-sahi",
          overlaySha256: sha("6"),
          overlaySizeBytes: 41_500,
          rawBoxes: [],
          displayCandidate: null,
          latencyMs: 84.2,
          candidateCount: 0,
          topK: 5,
          filterReasons: { below_confidence: 4, too_small: 1 },
          failureCode: null,
        },
      ],
    },
  ],
};

function renderPanel(
  changes: Partial<
    React.ComponentProps<typeof ProductionDetectorProbePanel>
  > = {},
) {
  const props: React.ComponentProps<typeof ProductionDetectorProbePanel> = {
    models,
    catalogState: "ready",
    job: null,
    mutationPending: false,
    onStart: vi.fn(),
    onCancel: vi.fn(),
    onRetry: vi.fn(),
    ...changes,
  };
  const view = render(
    <LanguageProvider>
      <ProductionDetectorProbePanel {...props} />
    </LanguageProvider>,
  );
  return { ...view, props, user: userEvent.setup() };
}

function loadAllEvidenceImages() {
  document.querySelectorAll("img").forEach((image) => fireEvent.load(image));
}

describe("ProductionDetectorProbePanel", () => {
  it("renders registered models and findings with the same ID/version as distinct cards", () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const sharedFinding: DetectorProbeModelView = {
      ...models[2],
      modelId: models[0].modelId,
      version: models[0].version,
      displayName: "Public predecessor finding",
    };

    try {
      renderPanel({ models: [models[0], sharedFinding] });
      expect(screen.getByText("Official COCO YOLO11n")).toBeVisible();
      expect(screen.getByText("Public predecessor finding")).toBeVisible();
      expect(
        consoleError.mock.calls.some((call) =>
          call.some((value) => String(value).includes("same key")),
        ),
      ).toBe(false);
    } finally {
      consoleError.mockRestore();
    }
  });

  it("shows exact immutable model/profile identity, lifecycle, licenses, availability, and egress", () => {
    renderPanel();

    const panel = screen.getByTestId("production-detector-probe-panel");
    expect(panel).toHaveClass("min-w-0", "w-full");
    expect(within(panel).getByText("Official COCO YOLO11n")).toBeVisible();
    expect(panel).toHaveTextContent("v8.4.0:yolo11n.pt");
    expect(panel).toHaveTextContent("ultralytics==8.4.31");
    expect(within(panel).getByText(sha("a"))).toBeVisible();
    expect(within(panel).getByText(sha("b"))).toBeVisible();
    expect(
      within(panel).getAllByText("unverified").length,
    ).toBeGreaterThanOrEqual(2);
    expect(
      within(panel).getAllByText(
        "Probe only; not eligible for trial acceptance.",
      ),
    ).toHaveLength(3);
    expect(panel).toHaveTextContent("AGPL-3.0");
    expect(panel).toHaveTextContent("COCO terms");
    expect(panel).toHaveTextContent("Confidence 0.05");
    expect(panel).toHaveTextContent("Tile 1280 × 720 · overlap 0.2 × 0.2");
    expect(
      within(panel).getAllByText(
        "Local only; frames do not leave this machine.",
      ),
    ).toHaveLength(2);
    expect(
      within(panel).getByText("Account-bound weights were not acquired."),
    ).toBeVisible();
    expect(panel).toHaveTextContent(
      "https://universe.roboflow.com/example/model/3",
    );
    expect(panel).toHaveTextContent("Frame egress is not established");
    expect(panel).toHaveTextContent("Roboflow soccer-ball-detection-s2sg3");
    expect(panel).toHaveTextContent("Account/plan and license review required");
    expect(
      within(panel).queryByRole("checkbox", {
        name: /public-soccer-ball-yolo11n/i,
      }),
    ).toBeNull();
  });

  it("requires two exact profiles and starts only the immutable profile IDs", async () => {
    const onStart = vi.fn();
    const { user } = renderPanel({ onStart });
    const start = screen.getByRole("button", {
      name: "Run bounded comparison",
    });

    expect(start).toBeEnabled();
    await user.click(start);
    expect(onStart).toHaveBeenCalledWith([
      "official-coco-yolo11n-direct",
      "official-coco-yolo11s-sahi",
    ]);
    expect(JSON.stringify(onStart.mock.calls)).not.toContain("model_path");

    await user.click(
      screen.getByRole("checkbox", { name: /official-coco-yolo11n-direct/i }),
    );
    expect(start).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Select 2–6 available exact profiles",
    );
  });

  it("pairs one recommended profile with a distinct model by default", async () => {
    const onStart = vi.fn();
    const oneRecommendation = models.map((model) => ({
      ...model,
      profiles: model.profiles.map((profile) => ({
        ...profile,
        recommended: profile.profileId === "official-coco-yolo11s-sahi",
      })),
    }));
    const { user } = renderPanel({ models: oneRecommendation, onStart });

    await user.click(
      screen.getByRole("button", { name: "Run bounded comparison" }),
    );

    expect(onStart).toHaveBeenCalledWith([
      "official-coco-yolo11s-sahi",
      "official-coco-yolo11n-direct",
    ]);
  });

  it("caps the bounded request at six exact profiles", async () => {
    const extraProfiles = Array.from({ length: 5 }, (_, index) => ({
      ...models[0].profiles[0],
      profileId: `extra-profile-${index + 1}`,
      digest: String(index + 4).repeat(64),
      recommended: false,
    }));
    const expandedModels = [
      {
        ...models[0],
        profiles: [...models[0].profiles, ...extraProfiles],
      },
      ...models.slice(1),
    ];
    const { user } = renderPanel({ models: expandedModels });

    for (const profile of extraProfiles.slice(0, 4)) {
      await user.click(
        screen.getByRole("checkbox", { name: new RegExp(profile.profileId) }),
      );
    }

    expect(
      screen.getByRole("checkbox", { name: /extra-profile-5/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Run bounded comparison" }),
    ).toBeEnabled();
  });

  it("reports progress and supports cancellation before commit", async () => {
    const onCancel = vi.fn();
    const { user } = renderPanel({
      job: {
        ...readyJob,
        status: "running",
        stage: "probing_frames",
        progressPercent: 43.25,
        frames: [],
      },
      onCancel,
    });

    expect(screen.getByRole("status")).toHaveTextContent("probing_frames");
    expect(
      screen.getByRole("progressbar", { name: "Detector probe progress" }),
    ).toHaveAttribute("aria-valuenow", "43.25");
    const cancel = screen.getByRole("button", { name: "Cancel comparison" });
    cancel.focus();
    await user.keyboard("{Enter}");
    expect(onCancel).toHaveBeenCalledWith("probe-job-1");
  });

  it("disables cancellation once publication is committing", () => {
    renderPanel({
      job: {
        ...readyJob,
        status: "committing",
        stage: "publishing",
        frames: [],
      },
    });

    expect(
      screen.getByRole("button", { name: "Cancel comparison" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/cannot be cancelled after publication begins/i),
    ).toBeVisible();
  });

  it.each(["failed", "blocked", "cancelled"] as const)(
    "offers only an explicit retry after %s",
    async (status) => {
      const onRetry = vi.fn();
      const { user } = renderPanel({
        job: {
          ...readyJob,
          status,
          stage: status,
          progressPercent: 55,
          frames: [],
          failureCode: "gpu_oom",
          recoveryAction: "Choose two smaller profiles and retry.",
        },
        onRetry,
      });

      expect(onRetry).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent("gpu_oom");
      expect(
        screen.getByText("Choose two smaller profiles and retry."),
      ).toBeVisible();
      await user.click(
        screen.getByRole("button", { name: "Retry comparison" }),
      );
      expect(onRetry).toHaveBeenCalledWith({
        retryFromJobId: "probe-job-1",
        profileIds: [
          "official-coco-yolo11n-direct",
          "official-coco-yolo11s-sahi",
        ],
      });
    },
  );

  it("renders same-frame source and side-by-side raw/display evidence for every profile", () => {
    renderPanel({ job: readyJob });
    loadAllEvidenceImages();

    expect(screen.getByText("Parent trial").parentElement).toHaveTextContent(
      "trial-1",
    );
    expect(
      screen.getByText("Same-frame request").parentElement,
    ).toHaveTextContent("120");
    const frame = screen.getByTestId("detector-probe-frame-120");
    expect(
      within(frame).getByRole("img", { name: "Source frame 120" }),
    ).toHaveAttribute("src", readyJob.frames[0].sourceImageUrl);
    expect(within(frame).getByText(sha("4"))).toBeVisible();
    expect(
      within(frame).getByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeVisible();
    expect(within(frame).getByText("32.5 ms")).toBeVisible();
    expect(within(frame).getByText("1 / top 5")).toBeVisible();
    expect(within(frame).getByText("outside_field: 2")).toBeVisible();
    expect(within(frame).getByText("No display candidate")).toBeVisible();
    expect(within(frame).getByText("below_confidence: 4")).toBeVisible();
    expect(within(frame).getByText("too_small: 1")).toBeVisible();
    expect(
      within(frame).getByRole("img", {
        name: "Raw detector overlay for official-coco-yolo11n-direct on frame 120",
      }),
    ).toHaveAttribute("src", readyJob.frames[0].profiles[0].overlayImageUrl);
    expect(within(frame).getByText(sha("5"))).toBeVisible();
  });

  it("loads exactly one evidence frame at a time and preserves trust on revisit", async () => {
    const secondFrame = structuredClone(readyJob.frames[0]);
    secondFrame.frameIndex = 240;
    secondFrame.sourceImageUrl =
      "/api/detector-probes/probe-job-1/artifacts/source-frame-000000240";
    secondFrame.profiles = secondFrame.profiles.map((profile) => ({
      ...profile,
      overlayImageUrl: `${profile.overlayImageUrl}-frame-240`,
    }));
    const pagedJob: DetectorProbeJobView = {
      ...readyJob,
      frameIndices: [120, 240],
      frames: [readyJob.frames[0], secondFrame],
    };
    const { user } = renderPanel({ job: pagedJob });

    expect(screen.getByText("Evidence frame 1 of 2")).toBeVisible();
    expect(screen.getByTestId("detector-probe-frame-120")).toBeVisible();
    expect(screen.queryByTestId("detector-probe-frame-240")).toBeNull();
    expect(document.querySelectorAll("img")).toHaveLength(3);
    expect(
      document.querySelector(`img[src="${secondFrame.sourceImageUrl}"]`),
    ).toBeNull();
    loadAllEvidenceImages();
    expect(
      screen.getByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Next frame" }));
    expect(screen.getByText("Evidence frame 2 of 2")).toBeVisible();
    expect(screen.queryByTestId("detector-probe-frame-120")).toBeNull();
    expect(screen.getByTestId("detector-probe-frame-240")).toBeVisible();
    expect(document.querySelectorAll("img")).toHaveLength(3);
    expect(
      document.querySelector(`img[src="${readyJob.frames[0].sourceImageUrl}"]`),
    ).toBeNull();
    expect(
      screen.queryByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeNull();
    loadAllEvidenceImages();
    expect(
      screen.getByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Previous frame" }));
    expect(screen.getByText("Evidence frame 1 of 2")).toBeVisible();
    expect(
      screen.getByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeVisible();
  });

  it("mounts only seven images for a 50-frame by six-profile evidence set", async () => {
    const profileIds = Array.from(
      { length: 6 },
      (_, index) => `bounded-profile-${index + 1}`,
    );
    const frames = Array.from({ length: 50 }, (_, frameOffset) => {
      const frameIndex = 1_000 + frameOffset;
      return {
        ...readyJob.frames[0],
        frameIndex,
        sourceImageUrl: `/api/detector-probes/probe-job-1/artifacts/source-${frameIndex}`,
        sourceSha256: ((frameOffset % 9) + 1).toString().repeat(64),
        profiles: profileIds.map((profileId, profileOffset) => ({
          ...readyJob.frames[0].profiles[0],
          profileId,
          profileSha256: (profileOffset + 1).toString().repeat(64),
          overlayImageUrl: `/api/detector-probes/probe-job-1/artifacts/overlay-${frameIndex}-${profileId}`,
          overlaySha256: (profileOffset + 2).toString().repeat(64),
        })),
      };
    });
    const largeJob: DetectorProbeJobView = {
      ...readyJob,
      selectedProfileIds: profileIds,
      frameIndices: frames.map((frame) => frame.frameIndex),
      frames,
    };
    const { user } = renderPanel({ job: largeJob });

    expect(screen.getByText("Evidence frame 1 of 50")).toBeVisible();
    expect(document.querySelectorAll("img")).toHaveLength(7);
    expect(screen.getByTestId("detector-probe-frame-1000")).toBeVisible();
    expect(screen.queryByTestId("detector-probe-frame-1001")).toBeNull();
    expect(screen.queryByTestId("detector-probe-frame-1049")).toBeNull();
    expect(
      document.querySelector(`img[src="${frames[49].sourceImageUrl}"]`),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next frame" }));
    expect(screen.getByText("Evidence frame 2 of 50")).toBeVisible();
    expect(document.querySelectorAll("img")).toHaveLength(7);
    expect(screen.queryByTestId("detector-probe-frame-1000")).toBeNull();
    expect(screen.getByTestId("detector-probe-frame-1001")).toBeVisible();
    expect(screen.queryByTestId("detector-probe-frame-1049")).toBeNull();
    expect(
      document.querySelector(`img[src="${frames[0].sourceImageUrl}"]`),
    ).toBeNull();
    expect(
      document.querySelector(`img[src="${frames[49].sourceImageUrl}"]`),
    ).toBeNull();
  });

  it("keeps overlays and zero/nonzero details hidden until every evidence image loads", () => {
    renderPanel({ job: readyJob });
    const images = [...document.querySelectorAll("img")];

    expect(images).toHaveLength(3);
    expect(images[0]).not.toBeVisible();
    expect(
      screen.queryByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeNull();
    expect(screen.queryByText("1 / top 5")).toBeNull();

    fireEvent.load(images[0]);
    fireEvent.load(images[1]);
    fireEvent.error(images[2]);

    expect(images[1]).not.toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "At least one evidence image could not be loaded",
    );
    expect(
      screen.queryByText("x=100, y=80, w=7, h=7 · 0.230 · sports ball"),
    ).toBeNull();
    expect(screen.queryByText(/20–50-frame feasibility check/i)).toBeNull();
  });

  it("does not describe a nonzero box, including a possible false positive, as verified or usable", () => {
    renderPanel({ job: readyJob });

    expect(
      screen.getByText(
        "After every evidence image is verified, any displayed candidate boxes are still unverified detector output, not confirmation that the football was found. T3 annotation determines correctness.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/usable (ball|football) candidate/i)).toBeNull();
    expect(
      screen.queryByText(/verified (ball|football) candidate/i),
    ).toBeNull();
    expect(screen.queryByText(/comparison succeeded/i)).toBeNull();
  });

  it("honestly routes an all-profile zero result toward T3 with lineage-bound retry and no acceptance", async () => {
    const onRetry = vi.fn();
    const { user } = renderPanel({
      job: {
        ...readyJob,
        noProfilesProducedCandidates: true,
        frames: readyJob.frames.map((frame) => ({
          ...frame,
          profiles: frame.profiles.map((profile) => ({
            ...profile,
            rawBoxes: [],
            displayCandidate: null,
            candidateCount: 0,
          })),
        })),
      },
      onRetry,
    });
    loadAllEvidenceImages();

    expect(
      screen
        .getByText(
          "No selected profile produced retained candidate boxes in this bounded comparison.",
        )
        .closest('[role="alert"]'),
    ).toHaveTextContent(
      "No selected profile produced retained candidate boxes",
    );
    expect(screen.getByText(/20–50-frame feasibility check/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /accept trial/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Run bounded comparison" }),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry comparison" }));
    expect(onRetry).toHaveBeenCalledWith({
      retryFromJobId: "probe-job-1",
      profileIds: readyJob.selectedProfileIds,
    });
  });

  it("does not treat a failed profile execution as an all-zero detector result", async () => {
    const onRetry = vi.fn();
    const { user } = renderPanel({
      job: {
        ...readyJob,
        noProfilesProducedCandidates: false,
        frames: [
          {
            ...readyJob.frames[0],
            profiles: [
              readyJob.frames[0].profiles[0],
              {
                ...readyJob.frames[0].profiles[1],
                status: "failed",
                failureCode: "runtime_load_failed",
              },
            ],
          },
        ],
      },
      onRetry,
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "missing complete, successfully executed same-frame evidence",
    );
    expect(screen.queryByText(/20–50-frame feasibility check/i)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry comparison" }));
    expect(onRetry).toHaveBeenCalledWith({
      retryFromJobId: readyJob.jobId,
      profileIds: readyJob.selectedProfileIds,
    });
  });

  it("starts a new root comparison instead of retrying when the terminal selection changes", async () => {
    const extraProfile = {
      ...models[0].profiles[0],
      profileId: "official-coco-yolo11n-low-confidence",
      digest: sha("8"),
      recommended: false,
    };
    const onStart = vi.fn();
    const onRetry = vi.fn();
    const { user } = renderPanel({
      models: [
        { ...models[0], profiles: [...models[0].profiles, extraProfile] },
        ...models.slice(1),
      ],
      job: {
        ...readyJob,
        status: "failed",
        stage: "failed",
        frames: [],
        failureCode: "gpu_oom",
      },
      onStart,
      onRetry,
    });

    await user.click(
      screen.getByRole("checkbox", {
        name: /official-coco-yolo11n-direct/i,
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: /official-coco-yolo11n-low-confidence/i,
      }),
    );
    await user.click(
      screen.getByRole("button", {
        name: "Run a new root comparison with this selection",
      }),
    );

    expect(onRetry).not.toHaveBeenCalled();
    expect(onStart).toHaveBeenCalledWith([
      "official-coco-yolo11s-sahi",
      "official-coco-yolo11n-low-confidence",
    ]);
  });

  it("fails closed when a server-hashed raw overlay is missing", () => {
    renderPanel({
      job: {
        ...readyJob,
        frames: [
          {
            ...readyJob.frames[0],
            profiles: [
              { ...readyJob.frames[0].profiles[0], overlaySha256: "" },
              readyJob.frames[0].profiles[1],
            ],
          },
        ],
      },
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "missing complete, successfully executed same-frame evidence",
    );
    expect(screen.queryByTestId("detector-probe-frame-120")).toBeNull();
  });

  it("keeps catalog failure honest and supports keyboard profile selection", async () => {
    const onReloadCatalog = vi.fn();
    const { user, rerender } = renderPanel({
      models: [],
      catalogState: "failed",
      onReloadCatalog,
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The exact model registry is unavailable",
    );
    await user.click(
      screen.getByRole("button", { name: "Reload model registry" }),
    );
    expect(onReloadCatalog).toHaveBeenCalledOnce();

    rerender(
      <LanguageProvider>
        <ProductionDetectorProbePanel
          models={models}
          catalogState="ready"
          job={null}
          mutationPending={false}
          onStart={vi.fn()}
          onCancel={vi.fn()}
          onRetry={vi.fn()}
        />
      </LanguageProvider>,
    );
    const profile = screen.getByRole("checkbox", {
      name: /official-coco-yolo11n-direct/i,
    });
    profile.focus();
    await user.keyboard(" ");
    expect(profile).not.toBeChecked();
  });

  it("shows start, cancel, or retry failures without inventing a job result", () => {
    renderPanel({ operationError: "HTTP 409: another probe intent is active" });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "another probe intent is active",
    );
    expect(screen.queryByText("probe-job-made-up")).toBeNull();
  });

  it("only allows discard for malformed local pointers", async () => {
    const onRefreshRecovery = vi.fn();
    const onDiscardRecovery = vi.fn();
    const { user, rerender } = renderPanel({
      recoveryError: "HTTP 503",
      recoveryErrorKind: "transport",
      actionsBlocked: true,
      onRefreshRecovery,
      onDiscardRecovery,
    });
    expect(
      screen.queryByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    ).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Refresh current job" }),
    );
    expect(onRefreshRecovery).toHaveBeenCalledOnce();

    rerender(
      <LanguageProvider>
        <ProductionDetectorProbePanel
          models={models}
          catalogState="ready"
          recoveryError="invalid pointer"
          recoveryErrorKind="invalid_pointer"
          job={null}
          mutationPending={false}
          actionsBlocked
          onStart={vi.fn()}
          onCancel={vi.fn()}
          onRetry={vi.fn()}
          onRefreshRecovery={onRefreshRecovery}
          onDiscardRecovery={onDiscardRecovery}
        />
      </LanguageProvider>,
    );
    expect(
      screen.queryByRole("button", { name: "Refresh current job" }),
    ).toBeNull();
    await user.click(
      screen.getByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    );
    expect(onDiscardRecovery).toHaveBeenCalledOnce();
  });

  it("localizes the probe-only boundary and actions in Chinese", () => {
    localStorage.setItem("app-language", "zh");
    try {
      renderPanel({ job: readyJob });
      loadAllEvidenceImages();
      expect(
        screen.getByRole("heading", { name: "模型与有界探针对比" }),
      ).toBeVisible();
      expect(screen.getAllByText("仅限探针；不能用于接受试跑。")).toHaveLength(
        3,
      );
      expect(screen.getAllByText("展示候选")).toHaveLength(2);
      expect(screen.getAllByText("原始叠加图 SHA-256")).toHaveLength(2);
    } finally {
      localStorage.removeItem("app-language");
    }
  });
});
