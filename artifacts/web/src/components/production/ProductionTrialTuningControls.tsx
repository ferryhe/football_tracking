import { Info } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useLanguage } from "@/contexts/LanguageContext";
import type {
  ProductionTrialTuningControl,
  ProductionTuningValue,
} from "@/lib/productionTrial";

const APPROVED_SECTION_ORDER = [
  "detector",
  "sahi",
  "filtering",
  "selection",
  "tracking",
  "postprocess",
] as const;

export interface ProductionTrialTuningControlsProps {
  controls: readonly ProductionTrialTuningControl[];
  currentValues: Readonly<Record<string, ProductionTuningValue>>;
  draft: Readonly<Record<string, ProductionTuningValue>>;
  disabled: boolean;
  onValueChange: (path: string, value: ProductionTuningValue) => void;
}

function sameValue(left: unknown, right: unknown): boolean {
  if (!Array.isArray(left) || !Array.isArray(right)) {
    return Object.is(left, right);
  }
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

export function ProductionTrialTuningControls({
  controls,
  currentValues,
  draft,
  disabled,
  onValueChange,
}: ProductionTrialTuningControlsProps) {
  const { language, t } = useLanguage();
  const extraSections = Array.from(
    new Set(
      controls
        .map((control) => control.section)
        .filter(
          (section) =>
            !APPROVED_SECTION_ORDER.includes(
              section as (typeof APPROVED_SECTION_ORDER)[number],
            ),
        ),
    ),
  );
  const sections = [...APPROVED_SECTION_ORDER, ...extraSections];

  const valueLabel = (path: string, value: unknown): string => {
    if (value === undefined || value === null || value === "") return "—";
    if (typeof value === "boolean") {
      return t.production.trialTuningBoolean(value);
    }
    if (Array.isArray(value)) {
      return value
        .map((option) => t.production.trialTuningOptionLabel(path, option))
        .join(", ");
    }
    if (typeof value === "string") {
      return t.production.trialTuningOptionLabel(path, value);
    }
    return String(value);
  };

  return (
    <Tabs defaultValue={sections[0]} className="space-y-3">
      <TabsList
        aria-label={t.production.trialTuningCategories}
        className="h-auto w-full justify-start overflow-x-auto"
      >
        {sections.map((section) => (
          <TabsTrigger key={section} value={section} className="min-h-11">
            {t.production.trialTuningSection(section)}
          </TabsTrigger>
        ))}
      </TabsList>

      {sections.map((section) => (
        <TabsContent key={section} value={section} className="space-y-2">
          {controls
            .filter((control) => control.section === section)
            .map((control) => {
              const id = `trial-tuning-${control.path.replaceAll(".", "-")}`;
              const label = t.production.trialTuningControlLabel(control.path);
              const currentValue = currentValues[control.path];
              const proposedValue = draft[control.path];
              const changed = !sameValue(currentValue, proposedValue);
              const description =
                language === "zh"
                  ? control.description_zh
                  : control.description;
              const optionLabels = (control.options ?? [])
                .map((option) =>
                  t.production.trialTuningOptionLabel(control.path, option),
                )
                .join(", ");

              return (
                <div
                  key={control.path}
                  className={`grid gap-3 rounded-md border p-3 md:grid-cols-[minmax(0,1fr)_minmax(12rem,20rem)] md:items-center ${changed ? "border-primary" : ""}`}
                >
                  <div className="min-w-0 space-y-1">
                    <div className="flex items-center gap-1">
                      <Label htmlFor={id} className="font-medium">
                        {label}
                      </Label>
                      <Popover>
                        <PopoverTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-11 w-11 shrink-0"
                            aria-label={t.production.trialTuningHelp(label)}
                          >
                            <Info className="h-4 w-4" aria-hidden="true" />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent
                          role="dialog"
                          aria-label={label}
                          align="start"
                          className="space-y-3"
                        >
                          <p className="font-semibold">{label}</p>
                          <p className="text-sm text-muted-foreground">
                            {description}
                          </p>
                          <div className="space-y-1 text-xs">
                            <p>
                              {control.options?.length
                                ? t.production.trialTuningOptions(optionLabels)
                                : t.production.trialTuningRange(
                                    control.minimum,
                                    control.maximum,
                                    control.step,
                                  )}
                            </p>
                            <p>
                              {t.production.trialTuningRuntime}:{" "}
                              {t.production.trialTuningRuntimeImpact(
                                control.runtime_impact,
                              )}
                            </p>
                            <p className="break-all font-mono">
                              {t.production.trialTuningTechnicalPath}:{" "}
                              {control.path}
                            </p>
                          </div>
                        </PopoverContent>
                      </Popover>
                      <Badge variant={changed ? "default" : "secondary"}>
                        {changed
                          ? t.production.trialTuningChanged
                          : t.production.trialTuningUnchanged}
                      </Badge>
                    </div>
                    <p className="truncate text-xs text-muted-foreground">
                      {t.production.trialTuningCurrent}:{" "}
                      {valueLabel(control.path, currentValue)} ·{" "}
                      {t.production.trialTuningProposed}:{" "}
                      {valueLabel(control.path, proposedValue)}
                    </p>
                  </div>

                  {control.kind === "boolean" ? (
                    <div className="flex min-h-10 items-center gap-2">
                      <Checkbox
                        id={id}
                        checked={proposedValue === true}
                        disabled={disabled}
                        onCheckedChange={(checked) =>
                          onValueChange(control.path, checked === true)
                        }
                      />
                      <span className="text-sm">
                        {t.production.trialTuningBoolean(
                          proposedValue === true,
                        )}
                      </span>
                    </div>
                  ) : control.kind === "select" ? (
                    <select
                      id={id}
                      value={String(proposedValue ?? "")}
                      disabled={disabled}
                      onChange={(event) =>
                        onValueChange(control.path, event.target.value)
                      }
                      className="flex h-10 w-full rounded-md border border-input bg-background px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {(control.options ?? []).map((option) => (
                        <option key={option} value={option}>
                          {t.production.trialTuningOptionLabel(
                            control.path,
                            option,
                          )}
                        </option>
                      ))}
                    </select>
                  ) : control.kind === "multi_select" ? (
                    <select
                      id={id}
                      multiple
                      size={Math.min(
                        Math.max(control.options?.length ?? 2, 2),
                        5,
                      )}
                      value={Array.isArray(proposedValue) ? proposedValue : []}
                      disabled={disabled}
                      onChange={(event) =>
                        onValueChange(
                          control.path,
                          Array.from(
                            event.target.selectedOptions,
                            (option) => option.value,
                          ),
                        )
                      }
                      className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {(control.options ?? []).map((option) => (
                        <option key={option} value={option}>
                          {t.production.trialTuningOptionLabel(
                            control.path,
                            option,
                          )}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <Input
                      id={id}
                      type="number"
                      value={String(proposedValue ?? "")}
                      min={control.minimum ?? undefined}
                      max={control.maximum ?? undefined}
                      step={control.step ?? undefined}
                      disabled={disabled}
                      onChange={(event) =>
                        onValueChange(
                          control.path,
                          event.target.value === ""
                            ? ""
                            : Number(event.target.value),
                        )
                      }
                    />
                  )}
                </div>
              );
            })}
        </TabsContent>
      ))}
    </Tabs>
  );
}
