import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` serves the UI on :5173 and proxies the core (:8000).
// Prod: `npm run build` → web/dist, served by nayantra-core at "/".
const core = process.env.NAYANTRA_CORE_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/v1/ws": { target: core.replace(/^http/, "ws"), ws: true },
      "/api": { target: core, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
