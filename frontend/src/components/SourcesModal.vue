<script setup>
import { ref } from "vue";

/** 溯源记录弹窗:展示某条回答的检索依据 */
const open = ref(false);
const sources = ref([]);

function show(msg) {
  sources.value = msg?.sources || [];
  open.value = true;
}
function close() {
  open.value = false;
}
defineExpose({ open: show, close });

/** 来源行:文件名 · 第N页 · 章节 · 小节 */
function meta(s) {
  return [s.file_name || "", s.page ? `第${s.page}页` : "", s.chapter || "", s.section || ""]
    .filter(Boolean)
    .join(" · ");
}
</script>

<template>
  <div id="sources-modal" class="modal" :class="{ hidden: !open }" @click.self="close">
    <div class="modal-box">
      <div class="modal-head">
        <span>溯源记录</span>
        <button id="btn-sources-close" @click="close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div id="sources-modal-body">
        <p v-if="!sources.length" class="muted">该回答暂无检索溯源记录</p>
        <div v-for="(s, i) in sources" :key="i" class="hit-card">
          <div class="hit-head">
            <span class="badge" :class="s.label === '高' ? 'high' : 'mid'">{{ s.label || "中" }}</span>
            <span class="hit-score">{{ Number(s.score || 0).toFixed(3) }}</span>
            <span class="hit-meta">{{ meta(s) }}</span>
          </div>
          <div class="hit-text">{{ s.text || "" }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
