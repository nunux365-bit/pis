"use client";

import { useState, Suspense, useEffect } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import Link from "next/link";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useAuth } from "@/contexts/AuthContext";
import { API_BASE } from "@/lib/api";

function LoginForm() {
  const { login, user, loading } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/chat";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [googleSsoEnabled, setGoogleSsoEnabled] = useState(false);

  useEffect(() => {
    const qErr = params.get("error");
    if (qErr) {
      const labels: Record<string, string> = {
        unknown_user:
          "This Google account is not in AgentOS yet and automatic sign-up is turned off. Ask an admin to add your email, or enable GOOGLE_SSO_AUTO_PROVISION on the server.",
        domain_not_allowed: "Your email domain is not allowed for Google sign-in.",
        email_not_verified: "Google reports this email as unverified.",
        rate_limited: "Too many sign-in attempts. Try again shortly.",
        user_inactive: "This account is disabled.",
        invalid_state: "Sign-in session expired. Try Google sign-in again.",
        sso_not_configured: "Google sign-in is not configured on the server.",
        missing_code: "Google did not return an authorization code. Close this tab and try signing in again.",
        token_exchange_failed: "Could not reach Google to finish sign-in. Check the network and try again.",
        token_exchange_denied: "Google rejected the sign-in request. Check client id, secret, and redirect URI in Google Cloud Console.",
        bad_token_response: "Unexpected response from Google. Try again or contact support.",
        no_id_token: "Google did not return an identity token. Try again.",
        invalid_id_token: "Google identity could not be verified. Try again.",
        no_email: "Google did not share an email for this account.",
        provision_failed: "Could not create your user profile. Try again or ask an admin.",
      };
      setError(labels[qErr] || `Sign-in failed (${qErr}).`);
    }
  }, [params]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/auth/google/enabled`);
        if (!res.ok) return;
        const data = (await res.json()) as { enabled?: boolean };
        if (!cancelled && data.enabled) setGoogleSsoEnabled(true);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!loading && user) router.replace(next.startsWith("/") ? next : "/chat");
  }, [loading, user, router, next]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      if (next.startsWith("/")) router.replace(next);
      else router.replace("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-8 shadow-xl">
      <div className="flex items-center gap-3 mb-8">
        <div className="w-10 h-10 rounded-lg bg-app-gradient-1 flex items-center justify-center font-bold text-sm text-white">
          1
        </div>
        <div>
          <h1 className="text-lg font-bold tracking-tight">AgentOS</h1>
          <p className="text-xs text-[var(--text-muted)]">TATA 1MG</p>
        </div>
      </div>
      <h2 className="text-xl font-semibold mb-1">Sign in</h2>
      <p className="text-sm text-[var(--text-secondary)] mb-6">
        {googleSsoEnabled
          ? "Use Google for the fastest path in. Email and password remain available for admin and legacy accounts."
          : "Use your workspace email and password. To enable Google sign-in, configure GOOGLE_SSO_CLIENT_ID on the API."}
      </p>
      <div className="flex flex-col gap-4">
        {error ? <ErrorBanner>{error}</ErrorBanner> : null}

        {googleSsoEnabled ? (
          <>
            <a
              href={`${API_BASE}/api/auth/google/start?next=${encodeURIComponent(next)}`}
              className="block text-center rounded-xl bg-app-gradient-1 py-3 text-sm font-semibold text-white shadow-sm transition hover:opacity-95"
            >
              Continue with Google
            </a>
            <div className="relative py-1 text-center text-[10px] font-medium uppercase tracking-wider text-[var(--text-muted)]">
              <span className="relative z-10 bg-[var(--bg-card)] px-2">or</span>
              <span className="absolute left-0 right-0 top-1/2 z-0 h-px bg-[var(--border)]" aria-hidden />
            </div>
          </>
        ) : null}

        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5">Email</label>
            <input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2.5 text-sm outline-none focus:border-[var(--border-active)]"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5">Password</label>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2.5 text-sm outline-none focus:border-[var(--border-active)]"
            />
          </div>
          <button
            type="submit"
            disabled={busy}
            className="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] py-3 text-sm font-semibold text-[var(--text-primary)] transition hover:border-[var(--border-active)] disabled:opacity-50"
          >
            {busy ? "Signing in…" : "Sign in with password"}
          </button>
        </form>
      </div>
      <p className="mt-6 text-center text-xs text-[var(--text-muted)]">
        <Link href="/" className="text-[var(--accent-blue)] hover:underline">
          Back to app
        </Link>
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="text-[var(--text-muted)]">Loading…</div>}>
      <LoginForm />
    </Suspense>
  );
}
