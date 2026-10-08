"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { RESPONDER_EVAL_DEFAULT_PATH, RESPONDER_PROGRAMS } from "@/lib/responderNav";
import { userHasAnyRole } from "@/lib/userAccess";

function mayAccessResponderEvalDashboard(roles: string[] | undefined): boolean {
  return userHasAnyRole(roles, "system_admin", "responder_eval_reviewer");
}

const domainRowBase =
  "block rounded-lg px-2 py-1.5 text-left text-[12px] font-semibold leading-snug transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#722F37]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]";

const domainActive =
  "bg-[#722F37]/10 font-bold text-[var(--text-primary)] shadow-[inset_3px_0_0_0_#722F37]";
const flowLinkBase =
  "block rounded-md px-1.5 py-1 text-left text-[11px] font-medium leading-snug transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#722F37]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]";

const flowActive = "bg-[#722F37] font-bold text-white shadow-sm";
const flowIdle =
  "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]";

export default function ResponderEvalsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    if (!mayAccessResponderEvalDashboard(user?.roles)) {
      router.replace("/dashboard");
    }
  }, [loading, user, router]);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--text-muted)]">
        Loading…
      </div>
    );
  }

  if (!mayAccessResponderEvalDashboard(user?.roles)) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--text-muted)]">
        Redirecting…
      </div>
    );
  }

  return (
    <div
      className="flex min-h-0 w-full flex-1 flex-col gap-3 md:flex-row md:items-stretch md:gap-4"
      style={{ colorScheme: "light" }}
      data-color-scheme="light"
    >
      <aside
        className="w-full shrink-0 md:w-24 lg:w-28"
        aria-label="Responder navigation"
      >
        <nav className="flex flex-col gap-1.5">
          <div className="min-w-0">
            <Link
              href={RESPONDER_EVAL_DEFAULT_PATH}
              title="Responder"
              className={`${domainRowBase} ${domainActive}`}
              aria-current="true"
            >
              <span className="block truncate">Responder</span>
            </Link>

            <ul className="ml-0 mt-1 space-y-0.5 border-l border-[var(--border)] pl-2">
              {RESPONDER_PROGRAMS.map((program) => {
                const selected =
                  pathname === program.href || pathname.startsWith(`${program.href}/`);
                return (
                  <li key={program.href}>
                    <Link
                      href={program.href}
                      title={program.label}
                      className={`${flowLinkBase} ${selected ? flowActive : flowIdle}`}
                      aria-current={selected ? "page" : undefined}
                    >
                      {program.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        </nav>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
