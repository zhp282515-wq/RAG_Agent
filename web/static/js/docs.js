/* ============================================
   docs.js — 知识库页(文档管理)
   上传(loading) / 列表 / 预览 / 删除 / md5 去重提示
   ============================================ */

const DocsUI = {
  uploading: false,
  allDocs: [],               // 全量文档(搜索过滤前的原始列表)
  formatFilter: "all",       // 当前选中的格式过滤("all"=全部)

  async refresh() {
    try {
      const data = await API.get("/api/documents");
      this.allDocs = data.documents || [];
      // 若当前选的格式已不存在,复位为全部
      const fmts = this.availableFormats();
      if (this.formatFilter !== "all" && !fmts.includes(this.formatFilter)) this.formatFilter = "all";
      this.renderFilters();
      this.render();
    } catch (e) {
      console.error("文档列表加载失败:", e);
    }
  },

  /** 全量文档里的格式集合(转大写,保持出现顺序) */
  availableFormats() {
    const seen = [];
    for (const d of this.allDocs) {
      const t = String(d.file_type || "?").toUpperCase();
      if (!seen.includes(t)) seen.push(t);
    }
    return seen;
  },

  /** 渲染顶部分类 chips:全部 / PDF / TXT / MD … */
  renderFilters() {
    const box = document.getElementById("doc-filters");
    if (!box) return;
    const fmts = this.availableFormats();
    const renderChip = (label, value, color) => `
      <button class="doc-filter-chip${this.formatFilter === value ? " active" : ""}"
              data-fmt="${value}"${color ? ` style="--fmt-c:${color}"` : ""}>
        ${this.esc(label)}${value === "all" ? "" : ` <span class="chip-count">${this.allDocs.filter(d => String(d.file_type||"?").toUpperCase() === value).length}</span>`}
      </button>`;
    let html = renderChip("全部", "all");
    for (const f of fmts) {
      const color = (this.fmtColors()[f] || "#7C8AA3");
      html += renderChip(f, f, color);
    }
    box.innerHTML = html;
  },

  fmtColors() {
    return { PDF: "#E5484D", DOCX: "#3E7BFA", DOC: "#3E7BFA", XLSX: "#30A46C", XLS: "#30A46C",
      CSV: "#F5A524", MD: "#8E4EC6", MARKDOWN: "#8E4EC6", TXT: "#7C8AA3" };
  },

  /** 按格式 + 搜索关键字过滤全量列表 */
  filterDocs() {
    const kw = (document.getElementById("doc-search-input").value || "").trim().toLowerCase();
    return this.allDocs.filter((d) => {
      const byFmt = this.formatFilter === "all" ||
        String(d.file_type || "?").toUpperCase() === this.formatFilter;
      const byKw = !kw || (d.file_name || "").toLowerCase().includes(kw);
      return byFmt && byKw;
    });
  },

  /** 文件格式徽章:毛玻璃 + 不同格式不同颜色 */
  fmtBadge(type) {
    const t = String(type || "?").toUpperCase();
    const c = this.fmtColors()[t] || "#7C8AA3";
    return `<span class="fmt-badge" style="--fmt-c:${c}">${this.esc(t)}</span>`;
  },

  /** 字节 → B/KB/MB/GB/TB 自动换算(保留 1~2 位,无冗余 0) */
  fmtSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let v = n;
    let i = -1;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    const digits = v >= 100 ? 0 : (v >= 10 ? 1 : 2);
    return v.toFixed(digits) + " " + units[i];
  },

  /** 去掉扩展名后的主名(PDF→空;.tar.gz 这类保留前段) */
  baseName(name) {
    const s = String(name || "");
    const i = s.lastIndexOf(".");
    return i > 0 ? s.slice(0, i) : s;
  },

  render() {
    const docs = this.filterDocs();
    const tbody = document.getElementById("docs-tbody");
    tbody.innerHTML = "";
    if (!docs.length) {
      const hasAny = this.allDocs.length > 0;
      const filtering = hasAny && (this.formatFilter !== "all" ||
        (document.getElementById("doc-search-input").value || "").trim());
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${filtering ? "没有匹配的文档,试试调整筛选或关键字" : "知识库暂无文档,点击右上角上传"}</td></tr>`;
      return;
    }
    for (const d of docs) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td title="${this.esc(d.file_name)}">${this.esc(this.baseName(d.file_name))}</td>
        <td>${this.fmtBadge(d.file_type)}</td>
        <td>${this.fmtSize(d.file_size)}</td>
        <td>${d.chunk_count ?? 0}</td>
        <td class="doc-ops">
          <button class="btn-preview-doc" title="预览分片">${ic("eye","ic-sm")}</button>
          <button class="btn-del-doc" title="删除">${ic("trash","ic-sm")}</button>
        </td>`;
      tr.querySelector(".btn-preview-doc").addEventListener("click", () => this.preview(d.file_name));
      tr.querySelector(".btn-del-doc").addEventListener("click", () => this.remove(d.file_name));
      tbody.appendChild(tr);
    }
  },

  async preview(fileName) {
    const titleEl = document.getElementById("doc-preview-title");
    const bodyEl = document.getElementById("doc-preview-body");
    titleEl.textContent = fileName;
    document.getElementById("doc-preview-modal").classList.remove("hidden");
    bodyEl.textContent = "加载中…";
    try {
      const data = await API.get(`/api/documents/chunks/${encodeURIComponent(fileName)}`);
      const chunks = data.chunks || [];
      // 渲染成可读卡片:每片带序号/页码/章节 + 文本
      bodyEl.innerHTML = "";
      bodyEl.classList.add("chunk-list");
      if (!chunks.length) {
        bodyEl.innerHTML = `<p class="muted">该文档在向量库中暂无分片内容</p>`;
        return;
      }
      for (let i = 0; i < chunks.length; i++) {
        const c = chunks[i];
        const head = document.createElement("div");
        head.className = "chunk-card-head";
        const loc = [c.page ? `第 ${c.page} 页` : "", c.chapter || "", c.section || ""]
          .filter(Boolean).join(" · ");
        head.innerHTML = `<span class="chunk-idx">分片 ${i + 1}</span>${loc ? `<span class="chunk-loc muted">${this.esc(loc)}</span>` : ""}`;
        const pre = document.createElement("div");
        pre.className = "chunk-text";
        pre.textContent = c.text;
        const card = document.createElement("div");
        card.className = "chunk-card";
        card.appendChild(head);
        card.appendChild(pre);
        bodyEl.appendChild(card);
      }
    } catch (e) {
      bodyEl.textContent = "预览失败:" + e.message;
    }
  },

  closePreview() {
    document.getElementById("doc-preview-modal").classList.add("hidden");
  },

  async remove(fileName) {
    if (!confirm(`确定从知识库删除「${fileName}」?`)) return;
    try {
      await API.del(`/api/documents/${encodeURIComponent(fileName)}`);
      await this.refresh();
    } catch (e) {
      alert("删除失败:" + e.message);
    }
  },

  async upload(file) {
    if (this.uploading) return;
    this.uploading = true;
    const status = document.getElementById("upload-status");
    status.textContent = `正在上传并入库「${file.name}」…(含向量化,可能需要一些时间)`;
    try {
      const data = await API.upload("/api/documents", file);
      if (data.ok) {
        status.textContent = `${ic("check","ic-sm")} ${data.message || "已入库"}(${data.chunk_count ?? 0} 个分片)`;
      } else {
        status.innerHTML = `${ic("link","ic-sm")} ${data.message || "上传失败"}`;
      }
      await this.refresh();
    } catch (e) {
      status.innerHTML = `${ic("x","ic-sm")} 上传失败:${this.esc(e.message)}`;
    } finally {
      this.uploading = false;
    }
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-upload").addEventListener("click", () =>
    document.getElementById("doc-file-input").click());
  document.getElementById("doc-file-input").addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (f) DocsUI.upload(f);
    e.target.value = "";
  });
  document.getElementById("btn-doc-preview-close").addEventListener("click", () => DocsUI.closePreview());
  document.getElementById("doc-preview-modal").addEventListener("click", (e) => {
    if (e.target.id === "doc-preview-modal") DocsUI.closePreview();
  });
  // 文件名搜索:即时过滤
  const searchInput = document.getElementById("doc-search-input");
  if (searchInput) searchInput.addEventListener("input", () => DocsUI.render());
  // 格式分类 chips:点击切换(事件委托,chips 由 renderFilters 动态重建)
  const filters = document.getElementById("doc-filters");
  if (filters) filters.addEventListener("click", (e) => {
    const chip = e.target.closest(".doc-filter-chip");
    if (!chip) return;
    DocsUI.formatFilter = chip.dataset.fmt;
    DocsUI.renderFilters();
    DocsUI.render();
  });
  DocsUI.refresh();
});
