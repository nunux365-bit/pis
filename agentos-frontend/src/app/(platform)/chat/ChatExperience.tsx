"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import {
  createChatSession,
  listChatMessages,
  listChatSessions,
  sendChatMessage,
  getAnalyticsSummary,
  type ChatMessageDto,
} from "@/lib/api";
import {
  InlineApprovalCard,
  type ApprovalCardData,
} from "@/components/approval-card/InlineApprovalCard";

type UiMsg = {
  /** Persisted message id when loaded from API */
  id?: string;
  /** Ephemeral key for optimistic rows before server echo */
  clientKey?: string;
  role: "system" | "user" | "agent";
  text: string;
  approval?: ApprovalCardData | null;
};

function dtoToUi(m: ChatMessageDto): UiMsg {
  const role =
    m.role === "user" ? "user" : m.role === "assistant" ? "agent" : "system";
  const meta = m.meta as
    | {
        approval_card?: Partial<ApprovalCardData> & { approval_id?: string };
      }
    | null
    | undefined;
  const raw = meta?.approval_card;
  let approval: ApprovalCardData | null = null;
  if (raw && typeof raw.approval_id === "string" && raw.approval_id.length > 0) {
    approval = {
      approval_id: raw.approval_id,
      title: raw.title ?? "Approval",
      amount: raw.amount,
      confidence: typeof raw.confidence === "number" ? raw.confidence : 0,
      risk:
        raw.risk === "low" || raw.risk === "medium" || raw.risk === "high"
          ? raw.risk
          : "medium",
      summary: raw.summary,
      expanded_detail: raw.expanded_detail,
    };
  }
  return {
    id: m.id,
    role,
    text: m.content,
    approval,
  };
}

export function ChatExperience() {
  const searchParams = useSearchParams();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMsg[]>([]);
  const [input, setInput] = useState("");
  const [typing, setTyping] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const appliedQueryRef = useRef(false);
  const sessionFromUrl = searchParams.get("session")?.trim() ?? "";

  const bootstrap = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const sessions = await listChatSessions();
      let sid: string;
      if (sessions.length === 0) {
        const s = await createChatSession("Main");
        sid = s.id;
      } else if (
        sessionFromUrl &&
        sessions.some((s) => s.id === sessionFromUrl)
      ) {
        sid = sessionFromUrl;
      } else {
        sid = sessions[0].id;
      }
      setSessionId(sid);
      const rows = await listChatMessages(sid);
      if (rows.length === 0) {
        let intro =
          "Welcome to AgentOS. Ask about your O2C / S2P review queue or type a task.";
        try {
          const sum = await getAnalyticsSummary();
          intro = `You have **${sum.pending_approvals}** item(s) in your review queue. Ask “What’s in my O2C / S2P queue?” for details.`;
        } catch {
          /* ignore */
        }
        setMessages([{ clientKey: "intro", role: "system", text: intro }]);
      } else {
        setMessages(rows.map(dtoToUi));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load chat");
      setMessages([
        {
          clientKey: "bootstrap-error",
          role: "system",
          text: "Could not load conversation. Check that the API is running and you are signed in.",
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionFromUrl]);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  /* Deep link: /chat?q=... from quick actions & command bar */
  useEffect(() => {
    const q = searchParams.get("q");
    if (!q || appliedQueryRef.current || loading) return;
    appliedQueryRef.current = true;
    setInput((prev) => prev || decodeURIComponent(q.trim()));
  }, [searchParams, loading]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, typing]);

  const handleSend = async (text?: string) => {
    const msg = (text || input).trim();
    if (!msg || !sessionId) return;
    const pendingKey = `pending-${crypto.randomUUID()}`;
    setMessages((prev) => [
      ...prev,
      { clientKey: pendingKey, role: "user", text: msg },
    ]);
    setInput("");
    setTyping(true);
    setError(null);
    try {
      const res = await sendChatMessage(msg, sessionId);
      setMessages((prev) => {
        const withoutPending = prev.filter((m) => m.clientKey !== pendingKey);
        return [
          ...withoutPending,
          dtoToUi(res.user_message),
          dtoToUi(res.assistant_message),
        ];
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Send failed");
      setMessages((prev) => {
        const withoutPending = prev.filter((m) => m.clientKey !== pendingKey);
        return [
          ...withoutPending,
          {
            clientKey: `err-${crypto.randomUUID()}`,
            role: "agent",
            text: "Sorry — the message could not be sent. Please try again.",
          },
        ];
      });
    } finally {
      setTyping(false);
    }
  };

  const suggestions = [
    "What’s in my O2C / S2P queue?",
    "Show supply chain anomalies",
    "Draft morning briefing",
    "Run budget variance check",
  ];

  return (
    <div className="flex flex-col h-[calc(100vh-80px)] max-h-[calc(100vh-80px)]">
      <div className="pb-4 mb-3 border-b border-[var(--border)]">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-app-gradient-1 flex items-center justify-center">
            <span className="text-white text-sm">✦</span>
          </div>
          <div>
            <h3 className="text-sm font-semibold">AgentOS Assistant</h3>
            <div
              className={`text-xs flex items-center gap-1 ${
                error ? "text-[var(--accent-red)]" : "text-[var(--accent-green)]"
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  error ? "bg-[var(--accent-red)]" : "bg-[var(--accent-green)]"
                }`}
              />
              {loading ? "Connecting…" : error ? "Connection issue" : "Connected"}
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-2 text-xs text-[var(--accent-red)] px-1">{error}</div>
      )}

      <div className="flex-1 overflow-y-auto flex flex-col gap-4 pr-2 pb-4">
        {messages.map((m, i) => (
          <div
            key={m.id ?? m.clientKey ?? `row-${i}`}
            className={`flex gap-2.5 ${
              m.role === "user" ? "justify-end" : "justify-start"
            } max-w-[85%] ${m.role === "user" ? "self-end" : "self-start"}`}
          >
            {m.role !== "user" && (
              <div className="w-8 h-8 rounded-lg bg-app-gradient-1 flex items-center justify-center flex-shrink-0 mt-0.5">
                <span className="text-white text-xs">✦</span>
              </div>
            )}
            <div
              className={`rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap ${
                m.role === "user"
                  ? "bg-[var(--accent-blue)] rounded-br-md text-white"
                  : "bg-[var(--bg-card)] border border-[var(--border)] rounded-bl-md"
              }`}
            >
              {m.role !== "user" ? (
                <span className="[&_strong]:font-semibold [&_strong]:text-[var(--text-primary)]">
                  {m.text.split("**").map((part, j) =>
                    j % 2 === 1 ? (
                      <strong key={j}>{part}</strong>
                    ) : (
                      <span key={j}>{part}</span>
                    )
                  )}
                </span>
              ) : (
                m.text
              )}
              {m.role === "agent" && m.approval && (
                <InlineApprovalCard data={m.approval} />
              )}
            </div>
          </div>
        ))}
        {typing && (
          <div className="flex gap-2.5 items-start">
            <div className="w-8 h-8 rounded-lg bg-app-gradient-1 flex items-center justify-center flex-shrink-0">
              <span className="text-white text-xs">✦</span>
            </div>
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-2xl rounded-bl-md px-4 py-3 flex gap-1.5">
              {[0, 1, 2].map((j) => (
                <div
                  key={j}
                  className="w-2 h-2 rounded-full bg-[var(--text-muted)] animate-pulse motion-reduce:animate-none"
                  style={{ animationDelay: `${j * 0.2}s` }}
                />
              ))}
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {messages.length <= 1 && !loading && (
        <div className="flex gap-2 mb-3 flex-wrap">
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => handleSend(s)}
              className="bg-[var(--bg-card)] border border-[var(--border)] rounded-full px-3.5 py-2 text-xs text-[var(--text-secondary)] hover:border-[var(--border-active)]/50 hover:text-[var(--text-primary)] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="flex gap-2.5 py-3 border-t border-[var(--border)]">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
          disabled={!sessionId || loading}
          placeholder="Ask anything…"
          className="flex-1 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl px-4 py-3 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:border-[var(--border-active)] disabled:opacity-50"
        />
        <button
          type="button"
          disabled={!sessionId || loading}
          onClick={() => handleSend()}
          className="w-12 h-12 rounded-xl bg-app-gradient-1 border-none text-white flex items-center justify-center cursor-pointer hover:opacity-90 transition-opacity disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <svg
            width="20"
            height="20"
            fill="currentColor"
            viewBox="0 0 24 24"
            aria-hidden
          >
            <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
