import { describe, expect, it } from "vitest";

import {
  legacyProductionDestination,
  productionRunMatchesDraft,
  runIdFromSearch,
} from "./productionCutover";
import { createProductionDraft, type ProductionDraft } from "./productionWorkflow";

function draftWithRuns(...runIds: string[]): ProductionDraft {
  const draft = createProductionDraft("2026-07-17T12:00:00Z", "workflow-a");
  draft.full_run = {
    revision: 1,
    attempts: runIds.map((run_id) => ({ run_id }) as never),
    pending_submission: null,
    current_run_id: runIds.at(-1) ?? null,
  };
  return draft;
}

describe("production cutover routing", () => {
  it("normalizes a non-empty run query and rejects empty values", () => {
    expect(runIdFromSearch("?run=%20full-7%20")).toBe("full-7");
    expect(runIdFromSearch("?run=%20%20")).toBeNull();
    expect(runIdFromSearch("")).toBeNull();
  });

  it("recognizes only the current parent run in the saved production", () => {
    const draft = draftWithRuns("full-1", "full-2");
    expect(productionRunMatchesDraft(draft, "full-2")).toBe(true);
    expect(productionRunMatchesDraft(draft, "full-1")).toBe(false);
    expect(productionRunMatchesDraft(draft, "full-other")).toBe(false);
    expect(productionRunMatchesDraft(createProductionDraft(), "full-1")).toBe(false);
  });

  it("routes an older saved attempt to History because Production rejects it", () => {
    expect(
      legacyProductionDestination(
        "broadcast",
        "?run=full-1",
        draftWithRuns("full-1", "full-2"),
      ),
    ).toBe("/history?run=full-1&from=broadcast");
  });

  it("migrates baseline to the gated production workspace", () => {
    expect(legacyProductionDestination("baseline", "", null)).toBe(
      "/production?from=baseline",
    );
  });

  it("preserves a matching broadcast run in production without creating a loop", () => {
    expect(
      legacyProductionDestination(
        "broadcast",
        "?run=full-2",
        draftWithRuns("full-1", "full-2"),
      ),
    ).toBe("/production?run=full-2&from=broadcast");
  });

  it("routes an unmatched broadcast run to focused history", () => {
    expect(
      legacyProductionDestination(
        "broadcast",
        "?run=full-other",
        draftWithRuns("full-2"),
      ),
    ).toBe("/history?run=full-other&from=broadcast");
    expect(
      legacyProductionDestination("broadcast", "?run=full-other", null),
    ).toBe("/history?run=full-other&from=broadcast");
  });

  it("migrates broadcast without a run to the normal production entry", () => {
    expect(legacyProductionDestination("broadcast", "", null)).toBe(
      "/production?from=broadcast",
    );
  });
});
