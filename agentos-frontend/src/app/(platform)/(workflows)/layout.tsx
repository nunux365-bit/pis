"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { mayEnterWorkflowsLayout } from "@/lib/workflowQueueAccess";
import { getDomainForPath, workflowProgramVisible } from "@/lib/workflowNav";

const domainRowBase =
  "block rounded-lg px-2 py-1.5 text-left text-[12px] font-semibold leading-snug transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#722F37]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]";

const domainActive =
  "bg-[#722F37]/10 font-bold text-[var(--text-primary)] shadow-[inset_3px_0_0_0_#722F37]";
const flowLinkBase =
  "block rounded-md px-1.5 py-1 text-left text-[11px] font-medium leading-snug transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#722F37]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]";

const flowActive = "bg-[#722F37] font-bold text-white shadow-sm";
const flowIdle =
  "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]";

export default function WorkflowsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, loading } = useAuth();
  const activeDomain = getDomainForPath(pathname);
  /** Only the current area (O2C or S2P); the other is a separate primary nav link. */
  const programsForPage = activeDomain ? [activeDomain] : [];

  const mayUseWorkflowsShell = Boolean(user) && mayEnterWorkflowsLayout(pathname, user?.roles);

  useEffect(() => {
    if (loading || !user) return;
    if (!mayUseWorkflowsShell) {
      router.replace("/dashboard");
    }
  }, [loading, user, router, pathname, mayUseWorkflowsShell]);

  if (loading || !user) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-sm text-[var(--text-muted)]">
        Loading…
      </div>
    );
  }

  if (!mayUseWorkflowsShell) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-sm text-[var(--text-muted)]">
        Redirecting…
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-1 flex-col gap-3 md:flex-row md:items-stretch md:gap-4">
      <aside
        className="w-full shrink-0 md:w-28 lg:w-32 xl:w-36 2xl:w-40"
        aria-label={activeDomain ? `${activeDomain.label} programs` : "Workflow programs"}
      >
        <p className="mb-1.5 text-xs font-bold uppercase tracking-[0.06em] text-[#722F37]">
          Programs
        </p>
        <nav className="flex flex-col gap-1.5">
          {programsForPage.map((domain) => {
            const visibleWorkflows = domain.workflows.filter((w) => workflowProgramVisible(w, user?.roles));
            const firstHref = visibleWorkflows[0]?.href ?? domain.prefix;

            return (
              <div key={domain.id} className="min-w-0">
                <Link
                  href={firstHref}
                  title={domain.label}
                  className={`${domainRowBase} ${domainActive}`}
                  aria-current="true"
                >
                  <span className="block truncate">{domain.label}</span>
                  <span className="mt-0.5 block text-[10px] font-normal leading-snug text-[var(--text-muted)]">
                    {domain.shortLabel}
                  </span>
                </Link>

                <ul className="ml-0 mt-1 space-y-0.5 border-l border-[var(--border)] pl-2">
                  {visibleWorkflows.map((w) => {
                    const selected = pathname === w.href || pathname.startsWith(w.href + "/");
                    return (
                      <li key={w.href}>
                        <Link
                          href={w.href}
                          title={w.label}
                          className={`${flowLinkBase} ${selected ? flowActive : flowIdle}`}
                          aria-current={selected ? "page" : undefined}
                        >
                          {w.label}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </nav>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">{children}</div>
    </div>
  );
}
