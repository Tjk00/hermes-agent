import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The Control Center API is served by the same process in production; in dev we
// proxy to it so `npm run dev` works against a live backend.
const API_TARGET = process.env.CC_DEV_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "build",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: true,
    port: 5173,
    strictPort: false,
    // Preview hosts (e.g. *.e2b.app) and LAN addresses must be allowed.
    allowedHosts: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true, ws: true },
      "/health": { target: API_TARGET, changeOrigin: true },
      "/status": { target: API_TARGET, changeOrigin: true },
    },
  },
  preview: { host: true, port: 5173, allowedHosts: true },
});
