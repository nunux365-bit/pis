/** Canonical finance requisition landing (S2P programs shell — all roles). */
export const PROCUREMENT_HOME_HREF = "/s2p/procurement" as const;

/** @deprecated Prefer ``PROCUREMENT_HOME_HREF`` — kept for short call sites that still pass role. */
export function procurementHomeHref(_userRole: string | undefined): string {
  return PROCUREMENT_HOME_HREF;
}
