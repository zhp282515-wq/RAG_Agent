/* ============================================
   chat.js — 聊天页
   文字+图片输入 / SSE 流式渲染 / 工作流面板(trace)
   消息操作栏(复制/重新生成/溯源记录) / 历史回填
   ============================================ */

const ChatUI = {
  messages: [],          // 当前渲染的消息 [{role, content, sources, images}]
  pendingImages: [],     // 待发送图片 {name, path}
  pendingDocs: [],       // 待发送文档 {name, path}
  sending: false,
  lastUserQuery: null,   // 最近一次用户提问(重新生成用)
  currentHandle: null,   // 当前 SSE 句柄(中断用)

  /* ---------- 渲染 ---------- */
  renderHistory(msgs) {
    // 走一遍,给每条 assistant 配对它前一条 user 的请求(重新生成历史回答用)
    const out = [];
    let lastUserReq = null;
    for (const m of msgs) {
      if (m.role === "user") {
        lastUserReq = { query: m.content || "" };
        // 历史 user 消息里的图片/文档附件路径还原(若有)
        const imgs = (m.images || []).map((im) => typeof im === "string" ? { name: im.split(/[\\/]/).pop(), path: im } : { name: im.file_name || "", path: im.saved_path || im.path || "" });
        const docs = (m.docs || []).map((d) => typeof d === "string" ? { name: d, path: d } : { name: d.name || "", path: d.path || "" });
        lastUserReq.images = imgs.filter((x) => x.path);
        lastUserReq.docs = docs.filter((x) => x.path);
      }
      const item = {
        role: m.role,
        content: m.content || "",
        sources: m.sources || [],
        workflow: m.workflow || [],
        images: m.images || [],
        docs: m.docs || [],
      };
      if (m.role === "assistant" && lastUserReq) item._req = lastUserReq;
      out.push(item);
    }
    this.messages = out;
    this.draw();
  },

  showSessionLoading() {
    this.messages = [];
    this.draw();
  },

  draw() {
    const box = document.getElementById("chat-messages");
    box.innerHTML = "";
    if (!this.messages.length) {
      box.innerHTML = `
        <div id="chat-welcome" class="welcome">
          <h2>你好</h2>
          <p>我是你的知识库助手,基于已接入知识库,为你提供准确、可靠的信息检索与分析。</p>
        </div>`;
      return;
    }
    for (const m of this.messages) box.appendChild(this.buildMsg(m));
    box.scrollTop = box.scrollHeight;
  },

  buildMsg(m) {
    const wrap = document.createElement("div");
    wrap.className = `msg ${m.role}`;

    for (const img of m.images || []) {
      // 历史消息里 images 可能是路径字符串或 {file_name, saved_path} 对象,统一归一
      const path = typeof img === "string" ? img : (img && (img.saved_path || img.path || ""));
      if (!path) continue;
      const im = document.createElement("img");
      im.className = "msg-img";
      im.src = path.startsWith("http") ? path : `/uploads/${path.split(/[\\/]/).pop()}`;
      wrap.appendChild(im);
    }

    if (m.role === "user") {
      // 文档附件 chip(图片已由上方统一渲染)
      const docNames = (m.docs || []).map((d) => d.name).filter(Boolean);
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";
      bubble.textContent = m.content;
      wrap.appendChild(bubble);
      if (docNames.length) {
        const row = document.createElement("div");
        row.className = "msg-doc-attach";
        row.innerHTML = docNames.map((n) =>
          `<span class="doc-chip">${ic("file", "ic-xs")} <span class="doc-chip-name">${this.esc(n)}</span></span>`
        ).join("");
        wrap.appendChild(row);
      }
    } else {
      // 助手消息:玻璃卡片 = Logo 头 + 正文(markdown 渲染)
      const card = document.createElement("div");
      card.className = "msg-card";
      card.innerHTML = `
        <div class="msg-head">
          <span class="msg-logo"></span>
          <span class="msg-head-name">知识库助手</span>
        </div>`;
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble md-body";
      bubble.innerHTML = this.mdToHtml(m.content);
      card.appendChild(bubble);
      wrap.appendChild(card);

      // 注意:不要在此把 this._streamSources/_streamWorkflow 覆盖到历史消息上——
      // 它们是最新流式回答的全局指针,每条消息的 sources/workflow 已在
      // onSources/onTrace 归属到各自的 asst 对象,重绘时直接读 m.xxx 即可。

      if (m.content) {
        const actions = document.createElement("div");
        actions.className = "msg-actions";
        // 纯图标操作:悬停才显示名称 title
        actions.innerHTML = `
          <button class="act-copy" title="复制">${ic("copy","ic-sm")}</button>
          <button class="act-regen" title="重新生成">${ic("refresh","ic-sm")}</button>
          <button class="act-sources" title="溯源记录">${ic("link","ic-sm")}</button>
          <button class="act-wf" title="工作流">${ic("activity","ic-sm")}</button>`;
        actions.querySelector(".act-copy").addEventListener("click", () => this.copyText(m.content));
        actions.querySelector(".act-regen").addEventListener("click", () => this.regenerateFor(m));
        actions.querySelector(".act-sources").addEventListener("click", () => SourcesModal.open(m));
        actions.querySelector(".act-wf").addEventListener("click", () => WorkflowModal.open(m));
        wrap.appendChild(actions);
      }
    }
    return wrap;
  },

  /** markdown → 安全 HTML(先 marked 再 DOMPurify 过滤 XSS) */
  mdToHtml(md) {
    if (!md) return "";
    try {
      const raw = window.marked ? marked.parse(md) : md;
      return window.DOMPurify ? DOMPurify.sanitize(raw) : this.esc(md);
    } catch (_) {
      return this.esc(md);
    }
  },

  async copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  /* 重新生成:重发该回答对应的那轮用户提问(从当前渲染会话里该回答前一条 user 取) */
  regenerateFor(asstMsg) {
    if (this.sending) return;
    const req = (asstMsg && asstMsg._req) || null;
    const query = req ? req.query : (this.lastUserQuery || "");
    const images = req ? req.images : [...this.pendingImages];
    const docs = req ? req.docs : [...this.pendingDocs];
    if (!query && !images.length && !(docs && docs.length)) return;
    this.send(query, images, docs, true);
  },

  focusInput(hint) {
    const input = document.getElementById("chat-input");
    if (hint) input.placeholder = hint;
    input.focus();
  },

  /* ---------- 发送 ---------- */
  async send(query, images, docs, isRegen = false) {
    if (this.sending) return;
    const text = (query || "").trim();
    if (!text && !images.length && !(docs && docs.length)) return;
    if (!SessionStore.currentId) {
      await SessionStore.create();
      if (!SessionStore.currentId) return;
    }

    this.sending = true;
    this.lastUserQuery = text;
    this.messages.push({
      role: "user", content: text, sources: [], images: (images || []).map((i) => i.path),
      docs: (docs || []).map((d) => ({ name: d.name })),
    });
    const asst = { role: "assistant", content: "", sources: [], images: [], workflow: [] };
    // 记住本轮请求(重新生成时精确复用,而非依赖全局 lastUserQuery)
    asst._req = {
      query: text,
      images: (images || []).map((i) => ({ name: i.name, path: i.path })),
      docs: (docs || []).map((d) => ({ name: d.name, path: d.path })),
    };
    this.messages.push(asst);
    this.draw();
    this.setInputEnabled(false);
    // 发送即清空文本框与已选附件(不影响本轮已提交的 images/docs)
    const inputEl = document.getElementById("chat-input");
    inputEl.value = "";
    inputEl.style.height = "auto";
    this.clearPendingImages();
    this.clearPendingDocs();
    WorkflowPanel.start();

    const bubbleEl = () => {
      const nodes = document.querySelectorAll("#chat-messages .msg.assistant .msg-bubble");
      return nodes[nodes.length - 1];
    };

    // 动态指示徽标:放在回答卡片内(气泡之后),markdown 重绘不触碰它 → spinner 平滑转动
    LiveStatus.start();
    const syncBadge = () => { LiveStatus.attach(LiveStatus.currentCard()); };

    let doneFired = false;
    const modelName = ModelPicker.current();
    // —— 流式 + 实时 markdown 排版 ——
    // 输出过程中每隔一小段(基础 60ms)就把当前内容局部 markdown 渲染到气泡内,
    // 让标题/加粗/表格边出边成型;间隔按渲染耗时自适应:渲染快→跟手,渲染慢→降频防卡。
    // 超长内容(>30k 字符)自动退化为纯文本追加,收尾一次性排版,避免持续重解析卡顿。
    const MAX_LIVE_MD = 30000;
    let baseMs = 60;             // 基础渲染间隔
    let liveMd = false;          // 当前是否处于"实时 markdown"模式
    let tickTimer = null;
    let lastShownLen = 0;        // 上次已渲染进气泡的长度

    const isLong = () => asst.content.length > MAX_LIVE_MD;

    const renderMdLive = () => {
      const b = bubbleEl();
      if (!b) return;
      const t0 = performance.now();
      b.innerHTML = this.mdToHtml(asst.content);
      const cost = performance.now() - t0;
      // 自适应:单次渲染越久,间隔越拉大;渲染很轻则贴近 60ms 保持跟手
      baseMs = cost > 25 ? Math.min(320, baseMs + 40) : Math.max(60, baseMs - 10);
      lastShownLen = asst.content.length;
      this.scrollBottom();
    };

    const appendPlain = () => {
      const b = bubbleEl();
      if (!b) return;
      const chunk = asst.content.slice(lastShownLen);
      if (chunk) {
        b.appendChild(document.createTextNode(chunk));
        lastShownLen = asst.content.length;
        this.scrollBottom();
      }
    };

    const tick = () => {
      tickTimer = null;
      if (!this.sending) return;
      // 决定本帧走哪条渲染路径:短内容实时 markdown;超长走纯文本(防卡)
      const wantLive = !isLong();
      if (liveMd !== wantLive) {
        liveMd = wantLive;
        lastShownLen = 0;
        const b = bubbleEl();
        if (b) b.innerHTML = "";
      }
      if (liveMd) {
        if (asst.content.length !== lastShownLen) renderMdLive();
      } else {
        appendPlain();
      }
      // 仍在输出则按自适应间隔续拍
      if (this.sending) tickTimer = setTimeout(tick, baseMs);
    };

    // token 到达:先即时把文本也喂给 content 累积,再由 tick 定时排版
    const kick = () => {
      if (tickTimer === null) tickTimer = setTimeout(tick, 24);
    };

    this.currentHandle = sseRequest({
      url: "/api/chat",
      body: {
        query: text,
        session_id: SessionStore.currentId,
        image_paths: (images || []).map((i) => i.path),
        doc_paths: (docs || []).map((d) => d.path),
        model: modelName || "",
      },
      onToken: (t) => {
        asst.content += t;
        LiveStatus.onOutput();  // 模型开始吐字 → "解答中"
        kick();
      },
      onTrace: (tr) => {
        WorkflowPanel.addStep(tr);
        asst.workflow.push(tr);
        LiveStatus.onEvent(tr);
      },
      onSources: (srcs) => {
        asst.sources = srcs || [];
        this.draw();  // 重绘整屏:每答从各自的 m.sources 渲染,溯源记录可用
        lastShownLen = 0;
        syncBadge();  // draw 重建卡片,把徽标挂回当前回答卡片内
      },
      onDone: () => { doneFired = true; },
      onError: (msg) => {
        if (!asst.content) asst.content = `【服务异常】${msg}`;
        const b = bubbleEl();
        if (b) b.innerHTML = this.mdToHtml(asst.content);
      },
    });

    await this.currentHandle.promise;
    LiveStatus.stop();  // 生成结束,移除动态徽标
    if (tickTimer !== null) { clearTimeout(tickTimer); tickTimer = null; }
    // 收尾:确保最终内容以完整 markdown 呈现
    const b = bubbleEl();
    if (b) b.innerHTML = this.mdToHtml(asst.content);
    this.sending = false;
    this.setInputEnabled(true);
    WorkflowPanel.finish();
    this.draw();
    SessionStore.refreshAfterChat(); // 会话标题已生成,刷新侧边栏
  },

  scrollBottom() {
    const box = document.getElementById("chat-messages");
    box.scrollTop = box.scrollHeight;
  },

  setInputEnabled(on) {
    document.getElementById("btn-send").disabled = !on;
    document.getElementById("chat-input").disabled = !on;
  },

  /* ---------- 图片 ---------- */
  async addImages(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      try {
        const data = await API.upload("/api/upload", f);
        this.pendingImages.push({ name: f.name, path: data.path });
      } catch (e) {
        alert(`图片上传失败:${e.message}`);
      }
    }
    this.renderPendingImages();
  },

  renderPendingImages() {
    const box = document.getElementById("chat-images-preview");
    box.innerHTML = "";
    this.pendingImages.forEach((img, idx) => {
      const w = document.createElement("div");
      w.className = "chat-img-preview";
      w.innerHTML = `<img src="/uploads/${img.path.split(/[\\/]/).pop()}" alt="${img.name}">
                     <button class="img-remove" title="移除">${ic("x")}</button>`;
      w.querySelector(".img-remove").addEventListener("click", () => {
        this.pendingImages.splice(idx, 1);
        this.renderPendingImages();
      });
      box.appendChild(w);
    });
  },

  clearPendingImages() {
    this.pendingImages = [];
    document.getElementById("chat-images-preview").innerHTML = "";
  },

  /* ---------- 文档附件 ---------- */
  async addDocs(files) {
    for (const f of files) {
      try {
        const data = await API.upload("/api/upload", f);
        this.pendingDocs.push({ name: f.name, path: data.path });
      } catch (e) {
        alert(`文档上传失败:${e.message}`);
      }
    }
    this.renderPendingDocs();
  },

  renderPendingDocs() {
    const box = document.getElementById("chat-docs-preview");
    box.innerHTML = "";
    this.pendingDocs.forEach((doc, idx) => {
      const w = document.createElement("span");
      w.className = "doc-chip";
      w.innerHTML = `${ic("file", "ic-xs")} <span class="doc-chip-name">${this.esc(doc.name)}</span>
        <button class="doc-chip-remove" title="移除">${ic("x", "ic-xs")}</button>`;
      w.querySelector(".doc-chip-remove").addEventListener("click", () => {
        this.pendingDocs.splice(idx, 1);
        this.renderPendingDocs();
      });
      box.appendChild(w);
    });
  },

  clearPendingDocs() {
    this.pendingDocs = [];
    const box = document.getElementById("chat-docs-preview");
    if (box) box.innerHTML = "";
  },
};

/* ---------- 工作流:阶段渲染共用 ---------- */
const WF = {
  /**
   * 把 trace 事件序列组装成详细阶段列表。
   * - phase_start/phase_end 配对成"阶段"(可重复,如多轮 模型/检索);
   * - substep 事件挂在最近一个未闭合阶段的 children 里 → 工作流精确到每一步;
   * - 阶段会按上下文推断动作语义(模型:首次=理解规划,检索后=整合生成)。
   */
  buildStages(events) {
    const stages = [];
    const openByPh = {};      // ph_id -> 未闭合阶段的 index(精确归属)
    const openSeq = [];       // 未闭合阶段 index 顺序(补全语义用)
    const now = performance.now();
    let retrievedOnce = false;

    const pushSub = (parentIdx, ev) => {
      const s = stages[parentIdx];
      if (!s) return;
      s.children.push({
        name: ev.name || "步骤",
        params: ev.params || {},
        dur: ev.duration != null ? Number(ev.duration) : null,
      });
    };

    for (const ev of events || []) {
      const step = ev.step || "未知";
      const act = ev.action || "phase";
      const ph = ev.ph != null ? ev.ph : null;

      if (act === "phase_start") {
        if (step === "检索") retrievedOnce = true;
        const idx = stages.length;
        stages.push({
          step, name: ev.name || "",
          params: ev.params || {},
          realT0: now, dur: null, state: "running",
          children: [],
        });
        if (ph != null) openByPh[ph] = idx;
        openSeq.push(idx);
      } else if (act === "phase_end") {
        // 优先按 ph_id 精确定位(不同名/同名的阶段都不串)
        let found = -1;
        if (ph != null && openByPh[ph] != null) {
          found = openByPh[ph];
          delete openByPh[ph];
          const oi = openSeq.indexOf(found);
          if (oi >= 0) openSeq.splice(oi, 1);
        } else {
          // 兼容无 ph 的历史事件:从尾部找同名未闭合
          for (let i = openSeq.length - 1; i >= 0; i--) {
            if (stages[openSeq[i]].step === step) { found = openSeq[i]; openSeq.splice(i, 1); break; }
          }
        }
        if (found >= 0) {
          stages[found].dur = ev.duration != null ? Number(ev.duration) : null;
          stages[found].state = "done";
          if (ev.params) stages[found].params = Object.assign({}, stages[found].params, ev.params);
        } else {
          stages.push({
            step, name: ev.name || "", params: ev.params || {}, realT0: now,
            dur: ev.duration != null ? Number(ev.duration) : null, state: "done", children: [],
          });
        }
      } else if (act === "substep") {
        // 细化步骤:按 ph_id 精确挂到所属阶段(而非"最近未闭合"——交错/同名时不再错挂)
        let parentIdx = -1;
        if (ph != null && openByPh[ph] != null) parentIdx = openByPh[ph];
        else parentIdx = openSeq.length ? openSeq[openSeq.length - 1] : stages.length - 1;
        if (parentIdx >= 0) pushSub(parentIdx, ev);
      } else {
        // 独立事件(瞬时提示)
        stages.push({
          step, name: ev.name || "", params: ev.params || {}, realT0: now,
          dur: ev.duration != null ? Number(ev.duration) : null, state: "done", children: [],
        });
      }
    }
    // 流式结束仍 running 的补为完成、无耗时(不该出现,防御)
    for (const s of stages) if (s.state === "running") { s.state = "done"; s.dur = null; }

    // 给"模型"阶段按位置定语义:
    //   开头/检索前 → 理解问题并规划检索;检索后(且非最后) → 评估结果决定下一步;
    //   最后一个模型 → 整合资料生成回答。检索阶段始终"检索并精排资料"。
    const lastModelIdx = (() => { let m = -1; stages.forEach((s, i) => { if (s.step === "模型") m = i; }); return m; })();
    let seenRetrieval = false;
    stages.forEach((s, i) => {
      if (s.step === "模型") {
        if (i === lastModelIdx && seenRetrieval) s.semantic = "整合检索资料,生成最终回答";
        else if (seenRetrieval) s.semantic = "评估检索结果,判断是否足够作答";
        else s.semantic = "理解问题,规划检索方案与调用工具";
      } else if (s.step === "检索") {
        seenRetrieval = true;
        s.semantic = "在知识库中向量召回 → rerank 精排 → 达标过滤";
      } else if (s.step === "报告场景") {
        s.semantic = "读取并填充报告所需的用户使用数据";
      }
    });
    return stages;
  },

  /** 阶段/子步骤的"在干什么"说明 */
  stepDesc(s) {
    if (s.semantic) return s.semantic;
    return "";
  },

  fmtDur(sec) {
    if (sec == null) return "";
    return (sec >= 60 ? (sec / 60).toFixed(1) + " 分钟" : Number(sec).toFixed(2) + "s");
  },

  /** 阶段 → 左侧标记(绿勾=完成,spinner=进行中) */
  iconFor(s) {
    return s.state === "running"
      ? `<span class="wf-spinner"></span>`
      : `<span class="wf-done-ic">${ic("check", "ic-xs")}</span>`;
  },

  /** 阶段正在进行的动作描述(进行中可视化文案) */
  runningAction(s) {
    const step = s.step || "";
    if (step === "模型") return s.semantic || "模型正在思考…";
    if (step === "检索") return "正在检索知识库…";
    if (step === "报告场景") return "正在读取报告数据…";
    if (s.name && s.name !== step) return `正在执行 ${s.name}…`;
    return `${step} 处理中…`;
  },

  /** 重要参数可视化(检索词/命中数/用户/月份/上下文条数/子步) */
  paramsText(params) {
    if (!params) return "";
    const parts = [];
    if (params.query) parts.push(`检索词:${params.query}`);
    if (params["命中"] != null) parts.push(`命中 ${params["命中"]} 条`);
    if (params["用户"]) parts.push(`用户 ${params["用户"]}`);
    if (params["月份"]) parts.push(`月份 ${params["月份"]}`);
    if (params["消息"] != null) parts.push(`${params["消息"]} 条上下文`);
    if (params["候选数"] != null) parts.push(`${params["候选数"]} 个候选`);
    if (params["通过"] != null) parts.push(`通过 ${params["通过"]} 条`);
    if (params["剔除低相关"] != null) parts.push(`剔除 ${params["剔除低相关"]} 条`);
    if (params["说明"]) parts.push(`${params["说明"]}`);
    if (params["阶段"]) parts.push(`${params["阶段"]}`);
    return parts.join(" · ");
  },

  /** 阶段行 DOM(面板与弹窗共用):阶段 + 语义说明 + 其细化子步骤 */
  stageRow(s) {
    const el = document.createElement("div");
    el.className = "wf-step " + (s.state === "running" ? "running" : "done");
    const durLabel = s.state === "running"
      ? `<span class="wf-running" data-real="${s.realT0}">${WF.runningAction(s)} (${WF.fmtDur((performance.now() - s.realT0) / 1000)})</span>`
      : (s.dur != null ? `<span class="wf-step-dur">${WF.fmtDur(s.dur)}</span>` : "");
    const tool = (s.name && s.name !== s.step) ? ` <em class="wf-step-tool">${WF.esc(s.name)}</em>` : "";
    const sub = WF.paramsText(s.params);
    const desc = WF.stepDesc(s);

    // 细化子步骤:放进 main 内 → 左缘天然对齐阶段标题,而非顶格到图标列
    let subsHtml = "";
    if (s.children && s.children.length) {
      const rows = s.children.map((c) => {
        const cDur = c.dur != null ? `<span class="wf-step-dur">${WF.fmtDur(c.dur)}</span>` : "";
        const cSub = WF.paramsText(c.params);
        return `
          <div class="wf-substep">
            <span class="wf-substep-dot"></span>
            <div class="wf-substep-main">
              <div class="wf-step-top">
                <span class="wf-substep-title"><span class="wf-substep-name">${WF.esc(c.name)}</span></span>${cDur}
              </div>
              ${cSub ? `<div class="wf-step-params">${WF.esc(cSub)}</div>` : ""}
            </div>
          </div>`;
      }).join("");
      subsHtml = `<div class="wf-substeps">${rows}</div>`;
    }

    // 阶段主体行
    el.innerHTML = `
      <span class="wf-step-icon">${WF.iconFor(s)}</span>
      <div class="wf-step-main">
        <div class="wf-step-top">
          <span class="wf-step-title"><span class="wf-step-name">${WF.esc(s.step)}</span>${tool}</span>
          ${durLabel}
        </div>
        ${desc ? `<div class="wf-step-desc">${WF.esc(desc)}</div>` : ""}
        ${sub ? `<div class="wf-step-params">${WF.esc(sub)}</div>` : ""}
        ${subsHtml}
      </div>`;
    return el;
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

/* ---------- 右侧常驻工作流面板 ---------- */
const WorkflowPanel = {
  _timer: null,

  start() {
    this._stop();
    document.getElementById("workflow-steps").innerHTML = "";
  },

  /** 事件到达 → 重列当前回答的阶段,并对进行中阶段做实时计时 */
  render(events) {
    const box = document.getElementById("workflow-steps");
    box.innerHTML = "";
    const stages = WF.buildStages(events || []);
    for (const s of stages) box.appendChild(WF.stageRow(s));
    box.scrollTop = box.scrollHeight;
    const hasRunning = stages.some((s) => s.state === "running");
    if (hasRunning) this._tick(); else this._stop();
  },

  _tick() {
    this._stop();
    const run = () => {
      const box = document.getElementById("workflow-steps");
      const running = box.querySelectorAll(".wf-step.running");
      if (!running.length) { this._stop(); return; }
      running.forEach((el) => {
        const span = el.querySelector(".wf-running[data-real]");
        if (!span) return;
        // 由阶段 step 推导动作文案(模型→正在生成/检索→正在检索…)
        const stepEl = el.querySelector(".wf-step-name");
        const stepText = stepEl ? stepEl.childNodes[0].textContent.trim() : "";
        const secs = (performance.now() - Number(span.dataset.real)) / 1000;
        span.textContent = WF.runningAction({ step: stepText }) + " (" + WF.fmtDur(secs) + ")";
      });
    };
    run();
    this._timer = setInterval(run, 200);
  },

  _stop() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  },

  addStep() {
    // trace 事件已 push 进当前回答 workflow → 直接重渲染
    const last = ChatUI.messages[ChatUI.messages.length - 1];
    this.render(last && last.workflow ? last.workflow : []);
  },

  finish() {
    this._stop();
    const last = ChatUI.messages[ChatUI.messages.length - 1];
    const events = (last && last.workflow) ? last.workflow : [];
    this.render(events);
    // 统计各阶段耗时合计,底部补总耗时(排除"完成"占位)
    const done = events.filter((e) => e.action === "phase_end" && e.duration != null);
    if (done.length) {
      const total = done.reduce((s, e) => s + Number(e.duration), 0);
      const box = document.getElementById("workflow-steps");
      const el = document.createElement("div");
      el.className = "wf-step done wf-total";
      el.innerHTML = `
        <span class="wf-step-icon"><span class="wf-done-ic">${ic("check", "ic-xs")}</span></span>
        <div class="wf-step-main">
          <div class="wf-step-top"><span class="wf-step-name">执行完成</span>
            <span class="wf-step-dur">共 ${WF.fmtDur(total)}</span></div>
        </div>`;
      box.appendChild(el);
      box.scrollTop = box.scrollHeight;
    }
  },
};

/* ---------- 单条回答的工作流弹窗(消息操作栏入口) ---------- */
const WorkflowModal = {
  open(msg) {
    const body = document.getElementById("wf-modal-body");
    const events = msg.workflow || [];
    if (!events.length) {
      body.innerHTML = `<p class="muted">该回答暂无工作流记录(未经过检索或工具执行)。</p>`;
      document.getElementById("wf-modal").classList.remove("hidden");
      return;
    }
    body.innerHTML = "";
    const stages = WF.buildStages(events);
    const list = document.createElement("div");
    for (const s of stages) list.appendChild(WF.stageRow(s));
    body.appendChild(list);
    const done = (events || []).filter((e) => e.action === "phase_end" && e.duration != null);
    if (done.length) {
      const total = done.reduce((s, e) => s + Number(e.duration), 0);
      const foot = document.createElement("div");
      foot.className = "wf-step done wf-total";
      foot.innerHTML = `
        <span class="wf-step-icon"><span class="wf-done-ic">${ic("check", "ic-xs")}</span></span>
        <div class="wf-step-main"><div class="wf-step-top">
          <span class="wf-step-name">执行完成</span>
          <span class="wf-step-dur">共 ${WF.fmtDur(total)}</span>
        </div></div>`;
      body.appendChild(foot);
    }
    document.getElementById("wf-modal").classList.remove("hidden");
  },

  close() {
    document.getElementById("wf-modal").classList.add("hidden");
  },
};

/* ---------- 溯源记录弹窗 ---------- */
const SourcesModal = {
  open(msg) {
    const body = document.getElementById("sources-modal-body");
    const srcs = msg.sources || [];
    if (!srcs.length) {
      body.innerHTML = `<p class="muted">该回答暂无检索溯源记录</p>`;
    } else {
      body.innerHTML = srcs.map((s, i) => `
        <div class="hit-card">
          <div class="hit-head">
            <span class="badge ${s.label === "高" ? "high" : "mid"}">${s.label || "中"}</span>
            <span class="hit-score">${Number(s.score || 0).toFixed(3)}</span>
            <span class="hit-meta">${this.esc(s.file_name || "")}${s.page ? " · 第" + s.page + "页" : ""}${s.chapter ? " · " + this.esc(s.chapter) : ""}${s.section ? " · " + this.esc(s.section) : ""}</span>
          </div>
          <div class="hit-text">${this.esc(s.text || "")}</div>
        </div>`).join("");
    }
    document.getElementById("sources-modal").classList.remove("hidden");
  },

  close() {
    document.getElementById("sources-modal").classList.add("hidden");
  },

  esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

/* ---------- 生成中动态指示(内联于回答文本末尾,替代闪烁光标) ---------- */
const LiveStatus = {
  _timer: null,
  _start: 0,
  _curLabel: "",
  _el: null,
  _phaseStartedAt: 0,

  /** 生成开始:创建内联徽标(spinner/文字/计时各建一次,避免每次重建让 spinner 卡顿) */
  start() {
    this.stop();
    this._start = performance.now();
    this._phaseStartedAt = this._start;
    this._curLabel = "准备中…";
    this._el = document.createElement("span");
    this._el.className = "ls-badge";
    const sp = document.createElement("span");
    sp.className = "ls-spinner";
    this._labelEl = document.createElement("span");
    this._labelEl.className = "ls-text";
    this._timeEl = document.createElement("span");
    this._timeEl.className = "ls-time";
    this._el.append(sp, this._labelEl, this._timeEl);
    this._render();
    this._timer = setInterval(() => this._render(), 100);
    const card = this.currentCard();
    if (card) this.attach(card);
  },

  /** 挂到卡片内、气泡之后(作为气泡兄弟节点,避开 innerHTML 重绘),徽标动画不被中断 */
  attach(card) {
    if (!this._el || !card) return;
    if (this._el.parentNode !== card) card.appendChild(this._el);
  },

  /** 取当前正在生成那条 AI 回答的卡片节点 */
  currentCard() {
    const cards = document.querySelectorAll(".msg.assistant .msg-card");
    return cards.length ? cards[cards.length - 1] : null;
  },

  /** 模型开始吐字 → 切"解答中"(输出阶段) */
  onOutput() {
    if (!this._el) return;
    this._curLabel = "解答中";
    this._render();
  },

  /** trace 事件到达:更新"当前在做什么"文案 */
  onEvent(tr) {
    if (!this._el) return;
    const act = tr.action;
    const step = tr.step || "";
    if (act === "phase_start") {
      this._phaseStartedAt = performance.now();
      if (step === "模型") this._curLabel = "模型思考中";
      else if (tr.name && tr.name !== step) this._curLabel = tr.name;
      else this._curLabel = step;
    } else if (act === "substep") {
      this._curLabel = tr.name || this._curLabel;
      this._phaseStartedAt = performance.now();
    } else if (act === "phase_end") {
      this._phaseStartedAt = performance.now();
      this._curLabel = "处理中…";
    }
    this._render();
  },

  /** 流结束:移除徽标 */
  stop() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this._el) { this._el.remove(); this._el = null; }
  },

  /** 只更新文字与计时,不重建 spinner(避免动画被打断卡顿) */
  _render() {
    if (!this._el) return;
    const totalSec = ((performance.now() - this._start) / 1000).toFixed(1);
    if (this._labelEl) this._labelEl.textContent = this._curLabel;
    if (this._timeEl) this._timeEl.textContent = `${totalSec}s`;
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
};

/* ---------- 当前对话模型只读徽标 ----------
   模型切换的唯一入口收敛到「系统配置 → 对话模型」(会写 DB + localStorage)。
   聊天输入框旁仅展示当前模型;点「切换」跳到系统配置页对话模型卡片。
   ModelPicker.setModel() 由系统配置页在选定模型时调用,实时同步本徽标。
 */
const ModelPicker = {
  models: [],
  currentModel: null,

  /** 当前选中的模型名(发送时随请求带上) */
  current() {
    return this.currentModel || "";
  },

  /** 程序化切换当前模型(系统配置页选定「默认模型」时同步);更新徽标 + 持久化。 */
  setModel(model, persist = true) {
    const name = String(model || "").trim();
    if (!name) return;
    this.currentModel = name;
    if (persist) localStorage.setItem("chat-model", name);
    const label = document.getElementById("chat-model-label");
    if (label) label.textContent = name;
  },

  /** 跳去系统配置页的「对话模型」卡片 */
  gotoConfig() {
    const go = async () => {
      App.switchView("config");
      // 顶层 const ConfigUI 不挂 window,用 typeof 判断即可
      if (typeof ConfigUI !== "undefined" && ConfigUI.switchPanel) {
        // 先确保配置数据加载完成,再切到对话模型面板(避免竞态)
        try { await ConfigUI.loadSettings(); } catch (_) {}
        await ConfigUI.switchPanel("model");
      }
      // 让对话模型卡片滚动到视图内
      const card = document.querySelector('#config-panel-model .cfg-card');
      if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
    // 切到 config 页可能需先确保 ConfigUI 已就绪;等一帧再切面板
    setTimeout(go, 30);
  },

  async init() {
    const label = document.getElementById("chat-model-label");
    const badge = document.getElementById("chat-model-badge");
    if (!label) return;

    try {
      const data = await API.get("/api/models");
      this.models = data.models || [];
    } catch (e) {
      console.error("模型列表加载失败:", e);
      this.models = [];
    }
    // 当前模型:优先系统配置保存的默认模型 / localStorage,否则默认(current)
    let chosen = "";
    try {
      const s = await API.get("/api/settings");
      chosen = s.model_default || "";
    } catch (_) { /* ignore */ }
    const saved = localStorage.getItem("chat-model");
    const def = this.models.find((m) => m.current);
    this.currentModel = chosen || saved ||
      (def ? def.model : (this.models[0] ? this.models[0].model : ""));
    if (this.currentModel) {
      label.textContent = this.currentModel;
      // 若默认模型与本地记忆不同,以系统配置为准(持久化同步)
      if (chosen && chosen !== saved) localStorage.setItem("chat-model", chosen);
    }
    if (badge) {
      badge.addEventListener("click", () => this.gotoConfig());
    }
  },
};

/* ---------- 事件绑定 ---------- */
document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("btn-send");
  ModelPicker.init();

  const doSend = () => {
    const q = input.value.trim();
    if (!q && !ChatUI.pendingImages.length && !ChatUI.pendingDocs.length) return;
    ChatUI.send(q, [...ChatUI.pendingImages], [...ChatUI.pendingDocs]);
  };

  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  // 图片:选择
  document.getElementById("btn-img-pick").addEventListener("click", () =>
    document.getElementById("img-file-input").click());
  document.getElementById("img-file-input").addEventListener("change", (e) => {
    ChatUI.addImages(e.target.files);
    e.target.value = "";
  });

  // 图片:粘贴
  document.addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
    if (files.length) ChatUI.addImages(files);
  });

  // 文档:选择
  document.getElementById("btn-file-pick").addEventListener("click", () =>
    document.getElementById("doc-file-input-chat").click());
  document.getElementById("doc-file-input-chat").addEventListener("change", (e) => {
    ChatUI.addDocs(e.target.files);
    e.target.value = "";
  });

  // 工作流面板:常驻聊天栏右侧,点击头部按钮整体折叠/展开(折叠后只剩竖条)
  document.getElementById("btn-wf-toggle").addEventListener("click", () => {
    document.getElementById("workflow-panel").classList.toggle("collapsed");
  });

  // 溯源弹窗关闭
  document.getElementById("btn-sources-close").addEventListener("click", () => SourcesModal.close());
  document.getElementById("sources-modal").addEventListener("click", (e) => {
    if (e.target.id === "sources-modal") SourcesModal.close();
  });

  // 单答工作流弹窗关闭
  document.getElementById("btn-wf-modal-close").addEventListener("click", () => WorkflowModal.close());
  document.getElementById("wf-modal").addEventListener("click", (e) => {
    if (e.target.id === "wf-modal") WorkflowModal.close();
  });
});
