import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "/dashboard/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/v3": "http://localhost:6767",
      "/v4": "http://localhost:6767",
    },
  },
});
