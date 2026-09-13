<script setup>
import { onMounted } from "vue";
import {
  stores,
  currentStore,
  storeMeta,
  refreshStores,
  setCurrent,
  createStore,
  renameStore,
  removeStore,
} from "../composables/useDocs";

onMounted(() => {
  refreshStores();
});
</script>

<template>
  <div id="sidebar-stores">
    <div class="store-head">
      <span class="sidebar-config-title">向量库</span>
      <button id="btn-new-store" class="icon-btn" title="新建向量库" @click="createStore">
        <svg class="ic ic-sm"><use href="#i-plus" /></svg>
      </button>
    </div>
    <div id="store-list">
      <p v-if="!stores.length" class="muted store-empty">暂无向量库</p>
      <div
        v-for="s in stores"
        :key="s.name"
        class="store-item"
        :class="{ active: s.name === currentStore }"
        @click="setCurrent(s.name)"
      >
        <svg class="ic ic-sm"><use href="#i-list" /></svg>
        <div class="store-body">
          <div class="store-name">{{ s.name }}</div>
          <div class="store-meta">{{ storeMeta(s) }}</div>
        </div>
        <button class="store-ren" title="重命名该向量库" @click.stop="renameStore(s.name)">
          <svg class="ic ic-xs"><use href="#i-edit" /></svg>
        </button>
        <button class="store-del" title="删除该向量库" @click.stop="removeStore(s.name)">
          <svg class="ic ic-xs"><use href="#i-trash" /></svg>
        </button>
      </div>
    </div>
    <p class="store-hint muted">点选切换当前库,agent 检索查该库</p>
  </div>
</template>
