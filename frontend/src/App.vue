<script setup>
import { computed, onMounted, ref } from "vue";
import IconSprite from "./components/IconSprite.vue";
import SidebarSessions from "./components/SidebarSessions.vue";
import SidebarConfig from "./components/SidebarConfig.vue";
import SidebarStores from "./components/SidebarStores.vue";
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
} from "./composables/useTheme";
import { refresh as refreshSessions } from "./composables/useSessions";
import { loadAll } from "./composables/useDocs";
import { refreshConfig } from "./composables/useConfig";

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

function switchView(view) {
  currentView.value = view;
  // 侧栏随页面联动:系统配置 → 配置子栏;知识库 → 向量库列表;其余 → 会话管理
  sidebarMode.value = view === "config" ? "config" : view === "docs" ? "stores" : "sessions";
  // 各页进入时刷新数据(与原生版 App.switchView 一致)
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
  refreshSessions(); // 侧栏会话列表首屏加载(原生版 DOMContentLoaded 里也做)
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
            @click="switchView(t.view)"
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
        <section class="view" :class="{ active: currentView === 'chat' }" id="view-chat">
          <div id="chat-area">
            <div id="chat-messages">
              <div id="chat-welcome" class="welcome">
                <h2>你好</h2>
                <p>我是你的知识库助手,基于已接入知识库,为你提供准确、可靠的信息检索与分析。</p>
              </div>
            </div>
            <div id="chat-input-wrap">
              <button id="btn-scroll-bottom" class="scroll-bottom-btn hidden" title="回到最新">
                <svg class="ic"><use href="#i-chevron-down" /></svg>
              </button>
              <div id="ctx-meter-wrap" class="hidden">
                <div id="ctx-meter" class="ctx-meter"><div id="ctx-meter-fill" class="ctx-fill"></div></div>
                <span id="ctx-meter-text" class="ctx-text">0 / 200k</span>
              </div>
              <div id="chat-docs-preview"></div>
              <div id="chat-images-preview"></div>
              <div id="chat-input-box" class="edge-lit">
                <div class="input-tools">
                  <button class="tool-btn" id="btn-img-pick" title="上传图片"><svg class="ic"><use href="#i-image" /></svg></button>
                  <button class="tool-btn" id="btn-file-pick" title="上传文档"><svg class="ic"><use href="#i-paperclip" /></svg></button>
                </div>
                <textarea id="chat-input" rows="1" placeholder="输入你的问题,Enter 发送,Shift+Enter 换行"></textarea>
                <div id="chat-model-badge" class="chat-model-badge" title="当前对话模型(系统配置→对话模型 中切换)">
                  <span id="chat-model-label">模型</span>
                  <span class="chat-model-goto">切换</span>
                </div>
                <button id="btn-send" class="btn-liquid" title="发送"><svg class="ic"><use href="#i-send" /></svg></button>
                <input type="file" id="img-file-input" accept="image/*" multiple hidden>
                <input type="file" id="doc-file-input-chat" accept=".pdf,.docx,.doc,.txt,.md,.markdown,.csv,.xlsx,.xls" multiple hidden>
              </div>
            </div>
          </div>

          <div id="workflow-panel" class="edge-lit">
            <div class="wf-header">
              <span><svg class="ic ic-sm"><use href="#i-gear" /></svg> 工作流</span>
              <button id="btn-wf-toggle" title="收起/展开"><svg class="ic ic-sm"><use href="#i-chevron-right" /></svg></button>
            </div>
            <div id="workflow-steps"><div class="wf-empty">暂无执行记录</div></div>
          </div>
        </section>

        <section class="view" :class="{ active: currentView === 'docs' }" id="view-docs">
          <div class="docs-main">
            <div class="page-head">
              <h2 id="docs-title">知识库</h2>
              <div class="upload-wrap">
                <button id="btn-upload" class="btn-liquid"><svg class="ic"><use href="#i-upload" /></svg> 上传文档</button>
                <input type="file" id="doc-file-input" hidden
                       accept=".pdf,.docx,.doc,.txt,.md,.markdown,.csv,.xlsx,.xls">
              </div>
            </div>
            <p id="upload-status" class="muted"></p>
            <div class="doc-toolbar">
              <div class="doc-filters" id="doc-filters"></div>
              <div class="doc-search">
                <svg class="ic ic-sm"><use href="#i-search" /></svg>
                <input type="text" id="doc-search-input" placeholder="搜索文件名…">
              </div>
            </div>
            <table id="docs-table">
              <thead>
                <tr><th>文件名</th><th>格式</th><th>大小</th><th>分片数</th><th>操作</th></tr>
              </thead>
              <tbody id="docs-tbody"></tbody>
            </table>
          </div>
        </section>

        <section class="view" :class="{ active: currentView === 'debug' }" id="view-debug">
          <div class="page-head"><h2>检索调试</h2></div>
          <div class="debug-form">
            <input type="text" id="debug-query" placeholder="输入检索 query">
            <input type="number" id="debug-topk" value="20" min="1" max="100" title="top_k">
            <button id="btn-debug-search" class="btn-liquid">检索</button>
          </div>
          <div id="debug-results"></div>
        </section>

        <section class="view" :class="{ active: currentView === 'report' }" id="view-report">
          <div class="page-head"><h2>报告记录</h2></div>
          <div id="report-list"></div>
        </section>

        <section class="view" :class="{ active: currentView === 'config' }" id="view-config">
          <div class="config-panel active" id="config-panel-retrieval">
            <div class="page-head">
              <h2>检索参数</h2>
              <span class="config-global-note">全局默认 · 每次提问 / 检索读取</span>
            </div>
            <div class="config-body">
              <div class="cfg-card">
                <div class="cfg-card-title">向量召回 &amp; Rerank 精排</div>
                <div class="setting-row">
                  <span class="setting-label">召回候选数 top_k</span>
                  <span class="setting-value" id="cfg-topk-val">20</span>
                </div>
                <input type="number" id="cfg-topk" class="slider-full cfg-num" min="1" max="100" value="20">
                <div class="setting-row">
                  <span class="setting-label">精排返回数 rerank_n</span>
                  <span class="setting-value" id="cfg-rerank-val">5</span>
                </div>
                <input type="number" id="cfg-rerank" class="slider-full cfg-num" min="1" max="100" value="5">
                <p class="setting-hint">top_k=向量召回候选数;rerank_n=精排后返回条数。检索调试页可临时覆盖 top_k,不改全局。</p>
              </div>
              <div class="cfg-card">
                <div class="cfg-card-title">相关度阈值</div>
                <div class="setting-row">
                  <span class="setting-label">高相关线</span>
                  <span class="setting-value" id="cfg-high-val">0.85</span>
                </div>
                <input type="range" id="cfg-high" min="0.50" max="1.0" step="0.01" value="0.85" class="slider-full">
                <div class="setting-row">
                  <span class="setting-label">达标线(注入上下文)</span>
                  <span class="setting-value" id="cfg-min-val">0.60</span>
                </div>
                <input type="range" id="cfg-min" min="0.1" max="0.9" step="0.01" value="0.60" class="slider-full">
                <p class="setting-hint">≥高相关线=「高」;≥达标线才注入上下文,&lt;达标线不返回。</p>
              </div>
              <div class="cfg-actions">
                <button class="btn-liquid" id="btn-cfg-save-retrieval">保存检索设置</button>
                <button class="btn-ghost" id="btn-cfg-reset-retrieval">恢复默认</button>
                <span class="cfg-feedback muted" id="cfg-feedback-retrieval"></span>
              </div>
            </div>
          </div>

          <div class="config-panel" id="config-panel-model">
            <div class="page-head">
              <h2>对话模型</h2>
              <span class="config-global-note">全局默认 · 下次提问生效</span>
            </div>
            <div class="config-body">
              <div class="cfg-card">
                <div class="cfg-card-title">默认模型</div>
                <div class="setting-row"><span class="setting-label">模型</span></div>
                <div id="config-model-picker" class="model-picker cfg-model-picker">
                  <button id="config-model-btn" class="model-picker-btn" type="button">
                    <span id="config-model-label">加载中…</span>
                    <svg class="ic ic-xs mp-arrow"><use href="#i-chevron-down" /></svg>
                  </button>
                  <div id="config-model-menu" class="model-picker-menu hidden"></div>
                </div>
                <p class="setting-hint">作为新会话与报告场景的默认模型;聊天输入框旁仍可临时切换。</p>
              </div>
              <div class="cfg-card">
                <div class="cfg-card-title">温度</div>
                <div class="setting-row">
                  <span class="setting-label">创造力 / 随机性</span>
                  <span class="setting-value" id="cfg-temp-val">0.7</span>
                </div>
                <input type="range" id="cfg-temp" min="0" max="2" step="0.1" value="0.7" class="slider-full">
                <p class="setting-hint">0=更确定保守;越高越多样发散。知识问答建议 0.3~0.7。</p>
              </div>
              <div class="cfg-card">
                <div class="cfg-card-title">模型服务 API Key</div>
                <div class="cfg-apikey-row">
                  <input type="password" id="cfg-api-key" class="slider-full cfg-txt" autocomplete="off"
                         placeholder="填你的 DashScope API Key(sk-…),留空则用 .env">
                  <span class="cfg-apikey-state muted" id="cfg-api-key-state"></span>
                </div>
                <div class="cfg-apikey-actions">
                  <button class="btn-ghost" id="btn-cfg-save-key">保存 API Key</button>
                  <button class="btn-ghost" id="btn-cfg-clear-key">清除(改用 .env)</button>
                </div>
                <p class="setting-hint">供 聊天 / 知识库向量 / 检索精排 / 图片描述 全部模型服务;已填则优先于 .env,加密保存、不回显。支持多人/多租户时按账号隔离各自的 key。</p>
              </div>
              <div class="cfg-actions">
                <button class="btn-liquid" id="btn-cfg-save-model">保存模型设置</button>
                <button class="btn-ghost" id="btn-cfg-reset-model">恢复默认</button>
                <span class="cfg-feedback muted" id="cfg-feedback-model"></span>
              </div>
            </div>
          </div>

          <div class="config-panel" id="config-panel-tools">
            <div class="page-head">
              <h2>工具</h2>
              <span class="config-global-note">可插拔 · 启停/注册后下次提问重建 agent</span>
            </div>
            <div class="config-body">
              <div class="cfg-card">
                <div class="cfg-card-title">内置工具</div>
                <div id="cfg-tools-builtin"></div>
                <p class="setting-hint">默认全开;关闭某项后该工具不再绑定到 agent(模型调用不到)。</p>
              </div>
              <div class="cfg-card">
                <div class="cfg-card-title">外部 MCP 工具</div>
                <div class="cfg-ext-toolbar">
                  <span id="cfg-tools-external-empty" class="muted">暂无外部工具</span>
                  <button class="btn-liquid" id="btn-add-external-tool"><svg class="ic ic-sm"><use href="#i-plus" /></svg> 新增外部工具</button>
                </div>
                <div id="cfg-tools-external"></div>
                <p class="setting-hint">通过 MCP(stdio/http)挂载外部能力;注册时先做连通探测。</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  </div>

  <!-- 溯源记录弹窗 -->
  <div id="sources-modal" class="modal hidden">
    <div class="modal-box">
      <div class="modal-head">
        <span>溯源记录</span>
        <button id="btn-sources-close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div id="sources-modal-body"></div>
    </div>
  </div>

  <!-- 单条回答工作流弹窗 -->
  <div id="wf-modal" class="modal hidden">
    <div class="modal-box">
      <div class="modal-head">
        <span><svg class="ic ic-sm"><use href="#i-activity" /></svg> 该回答的工作流</span>
        <button id="btn-wf-modal-close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div id="wf-modal-body"></div>
    </div>
  </div>

  <!-- 文档预览弹窗 -->
  <div id="doc-preview-modal" class="modal hidden">
    <div class="modal-box">
      <div class="modal-head">
        <span id="doc-preview-title">文档预览</span>
        <button id="btn-doc-preview-close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div id="doc-preview-body" class="doc-preview-body"></div>
    </div>
  </div>

  <!-- 报告预览/编辑弹窗 -->
  <div id="report-modal" class="modal hidden">
    <div class="modal-box report-modal-box">
      <div class="modal-head">
        <span id="report-modal-title">报告预览</span>
        <div>
          <button id="btn-report-edit-toggle"><svg class="ic ic-sm"><use href="#i-edit" /></svg> 编辑</button>
          <button id="btn-report-download"><svg class="ic ic-sm"><use href="#i-download" /></svg> 下载</button>
          <button id="btn-report-close"><svg class="ic"><use href="#i-x" /></svg></button>
        </div>
      </div>
      <div id="report-modal-preview" class="markdown-body"></div>
      <textarea id="report-modal-editor" class="hidden"></textarea>
      <div id="report-modal-footer" class="hidden">
        <button id="btn-report-save">保存</button>
        <button id="btn-report-cancel">取消</button>
      </div>
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

  <!-- 新增 / 编辑外部 MCP 工具弹窗 -->
  <div id="external-tool-modal" class="modal hidden">
    <div class="modal-box" style="width: 460px;">
      <div class="modal-head">
        <span id="ext-modal-title"><svg class="ic ic-sm"><use href="#i-gear" /></svg> 新增外部 MCP 工具</span>
        <button id="btn-exttool-close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div class="modal-body">
        <div class="setting-row"><span class="setting-label">工具 key</span></div>
        <input type="text" id="ext-key" class="slider-full cfg-txt" placeholder="如 weather_api(字母数字下划线,不能是内置工具名)">
        <div class="setting-row"><span class="setting-label">显示名</span></div>
        <input type="text" id="ext-label" class="slider-full cfg-txt" placeholder="如 天气数据服务">
        <div class="setting-row"><span class="setting-label">传输方式</span></div>
        <div class="cfg-radio-row">
          <label class="cfg-radio"><input type="radio" name="ext-transport" value="stdio" checked> stdio(本地进程)</label>
          <label class="cfg-radio"><input type="radio" name="ext-transport" value="http"> http(远程服务)</label>
        </div>
        <div id="ext-stdio-fields">
          <div class="setting-row"><span class="setting-label">启动命令 command</span></div>
          <input type="text" id="ext-command" class="slider-full cfg-txt" placeholder="如 python.exe 全路径">
          <div class="setting-row"><span class="setting-label">参数 args(空格分隔)</span></div>
          <input type="text" id="ext-args" class="slider-full cfg-txt" placeholder="如 D:/server.py">
        </div>
        <div id="ext-http-fields" class="hidden">
          <div class="setting-row"><span class="setting-label">服务 URL</span></div>
          <input type="text" id="ext-url" class="slider-full cfg-txt" placeholder="https://… / http://…">
        </div>
        <p class="setting-hint" id="ext-tip">注册时会先连通探测并列出该服务暴露的工具,成功才保存;编辑时会用新配置重新探测。</p>
        <p class="muted cfg-feedback" id="ext-feedback"></p>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="btn-exttool-cancel">取消</button>
        <button class="btn-liquid" id="btn-exttool-save"><svg class="ic ic-sm"><use href="#i-check" /></svg> 探测并注册</button>
      </div>
    </div>
  </div>
</template>
