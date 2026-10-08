"use client";

type Props = {
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  variant?: "danger" | "warning";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  variant = "warning",
  busy,
  onConfirm,
  onCancel,
}: Props) {
  if (!open) return null;
  const confirmClass =
    variant === "danger"
      ? "bg-red-600 text-white"
      : "bg-amber-600 text-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40">
      <div
        role="dialog"
        aria-modal="true"
        className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 max-w-md w-full shadow-lg"
      >
        <h3 className="text-sm font-semibold mb-2">{title}</h3>
        <p className="text-sm text-[var(--text-secondary)] mb-4 whitespace-pre-wrap">{message}</p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="px-3 py-1.5 text-sm rounded-lg border border-[var(--border)] disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className={`px-3 py-1.5 text-sm rounded-lg font-semibold disabled:opacity-50 ${confirmClass}`}
          >
            {busy ? "…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
