/**
 * Runtime validation for the `/api/prosight/news` payload.
 *
 * This IS a validation boundary — the thing `adapters.ts` is explicitly *not*.
 * Both modules exist because finding 4 conflated them: `contract.ts` was named
 * like a guard while only doing compile-time adaptation, so it was renamed to
 * `adapters.ts` and the guard was written here instead.
 *
 * ## Why the envelope is strict and the interior is not
 *
 * The obvious move is a deep schema mirroring `ProsightNewsData`. That would be
 * worse than nothing here, for two reasons:
 *
 *  1. **False rejections take the dashboard down.** The numeric leaves are
 *     genuinely nullable in live payloads — `adapters.ts` screens every one of
 *     them through `finite()` precisely because `lower`/`upper`/`predicted`
 *     arrive null. A schema demanding `z.number()` would reject data that
 *     renders correctly today.
 *  2. **`series_timeseries` and `feature_importance` are polymorphic by
 *     design.** Per the note on `ProsightNewsData`, each map carries *both* the
 *     legacy flat keys (whose values are arrays) *and* per-BU sub-objects keyed
 *     by BU name (whose values are objects). Asserting "array" would reject the
 *     multi-BU shape; asserting "object" would reject the legacy one.
 *
 * So this validates what the *callers* actually depend on structurally, and
 * leaves the numeric interior to `finite()`, which already handles it and
 * degrades gracefully. Concretely it catches: a non-object response (an HTML
 * error page served with 200, a bare string, null), a missing or misnamed
 * `dates`, and `dates` holding non-strings.
 *
 * ## What it deliberately does not catch
 *
 * A renamed field *inside* a day object, or a string where a number is expected
 * three levels down. Those still flow through to `finite()` and become null.
 * Tightening further means pinning the day-level shape, which is worth doing
 * only once the backend stops evolving it.
 */
import { z } from "zod";

import type { ProsightNewsData } from "./types";

/** Thrown when the news payload is not shaped like `ProsightNewsData`. */
export class ProsightNewsSchemaError extends Error {
  readonly issues: string;

  constructor(issues: string) {
    super(`Prosight news data is malformed.\n${issues}`);
    this.name = "ProsightNewsSchemaError";
    this.issues = issues;
  }
}

// `looseObject`, not `object`: zod strips unknown keys by default, which would
// silently delete any field this schema does not declare. Live payloads already
// carry one (`generated_at`), and the backend adds more without warning —
// dropping them is the same silent drift finding 10 was about, just relocated.
// Validation here means "reject malformed", never "filter".
const newsSchema = z.looseObject({
  // Required: both fetch sites index this immediately — `NewsPage/index.tsx`
  // does `d.dates[0]` with no guard, so a missing array is a TypeError during
  // render rather than a caught fetch error.
  dates: z.array(z.string()),

  bus: z.array(z.string()).optional(),
  default_bu: z.string().optional(),
  non_reconciling_cuts: z.array(z.string()).optional(),
  display_contract: z.unknown().optional(),

  // Defaulted rather than required: an absent map renders as "no data", while
  // `undefined` would throw on the first index. Values stay `unknown` — see the
  // polymorphism note in the module docstring.
  data_by_date: z.record(z.string(), z.unknown()).default({}),
  series_timeseries: z.record(z.string(), z.unknown()).default({}),
  feature_importance: z.record(z.string(), z.unknown()).default({}),
});

/**
 * Validate a raw `/api/prosight/news` response.
 *
 * @throws {ProsightNewsSchemaError} with a human-readable path to the first bad
 * field. Both call sites already funnel thrown errors into their `setError`, so
 * a malformed payload surfaces as an on-screen message instead of a blank panel
 * or a `NaN`.
 */
export function parseProsightNews(raw: unknown): ProsightNewsData {
  const result = newsSchema.safeParse(raw);
  if (!result.success) {
    throw new ProsightNewsSchemaError(z.prettifyError(result.error));
  }
  // The interior is intentionally `unknown` (see docstring), so the validated
  // envelope has to be re-asserted to the declared type. This cast is the
  // honest boundary of what was checked — not a claim about the nested values.
  return result.data as unknown as ProsightNewsData;
}
