import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy keeps the browser on one origin in development, so no CORS
    // preflight and the same relative API paths work in the production build.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/mock-api": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
