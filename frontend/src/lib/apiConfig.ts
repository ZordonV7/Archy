/**
 * Central API configuration for the Archy frontend.
 *
 * Reads the backend URL from the VITE_API_BASE_URL env var (set at build time).
 * Defaults to http://127.0.0.1:8000 for local development.
 *
 * For production web deployment (Vercel), set VITE_API_BASE_URL to your
 * Render backend URL, e.g. https://archy-backend.onrender.com
 *
 * For Tauri desktop deployment, leave it unset to use the default localhost.
 */

function resolveApiBase(): string {
  // Vite injects env vars at build time. Only VITE_* prefixed vars are exposed.
  const fromEnv = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (fromEnv && fromEnv.length > 0) {
    return fromEnv.replace(/\/+$/, ""); // strip trailing slash
  }
  // Desktop / local dev default
  return "http://127.0.0.1:8000";
}

export const API_BASE = resolveApiBase();

/**
 * Convert the HTTP API base URL into the equivalent WebSocket URL.
 * Handles http → ws and https → wss upgrades automatically.
 * Example: https://archy-backend.onrender.com → wss://archy-backend.onrender.com/events
 */
export function getWebSocketUrl(path: string = "/events"): string {
  if (API_BASE.startsWith("https://")) {
    return "wss://" + API_BASE.slice("https://".length) + path;
  }
  if (API_BASE.startsWith("http://")) {
    return "ws://" + API_BASE.slice("http://".length) + path;
  }
  // Fallback — assume ws:// for arbitrary schemes
  return "ws://" + API_BASE + path;
}
