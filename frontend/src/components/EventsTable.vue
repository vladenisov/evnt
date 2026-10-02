<script setup lang="ts">
import { computed, h, ref, watch } from "vue";
import {
  FlexRender,
  getCoreRowModel,
  useVueTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/vue-table";
import {
  countRows,
  describeTable,
  queryRows,
  type ColumnInfo,
} from "@/lib/clickhouse";
import { useSettings } from "@/stores/settings";
import JsonTree from "@/components/JsonTree.vue";

type Row = Record<string, unknown>;

const props = defineProps<{ qualified: string }>();

const settings = useSettings();
const rows = ref<Row[]>([]);
const totalRows = ref(0);
const columnsInfo = ref<ColumnInfo[]>([]);
const sorting = ref<SortingState>([{ id: "time", desc: true }]);
const pagination = ref({ pageIndex: 0, pageSize: 25 });
const loading = ref(false);
const error = ref<string | null>(null);

function isStructured(v: unknown): boolean {
  return v !== null && typeof v === "object";
}

function looksJsonLike(type: string): boolean {
  return /^(JSON|Array|Tuple|Map|Nested)/i.test(type);
}

function renderCell(value: unknown, type: string) {
  if (value === null || value === undefined) {
    return h("span", { class: "muted" }, "—");
  }
  if (isStructured(value) || looksJsonLike(type)) {
    return h(JsonTree, { data: value, initiallyExpanded: false });
  }
  if (typeof value === "string" && value.length > 80) {
    return h(
      "span",
      { class: "ellipsis", title: value },
      value.slice(0, 80) + "…",
    );
  }
  return h("span", { class: "scalar" }, String(value));
}

const columns = computed<ColumnDef<Row>[]>(() =>
  columnsInfo.value.map((col) => ({
    accessorKey: col.name,
    header: col.name,
    enableSorting: true,
    cell: (ctx) => renderCell(ctx.getValue(), col.type),
    meta: { type: col.type },
  })),
);

async function load() {
  if (!props.qualified) {
    rows.value = [];
    totalRows.value = 0;
    columnsInfo.value = [];
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    const [database, table] = props.qualified.split(".");
    if (columnsInfo.value.length === 0) {
      columnsInfo.value = await describeTable(database, table);
    }

    const sort = sorting.value[0];
    const orderBy = sort && columnsInfo.value.some((c) => c.name === sort.id)
      ? sort.id
      : undefined;

    const [data, count] = await Promise.all([
      queryRows<Row>({
        qualified: props.qualified,
        limit: pagination.value.pageSize,
        offset: pagination.value.pageIndex * pagination.value.pageSize,
        orderBy,
        desc: sort?.desc ?? true,
      }),
      countRows(props.qualified),
    ]);
    rows.value = data;
    totalRows.value = count;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    rows.value = [];
    totalRows.value = 0;
  } finally {
    loading.value = false;
  }
}

watch(
  () => props.qualified,
  () => {
    columnsInfo.value = [];
    pagination.value.pageIndex = 0;
    void load();
  },
  { immediate: true },
);

watch(
  [sorting, pagination, settings.snapshot],
  () => {
    void load();
  },
  { deep: true },
);

const table = useVueTable<Row>({
  get data() {
    return rows.value;
  },
  get columns() {
    return columns.value;
  },
  state: {
    get sorting() {
      return sorting.value;
    },
    get pagination() {
      return pagination.value;
    },
  },
  onSortingChange: (updater) => {
    sorting.value =
      typeof updater === "function" ? updater(sorting.value) : updater;
  },
  onPaginationChange: (updater) => {
    pagination.value =
      typeof updater === "function" ? updater(pagination.value) : updater;
  },
  manualSorting: true,
  manualPagination: true,
  get rowCount() {
    return totalRows.value;
  },
  getCoreRowModel: getCoreRowModel(),
});

const pageCount = computed(() =>
  Math.max(1, Math.ceil(totalRows.value / pagination.value.pageSize)),
);

function gotoPage(idx: number) {
  pagination.value.pageIndex = Math.max(
    0,
    Math.min(idx, pageCount.value - 1),
  );
}

function setPageSize(size: number) {
  pagination.value.pageSize = size;
  pagination.value.pageIndex = 0;
}

function reload() {
  void load();
}

defineExpose({ reload });
</script>

<template>
  <div class="events-table surface">
    <div class="status">
      <span v-if="loading" class="muted">Loading…</span>
      <span v-else class="muted">
        {{ totalRows.toLocaleString() }} rows total ·
        page {{ pagination.pageIndex + 1 }} / {{ pageCount }}
      </span>
      <button class="btn" type="button" @click="reload" :disabled="loading">
        Reload
      </button>
    </div>

    <div v-if="error" class="error">⚠ {{ error }}</div>

    <div class="scroll" v-else>
      <table>
        <thead>
          <tr
            v-for="headerGroup in table.getHeaderGroups()"
            :key="headerGroup.id"
          >
            <th
              v-for="header in headerGroup.headers"
              :key="header.id"
              :class="{
                sorted: header.column.getIsSorted(),
                sortable: header.column.getCanSort(),
              }"
              :title="
                (header.column.columnDef.meta as { type?: string } | undefined)
                  ?.type
              "
              :tabindex="header.column.getCanSort() ? 0 : undefined"
              :aria-sort="
                header.column.getCanSort()
                  ? header.column.getIsSorted() === 'asc'
                    ? 'ascending'
                    : header.column.getIsSorted() === 'desc'
                      ? 'descending'
                      : 'none'
                  : undefined
              "
              @click="header.column.getToggleSortingHandler()?.($event)"
              @keydown.enter.space.prevent="
                header.column.getToggleSortingHandler()?.($event)
              "
            >
              <FlexRender
                :render="header.column.columnDef.header"
                :props="header.getContext()"
              />
              <span class="sort-indicator" aria-hidden="true">
                {{
                  header.column.getIsSorted() === "asc"
                    ? "▲"
                    : header.column.getIsSorted() === "desc"
                      ? "▼"
                      : ""
                }}
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!rows.length && !loading">
            <td :colspan="columns.length" class="empty">No rows.</td>
          </tr>
          <tr v-for="row in table.getRowModel().rows" :key="row.id">
            <td v-for="cell in row.getVisibleCells()" :key="cell.id">
              <FlexRender
                :render="cell.column.columnDef.cell"
                :props="cell.getContext()"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="footer">
      <div class="page-size">
        <label class="muted" for="page-size">Rows per page</label>
        <select
          id="page-size"
          :value="pagination.pageSize"
          @change="
            setPageSize(Number(($event.target as HTMLSelectElement).value))
          "
        >
          <option :value="10">10</option>
          <option :value="25">25</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
          <option :value="250">250</option>
        </select>
      </div>
      <div class="pager">
        <button
          class="btn"
          type="button"
          :disabled="pagination.pageIndex === 0"
          @click="gotoPage(0)"
        >
          ⏮
        </button>
        <button
          class="btn"
          type="button"
          :disabled="pagination.pageIndex === 0"
          @click="gotoPage(pagination.pageIndex - 1)"
        >
          ◀ Prev
        </button>
        <button
          class="btn"
          type="button"
          :disabled="pagination.pageIndex >= pageCount - 1"
          @click="gotoPage(pagination.pageIndex + 1)"
        >
          Next ▶
        </button>
        <button
          class="btn"
          type="button"
          :disabled="pagination.pageIndex >= pageCount - 1"
          @click="gotoPage(pageCount - 1)"
        >
          ⏭
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.events-table {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--c-border);
}

.error {
  padding: 18px;
  color: var(--c-danger);
  font-family: var(--c-mono);
  font-size: 12px;
  white-space: pre-wrap;
}

.scroll {
  overflow: auto;
  max-height: 70vh;
}

table {
  border-collapse: separate;
  border-spacing: 0;
  width: 100%;
  font-size: 13px;
}

thead th {
  background: var(--c-surface-2);
  color: var(--c-text);
  text-align: left;
  font-weight: 600;
  padding: 8px 10px;
  border-bottom: 1px solid var(--c-border);
  position: sticky;
  top: 0;
  z-index: 1;
  white-space: nowrap;
}

thead th.sortable {
  cursor: pointer;
  user-select: none;
}

thead th.sorted {
  color: var(--c-accent);
}

.sort-indicator {
  display: inline-block;
  margin-left: 4px;
  font-size: 10px;
  color: var(--c-accent);
}

tbody td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--c-border);
  vertical-align: top;
  max-width: 360px;
  overflow: hidden;
}

tbody tr:hover td {
  background: var(--c-surface-2);
}

.scalar {
  font-family: var(--c-mono);
  font-size: 12.5px;
  white-space: nowrap;
}

.ellipsis {
  display: inline-block;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

.empty {
  padding: 24px;
  text-align: center;
  color: var(--c-text-muted);
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-top: 1px solid var(--c-border);
}

.page-size {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pager {
  display: flex;
  gap: 6px;
}
</style>
