import { describe, expect, it } from "vitest";

import { buildConfigPatch } from "./FieldSetupCard";

describe("buildConfigPatch", () => {
  it("clamps edge reentry expand values to at least one pixel", () => {
    const patch = buildConfigPatch(
      [
        [120, 120],
        [120, 120],
        [120, 120],
        [120, 120],
      ],
      [
        [200, 200],
        [200, 200],
        [200, 200],
        [200, 200],
      ],
    );

    expect(patch.scene_bias.dynamic_air_recovery.edge_reentry_expand_x).toBe(1);
    expect(patch.scene_bias.dynamic_air_recovery.edge_reentry_expand_y).toBe(1);
  });
});
