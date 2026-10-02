import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { TableInfo } from "@/lib/clickhouse";

const listTables = vi.fn<(db: string) => Promise<TableInfo[]>>();
vi.mock("@/lib/clickhouse", () => ({ listTables: (db: string) => listTables(db) }));

const { default: TableSelector } = await import("@/components/TableSelector.vue");
const { useSettings } = await import("@/stores/settings");

const table = (database: string, name: string, total_rows: number | null = 1): TableInfo => ({
  database,
  name,
  engine: "MergeTree",
  total_rows,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  setActivePinia(createPinia());
  listTables.mockReset();
});

describe("TableSelector", () => {
  it("lists the tables of the configured database", async () => {
    listTables.mockResolvedValue([table("evnt", "local", 1234)]);
    const wrapper = mount(TableSelector, { props: { modelValue: "evnt.local" } });
    await flushPromises();
    expect(listTables).toHaveBeenCalledWith("evnt");
    expect(wrapper.text()).toContain("evnt.local");
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  });

  it("refreshes when the connection settings change", async () => {
    listTables.mockResolvedValue([table("evnt", "local")]);
    mount(TableSelector, { props: { modelValue: "evnt.local" } });
    await flushPromises();
    listTables.mockResolvedValue([table("other", "t")]);
    useSettings().database = "other";
    await flushPromises();
    expect(listTables).toHaveBeenLastCalledWith("other");
  });

  it("selects the first table when the current one is not in the list", async () => {
    listTables.mockResolvedValue([table("other", "a"), table("other", "b")]);
    const wrapper = mount(TableSelector, { props: { modelValue: "evnt.local" } });
    await flushPromises();
    expect(wrapper.emitted("update:modelValue")).toEqual([["other.a"]]);
  });

  it("ignores a refresh that finishes after a newer one", async () => {
    const slow = deferred<TableInfo[]>();
    listTables.mockReturnValueOnce(slow.promise);
    const wrapper = mount(TableSelector, { props: { modelValue: "new.t" } });
    listTables.mockResolvedValueOnce([table("new", "t")]);
    useSettings().database = "new";
    await flushPromises();
    slow.resolve([table("stale", "t")]);
    await flushPromises();
    expect(wrapper.text()).toContain("new.t");
    expect(wrapper.text()).not.toContain("stale.t");
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  });

  it("shows the error and an empty list when listing fails", async () => {
    listTables.mockRejectedValue(new Error("Failed to fetch"));
    const wrapper = mount(TableSelector, { props: { modelValue: "" } });
    await flushPromises();
    expect(wrapper.text()).toContain("Failed to fetch");
    expect(wrapper.text()).toContain("No tables");
  });
});
