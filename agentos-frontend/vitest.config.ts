import path from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Transforms JSX so `.tsx` component tests can run.
  plugins: [react()],
  test: {
    // Default stays node: the bulk of the suite is pure-logic and gains nothing
    // from a DOM. Component tests opt in per file with an
    // `@vitest-environment jsdom` docblock. (Vitest 4 removed
    // `environmentMatchGlobs`, so the docblock is the supported way to scope it.)
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
