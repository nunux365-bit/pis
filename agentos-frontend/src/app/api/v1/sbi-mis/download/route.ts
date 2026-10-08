/**
 * Buffered download proxy for SBI MIS xlsx export.
 *
 * The generic Next.js rewrite (`/api/:path* → backend`) times out on large
 * binary responses in dev mode (`next dev`) because `proxyTimeout` only
 * applies to `next start`. This route handler bypasses the rewrite middleware
 * entirely: it runs inside the Next.js Node.js process, fully buffers the
 * backend response into an ArrayBuffer, then sends it to the browser in one
 * shot. Streaming via NextResponse(body) was tried but body streams get
 * dropped mid-transfer in dev mode for large (11 MB+) responses.
 */

import { NextRequest, NextResponse } from "next/server";

const BACKEND = (
  process.env.BACKEND_URL ||
  process.env.AGENTOS_INTERNAL_API_URL ||
  "http://127.0.0.1:8000"
).replace(/\/+$/, "");

export async function GET(request: NextRequest) {
  const backendUrl = `${BACKEND}/api/v1/sbi-mis/download`;

  // Forward auth cookie so the backend can authenticate the request.
  const cookie = request.headers.get("cookie") ?? "";

  let backendRes: Response;
  try {
    backendRes = await fetch(backendUrl, {
      headers: { cookie },
      // 10-minute timeout — xlsx generation for large dumps takes ~1-2 min.
      signal: AbortSignal.timeout(600_000),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { detail: `Download proxy error: ${msg}` },
      { status: 503 },
    );
  }

  if (!backendRes.ok) {
    // Forward non-200 as-is so the frontend error handler can read the detail.
    const text = await backendRes.text().catch(() => "");
    return new NextResponse(text, {
      status: backendRes.status,
      headers: { "Content-Type": "application/json" },
    });
  }

  const disposition =
    backendRes.headers.get("content-disposition") ??
    'attachment; filename="SBI_MIS.xlsx"';

  // Forward Content-Length so nginx and the browser know the exact body size.
  // Without it nginx can't tell when the body ends and may close the connection
  // early, causing res.blob() in the browser to throw TypeError: Failed to fetch.
  // X-Accel-Buffering: no disables nginx proxy buffering for this response so
  // large files aren't written to a temp file on disk (avoids disk-full issues).
  const responseHeaders: Record<string, string> = {
    "Content-Type":
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "Content-Disposition": disposition,
    "X-Accel-Buffering": "no",
  };
  const contentLength = backendRes.headers.get("content-length");
  if (contentLength) responseHeaders["Content-Length"] = contentLength;

  return new NextResponse(backendRes.body, {
    status: 200,
    headers: responseHeaders,
  });
}
