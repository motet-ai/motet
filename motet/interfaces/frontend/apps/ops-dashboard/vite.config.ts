import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { fileURLToPath } from "url";
import { existsSync } from "fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Cytoscape may live in app node_modules or workspace root (e.g. Docker npm ci)
const cytoscapeDir = [path.resolve(__dirname, "node_modules/cytoscape"), path.resolve(__dirname, "../../node_modules/cytoscape")].find((p) => existsSync(p)) ?? path.resolve(__dirname, "node_modules/cytoscape");
const antDesignXDir = [path.resolve(__dirname, "node_modules/@ant-design/x"), path.resolve(__dirname, "../../node_modules/@ant-design/x")].find((p) => existsSync(p)) ?? path.resolve(__dirname, "../../node_modules/@ant-design/x");
const antDesignXMarkdownDir = [path.resolve(__dirname, "node_modules/@ant-design/x-markdown"), path.resolve(__dirname, "../../node_modules/@ant-design/x-markdown")].find((p) => existsSync(p)) ?? path.resolve(__dirname, "../../node_modules/@ant-design/x-markdown");

// Vite configuration for the Admin Dashboard
export default defineConfig({
  plugins: [react()],
  base: '/manage/',
  resolve: {
    alias: {
      "@motet/ui-common": path.resolve(__dirname, "../../packages/motet-ui-common/src"),
      "@ant-design/x": antDesignXDir,
      "@ant-design/x-markdown": antDesignXMarkdownDir,
      cytoscape: cytoscapeDir
    }
  },
  server: {
    host: true,
    port: 5174,
    // Allow access from any host (needed for Docker proxy)
    allowedHosts: true,
    // Ensure HMR works through proxy
    hmr: {
      clientPort: 5174
    },
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
        secure: false
      }
    }
  }
});
