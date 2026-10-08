/** Stable DOM id for procurement form fields — used for labels, validation anchors, and error-summary links. */
export function procurementFieldDomId(kind: string, documentType: string, suffix: string): string {
  return `proc-f-${kind}-${documentType}-${suffix}`.replace(/[^a-zA-Z0-9_-]/g, "_");
}
