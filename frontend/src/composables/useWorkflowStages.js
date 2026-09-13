/* ============================================
   useWorkflowStages — trace 事件 → 阶段列表
   纯函数搬运自原生版 chat.js 的 WF 对象(buildStages / paramsText / fmtDur / totalRow …),
   算法逐行保持一致,只把 DOM 拼接换成数据返回。

   事件形状:{ step, action, ph?, name?, params?, duration?, tokens? }
   action ∈ { phase_start, phase_end, substep, <其它=瞬时事件> }
   ============================================ */

/** 阶段/子步骤的 token 简写:满千 → "1.2k" */
export function fmtTok(n) {
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return "";
  return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(Math.round(v));
}

/** 耗时:≥60s 用分钟 */
export function fmtDur(sec) {
  if (sec == null) return "";
  return sec >= 60 ? (sec / 60).toFixed(1) + " 分钟" : Number(sec).toFixed(2) + "s";
}

/** 阶段/子步骤的"在干什么"说明 */
export function stepDesc(s) {
  return s.semantic || "";
}

/** 某事件的 token 徽标文案(无计量则空) */
export function tokLabel(n) {
  const s = fmtTok(n);
  return s ? `${s} tok` : "";
}

/**
 * 把 trace 事件序列组装成详细阶段列表。
 * - phase_start/phase_end 配对成"阶段"(可重复,如多轮 模型/检索);
 * - substep 挂在所属未闭合阶段的 children 里 → 工作流精确到每一步;
 * - 阶段按上下文推断动作语义(模型:首次=理解规划,检索后=整合生成)。
 *
 * now 由调用方传入(便于测试;原生版用 performance.now())。
 */
export function buildStages(events, now = Date.now()) {
  const stages = [];
  const openByPh = {}; // ph_id -> 未闭合阶段的 index(精确归属)
  const openSeq = [];  // 未闭合阶段 index 顺序(补全语义用)

  const pushSub = (parentIdx, ev) => {
    const s = stages[parentIdx];
    if (!s) return;
    s.children.push({
      name: ev.name || "步骤",
      params: ev.params || {},
      dur: ev.duration != null ? Number(ev.duration) : null,
      tok: ev.tokens != null ? Number(ev.tokens) : null,
    });
  };

  for (const ev of events || []) {
    const step = ev.step || "未知";
    const act = ev.action || "phase";
    const ph = ev.ph != null ? ev.ph : null;

    if (act === "phase_start") {
      const idx = stages.length;
      stages.push({
        step,
        name: ev.name || "",
        params: ev.params || {},
        realT0: now,
        dur: null,
        state: "running",
        children: [],
      });
      if (ph != null) openByPh[ph] = idx;
      openSeq.push(idx);
    } else if (act === "phase_end") {
      // 优先按 ph_id 精确定位(不同名/同名的阶段都不串)
      let found = -1;
      if (ph != null && openByPh[ph] != null) {
        found = openByPh[ph];
        delete openByPh[ph];
        const oi = openSeq.indexOf(found);
        if (oi >= 0) openSeq.splice(oi, 1);
      } else {
        // 兼容无 ph 的历史事件:从尾部找同名未闭合
        for (let i = openSeq.length - 1; i >= 0; i--) {
          if (stages[openSeq[i]].step === step) {
            found = openSeq[i];
            openSeq.splice(i, 1);
            break;
          }
        }
      }
      if (found >= 0) {
        stages[found].dur = ev.duration != null ? Number(ev.duration) : null;
        stages[found].state = "done";
        if (ev.tokens != null) stages[found].tok = Number(ev.tokens);
        // params 合并而非覆盖:保留 phase_start 带来的检索词/命中数
        if (ev.params) stages[found].params = Object.assign({}, stages[found].params, ev.params);
      } else {
        stages.push({
          step, name: ev.name || "", params: ev.params || {}, realT0: now,
          dur: ev.duration != null ? Number(ev.duration) : null, state: "done", children: [],
          tok: ev.tokens != null ? Number(ev.tokens) : null,
        });
      }
    } else if (act === "substep") {
      // 按 ph_id 精确挂到所属阶段(而非"最近未闭合"——交错/同名时不再错挂)
      let parentIdx = -1;
      if (ph != null && openByPh[ph] != null) parentIdx = openByPh[ph];
      else parentIdx = openSeq.length ? openSeq[openSeq.length - 1] : stages.length - 1;
      if (parentIdx >= 0) pushSub(parentIdx, ev);
    } else {
      // 独立事件(瞬时提示)
      stages.push({
        step, name: ev.name || "", params: ev.params || {}, realT0: now,
        dur: ev.duration != null ? Number(ev.duration) : null, state: "done", children: [],
      });
    }
  }
  // 流式结束仍 running 的补为完成、无耗时(不该出现,防御)
  for (const s of stages) {
    if (s.state === "running") {
      s.state = "done";
      s.dur = null;
    }
  }

  // 给"模型"阶段按位置定语义:
  //   开头/检索前 → 理解问题并规划检索;检索后(且非最后) → 评估结果决定下一步;
  //   最后一个模型 → 整合资料生成回答。检索阶段始终"检索并精排资料"。
  const lastModelIdx = (() => {
    let m = -1;
    stages.forEach((s, i) => { if (s.step === "模型") m = i; });
    return m;
  })();
  let seenRetrieval = false;
  stages.forEach((s, i) => {
    if (s.step === "模型") {
      if (i === lastModelIdx && seenRetrieval) s.semantic = "整合检索资料,生成最终回答";
      else if (seenRetrieval) s.semantic = "评估检索结果,判断是否足够作答";
      else s.semantic = "理解问题,规划检索方案与调用工具";
    } else if (s.step === "检索") {
      seenRetrieval = true;
      s.semantic = "在知识库中向量召回 → rerank 精排 → 达标过滤";
    } else if (s.step === "报告场景") {
      s.semantic = "读取并填充报告所需的用户使用数据";
    }
  });
  return stages;
}

/** 阶段正在进行的动作描述(进行中可视化文案) */
export function runningAction(s) {
  const step = s.step || "";
  if (step === "模型") return s.semantic || "模型正在思考…";
  if (step === "检索") return "正在检索知识库…";
  if (step === "报告场景") return "正在读取报告数据…";
  if (s.name && s.name !== step) return `正在执行 ${s.name}…`;
  return `${step} 处理中…`;
}

/** 重要参数可视化(检索词/命中数/用户/月份/上下文条数/子步/压缩量) */
export function paramsText(params) {
  if (!params) return "";
  const parts = [];
  if (params.query) parts.push(`检索词:${params.query}`);
  if (params["命中"] != null) parts.push(`命中 ${params["命中"]} 条`);
  if (params["用户"]) parts.push(`用户 ${params["用户"]}`);
  if (params["月份"]) parts.push(`月份 ${params["月份"]}`);
  if (params["消息"] != null) parts.push(`${params["消息"]} 条上下文`);
  if (params["候选数"] != null) parts.push(`${params["候选数"]} 个候选`);
  if (params["通过"] != null) parts.push(`通过 ${params["通过"]} 条`);
  if (params["剔除低相关"] != null) parts.push(`剔除 ${params["剔除低相关"]} 条`);
  if (params["说明"]) parts.push(`${params["说明"]}`);
  if (params["阶段"]) parts.push(`${params["阶段"]}`);
  // 上下文压缩:压缩前后与省下的量
  if (params["压缩前"] != null) parts.push(`压缩前 ${params["压缩前"]}`);
  if (params["压缩后"] != null) parts.push(`压缩后 ${params["压缩后"]}`);
  if (params["省下"] != null) parts.push(`省下 ${params["省下"]}`);
  if (params["保留消息数"] != null) parts.push(`保留 ${params["保留消息数"]} 条原文`);
  // 模型调用的输入/输出/推理(合计走 tokens 徽标,不在此重复)
  if (params["输入"] != null) parts.push(`输入 ${fmtTok(params["输入"])}`);
  if (params["输出"] != null) parts.push(`输出 ${fmtTok(params["输出"])}`);
  if (params["推理"] != null && Number(params["推理"]) > 0) parts.push(`推理 ${fmtTok(params["推理"])}`);
  return parts.join(" · ");
}

/**
 * 底部「执行完成」汇总行。
 * 耗时:各 phase_end duration 之和。
 * token:优先落库的 usage.total(计费总量);历史消息无 usage 时退化为累加各事件 tokens。
 * 只累加 phase_end 的 tokens —— 子步骤的消耗已包含在其父阶段里,两者都加会把检索算两遍。
 * 返回 null 表示无任何可展示的汇总(调用方不渲染)。
 */
export function totalRow(events, usage) {
  const done = (events || []).filter((e) => e.action === "phase_end" && e.duration != null);
  const sumTok = (events || []).reduce(
    (a, e) => a + (e.action === "phase_end" ? Number(e.tokens) || 0 : 0), 0);
  const tokens = usage && usage.total != null ? Number(usage.total) : sumTok;
  const reason = usage && usage.reason != null ? Number(usage.reason) : 0;
  const totalDur = done.reduce((s, e) => s + Number(e.duration), 0);

  const badges = [];
  if (tokens > 0) badges.push({ kind: "tok", text: `共 ${fmtTok(tokens)} tok` });
  if (reason > 0) badges.push({ kind: "tok", text: `推理 ${fmtTok(reason)}` });
  if (done.length) badges.push({ kind: "dur", text: `共 ${fmtDur(totalDur)}` });
  if (!badges.length) return null;
  return { badges };
}
