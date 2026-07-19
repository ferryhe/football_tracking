import { describe, expect, it } from "vitest";

import { pythonCanonicalSha256Sync } from "./canonicalSha256";

describe("Python-canonical SHA-256", () => {
  it("matches the backend across integral, exponent, and signed-zero floats", () => {
    const value = {
      a: 0.00001,
      b: 1e16,
      c: 1e-7,
      d: 1.5,
      e: -0,
      f: 10_000_000_000_000_002_000,
    };

    expect(
      pythonCanonicalSha256Sync(
        value,
        Object.keys(value).map((key) => `$.${key}`),
      ),
    ).toBe("f20c30adbf6bdab8683bc674f502a8979aa2ed68d00d924bea964a26fc2082ed");
  });
});
