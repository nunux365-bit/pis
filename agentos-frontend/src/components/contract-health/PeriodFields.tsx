"use client";

type Props = {
  periodStart: string;
  periodEnd: string;
  onChange: (start: string, end: string) => void;
};

export function PeriodFields({ periodStart, periodEnd, onChange }: Props) {
  const invalid = Boolean(periodStart && periodEnd && periodStart > periodEnd);
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-[var(--text-muted)]">Billing period</span>
      <input
        type="date"
        value={periodStart}
        onChange={(e) => onChange(e.target.value, periodEnd)}
        className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-elev)] text-xs"
      />
      <span className="text-[var(--text-muted)]">to</span>
      <input
        type="date"
        value={periodEnd}
        onChange={(e) => onChange(periodStart, e.target.value)}
        className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-elev)] text-xs"
      />
      {invalid && <span className="text-xs text-red-500">Start must be before end</span>}
    </div>
  );
}
