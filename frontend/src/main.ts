import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "@/App.vue";
import { router } from "@/router";
import { installInterceptor } from "@/lib/interceptor";
import { initSnowplow, trackPageView } from "@/lib/snowplow";
import "@/styles/global.css";

const APP_TITLE = "evnt — Snowplow demo";

const app = createApp(App);
app.use(createPinia());
app.use(router);

// After Pinia is installed: the interceptor writes into the live-events store.
installInterceptor();
initSnowplow({ appId: "evnt-demo" });

// One page view per completed navigation, the initial one included, so events
// fired on a view are attributed to that view's page view rather than to
// whichever page the SPA was first opened on. The title is set first because
// the tracker reads document.title for the page view.
router.afterEach((to, _from, failure) => {
  if (failure) return;
  const title = typeof to.meta.title === "string" ? to.meta.title : null;
  document.title = title ? `${title} · ${APP_TITLE}` : APP_TITLE;
  trackPageView();

  const main = document.querySelector("main");
  if (main) {
    main.setAttribute("tabindex", "-1");
    main.focus();
  }
});

app.mount("#app");
