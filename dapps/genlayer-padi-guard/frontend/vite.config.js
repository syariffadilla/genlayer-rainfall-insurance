import { defineConfig } from "vite";

// Relative base so the build works on GitHub Pages (/<repo>/) and on Vercel/Netlify (/).
export default defineConfig({
  base: "./",
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 2000 },
});
