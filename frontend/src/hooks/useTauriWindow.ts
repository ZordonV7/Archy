/**
 * Tauri window management hooks.
 *
 * CRITICAL: The `@tauri-apps/api` imports MUST be dynamic (lazy), not static.
 * In dev mode (vite serve), static `import { invoke } from "@tauri-apps/api/core"`
 * fails to resolve in the browser because vite serves modules individually
 * and the bare specifier isn't rewritten. This causes a module-resolution
 * error that prevents the entire app from mounting (blank white page).
 *
 * Dynamic imports (`await import(...)`) defer the resolution to runtime,
 * where they only execute if we're actually in Tauri. In a pure browser,
 * they're never called, so they never fail.
 *
 * In build mode (vite build), the bundler resolves everything at build time
 * so static imports would work — but we keep them dynamic for dev-mode
 * compatibility and smaller initial bundle.
 */

/// Check if we're running in Tauri (not browser).
/// Uses a feature-detect that works WITHOUT importing the tauri module:
/// Tauri injects `window.__TAURI_INTERNALS__` (v2) or `window.__TAURI__` (v1).
export async function isTauri(): Promise<boolean> {
  try {
    if (typeof window === "undefined") return false;
    // Tauri v2 sets window.__TAURI_INTERNALS__; v1 sets window.__TAURI__
    return !!(window as any).__TAURI_INTERNALS__ || !!(window as any).__TAURI__;
  } catch {
    return false;
  }
}

/// Get the current window label (mascot / bubble / dashboard)
export async function getCurrentWindowLabel(): Promise<string> {
  if (await isTauri()) {
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      return getCurrentWindow().label;
    } catch {
      return "unknown";
    }
  }
  // In browser, derive from URL hash
  const hash = window.location.hash;
  if (hash.includes("dashboard")) return "dashboard";
  if (hash.includes("bubble")) return "bubble";
  return "mascot";
}

/**
 * Toggle dashboard visibility.
 *
 * - Tauri (desktop): invokes the Rust `toggle_dashboard` command which
 *   shows/hides the dashboard OS window.
 * - Web (browser): toggles the `dashboardOpen` flag in the Zustand store,
 *   which slides the dashboard panel in/out via CSS.
 */
export async function toggleDashboard(): Promise<boolean> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<boolean>("toggle_dashboard");
    } catch (e) {
      console.error("[Tauri] toggle_dashboard failed:", e);
      return false;
    }
  }
  // Browser fallback: toggle the Zustand store directly.
  const { useEventStore } = await import("../stores/eventStore");
  const current = useEventStore.getState().dashboardOpen;
  useEventStore.getState().setDashboardOpen(!current);
  window.dispatchEvent(new CustomEvent("archy-toggle-dashboard"));
  return true;
}

/// Show dashboard window
export async function showDashboard(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("show_dashboard");
    } catch (e) {
      console.error("[Tauri] show_dashboard failed:", e);
    }
  } else {
    const { useEventStore } = await import("../stores/eventStore");
    useEventStore.getState().setDashboardOpen(true);
    window.dispatchEvent(new CustomEvent("archy-show-dashboard"));
  }
}

/// Hide dashboard window
export async function hideDashboard(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("hide_dashboard");
    } catch (e) {
      console.error("[Tauri] hide_dashboard failed:", e);
    }
  } else {
    const { useEventStore } = await import("../stores/eventStore");
    useEventStore.getState().setDashboardOpen(false);
    window.dispatchEvent(new CustomEvent("archy-hide-dashboard"));
  }
}

/// Minimize mascot to tray
export async function minimizeToTray(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("minimize_to_tray");
    } catch (e) {
      console.error("[Tauri] minimize_to_tray failed:", e);
    }
  } else {
    window.dispatchEvent(new CustomEvent("archy-minimize"));
  }
}

/// Quit the app
export async function quitApp(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("quit_app");
    } catch (e) {
      console.error("[Tauri] quit_app failed:", e);
    }
  } else {
    window.close();
  }
}

/// Show the speech bubble window (Tauri only — in browser it's an overlay)
export async function showBubble(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("show_bubble");
    } catch (e) {
      console.error("[Tauri] show_bubble failed:", e);
    }
  }
  // In browser, the bubble is always rendered as an overlay — no action needed.
}

/// Hide the speech bubble window (Tauri only — in browser it auto-hides)
export async function hideBubble(): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("hide_bubble");
    } catch (e) {
      console.error("[Tauri] hide_bubble failed:", e);
    }
  }
  // In browser, the bubble auto-hides via its own timer — no action needed.
}

/// Position the bubble window (Tauri only — in browser it's positioned via CSS)
export async function positionBubble(_x: number, _y: number): Promise<void> {
  if (await isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("position_bubble", { x: _x, y: _y });
    } catch (e) {
      console.error("[Tauri] position_bubble failed:", e);
    }
  }
  // In browser, the bubble is positioned via CSS — no action needed.
}

/// Listen for dashboard window focus changes (Tauri only).
/// Returns an unsubscribe function, or null if not in Tauri.
export async function onDashboardFocusChange(
  callback: (focused: boolean) => void
): Promise<(() => void) | null> {
  if (!(await isTauri())) return null;
  try {
    const { getAllWindows } = await import("@tauri-apps/api/window");
    const windows = await getAllWindows();
    const dashboard = windows.find((w) => w.label === "dashboard");
    if (!dashboard) return null;

    const unlisten = await dashboard.onFocusChanged(({ event }) => {
      callback(event === "focus");
    });
    return unlisten;
  } catch (e) {
    console.error("[Tauri] onFocusChanged failed:", e);
    return null;
  }
}
