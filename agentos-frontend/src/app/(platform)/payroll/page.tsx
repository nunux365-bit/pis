"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { userRoleSet } from "@/lib/userAccess";

export default function PayrollRedirectPage() {
  const { user } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!user) return;

    const roles = userRoleSet(user.roles);

    if (roles.has("payroll_admin")) {
      router.replace("/payroll/admin");
    } else if (roles.has("maker")) {
      router.replace("/payroll/maker");
    } else if (roles.has("hrbp")) {
      router.replace("/payroll/hrbp");
    } else if (roles.has("hod")) {
      router.replace("/payroll/hod");
    } else if (roles.has("payroll")) {
      router.replace("/payroll/ops");
    } else {
      router.replace("/");
    }
  }, [user, router]);

  return (
    <div className="flex items-center justify-center min-h-[50vh]">
      <div className="text-center">
        <div className="w-12 h-12 border-4 border-[var(--accent-green)] border-t-transparent rounded-full animate-spin mx-auto mb-4"></div>
        <p className="text-sm text-[var(--text-secondary)]">Redirecting to your payroll dashboard...</p>
      </div>
    </div>
  );
}
