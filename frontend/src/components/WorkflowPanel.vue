<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { buildStages, totalRow, fmtDur } from "../composables/useWorkflowStages";
import WfStageRow from "./WfStageRow.vue";
import { activeWorkflowEvents, activeWorkflowUsage, sending } from "../composables/useChat";

/** 面板收起/展开 */
const collapsed = ref(false);
/** 进行中阶段的实时耗时基准(200ms 刷新) */
const tickNow = ref(Date.now());
let timer = null;
const stepsEl = ref(null);

// 依赖 messages / currentId:切会话或流式写入时会重新求值
const stages = computed(() => buildStages(activeWorkflowEvents(), tickNow.value));
const hasRunning = computed(() => stages.value.some((s) => s.state === "running"));

/**
 * 「执行完成」汇总行只在整轮真正结束后才显示。
 * 光看有没有 phase_end 是不够的:模型可能在检索完、又思考了一阵之后才开始吐字,
 * 那期间所有阶段都已结束但回答还在生成 —— 此时显示"执行完成"会误导。
 */
const total = computed(() =>
  !sending.value && !hasRunning.value ? totalRow(activeWorkflowEvents(), activeWorkflowUsage()) : null,
);

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

/**
 * 让"正在执行的那一步"始终可见。
 * 不用 scrollIntoView —— 它会连带滚动外层页面,且动画期间会反复触发;
 * 面板本身是 overflow-y:auto 的固定高度容器,直接设 scrollTop 最稳。
 * 阶段数变化时(新阶段出现)滚到底;同一阶段内计时刷新不动滚动位置。
 */
watch(
  () => stages.value.length,
  async (n, prev) => {
    if (!hasRunning.value) return;
    if (prev !== undefined && n < prev) return; // 切会话导致阶段变少时不滚
    await nextTick();
    if (stepsEl.value) stepsEl.value.scrollTop = stepsEl.value.scrollHeight;
  },
);
</script>

<template>
  <div id="workflow-panel" class="edge-lit" :class="{ collapsed }">
    <div class="wf-header">
      <span><svg class="ic ic-sm"><use href="#i-gear" /></svg> 工作流</span>
      <button id="btn-wf-toggle" title="收起/展开" @click="collapsed = !collapsed">
        <svg class="ic ic-sm"><use href="#i-chevron-right" /></svg>
      </button>
    </div>
    <div id="workflow-steps" ref="stepsEl">
      <div v-if="!stages.length" class="wf-empty">暂无执行记录</div>
      <WfStageRow v-for="(s, i) in stages" :key="i" :stage="s" :now="tickNow"
                  :current="hasRunning && i === stages.length - 1" />

      <!-- 「执行完成」汇总行(仅在整轮真正结束后) -->
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
