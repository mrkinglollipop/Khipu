import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts on purpose: that file is tuned for `tauri
// dev`/`tauri build` (fixed port, HMR host, src-tauri watch-ignore) and none
// of that applies to the Layer 3 DOM oracle (`npm run check:setup`) — see
// docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "the gaps become
// oracles", layer 3. jsdom gives the Database step a real DOM to render into
// without an actual Tauri webview.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
