import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { RunRecord, RunStatus } from "./types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function runMoment(run: RunRecord): string | null {
  return run.completed_at ?? run.started_at ?? run.created_at ?? null;
}

export function statusColor(status: RunStatus): string {
  switch (status) {
    case "queued":
      return "text-muted-foreground";
    case "running":
      return "text-blue-500";
    case "completed":
      return "text-emerald-600 dark:text-emerald-400";
    case "failed":
      return "text-destructive";
  }
}

export function statusBadgeClass(status: RunStatus): string {
  switch (status) {
    case "queued":
      return "bg-muted text-muted-foreground";
    case "running":
      return "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300";
    case "completed":
      return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
    case "failed":
      return "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300";
  }
}

export function isDeliverable(run: RunRecord): boolean {
  return run.source === "follow_cam_render";
}

export type HistoryCategory = "baseline" | "deliverable" | "failed";

export function historyCategory(run: RunRecord): HistoryCategory {
  if (run.status === "failed") return "failed";
  if (isDeliverable(run)) return "deliverable";
  return "baseline";
}
