<script setup lang="ts">
import { computed, ref } from "vue";
import JsonTree from "@/components/JsonTree.vue";
import type { LiveLog } from "@/stores/liveEvents";

const props = defineProps<{ log: LiveLog }>();

const time = computed(() => new Date(props.log.timestamp).toLocaleTimeString());
const tagClass = computed(() =>
  props.log.method === "GET" ? "tag-get" : "tag-post",
);

// navigator.clipboard is undefined outside a secure context (plain HTTP on a
// non-localhost host) and writeText rejects when permission is denied; both
// used to surface as uncaught errors. Report the outcome on the button instead.
const copyState = ref<"idle" | "copied" | "failed">("idle");

async function copyJson() {
  try {
    if (!navigator.clipboard) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(JSON.stringify(props.log.payload, null, 2));
    copyState.value = "copied";
  } catch {
    copyState.value = "failed";
  }
  setTimeout(() => {
    copyState.value = "idle";
  }, 1500);
}

const copyLabel = computed(() =>
  copyState.value === "copied" ? "Copied" : copyState.value === "failed" ? "Copy failed" : "Copy JSON",
);
</script>

<template>
  <article class="entry">
    <header class="head">
      <span class="tag" :class="tagClass">{{ log.method }}</span>
      <span class="url" :title="log.url">{{ log.url }}</span>
      <span class="ts">{{ time }}</span>
      <button class="btn copy" type="button" @click="copyJson">{{ copyLabel }}</button>
    </header>
    <div class="body">
      <JsonTree :data="log.payload" />
    </div>
  </article>
</template>

<style scoped>
.entry {
  border: 1px solid var(--c-border);
  border-left: 3px solid var(--c-accent);
  background: var(--c-surface);
  border-radius: var(--r-md);
  padding: 12px 14px;
}

.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.url {
  flex: 1;
  font-family: var(--c-mono);
  font-size: 12px;
  color: var(--c-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ts {
  font-family: var(--c-mono);
  font-size: 12px;
  color: var(--c-text-muted);
}

.copy {
  font-size: 11px;
  padding: 3px 8px;
}
</style>
