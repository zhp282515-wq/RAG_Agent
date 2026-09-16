"""路由器:按最终 decision 分支执行检索。

四条分支对应需求给定的四种行为,其中「原问题」永远是可用路径:

    use_rewritten  → 检索(改写后)
    dual_retrieval → 检索(原)∥检索(改写) 后合并去重
    use_original   → 检索(原)
    ask_clarify    → **不检索**,直接追问

检索器通过构造参数注入(默认走项目的 VectorStoreService + rerank),
这样单元测试可以塞一个假的 retriever,不碰 Milvus 也不耗 embedding。

双路并行用线程池:两路各自要跑一次 embedding + 一次 rerank,串行做墙钟翻倍。
并行后墙钟≈单路,代价是 rerank 调用仍是两次(dual 的固有成本,日志里标注)。
"""
from __future__ import annotations

import concurrent.futures as _futures
import time
from typing import Callable, Protocol

from utils.logger_tool import logger
from utils.query_rewrite.models import (
    DECISION_ASK_CLARIFY,
    DECISION_DUAL,
    DECISION_USE_REWRITTEN,
)
from utils.query_rewrite.rewriter import clarify_message


class Retriever(Protocol):
    """检索器协议:按 query 返回结构化命中列表 [{pk, text, score, ...}]。"""

    def __call__(self, query: str, *, top_k: int, rerank_n: int,
                 on_step: Callable | None = None,
                 raise_on_infra_error: bool = False) -> list[dict]:
        ...


def _default_retriever() -> Retriever:
    """项目默认检索器:Milvus 向量召回 + DashScope rerank(与旧工具同一条链路)。

    延迟 import:vector_store -> modelfactory -> settings_store 这条链在 import
    期会读配置/环境,放在函数内可避免「仅导入改写模块」时被牵连。
    """
    from utils.rag_settings import current_store

    def _retrieve(query: str, *, top_k: int, rerank_n: int,
                  on_step: Callable | None = None,
                  raise_on_infra_error: bool = False) -> list[dict]:
        from vector_store.vector_store import VectorStoreService
        return VectorStoreService(collection_name=current_store()).get_rerank_retriever(
            top_k=top_k, rerank_n=rerank_n,
            on_step=on_step, raise_on_infra_error=raise_on_infra_error,
        )(query)

    return _retrieve


def dedupe_hits(hit_lists: list[list[dict]]) -> list[dict]:
    """多路命中按 pk 合并去重,同一分片保留较高分,按分数降序返回。

    去重键优先用 pk(向量库主键,唯一);无 pk 的假检索器(测试)退到 text 前 80 字,
    避免同一段资料因两路召回的 score 不同而重复注入上下文、白占 token。
    """
    best: dict = {}
    for hits in hit_lists:
        for h in hits or []:
            key = h.get("pk")
            if key is None:
                key = str(h.get("text", ""))[:80]
            prev = best.get(key)
            if prev is None or float(h.get("score", 0) or 0) > float(prev.get("score", 0) or 0):
                best[key] = h
    return sorted(best.values(), key=lambda x: float(x.get("score", 0) or 0), reverse=True)


class Router:
    """按 decision 决定检索哪些查询串,并执行(或跳过)检索。"""

    def __init__(self, retriever: Retriever | None = None, max_workers: int = 2):
        # 延迟到真正检索时才建默认检索器:测试注入假检索器时完全不碰 Milvus
        self._retriever = retriever
        self._max_workers = max(1, int(max_workers))

    def _get_retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = _default_retriever()
        return self._retriever

    def route(
        self,
        decision: str,
        original: str,
        rewritten: str,
        *,
        top_k: int,
        rerank_n: int,
        on_step: Callable | None = None,
        reason: str = "",
    ) -> dict:
        """执行分支,返回 {decision, queries, hits, clarify, elapsed, dual}。

        ask_clarify 分支 hits 为空且 clarify 非空 —— 调用方据此直接向模型转述追问。
        """
        if decision == DECISION_ASK_CLARIFY:
            # 追问分支不检索:检索了也没法用(不知道该搜什么),只会白花 embedding 钱
            logger.info(f"【查询路由】ask_clarify,跳过检索:{original!r}")
            return {
                "decision": decision,
                "queries": [],
                "hits": [],
                "clarify": clarify_message(original, reason),
                "elapsed": 0.0,
                "dual": False,
            }

        if decision == DECISION_USE_REWRITTEN:
            queries = [rewritten]
            dual = False
        elif decision == DECISION_DUAL:
            # 原问题与改写后的问题都要检索;两者相同时(理论上 changed=false 不会走到
            # 这里,但阈值边界可能)去重成一路,避免同一个 query 搜两次
            queries = [original] if rewritten.strip() == original.strip() else [original, rewritten]
            dual = len(queries) > 1
        else:  # DECISION_USE_ORIGINAL 及其它未知值:原问题兜底
            queries = [original]
            dual = False

        logger.info(
            f"【查询路由】decision={decision} 检索串={queries} "
            f"(原问题={original!r})"
        )
        if on_step is not None:
            _safe_step(on_step, "路由决策", {
                "decision": decision,
                "检索串": " ‖ ".join(queries),
            })

        t0 = time.perf_counter()
        if dual:
            hit_lists = self._retrieve_dual(queries, top_k, rerank_n, on_step)
        else:
            hit_lists = [self._retrieve_one(queries[0], top_k, rerank_n, on_step)]
        elapsed = time.perf_counter() - t0

        hits = dedupe_hits(hit_lists) if dual else (hit_lists[0] or [])
        if dual:
            logger.info(
                f"【查询路由】双路合并后 {len(hits)} 条(合并前 "
                f"{[len(h or []) for h in hit_lists]}),耗时 {elapsed:.2f}s"
            )
            if on_step is not None:
                _safe_step(on_step, "双路合并", {
                    "各路条数": [len(h or []) for h in hit_lists],
                    "去重后": len(hits),
                })

        return {
            "decision": decision,
            "queries": queries,
            "hits": hits,
            "clarify": "",
            "elapsed": elapsed,
            "dual": dual,
        }

    # ---------- 单路 / 双路 ----------

    def _retrieve_one(self, query: str, top_k: int, rerank_n: int, on_step) -> list[dict]:
        """检索一路。基础设施故障上抛(由调用方转为重试),真「无结果」返回 []。"""
        t0 = time.perf_counter()
        hits = self._get_retriever()(
            query, top_k=top_k, rerank_n=rerank_n,
            on_step=on_step, raise_on_infra_error=True,
        )
        logger.debug(f"【查询路由】检索 {query!r} → {len(hits or [])} 条({time.perf_counter()-t0:.2f}s)")
        return list(hits or [])

    def _retrieve_dual(self, queries: list[str], top_k: int, rerank_n: int, on_step) -> list[list[dict]]:
        """并行检索多路,返回与 queries 同序的结果列表。

        某一路抛异常时:该路结果记为空列表,不让整次检索失败 —— 另一路的结果
        仍然有用,总比因为一路网络抖动就整体报错要好。
        """
        results: dict[int, list[dict]] = {}
        errors: dict[int, str] = {}

        def _work(i: int, q: str):
            try:
                return i, self._retrieve_one(q, top_k, rerank_n, on_step), None
            except Exception as e:  # noqa: BLE001 - 单路失败要降级而非中断
                return i, [], e

        with _futures.ThreadPoolExecutor(max_workers=min(self._max_workers, len(queries))) as ex:
            for i, hits, err in ex.map(lambda p: _work(*p), list(enumerate(queries))):
                results[i] = hits
                if err is not None:
                    errors[i] = str(err)

        if errors:
            # 一路失败不算全败,但必须记日志:否则「结果变少」会被误当成"没搜到"
            logger.warning(f"【查询路由】双路检索有路径失败(已降级用其余结果):{errors}")
        if len(errors) == len(queries):
            # 全失败才上抛,交给工具层的重试机制处理
            raise RuntimeError(f"双路检索全部失败:{errors}")

        return [results.get(i, []) for i in range(len(queries))]


def _safe_step(on_step: Callable, name: str, params: dict) -> None:
    """上报子步骤;回调异常绝不影响检索。"""
    try:
        on_step(name, params)
    except Exception:
        pass


__all__ = ["Router", "dedupe_hits", "Retriever", "DECISION_ASK_CLARIFY"]
