import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  BroadcastReviewEvidenceStep,
  type BroadcastReviewEvidenceStatus,
} from "./BroadcastReviewEvidenceStep";

const manifestSha256 = "a".repeat(64);

const statusLabels: Record<BroadcastReviewEvidenceStatus, string> = {
  not_available: "No compatible evidence bundle",
  available: "Evidence bundle available",
  queued: "Evidence import queued",
  copying: "Copying evidence bundle",
  validating: "Validating evidence bundle",
  committing: "Committing evidence generation",
  ready: "Review evidence ready",
  blocked: "Evidence import blocked",
  failed: "Evidence import failed",
  cancelled: "Evidence import cancelled",
};

describe("BroadcastReviewEvidenceStep", () => {
  it.each(
    Object.entries(statusLabels) as [BroadcastReviewEvidenceStatus, string][],
  )("renders the %s state", (status, label) => {
    render(
      <BroadcastReviewEvidenceStep
        state={{ status }}
        onPrepare={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(label);
  });

  it("shows a compatible bundle and only prepares it after an explicit action", async () => {
    const user = userEvent.setup();
    const onPrepare = vi.fn();
    const onCancel = vi.fn();
    const onRetry = vi.fn();
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "available",
          bundle: {
            bundleId: "bundle-qualified-1",
            manifestSha256,
          },
        }}
        onPrepare={onPrepare}
        onCancel={onCancel}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("bundle-qualified-1")).toBeVisible();
    expect(screen.getByText(manifestSha256)).toBeVisible();
    expect(onPrepare).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onRetry).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    );

    expect(onPrepare).toHaveBeenCalledTimes(1);
    expect(onPrepare).toHaveBeenCalledWith({
      bundleId: "bundle-qualified-1",
      manifestSha256,
    });
    expect(onCancel).not.toHaveBeenCalled();
    expect(onRetry).not.toHaveBeenCalled();
  });

  it("disables preparation when the compatible bundle identity is incomplete", () => {
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "available",
          bundle: { bundleId: "bundle-incomplete", manifestSha256: "" },
        }}
        onPrepare={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    ).toBeDisabled();
  });

  it("shows bundle capacity and blocks preparation or retry when capacity is insufficient", () => {
    const { rerender } = render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "available",
          bundle: { bundleId: "bundle-capacity", manifestSha256 },
          capacity: {
            totalSizeBytes: 64 * 1024 * 1024,
            requiredFreeBytes: 96 * 1024 * 1024,
            availableFreeBytes: 32 * 1024 * 1024,
            attemptQuotaBytes: 128 * 1024 * 1024,
            status: "insufficient",
          },
          blockerCode: "insufficient_capacity",
          recoveryAction: "Free disk space or increase the attempt quota.",
        }}
        onPrepare={vi.fn()}
      />,
    );

    expect(screen.getByText("Bundle capacity")).toBeVisible();
    expect(screen.getByText("64.0 MB")).toBeVisible();
    expect(screen.getByText("96.0 MB")).toBeVisible();
    expect(screen.getByText("32.0 MB")).toBeVisible();
    expect(screen.getByText("128.0 MB")).toBeVisible();
    expect(screen.getByText("Insufficient capacity")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "insufficient_capacity",
    );
    expect(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    ).toBeDisabled();

    rerender(
      <BroadcastReviewEvidenceStep
        state={{
          status: "failed",
          bundle: { bundleId: "bundle-capacity", manifestSha256 },
          capacity: { status: "insufficient" },
        }}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Retry import" })).toBeDisabled();
  });

  it.each(["queued", "copying", "validating"] as const)(
    "allows safe cancellation while %s",
    async (status) => {
      const user = userEvent.setup();
      const onCancel = vi.fn();
      render(
        <BroadcastReviewEvidenceStep
          state={{ status, stage: status, progressPercent: 42.5 }}
          onCancel={onCancel}
        />,
      );

      const progress = screen.getByRole("progressbar", {
        name: "Evidence import progress",
      });
      expect(progress).toHaveAttribute("aria-valuenow", "42.5");
      expect(progress).toHaveAttribute("aria-live", "polite");
      await user.click(screen.getByRole("button", { name: "Cancel import" }));
      expect(onCancel).toHaveBeenCalledTimes(1);
    },
  );

  it("clamps progress and disables cancellation once commit starts", () => {
    const onCancel = vi.fn();
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "committing",
          stage: "root_queue_commit",
          progressPercent: 125,
        }}
        onCancel={onCancel}
      />,
    );

    expect(
      screen.getByRole("progressbar", { name: "Evidence import progress" }),
    ).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("root_queue_commit")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Cancel import" }),
    ).toBeDisabled();
    expect(
      screen.getByText("Cancellation is unavailable after commit starts."),
    ).toBeVisible();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it.each(["blocked", "failed", "cancelled"] as const)(
    "offers an explicit retry from %s",
    async (status) => {
      const user = userEvent.setup();
      const onRetry = vi.fn();
      render(
        <BroadcastReviewEvidenceStep
          state={{
            status,
            blockerCode: "insufficient_capacity",
            recoveryAction: "Free disk space and retry the import.",
          }}
          onRetry={onRetry}
        />,
      );

      expect(screen.getByText("insufficient_capacity")).toBeVisible();
      expect(
        screen.getByText("Free disk space and retry the import."),
      ).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "insufficient_capacity",
      );
      await user.click(screen.getByRole("button", { name: "Retry import" }));
      expect(onRetry).toHaveBeenCalledTimes(1);
    },
  );

  it("shows a ready generation without invoking host review behavior", () => {
    const onPrepare = vi.fn();
    const onCancel = vi.fn();
    const onRetry = vi.fn();
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "ready",
          generationId: "review-evidence-generation-1",
          queueSha256: "b".repeat(64),
        }}
        onPrepare={onPrepare}
        onCancel={onCancel}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("review-evidence-generation-1")).toBeVisible();
    expect(screen.getByText("b".repeat(64))).toBeVisible();
    expect(screen.getByText("Queue SHA-256")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "review-evidence-generation-1",
    );
    expect(screen.getByRole("status")).toHaveTextContent("b".repeat(64));
    expect(screen.queryByRole("button")).toBeNull();
    expect(onPrepare).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onRetry).not.toHaveBeenCalled();
  });

  it.each(["blocked", "failed", "cancelled"] as const)(
    "supports retrying the same bundle or explicitly preparing one different bundle from %s",
    async (status) => {
      const user = userEvent.setup();
      const onRetry = vi.fn();
      const onPrepareAlternative = vi.fn();
      const alternative = {
        bundleId: "bundle-qualified-2",
        manifestSha256: "c".repeat(64),
      };
      render(
        <BroadcastReviewEvidenceStep
          state={{
            status,
            bundle: {
              bundleId: "bundle-qualified-1",
              manifestSha256,
            },
            alternativeBundle: alternative,
          }}
          onRetry={onRetry}
          onPrepareAlternative={onPrepareAlternative}
        />,
      );

      expect(screen.getByText("Different compatible bundle")).toBeVisible();
      expect(screen.getByText("bundle-qualified-2")).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Retry import" }));
      const prepareDifferent = screen.getByRole("button", {
        name: "Prepare different bundle",
      });
      prepareDifferent.focus();
      await user.keyboard("{Enter}");

      expect(onRetry).toHaveBeenCalledTimes(1);
      expect(onPrepareAlternative).toHaveBeenCalledWith(alternative);
    },
  );

  it("supports localized labels and keyboard activation", async () => {
    const user = userEvent.setup();
    const onPrepare = vi.fn();
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "available",
          bundle: { bundleId: "bundle-zh", manifestSha256 },
        }}
        labels={{
          title: "准备复核证据",
          available: "发现兼容证据包",
          prepare: "准备复核证据",
          bundleManifest: "证据包清单",
        }}
        onPrepare={onPrepare}
      />,
    );

    expect(screen.getByRole("heading", { name: "准备复核证据" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("发现兼容证据包");
    expect(screen.getByText("证据包清单")).toBeVisible();
    const prepare = screen.getByRole("button", { name: "准备复核证据" });
    prepare.focus();
    await user.keyboard("{Enter}");
    expect(onPrepare).toHaveBeenCalledTimes(1);
  });

  it("requires explicit distinct operator and reviewer identities before reconfirming the displayed challenge", async () => {
    const user = userEvent.setup();
    const onReconfirm = vi.fn();
    render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "blocked",
          blockerCode: "confirmed_config_lineage_reconfirmation_required",
          recoveryAction: "Reconfirm the production configuration.",
          configLineageReconfirmation: {
            targetRunId: "production-run-1",
            confirmedConfigName: "generated/production.yaml",
            confirmedTextSha256: "b".repeat(64),
            expectedObservedRawSha256: "c".repeat(64),
            workflowBindings: { workflow_id: "workflow-1" },
          },
        }}
        onReconfirmConfigLineage={onReconfirm}
      />,
    );

    expect(screen.getByText("production-run-1")).toBeVisible();
    expect(screen.getByText("generated/production.yaml")).toBeVisible();
    expect(screen.getByText("b".repeat(64))).toBeVisible();
    expect(screen.getByText("c".repeat(64))).toBeVisible();
    expect(screen.getByText("workflow-1")).toBeVisible();
    const submit = screen.getByRole("button", {
      name: "Reconfirm production configuration",
    });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Operator ID"), "operator-1");
    await user.type(
      screen.getByLabelText("Independent reviewer ID"),
      "operator-1",
    );
    expect(submit).toBeDisabled();
    expect(
      screen.getByText("Operator and reviewer must be different people."),
    ).toBeVisible();

    await user.clear(screen.getByLabelText("Independent reviewer ID"));
    await user.type(
      screen.getByLabelText("Independent reviewer ID"),
      "reviewer-1",
    );
    submit.focus();
    await user.keyboard("{Enter}");

    expect(onReconfirm).toHaveBeenCalledTimes(1);
    expect(onReconfirm).toHaveBeenCalledWith({
      operatorId: "operator-1",
      reviewerId: "reviewer-1",
    });
  });

  it("disables mutations while the host is pending or disabled", () => {
    const { rerender } = render(
      <BroadcastReviewEvidenceStep
        state={{
          status: "available",
          bundle: { bundleId: "bundle-1", manifestSha256 },
        }}
        isPreparing
        onPrepare={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Preparing review evidence…" }),
    ).toBeDisabled();

    rerender(
      <BroadcastReviewEvidenceStep
        state={{ status: "failed" }}
        disabled
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Retry import" })).toBeDisabled();
  });

  it("uses a narrow-screen-safe structure and an atomic polite status region", () => {
    render(<BroadcastReviewEvidenceStep state={{ status: "not_available" }} />);

    expect(screen.getByTestId("broadcast-review-evidence-step")).toHaveClass(
      "min-w-0",
      "w-full",
    );
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status")).toHaveAttribute("aria-atomic", "true");
  });
});
