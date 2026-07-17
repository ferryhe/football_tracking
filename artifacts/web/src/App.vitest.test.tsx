import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/pages/production", () => ({
  default: () => <h1>production workspace</h1>,
}));
vi.mock("@/components/history/GroupedProductionHistory", () => ({
  GroupedProductionHistory: () => <h1>production history</h1>,
}));
vi.mock("@/pages/ai-analysis", () => ({
  default: () => <h1>legacy ai analysis</h1>,
}));
vi.mock("@/pages/deliverable", () => ({
  default: () => <h1>legacy deliverable</h1>,
}));

import App from "./App";

beforeEach(() => {
  localStorage.clear();
  window.matchMedia = vi.fn().mockReturnValue({ matches: false });
  window.history.replaceState({}, "", "/");
});

describe("production cutover shell", () => {
  it("opens Production by default and exposes only the two primary destinations", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "production workspace" })).toBeVisible();
    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.textContent?.trim())).toEqual([
      "Production",
      "Production History",
    ]);
    expect(screen.queryByRole("link", { name: /baseline/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /broadcast/i })).toBeNull();
  });

  it("migrates the Baseline URL into the prerequisite-gated workspace", async () => {
    window.history.replaceState({}, "", "/baseline");
    render(<App />);
    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        "/production?from=baseline",
      ),
    );
    expect(await screen.findByRole("heading", { name: "production workspace" })).toBeVisible();
  });

  it("migrates an unknown Broadcast run to focused history", async () => {
    window.history.replaceState({}, "", "/broadcast?run=full-unknown");
    render(<App />);
    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        "/history?run=full-unknown&from=broadcast",
      ),
    );
    expect(await screen.findByRole("heading", { name: "production history" })).toBeVisible();
  });

  it("keeps legacy AI and deliverable pages routable but unlisted", async () => {
    window.history.replaceState({}, "", "/ai?run=full-7");
    const ai = render(<App />);
    expect(await screen.findByRole("heading", { name: "legacy ai analysis" })).toBeVisible();
    expect(screen.queryByRole("link", { name: /ai/i })).toBeNull();
    ai.unmount();

    window.history.replaceState({}, "", "/deliverable?run=full-7");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "legacy deliverable" })).toBeVisible();
    expect(screen.queryByRole("link", { name: /deliverable/i })).toBeNull();
  });

  it("closes mobile navigation after a route choice and moves focus to content", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "production workspace" });
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const mobileNav = screen.getAllByRole("navigation", {
      name: "Primary navigation",
    })[1];
    await user.click(
      within(mobileNav).getByRole("link", { name: "Production History" }),
    );
    expect(await screen.findByRole("heading", { name: "production history" })).toBeVisible();
    await waitFor(() =>
      expect(screen.queryByTestId("button-close-sidebar")).toBeNull(),
    );
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("closes mobile navigation and focuses content when the active route is chosen", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "production workspace" });
    await waitFor(() => expect(window.location.pathname).toBe("/production"));
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const dialog = screen.getByRole("dialog", { name: "Primary navigation" });

    await user.click(
      within(dialog).getByRole("link", { name: "Production" }),
    );

    expect(window.location.pathname).toBe("/production");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Primary navigation" }),
      ).toBeNull(),
    );
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("traps keyboard focus in the mobile dialog and restores it on Escape", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "production workspace" });
    const open = screen.getByRole("button", { name: "Open navigation" });
    await user.click(open);
    const dialog = screen.getByRole("dialog", { name: "Primary navigation" });
    const close = within(dialog).getByRole("button", {
      name: "Close navigation",
    });
    await waitFor(() => expect(close).toHaveFocus());

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(within(dialog).getByRole("button", { name: "中文" })).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Primary navigation" })).toBeNull();
    expect(open).toHaveFocus();
  });
});
