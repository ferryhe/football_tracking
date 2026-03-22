import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { LanguageToggle } from "../components/LanguageToggle";
import { I18nProvider, detectPreferredLanguage, useI18n } from "./i18n";

function LanguageProbe() {
  const { copy } = useI18n();
  return <span>{copy.header.title}</span>;
}

function setNavigatorLanguage(value: string) {
  Object.defineProperty(window.navigator, "language", {
    value,
    configurable: true,
  });
}

describe("i18n", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("detects Chinese from browser language when no override exists", () => {
    setNavigatorLanguage("zh-CN");

    expect(detectPreferredLanguage()).toBe("zh");
  });

  it("prefers stored language over browser language", () => {
    setNavigatorLanguage("zh-CN");
    window.localStorage.setItem("football-tracking-language", "en");

    expect(detectPreferredLanguage()).toBe("en");
  });

  it("switches between English and Chinese and persists the choice", () => {
    setNavigatorLanguage("en-US");

    render(
      <I18nProvider>
        <LanguageToggle />
        <LanguageProbe />
      </I18nProvider>,
    );

    expect(screen.getByText("Football Tracking Operator")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "中文" }));

    expect(screen.getByText("足球跟踪控制台")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("zh-CN");
    expect(window.localStorage.getItem("football-tracking-language")).toBe("zh");

    fireEvent.click(screen.getByRole("button", { name: "EN" }));

    expect(screen.getByText("Football Tracking Operator")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
    expect(window.localStorage.getItem("football-tracking-language")).toBe("en");
  });
});
