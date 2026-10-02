import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

const STORAGE_KEY = "evnt-demo:settings:v1";
const PASSWORD_KEY = "evnt-demo:settings:pwd:v1";

export interface ClickHouseSettings {
  url: string;
  user: string;
  password: string;
  database: string;
}

const DEFAULTS: ClickHouseSettings = {
  url: "http://localhost:8123",
  user: "default",
  password: "",
  database: "evnt",
};

function loadFromStorage(): ClickHouseSettings {
  let restored: ClickHouseSettings = { ...DEFAULTS };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ClickHouseSettings>;
      restored = { ...DEFAULTS, ...parsed };
    }
  } catch {
    restored = { ...DEFAULTS };
  }
  // Password is never persisted to localStorage; restore it from sessionStorage.
  try {
    restored.password = sessionStorage.getItem(PASSWORD_KEY) ?? "";
  } catch {
    restored.password = "";
  }
  return restored;
}

export const useSettings = defineStore("settings", () => {
  const initial = loadFromStorage();
  const url = ref(initial.url);
  const user = ref(initial.user);
  const password = ref(initial.password);
  const database = ref(initial.database);

  const snapshot = computed<ClickHouseSettings>(() => ({
    url: url.value,
    user: user.value,
    password: password.value,
    database: database.value,
  }));

  watch(snapshot, (next) => {
    const { password: nextPassword, ...persisted } = next;
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

  function reset() {
    url.value = DEFAULTS.url;
    user.value = DEFAULTS.user;
    password.value = DEFAULTS.password;
    database.value = DEFAULTS.database;
  }

  return { url, user, password, database, snapshot, reset };
});
