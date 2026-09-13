<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { buildStages, totalRow, fmtDur } from "../composables/useWorkflowStages";
import WfStageRow from "./WfStageRow.vue";
import { activeWorkflowEvents, activeWorkflowUsage } from "../composables/useChat";

/** 面板收起/展开 */
const collapsed = ref(false);
/** 进行中阶段的实时耗时基准(200ms 刷新) */
const tickNow = ref(Date.now());
let timer = null;

// 依赖 messages / currentId:切会话或流式写入时会重新求值
const stages = computed(() => buildStages(activeWorkflowEvents(), tickNow.value));
const total = computed(() => totalRow(activeWorkflowEvents(), activeWorkflowUsage()));
const hasRunning = computed(() => stages.value.some((s) => s.state === "running"));

/** 有进行中阶段时才跑实时计时器(与原生版 WorkflowPanel._tick 一致) */
watch(
  hasRunning,
  (running) => {
    if (running && !timer) {
      timer = setInterval(() => { tickNow.value = Date.now(); }, 200);
    } else if (!running && timer) {
      clearInterval(timer);
      timer = null;
    }
  },
  { immediate: true },
);
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <div id="workflow-panel" class="edge-lit" :class="{ collapsed }">
    <div class="wf-header">
      <span><svg class="ic ic-sm"><use href="#i-gear" /></svg> 工作流</span>
      <button id="btn-wf-toggle" title="收起/展开" @click="collapsed = !collapsed">
        <svg class="ic ic-sm"><use href="#i-chevron-right" /></svg>
      </button>
    </div>
    <div id="workflow-steps">
      <div v-if="!stages.length" class="wf-empty">暂无执行记录</div>
      <WfStageRow v-for="(s, i) in stages" :key="i" :stage="s" :now="tickNow" />

      <!-- 「执行完成」汇总行 -->
      <div v-if="total" class="wf-step done wf-total">
        <span class="wf-step-icon"><span class="wf-done-ic"><svg class="ic ic-xs"><use href="#i-check" /></svg></span></span>
        <div class="wf-step-main">
          <div class="wf-step-top">
            <span class="wf-step-name">执行完成</span>
            <span v-for="(b, bi) in total.badges" :key="bi"
                  :class="b.kind === 'tok' ? 'wf-step-tok' : 'wf-step-dur'">{{ b.text }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
