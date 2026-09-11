/* ============================================
   debug.js — 检索调试页
   query + top_k → 命中列表(等级徽标/分数/来源/页码/章节/正文)
   ============================================ */

const DebugUI = {
  /** 进入调试页时用全局 top_k/达标线预填(可再手动改;调试改的 top_k 仅本次临时覆盖,不写库) */
  async prefill() {
    try {
      const d = await API.get("/api/settings");
      const topk = document.getElementById("debug-topk");
      const box = document.getElementById("debug-results");
      if (topk && d.retrieval && topk.value === "20" && !topk.dataset.touched) {
        topk.value = d.retrieval.top_k;
      }
      if (box && d.retrieval) box.dataset.scoreMin = d.retrieval.score_min;
    } catch (_) { /* 忽略:保持默认 */ }
  },

  async search() {
    const query = document.getElementById("debug-query").value.trim();
    if (!query) return;
    const topkInput = document.getElementById("debug-topk");
    if (topkInput) topkInput.dataset.touched = "1"; // 记录用户手动改过,不再预填覆盖
    const topK = parseInt(topkInput.value) || 20;
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
      const sm = Number(box.dataset.scoreMin || 0.6).toFixed(2);
      box.innerHTML = `<p class="muted">无相关命中(相关度低于 ${sm} 的条目不返回)</p>`;
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
