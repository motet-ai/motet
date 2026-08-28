import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { existsSync } from "fs";

const antDesignXDir =
  [
    path.resolve(__dirname, "node_modules/@ant-design/x"),
    path.resolve(__dirname, "../../node_modules/@ant-design/x"),
  ].find((p) => existsSync(p)) ??
  path.resolve(__dirname, "../../node_modules/@ant-design/x");

const antDesignXMarkdownDir =
  [
    path.resolve(__dirname, "node_modules/@ant-design/x-markdown"),
    path.resolve(__dirname, "../../node_modules/@ant-design/x-markdown"),
  ].find((p) => existsSync(p)) ??
  path.resolve(__dirname, "../../node_modules/@ant-design/x-markdown");

// Vite configuration for the Chat Explorer Ant Design X app.
export default defineConfig({
  plugins: [react()],
  base: '/chat-explorer/',
  resolve: {
    alias: {
      "@motet/ui-common": path.resolve(__dirname, "../../packages/motet-ui-common/src"),
      "@ant-design/x": antDesignXDir,
      "@ant-design/x-markdown": antDesignXMarkdownDir,
    }
  },
  server: {
    host: true,
    port: 5173,
    allowedHosts: [
      'localhost',
      'host.docker.internal',
      '.localhost',
      'vite-dev',
      'vite-builder-chat',
    ],
    hmr: false, // Disable HMR to prevent refresh loop when proxy WebSocket fails
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
        secure: false
      }
    }
  }
});

