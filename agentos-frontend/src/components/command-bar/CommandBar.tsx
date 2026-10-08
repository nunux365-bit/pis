"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { navItemsForUser } from "@/lib/navConfig";
import { commandSearch, type SearchHit } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

type Row =
  | { kind: "nav"; id: string; label: string; icon: string; href: string }
  | { kind: "hit"; id: string; label: string; href: string; sub: string };

type Props = {
  open: boolean;
  onClose: () => void;
};

function hitKey(h: SearchHit, i: number) {
  return `hit-${h.type}-${h.href}-${i}`;
}

export function CommandBar({ open, onClose }: Props) {
  const router = useRouter();
  const { user } = useAuth();
  const [query, setQuery] = useState("");
  const [remote, setRemote] = useState<SearchHit[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const nav = useMemo(() => navItemsForUser(user?.roles), [user?.roles]);

  const navRows = useMemo((): Row[] => {
    const q = query.trim().toLowerCase();
    if (!q) {
      return nav.map((n) => ({
        kind: "nav" as const,
        id: n.id,
        label: n.label,
        icon: n.icon,
        href: n.href,
      }));
    }
    return nav
      .filter(
        (n) =>
          n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)
      )
      .map((n) => ({
        kind: "nav" as const,
        id: n.id,
        label: n.label,
        icon: n.icon,
        href: n.href,
      }));
  }, [nav, query]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setRemote([]);
      return;
    }
    const q = query.trim();
    if (q.length < 2) {
      setRemote([]);
      return;
    }
    const t = setTimeout(() => {
      setRemoteLoading(true);
      commandSearch(q)
        .then((res) => setRemote(res.hits))
        .catch(() => setRemote([]))
        .finally(() => setRemoteLoading(false));
    }, 220);
    return () => clearTimeout(t);
  }, [open, query]);

  const hitRows: Row[] = useMemo(
    () =>
      remote.map((h, i) => ({
        kind: "hit" as const,
        id: hitKey(h, i),
        label: h.label,
        href: h.href,
        sub: h.type,
      })),
    [remote]
  );

  const rows = useMemo(() => {
    const seen = new Set<string>();
    const out: Row[] = [];
    for (const r of [...navRows, ...hitRows]) {
      const k = `${r.href}::${r.label}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(r);
    }
    return out.slice(0, 24);
  }, [navRows, hitRows]);

  const go = useCallback(
    (href: string) => {
      router.push(href);
      setQuery("");
      onClose();
    },
    [router, onClose]
  );

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-sm flex justify-center pt-[20vh] z-50"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="w-[540px] bg-[var(--bg-card)] rounded-2xl border border-[var(--border)] overflow-hidden max-h-[min(400px,70vh)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 px-4 py-3.5 border-b border-[var(--border)]">
          <svg
            width="18"
            height="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            viewBox="0 0 24 24"
            className="text-[var(--text-muted)] shrink-0"
          >
            <path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text"
            placeholder="Search nav, O2C, S2P, chat…"
            className="flex-1 bg-transparent border-none outline-none text-[15px] text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && rows[0]) {
                e.preventDefault();
                go(rows[0].href);
              }
            }}
          />
          <span className="text-[11px] text-[var(--text-muted)] bg-[var(--bg-secondary)] px-2 py-1 rounded shrink-0">
            ESC
          </span>
        </div>
        <div className="px-3 pt-2 text-[10px] text-[var(--text-muted)] uppercase tracking-wide">
          {remoteLoading ? "Searching…" : query.trim().length >= 2 ? "Results" : "Navigation"}
        </div>
        <div className="p-2 max-h-[300px] overflow-y-auto">
          {rows.map((item) => (
            <button
              key={item.id}
              type="button"
              className="flex items-center gap-2.5 w-full px-3.5 py-2.5 rounded-lg hover:bg-white/5 text-left text-sm transition-colors"
              onClick={() => go(item.href)}
            >
              <div className="w-7 h-7 rounded-md bg-[var(--bg-secondary)] flex items-center justify-center text-[var(--text-muted)] text-sm">
                {item.kind === "nav" ? item.icon : "→"}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[var(--text-primary)] truncate">
                  {item.label}
                </div>
                {item.kind === "hit" && (
                  <div className="text-[10px] text-[var(--text-muted)]">
                    {item.sub}
                  </div>
                )}
              </div>
              <span className="ml-auto text-[11px] text-[var(--text-muted)] shrink-0">
                Open
              </span>
            </button>
          ))}
          {rows.length === 0 && (
            <p className="px-3 py-6 text-sm text-[var(--text-muted)] text-center">
              No matches
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
