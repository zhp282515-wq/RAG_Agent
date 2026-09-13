import { ref } from "vue";
import { API } from "../api";
import { marked } from "marked";

/* ============================================
   useReport — 报告记录页
   报告由聊天链路生成,后端在生成完成后自动保存;此处只做查看 / 编辑 / 下载。
   ============================================ */

export const reports = ref([]);

/** 弹窗状态 */
export const modalOpen = ref(false);
export const currentReportId = ref(null);
export const currentContent = ref("");
export const editMode = ref(false);
export const editorText = ref("");
export const modalTitle = ref("报告预览");

export async function refresh() {
  try {
    const data = await API.get("/api/reports");
    reports.value = data.reports || [];
  } catch (e) {
    console.error("报告列表加载失败:", e);
  }
}

export async function remove(reportId) {
  if (!confirm("确定删除该报告记录?")) return;
  try {
    await API.del(`/api/reports/${reportId}`);
    await refresh();
  } catch (e) {
    alert("删除失败:" + e.message);
  }
}

export async function open(reportId) {
  try {
    const data = await API.get(`/api/reports/${reportId}`);
    const r = data.report;
    currentReportId.value = reportId;
    currentContent.value = r.content || "";
    modalTitle.value = r.title || "报告预览";
    editMode.value = false;
    editorText.value = "";
    modalOpen.value = true;
  } catch (e) {
    alert("加载报告失败:" + e.message);
  }
}

export function close() {
  modalOpen.value = false;
}

/**
 * 预览 HTML。
 * 注意:这里有意保持裸 marked.parse、不做 DOMPurify 过滤 —— 与原生版行为一致
 * (聊天页的 mdToHtml 才有 sanitize)。
 */
export function previewHtml() {
  try {
    return marked.parse(currentContent.value || "");
  } catch (_) {
    return currentContent.value || "";
  }
}

export function toggleEdit() {
  if (!currentReportId.value) return;
  if (!editMode.value) editorText.value = currentContent.value || "";
  editMode.value = !editMode.value;
}

export async function save() {
  try {
    await API.put(`/api/reports/${currentReportId.value}`, { content: editorText.value });
    currentContent.value = editorText.value;
    editMode.value = false;
    refresh();
  } catch (e) {
    alert("保存失败:" + e.message);
  }
}

/** 下载为本地 .md(用 Blob,后端无下载接口) */
export function download() {
  const content = currentContent.value || "";
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${modalTitle.value || "报告"}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
}
