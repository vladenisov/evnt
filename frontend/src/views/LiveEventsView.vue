<script setup lang="ts">
import { computed } from "vue";
import { storeToRefs } from "pinia";
import { useLiveEvents } from "@/stores/liveEvents";
import { trackTestStructEvent } from "@/lib/snowplow";
import EventLogEntry from "@/components/EventLogEntry.vue";

const store = useLiveEvents();
const { logs, paused } = storeToRefs(store);

const pausedLabel = computed(() =>
  paused.value ? "Resume Tracking" : "Pause Tracking",
);

function onTrack() {
  trackTestStructEvent();
}
</script>

<template>
  <section class="live">
    <header class="intro surface">
      <div>
        <h2>Tracking Data Log</h2>
        <p class="muted">
          Live view of every payload sent to <code>/tracker</code>. Use the
          buttons to simulate a struct event, pause capture, or clear the log.
        </p>
      </div>
      <div class="controls">
        <button class="btn btn-primary" type="button" @click="onTrack">
          Track Test Event
        </button>
        <button
          class="btn"
          :class="{ 'btn-warn': !paused, 'btn-danger': paused }"
          type="button"
          @click="store.togglePaused()"
        >
          {{ pausedLabel }}
        </button>
        <button class="btn btn-danger" type="button" @click="store.clear()">
          Clear Log
        </button>
        <span v-if="paused" class="tag tag-paused">Paused</span>
      </div>
    </header>

    <div v-if="logs.length === 0" class="empty surface">
      Waiting for tracking events...
    </div>
    <div v-else class="entries">
      <EventLogEntry v-for="log in logs" :key="log.id" :log="log" />
    </div>
  </section>
</template>

<style scoped>
.live {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.intro {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
}

.intro h2 {
  margin: 0 0 4px;
  font-size: 16px;
}

.controls {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.empty {
  padding: 32px;
  text-align: center;
  color: var(--c-text-muted);
}

.entries {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

code {
  font-family: var(--c-mono);
  background: var(--c-surface-2);
  border-radius: 3px;
  padding: 1px 5px;
}
</style>
