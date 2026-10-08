/**
 * Interaction-level tests for the actionables feedback save flow.
 *
 * NOTE ON APPROACH: the ask was a React Testing Library test. RTL, @testing-
 * library/user-event and jsdom are all absent from this project (every existing
 * suite is a node-environment unit test) and cannot be installed here — the
 * registry is behind a TLS-intercepting proxy that fails every `npm install`
 * with UNABLE_TO_GET_ISSUER_CERT_LOCALLY. So the flow was extracted into
 * ./actionablesFeedback and is driven directly, with a fake store standing in
 * for React state.
 *
 * What that buys: the full sequence is asserted — optimistic write, the
 * 404-refetch vs 500-rollback split, in-flight bookkeeping, and overlapping
 * saves with out-of-order responses.
 * What it does not cover: that the buttons are wired to this flow, that
 * `disabled` actually follows the in-flight set, or anything about rendering.
 * Those need RTL; see the note at the bottom of this file.
 */
import { describe, expect, it, vi } from "vitest";
import {
  runSaveFeedback,
  isMissingRowError,
  resolveNext,
  NOTICE_FAILED,
  NOTICE_REFRESHED,
  type FeedbackValues,
  type SaveEffects,
} from "./actionablesFeedback";
import type { ProsightActionable } from "@/lib/prosightApi";

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeActionable(
  overrides: Partial<ProsightActionable> = {},
): ProsightActionable {
  return {
    action_hash: "h1",
    as_of_date: "2026-08-17",
    bu: "pharmacy",
    segment: "delhi",
    lens: "BOTH LENSES",
    rank: 1,
    action: "legacy string",
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
    run_days: 3,
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

/**
 * Stands in for the component's useState. `setRows`/`setSaving` take the same
 * functional updaters the component passes, so the reducers under test run for
 * real against accumulated state.
 */
function makeStore(initialRows: ProsightActionable[]) {
  const state = {
    rows: initialRows,
    saving: new Set<string>(),
    notice: null as string | null,
    reloads: 0,
  };
  // One map per store, standing in for the component's useRef — shared across
  // every effects object this store hands out, exactly as the component shares
  // one map across renders.
  const chains = new Map<string, Promise<ProsightActionable | null>>();
  const effects = (
    save: SaveEffects["save"],
  ): SaveEffects => ({
    save,
    chains,
    setRows: (updater) => {
      state.rows = updater(state.rows);
    },
    setSaving: (updater) => {
      state.saving = updater(state.saving);
    },
    setNotice: (msg) => {
      state.notice = msg;
    },
    reload: () => {
      state.reloads += 1;
    },
  });
  const rowFor = (hash: string) => state.rows.find((r) => r.action_hash === hash)!;
  return { state, effects, rowFor };
}

const httpError = (status: number) => new Error(`HTTP ${status}: request failed`);

// ── Error classification ─────────────────────────────────────────────────────

describe("isMissingRowError", () => {
  it("treats 404 and 'not found' as a missing row", () => {
    expect(isMissingRowError(new Error("HTTP 404: Not Found"))).toBe(true);
    expect(isMissingRowError(new Error("actionable not found"))).toBe(true);
    expect(isMissingRowError(new Error("NOT FOUND"))).toBe(true);
  });

  it("treats other failures as save failures", () => {
    expect(isMissingRowError(new Error("HTTP 500: server error"))).toBe(false);
    expect(isMissingRowError(new Error("Failed to fetch"))).toBe(false);
    expect(isMissingRowError("a bare string")).toBe(false);
    expect(isMissingRowError(undefined)).toBe(false);
  });
});

describe("resolveNext", () => {
  it("merges only the patched field, preserving the other", () => {
    const row = makeActionable({ is_actionable: true, days_saved: 2 });
    expect(resolveNext(row, { days_saved: 5 })).toEqual({
      is_actionable: true,
      days_saved: 5,
    });
    expect(resolveNext(row, { is_actionable: null })).toEqual({
      is_actionable: null,
      days_saved: 2,
    });
  });

  it("distinguishes an explicit null from an absent field", () => {
    const row = makeActionable({ is_actionable: true, days_saved: 2 });
    // Clearing the answer is `null`, not "leave alone".
    expect(resolveNext(row, { is_actionable: null }).is_actionable).toBeNull();
    expect(resolveNext(row, {}).is_actionable).toBe(true);
  });
});

// ── The save flow ────────────────────────────────────────────────────────────

describe("runSaveFeedback — success", () => {
  it("writes optimistically, then replaces the row with the server's copy", async () => {
    const row = makeActionable();
    const { state, effects, rowFor } = makeStore([row, makeActionable({ action_hash: "h2" })]);

    let sawDuringRequest: ProsightActionable | null = null;
    const save = vi.fn(async () => {
      // Mid-flight: the optimistic value is already visible and the row is busy.
      sawDuringRequest = rowFor("h1");
      expect(state.saving.has("h1")).toBe(true);
      return makeActionable({
        is_actionable: true,
        days_saved: 3,
        feedback_updated_by: "analytics.jira@1mg.com",
      });
    });

    await runSaveFeedback(row, { is_actionable: true }, effects(save));

    expect(sawDuringRequest!.is_actionable).toBe(true);
    expect(save).toHaveBeenCalledWith("h1", { is_actionable: true, days_saved: null });
    // Server copy wins — it carries attribution the optimistic value lacked.
    expect(rowFor("h1").feedback_updated_by).toBe("analytics.jira@1mg.com");
    expect(rowFor("h1").days_saved).toBe(3);
    expect(state.saving.size).toBe(0);
    expect(state.notice).toBeNull();
    expect(state.reloads).toBe(0);
  });

  it("clears a stale notice when a retry starts", async () => {
    const row = makeActionable();
    const { state, effects } = makeStore([row]);
    state.notice = NOTICE_FAILED;

    await runSaveFeedback(row, { is_actionable: false }, effects(async () => makeActionable()));

    expect(state.notice).toBeNull();
  });
});

describe("runSaveFeedback — 500 rolls back", () => {
  it("restores the row's previous values and shows the retry notice", async () => {
    const row = makeActionable({ is_actionable: true, days_saved: 4 });
    const { state, effects, rowFor } = makeStore([row]);

    await runSaveFeedback(
      row,
      { is_actionable: false, days_saved: 9 },
      effects(async () => {
        throw httpError(500);
      }),
    );

    expect(rowFor("h1").is_actionable).toBe(true); // rolled back
    expect(rowFor("h1").days_saved).toBe(4);
    expect(state.notice).toBe(NOTICE_FAILED);
    expect(state.reloads).toBe(0); // no refetch on a plain failure
    expect(state.saving.size).toBe(0);
  });

  it("rolls back only the failing row, not a concurrent save on another", async () => {
    // The regression that a whole-list snapshot would reintroduce: h2's
    // in-flight optimistic value must survive h1's failure.
    const h1 = makeActionable({ action_hash: "h1", is_actionable: true });
    const h2 = makeActionable({ action_hash: "h2", is_actionable: null });
    const { state, effects, rowFor } = makeStore([h1, h2]);

    let releaseH1: () => void = () => {};
    const h1Blocked = new Promise<void>((r) => {
      releaseH1 = r;
    });

    const failH1 = runSaveFeedback(
      h1,
      { is_actionable: false },
      effects(async () => {
        await h1Blocked;
        throw httpError(500);
      }),
    );

    // While h1 is still out, h2 is optimistically marked.
    const okH2 = runSaveFeedback(
      h2,
      { is_actionable: true },
      effects(async () => makeActionable({ action_hash: "h2", is_actionable: true })),
    );
    await okH2;
    expect(rowFor("h2").is_actionable).toBe(true);

    releaseH1();
    await failH1;

    expect(rowFor("h1").is_actionable).toBe(true); // h1 restored
    expect(rowFor("h2").is_actionable).toBe(true); // h2 NOT clobbered
    expect(state.saving.size).toBe(0);
  });

  it("rolls back on a network-style rejection too", async () => {
    const row = makeActionable({ is_actionable: true });
    const { state, effects, rowFor } = makeStore([row]);

    await runSaveFeedback(
      row,
      { is_actionable: false },
      effects(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    expect(rowFor("h1").is_actionable).toBe(true);
    expect(state.notice).toBe(NOTICE_FAILED);
  });
});

describe("runSaveFeedback — 404 refetches", () => {
  it("refetches instead of rolling back, and says so", async () => {
    const row = makeActionable({ is_actionable: true });
    const { state, effects } = makeStore([row]);

    await runSaveFeedback(
      row,
      { is_actionable: false },
      effects(async () => {
        throw httpError(404);
      }),
    );

    expect(state.reloads).toBe(1);
    expect(state.notice).toBe(NOTICE_REFRESHED);
    expect(state.saving.size).toBe(0);
  });

  it("does not restore the stale value — the server no longer has that row", async () => {
    // Rolling back here would resurrect a row the re-sync removed; the refetch
    // is what makes the list correct again.
    const row = makeActionable({ is_actionable: true, days_saved: 4 });
    const { effects, rowFor } = makeStore([row]);

    await runSaveFeedback(
      row,
      { is_actionable: false, days_saved: 9 },
      effects(async () => {
        throw httpError(404);
      }),
    );

    // The optimistic value is left in place for the refetch to overwrite,
    // rather than being reverted to a value the server disagrees with.
    expect(rowFor("h1").is_actionable).toBe(false);
    expect(rowFor("h1").days_saved).toBe(9);
  });

  it("matches a 'not found' message without a status code", async () => {
    const row = makeActionable();
    const { state, effects } = makeStore([row]);

    await runSaveFeedback(
      row,
      { is_actionable: true },
      effects(async () => {
        throw new Error("Actionable not found");
      }),
    );

    expect(state.reloads).toBe(1);
    expect(state.notice).toBe(NOTICE_REFRESHED);
  });
});

// ── In-flight bookkeeping (the savingHash race) ──────────────────────────────

describe("runSaveFeedback — in-flight tracking", () => {
  it("keeps both rows marked busy while both are in flight", async () => {
    const h1 = makeActionable({ action_hash: "h1" });
    const h2 = makeActionable({ action_hash: "h2" });
    const { state, effects } = makeStore([h1, h2]);

    let release1: () => void = () => {};
    let release2: () => void = () => {};
    const gate1 = new Promise<void>((r) => (release1 = r));
    const gate2 = new Promise<void>((r) => (release2 = r));

    const p1 = runSaveFeedback(h1, { is_actionable: true }, effects(async () => {
      await gate1;
      return makeActionable({ action_hash: "h1", is_actionable: true });
    }));
    const p2 = runSaveFeedback(h2, { is_actionable: true }, effects(async () => {
      await gate2;
      return makeActionable({ action_hash: "h2", is_actionable: true });
    }));

    // The single-string `savingHash` this replaced would show only h2 here,
    // re-enabling h1's buttons while its request was still out.
    expect(state.saving.has("h1")).toBe(true);
    expect(state.saving.has("h2")).toBe(true);

    // Finish out of order: h2 first.
    release2();
    await p2;
    expect(state.saving.has("h1")).toBe(true); // h1 still busy
    expect(state.saving.has("h2")).toBe(false);

    release1();
    await p1;
    expect(state.saving.size).toBe(0);
  });

  it("clears the in-flight hash on every outcome", async () => {
    const row = makeActionable();
    for (const save of [
      async () => makeActionable(),
      async () => {
        throw httpError(500);
      },
      async () => {
        throw httpError(404);
      },
    ]) {
      const { state, effects } = makeStore([row]);
      await runSaveFeedback(row, { is_actionable: true }, effects(save));
      expect(state.saving.size).toBe(0);
    }
  });

  it("does not reject even though the API call did", async () => {
    // The component calls this without awaiting; an escaping rejection would
    // surface as an unhandled promise rejection.
    const row = makeActionable();
    const { effects } = makeStore([row]);
    await expect(
      runSaveFeedback(row, { is_actionable: true }, effects(async () => {
        throw httpError(500);
      })),
    ).resolves.toBeUndefined();
  });
});

// STILL UNCOVERED (needs RTL + jsdom, blocked on the registry):
//  - buttons are actually disabled while their own row is in flight
//  - clicking the active option clears the answer (passes is_actionable: null)
//  - the days input commits on blur/Enter and discards out-of-range drafts
//  - UnifiedDimensionBreakdown's drill-down state

// ── Same-row serialization (the blur→click race) ─────────────────────────────
//
// `disabled={saving}` cannot prevent two saves on one row: the days input
// commits on blur, and blur fires before the click that caused it, so both
// handlers run before React re-renders with the button disabled. These pin the
// queueing that makes that safe.


/** Let queued microtasks run. The save chain resolves through several ticks
 *  before the request is issued, so a single `await Promise.resolve()` is not
 *  enough to observe it. */
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("runSaveFeedback — two saves on the SAME row", () => {
  it("runs them in order instead of concurrently", async () => {
    const row = makeActionable();
    const { effects } = makeStore([row]);
    const events: string[] = [];

    let release1: () => void = () => {};
    const gate1 = new Promise<void>((r) => (release1 = r));

    const p1 = runSaveFeedback(row, { days_saved: 5 }, effects(async () => {
      events.push("save1:start");
      await gate1;
      events.push("save1:end");
      return makeActionable({ days_saved: 5 });
    }));
    const p2 = runSaveFeedback(row, { is_actionable: true }, effects(async () => {
      events.push("save2:start");
      return makeActionable({ is_actionable: true, days_saved: 5 });
    }));

    await flush();
    // Save 2 must not have issued a request while save 1 is outstanding.
    expect(events).toEqual(["save1:start"]);

    release1();
    await Promise.all([p1, p2]);
    expect(events).toEqual(["save1:start", "save1:end", "save2:start"]);
  });

  it("keeps the row flagged busy across the whole queue", async () => {
    const row = makeActionable();
    const { state, effects } = makeStore([row]);
    let release1: () => void = () => {};
    const gate1 = new Promise<void>((r) => (release1 = r));

    const p1 = runSaveFeedback(row, { days_saved: 5 }, effects(async () => {
      await gate1;
      return makeActionable({ days_saved: 5 });
    }));
    const p2 = runSaveFeedback(row, { is_actionable: true }, effects(async () =>
      makeActionable({ is_actionable: true, days_saved: 5 })));

    expect(state.saving.has("h1")).toBe(true);
    release1();
    await p1;
    // Save 2 is still queued — the flag must not have cleared with save 1.
    expect(state.saving.has("h1")).toBe(true);
    await p2;
    expect(state.saving.size).toBe(0);
  });

  it("does not lose the first edit — the second merges onto its result", async () => {
    const row = makeActionable();
    const { state, effects } = makeStore([row]);
    const sent: FeedbackValues[] = [];
    const record = async (_h: string, next: FeedbackValues) => {
      sent.push(next);
      return makeActionable(next);
    };

    // Both hold the same click-time `row` (days_saved: null) — the realistic
    // blur→click pair.
    const p1 = runSaveFeedback(row, { days_saved: 5 }, effects(record));
    const p2 = runSaveFeedback(row, { is_actionable: true }, effects(record));
    await Promise.all([p1, p2]);

    expect(sent).toEqual([
      { is_actionable: null, days_saved: 5 },
      { is_actionable: true, days_saved: 5 }, // carries the first save's result
    ]);
    expect(state.rows[0].days_saved).toBe(5);
    expect(state.rows[0].is_actionable).toBe(true);
  });

  it("a failed save does not cancel the one queued behind it", async () => {
    const row = makeActionable({ days_saved: 2 });
    const { state, effects } = makeStore([row]);
    const sent: FeedbackValues[] = [];

    const p1 = runSaveFeedback(row, { days_saved: 9 }, effects(async () => {
      throw httpError(500);
    }));
    const p2 = runSaveFeedback(row, { is_actionable: true }, effects(
      async (_h: string, next: FeedbackValues) => {
        sent.push(next);
        return makeActionable(next);
      },
    ));
    await Promise.all([p1, p2]);

    // Save 2 ran, and merged onto the ROLLED-BACK value (2), not the failed 9.
    expect(sent).toEqual([{ is_actionable: true, days_saved: 2 }]);
    expect(state.rows[0].days_saved).toBe(2);
  });

  it("abandons a queued save when a 404 says the row is gone", async () => {
    const row = makeActionable();
    const { state, effects } = makeStore([row]);
    const second = vi.fn();

    const p1 = runSaveFeedback(row, { days_saved: 5 }, effects(async () => {
      throw httpError(404);
    }));
    const p2 = runSaveFeedback(row, { is_actionable: true }, effects(second));
    await Promise.all([p1, p2]);

    expect(second).not.toHaveBeenCalled();  // no re-send against a deleted row
    expect(state.reloads).toBe(1);          // and only one refetch, not two
    expect(state.saving.size).toBe(0);
  });

  it("still runs DIFFERENT rows concurrently", async () => {
    const h1 = makeActionable({ action_hash: "h1" });
    const h2 = makeActionable({ action_hash: "h2" });
    const { state, effects } = makeStore([h1, h2]);
    const inFlight: string[] = [];

    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const track = (hash: string) => async () => {
      inFlight.push(hash);
      await gate;
      return makeActionable({ action_hash: hash, is_actionable: true });
    };

    const p1 = runSaveFeedback(h1, { is_actionable: true }, effects(track("h1")));
    const p2 = runSaveFeedback(h2, { is_actionable: true }, effects(track("h2")));

    await flush();
    // Serialization is per row — these must overlap.
    expect(inFlight.sort()).toEqual(["h1", "h2"]);
    expect(state.saving.has("h1") && state.saving.has("h2")).toBe(true);

    release();
    await Promise.all([p1, p2]);
    expect(state.saving.size).toBe(0);
  });

  it("survives a predecessor that rejects outright", async () => {
    // `performSave` catches its own API failures, so the chain's `.catch` is
    // only reachable if an effect itself throws (a bad setter, a render-time
    // error). Without it that rejection would propagate into the queued save
    // and silently drop it.
    const row = makeActionable({ days_saved: 2 });
    const { state, effects } = makeStore([row]);
    const sent: FeedbackValues[] = [];

    const exploding = effects(async () => makeActionable());
    let firstCall = true;
    const boom: SaveEffects = {
      ...exploding,
      setRows: (updater) => {
        if (firstCall) {
          firstCall = false;
          throw new Error("setRows exploded");
        }
        exploding.setRows(updater);
      },
    };

    const p1 = runSaveFeedback(row, { days_saved: 9 }, boom).catch(() => {});
    const p2 = runSaveFeedback(
      row,
      { is_actionable: true },
      effects(async (_h: string, next: FeedbackValues) => {
        sent.push(next);
        return makeActionable(next);
      }),
    );
    await Promise.all([p1, p2]);

    // The queued save still ran, merging onto the click-time row.
    expect(sent).toEqual([{ is_actionable: true, days_saved: 2 }]);
    expect(state.saving.size).toBe(0);
  });
});
