// SBI MIS — main frontend
// No build step. Preact + htm via ESM CDN.

import { h, render } from "https://esm.sh/preact@10.22.0";
import { useState, useEffect, useCallback, useMemo } from "https://esm.sh/preact@10.22.0/hooks";
import htm from "https://esm.sh/htm@3.1.1";

const html = htm.bind(h);

/** When the app is mounted under a path (e.g. /sbi), index.html sets window.__SBI_PREFIX__. */
function sbiUrl(path) {
  const p = typeof path === "string" && path.startsWith("/") ? path : "/" + path;
  const root =
    typeof window !== "undefined" && window.__SBI_PREFIX__
      ? String(window.__SBI_PREFIX__).replace(/\/$/, "")
      : "";
  return root + p;
}

/** ISO-8601 or SQLite "YYYY-MM-DD HH:MM:SS" — interpret naive server times as UTC (Chrome/Firefox-consistent). */
function parseServerUtc(isoOrSql) {
  if (isoOrSql == null || isoOrSql === "") return null;
  let s = String(isoOrSql).trim();
  if (!s) return null;
  if (s.includes(" ") && !s.includes("T")) {
    s = s.replace(" ", "T");
  }
  if (!s.includes("T")) {
    s = `${s}T00:00:00`;
  }
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) {
    s = `${s}Z`;
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function formatServerTime(isoOrSql) {
  const d = parseServerUtc(isoOrSql);
  return d ? d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : "";
}

// ---------- API helpers ---------- //

// Common helper: parse a fetch Response as JSON, fall back to a useful error message
// when the body is HTML (typical for proxy error pages — 502 / 504 from ALB, nginx, etc).
async function _parseJsonOrThrow(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch (_) {
    const looksProxyTimeout = text.toLowerCase().includes("gateway") || text.toLowerCase().includes("timeout") || text.startsWith("<!DOCTYPE") || text.startsWith("<html");
    const hint = looksProxyTimeout
      ? "looks like a proxy error page (likely a gateway timeout). Retry, or have ops raise the ALB / nginx idle timeout."
      : "the server responded with non-JSON content.";
    throw new Error(`HTTP ${r.status}${r.statusText ? " " + r.statusText : ""} — ${hint}`);
  }
}

/** Avoid hung fetch (dead TCP / proxy) leaving the UI stuck on "Uploading…". */
function withTimeout(promise, ms, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(message || `Request timed out after ${Math.round(ms / 1000)}s`)), ms);
    }),
  ]);
}

/**
 * POST multipart with upload-bytes progress. `fetch` cannot report send progress, so large
 * dumps otherwise look "stuck" while `/api/status` stays idle (pipeline starts only after
 * the server receives the full body).
 */
function postFormWithUploadProgress(path, formData, { timeoutMs = 30 * 60 * 1000, onUploadProgress } = {}) {
  const url = sbiUrl(path);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    xhr.timeout = timeoutMs;
    xhr.responseType = "text";
    xhr.upload.onprogress = (ev) => {
      if (onUploadProgress && ev.lengthComputable && ev.total > 0) {
        onUploadProgress({ loaded: ev.loaded, total: ev.total, pct: ev.loaded / ev.total });
      }
    };
    xhr.onload = () => {
      const text = xhr.responseText || "";
      let data;
      try {
        data = JSON.parse(text);
      } catch (_) {
        const looksProxy =
          text.toLowerCase().includes("gateway") ||
          text.toLowerCase().includes("timeout") ||
          text.startsWith("<!DOCTYPE") ||
          text.startsWith("<html");
        const hint = looksProxy
          ? "looks like a proxy error page (likely a gateway timeout)."
          : "the server responded with non-JSON content.";
        reject(new Error(`HTTP ${xhr.status}${xhr.statusText ? " " + xhr.statusText : ""} — ${hint}`));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(data.error || data.detail || `HTTP ${xhr.status}`));
        return;
      }
      resolve(data);
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.ontimeout = () =>
      reject(
        new Error(
          `Upload timed out after ${Math.round(timeoutMs / 60000)} min — very large file, slow network, or proxy body limit.`,
        ),
      );
    xhr.send(formData);
  });
}

/** Per-chunk deadline: stay under 60s ALB/nginx-style caps; reduce SBI_UPLOAD_CHUNK_BYTES if timeouts persist. */
const CHUNK_PUT_TIMEOUT_MS = 58_000;

/** Use session + chunked PUTs so no single HTTP request carries the entire body (40MB+ under 60s caps). */
const CHUNKED_UPLOAD_THRESHOLD_BYTES = 5 * 1024 * 1024;

function putUploadChunkRaw(uploadId, chunkIndex, blob, timeoutMs) {
  const url = sbiUrl(`/api/upload/chunk/${encodeURIComponent(uploadId)}/${chunkIndex}`);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    xhr.timeout = timeoutMs;
    xhr.responseType = "text";
    xhr.onload = () => {
      const text = xhr.responseText || "";
      let data;
      try {
        data = JSON.parse(text);
      } catch (_) {
        const looksProxy =
          text.toLowerCase().includes("gateway") ||
          text.toLowerCase().includes("timeout") ||
          text.startsWith("<!DOCTYPE") ||
          text.startsWith("<html");
        reject(
          new Error(
            looksProxy
              ? `Chunk ${chunkIndex}: proxy/gateway error — try lowering SBI_UPLOAD_CHUNK_BYTES on the server.`
              : `Chunk ${chunkIndex}: invalid JSON (HTTP ${xhr.status})`,
          ),
        );
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(data.error || data.detail || `HTTP ${xhr.status}`));
        return;
      }
      resolve(data);
    };
    xhr.onerror = () => reject(new Error(`Network error during chunk ${chunkIndex}`));
    xhr.ontimeout = () =>
      reject(
        new Error(
          `Chunk ${chunkIndex} timed out (>${Math.round(timeoutMs / 1000)}s) — set SBI_UPLOAD_CHUNK_BYTES smaller in sbi-mis/.env (e.g. 1048576).`,
        ),
      );
    xhr.send(blob);
  });
}

/**
 * Large files: POST /api/upload/session, sequential PUT chunks, POST complete.
 * Each request stays short; total bytes are unchanged.
 */
async function uploadFileChunked({ file, month, kind, timeoutPerChunkMs, onProgress }) {
  const meta = await api.post("/api/upload/session", {
    month: month.trim(),
    kind,
    filename: file.name || "upload",
    file_size: file.size,
  });
  const uploadId = meta && meta.upload_id;
  const chunkSize = meta && meta.chunk_size;
  const chunkCount = meta && meta.chunk_count;
  if (!uploadId || typeof chunkSize !== "number" || chunkSize <= 0 || typeof chunkCount !== "number" || chunkCount < 1) {
    throw new Error("Invalid upload session response from server.");
  }
  let sent = 0;
  for (let i = 0; i < chunkCount; i++) {
    const start = i * chunkSize;
    const end = Math.min(start + chunkSize, file.size);
    const blob = file.slice(start, end);
    await putUploadChunkRaw(uploadId, i, blob, timeoutPerChunkMs);
    sent += blob.size;
    onProgress &&
      onProgress({
        loaded: sent,
        total: file.size,
        pct: file.size > 0 ? sent / file.size : 0,
        chunk: i + 1,
        chunkCount,
      });
  }
  return api.post("/api/upload/complete", { upload_id: uploadId });
}

const _fetchOpts = { credentials: "same-origin" };

const api = {
  async get(path) {
    const r = await fetch(sbiUrl(path), _fetchOpts);
    const data = await _parseJsonOrThrow(r);
    if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    return data;
  },
  async post(path, body) {
    const r = await fetch(sbiUrl(path), {
      ..._fetchOpts,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await _parseJsonOrThrow(r);
    if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    return data;
  },
  async put(path, body) {
    const r = await fetch(sbiUrl(path), {
      ..._fetchOpts,
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await _parseJsonOrThrow(r);
    if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    return data;
  },
  async delete(path) {
    const r = await fetch(sbiUrl(path), { ..._fetchOpts, method: "DELETE" });
    const data = await _parseJsonOrThrow(r);
    if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    return data;
  },
  async postForm(path, form) {
    const r = await fetch(sbiUrl(path), { ..._fetchOpts, method: "POST", body: form });
    const data = await _parseJsonOrThrow(r);
    if (!r.ok) {
      throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    }
    return data;
  },

  /** Poll /api/jobs/{id} until done|error, with sensible caps for hosted environments.
   *
   * - Total wall-time: opts.maxWallMs (default 15 min; use longer for heavy upload+pipeline).
   * - Tolerates up to opts.maxConsecErr transient failures (default 5).
   * - Job missing (404): fails immediately (e.g. server restart dropped in-memory jobs).
   * - onProgress(job) fires on every successful poll.
   */
  async pollJob(jobId, onProgress, opts = {}) {
    const id = jobId != null ? String(jobId).trim() : "";
    if (!id) throw new Error("Missing job_id from server — cannot track upload progress.");

    const POLL_INTERVAL_MS = opts.intervalMs ?? 1500;
    const MAX_WALL_MS      = opts.maxWallMs  ?? 15 * 60 * 1000;
    const MAX_CONSEC_ERR   = opts.maxConsecErr ?? 5;
    const t0 = Date.now();
    let consecErrors = 0;
    const isJobGone = (e) => {
      const m = String(e && e.message != null ? e.message : e).toLowerCase();
      return m.includes("job not found") || m.includes("404") || m.includes("not found");
    };
    const isTerminal = (s) => s === "done" || s === "error";

    while (true) {
      if (Date.now() - t0 > MAX_WALL_MS) {
        throw new Error(
          `Job ${id} did not finish within ${Math.round(MAX_WALL_MS / 60000)} min — pipeline may still be running on the server. Refresh status or try again.`,
        );
      }
      try {
        const j = await withTimeout(
          this.get(`/api/jobs/${encodeURIComponent(id)}`),
          90_000,
          "Job status request timed out (check network / proxy).",
        );
        consecErrors = 0;
        onProgress && onProgress(j);
        const st = j && j.status;
        if (isTerminal(st)) return j;
        // Unexpected but non-terminal — don't spin forever silently
        if (st !== "queued" && st !== "running") {
          throw new Error(`Unexpected job status "${st}" — refresh and retry.`);
        }
      } catch (e) {
        if (isJobGone(e)) {
          throw new Error(
            `Upload job ${id} no longer exists on the server (often after a restart). Re-upload the file.`,
          );
        }
        consecErrors++;
        if (consecErrors >= MAX_CONSEC_ERR) {
          throw new Error(
            `Lost contact with server while polling job ${id} (${consecErrors} consecutive errors). Last: ${e.message || e}`,
          );
        }
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  },
};

async function waitForRerun(res) {
  const jobId = res && res.rerun_job_id;
  if (!jobId) return null;
  const final = await api.pollJob(jobId, undefined, { maxWallMs: UPLOAD_PIPELINE_POLL_MS, maxConsecErr: 8 });
  if (final.status === "error") throw new Error(final.error || "Pipeline rerun failed");
  return final;
}

// ---------- Login ---------- //

function LoginPage({ onLoggedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e && e.preventDefault();
    setErr(null); setBusy(true);
    try {
      await api.post("/api/login", { username, password });
      onLoggedIn();
    } catch (e) {
      setErr(String(e).replace(/^Error:\s*/, ""));
    }
    setBusy(false);
  };

  return html`
    <div class="min-h-full flex items-center justify-center bg-slate-100">
      <form class="w-[360px] bg-white rounded-xl shadow-sm border border-slate-200 p-7" onSubmit=${submit}>
        <div class="mb-6">
          <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">SBI MIS</div>
          <div class="text-xl font-semibold text-slate-900 mt-1">Sign in</div>
        </div>
        <label class="block text-xs text-slate-600 mb-1">Username</label>
        <input class="w-full border border-slate-300 rounded-md px-3 py-2 text-sm mb-3 focus:outline-none focus:border-blue-500"
               value=${username} onInput=${(e) => setUsername(e.currentTarget.value)} autoFocus />
        <label class="block text-xs text-slate-600 mb-1">Password</label>
        <input type="password" class="w-full border border-slate-300 rounded-md px-3 py-2 text-sm mb-4 focus:outline-none focus:border-blue-500"
               value=${password} onInput=${(e) => setPassword(e.currentTarget.value)} />
        ${err ? html`<div class="mb-3 p-2 bg-red-50 border border-red-200 rounded text-red-700 text-xs">${err}</div>` : null}
        <button type="submit" class=${"w-full py-2 rounded-md text-white text-sm font-medium " + (busy ? "bg-slate-400" : "bg-slate-900 hover:bg-slate-700")}
                disabled=${busy}>
          ${busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  `;
}

// ---------- Utilities ---------- //

function chipFor(ruleType) {
  if (!ruleType) return null;
  if (ruleType === "blank") return { label: "TBD", cls: "chip-amber" };
  if (ruleType === "raw")   return { label: "raw", cls: "chip-gray" };
  if (ruleType === "direct") return { label: "direct", cls: "chip-gray" };
  if (ruleType === "formula") return { label: "formula", cls: "chip-blue" };
  if (ruleType === "filter") return { label: "filter", cls: "chip-blue" };
  if (ruleType === "pivot_key") return { label: "pivot key", cls: "chip-blue" };
  if (ruleType === "pivot_first") return { label: "pivot.first", cls: "chip-blue" };
  if (ruleType === "pivot_agg") return { label: "pivot.agg", cls: "chip-blue" };
  if (ruleType === "lookup")  return { label: "lookup", cls: "chip-blue" };
  if (ruleType === "static")  return { label: "static", cls: "chip-gray" };
  return { label: ruleType, cls: "chip-gray" };
}

// ---- Excel number-format renderer (subset) ---- //
//
// We handle the common cases the KAM actually sets from the UI presets:
//   - percent ("0%", "0.00%")
//   - integer ("0")
//   - decimal ("0.00")
//   - grouped ("#,##0", "#,##0.00")
//   - currency ("₹"#,##0, "₹"#,##0.00, _-₹* #,##0_-)
//   - date patterns (dd/mm/yy, dd/mm/yyyy, ddmmyy, dd-mmm-yyyy, mm/dd/yyyy, etc.)
//   - datetime (dd/mm/yyyy hh:mm, mm/dd/yyyy hh:mm)
//   - text ("@")
// Anything else falls through to toString().

function _parseServerDateLike(v) {
  if (v == null) return null;
  if (v instanceof Date) return v;
  if (typeof v === "number") {
    // Could be an Excel serial (unlikely from server but handle it)
    if (v > 20000 && v < 80000) {
      return new Date(Date.UTC(1899, 11, 30) + v * 86400000);
    }
    return null;
  }
  if (typeof v !== "string") return null;
  // ISO formats from our JSON cleaner
  const d = new Date(v);
  if (!isNaN(d.getTime())) return d;
  return null;
}

function _renderDate(d, nf) {
  const pad = (n, w) => String(n).padStart(w, "0");
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const dd = pad(d.getDate(), 2);
  const mm = pad(d.getMonth() + 1, 2);
  const yy = pad(d.getFullYear() % 100, 2);
  const yyyy = String(d.getFullYear());
  const hh = pad(d.getHours(), 2);
  const mi = pad(d.getMinutes(), 2);
  const ss = pad(d.getSeconds(), 2);
  const mmm = months[d.getMonth()];
  const mmmm = MONTHS[d.getMonth()];
  // Longest-first so we don't clobber substrings
  return nf
    .replace(/yyyy/gi, yyyy)
    .replace(/yy/gi, yy)
    .replace(/mmmm/g, mmmm)
    .replace(/mmm/g, mmm)
    .replace(/mm/g, mm)
    .replace(/dd/g, dd)
    .replace(/hh/gi, hh)
    .replace(/ss/gi, ss)
    // "minutes" — Excel uses mm for both month and minute depending on position;
    // here we already replaced mm (month) — for minutes in time portion, swap "mi" we won't hit.
    .replace(/\bmi\b/g, mi);
}

function _isDateFormat(nf) {
  if (!nf) return false;
  const n = nf.toLowerCase();
  // Heuristic: contains dd/mm/yy/hh and no # or 0 digit markers
  return /[dmyhs]/.test(n) && !/[#0](\.|$|[,#0%])/.test(nf) && !/[#0]%/.test(nf);
}

function fmt(val, numberFormat) {
  if (val === null || val === undefined || val === "") return "";
  const nf = numberFormat || "";

  // Text format — always stringify without transformation
  if (nf === "@") return String(val);

  // Date / datetime
  if (nf && _isDateFormat(nf)) {
    const d = _parseServerDateLike(val);
    if (d) return _renderDate(d, nf);
    return String(val);
  }

  // Numbers
  if (typeof val === "number" || (!isNaN(Number(val)) && typeof val === "string" && val.trim() !== "")) {
    const n = Number(val);
    if (!isFinite(n)) return String(val);
    // Percent
    if (nf.includes("%")) {
      const decMatch = nf.match(/0\.(0+)%/);
      const dec = decMatch ? decMatch[1].length : 0;
      return (n * 100).toFixed(dec) + "%";
    }
    // Decimal count
    const dotMatch = nf.match(/\.(0+)/);
    const dec = dotMatch ? dotMatch[1].length : 0;
    // Currency symbols
    let prefix = "";
    if (nf.includes("₹")) prefix = "₹";
    else if (nf.includes("$")) prefix = "$";
    // Grouped?
    const grouped = nf.includes("#,##0") || nf.includes("#,#");
    const opts = grouped
      ? { minimumFractionDigits: dec, maximumFractionDigits: dec }
      : { minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: false };
    if (typeof val === "number" && Number.isInteger(n) && dec === 0 && !grouped && !prefix) {
      return String(n);
    }
    return prefix + n.toLocaleString("en-IN", opts);
  }

  if (val instanceof Object) return JSON.stringify(val);
  return String(val);
}

// ---------- Upload page ---------- //

const UPLOAD_MAX_BYTES = 120 * 1024 * 1024; // soft client cap — align with reverse-proxy / body limits
const UPLOAD_POST_TIMEOUT_MS = 30 * 60 * 1000;
const UPLOAD_PIPELINE_POLL_MS = 55 * 60 * 1000;

const UPLOAD_SLOTS = [
  { kind: "raw",        title: "Raw data dump",  subtitle: "Metabase export / query result (.xlsx or .csv). Required — drives the pipeline.", required: true },
  { kind: "pf_summary", title: "PF summary",     subtitle: "Prior-month PF summary xlsx. Populates Jan/Feb historicals." },
  { kind: "ahc",        title: "AHC",            subtitle: "AHC data file (.xlsx or .csv)." },
];

function _isMonthYYYYMM(s) {
  return typeof s === "string" && /^\d{4}-\d{2}$/.test(s.trim());
}

function UploadSlot({ slot, month, existing, onUploaded }) {
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(null);  // { stage, progress, elapsed }
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);

  const onDrop = (e) => {
    e.preventDefault(); setDragOver(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  };

  const submit = async () => {
    if (!file) return;
    if (!_isMonthYYYYMM(month)) {
      setErr("Pick a valid month (YYYY-MM).");
      return;
    }
    if (!file.size || file.size <= 0) {
      setErr("File is empty.");
      return;
    }
    if (file.size > UPLOAD_MAX_BYTES) {
      setErr(
        `File is larger than ${(UPLOAD_MAX_BYTES / 1024 / 1024).toFixed(0)} MB — split or export a smaller extract, or raise the limit in code after fixing your proxy.`,
      );
      return;
    }
    if (existing && !confirm(`Replace the existing ${slot.title.toLowerCase()} for ${formatMonth(month)}?\n\nCurrent: ${existing.original_filename || existing.file_path?.split("/").pop()}\nNew:     ${file.name}`)) {
      return;
    }
    setUploading(true); setErr(null); setResult(null);
    setProgress({
      phase: "send",
      stage: "Sending file to server",
      pct: 0,
      loaded: 0,
      total: file.size || 0,
    });

    try {
      let res;
      try {
        if (file.size >= CHUNKED_UPLOAD_THRESHOLD_BYTES) {
          res = await uploadFileChunked({
            file,
            month: month.trim(),
            kind: slot.kind,
            timeoutPerChunkMs: CHUNK_PUT_TIMEOUT_MS,
            onProgress: ({ loaded, total, pct, chunk, chunkCount }) => {
              setProgress({
                phase: "send",
                stage: `Uploading part ${chunk} / ${chunkCount}`,
                loaded,
                total,
                pct: pct ?? (total > 0 ? loaded / total : 0),
              });
            },
          });
        } else {
          const fd = new FormData();
          fd.append("file", file);
          fd.append("month", month.trim());
          fd.append("kind", slot.kind);
          res = await postFormWithUploadProgress("/api/upload", fd, {
            timeoutMs: UPLOAD_POST_TIMEOUT_MS,
            onUploadProgress: ({ loaded, total, pct }) => {
              setProgress({
                phase: "send",
                stage: "Sending file to server",
                loaded,
                total,
                pct: pct ?? (total > 0 ? loaded / total : 0),
              });
            },
          });
        }
      } catch (e) {
        setErr(String(e).replace(/^Error:\s*/, ""));
        return;
      }
      if (res.error) {
        setErr(String(res.error));
        return;
      }
      if (!res.job_id) {
        setResult(res);
        setFile(null);
        onUploaded && onUploaded(slot.kind, res);
        return;
      }

      setProgress({ phase: "job", stage: "queued", progress: 0.1, elapsed: null });
      const finalJob = await api.pollJob(
        res.job_id,
        (j) => {
          setProgress({
            phase: "job",
            stage: j.stage || j.status,
            progress: j.progress,
            elapsed: j.elapsed_seconds,
          });
        },
        { maxWallMs: UPLOAD_PIPELINE_POLL_MS, maxConsecErr: 8 },
      );
      if (finalJob.status === "error") {
        setErr(finalJob.error || "Job failed");
        return;
      }
      const finalResult = finalJob.result || {};
      setResult(finalResult);
      setFile(null);
      onUploaded && onUploaded(slot.kind, finalResult);
    } catch (e) {
      setErr(String(e).replace(/^Error:\s*/, ""));
    } finally {
      setUploading(false);
      setProgress(null);
    }
  };

  const hasExisting = !!existing;
  const dropClass = "border-2 border-dashed rounded-lg p-5 text-center transition-colors " +
    (dragOver ? "border-blue-400 bg-blue-50"
              : hasExisting ? "border-slate-300 bg-white" : "border-slate-200 bg-slate-50");
  const fmtBytes = (b) => !b ? "" : `${(b / 1024 / 1024).toFixed(1)} MB`;

  return html`
    <div class=${"rounded-xl border p-5 " + (hasExisting ? "bg-emerald-50/40 border-emerald-200" : "bg-white border-slate-200")}>
      <div class="flex items-start justify-between mb-3">
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <div class="font-medium text-slate-900">${slot.title}</div>
            ${slot.required ? html`<span class="chip chip-amber">required</span>` : html`<span class="chip chip-gray">optional</span>`}
            ${hasExisting ? html`<span class="chip chip-green">✓ uploaded</span>` : null}
          </div>
          <div class="text-xs text-slate-500 mt-0.5">${slot.subtitle}</div>
        </div>
      </div>

      ${hasExisting ? html`
        <div class="mb-3 p-3 bg-white border border-emerald-200 rounded-md">
          <div class="flex items-start justify-between gap-3">
            <div class="flex-1 min-w-0">
              <div class="text-[10px] uppercase tracking-wider text-emerald-700 font-semibold">Current file</div>
              <div class="text-sm font-medium text-slate-900 mt-0.5 truncate" title=${existing.original_filename || existing.file_path}>
                ${existing.original_filename || existing.file_path?.split("/").pop()}
              </div>
              <div class="text-xs text-slate-500 mt-0.5">
                ${existing.row_count != null ? `${existing.row_count.toLocaleString()} rows` : ""}
                ${existing.row_count != null && existing.uploaded_at ? " · " : ""}
                ${existing.uploaded_at ? `uploaded ${formatServerTime(existing.uploaded_at)}` : ""}
              </div>
            </div>
          </div>
        </div>
      ` : null}

      <div class=${dropClass}
           onDragOver=${(e) => { e.preventDefault(); setDragOver(true); }}
           onDragLeave=${() => setDragOver(false)}
           onDrop=${onDrop}>
        ${file ? html`
          <div class="font-medium text-slate-800 text-sm">${file.name}</div>
          <div class="text-slate-500 text-xs mt-0.5">${fmtBytes(file.size)}</div>
          <button class="mt-2 text-xs text-slate-500 underline" onClick=${() => setFile(null)}>Change</button>
        ` : html`
          <div class="text-slate-500 text-xs mb-2">
            ${hasExisting ? "Drop a new file here to replace, or" : "Drop xlsx here or"}
          </div>
          <label class="cursor-pointer text-xs text-blue-600 hover:text-blue-700 font-medium">
            <input type="file" class="hidden"
                   accept=${slot.kind === "pf_summary" ? ".xlsx,.xlsm" : ".xlsx,.xlsm,.csv,.tsv"}
                   onChange=${(e) => setFile(e.currentTarget.files[0])} />
            browse
          </label>
        `}
      </div>

      ${progress ? html`
        <div class="mt-3 p-3 bg-violet-50 border border-violet-200 rounded">
          <div class="flex items-center justify-between mb-1">
            <div class="text-xs font-medium text-violet-900">
              ${progress.phase === "send"
                ? `${progress.stage} (${progress.total
                    ? `${(100 * (progress.pct || 0)).toFixed(0)}% · ${(progress.loaded / 1024 / 1024).toFixed(1)} / ${(progress.total / 1024 / 1024).toFixed(1)} MB`
                    : "starting…"})`
                : `${progress.stage}…`}
            </div>
            <div class="text-[11px] text-violet-700 tabular-nums">
              ${progress.phase === "job" && progress.elapsed != null ? `${progress.elapsed.toFixed(1)}s` : ""}
            </div>
          </div>
          ${progress.phase === "send"
            ? html`<div class="text-[10px] text-violet-800/90 mt-1 leading-snug">
                Single-request phase: dashboard status stays idle until the server receives the full file. Large files use many short requests instead (see server <span class="mono">SBI_UPLOAD_CHUNK_BYTES</span>).
              </div>`
            : null}
          <div class="h-1.5 bg-violet-100 rounded-full overflow-hidden mt-2">
            <div class="h-full bg-violet-500 transition-all" style=${`width: ${Math.min(
              95,
              progress.phase === "send"
                ? 100 * (progress.pct || 0)
                : (progress.progress || 0) * 100,
            )}%`}></div>
          </div>
        </div>
      ` : null}

      ${err ? html`<div class="mt-3 p-2 bg-red-50 border border-red-200 rounded text-red-700 text-xs">${err}</div>` : null}
      ${result ? html`
        <div class="mt-3 p-2 bg-emerald-50 border border-emerald-200 rounded text-emerald-800 text-xs">
          ${result.kind === "raw" ? `Loaded ${(result.row_count||0).toLocaleString()} rows${existing ? " (replaced previous upload)" : ""}` :
            result.kind === "pf_summary" ? `Imported ${result.records_imported||0} historical records (months: ${(result.months_detected||[]).join(", ") || "—"})` :
            result.note || `Uploaded`}
        </div>
      ` : null}

      <div class="mt-3 flex justify-end">
        <button class=${"px-3 py-1.5 text-xs font-medium rounded-md " +
                         (file && !uploading
                           ? (hasExisting ? "bg-amber-600 text-white hover:bg-amber-700" : "bg-slate-900 text-white hover:bg-slate-700")
                           : "bg-slate-100 text-slate-400 cursor-not-allowed")}
                onClick=${submit} disabled=${!file || uploading}>
          ${uploading ? "Uploading…" : (hasExisting ? "Replace" : "Upload")}
        </button>
      </div>
    </div>
  `;
}

function UploadPage({ onUploaded, onCancel, defaultMonth }) {
  const [month, setMonth] = useState(defaultMonth || (() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  })());
  const [files, setFiles] = useState({});  // {kind: record}
  const [loadingExisting, setLoadingExisting] = useState(false);

  // Reload existing files when month changes
  useEffect(() => {
    if (!_isMonthYYYYMM(month)) {
      setLoadingExisting(false);
      setFiles({});
      return;
    }
    setLoadingExisting(true);
    withTimeout(
      api.get(`/api/run-files?month=${encodeURIComponent(month.trim())}`),
      45_000,
      "Timeout loading existing uploads for this month.",
    )
      .then((rows) => {
        const m = {};
        for (const r of rows) m[r.kind] = r;
        setFiles(m);
        setLoadingExisting(false);
      })
      .catch(() => setLoadingExisting(false));
  }, [month]);

  const handleUploaded = (kind, res) => {
    // Re-fetch to get fresh uploaded_at timestamp from server
    api.get(`/api/run-files?month=${encodeURIComponent(month.trim())}`)
      .then(rows => {
        const m = {};
        for (const r of rows) m[r.kind] = r;
        setFiles(m);
      })
      .catch(() => {
        // optimistic fallback
        setFiles(prev => ({ ...prev, [kind]: { kind, month, original_filename: res.filename, row_count: res.row_count, uploaded_at: new Date().toISOString() } }));
      });
    if (kind === "raw" && onUploaded) onUploaded(res);
  };

  const hasRaw = !!files.raw;

  return html`
    <div class="min-h-full bg-slate-100 p-8">
      <div class="max-w-3xl mx-auto">
        <div class="flex items-start justify-between mb-6">
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Monthly input</div>
            <h1 class="text-xl font-semibold text-slate-900 mt-1">Upload files for ${formatMonth(month)}</h1>
            <p class="text-slate-500 text-sm mt-1">Upload any of the three slots. Raw dump is required before the pipeline can run. You can come back later and add the others.</p>
          </div>
          <div class="flex items-center gap-3">
            ${hasRaw && onUploaded ? html`
              <button class="text-sm px-3 py-1.5 rounded-md bg-slate-900 text-white hover:bg-slate-700"
                      onClick=${() => onUploaded({})}>Continue →</button>
            ` : null}
            ${onCancel ? html`
              <button class="text-sm px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700"
                      onClick=${onCancel}>Cancel</button>
            ` : null}
          </div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-4 mb-5">
          <label class="text-xs font-medium text-slate-600 mr-3">Month</label>
          <input class="border border-slate-300 rounded-md px-3 py-1.5 w-44 text-sm focus:outline-none focus:border-blue-500"
                 type="month" value=${month} onInput=${(e) => setMonth(e.currentTarget.value)} />
          ${loadingExisting ? html`<span class="ml-3 text-xs text-slate-400">loading existing uploads…</span>` : null}
        </div>

        <div class="space-y-3">
          ${UPLOAD_SLOTS.map(slot => html`
            <${UploadSlot} key=${slot.kind} slot=${slot} month=${month}
              existing=${files[slot.kind]}
              onUploaded=${handleUploaded} />
          `)}
        </div>
      </div>
    </div>
  `;
}

// ---------- Preview page ---------- //

function PreviewPage({ status, schema, reloadToken, onOpenRule }) {
  const sheetOrder = schema.sheet_order;
  const firstOutputSheet = sheetOrder.find(s => s !== "Dump") || sheetOrder[0] || "Dump";
  const [activeSheet, setActiveSheet] = useState(firstOutputSheet);
  const [previewData, setPreviewData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const PREVIEW_LIMIT = 100;

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const data = await api.get(`/api/preview/${encodeURIComponent(activeSheet)}?offset=0&limit=${PREVIEW_LIMIT}`);
      setPreviewData(data);
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [activeSheet, reloadToken]);

  useEffect(() => { load(); }, [load]);

  return html`
    <div class="flex-1 flex flex-col min-h-0 min-w-0 relative">
      ${err ? html`<div class="p-3 bg-red-50 text-red-800 border-b border-red-200">${err}</div>` : null}
      ${loading && !previewData ? html`<div class="p-6 text-slate-500">Loading…</div>` : null}

      ${previewData && previewData.kind === "summary" ? html`
        <${SummaryView} cells=${previewData.cells}
          onEdit=${(cell) => onOpenRule({ sheet: "Summary", col: cell.cell, header: cell.cell })} />
      ` : null}

      ${previewData && previewData.kind === "grid" ? html`
        <${GridView}
          sheet=${activeSheet}
          data=${previewData}
          schema=${schema}
          onHeaderClick=${(col) => onOpenRule({ sheet: activeSheet, col: col.col, header: col.header })}
          onFilterClick=${activeSheet === "Order level " || activeSheet === "Order level - non permissible"
                          ? () => onOpenRule({ sheet: activeSheet, col: "__filter__", header: "Row filter" })
                          : null}
        />
      ` : null}

      <${SheetTabs} sheets=${sheetOrder} active=${activeSheet} onSelect=${setActiveSheet} />
    </div>
  `;
}

function TopBar({ status, onReupload, onLogout, onActivateMonth }) {
  const [busy, setBusy] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [dlProgress, setDlProgress] = useState(null);   // { stage, progress, elapsed }
  const [dlErr, setDlErr] = useState(null);
  const months = status.available_months || [];
  const active = status.active_month;
  const activeRun = status.active_run;

  const download = async () => {
    setBusy(true); setDlErr(null); setDlProgress({ stage: "queued", progress: 0.05 });
    try {
      const res = await api.post("/api/downloads", {});
      if (!res.job_id) throw new Error("no job_id from server");
      const final = await api.pollJob(
        res.job_id,
        (j) => {
          setDlProgress({ stage: j.stage || j.status, progress: j.progress, elapsed: j.elapsed_seconds });
        },
        { maxWallMs: UPLOAD_PIPELINE_POLL_MS, maxConsecErr: 8 },
      );
      if (final.status === "error") { setDlErr(final.error || "Download generation failed"); return; }
      const url = (final.result || {}).download_url || "/api/download";
      // Trigger the browser download — file is now cached server-side, instant fetch
      window.location.href = sbiUrl(url);
    } catch (e) {
      setDlErr(String(e));
    } finally {
      setBusy(false);
      // Keep the progress visible for 2s after completion so the user sees "ready"
      setTimeout(() => setDlProgress(null), 2000);
    }
  };

  const switchMonth = async (m) => {
    if (!m || m === active) return;
    setSwitching(true);
    try { await onActivateMonth(m); } finally { setSwitching(false); }
  };

  return html`
    <div class="h-14 border-b border-slate-200 bg-white flex items-center px-5 gap-4 flex-shrink-0">
      <div class="flex items-baseline gap-2">
        <div class="text-sm font-semibold tracking-tight text-slate-900">SBI MIS</div>
      </div>

      ${months.length ? html`
        <div class="flex items-center gap-2">
          <label class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Month</label>
          <select class="text-xs border border-slate-300 rounded-md px-2 py-1 bg-white focus:outline-none focus:border-blue-500"
                  value=${active || ""} onChange=${(e) => switchMonth(e.currentTarget.value)}
                  disabled=${switching || status.computing}>
            ${months.map(m => html`<option key=${m} value=${m}>${formatMonth(m)}</option>`)}
          </select>
          ${activeRun ? html`<span class="text-xs text-slate-500">${activeRun.row_count?.toLocaleString() ?? 0} rows</span>` : null}
        </div>
      ` : null}

      ${status.computing || switching ? html`<div class="text-xs text-amber-600 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"></span>${switching ? "switching…" : "recomputing"}</div>` : null}
      ${dlProgress ? html`
        <div class="flex items-center gap-2 text-xs text-violet-700">
          <span class="w-1.5 h-1.5 rounded-full bg-violet-500 animate-pulse"></span>
          <span>generating · ${dlProgress.stage}</span>
          <div class="w-24 h-1.5 bg-violet-100 rounded-full overflow-hidden">
            <div class="h-full bg-violet-500 transition-all" style=${`width: ${Math.min(95, (dlProgress.progress || 0) * 100)}%`}></div>
          </div>
          ${dlProgress.elapsed != null ? html`<span class="tabular-nums text-[11px] text-violet-500">${dlProgress.elapsed.toFixed(1)}s</span>` : null}
        </div>
      ` : null}
      ${dlErr ? html`<div class="text-xs text-red-600 mono truncate max-w-md" title=${dlErr}>${dlErr}</div>` : null}
      ${status.error ? html`<div class="text-xs text-red-600 mono truncate max-w-md" title=${status.error}>${status.error}</div>` : null}
      <div class="flex-1"></div>
      <button class="text-xs px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700" onClick=${onReupload}>
        Upload Input
      </button>
      <button class=${"px-3 py-1.5 rounded-md text-white text-xs font-medium " + (busy ? "bg-slate-400" : "bg-slate-900 hover:bg-slate-700")}
              disabled=${busy} onClick=${download}>
        ${busy ? "Generating…" : "Download .xlsx"}
      </button>
      <div class="w-px h-5 bg-slate-200"></div>
      <button class="text-xs text-slate-500 hover:text-slate-700" onClick=${onLogout} title="Sign out">
        Sign out
      </button>
    </div>
  `;
}

function SheetTabs({ sheets, active, onSelect }) {
  // Excel-style bottom tabs, absolute-positioned along the bottom of the preview pane.
  return html`
    <div class="absolute bottom-0 left-0 right-0 h-10 flex items-end border-t border-slate-200 bg-white/80 backdrop-blur px-3 gap-1 overflow-x-auto flex-shrink-0 z-10">
      ${sheets.map(s => {
        const isActive = s === active;
        const label = s.trim() || s;
        return html`
          <button key=${s}
            class=${"px-3.5 py-1.5 text-xs whitespace-nowrap border-t-2 transition-colors " +
              (isActive
                ? "border-blue-600 text-slate-900 font-semibold bg-white"
                : "border-transparent text-slate-500 hover:text-slate-800")}
            onClick=${() => onSelect(s)}>
            ${label}
          </button>
        `;
      })}
    </div>
  `;
}

function GridView({ sheet, data, onHeaderClick, onFilterClick }) {
  const { columns, rows, grand_totals } = data;
  const [dismissedRawBanner, setDismissedRawBanner] = useState(false);
  const isRaw = sheet === "Dump";
  const shownRows = rows.length;

  // Build a friendly display title per sheet
  const title = sheet.trim() || sheet;
  const subtitle = isRaw
    ? `${data.total.toLocaleString()} rows from the uploaded file's Dump sheet · showing first ${shownRows.toLocaleString()}`
    : `${data.total.toLocaleString()} rows · showing first ${shownRows.toLocaleString()}`;

  // Which output columns have grand totals — render them as tiles
  const totalTiles = [];
  if (grand_totals) {
    for (const c of columns) {
      if (grand_totals[c.col] != null) {
        totalTiles.push({ col: c.col, header: c.header, value: grand_totals[c.col] });
      }
    }
  }

  return html`
    <div class="flex-1 flex flex-col min-h-0 min-w-0 bg-slate-100 overflow-hidden">
      ${isRaw && !dismissedRawBanner ? html`
        <div class="mx-5 mt-4 flex items-start gap-3 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm">
          <div class="flex-1">
            <div class="font-medium text-amber-900">This is the <span class="mono">Dump</span> sheet from the uploaded file.</div>
            <div class="text-amber-800 text-xs mt-0.5">Columns A–AG come straight from the Metabase export. AH–AX are enrichment columns produced by the rules here.</div>
          </div>
          <button class="text-amber-800 hover:text-amber-900 text-xs px-2 py-1 rounded border border-amber-300"
                  onClick=${() => setDismissedRawBanner(true)}>Dismiss</button>
        </div>
      ` : null}

      <div class="flex items-start justify-between px-5 pt-4 pb-3">
        <div>
          <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">${isRaw ? "Input sheet" : "Output sheet"}</div>
          <div class="text-lg font-semibold text-slate-900">${title}</div>
          <div class="text-xs text-slate-500 mt-0.5">${subtitle}</div>
        </div>
        ${onFilterClick ? html`
          <button class="text-xs px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700"
                  onClick=${onFilterClick}>Edit row filter</button>
        ` : null}
      </div>

      ${totalTiles.length ? html`
        <div class="px-5 pb-3 flex flex-wrap gap-3">
          ${totalTiles.map(t => html`
            <div key=${t.col} class="bg-white rounded-lg border border-slate-200 px-4 py-2.5 min-w-[140px]">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">${t.header}</div>
              <div class="text-sm font-semibold text-slate-900 tabular-nums mt-0.5">
                ${t.value.toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}
              </div>
            </div>
          `)}
        </div>
      ` : null}

      <div class="flex-1 min-h-0 px-5 pb-16">
        <div class="h-full bg-white rounded-lg border border-slate-200 overflow-auto">
          <table class="text-xs w-full">
            <thead class="bg-slate-50 sticky top-0 z-10 border-b border-slate-200">
              <tr>
                ${columns.map(c => {
                  const chip = chipFor(c.rule_type);
                  return html`
                    <th key=${c.col}
                        class="px-3 py-2.5 text-left font-medium text-slate-700 border-b border-slate-200 col-h-click hover:bg-blue-50"
                        onClick=${() => onHeaderClick(c)}
                        title=${c.is_derived ? "Click to edit rule or format" : "Click to edit display format"}>
                      <div class="flex flex-col gap-1">
                        <div class="flex items-center gap-2">
                          <span>${c.header}</span>
                          <span class="mono text-[10px] text-slate-400 font-normal">${c.col}</span>
                        </div>
                        ${chip ? html`<span class=${"chip " + chip.cls + " self-start"}>${chip.label}</span>` : null}
                      </div>
                    </th>
                  `;
                })}
              </tr>
            </thead>
            <tbody>
              ${rows.map((r, i) => html`
                <tr key=${i} class=${"hover:bg-blue-50/40 " + (i % 2 === 1 ? "bg-slate-50/60" : "")}>
                  ${r.map((v, j) => html`
                    <td key=${j} class="px-3 py-1.5 table-cell tabular-nums text-slate-700" title=${v == null ? "" : String(v)}>
                      ${fmt(v, columns[j]?.number_format)}
                    </td>
                  `)}
                </tr>
              `)}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

function SummaryView({ cells, onEdit }) {
  // Group cells by row number → { rowN: {A: cell, B: cell, C: cell} }
  const byRow = {};
  for (const c of cells) {
    const rowN = parseInt(c.cell.replace(/[A-Z]/g, ""), 10);
    const col = c.cell.replace(/\d/g, "");
    byRow[rowN] = byRow[rowN] || {};
    byRow[rowN][col] = c;
  }
  const rowNums = Object.keys(byRow).map(Number).sort((a, b) => a - b);

  const renderCell = (cell, isHeader) => {
    if (!cell) return html`<td></td>`;
    const chip = chipFor(cell.rule_type);
    const isTBD = cell.rule_type === "blank";
    const val = cell.value;
    const content = val == null
      ? html`<span class="italic text-slate-300">—</span>`
      : html`<span class="tabular-nums">${fmt(val, cell.number_format)}</span>`;
    const isLabelCol = cell.cell.startsWith("A");
    const isNumericB = cell.cell.startsWith("B") && typeof val === "number";
    return html`
      <td class=${"px-4 py-2 border-r border-slate-100 relative group cursor-pointer hover:bg-blue-50/60 " +
                   (isHeader ? "font-semibold text-slate-900 bg-slate-50 " : "") +
                   (isLabelCol && !isHeader ? "font-medium text-slate-800 " : "") +
                   (isNumericB ? "text-right " : "") +
                   (isTBD ? "bg-amber-50/50 " : "")}
          onClick=${() => onEdit(cell)}
          title=${"Click to edit " + cell.cell}>
        ${content}
        <span class="mono text-[9px] text-slate-300 absolute top-0.5 right-1 opacity-0 group-hover:opacity-100">${cell.cell}</span>
        ${chip ? html`<span class=${"chip " + chip.cls + " ml-2"}>${chip.label}</span>` : null}
      </td>
    `;
  };

  return html`
    <div class="flex-1 overflow-auto bg-slate-100 pb-16">
      <div class="px-5 pt-4 pb-3">
        <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Output sheet</div>
        <div class="text-lg font-semibold text-slate-900">Summary</div>
        <div class="text-xs text-slate-500 mt-0.5">Final billing roll-up. Click any cell to edit its rule.</div>
      </div>
      <div class="mx-5 bg-white rounded-lg border border-slate-200 overflow-hidden max-w-3xl">
        <table class="text-sm w-full">
          <thead class="bg-slate-50 text-slate-500 text-[11px]">
            <tr>
              <th class="w-10 px-2 py-1.5 font-medium text-center border-r border-slate-100"></th>
              <th class="px-4 py-1.5 font-medium text-left border-r border-slate-100">A</th>
              <th class="px-4 py-1.5 font-medium text-left border-r border-slate-100">B</th>
              <th class="px-4 py-1.5 font-medium text-left">C</th>
            </tr>
          </thead>
          <tbody>
            ${rowNums.map(n => {
              const row = byRow[n];
              const isHeader = n === 3;
              return html`
                <tr key=${n} class="border-t border-slate-100">
                  <td class="w-10 px-2 py-2 mono text-[10px] text-slate-400 text-center bg-slate-50 border-r border-slate-100">${n}</td>
                  ${renderCell(row.A, isHeader)}
                  ${renderCell(row.B, isHeader)}
                  ${renderCell(row.C, isHeader)}
                </tr>
              `;
            })}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// ---------- Rule inspector (right drawer) ---------- //

function ruleSummary(r) {
  if (!r) return "—";
  const t = r.rule_type;
  const c = r.config || {};
  if (t === "blank")   return "(blank / TBD)";
  if (t === "static")  return `static: ${JSON.stringify(c.value ?? "")}`;
  if (t === "direct")  return `direct from ${c.source || "?"}`;
  if (t === "formula") return c.formula || "(empty formula)";
  if (t === "filter")  return c.expr || "(empty filter)";
  if (t === "lookup")  return `lookup ${c.table || "?"} by ${c.key_col || "?"}`;
  if (t === "pivot_key")   return `group by ${c.source || "?"}`;
  if (t === "pivot_first") return `first of ${c.source || "?"}`;
  if (t === "pivot_agg")   return `${c.agg || "sum"}(${c.agg_col || "?"}) by ${c.source || "?"}`;
  return JSON.stringify(c);
}

function rulesEqual(a, b) {
  if (!a || !b) return false;
  if (a.rule_type !== b.rule_type) return false;
  return JSON.stringify(a.config || {}) === JSON.stringify(b.config || {});
}

// ---------- Pivot Structure panel (shown on pivot sheets) ---------- //

const PIVOT_SHEETS = new Set(["Order level ", "Order level - non permissible", "PF Summary"]);

function PivotStructure({ sheet, onJumpTo, currentCol, onColumnsChanged }) {
  // Fetches all rules for the current sheet and groups them by pivot role.
  const [rules, setRules] = useState(null);
  const [sheetCols, setSheetCols] = useState(null);
  const [err, setErr] = useState(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addHeader, setAddHeader] = useState("");
  const [addRuleType, setAddRuleType] = useState("blank");
  const [addFormula, setAddFormula] = useState("");
  const [addSource, setAddSource] = useState("");
  const [addAgg, setAddAgg] = useState("sum");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const reload = () => {
    Promise.all([
      api.get("/api/rules").then(all => all.filter(r => r.sheet === sheet)),
      api.get(`/api/sheet-columns/${encodeURIComponent(sheet)}`),
    ]).then(([rs, sc]) => { setRules(rs); setSheetCols(sc); })
      .catch(e => setErr(String(e)));
  };
  useEffect(reload, [sheet]);

  const addColumn = async () => {
    if (!addHeader.trim()) { setMsg({ ok: false, text: "Header required" }); return; }
    setBusy(true); setMsg(null);
    let config = {};
    if (addRuleType === "formula") config = { formula: addFormula.startsWith("=") ? addFormula : "=" + addFormula };
    else if (addRuleType === "pivot_agg") config = { source: addSource.toUpperCase(), agg_col: addSource.toUpperCase(), agg: addAgg };
    else if (addRuleType === "pivot_first") config = { source: addSource.toUpperCase() };
    try {
      const res = await api.post(`/api/sheet-columns/${encodeURIComponent(sheet)}`, {
        header: addHeader, rule_type: addRuleType, config,
      });
      if (res.error) throw new Error(res.error);
      await waitForRerun(res);
      setMsg({ ok: true, text: `Added column ${res.column.col_letter}: ${res.column.header}` });
      setAddHeader(""); setAddFormula(""); setAddSource("");
      setAddOpen(false);
      reload();
      onColumnsChanged && onColumnsChanged();
    } catch (e) { setMsg({ ok: false, text: String(e) }); }
    setBusy(false);
  };

  const deleteColumn = async (col_letter, is_system) => {
    const warn = is_system
      ? `"${col_letter}" is a system column (part of the source SBI format). Deleting it will change the downloaded xlsx structure. Continue?`
      : `Delete column "${col_letter}"? This also removes its rule and any format override.`;
    if (!confirm(warn)) return;
    setBusy(true);
    try {
      const res = await api.delete(`/api/sheet-columns/${encodeURIComponent(sheet)}/${encodeURIComponent(col_letter)}`);
      await waitForRerun(res);
      reload();
      onColumnsChanged && onColumnsChanged();
    } catch (e) { setMsg({ ok: false, text: String(e) }); }
    setBusy(false);
  };

  if (err) return html`<div class="p-2 text-xs text-red-700">${err}</div>`;
  if (!rules || !sheetCols) return null;

  const filter = rules.find(r => r.column_letter === "__filter__");
  const groupKeys = rules.filter(r => r.rule_type === "pivot_group_key" || r.rule_type === "pivot_key");
  const aggs      = rules.filter(r => r.rule_type === "pivot_agg");
  const firsts    = rules.filter(r => r.rule_type === "pivot_first");
  const counters  = rules.filter(r => r.rule_type === "counter");
  const others    = rules.filter(r => !["pivot_group_key","pivot_key","pivot_agg","pivot_first","counter","filter"].includes(r.rule_type) && r.column_letter !== "__filter__");

  const parseCfg = (r) => { try { return JSON.parse(r.config_json || "{}"); } catch { return {}; } };
  const colMeta = (letter) => sheetCols.find(c => c.col_letter === letter) || {};

  const Row = ({ r, label }) => {
    const cfg = parseCfg(r);
    const isCurrent = r.column_letter === currentCol;
    const meta = colMeta(r.column_letter);
    return html`
      <div key=${r.column_letter} class="flex items-center gap-1 group">
        <button onClick=${() => onJumpTo(r.column_letter)}
          class=${"flex-1 text-left px-2 py-1 rounded text-xs hover:bg-violet-100 flex items-center gap-2 " + (isCurrent ? "bg-violet-100 font-medium" : "")}>
          <span class="mono text-[10px] text-slate-400 w-6">${r.column_letter}</span>
          <span class="flex-1 truncate">${label}</span>
          ${meta.is_system === 0 ? html`<span class="chip chip-blue">custom</span>` : null}
          ${r.status === "approved" ? html`<span class="chip chip-green">approved</span>` : null}
        </button>
        <button
          title="Delete this column"
          class="opacity-0 group-hover:opacity-100 text-[11px] text-slate-400 hover:text-red-600 px-1"
          onClick=${(e) => { e.stopPropagation(); deleteColumn(r.column_letter, meta.is_system); }}
          disabled=${busy}>×</button>
      </div>
    `;
  };

  return html`
    <div class="border border-violet-200 bg-violet-50/50 rounded-md p-3 space-y-3">
      <div class="flex items-center gap-2">
        <div class="text-[10px] uppercase tracking-wider text-violet-800 font-semibold">Pivot structure</div>
        <div class="text-[11px] text-violet-700">${sheet.trim()}</div>
      </div>

      ${filter ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">Filter (which Dump rows flow in)</div>
          <button onClick=${() => onJumpTo("__filter__")}
            class=${"w-full text-left px-2 py-1 rounded text-xs hover:bg-violet-100 " + (currentCol === "__filter__" ? "bg-violet-100 font-medium" : "")}>
            <span class="mono">${parseCfg(filter).expr || "(no filter)"}</span>
          </button>
        </div>
      ` : null}

      ${groupKeys.length ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">Group by</div>
          <div class="space-y-0.5">
            ${groupKeys.map(r => {
              const cfg = parseCfg(r);
              const label = `${cfg.source || "?"} (key ${cfg.key ?? "—"})`;
              return Row({ r, label });
            })}
          </div>
        </div>
      ` : null}

      ${aggs.length ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">Values (aggregations)</div>
          <div class="space-y-0.5">
            ${aggs.map(r => {
              const cfg = parseCfg(r);
              const label = `${(cfg.agg || "sum").toUpperCase()}(${cfg.agg_col || cfg.source || "?"})`;
              return Row({ r, label });
            })}
          </div>
        </div>
      ` : null}

      ${firsts.length ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">First-of (categoricals)</div>
          <div class="space-y-0.5">
            ${firsts.map(r => Row({ r, label: `first of ${parseCfg(r).source || "?"}` }))}
          </div>
        </div>
      ` : null}

      ${counters.length ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">Counter</div>
          <div class="space-y-0.5">${counters.map(r => Row({ r, label: "row number" }))}</div>
        </div>
      ` : null}

      ${others.length ? html`
        <div>
          <div class="text-[10px] uppercase tracking-wider text-violet-700 font-medium mb-1">Other rules</div>
          <div class="space-y-0.5">${others.map(r => Row({ r, label: r.rule_type }))}</div>
        </div>
      ` : null}

      <div class="pt-2 border-t border-violet-200">
        ${addOpen ? html`
          <div class="space-y-2">
            <div class="text-[10px] uppercase tracking-wider text-violet-800 font-semibold">Add column</div>
            <div>
              <label class="block text-[10px] text-slate-600 mb-0.5">Header name</label>
              <input class="w-full border border-slate-300 rounded px-2 py-1 text-xs bg-white"
                     value=${addHeader} placeholder="e.g. Margin"
                     onInput=${(e) => setAddHeader(e.currentTarget.value)} />
            </div>
            <div>
              <label class="block text-[10px] text-slate-600 mb-0.5">Rule type</label>
              <select class="w-full border border-slate-300 rounded px-2 py-1 text-xs bg-white"
                      value=${addRuleType} onChange=${(e) => setAddRuleType(e.currentTarget.value)}>
                <option value="blank">blank (TBD)</option>
                <option value="pivot_agg">pivot_agg (SUM/MEAN/…)</option>
                <option value="pivot_first">pivot_first</option>
                <option value="formula">formula</option>
                <option value="static">static value</option>
              </select>
            </div>
            ${addRuleType === "formula" ? html`
              <div>
                <label class="block text-[10px] text-slate-600 mb-0.5">Formula</label>
                <input class="w-full border border-slate-300 rounded px-2 py-1 text-xs mono bg-white"
                       value=${addFormula} placeholder="=J*0.05 or =IF(…)"
                       onInput=${(e) => setAddFormula(e.currentTarget.value)} />
              </div>
            ` : null}
            ${addRuleType === "pivot_agg" ? html`
              <div class="flex gap-2">
                <div class="flex-1">
                  <label class="block text-[10px] text-slate-600 mb-0.5">Dump col</label>
                  <input class="w-full border border-slate-300 rounded px-2 py-1 text-xs mono bg-white"
                         value=${addSource} placeholder="Z"
                         onInput=${(e) => setAddSource(e.currentTarget.value.toUpperCase())} />
                </div>
                <div>
                  <label class="block text-[10px] text-slate-600 mb-0.5">Func</label>
                  <select class="border border-slate-300 rounded px-1 py-1 text-xs bg-white"
                          value=${addAgg} onChange=${(e) => setAddAgg(e.currentTarget.value)}>
                    <option value="sum">sum</option>
                    <option value="mean">mean</option>
                    <option value="count">count</option>
                    <option value="min">min</option>
                    <option value="max">max</option>
                  </select>
                </div>
              </div>
            ` : null}
            ${addRuleType === "pivot_first" ? html`
              <div>
                <label class="block text-[10px] text-slate-600 mb-0.5">Dump col to pick first value from</label>
                <input class="w-full border border-slate-300 rounded px-2 py-1 text-xs mono bg-white"
                       value=${addSource} placeholder="Q"
                       onInput=${(e) => setAddSource(e.currentTarget.value.toUpperCase())} />
              </div>
            ` : null}
            ${msg ? html`<div class=${"text-[11px] p-1.5 rounded " + (msg.ok ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800")}>${msg.text}</div>` : null}
            <div class="flex gap-2">
              <button class="text-xs px-3 py-1 rounded bg-violet-600 text-white disabled:bg-slate-400"
                      onClick=${addColumn} disabled=${busy}>
                ${busy ? "Adding…" : "Add"}
              </button>
              <button class="text-xs px-3 py-1 rounded border border-slate-300"
                      onClick=${() => setAddOpen(false)} disabled=${busy}>Cancel</button>
            </div>
          </div>
        ` : html`
          <button class="w-full text-left px-2 py-1.5 rounded text-xs text-violet-700 hover:bg-violet-100 border border-dashed border-violet-300"
                  onClick=${() => setAddOpen(true)}>
            + Add column
          </button>
        `}
      </div>
    </div>
  `;
}

// ---------- Number format editor ---------- //

const NF_PRESETS = [
  { label: "Default",             value: "" },
  { label: "Number",              value: "0" },
  { label: "Number, 2 decimals",  value: "0.00" },
  { label: "Number, 1000 sep",    value: "#,##0" },
  { label: "Number, 1000 sep + 2 dp", value: "#,##0.00" },
  { label: "Currency (₹)",         value: '"₹"#,##0.00' },
  { label: "Currency (₹ no dp)",   value: '"₹"#,##0' },
  { label: "Percent",              value: "0%" },
  { label: "Percent, 2 dp",        value: "0.00%" },
  { label: "Date — DD/MM/YY",      value: "dd/mm/yy" },
  { label: "Date — DDMMYY",        value: "ddmmyy" },
  { label: "Date — DD/MM/YYYY",    value: "dd/mm/yyyy" },
  { label: "Date — DD-Mon-YYYY",   value: "dd-mmm-yyyy" },
  { label: "Datetime",             value: "dd/mm/yyyy hh:mm" },
  { label: "Time",                 value: "hh:mm:ss" },
  { label: "Text",                 value: "@" },
];

function NumberFormatEditor({ sheet, col, value, originalValue, onChange }) {
  // value/originalValue/onChange are now passed down — state lives in the parent (RuleInspector).
  const custom = value !== "" && !NF_PRESETS.some(p => p.value === value);

  return html`
    <div class="border border-slate-200 rounded-md p-3 bg-slate-50/60">
      <div class="flex items-center justify-between mb-2">
        <label class="text-xs font-medium text-slate-600">Number format</label>
        ${value !== originalValue ? html`<span class="text-[10px] text-amber-600">unsaved</span>` : null}
      </div>
      <select class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-xs bg-white mb-2"
              value=${custom ? "__custom__" : (value || "")}
              onChange=${(e) => {
                const v = e.currentTarget.value;
                if (v === "__custom__") { onChange(value || " "); return; }  // flip to custom mode with current
                onChange(v);
              }}>
        ${NF_PRESETS.map(p => html`<option value=${p.value}>${p.label}${p.value ? ` — ${p.value}` : ""}</option>`)}
        <option value="__custom__">Custom…</option>
      </select>
      ${custom ? html`
        <input class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-xs mono focus:outline-none focus:border-blue-500"
               value=${value || ""}
               placeholder='e.g. "₹"#,##0.00 or dd/mm/yyyy'
               onInput=${(e) => onChange(e.currentTarget.value)} />
        <div class="text-[11px] text-slate-500 mt-1">Excel format string. Examples: <span class="mono">0.00%</span>, <span class="mono">dd/mm/yyyy</span>, <span class="mono">"₹"#,##0</span>.</div>
      ` : null}
      <div class="text-[11px] text-slate-500 mt-2">Applies to this column in the downloaded .xlsx.</div>
    </div>
  `;
}


function RuleInspector({ sheet, col, header, onClose, onSaved, onJumpTo }) {
  const [rule, setRule] = useState(null);
  const [originalRule, setOriginalRule] = useState(null);
  const [numberFormat, setNumberFormat] = useState("");
  const [originalNumberFormat, setOriginalNumberFormat] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [mode, setMode] = useState("guided"); // "guided" | "formula"
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true); setErr(null);
    Promise.all([
      api.get(`/api/rules/${encodeURIComponent(sheet)}/${encodeURIComponent(col)}`).catch((e) => ({ __err__: e })),
      api.get("/api/column-formats").catch(() => ({})),
    ]).then(([ruleResp, formats]) => {
      if (cancelled) return;

      // Seed number format from the overrides map
      const nf = (formats[sheet] || {})[col] || "";
      setNumberFormat(nf);
      setOriginalNumberFormat(nf);

      if (ruleResp && !ruleResp.__err__) {
        const r = ruleResp;
        let cfg = {};
        try { cfg = JSON.parse(r.config_json || "{}"); } catch {}
        setRule({ ...r, config: cfg });
        setOriginalRule({ rule_type: r.rule_type, config: JSON.parse(JSON.stringify(cfg)) });
        setMode(r.rule_type === "formula" ? "formula" : "guided");
      } else {
        // No rule — raw Dump column (format-only)
        setRule({
          rule_type: "__raw__", config: {},
          notes: "This column comes directly from the uploaded Dump (Metabase export). No transformation rule — you can only change its display format.",
        });
        setOriginalRule({ rule_type: "__raw__", config: {} });
        setMode("guided");
      }
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [sheet, col]);

  const approveAndSave = async () => {
    if (!rule) return;
    setSaving(true); setErr(null);
    try {
      let rerunJobId = null;
      // Save rule change (skip if raw-only)
      if (rule.rule_type !== "__raw__" && !rulesEqual(
            { rule_type: rule.rule_type, config: rule.config }, originalRule)) {
        const res = await api.put(`/api/rules/${encodeURIComponent(sheet)}/${encodeURIComponent(col)}`, {
          rule_type: rule.rule_type,
          config: rule.config,
          status: "approved",
        });
        rerunJobId = res.rerun_job_id || null;
      }
      // Save format change if different
      if (numberFormat !== originalNumberFormat) {
        await api.put(`/api/column-formats/${encodeURIComponent(sheet)}/${encodeURIComponent(col)}`, {
          number_format: numberFormat || null,
        });
      }
      await waitForRerun({ rerun_job_id: rerunJobId });
      onSaved && onSaved();
      onClose();
    } catch (e) { setErr(String(e)); }
    setSaving(false);
  };

  const update = (patch) => setRule(r => ({ ...r, ...patch }));
  const updateConfig = (patch) => setRule(r => ({ ...r, config: { ...r.config, ...patch } }));

  const availableTypes = useMemo(() => {
    if (col === "__filter__") return ["filter"];
    if (sheet === "Summary")  return ["static", "formula", "blank"];
    if (sheet === "PF Summary") return ["pivot_key", "pivot_first", "pivot_agg", "lookup", "formula", "static", "blank"];
    if (sheet === "Order level " || sheet === "Order level - non permissible")
      return ["counter", "pivot_group_key", "pivot_first", "pivot_agg", "formula", "static", "blank"];
    if (sheet === "Dump")
      return ["override", "direct", "formula", "lookup", "static", "blank"];
    return ["direct", "formula", "lookup", "static", "blank"];
  }, [sheet, col]);

  const ruleChanged = rule && rule.rule_type !== "__raw__" && originalRule && !rulesEqual(
    { rule_type: rule.rule_type, config: rule.config },
    originalRule,
  );
  const formatChanged = numberFormat !== originalNumberFormat;
  const hasChanges = ruleChanged || formatChanged;

  return html`
    <div class="fixed inset-0 z-20 pointer-events-none">
      <div class="absolute inset-0 bg-slate-900/20 pointer-events-auto" onClick=${onClose}></div>
      <div class="absolute right-0 top-0 h-full w-[560px] bg-white shadow-2xl pointer-events-auto drawer flex flex-col">
        <div class="p-4 border-b border-slate-200 flex items-center gap-3">
          <div class="flex-1">
            <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">${sheet.trim() || sheet} · ${col}</div>
            <div class="text-lg font-semibold text-slate-900 mt-0.5">${header}</div>
          </div>
          <div class="flex items-center gap-2">
            ${rule && rule.status === "approved"
              ? html`<span class="chip chip-green">approved</span>`
              : rule ? html`<span class="chip chip-gray">draft</span>` : null}
            <button class="text-slate-400 hover:text-slate-700 text-2xl leading-none" onClick=${onClose}>×</button>
          </div>
        </div>

        ${loading ? html`<div class="p-6 text-slate-500">Loading…</div>` : null}
        ${err ? html`<div class="p-3 bg-red-50 border-b border-red-200 text-red-700 text-sm">${err}</div>` : null}

        ${rule ? html`
          <div class="flex-1 overflow-auto p-4 space-y-4">
            ${PIVOT_SHEETS.has(sheet) && onJumpTo ? html`
              <${PivotStructure} sheet=${sheet} currentCol=${col} onJumpTo=${onJumpTo}
                                  onColumnsChanged=${onSaved} />
            ` : null}
            ${rule.notes ? html`<div class="p-3 bg-amber-50 border border-amber-200 text-amber-900 text-xs rounded-md">${rule.notes}</div>` : null}

            ${rule.rule_type === "__raw__" && sheet === "Dump" ? html`
              <div class="p-3 bg-blue-50 border border-blue-200 rounded-md">
                <div class="text-xs text-blue-900 mb-2">
                  This column comes from the upload. You can add an <span class="font-semibold">override rule</span>
                  to rewrite specific values (e.g. "if I=='Rx' then permissible") without changing the raw file.
                </div>
                <button class="text-xs px-3 py-1 rounded bg-blue-600 text-white hover:bg-blue-700"
                        onClick=${() => update({ rule_type: "override", config: { overrides: [] } })}>
                  + Add override rule
                </button>
              </div>
            ` : null}

            ${rule.rule_type !== "__raw__" ? html`
              <div>
                <label class="block text-xs font-medium text-slate-600 mb-1">Rule type</label>
                <select class="w-full border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white"
                        value=${rule.rule_type}
                        onChange=${(e) => update({ rule_type: e.currentTarget.value, config: {} })}>
                  ${availableTypes.map(t => html`<option value=${t}>${t}</option>`)}
                </select>
              </div>

              <div class="flex gap-1 bg-slate-100 rounded-md p-0.5 w-fit">
                <button class=${"text-xs px-3 py-1 rounded " + (mode === "guided" ? "bg-white shadow-sm font-medium" : "text-slate-600")}
                        onClick=${() => setMode("guided")}>Guided</button>
                <button class=${"text-xs px-3 py-1 rounded " + (mode === "formula" ? "bg-white shadow-sm font-medium" : "text-slate-600")}
                        onClick=${() => { setMode("formula"); if (rule.rule_type !== "formula") update({ rule_type: "formula", config: { formula: rule.config.formula || "=" } }); }}>Formula</button>
              </div>

              ${mode === "formula" || rule.rule_type === "formula" ? html`
                <${FormulaEditor} value=${rule.config.formula || ""}
                                  onChange=${(v) => updateConfig({ formula: v })}
                                  sheet=${sheet} column=${col} header=${header} />
              ` : html`
                <${GuidedEditor} rule=${rule} onChangeConfig=${updateConfig} />
              `}
            ` : null}

            ${col !== "__filter__" ? html`
              <${NumberFormatEditor}
                sheet=${sheet} col=${col}
                value=${numberFormat}
                originalValue=${originalNumberFormat}
                onChange=${(v) => setNumberFormat(v)} />
            ` : null}

            ${hasChanges ? html`
              <div class="border border-amber-300 bg-amber-50 rounded-md p-3 space-y-3">
                <div class="text-[10px] uppercase tracking-wider text-amber-800 font-medium">Review changes</div>
                ${ruleChanged ? html`
                  <div>
                    <div class="text-[11px] font-medium text-slate-700 mb-1">Rule</div>
                    <div class="space-y-1">
                      <div class="mono text-xs bg-white border border-slate-200 rounded px-2 py-1 break-all">
                        <span class="text-slate-400 mr-1">${originalRule.rule_type}</span>${ruleSummary(originalRule)}
                      </div>
                      <div class="mono text-xs bg-white border border-emerald-300 rounded px-2 py-1 break-all">
                        <span class="text-slate-400 mr-1">${rule.rule_type}</span>${ruleSummary(rule)}
                      </div>
                    </div>
                  </div>
                ` : null}
                ${formatChanged ? html`
                  <div>
                    <div class="text-[11px] font-medium text-slate-700 mb-1">Number format</div>
                    <div class="space-y-1">
                      <div class="mono text-xs bg-white border border-slate-200 rounded px-2 py-1 break-all">
                        ${originalNumberFormat || html`<span class="text-slate-400">(default)</span>`}
                      </div>
                      <div class="mono text-xs bg-white border border-emerald-300 rounded px-2 py-1 break-all">
                        ${numberFormat || html`<span class="text-slate-400">(default)</span>`}
                      </div>
                    </div>
                  </div>
                ` : null}
                <div class="text-[11px] text-amber-800">Approving saves ${ruleChanged && formatChanged ? "both" : (ruleChanged ? "the rule" : "the format")} — change reflects in all future runs for every month.</div>
              </div>
            ` : html`
              <div class="border border-slate-200 bg-slate-50 rounded-md p-3 text-xs text-slate-500">
                No changes yet. Edit ${rule.rule_type === "__raw__" ? "the format" : "the rule or format"} above, then approve & save.
              </div>
            `}
          </div>

          <div class="p-3 border-t border-slate-200 flex items-center gap-2 bg-slate-50">
            <button class="px-3 py-1.5 rounded-md border border-slate-300 bg-white text-slate-700 text-sm hover:bg-slate-50"
                    onClick=${onClose} disabled=${saving}>
              Cancel
            </button>
            <div class="flex-1"></div>
            <button class=${"px-4 py-1.5 rounded-md text-white text-sm font-medium " +
                           (hasChanges && !saving ? "bg-emerald-600 hover:bg-emerald-700" : "bg-slate-300 cursor-not-allowed")}
                    onClick=${approveAndSave}
                    disabled=${!hasChanges || saving}>
              ${saving ? "Saving…" : "Approve & save"}
            </button>
          </div>
        ` : null}
      </div>
    </div>
  `;
}

function FormulaEditor({ value, onChange, sheet, column, header }) {
  const [nlOpen, setNlOpen] = useState(false);
  const [nlText, setNlText] = useState("");
  const [nlBusy, setNlBusy] = useState(false);
  const [nlErr, setNlErr] = useState(null);

  const convert = async () => {
    if (!nlText.trim()) return;
    setNlBusy(true); setNlErr(null);
    try {
      const r = await api.post("/api/nl-to-formula", {
        text: nlText, sheet, column, header,
      });
      onChange(r.formula);
      setNlOpen(false);
      setNlText("");
    } catch (e) {
      setNlErr(String(e).replace(/^Error:\s*/, ""));
    }
    setNlBusy(false);
  };

  return html`
    <div>
      <div class="flex items-center justify-between mb-1">
        <label class="text-xs font-medium text-slate-600">Excel formula</label>
        <button type="button"
                class=${"text-[11px] px-2 py-0.5 rounded " +
                         (nlOpen ? "bg-violet-100 text-violet-700" : "text-violet-600 hover:bg-violet-50")}
                onClick=${() => setNlOpen(v => !v)}>
          ✨ Plain English
        </button>
      </div>
      <textarea class="w-full mono border border-slate-300 rounded-md px-2 py-1.5 text-xs h-28 focus:outline-none focus:border-blue-500"
                value=${value}
                placeholder="e.g. =IF(AH=\"non-permissible\",\"non-permissible\",IF(AM-AF<0,\"wallet exhausted\",\"No issue\"))"
                onInput=${(e) => onChange(e.currentTarget.value)}></textarea>

      ${nlOpen ? html`
        <div class="mt-3 border border-violet-200 bg-violet-50/60 rounded-md p-3">
          <label class="block text-xs font-medium text-violet-900 mb-1">Describe what the formula should do</label>
          <textarea class="w-full border border-violet-300 rounded-md px-2 py-1.5 text-xs h-20 bg-white focus:outline-none focus:border-violet-500"
                    value=${nlText}
                    placeholder="e.g. If tags contains Cashless and rejection_reasons is blank, mark as permissible, otherwise non-permissible"
                    onInput=${(e) => setNlText(e.currentTarget.value)}></textarea>
          <div class="flex items-center gap-2 mt-2">
            <button type="button"
                    class=${"text-xs px-3 py-1.5 rounded-md text-white font-medium " + (nlBusy ? "bg-violet-400" : "bg-violet-600 hover:bg-violet-700")}
                    disabled=${nlBusy || !nlText.trim()}
                    onClick=${convert}>
              ${nlBusy ? "Converting…" : "Convert to formula"}
            </button>
            <button type="button" class="text-xs text-slate-500 hover:text-slate-700" onClick=${() => setNlOpen(false)}>Cancel</button>
          </div>
          ${nlErr ? html`<div class="mt-2 p-2 bg-red-50 border border-red-200 rounded text-red-700 text-xs">${nlErr}</div>` : null}
          <div class="mt-2 text-[11px] text-violet-700">Needs <span class="mono">OPENAI_API_KEY</span> set in your shell.</div>
        </div>
      ` : null}

      <div class="mt-2 text-xs text-slate-500 space-y-1">
        <div>Column refs: plain letters (<span class="mono">AH</span>, <span class="mono">Z</span>) = current row.</div>
        <div>Cross-sheet aggregates: <span class="mono">Dump.AF</span>, <span class="mono">'PF Summary'.K</span></div>
        <div>Supported: IF, AND, OR, NOT, SUM, SUMIF, SUMIFS, MAX, MIN, ABS, ROUND, ROW, arithmetic, comparisons</div>
      </div>
    </div>
  `;
}

function GuidedEditor({ rule, onChangeConfig }) {
  const t = rule.rule_type;
  const cfg = rule.config || {};
  if (t === "direct")
    return html`
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Source column (letter in Dump)</label>
        <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.source || ""}
               onInput=${(e) => onChangeConfig({ source: e.currentTarget.value.toUpperCase() })} />
        <p class="text-xs text-slate-500 mt-1">e.g. <span class="mono">Z</span> (gmv_mrp), <span class="mono">Q</span> (PF ID)</p>
      </div>
    `;
  if (t === "static")
    return html`
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Value</label>
        <input class="border rounded px-2 py-1.5 text-sm w-full" value=${cfg.value ?? ""}
               onInput=${(e) => onChangeConfig({ value: e.currentTarget.value })} />
      </div>
    `;
  if (t === "blank")
    return html`<div class="text-sm text-slate-500">Blank — column will be empty. Useful as a TBD placeholder.</div>`;
  if (t === "filter")
    return html`
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Filter expression</label>
        <input class="border rounded px-2 py-1.5 text-sm w-full mono" value=${cfg.expr || ""}
               onInput=${(e) => onChangeConfig({ expr: e.currentTarget.value })} />
        <p class="text-xs text-slate-500 mt-1">
          Examples: <span class="mono">checker == 'permissible'</span>, <span class="mono">AH != 'non-permissible'</span>, <span class="mono">AX in ('No issue','wallet exhausted')</span>
        </p>
        <p class="text-xs text-slate-500 mt-1">Use column letters (AH, AX) or Dump header names (checker, Next Step).</p>
      </div>
    `;
  if (t === "override") {
    const overrides = Array.isArray(cfg.overrides) ? cfg.overrides : [];
    const update = (i, patch) => {
      const next = overrides.map((o, idx) => idx === i ? { ...o, ...patch } : o);
      onChangeConfig({ overrides: next });
    };
    const remove = (i) => onChangeConfig({ overrides: overrides.filter((_, idx) => idx !== i) });
    const add = () => onChangeConfig({ overrides: [...overrides, { if: "", then: "" }] });
    return html`
      <div class="space-y-2">
        <div class="text-xs text-slate-600">
          Walked top-to-bottom. The first row whose condition matches sets this cell's value.
          Rows that match nothing keep the raw uploaded value.
        </div>
        ${overrides.length === 0 ? html`<div class="text-xs italic text-slate-400">No overrides yet — column passes through raw.</div>` : null}
        ${overrides.map((ov, i) => html`
          <div key=${i} class="flex items-center gap-2 p-2 border border-slate-200 rounded-md bg-slate-50">
            <div class="flex-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 items-center text-xs">
              <span class="text-slate-500">when</span>
              <input class="border border-slate-300 rounded px-2 py-1 mono text-xs bg-white"
                     value=${ov.if || ""} placeholder="I == 'Rx'"
                     onInput=${(e) => update(i, { if: e.currentTarget.value })} />
              <span class="text-slate-500">then</span>
              <input class="border border-slate-300 rounded px-2 py-1 text-xs bg-white"
                     value=${ov.then ?? ""} placeholder="permissible"
                     onInput=${(e) => update(i, { then: e.currentTarget.value })} />
            </div>
            <button class="text-slate-400 hover:text-red-600 text-xl leading-none" onClick=${() => remove(i)}>×</button>
          </div>
        `)}
        <button type="button" class="text-xs px-3 py-1.5 rounded-md border border-dashed border-violet-300 text-violet-700 hover:bg-violet-50 w-full"
                onClick=${add}>+ Add override</button>
        <div class="text-[11px] text-slate-500">
          Condition syntax (same as filters): <span class="mono">I == 'Rx'</span>, <span class="mono">AH != 'permissible'</span>, <span class="mono">tags in ('Cashless')</span>
        </div>
      </div>
    `;
  }
  if (t === "counter")
    return html`
      <div class="text-sm text-slate-600">
        Auto-numbered row counter — 1, 2, 3… No parameters.
      </div>
    `;
  if (t === "pivot_key")
    return html`
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Group-by source column</label>
        <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.source || ""}
               onInput=${(e) => onChangeConfig({ source: e.currentTarget.value.toUpperCase() })} />
        <p class="text-xs text-slate-500 mt-1">e.g. <span class="mono">Q</span> (CORPORATE_IDENTIFIER)</p>
      </div>
    `;
  if (t === "pivot_group_key")
    return html`
      <div class="space-y-2">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Part of composite group key</label>
          <input class="border rounded px-2 py-1.5 text-sm w-32" type="number" min="0"
                 value=${cfg.key ?? 0}
                 onInput=${(e) => onChangeConfig({ key: parseInt(e.currentTarget.value, 10) || 0 })} />
          <p class="text-xs text-slate-500 mt-1">0 = first part, 1 = second part. Order level groups by (group_id, order_id) — two parts.</p>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Source column (letter in Dump)</label>
          <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.source || ""}
                 onInput=${(e) => onChangeConfig({ source: e.currentTarget.value.toUpperCase() })} />
          <p class="text-xs text-slate-500 mt-1">e.g. <span class="mono">D</span> (group_id), <span class="mono">E</span> (order_id)</p>
        </div>
      </div>
    `;
  if (t === "pivot_first")
    return html`
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Pick first-value from (Dump column letter)</label>
        <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.source || ""}
               onInput=${(e) => onChangeConfig({ source: e.currentTarget.value.toUpperCase() })} />
        <p class="text-xs text-slate-500 mt-1">e.g. <span class="mono">A</span> (corporate_partner → Type)</p>
      </div>
    `;
  if (t === "pivot_agg")
    return html`
      <div class="space-y-2">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Group-by column (same as pivot_key's source)</label>
          <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.source || ""}
                 onInput=${(e) => onChangeConfig({ source: e.currentTarget.value.toUpperCase() })} />
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Aggregate column (numeric)</label>
          <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.agg_col || ""}
                 onInput=${(e) => onChangeConfig({ agg_col: e.currentTarget.value.toUpperCase() })} />
          <p class="text-xs text-slate-500 mt-1">e.g. <span class="mono">AF</span> (co_pay_discount)</p>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Function</label>
          <select class="border rounded px-2 py-1.5 text-sm" value=${cfg.agg || "sum"}
                  onChange=${(e) => onChangeConfig({ agg: e.currentTarget.value })}>
            <option value="sum">sum</option>
            <option value="count">count</option>
            <option value="mean">mean (avg)</option>
            <option value="min">min</option>
            <option value="max">max</option>
          </select>
        </div>
      </div>
    `;
  if (t === "lookup")
    return html`
      <div class="space-y-2">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Lookup table name</label>
          <input class="border rounded px-2 py-1.5 text-sm w-56" value=${cfg.table || ""}
                 onInput=${(e) => onChangeConfig({ table: e.currentTarget.value })} />
          <p class="text-xs text-slate-500 mt-1">Default: <span class="mono">pf_wallet_limits</span></p>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Key column (in source)</label>
          <input class="border rounded px-2 py-1.5 text-sm mono w-32" value=${cfg.key_col || ""}
                 onInput=${(e) => onChangeConfig({ key_col: e.currentTarget.value.toUpperCase() })} />
        </div>
        <${LookupTableEditor} name=${cfg.table || ""} />
      </div>
    `;
  return html`<div class="text-sm text-slate-500">Switch to Formula mode for this rule type.</div>`;
}

function LookupTableEditor({ name }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const [text, setText] = useState("");

  useEffect(() => {
    if (!name) return;
    api.get("/api/lookup-tables").then(all => {
      const data = all[name] || [];
      setRows(data);
      setText(data.map(r => `${r.key}\t${r.value}`).join("\n"));
    }).catch(e => setErr(String(e)));
  }, [name]);

  const save = async () => {
    setSaving(true); setErr(null);
    try {
      const parsed = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean).map(l => {
        const [k, v] = l.split(/\t|,/).map(s => s.trim());
        const num = Number(v);
        return { key: k, value: Number.isFinite(num) && v !== "" ? num : v };
      });
      const res = await api.put(`/api/lookup-tables/${encodeURIComponent(name)}`, { data: parsed });
      await waitForRerun(res);
      setRows(parsed);
    } catch (e) { setErr(String(e)); }
    setSaving(false);
  };

  if (!name) return null;
  if (!rows) return html`<div class="text-xs text-slate-500">Loading lookup…</div>`;
  return html`
    <div class="mt-2 border rounded p-2 bg-slate-50">
      <div class="text-xs font-medium mb-1">Lookup data — one row per line, <span class="mono">key&lt;tab&gt;value</span></div>
      <textarea class="mono text-xs w-full border rounded h-28 p-2"
                value=${text}
                onInput=${(e) => setText(e.currentTarget.value)}></textarea>
      <div class="flex items-center gap-2 mt-1">
        <button class="text-xs px-2 py-1 rounded bg-slate-600 text-white" disabled=${saving} onClick=${save}>
          ${saving ? "Saving…" : "Save table"}
        </button>
        <div class="text-xs text-slate-500">${rows.length} entries</div>
      </div>
      ${err ? html`<div class="text-xs text-red-700">${err}</div>` : null}
    </div>
  `;
}

// ---------- App shell ---------- //

// ---------- Client selector ---------- //

function ClientSelector({ onPick }) {
  const [clients, setClients] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.get("/api/clients").then(setClients).catch(e => setErr(String(e)));
  }, []);
  if (err) return html`<div class="p-6 text-red-700 text-sm">${err}</div>`;
  if (!clients) return html`<div class="min-h-screen flex items-center justify-center text-slate-400 text-sm">Loading…</div>`;

  return html`
    <div class="min-h-screen flex items-center justify-center bg-slate-100">
      <div class="w-[440px] bg-white rounded-xl shadow-sm border border-slate-200 p-7">
        <div class="mb-5">
          <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Signed in</div>
          <div class="text-lg font-semibold text-slate-900 mt-0.5">Select a client</div>
          <p class="text-xs text-slate-500 mt-1">Rules, formulas and output format are per-client. More clients coming soon.</p>
        </div>
        <div class="space-y-2">
          ${clients.map(c => html`
            <button key=${c.id}
              onClick=${() => onPick(c)}
              disabled=${!c.active}
              class=${"w-full text-left px-4 py-3 rounded-lg border transition-colors " +
                (c.active
                  ? "border-slate-200 hover:border-blue-500 hover:bg-blue-50 cursor-pointer"
                  : "border-slate-100 bg-slate-50 text-slate-400 cursor-not-allowed")}>
              <div class="font-medium text-slate-900">${c.display_name}</div>
              <div class="text-xs text-slate-500 mt-0.5">
                <span class="mono">${c.code}</span>
                ${c.active ? "" : " · coming soon"}
              </div>
            </button>
          `)}
        </div>
      </div>
    </div>
  `;
}

// ---------- Left nav ---------- //

const NAV_ITEMS = [
  { id: "dashboard",   label: "Dashboard", icon: "📊" },
  { id: "working",     label: "Working sheet", icon: "📋" },
  { id: "rules",       label: "Rules", icon: "⚙️" },
  { id: "historicals", label: "Historicals", icon: "🗂️" },
  { id: "recon",       label: "Recon", icon: "🔍" },
];

function LeftNav({ active, onSelect, client, onSwitchClient }) {
  return html`
    <div class="w-48 bg-slate-900 text-slate-100 flex flex-col flex-shrink-0">
      <div class="px-4 py-4 border-b border-slate-800">
        <div class="text-[10px] uppercase tracking-widest text-slate-500 font-medium">MIS Platform</div>
        <div class="text-sm font-semibold mt-0.5 truncate" title=${client?.display_name || ""}>${client?.code || "—"}</div>
        ${onSwitchClient ? html`
          <button class="text-[10px] text-slate-400 hover:text-slate-200 mt-1" onClick=${onSwitchClient}>switch client</button>
        ` : null}
      </div>
      <nav class="flex-1 p-2 space-y-0.5">
        ${NAV_ITEMS.map(item => html`
          <button key=${item.id}
            class=${"w-full text-left px-3 py-2 rounded-md text-sm flex items-center gap-2.5 transition-colors " +
              (active === item.id ? "bg-slate-800 text-white font-medium" : "text-slate-300 hover:bg-slate-800/60 hover:text-white")}
            onClick=${() => onSelect(item.id)}>
            <span class="text-xs opacity-80">${item.icon}</span>
            ${item.label}
          </button>
        `)}
      </nav>
    </div>
  `;
}

// ---------- Dashboard ---------- //

function formatCurrency(v) {
  if (v == null) return "—";
  return "₹" + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function formatCount(v) {
  if (v == null) return "—";
  return Number(v).toLocaleString("en-IN");
}
const _MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];
function formatMonth(m) {
  // '2026-03' -> 'March 2026'
  if (!m || typeof m !== "string") return m || "—";
  const parts = m.split("-");
  if (parts.length !== 2) return m;
  const y = parseInt(parts[0], 10);
  const mm = parseInt(parts[1], 10);
  if (!mm || mm < 1 || mm > 12 || !y) return m;
  return `${_MONTH_NAMES[mm - 1]} ${y}`;
}

function Tile({ label, value, sub, tone }) {
  const toneCls = tone === "good" ? "text-emerald-700" : tone === "warn" ? "text-amber-700" : tone === "bad" ? "text-red-700" : "text-slate-900";
  return html`
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">${label}</div>
      <div class=${"text-2xl font-semibold tabular-nums mt-1.5 " + toneCls}>${value}</div>
      ${sub ? html`<div class="text-xs text-slate-500 mt-1">${sub}</div>` : null}
    </div>
  `;
}

function DashboardPage({ onOpenUpload, reloadToken }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    setErr(null);
    api.get("/api/dashboard").then(setD).catch(e => setErr(String(e)));
  }, [reload, reloadToken]);

  if (err) return html`<div class="p-6 text-red-700">${err}</div>`;
  if (!d) return html`<div class="p-6 text-slate-400 text-sm">Loading dashboard…</div>`;
  if (d.empty) return html`
    <div class="flex-1 flex items-center justify-center bg-slate-100">
      <div class="text-center bg-white rounded-xl border border-slate-200 p-8 max-w-md">
        <div class="text-sm text-slate-500 mb-4">No monthly input loaded yet.</div>
        <button class="px-4 py-2 rounded-md bg-slate-900 text-white text-sm font-medium hover:bg-slate-700"
                onClick=${onOpenUpload}>Upload Input</button>
      </div>
    </div>
  `;

  const nsTotal = d.next_step_breakdown.reduce((s, x) => s + x.count, 0);

  const totalGMV = (d.permissible.gmv || 0) + (d.non_permissible.gmv || 0);
  const totalOrders = (d.permissible.count || 0) + (d.non_permissible.count || 0);

  return html`
    <div class="flex-1 overflow-auto bg-slate-100">
      <div class="p-6 max-w-5xl mx-auto">
        <div class="flex items-baseline justify-between mb-6">
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Monthly billing</div>
            <h1 class="text-xl font-semibold text-slate-900 mt-0.5">${formatMonth(d.month)}</h1>
            <div class="text-xs text-slate-500 mt-0.5">${formatCount(d.row_count)} line items · ${formatCount(totalOrders)} orders</div>
          </div>
          <button class="text-xs text-slate-500 hover:text-slate-800 underline" onClick=${() => setReload(n => n + 1)}>Refresh</button>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <div class="bg-white rounded-xl border border-slate-200 p-6">
            <div class="flex items-center justify-between">
              <div class="text-[10px] uppercase tracking-wider text-emerald-700 font-semibold">Permissible</div>
              <span class="chip chip-green">to be billed</span>
            </div>
            <div class="mt-4">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Total GMV <span class="text-slate-300 normal-case">(${d.permissible.gmv_basis || "list price"})</span></div>
              <div class="text-3xl font-semibold text-slate-900 tabular-nums mt-1">${formatCurrency(d.permissible.gmv)}</div>
            </div>
            <div class="mt-4 pt-4 border-t border-slate-100">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Total orders</div>
              <div class="text-3xl font-semibold text-slate-900 tabular-nums mt-1">${formatCount(d.permissible.count)}</div>
            </div>
          </div>

          <div class="bg-white rounded-xl border border-slate-200 p-6">
            <div class="flex items-center justify-between">
              <div class="text-[10px] uppercase tracking-wider text-red-700 font-semibold">Non-permissible</div>
              <span class="chip chip-red">not billed</span>
            </div>
            <div class="mt-4">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Total GMV <span class="text-slate-300 normal-case">(${d.non_permissible.gmv_basis || "list price"})</span></div>
              <div class="text-3xl font-semibold text-slate-900 tabular-nums mt-1">${formatCurrency(d.non_permissible.gmv)}</div>
            </div>
            <div class="mt-4 pt-4 border-t border-slate-100">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Total orders</div>
              <div class="text-3xl font-semibold text-slate-900 tabular-nums mt-1">${formatCount(d.non_permissible.count)}</div>
            </div>
          </div>
        </div>

        <div class="mt-4 bg-white rounded-xl border border-slate-200 p-5 flex items-center justify-between">
          <div>
            <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Grand total</div>
            <div class="text-xs text-slate-600 mt-0.5">All orders, both categories · GMV on list price basis</div>
          </div>
          <div class="flex items-center gap-8">
            <div class="text-right">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">GMV</div>
              <div class="text-xl font-semibold text-slate-900 tabular-nums">${formatCurrency(totalGMV)}</div>
            </div>
            <div class="text-right">
              <div class="text-[10px] uppercase tracking-wider text-slate-400 font-medium">Orders</div>
              <div class="text-xl font-semibold text-slate-900 tabular-nums">${formatCount(totalOrders)}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ---------- Rules page ---------- //

function RulesPage({ onEdit, reloadToken }) {
  const [rules, setRules] = useState(null);
  const [formats, setFormats] = useState({});
  const [err, setErr] = useState(null);
  const [filterSheet, setFilterSheet] = useState("");
  const [filterTBDOnly, setFilterTBDOnly] = useState(false);
  const [reload, setReload] = useState(0);
  const [importing, setImporting] = useState(false);
  const [importErr, setImportErr] = useState(null);

  useEffect(() => {
    Promise.all([
      api.get("/api/rules"),
      api.get("/api/column-formats").catch(() => ({})),
    ]).then(([r, f]) => { setRules(r); setFormats(f); })
      .catch(e => setErr(String(e)));
  }, [reload, reloadToken]);

  const sheets = useMemo(() => {
    if (!rules) return [];
    return [...new Set(rules.map(r => r.sheet))];
  }, [rules]);

  const filtered = useMemo(() => {
    if (!rules) return [];
    return rules.filter(r => {
      if (filterSheet && r.sheet !== filterSheet) return false;
      if (filterTBDOnly && r.rule_type !== "blank") return false;
      return true;
    });
  }, [rules, filterSheet, filterTBDOnly]);

  const importFile = async (e) => {
    const file = e.currentTarget.files[0];
    if (!file) return;
    setImporting(true); setImportErr(null);
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const res = await api.post("/api/rules/import", payload);
      await waitForRerun(res);
      setReload(n => n + 1);
    } catch (err) { setImportErr(String(err)); }
    setImporting(false);
    e.currentTarget.value = "";
  };

  if (err) return html`<div class="p-6 text-red-700">${err}</div>`;
  if (!rules) return html`<div class="p-6 text-slate-400 text-sm">Loading rules…</div>`;

  const tbdCount = rules.filter(r => r.rule_type === "blank").length;

  return html`
    <div class="flex-1 overflow-auto bg-slate-100">
      <div class="p-6 max-w-6xl mx-auto">
        <div class="flex items-baseline justify-between mb-5">
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Persisted settings</div>
            <h1 class="text-xl font-semibold text-slate-900 mt-0.5">Rules</h1>
            <div class="text-xs text-slate-500 mt-0.5">
              ${rules.length} rules · ${tbdCount} still TBD · stored in <span class="mono">data/app.db</span> and persist across restarts
            </div>
          </div>
          <div class="flex items-center gap-2">
            <a class="text-xs px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700"
               href=${sbiUrl("/api/rules/export")} download>Export JSON</a>
            <label class="text-xs px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700 cursor-pointer">
              <input type="file" class="hidden" accept=".json" onChange=${importFile} />
              ${importing ? "Importing…" : "Import JSON"}
            </label>
          </div>
        </div>

        ${importErr ? html`<div class="mb-3 p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-xs">${importErr}</div>` : null}

        <div class="flex items-center gap-3 mb-3">
          <select class="text-xs border border-slate-300 rounded-md px-2 py-1.5 bg-white"
                  value=${filterSheet} onChange=${(e) => setFilterSheet(e.currentTarget.value)}>
            <option value="">All sheets</option>
            ${sheets.map(s => html`<option value=${s}>${s.trim() || s}</option>`)}
          </select>
          <label class="text-xs text-slate-600 flex items-center gap-1.5">
            <input type="checkbox" checked=${filterTBDOnly} onChange=${(e) => setFilterTBDOnly(e.currentTarget.checked)} />
            Only TBD
          </label>
          <div class="text-xs text-slate-400 ml-auto">${filtered.length} shown</div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 border-b border-slate-200 text-slate-600 text-xs">
              <tr>
                <th class="text-left font-medium px-3 py-2.5">Sheet</th>
                <th class="text-left font-medium px-3 py-2.5">Col</th>
                <th class="text-left font-medium px-3 py-2.5">Type</th>
                <th class="text-left font-medium px-3 py-2.5">Formula / Config</th>
                <th class="text-left font-medium px-3 py-2.5">Format</th>
                <th class="text-left font-medium px-3 py-2.5">Status</th>
                <th class="text-right font-medium px-3 py-2.5">Actions</th>
              </tr>
            </thead>
            <tbody>
              ${filtered.map(r => html`
                <${RulesRow} key=${r.id} rule=${r}
                  formatVal=${(formats[r.sheet] || {})[r.column_letter] || ""}
                  onOpen=${() => onEdit(r.sheet, r.column_letter)}
                  onReload=${() => setReload(n => n + 1)} />
              `)}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

function RulesRow({ rule, formatVal, onOpen, onReload }) {
  const [editingFormula, setEditingFormula] = useState(false);
  const [editingFormat, setEditingFormat] = useState(false);
  const [formulaDraft, setFormulaDraft] = useState("");
  const [formatDraft, setFormatDraft] = useState(formatVal);
  const [saving, setSaving] = useState(false);

  const parseCfg = () => { try { return JSON.parse(rule.config_json || "{}"); } catch { return {}; } };
  const cfg = parseCfg();
  const isFormula = rule.rule_type === "formula";
  const isTBD = rule.rule_type === "blank";

  const configLabel = (() => {
    if (isFormula) return cfg.formula || "(empty)";
    return ruleSummary({ rule_type: rule.rule_type, config: cfg });
  })();

  const startFormulaEdit = (e) => {
    e.stopPropagation();
    if (!isFormula) { onOpen(); return; }
    setFormulaDraft(cfg.formula || "");
    setEditingFormula(true);
  };

  const saveFormula = async () => {
    setSaving(true);
    try {
      await api.put(`/api/rules/${encodeURIComponent(rule.sheet)}/${encodeURIComponent(rule.column_letter)}`, {
        rule_type: "formula",
        config: { formula: formulaDraft },
        status: "approved",
      });
      onReload && onReload();
    } catch (e) { alert(String(e)); }
    setSaving(false); setEditingFormula(false);
  };

  const saveFormat = async () => {
    setSaving(true);
    try {
      await api.put(`/api/column-formats/${encodeURIComponent(rule.sheet)}/${encodeURIComponent(rule.column_letter)}`, { number_format: formatDraft || null });
      onReload && onReload();
    } catch (e) { alert(String(e)); }
    setSaving(false); setEditingFormat(false);
  };

  const chip = chipFor(rule.rule_type);

  return html`
    <tr class=${"border-b border-slate-100 " + (isTBD ? "bg-amber-50/30" : "")}>
      <td class="px-3 py-2 text-slate-600 text-xs">${rule.sheet.trim() || rule.sheet}</td>
      <td class="px-3 py-2 mono text-xs">${rule.column_letter}</td>
      <td class="px-3 py-2">${chip ? html`<span class=${"chip " + chip.cls}>${chip.label}</span>` : null}</td>

      <td class="px-3 py-2 mono text-xs max-w-md" onClick=${startFormulaEdit}>
        ${editingFormula ? html`
          <div class="flex gap-1 items-center">
            <input class="flex-1 border border-blue-400 rounded px-2 py-1 mono text-xs focus:outline-none focus:border-blue-600"
                   value=${formulaDraft} autoFocus disabled=${saving}
                   onInput=${(e) => setFormulaDraft(e.currentTarget.value)}
                   onKeyDown=${(e) => { if (e.key === "Enter") saveFormula(); if (e.key === "Escape") setEditingFormula(false); }} />
            <button class="text-xs px-2 py-1 bg-emerald-600 text-white rounded" disabled=${saving} onClick=${saveFormula}>Save</button>
            <button class="text-xs px-2 py-1 text-slate-500" disabled=${saving} onClick=${(e) => { e.stopPropagation(); setEditingFormula(false); }}>Esc</button>
          </div>
        ` : html`
          <span class=${"truncate block cursor-text hover:bg-blue-50 rounded px-1 " + (isTBD ? "italic text-slate-400" : "")} title=${configLabel}>
            ${configLabel}
          </span>
        `}
      </td>

      <td class="px-3 py-2 mono text-xs" onClick=${(e) => { e.stopPropagation(); setEditingFormat(true); setFormatDraft(formatVal); }}>
        ${editingFormat ? html`
          <div class="flex gap-1 items-center">
            <select class="border border-blue-400 rounded px-1 py-0.5 text-xs"
                    value=${NF_PRESETS.some(p => p.value === formatDraft) ? formatDraft : "__custom__"}
                    onChange=${(e) => { const v = e.currentTarget.value; if (v !== "__custom__") setFormatDraft(v); }}>
              ${NF_PRESETS.map(p => html`<option value=${p.value}>${p.label}</option>`)}
              <option value="__custom__">Custom…</option>
            </select>
            <input class="w-32 border border-slate-300 rounded px-1 py-0.5 mono text-xs"
                   value=${formatDraft} disabled=${saving}
                   onInput=${(e) => setFormatDraft(e.currentTarget.value)}
                   onKeyDown=${(e) => { if (e.key === "Enter") saveFormat(); if (e.key === "Escape") setEditingFormat(false); }} />
            <button class="text-xs px-2 py-0.5 bg-emerald-600 text-white rounded" disabled=${saving} onClick=${saveFormat}>Save</button>
            <button class="text-xs px-2 py-0.5 text-slate-500" disabled=${saving} onClick=${(e) => { e.stopPropagation(); setEditingFormat(false); }}>Esc</button>
          </div>
        ` : html`
          <span class="cursor-text hover:bg-blue-50 rounded px-1 inline-block min-w-[40px]">
            ${formatVal ? html`<span class="text-blue-700">${formatVal}</span>` : html`<span class="text-slate-300">—</span>`}
          </span>
        `}
      </td>

      <td class="px-3 py-2 text-xs">
        ${rule.status === "approved"
          ? html`<span class="chip chip-green">approved</span>`
          : html`<span class="chip chip-gray">draft</span>`}
      </td>

      <td class="px-3 py-2 text-right">
        <button class="text-xs text-blue-600 hover:text-blue-800" onClick=${(e) => { e.stopPropagation(); onOpen(); }}>Open →</button>
      </td>
    </tr>
  `;
}

// ---------- Historicals page ---------- //

function HistoricalsPage({ reloadToken }) {
  const [data, setData] = useState(null);
  const [monthFilter, setMonthFilter] = useState("");
  const [err, setErr] = useState(null);
  const [reload, setReload] = useState(0);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadMonth, setUploadMonth] = useState(() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  });
  const [file, setFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState(null);

  useEffect(() => {
    const q = monthFilter ? `?month=${encodeURIComponent(monthFilter)}` : "";
    api.get(`/api/historicals${q}`)
      .then(setData).catch(e => setErr(String(e)));
  }, [monthFilter, reload, reloadToken]);

  const doUpload = async () => {
    if (!file) return;
    if (!_isMonthYYYYMM(uploadMonth)) {
      setUploadMsg({ ok: false, text: "Pick a valid month (YYYY-MM)." });
      return;
    }
    if (!file.size) {
      setUploadMsg({ ok: false, text: "File is empty." });
      return;
    }
    setUploading(true); setUploadMsg(null);
    try {
      let res;
      if (file.size >= CHUNKED_UPLOAD_THRESHOLD_BYTES) {
        res = await uploadFileChunked({
          file,
          month: uploadMonth.trim(),
          kind: "pf_summary",
          timeoutPerChunkMs: CHUNK_PUT_TIMEOUT_MS,
        });
      } else {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("month", uploadMonth.trim());
        fd.append("kind", "pf_summary");
        res = await postFormWithUploadProgress("/api/upload", fd, {
          timeoutMs: UPLOAD_POST_TIMEOUT_MS,
        });
      }
      if (res.error) { setUploadMsg({ ok: false, text: res.error }); return; }
      const final = res.job_id
        ? await api.pollJob(res.job_id, undefined, { maxWallMs: UPLOAD_PIPELINE_POLL_MS, maxConsecErr: 8 })
        : { status: "done", result: res };
      if (final.status === "error") throw new Error(final.error || "Import failed");
      const result = final.result || {};
      setUploadMsg({ ok: true, text: `Imported ${result.records_imported || 0} records (months: ${(result.months_detected || []).join(", ") || "—"})` });
      setFile(null);
      setReload(n => n + 1);
    } catch (e) {
      setUploadMsg({ ok: false, text: String(e).replace(/^Error:\s*/, "") });
    } finally {
      setUploading(false);
    }
  };

  const deleteMonth = async (m) => {
    if (!confirm(`Delete all PF historicals for ${formatMonth(m)}?`)) return;
    const res = await api.delete(`/api/historicals/${encodeURIComponent(m)}`);
    await waitForRerun(res);
    setReload(n => n + 1);
  };

  if (err) return html`<div class="p-6 text-red-700 text-sm">${err}</div>`;
  if (!data) return html`<div class="p-6 text-slate-400 text-sm">Loading…</div>`;

  const rowsByMonth = {};
  for (const r of data.rows) {
    rowsByMonth[r.month] = rowsByMonth[r.month] || { count: 0, pharma_sum: 0, ahc_sum: 0, wallet_filled: 0 };
    rowsByMonth[r.month].count += 1;
    rowsByMonth[r.month].pharma_sum += r.pharma_total || 0;
    rowsByMonth[r.month].ahc_sum += r.ahc_total || 0;
    if (r.wallet_limit != null) rowsByMonth[r.month].wallet_filled += 1;
  }

  return html`
    <div class="flex-1 overflow-auto bg-slate-100 pb-20">
      <div class="p-6 max-w-6xl mx-auto">
        <div class="flex items-baseline justify-between mb-5">
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Cumulative record</div>
            <h1 class="text-xl font-semibold text-slate-900 mt-0.5">PF historicals</h1>
            <div class="text-xs text-slate-500 mt-0.5">${data.rows.length.toLocaleString()} records across ${data.months.length} months · auto-used by PF Summary</div>
          </div>
          <div class="flex items-center gap-2">
            <a class="text-xs px-3 py-1.5 rounded-md border border-slate-300 hover:bg-slate-50 text-slate-700"
               href=${sbiUrl("/api/historicals")} target="_blank">View JSON</a>
            <button class="text-xs px-3 py-1.5 rounded-md bg-slate-900 text-white hover:bg-slate-700"
                    onClick=${() => setUploadOpen(v => !v)}>
              ${uploadOpen ? "Close upload" : "+ Upload PF Summary"}
            </button>
          </div>
        </div>

        ${uploadOpen ? html`
          <div class="bg-white rounded-xl border border-slate-200 p-4 mb-5">
            <div class="text-sm font-medium text-slate-900 mb-2">Upload a historical PF Summary file</div>
            <div class="text-xs text-slate-500 mb-3">
              Pick the month the file's columns reference (<span class="mono">Jan</span>/<span class="mono">Feb</span>/etc. map to months of this year by default).
              Existing months in the file will be imported into pf_historicals.
            </div>
            <div class="flex items-center gap-3">
              <label class="text-xs font-medium text-slate-600">Reference month</label>
              <input class="border border-slate-300 rounded-md px-3 py-1.5 w-44 text-sm"
                     type="month" value=${uploadMonth}
                     onInput=${(e) => setUploadMonth(e.currentTarget.value)} />
              <input type="file" accept=".xlsx" class="text-xs"
                     onChange=${(e) => setFile(e.currentTarget.files[0])} />
              <button class=${"px-3 py-1.5 text-xs font-medium rounded-md " +
                               (file && !uploading ? "bg-blue-600 text-white hover:bg-blue-700" : "bg-slate-100 text-slate-400 cursor-not-allowed")}
                      onClick=${doUpload} disabled=${!file || uploading}>
                ${uploading ? "Uploading…" : "Import"}
              </button>
            </div>
            ${uploadMsg ? html`
              <div class=${"mt-3 p-2 border rounded text-xs " + (uploadMsg.ok ? "bg-emerald-50 border-emerald-200 text-emerald-800" : "bg-red-50 border-red-200 text-red-700")}>${uploadMsg.text}</div>
            ` : null}
          </div>
        ` : null}

        <div class="grid grid-cols-3 gap-3 mb-4">
          ${data.months.map(m => {
            const s = rowsByMonth[m] || {};
            return html`
              <div key=${m} class=${"bg-white rounded-lg border p-4 " + (monthFilter === m ? "border-blue-400" : "border-slate-200")}>
                <div class="flex items-start justify-between">
                  <button class="text-left"
                    onClick=${() => setMonthFilter(monthFilter === m ? "" : m)}>
                    <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">${formatMonth(m)}</div>
                    <div class="text-lg font-semibold text-slate-900 mt-0.5">${(s.count || 0).toLocaleString()} PFs</div>
                  </button>
                  <button class="text-[10px] text-red-500 hover:text-red-700" onClick=${() => deleteMonth(m)} title="Delete all entries for this month">delete</button>
                </div>
                <div class="text-xs text-slate-500 mt-1">
                  pharma ₹${(s.pharma_sum || 0).toLocaleString("en-IN", {maximumFractionDigits: 0})}
                  · AHC ₹${(s.ahc_sum || 0).toLocaleString("en-IN", {maximumFractionDigits: 0})}
                </div>
                <div class="text-[11px] text-slate-400 mt-0.5">wallets filled: ${s.wallet_filled || 0}</div>
              </div>
            `;
          })}
        </div>

        <div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <div class="flex items-center justify-between px-4 py-2 border-b border-slate-200 bg-slate-50">
            <div class="text-xs text-slate-600">
              ${monthFilter
                ? html`Showing <span class="font-medium">${formatMonth(monthFilter)}</span> · ${data.rows.length.toLocaleString()} rows`
                : html`Showing <span class="font-medium">all months</span> · ${data.rows.length.toLocaleString()} rows`}
            </div>
            ${monthFilter ? html`<button class="text-xs text-blue-600" onClick=${() => setMonthFilter("")}>clear filter</button>` : null}
          </div>
          <div style="max-height: 50vh" class="overflow-auto">
            <table class="w-full text-xs">
              <thead class="bg-slate-50 text-slate-600 text-[11px] sticky top-0">
                <tr>
                  <th class="text-left font-medium px-3 py-2">PF</th>
                  <th class="text-left font-medium px-3 py-2">Month</th>
                  <th class="text-left font-medium px-3 py-2">Type</th>
                  <th class="text-right font-medium px-3 py-2">Wallet</th>
                  <th class="text-right font-medium px-3 py-2">Pharma total</th>
                  <th class="text-right font-medium px-3 py-2">AHC total</th>
                </tr>
              </thead>
              <tbody>
                ${data.rows.slice(0, 500).map((r, i) => html`
                  <tr key=${i} class=${i % 2 === 1 ? "bg-slate-50/40" : ""}>
                    <td class="px-3 py-1.5 mono">${r.pf}</td>
                    <td class="px-3 py-1.5">${formatMonth(r.month)}</td>
                    <td class="px-3 py-1.5 text-slate-600">${r.pf_type || ""}</td>
                    <td class="px-3 py-1.5 text-right tabular-nums">${r.wallet_limit != null ? r.wallet_limit.toLocaleString("en-IN") : ""}</td>
                    <td class="px-3 py-1.5 text-right tabular-nums">${r.pharma_total != null ? r.pharma_total.toLocaleString("en-IN", {maximumFractionDigits: 2}) : ""}</td>
                    <td class="px-3 py-1.5 text-right tabular-nums">${r.ahc_total != null ? r.ahc_total.toLocaleString("en-IN", {maximumFractionDigits: 2}) : ""}</td>
                  </tr>
                `)}
              </tbody>
            </table>
            ${data.rows.length > 500 ? html`<div class="text-xs text-slate-400 p-2 border-t">Showing first 500 rows — use month filter above to narrow.</div>` : null}
          </div>
        </div>
      </div>
    </div>
  `;
}

// ---------- Recon page ---------- //

function ReconPage({ reloadToken }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [err, setErr] = useState(null);

  const run = useCallback(async () => {
    setLoading(true); setErr(null); setElapsed(0);
    const t0 = Date.now();
    const tick = setInterval(() => setElapsed(((Date.now() - t0) / 1000).toFixed(1)), 100);
    try {
      const r = await api.get("/api/recon");
      setData(r);
    } catch (e) { setErr(String(e)); }
    clearInterval(tick);
    setLoading(false);
  }, []);

  // No auto-run on mount. User clicks "Run recon" to trigger.

  const fmtNum = (v) => v == null ? "—" : Number(v).toLocaleString("en-IN");
  const fmtMoney = (v) => v == null ? "—" : "₹" + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // Idle (no data yet, not loading) → empty state with a Run button.
  if (!data && !loading && !err) return html`
    <div class="flex-1 flex items-center justify-center bg-slate-100">
      <div class="bg-white rounded-xl border border-slate-200 p-8 max-w-md w-full text-center">
        <div class="text-3xl mb-2">🔍</div>
        <div class="text-sm font-medium text-slate-900">Reconciliation</div>
        <div class="text-xs text-slate-500 mt-1 mb-5">
          Verifies row count, GMV_MRP grand total, and unique order_id count between the
          raw input file and the transformed Dump sheet. Computed fresh — no cached values.
        </div>
        <button class="px-4 py-2 rounded-md bg-slate-900 text-white text-sm font-medium hover:bg-slate-700"
                onClick=${run}>Run recon</button>
        <div class="text-[11px] text-slate-400 mt-3">Takes about 5–10 seconds for a 100k-row dump.</div>
      </div>
    </div>
  `;

  if (loading) return html`
    <div class="flex-1 flex items-center justify-center bg-slate-100">
      <div class="bg-white rounded-xl border border-slate-200 p-8 max-w-sm w-full text-center">
        <div class="flex items-center justify-center mb-3">
          <span class="w-3 h-3 rounded-full bg-violet-500 animate-pulse"></span>
        </div>
        <div class="text-sm font-medium text-slate-900">Computing recon…</div>
        <div class="text-xs text-slate-500 mt-1">Re-reading raw input file from disk and recomputing all three metrics independently.</div>
        <div class="text-2xl font-mono text-slate-700 mt-4 tabular-nums">${elapsed}s</div>
      </div>
    </div>
  `;

  if (err) return html`
    <div class="flex-1 flex items-center justify-center bg-slate-100">
      <div class="bg-white rounded-xl border border-red-200 p-6 max-w-md w-full">
        <div class="text-sm font-medium text-red-900 mb-2">Recon failed</div>
        <div class="text-xs text-red-700 mono break-all">${err}</div>
        <button class="mt-4 px-3 py-1.5 rounded-md border border-slate-300 text-sm hover:bg-slate-50"
                onClick=${run}>Try again</button>
      </div>
    </div>
  `;
  if (data.empty) return html`
    <div class="flex-1 flex items-center justify-center bg-slate-100">
      <div class="text-center bg-white rounded-xl border border-slate-200 p-8 max-w-md">
        <div class="text-sm text-slate-500 mb-2">No raw input loaded for the active month.</div>
        <div class="text-xs text-slate-400">${data.error || ""}</div>
      </div>
    </div>
  `;

  const rows = [
    { key: "row_count",        label: "Total rows",                 fmt: fmtNum },
    { key: "gmv_mrp_total",    label: "Grand total of GMV_MRP",     fmt: fmtMoney },
    { key: "unique_order_ids", label: "Unique order_id count",      fmt: fmtNum },
  ];

  return html`
    <div class="flex-1 overflow-auto bg-slate-100">
      <div class="p-6 max-w-5xl mx-auto">
        <div class="flex items-baseline justify-between mb-5">
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-400 font-medium">Reconciliation</div>
            <h1 class="text-xl font-semibold text-slate-900 mt-0.5">Recon · ${formatMonth(data.month)}</h1>
            <div class="text-xs text-slate-500 mt-0.5">
              Computed at request-time from two independent code paths
              ${data.compute_seconds != null ? ` · took ${data.compute_seconds}s` : ""}
            </div>
          </div>
          <button class=${"text-xs px-3 py-1.5 rounded-md border " +
                          (loading ? "border-slate-200 text-slate-400" : "border-slate-300 hover:bg-slate-50 text-slate-700")}
                  onClick=${run} disabled=${loading}>
            ${loading ? "Recomputing…" : "Re-run recon"}
          </button>
        </div>

        <div class=${"mb-4 p-4 rounded-lg border " + (data.matches
          ? "bg-emerald-50 border-emerald-200 text-emerald-900"
          : "bg-red-50 border-red-200 text-red-900")}>
          <div class="flex items-center gap-2">
            <span class="text-lg">${data.matches ? "✓" : "⚠️"}</span>
            <div class="font-semibold">
              ${data.matches
                ? "All metrics match — no data lost or corrupted in transformation."
                : "Mismatch detected — pipeline differs from raw input. See diffs below."}
            </div>
          </div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <div class="grid grid-cols-3 text-[10px] uppercase tracking-wider text-slate-400 font-medium border-b border-slate-200 bg-slate-50 px-4 py-2">
            <div></div>
            <div>Raw input</div>
            <div>Transformed (Dump sheet)</div>
          </div>
          <div class="grid grid-cols-3 text-[11px] text-slate-500 px-4 py-2 border-b border-slate-200">
            <div>source</div>
            <div class="mono truncate" title=${data.raw.source}>${data.raw.source}</div>
            <div class="mono truncate" title=${data.transformed.source}>${data.transformed.source}</div>
          </div>
          ${rows.map(r => {
            const a = data.raw[r.key];
            const b = data.transformed[r.key];
            const d = data.diffs[r.key];
            const ok = d === 0;
            return html`
              <div key=${r.key} class=${"grid grid-cols-3 px-4 py-3 border-b border-slate-100 items-center " + (ok ? "" : "bg-red-50/40")}>
                <div>
                  <div class="text-sm font-medium text-slate-900">${r.label}</div>
                  <div class="text-[11px] mono text-slate-400">diff = ${typeof d === "number" ? r.fmt(d) : d}</div>
                </div>
                <div class="text-lg tabular-nums font-semibold text-slate-900">${r.fmt(a)}</div>
                <div class=${"text-lg tabular-nums font-semibold " + (ok ? "text-slate-900" : "text-red-700")}>
                  ${r.fmt(b)}
                  ${ok ? html`<span class="ml-2 chip chip-green text-[9px]">match</span>`
                       : html`<span class="ml-2 chip chip-red text-[9px]">diff</span>`}
                </div>
              </div>
            `;
          })}
        </div>

        <div class="mt-3 text-[11px] text-slate-500">
          <div class="font-medium text-slate-700">How recon works</div>
          <ul class="list-disc list-inside mt-1 space-y-0.5">
            <li><b>Raw side</b>: re-read the uploaded .xlsx file from disk via openpyxl streaming. Counts rows, sums column <span class="mono">gmv_mrp</span>, collects distinct <span class="mono">order_id</span>.</li>
            <li><b>Transformed side</b>: same metrics computed via pandas on the in-memory pipeline result that gets written to the Dump sheet of the downloaded xlsx.</li>
            <li>Two independent code paths. If the pipeline drops or duplicates rows, the diffs show it.</li>
          </ul>
        </div>
      </div>
    </div>
  `;
}


// ---------- App shell ---------- //

function App() {
  const [authState, setAuthState] = useState("checking"); // "checking" | "anon" | "authed"
  const [client, setClient] = useState(() => {
    try { return JSON.parse(sessionStorage.getItem("sbi_client") || "null"); } catch { return null; }
  });
  const [status, setStatus] = useState(null);
  const [schema, setSchema] = useState(null);
  const [err, setErr] = useState(null);
  const [forceUpload, setForceUpload] = useState(false);
  const [activeNav, setActiveNav] = useState("dashboard");
  const [rulesEditTarget, setRulesEditTarget] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  /** Used to slow polling while the tab is in the background and the pipeline is busy. */
  const [tabHidden, setTabHidden] = useState(
    () => typeof document !== "undefined" && document.hidden,
  );

  const statusComputing = status?.computing === true;
  const statusReady = status != null;

  const checkAuth = useCallback(() => {
    api.get("/api/me")
      .then((r) => setAuthState(r.authenticated ? "authed" : "anon"))
      .catch(() => setAuthState("anon"));
  }, []);

  const refreshStatus = useCallback(() => {
    api.get("/api/status").then(setStatus).catch((e) => {
      if (String(e).includes("unauthorized")) setAuthState("anon");
      else setErr(String(e));
    });
  }, []);

  useEffect(() => {
    if (authState !== "authed") return;
    const onVis = () => {
      const h = document.hidden;
      setTabHidden(h);
      if (!h) {
        refreshStatus();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [authState, refreshStatus]);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  useEffect(() => {
    if (authState !== "authed") return;
    api.get("/api/schema").then(setSchema).catch((e) => setErr(String(e)));
    refreshStatus();
  }, [authState, refreshStatus]);

  useEffect(() => {
    if (authState !== "authed") return;
    const needPoll = !statusReady || statusComputing;
    if (!needPoll) {
      return undefined;
    }
    const intervalMs =
      !statusReady ? (tabHidden ? 5000 : 2000) : tabHidden ? 10_000 : 2500;
    const t = setInterval(refreshStatus, intervalMs);
    return () => clearInterval(t);
  }, [authState, refreshStatus, statusComputing, statusReady, tabHidden]);

  const logout = async () => {
    try { await api.post("/api/logout"); } catch {}
    sessionStorage.removeItem("sbi_client");
    setClient(null);
    setAuthState("anon");
    setStatus(null); setSchema(null); setForceUpload(false);
  };

  const pickClient = (c) => {
    sessionStorage.setItem("sbi_client", JSON.stringify(c));
    setClient(c);
  };

  if (authState === "checking") return html`<div class="min-h-screen flex items-center justify-center text-slate-400 text-sm">…</div>`;
  if (authState === "anon") return html`<${LoginPage} onLoggedIn=${() => setAuthState("authed")} />`;
  if (!client) return html`<${ClientSelector} onPick=${pickClient} />`;

  if (err) return html`<div class="p-6 text-red-700">${err}</div>`;
  if (!status || !schema) return html`<div class="min-h-screen flex items-center justify-center text-slate-400 text-sm">Loading…</div>`;

  // Upload flow (overlays the main shell)
  if (!status.has_upload || forceUpload) {
    return html`<${UploadPage}
      onUploaded=${() => { setForceUpload(false); setActiveNav("dashboard"); refreshStatus(); }}
      onCancel=${status.has_upload ? () => setForceUpload(false) : null} />`;
  }

  return html`
    <div class="h-full flex min-h-0">
      <${LeftNav} active=${activeNav} onSelect=${setActiveNav}
                  client=${client}
                  onSwitchClient=${() => { sessionStorage.removeItem("sbi_client"); setClient(null); }} />
      <div class="flex-1 flex flex-col min-w-0 min-h-0">
        <${TopBar} status=${status}
          onReupload=${() => setForceUpload(true)}
          onLogout=${logout}
          onActivateMonth=${async (m) => {
            await api.post("/api/runs/" + encodeURIComponent(m) + "/activate");
            refreshStatus();
            setReloadToken(t => t + 1);
          }} />
        ${activeNav === "dashboard" ? html`<${DashboardPage} reloadToken=${reloadToken} onOpenUpload=${() => setForceUpload(true)} />` : null}
        ${activeNav === "working" ? html`
          <${PreviewPage} status=${status} schema=${schema}
            reloadToken=${reloadToken}
            onOpenRule=${(t) => setRulesEditTarget(t)} />
        ` : null}
        ${activeNav === "rules" ? html`
          <${RulesPage} reloadToken=${reloadToken}
            onEdit=${(sheet, col) => setRulesEditTarget({ sheet, col, header: col })} />
        ` : null}
        ${activeNav === "historicals" ? html`
          <${HistoricalsPage} reloadToken=${reloadToken} />
        ` : null}
        ${activeNav === "recon" ? html`
          <${ReconPage} reloadToken=${reloadToken} />
        ` : null}
      </div>

      ${rulesEditTarget ? html`
        <${RuleInspector}
          sheet=${rulesEditTarget.sheet}
          col=${rulesEditTarget.col}
          header=${rulesEditTarget.header}
          onClose=${() => setRulesEditTarget(null)}
          onSaved=${() => { setReloadToken(t => t + 1); refreshStatus(); }}
          onJumpTo=${(newCol) => setRulesEditTarget({ sheet: rulesEditTarget.sheet, col: newCol, header: newCol })} />
      ` : null}
    </div>
  `;
}

render(h(App), document.getElementById("app"));
