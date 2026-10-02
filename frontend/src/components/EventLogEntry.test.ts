import { afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import EventLogEntry from "@/components/EventLogEntry.vue";
import type { LiveLog } from "@/stores/liveEvents";

const log: LiveLog = { id: 1, method: "POST", url: "/tracker", timestamp: 0, payload: { e: "pv" } };

function setClipboard(value: unknown) {
  Object.defineProperty(window.navigator, "clipboard", { value, configurable: true });
}

afterEach(() => setClipboard(undefined));

describe("EventLogEntry", () => {
  it("copies the payload as pretty JSON and says so", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn(async () => {});
    setClipboard({ writeText });
    const wrapper = mount(EventLogEntry, { props: { log } });
    await wrapper.get("button.copy").trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith('{\n  "e": "pv"\n}');
    expect(wrapper.get("button.copy").text()).toBe("Copied");
    vi.advanceTimersByTime(1500);
    await flushPromises();
    expect(wrapper.get("button.copy").text()).toBe("Copy JSON");
  });

  it.each([
    ["the clipboard API is missing (insecure origin)", undefined],
    ["the write is rejected", { writeText: () => Promise.reject(new Error("denied")) }],
  ])("reports a failure when %s instead of throwing", async (_label, clipboard) => {
    setClipboard(clipboard);
    const wrapper = mount(EventLogEntry, { props: { log } });
    await wrapper.get("button.copy").trigger("click");
    await flushPromises();
    expect(wrapper.get("button.copy").text()).toBe("Copy failed");
  });
});
