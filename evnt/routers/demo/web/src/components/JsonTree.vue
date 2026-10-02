<script setup lang="ts">
import { computed, ref } from "vue";

const props = withDefaults(
  defineProps<{
    data: unknown;
    name?: string | number | null;
    depth?: number;
    initiallyExpanded?: boolean;
    isLast?: boolean;
    maxDepth?: number;
  }>(),
  {
    name: null,
    depth: 0,
    initiallyExpanded: false,
    isLast: true,
    maxDepth: 6,
  },
);

const MAX_VISIBLE_CHILDREN = 100;

type ContainerKind = "object" | "array";

function containerKind(value: unknown): ContainerKind | null {
  if (value === null) return null;
  if (Array.isArray(value)) return "array";
  if (typeof value === "object") return "object";
  return null;
}

const kind = computed(() => containerKind(props.data));

const entries = computed<Array<[string | number, unknown]>>(() => {
  if (kind.value === "array") {
    return (props.data as unknown[]).map((v, i) => [i, v]);
  }
  if (kind.value === "object") {
    return Object.entries(props.data as Record<string, unknown>);
  }
  return [];
});

const expanded = ref<boolean>(props.initiallyExpanded || props.depth === 0);

function toggle() {
  expanded.value = !expanded.value;
}

function preview(value: unknown): string {
  const k = containerKind(value);
  if (k === "array") {
    const len = (value as unknown[]).length;
    return `[ ${len} ${len === 1 ? "item" : "items"} ]`;
  }
  if (k === "object") {
    const len = Object.keys(value as object).length;
    return `{ ${len} ${len === 1 ? "key" : "keys"} }`;
  }
  return "";
}

const isContainer = computed(() => kind.value !== null);
const itemsLabel = computed(() => preview(props.data));

const atMaxDepth = computed(() => props.depth >= props.maxDepth);

const visibleEntries = computed(() =>
  entries.value.slice(0, MAX_VISIBLE_CHILDREN),
);
const hiddenCount = computed(() =>
  Math.max(0, entries.value.length - MAX_VISIBLE_CHILDREN),
);

const toggleLabel = computed(() => {
  const action = expanded.value ? "Collapse" : "Expand";
  if (props.name === null) return action.toLowerCase();
  return `${action} ${props.name}`;
});

function valueClass(v: unknown): string {
  if (v === null) return "v-null";
  if (typeof v === "string") return "v-string";
  if (typeof v === "number") return "v-number";
  if (typeof v === "boolean") return "v-boolean";
  return "v-other";
}

function formatPrimitive(v: unknown): string {
  if (v === null) return "null";
  if (v === undefined) return "undefined";
  if (typeof v === "string") return JSON.stringify(v);
  return String(v);
}
</script>

<template>
  <div class="json-tree" :class="{ 'is-root': depth === 0 }">
    <div class="line">
      <button
        v-if="isContainer && entries.length > 0 && !atMaxDepth"
        class="caret"
        :class="{ open: expanded }"
        type="button"
        @click="toggle"
        :aria-label="toggleLabel"
      ></button>
      <span v-else class="caret caret-empty"></span>

      <span v-if="name !== null" class="key">{{
        typeof name === "number" ? name : `"${name}"`
      }}</span>
      <span v-if="name !== null" class="colon">:</span>

      <template v-if="isContainer">
        <span class="brace">{{ kind === "array" ? "[" : "{" }}</span>
        <span v-if="(!expanded || atMaxDepth) && entries.length > 0" class="muted">
          {{ itemsLabel }}
        </span>
        <span v-if="!expanded || atMaxDepth" class="brace">{{
          kind === "array" ? "]" : "}"
        }}</span>
        <span v-if="(!expanded || atMaxDepth) && !isLast" class="comma">,</span>
      </template>
      <template v-else>
        <span :class="['v', valueClass(data)]">
          {{ formatPrimitive(data) }}
        </span>
        <span v-if="!isLast" class="comma">,</span>
      </template>
    </div>

    <div v-if="isContainer && expanded && !atMaxDepth" class="children">
      <JsonTree
        v-for="([childKey, childValue], index) in visibleEntries"
        :key="String(childKey)"
        :data="childValue"
        :name="childKey"
        :depth="depth + 1"
        :max-depth="maxDepth"
        :is-last="index === visibleEntries.length - 1 && hiddenCount === 0"
      />
      <div v-if="hiddenCount > 0" class="line">
        <span class="caret caret-empty"></span>
        <span class="muted">…{{ hiddenCount }} more</span>
      </div>
      <div class="line close">
        <span class="caret caret-empty"></span>
        <span class="brace">{{ kind === "array" ? "]" : "}" }}</span>
        <span v-if="!isLast" class="comma">,</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.json-tree {
  font-family: var(--c-mono);
  font-size: 12.5px;
  line-height: 1.55;
}

.json-tree.is-root {
  padding: 8px 10px;
  background: var(--c-surface-2);
  border-radius: var(--r-sm);
  border: 1px solid var(--c-border);
  overflow-x: auto;
}

.line {
  display: flex;
  align-items: baseline;
  gap: 2px;
  white-space: pre;
}

.children {
  margin-left: 14px;
  border-left: 1px dashed var(--c-border);
  padding-left: 8px;
}

.caret {
  width: 12px;
  height: 12px;
  border: 0;
  background: transparent;
  color: var(--c-text-muted);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  margin-right: 4px;
}

.caret::before {
  content: "▶";
  font-size: 9px;
  transition: transform 120ms ease;
}

.caret.open::before {
  transform: rotate(90deg);
}

.caret-empty {
  cursor: default;
}

.caret-empty::before {
  content: "";
}

.key {
  color: #c678dd;
}

.colon {
  color: var(--c-text-muted);
  margin-right: 4px;
}

.brace {
  color: var(--c-text-muted);
}

.comma {
  color: var(--c-text-muted);
}

.muted {
  color: var(--c-text-muted);
  margin: 0 6px;
}

.v-string {
  color: #98c379;
}

.v-number {
  color: #d19a66;
}

.v-boolean {
  color: #56b6c2;
}

.v-null {
  color: #abb2bf;
  font-style: italic;
}

.v-other {
  color: var(--c-text);
}

@media (prefers-color-scheme: light) {
  .key {
    color: #6f42c1;
  }
  .v-string {
    color: #032f62;
  }
  .v-number {
    color: #b08800;
  }
  .v-boolean {
    color: #1a7f37;
  }
}
</style>
