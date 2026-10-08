<script setup lang="ts">
import { ref, watch } from "vue";
import { listTables, type TableInfo } from "@/lib/clickhouse";
import { useSettings } from "@/stores/settings";

const props = defineProps<{ modelValue: string }>();
const emit = defineEmits<{ (e: "update:modelValue", v: string): void }>();

const settings = useSettings();
const tables = ref<TableInfo[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

// Only the newest refresh may write: settings change per keystroke, and an
// older, slower answer (say, for a half-typed database name) must not win.
let latestRefresh = 0;

async function refresh() {
  const ticket = ++latestRefresh;
  loading.value = true;
  error.value = null;
  try {
    const result = await listTables(settings.database);
    if (ticket !== latestRefresh) return;
    tables.value = result;
    // Keep the selection pointing at a table that exists: after switching
    // database the old choice would query a table that is not there.
    const names = result.map((t) => `${t.database}.${t.name}`);
    const first = names[0] ?? "";
    if (!names.includes(props.modelValue) && props.modelValue !== first) {
      emit("update:modelValue", first);
    }
  } catch (e) {
    if (ticket !== latestRefresh) return;
    error.value = e instanceof Error ? e.message : String(e);
    tables.value = [];
  } finally {
    if (ticket === latestRefresh) loading.value = false;
  }
}

// Through a getter: `settings.snapshot` on the store is the unwrapped value,
// which watch() cannot track, so a changed connection never refreshed the list.
watch(
  () => settings.snapshot,
  () => {
    void refresh();
  },
  { immediate: true },
);

function onChange(event: Event) {
  emit("update:modelValue", (event.target as HTMLSelectElement).value);
}
</script>

<template>
  <div class="selector">
    <label class="lbl" for="table-select">Table</label>
    <select
      id="table-select"
      :value="modelValue"
      @change="onChange"
      :disabled="loading"
    >
      <option v-if="!tables.length" value="">
        {{ loading ? "Loading…" : "No tables" }}
      </option>
      <option
        v-for="t in tables"
        :key="`${t.database}.${t.name}`"
        :value="`${t.database}.${t.name}`"
      >
        {{ t.database }}.{{ t.name }}
        <template v-if="t.total_rows !== null"
          >&nbsp;({{ t.total_rows.toLocaleString() }} rows)</template
        >
      </option>
    </select>
    <button class="btn" type="button" :disabled="loading" @click="refresh">
      Refresh
    </button>
    <span v-if="error" class="err" :title="error">⚠ {{ error }}</span>
  </div>
</template>

<style scoped>
.selector {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.lbl {
  font-size: 12px;
  color: var(--c-text-muted);
}

select {
  min-width: 240px;
}

.err {
  color: var(--c-danger);
  font-size: 12px;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
