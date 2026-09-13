<script setup>
import { ref } from "vue";
import { API } from "../api";
import { currentView } from "../composables/useTheme";

/** 检索调试:query + top_k → 命中列表(query 不进库,纯临时覆盖 top_k) */
const query = ref("");
const topK = ref(20);
/** 用户是否手动改过 top_k —— 改过就不再被预填覆盖 */
let topkTouched = false;

const hits = ref([]);
const loading = ref(false);
const errorMsg = ref("");
/** 未命中提示里的达标线(来自全局检索配置) */
const scoreMin = ref(0.6);

/**
 * 进入调试页时用全局 top_k / 达标线预填。
 * 只在用户没手动改过、且仍是初始值 20 时覆盖(与原生版一致)。
 */
async function prefill() {
  try {
    const d = await API.get("/api/settings");
    if (!d.retrieval) return;
    if (topK.value === 20 && !topkTouched) topK.value = d.retrieval.top_k;
    scoreMin.value = d.retrieval.score_min;
  } catch (_) {
    /* 忽略:保持默认 */
  }
}
defineExpose({ prefill });

async function search() {
  const q = query.value.trim();
  if (!q) return;
  topkTouched = true; // 记录用户手动改过,不再预填覆盖
  loading.value = true;
  errorMsg.value = "";
  try {
    const data = await API.post("/api/search", { query: q, top_k: parseInt(topK.value) || 20 });
    hits.value = data.hits || [];
  } catch (e) {
    errorMsg.value = "检索失败:" + e.message;
    hits.value = [];
  } finally {
    loading.value = false;
  }
}

/** 命中来源行:#序号 · 文件名 · 第N页 · 章节 · 小节 */
function hitMeta(h, i) {
  return [`#${i + 1}`, h.file_name || "", h.page ? `第${h.page}页` : "", h.chapter || "", h.section || ""]
    .filter(Boolean)
    .join(" · ");
}
</script>

<template>
  <section class="view" id="view-debug" :class="{ active: currentView === 'debug' }">
    <div class="page-head"><h2>检索调试</h2></div>
    <div class="debug-form">
      <input type="text" id="debug-query" placeholder="输入检索 query" v-model="query" @keydown.enter="search">
      <input type="number" id="debug-topk" min="1" max="100" title="top_k" v-model.number="topK">
      <button id="btn-debug-search" class="btn-liquid" @click="search">检索</button>
    </div>

    <div id="debug-results">
      <p v-if="loading" class="muted">检索中…</p>
      <p v-else-if="errorMsg" class="muted">{{ errorMsg }}</p>
      <p v-else-if="hits.length === 0" class="muted">
        无相关命中(相关度低于 {{ Number(scoreMin).toFixed(2) }} 的条目不返回)
      </p>
      <div v-for="(h, i) in hits" :key="i" class="hit-card">
        <div class="hit-head">
          <span class="badge" :class="h.label === '高' ? 'high' : 'mid'">{{ h.label || "中" }}</span>
          <span class="hit-score">{{ Number(h.score || 0).toFixed(3) }}</span>
          <span class="hit-meta">{{ hitMeta(h, i) }}</span>
        </div>
        <div class="hit-text">{{ h.text || "" }}</div>
      </div>
    </div>
  </section>
</template>
