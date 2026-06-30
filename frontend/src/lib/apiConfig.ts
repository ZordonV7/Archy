/**
 * Central API configuration for the Archy frontend.
 *
 * Resolution order (first non-empty wins):
 *   1. `VITE_API_BASE_URL` env var (set at build time)
 *      - Use this when the backend is on a different host (e.g. Render) and
 *        you want to call it cross-origin. CORS must be configured on the
 *        backend (ARCHY_ALLOWED_ORIGINS) to match your Vercel URL.
 *   2. Same-origin `/api`
 *      - Used by default on Vercel. The frontend's `vercel.json` rewrites
 *        `/api/*` to the backend host, so the browser sees same-origin
 *        requests and CORS is never triggered for HTTP.
 *        NOTE: Vercel's edge rewrites do NOT proxy WebSocket upgrades, so
 *        the WebSocket connection goes DIRECT to the Render backend (see
 *        VITE_WS_BASE_URL below).
 *   3. `http://127.0.0.1:8000`
 *      - Local dev / Tauri desktop default.
 *
 * For Tauri desktop: leave `VITE_API_BASE_URL` unset. The desktop app calls
 * the backend at localhost:8000 (you run `archy serve` locally).
 *
 * WebSocket URL resolution:
 *   - If `VITE_WS_BASE_URL` is set, use it verbatim + path.
 *     Example: VITE_WS_BASE_URL=wss://archy-backend.onrender.com
 *   - Else if API_BASE is a full URL (http/https), derive ws/wss from it.
 *   - Else (same-origin /api case) fall back to VITE_WS_BASE_URL — required
 *     for Vercel deployments because Vercel can't proxy WebSockets.
 *   - Else (localhost dev) use ws://127.0.0.1:8000.
 */

function resolveApiBase(): string {
  const fromEnv = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (fromEnv && fromEnv.length > 0) {
    return fromEnv.replace(/\/+$/, ""); // strip trailing slash
  }

  // On Vercel (production) we use same-origin /api — vercel.json rewrites it
  // to the backend host, so the browser sees same-origin requests.
  // On localhost (dev or Tauri) we hit the backend directly.
  if (import.meta.env.PROD) {
    return "/api";
  }
  return "http://127.0.0.1:8000";
}

export const API_BASE = resolveApiBase();

/**
 * Convert the HTTP API base URL into the equivalent WebSocket URL.
 *
 * Handles http → ws and https → wss upgrades automatically.
 *
 * Example:
 *   - https://archy-backend.onrender.com → wss://archy-backend.onrender.com/events
 *   - http://127.0.0.1:8000            → ws://127.0.0.1:8000/events
 *   - /api (Vercel same-origin)         → falls back to VITE_WS_BASE_URL
 *     because Vercel can't proxy WebSocket upgrades.
 */
export function getWebSocketUrl(path: string = "/events"): string {
  // Explicit override always wins.
  const wsOverride = import.meta.env.VITE_WS_BASE_URL as string | undefined;
  if (wsOverride && wsOverride.length > 0) {
    return wsOverride.replace(/\/+$/, "") + path;
  }

  // Full-URL API base — derive ws/wss from it.
  if (API_BASE.startsWith("https://")) {
    return "wss://" + API_BASE.slice("https://".length) + path;
  }
  if (API_BASE.startsWith("http://")) {
    return "ws://" + API_BASE.slice("http://".length) + path;
  }

  // Same-origin /api case (Vercel default). Vercel's edge rewrites don't
  // support WebSocket. If VITE_WS_BASE_URL is not set, the WebSocket WILL fail.
  // Fix: set VITE_WS_BASE_URL=wss://archy-backend.onrender.com in Vercel env vars.
  if (API_BASE.startsWith("/")) {
    const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${window.location.host}${path}`;
    console.error(
      "[Archy] VITE_WS_BASE_URL is not set. WebSocket will connect to",
      wsUrl,
      "which Vercel cannot proxy. " +
      "Fix: add VITE_WS_BASE_URL=wss://archy-backend.onrender.com in your Vercel environment variables."
    );
    return wsUrl;
  }

  // Fallback — assume ws:// for arbitrary schemes
  return "ws://" + API_BASE + path;
}
