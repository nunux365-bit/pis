/**
 * Procurement **presentation** primitives (chrome, dock, buttons). No data fetching — keep API usage in
 * pages or ``@/lib/procurementApi``.
 */
import type { ReactNode } from "react";
import Link from "next/link";

/** Back navigation — reads as a control, not a raw “←” text link. */
export function ProcurementBackLink({
  href,
  children,
  dense = false,
}: {
  href: string;
  children: ReactNode;
  /** Tighter control for dense editors (reclaims vertical space under the app bar). */
  dense?: boolean;
}) {
  const icon = dense ? 14 : 16;
  return (
    <Link
      href={href}
      className={`group inline-flex items-center rounded-lg pl-0.5 pr-2 font-medium text-[var(--text-secondary)] outline-none transition hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)] focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/40 ${
        dense ? "mb-1 gap-1.5 py-0.5 text-sm" : "mb-3 gap-2 py-1 text-sm"
      }`}
    >
      <span
        className={`flex shrink-0 items-center justify-center rounded-lg border border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-muted)] shadow-sm transition group-hover:border-[var(--border-active)] group-hover:text-[var(--accent-green)] ${
          dense ? "h-6 w-6" : "h-8 w-8"
        }`}
        aria-hidden
      >
        <svg width={icon} height={icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M15 18l-6-6 6-6" />
        </svg>
      </span>
      <span>{children}</span>
    </Link>
  );
}

/** Two-step flow indicator for PR/PO editors — reads like a progress strip, not engineering steps. */
export function ProcurementFlowSteps({
  step,
  labels,
  compact = false,
  denseStrip = false,
}: {
  step: 1 | 2;
  labels: [string, string];
  /** Tighter strip — less vertical space on data-entry pages. */
  compact?: boolean;
  /** Minimal chrome for dense editors (PR workspace). */
  denseStrip?: boolean;
}) {
  const stripPad = denseStrip
    ? "mb-1 gap-1.5 rounded-lg border border-[var(--border)]/80 p-1"
    : compact
      ? "mb-3 gap-2 rounded-2xl border border-[var(--border)]/80 p-2"
      : "mb-6 gap-2 rounded-2xl border border-[var(--border)]/80 p-3";
  return (
    <nav
      aria-label="Progress"
      className={`flex flex-wrap items-center bg-[var(--bg-elev)]/90 shadow-inner ${stripPad}`}
    >
      {[1, 2].map((n) => {
        const active = step === n;
        const done = step > n;
        return (
          <div key={n} className="flex items-center gap-2">
            {n === 2 ? (
              <div
                className="hidden h-px w-8 shrink-0 bg-gradient-to-r from-transparent via-[var(--border)] to-transparent sm:block"
                aria-hidden
              />
            ) : null}
            <span
              className={`inline-flex items-center gap-2 rounded-xl border font-semibold tracking-wide transition ${
                denseStrip ? "px-2 py-1 text-[11px] sm:text-xs" : compact ? "px-2.5 py-1.5 text-[11px]" : "px-3 py-2 text-xs"
              } ${
                active
                  ? "border-[var(--accent-green)] bg-[var(--bg-card)] text-[var(--accent-green)] shadow-sm"
                  : done
                    ? "border-transparent bg-[var(--bg-card)]/70 text-[var(--text-secondary)]"
                    : "border-transparent bg-transparent text-[var(--text-muted)]"
              }`}
            >
              <span
                className={`flex shrink-0 items-center justify-center rounded-full font-bold ${
                  denseStrip ? "h-5 w-5 text-[10px]" : compact ? "h-5 w-5 text-[10px]" : "h-6 w-6 text-[11px]"
                } ${
                  done
                    ? "bg-[var(--accent-green)] text-white shadow-sm"
                    : active
                      ? "bg-[rgba(21,163,115,0.2)] text-[var(--accent-green)]"
                      : "bg-[var(--bg-secondary)] text-[var(--text-muted)] ring-1 ring-[var(--border)]"
                }`}
                aria-hidden
              >
                {done ? "✓" : n}
              </span>
              {labels[n - 1]}
            </span>
          </div>
        );
      })}
    </nav>
  );
}

/**
 * Form actions bar — hint + buttons.
 * - **sticky** (default): sticks to the bottom of the scroll parent (can overlap tall forms).
 * - **pinned**: sits in normal flex flow under a scroll region — actions stay in the viewport
 *   without covering fields when the page uses `flex flex-1 min-h-0` + inner `overflow-y-auto`.
 */
export function ProcurementFormDock({
  children,
  hint,
  variant = "sticky",
}: {
  children: ReactNode;
  hint?: ReactNode;
  /** `pinned` = non-overlapping bar for editor pages inside AppShell. */
  variant?: "sticky" | "pinned";
}) {
  const shell =
    variant === "sticky"
      ? "sticky bottom-0 z-20 mt-8 min-w-0 border-t border-[var(--border)] bg-[var(--bg-card)]/98 pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-2 shadow-[0_-1px_0_rgba(0,0,0,0.04)] backdrop-blur-md supports-[backdrop-filter]:bg-[var(--bg-card)]/92"
      : "relative z-10 mt-0 min-w-0 shrink-0 border-t border-[var(--border)] bg-[var(--bg-card)] pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-2 shadow-[0_-6px_20px_-6px_rgba(31,42,36,0.08)]";

  const innerPad = variant === "pinned" ? "px-3 py-1.5 sm:px-4" : "px-4 py-2 sm:px-5";
  const hintClass =
    variant === "pinned"
      ? "order-2 min-w-0 flex-1 text-xs leading-snug text-[var(--text-muted)] sm:order-1"
      : "order-2 min-w-0 flex-1 break-words text-[11px] leading-snug text-[var(--text-muted)] sm:order-1 sm:text-xs";

  return (
    <div className={shell} role="region" aria-label="Form actions">
      <div className={`flex min-h-0 min-w-0 flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3 ${innerPad}`}>
        {hint ? (
          <p className={hintClass}>
            {hint}
          </p>
        ) : null}
        <div
          className={
            hint
              ? "order-1 flex w-full shrink-0 items-center justify-stretch gap-2 sm:order-2 sm:ml-auto sm:w-auto sm:justify-end"
              : "flex w-full shrink-0 items-center justify-end gap-2"
          }
        >
          {children}
        </div>
      </div>
    </div>
  );
}

export const btnGhost =
  "inline-flex h-10 min-w-[5.5rem] flex-1 items-center justify-center rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-4 text-sm font-semibold text-[var(--text-primary)] outline-none transition hover:bg-[var(--bg-elev)] focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30 active:bg-[var(--bg-elev)] disabled:opacity-50 sm:h-10 sm:flex-initial";

export const btnPrimary =
  "inline-flex h-10 min-w-0 flex-1 items-center justify-center gap-2 rounded-lg bg-[var(--accent-green)] px-5 text-sm font-semibold text-white outline-none ring-1 ring-black/[0.06] transition hover:brightness-[1.05] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-green)] active:brightness-[0.95] disabled:pointer-events-none disabled:opacity-45 sm:min-w-[9.5rem] sm:flex-initial sm:px-6";

export function ProcurementButtonCancel({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className={btnGhost}>
      {children}
    </Link>
  );
}

export function ProcurementButtonSubmit({
  busy,
  children,
  busyLabel,
}: {
  busy: boolean;
  children: ReactNode;
  busyLabel: string;
}) {
  return (
    <button type="submit" disabled={busy} className={btnPrimary} aria-busy={busy}>
      {busy ? (
        <>
          <span
            className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-white/35 border-t-white"
            aria-hidden
          />
          <span>{busyLabel}</span>
        </>
      ) : (
        children
      )}
    </button>
  );
}
