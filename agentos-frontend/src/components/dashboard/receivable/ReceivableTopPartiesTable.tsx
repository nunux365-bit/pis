"use client";

import { useState } from "react";
import type { ReceivableDashboardSnapshot } from "@/lib/api";
import { HEADER_BG, HEADER_TEXT } from "./receivableDashboardConstants";
import { HorizontalTableScrollArea } from "./HorizontalTableScrollArea";
import { formatLakh, toFiniteMonths } from "./receivableDashboardUtils";

type TopRow = NonNullable<
  NonNullable<ReceivableDashboardSnapshot["payload"]["top_parties"]>["all"]
>[number];

async function downloadTopPartiesPdf(rows: TopRow[]): Promise<void> {
  const { default: jsPDF } = await import("jspdf");
  // jspdf-autotable augments jsPDF and exports a standalone function
  const { default: autoTable } = await import("jspdf-autotable");

  const doc = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4" });

  // ── Title ──────────────────────────────────────────────────────────────
  doc.setFontSize(13);
  doc.setFont("helvetica", "bold");
  doc.setTextColor(26, 54, 93); // matches #1a365d header colour
  doc.text("Top 100 Parties by Total Due", 14, 16);

  // ── Subtitle / generated date ──────────────────────────────────────────
  doc.setFontSize(8);
  doc.setFont("helvetica", "normal");
  doc.setTextColor(120);
  const generatedAt = new Date().toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
  doc.text(`Generated: ${generatedAt}`, 14, 22);

  // ── Table body ─────────────────────────────────────────────────────────
  const tableBody = rows.map((row, i) => {
    const wm = toFiniteMonths(row.wtd_age_months);
    return [
      String(i + 1),
      row.name || row.code || "—",
      row.business_unit ?? "—",
      formatLakh(row.net_due_lakh),
      wm != null ? wm.toFixed(1) : "—",
    ];
  });

  autoTable(doc, {
    startY: 27,
    head: [["S.No", "Party", "BU", "Total Due (L)", "Wtd Age (mo)"]],
    body: tableBody,
    theme: "striped",
    headStyles: {
      fillColor: [26, 54, 93],
      textColor: 255,
      fontSize: 8,
      fontStyle: "bold",
    },
    bodyStyles: { fontSize: 8, textColor: 50 },
    columnStyles: {
      0: { halign: "center", cellWidth: 13 },
      1: { cellWidth: "auto" },
      2: { cellWidth: 32 },
      3: { halign: "right", cellWidth: 30 },
      4: { halign: "right", cellWidth: 26 },
    },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    margin: { left: 14, right: 14 },
  });

  doc.save("top-100-parties-by-total-due.pdf");
}

// ── Small inline SVG icon (no icon library dependency) ──────────────────
function DownloadIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}

export function ReceivableTopPartiesTable(props: {
  rows: TopRow[] | null;
}) {
  const { rows } = props;
  const [downloading, setDownloading] = useState(false);

  if (!rows?.length) return null;

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await downloadTopPartiesPdf(rows);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <>
      <p
        className={`mb-0 rounded-t-md px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wide ${HEADER_BG} ${HEADER_TEXT}`}
      >
        <span className="flex items-center justify-between gap-2">
          <span>Top 100 parties by total due</span>
          <button
            type="button"
            onClick={handleDownload}
            disabled={downloading}
            title="Download top 100 as PDF"
            className="inline-flex items-center gap-1 rounded border border-white/40 bg-white/10 px-2 py-0.5 text-[9px] font-normal normal-case tracking-normal text-white transition-colors hover:bg-white/25 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {downloading ? (
              "Generating…"
            ) : (
              <>
                <DownloadIcon />
                PDF
              </>
            )}
          </button>
        </span>
        <span className="mt-0.5 block text-[9px] font-normal normal-case tracking-normal opacity-90">
          Ageing-bucket "due" for this BU; not the same as Summary overdue.
        </span>
      </p>
      <HorizontalTableScrollArea className="rounded-b-md border border-slate-200 border-t-0 max-h-[420px] overflow-y-auto">
        <table
          className="w-full min-w-[300px] text-left text-xs"
          aria-label="Top parties by total due"
        >
          <thead>
            <tr className={`sticky top-0 z-10 ${HEADER_BG} ${HEADER_TEXT} text-left`}>
              <th scope="col" className="w-8 p-2 pl-1.5 text-center text-[9px] font-semibold">
                S.No
              </th>
              <th scope="col" className="p-2.5 text-[10px] font-semibold">Party</th>
              <th scope="col" className="p-2.5 text-[10px] font-semibold">BU</th>
              <th
                scope="col"
                className="p-2.5 pr-2 text-right text-[10px] font-semibold"
              >
                <span className="block">Total due (L)</span>
                <span className="mt-0.5 block text-[9px] font-normal normal-case tracking-normal">
                </span>
              </th>
              <th scope="col" className="p-2.5 pr-2 text-right text-[10px] font-semibold">
                Wtd age (mo)
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr
                key={`${row.business_unit ?? ""}:${row.code}:${i}`}
                className={
                  i % 2 === 0
                    ? "border-t border-slate-200/60 bg-white"
                    : "border-t border-slate-200/60 bg-slate-50/90"
                }
              >
                <td className="p-2 pl-1.5 text-center text-[10px] text-slate-600">
                  {i + 1}
                </td>
                <td className="max-w-[14rem] p-2 text-[11px] text-slate-800" title={row.code}>
                  {row.name || row.code || "—"}
                </td>
                <td className="max-w-[8rem] p-2 text-[10px] text-slate-600">
                  {row.business_unit}
                </td>
                <td className="p-2 pr-2 text-right text-[11px] font-medium tabular-nums text-slate-900">
                  {formatLakh(row.net_due_lakh)}
                </td>
                <td className="p-2 pr-2 text-right text-[10px] tabular-nums text-slate-600">
                  {(() => {
                    const wm = toFiniteMonths(row.wtd_age_months);
                    return wm != null ? wm.toFixed(1) : "—";
                  })()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </HorizontalTableScrollArea>
    </>
  );
}
