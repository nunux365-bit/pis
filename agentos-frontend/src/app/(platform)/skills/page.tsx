"use client";

import { useEffect, useState } from "react";
import { listSkills, type SkillCategory } from "@/lib/api";

export default function SkillsPage() {
  const [categories, setCategories] = useState<SkillCategory[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setErr(null);
    listSkills()
      .then((data) => setCategories(data.categories ?? []))
      .catch((e) => {
        setCategories([]);
        setErr(e instanceof Error ? e.message : "Failed to load skills");
      });
  }, []);

  const colors: Record<string, string> = {
    Finance: "#3b82f6",
    HR: "#8b5cf6",
    "Supply Chain": "#10b981",
    IT: "#06b6d4",
    Admin: "#f59e0b",
    "Cross-Cutting": "#ef4444",
  };

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Skill library</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Versioned skills from the platform catalog (PostgreSQL). Boundaries and
        owners can evolve per environment without code deploys.
      </p>

      {err && (
        <p className="text-sm text-[var(--accent-red)] mb-4" role="alert">
          {err}
        </p>
      )}

      {categories.length === 0 && !err && (
        <p className="text-sm text-[var(--text-muted)] mb-6">
          No skill categories found. Run migrations and restart the API so
          bootstrap can seed{" "}
          <code className="text-xs font-mono">catalog_skill_*</code> tables.
        </p>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {categories.map((cat) => {
          const color = colors[cat.name] ?? "#64748b";
          const isOpen = expanded[cat.name];
          const visible = isOpen ? cat.skills : cat.skills.slice(0, 4);
          return (
            <div
              key={cat.name}
              className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5"
            >
              <div className="flex justify-between items-center mb-3">
                <div className="flex items-center gap-2">
                  <div
                    className="w-2.5 h-2.5 rounded"
                    style={{ background: color }}
                  />
                  <span className="font-semibold">{cat.name}</span>
                </div>
                <span className="text-[10px] font-semibold px-2.5 py-1 rounded-full bg-[var(--bg-secondary)]">
                  {cat.count} skills
                </span>
              </div>
              <div className="flex flex-col gap-1.5">
                {visible.map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center justify-between gap-2 text-[11px] bg-[var(--bg-secondary)] px-2.5 py-2 rounded-lg"
                  >
                    <span className="text-[var(--text-primary)] font-medium">
                      {s.title}
                    </span>
                    <span className="text-[var(--text-muted)] font-mono shrink-0">
                      v{s.version}
                    </span>
                  </div>
                ))}
              </div>
              {cat.skills.length > 4 && (
                <button
                  type="button"
                  className="mt-2 text-xs text-[var(--accent-blue)] font-medium hover:underline"
                  onClick={() =>
                    setExpanded((e) => ({
                      ...e,
                      [cat.name]: !e[cat.name],
                    }))
                  }
                >
                  {isOpen ? "Show less" : `Show all ${cat.skills.length}`}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
