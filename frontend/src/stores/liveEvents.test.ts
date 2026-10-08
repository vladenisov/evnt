import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { MAX_LOGS, useLiveEvents } from "@/stores/liveEvents";

const entry = (n: number) => ({ method: "POST" as const, url: "/tracker", timestamp: n, payload: { n } });

beforeEach(() => setActivePinia(createPinia()));

describe("liveEvents store", () => {
  it("puts the newest entry first with increasing ids", () => {
    const store = useLiveEvents();
    store.push(entry(1));
    store.push(entry(2));
    expect(store.logs.map((l) => l.timestamp)).toEqual([2, 1]);
    expect(store.logs.map((l) => l.id)).toEqual([2, 1]);
  });

  it(`keeps only the newest ${MAX_LOGS} entries`, () => {
    const store = useLiveEvents();
    for (let i = 1; i <= MAX_LOGS + 25; i++) store.push(entry(i));
    expect(store.logs).toHaveLength(MAX_LOGS);
    expect(store.logs[0]?.timestamp).toBe(MAX_LOGS + 25);
    expect(store.logs.at(-1)?.timestamp).toBe(26);
  });

  it("drops entries while paused and resumes after toggling back", () => {
    const store = useLiveEvents();
    store.togglePaused();
    expect(store.paused).toBe(true);
    store.push(entry(1));
    expect(store.logs).toHaveLength(0);
    store.togglePaused();
    store.push(entry(2));
    expect(store.logs).toHaveLength(1);
  });

  it("clear empties the log without resetting ids", () => {
    const store = useLiveEvents();
    store.push(entry(1));
    store.clear();
    expect(store.logs).toEqual([]);
    store.push(entry(2));
    expect(store.logs[0]?.id).toBe(2);
  });

  it("replaces the array on push, so watchers on the shallow ref fire", () => {
    const store = useLiveEvents();
    const before = store.logs;
    store.push(entry(1));
    expect(store.logs).not.toBe(before);
  });
});
