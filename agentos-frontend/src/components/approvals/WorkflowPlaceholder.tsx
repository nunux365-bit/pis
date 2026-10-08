import type { ReactNode } from "react";

type Props = {
  title: string;
  children: ReactNode;
};

export function WorkflowPlaceholder({ title, children }: Props) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-6">
      <h2 className="text-sm font-semibold text-[var(--text-primary)]">{title}</h2>
      <div className="mt-2 max-w-xl text-[12px] leading-relaxed text-[var(--text-secondary)]">
        {children}
      </div>
    </div>
  );
}
