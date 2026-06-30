import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { getCurrentWindowLabel, isTauri } from "./hooks/useTauriWindow";
import { useEventStore } from "./stores/eventStore";
import { useWebSocket } from "./hooks/useWebSocket";
import { useAuth } from "./hooks/useAuth";
import { getWebSocketUrl } from "./lib/apiConfig";
import MascotWindow from "./components/Mascot/MascotWindow";
import BubbleWindow from "./components/Mascot/BubbleWindow";
import DashboardWindow from "./components/Dashboard/DashboardWindow";
import LoginScreen from "./components/Auth/LoginScreen";
import { ErrorBoundary } from "./components/common/ErrorBoundary";

/**
 * App entrypoint.
 *
 * Two modes:
 * - **Tauri (desktop)**: Multi-window. Each OS window renders one component
 *   (mascot / bubble / dashboard) based on the Tauri window label.
 * - **Web (browser)**: Single-page. The three "windows" become overlays on
 *   one page — mascot floats bottom-right, bubble pops above it, dashboard
 *   slides in from the right when toggled. Selected automatically when
 *   `isTauri()` returns false.
 *
 * Auth: In web mode, if `require_auth` is true and the user is not logged in,
 * shows the LoginScreen instead of the app. Desktop mode skips auth entirely.
 */
export default function App() {
  const [windowLabel, setWindowLabel] = useState<string>("mascot");
  const [tauriMode, setTauriMode] = useState<boolean | null>(null);

  // Detect mode + window label on mount.
  useEffect(() => {
    (async () => {
      const tauri = await isTauri();
      setTauriMode(tauri);
      if (tauri) {
        const label = await getCurrentWindowLabel();
        setWindowLabel(label);
      }
      // In web mode, windowLabel stays "mascot" — we render everything.
    })();
  }, []);

  // Toggle the `web-mode` CSS class on <html> for browser-only styling.
  useEffect(() => {
    if (tauriMode === null) return; // still detecting
    if (!tauriMode) {
      document.documentElement.classList.add("web-mode");
      return () => document.documentElement.classList.remove("web-mode");
    }
  }, [tauriMode]);

  // All modes connect to the backend WebSocket.
  const wsUrl = getWebSocketUrl("/events");
  useWebSocket(wsUrl);

  // While we're detecting the mode, render nothing (avoids flicker).
  if (tauriMode === null) {
    return (
      <ErrorBoundary>
        <div style={{ width: "100vw", height: "100vh" }} />
      </ErrorBoundary>
    );
  }

  // --- Tauri (desktop) mode: multi-window, no auth gate ---
  if (tauriMode) {
    return (
      <ErrorBoundary>
        {windowLabel === "dashboard" && <DashboardWindow />}
        {windowLabel === "bubble" && <BubbleWindow />}
        {windowLabel === "mascot" && <MascotWindow />}
      </ErrorBoundary>
    );
  }

  // --- Web (browser) mode: single-page with overlays + auth gate ---
  return <WebApp />;
}

/** Single-page web layout with auth gate. */
function WebApp() {
  const { user, require_auth, loading, error, login } = useAuth();
  const dashboardOpen = useEventStore((s) => s.dashboardOpen);
  const setDashboardOpen = useEventStore((s) => s.setDashboardOpen);

  // Escape closes dashboard
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDashboardOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setDashboardOpen]);

  // While checking auth status, show a loading screen
  if (loading) {
    return (
      <ErrorBoundary>
        <div
          style={{
            position: "fixed",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background:
              "radial-gradient(circle at 15% 20%, rgba(139, 92, 246, 0.15), transparent 50%)," +
              "linear-gradient(180deg, #0f0a1f 0%, #1a0f2e 100%)",
            color: "#a5a0b8",
            fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
          }}
        >
          <motion.div
            animate={{ opacity: [0.4, 1, 0.4] }}
            transition={{ duration: 1.5, repeat: Infinity }}
          >
            Loading Archy...
          </motion.div>
        </div>
      </ErrorBoundary>
    );
  }

  // If auth is required and user is not logged in, show the login screen
  if (require_auth && !user) {
    return (
      <ErrorBoundary>
        <LoginScreen onLogin={login} error={error} />
      </ErrorBoundary>
    );
  }

  // Authenticated (or auth not required) — show the app
  return (
    <ErrorBoundary>
      <div className="web-app-root">
        {/* Backdrop — click anywhere outside dashboard to close */}
        <AnimatePresence>
          {dashboardOpen && (
            <motion.div
              className={`dashboard-backdrop ${dashboardOpen ? "open" : ""}`}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              onClick={() => setDashboardOpen(false)}
            />
          )}
        </AnimatePresence>

        {/* Dashboard panel — slides in from the right */}
        <div className={`dashboard-window ${dashboardOpen ? "open" : ""}`}>
          <DashboardWindow />
        </div>

        {/* Mascot — always visible bottom-right */}
        <MascotWindow />

        {/* Speech bubble — pops above mascot when there's a message */}
        <BubbleWindow />
      </div>
    </ErrorBoundary>
  );
}
