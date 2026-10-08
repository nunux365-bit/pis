"use client";

import type { ReactNode } from "react";

import { lifecycleStageLabel } from "@/lib/responderEvalLabels";

type Props = {
  artifact: Record<string, unknown>;
  lifecycleStage?: unknown;
};

function formatValue(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function KeyValueGrid({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(([, v]) => v != null && v !== "");
  if (!entries.length) {
    return <p className="text-sm text-[var(--text-muted)]">No data</p>;
  }
  return (
    <dl className="grid gap-2 sm:grid-cols-2">
      {entries.map(([key, value]) => (
        <div key={key} className="rounded-lg border border-[var(--border)]/60 bg-[var(--bg-primary)] px-3 py-2">
          <dt className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
            {key.replaceAll("_", " ")}
          </dt>
          <dd className="mt-0.5 break-words text-sm text-[var(--text-primary)]">{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-3">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">{title}</h3>
      {children}
    </section>
  );
}

type ChronologyRow = {
  label?: string;
  status_id?: string;
  at?: string;
  at_display?: string;
  source_label?: string;
};

function StatusTimeline({ rows }: { rows: unknown }) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return <p className="text-sm text-[var(--text-muted)]">No status timeline in ground truth.</p>;
  }
  const items = rows as ChronologyRow[];
  return (
    <ol className="relative space-y-0 border-l border-[var(--border)] pl-4">
      {items.map((row, i) => (
        <li key={`${row.at ?? i}-${row.label ?? i}`} className="relative pb-3 last:pb-0">
          <span className="absolute -left-[1.3rem] top-1.5 h-2 w-2 rounded-full bg-violet-500" />
          <p className="text-sm font-medium text-[var(--text-primary)]">
            {row.label ?? row.status_id ?? "Status"}
          </p>
          <p className="text-xs text-[var(--text-muted)]">
            {row.at_display ?? row.at ?? "—"}
            {row.source_label ? ` · ${row.source_label}` : null}
          </p>
        </li>
      ))}
    </ol>
  );
}

function EventList({ title, items }: { title: string; items: unknown }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <Section title={title}>
      <ul className="space-y-2 text-xs">
        {items.map((item, i) => (
          <li
            key={i}
            className="rounded-lg border border-[var(--border)]/60 bg-[var(--bg-card)] px-3 py-2 font-mono text-[var(--text-secondary)]"
          >
            {typeof item === "string" ? item : JSON.stringify(item)}
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function ResponderEvalGroundTruthPanel({ artifact, lifecycleStage }: Props) {
  const preflight = (artifact.preflight ?? {}) as Record<string, unknown>;
  const orderOps = (artifact.order_ops ?? {}) as Record<string, unknown>;
  const payment = (orderOps.payment_summary ?? {}) as Record<string, unknown>;
  const shipment = (orderOps.shipment_detail ?? {}) as Record<string, unknown>;
  const operations = (artifact.operations ?? {}) as Record<string, unknown>;
  const perfectOrder = (artifact.perfect_order ?? {}) as Record<string, unknown>;
  const warnings = Array.isArray(artifact.warnings) ? artifact.warnings : [];

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-violet-500/25 bg-violet-50/40 px-4 py-3 ">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-violet-700 ">
          RCA verdict
        </p>
        <p className="mt-1 text-sm text-[var(--text-primary)]">
          {formatValue(artifact.rca_verdict)}
        </p>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          {artifact.order_id ? (
            <span className="rounded-md bg-black/5 px-2 py-1 font-mono ">
              Order: {String(artifact.order_id)}
            </span>
          ) : null}
          {artifact.run_id ? (
            <span className="rounded-md bg-black/5 px-2 py-1 font-mono ">
              Run: {String(artifact.run_id)}
            </span>
          ) : null}
          {lifecycleStage ? (
            <span className="rounded-md bg-black/5 px-2 py-1 ">
              Lifecycle: {lifecycleStageLabel(lifecycleStage)}
            </span>
          ) : null}
        </div>
      </div>

      <Section title="Order status (preflight)">
        <KeyValueGrid data={preflight} />
      </Section>

      <Section title="Status timeline (RCA)">
        <StatusTimeline rows={operations.status_chronology} />
        {operations.timeline_note ? (
          <p className="mt-2 text-xs text-[var(--text-muted)]">{String(operations.timeline_note)}</p>
        ) : null}
      </Section>

      {operations.return_followed != null || operations.return_note ? (
        <Section title="Return workflow">
          <KeyValueGrid
            data={{
              return_followed: operations.return_followed,
              return_note: operations.return_note,
              return_reason: preflight.return_reason,
            }}
          />
        </Section>
      ) : null}

      <EventList title="Logistics segments" items={operations.segments} />
      <EventList title="Clickpost events" items={operations.clickpost_events} />

      <Section title="Payment summary">
        <KeyValueGrid data={payment} />
      </Section>

      <Section title="Shipment">
        <KeyValueGrid data={shipment} />
      </Section>

      <Section title="Operations summary">
        <KeyValueGrid
          data={{
            shipping_summary: operations.shipping_summary,
            last_mile_mode: operations.last_mile_mode,
            history_count: operations.history_count,
            split_child: operations.split_child,
          }}
        />
      </Section>

      {Object.keys(perfectOrder).length > 0 ? (
        <Section title="Perfect order check">
          <KeyValueGrid data={perfectOrder} />
        </Section>
      ) : null}

      {warnings.length > 0 ? (
        <Section title="Warnings">
          <ul className="list-inside list-disc space-y-1 text-sm text-amber-800 ">
            {warnings.map((w, i) => (
              <li key={i}>{typeof w === "string" ? w : JSON.stringify(w)}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <details className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium text-[var(--text-secondary)]">
          Raw JSON
        </summary>
        <pre className="mt-3 overflow-auto text-xs leading-relaxed text-[var(--text-muted)]">
          {JSON.stringify(artifact, null, 2)}
        </pre>
      </details>
    </div>
  );
}
