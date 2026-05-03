import type { ConfigListItem } from "./types";

function configCreatedAtValue(config: ConfigListItem): number {
  return config.created_at ? new Date(config.created_at).getTime() : 0;
}

export function sortConfigsByCreatedAt(configs: ConfigListItem[]): ConfigListItem[] {
  return [...configs].sort((left, right) => {
    const timeDelta = configCreatedAtValue(right) - configCreatedAtValue(left);
    if (timeDelta !== 0) {
      return timeDelta;
    }
    return left.name.localeCompare(right.name);
  });
}
