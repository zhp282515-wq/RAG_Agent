<script setup>
import { currentPanel, switchPanel } from "../composables/useConfig";

const ITEMS = [
  { key: "retrieval", label: "检索参数", icon: "search" },
  { key: "model", label: "对话模型", icon: "activity" },
  { key: "tools", label: "工具", icon: "gear" },
];
</script>

<template>
  <div id="sidebar-config">
    <div class="sidebar-config-title">系统配置</div>
    <div class="sidebar-config-nav">
      <button
        v-for="it in ITEMS"
        :key="it.key"
        class="sidebar-config-item"
        :class="{ active: currentPanel === it.key }"
        @click="switchPanel(it.key)"
      >
        <svg class="ic"><use :href="`#i-${it.icon}`" /></svg><span>{{ it.label }}</span>
      </button>
    </div>
  </div>
</template>
