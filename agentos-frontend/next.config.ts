import type { NextConfig } from "next";

/** Server-side only: FastAPI base URL for proxy rewrites (browser never sees this). */
const backend = (
  process.env.BACKEND_URL ||
  process.env.AGENTOS_INTERNAL_API_URL ||
  "http://127.0.0.1:8000"
).replace(/\/+$/, "");

/**
 * Rewrites use Node http-proxy. Override with NEXT_PROXY_TIMEOUT_MS (milliseconds).
 * Omit or unset for 30 minutes; minimum effective value is 30s.
 * Note: empty NEXT_PROXY_TIMEOUT_MS used to coerce to 0 and clamp to 30s — see parse below.
 */
function parseProxyTimeoutMs(): number {
  const fallback = 1_800_000;
  const raw = process.env.NEXT_PROXY_TIMEOUT_MS?.trim();
  if (!raw) return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.max(30_000, n);
}
const proxyTimeoutMs = parseProxyTimeoutMs();

/** When the browser calls a separate API origin, CSP must allow it in connect-src. */
function cspConnectSrc(): string {
  const parts: string[] = ["'self'", "https://cloudflareinsights.com"];
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (typeof raw === "string" && raw.trim() !== "") {
    try {
      const u = new URL(raw.trim());
      parts.push(`${u.protocol}//${u.host}`);
    } catch {
      // invalid URL — keep 'self' only
    }
  }
  return parts.join(" ");
}

const nextConfig: NextConfig = {
  // Dev-only: the demo box is reached through an ephemeral Cloudflare quick-tunnel
  // (*.trycloudflare.com). Next blocks cross-origin dev/HMR requests by default, which
  // hangs the client boot. Wildcard survives tunnel-URL changes on restart.
  allowedDevOrigins: ["216.48.190.56.sslip.io", "*.trycloudflare.com"],
  experimental: {
    proxyTimeout: proxyTimeoutMs,
  },
  async headers() {
    const isDev = process.env.NODE_ENV === "development";
    const scriptSrc = ["'self'", "'unsafe-inline'", "https://static.cloudflareinsights.com"];
    if (isDev) {
      scriptSrc.push("'unsafe-eval'");
    }

    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              // Cloudflare Web Analytics injects beacon from static.cloudflareinsights.com
              `script-src ${scriptSrc.join(" ")}`,
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data:",
              "font-src 'self'",
              `connect-src ${cspConnectSrc()}`,
              "frame-ancestors 'none'",
            ].join("; "),
          },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/health", destination: `${backend}/health` },
    ];
  },
};

export default nextConfig;
