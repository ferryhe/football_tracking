import { InfoIcon } from "./Icons";

interface TooltipBadgeProps {
  label: string;
}

export function TooltipBadge({ label }: TooltipBadgeProps) {
  return (
    <span className="tooltip-badge" title={label} aria-label={label}>
      <InfoIcon className="tooltip-icon" />
    </span>
  );
}
