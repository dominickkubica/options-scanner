import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development Vite serves the app and proxies /api to uvicorn, so the browser
// only ever talks to one origin and there is no CORS in the way. The API also
// allows 5173 directly, which is what makes a standalone fetch from devtools work.
//
// The build lands in dist/, which FastAPI mounts at the root when it exists. Same
// URLs in both modes, so nothing in the app code needs to know which one it is in.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
