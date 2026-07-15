type StorageAccess = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export interface SafeBrowserStorage extends StorageAccess {
  readonly isPersistent: boolean;
  readonly unavailableReason: string | null;
}

type StorageOverlayEntry =
  | { state: "value"; value: string }
  | { state: "removed" };

const sessionOverlay = new Map<string, StorageOverlayEntry>();
const adapterCache = new WeakMap<StorageAccess, SafeBrowserStorage>();
let unavailableAdapter: SafeBrowserStorage | null = null;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function createStorageAdapter(
  initialPersistent: StorageAccess | null,
  initialUnavailableReason: string | null,
): SafeBrowserStorage {
  let persistent = initialPersistent;
  let unavailableReason = initialUnavailableReason;

  function useFallback(error: unknown) {
    persistent = null;
    unavailableReason = errorMessage(error);
  }

  return {
    get isPersistent() {
      return persistent !== null;
    },
    get unavailableReason() {
      return unavailableReason;
    },
    getItem(key) {
      const overlay = sessionOverlay.get(key);
      if (overlay) return overlay.state === "value" ? overlay.value : null;

      if (persistent) {
        try {
          return persistent.getItem(key);
        } catch (error) {
          useFallback(error);
        }
      }
      return null;
    },
    setItem(key, value) {
      if (persistent) {
        try {
          persistent.setItem(key, value);
          sessionOverlay.delete(key);
          return;
        } catch (error) {
          useFallback(error);
        }
      }
      sessionOverlay.set(key, { state: "value", value });
    },
    removeItem(key) {
      if (persistent) {
        try {
          persistent.removeItem(key);
          sessionOverlay.delete(key);
          return;
        } catch (error) {
          useFallback(error);
        }
      }
      sessionOverlay.set(key, { state: "removed" });
    },
  };
}

export function createSafeBrowserStorage(
  getPersistentStorage: () => StorageAccess = () => window.localStorage,
): SafeBrowserStorage {
  let persistent: StorageAccess;
  try {
    persistent = getPersistentStorage();
  } catch (error) {
    unavailableAdapter ??= createStorageAdapter(null, errorMessage(error));
    return unavailableAdapter;
  }

  const cached = adapterCache.get(persistent);
  if (cached) return cached;

  const adapter = createStorageAdapter(persistent, null);
  adapterCache.set(persistent, adapter);
  return adapter;
}
