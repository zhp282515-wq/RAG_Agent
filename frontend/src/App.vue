<script setup>
import { computed, onMounted, ref } from "vue";
import IconSprite from "./components/IconSprite.vue";
import SidebarSessions from "./components/SidebarSessions.vue";
import SidebarConfig from "./components/SidebarConfig.vue";
import SidebarStores from "./components/SidebarStores.vue";
import ChatView from "./components/ChatView.vue";
import DocsView from "./components/DocsView.vue";
import DebugView from "./components/DebugView.vue";
import ReportView from "./components/ReportView.vue";
import ConfigView from "./components/ConfigView.vue";
import {
  isDark,
  sidebarCollapsed,
  blur,
  currentView,
  sidebarMode,
  toggleTheme,
  toggleSidebar,
  setBlur,
  wallpaperStyle,
  initBlur,
  settingsOpen,
  closeSettings,
  openAppearance,
  pickWallpaper,
  resetWallpaper,
  switchView,
} from "./composables/useTheme";
import { refresh as refreshSessions } from "./composables/useSessions";
import { loadAll } from "./composables/useDocs";
import { refreshConfig } from "./composables/useConfig";
import { init as initModelPicker } from "./composables/useModelPicker";

const TABS = [
  { view: "chat", label: "会话" },
  { view: "docs", label: "知识库" },
  { view: "debug", label: "检索调试" },
  { view: "report", label: "报告" },
  { view: "config", label: "系统配置" },
];

/** #bg-blur 的内联样式;有自定义壁纸时铺满,否则留空回退 CSS 内置摄影底 */
const bgStyle = computed(() => wallpaperStyle());
/** 有自定义壁纸时加 .custom(轻微模糊,保持清晰) */
const bgCustom = computed(() => !!bgStyle.value.backgroundImage);

const wallpaperInput = ref(null);
/** 右上头像下拉菜单开合 */
const avatarMenuOpen = ref(false);

/** 切换视图 + 各页进入时的数据刷新(与原生版 App.switchView 一致) */
function onSwitchView(view) {
  switchView(view);
  if (view === "docs") loadAll();
  if (view === "config") refreshConfig();
}

/** 头像菜单里的「外观设置」:关菜单 + 开弹窗(原生版也是这两步) */
function openAppearanceFromMenu() {
  avatarMenuOpen.value = false;
  openAppearance();
}

function onWallpaperPicked(e) {
  pickWallpaper(e.target.files[0]);
  e.target.value = ""; // 允许再次选同一文件
}

function onResetWallpaper() {
  if (!confirm("恢复默认背景壁纸?")) return;
  resetWallpaper();
}

onMounted(() => {
  initBlur();
  initModelPicker();     // 当前对话模型(徽标 + localStorage 同步)
  refreshSessions();     // 侧栏会话列表首屏加载(原生版 DOMContentLoaded 里也做)
  // 点击空白处关闭头像菜单(原生版挂在 document 上)
  document.addEventListener("click", () => { avatarMenuOpen.value = false; });
});
</script>

<template>
  <IconSprite />

  <!-- 背景模糊层(壁纸照片经强高斯模糊后置于最底层) -->
  <div id="bg-blur" :class="{ custom: bgCustom }" :style="bgStyle"></div>

  <div id="app">
    <!-- ============ 顶栏 ============ -->
    <header id="topbar" class="edge-lit">
      <div class="topbar-left">
        <button class="icon-btn" id="btn-sidebar-toggle" title="折叠/展开会话栏" @click="toggleSidebar">
          <svg class="ic"><use href="#i-sidebar" /></svg>
        </button>
        <span class="brand-logo"></span>
        <span class="brand">知识库助手</span>
        <nav id="tabs">
          <button
            v-for="t in TABS"
            :key="t.view"
            class="tab"
            :class="{ active: currentView === t.view }"
            @click="onSwitchView(t.view)"
          >{{ t.label }}</button>
        </nav>
      </div>
      <div class="topbar-right">
        <button class="icon-btn" id="btn-search" title="搜索(装饰)"><svg class="ic"><use href="#i-search" /></svg></button>
        <button class="icon-btn" id="btn-bell" title="通知(装饰)"><svg class="ic"><use href="#i-bell" /></svg></button>
        <button class="icon-btn" id="btn-theme" title="深色/浅色切换" @click="toggleTheme">
          <svg class="ic"><use :href="isDark ? '#i-sun' : '#i-moon'" /></svg>
        </button>
        <div class="avatar" id="avatar-wrap" title="用户菜单" @click.stop="avatarMenuOpen = !avatarMenuOpen">
          <span class="avatar-circle">张</span>
          <span class="avatar-name">test8</span>
          <svg class="ic ic-sm"><use href="#i-chevron-down" /></svg>
          <div class="avatar-menu" :class="{ hidden: !avatarMenuOpen }">
            <button id="btn-appearance" @click.stop="openAppearanceFromMenu">
              <svg class="ic ic-sm"><use href="#i-sun" /></svg> 外观设置
            </button>
          </div>
        </div>
      </div>
    </header>

    <!-- ============ 主体 ============ -->
    <div id="body" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
      <!-- 左侧边栏:会话管理 / 系统配置子栏 / 向量库列表,三选一 -->
      <aside id="sidebar" class="edge-lit">
        <SidebarSessions v-show="sidebarMode === 'sessions'" />
        <SidebarConfig v-show="sidebarMode === 'config'" />
        <SidebarStores v-show="sidebarMode === 'stores'" />
      </aside>

      <!-- 右侧内容区 -->
      <main id="content" class="edge-lit">
        <ChatView />
        <DocsView />
        <DebugView />
        <ReportView />
        <ConfigView />
      </main>
    </div>
  </div>

  <!-- 外观设置弹窗 -->
  <div id="settings-modal" class="modal" :class="{ hidden: !settingsOpen }" @click.self="closeSettings">
    <div class="modal-box" style="width: 440px;">
      <div class="modal-head">
        <span><svg class="ic ic-sm"><use href="#i-gear" /></svg> 外观设置</span>
        <button id="btn-settings-close" @click="closeSettings"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div class="modal-body">
        <div class="setting-row">
          <span class="setting-label">界面模糊度</span>
          <span class="setting-value" id="glass-blur-val">{{ blur }}</span>
        </div>
        <input type="range" id="glass-blur-slider" min="0" max="40" step="1" :value="blur"
               class="slider-full" @input="setBlur($event.target.value)">
        <p class="setting-hint">控制玻璃面板与背景的模糊强度,0 为完全清晰。</p>
        <div class="appearance-divider"></div>
        <div class="setting-row"><span class="setting-label">背景壁纸</span></div>
        <div class="wallpaper-actions">
          <button class="btn-ghost" id="btn-wallpaper" @click="wallpaperInput?.click()">
            <svg class="ic ic-sm"><use href="#i-image" /></svg> 更换壁纸
          </button>
          <button class="btn-ghost" id="btn-wallpaper-reset" @click="onResetWallpaper">恢复默认</button>
        </div>
        <p class="setting-hint">自定义图片作为背景;恢复默认回到内置背景。</p>
      </div>
    </div>
  </div>

  <!-- 壁纸文件选择(隐藏,由「更换壁纸」触发) -->
  <input ref="wallpaperInput" type="file" accept="image/*" class="wallpaper-input" @change="onWallpaperPicked">

</template>
