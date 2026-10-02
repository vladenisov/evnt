import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const trackTestStructEvent = vi.fn();
vi.mock("@/lib/snowplow", () => ({ trackTestStructEvent: () => trackTestStructEvent() }));

const { default: LiveEventsView } = await import("@/views/LiveEventsView.vue");
const { useLiveEvents } = await import("@/stores/liveEvents");

const button = (wrapper: ReturnType<typeof mount>, label: string) =>
  wrapper.findAll("button").find((b) => b.text() === label);

beforeEach(() => {
  setActivePinia(createPinia());
  trackTestStructEvent.mockReset();
});

describe("LiveEventsView", () => {
  it("shows logged requests newest first and clears them", async () => {
    const store = useLiveEvents();
    const wrapper = mount(LiveEventsView);
    expect(wrapper.text()).toContain("Waiting for tracking events");

    store.push({ method: "POST", url: "/tracker", timestamp: 0, payload: { n: 1 } });
    store.push({ method: "GET", url: "/i?e=pv", timestamp: 0, payload: { n: 2 } });
    await wrapper.vm.$nextTick();
    expect(wrapper.findAll("article.entry").map((a) => a.get(".tag").text())).toEqual(["GET", "POST"]);

    await button(wrapper, "Clear Log")?.trigger("click");
    expect(store.logs).toHaveLength(0);
  });

  it("fires a test event and toggles capture", async () => {
    const wrapper = mount(LiveEventsView);
    await button(wrapper, "Track Test Event")?.trigger("click");
    expect(trackTestStructEvent).toHaveBeenCalledOnce();

    await button(wrapper, "Pause Tracking")?.trigger("click");
    expect(useLiveEvents().paused).toBe(true);
    expect(wrapper.text()).toContain("Paused");
    expect(button(wrapper, "Resume Tracking")).toBeDefined();
  });
});
