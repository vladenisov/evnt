<script setup lang="ts">
import { ref } from "vue";
import { storeToRefs } from "pinia";
import { useSettings } from "@/stores/settings";
import { pingConnection } from "@/lib/clickhouse";

const settings = useSettings();
const { url, user, password, database } = storeToRefs(settings);

type PingState = "idle" | "ok" | "fail" | "loading";
const ping = ref<PingState>("idle");
const pingMsg = ref<string>("");

async function testConnection() {
  ping.value = "loading";
  pingMsg.value = "";
  try {
    const ok = await pingConnection();
    ping.value = ok ? "ok" : "fail";
    pingMsg.value = ok ? "Connection OK" : "Unexpected response from server";
  } catch (e) {
    ping.value = "fail";
    pingMsg.value = e instanceof Error ? e.message : String(e);
  }
}
</script>

<template>
  <form class="form surface" @submit.prevent="testConnection">
    <div class="row">
      <label for="ch-url">HTTP URL</label>
      <input id="ch-url" v-model="url" type="text" autocomplete="off" />
    </div>
    <div class="row">
      <label for="ch-user">User</label>
      <input id="ch-user" v-model="user" type="text" autocomplete="off" />
    </div>
    <div class="row">
      <label for="ch-password">Password</label>
      <input
        id="ch-password"
        v-model="password"
        type="password"
        autocomplete="off"
      />
    </div>
    <div class="row">
      <label for="ch-database">Database</label>
      <input id="ch-database" v-model="database" type="text" autocomplete="off" />
    </div>

    <div class="actions">
      <button class="btn btn-primary" type="submit">Test connection</button>
      <button class="btn" type="button" @click="settings.reset()">
        Reset to defaults
      </button>
      <span
        v-show="ping !== 'idle'"
        class="status"
        aria-live="polite"
        aria-atomic="true"
        :class="{
          ok: ping === 'ok',
          fail: ping === 'fail',
          loading: ping === 'loading',
        }"
      >
        <template v-if="ping === 'loading'">Pinging…</template>
        <template v-else>{{ pingMsg }}</template>
      </span>
    </div>
  </form>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px;
  max-width: 560px;
}

.row {
  display: grid;
  grid-template-columns: 120px 1fr;
  align-items: center;
  gap: 12px;
}

.row label {
  font-size: 13px;
  color: var(--c-text-muted);
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.status {
  font-size: 12px;
}

.status.ok {
  color: var(--c-success);
}

.status.fail {
  color: var(--c-danger);
}

.status.loading {
  color: var(--c-text-muted);
}
</style>
