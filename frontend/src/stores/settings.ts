import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

export const STORAGE_KEY = "evnt-demo:settings:v1";
export const PASSWORD_KEY = "evnt-demo:settings:pwd:v1";

export interface ClickHouseSettings {
  url: string;
  user: string;
  password: string;
  database: string;
}

export const DEFAULTS: Readonly<ClickHouseSettings> = Object.freeze({
  url: "http://localhost:8123",
  user: "default",
  // Matches CLICKHOUSE_PASSWORD in compose.yml, so the quickstart works as is.
  password: "password",
  database: "evnt",
});

type Persisted = Omit<ClickHouseSettings, "password">;
const PERSISTED_KEYS = ["url", "user", "database"] as const satisfies readonly (keyof Persisted)[];

/**
 * Read what an earlier visit saved. Anything that is not a string-valued field
 * we know (hand-edited storage, an older format) falls back to its default
 * instead of reaching the ClickHouse client as `undefined` or a number.
 */
function readPersisted(): Persisted {
  const result: Persisted = { url: DEFAULTS.url, user: DEFAULTS.user, database: DEFAULTS.database };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      for (const key of PERSISTED_KEYS) {
        const value = record[key];
        if (typeof value === "string") result[key] = value;
      }
    }
  } catch {
    /* corrupt JSON or storage unavailable: keep the defaults */
  }
  return result;
}

// The password is kept for this tab only (sessionStorage), never in localStorage.
function readPassword(): string {
  try {
    return sessionStorage.getItem(PASSWORD_KEY) ?? DEFAULTS.password;
  } catch {
    return DEFAULTS.password;
  }
}

export const useSettings = defineStore("settings", () => {
  const initial = readPersisted();
  const url = ref(initial.url);
  const user = ref(initial.user);
  const password = ref(readPassword());
  const database = ref(initial.database);

  const snapshot = computed<ClickHouseSettings>(() => ({
    url: url.value,
    user: user.value,
    password: password.value,
    database: database.value,
  }));

  watch(snapshot, ({ password: nextPassword, ...persisted }) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
    } catch {
      /* ignore quota / private mode */
    }
    try {
      sessionStorage.setItem(PASSWORD_KEY, nextPassword);
    } catch {
      /* ignore quota / private mode */
    }
  });

  function reset(): void {
    url.value = DEFAULTS.url;
    user.value = DEFAULTS.user;
    password.value = DEFAULTS.password;
    database.value = DEFAULTS.database;
  }

  return { url, user, password, database, snapshot, reset };
});
