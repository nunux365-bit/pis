"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";

export default function PayrollLayout({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const pathname = usePathname();

  if (!user) return <>{children}</>;

  // Check if they are system admin or have multiple roles
  const isSystemAdmin = user.roles.some((r) => r.toLowerCase() === "payroll_admin");
  const hasMultipleRoles = user.roles.length > 1 || isSystemAdmin;

  if (!hasMultipleRoles) return <>{children}</>;

  const tabs = [
    { name: "⚙️ Admin Settings", path: "/payroll/admin", role: "payroll_admin" },
    { name: "📝 Maker Dashboard", path: "/payroll/maker", role: "maker" },
    { name: "🔍 HRBP Review", path: "/payroll/hrbp", role: "hrbp" },
    { name: "👔 HOD Approval", path: "/payroll/hod", role: "hod" },
    { name: "💳 Payroll Queue", path: "/payroll/ops", role: "payroll" },
  ];

  // Filter tabs: if user is payroll_admin, show all. Otherwise, only show tabs matching their roles.
  const visibleTabs = tabs.filter((t) => isSystemAdmin || user.roles.some((r) => r.toLowerCase() === t.role));

  return (
    <div className="flex flex-col space-y-4 w-full">
      {/* Switcher Tab Bar */}
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-3 shadow-sm flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-2">
          <span className="text-xs font-bold text-[var(--text-muted)] uppercase tracking-wider">
            Dashboard Workspace:
          </span>
        </div>
        <div className="flex flex-wrap gap-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg p-1">
          {visibleTabs.map((t) => {
            const isActive = pathname === t.path;
            return (
              <Link
                key={t.path}
                href={t.path}
                className={`px-3 py-1.5 rounded-md text-[11px] font-bold transition flex items-center gap-1.5 ${
                  isActive
                    ? "bg-white text-[var(--text-primary)] shadow-sm"
                    : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                }`}
              >
                {t.name}
              </Link>
            );
          })}
        </div>
      </div>
      
      {/* Page Content */}
      <div className="flex-1">
        {children}
      </div>
    </div>
  );
}
