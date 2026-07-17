import assert from "node:assert/strict";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import {
  act,
  create,
  type ReactTestInstance,
  type ReactTestRenderer,
} from "react-test-renderer";

import { FieldPreviewCanvas } from "@/components/FieldPreviewCanvas";
import { BroadcastRenderStep } from "@/components/broadcast/BroadcastRenderStep";
import { BroadcastReviewStep } from "@/components/broadcast/BroadcastReviewStep";
import { LanguageProvider } from "@/contexts/LanguageContext";
import type {
  BroadcastReviewCandidate,
  BroadcastReviewWindowsResponse,
  FieldPreviewResponse,
  RunRecord,
} from "@workspace/api-client-react";

(
  globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;
Object.defineProperty(globalThis, "document", {
  configurable: true,
  value: {
    activeElement: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  },
});

let passed = 0;

async function test(name: string, body: () => Promise<void>): Promise<void> {
  try {
    await body();
    passed += 1;
  } catch (error) {
    throw new Error(`broadcast component test failed: ${name}`, {
      cause: error,
    });
  }
}

function reviewCandidate(): BroadcastReviewCandidate {
  return {
    candidate_id: "candidate-1",
    candidate_fingerprint: "a".repeat(64),
    variant_id: "full",
    frame_index: 100,
    bbox: [1, 2, 3, 4],
    detector_source: "detector",
    detector_confidence: 0.8,
    predicted_label: "match_ball",
    prediction_confidence: 0.7,
    selective_decision: "abstain",
    review_kind: "policy_abstention",
    evidence: {
      sample_id: "sample-1",
      sha256: "b".repeat(64),
      dataset_version: "c".repeat(64),
      artifacts: {
        tight_tensor: {
          path: "samples/sample-1/tight.npy",
          sha256: "d".repeat(64),
          size_bytes: 12,
        },
        context_tensor: {
          path: "samples/sample-1/context.npy",
          sha256: "e".repeat(64),
          size_bytes: 34,
        },
        review_montage: {
          path: "samples/sample-1/review_montage.png",
          sha256: "f".repeat(64),
          size_bytes: 56,
        },
      },
    },
  };
}

function reviewResponse(
  candidates: BroadcastReviewCandidate[],
  queueSha256 = "1".repeat(64),
): BroadcastReviewWindowsResponse {
  return {
    run_id: "broadcast-parent",
    status: "ready",
    queue_sha256: queueSha256,
    review_item_count: candidates.length === 0 ? 0 : 1,
    items:
      candidates.length === 0
        ? []
        : [
            {
              review_item_id: "window-1",
              variant_id: "full",
              start_frame: 0,
              end_frame: 100,
              duration_seconds: 5,
              compliance: "compliant",
              priority: 1,
              candidates,
            },
          ],
  };
}

function submitButton(renderer: ReactTestRenderer) {
  const buttons = renderer.root.findAllByType("button");
  const button = buttons.find((candidate) => {
    const text = candidate.children.filter(
      (child): child is string => typeof child === "string",
    );
    return text.some(
      (value) =>
        value.includes("Submit review decisions") ||
        value.includes("Continue without manual review"),
    );
  });
  assert.ok(button, "expected review submit button");
  return button;
}

function retryEvidenceButton(renderer: ReactTestRenderer) {
  const button = renderer.root
    .findAllByType("button")
    .find((candidate) =>
      candidate.children.some(
        (child) =>
          typeof child === "string" && child.includes("Retry evidence"),
      ),
    );
  assert.ok(button, "expected evidence retry button");
  return button;
}

await test("review evidence must load before decisions can submit", async () => {
  const candidate = reviewCandidate();
  let submitted = 0;
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <BroadcastReviewStep
        response={reviewResponse([candidate])}
        montageUrlsByCandidateId={{ "candidate-1": "/verified.png" }}
        decisions={[{ candidate_id: "candidate-1", action: "confirm_ball" }]}
        onDecisionsChange={() => undefined}
        onSubmit={() => {
          submitted += 1;
        }}
      />,
    );
  });

  assert.equal(submitButton(renderer).props.disabled, true);
  const image = renderer.root.findByType("img");
  await act(async () => image.props.onLoad());
  assert.equal(submitButton(renderer).props.disabled, false);
  await act(async () => submitButton(renderer).props.onClick());
  assert.equal(submitted, 1);

  await act(async () => image.props.onError());
  assert.equal(submitButton(renderer).props.disabled, true);
  assert.equal(renderer.root.findAllByType("img").length, 0);
  await act(async () => retryEvidenceButton(renderer).props.onClick());
  const retriedImage = renderer.root.findByType("img");
  assert.equal(submitButton(renderer).props.disabled, true);
  await act(async () => retriedImage.props.onLoad());
  assert.equal(submitButton(renderer).props.disabled, false);
  await act(async () => renderer.unmount());
});

await test("same URL in a new queue requires fresh bound evidence", async () => {
  const candidate = reviewCandidate();
  const props = {
    montageUrlsByCandidateId: { "candidate-1": "/verified.png" },
    decisions: [
      { candidate_id: "candidate-1", action: "confirm_ball" },
    ] as const,
    onDecisionsChange: () => undefined,
    onSubmit: () => undefined,
  };
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <BroadcastReviewStep {...props} response={reviewResponse([candidate])} />,
    );
  });

  const firstImage = renderer.root.findByType("img");
  const firstSrc = firstImage.props.src;
  const staleOnLoad = firstImage.props.onLoad;
  const staleOnError = firstImage.props.onError;
  await act(async () => firstImage.props.onLoad());
  assert.equal(submitButton(renderer).props.disabled, false);

  await act(async () => {
    renderer.update(
      <BroadcastReviewStep
        {...props}
        response={reviewResponse([candidate], "2".repeat(64))}
      />,
    );
  });
  const secondImage = renderer.root.findByType("img");
  assert.notEqual(secondImage, firstImage);
  assert.notEqual(secondImage.props.src, firstSrc);
  assert.match(secondImage.props.src, /broadcast_evidence=/);
  assert.equal(submitButton(renderer).props.disabled, true);

  await act(async () => staleOnLoad());
  assert.equal(submitButton(renderer).props.disabled, true);
  await act(async () => staleOnError());
  assert.equal(renderer.root.findAllByType("img").length, 1);
  assert.equal(submitButton(renderer).props.disabled, true);

  await act(async () => secondImage.props.onLoad());
  assert.equal(submitButton(renderer).props.disabled, false);
  await act(async () => staleOnError());
  assert.equal(submitButton(renderer).props.disabled, false);
  await act(async () => renderer.unmount());
});

await test("zero-candidate queue can continue with an exact empty decision list", async () => {
  let submitted: unknown = null;
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <BroadcastReviewStep
        response={reviewResponse([])}
        montageUrlsByCandidateId={{}}
        decisions={[]}
        onDecisionsChange={() => undefined}
        onSubmit={(decisions) => {
          submitted = decisions;
        }}
      />,
    );
  });
  const button = submitButton(renderer);
  assert.equal(button.props.disabled, false);
  await act(async () => button.props.onClick());
  assert.deepEqual(submitted, []);
  await act(async () => renderer.unmount());
});

function renderedText(node: ReactTestInstance): string {
  return node.children
    .map((child) => (typeof child === "string" ? child : renderedText(child)))
    .join("");
}

await test("rendering helper text uses the accessible foreground token", async () => {
  const trajectoryGenerationId = `trajectory-${"a".repeat(24)}`;
  const parent: RunRecord = {
    run_id: "broadcast-parent",
    source: "broadcast_hybrid",
    status: "completed",
    created_at: "2026-07-15T18:00:00Z",
    output_dir: "outputs/broadcast-parent",
    broadcast: {
      status: "trajectory_ready",
      trajectory_generation_id: trajectoryGenerationId,
    },
  };
  const operation: RunRecord = {
    run_id: "render-child",
    source: "broadcast_hybrid_render",
    status: "running",
    created_at: "2026-07-15T18:01:00Z",
    output_dir: "outputs/render-child",
    parent_run_id: parent.run_id,
    progress: { stage: "render", percent: 35 },
    broadcast: {
      operation: "render",
      operation_status: "running",
      parent_run_id: parent.run_id,
    },
  };
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <BroadcastRenderStep
        run={parent}
        operationRun={operation}
        trajectoryGenerationId={trajectoryGenerationId}
        onRender={() => undefined}
        isRendering
      />,
    );
  });

  for (const text of [
    "Render the reviewed trajectory, inspect the final video, and download its evidence.",
    "Ready",
    "running",
    "Waiting",
    "35.0%",
    "Allowed range: 320–7680 × 180–4320",
  ]) {
    const matching = renderer.root.findAll(
      (node) =>
        typeof node.props.className === "string" && renderedText(node) === text,
    );
    assert.ok(matching.length > 0, `missing rendered text: ${text}`);
    for (const node of matching) {
      assert.match(node.props.className, /\btext-foreground\b/);
      assert.doesNotMatch(node.props.className, /\btext-muted-foreground\b/);
    }
  }
  await act(async () => renderer.unmount());
});

class FakeImage {
  static instances: FakeImage[] = [];

  naturalWidth = 640;
  naturalHeight = 360;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  src = "";

  constructor() {
    FakeImage.instances.push(this);
  }
}

function preview(video: string, frame: number): FieldPreviewResponse {
  return {
    input_video: video,
    preview_data_url: `data:image/jpeg;base64,${video}-${frame}`,
    frame_width: 640,
    frame_height: 360,
    frame_index: frame,
    frame_time_seconds: frame / 20,
    sample_index: frame + 1,
    sample_count: 10,
  };
}

await test("late image decode cannot draw or confirm a replaced preview", async () => {
  const originalImage = globalThis.Image;
  const originalLocalStorage = globalThis.localStorage;
  Object.defineProperty(globalThis, "Image", {
    configurable: true,
    value: FakeImage,
  });
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => "en",
      setItem: () => undefined,
    },
  });
  FakeImage.instances = [];

  const drawn: FakeImage[] = [];
  const ready: boolean[] = [];
  const context = {
    clearRect: () => undefined,
    drawImage: (image: FakeImage) => drawn.push(image),
    setLineDash: () => undefined,
    beginPath: () => undefined,
    moveTo: () => undefined,
    lineTo: () => undefined,
    closePath: () => undefined,
    fill: () => undefined,
    stroke: () => undefined,
    fillText: () => undefined,
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => context,
  };
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => {
      renderer = create(
        <QueryClientProvider client={client}>
          <LanguageProvider>
            <FieldPreviewCanvas
              inputVideo="video-a.mp4"
              preview={preview("video-a.mp4", 0)}
              onPreviewChange={() => undefined}
              onPreviewReadyChange={(value) => ready.push(value)}
              autoFetch={false}
            />
          </LanguageProvider>
        </QueryClientProvider>,
        {
          createNodeMock: (element) =>
            element.type === "canvas" ? canvas : null,
        },
      );
    });
    const firstImage = FakeImage.instances.at(-1);
    assert.ok(firstImage?.onload);
    const staleOnload = firstImage.onload;

    await act(async () => {
      renderer.update(
        <QueryClientProvider client={client}>
          <LanguageProvider>
            <FieldPreviewCanvas
              inputVideo="video-b.mp4"
              preview={preview("video-b.mp4", 1)}
              onPreviewChange={() => undefined}
              onPreviewReadyChange={(value) => ready.push(value)}
              autoFetch={false}
              navigationDisabled
            />
          </LanguageProvider>
        </QueryClientProvider>,
      );
    });
    const currentImage = FakeImage.instances.at(-1);
    assert.ok(currentImage?.onload);
    assert.notEqual(currentImage, firstImage);

    await act(async () => staleOnload());
    assert.deepEqual(drawn, []);
    assert.notEqual(ready.at(-1), true);

    await act(async () => currentImage.onload?.());
    assert.deepEqual(drawn, [currentImage]);
    assert.equal(ready.at(-1), true);
    const nextButton = renderer.root.findByProps({
      "data-testid": "button-next-frame",
    });
    assert.equal(nextButton.props.disabled, true);
    await act(async () => renderer.unmount());
  } finally {
    Object.defineProperty(globalThis, "Image", {
      configurable: true,
      value: originalImage,
    });
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: originalLocalStorage,
    });
  }
});

console.log(`broadcastComponents: ${passed} tests passed`);
