import { describe, expect, it } from "vitest";

import { createSafeBrowserStorage } from "./browserStorage";

describe("safe browser storage", () => {
  it("uses shared in-memory storage when the localStorage property getter throws", () => {
    const host = {} as { localStorage: Storage };
    Object.defineProperty(host, "localStorage", {
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });

    const first = createSafeBrowserStorage(() => host.localStorage);
    expect(first.isPersistent).toBe(false);
    expect(() =>
      first.setItem("fallback-test", "saved-in-memory"),
    ).not.toThrow();

    const second = createSafeBrowserStorage(() => host.localStorage);
    expect(second.getItem("fallback-test")).toBe("saved-in-memory");
    second.removeItem("fallback-test");
    expect(first.getItem("fallback-test")).toBeNull();
  });

  it("falls back when a storage operation starts throwing", () => {
    const persistent = {
      getItem: () => {
        throw new DOMException("blocked", "SecurityError");
      },
      setItem: () => undefined,
      removeItem: () => undefined,
    } as Pick<Storage, "getItem" | "setItem" | "removeItem">;
    const storage = createSafeBrowserStorage(() => persistent);

    expect(storage.getItem("operation-fallback")).toBeNull();
    expect(storage.isPersistent).toBe(false);
    storage.setItem("operation-fallback", "value");
    expect(storage.getItem("operation-fallback")).toBe("value");
    storage.removeItem("operation-fallback");
  });

  it("keeps newer fallback writes and removals over stale persistent reads", () => {
    const persistentValues = new Map([["overlay-test", "stale"]]);
    const createReadOnlyStorage = () => ({
      getItem: (key: string) => persistentValues.get(key) ?? null,
      setItem: () => {
        throw new DOMException("read only", "SecurityError");
      },
      removeItem: () => {
        throw new DOMException("read only", "SecurityError");
      },
    });

    const firstPersistent = createReadOnlyStorage();
    const first = createSafeBrowserStorage(() => firstPersistent);
    expect(first.getItem("overlay-test")).toBe("stale");
    first.setItem("overlay-test", "newer-in-memory");

    const cached = createSafeBrowserStorage(() => firstPersistent);
    expect(cached).toBe(first);
    expect(cached.isPersistent).toBe(false);
    expect(cached.getItem("overlay-test")).toBe("newer-in-memory");

    const remounted = createSafeBrowserStorage(createReadOnlyStorage);
    expect(remounted.getItem("overlay-test")).toBe("newer-in-memory");
    remounted.removeItem("overlay-test");

    const afterRemoval = createSafeBrowserStorage(createReadOnlyStorage);
    expect(afterRemoval.getItem("overlay-test")).toBeNull();
  });
});
