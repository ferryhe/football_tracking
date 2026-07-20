import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import {
  createSafeBrowserStorage,
  type SafeBrowserStorage,
} from "@/lib/browserStorage";
import {
  buildDetectorProbeRequest,
  detectorProbeStorageKey,
} from "@/lib/productionDetectorProbe";
import { ballAnnotationStorageKey } from "@/lib/productionBallAnnotation";
import {
  DETECTOR_PROBE_FRAME_INDICES,
  DETECTOR_PROBE_PROFILE_IDS,
  detectorProbeCatalogFixture,
  detectorProbeJobFixture,
} from "@/test/detectorProbeFixtures";

import { ProductionDetectorProbeController } from "./ProductionDetectorProbeController";

const sha = (character: string) => character.repeat(64);
const FRAME_INDICES = [...DETECTOR_PROBE_FRAME_INDICES];
const PROFILE_IDS = [...DETECTOR_PROBE_PROFILE_IDS];

function legacyModel(
  modelId: string,
  descriptorDigest: string,
  weightsDigest: string,
) {
  return {
    descriptor: {
      model_id: modelId,
      version: "1.0.0",
      display_name: modelId,
      architecture_family: "yolo11",
      descriptor_sha256: descriptorDigest,
      weights: { sha256: weightsDigest },
      source: {
        project: "Ultralytics assets",
        version: "v8.4.0",
        asset_release: "v8.4.0",
        weight_url: `https://example.invalid/${modelId}.pt`,
        acquisition_method: "pinned local download",
        access_requirement: "pinned_local_file",
      },
      licenses: Object.fromEntries(
        ["dataset", "model", "runtime", "deployment"].map((kind) => [
          kind,
          {
            name: `${kind} license`,
            spdx_id: "LicenseRef-Test",
            reviewed: true,
            approved_for_local_probe: true,
          },
        ]),
      ),
      egress: {
        frames_leave_local_machine: false,
        destination: null,
        operator_consent: "not_required",
      },
      lifecycle_state: "unverified",
    },
    availability: {
      status: "available",
      reason_codes: [],
      observations: Object.fromEntries(
        ["file", "digest", "class_map", "license", "runtime_load"].map(
          (name) => [
            name,
            {
              status: "pass",
              reason: `${name}_passed`,
              ...(name === "runtime_load"
                ? {
                    installed_runtime: {
                      ultralytics: "8.4.31",
                      sahi: "0.11.36",
                      torch: "2.7.1",
                    },
                  }
                : {}),
            },
          ],
        ),
      ),
    },
    selectable_for_probe: true,
    qualification: {
      trial_eligible: false,
      source_segment_qualified: false,
      camera_qualified: false,
    },
  };
}

function legacyCatalogFixture() {
  return {
    schema_version: "1.0",
    artifact_type: "ball_detector_development_v1",
    models: [
      legacyModel("model-a", sha("a"), sha("b")),
      legacyModel("model-b", sha("c"), sha("d")),
    ],
    profiles: [
      {
        profile_id: PROFILE_IDS[0],
        version: "profile-v1",
        model_id: "model-a",
        model_version: "1.0.0",
        profile_sha256: sha("5"),
        mode: "direct",
        settings: {
          confidence_threshold: 0.05,
          image_size: 1280,
          top_k: 5,
        },
        availability: { status: "available", reason_codes: [] },
        selectable_for_probe: true,
        recommended: true,
      },
      {
        profile_id: PROFILE_IDS[1],
        version: "profile-v1",
        model_id: "model-b",
        model_version: "1.0.0",
        profile_sha256: sha("6"),
        mode: "sahi",
        settings: {
          confidence_threshold: 0.03,
          image_size: 1536,
          top_k: 5,
          slice_width: 640,
          slice_height: 640,
          overlap_width_ratio: 0.2,
          overlap_height_ratio: 0.2,
        },
        availability: { status: "available", reason_codes: [] },
        selectable_for_probe: true,
        recommended: true,
      },
    ],
    catalog_findings: [],
  };
}

function legacyProfileEvidence(
  jobId: string,
  frameIndex: number,
  profileId: string,
) {
  return {
    profile_id: profileId,
    profile_sha256: sha(profileId === PROFILE_IDS[0] ? "5" : "6"),
    status: "completed",
    latency_ms: 2.5,
    candidate_count: 0,
    top_k: 5,
    raw_candidates: [],
    display_candidate: null,
    filter_reasons: {},
    failure_code: null,
    raw_overlay_artifact_url: `/api/v1/detector-probes/${jobId}/artifacts/overlay-${frameIndex}-${profileId}`,
    raw_overlay_sha256: sha(profileId === PROFILE_IDS[0] ? "7" : "8"),
    raw_overlay_size_bytes: 90,
  };
}

function legacyJobFixture(
  jobId: string,
  status: "queued" | "running" | "ready" | "cancelled",
  retryFromJobId: string | null = null,
) {
  const tuningBinding = {
    state: "absent",
    schema_version: "1.0",
    version_id: null,
    parent_version_id: null,
    values_sha256:
      "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  };
  const frozenRequest = {
    parent_trial_id: "trial-1",
    source_sha256: sha("1"),
    source_width: 5120,
    source_height: 1440,
    tracking_contract_sha256: sha("2"),
    base_config_relative_path: "config/base.yaml",
    base_config_sha256: sha("3"),
    effective_config_relative_path: "config/generated/trial.yaml",
    effective_config_sha256: sha("4"),
    trial_intent_sha256: sha("9"),
    tuning_patch_binding: tuningBinding,
    tuning_patch_sha256: sha("0"),
    profile_ids: PROFILE_IDS,
    profile_sha256s: {
      [PROFILE_IDS[0]]: sha("5"),
      [PROFILE_IDS[1]]: sha("6"),
    },
    frame_indices: FRAME_INDICES,
    top_k: 5,
  };
  const report =
    status === "ready"
      ? {
          schema_version: "1.0",
          artifact_type: "detector_probe_report",
          job_id: jobId,
          request_sha256: sha("a"),
          top_k: 5,
          source: {
            sha256: sha("1"),
            width: 5120,
            height: 1440,
            tracking_contract_sha256: sha("2"),
          },
          lineage: {
            parent_trial_id: "trial-1",
            base_config_relative_path: "config/base.yaml",
            base_config_sha256: sha("3"),
            effective_config_relative_path: "config/generated/trial.yaml",
            effective_config_sha256: sha("4"),
            trial_intent_sha256: sha("9"),
            tuning_patch_binding: tuningBinding,
            tuning_patch_sha256: sha("0"),
            profile_sha256s: frozenRequest.profile_sha256s,
            retry_from_job_id: retryFromJobId,
          },
          frames: FRAME_INDICES.map((frameIndex) => ({
            frame_index: frameIndex,
            source_width: 5120,
            source_height: 1440,
            source_artifact_url: `/api/v1/detector-probes/${jobId}/artifacts/source-${frameIndex}`,
            source_frame_sha256: sha(frameIndex === 10 ? "b" : "c"),
            source_frame_size_bytes: 100,
            profile_results: PROFILE_IDS.map((profileId) =>
              legacyProfileEvidence(jobId, frameIndex, profileId),
            ),
          })),
        }
      : null;
  return {
    schema_version: "1.0",
    artifact_type: "detector_probe_job",
    job_id: jobId,
    request_sha256: sha("a"),
    status,
    stage: status,
    progress: {
      completed: status === "ready" ? 4 : 0,
      total: 4,
    },
    retry_from_job_id: retryFromJobId,
    frozen_request: frozenRequest,
    frozen_profiles: PROFILE_IDS.map((profileId) => ({
      profile_id: profileId,
      profile_sha256: sha(profileId === PROFILE_IDS[0] ? "5" : "6"),
    })),
    error_code: status === "cancelled" ? "cancelled_by_operator" : null,
    recovery_action: status === "cancelled" ? "Retry explicitly." : null,
    report,
  };
}

function catalogFixture() {
  return detectorProbeCatalogFixture();
}

function jobFixture(
  jobId: string,
  status: "queued" | "running" | "ready" | "cancelled",
  retryFromJobId: string | null = null,
) {
  return detectorProbeJobFixture(jobId, status, retryFromJobId);
}

function storedRecovery(jobId: string) {
  return JSON.stringify({
    state: "job_pointer",
    schema_version: "2.0",
    workflow_id: "workflow-1",
    parent_trial_id: "trial-1",
    job_id: jobId,
    immutable_identity: null,
    expected: {
      request_sha256: sha("e"),
      profile_ids: PROFILE_IDS,
      frame_indices: FRAME_INDICES,
      retry_from_job_id: null,
    },
  });
}

function storedPendingCreate() {
  return {
    state: "pending_create",
    schema_version: "1.0",
    artifact_type: "detector_probe_pending_create",
    workflow_id: "workflow-1",
    parent_trial_id: "trial-1",
    request: buildDetectorProbeRequest({
      parentTrialId: "trial-1",
      profileIds: PROFILE_IDS,
      frameIndices: FRAME_INDICES,
    }),
  };
}

function loadAllEvidenceImages() {
  document.querySelectorAll("img").forEach((image) => fireEvent.load(image));
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function controllerTree(
  parentTrialId: string,
  frameIndices: number[] | null = FRAME_INDICES,
  storageFactory?: React.ComponentProps<
    typeof ProductionDetectorProbeController
  >["storageFactory"],
  workflowId = "workflow-1",
) {
  return (
    <LanguageProvider>
      <ProductionDetectorProbeController
        workflowId={workflowId}
        parentTrialId={parentTrialId}
        onStartNewDevelopmentBatch={vi.fn()}
        {...(frameIndices === null ? {} : { frameIndices })}
        {...(storageFactory ? { storageFactory } : {})}
      />
    </LanguageProvider>
  );
}

function renderController(
  frameIndices: number[] | null = FRAME_INDICES,
  storageFactory?: React.ComponentProps<
    typeof ProductionDetectorProbeController
  >["storageFactory"],
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    user: userEvent.setup(),
    ...render(
      <QueryClientProvider client={queryClient}>
        {controllerTree("trial-1", frameIndices, storageFactory)}
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("ProductionDetectorProbeController", () => {
  it.each([
    ["a non-object", () => []],
    [
      "an invalid pending schema",
      () => ({ ...storedPendingCreate(), schema_version: "latest" }),
    ],
    [
      "a non-array pending profile set",
      () => ({
        ...storedPendingCreate(),
        request: { ...storedPendingCreate().request, profile_ids: "profile-a" },
      }),
    ],
    [
      "a non-string pending profile",
      () => ({
        ...storedPendingCreate(),
        request: {
          ...storedPendingCreate().request,
          profile_ids: [7, PROFILE_IDS[1]],
        },
      }),
    ],
    [
      "a non-number pending frame",
      () => ({
        ...storedPendingCreate(),
        request: { ...storedPendingCreate().request, frame_indices: ["7"] },
      }),
    ],
    [
      "a non-array pending frame set",
      () => ({
        ...storedPendingCreate(),
        request: { ...storedPendingCreate().request, frame_indices: "7" },
      }),
    ],
    [
      "a noncanonical pending request",
      () => ({
        ...storedPendingCreate(),
        request: { ...storedPendingCreate().request, top_k: 4 },
      }),
    ],
    [
      "a non-object pointer expectation",
      () => ({ ...JSON.parse(storedRecovery("probe-invalid")), expected: [] }),
    ],
    [
      "an unexpected pointer expectation field",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.unexpected = true;
        return pointer;
      },
    ],
    [
      "a non-number pointer frame",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.frame_indices = ["7"];
        return pointer;
      },
    ],
    [
      "a non-array pointer frame set",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.frame_indices = "7";
        return pointer;
      },
    ],
    [
      "a non-array pointer profile set",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.profile_ids = "profile-a";
        return pointer;
      },
    ],
    [
      "a non-string pointer profile",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.profile_ids = [7, PROFILE_IDS[1]];
        return pointer;
      },
    ],
    [
      "a noncanonical pointer profile order",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.profile_ids.reverse();
        return pointer;
      },
    ],
    [
      "an invalid pointer request digest",
      () => {
        const pointer = JSON.parse(storedRecovery("probe-invalid"));
        pointer.expected.request_sha256 = "latest";
        return pointer;
      },
    ],
    [
      "an invalid immutable identity",
      () => ({
        ...JSON.parse(storedRecovery("probe-invalid")),
        immutable_identity: 7,
      }),
    ],
  ])("fails closed for $0", async (_name, storedValue) => {
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      JSON.stringify(storedValue()),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/detector-models") {
          return json(catalogFixture());
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    renderController();
    expect(
      await screen.findByText(
        /saved detector-probe recovery pointer is invalid/i,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    ).toBeVisible();
  });

  it("uses generated clients for exact start, cancel, retry, and all-zero evidence", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const jobs = new Map<string, ReturnType<typeof jobFixture>>();
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models" && method === "GET") {
          return json(catalogFixture());
        }
        if (url === "/api/detector-probes" && method === "POST") {
          const body = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          createBodies.push(body);
          const retryFrom =
            (body.retry_from_job_id as string | undefined) ?? null;
          const jobId = retryFrom ? "probe-2" : "probe-1";
          jobs.set(
            jobId,
            jobFixture(jobId, retryFrom ? "ready" : "queued", retryFrom),
          );
          return json(
            {
              job_id: jobId,
              request_sha256: sha("e"),
              status: "queued",
              status_url: `/api/v1/detector-probes/${jobId}`,
              cancel_url: `/api/v1/detector-probes/${jobId}/cancel`,
              retry_from_job_id: retryFrom,
            },
            202,
          );
        }
        const cancel = url.match(/^\/api\/detector-probes\/([^/]+)\/cancel$/);
        if (cancel && method === "POST") {
          const cancelled = jobFixture(cancel[1], "cancelled");
          jobs.set(cancel[1], cancelled);
          return json(cancelled);
        }
        const get = url.match(/^\/api\/detector-probes\/([^/]+)$/);
        if (get && method === "GET" && jobs.has(get[1])) {
          return json(jobs.get(get[1]));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const { user } = renderController();
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    await waitFor(() => expect(createBodies).toHaveLength(1));
    expect(createBodies[0]).toEqual({
      parent_trial_id: "trial-1",
      profile_ids: PROFILE_IDS,
      frame_indices: FRAME_INDICES,
      top_k: 5,
    });
    expect(JSON.stringify(createBodies[0])).not.toContain("model_path");
    expect(await screen.findByText("queued")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Cancel comparison" }));
    expect(await screen.findByText("cancelled")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry comparison" }));

    await waitFor(() => expect(createBodies).toHaveLength(2));
    expect(createBodies[1]).toEqual({
      parent_trial_id: "trial-1",
      profile_ids: PROFILE_IDS,
      frame_indices: FRAME_INDICES,
      top_k: 5,
      retry_from_job_id: "probe-1",
    });
    await waitFor(() =>
      expect(document.querySelectorAll("img").length).toBeGreaterThan(0),
    );
    loadAllEvidenceImages();
    expect(
      await screen.findByText(
        "No selected profile produced retained candidate boxes in this bounded comparison.",
      ),
    ).toBeVisible();
    const ordinaryRetry = screen.getByRole("button", {
      name: "Retry comparison",
    });
    expect(ordinaryRetry).toBeEnabled();
    await user.click(
      screen.getByRole("button", {
        name: "Start development annotation on displayed frames",
      }),
    );
    expect(ordinaryRetry).toBeDisabled();
    expect(
      screen.getByText(/historical evidence and parent\/child lineage/i),
    ).toBeVisible();
    expect(screen.getAllByRole("img", { name: /Source frame/ })).toHaveLength(
      1,
    );
    expect(screen.queryByRole("button", { name: /accept trial/i })).toBeNull();
    expect(
      JSON.parse(
        localStorage.getItem(
          detectorProbeStorageKey("workflow-1", "trial-1"),
        ) ?? "{}",
      ),
    ).toMatchObject({
      schema_version: "2.0",
      job_id: "probe-2",
      immutable_identity: expect.any(String),
    });
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.cache === "no-store"),
    ).toBe(true);
  });

  it("keeps the exact pending create while showing a typed nested 409", async () => {
    const storageKey = detectorProbeStorageKey("workflow-1", "trial-1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes" && init?.method === "POST") {
          return json(
            {
              detail: {
                code: "invalid_parent_tuning_lineage",
                message:
                  "The parent tuning lineage failed canonical validation",
                private_context: "must-not-leak",
              },
            },
            409,
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );

    const { user } = renderController();
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("invalid_parent_tuning_lineage");
    expect(alert).toHaveTextContent(
      "The parent tuning lineage failed canonical validation",
    );
    expect(alert).not.toHaveTextContent("must-not-leak");
    expect(alert).not.toHaveTextContent("[object Object]");
    expect(
      screen.getByRole("button", { name: "Retry the exact create request" }),
    ).toBeVisible();
    expect(JSON.parse(localStorage.getItem(storageKey) ?? "{}")).toEqual(
      storedPendingCreate(),
    );
  });

  it("recovers a stored running job, polls to terminal, then stops polling", async () => {
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      storedRecovery("probe-running"),
    );
    let jobReads = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes/probe-running") {
          jobReads += 1;
          return json(
            jobFixture("probe-running", jobReads === 1 ? "running" : "ready"),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderController();
    expect(await screen.findByText("running")).toBeVisible();
    await waitFor(
      () => expect(document.querySelectorAll("img").length).toBeGreaterThan(0),
      { timeout: 2_500 },
    );
    loadAllEvidenceImages();
    expect(
      await screen.findByText(
        "No selected profile produced retained candidate boxes in this bounded comparison.",
        {},
        { timeout: 2_500 },
      ),
    ).toBeVisible();
    const terminalReadCount = jobReads;
    await new Promise((resolve) => window.setTimeout(resolve, 1_100));
    expect(jobReads).toBe(terminalReadCount);
  });

  it("lets the server select frozen trial frames when no explicit frame set is supplied", async () => {
    const createBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models" && method === "GET") {
          return json(catalogFixture());
        }
        if (url === "/api/detector-probes" && method === "POST") {
          createBodies.push(
            JSON.parse(String(init?.body)) as Record<string, unknown>,
          );
          return json(
            {
              job_id: "probe-server-frames",
              request_sha256: sha("e"),
              status: "queued",
              status_url: "/api/v1/detector-probes/probe-server-frames",
              cancel_url: "/api/v1/detector-probes/probe-server-frames/cancel",
              retry_from_job_id: null,
            },
            202,
          );
        }
        if (
          url === "/api/detector-probes/probe-server-frames" &&
          method === "GET"
        ) {
          return json(jobFixture("probe-server-frames", "queued"));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const { user } = renderController(null);
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    await waitFor(() => expect(createBodies).toHaveLength(1));

    expect(createBodies[0]).toEqual({
      parent_trial_id: "trial-1",
      profile_ids: PROFILE_IDS,
      top_k: 5,
    });
  });

  it("requires an explicit discard before replacing an invalid saved recovery pointer", async () => {
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      "not-json",
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/detector-models") {
          return json(catalogFixture());
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByText(
        /saved detector-probe recovery pointer is invalid/i,
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Run bounded comparison" }),
    ).toBeNull();

    await user.click(
      screen.getByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    );

    expect(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    ).toBeEnabled();
    expect(
      localStorage.getItem(detectorProbeStorageKey("workflow-1", "trial-1")),
    ).toBeNull();
  });

  it("fails closed on a corrupt saved annotation continuation until explicit discard", async () => {
    const jobId = "probe-corrupt-continuation";
    const annotationKey = ballAnnotationStorageKey("workflow-1", jobId);
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      storedRecovery(jobId),
    );
    localStorage.setItem(annotationKey, "not-json");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === `/api/detector-probes/${jobId}`) {
          return json(jobFixture(jobId, "ready"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByText(/saved annotation continuation is invalid/i),
    ).toBeVisible();
    loadAllEvidenceImages();
    const retry = await screen.findByRole("button", {
      name: "Retry comparison",
    });
    const annotate = screen.getByRole("button", {
      name: "Start development annotation on displayed frames",
    });
    expect(retry).toBeDisabled();
    expect(annotate).toBeDisabled();

    await user.click(
      screen.getByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    );

    expect(localStorage.getItem(annotationKey)).toBeNull();
    expect(retry).toBeEnabled();
    expect(annotate).toBeEnabled();
  });

  it("retains a valid pointer across GET 503 and offers explicit refetch without discard", async () => {
    const storageKey = detectorProbeStorageKey("workflow-1", "trial-1");
    const pointer = storedRecovery("probe-transport");
    localStorage.setItem(storageKey, pointer);
    let jobReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes/probe-transport") {
          jobReads += 1;
          return jobReads === 1
            ? json({ detail: "temporarily unavailable" }, 503)
            : json(jobFixture("probe-transport", "queued"));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByRole("button", { name: "Refresh current job" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    ).toBeNull();
    expect(localStorage.getItem(storageKey)).toBe(pointer);
    expect(
      screen.getByRole("button", { name: "Run bounded comparison" }),
    ).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Refresh current job" }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
    expect(localStorage.getItem(storageKey)).not.toBeNull();
  });

  it("retains the job pointer when authoritative response schema is untrusted", async () => {
    const storageKey = detectorProbeStorageKey("workflow-1", "trial-1");
    const pointer = storedRecovery("probe-schema");
    localStorage.setItem(storageKey, pointer);
    let jobReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes/probe-schema") {
          jobReads += 1;
          const job = jobFixture("probe-schema", "queued");
          if (jobReads === 1) {
            delete (job.frozen_request as Record<string, unknown>)[
              "source_file_identity_sha256"
            ];
          }
          return json(job);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByRole("button", { name: "Refresh current job" }),
    ).toBeVisible();
    expect(localStorage.getItem(storageKey)).toBe(pointer);
    expect(
      screen.queryByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    ).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Refresh current job" }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
  });

  it("keeps cancel available for a previously verified running job after a poll 503", async () => {
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      storedRecovery("probe-cancellable"),
    );
    let jobReads = 0;
    const cancelledJobIds: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models") return json(catalogFixture());
        if (
          url === "/api/detector-probes/probe-cancellable" &&
          method === "GET"
        ) {
          jobReads += 1;
          return jobReads === 1
            ? json(jobFixture("probe-cancellable", "running"))
            : json({ detail: "temporarily unavailable" }, 503);
        }
        if (
          url === "/api/detector-probes/probe-cancellable/cancel" &&
          method === "POST"
        ) {
          cancelledJobIds.push("probe-cancellable");
          return json(jobFixture("probe-cancellable", "cancelled"));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const { user } = renderController();
    expect(await screen.findByText("running")).toBeVisible();
    expect(
      await screen.findByRole(
        "button",
        { name: "Refresh current job" },
        {
          timeout: 2_500,
        },
      ),
    ).toBeVisible();
    const cancel = screen.getByRole("button", { name: "Cancel comparison" });
    expect(cancel).toBeEnabled();
    await user.click(cancel);
    await waitFor(() => expect(cancelledJobIds).toEqual(["probe-cancellable"]));
  });

  it("fails closed and keeps recovery when immutable source identity drifts between polls", async () => {
    const storageKey = detectorProbeStorageKey("workflow-1", "trial-1");
    localStorage.setItem(storageKey, storedRecovery("probe-drift"));
    let jobReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes/probe-drift") {
          jobReads += 1;
          const job = jobFixture("probe-drift", "running");
          if (jobReads > 1) {
            job.frozen_request.source_file_identity_sha256 = sha("8");
          }
          return json(job);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderController();
    expect(await screen.findByText("running")).toBeVisible();
    expect(
      await screen.findByText(
        /immutable identity changed between polls/i,
        {},
        {
          timeout: 2_500,
        },
      ),
    ).toBeVisible();
    expect(localStorage.getItem(storageKey)).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Refresh current job" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: "Discard invalid local recovery pointer",
      }),
    ).toBeNull();
  });

  it("retries a malformed create response with the exact same POST body", async () => {
    const createBodies: string[] = [];
    let creates = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes" && method === "POST") {
          creates += 1;
          createBodies.push(String(init?.body));
          return creates === 1
            ? json({ job_id: "malformed" }, 202)
            : json(
                {
                  job_id: "probe-create-retry",
                  request_sha256: sha("e"),
                  status: "queued",
                  status_url: "/api/v1/detector-probes/probe-create-retry",
                  cancel_url:
                    "/api/v1/detector-probes/probe-create-retry/cancel",
                  retry_from_job_id: null,
                },
                202,
              );
        }
        if (url === "/api/detector-probes/probe-create-retry") {
          return json(jobFixture("probe-create-retry", "queued"));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const { user } = renderController();
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Retry the exact create request",
      }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
    expect(createBodies).toHaveLength(2);
    expect(createBodies[1]).toBe(createBodies[0]);
    expect(
      JSON.parse(
        localStorage.getItem(
          detectorProbeStorageKey("workflow-1", "trial-1"),
        ) ?? "{}",
      ),
    ).toMatchObject({
      state: "job_pointer",
      workflow_id: "workflow-1",
      parent_trial_id: "trial-1",
      job_id: "probe-create-retry",
    });
  });

  it("reloads an unresolved durable create intent without auto-POST and resumes exact bytes", async () => {
    const createBodies: string[] = [];
    let creates = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes" && method === "POST") {
          creates += 1;
          createBodies.push(String(init?.body));
          if (creates === 1) throw new Error("connection lost after POST");
          return json(
            {
              job_id: "probe-recovered-create",
              request_sha256: sha("e"),
              status: "queued",
              status_url: "/api/v1/detector-probes/probe-recovered-create",
              cancel_url:
                "/api/v1/detector-probes/probe-recovered-create/cancel",
              retry_from_job_id: null,
            },
            202,
          );
        }
        if (url === "/api/detector-probes/probe-recovered-create") {
          return json(jobFixture("probe-recovered-create", "queued"));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = renderController();
    await first.user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    expect(
      await screen.findByRole("button", {
        name: "Retry the exact create request",
      }),
    ).toBeVisible();
    const durablePending = JSON.parse(
      localStorage.getItem(detectorProbeStorageKey("workflow-1", "trial-1")) ??
        "{}",
    ) as Record<string, unknown>;
    expect(durablePending).toMatchObject({
      state: "pending_create",
      workflow_id: "workflow-1",
      parent_trial_id: "trial-1",
    });
    first.unmount();

    const second = renderController();
    expect(
      await screen.findByRole("button", {
        name: "Retry the exact create request",
      }),
    ).toBeVisible();
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(createBodies).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "Run bounded comparison" }),
    ).toBeDisabled();

    await second.user.click(
      screen.getByRole("button", {
        name: "Retry the exact create request",
      }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
    expect(createBodies).toHaveLength(2);
    expect(createBodies[1]).toBe(createBodies[0]);
  });

  it("locks an ambiguous terminal retry to its exact child-create payload", async () => {
    localStorage.setItem(
      detectorProbeStorageKey("workflow-1", "trial-1"),
      storedRecovery("probe-terminal"),
    );
    const bodies: string[] = [];
    let creates = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes/probe-terminal") {
          return json(jobFixture("probe-terminal", "cancelled"));
        }
        if (url === "/api/detector-probes" && method === "POST") {
          creates += 1;
          bodies.push(String(init?.body));
          return creates === 1
            ? json({ job_id: "malformed-child" }, 202)
            : json(
                {
                  job_id: "probe-terminal-child",
                  request_sha256: sha("e"),
                  status: "queued",
                  status_url: "/api/v1/detector-probes/probe-terminal-child",
                  cancel_url:
                    "/api/v1/detector-probes/probe-terminal-child/cancel",
                  retry_from_job_id: "probe-terminal",
                },
                202,
              );
        }
        if (url === "/api/detector-probes/probe-terminal-child") {
          return json(
            jobFixture("probe-terminal-child", "queued", "probe-terminal"),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const { user } = renderController();
    await user.click(
      await screen.findByRole("button", { name: "Retry comparison" }),
    );
    expect(
      await screen.findByRole("button", {
        name: "Retry the exact create request",
      }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Retry comparison" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Run bounded comparison" }),
    ).toBeDisabled();
    expect(
      screen
        .getAllByRole("checkbox")
        .every((box) => box.hasAttribute("disabled")),
    ).toBe(true);
    await user.click(
      screen.getByRole("button", {
        name: "Retry the exact create request",
      }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
  });

  it.each([
    {
      name: "persistent storage unavailable",
      storage: (): SafeBrowserStorage => ({
        isPersistent: false,
        unavailableReason: "blocked",
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      }),
    },
    {
      name: "pending write degrades storage",
      storage: (): SafeBrowserStorage => {
        let persistent = true;
        return {
          get isPersistent() {
            return persistent;
          },
          unavailableReason: null,
          getItem: () => null,
          setItem: () => {
            persistent = false;
          },
          removeItem: () => undefined,
        };
      },
    },
    {
      name: "pending readback mismatch",
      storage: (): SafeBrowserStorage => {
        let value: string | null = null;
        return {
          isPersistent: true,
          unavailableReason: null,
          getItem: () => value,
          setItem: () => {
            value = "tampered-readback";
          },
          removeItem: (): void => {
            value = null;
          },
        };
      },
    },
  ])("sends zero POSTs when $name", async ({ storage }) => {
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/detector-models") {
          return json(catalogFixture());
        }
        if (init?.method === "POST") posts += 1;
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    const { user } = renderController(FRAME_INDICES, storage);
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    expect(
      await screen.findByText(
        /persistent browser recovery storage is unavailable/i,
      ),
    ).toBeVisible();
    expect(posts).toBe(0);
  });

  it("fails closed with zero POSTs when persistent storage getItem throws", async () => {
    const adapter = createSafeBrowserStorage(() => ({
      getItem: () => {
        throw new Error("storage getter blocked");
      },
      setItem: () => undefined,
      removeItem: () => undefined,
    }));
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/detector-models") {
          return json(catalogFixture());
        }
        if (init?.method === "POST") posts += 1;
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    const { user } = renderController(FRAME_INDICES, () => adapter);
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    expect(
      await screen.findByText(
        /persistent browser recovery storage is unavailable/i,
      ),
    ).toBeVisible();
    expect(adapter.isPersistent).toBe(false);
    expect(posts).toBe(0);
  });

  it("does not resend or claim no job when restored pending storage degrades", async () => {
    const serialized = JSON.stringify({
      state: "pending_create",
      schema_version: "1.0",
      artifact_type: "detector_probe_pending_create",
      workflow_id: "workflow-1",
      parent_trial_id: "trial-1",
      request: buildDetectorProbeRequest({
        parentTrialId: "trial-1",
        profileIds: PROFILE_IDS,
        frameIndices: FRAME_INDICES,
      }),
    });
    let persistent = true;
    const storage: SafeBrowserStorage = {
      get isPersistent() {
        return persistent;
      },
      unavailableReason: null,
      getItem: () => serialized,
      setItem: () => undefined,
      removeItem: () => undefined,
    };
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/detector-models") {
          return json(catalogFixture());
        }
        if (init?.method === "POST") posts += 1;
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    const { user } = renderController(FRAME_INDICES, () => storage);
    expect(
      await screen.findByRole("button", {
        name: "Retry the exact create request",
      }),
    ).toBeVisible();
    persistent = false;
    await user.click(
      screen.getByRole("button", {
        name: "Retry the exact create request",
      }),
    );
    expect(
      await screen.findByText(
        /no new POST was sent; an earlier create result may still be unresolved/i,
      ),
    ).toBeVisible();
    expect(screen.queryByText(/no detector-probe job was started/i)).toBeNull();
    expect(posts).toBe(0);
  });

  it("restores the durable pending intent when job-pointer replacement readback fails", async () => {
    let stored: string | null = null;
    let persistent = true;
    const storage: SafeBrowserStorage = {
      get isPersistent() {
        return persistent;
      },
      unavailableReason: null,
      getItem: () => stored,
      setItem: (_key, value) => {
        if (value.includes('"state":"job_pointer"')) {
          persistent = false;
          return;
        }
        stored = value;
      },
      removeItem: () => {
        stored = null;
      },
    };
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes" && init?.method === "POST") {
          posts += 1;
          return json(
            {
              job_id: "probe-pointer-write-failed",
              request_sha256: sha("e"),
              status: "queued",
              status_url: "/api/v1/detector-probes/probe-pointer-write-failed",
              cancel_url:
                "/api/v1/detector-probes/probe-pointer-write-failed/cancel",
              retry_from_job_id: null,
            },
            202,
          );
        }
        if (url === "/api/detector-probes/probe-pointer-write-failed") {
          return json(jobFixture("probe-pointer-write-failed", "queued"));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );

    const { user } = renderController(FRAME_INDICES, () => storage);
    await user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    expect(await screen.findByText("queued")).toBeVisible();
    await waitFor(() =>
      expect(
        screen.getByText(/will reconcile the returned job identity/i),
      ).toBeVisible(),
    );
    expect(screen.queryByText(/verified job/i)).toBeNull();
    expect(posts).toBe(1);
    await waitFor(() =>
      expect(JSON.parse(stored ?? "{}")).toMatchObject({
        state: "pending_create",
        workflow_id: "workflow-1",
        parent_trial_id: "trial-1",
      }),
    );
  });

  it("ignores an old-scope create response after the parent trial changes", async () => {
    let resolveCreate!: (response: Response) => void;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/detector-models") return json(catalogFixture());
        if (url === "/api/detector-probes" && init?.method === "POST") {
          return pendingResponse;
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );

    const view = renderController();
    await view.user.click(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    );
    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        {controllerTree("trial-2")}
      </QueryClientProvider>,
    );
    resolveCreate(
      json(
        {
          job_id: "probe-old-scope",
          request_sha256: sha("e"),
          status: "queued",
          status_url: "/api/v1/detector-probes/probe-old-scope",
          cancel_url: "/api/v1/detector-probes/probe-old-scope/cancel",
          retry_from_job_id: null,
        },
        202,
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Run bounded comparison" }),
      ).toBeEnabled(),
    );
    expect(
      localStorage.getItem(detectorProbeStorageKey("workflow-1", "trial-2")),
    ).toBeNull();
    expect(
      JSON.parse(
        localStorage.getItem(
          detectorProbeStorageKey("workflow-1", "trial-1"),
        ) ?? "{}",
      ),
    ).toMatchObject({ state: "pending_create", parent_trial_id: "trial-1" });
  });

  it.each([
    ["a new parent trial", "workflow-1", "trial-2", "launch"],
    ["a new workflow", "workflow-2", "trial-1", "error"],
  ])(
    "drops annotation authority synchronously for %s and starts only in the new scope",
    async (_label, nextWorkflowId, nextParentTrialId, oldAuthority) => {
      const oldJobId = "probe-sealed-check";
      const oldAnnotationKey = ballAnnotationStorageKey("workflow-1", oldJobId);
      const oldAnnotationPointer =
        oldAuthority === "launch"
          ? JSON.stringify({
              schema_version: "1.0",
              artifact_type: "ball_annotation_session_pointer",
              state: "session_pointer",
              workflow_id: "workflow-1",
              development_probe_job_ids: [oldJobId],
              locked_profile_id: PROFILE_IDS[0],
              session_id: "sealed-check-session",
              data_role: "check",
            })
          : "not-json";
      localStorage.setItem(
        detectorProbeStorageKey("workflow-1", "trial-1"),
        storedRecovery(oldJobId),
      );
      localStorage.setItem(oldAnnotationKey, oldAnnotationPointer);
      const createBodies: Array<Record<string, unknown>> = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          const method = init?.method ?? "GET";
          if (url === "/api/detector-models") return json(catalogFixture());
          if (url === `/api/detector-probes/${oldJobId}`) {
            return json(jobFixture(oldJobId, "ready"));
          }
          if (url === "/api/detector-probes" && method === "POST") {
            const request = JSON.parse(String(init?.body)) as Record<
              string,
              unknown
            >;
            createBodies.push(request);
            return json(
              {
                job_id: "probe-new-scope",
                request_sha256: sha("e"),
                status: "queued",
                status_url: "/api/v1/detector-probes/probe-new-scope",
                cancel_url: "/api/v1/detector-probes/probe-new-scope/cancel",
                retry_from_job_id: null,
              },
              202,
            );
          }
          if (url === "/api/detector-probes/probe-new-scope") {
            const nextJob = jobFixture("probe-new-scope", "queued");
            nextJob.frozen_request.parent_trial_id = nextParentTrialId;
            return json(nextJob);
          }
          if (url.startsWith("/api/ball-annotation-sessions/")) {
            return json({ detail: "sealed session stays historical" }, 409);
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        }),
      );

      const view = renderController();
      if (oldAuthority === "launch") {
        expect(
          await screen.findByTestId("ball-annotation-setup"),
        ).toBeVisible();
      } else {
        expect(
          await screen.findByText(/saved annotation continuation is invalid/i),
        ).toBeVisible();
      }

      view.rerender(
        <QueryClientProvider client={view.queryClient}>
          {controllerTree(
            nextParentTrialId,
            FRAME_INDICES,
            undefined,
            nextWorkflowId,
          )}
        </QueryClientProvider>,
      );

      expect(
        screen.queryByTestId("ball-annotation-setup"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText(/saved annotation continuation is invalid/i),
      ).not.toBeInTheDocument();
      const start = await screen.findByRole("button", {
        name: "Run bounded comparison",
      });
      expect(start).toBeEnabled();
      await view.user.click(start);
      await waitFor(() => expect(createBodies).toHaveLength(1));
      expect(createBodies[0]).toMatchObject({
        parent_trial_id: nextParentTrialId,
      });
      expect(
        localStorage.getItem(
          detectorProbeStorageKey(nextWorkflowId, nextParentTrialId),
        ),
      ).toContain('"job_id":"probe-new-scope"');
      expect(localStorage.getItem(oldAnnotationKey)).toBe(oldAnnotationPointer);
    },
  );

  it("offers an explicit catalog refetch before enabling model actions", async () => {
    let catalogReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/detector-models") {
          catalogReads += 1;
          return catalogReads === 1
            ? json({ detail: "temporarily unavailable" }, 503)
            : json(catalogFixture());
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByRole("button", { name: "Reload model registry" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Run bounded comparison" }),
    ).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Reload model registry" }),
    );
    expect(
      await screen.findByRole("button", { name: "Run bounded comparison" }),
    ).toBeEnabled();
  });

  it("keeps a malformed catalog untrusted until explicit reload succeeds", async () => {
    let catalogReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) !== "/api/detector-models") {
          throw new Error(`Unexpected request: ${String(input)}`);
        }
        catalogReads += 1;
        return catalogReads === 1
          ? json({
              schema_version: "1.0",
              artifact_type: "ball_detector_development_v1",
              models: "not-a-list",
              profiles: [],
              catalog_findings: [],
            })
          : json(catalogFixture());
      }),
    );

    const { user } = renderController();
    expect(
      await screen.findByRole("button", { name: "Reload model registry" }),
    ).toBeVisible();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    await user.click(
      screen.getByRole("button", { name: "Reload model registry" }),
    );
    expect(await screen.findAllByRole("checkbox")).toHaveLength(2);
  });
});
