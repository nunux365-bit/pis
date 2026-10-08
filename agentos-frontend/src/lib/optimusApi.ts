/**
 * Optimus SmartQnA API client
 */

import { apiJson, apiFetch } from "./api";

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

export interface OptimusConversation {
  id: string;
  title: string;
  service: string;
  channel: string;
  created_at: string;
  updated_at: string;
}

export interface OptimusMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: OptimusChatResponse["citations"];
  confidence?: string | null;
  created_at: string;
}

export interface OptimusChatResponse {
  message_id: string;
  conversation_id: string;
  answer: string;
  citations: Array<{
    document: string;
    text: string;
    score?: number;
    section_title?: string;
    section_level?: number;  // 1=section, 2=subsection, etc.
    doc_id?: string;
    type?: string;  // "chunk", "section_summary", "document_summary"
  }>;
  confidence: string;
  needs_clarification?: boolean;  // True when bot is asking for clarification
  suggestions?: string[];  // Suggested topics when clarifying
}

export interface OptimusDocument {
  id: string;
  filename: string;
  status: "uploaded" | "pending" | "parsing" | "chunking" | "summarizing" | "ingested" | "failed";
  chunk_count: number;
  page_count?: number;
  summary?: string;
  is_active: boolean;
  created_at: string;
  ingested_at?: string | null;
}

export interface OptimusDocumentsResponse {
  documents: OptimusDocument[];
}

export interface OptimusMessagesResponse {
  messages: OptimusMessage[];
}

// ─────────────────────────────────────────────────────────────────────────────
// Conversations API
// ─────────────────────────────────────────────────────────────────────────────

export async function optimusListConversations(limit = 50): Promise<OptimusConversation[]> {
  // Backend returns a list directly, not wrapped in { conversations: [...] }
  return apiJson<OptimusConversation[]>(
    `/api/optimus/smartqna/conversations?limit=${limit}`
  );
}

export async function optimusCreateConversation(title = "New conversation"): Promise<OptimusConversation> {
  // Backend endpoint is /conversations/new
  return apiJson<OptimusConversation>("/api/optimus/smartqna/conversations/new", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function optimusDeleteConversation(conversationId: string): Promise<void> {
  const res = await apiFetch(`/api/optimus/smartqna/conversations/${conversationId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const error = await res.text().catch(() => "");
    throw new Error(error || `Delete failed: ${res.status}`);
  }
}

export async function optimusGetConversationMessages(
  conversationId: string,
  limit = 100
): Promise<OptimusMessagesResponse> {
  return apiJson<OptimusMessagesResponse>(
    `/api/optimus/smartqna/conversations/${conversationId}/messages?limit=${limit}`
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat API
// ─────────────────────────────────────────────────────────────────────────────

export async function optimusChat(
  message: string,
  conversationId?: string
): Promise<OptimusChatResponse> {
  // Backend uses "message" field, not "query"
  return apiJson<OptimusChatResponse>("/api/optimus/smartqna/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      conversation_id: conversationId || null,
    }),
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Documents API
// ─────────────────────────────────────────────────────────────────────────────

export async function optimusListDocuments(): Promise<OptimusDocumentsResponse> {
  return apiJson<OptimusDocumentsResponse>("/api/optimus/smartqna/documents");
}

export async function optimusUploadDocument(file: File): Promise<{ status: string; filename: string; message: string }> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await apiFetch("/api/optimus/smartqna/documents/upload", {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || `Upload failed: ${response.status}`);
  }

  return response.json();
}

export async function optimusDeleteDocument(documentId: string): Promise<void> {
  const res = await apiFetch(`/api/optimus/smartqna/documents/${documentId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const error = await res.text().catch(() => "");
    throw new Error(error || `Delete failed: ${res.status}`);
  }
}

export async function optimusIngestDocuments(
  documentIds: string[]
): Promise<{ status: string; document_count: number; document_ids: string[]; message: string }> {
  return apiJson("/api/optimus/smartqna/documents/ingest", {
    method: "POST",
    body: JSON.stringify({ document_ids: documentIds }),
  });
}

export async function optimusToggleDocumentActive(
  documentId: string
): Promise<{ document_id: string; is_active: boolean; message: string }> {
  return apiJson(`/api/optimus/smartqna/documents/${documentId}/toggle-active`, {
    method: "POST",
  });
}

export async function optimusCleanupStuckDocuments(): Promise<{
  status: string;
  cleaned_count: number;
  document_ids: string[];
  message: string;
}> {
  return apiJson("/api/optimus/smartqna/documents/cleanup-stuck", {
    method: "POST",
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Library API (unified history)
// ─────────────────────────────────────────────────────────────────────────────

export interface OptimusLibraryItem {
  id: string;
  title: string;
  service: string;
  channel: string;
  created_at: string;
  updated_at: string;
}

export async function optimusListLibrary(options?: {
  limit?: number;
  offset?: number;
}): Promise<{ conversations: OptimusLibraryItem[]; total: number }> {
  const params = new URLSearchParams();
  if (options?.limit) params.set("limit", String(options.limit));
  if (options?.offset) params.set("offset", String(options.offset));

  const qs = params.toString();
  return apiJson(`/api/optimus/library/conversations${qs ? `?${qs}` : ""}`);
}

// ─────────────────────────────────────────────────────────────────────────────
// Admin API (System Admin only) - Optimus Access Control
// Access logic:
// - System admins (users.role = 'system_admin') have implicit access
// - Others need explicit grant in optimus_user_access table
// ─────────────────────────────────────────────────────────────────────────────

export interface OptimusUserAccess {
  id: string;
  email: string;
  full_name: string;
  department: string;
  roles: string[];  // User's roles - check for system_admin or optimus_user for access
  is_active: boolean;
}

export async function optimusListUsers(search = "", includeInactive = false): Promise<{ users: OptimusUserAccess[] }> {
  const params = new URLSearchParams({ include_inactive: String(includeInactive) });
  if (search.trim()) params.set("search", search.trim());
  return apiJson(`/api/optimus/admin/users?${params}`);
}

export async function optimusGrantAccess(userId: string): Promise<{ status: string; user_id?: string; message?: string }> {
  return apiJson(`/api/optimus/admin/users/${userId}/grant-access`, {
    method: "POST",
  });
}

export async function optimusRevokeAccess(userId: string): Promise<{ status: string; user_id: string }> {
  return apiJson(`/api/optimus/admin/users/${userId}/revoke-access`, {
    method: "POST",
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Flock Admin API
// ─────────────────────────────────────────────────────────────────────────────

export interface FlockConfig {
  enabled: boolean;
  webhook_url: string;
  app_id: string | null;
  bot_configured: boolean;
}

export interface FlockAccount {
  id: string;
  flock_user_id: string;
  flock_email: string | null;
  flock_name: string | null;
  user_id: string;
  user_email: string;
  user_full_name: string;
  linked_at: string;
}

export async function optimusGetFlockConfig(): Promise<FlockConfig> {
  return apiJson("/api/optimus/admin/flock/config");
}

export async function optimusListFlockAccounts(): Promise<{ accounts: FlockAccount[]; total: number }> {
  return apiJson("/api/optimus/admin/flock/accounts");
}

export async function optimusUnlinkFlockAccount(flockUserId: string): Promise<{ status: string }> {
  return apiJson(`/api/optimus/admin/flock/accounts/${encodeURIComponent(flockUserId)}`, {
    method: "DELETE",
  });
}
