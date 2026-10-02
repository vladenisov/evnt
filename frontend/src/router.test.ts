import { describe, expect, it } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";
import { router, routes } from "@/router";

describe("router", () => {
  // Vitest runs with base "/", so this only pins that the router follows
  // Vite's base rather than a hard-coded path; the build sets it to /demo/.
  it("takes its base from Vite's BASE_URL", () => {
    expect(router.options.history.base).toBe(import.meta.env.BASE_URL.replace(/\/$/, ""));
  });

  it.each([
    ["/", "/live"],
    ["/tables", "/tables"],
    ["/no/such/page", "/live"],
  ])("resolves %s to %s", async (path, expected) => {
    const r = createRouter({ history: createMemoryHistory(), routes });
    await r.push(path);
    expect(r.currentRoute.value.path).toBe(expected);
  });
});
