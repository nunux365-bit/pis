"use client";

/**
 * Accessible inline error for API / form failures. Prefer over raw JSON in UI.
 */
export function ErrorBanner({
  children,
  onRetry,
  retryLabel = "Try again",
}: {
  children: React.ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div
      role="alert"
      aria-live="polite"
      className="rounded-xl border border-[var(--accent-red)]/35 bg-[rgba(239,68,68,0.08)] px-4 py-3 text-sm text-red-950"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 whitespace-pre-wrap leading-relaxed">{children}</div>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="shrink-0 rounded-lg border border-red-300/80 bg-[var(--bg-card)] px-3 py-1.5 text-xs font-semibold text-red-900 shadow-sm hover:bg-red-50"
          >
            {retryLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}
