"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { SbiMisTopNav } from "@/components/sbi-mis/SbiMisLeftNav";
import { userHasAnyRole } from "@/lib/userAccess";

export default function SbiMisLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    if (!user) return; // platform layout handles unauthenticated redirect
    if (!userHasAnyRole(user.roles, "pharma_mis_operator", "system_admin")) {
      router.replace("/o2c/pharma-mis");
    }
  }, [user, loading, router]);

  // Render nothing while auth loads or while redirect fires
  if (loading || !user || !userHasAnyRole(user.roles, "pharma_mis_operator", "system_admin")) {
    return null;
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <SbiMisTopNav />
      <main className="flex-1 min-h-0 overflow-auto bg-[var(--bg-primary)]">
        {children}
      </main>
    </div>
  );
}
