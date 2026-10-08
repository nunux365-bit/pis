/** Join Tailwind class fragments; falsy values are skipped. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Allow only http(s) links for user-facing anchors. */
export function isSafeHttpUrl(value: string | undefined | null): boolean {
  if (!value?.trim()) return false;
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
