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
};

/** 模糊度默认值(原生版 App.DEFAULT_BLUR) */
export const DEFAULT_BLUR = 20;

/** 深色模式:<body> 上的 .dark 类(非 media query) */
export const isDark = ref(localStorage.getItem(LS.theme) === "dark");

/** 左侧栏折叠:#body 上的 .sidebar-collapsed 类 */
export const sidebarCollapsed = ref(localStorage.getItem(LS.collapsed) === "1");

/** 自定义壁纸(data URL);null = 用内置摄影底 */
export const wallpaper = ref(localStorage.getItem(LS.wallpaper) || null);

/** 毛玻璃模糊度 0~40 */
export const blur = ref(DEFAULT_BLUR);

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

// ---- 模糊度:写到 <html> 的 CSS 变量上 ----
function applyBlur(v) {
  document.documentElement.style.setProperty("--glass-blur", String(v));
  // 背景层模糊单独跟随(原生版:bg-blur-px = round(v * 1.7))
  document.documentElement.style.setProperty("--bg-blur-px", String(Math.round(v * 1.7)));
}

watch(
  blur,
  (v) => {
    applyBlur(v);
    localStorage.setItem(LS.blur, String(v));
  },
  { immediate: true },
);

/** 设置模糊度(自动 clamp 到 0~40,与原生版一致) */
export function setBlur(val) {
  blur.value = Math.max(0, Math.min(40, Number(val) || 0));
}

/** 首屏初始化:读 localStorage,无值时用默认值(且不覆盖持久化) */
export function initBlur() {
  const saved = localStorage.getItem(LS.blur);
  const v = saved !== null ? Number(saved) : DEFAULT_BLUR;
  blur.value = Math.max(0, Math.min(40, Number(v) || 0));
  applyBlur(blur.value);
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
