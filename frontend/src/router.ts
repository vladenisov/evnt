import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

export const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/live" },
  {
    path: "/live",
    name: "live",
    component: () => import("@/views/LiveEventsView.vue"),
    meta: { title: "Live Events" },
  },
  {
    path: "/tables",
    name: "tables",
    component: () => import("@/views/TablesView.vue"),
    meta: { title: "ClickHouse Tables" },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("@/views/SettingsView.vue"),
    meta: { title: "Settings" },
  },
  // The backend answers every unknown /demo/* path with index.html, so an
  // unknown route must land somewhere instead of rendering an empty page.
  { path: "/:pathMatch(.*)*", redirect: "/live" },
];

export const router = createRouter({
  // Vite's `base` (vite.config.ts), i.e. the /demo mount on the backend.
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
});
