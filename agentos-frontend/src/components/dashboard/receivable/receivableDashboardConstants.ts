/** Shared header styling for receivables tables (matches finance exec navy band). */
export const HEADER_BG = "bg-[#1a365d]";
export const HEADER_TEXT = "text-white";

export const tableShell =
  "overflow-hidden rounded-md border border-slate-200";

const thBase = `px-2 py-2 text-[10px] font-semibold text-white ${HEADER_BG}`;
/** Header cell: left (labels / first numeric column when aligned to label). */
export const th = `${thBase} text-left`;
/** Header cell: centered. */
export const thCenter = `${thBase} text-center`;
/** Header cell: right (amount columns). */
export const thRight = `${thBase} text-right`;
