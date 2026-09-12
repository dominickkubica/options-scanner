import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development Vite serves the app and proxies /api to uvicorn, so the browser
// only ever talks to one origin and there is no CORS in the way. The API also
// allows 5173 directly, which is what makes a standalone fetch from devtools work.
//
// The build lands in dist/, which FastAPI mounts at the root when it exists. Same
// URLs in both modes, so nothing in the app code needs to know which one it is in.
// Both overridable for running a second copy beside the first, which is how a new
// backend gets checked without restarting the one in use: binding a second server to
// 8000 succeeds while the first still answers localhost, so the only safe way to run
// two is on two ports. Defaults are unchanged.
const API_URL = process.env.OPTSCAN_API_URL || "http://127.0.0.1:8000";
const WEB_PORT = Number(process.env.OPTSCAN_WEB_PORT || 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    port: WEB_PORT,
    strictPort: true,
    proxy: {
      "/api": {
        target: API_URL,
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
