"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useAuth } from "@/contexts/AuthContext";
import {
  optimusChat,
  optimusListConversations,
  optimusCreateConversation,
  optimusDeleteConversation,
  optimusGetConversationMessages,
  optimusListDocuments,
  type OptimusConversation,
  type OptimusDocument,
  type OptimusChatResponse,
} from "@/lib/optimusApi";

// ─────────────────────────────────────────────────────────────────────────────
// Utility functions
// ─────────────────────────────────────────────────────────────────────────────

function formatRelativeTime(timestamp: string | undefined): string {
  if (!timestamp) return "";
  const now = new Date();
  const date = new Date(timestamp);
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin} min ago`;
  if (diffHour < 24) return `${diffHour} hour${diffHour > 1 ? "s" : ""} ago`;
  if (diffDay === 1) return "Yesterday";
  if (diffDay < 7) {
    const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    return `${days[date.getDay()]} ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function cn(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: OptimusChatResponse["citations"];
  confidence?: string;
  timestamp?: string;
  isError?: boolean;
  needsClarification?: boolean;
  suggestions?: string[];
}

// ─────────────────────────────────────────────────────────────────────────────
// Components
// ─────────────────────────────────────────────────────────────────────────────

function GreetingScreen({ onExampleClick }: { onExampleClick: (query: string) => void }) {
  const { user } = useAuth();
  const firstName = user?.full_name?.split(" ")[0] || "there";

  return (
    <div className="flex flex-1 flex-col items-center justify-center px-4 py-8">
      <div className="max-w-2xl text-center">
        <div className="mb-4 text-4xl">👋</div>
        <h2 className="text-2xl font-bold text-[var(--text-primary)]">
          Hello, {firstName}!
        </h2>
        <p className="mt-2 text-[var(--text-secondary)]">
          I&apos;m Optimus, your intelligent assistant for company policies and documents
        </p>

        <div className="mt-8 grid grid-cols-2 gap-3 text-left">
          {[
            { icon: "📚", title: "Company Policies", desc: "HR, Legal, AI, POSH, and more" },
            { icon: "💼", title: "Career & Performance", desc: "Appraisals, promotions, and growth" },
            { icon: "🎁", title: "Benefits & Reimbursements", desc: "Medical, travel, and allowances" },
            { icon: "🕐", title: "Attendance & Working Hours", desc: "Shifts, WFH, and overtime" },
          ].map((item) => (
            <div
              key={item.title}
              className="flex items-start gap-3 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3"
            >
              <span className="text-xl">{item.icon}</span>
              <div>
                <div className="text-sm font-medium text-[var(--text-primary)]">{item.title}</div>
                <div className="text-xs text-[var(--text-muted)]">{item.desc}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-8">
          <p className="mb-3 text-sm font-medium text-[var(--text-secondary)]">Try asking:</p>
          <div className="flex flex-col gap-2">
            {[
              "What health insurance benefits are available?",
              "How does the performance review process work?",
              "What is the policy for remote work?",
            ].map((q) => (
              <button
                key={q}
                onClick={() => onExampleClick(q)}
                className="flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-4 py-2.5 text-left text-sm text-[var(--text-primary)] transition-colors hover:border-[var(--accent-green)] hover:bg-[var(--bg-elev)]"
              >
                <span className="text-[var(--accent-green)]">→</span>
                {q}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function ConversationsSidebar({
  conversations,
  currentId,
  onSelect,
  onNew,
  onDelete,
  isCollapsed,
  onToggle,
}: {
  conversations: OptimusConversation[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  isCollapsed: boolean;
  onToggle: () => void;
}) {
  const [deleteModal, setDeleteModal] = useState<{ open: boolean; id: string | null; title: string }>({
    open: false,
    id: null,
    title: "",
  });

  return (
    <div
      className={cn(
        "flex flex-col border-r border-[var(--border)] bg-[var(--bg-secondary)] transition-all",
        isCollapsed ? "w-12" : "w-56"
      )}
    >
      <div className="flex items-center gap-2 border-b border-[var(--border)] p-2">
        {!isCollapsed && (
          <button
            onClick={onNew}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-[var(--accent-green)] px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-[var(--accent-green-hover)]"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New Chat
          </button>
        )}
        <button
          onClick={onToggle}
          className="rounded-md p-1.5 text-[var(--text-muted)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]"
          title={isCollapsed ? "Expand" : "Collapse"}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d={isCollapsed ? "M9 18l6-6-6-6" : "M15 18l-6-6 6-6"} />
          </svg>
        </button>
      </div>

      {isCollapsed ? (
        <div className="flex flex-col items-center py-2">
          <button
            onClick={onNew}
            className="rounded-md p-2 text-[var(--text-muted)] hover:bg-[var(--bg-elev)] hover:text-[var(--accent-green)]"
            title="New Chat"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-2">
          {(!conversations || conversations.length === 0) ? (
            <div className="py-4 text-center text-xs text-[var(--text-muted)]">
              No conversations yet
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {conversations.map((conv) => (
                <div
                  key={conv.id}
                  onClick={() => onSelect(conv.id)}
                  className={cn(
                    "group flex cursor-pointer items-start justify-between rounded-lg px-2.5 py-2 transition-colors",
                    conv.id === currentId
                      ? "bg-[var(--accent-green)]/10 text-[var(--accent-green)]"
                      : "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                  )}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {conv.channel === "flock" && (
                        <span className="shrink-0 rounded bg-purple-100 px-1 py-0.5 text-[9px] font-semibold text-purple-700">
                          Flock
                        </span>
                      )}
                      <span className="truncate text-xs font-medium">{conv.title || "New conversation"}</span>
                    </div>
                    <div className="mt-0.5 text-[10px] text-[var(--text-muted)]">
                      {formatRelativeTime(conv.updated_at)}
                    </div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteModal({ open: true, id: conv.id, title: conv.title || "Untitled" });
                    }}
                    className="ml-1 rounded p-1 opacity-0 transition-opacity hover:bg-[var(--bg-secondary)] group-hover:opacity-100"
                    title="Delete"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Delete Modal */}
      {deleteModal.open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setDeleteModal({ open: false, id: null, title: "" })}>
          <div className="w-80 rounded-xl bg-[var(--bg-card)] p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Delete Conversation?</h3>
            <p className="mt-2 text-xs text-[var(--text-secondary)]">
              &quot;{deleteModal.title}&quot; will be permanently deleted.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setDeleteModal({ open: false, id: null, title: "" })}
                className="rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  if (deleteModal.id) onDelete(deleteModal.id);
                  setDeleteModal({ open: false, id: null, title: "" });
                }}
                className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Categorize documents by type based on filename patterns
function categorizeDocuments(docs: OptimusDocument[]): Record<string, OptimusDocument[]> {
  const categories: Record<string, OptimusDocument[]> = {
    "HR Policies": [],
    "Legal & Compliance": [],
    "Operations": [],
    "Other": [],
  };

  for (const doc of docs) {
    const name = doc.filename.toLowerCase();

    // HR-related documents
    if (
      name.includes("hr") ||
      name.includes("leave") ||
      name.includes("policy") ||
      name.includes("employee") ||
      name.includes("attendance") ||
      name.includes("posh") ||
      name.includes("workplace") ||
      name.includes("occupational") ||
      name.includes("health") ||
      name.includes("safety") ||
      name.includes("conduct") ||
      name.includes("grievance") ||
      name.includes("benefits")
    ) {
      categories["HR Policies"].push(doc);
    }
    // Legal/Compliance documents
    else if (
      name.includes("legal") ||
      name.includes("compliance") ||
      name.includes("contract") ||
      name.includes("agreement") ||
      name.includes("terms") ||
      name.includes("regulation")
    ) {
      categories["Legal & Compliance"].push(doc);
    }
    // Operations documents
    else if (
      name.includes("operation") ||
      name.includes("process") ||
      name.includes("sop") ||
      name.includes("procedure") ||
      name.includes("workflow")
    ) {
      categories["Operations"].push(doc);
    }
    // Everything else
    else {
      categories["Other"].push(doc);
    }
  }

  // Remove empty categories
  return Object.fromEntries(
    Object.entries(categories).filter(([, docs]) => docs.length > 0)
  );
}

function SourcesSidebar({
  documents,
  isCollapsed,
  onToggle,
}: {
  documents: OptimusDocument[];
  isCollapsed: boolean;
  onToggle: () => void;
}) {
  const docsList = documents || [];
  const categorized = categorizeDocuments(docsList);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(
    new Set(Object.keys(categorized))
  );

  const toggleCategory = (category: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  };

  // Category icons
  const categoryIcons: Record<string, string> = {
    "HR Policies": "👥",
    "Legal & Compliance": "⚖️",
    "Operations": "⚙️",
    "Other": "📄",
  };

  return (
    <div
      className={cn(
        "hidden flex-col border-l border-[var(--border)] bg-[var(--bg-secondary)] transition-all lg:flex",
        isCollapsed ? "w-12" : "w-60"
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-[var(--border)] p-2">
        <button
          onClick={onToggle}
          className="rounded-md p-1.5 text-[var(--text-muted)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]"
          title={isCollapsed ? "Expand" : "Collapse"}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d={isCollapsed ? "M15 18l-6-6 6-6" : "M9 18l6-6-6-6"} />
          </svg>
        </button>
        {!isCollapsed && (
          <div className="min-w-0 flex-1">
            <h3 className="text-xs font-semibold text-[var(--text-primary)]">Sources</h3>
            <p className="text-[10px] text-[var(--text-muted)]">{docsList.length} documents</p>
          </div>
        )}
      </div>

      {/* Collapsed state */}
      {isCollapsed ? (
        <div className="flex flex-col items-center gap-2 py-3">
          <div
            className="rounded-md p-2 text-[var(--text-muted)]"
            title={`${docsList.length} documents indexed`}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
            </svg>
          </div>
          <span className="text-[10px] font-medium text-[var(--text-muted)]">{docsList.length}</span>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-2">
          {docsList.length === 0 ? (
            <p className="py-4 text-center text-xs text-[var(--text-muted)]">No documents indexed yet</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {Object.entries(categorized).map(([category, categoryDocs]) => (
                <div key={category} className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] overflow-hidden">
                  {/* Category header */}
                  <button
                    onClick={() => toggleCategory(category)}
                    className="flex w-full items-center gap-2 px-2.5 py-2 text-left hover:bg-[var(--bg-elev)] transition-colors"
                  >
                    <span className="text-sm">{categoryIcons[category] || "📁"}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-semibold text-[var(--text-primary)]">{category}</div>
                      <div className="text-[10px] text-[var(--text-muted)]">{categoryDocs.length} documents</div>
                    </div>
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      className={cn(
                        "shrink-0 text-[var(--text-muted)] transition-transform",
                        expandedCategories.has(category) && "rotate-180"
                      )}
                    >
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </button>

                  {/* Documents list */}
                  {expandedCategories.has(category) && (
                    <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)]">
                      {categoryDocs.map((doc) => (
                        <div
                          key={doc.id}
                          className="border-b border-[var(--border)] last:border-b-0 px-2.5 py-2 hover:bg-[var(--bg-elev)] transition-colors"
                          title={doc.filename}
                        >
                          <div className="truncate text-[11px] font-medium text-[var(--text-primary)]">
                            {doc.filename.replace(/\.(pdf|docx|txt|md)$/i, "")}
                          </div>
                          <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-[var(--text-muted)]">
                            <span>{doc.chunk_count} chunks</span>
                            {doc.page_count && (
                              <>
                                <span>·</span>
                                <span>{doc.page_count} pages</span>
                              </>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MessageBubble({
  msg,
  onRetry,
  onSuggestionClick,
}: {
  msg: Message;
  onRetry?: () => void;
  onSuggestionClick?: (suggestion: string) => void;
}) {
  const isUser = msg.role === "user";
  const [expandedSources, setExpandedSources] = useState<Set<number>>(new Set());

  const toggleSource = (index: number) => {
    setExpandedSources((prev) => {
      const next = new Set(prev);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  };

  return (
    <div className={cn("flex gap-3", isUser && "flex-row-reverse")}>
      <div
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-bold",
          isUser
            ? "bg-[var(--accent-green)] text-white"
            : msg.needsClarification
              ? "bg-amber-500 text-white"
              : "bg-gradient-to-br from-violet-500 to-purple-600 text-white"
        )}
      >
        {isUser ? "U" : msg.needsClarification ? "?" : "O"}
      </div>
      <div className="max-w-[90%] min-w-0">
        <div className={cn("mb-1 flex items-center gap-2 text-xs text-[var(--text-muted)]", isUser && "justify-end")}>
          <span>{isUser ? "You" : "Optimus"}</span>
          {msg.needsClarification && (
            <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
              Clarification needed
            </span>
          )}
          {msg.timestamp && <span>{formatRelativeTime(msg.timestamp)}</span>}
        </div>
        <div
          className={cn(
            "rounded-2xl px-4 py-2.5 text-sm",
            isUser
              ? "bg-[var(--accent-green)] text-white"
              : msg.isError
                ? "bg-red-100 text-red-800"
                : msg.needsClarification
                  ? "bg-amber-50 border border-amber-200 text-[var(--text-primary)]"
                  : "bg-[var(--bg-card)] text-[var(--text-primary)]"
          )}
        >
          {isUser ? (
            <div className="whitespace-pre-wrap">{msg.content}</div>
          ) : (
            <div className="smartqna-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            </div>
          )}
        </div>
        {/* Clickable suggestions for clarification */}
        {msg.needsClarification && msg.suggestions && msg.suggestions.length > 0 && (
          <div className="mt-2 space-y-1.5">
            <div className="text-xs font-medium text-[var(--text-secondary)]">
              Click a topic to ask about it:
            </div>
            <div className="flex flex-wrap gap-1.5">
              {msg.suggestions.map((suggestion, i) => (
                <button
                  key={i}
                  onClick={() => onSuggestionClick?.(suggestion)}
                  className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 transition-colors hover:bg-amber-100 hover:border-amber-400"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}
        {msg.isError && onRetry && (
          <button
            onClick={onRetry}
            className="mt-1.5 flex items-center gap-1 text-xs text-red-600 hover:text-red-700"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M1 4v6h6M23 20v-6h-6" />
              <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15" />
            </svg>
            Retry
          </button>
        )}
        {msg.citations && msg.citations.length > 0 && (
          <div className="mt-3 space-y-2">
            <div className="flex items-center gap-1.5 text-xs font-medium text-[var(--text-secondary)]">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
              </svg>
              Sources ({msg.citations.length})
            </div>
            <div className="space-y-1.5">
              {msg.citations.map((c, i) => {
                // Build section indicator based on level
                const levelLabel = c.section_level && c.section_level > 0
                  ? c.section_level === 1 ? "Section" : c.section_level === 2 ? "Sub-section" : "Sub-sub-section"
                  : null;

                return (
                  <div
                    key={i}
                    className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] overflow-hidden transition-all"
                  >
                    <button
                      onClick={() => toggleSource(i)}
                      className="flex w-full items-start justify-between gap-2 px-3 py-2 text-left hover:bg-[var(--bg-elev)]"
                    >
                      <div className="flex flex-col gap-1 min-w-0 flex-1">
                        {/* Document name with number badge */}
                        <div className="flex items-center gap-2">
                          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--accent-green)] text-[10px] font-bold text-white">
                            {i + 1}
                          </span>
                          <span className="text-xs font-medium text-[var(--text-primary)] truncate">
                            {c.document}
                          </span>
                        </div>
                        {/* Section info */}
                        {c.section_title && (
                          <div className="flex items-center gap-1.5 pl-7">
                            {levelLabel && (
                              <span className="text-[10px] text-[var(--accent-green)] font-medium">
                                {levelLabel}:
                              </span>
                            )}
                            <span className="text-[11px] text-[var(--text-secondary)] truncate">
                              {c.section_title}
                            </span>
                          </div>
                        )}
                      </div>
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        className={cn(
                          "shrink-0 mt-0.5 text-[var(--text-muted)] transition-transform",
                          expandedSources.has(i) && "rotate-180"
                        )}
                      >
                        <polyline points="6 9 12 15 18 9" />
                      </svg>
                    </button>
                    {expandedSources.has(i) && c.text && (
                      <div className="border-t border-[var(--border)] bg-[var(--bg-card)] px-3 py-2">
                        <p className="text-[11px] leading-relaxed text-[var(--text-secondary)] whitespace-pre-wrap">
                          {c.text}
                        </p>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function LoadingBubble() {
  return (
    <div className="flex gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-purple-600 text-sm font-bold text-white">
        O
      </div>
      <div className="max-w-[75%]">
        <div className="mb-1 text-xs text-[var(--text-muted)]">Optimus</div>
        <div className="flex gap-1 rounded-2xl bg-[var(--bg-card)] px-4 py-3">
          <span className="h-2 w-2 animate-bounce rounded-full bg-violet-400 [animation-delay:-0.3s]" />
          <span className="h-2 w-2 animate-bounce rounded-full bg-violet-400 [animation-delay:-0.15s]" />
          <span className="h-2 w-2 animate-bounce rounded-full bg-violet-400" />
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Page Component
// ─────────────────────────────────────────────────────────────────────────────

export default function OptimusPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [conversations, setConversations] = useState<OptimusConversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [documents, setDocuments] = useState<OptimusDocument[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sourcesSidebarCollapsed, setSourcesSidebarCollapsed] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Load initial data
  useEffect(() => {
    let cancelled = false;

    const init = async () => {
      try {
        // Load documents (wrapped in { documents: [...] })
        const docsResponse = await optimusListDocuments();
        if (!cancelled) setDocuments(docsResponse?.documents || []);

        // Load conversations (returns array directly)
        const convs = await optimusListConversations();
        const conversationsList = Array.isArray(convs) ? convs : [];
        if (!cancelled) setConversations(conversationsList);

        // Create new conversation if none exists
        if (!cancelled && conversationsList.length === 0) {
          const newConv = await optimusCreateConversation();
          setCurrentConversationId(newConv.id);
          setConversations([newConv]);
        } else if (!cancelled && conversationsList.length > 0) {
          setCurrentConversationId(conversationsList[0].id);
        }
      } catch (error) {
        console.error("Init error:", error);
        // Set empty arrays on error to prevent rendering issues
        if (!cancelled) {
          setConversations([]);
          setDocuments([]);
        }
      }
    };

    init();
    return () => { cancelled = true; };
  }, []);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Load sidebar states
  useEffect(() => {
    try {
      const savedLeft = localStorage.getItem("optimus-sidebar-collapsed");
      if (savedLeft) setSidebarCollapsed(savedLeft === "true");
      const savedRight = localStorage.getItem("optimus-sources-collapsed");
      if (savedRight) setSourcesSidebarCollapsed(savedRight === "true");
    } catch {}
  }, []);

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim() || isLoading || !currentConversationId) return;

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setIsLoading(true);

    try {
      const response = await optimusChat(text, currentConversationId);
      const assistantMessage: Message = {
        id: response.message_id,
        role: "assistant",
        content: response.answer,
        citations: response.citations,
        confidence: response.confidence,
        timestamp: new Date().toISOString(),
        needsClarification: response.needs_clarification,
        suggestions: response.suggestions,
      };
      setMessages((prev) => [...prev, assistantMessage]);

      // Refresh conversations list
      const convs = await optimusListConversations();
      setConversations(Array.isArray(convs) ? convs : []);
    } catch (error) {
      const errorMessage: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: `Error: ${error instanceof Error ? error.message : "Something went wrong"}`,
        timestamp: new Date().toISOString(),
        isError: true,
      };
      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  }, [currentConversationId, isLoading]);

  const handleRetry = useCallback((messageIndex: number) => {
    const userMessageIndex = messageIndex - 1;
    if (userMessageIndex >= 0 && messages[userMessageIndex]?.role === "user") {
      const userQuery = messages[userMessageIndex].content;
      setMessages((prev) => prev.slice(0, messageIndex));
      sendMessage(userQuery);
    }
  }, [messages, sendMessage]);

  const handleNewConversation = useCallback(async () => {
    try {
      const newConv = await optimusCreateConversation();
      setCurrentConversationId(newConv.id);
      setMessages([]);
      const convs = await optimusListConversations();
      setConversations(Array.isArray(convs) ? convs : []);
    } catch (error) {
      console.error("Failed to create conversation:", error);
    }
  }, []);

  const handleSelectConversation = useCallback(async (id: string) => {
    try {
      setCurrentConversationId(id);
      const msgs = await optimusGetConversationMessages(id);
      setMessages(
        msgs.messages.map((m) => ({
          id: m.id,
          role: m.role as "user" | "assistant",
          content: m.content,
          citations: m.citations,
          confidence: m.confidence ?? undefined,
          timestamp: m.created_at,
        }))
      );
    } catch (error) {
      console.error("Failed to load conversation:", error);
    }
  }, []);

  const handleDeleteConversation = useCallback(async (id: string) => {
    try {
      await optimusDeleteConversation(id);
      if (id === currentConversationId) {
        await handleNewConversation();
      } else {
        const convs = await optimusListConversations();
        setConversations(Array.isArray(convs) ? convs : []);
      }
    } catch (error) {
      console.error("Failed to delete conversation:", error);
    }
  }, [currentConversationId, handleNewConversation]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    sendMessage(input);
    setInput("");
  };

  const toggleSidebar = () => {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    try {
      localStorage.setItem("optimus-sidebar-collapsed", String(next));
    } catch {}
  };

  const toggleSourcesSidebar = () => {
    const next = !sourcesSidebarCollapsed;
    setSourcesSidebarCollapsed(next);
    try {
      localStorage.setItem("optimus-sources-collapsed", String(next));
    } catch {}
  };

  return (
    <div className="flex h-full min-h-0 bg-[var(--bg-primary)]">
      <ConversationsSidebar
        conversations={conversations}
        currentId={currentConversationId}
        onSelect={handleSelectConversation}
        onNew={handleNewConversation}
        onDelete={handleDeleteConversation}
        isCollapsed={sidebarCollapsed}
        onToggle={toggleSidebar}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Header */}
        <div className="border-b border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-xs font-semibold text-violet-700">
              knowledge
            </span>
            <h1 className="text-sm font-semibold text-[var(--text-primary)]">
              Ask the company&apos;s documents
            </h1>
          </div>
        </div>

        {/* Messages area */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {messages.length === 0 ? (
            <GreetingScreen onExampleClick={sendMessage} />
          ) : (
            <div className="mx-auto max-w-5xl space-y-4">
              {messages.map((msg, idx) => (
                <MessageBubble
                  key={msg.id}
                  msg={msg}
                  onRetry={msg.isError ? () => handleRetry(idx) : undefined}
                  onSuggestionClick={(suggestion) => {
                    // Extract the topic from "Topic Name (Document Name)" format
                    const topic = suggestion.split("(")[0].trim();
                    sendMessage(`Tell me about ${topic}`);
                  }}
                />
              ))}
              {isLoading && <LoadingBubble />}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input */}
        <div className="border-t border-[var(--border)] bg-[var(--bg-secondary)] p-4">
          <form onSubmit={handleSubmit} className="mx-auto max-w-5xl">
            <div className="flex items-center gap-2 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 focus-within:border-[var(--accent-green)] focus-within:ring-2 focus-within:ring-[var(--accent-green)]/20">
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--text-muted)"
                strokeWidth="2"
              >
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
              <input
                type="text"
                className="flex-1 bg-transparent text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none"
                placeholder="Ask about any policy or document..."
                value={input}
                onChange={(e) => setInput(e.target.value)}
                disabled={isLoading}
              />
              <button
                type="submit"
                disabled={isLoading || !input.trim()}
                className="rounded-lg bg-[var(--accent-green)] px-4 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-[var(--accent-green-hover)] disabled:opacity-50"
              >
                {isLoading ? "Thinking..." : "Ask"}
              </button>
            </div>
          </form>
        </div>
      </div>

      <SourcesSidebar
        documents={documents}
        isCollapsed={sourcesSidebarCollapsed}
        onToggle={toggleSourcesSidebar}
      />
    </div>
  );
}
