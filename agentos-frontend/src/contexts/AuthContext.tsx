"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  loadCurrentUser,
  loginRequest,
  logoutRequest,
  type UserMe,
} from "@/lib/api";
import { clearTokens } from "@/lib/tokens";

type AuthState = {
  user: UserMe | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
};

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserMe | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    clearTokens();
  }, []);

  const refreshUser = useCallback(async () => {
    const result = await loadCurrentUser();
    if (result.ok) {
      setUser(result.user);
    } else if (result.clearSession) {
      clearTokens();
      setUser(null);
    }
    /* Transient /me failure: keep prior user so a blip does not log everyone out. */
    setLoading(false);
  }, []);

  useEffect(() => {
    refreshUser();

    // Periodically refresh user profile every 5 minutes to detect role additions/revocations
    const interval = setInterval(() => {
      refreshUser();
    }, 5 * 60 * 1000);

    // Refresh user profile when switching back to tab/window
    const onFocus = () => {
      refreshUser();
    };
    window.addEventListener("focus", onFocus);

    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", onFocus);
    };
  }, [refreshUser]);

  const login = useCallback(async (email: string, password: string) => {
    await loginRequest(email, password);
    const result = await loadCurrentUser();
    if (!result.ok) {
      clearTokens();
      setUser(null);
      throw new Error(
        result.message || "Could not load your profile after sign-in."
      );
    }
    setUser(result.user);
  }, []);

  const logout = useCallback(async () => {
    await logoutRequest();
    setUser(null);
    if (typeof window !== "undefined") window.location.href = "/login";
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, logout, refreshUser }),
    [user, loading, login, logout, refreshUser]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
