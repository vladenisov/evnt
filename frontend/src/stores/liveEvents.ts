import { defineStore } from "pinia";
import { ref } from "vue";

export type LogMethod = "GET" | "POST" | "OTHER";

export interface LiveLog {
  id: number;
  method: LogMethod;
  url: string;
  timestamp: number;
  payload: unknown;
}

const MAX_LOGS = 500;

export const useLiveEvents = defineStore("liveEvents", () => {
  const logs = ref<LiveLog[]>([]);
  const paused = ref(false);
  let nextId = 1;

  function push(entry: Omit<LiveLog, "id">) {
    if (paused.value) return;
    logs.value.unshift({ id: nextId++, ...entry });
    if (logs.value.length > MAX_LOGS) {
      logs.value.length = MAX_LOGS;
    }
  }

  function clear() {
    logs.value = [];
  }

  function togglePaused() {
    paused.value = !paused.value;
  }

  return { logs, paused, push, clear, togglePaused };
});
