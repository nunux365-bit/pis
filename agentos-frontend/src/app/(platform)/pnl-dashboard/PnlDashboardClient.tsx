"use client";

import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { userHasAnyRole } from "@/lib/userAccess";

/* ============================================================
   TYPES
   ============================================================ */
type LineItem = {
  sort: number;
  particulars: string;
  value_cr: number;
  pct_nmv: number | null;
  type: string;
  section?: string;
  indent?: number;
  italic?: boolean;
  bold?: boolean;
  highlight?: string;
};
type Row = {
  pnl_type: string;
  month: string;
  city: string;
  store_type: string;
  order_channel: string;
  line_items: LineItem[];
};
export type PnlData = {
  generated_at: string;
  months: string[];
  pnl_types: string[];
  cities: string[];
  store_types: string[];
  order_channels: string[];
  rows: Row[];
};


/* ============================================================
   FORMATTERS — Indian conventions
   ============================================================ */
const inr = (n: number, dp = 2) => Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function monthLabel(m: string) {
  const [y, mo] = m.split("-").map(Number);
  return MONTH_NAMES[mo - 1] + " " + y;
}
function monthShort(m: string) {
  const [y, mo] = m.split("-").map(Number);
  return MONTH_SHORT[mo - 1] + " '" + String(y).slice(-2);
}
function fyLabel(m: string) {
  const [y, mo] = m.split("-").map(Number);
  return "FY" + String(mo >= 4 ? y : y - 1).slice(-2);
}

/* value formatting per type → JSX (negatives shown in red parens) */
function fmtCell(li: LineItem): ReactNode {
  const v = li.value_cr;
  switch (li.type) {
    case "count":
      return inr(v, 0);
    case "ratio":
      return inr(v, 2);
    case "pct":
      return inr(v, 1) + "%";
    case "rupees":
      return "₹ " + inr(v, 2);
    default:
      return v < 0 ? <span className="neg">({inr(v, 2)})</span> : inr(v, 2);
  }
}
function fmtPctVal(p: number | null | undefined): ReactNode {
  if (p === null || p === undefined) return "—";
  return p < 0 ? <span className="neg">({inr(p, 1)})%</span> : inr(p, 1) + "%";
}

function computeChildren(items: LineItem[]) {
  const map = new Map<number, number[]>();
  for (let i = 0; i < items.length; i++) {
    const ind = items[i].indent || 0;
    const kids: number[] = [];
    for (let j = i + 1; j < items.length; j++) {
      if ((items[j].indent || 0) > ind) kids.push(items[j].sort);
      else break;
    }
    if (kids.length) map.set(items[i].sort, kids);
  }
  return map;
}

const FILTERS = ["pnl", "month", "city", "store", "channel"] as const;
type Which = (typeof FILTERS)[number];

export default function PnlDashboardClient({ data: initialData }: { data: PnlData }) {
  const router = useRouter();
  const { user, loading } = useAuth();
  const mayView = Boolean(user) && userHasAnyRole(user?.roles, "system_admin", "pnl_dashboard_access");

  useEffect(() => {
    if (loading || !user) return;
    if (!mayView) router.replace("/dashboard");
  }, [loading, user, mayView, router]);

  const data = initialData;
  const [pnl, setPnl] = useState<Set<string>>(() => new Set());
  const [month, setMonth] = useState<Set<string>>(() => new Set([initialData.months[initialData.months.length - 1]]));
  const [city, setCity] = useState<Set<string>>(() => new Set(["All"]));
  const [store, setStore] = useState<Set<string>>(() => new Set(["All"]));
  const [channel, setChannel] = useState<Set<string>>(() => new Set(["All"]));
  const [collapsed, setCollapsed] = useState<Set<number>>(() => new Set());
  const [openPanel, setOpenPanel] = useState<Which | null>(null);

  /* close any open dropdown on a click outside the filter groups */
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (!(e.target as HTMLElement)?.closest?.(".f-group")) setOpenPanel(null);
    };
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, []);

  const blockIndex = useMemo(() => {
    const m = new Map<string, Row>();
    for (const r of data.rows) m.set([r.pnl_type, r.month, r.city, r.store_type, r.order_channel].join("|"), r);
    return m;
  }, [data]);
  const getBlock = (p: string, mo: string, c: string, s: string, ch: string) => blockIndex.get([p, mo, c, s, ch].join("|")) || null;

  const stateSet = (which: Which): Set<string> =>
    which === "pnl" ? pnl : which === "month" ? month : which === "city" ? city : which === "store" ? store : channel;
  const setStateSet = (which: Which, ns: Set<string>) => {
    if (which === "pnl") setPnl(ns);
    else if (which === "month") setMonth(ns);
    else if (which === "city") setCity(ns);
    else if (which === "store") setStore(ns);
    else setChannel(ns);
  };

  function selectedList(set: Set<string>): string[] {
    if (set.size === 0 || set.has("All")) return ["All"];
    return [...set];
  }

  function aggregateMonth(mo: string) {
    const pnls = pnl.size ? [...pnl] : data.pnl_types.slice();
    const cities = selectedList(city);
    const stores = selectedList(store);
    const chans = selectedList(channel);
    const blocks: Row[] = [];
    for (const p of pnls)
      for (const c of cities)
        for (const s of stores)
          for (const ch of chans) {
            const b = getBlock(p, mo, c, s, ch);
            if (b) blocks.push(b);
          }
    if (!blocks.length) return null;
    const acc = new Map<string, LineItem & { _mc: boolean }>();
    for (const b of blocks)
      for (const li of b.line_items) {
        const key = li.sort + "|" + li.particulars;
        if (!acc.has(key)) acc.set(key, { ...li, value_cr: 0, _mc: li.type === "money" || li.type === "count" });
        const a = acc.get(key)!;
        if (a._mc) a.value_cr += li.value_cr || 0;
        else a.value_cr = li.value_cr;
      }
    const items = [...acc.values()].sort((x, y) => x.sort - y.sort);
    const nmvItem = items.find((x) => x.highlight === "anchor");
    const NMV = nmvItem ? nmvItem.value_cr : 0;
    const orders = (items.find((x) => x.sort === 1) || ({} as LineItem)).value_cr || 0;
    const ol = (items.find((x) => x.sort === 2) || ({} as LineItem)).value_cr || 0;
    for (const it of items) {
      if (it.type === "money") {
        it.pct_nmv = it.highlight === "anchor" ? 100 : NMV ? +((it.value_cr / NMV) * 100).toFixed(2) : null;
      } else if (it.type === "ratio" && it.sort === 3) {
        it.value_cr = orders ? +(ol / orders).toFixed(4) : 0;
      } else if (it.type === "rupees") {
        if (/AOV \(without VAS\)/i.test(it.particulars)) {
          const g = items.find((x) => x.sort === 20);
          it.value_cr = orders && g ? +((g.value_cr * 1e7) / orders).toFixed(2) : 0;
        } else if (/AOV \(with VAS\)/i.test(it.particulars)) {
          const g = items.find((x) => x.sort === 40);
          it.value_cr = orders && g ? +((g.value_cr * 1e7) / orders).toFixed(2) : 0;
        } else if (/per Order Line/i.test(it.particulars)) {
          const f = items.find((x) => x.sort === 100);
          it.value_cr = ol && f ? +((Math.abs(f.value_cr) * 1e7) / ol).toFixed(2) : it.value_cr;
        }
      }
    }
    return { items, nmv: NMV, orders, blocks: blocks.length };
  }

  /* ---- filter interactions ---- */
  function toggleOpt(which: Which, val: string, checked: boolean) {
    const set = new Set(stateSet(which));
    const hasAll = which === "city" || which === "store" || which === "channel";
    if (hasAll) {
      if (val === "All") {
        set.clear();
        set.add("All");
      } else {
        set.delete("All");
        if (checked) set.add(val);
        else set.delete(val);
        if (set.size === 0) set.add("All");
      }
    } else {
      if (checked) set.add(val);
      else set.delete(val);
      if (which === "month" && set.size === 0) set.add(data.months[data.months.length - 1]);
    }
    setCollapsed(new Set());
    setStateSet(which, set);
  }
  function clearChip(which: Which, val: string) {
    const set = new Set(stateSet(which));
    set.delete(val);
    if (which !== "pnl" && set.size === 0) set.add("All");
    setStateSet(which, set);
  }
  function resetAll() {
    setPnl(new Set());
    setMonth(new Set([data.months[data.months.length - 1]]));
    setCity(new Set(["All"]));
    setStore(new Set(["All"]));
    setChannel(new Set(["All"]));
    setCollapsed(new Set());
  }

  /* ---- derived view data ---- */
  const months = [...month].sort().reverse();
  const aggs = months.map((m) => ({ m, agg: aggregateMonth(m) }));
  const ref = aggs.find((a) => a.agg);

  const pnlTxt = pnl.size === 0 ? "All PnL" : pnl.size === 1 ? [...pnl][0] : pnl.size + " PnL types";
  const monTxt = months.length === 1 ? monthLabel(months[0]) : months.length + " months";
  const fy = months.length ? fyLabel(months[0]) : "";
  const generated = new Date(data.generated_at);
  const tcSub = [pnlTxt];
  if (!city.has("All")) tcSub.push([...city].join(", "));
  if (!store.has("All")) tcSub.push([...store].join(", "));
  if (!channel.has("All")) tcSub.push([...channel].join(", "));

  /* KPI cards — latest selected month */
  const kpiAgg = months.length ? aggregateMonth(months[0]) : null;
  const kpiLbl = months.length ? monthShort(months[0]) : "";
  const findLine = (items: LineItem[] | undefined, pred: (l: LineItem) => boolean) => (items ? items.find(pred) : undefined);
  const kpiDefs: { label: string; get: () => LineItem | undefined; fmt: (l: LineItem) => ReactNode }[] = [
    { label: "Delivered NMV", get: () => findLine(kpiAgg?.items, (l) => l.highlight === "anchor"), fmt: (l) => <>₹{inr(l.value_cr, 1)}<span className="k-unit">Cr</span></> },
    { label: "Gross Margin", get: () => findLine(kpiAgg?.items, (l) => l.highlight === "gm"), fmt: (l) => <>{inr(l.pct_nmv ?? 0, 1)}<span className="k-unit">%</span></> },
    { label: "CM1", get: () => findLine(kpiAgg?.items, (l) => l.highlight === "cm1"), fmt: (l) => <>{inr(l.pct_nmv ?? 0, 1)}<span className="k-unit">%</span></> },
    { label: "CM2", get: () => findLine(kpiAgg?.items, (l) => l.highlight === "cm2"), fmt: (l) => <>{inr(l.pct_nmv ?? 0, 1)}<span className="k-unit">%</span></> },
    { label: "Delivered Orders", get: () => findLine(kpiAgg?.items, (l) => l.sort === 1), fmt: (l) => inr(l.value_cr, 0) },
  ];

  /* table skeleton + collapse handling */
  const skeleton = ref?.agg ? [...ref.agg.items].sort((a, b) => a.sort - b.sort) : [];
  const childMap = computeChildren(skeleton);
  const hidden = new Set<number>();
  for (const s of collapsed) {
    const kids = childMap.get(s);
    if (kids) kids.forEach((k) => hidden.add(k));
  }
  const monthMaps = aggs.map((a) => {
    const mp = new Map<string, LineItem>();
    if (a.agg) a.agg.items.forEach((it) => mp.set(it.sort + "|" + it.particulars, it));
    return mp;
  });

  /* ---- filter dropdown configs ---- */
  const panelCfg: Record<Which, { set: Set<string>; opts: string[]; fmt?: (o: string) => string }> = {
    pnl: { set: pnl, opts: data.pnl_types },
    month: { set: month, opts: data.months, fmt: monthShort },
    city: { set: city, opts: data.cities },
    store: { set: store, opts: data.store_types },
    channel: { set: channel, opts: data.order_channels },
  };
  const filterLabels: Record<Which, string> = { pnl: "PnL Type", month: "Month", city: "City", store: "Store Type", channel: "Order Channel" };

  function multiBtnLabel(which: Which): ReactNode {
    if (which === "pnl") {
      if (pnl.size === 0) return "All PnL";
      const arr = [...pnl];
      return pnl.size === 1 ? arr[0] : <>{arr[0]} <span className="ms-count">+{pnl.size - 1}</span></>;
    }
    if (which === "month") {
      if (month.size === 0) return "Select months";
      return [...month].sort().reverse().map(monthShort).join(", ");
    }
    const set = stateSet(which);
    if (set.size === 0 || set.has("All")) return "All";
    const arr = [...set];
    if (arr.length === 1) return arr[0];
    return <>{arr[0]} <span className="ms-count">+{arr.length - 1}</span></>;
  }

  /* active filter chips */
  const chips: { label: string; which: Which; val: string }[] = [];
  ([["City", "city"], ["Store Type", "store"], ["Order Channel", "channel"]] as [string, Which][]).forEach(([lab, w]) => {
    const set = stateSet(w);
    if (!set.has("All") && set.size) [...set].forEach((v) => chips.push({ label: lab, which: w, val: v }));
  });
  if (pnl.size) [...pnl].forEach((v) => chips.push({ label: "PnL", which: "pnl", val: v }));

  if (loading || !user) {
    return <div className="flex flex-1 items-center justify-center p-6 text-sm text-[var(--text-muted)]">Loading…</div>;
  }
  if (!mayView) {
    return <div className="flex flex-1 items-center justify-center p-6 text-sm text-[var(--text-muted)]">Redirecting…</div>;
  }

  return (
    <div className="pnl-root">
      <header>
        <div className="hd-inner">
          <div>
            <h1>P&amp;L Dashboard</h1>
            <div className="subtitle">
              <b>{pnlTxt}</b> · {monTxt}
              {fy ? " · " + fy : ""}
            </div>
          </div>
          <div className="freshness">
            <span className="dot" />
            Data as of {generated.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: true })}
          </div>
        </div>
      </header>

      <div className="filters-wrap">
        <div className="filters">
          {FILTERS.map((which) => {
            const cfg = panelCfg[which];
            return (
              <div className="f-group" key={which}>
                <label>{filterLabels[which]}</label>
                <button
                  className="ms-btn"
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpenPanel(openPanel === which ? null : which);
                  }}
                >
                  {multiBtnLabel(which)}
                </button>
                <div className={"ms-panel" + (openPanel === which ? " open" : "")} onClick={(e) => e.stopPropagation()}>
                  {cfg.opts.map((o) => {
                    const isAll = o === "All";
                    const label = cfg.fmt ? cfg.fmt(o) : o;
                    return (
                      <label className={"ms-opt" + (isAll ? " is-all" : "")} key={o}>
                        <input type="checkbox" checked={cfg.set.has(o)} onChange={(e) => toggleOpt(which, o, e.target.checked)} />
                        <span>{label}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}
          <div className="f-actions">
            <button className="reset-btn" onClick={resetAll}>
              Reset all
            </button>
          </div>
        </div>
        <div className="chips">
          {chips.map((c) => (
            <span className="chip" key={c.which + ":" + c.val}>
              {c.label}: {c.val}
              <button aria-label="Clear" onClick={() => clearChip(c.which, c.val)}>
                ×
              </button>
            </span>
          ))}
        </div>
      </div>

      <main>
        <div className="kpis">
          {kpiAgg &&
            kpiDefs.map((d) => {
              const v = d.get();
              if (!v) return null;
              return (
                <div className="kpi" key={d.label}>
                  <div className="k-label">{d.label}</div>
                  <div className="k-value num">{d.fmt(v)}</div>
                  <div className="k-sub">{kpiLbl}</div>
                </div>
              );
            })}
        </div>

        <div className="table-card">
          <div className="tc-toolbar">
            <div>
              <h2>Profit &amp; Loss Statement</h2>
              <div className="tc-sub">{tcSub.join(" · ")}</div>
            </div>
            <div />
          </div>
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th style={months.length > 1 ? undefined : { width: "46%" }}>Particulars</th>
                  {months.map((m) => (
                    <th key={m}>
                      <span className="th-mon">{monthShort(m)}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {!months.length ? (
                  <tr>
                    <td style={{ textAlign: "center", color: "var(--muted)", padding: "40px" }}>Select at least one month</td>
                  </tr>
                ) : !ref ? (
                  <tr>
                    <td style={{ textAlign: "center", color: "var(--muted)", padding: "40px" }}>No data for this filter combination</td>
                  </tr>
                ) : (
                  skeleton.map((li) => {
                    const cls: string[] = [];
                    if (li.indent === 1) cls.push("ind-1");
                    if (li.indent === 2) cls.push("ind-2");
                    if (li.italic) cls.push("r-italic");
                    if (li.bold && !li.highlight) cls.push("r-bold");
                    if (li.highlight) {
                      cls.push("hl", "hl-" + li.highlight);
                    } else if (li.bold && li.section === "revenue") {
                      cls.push("hl", "hl-revanchor");
                    }
                    if (collapsed.has(li.sort)) cls.push("collapsed-parent");
                    if (hidden.has(li.sort)) cls.push("row-hidden");
                    const hasKids = childMap.has(li.sort);
                    const unit = li.type === "money" ? <span className="cr-unit">(₹ Cr)</span> : null;
                    return (
                      <Fragment key={li.sort + "|" + li.particulars}>
                        <tr className={cls.join(" ")}>
                          <td>
                            <span className="p-cell">
                              {hasKids ? (
                                <button
                                  className="chev"
                                  aria-label="Toggle section"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    const next = new Set(collapsed);
                                    if (next.has(li.sort)) next.delete(li.sort);
                                    else next.add(li.sort);
                                    setCollapsed(next);
                                  }}
                                >
                                  <svg width="10" height="10" viewBox="0 0 10 10">
                                    <path d="M2 3.5l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                                  </svg>
                                </button>
                              ) : (
                                <span className="chev-spacer" />
                              )}
                              {li.particulars}
                              {unit}
                            </span>
                          </td>
                          {monthMaps.map((mp, i) => {
                            const cell = mp.get(li.sort + "|" + li.particulars);
                            return (
                              <td className="val num" key={i}>
                                {cell ? fmtCell(cell) : "—"}
                              </td>
                            );
                          })}
                        </tr>
                        {li.type === "money" && (
                          <tr className={"pct-row" + (hidden.has(li.sort) ? " row-hidden" : "")}>
                            <td>
                              <span className="p-cell">% of Delivered NMV (including VAS)</span>
                            </td>
                            {monthMaps.map((mp, i) => {
                              const cell = mp.get(li.sort + "|" + li.particulars);
                              return (
                                <td className="val num" key={i}>
                                  {cell ? fmtPctVal(cell.pct_nmv) : "—"}
                                </td>
                              );
                            })}
                          </tr>
                        )}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>

      <footer>Confidential — Internal use only</footer>

      <style jsx>{`
        .pnl-root {
          --orange: #ff6f4e;
          --orange-deep: #e85a38;
          --orange-band: #fff1ec;
          --orange-soft: #fff8f5;
          --ink: #1f2933;
          --ink-soft: #52606d;
          --muted: #7b8794;
          --blue-ratio: #4a6b8a;
          --paper: #ffffff;
          --warm: #f7f5f2;
          --border: #e5e1db;
          --green: #0e7c5a;
          --red: #c0392b;
          --gm-band: #fbe9e5;
          --dve-band: #23272e;
          --amber: #b7791f;
          --amber-bg: #fdf3e3;
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: var(--warm);
          color: var(--ink);
          -webkit-font-smoothing: antialiased;
          line-height: 1.45;
          font-size: 16px;
          min-height: 100%;
          display: block;
        }
        * {
          margin: 0;
          padding: 0;
          box-sizing: border-box;
        }
        .num {
          font-variant-numeric: tabular-nums;
        }
        header {
          background: var(--paper);
          border-bottom: 3px solid var(--orange);
        }
        .hd-inner {
          max-width: 1400px;
          margin: 0 auto;
          padding: 22px 32px 18px;
          display: flex;
          align-items: flex-end;
          justify-content: space-between;
          gap: 24px;
        }
        h1 {
          font-size: 28px;
          font-weight: 600;
          letter-spacing: -0.01em;
        }
        .subtitle {
          font-size: 14px;
          color: var(--ink-soft);
          margin-top: 4px;
        }
        .subtitle b {
          color: var(--ink);
          font-weight: 600;
        }
        .freshness {
          font-size: 12px;
          color: var(--muted);
          text-align: right;
          white-space: nowrap;
          padding-bottom: 4px;
        }
        .freshness .dot {
          display: inline-block;
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: var(--green);
          margin-right: 6px;
        }
        .filters-wrap {
          background: var(--paper);
          border-bottom: 1px solid var(--border);
        }
        .filters {
          max-width: 1400px;
          margin: 0 auto;
          padding: 14px 32px;
          display: flex;
          align-items: flex-start;
          gap: 18px;
          flex-wrap: wrap;
        }
        .f-group {
          display: flex;
          flex-direction: column;
          gap: 5px;
          position: relative;
        }
        .f-group label {
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.07em;
          color: var(--muted);
        }
        .ms-btn {
          font-family: inherit;
          font-size: 14px;
          color: var(--ink);
          text-align: left;
          padding: 8px 30px 8px 12px;
          border: 1px solid var(--border);
          border-radius: 8px;
          background: var(--paper)
            url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8" viewBox="0 0 12 8"><path d="M1 1l5 5 5-5" fill="none" stroke="%237B8794" stroke-width="1.6" stroke-linecap="round"/></svg>')
            no-repeat right 11px center;
          cursor: pointer;
          min-width: 150px;
          max-width: 240px;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .ms-btn:hover {
          border-color: var(--orange);
        }
        .ms-btn:focus {
          outline: 2px solid var(--orange);
          outline-offset: 1px;
          border-color: var(--orange);
        }
        .ms-btn :global(.ms-count) {
          color: var(--orange-deep);
          font-weight: 600;
        }
        .ms-panel {
          position: absolute;
          top: 100%;
          left: 0;
          margin-top: 6px;
          z-index: 50;
          background: var(--paper);
          border: 1px solid var(--border);
          border-radius: 10px;
          box-shadow: 0 8px 28px rgba(31, 41, 51, 0.14);
          padding: 6px;
          min-width: 230px;
          max-height: 340px;
          overflow-y: auto;
          display: none;
        }
        .ms-panel.open {
          display: block;
        }
        .ms-opt {
          display: flex;
          align-items: center;
          gap: 9px;
          font-size: 13.5px;
          color: var(--ink);
          padding: 7px 10px;
          border-radius: 7px;
          cursor: pointer;
          user-select: none;
          white-space: nowrap;
        }
        .ms-opt:hover {
          background: var(--orange-soft);
        }
        .ms-opt input {
          width: 15px;
          height: 15px;
          accent-color: var(--orange);
          cursor: pointer;
          flex: none;
        }
        .ms-opt.is-all {
          font-weight: 600;
          border-bottom: 1px solid var(--border);
          border-radius: 0;
          margin-bottom: 3px;
          padding-bottom: 9px;
        }
        .f-actions {
          margin-left: auto;
          display: flex;
          align-items: center;
          gap: 14px;
          padding-top: 18px;
        }
        .reset-btn {
          background: none;
          border: none;
          font-family: inherit;
          font-size: 13px;
          font-weight: 500;
          color: var(--orange-deep);
          cursor: pointer;
          text-decoration: underline;
          text-underline-offset: 3px;
        }
        .chips {
          max-width: 1400px;
          margin: 0 auto;
          padding: 0 32px 12px;
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
        }
        .chips:empty {
          padding-bottom: 0;
        }
        .chip {
          display: inline-flex;
          align-items: center;
          gap: 7px;
          font-size: 12.5px;
          font-weight: 500;
          background: var(--orange-soft);
          color: var(--orange-deep);
          border: 1px solid #ffd9cc;
          border-radius: 999px;
          padding: 4px 7px 4px 11px;
        }
        .chip button {
          background: none;
          border: none;
          color: var(--orange-deep);
          cursor: pointer;
          font-size: 14px;
          line-height: 1;
          padding: 1px 3px;
          border-radius: 50%;
        }
        .chip button:hover {
          background: #ffd9cc;
        }
        main {
          max-width: 1400px;
          margin: 0 auto;
          padding: 26px 32px 40px;
        }
        .kpis {
          display: grid;
          grid-template-columns: repeat(5, 1fr);
          gap: 16px;
          margin-bottom: 26px;
        }
        .kpi {
          background: var(--paper);
          border: 1px solid var(--border);
          border-radius: 12px;
          padding: 18px 20px 16px;
          position: relative;
          overflow: hidden;
          box-shadow: 0 1px 3px rgba(31, 41, 51, 0.05);
        }
        .kpi::before {
          content: "";
          position: absolute;
          left: 0;
          top: 0;
          bottom: 0;
          width: 4px;
          background: var(--orange);
        }
        .kpi .k-label {
          font-size: 12px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--muted);
        }
        .kpi .k-value {
          font-size: 30px;
          font-weight: 650;
          letter-spacing: -0.02em;
          margin-top: 6px;
          line-height: 1.1;
        }
        .kpi .k-value :global(.k-unit) {
          font-size: 14px;
          font-weight: 500;
          color: var(--ink-soft);
          margin-left: 3px;
        }
        .kpi .k-sub {
          margin-top: 8px;
          font-size: 12px;
          color: var(--muted);
        }
        .table-card {
          background: var(--paper);
          border: 1px solid var(--border);
          border-radius: 12px;
          box-shadow: 0 1px 3px rgba(31, 41, 51, 0.05);
          overflow: hidden;
        }
        .tc-toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 16px 24px;
          border-bottom: 1px solid var(--border);
        }
        .tc-toolbar h2 {
          font-size: 16px;
          font-weight: 600;
        }
        .tc-toolbar .tc-sub {
          font-size: 12.5px;
          color: var(--muted);
          margin-top: 2px;
        }
        table {
          width: 100%;
          border-collapse: collapse;
          font-size: 15px;
        }
        thead th {
          text-align: right;
          font-size: 12px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          color: var(--muted);
          padding: 13px 24px;
          border-bottom: 2px solid var(--border);
          background: var(--paper);
          white-space: nowrap;
        }
        thead th:first-child {
          text-align: left;
        }
        thead th :global(.th-mon) {
          display: block;
          color: var(--ink);
          font-size: 13px;
        }
        tbody td {
          padding: 0 24px;
          height: 42px;
          border-bottom: 1px solid #f0ede8;
          vertical-align: middle;
        }
        tbody td.val {
          text-align: right;
          white-space: nowrap;
        }
        tbody tr:nth-child(even):not(.hl):not(.pct-row) {
          background: #fbfaf8;
        }
        tbody tr:hover:not(.hl) {
          background: var(--orange-soft);
        }
        tbody tr:last-child td {
          border-bottom: none;
        }
        .p-cell {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .ind-1 .p-cell {
          padding-left: 20px;
        }
        .ind-2 .p-cell {
          padding-left: 40px;
          font-size: 13.5px;
        }
        .ind-2 td {
          height: 36px;
        }
        .r-italic td {
          font-style: italic;
          color: var(--blue-ratio);
          font-size: 13.5px;
        }
        .r-bold td {
          font-weight: 600;
        }
        .neg {
          color: var(--red);
        }
        .cr-unit {
          font-size: 11px;
          font-weight: 400;
          color: var(--muted);
          margin-left: 6px;
          white-space: nowrap;
        }
        tr.hl-dve .cr-unit,
        tr.hl-cm1 .cr-unit,
        tr.hl-cm2 .cr-unit {
          color: rgba(255, 255, 255, 0.7);
        }
        tr.hl-gm .cr-unit,
        tr.hl-anchor .cr-unit,
        tr.hl-revanchor .cr-unit {
          color: var(--ink-soft);
        }
        tr.pct-row td {
          height: 28px;
          border-bottom: 1px solid #f4f1ec;
          background: transparent;
        }
        tr.pct-row .p-cell {
          padding-left: 24px;
          font-style: italic;
          color: var(--blue-ratio);
          font-size: 12px;
        }
        tr.pct-row td.val {
          font-style: italic;
          color: var(--blue-ratio);
          font-size: 12px;
        }
        tr.pct-row:hover {
          background: var(--orange-soft);
        }
        tr.hl td {
          border-bottom: none;
        }
        tr.hl-revanchor {
          background: var(--orange-band);
        }
        tr.hl-revanchor td {
          font-weight: 600;
        }
        tr.hl-anchor {
          background: var(--orange-band);
          border-left: 4px solid var(--orange);
        }
        tr.hl-anchor td {
          font-weight: 650;
        }
        tr.hl-gm {
          background: var(--gm-band);
        }
        tr.hl-gm td {
          font-weight: 650;
        }
        tr.hl-dve {
          background: var(--dve-band);
        }
        tr.hl-dve td {
          color: #fff;
          font-weight: 600;
        }
        tr.hl-dve:hover {
          background: #1a1e24;
        }
        tr.hl-cm1 td,
        tr.hl-cm2 td {
          font-weight: 650;
          font-size: 16px;
          height: 50px;
          color: #fff;
        }
        tr.hl-cm1 {
          background: var(--orange-deep);
        }
        tr.hl-cm2 {
          background: var(--orange);
        }
        tr.hl-cm1 .neg,
        tr.hl-cm2 .neg,
        tr.hl-dve .neg {
          color: #ffd2c5;
        }
        .chev {
          flex: none;
          width: 18px;
          height: 18px;
          border: none;
          background: none;
          cursor: pointer;
          padding: 0;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          color: inherit;
          opacity: 0.65;
          transition: transform 0.15s;
        }
        .chev svg {
          display: block;
        }
        .collapsed-parent .chev {
          transform: rotate(-90deg);
        }
        .chev-spacer {
          width: 18px;
          flex: none;
        }
        tr.row-hidden {
          display: none;
        }
        footer {
          max-width: 1400px;
          margin: 0 auto;
          padding: 18px 32px 36px;
          font-size: 12px;
          color: var(--muted);
          text-align: center;
        }
        @media (max-width: 1100px) {
          .kpis {
            grid-template-columns: repeat(3, 1fr);
          }
        }
        @media (max-width: 760px) {
          .kpis {
            grid-template-columns: repeat(2, 1fr);
          }
          .hd-inner,
          .filters,
          main,
          footer,
          .chips {
            padding-left: 18px;
            padding-right: 18px;
          }
          thead th,
          tbody td {
            padding-left: 14px;
            padding-right: 14px;
          }
        }
      `}</style>
    </div>
  );
}
