"""规则硬校验层:在 LLM 之后运行,不耗 token,规则优先于模型自检。

职责边界:
  - 本层**只做判定**,不做改写。它读 LLM 的产出,再用确定性规则复核一遍。
  - 任何一条规则不通过 → 最终 decision 强制为 use_original(覆盖 LLM 的判断)。
  - 规则不依赖 LLM 自报的字段是否诚实:否定词/数字/实体是从**原问题与改写后的
    问题文本里重新抽取**的,LLM 在 preserved_constraints 里漏报也没用。

为什么还要「从置信度反推 decision」而不是照抄 LLM 的 decision:
需求要求阈值可配置、可调优。如果直接采信 LLM 给的 decision,那么把 conf_high 从
0.9 调到 0.95 将毫无效果 —— 阈值形同虚设。所以这里由 (changed, self_check,
confidence) 按阈值**重新推导** decision,LLM 的 decision 只作为交叉验证信号记进日志。
唯一的例外是 ask_clarify:追问与否是语义判断,启发式判不准,故仍由 LLM 发起,
但必须通过「改写实质上没动」的一致性闸门。
"""
from __future__ import annotations

from utils.logger_tool import logger
from utils.query_rewrite import config, glossary
from utils.query_rewrite.models import (
    DECISION_ASK_CLARIFY,
    DECISION_DUAL,
    DECISION_USE_ORIGINAL,
    DECISION_USE_REWRITTEN,
    HardCheckResult,
    RewriteResult,
)

# 规则名常量:日志里按名字聚合,可以统计哪条规则最常拦下改写(调阈值/改词典的依据)
RULE_MULTI_INTENT_DUAL = "多意图禁用双路检索"
RULE_NEGATION_MISMATCH = "否定词数量不一致"
RULE_NUMBER_MISMATCH = "数字/时间不一致"
RULE_ENTITY_DROPPED = "实体未被保留"
RULE_DOMAIN_TERM_DROPPED = "领域专名未被保留"
RULE_SIMILARITY_LOW = "语义相似度低于阈值"
RULE_CLARIFY_INCONSISTENT = "追问与实质改写不一致"
RULE_UNCHANGED_LOST_NEGATION = "保留原问题时丢失否定词"
# 注:不设「LLM 决策与阈值推导不一致」违规项 —— 决策被阈值重新分级(如把
# use_rewritten 降为 dual_retrieval)属正常纠偏,不是安全问题,记进 detail 即可。
# 把它当违规会让「调阈值」这条路径把 dual 一路打回 use_original,阈值形同虚设。
RULE_CONFIDENCE_LOW = "置信度低于中间阈值"
RULE_SELF_CHECK_FAILED = "自检未全部通过"
RULE_NOT_CHANGED = "未做改写"


def _norm(s: str) -> str:
    """比对用归一化:去空白 + 小写。只用于实体/专名这类"是否还在"的判断。"""
    return "".join(str(s or "").split()).lower()


def _diff_pair(orig: list[str], new: list[str]) -> dict:
    """诊断用:返回两侧的词差异(原问题独有 / 改写后独有)。"""
    only_o, only_n = glossary.counter_diff(orig, new)
    return {"original": orig, "rewritten": new,
            "only_original": only_o, "only_rewritten": only_n}


def _entity_missing(entities: list[str], rewritten: str) -> list[str]:
    """返回 entities 里**没有**出现在 rewritten 中的项。

    用子串包含而非精确相等:LLM 常把「扫地机器人」归一化进更长的表述
    (如「科沃斯扫地机器人」),精确比会误杀。
    """
    target = _norm(rewritten)
    return [e for e in entities if _norm(e) and _norm(e) not in target]


def derive_decision(result: RewriteResult, multi_intent: bool) -> tuple[str, list[str]]:
    """按阈值从 (changed, self_check, confidence) 推导 decision。

    返回 (decision, 触发的原因标签列表)。标签用于日志聚合。
    """
    sc = result.self_check
    conf_high = float(config.get("conf_high"))
    conf_mid = float(config.get("conf_mid"))
    notes: list[str] = []

    if not result.changed or result.rewritten.strip() == result.original.strip():
        # changed=false 时无论置信度多高都不该走改写路径:没有改写就没什么可用的
        return DECISION_USE_ORIGINAL, [RULE_NOT_CHANGED]

    if not sc.all_same:
        notes.append(RULE_SELF_CHECK_FAILED)
        return DECISION_USE_ORIGINAL, notes

    if sc.confidence < conf_mid:
        notes.append(RULE_CONFIDENCE_LOW)
        return DECISION_USE_ORIGINAL, notes

    if sc.confidence >= conf_high:
        return DECISION_USE_REWRITTEN, notes

    # 中间区间:双路检索。多意图时双路无收益(两路拿回同一批文档),退回原问题。
    if multi_intent:
        notes.append(RULE_MULTI_INTENT_DUAL)
        return DECISION_USE_ORIGINAL, notes
    return DECISION_DUAL, notes


class HardChecker:
    """对 LLM 改写结果做确定性硬校验,并给出最终 decision。

    用法:
        checker = HardChecker()
        hc = checker.check(result, orig_emb=..., new_emb=...)
        if not hc.passed:
            ...  # 已经由 hc.final_decision 指示走 use_original

    嵌入向量由调用方传入(便于缓存复用 + 便于测试注入),本层不自己调 embedding。
    """

    def check(
        self,
        result: RewriteResult,
        orig_emb: list[float] | None = None,
        new_emb: list[float] | None = None,
    ) -> HardCheckResult:
        original = result.original
        rewritten = result.rewritten
        violations: list[str] = []
        detail: dict = {}

        multi_intent = glossary.looks_multi_intent(original)
        detail["multi_intent"] = multi_intent

        # ---- 0) ask_clarify 单独处理:追问合理则原样保留,不合理则降级 ----
        if result.decision == DECISION_ASK_CLARIFY:
            return self._check_clarify(result, multi_intent, detail)

        # ---- 1) 由阈值推导 decision(而非照抄 LLM) ----
        # 注意:推导结果与 LLM 的判断不一致时**不算违规** —— 阈值调高后把
        # use_rewritten 降级为 dual_retrieval 是正常的重新分级,改写本身仍然安全。
        # 只有下面 2~6 条「安全类」检查不通过才置 use_original。把「决策不一致」
        # 也记成违规会导致调阈值时把 dual 一路打到 use_original,阈值就白调了。
        derived, notes = derive_decision(result, multi_intent)
        detail["llm_decision"] = result.decision
        detail["derived_decision"] = derived
        detail["recalibrated"] = (result.decision != derived)
        final = derived

        # ---- 2) 否定词数量一一对应 ----
        neg_o = glossary.negations(original)
        neg_n = glossary.negations(rewritten)
        only_o, only_n = glossary.counter_diff(neg_o, neg_n)
        detail["negations"] = {"original": neg_o, "rewritten": neg_n}
        if only_o or only_n:
            violations.append(RULE_NEGATION_MISMATCH)
            detail["negations"]["missing_in_rewritten"] = only_o
            detail["negations"]["added_in_rewritten"] = only_n

        # ---- 3) 数字/时间完全一致 ----
        num_o = glossary.numbers_and_times(original)
        num_n = glossary.numbers_and_times(rewritten)
        n_only_o, n_only_n = glossary.counter_diff(num_o, num_n)
        detail["numbers"] = {"original": num_o, "rewritten": num_n}
        if n_only_o or n_only_n:
            violations.append(RULE_NUMBER_MISMATCH)
            detail["numbers"]["missing_in_rewritten"] = n_only_o
            detail["numbers"]["added_in_rewritten"] = n_only_n

        # ---- 4) 实体必须全部出现在改写后的问题里 ----
        # 注:断言的是**改写后**的问题包含该实体,不是与原问题相等 —— LLM 做
        # 术语归一化时可以在保留实体字面的前提下调整其余措辞。
        missing_entities = _entity_missing(result.constraints.entities, rewritten)
        if missing_entities:
            violations.append(RULE_ENTITY_DROPPED)
            detail["missing_entities"] = missing_entities
        # 原问题里命中的领域专名也必须还在(LLM 漏报 entities 时这一条兜住)
        dropped_domain = [t for t in glossary.domain_terms(original) if _norm(t) not in _norm(rewritten)]
        if dropped_domain:
            violations.append(RULE_DOMAIN_TERM_DROPPED)
            detail["dropped_domain_terms"] = dropped_domain

        # ---- 5) 原样返回时不得丢否定词 ----
        if _norm(rewritten) == _norm(original) and len(neg_n) < len(neg_o):
            violations.append(RULE_UNCHANGED_LOST_NEGATION)

        # ---- 5b) 条件词/比较词/量词:只做**诊断记录**,不做硬拦截 ----
        # 这三类不进 violations 是刻意的。它们极易出现良性同义替换:
        #   「所有机型」→「全部机型」、「比较好」→「更合适」(而「比」是「比较」的子串)、
        #   「只」「仅」互换。中文没有词边界,子串匹配无法区分「A 比 B 好」里的比较语义
        #   与「比较」这个副词。若把它们当违规,大量忠实改写会被误杀成 use_original,
        #   改写模块的收益会被吃掉大半。
        # 高风险的是**否定词**(丢一个「不」会让检索方向完全反转),那一条已经硬拦了。
        # 这里仍把差异记进日志:若某类词在 fallback 归因里长期异常,说明该收紧 prompt
        # 或扩充词表 —— 数据先留出来,阈值/规则怎么改由人工看数据决定。
        detail["soft_signals"] = {
            "conditions": _diff_pair(glossary.conditions(original), glossary.conditions(rewritten)),
            "comparatives": _diff_pair(glossary.comparatives(original), glossary.comparatives(rewritten)),
            "quantifiers": _diff_pair(glossary.quantifiers(original), glossary.quantifiers(rewritten)),
            "vague": glossary.is_vague(original),
        }

        # ---- 6) embedding 相似度闸门 ----
        similarity_skipped = False
        sim = None
        if orig_emb and new_emb:
            sim = glossary.cosine(orig_emb, new_emb)
            if sim < 0:
                # 维度不符等异常:跳过该检查,不因此判死(其余规则照跑)
                similarity_skipped = True
                sim = None
                logger.debug("query_rewrite.hard_checker: 向量不可用,跳过相似度检查")
            else:
                detail["similarity"] = round(sim, 4)
                if sim < float(config.get("sim_min")):
                    violations.append(RULE_SIMILARITY_LOW)
        else:
            similarity_skipped = True

        detail["reasons"] = notes
        # 最终决策 = derive_decision 的结果。违规项(否定词/数字/实体/相似度/原样返回
        # 丢否定词)在下面 2~6 条里已经通过 final 逻辑收敛为 use_original:
        # 一旦有 violations,final 必为 use_original,无需再单独覆盖一次。
        passed = not violations
        final = DECISION_USE_ORIGINAL if violations else derived
        hc = HardCheckResult(
            passed=passed,
            violations=violations,
            overridden=(final != result.decision),
            original_decision=result.decision,
            final_decision=final,
            multi_intent=multi_intent,
            similarity_skipped=similarity_skipped,
            detail=detail,
        )
        if not passed:
            logger.info(
                f"【改写硬校验】拦截 → {final}(原判 {result.decision})"
                f" 违规项:{'、'.join(violations)}"
            )
        elif final != result.decision:
            # 无违规但决策被阈值改写:属正常纠偏,记 info 便于观察阈值效果
            logger.info(f"【改写硬校验】决策纠偏 {result.decision} → {final}")
        return hc

    # ---------- ask_clarify 分支 ----------

    def _check_clarify(self, result: RewriteResult, multi_intent: bool, detail: dict) -> HardCheckResult:
        """校验 LLM 的 ask_clarify 是否合理。

        合理:改写实质上没动(原问题本身就缺信息,没什么可改的)。
        不合理:模型实质改写了问题(说明它已理解意图)却回头要追问 —— 答非所问,
        降级为 use_original(用原问题检索,至少不会答偏)。
        """
        plausible = glossary.clarify_is_plausible(
            result.original, result.rewritten, result.changed
        )
        detail["clarify_plausible"] = plausible
        detail["jaccard"] = round(glossary.jaccard(result.original, result.rewritten), 4)
        if plausible:
            return HardCheckResult(
                passed=True,
                violations=[],
                overridden=False,
                original_decision=result.decision,
                final_decision=DECISION_ASK_CLARIFY,
                multi_intent=multi_intent,
                similarity_skipped=True,
                detail=detail,
            )
        logger.info("【改写硬校验】追问不合理(已实质改写却要追问),降级 use_original")
        return HardCheckResult(
            passed=False,
            violations=[RULE_CLARIFY_INCONSISTENT],
            overridden=True,
            original_decision=result.decision,
            final_decision=DECISION_USE_ORIGINAL,
            multi_intent=multi_intent,
            similarity_skipped=True,
            detail=detail,
        )
