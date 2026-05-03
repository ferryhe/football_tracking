import { GlobeIcon } from "./Icons";
import { useI18n } from "../lib/i18n";

export function LanguageToggle() {
  const { language, setLanguage, copy } = useI18n();
  const nextLanguage = language === "en" ? "zh" : "en";
  const buttonLabel = nextLanguage === "en" ? copy.common.english : copy.common.chinese;

  return (
    <button
      type="button"
      className="language-toggle compact"
      aria-label={buttonLabel}
      title={copy.header.language}
      onClick={() => setLanguage(nextLanguage)}
    >
      <GlobeIcon className="section-icon tiny" />
      <span>{buttonLabel}</span>
    </button>
  );
}
