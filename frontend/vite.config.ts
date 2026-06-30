import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    // CRITICAL: Ignore the Rust target folder — Vite's file watcher
    // crashes on Windows when it tries to watch compiled DLLs (EBUSY error).
    watch: {
      ignored: [
        "**/src-tauri/target/**",
        "**/.cargo/**",
        "**/node_modules/**",
      ],
    },
  },
  // Also ignore in build optimization
  optimizeDeps: {
    exclude: ["src-tauri"],
  },
});
