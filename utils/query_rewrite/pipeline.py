"""闭环编排:改写 → 硬校验 → 路由检索,附带决策缓存与评估日志。

这是模块对外的**主入口**(工具层只调 `plan_query` 一个函数):

    plan = plan_query("那它续航多久", history=[...])
    plan.decision   # 最终决策(已被规则层复核)
    plan.hits       # 检索结果(ask_clarify 时为空)
    plan.clarify    # 追问文案(仅 ask_clarify 分支非空)

降级是一个贯穿全链路的主题,每一层失败都退到「用原问题检索」:
    改写模型不可用/超时/JSON 坏 → use_original
    embedding 不可用           → 跳过相似度规则(其余规则照跑),不判死
    双路其中一路检索失败        → 用另一路的结果
    全局开关关闭                → 完全不调 LLM 也不调 embedding,直通原问题
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from utils.logger_tool import fmt_duration, logger
from utils.query_rewrite import config, glossary
from utils.query_rewrite.hard_checker import HardChecker
from utils.query_rewrite.models import (
    DECISION_ASK_CLARIFY,
    DECISION_DUAL,
    DECISION_USE_ORIGINAL,
    DECISION_USE_REWRITTEN,
    HardCheckResult,
    RewriteResult,
)
from utils.query_rewrite.rewriter import Rewriter, clarify_message
from utils.query_rewrite.router import Router

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs"
)

# 工作流面板上的两个平级阶段名(前端按这两个字面量做语义映射,改名字要同步改前端)
PHASE_REWRITE = "问题改写"
PHASE_RETRIEVE = "知识库检索"


def _emit(phase_emit, action: str, name: str, params: dict | None = None,
          duration: float | None = None, tokens: int | None = None) -> None:
    """发一条阶段事件;未注入回调或回调抛异常都静默忽略(工作流可视化不能影响问答)。"""
    if phase_emit is None:
        return
    try:
        phase_emit(action, name, params or {}, duration, tokens)
    except Exception as e:
        logger.debug(f"query_rewrite.pipeline: 阶段事件上报失败(忽略):{e}")


def usage_take(kind: str) -> int:
    """取走自上次调用以来某类消耗的累计值(供阶段归因),无则 0。

    延迟 import:agent_middleware 会间接引用改写模块,顶层 import 成环。
    非工具调用期间 usage_take_since 返回 0,故评测/自测场景不受影响。
    """
    try:
        from ReAct.middleware.agent_middleware import usage_take_since
        return int(usage_take_since(kind) or 0)
    except Exception:
        return 0


def _planned_queries(plan: "QueryPlan") -> list[str]:
    """按最终决策预先算出将要用到的检索串(用于在检索开始前就如实展示检索词)。

    与 Router.route 的分支判定保持一致:改写可信就用改写串、双路则两串都检索、
    其余(含回退)都用原问题。
    """
    if plan.decision == DECISION_USE_REWRITTEN and plan.rewritten.strip():
        return [plan.rewritten]
    if plan.decision == DECISION_DUAL:
        rw = plan.rewritten.strip()
        if rw and rw != plan.original.strip():
            return [plan.original, rw]
    return [plan.original]


def _rewrite_phase_params(plan: "QueryPlan") -> dict:
    """「问题改写」阶段的展示参数:只放用户真正关心的那几项。

    刻意不放 changes / preserved_constraints 这类完整字段 —— 面板是概览,塞太多会
    淹没关键信息;完整数据在 logs/rewrite_*.jsonl 里,需要时查日志。
    """
    rr = plan.rewrite_result
    hc = plan.hard_check
    params: dict = {"原问题": plan.original}
    if rr is not None:
        if plan.rewritten and plan.rewritten != plan.original:
            # 用「原问题 → 改写」的箭头形式表达,比两个独立字段省横向空间也更直观
            params["改写"] = plan.rewritten
        params["决策"] = plan.decision
        if not rr.rewriter_failed:
            params["置信度"] = f"{rr.self_check.confidence:.2f}"
    if hc is not None:
        sim = hc.detail.get("similarity")
        if sim is not None:
            params["相似度"] = f"{sim:.2f}"
        if hc.violations:
            params["违规项"] = "、".join(hc.violations)
    if plan.fell_back:
        params["回退"] = plan.fallback_reason or "已回退原问题"
    if plan.cached:
        params["缓存"] = "命中"
    return params


@dataclass
class QueryPlan:
    """一次查询前处理的完整结果,供工具层直接使用/落日志。"""

    original: str
    rewritten: str
    decision: str
    hits: list[dict] = field(default_factory=list)
    clarify: str = ""
    queries: list[str] = field(default_factory=list)
    rewrite_result: RewriteResult | None = None
    hard_check: HardCheckResult | None = None
    # 最终 decision 是否与 LLM 的判断不同(即发生过回退或纠正)
    fell_back: bool = False
    fallback_reason: str = ""
    cached: bool = False
    disabled: bool = False
    # 规则判定「无需改写」而跳过了 LLM 调用(见 glossary.looks_no_rewrite_needed)
    skipped_rewrite: bool = False
    rewrite_elapsed: float = 0.0
    retrieve_elapsed: float = 0.0
    error: str = ""

    @property
    def search_query(self) -> str:
        """实际用于检索的主查询串(单路时就是它;双路时取原问题)。"""
        return self.queries[0] if self.queries else self.original


# ---------------- 决策缓存 ----------------
# 进程内 LRU(与项目既有 _agent_cache / vlm_tool._cache 风格一致,无 TTL)。
# 缓存 key 是用户原问题,**不做持久化**:落库涉及隐私且收益有限。
# 命中时复用已算好的原问题 embedding,省掉一次 DashScope 调用 —— 这是缓存的主要收益。
_cache_lock = threading.Lock()
_cache: "OrderedDict[str, dict]" = OrderedDict()


def _cache_key(query: str) -> str:
    """归一化缓存键:去空白 + 小写(「续航 多久」与「续航多久」视为同一个问题)。"""
    return "".join((query or "").split()).lower()


def _cache_get(key: str) -> dict | None:
    if not config.get_bool("cache_enabled"):
        return None
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        _cache.move_to_end(key)
        return hit


def _cache_put(key: str, value: dict) -> None:
    if not config.get_bool("cache_enabled"):
        return
    size = int(config.get("cache_size") or 512)
    if size <= 0:
        return
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > size:
            _cache.popitem(last=False)


def clear_cache() -> None:
    """清空决策缓存(改词典/调阈值后调用,或测试用)。"""
    with _cache_lock:
        _cache.clear()


def cache_size() -> int:
    with _cache_lock:
        return len(_cache)


# ---------------- 评估日志 ----------------


def _log_record(plan: QueryPlan, extra: dict | None = None) -> None:
    """每次调用追加一行 JSON 到 logs/rewrite_YYYY-MM-DD.jsonl。

    用于离线评估与阈值调优:原问题、改写、各检测分数、最终 decision、是否回退。
    独立于 logger 的 .log 文件,便于用 pandas 直接读。
    """
    if not config.get_bool("log_enabled"):
        return
    try:
        rr = plan.rewrite_result
        hc = plan.hard_check
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original": plan.original,
            "rewritten": plan.rewritten,
            "changed": bool(rr.changed) if rr else False,
            "changes": list(rr.changes) if rr else [],
            "decision": plan.decision,
            "llm_decision": hc.original_decision if hc else "",
            "derived_decision": hc.final_decision if hc else "",
            "fell_back": plan.fell_back,
            "fallback_reason": plan.fallback_reason,
            "violations": list(hc.violations) if hc else [],
            "scores": {
                "confidence": rr.self_check.confidence if rr else None,
                "intent_same": rr.self_check.intent_same if rr else None,
                "entities_same": rr.self_check.entities_same if rr else None,
                "constraints_same": rr.self_check.constraints_same if rr else None,
                "similarity": (hc.detail.get("similarity") if hc else None),
                "similarity_skipped": hc.similarity_skipped if hc else None,
                "jaccard": (hc.detail.get("jaccard") if hc else None),
            },
            "multi_intent": hc.multi_intent if hc else None,
            # 条件词/比较词/量词的差异(不拦拦截,仅供离线分析:哪类词被改写动得最多)
            "soft_signals": hc.detail.get("soft_signals") if hc else None,
            "cached": plan.cached,
            "disabled": plan.disabled,
            "skipped_rewrite": plan.skipped_rewrite,
            "queries": list(plan.queries),
            "hit_count": len(plan.hits),
            "rewrite_elapsed": round(plan.rewrite_elapsed, 4),
            "retrieve_elapsed": round(plan.retrieve_elapsed, 4),
            "rewriter_failed": bool(rr.rewriter_failed) if rr else False,
            # 改写失败的具体原因(无 key / 超时 / JSON 坏 / 撞 max_tokens 等)。
            # 与下面的 error 分开:error 是**检索**故障,rewrite_error 是**改写**故障;
            # 早前两者混用同一个字段,导致排查改写问题时误看检索错误(踩过)。
            "rewrite_error": (rr.error if rr and rr.rewriter_failed else ""),
            "error": plan.error,
        }
        if extra:
            rec.update(extra)
        os.makedirs(_LOG_DIR, exist_ok=True)
        path = os.path.join(_LOG_DIR, f"rewrite_{datetime.now():%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # 评估日志写失败绝不能影响问答本身
        logger.debug(f"query_rewrite.pipeline: 评估日志写入失败(忽略):{e}")


# ---------------- 主入口 ----------------


class QueryRewritePipeline:
    """闭环编排器。可注入 rewriter / router 以便测试。"""

    def __init__(self, rewriter: Rewriter | None = None, router: Router | None = None,
                 checker: HardChecker | None = None,
                 embedder: Callable[[str], list[float]] | None = None):
        self._rewriter = rewriter
        self._router = router
        self._checker = checker or HardChecker()
        self._embedder = embedder

    def _get_rewriter(self) -> Rewriter:
        if self._rewriter is None:
            self._rewriter = Rewriter()
        return self._rewriter

    def _get_router(self) -> Router:
        if self._router is None:
            self._router = Router()
        return self._router

    def _embed(self, text: str) -> list[float] | None:
        """取原问题向量;失败返回 None(调用方据此跳过相似度检查,不判死)。"""
        try:
            fn = self._embedder
            if fn is None:
                from model.modelfactory import emb_model
                fn = emb_model.embed_query
            return fn(text)
        except Exception as e:
            logger.warning(f"query_rewrite.pipeline: 原问题向量化失败,将跳过相似度校验:{e}")
            return None

    def run(
        self,
        query: str,
        *,
        history=None,
        top_k: int,
        rerank_n: int,
        on_step: Callable | None = None,
        phase_emit: "Callable | None" = None,
    ) -> QueryPlan:
        """跑完整闭环。top_k / rerank_n 由调用方(工具层)现读设置后传入。

        phase_emit: 可选的阶段事件回调,签名 (action, name, params, duration, tokens)。
        用来把「问题改写」与「知识库检索」上报成**两个各自计时的平级工作流阶段**,
        让前端能看出等待时间分别花在哪一段(而不是笼统地都算进"检索")。
        action ∈ {"start", "end"};未注入时全部为空操作。
        """
        original = (query or "").strip()
        plan = QueryPlan(original=original, rewritten=original, decision=DECISION_USE_ORIGINAL)

        # ---- 开关:关闭则零 LLM、零 embedding,直通原问题 ----
        if not config.resolve_enabled():
            plan.disabled = True
            plan.fallback_reason = "改写模块已关闭"
            plan.queries = [original]
            _emit(phase_emit, "start", PHASE_RETRIEVE, {"原问题": original, "说明": "改写已关闭"})
            plan.retrieve_elapsed = self._retrieve_into(
                plan, top_k, rerank_n, on_step, reason="改写已关闭"
            )
            _emit(phase_emit, "end", PHASE_RETRIEVE,
                  {"命中": len(plan.hits), "原问题": original},
                  duration=plan.retrieve_elapsed)
            _log_record(plan)
            return plan

        if not original:
            plan.fallback_reason = "空问题"
            plan.queries = [original]
            _log_record(plan)
            return plan

        cache_key = _cache_key(original)
        cached = _cache_get(cache_key)

        # ---- 第 0 步:规则快速直通(省掉整次 LLM 调用)----
        # 依据实测:真实调用里多数最终都落在 use_original(改写成果未被采用),
        # 白等十几秒。对「本身就是规范检索句」的问题,规则就能判定无需改写。
        # 放在缓存之后:缓存命中更省(连 embedding 都免了)。
        t0 = time.perf_counter()
        if (cached is None and config.get_bool("skip_when_no_rewrite_needed")
                and glossary.looks_no_rewrite_needed(original)):
            plan.decision = DECISION_USE_ORIGINAL
            plan.fallback_reason = "规则判定无需改写(已是规范检索句)"
            plan.skipped_rewrite = True
            plan.queries = [original]
            plan.rewrite_elapsed = time.perf_counter() - t0
            logger.info(f"【查询改写】规则直通,跳过改写模型:{original!r}")
            # start/end 必须成对:只发 end 的话前端虽能容错渲染,但拿不到配对的 ph id,
            # 阶段归属会退化成"按名字猜"(见 useWorkflowStages 的兼容分支)。
            _emit(phase_emit, "start", PHASE_REWRITE, {"原问题": original})
            _emit(phase_emit, "end", PHASE_REWRITE,
                  {"原问题": original, "决策": plan.decision,
                   "说明": "规则判定无需改写,已跳过模型"},
                  duration=plan.rewrite_elapsed)
            _emit(phase_emit, "start", PHASE_RETRIEVE, {"检索词": original})
            plan.retrieve_elapsed = self._retrieve_into(
                plan, top_k, rerank_n, on_step, reason=plan.fallback_reason)
            _emit(phase_emit, "end", PHASE_RETRIEVE,
                  {"命中": len(plan.hits), "检索词": original},
                  duration=plan.retrieve_elapsed,
                  tokens=usage_take("embedding") + usage_take("rerank"))
            _log_record(plan)
            return plan

        # ---- 第 1 步:改写(单次 LLM 调用)----
        # 阶段在这里开、在第 2 步之后闭:改写与硬校验同属「问题改写」这一段,
        # 两者耗时合起来才是用户真正为"改写"等待的时间。
        _emit(phase_emit, "start", PHASE_REWRITE, {"原问题": original})
        t0 = time.perf_counter()
        if cached is not None:
            rr: RewriteResult = cached["rewrite_result"]
            plan.cached = True
            logger.debug(f"【查询改写】缓存命中:{original!r} → decision={rr.decision}")
        else:
            rr = self._get_rewriter().rewrite(original, history=history)
            plan.rewrite_elapsed = time.perf_counter() - t0

        plan.rewrite_result = rr
        plan.rewritten = rr.rewritten

        # ---- 第 2 步:硬校验(规则优先,不耗 token)----
        orig_emb = cached.get("orig_emb") if cached else None
        if orig_emb is None:
            orig_emb = self._embed(original)
        new_emb = None
        # 只有改写确实动了才需要算第二个向量 —— 否则纯属浪费一次 embedding 调用
        if rr.rewritten.strip() and rr.rewritten.strip() != original:
            new_emb = cached.get("new_emb") if cached else None
            if new_emb is None:
                new_emb = self._embed(rr.rewritten)

        hc = self._checker.check(rr, orig_emb=orig_emb, new_emb=new_emb)
        plan.hard_check = hc
        plan.decision = hc.final_decision
        plan.fell_back = (hc.final_decision != rr.decision)
        if plan.fell_back:
            plan.fallback_reason = (
                "、".join(hc.violations) if hc.violations else f"{rr.decision} → {hc.final_decision}"
            )
        if rr.rewriter_failed:
            plan.fallback_reason = plan.fallback_reason or f"改写不可用:{rr.error}"

        if cached is None:
            _cache_put(cache_key, {
                "rewrite_result": rr,
                "hard_check": hc,
                "orig_emb": orig_emb,
                "new_emb": new_emb,
            })

        # 改写阶段收尾:把用户最关心的几项摊在 params 里(前端逐项渲染)
        _emit(phase_emit, "end", PHASE_REWRITE, _rewrite_phase_params(plan),
              duration=time.perf_counter() - t0,
              tokens=usage_take("summary"))

        # ---- 第 3 步:路由检索 ----
        if plan.decision == DECISION_ASK_CLARIFY:
            plan.clarify = clarify_message(original, rr.reason)
            plan.queries = []
            logger.info(f"【查询路由】追问分支,不检索:{original!r}")
            # 追问不检索:发一个零耗时的说明阶段,而不是让面板停在"检索中"等下去
            _emit(phase_emit, "start", PHASE_RETRIEVE, {"说明": "需澄清,不检索"})
            _emit(phase_emit, "end", PHASE_RETRIEVE,
                  {"命中": 0, "说明": "需澄清,已跳过检索"}, duration=0.0)
        else:
            # 检索阶段的「检索词」要按最终决策预先算出来,不能等 _retrieve_into 之后
            # 再读 plan.queries —— 那时阶段已经开始了,start 的 params 会显示成原问题,
            # 与 end 显示的改写后检索词自相矛盾。
            _emit(phase_emit, "start", PHASE_RETRIEVE,
                  {"检索词": " ‖ ".join(_planned_queries(plan))})
            plan.retrieve_elapsed = self._retrieve_into(
                plan, top_k, rerank_n, on_step,
                reason=plan.fallback_reason,
            )
            _emit(phase_emit, "end", PHASE_RETRIEVE,
                  {"命中": len(plan.hits), "检索词": " ‖ ".join(plan.queries)},
                  duration=plan.retrieve_elapsed,
                  tokens=usage_take("embedding") + usage_take("rerank"))

        _log_record(plan)
        return plan

    def _retrieve_into(self, plan: QueryPlan, top_k: int, rerank_n: int,
                       on_step: Callable | None, reason: str = "") -> float:
        """执行路由检索并把结果写回 plan;返回耗时。

        检索失败(基础设施故障)时清空 hits 并记录 error,由工具层决定是否重试 ——
        本层不抛出,避免「改写成功但检索抖动」把整轮问答打断。
        """
        t0 = time.perf_counter()
        try:
            out = self._get_router().route(
                plan.decision, plan.original, plan.rewritten,
                top_k=top_k, rerank_n=rerank_n, on_step=on_step, reason=reason,
            )
            plan.hits = out["hits"]
            plan.queries = out["queries"]
            if out.get("clarify"):
                plan.clarify = out["clarify"]
        except Exception as e:
            plan.hits = []
            plan.error = str(e)
            logger.error(f"query_rewrite.pipeline: 检索失败:{e}")
        return time.perf_counter() - t0


# 模块级单例:缓存与改写模型都挂在实例上,复用同一个实例才有意义
_default_pipeline: QueryRewritePipeline | None = None


def get_pipeline() -> QueryRewritePipeline:
    global _default_pipeline
    if _default_pipeline is None:
        _default_pipeline = QueryRewritePipeline()
    return _default_pipeline


def plan_query(query: str, *, history=None, top_k: int, rerank_n: int,
               on_step: Callable | None = None,
               phase_emit: "Callable | None" = None) -> QueryPlan:
    """对外主入口:跑闭环,返回 QueryPlan。

    Args:
        query: 用户原始问题(**必须传原文**,不要传已经改写过的串)
        history: 可选对话历史,仅用于指代消解(只取最近若干轮)
        top_k / rerank_n: 检索参数,由调用方现读设置后传入(保留「下次提问即生效」)
        on_step: 可选子步骤回调(工具层用于向前端上报工作流细化步骤)
        phase_emit: 可选阶段回调(工具层用于把「问题改写」「知识库检索」上报成
                    两个各自计时的平级工作流阶段)
    """
    return get_pipeline().run(
        query, history=history, top_k=top_k, rerank_n=rerank_n,
        on_step=on_step, phase_emit=phase_emit,
    )


if __name__ == "__main__":
    # 自测:真实调用(会消耗改写模型 + embedding 额度)
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "那它续航多久"
    hist = [("user", "扫地机器人续航一般多久"), ("assistant", "主流机型约 120 分钟。")]
    t0 = time.perf_counter()
    p = plan_query(q, history=hist, top_k=20, rerank_n=5)
    print(f"原问题  : {p.original}")
    print(f"改写后  : {p.rewritten}")
    print(f"决策    : {p.decision}(LLM 原判 {p.hard_check.original_decision if p.hard_check else '-'})")
    print(f"是否回退: {p.fell_back} {p.fallback_reason}")
    print(f"检索串  : {p.queries}")
    print(f"命中    : {len(p.hits)} 条")
    if p.hard_check:
        print(f"违规项  : {p.hard_check.violations}")
        print(f"检测分数: {p.hard_check.detail}")
    if p.clarify:
        print(f"追问    : {p.clarify}")
    print(f"总耗时  : {fmt_duration(time.perf_counter() - t0)}")
