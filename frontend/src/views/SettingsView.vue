<script setup lang="ts">
import ConnectionForm from "@/components/ConnectionForm.vue";
</script>

<template>
  <section class="settings">
    <h2>ClickHouse connection</h2>
    <p class="muted">
      The browser queries ClickHouse over HTTP directly. The server must
      respond with CORS headers (preflight + <code>Access-Control-Allow-Origin</code>).
      Settings are stored in <code>localStorage</code>.
    </p>
    <ConnectionForm />
  </section>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.settings h2 {
  margin: 0;
  font-size: 16px;
}

code {
  font-family: var(--c-mono);
  background: var(--c-surface-2);
  border-radius: 3px;
  padding: 1px 5px;
}
</style>
