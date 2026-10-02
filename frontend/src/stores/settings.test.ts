import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { DEFAULTS, PASSWORD_KEY, STORAGE_KEY, useSettings } from "@/stores/settings";

beforeEach(() => setActivePinia(createPinia()));

describe("settings store", () => {
  it("starts from the defaults", () => {
    const s = useSettings();
    expect(s.snapshot).toEqual(DEFAULTS);
  });

  it("restores the saved fields and the tab's password", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ url: "http://ch:8123", user: "ro", database: "x" }));
    sessionStorage.setItem(PASSWORD_KEY, "secret");
    expect(useSettings().snapshot).toEqual({
      url: "http://ch:8123",
      user: "ro",
      password: "secret",
      database: "x",
    });
  });

  it("ignores a password that an old build left in localStorage", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ password: "leaked" }));
    expect(useSettings().password).toBe("");
  });

  it.each([
    ["corrupt JSON", "{not json"],
    ["a non-object", "42"],
    ["null", "null"],
    ["wrong field types", JSON.stringify({ url: 8123, user: null, database: ["x"] })],
  ])("falls back to defaults for %s", (_label, raw) => {
    localStorage.setItem(STORAGE_KEY, raw);
    const s = useSettings();
    expect(s.url).toBe(DEFAULTS.url);
    expect(s.user).toBe(DEFAULTS.user);
    expect(s.database).toBe(DEFAULTS.database);
  });

  it("keeps valid fields when only some are wrong", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ url: "http://ok", user: 1 }));
    const s = useSettings();
    expect(s.url).toBe("http://ok");
    expect(s.user).toBe(DEFAULTS.user);
  });

  it("persists changes, keeping the password out of localStorage", async () => {
    const s = useSettings();
    s.database = "other";
    s.password = "pw";
    await nextTick();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}")).toEqual({
      url: DEFAULTS.url,
      user: DEFAULTS.user,
      database: "other",
    });
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain("pw");
    expect(sessionStorage.getItem(PASSWORD_KEY)).toBe("pw");
  });

  it("survives storage that throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    const s = useSettings();
    expect(s.snapshot).toEqual(DEFAULTS);
    s.user = "x";
    await nextTick();
    expect(s.user).toBe("x");
  });

  it("reset restores every field", async () => {
    const s = useSettings();
    s.url = "http://elsewhere";
    s.user = "u";
    s.password = "p";
    s.database = "d";
    s.reset();
    expect(s.snapshot).toEqual(DEFAULTS);
    await nextTick();
    expect(sessionStorage.getItem(PASSWORD_KEY)).toBe("");
  });
});
