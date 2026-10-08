"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { userHasRole, userIsAdmin } from "@/lib/userAccess";

function adminLayoutAllowed(roles: string[] | undefined, pathname: string): boolean {
  if (userIsAdmin(roles)) return true;
  if (
    userHasRole(roles, "glp_compliance_reviewer") &&
    pathname === "/admin/call-quality"
  ) {
    return true;
  }
  if (
    userHasRole(roles, "email_agent_access") &&
    (pathname === "/admin/email-agent" || pathname.startsWith("/admin/email-agent/"))
  ) {
    return true;
  }
  if (
    userHasRole(roles, "optimus_admin") &&
    (pathname === "/admin/optimus" || pathname.startsWith("/admin/optimus/") ||
     pathname === "/admin/prosight" || pathname.startsWith("/admin/prosight/"))
  ) {
    return true;
  }
  return false;
}

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (loading) return;
    if (!adminLayoutAllowed(user?.roles, pathname)) {
      if (userHasRole(user?.roles, "optimus_admin") && pathname === "/admin") {
        router.replace("/admin/optimus");
      } else {
        router.replace("/dashboard");
      }
    }
  }, [loading, user, router, pathname]);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--text-muted)]">
        Loading…
      </div>
    );
  }

  if (!adminLayoutAllowed(user?.roles, pathname)) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--text-muted)]">
        Redirecting…
      </div>
    );
  }

  return <>{children}</>;
}
