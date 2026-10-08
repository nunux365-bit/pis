"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { userHasAnyRole } from "@/lib/userAccess";

function mayAccessComplianceDashboard(roles: string[] | undefined): boolean {
  return userHasAnyRole(roles, "system_admin", "glp_compliance_reviewer");
}

export default function ComplianceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    if (!mayAccessComplianceDashboard(user?.roles)) {
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

  if (!mayAccessComplianceDashboard(user?.roles)) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-sm text-[var(--text-muted)]">
        Redirecting…
      </div>
    );
  }

  return <>{children}</>;
}
