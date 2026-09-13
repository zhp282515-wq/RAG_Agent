import { ref, watch } from "vue";

/* ============================================
   useTheme — 全局外观状态:深色模式 / 侧栏折叠 / 壁纸 / 毛玻璃模糊度
   与原生版 app.js 的 localStorage 键与取值保持完全一致。

   模块级 ref = 应用单例,多个组件(如外观设置弹窗)读同一份状态。
   ============================================ */

const LS = {
  theme: "theme",
  collapsed: "sidebar-collapsed",
  wallpaper: "wallpaper",
  blur: "glass-blur",
  wallBlur: "wall-blur",
};

/** 玻璃面板模糊度默认值(原生版 App.DEFAULT_BLUR) */
export const DEFAULT_BLUR = 20;
/** 壁纸层模糊度默认值 */
export const DEFAULT_WALL_BLUR = 24;

/** 深色模式:<body> 上的 .dark 类(非 media query) */
export const isDark = ref(localStorage.getItem(LS.theme) === "dark");

/** 左侧栏折叠:#body 上的 .sidebar-collapsed 类 */
export const sidebarCollapsed = ref(localStorage.getItem(LS.collapsed) === "1");

/** 自定义壁纸(data URL);null = 用内置摄影底 */
export const wallpaper = ref(localStorage.getItem(LS.wallpaper) || null);

/** 模糊度通用的取值/夹取工具(定义在 ref 之前,避免 TDZ) */
const clampBlur = (v) => Math.max(0, Math.min(40, Number(v) || 0));
const readBlur = (key, fallback) => {
  const raw = localStorage.getItem(key);
  return raw === null ? fallback : clampBlur(raw);
};

/**
 * 模糊度 0~40。
 * **初始值必须直接读 localStorage**,与 theme / sidebar-collapsed / wallpaper 保持一致:
 * 若初始值给默认值、再靠 `watch(..., {immediate:true})` 回写,那个 immediate 会在
 * **模块加载瞬间**用默认值覆盖掉用户已保存的设置,之后读到的已是脏数据 ——
 * 表现为"每次刷新都回到默认"。
 */
/** 玻璃面板模糊度(外观设置 → 界面模糊度) */
export const blur = ref(readBlur(LS.blur, DEFAULT_BLUR));
/** 壁纸层模糊度(外观设置 → 背景模糊度)。与 blur 完全独立。 */
export const wallBlur = ref(readBlur(LS.wallBlur, DEFAULT_WALL_BLUR));

/** 当前视图 / 侧栏模式 */
export const currentView = ref("chat");
/** sessions(会话) / config(系统配置子栏) / stores(向量库列表) */
export const sidebarMode = ref("sessions");

/**
 * 切换视图,并联动侧栏内容。
 * 各页进入时的数据刷新由 App.vue 负责(避免这里反向依赖各页 composable 造成循环 import)。
 */
export function switchView(view) {
  currentView.value = view;
  sidebarMode.value = view === "config" ? "config" : view === "docs" ? "stores" : "sessions";
}

// ---- 深色模式:<body> 不在 Vue 应用根内,只能命令式写类 ----
watch(
  isDark,
  (dark) => {
    document.body.classList.toggle("dark", dark);
    localStorage.setItem(LS.theme, dark ? "dark" : "light");
  },
  { immediate: true },
);

export function toggleTheme() {
  isDark.value = !isDark.value;
}

// ---- 侧栏折叠 ----
watch(
  sidebarCollapsed,
  (collapsed) => {
    localStorage.setItem(LS.collapsed, collapsed ? "1" : "0");
  },
  { immediate: true },
);

export function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
}

// ---- 壁纸 ----
watch(
  wallpaper,
  (dataUrl) => {
    if (dataUrl) localStorage.setItem(LS.wallpaper, dataUrl);
    else localStorage.removeItem(LS.wallpaper);
  },
  { immediate: true },
);

export function setWallpaper(dataUrl) {
  wallpaper.value = dataUrl;
}

export function resetWallpaper() {
  wallpaper.value = null;
}

/** #bg-blur 的内联样式:有自定义壁纸时铺满;否则留空回退 CSS 内置摄影底 */
export function wallpaperStyle() {
  return wallpaper.value
    ? {
        backgroundImage: `url(${wallpaper.value})`,
        backgroundSize: "cover",
        backgroundPosition: "center",
      }
    : {};
}

// ---- 模糊度:两套互相独立,各写各的 CSS 变量 ----
/** 玻璃面板虚化(外观设置 → 界面模糊度) */
function applyGlassBlur(v) {
  document.documentElement.style.setProperty("--glass-blur", String(v));
}
/** 壁纸层虚化(外观设置 → 背景模糊度) */
function applyWallBlur(v) {
  document.documentElement.style.setProperty("--wall-blur", String(v));
}

watch(
  blur,
  (v) => {
    applyGlassBlur(v);
    localStorage.setItem(LS.blur, String(v));
  },
  { immediate: true },
);

watch(
  wallBlur,
  (v) => {
    applyWallBlur(v);
    localStorage.setItem(LS.wallBlur, String(v));
  },
  { immediate: true },
);

/** 设置玻璃模糊度(clamp 到 0~40) */
export function setBlur(val) {
  blur.value = clampBlur(val);
}

/** 设置壁纸模糊度(clamp 到 0~40) */
export function setWallBlur(val) {
  wallBlur.value = clampBlur(val);
}

// ---- 外观设置弹窗 ----
export const settingsOpen = ref(false);

export function openAppearance() {
  settingsOpen.value = true;
}
export function closeSettings() {
  settingsOpen.value = false;
}

/** 选文件 → 读成 data URL 存进 localStorage(与原生版 FileReader.readAsDataURL 一致) */
export function pickWallpaper(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => setWallpaper(reader.result);
  reader.readAsDataURL(file);
}
