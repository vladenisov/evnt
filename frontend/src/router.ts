import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
} from "vue-router";

const routes: RouteRecordRaw[] = [
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
];

export const router = createRouter({
  history: createWebHistory("/demo/"),
  routes,
});
