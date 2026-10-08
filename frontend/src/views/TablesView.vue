<script setup lang="ts">
import { ref } from "vue";
import EventsTable from "@/components/EventsTable.vue";
import TableSelector from "@/components/TableSelector.vue";

const qualified = ref<string>("evnt.local");
const tableRef = ref<InstanceType<typeof EventsTable> | null>(null);
</script>

<template>
  <section class="tables">
    <header class="bar surface">
      <TableSelector v-model="qualified" />
      <button class="btn" type="button" @click="tableRef?.reload()">
        Reload data
      </button>
    </header>

    <EventsTable v-if="qualified" ref="tableRef" :qualified="qualified" />
    <div v-else class="surface empty">Pick a table to inspect.</div>
  </section>
</template>

<style scoped>
.tables {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.bar {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 10px 14px;
  flex-wrap: wrap;
}

.empty {
  padding: 32px;
  text-align: center;
  color: var(--c-text-muted);
}
</style>
