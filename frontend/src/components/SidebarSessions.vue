<script setup>
import { nextTick, ref, watch } from "vue";
import {
  currentId,
  searchKeyword,
  renamingId,
  visibleGroups,
  sessionTitle,
  sessionSummary,
  timeLabel,
  create,
  switchTo,
  remove,
  startRename,
  commitRename,
  cancelRename,
} from "../composables/useSessions";
import { saveCurrentMessages } from "../composables/useChat";

const renameInput = ref(null);
/** 防止 blur 与 Enter/Esc 双触发重复提交(原生版用 committed 标志) */
let committed = false;

/**
 * 切换会话前先把当前消息存进缓存。
 * 否则正在生成的那轮(消息对象还在被 SSE 回调写入)会随消息列表清空而丢失,
 * 切回来就看不到内容了。
 */
function onSwitch(id) {
  if (id === currentId.value) return;
  saveCurrentMessages();
  switchTo(id);
}

/** 新建会话同样要先把当前消息存下(可能正有生成中的回答) */
function onNewSession() {
  saveCurrentMessages();
  create();
}

/**
 * 用函数 ref 而不是 ref="name":后者放在 v-for 里会被 Vue 收集成数组,
 * 导致 renameInput.value 是数组(读不到 .value、也调不了 .focus())。
 * 同一时刻只有一个会话在重命名,直接持有该元素即可。
 */
function setRenameInput(el) {
  if (el) renameInput.value = el;
}

watch(renamingId, async (id) => {
  if (!id) return;
  committed = false;
  await nextTick();
  renameInput.value?.focus();
  renameInput.value?.select();
});

function onRenameKey(e, s) {
  // 阻止冒泡,免得 Enter 触发会话切换
  e.stopPropagation();
  if (e.key === "Enter") {
    e.preventDefault();
    finishRename(s, true);
  } else if (e.key === "Escape") {
    finishRename(s, false);
  }
}

function finishRename(s, save) {
  if (committed) return;
  committed = true;
  const val = renameInput.value?.value ?? "";
  if (save) commitRename(s.session_id, s.title, val);
  else cancelRename();
}
</script>

<template>
  <div id="sidebar-sessions">
    <button id="btn-new-session" class="btn-liquid" @click="onNewSession">
      <svg class="ic"><use href="#i-plus" /></svg> 新建会话
    </button>
    <div class="session-search">
      <svg class="ic ic-sm"><use href="#i-search" /></svg>
      <input type="text" id="session-search-input" placeholder="搜索会话" v-model="searchKeyword">
    </div>
    <div id="session-list">
      <template v-for="g in visibleGroups()" :key="g.name">
        <div class="session-group">
          <div class="session-group-title">{{ g.name }}</div>
          <div
            v-for="s in g.items"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === currentId }"
            @click="onSwitch(s.session_id)"
          >
            <span class="session-icon"><svg class="ic ic-xs"><use href="#i-chat" /></svg></span>
            <div class="session-body">
              <!-- 重命名中:标题位置换成输入框;summary 隐藏(与原生版一致) -->
              <input
                v-if="renamingId === s.session_id"
                :ref="setRenameInput"
                type="text"
                class="session-rename-input"
                :value="s.title === '新会话' ? '' : s.title"
                placeholder="输入会话名称"
                maxlength="40"
                @click.stop
                @keydown="onRenameKey($event, s)"
                @blur="finishRename(s, true)"
              >
              <div v-else class="session-title">{{ sessionTitle(s) }}</div>
              <div class="session-summary" :style="renamingId === s.session_id ? 'display:none' : ''">
                {{ sessionSummary(s) }}
              </div>
            </div>
            <span class="session-time">{{ timeLabel(s.updated_at || s.created_at) }}</span>
            <button class="session-del" title="删除会话" @click.stop="remove(s.session_id)">
              <svg class="ic ic-xs"><use href="#i-trash" /></svg>
            </button>
            <button class="session-ren" title="重命名会话" @click.stop="startRename(s.session_id)">
              <svg class="ic ic-xs"><use href="#i-edit" /></svg>
            </button>
          </div>
        </div>
      </template>
      <div v-if="!visibleGroups().length" class="wf-empty">暂无会话</div>
    </div>
  </div>
</template>
