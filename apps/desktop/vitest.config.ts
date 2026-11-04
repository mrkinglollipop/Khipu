import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts on purpose: that file is tuned for `tauri
// dev`/`tauri build` (fixed port, HMR host, src-tauri watch-ignore) and none
// of that applies to the Layer 3 DOM oracle (`npm run check:setup`) — see
// docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "the gaps become
// oracles", layer 3. jsdom gives the Database step a real DOM to render into
// without an actual Tauri webview.
export default defineConfig({
  plugins: [{
    name: "routing-writer-reachability",
    enforce: "pre",
    transform(code: string, id: string) {
      const path = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env.KHIPU_WRITER_PROBE;
      if (!path || !id.split("?")[0].endsWith("/src/RoutingSettings.tsx")) return;
      let site = 0;
      const instrumented = code.replace(/\bw\.run\(/g, () => `__captureWriter(w.run, ${++site})(`);
      // Opt-in, fixture-only instrumentation records actual call-site execution.
      // It is never installed in the production Vite configuration.
      return `import { appendFileSync as __writerAppend } from "node:fs";
        function __captureWriter(fn: (...args: any[]) => any, site: number) {
          return (...args: any[]) => {
            __writerAppend(${JSON.stringify(path)}, JSON.stringify({ site, id: args[0], command: args[1] }) + "\\n");
            return fn(...args);
          };
        }
        ${instrumented}`;
    },
  }, react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
    // A test that mounts the whole App spends seconds importing and first
    // rendering it on a busy machine. No test here measures speed, so these
    // are the limits a stuck test is failed at, not budgets: the default 5 s
    // failed sound tests whenever other work had the CPU.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
