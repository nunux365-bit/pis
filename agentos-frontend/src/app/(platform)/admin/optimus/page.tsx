"use client";

import { useState, useEffect, useCallback } from "react";
import {
  optimusListUsers,
  optimusGrantAccess,
  optimusRevokeAccess,
  optimusListDocuments,
  optimusUploadDocument,
  optimusDeleteDocument,
  optimusIngestDocuments,
  optimusToggleDocumentActive,
  optimusCleanupStuckDocuments,
  type OptimusUserAccess,
  type OptimusDocument,
} from "@/lib/optimusApi";

type Tab = "users" | "documents";

function cn(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

/** Check if user has explicit optimus_user role (for admin display purposes) */
const hasExplicitOptimusRole = (roles: string[]) =>
  roles.includes("system_admin") || roles.includes("optimus_user");

// ─────────────────────────────────────────────────────────────────────────────
// User Access Management
// ─────────────────────────────────────────────────────────────────────────────

function UserAccessTab() {
  const [users, setUsers] = useState<OptimusUserAccess[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [updating, setUpdating] = useState<string | null>(null);

  const loadUsers = useCallback(async (search: string) => {
    if (!search.trim()) { setUsers([]); return; }
    try {
      setLoading(true);
      const data = await optimusListUsers(search, showAll);
      setUsers(data.users || []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load users");
    } finally {
      setLoading(false);
    }
  }, [showAll]);

  const handleToggleAccess = async (user: OptimusUserAccess) => {
    // System admins have implicit access, can't toggle
    if ((user.roles || []).includes("system_admin")) return;

    setUpdating(user.id);
    try {
      if (hasExplicitOptimusRole(user.roles)) {
        await optimusRevokeAccess(user.id);
      } else {
        await optimusGrantAccess(user.id);
      }
      await loadUsers(filter);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update access");
    } finally {
      setUpdating(null);
    }
  };

  return (
    <div className="space-y-4">
      {/* Stats */}
      {users.length > 0 && (
        <div className="flex items-center gap-6 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-4">
          <div>
            <div className="text-2xl font-bold text-[var(--text-primary)]">{users.length}</div>
            <div className="text-xs text-[var(--text-muted)]">Results</div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3">
        <input
          type="text"
          placeholder="Search by name or email — press Enter to search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && loadUsers(filter)}
          className="flex-1 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent-green)] focus:outline-none"
        />
        <button
          onClick={() => loadUsers(filter)}
          disabled={loading}
          className="rounded-lg bg-[var(--accent-green)] px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          Search
        </button>
        <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => setShowAll(e.target.checked)}
            className="rounded"
          />
          Include inactive
        </label>
      </div>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Users table */}
      <div className="overflow-hidden rounded-lg border border-[var(--border)]">
        <table className="w-full">
          <thead className="bg-[var(--bg-secondary)]">
            <tr className="text-left text-xs font-medium text-[var(--text-muted)]">
              <th className="px-4 py-3">User</th>
              <th className="px-4 py-3">Department</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3 text-center">Optimus Access</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {!filter.trim() && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-sm text-[var(--text-muted)]">Type a name or email to search users</td></tr>
            )}
            {loading && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-sm text-[var(--text-muted)]">Searching…</td></tr>
            )}
            {!loading && filter.trim() && users.length === 0 && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-sm text-[var(--text-muted)]">No users found</td></tr>
            )}
            {users.map((user) => {
              const roles = user.roles || [];
              const isSystemAdmin = roles.includes("system_admin");
              // Get primary role for display (first non-optimus role, or first role)
              const displayRole = roles.find(r => r !== "optimus_user") || roles[0] || "employee";

              return (
                <tr
                  key={user.id}
                  className={cn(
                    "bg-[var(--bg-card)] transition-colors hover:bg-[var(--bg-elev)]",
                    !user.is_active && "opacity-50"
                  )}
                >
                  <td className="px-4 py-3">
                    <div className="text-sm font-medium text-[var(--text-primary)]">
                      {user.full_name}
                    </div>
                    <div className="text-xs text-[var(--text-muted)]">{user.email}</div>
                  </td>
                  <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                    {user.department}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          isSystemAdmin
                            ? "bg-purple-100 text-purple-700"
                            : displayRole === "dept_head"
                              ? "bg-blue-100 text-blue-700"
                              : "bg-gray-100 text-gray-700"
                        )}
                      >
                        {displayRole.replace(/_/g, " ")}
                      </span>
                      {roles.includes("optimus_user") && (
                        <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700">
                          optimus
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-center">
                    {isSystemAdmin ? (
                      <span className="text-xs text-[var(--text-muted)]">Always</span>
                    ) : (
                      <button
                        onClick={() => handleToggleAccess(user)}
                        disabled={updating === user.id}
                        className={cn(
                          "relative h-6 w-11 rounded-full transition-colors",
                          hasExplicitOptimusRole(user.roles)
                            ? "bg-[var(--accent-green)]"
                            : "bg-gray-300",
                          updating === user.id && "opacity-50"
                        )}
                      >
                        <span
                          className={cn(
                            "absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform",
                            hasExplicitOptimusRole(user.roles) ? "left-[22px]" : "left-0.5"
                          )}
                        />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {users.length === 0 && (
          <div className="py-8 text-center text-sm text-[var(--text-muted)]">
            No users found
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Document Management
// ─────────────────────────────────────────────────────────────────────────────

function DocumentsTab() {
  const [documents, setDocuments] = useState<OptimusDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [cleaningUp, setCleaningUp] = useState(false);
  const [deleteModal, setDeleteModal] = useState<{ open: boolean; doc: OptimusDocument | null }>({
    open: false,
    doc: null,
  });

  const loadDocuments = useCallback(async () => {
    try {
      setLoading(true);
      const data = await optimusListDocuments();
      setDocuments(data.documents || []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load documents");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDocuments();
  }, [loadDocuments]);

  // Auto-refresh to update status during ingestion
  useEffect(() => {
    const hasProcessing = documents.some((d) =>
      ["pending", "parsing", "chunking", "summarizing"].includes(d.status)
    );
    if (hasProcessing) {
      const interval = setInterval(loadDocuments, 3000);
      return () => clearInterval(interval);
    }
  }, [documents, loadDocuments]);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setUploading(true);
    setError(null);
    setSuccess(null);

    const fileArray = Array.from(files);
    const results: { name: string; success: boolean; error?: string }[] = [];

    for (const file of fileArray) {
      try {
        await optimusUploadDocument(file);
        results.push({ name: file.name, success: true });
      } catch (err) {
        results.push({
          name: file.name,
          success: false,
          error: err instanceof Error ? err.message : "Upload failed",
        });
      }
    }

    const successful = results.filter((r) => r.success);
    const failed = results.filter((r) => !r.success);

    if (successful.length > 0) {
      const names = successful.map((r) => `"${r.name}"`).join(", ");
      setSuccess(
        `${successful.length} file${successful.length > 1 ? "s" : ""} uploaded: ${names}. Select and click "Ingest Now" to process.`
      );
    }
    if (failed.length > 0) {
      const errors = failed.map((r) => `${r.name}: ${r.error}`).join("; ");
      setError(`Failed to upload ${failed.length} file${failed.length > 1 ? "s" : ""}: ${errors}`);
    }

    await loadDocuments();
    setUploading(false);
    e.target.value = "";
  };

  const handleIngestSelected = async () => {
    if (selectedIds.size === 0) return;

    setIngesting(true);
    setError(null);
    setSuccess(null);

    try {
      const result = await optimusIngestDocuments(Array.from(selectedIds));
      setSuccess(result.message);
      setSelectedIds(new Set());
      // Refresh to show status changes
      setTimeout(() => loadDocuments(), 500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start ingestion");
    } finally {
      setIngesting(false);
    }
  };

  const handleToggleActive = async (doc: OptimusDocument) => {
    setTogglingId(doc.id);
    setError(null);

    try {
      await optimusToggleDocumentActive(doc.id);
      await loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to toggle document status");
    } finally {
      setTogglingId(null);
    }
  };

  const handleDelete = async () => {
    if (!deleteModal.doc) return;

    try {
      await optimusDeleteDocument(deleteModal.doc.id);
      setDeleteModal({ open: false, doc: null });
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(deleteModal.doc!.id);
        return next;
      });
      await loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete document");
    }
  };

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds(new Set(ingestableDocuments.map((d) => d.id)));
    } else {
      setSelectedIds(new Set());
    }
  };

  const handleSelectOne = (id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  };

  const handleCleanupStuck = async () => {
    setCleaningUp(true);
    setError(null);
    setSuccess(null);

    try {
      const result = await optimusCleanupStuckDocuments();
      if (result.cleaned_count > 0) {
        setSuccess(result.message);
      } else {
        setSuccess("No stuck documents found.");
      }
      await loadDocuments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to cleanup stuck documents");
    } finally {
      setCleaningUp(false);
    }
  };

  // Documents that can be ingested (uploaded or failed)
  const ingestableDocuments = documents.filter((d) =>
    ["uploaded", "failed"].includes(d.status)
  );
  const selectedIngestable = Array.from(selectedIds).filter((id) =>
    ingestableDocuments.some((d) => d.id === id)
  );
  // Documents stuck in processing states
  const stuckDocuments = documents.filter((d) =>
    ["pending", "parsing", "chunking", "summarizing"].includes(d.status)
  );

  const statusConfig: Record<string, { label: string; color: string }> = {
    uploaded: { label: "Uploaded", color: "bg-gray-100 text-gray-700" },
    pending: { label: "Pending", color: "bg-yellow-100 text-yellow-700" },
    parsing: { label: "Parsing", color: "bg-blue-100 text-blue-700" },
    chunking: { label: "Chunking", color: "bg-blue-100 text-blue-700" },
    summarizing: { label: "Summarizing", color: "bg-blue-100 text-blue-700" },
    ingested: { label: "Ingested", color: "bg-green-100 text-green-700" },
    failed: { label: "Failed", color: "bg-red-100 text-red-700" },
  };

  if (loading && documents.length === 0) {
    return (
      <div className="flex items-center justify-center py-12 text-sm text-[var(--text-muted)]">
        Loading documents...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Upload section */}
      <div className="flex items-center gap-4 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-4">
        <div className="rounded-full bg-[var(--bg-secondary)] p-2">
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="var(--text-muted)"
            strokeWidth="2"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="17 8 12 3 7 8" />
            <line x1="12" y1="3" x2="12" y2="15" />
          </svg>
        </div>
        <div className="flex-1">
          <p className="text-sm font-medium text-[var(--text-primary)]">
            Upload documents to knowledge base
          </p>
          <p className="text-xs text-[var(--text-muted)]">
            PDF, DOCX, TXT, or MD files. Select multiple files at once.
          </p>
        </div>
        <label
          className={cn(
            "cursor-pointer rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2 text-sm font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-elev)]",
            uploading && "pointer-events-none opacity-50"
          )}
        >
          {uploading ? "Uploading..." : "Upload Files"}
          <input
            type="file"
            accept=".pdf,.docx,.txt,.md"
            multiple
            onChange={handleUpload}
            disabled={uploading}
            className="hidden"
          />
        </label>
      </div>

      {/* Success/Error messages */}
      {success && (
        <div className="rounded-lg border border-green-300 bg-green-50 p-3 text-sm text-green-700">
          {success}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Action bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium text-[var(--text-primary)]">
            Documents ({documents.length})
          </h3>
          {selectedIngestable.length > 0 && (
            <span className="text-xs text-[var(--text-muted)]">
              {selectedIngestable.length} selected for ingestion
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleIngestSelected}
            disabled={selectedIngestable.length === 0 || ingesting}
            className={cn(
              "rounded-lg px-4 py-2 text-sm font-medium transition-colors",
              selectedIngestable.length > 0 && !ingesting
                ? "bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green-hover)]"
                : "cursor-not-allowed bg-gray-200 text-gray-500"
            )}
          >
            {ingesting ? "Ingesting..." : `Ingest Now${selectedIngestable.length > 0 ? ` (${selectedIngestable.length})` : ""}`}
          </button>
          {stuckDocuments.length > 0 && (
            <button
              onClick={handleCleanupStuck}
              disabled={cleaningUp}
              className={cn(
                "rounded-lg border border-yellow-400 bg-yellow-50 px-3 py-2 text-xs font-medium text-yellow-700 transition-colors hover:bg-yellow-100",
                cleaningUp && "opacity-50"
              )}
              title="Reset documents stuck in processing state"
            >
              {cleaningUp ? "Cleaning..." : `Cleanup Stuck (${stuckDocuments.length})`}
            </button>
          )}
          <button
            onClick={loadDocuments}
            className="rounded-lg border border-[var(--border)] px-3 py-2 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Documents table */}
      {documents.length === 0 ? (
        <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-8 text-center text-sm text-[var(--text-muted)]">
          No documents yet. Upload a document to get started.
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-[var(--border)]">
          <table className="w-full">
            <thead className="bg-[var(--bg-secondary)]">
              <tr className="text-left text-xs font-medium text-[var(--text-muted)]">
                <th className="w-10 px-4 py-3">
                  <input
                    type="checkbox"
                    checked={ingestableDocuments.length > 0 && selectedIngestable.length === ingestableDocuments.length}
                    onChange={(e) => handleSelectAll(e.target.checked)}
                    disabled={ingestableDocuments.length === 0}
                    className="rounded"
                    title="Select all ingestable documents"
                  />
                </th>
                <th className="px-4 py-3">Document</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Uploaded</th>
                <th className="px-4 py-3">Ingested</th>
                <th className="px-4 py-3 text-center">Active</th>
                <th className="w-16 px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {documents.map((doc) => {
                const canIngest = ["uploaded", "failed"].includes(doc.status);
                const isProcessing = ["pending", "parsing", "chunking", "summarizing"].includes(doc.status);
                const config = statusConfig[doc.status] || { label: doc.status, color: "bg-gray-100 text-gray-700" };

                return (
                  <tr
                    key={doc.id}
                    className={cn(
                      "bg-[var(--bg-card)] transition-colors hover:bg-[var(--bg-elev)]",
                      !doc.is_active && "opacity-60"
                    )}
                  >
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(doc.id)}
                        onChange={(e) => handleSelectOne(doc.id, e.target.checked)}
                        disabled={!canIngest}
                        className={cn("rounded", !canIngest && "opacity-30")}
                      />
                    </td>
                    <td className="px-4 py-3">
                      <div className="text-sm font-medium text-[var(--text-primary)]">
                        {doc.filename}
                      </div>
                      <div className="text-xs text-[var(--text-muted)]">
                        {doc.chunk_count > 0 ? `${doc.chunk_count} chunks` : "Not processed"}
                        {doc.page_count ? ` · ${doc.page_count} pages` : ""}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={cn(
                          "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-medium",
                          config.color
                        )}
                      >
                        {isProcessing && (
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
                        )}
                        {config.label}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {new Date(doc.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {doc.ingested_at
                        ? new Date(doc.ingested_at).toLocaleDateString()
                        : "—"}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {doc.status === "ingested" ? (
                        <button
                          onClick={() => handleToggleActive(doc)}
                          disabled={togglingId === doc.id}
                          className={cn(
                            "relative h-6 w-11 rounded-full transition-colors",
                            doc.is_active
                              ? "bg-[var(--accent-green)]"
                              : "bg-gray-300",
                            togglingId === doc.id && "opacity-50"
                          )}
                          title={doc.is_active ? "Disable in retrieval" : "Enable in retrieval"}
                        >
                          <span
                            className={cn(
                              "absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform",
                              doc.is_active ? "left-[22px]" : "left-0.5"
                            )}
                          />
                        </button>
                      ) : (
                        <span className="text-xs text-[var(--text-muted)]">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => setDeleteModal({ open: true, doc })}
                        className="rounded p-1.5 text-[var(--text-muted)] transition-colors hover:bg-red-100 hover:text-red-600"
                        title="Delete document"
                      >
                        <svg
                          width="16"
                          height="16"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                        >
                          <polyline points="3 6 5 6 21 6" />
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Delete Modal */}
      {deleteModal.open && deleteModal.doc && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => setDeleteModal({ open: false, doc: null })}
        >
          <div
            className="w-96 rounded-xl bg-[var(--bg-card)] p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">
              Delete Document?
            </h3>
            <p className="mt-2 text-xs text-[var(--text-secondary)]">
              &quot;{deleteModal.doc.filename}&quot; and all its chunks will be permanently deleted
              from the knowledge base.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setDeleteModal({ open: false, doc: null })}
                className="rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
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

// ─────────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────────────

export default function OptimusAdminPage() {
  const [activeTab, setActiveTab] = useState<Tab>("users");

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-xl font-bold text-[var(--text-primary)]">Optimus Administration</h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Manage user access and document knowledge base
        </p>
      </div>

      {/* Tabs */}
      <div className="mb-6 flex gap-1 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-1">
        <button
          onClick={() => setActiveTab("users")}
          className={cn(
            "flex-1 rounded-md px-4 py-2 text-sm font-medium transition-colors",
            activeTab === "users"
              ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm"
              : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          )}
        >
          User Access
        </button>
        <button
          onClick={() => setActiveTab("documents")}
          className={cn(
            "flex-1 rounded-md px-4 py-2 text-sm font-medium transition-colors",
            activeTab === "documents"
              ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm"
              : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          )}
        >
          Documents
        </button>
      </div>

      {/* Tab content */}
      {activeTab === "users" && <UserAccessTab />}
      {activeTab === "documents" && <DocumentsTab />}
    </div>
  );
}
