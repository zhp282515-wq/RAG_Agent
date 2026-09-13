<script setup>
import { computed, ref } from "vue";
import { buildStages, totalRow } from "../composables/useWorkflowStages";
import WfStageRow from "./WfStageRow.vue";

/** 单条回答的工作流弹窗(与右侧常驻面板共用同一套阶段行) */
const open = ref(false);
const events = ref([]);
const usage = ref(null);

function show(msg) {
  events.value = msg?.workflow || [];
  usage.value = msg?.usage || null;
  open.value = true;
}
function close() {
  open.value = false;
}
defineExpose({ open: show, close });

const stages = computed(() => buildStages(events.value));
const hasRunning = computed(() => stages.value.some((s) => s.state === "running"));
/** 弹窗展示的是已结束的历史回答,有 running 阶段说明记录不完整,此时不显示汇总行 */
const total = computed(() => (hasRunning.value ? null : totalRow(events.value, usage.value)));
</script>

<template>
  <div id="wf-modal" class="modal" :class="{ hidden: !open }" @click.self="close">
    <div class="modal-box">
      <div class="modal-head">
        <span><svg class="ic ic-sm"><use href="#i-activity" /></svg> 该回答的工作流</span>
        <button id="btn-wf-modal-close" @click="close"><svg class="ic"><use href="#i-x" /></svg></button>
      </div>
      <div id="wf-modal-body">
        <p v-if="!events.length" class="muted">该回答暂无工作流记录(未经过检索或工具执行)。</p>
        <template v-else>
          <WfStageRow v-for="(s, i) in stages" :key="i" :stage="s" />
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
        </template>
      </div>
    </div>
  </div>
</template>
