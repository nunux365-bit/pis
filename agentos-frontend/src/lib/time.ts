/** Parse ISO timestamps; naive values are treated as UTC (chat DB convention). */
export function parseAssumedUtcIso(iso: string): Date {
  const trimmed = iso.trim();
  if (!trimmed) return new Date(NaN);
  if (/[zZ]$|[+-]\d{2}:\d{2}$/.test(trimmed)) {
    return new Date(trimmed);
  }
  const normalized = trimmed.includes("T") ? `${trimmed}Z` : `${trimmed}T00:00:00Z`;
  return new Date(normalized);
}

export function formatLocalDateTime(iso: string): string {
  const d = parseAssumedUtcIso(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatRelativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const d = Math.floor(hr / 24);
  if (d < 7) return `${d} day(s) ago`;
  return new Date(iso).toLocaleDateString();
}
