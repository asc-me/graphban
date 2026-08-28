import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { api, hasSession } from "@/lib/api";
import type { User } from "@/lib/types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (
    name: string,
    email: string,
    handle: string,
    password: string,
    inviteToken?: string,
  ) => Promise<void>;
  completePasswordReset: (token: string, newPassword: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = React.createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = React.useState<User | null>(null);
  const [loading, setLoading] = React.useState(true);
  const qc = useQueryClient();

  React.useEffect(() => {
    let active = true;
    if (!hasSession()) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then((u) => active && setUser(u))
      .catch(() => active && setUser(null))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  const login = React.useCallback(async (email: string, password: string) => {
    const u = await api.login(email, password);
    setUser(u);
  }, []);

  const register = React.useCallback(
    async (name: string, email: string, handle: string, password: string, inviteToken?: string) => {
      const u = await api.register(name, email, handle, password, inviteToken);
      setUser(u);
    },
    [],
  );

  /** Spend a reset link and land signed in (GRPH-570). Mirrors `login`: the server has
   *  already revoked every other session and issued a fresh pair, so there is nothing left
   *  to authenticate with — sending the user to the login form afterwards would ask them to
   *  prove something they have just proved. */
  const completePasswordReset = React.useCallback(
    async (token: string, newPassword: string) => {
      const u = await api.confirmPasswordReset(token, newPassword);
      setUser(u);
    },
    [],
  );

  const logout = React.useCallback(() => {
    api.logout();
    setUser(null);
    qc.clear();
  }, [qc]);

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout, completePasswordReset }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
