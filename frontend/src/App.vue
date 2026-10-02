<script setup lang="ts">
import AppNav from "@/components/AppNav.vue";
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <h1>evnt — Snowplow demo</h1>
      <AppNav />
    </header>
    <main class="app-main">
      <RouterView v-slot="{ Component }">
        <component :is="Component" />
      </RouterView>
    </main>
    <footer class="app-footer">
      <span>Direct ClickHouse queries from the browser. No backend proxy.</span>
    </footer>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  gap: 18px;
}

.app-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  border-bottom: 1px solid var(--c-border);
  padding-bottom: 12px;
}

.app-header h1 {
  margin: 0;
  font-size: 22px;
  letter-spacing: -0.01em;
}

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.app-footer {
  font-size: 12px;
  color: var(--c-text-muted);
  border-top: 1px solid var(--c-border);
  padding-top: 12px;
}
</style>
