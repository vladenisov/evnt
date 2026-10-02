import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

// The collector the dev server forwards tracker traffic to. Point it elsewhere
// with VITE_PROXY_TARGET=http://host:port pnpm dev.
const collector = process.env.VITE_PROXY_TARGET ?? "http://localhost:8000";
const toCollector = { target: collector, changeOrigin: true };

export default defineConfig(({ mode }) => ({
  // The backend serves the built SPA at /demo (EVNT_COMMON__DEMO=true); the
  // router reads this through import.meta.env.BASE_URL, so the two cannot drift.
  base: "/demo/",
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: mode !== "production",
    // Vendor chunks, so a lazy view only pays for what it uses. Vue's own
    // packages get the highest priority: otherwise the first group that
    // reaches them (vue-table depends on vue) absorbs the runtime, and
    // index.html then preloads the whole table library on every page.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "vue-vendor",
              test: /node_modules[\\/](@vue|vue|vue-router|pinia)[\\/]/,
              priority: 30,
            },
            { name: "clickhouse-vendor", test: /node_modules[\\/]@clickhouse[\\/]/, priority: 20 },
            { name: "table-vendor", test: /node_modules[\\/]@tanstack[\\/]/, priority: 20 },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    // The tracker posts to the page's own origin (see lib/snowplow.ts), so in
    // dev every collector path has to be forwarded: the POST and GET
    // endpoints, the self-hosted sp.js, and the sealed-payload script and
    // endpoint when encryption is on.
    proxy: {
      "/tracker": toCollector,
      "^/i(\\?.*)?$": toCollector,
      "^/e(\\.js)?(\\?.*)?$": toCollector,
      "/static/": toCollector,
    },
  },
  test: {
    environment: "happy-dom",
    environmentOptions: {
      // Tests inject <script src> tags (sp.js, e.js); never fetch them.
      happyDOM: {
        settings: {
          disableJavaScriptFileLoading: true,
          handleDisabledFileLoadingAsSuccess: true,
        },
      },
    },
    setupFiles: "./src/test-setup.ts",
    include: ["src/**/*.test.ts"],
    restoreMocks: true,
    unstubGlobals: true,
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,vue}"],
      exclude: ["src/**/*.test.ts", "src/test-setup.ts", "src/main.ts"],
      reporter: ["text", "json-summary"],
      thresholds: {
        // A floor on the pure logic, where no DOM excuses a gap. Raise it when
        // coverage rises; never lower it to get a red run through.
        // Measured 2026-10-02, rounded down.
        "src/lib/**": { lines: 97, statements: 97, functions: 95, branches: 88 },
        "src/stores/**": { lines: 95, statements: 95, functions: 95, branches: 95 },
      },
    },
  },
}));
