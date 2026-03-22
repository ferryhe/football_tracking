import { GlobeIcon } from "./Icons";
import { useI18n } from "../lib/i18n";

export function LanguageToggle() {
  const { language, setLanguage, copy } = useI18n();

  return (
    <div className="language-toggle" role="group" aria-label={copy.header.language}>
      <span className="language-toggle-label">
        <GlobeIcon className="section-icon tiny" />
        <span>{copy.header.language}</span>
      </span>
      <button
        type="button"
        className={`language-button ${language === "en" ? "selected" : ""}`}
        onClick={() => setLanguage("en")}
      >
        {copy.common.english}
      </button>
      <button
        type="button"
        className={`language-button ${language === "zh" ? "selected" : ""}`}
        onClick={() => setLanguage("zh")}
      >
        {copy.common.chinese}
      </button>
    </div>
  );
}
