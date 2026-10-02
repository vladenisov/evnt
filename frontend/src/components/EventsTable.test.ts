import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const describeTable = vi.fn();
const queryRows = vi.fn();
const countRows = vi.fn();

vi.mock("@/lib/clickhouse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/clickhouse")>();
  return {
    parseQualified: actual.parseQualified,
    describeTable: (...a: unknown[]) => describeTable(...a),
    queryRows: (...a: unknown[]) => queryRows(...a),
    countRows: (...a: unknown[]) => countRows(...a),
  };
});

const { default: EventsTable } = await import("@/components/EventsTable.vue");
const { useSettings } = await import("@/stores/settings");

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  setActivePinia(createPinia());
  describeTable.mockReset().mockResolvedValue([
    { name: "time", type: "DateTime" },
    { name: "event", type: "String" },
  ]);
  queryRows.mockReset().mockResolvedValue([{ time: "2026-10-02 00:00:00", event: "page_view" }]);
  countRows.mockReset().mockResolvedValue(1);
});

describe("EventsTable", () => {
  it("loads columns, rows and the count once on mount, newest first", async () => {
    const wrapper = mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();
    expect(describeTable).toHaveBeenCalledWith("evnt", "local");
    expect(queryRows).toHaveBeenCalledOnce();
    expect(queryRows).toHaveBeenCalledWith({
      qualified: "evnt.local",
      limit: 25,
      offset: 0,
      orderBy: "time",
      desc: true,
    });
    expect(wrapper.text()).toContain("page_view");
    expect(wrapper.text()).toContain("1 rows total");
  });

  it("does not sort by a column the table lacks", async () => {
    describeTable.mockResolvedValue([{ name: "id", type: "UInt64" }]);
    mount(EventsTable, { props: { qualified: "evnt.other" } });
    await flushPromises();
    expect(queryRows.mock.calls[0]?.[0]).toMatchObject({ orderBy: undefined });
  });

  it("switching table re-describes it and queries once from page one", async () => {
    const wrapper = mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();
    queryRows.mockClear();
    describeTable.mockClear();
    await wrapper.setProps({ qualified: "evnt.other" });
    await flushPromises();
    expect(describeTable).toHaveBeenCalledWith("evnt", "other");
    expect(queryRows).toHaveBeenCalledOnce();
    expect(queryRows.mock.calls[0]?.[0]).toMatchObject({ qualified: "evnt.other", offset: 0 });
  });

  it("reloads when the connection settings change", async () => {
    mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();
    queryRows.mockClear();
    useSettings().url = "http://elsewhere:8123";
    await flushPromises();
    expect(queryRows).toHaveBeenCalledOnce();
  });

  it("a slow, older response cannot overwrite a newer one", async () => {
    const slow = deferred<unknown[]>();
    queryRows.mockReturnValueOnce(slow.promise);
    const wrapper = mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();

    queryRows.mockResolvedValueOnce([{ time: "t", event: "fresh" }]);
    await wrapper.get("select#page-size").setValue("50");
    await flushPromises();
    expect(wrapper.text()).toContain("fresh");

    slow.resolve([{ time: "t", event: "stale" }]);
    await flushPromises();
    expect(wrapper.text()).toContain("fresh");
    expect(wrapper.text()).not.toContain("stale");
    expect(wrapper.text()).not.toContain("Loading…");
  });

  it("shows query errors", async () => {
    queryRows.mockRejectedValue(new Error("Code: 60. Unknown table"));
    const wrapper = mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();
    expect(wrapper.text()).toContain("Unknown table");
  });

  it("pages forward with the right offset", async () => {
    countRows.mockResolvedValue(100);
    const wrapper = mount(EventsTable, { props: { qualified: "evnt.local" } });
    await flushPromises();
    const next = wrapper.findAll("button").find((b) => b.text().includes("Next"));
    await next?.trigger("click");
    await flushPromises();
    expect(queryRows.mock.calls.at(-1)?.[0]).toMatchObject({ offset: 25 });
    expect(wrapper.text()).toContain("page 2 / 4");
  });
});
