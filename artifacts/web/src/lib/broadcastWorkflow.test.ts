import assert from "node:assert/strict";

import type {
  ArtifactSummary,
  BroadcastCalibrationConfirmation,
  BroadcastReviewCandidate,
  BroadcastReviewWindowsResponse,
  RunRecord,
} from "@workspace/api-client-react";

import {
  broadcastArtifactQueryIdentity,
  broadcastCancellationTarget,
  broadcastRecomputeRecoveryMode,
  buildBroadcastCreateRequest,
  deriveBroadcastWorkflowState,
  localizeBroadcastWorkflowMessage,
  mergeBroadcastArtifacts,
  recoverBroadcastWorkflowRun,
  resolveBroadcastMontageArtifact,
  validateAndBuildBroadcastReviewActions,
  validateBroadcastCalibration,
  type BroadcastConfirmedFrame,
  type BroadcastReviewDecision,
  type BroadcastSetupInput,
} from "./broadcastWorkflow.ts";

let passed = 0;

function test(name: string, body: () => void): void {
  try {
    body();
    passed += 1;
  } catch (error) {
    throw new Error(`broadcastWorkflow test failed: ${name}`, { cause: error });
  }
}

function parentRun(
  status: RunRecord["status"] = "completed",
  broadcastStatus = "needs_review",
  overrides: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: "broadcast-parent",
    source: "broadcast_hybrid",
    status,
    created_at: "2026-07-10T12:00:00Z",
    output_dir: "/runs/broadcast-parent",
    artifacts: [],
    broadcast: { status: broadcastStatus },
    ...overrides,
  };
}

function operationRun(
  operation: "recompute" | "render",
  status: RunRecord["status"],
  overrides: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: `${operation}-child`,
    source: `broadcast_hybrid_${operation}`,
    status,
    created_at: "2026-07-10T12:01:00Z",
    parent_run_id: "broadcast-parent",
    output_dir: `/runs/${operation}-child`,
    artifacts: [],
    broadcast: {
      operation,
      operation_status: status === "completed" ? "completed" : status,
      parent_run_id: "broadcast-parent",
    },
    ...overrides,
  };
}

for (const [name, run, expected] of [
  ["setup", null, "setup"],
  ["queued tracking", parentRun("queued", "tracking"), "tracking"],
  ["running tracking", parentRun("running", "tracking"), "tracking"],
  ["needs review", parentRun("completed", "needs_review"), "needs_review"],
  [
    "trajectory ready",
    parentRun("completed", "trajectory_ready"),
    "trajectory_ready",
  ],
  ["ready", parentRun("completed", "ready"), "ready"],
  [
    "parent failed",
    parentRun("failed", "tracking", { error: "tracker failed" }),
    "failed",
  ],
  ["parent cancelled", parentRun("cancelled", "tracking"), "cancelled"],
] as const) {
  test(name, () => {
    const state = deriveBroadcastWorkflowState(run);
    assert.equal(state.state, expected);
  });
}

test("ready artifacts use a generation-scoped query without mutable cache data", () => {
  const mutable = broadcastArtifactQueryIdentity("rendering", "a".repeat(64));
  const ready = broadcastArtifactQueryIdentity("ready", "a".repeat(64));
  const nextReady = broadcastArtifactQueryIdentity("ready", "b".repeat(64));
  assert.equal(mutable.deliveryReady, false);
  assert.equal(ready.deliveryReady, true);
  assert.notEqual(mutable.scope, ready.scope);
  assert.notEqual(ready.scope, nextReady.scope);
});

test("ready artifacts stay blocked without a valid status generation", () => {
  const missing = broadcastArtifactQueryIdentity("ready", null);
  const malformed = broadcastArtifactQueryIdentity("ready", "not-a-hash");
  assert.equal(missing.deliveryReady, false);
  assert.equal(malformed.deliveryReady, false);
  assert.equal(missing.scope, "ready:missing");
});

test("active recompute maps to recomputing and polls parent plus child", () => {
  const state = deriveBroadcastWorkflowState(
    parentRun(),
    operationRun("recompute", "running"),
  );
  assert.equal(state.state, "recomputing");
  assert.deepEqual(state.pollRunIds, ["broadcast-parent", "recompute-child"]);
});

test("active render maps to rendering", () => {
  assert.equal(
    deriveBroadcastWorkflowState(
      parentRun("completed", "trajectory_ready"),
      operationRun("render", "queued"),
    ).state,
    "rendering",
  );
});

for (const operationStatus of ["committing", "reconciling"] as const) {
  test(`${operationStatus} recompute metadata remains active and polled`, () => {
    const child = operationRun("recompute", "completed", {
      broadcast: {
        operation: "recompute",
        operation_status: operationStatus,
        parent_run_id: "broadcast-parent",
      },
    });
    const state = deriveBroadcastWorkflowState(parentRun(), child);
    assert.equal(state.state, "recomputing");
    assert.deepEqual(state.pollRunIds, ["broadcast-parent", child.run_id]);
  });
}

test("ready parent keeps polling an independently stale active child", () => {
  const child = operationRun("render", "running");
  const state = deriveBroadcastWorkflowState(
    parentRun("completed", "ready"),
    child,
  );
  assert.equal(state.state, "ready");
  assert.deepEqual(state.pollRunIds, ["broadcast-parent", child.run_id]);
});

test("ready parent still polls a stale child until reconciliation is visible", () => {
  const child = operationRun("render", "running");
  const parent = parentRun("completed", "ready", {
    broadcast: {
      status: "ready",
      last_operation: {
        operation_run_id: child.run_id,
        operation: "render",
        status: "completed",
      },
    },
  });
  assert.deepEqual(deriveBroadcastWorkflowState(parent, child).pollRunIds, [
    parent.run_id,
    child.run_id,
  ]);
});

test("ready workflow surfaces operation reconciliation conflicts", () => {
  const child = operationRun("render", "completed", {
    broadcast: {
      operation: "render",
      operation_status: "metadata_conflict",
      operation_report_status: "conflict",
      parent_run_id: "broadcast-parent",
    },
  });
  const state = deriveBroadcastWorkflowState(
    parentRun("completed", "ready"),
    child,
  );
  assert.equal(state.state, "ready");
  assert.match(state.messages.join(" "), /reconciliation.*conflict/i);
});

test("failed recompute returns to review with the child error", () => {
  const state = deriveBroadcastWorkflowState(
    parentRun(),
    operationRun("recompute", "failed", { error: "solver failed" }),
  );
  assert.equal(state.state, "needs_review");
  assert.deepEqual(state.messages, ["solver failed"]);
});

test("cancelled recompute returns to review instead of cancelling the workflow", () => {
  const state = deriveBroadcastWorkflowState(
    parentRun(),
    operationRun("recompute", "cancelled"),
  );
  assert.equal(state.state, "needs_review");
  assert.match(state.messages[0], /cancelled/);
});

test("failed render returns to trajectory ready", () => {
  const state = deriveBroadcastWorkflowState(
    parentRun("completed", "trajectory_ready"),
    operationRun("render", "failed", { error: "encoder failed" }),
  );
  assert.equal(state.state, "trajectory_ready");
  assert.deepEqual(state.messages, ["encoder failed"]);
});

test("cancelled render returns to trajectory ready", () => {
  assert.equal(
    deriveBroadcastWorkflowState(
      parentRun("completed", "trajectory_ready"),
      operationRun("render", "cancelled"),
    ).state,
    "trajectory_ready",
  );
});

test("ready parent wins over a historical failed child", () => {
  assert.equal(
    deriveBroadcastWorkflowState(
      parentRun("completed", "ready"),
      operationRun("render", "failed"),
    ).state,
    "ready",
  );
});

test("failed last recompute returns to review when listRuns omits the child", () => {
  const parent = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: "missing-recompute",
        operation: "recompute",
        status: "failed",
        error: "lost worker",
      },
    },
  });
  const state = deriveBroadcastWorkflowState(parent);
  assert.equal(state.state, "needs_review");
  assert.deepEqual(state.messages, ["lost worker"]);
});

for (const status of [
  "queued",
  "running",
  "committing",
  "reconciling",
] as const) {
  test(`missing ${status} last_operation remains resumable and polled`, () => {
    const parent = parentRun("completed", "needs_review", {
      broadcast: {
        status: "needs_review",
        last_operation: {
          operation_run_id: `missing-${status}`,
          operation: "recompute",
          status,
        },
      },
    });
    const recovered = recoverBroadcastWorkflowRun(parent.run_id, [parent]);
    assert.equal(recovered.state, "recomputing");
    assert.deepEqual(recovered.pollRunIds, [
      parent.run_id,
      `missing-${status}`,
    ]);
  });
}

test("cancelled last render returns to trajectory ready", () => {
  const parent = parentRun("completed", "trajectory_ready", {
    broadcast: {
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: "missing-render",
        operation: "render",
        status: "cancelled",
      },
    },
  });
  assert.equal(deriveBroadcastWorkflowState(parent).state, "trajectory_ready");
});

test("invalid completed broadcast metadata fails closed", () => {
  const state = deriveBroadcastWorkflowState(parentRun("completed", "mystery"));
  assert.equal(state.state, "failed");
  assert.match(state.messages.at(-1) ?? "", /unsupported broadcast status/);
});

test("recover parent URL chooses the newest active child over last_operation", () => {
  const completed = operationRun("recompute", "completed", {
    run_id: "old-recompute",
    created_at: "2026-07-10T12:01:00Z",
  });
  const active = operationRun("render", "running", {
    run_id: "new-render",
    created_at: "2026-07-10T12:02:00Z",
  });
  const parent = parentRun("completed", "trajectory_ready", {
    broadcast: {
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: completed.run_id,
        operation: "recompute",
        status: "completed",
      },
    },
  });
  const recovered = recoverBroadcastWorkflowRun(parent.run_id, [
    parent,
    completed,
    active,
  ]);
  assert.equal(recovered.parentRun?.run_id, parent.run_id);
  assert.equal(recovered.operationRun?.run_id, active.run_id);
  assert.equal(recovered.state, "rendering");
});

test("recover child URL restores its parent and newest server operation", () => {
  const parent = parentRun("completed", "trajectory_ready");
  const oldChild = operationRun("recompute", "completed", {
    run_id: "old-child",
  });
  const newChild = operationRun("render", "queued", {
    run_id: "new-child",
    created_at: "2026-07-10T12:03:00Z",
  });
  const recovered = recoverBroadcastWorkflowRun(oldChild.run_id, [
    parent,
    oldChild,
    newChild,
  ]);
  assert.equal(recovered.parentRun?.run_id, parent.run_id);
  assert.equal(recovered.operationRun?.run_id, newChild.run_id);
});

test("newer listed parent facade wins over a stale requested parent snapshot", () => {
  const child = operationRun("recompute", "completed");
  const staleRequested = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: child.run_id,
        operation: "recompute",
        status: "queued",
      },
    },
  });
  const currentListed = parentRun("completed", "trajectory_ready", {
    broadcast: {
      status: "trajectory_ready",
      trajectory_generation_id: "trajectory-1234567890abcdef12345678",
      last_operation: {
        operation_run_id: child.run_id,
        operation: "recompute",
        status: "completed",
      },
    },
  });
  const recovered = recoverBroadcastWorkflowRun(staleRequested, [
    currentListed,
    child,
  ]);
  assert.equal(recovered.state, "trajectory_ready");
  assert.equal(
    recovered.parentRun?.broadcast?.trajectory_generation_id,
    "trajectory-1234567890abcdef12345678",
  );
});

test("terminal child keeps stale active parent polling until facade reconciliation", () => {
  const child = operationRun("recompute", "completed");
  const staleParent = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: child.run_id,
        operation: "recompute",
        status: "reconciling",
      },
    },
  });
  const recovered = recoverBroadcastWorkflowRun(staleParent, [
    staleParent,
    child,
  ]);
  assert.equal(recovered.state, "recomputing");
  assert.deepEqual(recovered.pollRunIds, [staleParent.run_id, child.run_id]);
});

for (const currentSnapshotSource of ["requested", "listed"] as const) {
  test(`new retry parent wins an older failed operation snapshot from ${currentSnapshotSource === "requested" ? "listRuns" : "getRun"}`, () => {
    const current = parentRun("completed", "needs_review", {
      broadcast: {
        status: "needs_review",
        last_operation: {
          operation_run_id: "retry-child-2",
          operation: "recompute",
          status: "queued",
        },
      },
    });
    const stale = parentRun("completed", "needs_review", {
      broadcast: {
        status: "needs_review",
        last_operation: {
          operation_run_id: "failed-child-1",
          operation: "recompute",
          status: "failed",
        },
      },
    });
    const requested = currentSnapshotSource === "requested" ? current : stale;
    const listedParent = currentSnapshotSource === "listed" ? current : stale;
    const recovered = recoverBroadcastWorkflowRun(requested, [listedParent]);
    assert.equal(recovered.state, "recomputing");
    assert.deepEqual(recovered.pollRunIds, [current.run_id, "retry-child-2"]);
  });
}

for (const currentSnapshotSource of ["requested", "listed"] as const) {
  test(`newer failed child keeps the current terminal parent snapshot from ${currentSnapshotSource}`, () => {
    const oldChild = operationRun("recompute", "failed", {
      run_id: "failed-child-a",
      created_at: "2026-07-10T12:01:00Z",
      completed_at: "2026-07-10T12:02:00Z",
    });
    const newChild = operationRun("recompute", "failed", {
      run_id: "failed-child-b",
      created_at: "2026-07-10T12:03:00Z",
      completed_at: "2026-07-10T12:04:00Z",
    });
    const current = parentRun("completed", "needs_review", {
      broadcast: {
        status: "needs_review",
        last_operation: {
          operation_run_id: newChild.run_id,
          operation: "recompute",
          status: "failed",
        },
      },
    });
    const stale = parentRun("completed", "needs_review", {
      broadcast: {
        status: "needs_review",
        last_operation: {
          operation_run_id: oldChild.run_id,
          operation: "recompute",
          status: "failed",
        },
      },
    });
    const requested = currentSnapshotSource === "requested" ? current : stale;
    const listedParent = currentSnapshotSource === "listed" ? current : stale;
    const recovered = recoverBroadcastWorkflowRun(requested, [
      listedParent,
      oldChild,
      newChild,
    ]);
    assert.equal(
      recovered.parentRun?.broadcast?.last_operation?.operation_run_id,
      newChild.run_id,
    );
    assert.equal(recovered.operationRun?.run_id, newChild.run_id);
    assert.equal(recovered.state, "needs_review");
  });
}

test("recover uses exact last_operation when no operation is active", () => {
  const latestByTime = operationRun("render", "completed", {
    run_id: "latest-by-time",
    created_at: "2026-07-10T12:05:00Z",
  });
  const referenced = operationRun("recompute", "completed", {
    run_id: "referenced-child",
    created_at: "2026-07-10T12:02:00Z",
  });
  const parent = parentRun("completed", "trajectory_ready", {
    broadcast: {
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: referenced.run_id,
        operation: "recompute",
        status: "completed",
      },
    },
  });
  assert.equal(
    recoverBroadcastWorkflowRun(parent, [parent, latestByTime, referenced])
      .operationRun?.run_id,
    referenced.run_id,
  );
});

test("ready parent polls until its referenced completed child becomes visible", () => {
  const oldChild = operationRun("recompute", "completed", {
    run_id: "old-recompute",
  });
  const parent = parentRun("completed", "ready", {
    broadcast: {
      status: "ready",
      last_operation: {
        operation_run_id: "missing-render",
        operation: "render",
        status: "completed",
      },
    },
  });
  const recovered = recoverBroadcastWorkflowRun(parent, [parent, oldChild]);
  assert.equal(recovered.state, "ready");
  assert.equal(recovered.operationRun, null);
  assert.deepEqual(recovered.pollRunIds, [parent.run_id, "missing-render"]);
});

test("missing referenced active render wins over an unrelated historical child", () => {
  const oldChild = operationRun("recompute", "completed", {
    run_id: "old-recompute",
  });
  const parent = parentRun("completed", "trajectory_ready", {
    broadcast: {
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: "missing-active-render",
        operation: "render",
        status: "running",
      },
    },
  });
  const recovered = recoverBroadcastWorkflowRun(parent, [parent, oldChild]);
  assert.equal(recovered.state, "rendering");
  assert.equal(recovered.operationRun, null);
  assert.deepEqual(recovered.pollRunIds, [
    parent.run_id,
    "missing-active-render",
  ]);
  assert.equal(broadcastCancellationTarget(recovered), "missing-active-render");
});

test("terminal operation fallback is never a cancellation target", () => {
  const parent = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: "failed-recompute",
        operation: "recompute",
        status: "failed",
      },
    },
  });
  assert.equal(
    broadcastCancellationTarget(
      recoverBroadcastWorkflowRun(parent.run_id, [parent]),
    ),
    null,
  );
});

test("recover ignores unrelated children", () => {
  const parent = parentRun();
  const unrelated = operationRun("render", "running", {
    parent_run_id: "someone-else",
  });
  const recovered = recoverBroadcastWorkflowRun(parent, [parent, unrelated]);
  assert.equal(recovered.operationRun, null);
  assert.equal(recovered.state, "needs_review");
});

test("recover missing URL run fails with a structured message", () => {
  const recovered = recoverBroadcastWorkflowRun("missing-run", []);
  assert.equal(recovered.state, "failed");
  assert.match(recovered.messages[0], /no longer present/);
});

test("recompute recovery is automatic only before the first operation exists", () => {
  assert.equal(broadcastRecomputeRecoveryMode(parentRun(), true), "auto");
  const failed = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: "failed-recompute",
        operation: "recompute",
        status: "failed",
      },
    },
  });
  assert.equal(broadcastRecomputeRecoveryMode(failed, true), "retry");
  assert.equal(broadcastRecomputeRecoveryMode(failed, false), "none");
});

test("cancelled recompute requires explicit retry instead of auto restart", () => {
  const cancelled = parentRun("completed", "needs_review", {
    broadcast: {
      status: "needs_review",
      last_operation: {
        operation_run_id: "cancelled-recompute",
        operation: "recompute",
        status: "cancelled",
      },
    },
  });
  assert.equal(broadcastRecomputeRecoveryMode(cancelled, true), "retry");
});

test("run snapshot wins an out-of-order artifact-list response by name", () => {
  assert.deepEqual(
    mergeBroadcastArtifacts(
      [
        {
          name: "broadcast.mp4",
          exists: true,
          size_bytes: 200,
        },
      ],
      [
        {
          name: "broadcast.mp4",
          exists: false,
          size_bytes: 0,
        },
        {
          name: "camera_target.csv",
          exists: true,
          size_bytes: 50,
        },
      ],
    ),
    [
      { name: "broadcast.mp4", exists: true, size_bytes: 200 },
      { name: "camera_target.csv", exists: true, size_bytes: 50 },
    ],
  );
});

test("client validation and evidence-path failures localize in Chinese", () => {
  assert.equal(
    localizeBroadcastWorkflowMessage(
      "Field polygon must have non-zero area.",
      "zh",
    ),
    "球场多边形的面积必须大于零。",
  );
  assert.equal(
    localizeBroadcastWorkflowMessage(
      "Montage path samples/a.png is missing from the run artifact allowlist.",
      "zh",
    ),
    "任务产物白名单中缺少拼图路径 samples/a.png。",
  );
  assert.equal(
    localizeBroadcastWorkflowMessage("Operation child-1 was cancelled.", "zh"),
    "操作 child-1已取消。",
  );
});

const validCalibration: BroadcastCalibrationConfirmation = {
  source_resolution: [1920, 1080],
  confirmed_sample_frames: [0, 500, 1000],
  field_polygon: [
    [10, 10],
    [1910, 10],
    [1910, 1070],
    [10, 1070],
  ],
  exclusion_polygons: [
    [
      [20, 20],
      [80, 20],
      [50, 60],
    ],
  ],
};

const validConfirmedFrames: BroadcastConfirmedFrame[] = [
  {
    frame_index: 0,
    frame_width: 1920,
    frame_height: 1080,
    sample_index: 0,
    frame_time_seconds: 0,
  },
  {
    frame_index: 500,
    frame_width: 1920,
    frame_height: 1080,
    sample_index: 1,
    frame_time_seconds: 20,
  },
  {
    frame_index: 1000,
    frame_width: 1920,
    frame_height: 1080,
    sample_index: 2,
    frame_time_seconds: 40,
  },
];

test("valid calibration accepts three same-resolution frames", () => {
  assert.equal(
    validateBroadcastCalibration(validCalibration, validConfirmedFrames).ok,
    true,
  );
});

for (const [name, frames] of [
  ["duplicate frames", [0, 0, 1000]],
  ["descending frames", [500, 0, 1000]],
  ["negative frame", [-1, 500, 1000]],
] as const) {
  test(`calibration rejects ${name}`, () => {
    const result = validateBroadcastCalibration({
      ...validCalibration,
      confirmed_sample_frames: [...frames] as [number, number, number],
    });
    assert.equal(result.ok, false);
  });
}

test("calibration rejects frame confirmations with mixed resolutions", () => {
  const mixed = validConfirmedFrames.map((frame) => ({ ...frame }));
  mixed[2].frame_width = 1280;
  const result = validateBroadcastCalibration(validCalibration, mixed);
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /different source resolution/);
});

test("calibration rejects out-of-frame polygon points", () => {
  const result = validateBroadcastCalibration({
    ...validCalibration,
    field_polygon: [
      [0, 0],
      [1920, 10],
      [10, 10],
    ],
  });
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /inside/);
});

test("calibration rejects zero-area field polygon", () => {
  const result = validateBroadcastCalibration({
    ...validCalibration,
    field_polygon: [
      [0, 0],
      [10, 10],
      [20, 20],
    ],
  });
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /non-zero area/);
});

test("calibration rejects zero-area exclusion polygon", () => {
  const result = validateBroadcastCalibration({
    ...validCalibration,
    exclusion_polygons: [
      [
        [0, 0],
        [10, 10],
        [20, 20],
      ],
    ],
  });
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /Exclusion polygon 1/);
});

function validSetup(): BroadcastSetupInput {
  return {
    inputVideo: "data/match.mp4",
    configName: "stable.yaml",
    confirmedFrames: validConfirmedFrames.map((frame) => ({ ...frame })),
    fieldPolygon: validCalibration.field_polygon,
    exclusionPolygons: validCalibration.exclusion_polygons ?? [],
    maxManualReviewWindows: 12,
    configPatch: {
      detector: { confidence: 0.42 },
      runtime: { start_frame: 99, max_frames: 100 },
      output: { save_video: false, save_tracking_contract: false },
    },
  };
}

test("create request is full-video broadcast_hybrid and deep-merges required patch fields", () => {
  const setup = validSetup();
  const originalPatch = structuredClone(setup.configPatch);
  const result = buildBroadcastCreateRequest(setup);
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.equal(result.value.pipeline_mode, "broadcast_hybrid");
  assert.equal(result.value.quality_profile, "stable_broadcast");
  assert.equal(result.value.enable_follow_cam, false);
  assert.equal(result.value.start_frame, 0);
  assert.equal(result.value.max_frames, null);
  assert.deepEqual(result.value.config_patch?.runtime, {
    start_frame: 99,
    max_frames: null,
  });
  assert.deepEqual(result.value.config_patch?.output, {
    save_video: false,
    save_tracking_contract: true,
  });
  assert.deepEqual(result.value.config_patch?.detector, { confidence: 0.42 });
  assert.deepEqual(result.value.config_patch?.filtering, {
    roi: [0, 0, 1919, 1079],
  });
  assert.deepEqual(result.value.config_patch?.scene_bias, {
    enabled: true,
    ground_zones: [
      { name: "field_core", points: validCalibration.field_polygon },
    ],
    positive_rois: [
      {
        name: "field_buffer",
        points: [
          [0, 0],
          [1919, 0],
          [1919, 1079],
          [0, 1079],
        ],
      },
    ],
    dynamic_air_recovery: {
      enabled: true,
      edge_reentry_expand_x: 1919,
      edge_reentry_expand_y: 1079,
    },
  });
  assert.deepEqual(setup.configPatch, originalPatch);
  assert.deepEqual(
    result.value.calibration_confirmation?.confirmed_sample_frames,
    [0, 500, 1000],
  );
});

test("create request preserves accepted suggestion ROI and recovery tuning", () => {
  const setup = validSetup();
  setup.configPatch = {
    filtering: { roi: [100, 50, 1800, 1000], confidence: 0.2 },
    scene_bias: {
      positive_rois: [
        {
          name: "accepted-buffer",
          points: [
            [100, 50],
            [1800, 50],
            [1800, 1000],
          ],
        },
      ],
      dynamic_air_recovery: {
        profile: "accepted",
        edge_reentry_expand_x: 333,
      },
    },
  };
  const result = buildBroadcastCreateRequest(setup);
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.deepEqual(result.value.config_patch?.filtering, {
    roi: [100, 50, 1800, 1000],
    confidence: 0.2,
  });
  const sceneBias = result.value.config_patch?.scene_bias as Record<
    string,
    unknown
  >;
  assert.deepEqual(sceneBias.positive_rois, [
    {
      name: "accepted-buffer",
      points: [
        [100, 50],
        [1800, 50],
        [1800, 1000],
      ],
    },
  ]);
  assert.deepEqual(sceneBias.dynamic_air_recovery, {
    profile: "accepted",
    enabled: true,
    edge_reentry_expand_x: 333,
    edge_reentry_expand_y: 1079,
  });
});

for (const maxWindows of [0, 31, 1.5]) {
  test(`create request rejects review limit ${maxWindows}`, () => {
    assert.equal(
      buildBroadcastCreateRequest({
        ...validSetup(),
        maxManualReviewWindows: maxWindows,
      }).ok,
      false,
    );
  });
}

test("create request rejects missing video and config", () => {
  const result = buildBroadcastCreateRequest({
    ...validSetup(),
    inputVideo: " ",
    configName: "",
  });
  assert.equal(result.ok, false);
  assert.equal(result.messages.length >= 2, true);
});

test("create request rejects invalid sample metadata", () => {
  const frames = validConfirmedFrames.map((frame) => ({ ...frame }));
  frames[1].sample_index = -1;
  frames[2].frame_time_seconds = Number.NaN;
  const result = buildBroadcastCreateRequest({
    ...validSetup(),
    confirmedFrames: frames,
  });
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /sample index/);
  assert.match(result.messages.join(" "), /time/);
});

function candidate(
  candidateId: string,
  montagePath = "samples/sample-1/review_montage.png",
  size = 123,
): BroadcastReviewCandidate {
  return {
    candidate_id: candidateId,
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
      sample_id: `sample-${candidateId}`,
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
          path: montagePath,
          sha256: "f".repeat(64),
          size_bytes: size,
        },
      },
    },
  };
}

function reviewResponse(
  candidates = [candidate("candidate-1"), candidate("candidate-2")],
): BroadcastReviewWindowsResponse {
  return {
    run_id: "broadcast-parent",
    status: "ready",
    review_item_count: 1,
    items: [
      {
        review_item_id: "review-window-1",
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

const validDecisions: BroadcastReviewDecision[] = [
  { candidate_id: "candidate-1", action: "confirm_ball" },
  {
    candidate_id: "candidate-2",
    action: "reject_noise",
    noise_subtype: "player_body_or_shoe",
  },
];

test("review builder covers every candidate exactly once in queue order", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse(),
    [...validDecisions].reverse(),
    "operator-ui",
    "2026-07-10T12:00:00Z",
  );
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.deepEqual(
    result.value.actions.map((action) => action.candidate_id),
    ["candidate-1", "candidate-2"],
  );
  assert.deepEqual(
    result.value.actions.map((action) => action.action_id),
    ["broadcast-review-0001", "broadcast-review-0002"],
  );
  assert.equal(result.value.actions[1].noise_subtype, "player_body_or_shoe");
});

test("review builder accepts mark_unknown", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [{ candidate_id: "candidate-1", action: "mark_unknown" }],
    "operator-ui",
  );
  assert.equal(result.ok, true);
});

test("review builder publishes an exact empty envelope for a zero-candidate queue", () => {
  const result = validateAndBuildBroadcastReviewActions(
    {
      run_id: "broadcast-parent",
      status: "ready",
      review_item_count: 0,
      items: [],
    },
    [],
    "",
  );
  assert.equal(result.ok, true);
  if (result.ok) assert.deepEqual(result.value.actions, []);
});

test("review builder rejects a missing candidate decision", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse(),
    [validDecisions[0]],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /candidate-2 is missing/);
});

test("review builder rejects duplicate decisions", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [validDecisions[0], validDecisions[0]],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /more than one/);
});

test("review builder rejects decisions outside the bound queue", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [validDecisions[0], { candidate_id: "extra", action: "mark_unknown" }],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /not in the review queue/);
});

test("review builder requires a concrete reject_noise subtype", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [{ candidate_id: "candidate-1", action: "reject_noise" }],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /concrete noise subtype/);
});

test("review builder rejects noise subtype on other actions", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [
      {
        candidate_id: "candidate-1",
        action: "confirm_ball",
        noise_subtype: "field_line_or_mark",
      },
    ],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /only for reject_noise/);
});

test("review builder explicitly forbids correct_trajectory", () => {
  const result = validateAndBuildBroadcastReviewActions(
    reviewResponse([candidate("candidate-1")]),
    [{ candidate_id: "candidate-1", action: "correct_trajectory" }],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(
    result.messages.join(" "),
    /correct_trajectory is not supported/,
  );
});

test("review builder fails closed on duplicated queue candidates", () => {
  const duplicate = candidate("candidate-1");
  const response = reviewResponse([duplicate, duplicate]);
  const result = validateAndBuildBroadcastReviewActions(
    response,
    [validDecisions[0]],
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /multiple review windows/);
});

test("review builder rejects non-ready and inconsistent queue metadata", () => {
  const response = {
    ...reviewResponse(),
    status: "needs_review" as const,
    review_item_count: 2,
  };
  const result = validateAndBuildBroadcastReviewActions(
    response,
    validDecisions,
    "operator-ui",
  );
  assert.equal(result.ok, false);
  assert.match(result.messages.join(" "), /not ready/);
  assert.match(result.messages.join(" "), /count/);
});

const exactArtifact: ArtifactSummary = {
  name: "samples/sample-1/review_montage.png",
  path: "/runs/parent/samples/sample-1/review_montage.png",
  kind: "image",
  exists: true,
  size_bytes: 123,
  content_type: "image/png",
};

test("montage resolver accepts an exact allowlisted path and size", () => {
  const result = resolveBroadcastMontageArtifact(
    [exactArtifact],
    candidate("candidate-1"),
  );
  assert.equal(result.ok, true);
  if (result.ok) assert.equal(result.value, exactArtifact);
});

test("montage resolver accepts one unique suffix-and-size match", () => {
  const nested = {
    ...exactArtifact,
    name: `candidate_dataset/${exactArtifact.name}`,
  };
  const result = resolveBroadcastMontageArtifact(
    [nested],
    candidate("candidate-1"),
  );
  assert.equal(result.ok, true);
  if (result.ok) assert.equal(result.value.name, nested.name);
});

test("montage resolver rejects a missing allowlist entry", () => {
  const result = resolveBroadcastMontageArtifact([], candidate("candidate-1"));
  assert.equal(result.ok, false);
  assert.match(result.messages[0], /missing/);
});

test("montage resolver rejects a wrong-size exact path without suffix fallback", () => {
  const result = resolveBroadcastMontageArtifact(
    [
      { ...exactArtifact, size_bytes: 122 },
      { ...exactArtifact, name: `candidate_dataset/${exactArtifact.name}` },
    ],
    candidate("candidate-1"),
  );
  assert.equal(result.ok, false);
  assert.match(result.messages[0], /evidence-bound size/);
});

test("montage resolver rejects ambiguous suffix-and-size matches", () => {
  const result = resolveBroadcastMontageArtifact(
    [
      { ...exactArtifact, name: `dataset-a/${exactArtifact.name}` },
      { ...exactArtifact, name: `dataset-b/${exactArtifact.name}` },
    ],
    candidate("candidate-1"),
  );
  assert.equal(result.ok, false);
  assert.match(result.messages[0], /multiple suffix-and-size matches/);
});

test("montage resolver rejects traversal and absolute evidence paths", () => {
  assert.equal(
    resolveBroadcastMontageArtifact(
      [exactArtifact],
      candidate("candidate-1", "../secret.png"),
    ).ok,
    false,
  );
  assert.equal(
    resolveBroadcastMontageArtifact(
      [exactArtifact],
      candidate("candidate-1", "/secret.png"),
    ).ok,
    false,
  );
});

test("montage resolver ignores missing and unsafe allowlist entries", () => {
  const result = resolveBroadcastMontageArtifact(
    [
      { ...exactArtifact, exists: false },
      { ...exactArtifact, name: "../samples/sample-1/review_montage.png" },
    ],
    candidate("candidate-1"),
  );
  assert.equal(result.ok, false);
});

console.log(`broadcastWorkflow: ${passed} tests passed`);
