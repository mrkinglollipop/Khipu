import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
// Vite 8 (Rolldown) + @vitejs/plugin-react 6: no config changes needed here,
// verified 2026-09-05 — no rollupOptions/css/plugin-API usage in this file.
export default defineConfig(async () => ({
  plugins: [react()],

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available.
  //    Moved off Tauri's 1420/1421 default: Aegis (Code/aegis/gui) is also a
  //    Tauri app on the stock ports, so sharing them means only one of the two
  //    dev servers can run at a time. Keep in sync with tauri.conf.json devUrl.
  server: {
    port: 1430,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1431,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
  },
}));
