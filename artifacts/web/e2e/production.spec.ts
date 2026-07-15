import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const inputCatalog = {
  root_dir: "data",
  videos: [
    {
      name: "match-a.mp4",
      path: "data/match-a.mp4",
      size_bytes: 1_024,
      modified_at: "2026-07-14T10:00:00Z",
    },
  ],
};

async function mockInputs(page: Page) {
  await page.route("**/api/inputs", async (route) => {
    await route.fulfill({ json: inputCatalog });
  });
}

test.beforeEach(async ({ page }) => {
  await mockInputs(page);
});

test("selects an original video, advances, and restores after refresh", async ({
  page,
}) => {
  const mutationRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET") {
      mutationRequests.push(`${request.method()} ${request.url()}`);
    }
  });

  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  await expect(page.locator("nav").getByText("Match production")).toHaveCount(
    0,
  );

  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByText("Original video selected")).toBeVisible();

  await page.reload();
  await expect(
    page.getByText("Your unfinished production was restored."),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  expect(mutationRequests).toEqual([]);
});

test("requires confirmation before starting over and has no serious accessibility findings", async ({
  page,
}) => {
  await page.goto("/production");
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  const startNewButton = page.getByRole("button", {
    name: "Start new production",
  });
  const alertDialog = page.getByRole("alertdialog");
  await startNewButton.click();
  await expect(alertDialog).toBeVisible();
  await page.getByRole("button", { name: "Keep current production" }).click();
  await expect(alertDialog).toHaveCount(0);
  await expect(page.locator("main")).not.toHaveAttribute("aria-hidden", "true");
  await expect(startNewButton).toBeFocused();

  const results = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const seriousFindings = results.violations.filter(
    (violation) =>
      violation.impact === "critical" || violation.impact === "serious",
  );
  expect(seriousFindings).toEqual([]);
});

test("renders the foundation flow in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("app-language", "zh"));
  await page.goto("/production");
  await expect(page.getByRole("heading", { name: "选择原片" })).toBeVisible();
  await expect(page.getByText("步骤 1/5 · 原片")).toBeVisible();
});

test("keeps production usable when the localStorage property is blocked", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });
  });

  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await expect(page.getByRole("button", { name: "Next" })).toBeEnabled();
});

test("restores the session draft after save and exit when storage is read-only", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException("read only", "SecurityError");
    };
  });

  await page.goto("/production");
  expect(await page.evaluate(() => localStorage.getItem("missing"))).toBeNull();

  const languageToggle = page.getByTestId("button-toggle-language").first();
  await languageToggle.click();
  await expect(page.getByRole("heading", { name: "选择原片" })).toBeVisible();
  await languageToggle.click();
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();

  const themeToggle = page.getByTestId("button-toggle-theme").first();
  const wasDark = await page
    .locator("html")
    .evaluate((element) => element.classList.contains("dark"));
  await themeToggle.click();
  await expect
    .poll(() =>
      page
        .locator("html")
        .evaluate((element) => element.classList.contains("dark")),
    )
    .toBe(!wasDark);

  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save and exit" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.goBack();
  await expect(page).toHaveURL(/\/production$/);
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByText("data/match-a.mp4")).toBeVisible();
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
});
