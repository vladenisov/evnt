<script setup lang="ts">
import { RouterLink } from "vue-router";
</script>

<template>
  <nav class="app-nav">
    <RouterLink
      to="/live"
      class="nav-link"
      active-class="is-active"
      aria-current-value="page"
    >
      Live Events
    </RouterLink>
    <RouterLink
      to="/tables"
      class="nav-link"
      active-class="is-active"
      aria-current-value="page"
    >
      ClickHouse Tables
    </RouterLink>
    <RouterLink
      to="/settings"
      class="nav-link"
      active-class="is-active"
      aria-current-value="page"
    >
      Settings
    </RouterLink>
  </nav>
</template>

<style scoped>
.app-nav {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  background: var(--c-surface-2);
  border-radius: 999px;
  border: 1px solid var(--c-border);
}

.nav-link {
  font-size: 13px;
  padding: 6px 12px;
  border-radius: 999px;
  color: var(--c-text-muted);
  text-decoration: none;
  transition: background 120ms ease, color 120ms ease;
}

.nav-link:hover {
  color: var(--c-text);
}

.nav-link.is-active {
  background: var(--c-accent);
  color: #fff;
}
</style>
