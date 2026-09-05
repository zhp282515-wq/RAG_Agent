/* ============================================
   report.js — 报告页(报告记录)
   历史报告列表 / 预览 / 编辑 / 下载
   报告由聊天链路生成,后端在报告生成完成后自动保存(POST /api/reports)
   ============================================ */

const ReportUI = {
  currentReportId: null,   // 当前正在查看的报告
  currentContent: null,    // 当前报告的 markdown 原文
  editMode: false,

  /* ---------- 历史列表 ---------- */
  async refresh() {
    try {
      const data = await API.get("/api/reports");
      this.renderList(data.reports || []);
    } catch (e) {
      console.error("报告列表加载失败:", e);
    }
  },

  renderList(reports) {
    const box = document.getElementById("report-list");
    box.innerHTML = "";
    if (!reports.length) {
      box.innerHTML = `<p class="muted">暂无报告记录</p>`;
      return;
    }
    for (const r of reports) {
      const item = document.createElement("div");
      item.className = "report-item";
      item.innerHTML = `
        <div class="report-item-title">${this.esc(r.title || "未命名报告")}</div>
        <div class="report-item-meta muted">${this.esc(r.month || "")} · ${this.esc(r.created_at || "")}</div>
        <div class="report-item-actions">
          <button class="r-view" title="预览">${ic("eye","ic-sm")}</button>
          <button class="r-del" title="删除">${ic("trash","ic-sm")}</button>
        </div>`;
      item.querySelector(".r-view").addEventListener("click", () => this.open(r.report_id));
      item.querySelector(".r-del").addEventListener("click", (ev) => this.remove(r.report_id, ev));
      box.appendChild(item);
    }
  },

  async remove(reportId, ev) {
    ev.stopPropagation();
    if (!confirm("确定删除该报告记录?")) return;
    try {
      await API.del(`/api/reports/${reportId}`);
      await this.refresh();
    } catch (e) {
      alert("删除失败:" + e.message);
    }
  },

  /* ---------- 预览 / 编辑 / 下载 ---------- */
  async open(reportId) {
    try {
      const data = await API.get(`/api/reports/${reportId}`);
      this.currentReportId = reportId;
      this.editMode = false;
      const r = data.report;
      this.currentContent = r.content || "";
      document.getElementById("report-modal-title").textContent = r.title || "报告预览";
      this.showPreview(this.currentContent);
      document.getElementById("report-modal").classList.remove("hidden");
    } catch (e) {
      alert("加载报告失败:" + e.message);
    }
  },

  showPreview(md) {
    const preview = document.getElementById("report-modal-preview");
    try { preview.innerHTML = marked.parse(md); } catch (_) { preview.textContent = md; }
    preview.classList.remove("hidden");
    document.getElementById("report-modal-editor").classList.add("hidden");
    document.getElementById("report-modal-footer").classList.add("hidden");
    document.getElementById("btn-report-edit-toggle").innerHTML = `${ic("edit","ic-sm")} 编辑`;
  },

  toggleEdit() {
    if (!this.currentReportId) return;
    const preview = document.getElementById("report-modal-preview");
    const editor = document.getElementById("report-modal-editor");
    const footer = document.getElementById("report-modal-footer");
    this.editMode = !this.editMode;
    if (this.editMode) {
      editor.value = this.currentContent || "";
      preview.classList.add("hidden");
      editor.classList.remove("hidden");
      footer.classList.remove("hidden");
      document.getElementById("btn-report-edit-toggle").innerHTML = `${ic("eye","ic-sm")} 预览`;
    } else {
      this.showPreview(this.currentContent);
      document.getElementById("btn-report-edit-toggle").innerHTML = `${ic("edit","ic-sm")} 编辑`;
    }
  },

  async save() {
    const editor = document.getElementById("report-modal-editor");
    const content = editor.value;
    try {
      await API.put(`/api/reports/${this.currentReportId}`, { content });
      this.currentContent = content;
      this.editMode = false;
      this.showPreview(content);
      document.getElementById("btn-report-edit-toggle").innerHTML = `${ic("edit","ic-sm")} 编辑`;
      this.refresh();
    } catch (e) {
      alert("保存失败:" + e.message);
    }
  },

  download() {
    const content = this.currentContent ||
      document.getElementById("report-modal-preview").innerText;
    const title = document.getElementById("report-modal-title").textContent || "报告";
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${title}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  },

  close() {
    document.getElementById("report-modal").classList.add("hidden");
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-report-close").addEventListener("click", () => ReportUI.close());
  document.getElementById("btn-report-edit-toggle").addEventListener("click", () => ReportUI.toggleEdit());
  document.getElementById("btn-report-download").addEventListener("click", () => ReportUI.download());
  document.getElementById("btn-report-save").addEventListener("click", () => ReportUI.save());
  document.getElementById("btn-report-cancel").addEventListener("click", () => ReportUI.toggleEdit());
  ReportUI.refresh();
});
