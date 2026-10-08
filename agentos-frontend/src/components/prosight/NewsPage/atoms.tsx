/**
 * Small UI components (atoms) for NewsPage - Prosight N Dashboard
 */

import type { ReactNode } from "react";

// ══════════════════════════════════════════════════════════════════════════════
// Filter Section Wrapper
// ══════════════════════════════════════════════════════════════════════════════

interface FSProps {
  title: string;
  children: ReactNode;
}

export function FS({ title, children }: FSProps) {
  return (
    <div className="border-b border-[#e5e7eb] flex-shrink-0">
      <div className="px-3 py-1.5 bg-[#f3f4f6] border-b border-[#e5e7eb]">
        <span className="text-[9px] font-bold uppercase tracking-[2px] text-[#4b5563]">
          {title}
        </span>
      </div>
      <div className="p-3">{children}</div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Label
// ══════════════════════════════════════════════════════════════════════════════

interface LProps {
  children: ReactNode;
}

export function L({ children }: LProps) {
  return (
    <label className="text-[10px] text-[#6b7280] block mb-0.5">{children}</label>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Select Dropdown
// ══════════════════════════════════════════════════════════════════════════════

interface SelProps {
  value: string;
  opts: string[];
  onChange: (v: string) => void;
}

export function Sel({ value, opts, onChange }: SelProps) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full text-[10px] p-1 border border-[#d1d5db] rounded bg-white mb-1"
    >
      {opts.map((d) => (
        <option key={d} value={d}>
          {d}
        </option>
      ))}
    </select>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Radio Button
// ══════════════════════════════════════════════════════════════════════════════

interface RadProps {
  label: string;
  checked: boolean;
  onChange: () => void;
}

export function Rad({ label, checked, onChange }: RadProps) {
  return (
    <label className="flex items-center gap-2 cursor-pointer mb-1">
      <input
        type="radio"
        checked={checked}
        onChange={onChange}
        className="w-3 h-3 accent-[#2563eb] flex-shrink-0"
      />
      <span
        className={`text-[11px] ${checked ? "text-[#111827] font-semibold" : "text-[#4b5563]"}`}
      >
        {label}
      </span>
    </label>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Checkbox
// ══════════════════════════════════════════════════════════════════════════════

interface ChkProps {
  label: string;
  checked: boolean;
  onChange: () => void;
}

export function Chk({ label, checked, onChange }: ChkProps) {
  return (
    <label className="flex items-center gap-2 cursor-pointer mb-1">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="w-3 h-3 accent-[#2563eb] flex-shrink-0"
      />
      <span
        className={`text-[11px] ${checked ? "text-[#111827] font-semibold" : "text-[#4b5563]"}`}
      >
        {label}
      </span>
    </label>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Metrics Display
// ══════════════════════════════════════════════════════════════════════════════

interface MetsProps {
  title: string;
  rows: [string, string, string][];
}

export function Mets({ title, rows }: MetsProps) {
  return (
    <div className="p-2 px-3">
      <p className="text-[9px] uppercase tracking-[1px] text-[#9ca3af] mb-1 font-semibold">
        {title}
      </p>
      {rows.map(([k, v, c]) => (
        <div
          key={k}
          className="flex justify-between gap-2 text-[10px] mb-0.5"
        >
          <span className="text-[#9ca3af]">{k}</span>
          <span className="font-mono text-right" style={{ color: c || "#374151" }}>
            {v}
          </span>
        </div>
      ))}
    </div>
  );
}
