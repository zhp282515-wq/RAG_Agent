/* ============================================
   sessions.js — 侧边栏会话管理
   列表分组渲染 / 新建(空会话去重) / 切换 / 搜索过滤 / 删除
   ============================================ */

const SessionStore = {
  sessions: [],       // [{session_id,title,created_at,updated_at}]
  currentId: null,   // 当前会话 id(可为 null,尚未建)
  hasEmptySession: false, // 是否存在无消息的空会话

  async refresh() {
    try {
      const data = await API.get("/api/sessions");
      this.sessions = data.sessions || [];
      this.hasEmptySession = this.sessions.some(
        (s) => !s.title || s.title.trim() === ""
      );
      this.render();
    } catch (e) {
      console.error("会话列表加载失败:", e);
    }
  },

  /** 新建会话:已存在空会话时直接切到它,不创建第二个 */
  async create() {
    if (this.hasEmptySession) {
      const empty = this.sessions.find((s) => !s.title || s.title.trim() === "");
      if (empty) {
        await this.switchTo(empty.session_id);
        ChatUI.focusInput("新的会话已就绪,输入你的问题开始吧");
        return;
      }
    }
    try {
      const data = await API.post("/api/sessions", {});
      await this.refresh();
      await this.switchTo(data.session.session_id);
    } catch (e) {
      alert("新建会话失败:" + e.message);
    }
  },

  async switchTo(sessionId) {
    this.currentId = sessionId;
    this.render();
    ChatUI.showSessionLoading();
    try {
      const data = await API.get(`/api/sessions/${sessionId}`);
      const s = data.session;
      ChatUI.renderHistory(s ? (s.messages || []) : []);
    } catch (e) {
      console.error("会话详情加载失败:", e);
      ChatUI.renderHistory([]);
    }
  },

  async remove(sessionId, ev) {
    ev && ev.stopPropagation();
    if (!confirm("确定删除该会话?删除后不可恢复。")) return;
    try {
      await API.del(`/api/sessions/${sessionId}`);
      if (this.currentId === sessionId) {
        this.currentId = null;
        ChatUI.renderHistory([]);
      }
      await this.refresh();
    } catch (e) {
      alert("删除会话失败:" + e.message);
    }
  },

  /** 会话被第一轮问答填充后,后端 title 已更新 → 刷新列表 */
  async refreshAfterChat() {
    await this.refresh();
    this.render();
  },

  /** 重命名会话:把该条标题换成内联输入框,回车/失焦保存,Esc 取消 */
  async rename(sessionId, currentTitle, ev) {
    ev && ev.stopPropagation();
    const item = ev.target.closest(".session-item");
    const titleEl = item.querySelector(".session-title");
    const summaryEl = item.querySelector(".session-summary");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "session-rename-input";
    input.value = currentTitle === "新会话" ? "" : currentTitle;
    input.placeholder = "输入会话名称";
    input.maxLength = 40;

    // 用一个包裹态记录当前是否已提交,避免 blur/Enter 双触发重复请求
    let committed = false;
    titleEl.replaceWith(input);
    summaryEl.style.display = "none";
    input.focus();
    input.select();

    const commit = async (save) => {
      if (committed) return;
      committed = true;
      const val = input.value.trim();
      // 先还原静态标题(不再依赖 input,避免后续 refresh 覆盖时序问题)
      const restored = document.createElement("div");
      restored.className = "session-title";
      restored.textContent = (val || "新会话");
      input.replaceWith(restored);
      summaryEl.style.display = "";
      if (!save || !val) return;
      // 值没变就不发请求(避免无意义的 PUT;后端已修 rowcount 误判,双保险)
      if (val === currentTitle) return;
      try {
        await API.put(`/api/sessions/${sessionId}`, { title: val });
        await this.refresh();
      } catch (e) {
        alert("重命名失败:" + e.message);
      }
    };

    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      else if (e.key === "Escape") { commit(false); }
    });
    input.addEventListener("blur", () => commit(true));
  },

  /** 按更新时间分组:今天/昨天/最近7天/更早 */
  groupSessions(sessions) {
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const groups = { "今天": [], "昨天": [], "最近7天": [], "更早": [] };
    for (const s of sessions) {
      const d = new Date((s.updated_at || s.created_at || "").replace(" ", "T"));
      if (isNaN(d)) { groups["更早"].push(s); continue; }
      const dayDiff = Math.floor((startOfToday - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
      if (dayDiff <= 0) groups["今天"].push(s);
      else if (dayDiff === 1) groups["昨天"].push(s);
      else if (dayDiff < 7) groups["最近7天"].push(s);
      else groups["更早"].push(s);
    }
    return groups;
  },

  render() {
    const el = document.getElementById("session-list");
    el.innerHTML = "";
    const keyword = (document.getElementById("session-search-input").value || "").trim().toLowerCase();
    const filtered = keyword
      ? this.sessions.filter((s) => (s.title || "").toLowerCase().includes(keyword))
      : this.sessions;

    const groups = this.groupSessions(filtered);
    let rendered = 0;
    for (const [name, items] of Object.entries(groups)) {
      if (!items.length) continue;
      const g = document.createElement("div");
      g.className = "session-group";
      g.innerHTML = `<div class="session-group-title">${name}</div>`;
      for (const s of items) {
        const time = this.timeLabel(s.updated_at || s.created_at);
        const title = (s.title && s.title.trim()) ? s.title : "新会话";
        const summary = (s.title && s.title.trim()) ? s.title : "还没有内容";
        const item = document.createElement("div");
        item.className = "session-item" + (s.session_id === this.currentId ? " active" : "");
        item.innerHTML = `
          <span class="session-icon">${ic("chat","ic-xs")}</span>
          <div class="session-body">
            <div class="session-title">${this.escapeHtml(title)}</div>
            <div class="session-summary">${this.escapeHtml(summary)}</div>
          </div>
          <span class="session-time">${time}</span>
          <button class="session-del" title="删除会话">${ic("trash","ic-xs")}</button>
          <button class="session-ren" title="重命名会话">${ic("edit","ic-xs")}</button>`;
        item.addEventListener("click", () => this.switchTo(s.session_id));
        item.querySelector(".session-del").addEventListener("click", (ev) => this.remove(s.session_id, ev));
        item.querySelector(".session-ren").addEventListener("click", (ev) => this.rename(s.session_id, title, ev));
        g.appendChild(item);
        rendered++;
      }
      el.appendChild(g);
    }
    if (!rendered) {
      el.innerHTML = `<div class="wf-empty">暂无会话</div>`;
    }
  },

  timeLabel(ts) {
    if (!ts) return "";
    const d = new Date(String(ts).replace(" ", "T"));
    if (isNaN(d)) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  },

  escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

/* ---------- 初始化 ---------- */
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-new-session").addEventListener("click", () => SessionStore.create());
  document.getElementById("session-search-input").addEventListener("input", () => SessionStore.render());
  SessionStore.refresh();
});
