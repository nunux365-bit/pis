import Link from "next/link";

type Session = {
  id: string;
  label: string;
  progress_pct: number;
  eta?: string;
};

export function SessionInspectorCard({ sessions }: { sessions: Session[] }) {
  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-2xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold">Session inspector</h3>
        <Link
          href="/sessions"
          className="text-xs text-[var(--accent-blue)] font-medium hover:underline"
        >
          View all
        </Link>
      </div>
      {sessions.length === 0 ?
        <p className="text-sm text-[var(--text-muted)] leading-relaxed">
          No agent sessions yet.{" "}
          <Link
            href="/sessions"
            className="text-[var(--accent-blue)] font-medium hover:underline"
          >
            Start a demo session
          </Link>{" "}
          to see durable threads here.
        </p>
      : <div className="flex flex-col gap-3">
        {sessions.map((s) => (
          <div key={s.id}>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-[var(--text-primary)] font-medium">
                {s.label}
              </span>
              <span className="text-[var(--text-muted)]">{s.progress_pct}%</span>
            </div>
            <div className="h-1.5 bg-[var(--bg-secondary)] rounded-full overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-[var(--accent-blue)] to-[var(--accent-purple)]"
                style={{ width: `${s.progress_pct}%` }}
              />
            </div>
            {s.eta && (
              <div className="text-[10px] text-[var(--text-muted)] mt-0.5">
                {s.eta}
              </div>
            )}
          </div>
        ))}
      </div>
      }
    </div>
  );
}
