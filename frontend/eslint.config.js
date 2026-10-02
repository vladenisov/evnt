import pluginVue from "eslint-plugin-vue";
import { defineConfigWithVueTs, vueTsConfigs } from "@vue/eslint-config-typescript";

export default defineConfigWithVueTs(
  pluginVue.configs["flat/recommended"],
  vueTsConfigs.recommended,
  {
    files: ["**/*.{ts,vue}"],
    rules: {
      "vue/no-v-html": "error",
      "vue/require-v-for-key": "error",
      "vue/valid-v-for": "error",
    },
  },
);
