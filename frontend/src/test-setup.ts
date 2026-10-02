import { format } from "node:util";
import { afterEach, beforeEach, vi } from "vitest";

// Anything printed through console.error / console.warn fails the test that
// printed it: that is how Vue reports invalid watch sources, missing props and
// unhandled errors in handlers, which are real defects and used to pass green.
// A test that expects a warning spies on console itself.
const consoleCalls: string[] = [];

beforeEach(() => {
  for (const level of ["error", "warn"] as const) {
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
      consoleCalls.push(`console.${level}: ${format(...args)}`);
    });
  }
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();

  const unexpected = consoleCalls.splice(0);
  if (unexpected.length > 0) {
    throw new Error(
      `The test printed ${unexpected.length} unexpected console message(s):\n\n${unexpected.join("\n\n")}`,
    );
  }
});
