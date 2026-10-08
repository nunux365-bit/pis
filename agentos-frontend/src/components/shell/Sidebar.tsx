"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { navItemsForUser, type NavItem } from "@/lib/navConfig";
import { canAccessWorkflowQueue } from "@/lib/workflowQueueAccess";
import { isWorkflowQueueNavId } from "@/lib/workflowNav";
import { roleDisplayLabels } from "@/lib/roleLabels";
import { useAuth } from "@/contexts/AuthContext";
import { listPendingApprovals } from "@/lib/api";

const navLinkBase =
  "group flex min-w-0 items-center gap-2 rounded-lg px-1.5 py-2 text-[13px] font-medium leading-snug transition-colors duration-150 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-secondary)]";

const navLinkActive =
  "bg-[rgba(21,163,115,0.09)] font-semibold text-[var(--accent-green)] shadow-[inset_3px_0_0_0_var(--accent-green)]";

const navLinkIdle =
  "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]";

function itemIsActive(pathname: string, item: NavItem): boolean {
  if (item.id === "o2c") {
    return pathname === "/o2c" || pathname.startsWith("/o2c/");
  }
  if (item.id === "s2p") {
    return (
      pathname === "/s2p" ||
      pathname.startsWith("/s2p/") ||
      pathname === "/procurement" ||
      pathname.startsWith("/procurement/")
    );
  }
  if (item.id === "admin") {
    return pathname === item.href;
  }
  return (
    pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href + "/"))
  );
}

function ChevronLeftIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M15 18l-6-6 6-6" />
    </svg>
  );
}

function ChevronRightIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const navItems = navItemsForUser(user?.roles);
  const [pendingCount, setPendingCount] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem("sidebar-collapsed") === "true");
    } catch {}
  }, []);

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem("sidebar-collapsed", String(next));
    } catch {}
  };

  useEffect(() => {
    if (!canAccessWorkflowQueue(user?.roles)) {
      setPendingCount(null);
      return;
    }
    let cancelled = false;
    listPendingApprovals()
      .then((r) => {
        if (!cancelled) setPendingCount(r.count);
      })
      .catch(() => {
        if (!cancelled) setPendingCount(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname, user?.roles]);

  const initials = user?.full_name
    ? user.full_name
        .split(/\s+/)
        .map((w) => w[0])
        .join("")
        .slice(0, 2)
        .toUpperCase()
    : "•";

  const deptRoleLine = [user?.department, user?.roles?.length ? roleDisplayLabels(user.roles) : ""]
    .filter(Boolean)
    .join(" · ");

  return (
    <aside
      className={`flex shrink-0 flex-col border-r border-[var(--border)] bg-[var(--bg-secondary)] transition-[width] duration-200 ease-in-out ${
        collapsed ? "w-14" : "w-36 xl:w-40 2xl:w-44"
      }`}
    >
      {/* Logo */}
      <div className="flex items-center border-b border-[var(--border)] px-2 py-2.5">
        <div
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-app-gradient-1 text-sm font-bold text-white shadow-sm"
          aria-hidden
        >
          1
        </div>
        {!collapsed && (
          <div className="ml-2.5 min-w-0 flex-1">
            <div className="truncate text-sm font-semibold tracking-tight text-[var(--text-primary)]">
              AgentOS
            </div>
            <div className="truncate text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--text-muted)]">
              TATA 1MG
            </div>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav
        className="flex flex-1 flex-col gap-0.5 overflow-y-auto overflow-x-hidden px-1.5 py-2"
        aria-label="Primary"
      >
        {navItems.map((item) => {
          const isActive = itemIsActive(pathname, item);
          const badge =
            isWorkflowQueueNavId(item.id) && pendingCount != null && pendingCount > 0
              ? pendingCount
              : item.badge;

          return (
            <Link
              key={item.id}
              href={item.href}
              title={item.label}
              aria-current={isActive ? "page" : undefined}
              className={`${navLinkBase} ${isActive ? navLinkActive : navLinkIdle} ${collapsed ? "justify-center px-0" : ""}`}
            >
              <span className="relative flex h-5 w-5 shrink-0 items-center justify-center text-[15px] leading-none" aria-hidden>
                {item.icon}
                {collapsed && badge != null && (
                  <span className="absolute -right-1 -top-1 flex h-3.5 min-w-[0.875rem] items-center justify-center rounded-full bg-[var(--accent-red)] px-0.5 text-[8px] font-bold text-white leading-none">
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </span>
              {!collapsed && (
                <>
                  <span className="min-w-0 flex-1 whitespace-normal break-words">
                    {item.label}
                  </span>
                  {badge != null && (
                    <span
                      className="ml-auto flex h-5 min-w-[1.25rem] shrink-0 items-center justify-center rounded-full bg-[var(--accent-red)] px-1 text-[9px] font-bold tabular-nums text-white leading-none"
                      aria-label={
                        isWorkflowQueueNavId(item.id)
                          ? `${badge} items in my review queue`
                          : `${badge}`
                      }
                    >
                      {badge > 99 ? "99+" : badge}
                    </span>
                  )}
                </>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Collapse toggle */}
      <div className={`px-1.5 pb-1 ${collapsed ? "flex justify-center" : ""}`}>
        <button
          type="button"
          onClick={toggleCollapsed}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="flex h-7 w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] text-[11px] font-medium text-[var(--text-muted)] transition-colors hover:border-[var(--border-active)]/50 hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]"
        >
          {collapsed ? <ChevronRightIcon /> : (
            <>
              <ChevronLeftIcon />
              <span>Collapse</span>
            </>
          )}
        </button>
      </div>

      {/* User */}
      <div className="border-t border-[var(--border)] px-2.5 py-3">
        {collapsed ? (
          <div
            className="flex justify-center"
            title={user?.full_name ?? undefined}
          >
            <div
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-app-gradient-2 text-xs font-bold leading-none text-white ring-1 ring-black/[0.06]"
              aria-hidden
            >
              {initials}
            </div>
          </div>
        ) : (
          <>
            <div
              className="flex min-w-0 items-center gap-2.5 rounded-lg px-1 py-0.5"
              title={user?.full_name ?? undefined}
            >
              <div
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-app-gradient-2 text-xs font-bold leading-none text-white ring-1 ring-black/[0.06]"
                aria-hidden
              >
                {initials}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-[var(--text-primary)]">
                  {user?.full_name ?? "User"}
                </div>
                {deptRoleLine ? (
                  <div className="mt-0.5 truncate text-[10px] leading-snug text-[var(--text-muted)]">
                    {deptRoleLine}
                  </div>
                ) : null}
              </div>
            </div>
            <button
              type="button"
              onClick={() => void logout()}
              className="mt-2.5 w-full rounded-lg border border-[#FF6B35]/45 bg-[var(--tata-1mg-orange-soft)] px-2 py-2 text-left text-xs font-semibold text-[var(--tata-1mg-orange-deep)] shadow-sm transition-colors hover:border-[#FF6B35]/60 hover:bg-[var(--tata-1mg-orange-soft-hover)] hover:text-[#b8320a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF6B35]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-secondary)]"
            >
              Log out
            </button>
          </>
        )}
      </div>
    </aside>
  );
}
