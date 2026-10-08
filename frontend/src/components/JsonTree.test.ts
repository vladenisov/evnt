import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import JsonTree from "@/components/JsonTree.vue";

const data = { a: 1, nested: { b: "x" } };

describe("JsonTree", () => {
  it("expands the root by default", () => {
    const wrapper = mount(JsonTree, { props: { data } });
    expect(wrapper.find(".children").exists()).toBe(true);
    expect(wrapper.text()).toContain('"a"');
  });

  it("keeps the root collapsed when initiallyExpanded is false (table cells)", async () => {
    const wrapper = mount(JsonTree, { props: { data, initiallyExpanded: false } });
    expect(wrapper.find(".children").exists()).toBe(false);
    expect(wrapper.text()).toContain("{ 2 keys }");

    await wrapper.get("button.caret").trigger("click");
    expect(wrapper.find(".children").exists()).toBe(true);
  });

  it("caps the number of rendered children", () => {
    const wrapper = mount(JsonTree, { props: { data: Array.from({ length: 105 }, (_, i) => i) } });
    expect(wrapper.text()).toContain("…5 more");
  });

  it("renders primitives with their JSON form", () => {
    expect(mount(JsonTree, { props: { data: "s" } }).text()).toContain('"s"');
    expect(mount(JsonTree, { props: { data: null } }).text()).toContain("null");
  });
});
