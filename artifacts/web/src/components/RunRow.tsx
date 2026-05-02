import { formatDateTime, historyCategory, runMoment } from "@/lib/utils";
import { StatusBadge } from "./StatusBadge";
import type { RunRecord } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Film, Crosshair } from "lucide-react";

interface RunRowProps {
  run: RunRecord;
  selected?: boolean;
  onClick?: () => void;
}

export function RunRow({ run, selected, onClick }: RunRowProps) {
  const cat = historyCategory(run);
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`run-row-${run.run_id}`}
      className={cn(
        "w-full text-left rounded-lg border px-3 py-2.5 flex items-start gap-3 transition-colors",
        "hover:bg-accent/50",
        selected
          ? "border-primary bg-accent/60"
          : "border-border bg-card"
      )}
    >
      <span className="mt-0.5 text-muted-foreground">
        {cat === "deliverable" ? (
          <Film className="h-4 w-4" />
        ) : (
          <Crosshair className="h-4 w-4" />
        )}
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{run.run_id}</p>
        <p className="text-xs text-muted-foreground truncate font-mono">
          {run.config_name ?? run.output_dir}
        </p>
      </div>
      <div className="shrink-0 flex flex-col items-end gap-1">
        <StatusBadge status={run.status} />
        <span className="text-xs text-muted-foreground">
          {formatDateTime(runMoment(run))}
        </span>
      </div>
    </button>
  );
}
