"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { LastMilePanel } from "@/components/order-rca/LastMilePanel";
import {
  displayVal,
  formatDurationMinutes,
  getOrderRcaRun,
  startOrderRcaDiagnose,
  type OrderRcaAllocationUnavailable,
  type OrderRcaPreflight,
  type OrderRcaReport,
  type OrderRcaRun,
  type OrderRcaMsnAdherence,
  type OrderRcaPerfectOrder,
  type OrderRcaPerfectOrderPillar,
  type OrderRcaSplitInsights,
  type OrderRcaStoreViews,
  type CartAllocationJourney,
  type MsnAdherenceSkuLine,
  type MsnAdherenceStore,
} from "@/lib/orderRca";

const POLL_INTERVAL_MS = 1500;
const POLL_MAX_MS = 60 * 60 * 1000;
const LAST_PO_KEY = "order-rca:last-po";

type StoreGroup = {
  physical_store?: string;
  distance_km?: number;
  vendor_type?: string;
  virtual_store_count?: number;
  virtual_stores?: Array<Record<string, unknown>>;
};

type PlanningRow = Record<string, unknown>;

type ColDef = { key: string; label: string; title?: string; className?: string };

const DELIVED_HINT = "Stock in the depletion pipeline (Delived), not on live shelf yet.";

const STORE_COLS: ColDef[] = [
  {
    key: "code",
    label: "Virtual store",
    title: "Vendor code at this location (e.g. 1MG_BRM_01)",
  },
  {
    key: "km",
    label: "Distance",
    title: "Road distance in km from this vendor to the delivery address",
  },
  {
    key: "loc",
    label: "Location serviceable",
    title: "Whether this vendor can serve the delivery address (pincode mapped or an active delivery window)",
  },
  { key: "inv", label: "Stock", title: "Distinct stock issue types on this vendor (Unmapped, Out of stock, Partial, etc.). Per-SKU detail is in Reason." },
  {
    key: "active_svc",
    label: "Active services",
    title: "Delivery windows that were active at order time (hover for schedule). Pincode orders show Standard.",
  },
  {
    key: "inactive_svc",
    label: "Inactive services",
    title:
      "Inactive at order placement: Beyond time (outside operating window) vs Disabled (inside window but inactive). Hover chips for schedule.",
  },
  { key: "out", label: "Result", title: "Whether this vendor was allocated, rejected, or in the shortlisted set" },
  {
    key: "why",
    label: "Reason",
    title: "Why this store was rejected or chosen (one line per blocking SKU when applicable)",
  },
];

function cn(...p: Array<string | false | null | undefined>) {
  return p.filter(Boolean).join(" ");
}

function Badge({ badge }: { badge: string }) {
  const b = badge.toUpperCase();
  return (
    <span
      className={cn(
        "inline-flex rounded px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-white",
        b === "IDEAL" && "bg-emerald-600",
        b === "CROSS" && "bg-rose-600",
        !["IDEAL", "CROSS"].includes(b) && "bg-slate-500"
      )}
    >
      {badge || "—"}
    </span>
  );
}

function Tag({
  children,
  tone = "slate",
  className,
  compact,
  title,
}: {
  children: React.ReactNode;
  tone?: "slate" | "ok" | "bad" | "warn" | "info" | "emerald" | "violet";
  className?: string;
  compact?: boolean;
  title?: string;
}) {
  const t = {
    slate: "bg-slate-100 text-slate-700",
    ok: "bg-emerald-100 text-emerald-800",
    bad: "bg-rose-100 text-rose-800",
    warn: "bg-amber-100 text-amber-900",
    info: "bg-sky-100 text-sky-800",
    emerald: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
    violet: "bg-violet-100 text-violet-800",
  }[tone];
  return (
    <span
      title={title}
      className={cn(
        "inline-flex rounded font-semibold leading-none",
        compact ? "px-1.5 py-0.5 text-[11px]" : "px-2 py-0.5 text-[12px] leading-tight",
        t,
        className
      )}
    >
      {children}
    </span>
  );
}

type SignalTone = "ok" | "bad" | "warn" | "info" | "violet" | "slate";

/** Classify rejection signals for chip color (order matters — specific rules first). */
function signalTone(signal: string): SignalTone {
  const s = signal.toLowerCase();
  if (/allocated to this store|allocated to this order|included in shortlisted/.test(s)) return "ok";
  if (
    /not chosen by allocation engine|allocation engine|correlate further|split fulfilment disabled/.test(
      s
    )
  ) {
    return "violet";
  }
  if (
    /address not serviceable|location not serviceable|no pincode mapped|not pincode mapped|pincode not mapped|no active services/.test(
      s
    )
  ) {
    return "warn";
  }
  if (
    /unmapped on vendor|not mapped on vendor|out of stock|stock not available|inventory not available|partial stock|partial and delived|unavailable and delived|delived pipeline|depleted/.test(
      s
    )
  ) {
    return "bad";
  }
  if (
    /no active delivery service|delivery service window|service slot|service unavailable|inactive.*service|service:/.test(s)
  ) {
    return "info";
  }
  return "slate";
}

function ReasonChip({ signal }: { signal: string }) {
  const tone = signalTone(signal);
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center rounded px-2 py-1 text-[11px] font-semibold leading-snug shadow-sm ring-1 ring-black/10",
        tone === "ok" && "bg-emerald-600 text-white",
        tone === "bad" && "bg-rose-600 text-white",
        tone === "warn" && "bg-amber-600 text-white",
        tone === "info" && "bg-sky-700 text-white",
        tone === "violet" && "bg-violet-700 text-white",
        tone === "slate" && "bg-slate-700 text-white"
      )}
      title={signal}
    >
      <span className="break-words">{signal}</span>
    </span>
  );
}

type ServiceWindowRow = {
  service_key?: string;
  service_label?: string;
  window?: string;
  status?: string;
  inactivity_kind?: "beyond_time" | "disabled" | "unknown";
};

/** Mirror backend: derive display text from API keys (no static service catalog). */
function serviceKeyLabel(key: string): string {
  const k = key.trim();
  if (!k) return "—";
  if (!k.includes("_")) return k;
  const parts = k.split("_").filter(Boolean);
  if (parts.length === 2 && /^\d+(\.\d+)?$/.test(parts[0])) return `${parts[0]} ${parts[1]}`;
  return parts.join(" ");
}

function windowLabel(row: ServiceWindowRow): string {
  return row.service_label || serviceKeyLabel(String(row.service_key ?? ""));
}

function serviceRowKey(row: ServiceWindowRow): string {
  return String(row.service_key ?? windowLabel(row));
}

function partitionServiceWindows(v: Record<string, unknown>) {
  const active = (Array.isArray(v.active_services) ? v.active_services : []) as ServiceWindowRow[];
  const inactiveRaw = (Array.isArray(v.inactive_services) ? v.inactive_services : []) as ServiceWindowRow[];
  const activeKeys = new Set(active.map(serviceRowKey));
  const inactiveOnly = inactiveRaw.filter((row) => !activeKeys.has(serviceRowKey(row)));
  const pin = v.pincode_mapped === true;
  const standardUnavailable = pin !== true;
  const advertised = (Array.isArray(v.advertised_rapid_sla) ? v.advertised_rapid_sla : []) as ServiceWindowRow[];
  return { active, inactiveOnly, pin, standardUnavailable, advertised };
}

const SERVICE_CHIP_MAX = 4;

function CompactServiceChip({
  row,
  tone,
  inactivityKind,
}: {
  row: ServiceWindowRow;
  tone: "ok" | "slate" | "warn" | "violet";
  inactivityKind?: string;
}) {
  const label = windowLabel(row);
  const win = String(row.window ?? "").trim();
  const kindHint =
    inactivityKind === "beyond_time"
      ? "Beyond operating window at order placement"
      : inactivityKind === "disabled"
        ? "Disabled — inside operating window but inactive at order placement"
        : undefined;
  return (
    <span title={kindHint ?? (win ? `${label} · ${win}` : label)} className="inline-flex shrink-0">
      <Tag tone={tone} compact>
        {label}
      </Tag>
    </span>
  );
}

function CompactServiceList({
  rows,
  tone,
  max = SERVICE_CHIP_MAX,
  title,
  inactiveKinds,
  inactivityKind,
}: {
  rows: ServiceWindowRow[];
  tone: "ok" | "slate" | "warn" | "violet";
  max?: number;
  title?: string;
  inactiveKinds?: boolean;
  inactivityKind?: ServiceWindowRow["inactivity_kind"];
}) {
  if (!rows.length) return null;
  const shown = rows.slice(0, max);
  const extra = rows.length - shown.length;
  const allWindows = rows.map((r) => `${windowLabel(r)}${r.window ? ` (${r.window})` : ""}`).join(" · ");
  return (
    <div className="flex max-w-full flex-wrap items-center gap-0.5" title={title ?? allWindows}>
      {shown.map((row, i) => (
        <CompactServiceChip
          key={`${tone}-${i}-${serviceRowKey(row)}`}
          row={row}
          tone={tone}
          inactivityKind={inactiveKinds ? inactivityKind ?? row.inactivity_kind : undefined}
        />
      ))}
      {extra > 0 ? (
        <span
          className="rounded-md bg-slate-200 px-1.5 py-0.5 text-[11px] font-semibold text-slate-700"
          title={allWindows}
        >
          +{extra} more
        </span>
      ) : null}
    </div>
  );
}

function ActiveServicesAtOrderCell({ v }: { v: Record<string, unknown> }) {
  const { active, pin, advertised } = partitionServiceWindows(v);
  const standardAtStore = pin;

  if (!standardAtStore && !active.length && !advertised.length) {
    return <span className="text-slate-400">—</span>;
  }

  return (
    <div className="flex max-w-full flex-wrap items-center gap-0.5">
      {standardAtStore ? (
        <Tag tone="info" compact title="Standard delivery available (pincode mapped)">
          Standard
        </Tag>
      ) : null}
      <CompactServiceList rows={active} tone="ok" title="Active delivery windows at order time" />
      <CompactServiceList
        rows={advertised}
        tone="violet"
        title="Advertised SLA on this store (no active slot at order time)"
      />
    </div>
  );
}

function InactiveServiceGroup({
  label,
  rows,
  tone,
  inactivityKind,
}: {
  label: string;
  rows: ServiceWindowRow[];
  tone: "slate" | "warn";
  inactivityKind?: ServiceWindowRow["inactivity_kind"];
}) {
  if (!rows.length) return null;
  return (
    <div className="space-y-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</span>
      <CompactServiceList rows={rows} tone={tone} max={32} inactiveKinds inactivityKind={inactivityKind} />
    </div>
  );
}

function InactiveServicesAtOrderCell({ v }: { v: Record<string, unknown> }) {
  const { inactiveOnly, standardUnavailable } = partitionServiceWindows(v);

  if (!inactiveOnly.length && !standardUnavailable) {
    return <span className="text-slate-400">—</span>;
  }

  const beyond = inactiveOnly.filter((r) => r.inactivity_kind === "beyond_time");
  const disabled = inactiveOnly.filter((r) => r.inactivity_kind === "disabled");
  const other = inactiveOnly.filter(
    (r) => r.inactivity_kind !== "beyond_time" && r.inactivity_kind !== "disabled"
  );

  return (
    <div className="space-y-1.5">
      {standardUnavailable ? (
        <Tag tone="bad" compact title="Standard delivery unavailable (pincode not mapped)">
          Standard
        </Tag>
      ) : null}
      <InactiveServiceGroup label="Beyond time" rows={beyond} tone="slate" inactivityKind="beyond_time" />
      <InactiveServiceGroup label="Disabled" rows={disabled} tone="warn" inactivityKind="disabled" />
      {other.length > 0 ? (
        <InactiveServiceGroup label="Inactive" rows={other} tone="slate" />
      ) : null}
    </div>
  );
}

function locationNotOkHint(v: Record<string, unknown>): string {
  const pin = v.pincode_mapped;
  const active = Array.isArray(v.active_services) ? v.active_services : [];
  const notPin = pin !== true;
  const noActive = active.length === 0;
  if (notPin && noActive) return "No pincode and no active windows";
  if (notPin) return "Pincode not mapped";
  if (noActive) return "No active delivery windows";
  return "Address not serviceable";
}

function LocationServiceableCell({ v }: { v: Record<string, unknown> }) {
  const ok =
    v.location_serviceable === true
      ? true
      : v.location_not_serviceable === true
        ? false
        : v.location_serviceable === false
          ? false
          : null;
  const reason = String(v.location_reason ?? "");
  const pin = v.pincode_mapped;
  const pinLabel = pin === true ? "Pincode: yes" : pin === false ? "Pincode: no" : "Pincode: —";
  const title = [reason, pinLabel].filter(Boolean).join(" · ");
  if (ok === true) {
    const hint =
      pin === true
        ? "Pincode mapped"
        : Array.isArray(v.active_services) && (v.active_services as unknown[]).length > 0
          ? "Active service path"
          : v.outcome === "selected"
            ? "In shortlisted set"
            : "";
    return (
      <span title={hint ? `${title} · ${hint}` : title} className="inline-flex items-center gap-1">
        <Tag tone="ok" compact>
          Yes
        </Tag>
        {hint ? <span className="text-[11px] text-slate-600">{hint}</span> : null}
      </span>
    );
  }
  if (ok === false) {
    const hint = locationNotOkHint(v);
    return (
      <span title={title} className="inline-flex max-w-full flex-wrap items-center gap-1">
        <Tag tone="bad" compact>
          No
        </Tag>
        <span className="text-[11px] leading-snug text-slate-700">{hint}</span>
      </span>
    );
  }
  return <span className="text-slate-300">—</span>;
}

function BoolCell({ v, yes, no }: { v?: boolean | null; yes: string; no: string }) {
  if (v === true) return <Tag tone="ok">{yes}</Tag>;
  if (v === false) return <Tag tone="bad">{no}</Tag>;
  return <span className="text-slate-300">—</span>;
}

function inventoryClassTone(cls: string): "ok" | "warn" | "bad" | "violet" {
  if (cls === "ok") return "ok";
  if (cls === "partial" || cls === "delived_partial") return "warn";
  if (cls === "delived_avail") return "violet";
  return "bad";
}

function inventoryTagTitle(cls: string, label: string): string {
  if (cls === "unmapped") return "SKU not on this vendor — see Reason column for SKU ids";
  if (cls.startsWith("delived")) return DELIVED_HINT;
  if (cls === "partial") return "Available quantity is less than ordered";
  return label;
}

function InventoryCell({ v }: { v: Record<string, unknown> }) {
  const classes = Array.isArray(v.inventory_classes)
    ? (v.inventory_classes as string[])
    : v.inventory_class
      ? [String(v.inventory_class)]
      : [];
  const labels = Array.isArray(v.inventory_labels)
    ? (v.inventory_labels as string[])
    : v.inventory_label
      ? [String(v.inventory_label)]
      : [];

  if (labels.length > 0 && !(labels.length === 1 && labels[0] === "")) {
    const pairs = labels.map((label, i) => ({
      label,
      cls: classes[i] ?? classes[0] ?? "",
    }));
    const uniquePairs = pairs.filter(
      (p, i) => pairs.findIndex((q) => q.cls === p.cls && q.label === p.label) === i
    );
    if (uniquePairs.length === 1 && uniquePairs[0].cls === "ok") {
      return (
        <span title="Stock can fulfil the order on this vendor" className="block">
          <Tag tone="ok" compact={false}>
            {uniquePairs[0].label}
          </Tag>
        </span>
      );
    }
    if (uniquePairs.some((p) => p.cls !== "ok")) {
      return (
        <div className="flex max-w-full flex-wrap gap-0.5">
          {uniquePairs
            .filter((p) => p.cls !== "ok")
            .map((p) => (
              <span key={`${p.cls}-${p.label}`} title={inventoryTagTitle(p.cls, p.label)}>
                <Tag tone={inventoryClassTone(p.cls)} compact={false}>
                  {p.label}
                </Tag>
              </span>
            ))}
        </div>
      );
    }
  }

  return (
    <BoolCell
      v={v.inventory_not_available === true ? false : v.inventory_not_available === false ? true : null}
      yes="OK"
      no="OOS"
    />
  );
}

function OutcomeTag({ o }: { o?: string }) {
  const x = (o || "").toLowerCase();
  if (x === "allocated") return <Tag tone="ok" compact={false}>Allocated</Tag>;
  if (x === "rejected") return <Tag tone="bad" compact={false}>Rejected</Tag>;
  if (x === "selected") return <Tag tone="info" compact={false}>Shortlisted</Tag>;
  return <Tag compact={false}>{displayVal(o)}</Tag>;
}

/** Plain text after ``SKU {id}:`` — mirrors backend inventory_issue_message. */
function inventoryIssueMessage(msg: string): string {
  let m = msg.trim();
  if (!m) return m;
  if (m.includes("(")) m = m.split("(", 1)[0].trim();
  const ml = m.toLowerCase();
  if (ml.includes("not mapped")) return "Unmapped on vendor";
  if (ml.includes("not available") && ml.includes("delived")) return "Unavailable and Delived";
  if (ml.includes("partial") && ml.includes("delived")) return "Partial and Delived";
  if (ml.includes("delived") && (ml.includes("pipeline") || ml.includes("available but")))
    return "Delived pipeline";
  if (ml.includes("partial")) return "Partial stock";
  if (ml.includes("out of stock") || ml === "oos") return "Out of stock";
  if (ml === "jit" || ml.includes("just-in-time")) return "Just-in-time";
  if (ml.includes("location not serviceable")) return "Address not serviceable";
  if (ml.includes("inventory not available")) return "Stock not available";
  return m.replace(/^sku\s+/i, "");
}

/** Align display with backend (also handles older cached reports). */
function displaySignal(raw: string): string {
  const m = raw.trim();
  if (!m) return m;
  if (m.startsWith("Not chosen by allocation engine")) return m;
  if (/^sku\s+\d/i.test(m) && m.includes(": ")) {
    const idx = m.indexOf(": ");
    return `${m.slice(0, idx)}: ${inventoryIssueMessage(m.slice(idx + 2))}`;
  }
  if (m.includes("Vendor has no active rapid/standard")) return "";
  if (/rejected by allocator without/i.test(m)) {
    return "Not chosen by allocation engine (Correlate further)";
  }
  if (/split fulfilment disabled|split_enabled=false/i.test(m) && /not chosen by allocation engine/i.test(m)) {
    return "Not chosen by allocation engine (Split fulfilment disabled, Correlate further)";
  }
  if (/not chosen by allocation engine/i.test(m) && /address ok|stock ok|delivery windows ok|another store ranked/i.test(m)) {
    return /split_enabled=false|split fulfilment disabled/i.test(m)
      ? "Not chosen by allocation engine (Split fulfilment disabled, Correlate further)"
      : "Not chosen by allocation engine (Correlate further)";
  }
  if (/^sku\s+not mapped/i.test(m)) return "Unmapped on vendor";
  return inventoryIssueMessage(m);
}

function normalizeSignals(v: Record<string, unknown>): string[] {
  const raw = Array.isArray(v.signals) ? (v.signals as string[]).filter(Boolean) : [];
  const short = String(v.reason_short ?? "").trim();
  const list = raw.length ? raw : short ? [short] : [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of list) {
    const norm = displaySignal(s);
    if (!norm) continue;
    const key = norm.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(norm);
  }
  return out;
}

function ReasonCell({ v }: { v: Record<string, unknown> }) {
  const signals = normalizeSignals(v);

  if (!signals.length) {
    return <span className="text-slate-400">—</span>;
  }

  if (signals.length === 1) {
    return <ReasonChip signal={signals[0]} />;
  }

  return (
    <ul className="space-y-1" aria-label={`${signals.length} rejection reasons`}>
      {signals.map((s, i) => (
        <li key={`${i}-${s.slice(0, 20)}`}>
          <ReasonChip signal={s} />
        </li>
      ))}
    </ul>
  );
}

function textOverlaps(a: string, b: string): boolean {
  const x = a.toLowerCase().trim();
  const y = b.toLowerCase().trim();
  if (!x || !y) return false;
  const probe = x.slice(0, Math.min(60, x.length));
  return y.includes(probe) || x.includes(y.slice(0, Math.min(60, y.length)));
}

const TD = "min-w-0 px-2.5 py-2 align-top text-[12px] leading-snug";

function DataTable({
  cols,
  children,
  colWidths,
  embedded,
  minWidth = 920,
}: {
  cols: ColDef[];
  children: React.ReactNode;
  colWidths?: string[];
  embedded?: boolean;
  /** Minimum table width in px; 0 = fit container (no forced horizontal scroll). */
  minWidth?: number;
}) {
  const widths = colWidths?.length === cols.length ? colWidths : cols.map(() => `${Math.floor(100 / cols.length)}%`);
  return (
    <div className={cn("overflow-x-auto", embedded ? "" : "rounded-md border border-slate-200")}>
      <table
        className="w-full min-w-0 table-fixed border-collapse text-left"
        style={minWidth > 0 ? { minWidth: `${minWidth}px` } : undefined}
      >
        <colgroup>
          {widths.map((w, i) => (
            <col key={i} style={{ width: w }} />
          ))}
        </colgroup>
        <thead>
          <tr className="bg-slate-800 text-[12px] font-semibold tracking-wide text-slate-100">
            {cols.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={cn(TD, "py-1.5 font-semibold")}
                title={c.title}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 text-slate-800">{children}</tbody>
      </table>
    </div>
  );
}

const STORE_COL_WIDTHS = ["11%", "6%", "12%", "8%", "11%", "10%", "8%", "34%"];

function UnconfiguredPanel({ label, status, message }: { label: string; status?: string; message?: string }) {
  const body =
    message ??
    (status === "not_configured"
      ? "Planning data is not connected yet. When live, this table shows stock and delivery service at ideal-tier stores."
      : "No rows returned for this order.");
  return (
    <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-center">
      <p className="text-[13px] font-semibold text-slate-800">{label}</p>
      <p className="mx-auto mt-2 max-w-md text-[12px] leading-relaxed text-slate-600">{body}</p>
      <div className="mt-3">
        <Tag tone="warn">{status === "not_configured" ? "Not configured" : displayVal(status, "Unavailable")}</Tag>
      </div>
    </div>
  );
}

function PlanningTable({ rows }: { rows: PlanningRow[] }) {
  if (!rows.length) return null;
  const cols: ColDef[] = [
    { key: "store", label: "Store" },
    { key: "km", label: "km" },
    { key: "drop", label: "Drop" },
    { key: "rep", label: "Replenish" },
  ];
  return (
    <DataTable cols={cols}>
      {rows.map((r, i) => (
        <tr key={i}>
          <td className={cn(TD, "font-mono font-semibold")}>{displayVal(r.physical_store ?? r.store)}</td>
          <td className={cn(TD, "tabular-nums")}>{displayVal(r.distance_km ?? r.km)}</td>
          <td className={TD}>{displayVal(r.drop)}</td>
          <td className={TD}>{displayVal(r.replenish)}</td>
        </tr>
      ))}
    </DataTable>
  );
}

function msnToneToTag(tone?: string): "ok" | "warn" | "bad" | "slate" {
  if (tone === "ok" || tone === "warn" || tone === "bad") return tone;
  return "slate";
}

function MsnStatusTag({ label, tone }: { label?: string; tone?: string }) {
  const text = label?.trim() || "—";
  if (text === "—") {
    return <span className="text-slate-400">—</span>;
  }
  return (
    <Tag tone={msnToneToTag(tone)} className="max-w-[220px] whitespace-normal text-left leading-snug">
      {text}
    </Tag>
  );
}

const MSN_SKU_COLS: ColDef[] = [
  { key: "sku", label: "SKU", title: "Order line product" },
  { key: "subgrade", label: "Sub grade", title: "SKU sub grade from P1 planning API" },
  { key: "msn", label: "MSN", title: "Effective minimum stock norm" },
  { key: "shelf", label: "On-shelf", title: "On-shelf inventory at allocation time" },
  { key: "asked", label: "Asked", title: "Quantity on this order line" },
  { key: "status", label: "Status", title: "MSN adherence vs asked quantity" },
];

const MSN_SKU_COL_WIDTHS = ["28%", "10%", "9%", "9%", "8%", "36%"];

function MsnAdherenceStoreBlock({ store }: { store: MsnAdherenceStore }) {
  const skus = (store.skus ?? []) as MsnAdherenceSkuLine[];
  const rollupTone = msnToneToTag(store.rollup_tone);

  return (
    <article className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-slate-200 bg-slate-50 px-3 py-2">
        <span className="font-mono text-[13px] font-bold text-slate-900">{displayVal(store.physical_store)}</span>
        <span className="text-[12px] tabular-nums text-slate-600">{displayVal(store.distance_km)} km</span>
        <span className="text-[12px] text-slate-600">{displayVal(store.vendor_type)}</span>
        <Tag tone={rollupTone} className="ml-auto shrink-0">
          {displayVal(store.rollup_label)}
        </Tag>
      </header>
      {skus.length === 0 ? (
        <p className="px-3 py-3 text-[12px] text-slate-500">No order lines for this store.</p>
      ) : (
        <DataTable
          cols={MSN_SKU_COLS}
          colWidths={MSN_SKU_COL_WIDTHS}
          embedded
          minWidth={0}
        >
          {skus.map((sku, ki) => (
            <tr key={ki} className={ki % 2 === 1 ? "bg-slate-50/70" : undefined}>
              <td className={cn(TD, "font-medium text-slate-800")}>{displayVal(sku.name ?? sku.sku_id)}</td>
              <td className={TD}>{displayVal(sku.sku_sub_grade)}</td>
              <td className={cn(TD, "tabular-nums")}>{displayVal(sku.effective_msn_display)}</td>
              <td className={cn(TD, "tabular-nums")}>{displayVal(sku.on_shelf_display)}</td>
              <td className={cn(TD, "tabular-nums font-medium")}>{displayVal(sku.asked_display)}</td>
              <td className={TD}>
                <MsnStatusTag label={sku.status_label} tone={sku.status_tone} />
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </article>
  );
}

/** One card per physical store; each order SKU is a row in the inner table. */
function MsnAdherenceTable({ block }: { block: OrderRcaMsnAdherence }) {
  const stores = (block.stores ?? []) as MsnAdherenceStore[];
  if (!stores.length) {
    if (block.panel_note) {
      return (
        <Alert tone="warn">
          <strong className="block text-sm">MSN adherence</strong>
          <p className="mt-1 text-[12px] leading-relaxed">{block.panel_note}</p>
        </Alert>
      );
    }
    return (
      <UnconfiguredPanel
        label="MSN adherence"
        status={block.status}
        message="No stores in the allocation matrix for this order."
      />
    );
  }

  return (
    <div className="space-y-3">
      {stores.map((store, si) => (
        <MsnAdherenceStoreBlock key={store.physical_store ?? si} store={store} />
      ))}
    </div>
  );
}

function PhysicalStoreCard({ g, children }: { g: StoreGroup; children: React.ReactNode }) {
  const retail = (g.vendor_type || "").toLowerCase() === "retail";
  const count = Number(g.virtual_store_count) || (g.virtual_stores ?? []).length;
  return (
    <article
      className={cn(
        "overflow-hidden rounded-xl border-2 bg-white shadow-md",
        retail ? "border-sky-300 ring-1 ring-sky-100" : "border-violet-300 ring-1 ring-violet-100"
      )}
    >
      <details className="group">
        <summary
          className={cn(
            "flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1.5 border-b-2 px-4 py-3 [&::-webkit-details-marker]:hidden",
            retail ? "border-sky-200 bg-sky-50" : "border-violet-200 bg-violet-50"
          )}
          title="Expand or collapse virtual vendor rows"
        >
          <span
            aria-hidden
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center text-[14px] font-bold leading-none text-slate-600 transition group-open:rotate-90"
          >
            ▸
          </span>
          <span
            className={cn(
              "text-[10px] font-bold uppercase tracking-widest",
              retail ? "text-sky-800" : "text-violet-800"
            )}
          >
            Physical store
          </span>
          <span className="font-mono text-[15px] font-bold text-slate-900">{displayVal(g.physical_store)}</span>
          <span className="inline-flex rounded-full bg-white px-3 py-1 text-[13px] font-bold tabular-nums text-slate-900 shadow-sm ring-1 ring-slate-200">
            {displayVal(g.distance_km)} km
          </span>
          <Tag tone={retail ? "info" : "violet"}>{displayVal(g.vendor_type)}</Tag>
          <span className="text-[12px] font-medium text-slate-700">
            {count} virtual {count === 1 ? "store" : "stores"}
          </span>
        </summary>
        <div className="bg-slate-50/60 px-1 pb-1 pt-1">{children}</div>
      </details>
    </article>
  );
}

function DistanceKmCell({ v, groupKm }: { v: Record<string, unknown>; groupKm?: number }) {
  const vKm = v.distance_km;
  if (vKm == null || vKm === "") {
    return <span className="text-slate-400">—</span>;
  }
  const same = groupKm != null && Number(vKm) === Number(groupKm);
  return (
    <span
      className="whitespace-nowrap tabular-nums text-slate-700"
      title={same ? `${displayVal(vKm)} km — same as physical store above` : `${displayVal(vKm)} km to delivery address`}
    >
      {displayVal(vKm)}
      <span className="text-slate-500"> km</span>
    </span>
  );
}

function VirtualStoreRowCells({ v, groupKm }: { v: Record<string, unknown>; groupKm?: number }) {
  return (
    <>
      <td className={cn(TD, "border-l-[3px] border-l-slate-400 bg-white pl-3")}>
        <span className="font-mono text-[12px] font-semibold text-slate-900">{displayVal(v.vendor_code)}</span>
      </td>
      <td className={cn(TD, "bg-white")}>
        <DistanceKmCell v={v} groupKm={groupKm} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <LocationServiceableCell v={v} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <InventoryCell v={v} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <ActiveServicesAtOrderCell v={v} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <InactiveServicesAtOrderCell v={v} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <OutcomeTag o={String(v.outcome ?? "")} />
      </td>
      <td className={cn(TD, "bg-white")}>
        <ReasonCell v={v} />
      </td>
    </>
  );
}

function GroupedStoreTable({ groups }: { groups: StoreGroup[] }) {
  if (!groups.length) return <p className="text-[12px] text-slate-500">No stores in this view.</p>;

  return (
    <div className="space-y-5">
      {groups.map((g) => {
        const storeKey = String(g.physical_store);
        const retail = (g.vendor_type || "").toLowerCase() === "retail";
        const virtualStoreRows = (g.virtual_stores ?? []).map((v) => (
          <tr
            key={String(v.vendor_code)}
            className={cn("bg-white", retail ? "hover:bg-sky-50/30" : "hover:bg-violet-50/20")}
          >
            <VirtualStoreRowCells v={v} groupKm={g.distance_km as number | undefined} />
          </tr>
        ));
        return (
          <PhysicalStoreCard key={storeKey} g={g}>
            <DataTable cols={STORE_COLS} colWidths={STORE_COL_WIDTHS} embedded>
              {virtualStoreRows}
            </DataTable>
          </PhysicalStoreCard>
        );
      })}
    </div>
  );
}

function StoreSection({
  title,
  subtitle,
  groups,
  highlight,
}: {
  title: string;
  subtitle: string;
  groups: StoreGroup[];
  highlight?: boolean;
}) {
  if (!groups.length) return null;
  const body = <GroupedStoreTable groups={groups} />;
  return (
    <div className={highlight ? "rounded-xl ring-2 ring-emerald-400/70 ring-offset-2" : undefined}>
      <div className={cn("mb-3", highlight && "rounded-t-xl bg-emerald-50 px-3 pt-3")}>
        <h3 className="text-[13px] font-bold text-slate-900">{title}</h3>
        <p className="mt-0.5 text-[12px] leading-relaxed text-slate-600">{subtitle}</p>
      </div>
      <div className={highlight ? "px-3 pb-3" : undefined}>{body}</div>
    </div>
  );
}

function ReasonColorLegend() {
  const items: Array<{ label: string; tone: SignalTone }> = [
    { label: "Inventory (per SKU)", tone: "bad" },
    { label: "Location", tone: "warn" },
    { label: "Engine", tone: "violet" },
    { label: "Allocated", tone: "ok" },
    { label: "Inactive · beyond time", tone: "slate" },
    { label: "Inactive · disabled", tone: "warn" },
  ];
  const chipClass: Record<SignalTone, string> = {
    ok: "bg-emerald-600",
    bad: "bg-rose-600",
    warn: "bg-amber-600",
    info: "bg-sky-700",
    violet: "bg-violet-700",
    slate: "bg-slate-700",
  };
  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2.5">
      <span className="text-[12px] font-semibold text-slate-800">Reason colors</span>
      {items.map(({ label, tone }) => (
        <span key={label} className="inline-flex items-center gap-1.5 text-[11px] text-slate-700">
          <span className={cn("h-2.5 w-2.5 rounded-sm", chipClass[tone])} />
          {label}
        </span>
      ))}
    </div>
  );
}

function StorePanel({
  views,
  allocationUnavailable,
}: {
  views: OrderRcaStoreViews;
  allocationUnavailable?: OrderRcaAllocationUnavailable | null;
}) {
  if (views.skipped) {
    return (
      <p className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
        {views.skip_reason ?? "Preferred allocation — detailed store comparison was not needed."}
      </p>
    );
  }
  const retentionNote =
    allocationUnavailable?.message ?? views.panel_note ?? null;
  if (retentionNote) {
    return (
      <Alert tone="warn">
        <strong className="block text-sm">Allocation data unavailable</strong>
        <p className="mt-1 text-[12px] leading-relaxed">{retentionNote}</p>
      </Alert>
    );
  }
  const nearby = (views.nearby_stores ?? []) as StoreGroup[];
  const wh = (views.nearby_warehouses ?? []) as StoreGroup[];
  const alloc = views.allocated_store as StoreGroup | undefined;

  return (
    <div className="space-y-4">
      <ReasonColorLegend />
      <StoreSection
        title="Up to 5 physical stores"
        subtitle="Virtual vendor codes evaluated at allocation — result and rejection reasons on each row"
        groups={nearby}
      />
      <StoreSection
        title="Nearest 3 warehouses (outside top 5)"
        subtitle="Warehouse physical locations beyond the top 5 retail/nearest set"
        groups={wh}
      />
      <StoreSection
        title="Allocated physical store"
        subtitle="Store that fulfilled this order"
        groups={alloc ? [alloc] : []}
        highlight
      />
    </div>
  );
}

type PanelAccent = "sky" | "rose" | "emerald" | "amber" | "slate" | "indigo";

const PANEL_ACCENT: Record<
  PanelAccent,
  { stripe: string; header: string; phase: string; title?: string }
> = {
  indigo: {
    stripe: "bg-indigo-600",
    header: "border-indigo-200 bg-gradient-to-br from-indigo-100 via-sky-50 to-white",
    phase: "bg-indigo-700 text-white",
    title: "text-indigo-950",
  },
  sky: {
    stripe: "bg-sky-500",
    header: "border-sky-100 bg-gradient-to-br from-sky-50 to-white",
    phase: "bg-sky-600 text-white",
  },
  rose: {
    stripe: "bg-rose-500",
    header: "border-rose-100 bg-gradient-to-br from-rose-50 to-white",
    phase: "bg-rose-600 text-white",
  },
  emerald: {
    stripe: "bg-emerald-500",
    header: "border-emerald-100 bg-gradient-to-br from-emerald-50 to-white",
    phase: "bg-emerald-600 text-white",
  },
  amber: {
    stripe: "bg-amber-500",
    header: "border-amber-100 bg-gradient-to-br from-amber-50 to-white",
    phase: "bg-amber-600 text-white",
  },
  slate: {
    stripe: "bg-slate-500",
    header: "border-slate-100 bg-gradient-to-br from-slate-50 to-white",
    phase: "bg-slate-700 text-white",
  },
};

function Panel({
  phase,
  title,
  description,
  badge,
  children,
  dev,
  source,
  className,
  accent = "slate",
}: {
  phase?: string;
  title: string;
  description?: string;
  badge?: string;
  children: React.ReactNode;
  dev?: boolean;
  source?: string;
  className?: string;
  accent?: PanelAccent;
}) {
  const a = PANEL_ACCENT[accent];
  return (
    <section className={cn("overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm", className)}>
      <div className={cn("flex border-b", a.header)}>
        <div className={cn("w-1 shrink-0", a.stripe)} aria-hidden />
        <div className="flex min-w-0 flex-1 flex-wrap items-start gap-3 px-4 py-3">
          {phase ? (
            <span
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[11px] font-bold uppercase shadow-sm",
                a.phase
              )}
            >
              {phase}
            </span>
          ) : null}
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2
                className={cn(
                  "text-[15px] font-bold leading-tight",
                  a.title ?? "text-slate-900"
                )}
              >
                {title}
              </h2>
              {badge ? <Tag tone="slate">{badge}</Tag> : null}
            </div>
            {description ? <p className="mt-1 max-w-2xl text-[12px] leading-relaxed text-slate-600">{description}</p> : null}
          </div>
          {dev && source ? <span className="ml-auto shrink-0 text-[10px] text-violet-600">{source}</span> : null}
        </div>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function CartAllocationJourneyPanel({ journey }: { journey?: CartAllocationJourney }) {
  if (!journey?.available) return null;
  const steps = journey.steps ?? [];
  const hasNarrative = Boolean(journey.headline) || (journey.insights ?? []).length > 0;
  if (!steps.length && !hasNarrative && journey.empty) return null;

  return (
    <div className="rounded-lg border border-violet-200 bg-gradient-to-r from-violet-50/80 to-white px-3 py-2.5 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-[11px] font-bold uppercase tracking-wide text-violet-900">Allocation journey on cart</p>
        {journey.cart_id ? (
          <span className="text-[10px] font-medium text-violet-700">Cart {journey.cart_id}</span>
        ) : null}
      </div>
      {hasNarrative && !steps.length ? (
        <div className="mt-2 space-y-1 text-[12px] leading-relaxed text-slate-800">
          {(journey.insights ?? []).map((line, i) => (
            <p key={i}>{line}</p>
          ))}
        </div>
      ) : null}
      {steps.length > 0 ? (
      <ol className="mt-3 space-y-2">
        {steps.map((step, i) => (
          <li
            key={`${step.at ?? i}-${i}`}
            className="rounded-md border border-violet-100 bg-white/90 px-3 py-2 text-[12px] leading-relaxed text-slate-800"
          >
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="font-semibold text-slate-900">{displayVal(step.at)}</span>
              {step.step_label ? <Tag tone="violet">{step.step_label}</Tag> : null}
              {step.layout_label ? <Tag tone="slate">{step.layout_label}</Tag> : null}
            </div>
            {step.sku_summary ? <p className="mt-1 text-slate-700">{step.sku_summary}</p> : null}
            {(step.sku_changes ?? []).length > 0 ? (
              <div className="mt-1 flex flex-wrap gap-1">
                {(step.sku_changes ?? []).map((c, ci) => {
                  const label = c.label ?? "Item";
                  if (c.kind === "added") {
                    const q = c.qty != null ? ` ×${displayVal(c.qty)}` : "";
                    return (
                      <span key={`sku-add-${ci}`} className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                        Added: {label}{q}
                      </span>
                    );
                  }
                  if (c.kind === "removed") {
                    return (
                      <span key={`sku-rm-${ci}`} className="rounded bg-rose-100 px-1.5 py-0.5 text-[10px] text-rose-800">
                        Removed: {label}
                      </span>
                    );
                  }
                  if (c.kind === "qty") {
                    return (
                      <span key={`sku-qty-${ci}`} className="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-900">
                        {label} qty {displayVal(c.from_qty)}→{displayVal(c.to_qty)}
                      </span>
                    );
                  }
                  return (
                    <span key={`sku-chg-${ci}`} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-700">
                      {label}
                    </span>
                  );
                })}
              </div>
            ) : step.sku_delta ? (
              <p className="mt-0.5 text-[11px] text-violet-800">Cart edit: {step.sku_delta}</p>
            ) : null}
            {step.shipment_delta ? (
              <p className="mt-0.5 font-medium text-slate-900">{step.shipment_delta}</p>
            ) : null}
            {(step.shipments ?? []).length > 0 ? (
              <ul className="mt-2 space-y-2">
                {(step.shipments ?? []).map((g) => {
                  const delta = g.options_delta;
                  const options = g.options ?? [];
                  return (
                    <li key={g.group_index ?? 0}>
                      {g.group_status === "removed" ? (
                        <p className="mb-1 text-[11px] font-semibold text-rose-800">
                          Shipment {g.group_index} removed
                          {g.sku_labels ? <span className="font-normal text-rose-600"> · {g.sku_labels}</span> : null}
                        </p>
                      ) : g.group_status === "new" ? (
                        <p className="mb-1 text-[11px] font-semibold text-emerald-800">
                          Shipment {g.group_index} added
                          {g.sku_labels ? <span className="font-normal text-emerald-700"> · {g.sku_labels}</span> : null}
                        </p>
                      ) : step.layout !== "single" && g.group_index ? (
                        <p className="mb-1 text-[11px] font-semibold text-slate-700">
                          Shipment {g.group_index}
                          {g.sku_labels ? <span className="font-normal text-slate-500"> · {g.sku_labels}</span> : null}
                          {delta?.prev_count != null &&
                          delta?.cur_count != null &&
                          delta.prev_count !== delta.cur_count ? (
                            <span className="ml-1 font-normal text-slate-500">
                              ({delta.prev_count} → {delta.cur_count} options shown)
                            </span>
                          ) : null}
                        </p>
                      ) : delta?.prev_count != null &&
                        delta?.cur_count != null &&
                        delta.prev_count !== delta.cur_count ? (
                        <p className="mb-1 text-[11px] text-slate-600">
                          {delta.prev_count} → {delta.cur_count} delivery options shown
                        </p>
                      ) : null}
                      {delta?.changed?.length ||
                      delta?.added?.length ||
                      delta?.removed?.length ||
                      delta?.eta_changed?.length ? (
                        <div className="mb-1 flex flex-wrap gap-1">
                          {(delta.changed ?? []).map((t) => (
                            <span
                              key={`chg-${t}`}
                              className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-950"
                            >
                              {t}
                            </span>
                          ))}
                          {(delta.eta_changed ?? []).map((t) => (
                            <span key={`eta-${t}`} className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-900">
                              {t}
                            </span>
                          ))}
                          {(delta.added ?? []).map((t) => (
                            <span key={`add-${t}`} className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                              New: {t}
                            </span>
                          ))}
                          {(delta.removed ?? []).map((t) => (
                            <span key={`rm-${t}`} className="rounded bg-rose-100 px-1.5 py-0.5 text-[10px] text-rose-800">
                              Removed: {t}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      {options.length > 0 ? (
                        <div className="flex gap-1.5 overflow-x-auto pb-1">
                          {options.map((opt, oi) => (
                            <span
                              key={`${opt.title ?? oi}-${oi}`}
                              className={cn(
                                "shrink-0 rounded-md border px-2 py-1 text-[11px] leading-snug",
                                opt.is_fastest
                                  ? "border-violet-400 bg-violet-50 font-semibold text-violet-950"
                                  : "border-slate-200 bg-slate-50 text-slate-700"
                              )}
                              title={opt.eta_to ?? undefined}
                            >
                              {opt.title ?? "—"}
                              {opt.eta_short ? <span className="ml-1 font-normal text-slate-500">{opt.eta_short}</span> : null}
                            </span>
                          ))}
                        </div>
                      ) : g.fastest_title ? (
                        <p className="text-[11px] text-slate-600">
                          {g.fastest_title}
                          {g.fastest_eta ? <span className="text-slate-500"> · {g.fastest_eta}</span> : null}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </li>
        ))}
      </ol>
      ) : null}
      {journey.cart_vs_order?.detail ? (
        <p
          className={cn(
            "mt-3 border-t border-violet-200/60 pt-2 text-[11px] leading-relaxed",
            journey.cart_vs_order.match ? "text-emerald-800" : "text-slate-700"
          )}
        >
          {journey.cart_vs_order.detail}
        </p>
      ) : null}
      {journey.note ? <p className="mt-2 text-[10px] text-slate-500">{journey.note}</p> : null}
    </div>
  );
}

function SkuStrip({ skus }: { skus?: Array<{ sku_id?: string; name?: string; quantity?: number | string; is_sampling?: boolean }> }) {
  const lines = (skus ?? []).filter((s) => s.name || s.sku_id);
  if (!lines.length) return null;
  return (
    <div className="rounded-lg border border-violet-200 bg-gradient-to-r from-violet-50/80 to-white px-3 py-2.5 shadow-sm">
      <p className="text-[11px] font-bold uppercase tracking-wide text-violet-900">Skus</p>
      <ul className="mt-2 flex flex-wrap gap-2">
        {lines.map((s) => (
          <li
            key={String(s.sku_id ?? s.name)}
            className="rounded-md border border-violet-200/80 bg-white px-3 py-1.5 text-[13px] shadow-sm"
            title={s.sku_id ? `SKU ${s.sku_id}` : undefined}
          >
            <span className="font-semibold leading-snug text-slate-900">{displayVal(s.name, s.sku_id)}</span>
            {s.is_sampling ? (
              <span className="ml-1.5 font-medium text-slate-500">(Sampling)</span>
            ) : null}
            {s.quantity != null ? (
              <span className="ml-2 inline-flex items-baseline gap-0.5 font-bold tabular-nums text-violet-800">
                <span className="text-[11px] font-semibold text-violet-600">×</span>
                {displayVal(s.quantity)}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SynthesisSourceTag({ source }: { source?: string }) {
  if (!source) return null;
  const s = source.toLowerCase();
  const tone =
    s === "openai"
      ? "ok"
      : s === "perfect_template"
        ? "info"
        : s === "fallback" || s === "mock"
          ? "warn"
          : "slate";
  return <Tag tone={tone}>Narrative: {source}</Tag>;
}

function ProgressStatus({ run }: { run: OrderRcaRun }) {
  const p = run.progress;
  const completed = p?.completed ?? 0;
  const total = p?.total ?? 6;
  const label = p?.label ?? "Working…";
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  return (
    <div className="mb-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-2" aria-live="polite" aria-busy="true">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-sky-900">
        <span className="font-semibold">{label}</span>
        <span className="tabular-nums text-sky-700">
          Step {completed}/{total}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-sky-200">
        <div className="h-full rounded-full bg-sky-600 transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function IdleState() {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white px-6 py-10 text-center shadow-sm">
      <p className="text-base font-semibold text-slate-900">Diagnose cross-allocation and fulfilment</p>
      <p className="mx-auto mt-2 max-w-lg text-[13px] leading-relaxed text-slate-600">
        Enter a fulfilling PO (for split orders, use the <strong>child</strong> PO). The report shows allocation stores, rejection
        reasons, operations timing, and an RCA narrative.
      </p>
    </div>
  );
}

function ReportIncomplete({ run }: { run: OrderRcaRun }) {
  return (
    <Alert tone="bad">
      <strong className="block text-sm">Report incomplete</strong>
      <p className="mt-1 text-[12px]">
        Run finished with status <strong>{run.status}</strong> but the report payload is missing. Try running diagnosis again.
        {run.run_id ? (
          <>
            {" "}
            Run id: <span className="font-mono">{run.run_id}</span>
          </>
        ) : null}
      </p>
    </Alert>
  );
}

const PERFECT_ORDER_PILLAR_TITLES: Record<string, string> = {
  allocation: "Allocation",
  pushback: "Pushback",
  delivery: "Delivery",
  customer_contact: "Customer contact",
  price_integrity: "Price integrity",
};

function OrderOutcomeCard({
  scorecard,
  synthesis,
  synthesisSource,
  splitInsights,
  warnings,
  dataGaps,
  cartJourney,
  embedded,
}: {
  scorecard: OrderRcaPerfectOrder;
  synthesis?: OrderRcaReport["synthesis"];
  synthesisSource?: string;
  splitInsights?: OrderRcaSplitInsights | null;
  warnings?: string[];
  dataGaps?: string[];
  cartJourney?: CartAllocationJourney;
  embedded?: boolean;
}) {
  const overallPerfect = scorecard.overall_pass;
  const title = overallPerfect ? "Perfect order" : "Imperfect order";
  const verdict = synthesis?.verdict?.trim() ?? "";
  const subline = synthesis?.verdict_subline?.trim() ?? "";
  const showSubline = subline.length > 0 && !textOverlaps(verdict, subline);
  const primary = synthesis?.primary_cause?.trim() ?? "";
  const action = synthesis?.recommended_action?.trim() ?? "";
  const failedPillarText = (scorecard.pillars ?? [])
    .filter((p) => p.status === "fail")
    .map((p) => `${p.label ?? ""} ${p.detail ?? ""}`.trim())
    .filter(Boolean);
  const showPrimary =
    !overallPerfect &&
    primary.length > 0 &&
    !textOverlaps(verdict, primary) &&
    !failedPillarText.some((t) => textOverlaps(primary, t));
  const showCauseNext = !overallPerfect && (showPrimary || action.length > 0);
  const warningList = (warnings ?? []).filter(Boolean);
  const gapList = (dataGaps ?? []).filter(Boolean);
  const splitList = splitInsights?.insights ?? [];
  const hasContextBlocks = splitList.length > 0 || warningList.length > 0 || gapList.length > 0;

  return (
    <section
      className={cn(
        embedded ? "border-t border-slate-200/80" : "rounded-lg border-2 shadow-sm",
        !embedded && (overallPerfect ? "border-emerald-400 bg-emerald-50/60" : "border-rose-400 bg-rose-50/50"),
        embedded && (overallPerfect ? "bg-emerald-50/40" : "bg-rose-50/35")
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/70 px-4 py-3">
        <div>
          <h2 className="text-sm font-bold text-slate-900">{title}</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">Checked against Perfect Order criteria</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SynthesisSourceTag source={synthesisSource} />
          <Tag tone={overallPerfect ? "ok" : "bad"} className="text-[12px] uppercase tracking-wide">
            {overallPerfect ? "Pass" : "Fail"}
          </Tag>
        </div>
      </div>
      <ul className="grid gap-2 border-b border-slate-200/70 px-3 py-3 sm:grid-cols-2 lg:grid-cols-5">
        {(scorecard.pillars ?? []).map((p) => (
          <PerfectOrderPillar key={p.id} pillar={p} />
        ))}
      </ul>
      <div className="space-y-3 px-4 py-3">
        {verdict ? (
          <p className="text-[15px] font-semibold leading-snug text-slate-900">{verdict}</p>
        ) : null}
        {showSubline ? <p className="text-[13px] leading-relaxed text-slate-700">{subline}</p> : null}
        {showCauseNext ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {showPrimary ? (
              <div className="rounded-md border border-slate-200 bg-white/80 px-3 py-2">
                <p className="text-[10px] font-bold uppercase text-slate-500">Cause</p>
                <p className="mt-1 text-[12px] leading-relaxed text-slate-900">{primary}</p>
              </div>
            ) : null}
            {action ? (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2">
                <p className="text-[10px] font-bold uppercase text-amber-800">Next step</p>
                <p className="mt-1 text-[12px] leading-relaxed text-amber-950">{action}</p>
              </div>
            ) : null}
          </div>
        ) : null}
        {cartJourney?.available && (cartJourney.headline || (cartJourney.insights ?? []).length > 0) ? (
          <div className="rounded-md border border-violet-200 bg-violet-50/80 px-3 py-2">
            <p className="text-[10px] font-bold uppercase tracking-wide text-violet-800">Allocation journey on cart</p>
            <div className="mt-1.5 space-y-1 text-[12px] leading-relaxed text-violet-950">
              {cartJourney.headline ? <p>{cartJourney.headline}</p> : null}
              {(cartJourney.insights ?? []).slice(0, 2).map((line, i) => (
                <p key={i}>{line}</p>
              ))}
            </div>
          </div>
        ) : null}
        {scorecard.hint ? (
          <p className="text-[11px] text-slate-500" title={scorecard.hint}>
            {scorecard.hint}
          </p>
        ) : null}
        {hasContextBlocks ? (
          <div className="space-y-2 border-t border-slate-200/60 pt-3">
            {splitList.length > 0 ? (
              <div className="rounded-md border border-violet-200 bg-violet-50/80 px-3 py-2">
                <p className="text-[10px] font-bold uppercase tracking-wide text-violet-800">Split order</p>
                <div className="mt-1.5 space-y-1.5 text-[12px] leading-relaxed text-violet-950">
                  {splitList.map((ins, i) => (
                    <p key={i}>
                      <strong>{ins.headline}</strong>
                      {ins.detail ? <span className="opacity-90"> — {ins.detail}</span> : null}
                    </p>
                  ))}
                </div>
              </div>
            ) : null}
            {warningList.length > 0 ? (
              <div className="rounded-md border border-amber-200 bg-amber-50/80 px-3 py-2">
                <p className="text-[10px] font-bold uppercase tracking-wide text-amber-900">Data quality</p>
                <ul className="mt-1 list-inside list-disc text-[12px] leading-relaxed text-amber-950">
                  {warningList.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {gapList.length > 0 ? (
              <div className="rounded-md border border-amber-200 bg-amber-50/80 px-3 py-2">
                <p className="text-[10px] font-bold uppercase tracking-wide text-amber-900">Data gaps</p>
                <ul className="mt-1 list-inside list-disc text-[12px] leading-relaxed text-amber-950">
                  {gapList.map((g, i) => (
                    <li key={i}>{g}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function PerfectOrderPillar({ pillar }: { pillar: OrderRcaPerfectOrderPillar }) {
  const title = PERFECT_ORDER_PILLAR_TITLES[pillar.id] ?? pillar.id;
  const tone =
    pillar.status === "unknown" ? "slate" : pillar.status === "pass" ? "ok" : ("bad" as const);
  return (
    <li
      className={cn(
        "rounded-lg border px-3 py-2.5",
        pillar.status === "pass" && "border-emerald-200 bg-emerald-50/80",
        pillar.status === "fail" && "border-rose-200 bg-rose-50/80",
        pillar.status === "unknown" && "border-slate-200 bg-slate-50"
      )}
      title={pillar.detail}
    >
      <p className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{title}</p>
      <p className="mt-1 flex flex-wrap items-center gap-1.5">
        <span
          className={cn(
            "inline-block h-2 w-2 shrink-0 rounded-full",
            pillar.status === "pass" && "bg-emerald-500",
            pillar.status === "fail" && "bg-rose-500",
            pillar.status === "unknown" && "bg-slate-400"
          )}
          aria-hidden
        />
        <Tag tone={tone} compact>
          {displayVal(pillar.label)}
        </Tag>
      </p>
      {pillar.detail ? (
        <p className="mt-1 line-clamp-2 text-[10px] leading-snug text-slate-600">{pillar.detail}</p>
      ) : null}
    </li>
  );
}

function KpiStrip({
  pf,
  preferred,
  orderId,
  parentId,
  hideAllocationReason,
  scorecard,
  synthesis,
  synthesisSource,
  splitInsights,
  warnings,
  dataGaps,
  cartJourney,
}: {
  pf: OrderRcaPreflight;
  preferred: OrderRcaPreflight["preferred_vendors"];
  orderId: string;
  parentId?: string | null;
  hideAllocationReason?: boolean;
  scorecard?: OrderRcaPerfectOrder;
  synthesis?: OrderRcaReport["synthesis"];
  synthesisSource?: string;
  splitInsights?: OrderRcaSplitInsights | null;
  warnings?: string[];
  dataGaps?: string[];
  cartJourney?: CartAllocationJourney;
}) {
  const late = typeof pf.late_minutes === "number" ? pf.late_minutes : null;
  const breachMin =
    typeof pf.breach_minutes === "number" ? pf.breach_minutes : null;
  const lateDisplay =
    pf.late_minutes_display ?? formatDurationMinutes(late) ?? null;
  const breachDisplay =
    pf.breach_minutes_display ?? formatDurationMinutes(breachMin) ?? null;
  const deliverySub = (() => {
    const vsFirst = " vs first promised";
    if (pf.breach_kind === "delivered_late" && breachDisplay) {
      return `${breachDisplay} late${vsFirst}`;
    }
    if (pf.breach_kind === "open_past_promise" && breachDisplay) {
      return `${breachDisplay} past promise${vsFirst}`;
    }
    if (pf.breach_kind === "delivered_on_time") return "On time vs first promised";
    if (pf.breach_kind === "pending_within_sla") return "Within promise";
    if (lateDisplay) return `${lateDisplay} late${vsFirst}`;
    if (pf.is_eta_breached && pf.breach_kind === "open_past_promise") {
      return `Past promise${vsFirst}`;
    }
    return undefined;
  })();
  const etaJumps =
    pf.eta_jumps?.filter((j) => j.display?.trim()) ??
    (() => {
      const first = pf.promised_first?.display ?? pf.promised_delivery;
      const current = pf.promised_current?.display;
      const actual = pf.actual_delivery;
      const rows: NonNullable<OrderRcaPreflight["eta_jumps"]> = [];
      if (first) rows.push({ label: "First promised", display: first });
      if (current && current !== first) rows.push({ label: "ETA 1", display: current });
      if (actual) rows.push({ label: "Actual", display: actual });
      return rows;
    })();
  const locationParts = [pf.city, pf.zone, pf.pincode].filter(Boolean);
  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-stretch gap-2 bg-slate-900 p-2.5 text-white">
      {locationParts.length > 0 ? (
        <Kpi
          label="Location"
          value={
            <span className="text-[11px] leading-snug">
              {locationParts.map((p, i) => (
                <span key={i}>
                  {i > 0 ? <span className="text-slate-400"> · </span> : null}
                  {i === locationParts.length - 1 && pf.pincode ? (
                    <span className="font-mono">{displayVal(p)}</span>
                  ) : (
                    displayVal(p)
                  )}
                </span>
              ))}
            </span>
          }
        />
      ) : null}
      {pf.order_status ? (
        <Kpi label="Order status" value={<span className="text-[11px] leading-snug">{displayVal(pf.order_status)}</span>} />
      ) : null}
      {pf.service_type ? (
        <Kpi label="Service" value={<span className="text-[11px] leading-snug">{displayVal(pf.service_type)}</span>} />
      ) : null}
      <Kpi
        label="Order placed (IST)"
        value={<span className="text-[11px] leading-snug">{displayVal(pf.placed_at)}</span>}
      />
      <Kpi label="Order" value={<span className="font-mono text-[12px]">{orderId}</span>} />
      {parentId ? <Kpi label="Parent order" value={<span className="font-mono text-[11px]">{parentId}</span>} /> : null}
      <Kpi
        label="Allocation"
        value={
          pf.allocation_badge === "N/A" ? (
            <span className="text-[11px] font-semibold text-slate-300">N/A</span>
          ) : (
            <Badge badge={String(pf.allocation_badge ?? "")} />
          )
        }
      />
      {pf.allocation_badge_reason && !hideAllocationReason ? (
        <Kpi
          label="Why"
          value={
            <span className="max-w-[200px] text-[11px] leading-snug" title={pf.allocation_badge_reason}>
              {pf.allocation_badge_reason}
            </span>
          }
        />
      ) : null}
      <Kpi label="Tier" value={displayVal(pf.allocation_tier_label)} />
      <Kpi
        label="Fulfilled from"
        value={
          <span className="max-w-[160px] truncate font-mono text-[11px]" title={pf.actual_vendor}>
            {displayVal(pf.actual_vendor)}
          </span>
        }
      />
      <Kpi label="Distance" value={pf.distance_km != null ? `${pf.distance_km} km` : "—"} />
      <Kpi
        label="Delivery ETA (IST)"
        value={
          <div className="space-y-0.5 text-[11px]">
            {etaJumps.map((row, i) => (
              <div key={`${row.label ?? "eta"}-${i}`}>
                <span className="text-slate-400">{row.label ?? "ETA"} </span>
                {displayVal(row.display)}
              </div>
            ))}
            {etaJumps.length === 0 ? (
              <span className="text-slate-400">—</span>
            ) : null}
          </div>
        }
        sub={deliverySub}
      />
      {preferred?.map((p) => (
        <Kpi
          key={p.rank}
          label={`Preferred warehouse #${p.rank}`}
          value={
            <span className="font-mono text-[11px]">
              {displayVal(p.physical_store)} · {displayVal(p.km)} km
            </span>
          }
        />
      ))}
      </div>
      {scorecard ? (
        <OrderOutcomeCard
          embedded
          scorecard={scorecard}
          synthesis={synthesis}
          synthesisSource={synthesisSource}
          splitInsights={splitInsights}
          warnings={warnings}
          dataGaps={dataGaps}
          cartJourney={cartJourney}
        />
      ) : null}
    </section>
  );
}

function Kpi({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="rounded-md bg-white/10 px-2.5 py-1.5">
      <p className="text-[9px] font-bold uppercase tracking-wider text-slate-400">{label}</p>
      <div className="mt-0.5 font-semibold leading-tight">{value}</div>
      {sub ? <p className={cn("mt-0.5 text-[10px]", sub.includes("late") ? "text-amber-300" : "text-emerald-300")}>{sub}</p> : null}
    </div>
  );
}

function OpsSlaStatusTag({ status }: { status?: string }) {
  const s = String(status ?? "").toLowerCase();
  if (s === "on_time") {
    return <Tag tone="ok">On time</Tag>;
  }
  if (s === "late") {
    return <Tag tone="bad">Late</Tag>;
  }
  return <span className="text-slate-400">—</span>;
}

function OpsTimelineView({ transitions }: { transitions: Array<Record<string, unknown>> }) {
  if (!transitions.length) return <p className="text-[12px] text-slate-500">Not enough status milestones for a timeline.</p>;
  const scaleTransitions = transitions.filter((t) => !t.sla_excluded);
  const max = Math.max(...scaleTransitions.map((t) => Number(t.duration_min) || 0), 1);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-0">
        {transitions.map((t, i) => {
          const min = Number(t.duration_min);
          const durationLabel = String(t.duration_display ?? (min > 0 ? `${min}m` : "< 1 min"));
          const showMin = typeof min === "number" && min > 0;
          const slaExcluded = Boolean(t.sla_excluded);
          const long = showMin && min > 180 && !slaExcluded;
          const pct = showMin ? Math.max(8, Math.round((min / max) * 100)) : 8;
          const late = !slaExcluded && String(t.sla_status ?? "").toLowerCase() === "late";
          return (
            <div
              key={i}
              className="flex min-w-0 flex-1 flex-col items-center"
              title={String(t.hover ?? t.at ?? "")}
            >
              <span
                className={cn(
                  "mb-1 text-[10px] font-bold tabular-nums",
                  late ? "text-amber-700" : showMin ? "text-slate-600" : "text-slate-400",
                )}
              >
                {durationLabel}
              </span>
              <div className="h-2 w-full max-w-[120px] overflow-hidden rounded-full bg-slate-200">
                <div
                  className={cn(
                    "h-full rounded-full",
                    slaExcluded ? "bg-slate-400" : long || late ? "bg-amber-500" : "bg-sky-500",
                  )}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <p className="mt-1.5 px-0.5 text-center text-[10px] font-semibold leading-tight text-slate-800">
                {displayVal(t.label)}
              </p>
            </div>
          );
        })}
      </div>
      <DataTable
        cols={[
          { key: "phase", label: "Phase completed" },
          { key: "min", label: "Minutes" },
          { key: "at", label: "At" },
          { key: "sla", label: "Default SLA" },
          { key: "status", label: "Status" },
        ]}
        colWidths={["28%", "14%", "26%", "14%", "18%"]}
      >
        {transitions.map((t, i) => (
          <tr key={i} className="border-t border-slate-100" title={String(t.hover ?? "")}>
            <td className="break-words px-2 py-1.5 font-medium text-slate-800">{displayVal(t.label)}</td>
            <td className="px-2 py-1.5 tabular-nums text-slate-700">{displayVal(t.duration_display)}</td>
            <td className="break-words px-2 py-1.5 text-slate-600">{displayVal(t.at)}</td>
            <td className="px-2 py-1.5 tabular-nums text-slate-700">{displayVal(t.default_sla)}</td>
            <td className="px-2 py-1.5">
              <OpsSlaStatusTag status={String(t.sla_status ?? "")} />
            </td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}

function HypothesisTable({ items }: { items: OrderRcaReport["synthesis"]["hypotheses"] }) {
  if (!items?.length) return null;
  return (
    <DataTable
      cols={[
        { key: "align", label: "Fit" },
        { key: "finding", label: "Finding" },
        { key: "hyp", label: "Interpretation" },
      ]}
      colWidths={["14%", "42%", "44%"]}
    >
      {items.map((h, i) => {
        const rawAlign = h.alignment ?? "";
        const a = String(rawAlign).toLowerCase();
        const tone = a === "supported" ? "ok" : a === "partial" ? "warn" : a === "contradicted" ? "bad" : "slate";
        return (
          <tr key={i} className="border-t border-slate-100 align-top">
            <td className="px-2 py-2">
              <Tag tone={tone}>{rawAlign ? String(rawAlign) : "—"}</Tag>
            </td>
            <td className="break-words px-2 py-2 text-[11px] text-slate-900">{displayVal(h.finding)}</td>
            <td className="break-words px-2 py-2 text-[11px] text-slate-600">{displayVal(h.hypothesis)}</td>
          </tr>
        );
      })}
    </DataTable>
  );
}

export default function OrderRcaPage() {
  const [orderId, setOrderId] = useState("");
  const [run, setRun] = useState<OrderRcaRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollCancelledRef = useRef(false);
  const pollStartedRef = useRef<number>(0);

  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const po = params.get("po")?.trim();
      if (po) {
        setOrderId(po);
        sessionStorage.setItem(LAST_PO_KEY, po);
        return;
      }
      const saved = sessionStorage.getItem(LAST_PO_KEY);
      if (saved) setOrderId(saved);
    } catch {
      /* ignore */
    }
  }, []);

  const stopPoll = useCallback(() => {
    pollCancelledRef.current = true;
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  }, []);

  useEffect(() => () => stopPoll(), [stopPoll]);

  const poll = useCallback(
    (runId: string) => {
      stopPoll();
      pollCancelledRef.current = false;
      pollStartedRef.current = Date.now();
      const tick = async () => {
        if (pollCancelledRef.current) return;
        if (Date.now() - pollStartedRef.current > POLL_MAX_MS) {
          stopPoll();
          setBusy(false);
          setErr("Diagnosis timed out after 1 hour. Start a new run or check run status later.");
          return;
        }
        try {
          const doc = await getOrderRcaRun(runId);
          if (pollCancelledRef.current) return;
          setRun(doc);
          if (doc.status === "completed" || doc.status === "failed") {
            stopPoll();
            setBusy(false);
          }
        } catch (e) {
          if (!pollCancelledRef.current) {
            setErr(e instanceof Error ? e.message : "Poll failed");
            setBusy(false);
          }
          stopPoll();
        }
      };
      void tick();
      pollRef.current = setInterval(() => void tick(), POLL_INTERVAL_MS);
    },
    [stopPoll]
  );

  const onRun = async () => {
    const id = orderId.trim();
    if (!id) return;
    setErr(null);
    setRun(null);
    setBusy(true);
    try {
      const started = await startOrderRcaDiagnose(id);
      try {
        sessionStorage.setItem(LAST_PO_KEY, id);
      } catch {
        /* ignore */
      }
      setRun({ ...started, status: "queued", progress: { completed: 0, total: 6, label: "Queued" } });
      poll(started.run_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to start");
      setBusy(false);
    }
  };

  const report = run?.report;
  const facts = report?.facts;
  const syn = report?.synthesis;
  const pf = facts?.preflight;
  const preferred = pf?.preferred_vendors ?? [];
  const allocationUnavailableOnly = Boolean(facts?.allocation_unavailable?.message);
  const p1 = facts?.p1;
  const p2 = facts?.p2;
  const splitInsights = facts?.split_insights;
  const reportReady = Boolean(facts && syn && pf);
  const runComplete = run?.status === "completed";
  const runFailed = run?.status === "failed";

  return (
    <div className="min-h-0 bg-slate-100/80">
      <div className="mx-auto max-w-[1500px] px-3 py-3">
        <header className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 shadow-sm">
          <h1 className="text-base font-bold text-slate-900">Order RCA</h1>
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void onRun();
            }}
          >
            <label htmlFor="order-rca-po" className="sr-only">
              Order PO number
            </label>
            <input
              id="order-rca-po"
              name="orderId"
              aria-label="Order PO number"
              className="min-w-[200px] rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1.5 font-mono text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20"
              value={orderId}
              onChange={(e) => setOrderId(e.target.value)}
              placeholder="PO number"
              disabled={busy}
              autoComplete="off"
            />
            <button
              type="submit"
              className="rounded-md bg-sky-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-sky-700 disabled:opacity-50"
              disabled={busy || !orderId.trim()}
              aria-busy={busy}
            >
              {busy ? "Running…" : "Run diagnosis"}
            </button>
            {busy ? (
              <button
                type="button"
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
                onClick={() => {
                  stopPoll();
                  setBusy(false);
                }}
              >
                Cancel
              </button>
            ) : null}
          </form>
        </header>

        <div role="status" aria-live="polite" aria-atomic="true">
          {err ? (
            <div role="alert">
              <Alert tone="bad">{err}</Alert>
            </div>
          ) : null}
          {busy && run ? <ProgressStatus run={run} /> : null}
          {runFailed ? (
            <div role="alert">
              <Alert tone="bad">{displayVal(run.error?.message, "Failed")}</Alert>
            </div>
          ) : null}
        </div>

        {runComplete && !reportReady && run ? <ReportIncomplete run={run} /> : null}

        {!busy && !run && !reportReady ? <IdleState /> : null}

        {reportReady && facts && syn && pf ? (
          <div className="space-y-3">
            <KpiStrip
              pf={pf}
              preferred={preferred}
              orderId={facts.order_id}
              parentId={facts.parent_id}
              hideAllocationReason={allocationUnavailableOnly}
              scorecard={facts.perfect_order}
              synthesis={syn}
              synthesisSource={report?.synthesis_source}
              splitInsights={splitInsights}
              warnings={facts.warnings}
              dataGaps={syn.data_gaps}
              cartJourney={facts.cart_allocation_journey}
            />
            <SkuStrip skus={facts.skus} />
            <CartAllocationJourneyPanel journey={facts.cart_allocation_journey} />

            {allocationUnavailableOnly ? (
              <Alert tone="warn">
                <p className="text-[13px] font-medium leading-relaxed">{facts.allocation_unavailable!.message}</p>
              </Alert>
            ) : null}

            {!allocationUnavailableOnly ? (
              <>
            <Panel
                  title="Allocation store matrix"
                  description="Every physical store and virtual vendor evaluated at allocation — location, stock, services, outcome, and rejection reasons."
                  badge="Allocation"
                  accent="indigo"
                  className="min-w-0 ring-1 ring-indigo-200/60"
                >
                  <StorePanel
                    views={facts.p3 ?? {}}
                    allocationUnavailable={facts.allocation_unavailable}
                  />
                </Panel>

                <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                  <Panel
                    phase="P1"
                    title="MSN adherence"
                    description="Per store in the allocation matrix: one row per order line (MSN, on-shelf, asked qty, status)."
                    badge="planning"
                    accent="emerald"
                  >
                    {p1?.msn_adherence ? (
                      <MsnAdherenceTable block={p1.msn_adherence} />
                    ) : (
                      <UnconfiguredPanel label="MSN adherence" status={p1?.status} />
                    )}
                  </Panel>
                  <Panel
                    phase="P2"
                    title="Procurement funnel"
                    description="Drop and replenish signals from planning when connected."
                    badge="planning"
                    accent="amber"
                  >
                    {(p2?.rows ?? []).length > 0 ? (
                      <PlanningTable rows={(p2?.rows ?? []) as PlanningRow[]} />
                    ) : (
                      <UnconfiguredPanel
                        label="Procurement funnel"
                        status={p2?.status}
                        message={p2?.status_message}
                      />
                    )}
                  </Panel>
                </div>

                <Panel
                  phase="Ops"
                  title="Operations trace"
                  description="Status milestones with minute cutoffs (Default SLA) vs actual duration. All clock times are IST (Asia/Kolkata)."
                  badge="Status history"
                  accent="slate"
                >
                  <div className="min-w-0 space-y-4">
                    {facts.operations?.timeline_note ? (
                      <p className="text-[12px] leading-relaxed text-slate-700">{facts.operations.timeline_note}</p>
                    ) : null}
                    {facts.operations?.return_note ? (
                      <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-950">
                        {facts.operations.return_note}
                      </p>
                    ) : null}
                    <OpsTimelineView
                      transitions={(facts.operations?.status_transitions ?? []) as Array<Record<string, unknown>>}
                    />
                    <LastMilePanel operations={facts.operations} />
                  </div>
                </Panel>

                {(syn.hypotheses?.length ?? 0) > 0 ? (
                  <Panel
                    title="Hypotheses"
                    description="Top hypotheses ranked by the model (max 10). Finding vs interpretation vs fit."
                    badge={report?.synthesis_source === "openai" ? "OpenAI" : "narrative"}
                    accent="slate"
                  >
                    <HypothesisTable items={syn.hypotheses} />
                  </Panel>
                ) : null}
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Alert({ tone, children }: { tone: "bad" | "warn" | "violet"; children: React.ReactNode }) {
  const t = {
    bad: "border-rose-200 bg-rose-50 text-rose-900",
    warn: "border-amber-200 bg-amber-50 text-amber-950",
    violet: "border-violet-200 bg-violet-50 text-violet-900",
  }[tone];
  return <div className={cn("rounded-lg border px-3 py-2 text-[12px] leading-relaxed", t)}>{children}</div>;
}
