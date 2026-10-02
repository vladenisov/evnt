import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const pingConnection = vi.fn<() => Promise<boolean>>();
vi.mock("@/lib/clickhouse", () => ({ pingConnection: () => pingConnection() }));

const { default: ConnectionForm } = await import("@/components/ConnectionForm.vue");
const { useSettings, DEFAULTS } = await import("@/stores/settings");

beforeEach(() => {
  setActivePinia(createPinia());
  pingConnection.mockReset();
});

describe("ConnectionForm", () => {
  it("binds the inputs to the settings store", async () => {
    const wrapper = mount(ConnectionForm);
    await wrapper.get("#ch-database").setValue("other");
    expect(useSettings().database).toBe("other");
  });

  it.each([
    [true, "Connection OK"],
    [false, "Unexpected response from server"],
  ])("reports a ping that resolves %s", async (ok, message) => {
    pingConnection.mockResolvedValue(ok);
    const wrapper = mount(ConnectionForm);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.get(".status").text()).toBe(message);
  });

  it("shows the error of a failed ping", async () => {
    pingConnection.mockRejectedValue(new Error("Failed to fetch"));
    const wrapper = mount(ConnectionForm);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.get(".status").text()).toBe("Failed to fetch");
  });

  it("resets to the defaults", async () => {
    const settings = useSettings();
    settings.user = "someone";
    const wrapper = mount(ConnectionForm);
    const reset = wrapper.findAll("button").find((b) => b.text().includes("Reset"));
    await reset?.trigger("click");
    expect(settings.user).toBe(DEFAULTS.user);
  });
});
