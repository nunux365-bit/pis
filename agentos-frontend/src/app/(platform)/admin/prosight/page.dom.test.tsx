/**
 * @vitest-environment jsdom
 */
/**
 * Interaction tests for the Prosight admin page's sync-progress announcer.
 *
 * The defect: both sync buttons set `disabled` on click, which pulls them out of
 * the focus order and drops focus to <body>, so the label flip to "Syncing..."
 * is never announced. A screen-reader user got silence from the click until the
 * result banner — minutes, for "Sync Now (Wait)".
 *
 * These assert the property that actually matters and that a human cannot
 * easily check: that a live region carries in-flight text *while* the request
 * is outstanding, and stops carrying it afterwards.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/prosightApi", () => ({
  prosightGetStatus: vi.fn(),
  prosightListSnapshots: vi.fn(),
  prosightTriggerSync: vi.fn(),
  prosightSyncNow: vi.fn(),
}));

import {
  prosightGetStatus,
  prosightListSnapshots,
  prosightSyncNow,
  prosightTriggerSync,
} from "@/lib/prosightApi";
import AdminProsightPage from "./page";

const statusMock = vi.mocked(prosightGetStatus);
const snapshotsMock = vi.mocked(prosightListSnapshots);
const syncNowMock = vi.mocked(prosightSyncNow);
const triggerMock = vi.mocked(prosightTriggerSync);

/** A deferred promise, so a sync can be observed mid-flight. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** The live region is the only `role="status"` that is empty at rest. */
function announcer(): HTMLElement {
  const regions = screen.getAllByRole("status", { hidden: true });
  const found = regions.find((r) => r.className.includes("sr-only"));
  if (!found) throw new Error("sr-only announcer not in the DOM");
  return found;
}

beforeEach(() => {
  vi.clearAllMocks();
  statusMock.mockResolvedValue({
    status: "active",
    databricks_configured: true,
    latest_snapshot: "2026-08-19",
    sync_hour: 11,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  snapshotsMock.mockResolvedValue({ snapshots: [] } as any);
});

describe("sync-progress announcer", () => {
  it("is mounted and empty before any sync — not injected on demand", async () => {
    render(<AdminProsightPage />);
    await screen.findByRole("button", { name: "Sync Now (Wait)" });

    // Always-mounted is the point: a live region inserted at the same instant
    // as its text is announced unreliably across screen readers.
    expect(announcer()).toBeInTheDocument();
    expect(announcer()).toHaveTextContent("");
    expect(announcer()).toHaveAttribute("aria-live", "polite");
  });

  it("announces while `Sync Now` is in flight, and clears when it lands", async () => {
    const d = deferred<{ status: string; snapshot_date: string }>();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    syncNowMock.mockReturnValue(d.promise as any);

    const user = userEvent.setup();
    render(<AdminProsightPage />);
    await user.click(await screen.findByRole("button", { name: "Sync Now (Wait)" }));

    // Mid-flight: the button is disabled (focus dropped), so this text is the
    // only thing a screen reader has to go on.
    await waitFor(() =>
      expect(announcer()).toHaveTextContent(
        "Sync started. This may take a few minutes."
      )
    );
    // Both buttons flip to "Syncing..." and disable — which is precisely why
    // the label is useless as an announcement: neither can hold focus.
    const busy = screen.getAllByRole("button", { name: "Syncing..." });
    expect(busy).toHaveLength(2);
    busy.forEach((b) => expect(b).toBeDisabled());

    d.resolve({ status: "success", snapshot_date: "2026-08-19" });

    // Cleared afterwards, so the result banner is not competing with a stale
    // "in progress" message still sitting in the live region.
    await waitFor(() => expect(announcer()).toHaveTextContent(""));
    expect(
      await screen.findByText(/Sync completed\. Snapshot: 2026-08-19/)
    ).toBeInTheDocument();
  });

  it("announces the background trigger too", async () => {
    const d = deferred<{ message: string }>();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    triggerMock.mockReturnValue(d.promise as any);

    const user = userEvent.setup();
    render(<AdminProsightPage />);
    await user.click(
      await screen.findByRole("button", { name: "Trigger Background Sync" })
    );

    await waitFor(() =>
      expect(announcer()).toHaveTextContent("Starting background sync.")
    );
    d.resolve({ message: "queued" });
    await waitFor(() => expect(announcer()).toHaveTextContent(""));
  });

  it("clears the announcement when a sync fails, leaving the alert to speak", async () => {
    const d = deferred<never>();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    syncNowMock.mockReturnValue(d.promise as any);

    const user = userEvent.setup();
    render(<AdminProsightPage />);
    await user.click(await screen.findByRole("button", { name: "Sync Now (Wait)" }));
    await waitFor(() =>
      expect(announcer()).toHaveTextContent(/Sync started/)
    );

    d.reject(new Error("Databricks unreachable"));

    // A stuck "in progress" after a failure would be worse than no announcement.
    await waitFor(() => expect(announcer()).toHaveTextContent(""));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Databricks unreachable"
    );
  });
});
