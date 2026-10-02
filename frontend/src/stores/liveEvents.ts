import { defineStore } from "pinia";
import { ref, shallowRef } from "vue";

export type LogMethod = "GET" | "POST" | "OTHER";

export interface LiveLog {
  id: number;
  method: LogMethod;
  url: string;
  timestamp: number;
  payload: unknown;
}

/** The log keeps the newest entries only; a long-open tab must not grow without bound. */
export const MAX_LOGS = 500;

export const useLiveEvents = defineStore("liveEvents", () => {
  // Entries are never edited after they are logged, so a shallow ref skips
  // making every (possibly large) payload deeply reactive.
  const logs = shallowRef<readonly LiveLog[]>([]);
  const paused = ref(false);
  let nextId = 1;

  function push(entry: Omit<LiveLog, "id">): void {
    if (paused.value) return;
    logs.value = [{ id: nextId++, ...entry }, ...logs.value.slice(0, MAX_LOGS - 1)];
  }

  function clear(): void {
    logs.value = [];
  }

  function togglePaused(): void {
    paused.value = !paused.value;
  }

  return { logs, paused, push, clear, togglePaused };
});
