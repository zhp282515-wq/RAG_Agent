<script setup>
import { onMounted, ref } from "vue";
import { currentView } from "../composables/useTheme";
import {
  docsTitle,
  allDocs,
  formatFilter,
  searchKeyword,
  formatChips,
  filteredDocs,
  emptyDocsHint,
  uploading,
  uploadStatus,
  fmtColor,
  fmtSize,
  baseName,
  upload,
  removeDoc,
  refresh,
  loadAll,
  preview,
  closePreview,
  previewOpen,
  previewTitle,
  previewChunks,
  previewError,
  chunkLoc,
} from "../composables/useDocs";

const fileInputRef = ref(null);

onMounted(() => {
  loadAll();
});

function pickFile() {
  fileInputRef.value?.click();
}
function onFileChange(e) {
  const f = e.target.files[0];
  if (f) upload(f);
  e.target.value = "";
}
</script>

<template>
  <section class="view" id="view-docs" :class="{ active: currentView === 'docs' }">
    <div class="docs-main">
      <div class="page-head">
        <h2 id="docs-title">{{ docsTitle }}</h2>
        <div class="upload-wrap">
          <button id="btn-upload" class="btn-liquid" @click="pickFile">
            <svg class="ic"><use href="#i-upload" /></svg> 上传文档
          </button>
          <input ref="fileInputRef" type="file" id="doc-file-input" hidden
                 accept=".pdf,.docx,.doc,.txt,.md,.markdown,.csv,.xlsx,.xls" @change="onFileChange">
        </div>
      </div>

      <p id="upload-status" class="muted">
        <template v-if="uploadStatus.icon">
          <svg class="ic ic-sm"><use :href="`#i-${uploadStatus.icon}`" /></svg>
        </template>{{ uploadStatus.text }}
      </p>

      <div class="doc-toolbar">
        <div class="doc-filters" id="doc-filters">
          <button
            v-for="c in formatChips"
            :key="c.value"
            class="doc-filter-chip"
            :class="{ active: formatFilter === c.value }"
            :style="c.color ? `--fmt-c:${c.color}` : ''"
            @click="formatFilter = c.value"
          >
            {{ c.label }}<span v-if="c.count != null" class="chip-count">{{ c.count }}</span>
          </button>
        </div>
        <div class="doc-search">
          <svg class="ic ic-sm"><use href="#i-search" /></svg>
          <input type="text" id="doc-search-input" placeholder="搜索文件名…" v-model="searchKeyword">
        </div>
      </div>

      <table id="docs-table">
        <thead>
          <tr><th>文件名</th><th>格式</th><th>大小</th><th>分片数</th><th>操作</th></tr>
        </thead>
        <tbody id="docs-tbody">
          <tr v-if="!filteredDocs.length">
            <td colspan="5" class="muted">{{ emptyDocsHint }}</td>
          </tr>
          <tr v-for="d in filteredDocs" :key="d.file_name">
            <td :title="d.file_name">{{ baseName(d.file_name) }}</td>
            <td>
              <span class="fmt-badge" :style="`--fmt-c:${fmtColor(d.file_type)}`">
                {{ String(d.file_type || "?").toUpperCase() }}
              </span>
            </td>
            <td>{{ fmtSize(d.file_size) }}</td>
            <td>{{ d.chunk_count ?? 0 }}</td>
            <td class="doc-ops">
              <button class="btn-preview-doc" title="预览分片" @click="preview(d.file_name)">
                <svg class="ic ic-sm"><use href="#i-eye" /></svg>
              </button>
              <button class="btn-del-doc" title="删除" @click="removeDoc(d.file_name)">
                <svg class="ic ic-sm"><use href="#i-trash" /></svg>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 文档分片预览弹窗(随知识库页,打开时附带当前库上下文) -->
    <div id="doc-preview-modal" class="modal" :class="{ hidden: !previewOpen }" @click.self="closePreview">
      <div class="modal-box">
        <div class="modal-head">
          <span id="doc-preview-title">{{ previewTitle }}</span>
          <button id="btn-doc-preview-close" @click="closePreview">
            <svg class="ic"><use href="#i-x" /></svg>
          </button>
        </div>
        <div id="doc-preview-body" class="doc-preview-body" :class="{ 'chunk-list': previewChunks && previewChunks.length }">
          <p v-if="previewError" class="muted">{{ previewError }}</p>
          <p v-else-if="previewChunks === null" class="muted">加载中…</p>
          <p v-else-if="!previewChunks.length" class="muted">该文档在向量库中暂无分片内容</p>
          <template v-else>
            <div v-for="(c, i) in previewChunks" :key="i" class="chunk-card">
              <div class="chunk-card-head">
                <span class="chunk-idx">分片 {{ i + 1 }}</span>
                <span v-if="chunkLoc(c)" class="chunk-loc muted">{{ chunkLoc(c) }}</span>
              </div>
              <div class="chunk-text">{{ c.text }}</div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>
