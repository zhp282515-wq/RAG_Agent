import { ref } from "vue";
import { API } from "../api";
import { setCtxLimit } from "./useChat";
import { setModel as setChatModel, models } from "./useModelPicker";

/* ============================================
   useConfig — 系统配置页(检索参数 / 对话模型 / 工具)
   设置存 MySQL(app_settings / tool_registry),服务端每次提问前现读 → 下次提问生效。
   ============================================ */

/** retrieval(检索参数) / model(对话模型) / tools(工具) */
export const currentPanel = ref("retrieval");

export const settings = ref(null);  // GET /api/settings 缓存
export const tools = ref([]);       // GET /api/tools 缓存
// 可选模型清单复用 useModelPicker 的 models(唯一来源,避免两处 ref 不同步)
export { models };

/* ----- 检索参数(可编辑的本地态,保存时才写库) ----- */
export const retrieval = ref({ top_k: 20, rerank_n: 5, score_high: 0.85, score_min: 0.60 });
const RETRIEVAL_DEFAULT = { top_k: 20, rerank_n: 5, score_high: 0.85, score_min: 0.60 };

/* ----- 对话模型 ----- */
export const temp = ref(0.7);
export const apiKeyInput = ref("");
export const modelMenuOpen = ref(false);
/** 用户本次在下拉里选中的模型(未选过则回退到当前默认) */
const pendingModel = ref("");

/* ----- 反馈文案 { [target]: {text, ok} } ----- */
export const feedbacks = ref({});
const feedbackTimers = {};

/* ----- 外部工具弹窗 ----- */
export const extModalOpen = ref(false);
export const extEditing = ref(null); // null=新增;非 null=编辑该 key
export const extForm = ref({
  key: "", label: "", transport: "stdio",
  command: "", args: "", url: "",
});
export const extFeedback = ref("");
export const extSaving = ref(false);

// ---------------- 侧栏子导航 ----------------

export async function switchPanel(name) {
  currentPanel.value = name;
  // 确保数据已加载(避免快速连点导致 settings 未就绪)
  if (name === "tools" && !tools.value.length) await loadTools();
  if ((name === "model" || name === "retrieval") && !settings.value) await loadSettings();
}

// ---------------- 读取 ----------------

export async function loadSettings() {
  try {
    const d = await API.get("/api/settings");
    settings.value = d;
    models.value = d.models || [];
    // 同步上下文进度条分母(与后端摘要触发阈值同源)
    if (d.context_limit) setCtxLimit(d.context_limit);
    // 用服务端值填充可编辑态
    if (d.retrieval) retrieval.value = { ...d.retrieval };
    if (d.temperature != null) temp.value = Number(d.temperature);
    // 当前默认模型:同步聊天徽标(服务端为准)
    if (d.model_default) {
      setChatModel(d.model_default);
      if (!pendingModel.value) pendingModel.value = d.model_default;
    }
    // key 不回显明文,输入框清空(placeholder 提示状态)
    apiKeyInput.value = "";
  } catch (e) {
    console.error("系统配置加载失败:", e);
    settings.value = null;
  }
}

export async function loadTools() {
  try {
    const d = await API.get("/api/tools");
    tools.value = d.tools || [];
  } catch (e) {
    console.error("工具列表加载失败:", e);
    tools.value = [];
  }
}

/** 进入配置页时刷新 */
export async function refreshConfig() {
  await Promise.all([loadSettings(), loadTools()]);
}

// ---------------- 检索参数 ----------------

export async function saveRetrieval() {
  const r = retrieval.value;
  try {
    await API.put("/api/settings", {
      retrieval: {
        top_k: Number(r.top_k),
        rerank_n: Number(r.rerank_n),
        score_high: Number(r.score_high),
        score_min: Number(r.score_min),
      },
    });
    feedback("retrieval", "已保存 · 下次提问/检索生效", true);
    await loadSettings();
  } catch (e) {
    feedback("retrieval", "保存失败:" + e.message, false);
  }
}

export function resetRetrieval() {
  retrieval.value = { ...RETRIEVAL_DEFAULT };
}

/** 阈值显示两位小数 */
export function fmtThreshold(v) {
  return Number(v).toFixed(2);
}

// ---------------- 对话模型 ----------------

/** 当前展示的默认模型(用户本次选过就用它,否则用服务端默认) */
export function shownModel() {
  return pendingModel.value || (settings.value && settings.value.model_default) || models.value[0] || "";
}

/** 下拉选即生效:写全局默认模型 + 同步聊天徽标 + 持久化 */
export async function pickModel(m) {
  pendingModel.value = m;
  modelMenuOpen.value = false;
  try {
    await API.put("/api/settings", { model: { default: m } });
    if (settings.value) settings.value.model_default = m;
  } catch (e) {
    console.error("保存默认模型失败:", e);
  }
  setChatModel(m); // 同步聊天输入框旁的徽标 + localStorage
}

export async function saveModel() {
  const modelDefault = shownModel();
  if (!modelDefault) return feedback("model", "请先在模型下拉选择默认模型", false);
  try {
    await API.put("/api/settings", {
      model: { default: modelDefault, temperature: Number(temp.value) },
    });
    feedback("model", "已保存 · 下次提问生效(新模型/温度)", true);
    await loadSettings();
  } catch (e) {
    feedback("model", "保存失败:" + e.message, false);
  }
}

export function resetModel() {
  pendingModel.value = models.value[0] || "qwen3.8-flash";
  temp.value = 0.7;
}

/** API Key 来源状态:DB 自设 / .env 兜底 / 都无 */
export function keyState() {
  const s = settings.value;
  if (!s) return { text: "", ok: false, isDb: false };
  if (s.api_key_is_db) {
    return { text: "✓ 已在系统配置填写(优先于此 .env)", ok: true, isDb: true };
  }
  if (s.api_key_configured) {
    return {
      text: "使用 .env 中的 key(点击下方「清除」无此 key 可清,如需换请在输入框填新 key)",
      ok: false,
      isDb: false,
    };
  }
  return { text: "未配置:请填入 API Key 才能使用", ok: false, isDb: false };
}

/** 编辑态不预填任何值(避免把已存 key 暴露到 DOM) */
export function keyPlaceholder() {
  const isDb = settings.value && settings.value.api_key_is_db;
  return isDb
    ? "已填 key(留空保存=不改;想换则输入新 key)"
    : "输入你的 DashScope API Key(sk-…)";
}

export async function saveApiKey() {
  const key = apiKeyInput.value.trim();
  const isDb = settings.value && settings.value.api_key_is_db;
  if (!key && isDb) {
    return feedback("model", "未改动:当前 DB 已有关键,留空保存=不更换", false);
  }
  if (!key) return alert("请输入要保存的 API Key");
  try {
    await API.put("/api/settings", { model: { api_key: key } });
    apiKeyInput.value = "";
    feedback("model", "API Key 已保存 · 下次提问起全部模型服务生效", true);
    await loadSettings();
  } catch (e) {
    feedback("model", "API Key 保存失败:" + e.message, false);
  }
}

export async function clearApiKey() {
  const isDb = settings.value && settings.value.api_key_is_db;
  if (!isDb) {
    return feedback(
      "model",
      "当前用的是 .env 中的 key(不在系统配置保存)。如需改用别的 key,直接在输入框填新 key 保存即可",
      false,
    );
  }
  if (!confirm("清除系统配置里保存的 API Key,改用 .env 中的 key?")) return;
  try {
    await API.put("/api/settings", { model: { api_key: "" } });
    feedback("model", "已清除系统配置里的 key · 现回退 .env", true);
    await loadSettings();
  } catch (e) {
    feedback("model", "清除失败:" + e.message, false);
  }
}

// ---------------- 工具 ----------------

export const builtinTools = () => tools.value.filter((t) => t.kind === "builtin");
export const externalTools = () => tools.value.filter((t) => t.kind === "external");

export async function toggleTool(key, enabled) {
  try {
    await API.put(`/api/tools/${encodeURIComponent(key)}`, { enabled });
    const t = tools.value.find((x) => x.key === key);
    if (t) t.enabled = enabled;
  } catch (e) {
    alert("开关工具失败:" + e.message);
    await loadTools();
  }
}

export async function removeExternal(key) {
  if (!confirm(`确定删除外部工具「${key}」?删除后需重新注册。`)) return;
  try {
    await API.del(`/api/tools/${encodeURIComponent(key)}`);
    await loadTools();
  } catch (e) {
    alert("删除失败:" + e.message);
  }
}

/** 外部工具的位置描述:http → url;stdio → command + args */
export function extLocation(t) {
  const cfg = t.config || {};
  return t.transport === "http"
    ? cfg.url || ""
    : `${cfg.command || ""} ${(cfg.args || []).join(" ")}`.trim();
}

// ---------------- 外部工具弹窗 ----------------

export function openAddExternal() {
  extEditing.value = null;
  extForm.value = { key: "", label: "", transport: "stdio", command: "", args: "", url: "" };
  extFeedback.value = "";
  extModalOpen.value = true;
}

export function editExternal(key) {
  const t = tools.value.find((x) => x.key === key);
  if (!t) return alert("工具不存在");
  extEditing.value = key;
  const cfg = t.config || {};
  extForm.value = {
    key,
    label: t.label || key,
    transport: t.transport || "stdio",
    command: cfg.command || "",
    args: (cfg.args || []).join(" "),
    url: cfg.url || "",
  };
  extFeedback.value = "";
  extModalOpen.value = true;
}

export function closeExtModal() {
  extModalOpen.value = false;
  extEditing.value = null;
}

/** 提交:新增走 POST(探测),编辑走 PUT(重新探测新配置) */
export async function submitExternal() {
  const isEdit = !!extEditing.value;
  const f = extForm.value;
  const key = (extEditing.value || f.key || "").trim();
  const label = (f.label || "").trim();
  const transport = f.transport || "stdio";
  let config;
  if (transport === "stdio") {
    config = {
      command: (f.command || "").trim(),
      args: (f.args || "").trim().split(/\s+/).filter(Boolean),
    };
  } else {
    config = { url: (f.url || "").trim() };
  }
  if (!key) return alert("请填写工具 key");

  extFeedback.value = "正在连接探测新配置…";
  extSaving.value = true;
  try {
    if (isEdit) {
      await API.put(`/api/tools/${encodeURIComponent(key)}`, { label, transport, config, enabled: true });
      extFeedback.value = "已保存并重新探测 · 下次提问用新配置";
    } else {
      const d = await API.post("/api/tools", { key, label, transport, config });
      extFeedback.value = `注册成功,暴露 ${(d.mcp_tools || []).length} 个工具:${
        (d.mcp_tools || []).map((t) => t.name).join(", ") || "无"}`;
    }
    await loadTools();
    closeExtModal();
  } catch (e) {
    extFeedback.value = (isEdit ? "保存失败:" : "注册失败:") + e.message;
  } finally {
    extSaving.value = false;
  }
}

// ---------------- 反馈 ----------------

/** 反馈文案,4 秒后自动清除(与原生版一致) */
function feedback(target, text, ok) {
  feedbacks.value = { ...feedbacks.value, [target]: { text, ok } };
  clearTimeout(feedbackTimers[target]);
  feedbackTimers[target] = setTimeout(() => {
    feedbacks.value = { ...feedbacks.value, [target]: { text: "", ok } };
  }, 4000);
}
