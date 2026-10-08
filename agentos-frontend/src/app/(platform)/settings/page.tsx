"use client";

import { useAuth } from "@/contexts/AuthContext";
import { API_BASE } from "@/lib/api";
import { roleDisplayLabels } from "@/lib/roleLabels";

export default function SettingsPage() {
  const { user, logout } = useAuth();

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Settings</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Session and platform preferences
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5">
          <h3 className="font-semibold mb-4">Signed in as</h3>
          <p className="text-sm font-medium">{user?.full_name}</p>
          <p className="text-xs text-[var(--text-muted)] mt-1">{user?.email}</p>
          <p className="text-xs text-[var(--text-muted)] mt-1">
            {user?.department} • {roleDisplayLabels(user?.roles)}
          </p>
          <p className="text-[10px] text-[var(--text-muted)] mt-2 font-mono break-all">
            API: {API_BASE || "same origin (proxied to backend via Next.js)"}
          </p>
          <button
            type="button"
            onClick={() => logout()}
            className="mt-4 px-4 py-2 rounded-lg border border-[var(--border)] text-sm font-medium text-[var(--accent-red)] hover:bg-white/5"
          >
            Sign out
          </button>
        </div>
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5">
          <h3 className="font-semibold mb-4">Tech stack</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-[var(--text-muted)]">Agent framework</span>
              <span>LangGraph</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[var(--text-muted)]">API</span>
              <span>FastAPI + PostgreSQL</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[var(--text-muted)]">Auth</span>
              <span>JWT + refresh rotation</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
