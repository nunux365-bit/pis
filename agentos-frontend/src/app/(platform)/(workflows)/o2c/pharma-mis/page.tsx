import Link from "next/link";

export default function PharmaMisHubPage() {
  return (
    <div className="p-6 max-w-3xl space-y-4">
      <h1 className="text-base font-bold text-[var(--text-primary)]">Pharma MIS</h1>
      <p className="text-sm text-[var(--text-secondary)]">
        Select a billing workflow below.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        {/* SBI Pharmacy MIS — active */}
        <Link
          href="/o2c/pharma-mis/sbi/dashboard"
          className="group rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5 hover:border-[var(--accent-green)] hover:shadow-sm transition-all"
        >
          <p className="font-semibold text-sm text-[var(--text-primary)] group-hover:text-[var(--accent-green)] transition-colors">
            SBI Pharmacy MIS
          </p>
        </Link>

        {/* IIT Billing MIS — coming soon */}
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-elev)] p-5 opacity-60 cursor-not-allowed pointer-events-none">
          <div className="flex items-start justify-between gap-2">
            <p className="font-semibold text-sm text-[var(--text-muted)]">
              IIT Billing MIS
            </p>
            <span className="shrink-0 text-[10px] font-medium px-2 py-0.5 rounded bg-[var(--bg-card)] text-[var(--text-muted)] border border-[var(--border)]">
              Coming soon
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
