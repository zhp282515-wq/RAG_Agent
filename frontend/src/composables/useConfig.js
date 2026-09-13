import { ref } from "vue";
import { API } from "../api";

/* ============================================
   useConfig — 系统配置页(检索参数 / 对话模型 / 工具)
   侧栏子导航 + 三个面板共享 currentPanel,故状态放这里。
   ============================================ */

/** retrieval(检索参数) / model(对话模型) / tools(工具) */
export const currentPanel = ref("retrieval");

/** GET /api/settings 的缓存(每次写入后重新拉取) */
export const settings = ref(null);
/** GET /api/tools 的缓存 */
export const tools = ref([]);
/** 可选模型清单 */
export const models = ref([]);
/** 后端 agent 缓存版本号:工具启停/注册会 bump,用于触发重建 */
export const toolsRev = ref(0);

export function switchPanel(name) {
  currentPanel.value = name;
}

/** 进入配置页时刷新(检索参数 / 模型 / 工具) */
export async function refreshConfig() {
  await Promise.all([loadSettings(), loadTools()]);
}

export async function loadSettings() {
  try {
    settings.value = await API.get("/api/settings");
    models.value = settings.value.models || [];
  } catch (e) {
    console.error("配置加载失败:", e);
  }
}

export async function loadTools() {
  try {
    const d = await API.get("/api/tools");
    tools.value = d.tools || [];
    toolsRev.value = d.rev ?? 0;
  } catch (e) {
    console.error("工具列表加载失败:", e);
  }
}
