/**
 * The actionables feedback save flow, lifted out of ActionablesCard so it can be
 * tested without a DOM.
 *
 * This project has no jsdom/RTL (see actionablesFeedback.test.ts), so the flow
 * is expressed as a plain async function over an injected effects object rather
 * than as a hook. The component supplies React's setters; a test supplies spies
 * and a stub API, and can therefore assert the whole sequence — optimistic
 * write, the 404-refetch vs 500-rollback split, and the in-flight bookkeeping —
 * including two saves overlapping and responses landing out of order.
 *
 * Rules encoded here (each has a test):
 *  - The optimistic write lands before the request goes out.
 *  - A 404 means the row no longer exists after an upstream re-sync: do NOT roll
 *    back to a stale value, refetch instead.
 *  - Any other failure rolls back THIS row's two fields only — never the whole
 *    list, which would discard a concurrent save on another row.
 *  - The in-flight hash is always cleared, on every path.
 */
import type { ProsightActionable } from "@/lib/prosightApi";

/** The two user-editable fields; both are patched as a unit. */
export interface FeedbackValues {
  is_actionable: boolean | null;
  days_saved: number | null;
}

export type FeedbackPatch = Partial<FeedbackValues>;

/**
 * What a completed save leaves behind for the next save on the same row:
 * the row as it now stands, or `null` when the row is gone (404) and any
 * queued save for it should be abandoned rather than re-sent.
 */
type SaveOutcome = ProsightActionable | null;

export interface SaveEffects {
  /** The API call — injected so tests can reject with a chosen error. */
  save: (hash: string, next: FeedbackValues) => Promise<ProsightActionable>;
  setRows: (updater: (rows: ProsightActionable[]) => ProsightActionable[]) => void;
  setSaving: (updater: (hashes: Set<string>) => Set<string>) => void;
  setNotice: (msg: string | null) => void;
  /** Refetch the list (404 path only). */
  reload: () => void;
  /**
   * Per-row serialization chains, keyed by action_hash. Owned by the component
   * (a `useRef`) so it survives re-renders. Two saves for the *same* row must
   * not overlap: `disabled={saving}` cannot prevent it, because the days input
   * commits on blur and blur fires before the click that follows it — both
   * handlers run before React re-renders with the button disabled.
   */
  chains: Map<string, Promise<SaveOutcome>>;
}

export const NOTICE_REFRESHED =
  "Actionables were refreshed — please re-apply your answer.";
export const NOTICE_FAILED = "Couldn't save — please retry.";

/**
 * Does this failure mean "the row is gone" (refetch) rather than "the write
 * failed" (roll back)? The API layer surfaces HTTP failures as Error messages,
 * so this matches on the message text.
 */
export function isMissingRowError(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : "";
  return msg.includes("404") || msg.toLowerCase().includes("not found");
}

/** Merge a patch onto a row's current values; `undefined` means "leave alone". */
export function resolveNext(
  row: ProsightActionable,
  patch: FeedbackPatch,
): FeedbackValues {
  return {
    is_actionable:
      patch.is_actionable !== undefined ? patch.is_actionable : row.is_actionable,
    days_saved: patch.days_saved !== undefined ? patch.days_saved : row.days_saved,
  };
}

/** Replace one row by hash, leaving every other row's identity untouched. */
function patchRow(
  rows: ProsightActionable[],
  hash: string,
  change: (row: ProsightActionable) => ProsightActionable,
): ProsightActionable[] {
  return rows.map((r) => (r.action_hash === hash ? change(r) : r));
}

/**
 * Save one row's feedback. Saves on *different* rows run concurrently; saves on
 * the *same* row are queued behind each other, and each one merges onto the
 * previous one's result rather than onto the row captured when it was clicked.
 *
 * Queueing rather than dropping matters: the two controls patch different
 * fields, so a dropped save is a silently lost edit. Merging onto the carried
 * result is what stops the second save's payload from resurrecting the first's
 * pre-save values.
 */
export async function runSaveFeedback(
  row: ProsightActionable,
  patch: FeedbackPatch,
  fx: SaveEffects,
): Promise<void> {
  const hash = row.action_hash;
  const prior = fx.chains.get(hash);

  const mine: Promise<SaveOutcome> = (prior ?? Promise.resolve<SaveOutcome>(row))
    // A failed predecessor must not cancel this save; it hands back the row as
    // it stands (rolled back) and we merge onto that.
    .catch(() => row)
    .then((carried) =>
      // `null` means a 404 already established the row is gone — re-sending
      // would only trigger another refetch.
      carried === null ? null : performSave(carried, patch, fx),
    );

  fx.chains.set(hash, mine);
  // Flagged for the whole queue, not per request: clearing between two queued
  // saves would re-enable the row mid-sequence.
  fx.setSaving((hashes) => new Set(hashes).add(hash));

  try {
    await mine;
  } finally {
    // Only the last save queued for this row clears the flag and the chain —
    // `chains.get(hash) === mine` is false while another is still behind us.
    if (fx.chains.get(hash) === mine) {
      fx.chains.delete(hash);
      fx.setSaving((hashes) => {
        const rest = new Set(hashes);
        rest.delete(hash);
        return rest;
      });
    }
  }
}

/** One request. `base` is the row this save merges onto — never a stale prop. */
async function performSave(
  base: ProsightActionable,
  patch: FeedbackPatch,
  fx: SaveEffects,
): Promise<SaveOutcome> {
  const hash = base.action_hash;
  const next = resolveNext(base, patch);
  // This row's pre-patch values only — a whole-list snapshot would clobber a
  // concurrent save on another row when restored.
  const prev: FeedbackValues = {
    is_actionable: base.is_actionable,
    days_saved: base.days_saved,
  };

  fx.setRows((rows) => patchRow(rows, hash, (r) => ({ ...r, ...next })));
  fx.setNotice(null);

  try {
    const updated = await fx.save(hash, next);
    fx.setRows((rows) => patchRow(rows, hash, () => updated));
    return updated;
  } catch (e) {
    if (isMissingRowError(e)) {
      // The list changed under us after a re-sync. Rolling back would restore a
      // row the server no longer has, so refetch instead.
      fx.setNotice(NOTICE_REFRESHED);
      fx.reload();
      return null;
    }
    fx.setRows((rows) => patchRow(rows, hash, (r) => ({ ...r, ...prev })));
    fx.setNotice(NOTICE_FAILED);
    // Rolled back — a queued save must merge onto these values, not the ones
    // this attempt tried and failed to write.
    return { ...base, ...prev };
  }
}
