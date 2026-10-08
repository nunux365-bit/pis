// jest-dom matchers (toBeDisabled, toHaveAttribute, …).
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

// Node 25+ exposes a stub `localStorage` without Storage methods unless
// `--localstorage-file` is set. jsdom tests need a real in-memory store.
if (typeof globalThis.localStorage === "undefined" || typeof globalThis.localStorage.clear !== "function") {
  const store = new Map<string, string>();
  const memoryStorage: Storage = {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null;
    },
    key(index: number) {
      return [...store.keys()][index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: memoryStorage,
  });
}

// RTL only auto-registers its cleanup when vitest runs with `globals: true`,
// which this project does not. Without it, each test's render stacks on top of
// the last and queries match elements from a previous test.
// Guarded on `document` because this setup file also loads for the node-env
// suites, where importing RTL would fail.
if (typeof document !== "undefined") {
  const { cleanup } = await import("@testing-library/react");
  afterEach(cleanup);
}
