import { computed, reactive, ref } from "vue";
import { API, sseRequest } from "../api";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { currentId, refreshAfterChat, registerChatHooks, create as createSession } from "./useSessions";
import { current as currentModel } from "./useModelPicker";

/* ============================================
   useChat — 聊天状态 + SSE 流式渲染
   搬运自原生版 chat.js 的 ChatUI。

   两处必须原样保留的机制(都是为性能/UX 打磨出来的):
   1. 流式渲染双路径:短内容走实时 markdown(整块替换),超长(>30k 字符)退化为
      纯文本追加,避免持续重解析卡顿。
   2. 自适应节流:baseMs 从 60ms 起步,按单次 markdown 渲染耗时在 60~320ms 之间浮动
      (渲染慢就降频防卡,渲染快就贴近 60ms 保持跟手)。首帧 24ms。
   ============================================ */

/** 超过这个长度就退化为纯文本追加(原生版 MAX_LIVE_MD) */
const MAX_LIVE_MD = 30000;

/** 消息列表 [{role, content, sources, workflow, images, docs, usage, _req?, renderedHtml?}] */
export const messages = ref([]);
export const sending = ref(false);
export const pendingImages = ref([]); // [{name, path}]
export const pendingDocs = ref([]);   // [{name, path}]

/** 滚动跟随意图:用户主动上滑才断开 */
export const stick = ref(true);
/** 「回到最新」按钮可见性 */
export const showJumpBtn = ref(false);

/** 输入框 ref(由组件注入,便于聚焦) */
let inputEl = null;
export function registerInputEl(el) {
  inputEl = el;
}
/** 消息容器 ref(滚动控制用) */
let messagesEl = null;
export function registerMessagesEl(el) {
  messagesEl = el;
}

/** markdown → 安全 HTML(与原生版 mdToHtml 一致) */
export function mdToHtml(md) {
  if (!md) return "";
  try {
    const raw = marked ? marked.parse(md) : md;
    return DOMPurify ? DOMPurify.sanitize(raw) : String(md);
  } catch (_) {
    return String(md);
  }
}

/* ---------------- 滚动 ---------------- */

/**
 * 仅在"贴着底部看"时自动跟随。用户上滑翻阅历史时不再把他拽回底部
 * (流式输出每 ~60ms 就会调到这里,无条件拉底会让人根本没法往回看)。
 */
export function scrollBottom() {
  if (!stick.value || !messagesEl) return;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

/**
 * 绑定滚动监听:记录用户是否处于"贴底"状态。
 * 不能只看 scroll 事件 —— 流式 markdown 每次重绘都会整块替换 innerHTML,
 * scrollHeight 突变也会触发 scroll,会被误判成"用户滑上去了" → 跟随静默失效。
 * 故只用真实的用户操作(滚轮/触摸/拖滚动条)断开跟随;回到贴底则永远恢复。
 */
export function initScrollFollow() {
  const box = messagesEl;
  if (!box) return;
  const GAP = 40; // 容差(px),避免像素级抖动误判
  let userScrolling = false;
  let idleTimer = null;
  const gap = () => box.scrollHeight - box.scrollTop - box.clientHeight;

  const markUser = () => {
    userScrolling = true;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { userScrolling = false; }, 250);
  };

  const sync = () => {
    const g = gap();
    if (g <= GAP) stick.value = true;        // 贴底 → 总是恢复跟随
    else if (userScrolling) stick.value = false; // 仅用户主动上滑才断开
    // 自愈:几何上已贴底就把跟随复位,避免边界时序留下 stick=false 残状态
    showJumpBtn.value = !stick.value;
  };

  box.addEventListener("scroll", sync, { passive: true });
  box.addEventListener("wheel", markUser, { passive: true });
  box.addEventListener("touchmove", markUser, { passive: true });
  box.addEventListener("mousedown", markUser); // 拖滚动条

  // 暴露给组件:收起/清空时复位
  scrollFollowCleanup = () => {
    box.removeEventListener("scroll", sync);
    box.removeEventListener("wheel", markUser);
    box.removeEventListener("touchmove", markUser);
    box.removeEventListener("mousedown", markUser);
    clearTimeout(idleTimer);
  };
}
let scrollFollowCleanup = null;
export function destroyScrollFollow() {
  if (scrollFollowCleanup) scrollFollowCleanup();
}

/** 点按钮:跳回底部并恢复跟随 */
export function jumpToBottom() {
  stick.value = true;
  showJumpBtn.value = false;
  if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
}

/* ---------------- 渲染 ---------------- */

/** 输入框聚焦并给出提示 */
export function focusInput(hint) {
  if (!inputEl) return;
  inputEl.focus();
  if (hint) inputEl.placeholder = hint;
}

/** 切换会话时的加载态:清空并显示占位 */
export function showSessionLoading() {
  messages.value = [];
  stick.value = true;
}

/** 用服务端历史重建消息列表(含把前一条 user 配对成 assistant 的 _req) */
export function renderHistory(msgs) {
  const out = [];
  let lastUserReq = null;
  let lastCtx = 0;
  for (const m of msgs || []) {
    if (m.role === "user") {
      // 历史消息里的附件可能是纯路径字符串或 {file_name, saved_path} 对象,统一归一
      const imgs = (m.images || []).map((im) =>
        typeof im === "string"
          ? { name: im.split(/[\\/]/).pop(), path: im }
          : { name: im.file_name || "", path: im.saved_path || im.path || "" },
      );
      const docs = (m.docs || []).map((d) =>
        typeof d === "string" ? { name: d, path: d } : { name: d.name || "", path: d.path || "" },
      );
      lastUserReq = {
        query: m.content || "",
        images: imgs.filter((x) => x.path),
        docs: docs.filter((x) => x.path),
      };
    }
    const item = {
      role: m.role,
      content: m.content || "",
      sources: m.sources || [],
      workflow: m.workflow || [],
      images: m.images || [],
      docs: m.docs || [],
      usage: m.usage || null,
      renderedHtml: m.role === "assistant" ? mdToHtml(m.content || "") : "",
    };
    // 重新生成历史回答时精确复用该轮提问
    if (m.role === "assistant" && lastUserReq) item._req = lastUserReq;
    if (m.role === "assistant" && m.usage && m.usage.context) lastCtx = m.usage.context;
    out.push(item);
  }
  messages.value = out;
  seedContext(lastCtx); // 进度条以最近一条 assistant 的 context 为准
  stick.value = true;   // 打开会话默认看最新内容 → 重置为贴底跟随
  showJumpBtn.value = false;
  scrollBottom();
}

/** 重新生成:用该条回答自己的 _req 提问(而非全局 lastUserQuery) */
export async function regenerateFor(asstMsg) {
  if (sending.value) return;
  const req = asstMsg._req || {};
  await send(req.query || "", req.images || [], req.docs || []);
}

export function copyText(text) {
  navigator.clipboard?.writeText(text);
}

/* ---------------- 附件 ---------------- */

export async function addImages(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    try {
      const data = await API.upload("/api/upload", f);
      pendingImages.value.push({ name: f.name, path: data.path });
    } catch (e) {
      alert(`图片上传失败:${e.message}`);
    }
  }
}

export async function addDocs(files) {
  for (const f of files) {
    try {
      const data = await API.upload("/api/upload", f);
      pendingDocs.value.push({ name: f.name, path: data.path });
    } catch (e) {
      alert(`文档上传失败:${e.message}`);
    }
  }
}

export function clearPendingImages() {
  pendingImages.value = [];
}
export function clearPendingDocs() {
  pendingDocs.value = [];
}

/** 图片预览用的 URL:后端按 basename 从 /uploads 提供 */
export function imageUrl(path) {
  return "/uploads/" + String(path).split(/[\\/]/).pop();
}

/* ---------------- 发送 ---------------- */

/** 当前请求句柄(可 abort;原生版没接取消 UI,这里同样保留) */
let currentHandle = null;

export async function send(query, images = [], docs = []) {
  if (sending.value) return;
  const text = (query || "").trim();
  if (!text && !images.length && !docs.length) return;
  if (!currentId.value) {
    await createSession();
    if (!currentId.value) return;
  }

  sending.value = true;
  // 用户主动发消息 → 重新贴底跟随(他显然想看这一轮的回答)
  stick.value = true;

  messages.value.push({
    role: "user",
    content: text,
    sources: [],
    images: images.map((i) => i.path),
    docs: docs.map((d) => ({ name: d.name })),
  });

  const asst = reactive({
    role: "assistant",
    content: "",
    sources: [],
    images: [],
    workflow: [],
    renderedHtml: "",
    /** 生成中徽标文案(如"模型思考中"/"解答中"),空则隐藏 */
    liveText: "",
    /** 徽标计时(秒) */
    liveSecs: 0,
    liveVisible: false,
  });
  // 记住本轮请求(重新生成时精确复用,而非依赖全局 lastUserQuery)
  asst._req = {
    query: text,
    images: images.map((i) => ({ name: i.name, path: i.path })),
    docs: docs.map((d) => ({ name: d.name, path: d.path })),
  };
  // 先 push 代理对象再改,后续回调都走代理 → 触发重渲染。
  // (直接改原始对象会绕过 Vue 响应式,DOM 不更新 —— 曾导致流式内容整块才出现)
  messages.value.push(asst);
  scrollBottom();

  // 发送即清空文本框与已选附件(不影响本轮已提交的 images/docs)
  if (inputEl) {
    inputEl.value = "";
    inputEl.style.height = "auto";
  }
  clearPendingImages();
  clearPendingDocs();
  startWorkflow();

  // ---- 生成中动态徽标(内联于回答卡片内,气泡之后:当前动作 + 总计时) ----
  // 思维链模型会先静默推理数秒,期间没有任何 token;靠这个徽标告诉用户"在做什么",
  // 否则界面看起来像卡死了。100ms 刷新计时,spinner 与计时分离避免动画被打断。
  let liveTimer = null;
  const liveStart = performance.now();
  const renderLive = () => {
    asst.liveSecs = ((performance.now() - liveStart) / 1000).toFixed(1);
  };
  const startLive = () => {
    asst.liveVisible = true;
    asst.liveText = "准备中…";
    renderLive();
    liveTimer = setInterval(renderLive, 100);
  };
  const stopLive = () => {
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    asst.liveVisible = false;
  };
  /** trace 事件到达 → 更新"当前在做什么"文案 */
  const onLiveEvent = (tr) => {
    const act = tr.action;
    const step = tr.step || "";
    if (act === "phase_start") {
      if (step === "模型") asst.liveText = "模型思考中";
      else if (tr.name && tr.name !== step) asst.liveText = tr.name;
      else asst.liveText = step;
    } else if (act === "substep") {
      asst.liveText = tr.name || asst.liveText;
    } else if (act === "phase_end") {
      asst.liveText = "处理中…";
    }
  };
  startLive();

  const modelName = currentModel.value;

  let tickTimer = null;
  let liveMd = false;
  let baseMs = 60;
  let lastShownLen = 0;

  const renderMdLive = () => {
    const follow = stick.value;
    const t0 = performance.now();
    asst.renderedHtml = mdToHtml(asst.content);
    const cost = performance.now() - t0;
    // 自适应:单次渲染越久,间隔越拉大;渲染很轻则贴近 60ms 保持跟手
    baseMs = cost > 25 ? Math.min(320, baseMs + 40) : Math.max(60, baseMs - 10);
    lastShownLen = asst.content.length;
    if (follow) scrollBottom();
  };

  /** 超长内容:只追加新增的纯文本,不重解析 markdown */
  const appendPlain = () => {
    const chunk = asst.content.slice(lastShownLen);
    if (!chunk) return;
    const follow = stick.value;
    asst.renderedHtml += escapeHtml(chunk);
    lastShownLen = asst.content.length;
    if (follow) scrollBottom();
  };

  const tick = () => {
    tickTimer = null;
    if (!sending.value) return;
    const wantLive = asst.content.length <= MAX_LIVE_MD;
    if (liveMd !== wantLive) {
      liveMd = wantLive;
      lastShownLen = 0;
      asst.renderedHtml = "";
    }
    if (liveMd) {
      if (asst.content.length !== lastShownLen) renderMdLive();
    } else {
      appendPlain();
    }
    if (sending.value) tickTimer = setTimeout(tick, baseMs);
  };

  /** token 到达:先累积文本,再由 tick 定时排版 */
  const kick = () => {
    if (tickTimer === null) tickTimer = setTimeout(tick, 24);
  };

  currentHandle = sseRequest({
    url: "/api/chat",
    body: {
      query: text,
      session_id: currentId.value,
      image_paths: images.map((i) => i.path),
      doc_paths: docs.map((d) => d.path),
      model: modelName || "",
    },
    onToken: (t) => {
      asst.content += t;
      asst.liveText = "解答中"; // 模型开始吐字 → 切"解答中"
      kick();
    },
    onTrace: (tr) => {
      asst.workflow.push(tr);
      addWorkflowStep();
      onLiveEvent(tr);
    },
    onCtx: (c) => {
      updateContext(c);
      // 累计本轮的计费总量/推理量(服务端权威值),供"执行完成"汇总行使用
      asst.usage = Object.assign({}, asst.usage, {
        context: c.used || (asst.usage && asst.usage.context),
        total: c.total != null ? c.total : (asst.usage && asst.usage.total),
        reason: c.reason != null ? c.reason : (asst.usage && asst.usage.reason),
      });
    },
    onSources: (srcs) => {
      // 写回该条回答自己身上 —— 各答数据独立,不用全局 buffer
      asst.sources = srcs || [];
    },
    onDone: () => {},
    onError: (msg) => {
      if (!asst.content) asst.content = `【服务异常】${msg}`;
      asst.renderedHtml = mdToHtml(asst.content);
    },
  });

  await currentHandle.promise;
  if (tickTimer !== null) {
    clearTimeout(tickTimer);
    tickTimer = null;
  }
  stopLive(); // 生成结束,移除动态徽标
  // 收尾:确保最终内容以完整 markdown 呈现
  asst.renderedHtml = mdToHtml(asst.content);
  sending.value = false;
  finishWorkflow();
  scrollBottom();
  await refreshAfterChat(); // 会话标题已生成,刷新侧边栏
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 工作流/上下文(供聊天页与面板联动) ---------------- */

/** 当前正在进行的回答(工作流面板读取它的事件流) */
export const activeWorkflow = ref([]);
export const activeUsage = ref(null);

function startWorkflow() {
  activeWorkflow.value = [];
  activeUsage.value = null;
}
function addWorkflowStep() {
  const last = messages.value[messages.value.length - 1];
  activeWorkflow.value = last?.workflow ? [...last.workflow] : [];
}
function finishWorkflow() {
  const last = messages.value[messages.value.length - 1];
  activeWorkflow.value = last?.workflow ? [...last.workflow] : [];
  activeUsage.value = last?.usage || null;
}

/* ---------------- 上下文占用进度条 ---------------- */

export const ctxLimit = ref(200000);
export const ctxUsed = ref(0);
export const ctxVisible = ref(false);
export const ctxReason = ref(0);
export const ctxTotal = ref(0);

export function setCtxLimit(limit) {
  if (limit && Number(limit) > 0) ctxLimit.value = Number(limit);
}

/**
 * 历史会话切换:用落库的 usage.context 恢复进度条;无记录则隐藏。
 * (原生版 ContextMeter.seed)
 */
export function seedContext(contextTokens) {
  const v = Number(contextTokens || 0);
  if (!v) {
    ctxVisible.value = false;
    return;
  }
  ctxUsed.value = v;
  ctxVisible.value = true;
}

export function updateContext(c) {
  // used===0 视为 no-op(与原生版一致)
  if (c.used === 0) return;
  if (c.limit) ctxLimit.value = Number(c.limit);
  if (c.used != null) ctxUsed.value = Number(c.used);
  if (c.total != null) ctxTotal.value = Number(c.total);
  if (c.reason != null) ctxReason.value = Number(c.reason);
  ctxVisible.value = true;
}

/** 进度百分比 */
export const ctxPercent = computed(() => {
  const lim = ctxLimit.value || 1;
  return Math.min(100, Math.max(0, (ctxUsed.value / lim) * 100));
});

/** "12.3k / 200k" 文案 */
export function fmtCtxTokens(n) {
  const v = Number(n) || 0;
  return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(Math.round(v));
}

// 把聊天侧能力注入会话侧(避免 useSessions ↔ useChat 循环 import)
registerChatHooks({ showLoading: showSessionLoading, renderHistory, focusInput });
