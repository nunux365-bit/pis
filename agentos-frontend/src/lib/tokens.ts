const ACCESS = "agentos_access_token";
const REFRESH = "agentos_refresh_token";

/** Drop legacy JWT keys from sessionStorage (session is HttpOnly cookies only). */
export function clearTokens() {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem(ACCESS);
  sessionStorage.removeItem(REFRESH);
}
