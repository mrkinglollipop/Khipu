// Dev-only preview of every phase 9 (9C) account state, by URL hash, for
// reviewers and headless screenshots. Separate from ../vite.config.ts and
// never part of `npm run build` (whose root is apps/desktop/index.html).
//   cd apps/desktop && npx vite --config dev/vite.config.ts
// then open http://localhost:1440/#dialog-step1 (the hash list is on the
// page at http://localhost:1440/).
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  root: here("."),
  plugins: [react()],
  resolve: {
    alias: {
      "@tauri-apps/api/core": here("./fakeTauri.ts"),
      "@tauri-apps/plugin-dialog": here("./fakeDialog.ts"),
    },
  },
  server: { port: 1440, strictPort: false, fs: { allow: [here("..")] } },
});
