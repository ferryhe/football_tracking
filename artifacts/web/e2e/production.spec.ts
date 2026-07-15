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

const previewDataUrl = `data:image/svg+xml;base64,${Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><rect width="1920" height="1080" fill="#174f2a"/><path d="M100 100H1820V980H100Z" fill="none" stroke="white" stroke-width="8"/></svg>',
).toString("base64")}`;

const squarePreviewSvg =
  '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000"><rect width="1000" height="1000" fill="#174f2a"/><path d="M80 80H920V920H80Z" fill="none" stroke="white" stroke-width="8"/></svg>';

interface CanvasBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

function sourcePointOnCanvas(
  box: CanvasBox,
  point: [number, number],
  source: { width: number; height: number },
) {
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const offsetX = (box.width - source.width * scale) / 2;
  const offsetY = (box.height - source.height * scale) / 2;
  return {
    x: offsetX + point[0] * scale,
    y: offsetY + point[1] * scale,
  };
}

function chromiumMouseSourcePoint(
  box: CanvasBox,
  position: { x: number; y: number },
  source: { width: number; height: number },
): [number, number] {
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const offsetX = (box.width - source.width * scale) / 2;
  const offsetY = (box.height - source.height * scale) / 2;
  const localX = Math.floor(box.x + position.x) - box.x;
  const localY = Math.floor(box.y + position.y) - box.y;
  return [
    Math.max(
      0,
      Math.min(source.width - 1, Math.round((localX - offsetX) / scale)),
    ),
    Math.max(
      0,
      Math.min(source.height - 1, Math.round((localY - offsetY) / scale)),
    ),
  ];
}

function draftWithApprovedPolygon() {
  const timestamp = "2026-07-14T12:00:00Z";
  return {
    schema_version: 2,
    workflow_id: "workflow-overlay-readiness",
    created_at: timestamp,
    updated_at: timestamp,
    status: "active",
    source: inputCatalog.videos[0],
    calibration: {
      source_resolution: { width: 1920, height: 1080 },
      suggestion: null,
      approved_polygon: [
        [100, 100],
        [1800, 100],
        [1800, 1000],
      ],
      exclusions: [],
      polygon_digest: "a".repeat(64),
      confirmed_frames: [],
    },
    trial: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

function draftWithCompletedCalibration() {
  const draft = draftWithApprovedPolygon();
  const digest = draft.calibration.polygon_digest;
  return {
    ...draft,
    workflow_id: "workflow-completed-calibration",
    calibration: {
      ...draft.calibration,
      confirmed_frames: [10, 20, 30].map((frameIndex, index) => ({
        input_video: inputCatalog.videos[0].path,
        frame_index: frameIndex,
        frame_time_seconds: frameIndex / 25,
        sample_index: index + 1,
        source_resolution: { width: 1920, height: 1080 },
        polygon_digest: digest,
      })),
    },
  };
}

async function mockInputs(page: Page) {
  await page.route("**/api/inputs", async (route) => {
    await route.fulfill({ json: inputCatalog });
  });
  await page.route("**/api/inputs/field-preview", async (route) => {
    const body = route.request().postDataJSON() as { sample_index?: number };
    const sampleIndex = body.sample_index ?? 1;
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: previewDataUrl,
        frame_width: 1920,
        frame_height: 1080,
        frame_index: sampleIndex * 10,
        frame_time_seconds: (sampleIndex * 10) / 25,
        sample_index: sampleIndex,
        sample_count: 3,
      },
    });
  });
  await page.route("**/api/inputs/field-suggestion", async (route) => {
    const body = route.request().postDataJSON() as { frame_index?: number };
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: previewDataUrl,
        preview_bounds: [0, 0, 1919, 1079],
        frame_width: 1920,
        frame_height: 1080,
        frame_index: body.frame_index ?? 10,
        frame_time_seconds: (body.frame_index ?? 10) / 25,
        sample_index: Math.max(1, Math.round((body.frame_index ?? 10) / 10)),
        sample_count: 3,
        field_polygon: [
          [100, 100],
          [1800, 100],
          [1800, 1000],
          [100, 1000],
        ],
        expanded_polygon: [
          [80, 80],
          [1820, 80],
          [1820, 1020],
          [80, 1020],
        ],
        field_roi: [100, 100, 1800, 1000],
        expanded_roi: [80, 80, 1820, 1020],
        confidence: "detected",
        source: "system-detector",
        field_coverage: 0.78,
        config_patch: {},
      },
    });
  });
}

async function openCalibration(page: Page) {
  await page.goto("/production");
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();
  await expect(page.getByAltText("Original source frame 10")).toBeVisible();
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
  expect(
    mutationRequests.every((request) =>
      request.includes("/api/inputs/field-preview"),
    ),
  ).toBe(true);
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
  await expect(page.getByText("data/match-a.mp4").first()).toBeVisible();
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
});

test("edits a real Konva polygon by click and drag, then deletes, undoes, and clears", async ({
  page,
}) => {
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
  await page.getByRole("button", { name: "Use this suggestion" }).click();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "4. (100, 1000)",
  );

  const canvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (!box) return;

  await canvas.click({ position: { x: box.width * 0.5, y: box.height * 0.5 } });
  await expect(page.getByTestId("approved-coordinates")).toContainText("5.");

  const draggedX = box.width * 0.5;
  const draggedY = (538 / 1080) * box.height;
  await page.mouse.move(box.x + draggedX, box.y + draggedY);
  await page.mouse.down();
  await page.mouse.move(box.x + draggedX + 45, box.y + draggedY + 30, {
    steps: 5,
  });
  await page.mouse.up();
  await expect(page.getByTestId("approved-coordinates")).not.toContainText(
    "5. (960, 538)",
  );

  await page.getByRole("button", { name: "Delete point 1" }).click();
  await expect(page.getByTestId("approved-coordinates")).not.toContainText(
    "5.",
  );
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("approved-coordinates")).toContainText("5.");
  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
});

test("supports keyboard coordinates and completes three distinct frame confirmations", async ({
  page,
}) => {
  await openCalibration(page);
  for (let index = 0; index < 2; index += 1) {
    await page.getByRole("button", { name: "Add point" }).click();
  }
  await expect(page.getByText("Add at least three points.")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Add point" }).click();
  const pointOneX = page.getByLabel("Point 1 X coordinate");
  await pointOneX.fill("1920");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeDisabled();
  await pointOneX.fill("120");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120,",
  );

  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await page.getByRole("button", { name: "Next frame" }).click();
  await expect(page.getByTestId("calibration-frame-meta")).toContainText(
    "source frame 20",
  );
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await page.getByRole("button", { name: "Next frame" }).click();
  await expect(page.getByTestId("calibration-frame-meta")).toContainText(
    "source frame 30",
  );
  await page.getByRole("button", { name: "Confirm this frame" }).click();

  await expect(page.getByText("3 frames confirmed")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Next$/ })).toBeEnabled();
  await expect(page.locator(".konvajs-content")).toHaveCount(1);
  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(
    page.getByRole("heading", { name: "Trial and tuning" }),
  ).toBeVisible();
  await expect(page.locator(".konvajs-content")).toHaveCount(0);
});

test("restores approved calibration and suggestion without persisting preview image data", async ({
  page,
}) => {
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await page.getByRole("button", { name: "Use this suggestion" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw ? JSON.parse(raw).calibration?.polygon_digest : null;
      }),
    )
    .toMatch(/^[a-f\d]{64}$/);
  const raw = await page.evaluate(() =>
    localStorage.getItem("football-tracking.production-draft.v1"),
  );
  expect(raw).not.toContain("preview_data_url");

  await page.reload();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByAltText("Original source frame 10")).toBeVisible();
});

test("blocks Next for unresolved restored coordinates and requires three fresh confirmations after an edit", async ({
  page,
}) => {
  await page.addInitScript((draft) => {
    localStorage.setItem(
      "football-tracking.production-draft.v1",
      JSON.stringify(draft),
    );
  }, draftWithCompletedCalibration());
  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Trial and tuning" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();

  const workspaceNext = page.getByRole("button", { name: /^Next$/ });
  await expect(workspaceNext).toBeEnabled();
  const pointOneX = page.getByLabel("Point 1 X coordinate");

  await pointOneX.fill("100.0");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(workspaceNext).toBeDisabled();
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveValue("100");
  await expect(workspaceNext).toBeEnabled();
  const persistedAfterEquivalentCommit = await page.evaluate(() => {
    const raw = localStorage.getItem("football-tracking.production-draft.v1");
    return raw ? JSON.parse(raw).calibration : null;
  });
  expect(persistedAfterEquivalentCommit).toEqual(
    draftWithCompletedCalibration().calibration,
  );

  await pointOneX.fill("1920");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByText(/enter a value from 0 through 1919/i),
  ).toBeVisible();
  await expect(workspaceNext).toBeDisabled();
  const persistedWhileInvalid = await page.evaluate(() => {
    const raw = localStorage.getItem("football-tracking.production-draft.v1");
    const draft = raw ? JSON.parse(raw) : null;
    return {
      point: draft?.calibration?.approved_polygon?.[0],
      frames: draft?.calibration?.confirmed_frames?.length,
    };
  });
  expect(persistedWhileInvalid).toEqual({ point: [100, 100], frames: 3 });

  await pointOneX.fill("120");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(workspaceNext).toBeDisabled();
  await pointOneX.press("Enter");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await expect(page.getByText("0 frames confirmed")).toBeVisible();
  await expect(workspaceNext).toBeDisabled();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw
          ? JSON.parse(raw).calibration?.confirmed_frames?.length
          : null;
      }),
    )
    .toBe(0);

  for (const frameIndex of [10, 20, 30]) {
    await expect(page.getByTestId("calibration-frame-meta")).toContainText(
      `source frame ${frameIndex}`,
    );
    const confirm = page.getByRole("button", { name: "Confirm this frame" });
    await expect(confirm).toBeEnabled();
    await confirm.click();
    if (frameIndex < 30) {
      await page.getByRole("button", { name: "Next frame" }).click();
    }
  }
  await expect(page.getByText("3 frames confirmed")).toBeVisible();
  await expect(workspaceNext).toBeEnabled();
});

test("maps real Chromium canvas coordinates at device scale factor 2", async ({
  browser,
}) => {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  try {
    await mockInputs(page);
    await openCalibration(page);
    expect(await page.evaluate(() => window.devicePixelRatio)).toBe(2);
    const canvas = page
      .getByTestId("field-polygon-editor")
      .locator("canvas")
      .first();
    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    if (!box) return;

    const sourcePoint: [number, number] = [640, 360];
    const position = sourcePointOnCanvas(box, sourcePoint, {
      width: 1920,
      height: 1080,
    });
    const clickPosition = { x: position.x + 0.25, y: position.y + 0.25 };
    const expectedSourcePoint = chromiumMouseSourcePoint(box, clickPosition, {
      width: 1920,
      height: 1080,
    });
    await canvas.click({
      position: clickPosition,
    });
    await expect(page.getByTestId("approved-coordinates")).toHaveText(
      `1. (${expectedSourcePoint[0]}, ${expectedSourcePoint[1]})`,
    );
    expect(
      Math.abs(expectedSourcePoint[0] - sourcePoint[0]),
    ).toBeLessThanOrEqual(1);
    expect(
      Math.abs(expectedSourcePoint[1] - sourcePoint[1]),
    ).toBeLessThanOrEqual(1);
  } finally {
    await context.close();
  }
});

test("maps a letterboxed square source to and from display coordinates", async ({
  page,
}) => {
  await page.unroute("**/api/inputs/field-preview");
  await page.route("**/api/inputs/field-preview", async (route) => {
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: `data:image/svg+xml;base64,${Buffer.from(squarePreviewSvg).toString("base64")}`,
        frame_width: 1000,
        frame_height: 1000,
        frame_index: 10,
        frame_time_seconds: 0.4,
        sample_index: 1,
        sample_count: 3,
      },
    });
  });
  await openCalibration(page);
  const canvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (!box) return;

  await canvas.click({ position: { x: 5, y: box.height / 2 } });
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");

  const requested: [number, number] = [250, 750];
  const clickPosition = sourcePointOnCanvas(box, requested, {
    width: 1000,
    height: 1000,
  });
  const biasedClickPosition = clickPosition;
  const expectedSourcePoint = chromiumMouseSourcePoint(
    box,
    biasedClickPosition,
    { width: 1000, height: 1000 },
  );
  await canvas.click({ position: biasedClickPosition });
  await expect(page.getByTestId("approved-coordinates")).toHaveText(
    `1. (${expectedSourcePoint[0]}, ${expectedSourcePoint[1]})`,
  );
  const roundTrip = sourcePointOnCanvas(
    box,
    [
      Number(await page.getByLabel("Point 1 X coordinate").inputValue()),
      Number(await page.getByLabel("Point 1 Y coordinate").inputValue()),
    ],
    {
      width: 1000,
      height: 1000,
    },
  );
  expect(
    Math.hypot(
      roundTrip.x - biasedClickPosition.x,
      roundTrip.y - biasedClickPosition.y,
    ),
  ).toBeLessThanOrEqual(1);
});

test("keeps confirmation disabled until preview, image, and approved overlay are ready", async ({
  page,
}) => {
  let releasePreview!: () => void;
  let releaseImage!: () => void;
  let releaseOverlay!: () => void;
  const previewGate = new Promise<void>((resolve) => {
    releasePreview = resolve;
  });
  const imageGate = new Promise<void>((resolve) => {
    releaseImage = resolve;
  });
  const overlayGate = new Promise<void>((resolve) => {
    releaseOverlay = resolve;
  });
  await page.addInitScript((draft) => {
    localStorage.setItem(
      "football-tracking.production-draft.v1",
      JSON.stringify(draft),
    );
  }, draftWithApprovedPolygon());
  await page.unroute("**/api/inputs/field-preview");
  await page.route("**/api/inputs/field-preview", async (route) => {
    await previewGate;
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: "http://127.0.0.1:5173/e2e-preview.svg",
        frame_width: 1920,
        frame_height: 1080,
        frame_index: 10,
        frame_time_seconds: 0.4,
        sample_index: 1,
        sample_count: 3,
      },
    });
  });
  await page.route("**/e2e-preview.svg", async (route) => {
    await imageGate;
    await route.fulfill({
      contentType: "image/svg+xml",
      body: squarePreviewSvg,
    });
  });
  await page.route(
    "**/src/components/production/FieldPolygonEditor.tsx*",
    async (route) => {
      await overlayGate;
      await route.continue();
    },
  );

  await page.goto("/production");
  const confirm = page.getByRole("button", { name: "Confirm this frame" });
  await expect(confirm).toBeDisabled();
  await expect(page.getByAltText("Original source frame 10")).toHaveCount(0);

  releasePreview();
  const image = page.getByAltText("Original source frame 10");
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((element: HTMLImageElement) => element.complete))
    .toBe(false);
  await expect(confirm).toBeDisabled();

  releaseImage();
  await expect
    .poll(() =>
      image.evaluate(
        (element: HTMLImageElement) =>
          element.complete && element.naturalWidth > 0,
      ),
    )
    .toBe(true);
  await expect(page.getByTestId("field-polygon-editor")).toHaveCount(0);
  await expect(confirm).toBeDisabled();

  releaseOverlay();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();
  await expect(confirm).toBeEnabled();
});

test("keeps the overlay aligned at desktop and mobile sizes with accessible controls", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "4. (100, 1000)",
  );
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
  await testInfo.attach("calibration-1440-suggested", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.getByRole("button", { name: "Use this suggestion" }).click();
  const pointOneX = page.getByLabel("Point 1 X coordinate");
  await pointOneX.fill("120");
  await pointOneX.press("Enter");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await testInfo.attach("calibration-1440-editing", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await expect(page.getByText("1 frames confirmed")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Frame already confirmed" }),
  ).toBeDisabled();
  await testInfo.attach("calibration-1440-confirmed", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () => {
      const canvasBox = await page
        .getByTestId("field-polygon-editor")
        .locator("canvas")
        .first()
        .boundingBox();
      const containerBox = await page
        .getByTestId("field-polygon-editor")
        .boundingBox();
      return Boolean(
        canvasBox &&
        containerBox &&
        Math.abs(canvasBox.width - containerBox.width) <= 1 &&
        Math.abs(canvasBox.height - containerBox.height) <= 1,
      );
    })
    .toBe(true);
  const previewBox = await page
    .getByTestId("calibration-preview")
    .boundingBox();
  const imageBox = await page
    .getByAltText("Original source frame 10")
    .boundingBox();
  const editorBox = await page
    .getByTestId("field-polygon-editor")
    .boundingBox();
  expect(previewBox).not.toBeNull();
  expect(imageBox).not.toBeNull();
  expect(editorBox).not.toBeNull();
  expect(
    Math.abs((imageBox?.width ?? 0) - (editorBox?.width ?? 0)),
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs((imageBox?.height ?? 0) - (editorBox?.height ?? 0)),
  ).toBeLessThanOrEqual(1);
  expect(previewBox?.width ?? 0).toBeLessThanOrEqual(390);
  await expect(page.getByText("1 frames confirmed")).toBeVisible();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await testInfo.attach("calibration-mobile-confirmed", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  const mobileCanvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const mobileBox = await mobileCanvas.boundingBox();
  expect(mobileBox).not.toBeNull();
  if (!mobileBox) return;
  const addedPoint: [number, number] = [960, 540];
  const addedPosition = sourcePointOnCanvas(mobileBox, addedPoint, {
    width: 1920,
    height: 1080,
  });
  const biasedAddedPosition = {
    x: addedPosition.x + 0.25,
    y: addedPosition.y + 0.25,
  };
  const expectedAddedPoint = chromiumMouseSourcePoint(
    mobileBox,
    biasedAddedPosition,
    { width: 1920, height: 1080 },
  );
  await mobileCanvas.click({ position: biasedAddedPosition });
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    `5. (${expectedAddedPoint[0]}, ${expectedAddedPoint[1]})`,
  );

  const dragBox = await mobileCanvas.boundingBox();
  expect(dragBox).not.toBeNull();
  if (!dragBox) return;
  const dragStartPosition = sourcePointOnCanvas(dragBox, expectedAddedPoint, {
    width: 1920,
    height: 1080,
  });
  const draggedPoint: [number, number] = [1200, 650];
  const draggedPosition = sourcePointOnCanvas(dragBox, draggedPoint, {
    width: 1920,
    height: 1080,
  });
  const biasedDraggedPosition = {
    x: draggedPosition.x + 0.25,
    y: draggedPosition.y + 0.25,
  };
  const expectedDraggedPoint = chromiumMouseSourcePoint(
    dragBox,
    biasedDraggedPosition,
    { width: 1920, height: 1080 },
  );
  await page.mouse.move(
    dragBox.x + dragStartPosition.x,
    dragBox.y + dragStartPosition.y,
  );
  await page.mouse.down();
  await page.mouse.move(
    dragBox.x + biasedDraggedPosition.x,
    dragBox.y + biasedDraggedPosition.y,
    { steps: 6 },
  );
  await page.mouse.up();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    `5. (${expectedDraggedPoint[0]}, ${expectedDraggedPoint[1]})`,
  );

  const results = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    results.violations.filter(
      (violation) =>
        violation.impact === "critical" || violation.impact === "serious",
    ),
  ).toEqual([]);
});

test("renders interactive calibration copy in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("app-language", "zh"));
  await page.goto("/production");
  await page.getByLabel("原片").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.getByRole("heading", { name: "球场校准" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "系统建议" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "三帧校准确认" }),
  ).toBeVisible();
});
