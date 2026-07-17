import { useEffect, useState } from "react";
import { useLocation, useSearch } from "wouter";

import { useLanguage } from "@/contexts/LanguageContext";
import { createSafeBrowserStorage } from "@/lib/browserStorage";
import {
  legacyProductionDestination,
  type LegacyProductionRoute,
} from "@/lib/productionCutover";
import { loadProductionDraft, type ProductionDraft } from "@/lib/productionWorkflow";

export function ProductionEntryRedirect() {
  const { t } = useLanguage();
  const [, setLocation] = useLocation();

  useEffect(() => {
    setLocation("/production", { replace: true });
  }, [setLocation]);

  return (
    <p role="status" aria-live="polite">
      {t.cutover.openingProduction}
    </p>
  );
}

export function LegacyProductionRedirect({
  route,
}: {
  route: LegacyProductionRoute;
}) {
  const { t } = useLanguage();
  const search = useSearch();
  const [, setLocation] = useLocation();
  const [draft] = useState<ProductionDraft | null>(() => {
    const loaded = loadProductionDraft(createSafeBrowserStorage());
    return loaded.status === "restored" ? loaded.draft : null;
  });
  const destination = legacyProductionDestination(route, search, draft);

  useEffect(() => {
    setLocation(destination, { replace: true });
  }, [destination, setLocation]);

  return (
    <p role="status" aria-live="polite">
      {route === "baseline"
        ? t.cutover.migratingBaseline
        : t.cutover.migratingBroadcast}
    </p>
  );
}
