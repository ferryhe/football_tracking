import type { ProductionDraft } from "./productionWorkflow";

export type LegacyProductionRoute = "baseline" | "broadcast";

export function runIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("run")?.trim();
  return value || null;
}

export function productionRunMatchesDraft(
  draft: ProductionDraft | null,
  runId: string,
): boolean {
  if (!draft?.full_run) return false;
  return draft.full_run.current_run_id === runId;
}

export function legacyProductionDestination(
  route: LegacyProductionRoute,
  search: string,
  draft: ProductionDraft | null,
): string {
  const runId = runIdFromSearch(search);
  if (route === "broadcast" && runId) {
    const target = productionRunMatchesDraft(draft, runId)
      ? "/production"
      : "/history";
    return `${target}?run=${encodeURIComponent(runId)}&from=broadcast`;
  }
  return `/production?from=${route}`;
}
