import Link from "next/link";

function IconDoc({ className }: { className?: string }) {
  return (
    <svg className={className} width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M14 2v6h6M8 13h8M8 17h6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function IconCart({ className }: { className?: string }) {
  return (
    <svg className={className} width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M6 6h15l-1.5 9h-12L6 6Zm0 0L5 3H2"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M9 21a1 1 0 1 0 0-2 1 1 0 0 0 0 2Zm8 0a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" fill="currentColor" />
    </svg>
  );
}

function IconStack({ className }: { className?: string }) {
  return (
    <svg className={className} width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M12 3 4 7l8 4 8-4-8-4ZM4 12l8 4 8-4M4 17l8 4 8-4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconCheck({ className }: { className?: string }) {
  return (
    <svg className={className} width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M20 6L9 17l-5-5"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconChevron({ className }: { className?: string }) {
  return (
    <svg className={className} width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M9 6l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const trustChips = [
  { short: "Simple forms", hint: "Only SAP-required fields say Req" },
  { short: "Files in Drive", hint: "One folder per ticket" },
  { short: "SAP in sync", hint: "Submit or resubmit to SAP anytime" },
];

const flowSteps = [
  {
    n: "1",
    title: "Request",
    micro: "What to buy",
    body: "Tell us what to buy — we map it for SAP.",
  },
  {
    n: "2",
    title: "SAP number",
    micro: "On your ticket",
    body: "When SAP accepts it, you will see a document id.",
  },
  {
    n: "3",
    title: "Order",
    micro: "PO in SAP",
    body: "Create a PO with or without a linked request — copy lines when you have a SAP-backed PR, or enter manually.",
  },
];

const faq = [
  {
    q: "What is a purchase request (PR)?",
    a: "It is the internal “please buy this” form. It does not spend money by itself — it starts the approval and buying process.",
  },
  {
    q: "When can I create a purchase order (PO)?",
    a: "Anytime: you can start a standalone PO and fill lines yourself, or — when you have a purchase request that SAP has already accepted — pick it on the PO screen to copy its lines. The optional list only shows your own SAP-backed requests.",
  },
  {
    q: "Where do my uploads go?",
    a: "Each ticket gets its own folder in your organisation Google Drive so finance and buyers can find quotes and specs in one place.",
  },
];

/**
 * Procurement home — command-centre layout: understand + act in one glance, minimal scroll.
 */
export default function ProcurementLanding() {
  return (
    <>
      <a
        href="#procurement-main"
        className="pointer-events-none fixed left-4 top-4 z-[100] -translate-y-20 rounded-xl bg-[var(--bg-card)] px-4 py-2.5 text-sm font-semibold text-[var(--text-primary)] opacity-0 shadow-xl ring-2 ring-[var(--accent-green)] transition focus:pointer-events-auto focus:translate-y-0 focus:opacity-100"
      >
        Skip to procurement content
      </a>

      <div className="mx-auto w-full max-w-[min(100%,88rem)] space-y-4 pb-6 sm:space-y-5 sm:pb-8 lg:space-y-4">
        {/* Hero + trust — one tight band */}
        <div className="proc-landing-hero relative -mx-6 -mt-6 rounded-b-2xl border-b border-[var(--border)] bg-[var(--bg-card)] px-5 pb-6 pt-5 sm:-mx-6 sm:rounded-b-3xl sm:px-6 sm:pb-7 sm:pt-6 lg:-mx-4 lg:px-4 xl:-mx-5 xl:px-5 2xl:-mx-6 2xl:px-6">
          <div
            className="pointer-events-none absolute inset-0 overflow-hidden rounded-b-2xl sm:rounded-b-3xl"
            aria-hidden
          >
            <div className="proc-hero-mesh absolute inset-0 opacity-90" />
            <div className="absolute -right-24 top-0 h-[280px] w-[280px] rounded-full bg-[rgba(21,163,115,0.08)] blur-3xl" />
            <div className="absolute -bottom-28 -left-16 h-56 w-56 rounded-full bg-[rgba(47,127,210,0.08)] blur-3xl" />
          </div>

          <main id="procurement-main" className="relative z-10">
            <div className="lg:grid lg:grid-cols-12 lg:items-end lg:gap-8">
              <div className="lg:col-span-7">
                <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-[var(--accent-green)] sm:text-[11px]">
                  Procurement desk
                </p>
                <h1 className="mt-1.5 text-pretty text-[clamp(1.35rem,2.4vw,2.15rem)] font-bold leading-[1.15] tracking-tight text-[var(--text-primary)]">
                  Buying made simple — for everyone on the team
                </h1>
                <p className="mt-2 max-w-2xl text-sm leading-snug text-[var(--text-secondary)] sm:text-[15px] sm:leading-relaxed">
                  One short form. We keep SAP, Drive, and your ticket aligned — then you choose what to do next.
                </p>
              </div>
              <ul className="mt-4 flex list-none flex-wrap gap-2 lg:col-span-5 lg:mt-0 lg:justify-end" aria-label="At a glance">
                {trustChips.map((c) => (
                  <li
                    key={c.short}
                    title={c.hint}
                    className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-secondary)]/90 px-3 py-1.5 text-[11px] font-semibold text-[var(--text-secondary)] shadow-sm ring-1 ring-black/[0.02] backdrop-blur-sm sm:text-xs"
                  >
                    <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[rgba(21,163,115,0.14)] text-[var(--accent-green)]">
                      <IconCheck className="h-3 w-3" />
                    </span>
                    {c.short}
                  </li>
                ))}
              </ul>
            </div>
          </main>
        </div>

        {/* Primary actions — first thing after the promise (above the fold) */}
        <section aria-labelledby="proc-actions-title" className="relative z-10 -mt-1">
          <h2 id="proc-actions-title" className="sr-only">
            What do you want to do?
          </h2>
          <div className="grid gap-2.5 sm:grid-cols-3 sm:gap-3">
            <Link
              href="/procurement/pr/new"
              className="group relative flex min-h-[4.5rem] items-center gap-3 rounded-2xl border-2 border-[var(--accent-green)]/35 bg-[var(--bg-card)] p-3.5 shadow-md ring-1 ring-[rgba(21,163,115,0.12)] outline-none transition hover:border-[var(--accent-green)]/60 hover:shadow-lg focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/45 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)] sm:min-h-[5rem] sm:p-4"
            >
              <span className="absolute right-2.5 top-2.5 rounded-full bg-[rgba(21,163,115,0.14)] px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide text-[var(--accent-green)] sm:text-[10px]">
                Start
              </span>
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-[rgba(21,163,115,0.12)] text-[var(--accent-green)] transition group-hover:scale-[1.03] sm:h-12 sm:w-12">
                <IconDoc />
              </span>
              <span className="min-w-0 flex-1 pr-6">
                <span className="block text-sm font-bold text-[var(--text-primary)] group-hover:text-[var(--accent-green)] sm:text-base">
                  Request something
                </span>
                <span className="mt-0.5 block text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
                  New purchase request (PR)
                </span>
              </span>
              <IconChevron className="shrink-0 text-[var(--accent-green)] opacity-70 transition group-hover:translate-x-0.5 group-hover:opacity-100" />
            </Link>

            <Link
              href="/procurement/po/new"
              className="group flex min-h-[4.5rem] items-center gap-3 rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-3.5 shadow-sm outline-none transition hover:border-[var(--accent-blue)]/45 hover:shadow-md focus-visible:ring-2 focus-visible:ring-[var(--accent-blue)]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)] sm:min-h-[5rem] sm:p-4"
            >
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-[rgba(47,127,210,0.1)] text-[var(--accent-blue)] sm:h-12 sm:w-12">
                <IconCart />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-bold text-[var(--text-primary)] group-hover:text-[var(--accent-blue)] sm:text-base">
                  Place an order
                </span>
                <span className="mt-0.5 block text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
                  Purchase order (PO) — standalone or from your request
                </span>
              </span>
              <IconChevron className="shrink-0 text-[var(--accent-blue)] opacity-60 transition group-hover:translate-x-0.5 group-hover:opacity-100" />
            </Link>

            <Link
              href="/procurement/tickets"
              className="group flex min-h-[4.5rem] items-center gap-3 rounded-2xl border border-[var(--border)] bg-[var(--bg-elev)]/90 p-3.5 shadow-sm outline-none transition hover:bg-[var(--bg-card)] hover:shadow-md focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)] sm:min-h-[5rem] sm:p-4"
            >
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-[var(--bg-card)] text-[var(--text-secondary)] ring-1 ring-[var(--border)] sm:h-12 sm:w-12">
                <IconStack />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-bold text-[var(--text-primary)] sm:text-base">Your tickets</span>
                <span className="mt-0.5 block text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
                  Drafts, submissions, SAP status
                </span>
              </span>
              <IconChevron className="shrink-0 text-[var(--text-muted)] transition group-hover:translate-x-0.5 group-hover:text-[var(--accent-blue)]" />
            </Link>
          </div>
        </section>

        {/* Flow — one horizontal strip (no tall cards) */}
        <section aria-labelledby="proc-flow-title" className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/95 p-3.5 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
          <h2 id="proc-flow-title" className="mb-2.5 text-xs font-bold uppercase tracking-wide text-[var(--text-muted)]">
            The flow · 3 steps
          </h2>
          <ol className="grid list-none grid-cols-1 gap-2 p-0 sm:grid-cols-3 sm:gap-0 sm:divide-x sm:divide-[var(--border)]">
            {flowSteps.map((item, i) => (
              <li key={item.n} className="relative min-w-0 sm:px-3 sm:first:pl-0 sm:last:pr-0">
                <div className="flex items-start gap-2.5 rounded-xl px-2 py-2 sm:block sm:px-0 sm:py-0">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-app-gradient-1 text-xs font-bold text-white shadow-sm sm:mb-2">
                    {item.n}
                  </span>
                  <div className="min-w-0">
                    <p className="text-[11px] font-bold uppercase tracking-wide text-[var(--text-muted)]">{item.micro}</p>
                    <h3 className="text-sm font-semibold text-[var(--text-primary)] sm:text-[15px]">{item.title}</h3>
                    <p className="mt-1 text-[11px] leading-snug text-[var(--text-secondary)] sm:text-xs">{item.body}</p>
                  </div>
                </div>
                {i < flowSteps.length - 1 ? (
                  <div className="my-1 flex justify-center sm:hidden" aria-hidden>
                    <span className="text-lg text-[var(--border)]">↓</span>
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </section>

        {/* FAQ — one closed block so the page stays short */}
        <details
          id="proc-faq"
          className="group rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]"
        >
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3.5 text-sm font-semibold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden hover:bg-[var(--bg-elev)]/50 sm:px-5 sm:py-4">
            <span>
              Quick answers{" "}
              <span className="font-normal text-[var(--text-muted)]">(PR, PO, uploads — optional read)</span>
            </span>
            <span className="text-xs font-medium text-[var(--accent-blue)] group-open:hidden">Open</span>
            <span className="hidden text-xs font-medium text-[var(--accent-blue)] group-open:inline">Close</span>
          </summary>
          <div className="border-t border-[var(--border)] px-3 pb-3 pt-2 sm:px-4 sm:pb-4">
            <div className="grid gap-2 sm:grid-cols-2 sm:gap-3">
              {faq.map((item, fi) => (
                <details
                  key={item.q}
                  className={`overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-primary)]/40 ${fi === 2 ? "sm:col-span-2" : ""}`}
                >
                  <summary className="cursor-pointer list-none px-3 py-2.5 text-left text-xs font-semibold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden hover:bg-[var(--bg-elev)]/60 sm:px-4 sm:py-3 sm:text-sm">
                    {item.q}
                  </summary>
                  <p className="border-t border-[var(--border)]/70 px-3 py-2.5 text-[11px] leading-relaxed text-[var(--text-secondary)] sm:px-4 sm:text-xs">
                    {item.a}
                  </p>
                </details>
              ))}
            </div>
          </div>
        </details>
      </div>
    </>
  );
}
