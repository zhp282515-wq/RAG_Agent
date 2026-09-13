import { computed, ref } from "vue";
import { API } from "../api";

/* ============================================
   useDocs — 知识库页(向量库管理 + 文档管理)
   左栏(向量库)与右侧(文档)共享同一份状态,所以放一个 composable。
   ============================================ */

export const stores = ref([]);          // [{name, chunks, is_default}]
export const currentStore = ref("");
export const allDocs = ref([]);         // 全量文档(过滤前)
export const formatFilter = ref("all"); // 当前格式过滤("all"=全部)
export const searchKeyword = ref("");   // 文件名搜索
export const uploading = ref(false);
/** 上传状态:{ icon: "check"|"link"|"x"|null, text } */
export const uploadStatus = ref({ icon: null, text: "" });

/** 预览弹窗状态 */
export const previewOpen = ref(false);
export const previewTitle = ref("");
export const previewChunks = ref(null); // null=加载中; [...] = 分片数组
export const previewError = ref("");

const FMT_COLORS = {
  PDF: "#E5484D", DOCX: "#3E7BFA", DOC: "#3E7BFA", XLSX: "#30A46C", XLS: "#30A46C",
  CSV: "#F5A524", MD: "#8E4EC6", MARKDOWN: "#8E4EC6", TXT: "#7C8AA3",
};

export function fmtColor(type) {
  return FMT_COLORS[String(type || "?").toUpperCase()] || "#7C8AA3";
}

/** 字节 → B/KB/MB/GB/TB 自动换算(保留 1~2 位,无冗余 0) */
export function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = n;
  let i = -1;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
  return v.toFixed(digits) + " " + units[i];
}

/** 去掉扩展名后的主名(PDF→空;.tar.gz 这类保留前段) */
export function baseName(name) {
  const s = String(name || "");
  const i = s.lastIndexOf(".");
  return i > 0 ? s.slice(0, i) : s;
}

/** 当前库的 query 后缀 */
function storeQuery() {
  return currentStore.value ? `?store=${encodeURIComponent(currentStore.value)}` : "";
}

// ---------------- 向量库 ----------------

export async function refreshStores() {
  try {
    const d = await API.get("/api/stores");
    stores.value = d.stores || [];
    currentStore.value = d.current || "";
  } catch (e) {
    console.error("向量库列表加载失败:", e);
  }
}

export async function setCurrent(name) {
  if (name === currentStore.value) return;
  try {
    await API.put("/api/stores/current", { name });
    currentStore.value = name;
    await refresh(); // 文档列表跟着切到该库
  } catch (e) {
    alert("切换向量库失败:" + e.message);
  }
}

export async function createStore() {
  const name = (prompt("新建向量库名称(只能含字母/数字/下划线):") || "").trim();
  if (!name) return;
  try {
    const d = await API.post("/api/stores", { name });
    await refreshStores();
    // 新建后直接切过去,省得再点一次
    if (d.created) await setCurrent(d.name);
  } catch (e) {
    alert("新建失败:" + e.message);
  }
}

export async function renameStore(oldName) {
  const input = prompt(`重命名向量库「${oldName}」\n(只能含字母/数字/下划线)`, oldName);
  if (input === null) return; // 取消
  const newName = input.trim();
  if (!newName || newName === oldName) return;
  try {
    await API.put(`/api/stores/${encodeURIComponent(oldName)}`, { name: newName });
    await refreshStores();
    await refresh(); // 当前库改名后,文档区标题要跟着变
  } catch (e) {
    alert("重命名失败:" + e.message);
  }
}

export async function removeStore(name) {
  const s = stores.value.find((x) => x.name === name);
  const n = s && s.chunks >= 0 ? s.chunks : "?";
  if (!confirm(`确定删除向量库「${name}」?\n\n将连同库内全部 ${n} 个分片一并删除,不可恢复。`)) return;
  try {
    await API.del(`/api/stores/${encodeURIComponent(name)}`);
    await refreshStores();
    await refresh();
  } catch (e) {
    alert("删除失败:" + e.message);
  }
}

/** 向量库条目的副标题:分片数 · 默认 */
export function storeMeta(s) {
  const chunks = s.chunks >= 0 ? `${s.chunks} 分片` : "—";
  return chunks + (s.is_default ? " · 默认" : "");
}

// ---------------- 文档 ----------------

/** 页面标题跟随当前库 */
export const docsTitle = computed(() =>
  currentStore.value ? `知识库 · ${currentStore.value}` : "知识库",
);

/** 全量文档里的格式集合(转大写,保持出现顺序) */
export const availableFormats = computed(() => {
  const seen = [];
  for (const d of allDocs.value) {
    const t = String(d.file_type || "?").toUpperCase();
    if (!seen.includes(t)) seen.push(t);
  }
  return seen;
});

/** 格式 chips(全部 + 各格式的计数) */
export const formatChips = computed(() => {
  const chips = [{ label: "全部", value: "all", count: null, color: "" }];
  for (const f of availableFormats.value) {
    chips.push({
      label: f,
      value: f,
      count: allDocs.value.filter((d) => String(d.file_type || "?").toUpperCase() === f).length,
      color: fmtColor(f),
    });
  }
  return chips;
});

/** 按格式 + 搜索关键字过滤 */
export const filteredDocs = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase();
  return allDocs.value.filter((d) => {
    const byFmt =
      formatFilter.value === "all" || String(d.file_type || "?").toUpperCase() === formatFilter.value;
    const byKw = !kw || (d.file_name || "").toLowerCase().includes(kw);
    return byFmt && byKw;
  });
});

/** 空表提示文案:区分「筛选后无结果」与「库里本来就没文档」 */
export const emptyDocsHint = computed(() => {
  const filtering =
    allDocs.value.length > 0 && (formatFilter.value !== "all" || searchKeyword.value.trim());
  return filtering ? "没有匹配的文档,试试调整筛选或关键字" : "知识库暂无文档,点击右上角上传";
});

export async function refresh() {
  try {
    const data = await API.get("/api/documents" + storeQuery());
    allDocs.value = data.documents || [];
    // 若当前选的格式已不存在,复位为全部
    if (formatFilter.value !== "all" && !availableFormats.value.includes(formatFilter.value)) {
      formatFilter.value = "all";
    }
  } catch (e) {
    console.error("文档列表加载失败:", e);
  }
}

/** 切换向量库/首次进入时:先取库列表(确定当前库),再拉该库文档 */
export async function loadAll() {
  await refreshStores();
  await refresh();
}

export async function upload(file) {
  if (uploading.value) return;
  const store = currentStore.value;
  if (!store) {
    uploadStatus.value = { icon: null, text: "请先在左侧栏新建或选择一个向量库" };
    return;
  }
  uploading.value = true;
  uploadStatus.value = { icon: null, text: `正在上传并入库到「${store}」…(含向量化,可能需要一些时间)` };
  try {
    const data = await API.upload("/api/documents?store=" + encodeURIComponent(store), file);
    if (data.ok) {
      uploadStatus.value = {
        icon: "check",
        text: `${data.message || "已入库"}(${data.chunk_count ?? 0} 个分片)`,
      };
    } else {
      uploadStatus.value = { icon: "link", text: data.message || "上传失败" };
    }
    await refreshStores(); // 分片数变了,刷新左栏统计
    await refresh();
  } catch (e) {
    uploadStatus.value = { icon: "x", text: `上传失败:${e.message}` };
  } finally {
    uploading.value = false;
  }
}

export async function removeDoc(fileName) {
  if (!confirm(`确定从知识库删除「${fileName}」?`)) return;
  try {
    await API.del(`/api/documents/${encodeURIComponent(fileName)}${storeQuery()}`);
    await refresh();
  } catch (e) {
    alert("删除失败:" + e.message);
  }
}

/** 打开分片预览弹窗 */
export async function preview(fileName) {
  previewTitle.value = fileName;
  previewChunks.value = null; // 加载中
  previewError.value = "";
  previewOpen.value = true;
  try {
    const data = await API.get(`/api/documents/chunks/${encodeURIComponent(fileName)}${storeQuery()}`);
    previewChunks.value = data.chunks || [];
  } catch (e) {
    previewError.value = "预览失败:" + e.message;
  }
}

export function closePreview() {
  previewOpen.value = false;
}

/** 分片的位置说明:第 N 页 · 章节 · 小节 */
export function chunkLoc(c) {
  return [c.page ? `第 ${c.page} 页` : "", c.chapter || "", c.section || ""].filter(Boolean).join(" · ");
}
