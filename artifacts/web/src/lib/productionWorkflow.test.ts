import { describe, expect, it } from "vitest";

import {
  PRODUCTION_DRAFT_SCHEMA_VERSION,
  PRODUCTION_DRAFT_STORAGE_KEY,
  canEnterProductionStage,
  clearProductionDraft,
  createProductionDraft,
  createProductionWorkflowId,
  deriveProductionWorkflow,
  invalidateProductionDraft,
  loadProductionDraft,
  productionHistoryOpenAction,
  requiresDraftReplacementConfirmation,
  saveProductionDraft,
  sourceSignaturesMatch,
  updateProductionSource,
  type ProductionDraft,
  type ProductionWorkflowStage,
  type SourceSignature,
} from "./productionWorkflow";

const NOW = "2026-07-14T12:00:00.000Z";
const LATER = "2026-07-14T12:05:00.000Z";
const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 1_024,
  modified_at: "2026-07-14T10:00:00Z",
};

function draftWithEvidence(): ProductionDraft {
  return {
    ...createProductionDraft(NOW, "workflow-a"),
    source: SOURCE,
    calibration: {
      polygon_digest: "polygon-a",
      confirmed_frame_ids: ["10", "20", "30"],
    },
    trial: {
      latest_run_id: "trial-2",
      accepted_run_id: "trial-2",
    },
    confirmed_config: {
      name: "production-workflow-a.yaml",
      sha256: "a".repeat(64),
    },
    full_run: {
      run_id: "full-a",
      status: "trajectory_ready",
    },
    verified_product: null,
  };
}

function memoryStorage(initial?: string): Storage {
  const values = new Map<string, string>();
  if (initial !== undefined) values.set(PRODUCTION_DRAFT_STORAGE_KEY, initial);

  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => void values.delete(key),
    setItem: (key, value) => void values.set(key, value),
  };
}

describe("production workflow stage derivation", () => {
  const cases: Array<{
    name: string;
    change: (draft: ProductionDraft) => void;
    expected: ProductionWorkflowStage;
    deliveryBlocked?: boolean;
  }> = [
    { name: "source", change: () => undefined, expected: "source" },
    {
      name: "calibration",
      change: (draft) => void (draft.source = SOURCE),
      expected: "calibration",
    },
    {
      name: "trial",
      change: (draft) => {
        draft.source = SOURCE;
        draft.calibration = {
          polygon_digest: "polygon-a",
          confirmed_frame_ids: ["10", "20", "30"],
        };
      },
      expected: "trial",
    },
    {
      name: "configuration confirmation",
      change: (draft) => {
        Object.assign(draft, draftWithEvidence(), {
          confirmed_config: null,
          full_run: null,
        });
      },
      expected: "config_confirmation",
    },
    {
      name: "full tracking not started",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), { full_run: null }),
      expected: "full_tracking",
    },
    {
      name: "queued full tracking",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "queued" },
        }),
      expected: "full_tracking",
    },
    {
      name: "running full tracking",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "running" },
        }),
      expected: "full_tracking",
    },
    {
      name: "review",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "needs_review" },
        }),
      expected: "review",
    },
    {
      name: "recomputing",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "recomputing" },
        }),
      expected: "recomputing",
    },
    {
      name: "trajectory ready",
      change: (draft) => Object.assign(draft, draftWithEvidence()),
      expected: "trajectory_ready",
    },
    {
      name: "rendering",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "rendering" },
        }),
      expected: "rendering",
    },
    {
      name: "ready without a verified product",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "ready" },
        }),
      expected: "rendering",
      deliveryBlocked: true,
    },
    {
      name: "ready with a verified product",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          status: "completed",
          full_run: { run_id: "full-a", status: "ready" },
          verified_product: {
            run_id: "full-a",
            artifact_name: "broadcast.mp4",
            status_generation: "b".repeat(64),
          },
        }),
      expected: "ready",
    },
    {
      name: "failed",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "failed" },
        }),
      expected: "failed",
    },
    {
      name: "cancelled",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: { run_id: "full-a", status: "cancelled" },
        }),
      expected: "cancelled",
    },
  ];

  for (const testCase of cases) {
    it(`derives ${testCase.name}`, () => {
      const draft = createProductionDraft(NOW, "workflow-a");
      testCase.change(draft);
      const result = deriveProductionWorkflow(draft);
      expect(result.stage).toBe(testCase.expected);
      expect(result.delivery_blocked).toBe(testCase.deliveryBlocked ?? false);
    });
  }
});

describe("production workflow identifiers", () => {
  it("uses randomUUID when the runtime provides it", () => {
    expect(
      createProductionWorkflowId({
        randomUUID: () => "runtime-uuid",
        getRandomValues: (bytes) => bytes,
      }),
    ).toBe("runtime-uuid");
  });

  it("uses secure random bytes when randomUUID is unavailable", () => {
    const id = createProductionWorkflowId({
      getRandomValues: (bytes) => {
        bytes.forEach((_, index) => {
          bytes[index] = index;
        });
        return bytes;
      },
    });

    expect(id).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});

describe("production workflow guards", () => {
  it("enforces each sequential prerequisite", () => {
    const empty = createProductionDraft(NOW, "workflow-a");
    expect(canEnterProductionStage(empty, "source")).toBe(true);
    expect(canEnterProductionStage(empty, "calibration")).toBe(false);

    const source = { ...empty, source: SOURCE };
    expect(canEnterProductionStage(source, "calibration")).toBe(true);
    expect(canEnterProductionStage(source, "trial")).toBe(false);

    const trial = draftWithEvidence();
    trial.trial = { latest_run_id: "trial-a", accepted_run_id: null };
    trial.confirmed_config = null;
    trial.full_run = null;
    expect(canEnterProductionStage(trial, "trial")).toBe(true);
    expect(canEnterProductionStage(trial, "config_confirmation")).toBe(false);

    trial.trial.accepted_run_id = "trial-a";
    expect(canEnterProductionStage(trial, "config_confirmation")).toBe(true);
    expect(canEnterProductionStage(trial, "full_tracking")).toBe(false);

    trial.confirmed_config = {
      name: "locked.yaml",
      sha256: "a".repeat(64),
    };
    expect(canEnterProductionStage(trial, "full_tracking")).toBe(true);
    expect(canEnterProductionStage(trial, "review")).toBe(false);

    trial.full_run = { run_id: "full-a", status: "needs_review" };
    expect(canEnterProductionStage(trial, "review")).toBe(true);
    expect(canEnterProductionStage(trial, "recomputing")).toBe(false);

    trial.full_run.status = "recomputing";
    expect(canEnterProductionStage(trial, "recomputing")).toBe(true);
    trial.full_run.status = "trajectory_ready";
    expect(canEnterProductionStage(trial, "trajectory_ready")).toBe(true);
    trial.full_run.status = "rendering";
    expect(canEnterProductionStage(trial, "rendering")).toBe(true);

    trial.full_run.status = "ready";
    expect(canEnterProductionStage(trial, "rendering")).toBe(true);
    expect(canEnterProductionStage(trial, "ready")).toBe(false);
    trial.verified_product = {
      run_id: "full-a",
      artifact_name: "broadcast.mp4",
      status_generation: "b".repeat(64),
    };
    expect(canEnterProductionStage(trial, "ready")).toBe(true);

    trial.full_run.status = "failed";
    expect(canEnterProductionStage(trial, "failed")).toBe(true);
    trial.full_run.status = "cancelled";
    expect(canEnterProductionStage(trial, "cancelled")).toBe(true);
  });
});

describe("production workflow invalidation", () => {
  it("invalidates all downstream evidence when the source changes", () => {
    const current = draftWithEvidence();
    const nextSource = { ...SOURCE, path: "data/match-b.mp4" };
    const updated = updateProductionSource(current, nextSource, LATER);

    expect(updated.source).toEqual(nextSource);
    expect(updated.calibration).toBeNull();
    expect(updated.trial).toBeNull();
    expect(updated.confirmed_config).toBeNull();
    expect(updated.full_run).toBeNull();
    expect(updated.verified_product).toBeNull();
    expect(updated.status).toBe("active");
    expect(updated.updated_at).toBe(LATER);
  });

  it.each([
    ["size", { ...SOURCE, size_bytes: SOURCE.size_bytes + 1 }],
    ["modified time", { ...SOURCE, modified_at: "2026-07-14T11:00:00Z" }],
  ])(
    "treats same-path source replacement by %s as a source change",
    (_, replacement) => {
      const updated = updateProductionSource(
        draftWithEvidence(),
        replacement,
        LATER,
      );
      expect(updated.source).toEqual(replacement);
      expect(updated.calibration).toBeNull();
      expect(deriveProductionWorkflow(updated).stage).toBe("calibration");
    },
  );

  it("does not invalidate evidence for an identical source signature", () => {
    const current = draftWithEvidence();
    expect(updateProductionSource(current, { ...SOURCE }, LATER)).toBe(current);
    expect(sourceSignaturesMatch(current.source, SOURCE)).toBe(true);
  });

  it("invalidates from the edited upstream stage only", () => {
    const fromCalibration = invalidateProductionDraft(
      draftWithEvidence(),
      "calibration",
      LATER,
    );
    expect(fromCalibration.source).toEqual(SOURCE);
    expect(fromCalibration.calibration).toBeNull();
    expect(fromCalibration.trial).toBeNull();

    const fromTrial = invalidateProductionDraft(
      draftWithEvidence(),
      "trial",
      LATER,
    );
    expect(fromTrial.calibration).not.toBeNull();
    expect(fromTrial.trial).toBeNull();
    expect(fromTrial.confirmed_config).toBeNull();
    expect(fromTrial.full_run).toBeNull();

    const fromSource = invalidateProductionDraft(
      draftWithEvidence(),
      "source",
      LATER,
    );
    expect(fromSource.source).toBeNull();
    expect(fromSource.calibration).toBeNull();

    const fromConfig = invalidateProductionDraft(
      draftWithEvidence(),
      "config_confirmation",
      LATER,
    );
    expect(fromConfig.trial).not.toBeNull();
    expect(fromConfig.confirmed_config).toBeNull();

    const fromFullRun = invalidateProductionDraft(
      draftWithEvidence(),
      "full_tracking",
      LATER,
    );
    expect(fromFullRun.confirmed_config).not.toBeNull();
    expect(fromFullRun.full_run).toBeNull();
  });
});

describe("production draft persistence", () => {
  it("serializes and restores a current draft", () => {
    const storage = memoryStorage();
    const draft = draftWithEvidence();
    expect(saveProductionDraft(storage, draft)).toEqual({ ok: true });
    expect(loadProductionDraft(storage)).toEqual({
      status: "restored",
      draft,
      migrated: false,
    });
  });

  it("returns an empty result when no draft exists", () => {
    expect(loadProductionDraft(memoryStorage())).toEqual({ status: "empty" });
  });

  it("fails safely for corrupt JSON and invalid current schemas", () => {
    expect(loadProductionDraft(memoryStorage("{not-json"))).toMatchObject({
      status: "corrupt",
    });
    expect(
      loadProductionDraft(
        memoryStorage(JSON.stringify({ schema_version: 1, workflow_id: 42 })),
      ),
    ).toMatchObject({ status: "corrupt" });
  });

  it.each([
    ["source", []],
    ["calibration", []],
    ["trial", []],
    ["confirmed_config", []],
    ["full_run", []],
    ["verified_product", []],
  ])("rejects an invalid %s evidence object", (field, invalidValue) => {
    const invalid = { ...draftWithEvidence(), [field]: invalidValue };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(invalid))),
    ).toMatchObject({
      status: "corrupt",
    });
  });

  it("rejects non-object and versionless draft payloads", () => {
    expect(loadProductionDraft(memoryStorage("[]"))).toMatchObject({
      status: "corrupt",
    });
    expect(loadProductionDraft(memoryStorage("{}"))).toMatchObject({
      status: "corrupt",
    });
  });

  it("identifies unknown future versions without clearing them", () => {
    const storage = memoryStorage(JSON.stringify({ schema_version: 99 }));
    expect(loadProductionDraft(storage)).toEqual({
      status: "unsupported",
      version: 99,
    });
    expect(storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).not.toBeNull();
  });

  it("migrates the defined version-zero draft", () => {
    const legacy = {
      schema_version: 0,
      workflow_id: "legacy-a",
      created_at: NOW,
      updated_at: NOW,
      source: SOURCE,
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        workflow_id: "legacy-a",
        source: SOURCE,
        calibration: null,
        trial: null,
      },
    });
  });

  it("rejects malformed version-zero drafts", () => {
    expect(
      loadProductionDraft(
        memoryStorage(
          JSON.stringify({ schema_version: 0, workflow_id: "legacy-a" }),
        ),
      ),
    ).toMatchObject({ status: "corrupt" });
  });

  it("surfaces storage read and write failures", () => {
    const storage = memoryStorage();
    storage.getItem = () => {
      throw new Error("blocked");
    };
    expect(loadProductionDraft(storage)).toMatchObject({
      status: "unavailable",
    });

    const writeStorage = memoryStorage();
    writeStorage.setItem = () => {
      throw new Error("quota");
    };
    expect(
      saveProductionDraft(writeStorage, draftWithEvidence()),
    ).toMatchObject({
      ok: false,
    });

    const nonErrorStorage = memoryStorage();
    nonErrorStorage.setItem = () => {
      throw "blocked";
    };
    expect(saveProductionDraft(nonErrorStorage, draftWithEvidence())).toEqual({
      ok: false,
      message: "blocked",
    });
  });

  it("clears only the production draft key", () => {
    const storage = memoryStorage(JSON.stringify(draftWithEvidence()));
    storage.setItem("keep-me", "yes");
    expect(clearProductionDraft(storage)).toEqual({ ok: true });
    expect(storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).toBeNull();
    expect(storage.getItem("keep-me")).toBe("yes");
  });

  it("surfaces storage clear failures", () => {
    const storage = memoryStorage();
    storage.removeItem = () => {
      throw new Error("blocked");
    };
    expect(clearProductionDraft(storage)).toEqual({
      ok: false,
      message: "blocked",
    });
  });
});

describe("draft replacement rules", () => {
  it("requires confirmation before replacing an unfinished production", () => {
    const current = {
      ...createProductionDraft(NOW, "workflow-a"),
      source: SOURCE,
    };
    expect(requiresDraftReplacementConfirmation(current, "workflow-b")).toBe(
      true,
    );
    expect(requiresDraftReplacementConfirmation(current, "workflow-a")).toBe(
      false,
    );
  });

  it("allows empty, completed, and archived drafts to be replaced", () => {
    expect(
      requiresDraftReplacementConfirmation(
        createProductionDraft(NOW, "workflow-a"),
        "workflow-b",
      ),
    ).toBe(false);

    for (const status of ["completed", "archived"] as const) {
      const current = { ...draftWithEvidence(), status };
      expect(requiresDraftReplacementConfirmation(current, "workflow-b")).toBe(
        false,
      );
    }
  });

  it("defines the history-open action through the unfinished replacement guard", () => {
    const unfinished = {
      ...createProductionDraft(NOW, "workflow-a"),
      source: SOURCE,
    };
    expect(productionHistoryOpenAction(unfinished, "workflow-a")).toBe(
      "resume_current",
    );
    expect(productionHistoryOpenAction(unfinished, "workflow-b")).toBe(
      "confirm_replace",
    );
    expect(
      productionHistoryOpenAction(
        { ...unfinished, status: "completed" },
        "workflow-b",
      ),
    ).toBe("open_requested");
  });
});
