<script setup>
import { computed, onMounted, ref } from "vue";
import {
  messages,
  sending,
  pendingImages,
  pendingDocs,
  stick,
  showJumpBtn,
  send,
  addImages,
  addDocs,
  clearPendingImages,
  clearPendingDocs,
  imageUrl,
  registerInputEl,
  registerMessagesEl,
  initScrollFollow,
  showSessionLoading,
  renderHistory,
  focusInput,
  jumpToBottom,
  copyText,
  regenerateFor,
  ctxVisible,
  ctxPercent,
  ctxUsed,
  ctxLimit,
  fmtCtxTokens,
  setCtxLimit,
} from "../composables/useChat";
import { current as currentModel } from "../composables/useModelPicker";
import { currentView, switchView } from "../composables/useTheme";
import { switchPanel } from "../composables/useConfig";
import { loadSettings } from "../composables/useConfig";
import WorkflowPanel from "./WorkflowPanel.vue";
import SourcesModal from "./SourcesModal.vue";
import WorkflowModal from "./WorkflowModal.vue";
import { API } from "../api";

const inputRef = ref(null);
const msgBoxRef = ref(null);
const imgInputRef = ref(null);
const docInputRef = ref(null);
const sourcesModalRef = ref(null);
const wfModalRef = ref(null);

/** 上下文条样式:接近阈值变色预警(>80% 偏橙,满格红) */
const ctxFillClass = computed(() => {
  const p = ctxPercent.value;
  return "ctx-fill" + (p >= 100 ? " full" : p >= 80 ? " warn" : "");
});
const ctxTitle = computed(
  () => `当前上下文约 ${ctxUsed.value.toLocaleString()} tokens,达 ${ctxLimit.value.toLocaleString()} 时自动压缩历史`,
);

onMounted(async () => {
  registerInputEl(inputRef.value);
  registerMessagesEl(msgBoxRef.value);
  initScrollFollow();
  // 进度条分母取后端阈值(与摘要触发阈值同源);失败则保留默认 200k
  try {
    const s = await API.get("/api/settings");
    if (s && s.context_limit) setCtxLimit(s.context_limit);
  } catch (_) { /* 保留默认 */ }
});

function doSend() {
  const q = (inputRef.value?.value || "").trim();
  if (!q && !pendingImages.value.length && !pendingDocs.value.length) return;
  send(q, [...pendingImages.value], [...pendingDocs.value]);
}

function onKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
}

/** 输入框自增高(上限 120px) */
function onInput() {
  const el = inputRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

function pickImages(e) {
  addImages(e.target.files);
  e.target.value = "";
}
function pickDocs(e) {
  addDocs(e.target.files);
  e.target.value = "";
}

/** 粘贴图片 */
function onPaste(e) {
  const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
  if (files.length) addImages(files);
}

/** 点模型徽标 → 跳到系统配置的对话模型面板 */
async function gotoConfig() {
  switchView("config");
  try {
    await loadSettings();
  } catch (_) {}
  await switchPanel("model");
  requestAnimationFrame(() => {
    document.querySelector("#config-panel-model .cfg-card")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

/** 历史消息里的图片来源:路径字符串或 {saved_path,path} 对象都要兼容 */
function msgImageSrc(img) {
  const path = typeof img === "string" ? img : (img && (img.saved_path || img.path || ""));
  if (!path) return "";
  return path.startsWith("http") ? path : imageUrl(path);
}
function msgImages(m) {
  return (m.images || []).map(msgImageSrc).filter(Boolean);
}
function msgDocs(m) {
  return (m.docs || []).map((d) => d.name).filter(Boolean);
}
</script>

<template>
  <section class="view" :class="{ active: currentView === 'chat' }" id="view-chat">
    <div id="chat-area">
      <div id="chat-messages" ref="msgBoxRef">
        <div v-if="!messages.length" id="chat-welcome" class="welcome">
          <h2>你好</h2>
          <p>我是你的知识库助手,基于已接入知识库,为你提供准确、可靠的信息检索与分析。</p>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <!-- 图片(用户消息的附件) -->
          <img v-for="(src, ii) in msgImages(m)" :key="ii" class="msg-img" :src="src" :alt="''">

          <template v-if="m.role === 'user'">
            <div class="msg-bubble">{{ m.content }}</div>
            <div v-if="msgDocs(m).length" class="msg-doc-attach">
              <span v-for="(n, di) in msgDocs(m)" :key="di" class="doc-chip">
                <svg class="ic ic-xs"><use href="#i-file" /></svg>
                <span class="doc-chip-name">{{ n }}</span>
              </span>
            </div>
          </template>

          <template v-else>
            <div class="msg-card">
              <div class="msg-head">
                <span class="msg-logo"></span>
                <span class="msg-head-name">知识库助手</span>
              </div>
              <div class="msg-bubble md-body" v-html="m.renderedHtml"></div>
            </div>
            <div v-if="m.content" class="msg-actions">
              <button class="act-copy" title="复制" @click="copyText(m.content)">
                <svg class="ic ic-sm"><use href="#i-copy" /></svg>
              </button>
              <button class="act-regen" title="重新生成" @click="regenerateFor(m)">
                <svg class="ic ic-sm"><use href="#i-refresh" /></svg>
              </button>
              <button class="act-sources" title="溯源记录" @click="sourcesModalRef.open(m)">
                <svg class="ic ic-sm"><use href="#i-link" /></svg>
              </button>
              <button class="act-wf" title="工作流" @click="wfModalRef.open(m)">
                <svg class="ic ic-sm"><use href="#i-activity" /></svg>
              </button>
            </div>
          </template>
        </div>
      </div>

      <div id="chat-input-wrap">
        <button id="btn-scroll-bottom" class="scroll-bottom-btn" :class="{ hidden: !showJumpBtn }"
                title="回到最新" @click="jumpToBottom">
          <svg class="ic"><use href="#i-chevron-down" /></svg>
        </button>

        <div id="ctx-meter-wrap" :class="{ hidden: !ctxVisible }">
          <div id="ctx-meter" class="ctx-meter">
            <div id="ctx-meter-fill" :class="ctxFillClass" :style="{ width: ctxPercent + '%' }"></div>
          </div>
          <span id="ctx-meter-text" class="ctx-text">{{ fmtCtxTokens(ctxUsed) }} / {{ fmtCtxTokens(ctxLimit) }}</span>
        </div>

        <div id="chat-docs-preview">
          <span v-for="(d, i) in pendingDocs" :key="i" class="doc-chip">
            <svg class="ic ic-xs"><use href="#i-file" /></svg>
            <span class="doc-chip-name">{{ d.name }}</span>
            <button class="doc-chip-remove" title="移除" @click="pendingDocs.splice(i, 1)">
              <svg class="ic ic-xs"><use href="#i-x" /></svg>
            </button>
          </span>
        </div>
        <div id="chat-images-preview">
          <div v-for="(img, i) in pendingImages" :key="i" class="chat-img-preview">
            <img :src="imageUrl(img.path)" :alt="img.name">
            <button class="img-remove" title="移除" @click="pendingImages.splice(i, 1)">
              <svg class="ic"><use href="#i-x" /></svg>
            </button>
          </div>
        </div>

        <div id="chat-input-box" class="edge-lit">
          <div class="input-tools">
            <button class="tool-btn" id="btn-img-pick" title="上传图片" @click="imgInputRef.click()">
              <svg class="ic"><use href="#i-image" /></svg>
            </button>
            <button class="tool-btn" id="btn-file-pick" title="上传文档" @click="docInputRef.click()">
              <svg class="ic"><use href="#i-paperclip" /></svg>
            </button>
          </div>
          <textarea
            id="chat-input" ref="inputRef" rows="1"
            placeholder="输入你的问题,Enter 发送,Shift+Enter 换行"
            @keydown="onKeydown" @input="onInput" @paste="onPaste"
          ></textarea>
          <div id="chat-model-badge" class="chat-model-badge"
               title="当前对话模型(系统配置→对话模型 中切换)" @click="gotoConfig">
            <span id="chat-model-label">{{ currentModel || "模型" }}</span>
            <span class="chat-model-goto">切换</span>
          </div>
          <button id="btn-send" class="btn-liquid" title="发送" :disabled="sending" @click="doSend">
            <svg class="ic"><use href="#i-send" /></svg>
          </button>
          <input ref="imgInputRef" type="file" id="img-file-input" accept="image/*" multiple hidden @change="pickImages">
          <input ref="docInputRef" type="file" id="doc-file-input-chat" multiple hidden
                 accept=".pdf,.docx,.doc,.txt,.md,.markdown,.csv,.xlsx,.xls" @change="pickDocs">
        </div>
      </div>
    </div>

    <WorkflowPanel />
    <SourcesModal ref="sourcesModalRef" />
    <WorkflowModal ref="wfModalRef" />
  </section>
</template>
