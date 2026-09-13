<script setup>
import { onMounted } from "vue";
import { currentView } from "../composables/useTheme";
import {
  currentPanel,
  retrieval,
  temp,
  apiKeyInput,
  modelMenuOpen,
  models,
  settings,
  feedbacks,
  extModalOpen,
  extEditing,
  extForm,
  extFeedback,
  extSaving,
  saveRetrieval,
  resetRetrieval,
  fmtThreshold,
  shownModel,
  pickModel,
  saveModel,
  resetModel,
  keyState,
  keyPlaceholder,
  saveApiKey,
  clearApiKey,
  builtinTools,
  externalTools,
  toggleTool,
  removeExternal,
  extLocation,
  openAddExternal,
  editExternal,
  closeExtModal,
  submitExternal,
  loadSettings,
} from "../composables/useConfig";

onMounted(() => {
  loadSettings();
});

/** 反馈文案(4 秒后由 composable 自动清除) */
function fb(target) {
  return feedbacks.value[target] || { text: "", ok: false };
}
</script>

<template>
  <section class="view" id="view-config" :class="{ active: currentView === 'config' }">
    <!-- ===== 检索参数 ===== -->
    <div class="config-panel" :class="{ active: currentPanel === 'retrieval' }" id="config-panel-retrieval">
      <div class="page-head">
        <h2>检索参数</h2>
        <span class="config-global-note">全局默认 · 每次提问 / 检索读取</span>
      </div>
      <div class="config-body">
        <div class="cfg-card">
          <div class="cfg-card-title">向量召回 &amp; Rerank 精排</div>
          <div class="setting-row">
            <span class="setting-label">召回候选数 top_k</span>
            <span class="setting-value" id="cfg-topk-val">{{ retrieval.top_k }}</span>
          </div>
          <input type="number" id="cfg-topk" class="slider-full cfg-num" min="1" max="100" v-model.number="retrieval.top_k">
          <div class="setting-row">
            <span class="setting-label">精排返回数 rerank_n</span>
            <span class="setting-value" id="cfg-rerank-val">{{ retrieval.rerank_n }}</span>
          </div>
          <input type="number" id="cfg-rerank" class="slider-full cfg-num" min="1" max="100" v-model.number="retrieval.rerank_n">
          <p class="setting-hint">top_k=向量召回候选数;rerank_n=精排后返回条数。检索调试页可临时覆盖 top_k,不改全局。</p>
        </div>

        <div class="cfg-card">
          <div class="cfg-card-title">相关度阈值</div>
          <div class="setting-row">
            <span class="setting-label">高相关线</span>
            <span class="setting-value" id="cfg-high-val">{{ fmtThreshold(retrieval.score_high) }}</span>
          </div>
          <input type="range" id="cfg-high" min="0.50" max="1.0" step="0.01" class="slider-full" v-model.number="retrieval.score_high">
          <div class="setting-row">
            <span class="setting-label">达标线(注入上下文)</span>
            <span class="setting-value" id="cfg-min-val">{{ fmtThreshold(retrieval.score_min) }}</span>
          </div>
          <input type="range" id="cfg-min" min="0.1" max="0.9" step="0.01" class="slider-full" v-model.number="retrieval.score_min">
          <p class="setting-hint">≥高相关线=「高」;≥达标线才注入上下文,&lt;达标线不返回。</p>
        </div>

        <div class="cfg-actions">
          <button class="btn-liquid" id="btn-cfg-save-retrieval" @click="saveRetrieval">保存检索设置</button>
          <button class="btn-ghost" id="btn-cfg-reset-retrieval" @click="resetRetrieval">恢复默认</button>
          <span class="cfg-feedback" id="cfg-feedback-retrieval"
                :class="{ muted: !fb('retrieval').ok, 'cfg-ok': fb('retrieval').ok }">{{ fb("retrieval").text }}</span>
        </div>
      </div>
    </div>

    <!-- ===== 对话模型 ===== -->
    <div class="config-panel" :class="{ active: currentPanel === 'model' }" id="config-panel-model">
      <div class="page-head">
        <h2>对话模型</h2>
        <span class="config-global-note">全局默认 · 下次提问生效</span>
      </div>
      <div class="config-body">
        <div class="cfg-card">
          <div class="cfg-card-title">默认模型</div>
          <div class="setting-row"><span class="setting-label">模型</span></div>
          <div id="config-model-picker" class="model-picker cfg-model-picker" :class="{ open: modelMenuOpen }">
            <button id="config-model-btn" class="model-picker-btn" type="button"
                    @click.stop="modelMenuOpen = !modelMenuOpen">
              <span id="config-model-label">{{ shownModel() }}</span>
              <svg class="ic ic-xs mp-arrow"><use href="#i-chevron-down" /></svg>
            </button>
            <div id="config-model-menu" class="model-picker-menu" :class="{ hidden: !modelMenuOpen }">
              <button v-for="m in models" :key="m" type="button" class="model-picker-item"
                      :class="{ active: m === shownModel() }" @click="pickModel(m)">{{ m }}</button>
            </div>
          </div>
          <p class="setting-hint">作为新会话与报告场景的默认模型;聊天输入框旁仍可临时切换。</p>
        </div>

        <div class="cfg-card">
          <div class="cfg-card-title">温度</div>
          <div class="setting-row">
            <span class="setting-label">创造力 / 随机性</span>
            <span class="setting-value" id="cfg-temp-val">{{ Number(temp).toFixed(1) }}</span>
          </div>
          <input type="range" id="cfg-temp" min="0" max="2" step="0.1" class="slider-full" v-model.number="temp">
          <p class="setting-hint">0=更确定保守;越高越多样发散。知识问答建议 0.3~0.7。</p>
        </div>

        <div class="cfg-card">
          <div class="cfg-card-title">模型服务 API Key</div>
          <div class="cfg-apikey-row">
            <input type="password" id="cfg-api-key" class="slider-full cfg-txt" autocomplete="off"
                   :placeholder="keyPlaceholder()" v-model="apiKeyInput">
            <span class="cfg-apikey-state" id="cfg-api-key-state"
                  :class="{ muted: !keyState().ok, 'cfg-ok': keyState().ok }">{{ keyState().text }}</span>
          </div>
          <div class="cfg-apikey-actions">
            <button class="btn-ghost" id="btn-cfg-save-key" @click="saveApiKey">保存 API Key</button>
            <button class="btn-ghost" id="btn-cfg-clear-key" @click="clearApiKey">清除(改用 .env)</button>
          </div>
          <p class="setting-hint">供 聊天 / 知识库向量 / 检索精排 / 图片描述 全部模型服务;已填则优先于 .env,加密保存、不回显。支持多人/多租户时按账号隔离各自的 key。</p>
        </div>

        <div class="cfg-actions">
          <button class="btn-liquid" id="btn-cfg-save-model" @click="saveModel">保存模型设置</button>
          <button class="btn-ghost" id="btn-cfg-reset-model" @click="resetModel">恢复默认</button>
          <span class="cfg-feedback" id="cfg-feedback-model"
                :class="{ muted: !fb('model').ok, 'cfg-ok': fb('model').ok }">{{ fb("model").text }}</span>
        </div>
      </div>
    </div>

    <!-- ===== 工具 ===== -->
    <div class="config-panel" :class="{ active: currentPanel === 'tools' }" id="config-panel-tools">
      <div class="page-head">
        <h2>工具</h2>
        <span class="config-global-note">可插拔 · 启停/注册后下次提问重建 agent</span>
      </div>
      <div class="config-body">
        <div class="cfg-card">
          <div class="cfg-card-title">内置工具</div>
          <div id="cfg-tools-builtin">
            <p v-if="!builtinTools().length" class="muted">无内置工具</p>
            <div v-for="t in builtinTools()" :key="t.key" class="tool-row">
              <div class="tool-row-main">
                <div class="tool-row-title">
                  {{ t.label }}<span class="cfg-key">{{ t.key }}</span>
                </div>
                <span v-if="t.note" class="cfg-note muted">{{ t.note }}</span>
              </div>
              <label class="switch">
                <input type="checkbox" :checked="t.enabled" @change="toggleTool(t.key, $event.target.checked)">
                <span class="switch-slider"></span>
              </label>
            </div>
          </div>
          <p class="setting-hint">默认全开;关闭某项后该工具不再绑定到 agent(模型调用不到)。</p>
        </div>

        <div class="cfg-card">
          <div class="cfg-card-title">外部 MCP 工具</div>
          <div class="cfg-ext-toolbar">
            <span v-show="!externalTools().length" id="cfg-tools-external-empty" class="muted">暂无外部工具</span>
            <button class="btn-liquid" id="btn-add-external-tool" @click="openAddExternal">
              <svg class="ic ic-sm"><use href="#i-plus" /></svg> 新增外部工具
            </button>
          </div>
          <div id="cfg-tools-external">
            <div v-for="t in externalTools()" :key="t.key" class="tool-row">
              <div class="tool-row-main">
                <div class="tool-row-title">
                  {{ t.label }}
                  <span class="cfg-key">{{ t.key }}</span>
                  <span class="fmt-badge" style="--fmt-c:#3E7BFA">{{ t.transport || "stdio" }}</span>
                </div>
                <div class="cfg-loc muted" :title="extLocation(t)">{{ extLocation(t) || "未配置" }}</div>
              </div>
              <div class="tool-row-ops">
                <label class="switch">
                  <input type="checkbox" :checked="t.enabled" @change="toggleTool(t.key, $event.target.checked)">
                  <span class="switch-slider"></span>
                </label>
                <button class="icon-btn btn-edit-ext" title="编辑外部工具(连接/显示名)" @click="editExternal(t.key)">
                  <svg class="ic ic-sm"><use href="#i-edit" /></svg>
                </button>
                <button class="icon-btn btn-del-ext" title="删除外部工具" @click="removeExternal(t.key)">
                  <svg class="ic ic-sm"><use href="#i-trash" /></svg>
                </button>
              </div>
            </div>
          </div>
          <p class="setting-hint">通过 MCP(stdio/http)挂载外部能力;注册时先做连通探测。</p>
        </div>
      </div>
    </div>

    <!-- ===== 新增 / 编辑外部 MCP 工具弹窗 ===== -->
    <div id="external-tool-modal" class="modal" :class="{ hidden: !extModalOpen }" @click.self="closeExtModal">
      <div class="modal-box" style="width: 460px;">
        <div class="modal-head">
          <span id="ext-modal-title">
            <svg class="ic ic-sm"><use href="#i-gear" /></svg>
            {{ extEditing ? `编辑外部工具 · ${extEditing}` : "新增外部 MCP 工具" }}
          </span>
          <button id="btn-exttool-close" @click="closeExtModal"><svg class="ic"><use href="#i-x" /></svg></button>
        </div>
        <div class="modal-body">
          <div class="setting-row"><span class="setting-label">工具 key</span></div>
          <input type="text" id="ext-key" class="slider-full cfg-txt" v-model="extForm.key" :readonly="!!extEditing"
                 :title="extEditing ? '工具 key 为唯一标识,创建后不可修改' : ''"
                 placeholder="如 weather_api(字母数字下划线,不能是内置工具名)">

          <div class="setting-row"><span class="setting-label">显示名</span></div>
          <input type="text" id="ext-label" class="slider-full cfg-txt" v-model="extForm.label"
                 placeholder="如 天气数据服务">

          <div class="setting-row"><span class="setting-label">传输方式</span></div>
          <div class="cfg-radio-row">
            <label class="cfg-radio"><input type="radio" name="ext-transport" value="stdio" v-model="extForm.transport"> stdio(本地进程)</label>
            <label class="cfg-radio"><input type="radio" name="ext-transport" value="http" v-model="extForm.transport"> http(远程服务)</label>
          </div>

          <div id="ext-stdio-fields" :class="{ hidden: extForm.transport !== 'stdio' }">
            <div class="setting-row"><span class="setting-label">启动命令 command</span></div>
            <input type="text" id="ext-command" class="slider-full cfg-txt" v-model="extForm.command"
                   placeholder="如 python.exe 全路径">
            <div class="setting-row"><span class="setting-label">参数 args(空格分隔)</span></div>
            <input type="text" id="ext-args" class="slider-full cfg-txt" v-model="extForm.args"
                   placeholder="如 D:/server.py">
          </div>

          <div id="ext-http-fields" :class="{ hidden: extForm.transport !== 'http' }">
            <div class="setting-row"><span class="setting-label">服务 URL</span></div>
            <input type="text" id="ext-url" class="slider-full cfg-txt" v-model="extForm.url"
                   placeholder="https://… / http://…">
          </div>

          <p class="setting-hint" id="ext-tip">注册时会先连通探测并列出该服务暴露的工具,成功才保存;编辑时会用新配置重新探测。</p>
          <p class="muted cfg-feedback" id="ext-feedback">{{ extFeedback }}</p>
        </div>
        <div class="modal-foot">
          <button class="btn-ghost" id="btn-exttool-cancel" @click="closeExtModal">取消</button>
          <button class="btn-liquid" id="btn-exttool-save" :disabled="extSaving" @click="submitExternal">
            <svg class="ic ic-sm"><use href="#i-check" /></svg>
            {{ extEditing ? "保存更改" : "探测并注册" }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
