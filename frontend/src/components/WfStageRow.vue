<script setup>
import { fmtDur, runningAction, paramsText, stepDesc, tokLabel } from "../composables/useWorkflowStages";

/**
 * 工作流阶段行(阶段 + 语义说明 + 参数 + 细化子步骤)。
 * 右侧常驻面板与「该回答的工作流」弹窗共用,避免两处各写一份导致显示不一致。
 */
const props = defineProps({
  stage: { type: Object, required: true },
  /** 实时计时基准:进行中阶段的耗时 = now - realT0。面板传响应式的 now,静态场景可不传 */
  now: { type: Number, default: 0 },
});

function durLabel(s) {
  if (s.state === "running") {
    const base = props.now || Date.now();
    return `${runningAction(s)} (${fmtDur((base - s.realT0) / 1000)})`;
  }
  return s.dur != null ? fmtDur(s.dur) : "";
}
function toolTag(s) {
  return s.name && s.name !== s.step ? s.name : "";
}
</script>

<template>
  <div class="wf-step" :class="stage.state">
    <span class="wf-step-icon">
      <span v-if="stage.state === 'running'" class="wf-spinner"></span>
      <span v-else class="wf-done-ic"><svg class="ic ic-xs"><use href="#i-check" /></svg></span>
    </span>
    <div class="wf-step-main">
      <div class="wf-step-top">
        <span class="wf-step-title">
          <span class="wf-step-name">{{ stage.step }}</span>
          <em v-if="toolTag(stage)" class="wf-step-tool">{{ toolTag(stage) }}</em>
        </span>
        <span v-if="stage.state !== 'running' && tokLabel(stage.tok)" class="wf-step-tok">{{ tokLabel(stage.tok) }}</span>
        <span v-if="stage.state === 'running'" class="wf-running" :data-real="stage.realT0">{{ durLabel(stage) }}</span>
        <span v-else-if="stage.dur != null" class="wf-step-dur">{{ fmtDur(stage.dur) }}</span>
      </div>
      <div v-if="stepDesc(stage)" class="wf-step-desc">{{ stepDesc(stage) }}</div>
      <div v-if="paramsText(stage.params)" class="wf-step-params">{{ paramsText(stage.params) }}</div>

      <div v-if="stage.children && stage.children.length" class="wf-substeps">
        <div v-for="(c, ci) in stage.children" :key="ci" class="wf-substep">
          <span class="wf-substep-dot"></span>
          <div class="wf-substep-main">
            <div class="wf-step-top">
              <span class="wf-substep-title"><span class="wf-substep-name">{{ c.name }}</span></span>
              <span v-if="tokLabel(c.tok)" class="wf-step-tok">{{ tokLabel(c.tok) }}</span>
              <span v-if="c.dur != null" class="wf-step-dur">{{ fmtDur(c.dur) }}</span>
            </div>
            <div v-if="paramsText(c.params)" class="wf-step-params">{{ paramsText(c.params) }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
