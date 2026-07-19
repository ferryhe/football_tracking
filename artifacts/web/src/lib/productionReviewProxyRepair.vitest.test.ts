import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cancelReviewProxyRepair,
  createReviewProxyRepair,
  getReviewProxyRepair,
  parseReviewProxyRepairJob,
  requireReviewProxyRepairRetryLineage,
  retryReviewProxyRepair,
  ReviewProxyRepairRequestError,
  reviewProxyRepairUpdateIsCurrent,
} from "./productionReviewProxyRepair";

const sha = (character: string) => character.repeat(64);

function repairJobFixture(status: string = "running") {
  const ready = status === "ready";
  const stage =
    status === "committing"
      ? "proxy_ready"
      : status === "running"
        ? "transcoding"
        : status;
  const stageCompleted = stage === "proxy_ready" ? 1 : ready ? 6 : 0;
  const failed = status === "failed" || status === "blocked";
  return {
    schema_version: "1.0",
    artifact_type: "detector_review_proxy_repair_job",
    repair_id: "proxy-repair-1",
    attempt_root_repair_id: "proxy-repair-1",
    attempt_number: 1,
    retry_from_repair_id: null,
    idempotency_key: sha("1"),
    request_sha256: sha("1"),
    status,
    stage,
    preset_id: "h264-cfr-720p-v1",
    eligibility: {
      eligible: true,
      action: "generate_verified_review_proxy",
      blocker_code: "review_proxy_required",
    },
    authority: {
      blocked_session_id: "annotation-blocked-1",
      blocked_session_request_sha256: sha("2"),
      blocked_session_record_sha256: sha("3"),
      parent_probe_job_id: "probe-parent-17",
      development_probe_job_ids: ["probe-parent-17"],
      parent_probe_request_sha256: sha("4"),
      parent_probe_intent_sha256: sha("5"),
      parent_probe_semantic_intent_sha256: sha("6"),
      parent_probe_report_sha256: sha("7"),
      parent_probe_result_manifest_sha256: sha("8"),
      parent_probe_record_sha256: sha("9"),
      parent_execution_bundle_sha256: sha("a"),
      parent_runtime_environment_sha256: sha("b"),
      source_frame_evidence_sha256: sha("c"),
      source_id: "source-one",
      source_sha256: sha("d"),
      source_file_identity_sha256: sha("e"),
      source_size_bytes: 4096,
      source_width: 5120,
      source_height: 1440,
      source_frame_count: 100,
      source_fps: 20,
      locked_profile_id: "official-coco-yolo11s-sahi",
      locked_profile_sha256: sha("f"),
      frame_indices: [10, 20],
      sampling_manifest_sha256: sha("0"),
      temporal_groups_sha256: sha("1"),
      candidate_evidence_sha256: sha("2"),
      replacement_request_authority_sha256: sha("3"),
    },
    progress: {
      stage_completed: stageCompleted,
      stage_total: 6,
      source_frames_completed:
        stageCompleted >= 1 ? 100 : status === "running" ? 50 : 0,
      source_frames_total: 100,
      updated_at: ready ? "2026-07-18T17:10:00Z" : "2026-07-18T17:00:00Z",
    },
    can_cancel: status === "queued" || status === "running",
    can_retry: failed || status === "cancelled",
    result: ready
      ? {
          proxy: {
            review_proxy_id: "review-proxy-1",
            review_proxy_manifest_sha256: sha("e"),
            proxy_media_sha256: sha("f"),
            proxy_size_bytes: 2048,
            proxy_width: 2560,
            proxy_height: 720,
            proxy_frame_count: 100,
            proxy_fps: 20,
            mapping_sha256: sha("0"),
            sampled_artifact_count: 2,
            encoder_binding_sha256: sha("1"),
            repair_execution_binding_sha256: sha("2"),
            repair_code_bundle_sha256: sha("3"),
            repair_runtime_sha256: sha("4"),
            repair_decoder_fingerprint_sha256: sha("5"),
          },
          child_probe: {
            job_id: "probe-child-18",
            request_sha256: sha("2"),
            intent_sha256: sha("3"),
            semantic_intent_sha256: sha("4"),
            resource_sha256: sha("5"),
            frozen_profiles_sha256: sha("6"),
            report_sha256: sha("7"),
            result_manifest_sha256: sha("8"),
            execution_bundle_sha256: sha("9"),
            runtime_environment_sha256: sha("a"),
            continuation_execution_binding_sha256: sha("b"),
            continuation_code_bundle_sha256: sha("c"),
            continuation_runtime_sha256: sha("d"),
            retry_from_job_id: "probe-parent-17",
            retry_kind: "review_proxy_decode_upgrade",
            status_url: "/api/v1/detector-probes/probe-child-18",
            report_url: "/api/v1/detector-probes/probe-child-18",
          },
          replacement_session: {
            session_id: "annotation-replacement-1",
            request_sha256: sha("9"),
            status: "annotating",
            retry_from_session_id: "annotation-blocked-1",
            retry_mode: "review_proxy_decode_upgrade",
            attempt_family_sha256: sha("a"),
            development_probe_job_ids: ["probe-parent-17", "probe-child-18"],
            status_url:
              "/api/v1/ball-annotation-sessions/annotation-replacement-1",
          },
          parent_probe_record_sha256_after: sha("9"),
        }
      : null,
    error_code: failed ? "review_proxy_failed" : null,
    blocker_code: status === "blocked" ? "review_proxy_failed" : null,
    recovery_action: failed ? "retry" : null,
    created_at: "2026-07-18T16:59:00Z",
    updated_at: ready ? "2026-07-18T17:10:00Z" : "2026-07-18T17:00:00Z",
    status_url: "/api/v1/detector-review-proxy-repairs/proxy-repair-1",
    cancel_url: "/api/v1/detector-review-proxy-repairs/proxy-repair-1/cancel",
    retry_url: "/api/v1/detector-review-proxy-repairs/proxy-repair-1/retry",
  };
}

function retryJobFixture(status: string = "queued") {
  const job: any = repairJobFixture(status);
  job.repair_id = "proxy-repair-2";
  job.attempt_root_repair_id = "proxy-repair-1";
  job.attempt_number = 2;
  job.retry_from_repair_id = "proxy-repair-1";
  job.idempotency_key = sha("2");
  job.request_sha256 = sha("2");
  job.status_url = "/api/v1/detector-review-proxy-repairs/proxy-repair-2";
  job.cancel_url = `${job.status_url}/cancel`;
  job.retry_url = `${job.status_url}/retry`;
  return job;
}

describe("review-proxy repair authority", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("parses the active server-owned job and its bounded progress", () => {
    expect(parseReviewProxyRepairJob(repairJobFixture())).toEqual(
      expect.objectContaining({
        repairId: "proxy-repair-1",
        attemptRootRepairId: "proxy-repair-1",
        attemptNumber: 1,
        retryFromRepairId: null,
        status: "running",
        canCancel: true,
        canRetry: false,
        authority: expect.objectContaining({
          blockedSessionId: "annotation-blocked-1",
          parentProbeJobId: "probe-parent-17",
          frameIndices: [10, 20],
        }),
        progress: {
          stageCompleted: 0,
          stageTotal: 6,
          sourceFramesCompleted: 50,
          sourceFramesTotal: 100,
          updatedAt: "2026-07-18T17:00:00Z",
        },
      }),
    );
  });

  it("parses the immutable parent-child-proxy-replacement continuation", () => {
    const ready = parseReviewProxyRepairJob(repairJobFixture("ready"));

    expect(ready.result).toEqual(
      expect.objectContaining({
        parentProbeRecordSha256After: sha("9"),
        proxy: expect.objectContaining({
          reviewProxyManifestSha256: sha("e"),
          mappingSha256: sha("0"),
        }),
        childProbe: expect.objectContaining({
          jobId: "probe-child-18",
          retryFromJobId: "probe-parent-17",
          resourceSha256: sha("5"),
          statusUrl: "/api/v1/detector-probes/probe-child-18",
        }),
        replacementSession: expect.objectContaining({
          sessionId: "annotation-replacement-1",
          retryFromSessionId: "annotation-blocked-1",
          developmentProbeJobIds: ["probe-parent-17", "probe-child-18"],
          statusUrl:
            "/api/v1/ball-annotation-sessions/annotation-replacement-1",
        }),
      }),
    );
  });

  it.each([
    ["unknown field", (job: any) => (job.untrusted = true)],
    [
      "changed parent bytes",
      (job: any) => (job.result.parent_probe_record_sha256_after = sha("f")),
    ],
    [
      "changed parent lineage",
      (job: any) => (job.result.child_probe.retry_from_job_id = "other-parent"),
    ],
    [
      "changed replacement lineage",
      (job: any) =>
        (job.result.replacement_session.retry_from_session_id =
          "other-session"),
    ],
    ["result before ready", (job: any) => (job.status = "running")],
  ])("fails closed on %s", (_name, tamper) => {
    const job = repairJobFixture("ready");
    tamper(job);
    expect(() => parseReviewProxyRepairJob(job)).toThrow();
  });

  it.each<[string, (job: any) => void]>([
    [
      "same-length top-level key substitution",
      (job) => {
        delete job.stage;
        job.forged_stage = "running";
      },
    ],
    ["non-string repair ID", (job) => (job.repair_id = 7)],
    ["unsafe repair ID", (job) => (job.repair_id = "../repair")],
    ["non-string request digest", (job) => (job.request_sha256 = 7)],
    ["non-lowercase request digest", (job) => (job.request_sha256 = sha("A"))],
    [
      "fractional source frame count",
      (job) => (job.authority.source_frame_count = 1.5),
    ],
    [
      "zero source frame count",
      (job) => (job.authority.source_frame_count = 0),
    ],
    ["non-numeric FPS", (job) => (job.authority.source_fps = "20")],
    ["non-finite FPS", (job) => (job.authority.source_fps = Number.NaN)],
    ["non-positive FPS", (job) => (job.authority.source_fps = 0)],
    ["empty stage", (job) => (job.stage = "")],
    ["non-string stage", (job) => (job.stage = 1)],
    ["invalid timestamp", (job) => (job.created_at = "not-a-time")],
    ["unknown status", (job) => (job.status = "paused")],
    ["non-array frame indices", (job) => (job.authority.frame_indices = null)],
    ["empty frame indices", (job) => (job.authority.frame_indices = [])],
    [
      "too many frame indices",
      (job) => {
        job.authority.source_frame_count = 1000;
        job.authority.frame_indices = Array.from(
          { length: 51 },
          (_, index) => index,
        );
      },
    ],
    [
      "out-of-range frame index",
      (job) => (job.authority.frame_indices = [10, 100]),
    ],
    [
      "unordered frame indices",
      (job) => (job.authority.frame_indices = [20, 10]),
    ],
    [
      "duplicate frame indices",
      (job) => (job.authority.frame_indices = [10, 10]),
    ],
    ["fractional frame index", (job) => (job.authority.frame_indices = [10.5])],
    ["wrong schema version", (job) => (job.schema_version = "2.0")],
    ["wrong artifact type", (job) => (job.artifact_type = "forged")],
    ["wrong preset", (job) => (job.preset_id = "unbounded")],
    ["missing attempt root", (job) => delete job.attempt_root_repair_id],
    [
      "wrong first-attempt root",
      (job) => (job.attempt_root_repair_id = "proxy-repair-other"),
    ],
    ["boolean attempt number", (job) => (job.attempt_number = true)],
    ["missing first-attempt parent", (job) => (job.retry_from_repair_id = "x")],
    ["changed idempotency digest", (job) => (job.idempotency_key = sha("2"))],
    ["ineligible repair", (job) => (job.eligibility.eligible = false)],
    ["wrong repair action", (job) => (job.eligibility.action = "retry_probe")],
    [
      "wrong eligibility blocker",
      (job) => (job.eligibility.blocker_code = "other"),
    ],
    [
      "completed stages beyond total",
      (job) => (job.progress.stage_completed = 7),
    ],
    ["wrong stage total", (job) => (job.progress.stage_total = 4)],
    [
      "stale progress timestamp",
      (job) => (job.progress.updated_at = "2026-07-18T16:59:59Z"),
    ],
    [
      "completed frames beyond total",
      (job) => (job.progress.source_frames_completed = 101),
    ],
    [
      "progress total differs from source",
      (job) => (job.progress.source_frames_total = 99),
    ],
    ["non-boolean cancellation", (job) => (job.can_cancel = "yes")],
    ["non-boolean retry", (job) => (job.can_retry = 1)],
    ["wrong active cancellation", (job) => (job.can_cancel = false)],
    ["wrong active retry", (job) => (job.can_retry = true)],
    [
      "ready without result",
      (job) => {
        job.status = "ready";
        job.can_cancel = false;
      },
    ],
    [
      "failure without error",
      (job) => {
        job.status = "failed";
        job.can_cancel = false;
      },
    ],
    ["active job with error", (job) => (job.error_code = "unexpected")],
    [
      "non-blocked job with blocker",
      (job) => (job.blocker_code = "unexpected"),
    ],
    ["wrong status URL", (job) => (job.status_url = "/api/v1/forged")],
    ["wrong cancel URL", (job) => (job.cancel_url = "/api/v1/forged/cancel")],
    ["wrong retry URL", (job) => (job.retry_url = "/api/v1/forged/retry")],
  ])("rejects invalid scalar/lifecycle authority: %s", (_name, tamper) => {
    const job = repairJobFixture();
    tamper(job);
    expect(() => parseReviewProxyRepairJob(job)).toThrow();
  });

  it.each<[string, (job: any) => void]>([
    [
      "blocked-session record binding",
      (job) => delete job.authority.blocked_session_record_sha256,
    ],
    [
      "parent semantic-intent binding",
      (job) => delete job.authority.parent_probe_semantic_intent_sha256,
    ],
    [
      "parent execution binding",
      (job) => delete job.authority.parent_execution_bundle_sha256,
    ],
    [
      "parent runtime binding",
      (job) => delete job.authority.parent_runtime_environment_sha256,
    ],
    [
      "source-frame evidence binding",
      (job) => delete job.authority.source_frame_evidence_sha256,
    ],
    [
      "replacement-request authority binding",
      (job) => delete job.authority.replacement_request_authority_sha256,
    ],
    [
      "development probe lineage",
      (job) => (job.authority.development_probe_job_ids = ["probe-other"]),
    ],
  ])("rejects missing or tampered current authority: %s", (_name, tamper) => {
    const job = repairJobFixture();
    tamper(job);
    expect(() => parseReviewProxyRepairJob(job)).toThrow();
  });

  it.each<[string, (job: any) => void]>([
    [
      "non-array replacement lineage",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = null;
      },
    ],
    [
      "short replacement lineage",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = [
          "probe-parent-17",
        ];
      },
    ],
    [
      "long replacement lineage",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = Array.from(
          { length: 9 },
          (_, index) => `probe-${index}`,
        );
      },
    ],
    [
      "duplicate replacement lineage",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = [
          "probe-parent-17",
          "probe-parent-17",
        ];
      },
    ],
    [
      "unsafe replacement lineage ID",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = [
          "probe-parent-17",
          "../child",
        ];
      },
    ],
    ["wrong proxy width", (job) => (job.result.proxy.proxy_width = 1280)],
    ["wrong proxy height", (job) => (job.result.proxy.proxy_height = 1080)],
    [
      "wrong proxy frame count",
      (job) => (job.result.proxy.proxy_frame_count = 99),
    ],
    ["wrong proxy FPS", (job) => (job.result.proxy.proxy_fps = 19.5)],
    [
      "wrong sampled artifact count",
      (job) => (job.result.proxy.sampled_artifact_count = 1),
    ],
    [
      "wrong child retry kind",
      (job) => (job.result.child_probe.retry_kind = "ordinary"),
    ],
    [
      "wrong replacement status",
      (job) => (job.result.replacement_session.status = "blocked"),
    ],
    [
      "wrong replacement retry mode",
      (job) => {
        job.result.replacement_session.retry_mode = "same_authority";
      },
    ],
    [
      "wrong final parent digest",
      (job) => {
        job.result.parent_probe_record_sha256_after = sha("6");
      },
    ],
    [
      "wrong child parent",
      (job) => {
        job.result.child_probe.retry_from_job_id = "probe-other";
      },
    ],
    [
      "wrong replacement parent",
      (job) => {
        job.result.replacement_session.retry_from_session_id =
          "annotation-other";
      },
    ],
    [
      "missing parent at lineage tail",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = [
          "probe-other",
          "probe-child-18",
        ];
      },
    ],
    [
      "missing child at lineage tail",
      (job) => {
        job.result.replacement_session.development_probe_job_ids = [
          "probe-parent-17",
          "probe-other",
        ];
      },
    ],
    ["unknown proxy result field", (job) => (job.result.proxy.forged = true)],
    [
      "missing proxy execution binding",
      (job) => delete job.result.proxy.repair_execution_binding_sha256,
    ],
    [
      "unknown child result field",
      (job) => (job.result.child_probe.forged = true),
    ],
    [
      "missing child resource binding",
      (job) => delete job.result.child_probe.resource_sha256,
    ],
    [
      "wrong child status URL",
      (job) => (job.result.child_probe.status_url = "/api/v1/forged"),
    ],
    [
      "unknown replacement result field",
      (job) => {
        job.result.replacement_session.forged = true;
      },
    ],
    [
      "wrong replacement status URL",
      (job) => (job.result.replacement_session.status_url = "/api/v1/forged"),
    ],
  ])("rejects invalid ready continuation: %s", (_name, tamper) => {
    const job = repairJobFixture("ready");
    tamper(job);
    expect(() => parseReviewProxyRepairJob(job)).toThrow();
  });

  it.each(["queued", "committing", "failed", "blocked", "cancelled"])(
    "accepts the bounded %s lifecycle",
    (status) => {
      expect(parseReviewProxyRepairJob(repairJobFixture(status)).status).toBe(
        status,
      );
    },
  );

  it.each([null, [], "not-an-object"])(
    "rejects a non-record job response %#",
    (value) => {
      expect(() => parseReviewProxyRepairJob(value)).toThrow();
    },
  );

  it("rejects a stale or regressive poll update", () => {
    const current = parseReviewProxyRepairJob(repairJobFixture("running"));
    const staleValue = repairJobFixture("queued");
    staleValue.updated_at = "2026-07-18T16:59:59Z";
    staleValue.progress.updated_at = staleValue.updated_at;
    const stale = parseReviewProxyRepairJob(staleValue);

    expect(reviewProxyRepairUpdateIsCurrent(current, stale)).toBe(false);
    expect(
      reviewProxyRepairUpdateIsCurrent(
        current,
        parseReviewProxyRepairJob(repairJobFixture("ready")),
      ),
    ).toBe(true);
  });

  it("rejects changed poll authority and every progress regression", () => {
    const current = parseReviewProxyRepairJob(repairJobFixture("running"));
    for (const change of [
      (job: any) => {
        job.repair_id = "proxy-repair-2";
        job.attempt_root_repair_id = "proxy-repair-2";
        job.status_url = "/api/v1/detector-review-proxy-repairs/proxy-repair-2";
        job.cancel_url = `${job.status_url}/cancel`;
        job.retry_url = `${job.status_url}/retry`;
      },
      (job: any) => {
        job.request_sha256 = sha("2");
        job.idempotency_key = sha("2");
      },
      (job: any) => (job.authority.source_sha256 = sha("f")),
    ]) {
      const raw = repairJobFixture("running");
      change(raw);
      expect(() =>
        reviewProxyRepairUpdateIsCurrent(
          current,
          parseReviewProxyRepairJob(raw),
        ),
      ).toThrow(/authority changed/);
    }

    const statusRegression = repairJobFixture("queued");
    expect(
      reviewProxyRepairUpdateIsCurrent(
        current,
        parseReviewProxyRepairJob(statusRegression),
      ),
    ).toBe(false);
    const frameRegression = repairJobFixture("running");
    frameRegression.progress.source_frames_completed = 49;
    expect(
      reviewProxyRepairUpdateIsCurrent(
        current,
        parseReviewProxyRepairJob(frameRegression),
      ),
    ).toBe(false);
    const committing = parseReviewProxyRepairJob(
      repairJobFixture("committing"),
    );
    const stageRegression = repairJobFixture("running");
    stageRegression.updated_at = "2026-07-18T17:00:01Z";
    stageRegression.progress.updated_at = stageRegression.updated_at;
    expect(
      reviewProxyRepairUpdateIsCurrent(
        committing,
        parseReviewProxyRepairJob(stageRegression),
      ),
    ).toBe(false);
  });

  it("treats an accepted terminal result as immutable across later reads", () => {
    const current = parseReviewProxyRepairJob(repairJobFixture("ready"));
    const changedValue = repairJobFixture("ready");
    changedValue.result!.proxy.proxy_media_sha256 = sha("c");
    const changed = parseReviewProxyRepairJob(changedValue);

    expect(reviewProxyRepairUpdateIsCurrent(current, current)).toBe(true);
    expect(reviewProxyRepairUpdateIsCurrent(current, changed)).toBe(false);
  });

  it("accepts only the exact next server retry attempt lineage", () => {
    const current = parseReviewProxyRepairJob(repairJobFixture("failed"));
    const exact = parseReviewProxyRepairJob(retryJobFixture());
    expect(() =>
      requireReviewProxyRepairRetryLineage(current, exact),
    ).not.toThrow();

    for (const tamper of [
      (job: any) => (job.attempt_root_repair_id = "proxy-repair-other"),
      (job: any) => (job.attempt_number = 3),
      (job: any) => (job.retry_from_repair_id = "proxy-repair-other"),
      (job: any) => (job.authority.source_sha256 = sha("f")),
    ]) {
      const raw = retryJobFixture();
      tamper(raw);
      const next = parseReviewProxyRepairJob(raw);
      expect(() => requireReviewProxyRepairRetryLineage(current, next)).toThrow(
        /retry lineage/,
      );
    }
  });

  it("posts only the blocked session authority and uses canonical control URLs", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) =>
        new Response(JSON.stringify(repairJobFixture()), {
          status: 202,
          headers: {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
          },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createReviewProxyRepair("annotation-blocked-1");
    await getReviewProxyRepair("proxy-repair-1");
    await cancelReviewProxyRepair("proxy-repair-1");
    await retryReviewProxyRepair("proxy-repair-1");

    expect(fetchMock.mock.calls[0]).toEqual([
      "/api/detector-review-proxy-repairs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ blocked_session_id: "annotation-blocked-1" }),
      }),
    ]);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/detector-review-proxy-repairs/proxy-repair-1",
    );
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/detector-review-proxy-repairs/proxy-repair-1/cancel",
    );
    expect(fetchMock.mock.calls[3]).toEqual([
      "/api/detector-review-proxy-repairs/proxy-repair-1/retry",
      expect.objectContaining({ method: "POST", body: "{}" }),
    ]);
  });

  it.each([
    [
      "non-JSON success",
      200,
      "not-json",
      { "Cache-Control": "no-store" },
      /valid JSON/,
    ],
    [
      "string server detail",
      409,
      JSON.stringify({ detail: "repair conflict" }),
      { "Cache-Control": "no-store" },
      /repair conflict/,
    ],
    [
      "object server detail",
      409,
      JSON.stringify({ detail: { code: "conflict" } }),
      { "Cache-Control": "no-store" },
      /failed \(409\)/,
    ],
    [
      "empty server error",
      503,
      "",
      { "Cache-Control": "no-store" },
      /failed \(503\)/,
    ],
    [
      "missing no-store",
      200,
      JSON.stringify(repairJobFixture()),
      {},
      /no-store/,
    ],
  ] as const)(
    "fails closed on %s",
    async (_name, status, body, headers, message) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(body, {
            status,
            headers,
          }),
        ),
      );
      await expect(getReviewProxyRepair("proxy-repair-1")).rejects.toThrow(
        message,
      );
    },
  );

  it("preserves typed server error detail for retry conflicts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "review_proxy_retry_ineligible",
              message: "This repair phase cannot create a new attempt",
            },
          }),
          {
            status: 409,
            headers: { "Cache-Control": "no-store" },
          },
        ),
      ),
    );

    const error = await retryReviewProxyRepair("proxy-repair-1").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ReviewProxyRepairRequestError);
    expect(error).toMatchObject({
      status: 409,
      code: "review_proxy_retry_ineligible",
      message: "This repair phase cannot create a new attempt",
    });
  });

  it("rejects unsafe request IDs before fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(() => createReviewProxyRepair("../session")).toThrow();
    expect(() => getReviewProxyRepair("../repair")).toThrow();
    expect(() => cancelReviewProxyRepair("../repair")).toThrow();
    expect(() => retryReviewProxyRepair("../repair")).toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
