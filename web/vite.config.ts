import { defineConfig } from "vite";

// Static SPA build — output is uploaded to S3 + served via CloudFront (design §11).
// No dev server in production; this is purely a static asset pipeline.
export default defineConfig({
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
