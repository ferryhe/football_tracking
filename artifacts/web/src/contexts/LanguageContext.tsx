import { createContext, useContext, useState, type ReactNode } from "react";
import { createSafeBrowserStorage } from "@/lib/browserStorage";
import { translations, type Language, type Translations } from "@/lib/i18n";

interface LanguageContextValue {
  language: Language;
  setLanguage: (lang: Language) => void;
  t: Translations;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [storage] = useState(createSafeBrowserStorage);
  const [language, setLanguageState] = useState<Language>(() => {
    const stored = storage.getItem("app-language");
    return stored === "zh" || stored === "en" ? stored : "en";
  });

  function setLanguage(lang: Language) {
    setLanguageState(lang);
    storage.setItem("app-language", lang);
  }

  return (
    <LanguageContext.Provider
      value={{ language, setLanguage, t: translations[language] }}
    >
      {children}
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLanguage must be used within LanguageProvider");
  return ctx;
}
