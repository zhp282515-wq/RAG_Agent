import { ref } from "vue";
import { API } from "../api";

/* ============================================
   useSessions — 侧边栏会话管理
   列表分组 / 新建(空会话去重) / 切换 / 搜索过滤 / 删除 / 重命名
   与原生版 sessions.js 的行为、localStorage 无关(状态全在服务端)。

   会话切换后需要驱动聊天区重绘,故聊天侧通过 registerChatHooks 注入回调,
   避免 sessions ↔ chat 两个 composable 循环 import。
   ============================================ */

export const sessions = ref([]);        // [{session_id,title,created_at,updated_at}]
export const currentId = ref(null);
export const searchKeyword = ref("");
/** 内联重命名中的会话 id(null = 无) */
export const renamingId = ref(null);

/** 聊天侧注入的钩子(避免循环依赖) */
let chatHooks = { showLoading: () => {}, renderHistory: () => {}, focusInput: () => {} };

export function registerChatHooks(hooks) {
  chatHooks = { ...chatHooks, ...hooks };
}

/** 是否存在无标题的空会话(纯前端规则:有就不重复建) */
function hasEmptySession() {
  return sessions.value.some((s) => !s.title || s.title.trim() === "");
}

export async function refresh() {
  try {
    const data = await API.get("/api/sessions");
    sessions.value = data.sessions || [];
  } catch (e) {
    console.error("会话列表加载失败:", e);
  }
}

/** 新建会话:已存在空会话时直接切到它,不创建第二个 */
export async function create() {
  if (hasEmptySession()) {
    const empty = sessions.value.find((s) => !s.title || s.title.trim() === "");
    if (empty) {
      await switchTo(empty.session_id);
      chatHooks.focusInput("新的会话已就绪,输入你的问题开始吧");
      return;
    }
  }
  try {
    const data = await API.post("/api/sessions", {});
    await refresh();
    await switchTo(data.session.session_id);
  } catch (e) {
    alert("新建会话失败:" + e.message);
  }
}

export async function switchTo(sessionId) {
  currentId.value = sessionId;
  chatHooks.showLoading();
  try {
    const data = await API.get(`/api/sessions/${sessionId}`);
    const s = data.session;
    chatHooks.renderHistory(s ? (s.messages || []) : []);
  } catch (e) {
    console.error("会话详情加载失败:", e);
    chatHooks.renderHistory([]);
  }
}

export async function remove(sessionId) {
  if (!confirm("确定删除该会话?删除后不可恢复。")) return;
  try {
    await API.del(`/api/sessions/${sessionId}`);
    if (currentId.value === sessionId) {
      currentId.value = null;
      chatHooks.renderHistory([]);
    }
    await refresh();
  } catch (e) {
    alert("删除会话失败:" + e.message);
  }
}

/** 会话被第一轮问答填充后,后端 title 已更新 → 刷新列表 */
export async function refreshAfterChat() {
  await refresh();
}

/** 开始内联重命名(输入框的提交/取消逻辑在组件里) */
export function startRename(sessionId) {
  renamingId.value = sessionId;
}

/** 提交重命名。val 为空或未变更时不发请求(与原生版 double-guard 一致) */
export async function commitRename(sessionId, currentTitle, val) {
  renamingId.value = null;
  const v = (val || "").trim();
  if (!v) return;                 // 空值:放弃保存
  if (v === currentTitle) return; // 值没变:不发请求
  try {
    await API.put(`/api/sessions/${sessionId}`, { title: v });
    await refresh();
  } catch (e) {
    alert("重命名失败:" + e.message);
  }
}

export function cancelRename() {
  renamingId.value = null;
}

/** 按更新时间分组:今天/昨天/最近7天/更早 */
export function groupSessions(list) {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const groups = { 今天: [], 昨天: [], 最近7天: [], 更早: [] };
  for (const s of list) {
    const d = new Date((s.updated_at || s.created_at || "").replace(" ", "T"));
    if (isNaN(d)) {
      groups["更早"].push(s);
      continue;
    }
    const dayDiff = Math.floor(
      (startOfToday - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000,
    );
    if (dayDiff <= 0) groups["今天"].push(s);
    else if (dayDiff === 1) groups["昨天"].push(s);
    else if (dayDiff < 7) groups["最近7天"].push(s);
    else groups["更早"].push(s);
  }
  return groups;
}

/** 关键字过滤(大小写不敏感)+ 分组,只保留非空分组 */
export function visibleGroups() {
  const kw = searchKeyword.value.trim().toLowerCase();
  const filtered = kw
    ? sessions.value.filter((s) => (s.title || "").toLowerCase().includes(kw))
    : sessions.value;
  const groups = groupSessions(filtered);
  return Object.entries(groups)
    .filter(([, items]) => items.length)
    .map(([name, items]) => ({ name, items }));
}

/** 标题为空时显示占位「新会话」 */
export function sessionTitle(s) {
  return s.title && s.title.trim() ? s.title : "新会话";
}

/** 摘要:有标题时用标题,否则「还没有内容」(与原生版一致) */
export function sessionSummary(s) {
  return s.title && s.title.trim() ? s.title : "还没有内容";
}

/** HH:MM */
export function timeLabel(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T"));
  if (isNaN(d)) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
