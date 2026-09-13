import { ref } from "vue";
import { API } from "../api";

/* ============================================
   useModelPicker — 当前对话模型
   模型切换的唯一入口收敛到「系统配置 → 对话模型」(写 DB + localStorage);
   聊天输入框旁仅展示当前模型,点「切换」跳到系统配置页。
   ============================================ */

export const models = ref([]);
/** 当前选中的模型名(发送时随请求带上) */
export const current = ref("");
/** 配置页下拉菜单开合 */
export const menuOpen = ref(false);

/** 程序化切换当前模型(系统配置页选定「默认模型」时同步);持久化 */
export function setModel(model, persist = true) {
  const name = String(model || "").trim();
  if (!name) return;
  current.value = name;
  if (persist) localStorage.setItem("chat-model", name);
}

export async function init() {
  try {
    const data = await API.get("/api/models");
    models.value = data.models || [];
  } catch (e) {
    console.error("模型列表加载失败:", e);
    models.value = [];
  }
  // 当前模型:优先系统配置保存的默认模型,其次 localStorage,否则默认(current)
  let chosen = "";
  try {
    const s = await API.get("/api/settings");
    chosen = s.model_default || "";
  } catch (_) { /* ignore */ }
  const saved = localStorage.getItem("chat-model");
  const def = models.value.find((m) => m.current);
  current.value =
    chosen || saved || (def ? def.model : models.value[0] ? models.value[0].model : "");
  // 若默认模型与本地记忆不同,以系统配置为准(持久化同步)
  if (current.value && chosen && chosen !== saved) {
    localStorage.setItem("chat-model", chosen);
  }
}
