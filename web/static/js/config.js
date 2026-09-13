/* ============================================
   config.js — 「系统配置」页
   左子栏(检索参数/对话模型/工具) + 右侧对应面板
   设置存 MySQL(app_settings / tool_registry),服务端每次提问前现读 → 下次提问即生效
   ============================================ */

const ConfigUI = {
  currentPanel: "retrieval",   // retrieval | model | tools
  settings: null,              // 缓存 GET /api/settings 结果
  tools: [],                   // 缓存 GET /api/tools 结果
  models: [],                  // 可选模型清单

  /* ---------- 侧栏子导航 ---------- */
  async switchPanel(name) {
    this.currentPanel = name;
    document.querySelectorAll("#sidebar-config .sidebar-config-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.configView === name));
    document.querySelectorAll("#view-config .config-panel").forEach((p) =>
      p.classList.toggle("active", p.id === `config-panel-${name}`));
    // 确保数据已加载(避免快速连点导致 settings 未就绪)
    if (name === "tools" && !this.tools.length) await this.loadTools();
    if ((name === "model" || name === "retrieval") && !this.settings) await this.loadSettings();
    if (name === "model") this.initModelPicker();
    if (name === "retrieval") this.fillRetrieval();
    if (name === "tools") this.renderTools();
    // 温度/API Key 状态在切到对话模型面板时也刷新(幂等,无副作用)
    if (name === "model") this.fillModelValues();
  },

  /* ---------- 进入页刷新 ---------- */
  async refresh() {
    await Promise.all([this.loadSettings(), this.loadTools()]);
    // 对话模型相关(下拉 label / 温度滑块 / API Key 状态)始终填充——
    // 即使当前面板停在检索或工具,刷新后也要反映 DB 里的最新值,
    // 而不是停在 HTML 初始占位(温度 0.7 / "加载中…" / key 状态空)。
    this.initModelPicker();
    this.fillModelValues();
    if (this.currentPanel === "retrieval") this.fillRetrieval();
    if (this.currentPanel === "tools") this.renderTools();
  },

  async loadSettings() {
    try {
      this.settings = await API.get("/api/settings");
      // 同步上下文进度条分母(与后端摘要触发阈值同源)
      if (typeof ContextMeter !== "undefined" && this.settings && this.settings.context_limit) {
        ContextMeter.setLimit(this.settings.context_limit);
      }
    } catch (e) {
      console.error("系统配置加载失败:", e);
      this.settings = null;
    }
  },

  async loadTools() {
    try {
      const d = await API.get("/api/tools");
      this.tools = d.tools || [];
    } catch (e) {
      console.error("工具列表加载失败:", e);
      this.tools = [];
    }
  },

  /* ---------- 检索参数面板 ---------- */
  fillRetrieval() {
    if (!this.settings || !this.settings.retrieval) return;
    const r = this.settings.retrieval;
    const setNum = (inputId, valId, v) => {
      const input = document.getElementById(inputId);
      if (input) input.value = v;
      const val = document.getElementById(valId);
      if (val) val.textContent = v;
    };
    setNum("cfg-topk", "cfg-topk-val", r.top_k);
    setNum("cfg-rerank", "cfg-rerank-val", r.rerank_n);
    const hi = document.getElementById("cfg-high");
    const mn = document.getElementById("cfg-min");
    if (hi) hi.value = r.score_high;
    if (mn) mn.value = r.score_min;
    const hiv = document.getElementById("cfg-high-val");
    const mnv = document.getElementById("cfg-min-val");
    if (hiv) hiv.textContent = Number(r.score_high).toFixed(2);
    if (mnv) mnv.textContent = Number(r.score_min).toFixed(2);
  },

  _liveSync(sliderId, valId, digits) {
    const s = document.getElementById(sliderId);
    if (!s) return;
    s.addEventListener("input", () => {
      const val = document.getElementById(valId);
      if (val) val.textContent = Number(s.value).toFixed(digits);
    });
  },

  async saveRetrieval() {
    const body = {
      retrieval: {
        top_k: Number(document.getElementById("cfg-topk").value),
        rerank_n: Number(document.getElementById("cfg-rerank").value),
        score_high: Number(document.getElementById("cfg-high").value),
        score_min: Number(document.getElementById("cfg-min").value),
      },
    };
    try {
      await API.put("/api/settings", body);
      this.feedback("cfg-feedback-retrieval", "已保存 · 下次提问/检索生效", true);
      await this.loadSettings();
    } catch (e) {
      this.feedback("cfg-feedback-retrieval", "保存失败:" + e.message, false);
    }
  },

  resetRetrieval() {
    const d = { top_k: 20, rerank_n: 5, score_high: 0.85, score_min: 0.60 };
    const setNum = (inputId, valId, v) => {
      const input = document.getElementById(inputId);
      if (input) input.value = v;
      const val = document.getElementById(valId);
      if (val) val.textContent = v;
    };
    setNum("cfg-topk", "cfg-topk-val", d.top_k);
    setNum("cfg-rerank", "cfg-rerank-val", d.rerank_n);
    const hi = document.getElementById("cfg-high"), mn = document.getElementById("cfg-min");
    if (hi) hi.value = d.score_high;
    if (mn) mn.value = d.score_min;
    document.getElementById("cfg-high-val").textContent = "0.85";
    document.getElementById("cfg-min-val").textContent = "0.60";
  },

  /* ---------- 对话模型面板 ---------- */
  async initModelPicker() {
    const btn = document.getElementById("config-model-btn");
    const menu = document.getElementById("config-model-menu");
    const label = document.getElementById("config-model-label");
    if (!btn || !menu || !label) return;
    if (this.settings && this.settings.models) this.models = this.settings.models;
    if (!this.models.length) return;
    // 只绑定一次事件;之后刷新仅更新展示
    if (!this._modelPickerBound) {
      this._modelPickerBound = true;
      btn.onclick = (e) => {
        e.stopPropagation();
        menu.classList.contains("hidden") ? this._openModelMenu() : this._closeModelMenu();
      };
      menu.onclick = async (e) => {
        const item = e.target.closest(".model-picker-item");
        if (!item) return;
        const picked = item.dataset.model;
        this._pendingModel = picked;
        document.getElementById("config-model-label").textContent = picked;
        // 选即生效:立即写全局默认模型(下次提问走该模型)+ 同步聊天徽标。
        try {
          await API.put("/api/settings", { model: { default: picked } });
          if (this.settings) this.settings.model_default = picked;
        } catch (err) {
          console.error("保存默认模型失败:", err);
        }
        // 同步聊天输入框旁的模型徽标 + 持久化
        try {
          if (typeof ModelPicker !== "undefined" && ModelPicker.setModel) {
            ModelPicker.setModel(picked);
          } else {
            localStorage.setItem("chat-model", picked);
          }
        } catch (_) { /* 忽略 */ }
        this._closeModelMenu();
      };
    }
    // 同步当前默认模型到 label(用最新 settings,而非首次的旧 cur)
    const cur = (this.settings && this.settings.model_default) || this.models[0];
    label.textContent = cur;
    this._renderModelMenu(cur);
  },

  _openModelMenu() {
    const menu = document.getElementById("config-model-menu");
    document.getElementById("config-model-picker").classList.add("open");
    menu.classList.remove("hidden");
    // 每次打开都按最新默认高亮
    const cur = document.getElementById("config-model-label").textContent;
    this._renderModelMenu(cur);
  },

  _closeModelMenu() {
    document.getElementById("config-model-picker").classList.remove("open");
    document.getElementById("config-model-menu").classList.add("hidden");
  },

  _renderModelMenu(cur) {
    const menu = document.getElementById("config-model-menu");
    if (!menu || !this.models.length) return;
    menu.innerHTML = this.models.map((m) =>
      `<button type="button" class="model-picker-item${m === cur ? " active" : ""}" data-model="${m}">${m}</button>`
    ).join("");
  },

  fillModelValues() {
    if (!this.settings) return;
    const label = document.getElementById("config-model-label");
    if (label && this.settings.model_default) label.textContent = this.settings.model_default;
    const t = document.getElementById("cfg-temp");
    const tv = document.getElementById("cfg-temp-val");
    const temp = this.settings.temperature;
    if (t) t.value = temp;
    if (tv) tv.textContent = Number(temp).toFixed(1);
    this.fillKeyCard();
  },

  /* ---------- API Key 卡片:反映"是否已配置"(DB 或 .env),不回显明文 ---------- */
  fillKeyCard() {
    const state = document.getElementById("cfg-api-key-state");
    const input = document.getElementById("cfg-api-key");
    if (!state || !input) return;
    const configured = this.settings && this.settings.api_key_configured;
    const isDb = this.settings && this.settings.api_key_is_db;
    // 三种来源状态:DB 自设 / .env 兜底 / 都无
    if (isDb) {
      state.textContent = "✓ 已在系统配置填写(优先于此 .env)";
      state.classList.add("cfg-ok");
    } else if (configured) {
      state.textContent = "使用 .env 中的 key(点击下方「清除」无此 key 可清,如需换请在输入框填新 key)";
      state.classList.remove("cfg-ok");
    } else {
      state.textContent = "未配置:请填入 API Key 才能使用";
      state.classList.remove("cfg-ok");
    }
    // 编辑态:不预填任何值(避免把已存 key 暴露到 DOM),placeholder 提示
    input.value = "";
    input.placeholder = isDb ? "已填 key(留空保存=不改;想换则输入新 key)" : "输入你的 DashScope API Key(sk-…)";
  },

  async saveApiKey() {
    const input = document.getElementById("cfg-api-key");
    const key = (input && input.value || "").trim();
    const isDb = this.settings && this.settings.api_key_is_db;
    if (!key && isDb) return this.feedback("cfg-feedback-model", "未改动:当前 DB 已有关键,留空保存=不更换", false);
    if (!key) return alert("请输入要保存的 API Key");
    const state = document.getElementById("cfg-api-key-state");
    if (state) state.textContent = "保存中…";
    try {
      await API.put("/api/settings", { model: { api_key: key } });
      input.value = "";
      this.feedback("cfg-feedback-model", "API Key 已保存 · 下次提问起全部模型服务生效", true);
      await this.loadSettings();
      this.fillKeyCard();
    } catch (e) {
      this.feedback("cfg-feedback-model", "API Key 保存失败:" + e.message, false);
    }
  },

  async clearApiKey() {
    // 只有当 DB 里确实有自设 key 时"清除"才有意义;否则提示来源是 .env,无法在此清除。
    const isDb = this.settings && this.settings.api_key_is_db;
    if (!isDb) {
      return this.feedback("cfg-feedback-model",
        "当前用的是 .env 中的 key(不在系统配置保存)。如需改用别的 key,直接在输入框填新 key 保存即可", false);
    }
    if (!confirm("清除系统配置里保存的 API Key,改用 .env 中的 key?")) return;
    try {
      await API.put("/api/settings", { model: { api_key: "" } });
      this.feedback("cfg-feedback-model", "已清除系统配置里的 key · 现回退 .env", true);
      await this.loadSettings();
      this.fillKeyCard();
    } catch (e) {
      this.feedback("cfg-feedback-model", "清除失败:" + e.message, false);
    }
  },

  async saveModel() {
    // 优先取用户本次在下拉里选的;否则取当前展示的模型(排除"加载中…"占位)。
    const labelEl = document.getElementById("config-model-label");
    const shown = labelEl && labelEl.textContent.trim();
    const modelDefault = this._pendingModel ||
      (shown && shown !== "加载中…" ? shown : (this.settings && this.settings.model_default) || "");
    if (!modelDefault) return this.feedback("cfg-feedback-model", "请先在模型下拉选择默认模型", false);
    const body = {
      model: {
        default: modelDefault,
        temperature: Number(document.getElementById("cfg-temp").value),
      },
    };
    try {
      await API.put("/api/settings", body);
      this.feedback("cfg-feedback-model", "已保存 · 下次提问生效(新模型/温度)", true);
      await this.loadSettings();
    } catch (e) {
      this.feedback("cfg-feedback-model", "保存失败:" + e.message, false);
    }
  },

  resetModel() {
    const m = document.getElementById("config-model-label");
    if (m) m.textContent = (this.settings && this.settings.models && this.settings.models[0]) || "qwen3.8-flash";
    const t = document.getElementById("cfg-temp");
    if (t) t.value = 0.7;
    document.getElementById("cfg-temp-val").textContent = "0.7";
  },

  /* ---------- 工具面板 ---------- */
  renderTools() {
    const builtin = document.getElementById("cfg-tools-builtin");
    const external = document.getElementById("cfg-tools-external");
    if (!builtin || !external) return;
    const b = this.tools.filter((t) => t.kind === "builtin");
    const e = this.tools.filter((t) => t.kind === "external");
    builtin.innerHTML = b.length ? b.map((t) => this.builtinRow(t)).join("") :
      `<p class="muted">无内置工具</p>`;
    external.innerHTML = e.length ? e.map((t) => this.externalRow(t)).join("") : "";
    document.getElementById("cfg-tools-external-empty").style.display = e.length ? "none" : "";
    // 绑定开关
    builtin.querySelectorAll("[data-toggle-key]").forEach((el) =>
      el.addEventListener("change", (ev) => this.toggleTool(ev.target.dataset.toggleKey, ev.target.checked)));
    external.querySelectorAll("[data-toggle-key]").forEach((el) =>
      el.addEventListener("change", (ev) => this.toggleTool(ev.target.dataset.toggleKey, ev.target.checked)));
    external.querySelectorAll("[data-del-key]").forEach((el) =>
      el.addEventListener("click", (ev) => this.removeExternal(ev.target.dataset.delKey)));
    external.querySelectorAll("[data-edit-key]").forEach((el) =>
      el.addEventListener("click", (ev) => this.editExternal(ev.target.dataset.editKey)));
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  builtinRow(t) {
    const note = t.note ? `<span class="cfg-note muted">${this.esc(t.note)}</span>` : "";
    return `
      <div class="tool-row">
        <div class="tool-row-main">
          <div class="tool-row-title">${this.esc(t.label)}<span class="cfg-key">${this.esc(t.key)}</span></div>
          ${note}
        </div>
        <label class="switch">
          <input type="checkbox" data-toggle-key="${this.esc(t.key)}" ${t.enabled ? "checked" : ""}>
          <span class="switch-slider"></span>
        </label>
      </div>`;
  },

  externalRow(t) {
    const cfg = t.config || {};
    const loc = t.transport === "http"
      ? (cfg.url || "")
      : `${cfg.command || ""} ${(cfg.args || []).join(" ")}`;
    return `
      <div class="tool-row">
        <div class="tool-row-main">
          <div class="tool-row-title">
            ${this.esc(t.label)}
            <span class="cfg-key">${this.esc(t.key)}</span>
            <span class="fmt-badge" style="--fmt-c:#3E7BFA">${this.esc(t.transport || "stdio")}</span>
          </div>
          <div class="cfg-loc muted" title="${this.esc(loc)}">${this.esc(loc || "未配置")}</div>
        </div>
        <div class="tool-row-ops">
          <label class="switch">
            <input type="checkbox" data-toggle-key="${this.esc(t.key)}" ${t.enabled ? "checked" : ""}>
            <span class="switch-slider"></span>
          </label>
          <button class="icon-btn btn-edit-ext" data-edit-key="${this.esc(t.key)}" title="编辑外部工具(连接/显示名)">
            ${ic("edit", "ic-sm")}
          </button>
          <button class="icon-btn btn-del-ext" data-del-key="${this.esc(t.key)}" title="删除外部工具">
            ${ic("trash", "ic-sm")}
          </button>
        </div>
      </div>`;
  },

  async toggleTool(key, enabled) {
    try {
      const d = await API.put(`/api/tools/${encodeURIComponent(key)}`, { enabled });
      // 更新本地缓存状态
      const t = this.tools.find((x) => x.key === key);
      if (t) t.enabled = enabled;
    } catch (e) {
      alert("开关工具失败:" + e.message);
      await this.loadTools();
      this.renderTools();
    }
  },

  async removeExternal(key) {
    if (!confirm(`确定删除外部工具「${key}」?删除后需重新注册。`)) return;
    try {
      await API.del(`/api/tools/${encodeURIComponent(key)}`);
      await this.loadTools();
      this.renderTools();
    } catch (e) {
      alert("删除失败:" + e.message);
    }
  },

  /* ---------- 新增 / 编辑外部工具弹窗 ---------- */
  // 当前正在编辑的外部工具 key(为 null 表示新增模式)
  _editingKey: null,

  /** 打开"新增"弹窗(空表单) */
  openAddExternal() {
    this._editingKey = null;
    document.getElementById("ext-modal-title").innerHTML =
      `${ic("gear", "ic-sm")} 新增外部 MCP 工具`;
    document.getElementById("btn-exttool-save").innerHTML =
      `${ic("check", "ic-sm")} 探测并注册`;
    // 清空表单
    document.getElementById("ext-key").value = "";
    document.getElementById("ext-key").readOnly = false;
    document.getElementById("ext-key").title = "";
    document.getElementById("ext-label").value = "";
    document.getElementById("ext-command").value = "";
    document.getElementById("ext-args").value = "";
    document.getElementById("ext-url").value = "";
    document.querySelector('input[name="ext-transport"][value="stdio"]').checked = true;
    document.getElementById("ext-stdio-fields").classList.remove("hidden");
    document.getElementById("ext-http-fields").classList.add("hidden");
    document.getElementById("ext-feedback").textContent = "";
    document.getElementById("external-tool-modal").classList.remove("hidden");
  },

  /** 打开"编辑"弹窗并预填该外部工具当前值 */
  editExternal(key) {
    const t = this.tools.find((x) => x.key === key);
    if (!t) return alert("工具不存在");
    this._editingKey = key;
    document.getElementById("ext-modal-title").innerHTML =
      `${ic("gear", "ic-sm")} 编辑外部工具 · ${this.esc(key)}`;
    document.getElementById("btn-exttool-save").innerHTML =
      `${ic("check", "ic-sm")} 保存更改`;
    document.getElementById("ext-key").value = key;
    document.getElementById("ext-key").readOnly = true; // key 不可改(是唯一标识)
    document.getElementById("ext-key").title = "工具 key 为唯一标识,创建后不可修改";
    document.getElementById("ext-label").value = t.label || key;
    const cfg = t.config || {};
    const transport = t.transport || "stdio";
    const stdio = transport === "stdio";
    document.querySelector(`input[name="ext-transport"][value="${stdio ? "stdio" : "http"}"]`).checked = true;
    document.getElementById("ext-stdio-fields").classList.toggle("hidden", !stdio);
    document.getElementById("ext-http-fields").classList.toggle("hidden", stdio);
    document.getElementById("ext-command").value = cfg.command || "";
    document.getElementById("ext-args").value = (cfg.args || []).join(" ");
    document.getElementById("ext-url").value = cfg.url || "";
    document.getElementById("ext-feedback").textContent = "";
    document.getElementById("external-tool-modal").classList.remove("hidden");
  },

  /** 提交:新增走 POST(探测),编辑走 PUT(重新探测新配置)。 */
  async submitExternal() {
    const isEdit = !!this._editingKey;
    const key = (this._editingKey || document.getElementById("ext-key").value || "").trim();
    const label = (document.getElementById("ext-label").value || "").trim();
    const transport = (document.querySelector('input[name="ext-transport"]:checked') || {}).value || "stdio";
    let config = {};
    if (transport === "stdio") {
      const cmd = (document.getElementById("ext-command").value || "").trim();
      const args = (document.getElementById("ext-args").value || "").trim()
        .split(/\s+/).filter(Boolean);
      config = { command: cmd, args };
    } else {
      config = { url: (document.getElementById("ext-url").value || "").trim() };
    }
    if (!key) return alert("请填写工具 key");
    const fb = document.getElementById("ext-feedback");
    const saveBtn = document.getElementById("btn-exttool-save");
    fb.textContent = "正在连接探测新配置…";
    saveBtn.disabled = true;
    try {
      if (isEdit) {
        const d = await API.put(`/api/tools/${encodeURIComponent(key)}`,
          { label, transport, config, enabled: true });
        fb.textContent = `已保存并重新探测,暴露 ${(d.tool && "见工具列表") || "工具"} · 下次提问用新配置`;
      } else {
        const d = await API.post("/api/tools", { key, label, transport, config });
        fb.textContent = `注册成功,暴露 ${(d.mcp_tools || []).length} 个工具:${
          (d.mcp_tools || []).map((t) => t.name).join(", ") || "无"}`;
      }
      document.getElementById("external-tool-modal").classList.add("hidden");
      this._editingKey = null;
      await this.loadTools();
      this.renderTools();
    } catch (e) {
      fb.textContent = (isEdit ? "保存失败:" : "注册失败:") + e.message;
    } finally {
      saveBtn.disabled = false;
    }
  },

  /* ---------- 反馈 ---------- */
  feedback(id, msg, ok) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("muted", !ok);
    el.classList.toggle("cfg-ok", ok);
    setTimeout(() => { if (el) el.textContent = ""; }, 4000);
  },
};

/* ============================================
   事件绑定
   ============================================ */
document.addEventListener("DOMContentLoaded", () => {
  // 配置子栏:切换右侧面板
  document.querySelectorAll("#sidebar-config .sidebar-config-item").forEach((b) =>
    b.addEventListener("click", () => ConfigUI.switchPanel(b.dataset.configView)));

  // 检索面板
  ConfigUI._liveSync("cfg-high", "cfg-high-val", 2);
  ConfigUI._liveSync("cfg-min", "cfg-min-val", 2);
  document.getElementById("btn-cfg-save-retrieval").addEventListener("click", () => ConfigUI.saveRetrieval());
  document.getElementById("btn-cfg-reset-retrieval").addEventListener("click", () => ConfigUI.resetRetrieval());

  // 模型面板
  const tempSlider = document.getElementById("cfg-temp");
  if (tempSlider) tempSlider.addEventListener("input", () => {
    const v = document.getElementById("cfg-temp-val");
    if (v) v.textContent = Number(tempSlider.value).toFixed(1);
  });
  document.getElementById("btn-cfg-save-model").addEventListener("click", () => ConfigUI.saveModel());
  document.getElementById("btn-cfg-reset-model").addEventListener("click", () => ConfigUI.resetModel());
  const saveKey = document.getElementById("btn-cfg-save-key");
  if (saveKey) saveKey.addEventListener("click", () => ConfigUI.saveApiKey());
  const clearKey = document.getElementById("btn-cfg-clear-key");
  if (clearKey) clearKey.addEventListener("click", () => ConfigUI.clearApiKey());
  // 点外部关闭模型下拉
  document.addEventListener("click", (e) => {
    const mk = document.getElementById("config-model-picker");
    if (mk && !e.target.closest("#config-model-picker")) {
      mk.classList.remove("open");
      const m = document.getElementById("config-model-menu");
      if (m) m.classList.add("hidden");
    }
  });

  // 新增外部工具弹窗
  document.getElementById("btn-add-external-tool").addEventListener("click", () => ConfigUI.openAddExternal());
  const closeExt = () => { document.getElementById("external-tool-modal").classList.add("hidden"); ConfigUI._editingKey = null; };
  document.getElementById("btn-exttool-close").addEventListener("click", closeExt);
  document.getElementById("btn-exttool-cancel").addEventListener("click", closeExt);
  document.getElementById("external-tool-modal").addEventListener("click", (e) => {
    if (e.target.id === "external-tool-modal") closeExt();
  });
  // transport 切换字段
  document.querySelectorAll('input[name="ext-transport"]').forEach((r) =>
    r.addEventListener("change", () => {
      const stdio = r.value === "stdio";
      document.getElementById("ext-stdio-fields").classList.toggle("hidden", !stdio);
      document.getElementById("ext-http-fields").classList.toggle("hidden", stdio);
    }));
  document.getElementById("btn-exttool-save").addEventListener("click", () => ConfigUI.submitExternal());
});
