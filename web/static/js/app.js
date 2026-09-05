/* ============================================
   app.js — 全局:顶栏标签导航 / 深色模式 / 壁纸更换
   ============================================ */

const App = {
  currentView: "chat",

  switchView(view) {
    this.currentView = view;
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("active", v.id === `view-${view}`));
    // 各页进入时刷新数据
    if (view === "docs") DocsUI.refresh();
    if (view === "chat") ChatUI.scrollBottom();
  },

  /* ---------- 深色模式 ---------- */
  toggleTheme() {
    const dark = document.body.classList.toggle("dark");
    localStorage.setItem("theme", dark ? "dark" : "light");
    document.getElementById("btn-theme").innerHTML = `<svg class="ic"><use href="#i-${dark ? "sun" : "moon"}"/></svg>`;
  },

  initTheme() {
    if (localStorage.getItem("theme") === "dark") {
      document.body.classList.add("dark");
      document.getElementById("btn-theme").innerHTML = `<svg class="ic"><use href="#i-sun"/></svg>`;
    }
  },

  /* ---------- 左侧会话栏折叠/展开 ---------- */
  toggleSidebar() {
    const body = document.getElementById("body");
    const collapsed = body.classList.toggle("sidebar-collapsed");
    localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
  },

  initSidebar() {
    const body = document.getElementById("body");
    if (localStorage.getItem("sidebar-collapsed") === "1") {
      body.classList.add("sidebar-collapsed");
    }
    document.getElementById("btn-sidebar-toggle").addEventListener("click", () => App.toggleSidebar());
  },

  /* ---------- 壁纸(默认摄影底;自定义壁纸作用于背景层 #bg-blur 并保持清晰) ---------- */
  applyWallpaper() {
    const bg = document.getElementById("bg-blur");
    if (!bg) return;
    const custom = localStorage.getItem("wallpaper");
    if (custom) {
      bg.style.backgroundImage = `url(${custom})`;
      bg.style.backgroundSize = "cover";
      bg.style.backgroundPosition = "center";
      bg.classList.add("custom"); // 自定义壁纸:轻微模糊,保持清晰
    } else {
      bg.style.backgroundImage = "";
      bg.classList.remove("custom"); // 回退默认摄影底(强模糊)
    }
  },

  setWallpaper(dataUrl) {
    localStorage.setItem("wallpaper", dataUrl);
    this.applyWallpaper();
  },

  resetWallpaper() {
    localStorage.removeItem("wallpaper");
    this.applyWallpaper();
  },

  /* ---------- 设置:齿轮弹出菜单 → 外观设置二级弹窗 ---------- */
  toggleSettingsMenu(e) {
    if (e) e.stopPropagation();
    const menu = document.getElementById("settings-menu");
    menu.classList.toggle("hidden");
  },
  openAppearance(e) {
    if (e) e.stopPropagation();
    document.getElementById("settings-menu").classList.add("hidden");
    document.getElementById("settings-modal").classList.remove("hidden");
  },
  closeSettings() {
    document.getElementById("settings-modal").classList.add("hidden");
  },

  /* ---------- 模糊度调节(滑块持久化) ---------- */
  DEFAULT_BLUR: 20,

  setBlur(val, persist = true) {
    const v = Math.max(0, Math.min(40, Number(val) || 0));
    document.documentElement.style.setProperty("--glass-blur", String(v));
    document.documentElement.style.setProperty("--bg-blur-px", String(Math.round(v * 1.7)));
    const disp = document.getElementById("glass-blur-val");
    if (disp) disp.textContent = String(v);
    const slider = document.getElementById("glass-blur-slider");
    if (slider) slider.value = String(v);
    if (persist) localStorage.setItem("glass-blur", String(v));
  },

  initBlur() {
    const saved = localStorage.getItem("glass-blur");
    this.setBlur(saved !== null ? Number(saved) : this.DEFAULT_BLUR, false);
  },
};

document.addEventListener("DOMContentLoaded", () => {
  // 顶栏标签导航
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => App.switchView(t.dataset.view)));

  // 深色模式
  App.initTheme();
  document.getElementById("btn-theme").addEventListener("click", () => App.toggleTheme());

  // 会话栏折叠
  App.initSidebar();

  // 设置弹窗:齿轮用内联 onclick 打开;这里只处理关闭与遮罩
  document.getElementById("btn-settings-close").addEventListener("click", () => App.closeSettings());
  const settingsModal = document.getElementById("settings-modal");
  settingsModal.addEventListener("click", (e) => {
    if (e.target === settingsModal) App.closeSettings();
  });
  // 模糊度滑块
  const blurSlider = document.getElementById("glass-blur-slider");
  blurSlider.addEventListener("input", () => App.setBlur(blurSlider.value));
  App.initBlur();

  // 壁纸:默认渐变;头像菜单点击展开
  App.applyWallpaper();
  const avatar = document.getElementById("avatar-wrap");
  const menu = avatar.querySelector(".avatar-menu");
  avatar.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  });
  // 点击空白处关闭各下拉菜单(齿轮设置菜单/头像菜单);菜单内点击已 stopPropagation 不会触发
  document.addEventListener("click", () => {
    menu.classList.add("hidden");
    const sm = document.getElementById("settings-menu");
    if (sm) sm.classList.add("hidden");
  });

  const wallpaperInput = document.createElement("input");
  wallpaperInput.type = "file";
  wallpaperInput.accept = "image/*";
  wallpaperInput.className = "wallpaper-input";
  document.body.appendChild(wallpaperInput);

  document.getElementById("btn-wallpaper").addEventListener("click", () => wallpaperInput.click());
  wallpaperInput.addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => App.setWallpaper(reader.result);
    reader.readAsDataURL(f);
    e.target.value = "";
  });

  // 默认进入聊天页
  App.switchView("chat");
});
