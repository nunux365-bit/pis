/**
 * @vitest-environment jsdom
 */
/**
 * Interaction tests for ActionablesCard — the layer `actionablesFeedback.test.ts`
 * cannot reach.
 *
 * That suite already covers the save flow's branch logic (rollback vs refetch,
 * queueing, in-flight bookkeeping) against an injected store, and re-testing it
 * through the DOM would only be slower. What is asserted here is the wiring the
 * logic tests have to assume: that the buttons are connected to the flow, that
 * `disabled` really follows the in-flight set per row, and that a failure is
 * visible to a user rather than merely handled in a reducer.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProsightActionable, ProsightActionablesResponse } from "@/lib/prosightApi";

vi.mock("@/lib/prosightApi", () => ({
  prosightListActionables: vi.fn(),
  prosightSaveActionableFeedback: vi.fn(),
}));

import {
  prosightListActionables,
  prosightSaveActionableFeedback,
} from "@/lib/prosightApi";
import ActionablesCard from "./ActionablesCard";

const listMock = vi.mocked(prosightListActionables);
const saveMock = vi.mocked(prosightSaveActionableFeedback);

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeRow(overrides: Partial<ProsightActionable> = {}): ProsightActionable {
  return {
    action_hash: "h1",
    as_of_date: "2026-08-17",
    bu: "pharmacy",
    segment: "delhi",
    lens: "BOTH LENSES",
    rank: 1,
    action: "legacy",
    segment_label: "Delhi",
    dimension: "city",
    impact_display: "₹1.2L",
    fact: null,
    l2_pocket: null,
    why: null,
    lever: null,
    news_summary: null,
    news_source: null,
    news_url: null,
    news_relation: null,
    run_days: null,
    wow_pct: null,
    dod_pct: null,
    daily_order_gap: null,
    l2_gap_orders: null,
    l2_share_pct: null,
    impact_inr_1d: null,
    impact_inr_3d: null,
    is_actionable: null,
    days_saved: null,
    feedback_updated_by: null,
    feedback_updated_at: null,
    synced_at: null,
    ...overrides,
  };
}

function response(rows: ProsightActionable[]): ProsightActionablesResponse {
  return {
    actionables: rows,
    total: rows.length,
    status: "ok",
    day_summary: null,
    synced_at: null,
    limit: 500,
    truncated: false,
  };
}

/** A promise whose settlement the test controls, to observe mid-flight state. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const rowFor = (label: string) => screen.getByText(label).closest("li")!;
const yesIn = (label: string) =>
  within(rowFor(label)).getByRole("button", { name: "Yes" });

async function renderCard(rows: ProsightActionable[]) {
  listMock.mockResolvedValue(response(rows));
  render(<ActionablesCard bu="pharmacy" date="2026-08-17" />);
  await screen.findByText(rows[0].segment_label!);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

// ── The wiring ───────────────────────────────────────────────────────────────

describe("ActionablesCard — feedback buttons", () => {
  it("sends the answer and reflects the server's copy", async () => {
    const user = userEvent.setup();
    await renderCard([makeRow()]);
    saveMock.mockResolvedValue(makeRow({ is_actionable: true }));

    await user.click(yesIn("Delhi"));

    expect(saveMock).toHaveBeenCalledWith("h1", {
      is_actionable: true,
      days_saved: null,
    });
    await waitFor(() =>
      expect(yesIn("Delhi")).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("clicking the active answer clears it", async () => {
    const user = userEvent.setup();
    await renderCard([makeRow({ is_actionable: true })]);
    saveMock.mockResolvedValue(makeRow({ is_actionable: null }));

    expect(yesIn("Delhi")).toHaveAttribute("aria-pressed", "true");
    await user.click(yesIn("Delhi"));

    expect(saveMock).toHaveBeenCalledWith("h1", {
      is_actionable: null,
      days_saved: null,
    });
  });
});

// ── The failure paths the review named ───────────────────────────────────────

describe("ActionablesCard — optimistic update failure", () => {
  it("500: rolls the row back and tells the user", async () => {
    const user = userEvent.setup();
    await renderCard([makeRow()]);
    const gate = deferred<ProsightActionable>();
    saveMock.mockReturnValue(gate.promise);

    await user.click(yesIn("Delhi"));

    // Optimistic: shown as answered while the request is still out.
    await waitFor(() =>
      expect(yesIn("Delhi")).toHaveAttribute("aria-pressed", "true"),
    );

    gate.reject(new Error("HTTP 500: server error"));

    await waitFor(() =>
      expect(yesIn("Delhi")).toHaveAttribute("aria-pressed", "false"),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save — please retry.",
    );
    // A plain failure must not refetch.
    expect(listMock).toHaveBeenCalledTimes(1);
  });

  it("404: refetches instead of rolling back", async () => {
    const user = userEvent.setup();
    await renderCard([makeRow()]);
    saveMock.mockRejectedValue(new Error("HTTP 404: Not Found"));

    await user.click(yesIn("Delhi"));

    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Actionables were refreshed",
    );
  });
});

// ── The savingHash race, at the DOM level ────────────────────────────────────

describe("ActionablesCard — concurrent saves across rows", () => {
  it("disables only the row being saved, and re-enables it only when done", async () => {
    const user = userEvent.setup();
    await renderCard([
      makeRow({ action_hash: "h1", segment_label: "Delhi" }),
      makeRow({ action_hash: "h2", segment_label: "Mumbai" }),
    ]);
    const gate = deferred<ProsightActionable>();
    saveMock.mockReturnValue(gate.promise);

    await user.click(yesIn("Delhi"));

    // The row in flight is locked; the other stays usable. A single
    // `savingHash` string could not express this.
    await waitFor(() => expect(yesIn("Delhi")).toBeDisabled());
    expect(yesIn("Mumbai")).toBeEnabled();

    gate.resolve(makeRow({ action_hash: "h1", is_actionable: true }));
    await waitFor(() => expect(yesIn("Delhi")).toBeEnabled());
  });

  it("keeps BOTH rows disabled while both saves are in flight", async () => {
    // The original defect, stated at the DOM level: with a single
    // `savingHash`, starting Mumbai's save re-enabled Delhi's buttons while
    // Delhi's request was still outstanding. One save in flight cannot
    // distinguish the two implementations — two can.
    const user = userEvent.setup();
    await renderCard([
      makeRow({ action_hash: "h1", segment_label: "Delhi" }),
      makeRow({ action_hash: "h2", segment_label: "Mumbai" }),
    ]);

    const first = deferred<ProsightActionable>();
    const second = deferred<ProsightActionable>();
    saveMock.mockImplementation((hash: string) =>
      hash === "h1" ? first.promise : second.promise,
    );

    await user.click(yesIn("Delhi"));
    await waitFor(() => expect(yesIn("Delhi")).toBeDisabled());
    await user.click(yesIn("Mumbai"));
    await waitFor(() => expect(yesIn("Mumbai")).toBeDisabled());

    // Delhi is still out — it must not have been re-enabled by Mumbai.
    expect(yesIn("Delhi")).toBeDisabled();

    first.resolve(
      makeRow({ action_hash: "h1", segment_label: "Delhi", is_actionable: true }),
    );
    await waitFor(() => expect(yesIn("Delhi")).toBeEnabled());
    expect(yesIn("Mumbai")).toBeDisabled();  // still its own request

    second.resolve(
      makeRow({ action_hash: "h2", segment_label: "Mumbai", is_actionable: true }),
    );
    await waitFor(() => expect(yesIn("Mumbai")).toBeEnabled());
  });

  it("a failure on one row leaves the other row's answer intact", async () => {
    const user = userEvent.setup();
    await renderCard([
      makeRow({ action_hash: "h1", segment_label: "Delhi" }),
      makeRow({ action_hash: "h2", segment_label: "Mumbai" }),
    ]);

    const failing = deferred<ProsightActionable>();
    saveMock.mockImplementation((hash: string) =>
      hash === "h1"
        ? failing.promise
        : Promise.resolve(
            // The server copy replaces the row wholesale, so it has to carry
            // the label too — otherwise the row renames itself mid-test.
            makeRow({
              action_hash: "h2",
              segment_label: "Mumbai",
              is_actionable: true,
            }),
          ),
    );

    await user.click(yesIn("Delhi"));   // will fail, still in flight
    await user.click(yesIn("Mumbai"));  // succeeds meanwhile
    await waitFor(() =>
      expect(yesIn("Mumbai")).toHaveAttribute("aria-pressed", "true"),
    );

    failing.reject(new Error("HTTP 500"));

    await waitFor(() =>
      expect(yesIn("Delhi")).toHaveAttribute("aria-pressed", "false"),
    );
    // The regression a whole-list rollback snapshot would reintroduce.
    expect(yesIn("Mumbai")).toHaveAttribute("aria-pressed", "true");
  });
});
