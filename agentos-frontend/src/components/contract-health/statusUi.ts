export function statusLabel(status: string): string {
  const s = status.toLowerCase();
  if (s === "approved") return "Approved";
  if (s === "pending") return "Pending review";
  if (s === "draft") return "Draft";
  if (s === "expired") return "Expired";
  if (s === "rejected") return "Rejected";
  return status || "Unknown";
}

export function statusBadgeClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "approved") return "bg-emerald-100 text-emerald-800 border-emerald-200";
  if (s === "pending" || s === "draft") return "bg-amber-100 text-amber-800 border-amber-200";
  if (s === "expired") return "bg-slate-100 text-slate-600 border-slate-200";
  if (s === "rejected") return "bg-red-100 text-red-800 border-red-200";
  return "bg-slate-100 text-slate-700 border-slate-200";
}

export function formatDateRange(from: string | null, to: string | null): string {
  const f = from ?? "?";
  const t = to ?? "open-ended";
  return `${f} → ${t}`;
}
