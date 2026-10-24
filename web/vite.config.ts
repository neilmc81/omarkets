import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export function backendTarget(raw = process.env.OMARKETS_BACKEND_TARGET ?? process.env.MARKETOS_BACKEND_TARGET ?? "http://127.0.0.1:8765"): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("OMARKETS_BACKEND_TARGET must be an HTTP URL on 127.0.0.1");
  }
  if (url.hostname !== "127.0.0.1" || url.protocol !== "http:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("OMARKETS_BACKEND_TARGET must be an HTTP origin on 127.0.0.1 without credentials, path, query or fragment");
  }
  return url.origin;
}

const target = backendTarget();
const proxy = {
  "/api": { target },
  "/health": { target },
  "/ws": { target, ws: true },
};

export default defineConfig({
  cacheDir: "node_modules/.vite",
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
  test: {
    root: new URL("..", import.meta.url).pathname,
    environment: "jsdom",
    include: (process.env.OMARKETS_LIVE_CONTRACT ?? process.env.MARKETOS_LIVE_CONTRACT) === "1"
      ? ["tests/web/api/*.check.ts"]
      : ["tests/web/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/web/setup.ts"],
    alias: {
      "lightweight-charts": new URL("./node_modules/lightweight-charts/dist/lightweight-charts.development.mjs", import.meta.url).pathname,
      react: new URL("./node_modules/react", import.meta.url).pathname,
      "react-dom": new URL("./node_modules/react-dom", import.meta.url).pathname,
      "@testing-library/react": new URL("./node_modules/@testing-library/react/dist/index.js", import.meta.url).pathname,
      "@testing-library/jest-dom/vitest": new URL("./node_modules/@testing-library/jest-dom/dist/vitest.mjs", import.meta.url).pathname,
    },
  },
});
