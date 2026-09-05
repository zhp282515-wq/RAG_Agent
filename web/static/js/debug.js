/* ============================================
   debug.js — 检索调试页
   query + top_k → 命中列表(等级徽标/分数/来源/页码/章节/正文)
   ============================================ */

const DebugUI = {
  async search() {
    const query = document.getElementById("debug-query").value.trim();
    if (!query) return;
    const topK = parseInt(document.getElementById("debug-topk").value) || 20;
    const box = document.getElementById("debug-results");
    box.innerHTML = `<p class="muted">检索中…</p>`;
    try {
      const data = await API.post("/api/search", { query, top_k: topK });
      this.render(data.hits || []);
    } catch (e) {
      box.innerHTML = `<p class="muted">检索失败:${this.esc(e.message)}</p>`;
    }
  },

  render(hits) {
    const box = document.getElementById("debug-results");
    if (!hits.length) {
      box.innerHTML = `<p class="muted">无相关命中(相关度低于 0.60 的条目不返回)</p>`;
      return;
    }
    box.innerHTML = hits.map((h, i) => `
      <div class="hit-card">
        <div class="hit-head">
          <span class="badge ${h.label === "高" ? "high" : "mid"}">${this.esc(h.label || "中")}</span>
          <span class="hit-score">${Number(h.score || 0).toFixed(3)}</span>
          <span class="hit-meta">#${i + 1} · ${this.esc(h.file_name || "")}${h.page ? " · 第" + h.page + "页" : ""}${h.chapter ? " · " + this.esc(h.chapter) : ""}${h.section ? " · " + this.esc(h.section) : ""}</span>
        </div>
        <div class="hit-text">${this.esc(h.text || "")}</div>
      </div>`).join("");
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-debug-search").addEventListener("click", () => DebugUI.search());
  document.getElementById("debug-query").addEventListener("keydown", (e) => {
    if (e.key === "Enter") DebugUI.search();
  });
});
