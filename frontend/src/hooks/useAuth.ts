import { useState, useEffect, useCallback } from "react";
import { API_BASE } from "../lib/apiConfig";

export interface ArchyUser {
  id: string;
  email: string;
  name: string;
  picture: string;
}

interface AuthState {
  user: ArchyUser | null;
  require_auth: boolean;
  loading: boolean;       // true while checking /auth/me on mount
  error: string | null;   // set if auth check fails or URL has ?auth_error=
}

/**
 * useAuth — manages Google OAuth login state.
 *
 * On mount, checks /auth/me to see if the user is already logged in
 * (via the session cookie). If `require_auth` is true and no user is
 * logged in, the app should show the LoginScreen.
 *
 * Flow:
 * 1. User clicks "Sign in with Google" → call login()
 * 2. login() redirects the browser to `${API_BASE}/auth/google/login`
 * 3. Backend redirects to Google's consent screen
 * 4. User consents → Google redirects to backend `/auth/google/callback`
 * 5. Backend exchanges code, sets session cookie, redirects to frontend
 * 6. Frontend reloads → useAuth re-checks /auth/me → user is logged in
 *
 * The session cookie is httpOnly + signed, so JS can't read it directly.
 * credentials: 'include' on the fetch call is what sends it.
 */
export function useAuth(): AuthState & {
  login: () => void;
  logout: () => Promise<void>;
} {
  const [state, setState] = useState<AuthState>({
    user: null,
    require_auth: false,
    loading: true,
    error: null,
  });

  // Check for ?auth_error= or ?auth_success= in the URL (set by the OAuth callback)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authError = params.get("auth_error");
    const authSuccess = params.get("auth_success");
    if (authError) {
      setState((s) => ({ ...s, error: decodeURIComponent(authError), loading: false }));
      // Clean the URL so the error doesn't persist on refresh
      window.history.replaceState({}, "", window.location.pathname);
    } else if (authSuccess) {
      // Clean the URL — the /auth/me check below will pick up the session
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  // Check /auth/me on mount (and after auth_success redirect)
  const checkAuth = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/auth/me`, {
        credentials: "include",
      });
      if (!resp.ok) {
        setState((s) => ({ ...s, loading: false, user: null }));
        return;
      }
      const data = await resp.json();
      setState({
        user: data.user,
        require_auth: data.require_auth ?? false,
        loading: false,
        error: null,
      });
    } catch (e) {
      // Backend might be down — if require_auth is false, app still works
      setState((s) => ({ ...s, loading: false, error: String(e) }));
    }
  }, []);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const login = useCallback(() => {
    // Redirect the browser to the backend's OAuth start endpoint.
    // The backend will redirect to Google, then back here after callback.
    window.location.href = `${API_BASE}/auth/google/login`;
  }, []);

  const logout = useCallback(async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // ignore — we'll clear local state anyway
    }
    setState({ user: null, require_auth: state.require_auth, loading: false, error: null });
  }, [state.require_auth]);

  return { ...state, login, logout };
}
