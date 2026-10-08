"use client";

/**
 * Placeholder for product-spec §13.5 — side-by-side document + agent analysis.
 * Wire to signed URLs / PDF viewer when backend serves documents.
 */
export function DocumentViewerPlaceholder({
  title = "Source document",
}: {
  title?: string;
}) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--bg-secondary)]/40 p-8 text-center text-sm text-[var(--text-muted)]">
      <p className="font-medium text-[var(--text-primary)] mb-1">{title}</p>
      <p className="text-xs">
        Invoice / rate card renders here next to extracted fields and deltas.
      </p>
    </div>
  );
}
