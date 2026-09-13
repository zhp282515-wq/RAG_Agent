<script setup>
import { onMounted } from "vue";
import { currentView } from "../composables/useTheme";
import {
  reports,
  modalOpen,
  editMode,
  editorText,
  modalTitle,
  refresh,
  remove,
  open,
  close,
  previewHtml,
  toggleEdit,
  save,
  download,
} from "../composables/useReport";

onMounted(() => {
  refresh();
});
</script>

<template>
  <section class="view" id="view-report" :class="{ active: currentView === 'report' }">
    <div class="page-head"><h2>报告记录</h2></div>

    <div id="report-list">
      <p v-if="!reports.length" class="muted">暂无报告记录</p>
      <div v-for="r in reports" :key="r.report_id" class="report-item">
        <div class="report-item-title">{{ r.title || "未命名报告" }}</div>
        <div class="report-item-meta muted">{{ r.month || "" }} · {{ r.created_at || "" }}</div>
        <div class="report-item-actions">
          <button class="r-view" title="预览" @click="open(r.report_id)">
            <svg class="ic ic-sm"><use href="#i-eye" /></svg>
          </button>
          <button class="r-del" title="删除" @click="remove(r.report_id)">
            <svg class="ic ic-sm"><use href="#i-trash" /></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 预览 / 编辑弹窗 -->
    <div id="report-modal" class="modal" :class="{ hidden: !modalOpen }" @click.self="close">
      <div class="modal-box report-modal-box">
        <div class="modal-head">
          <span id="report-modal-title">{{ modalTitle }}</span>
          <div>
            <button id="btn-report-edit-toggle" @click="toggleEdit">
              <svg class="ic ic-sm"><use :href="editMode ? '#i-eye' : '#i-edit'" /></svg>
              {{ editMode ? "预览" : "编辑" }}
            </button>
            <button id="btn-report-download" @click="download">
              <svg class="ic ic-sm"><use href="#i-download" /></svg> 下载
            </button>
            <button id="btn-report-close" @click="close"><svg class="ic"><use href="#i-x" /></svg></button>
          </div>
        </div>

        <!-- 注意:报告预览有意不做 DOMPurify(与原生版一致) -->
        <div v-show="!editMode" id="report-modal-preview" class="markdown-body" v-html="previewHtml()"></div>
        <textarea v-show="editMode" id="report-modal-editor" v-model="editorText"></textarea>
        <div v-show="editMode" id="report-modal-footer">
          <button id="btn-report-save" @click="save">保存</button>
          <button id="btn-report-cancel" @click="toggleEdit">取消</button>
        </div>
      </div>
    </div>
  </section>
</template>
