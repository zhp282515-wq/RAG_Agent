"""用户问题改写 + 一致性自检 + 分级路由。

闭环:改写(1 次轻量 LLM 调用)→ 规则硬校验(零 token)→ 分级路由 → 检索。
原问题永远是兜底路径;规则判定优先于模型自检;任何环节失败都退化为用原问题检索。

对外主入口:
    from utils.query_rewrite import plan_query
    plan = plan_query(原始问题, history=历史, top_k=20, rerank_n=5)
    plan.hits / plan.decision / plan.clarify

详见 README.md。
"""
from utils.query_rewrite.models import (
    DECISION_ASK_CLARIFY,
    DECISION_DUAL,
    DECISION_USE_ORIGINAL,
    DECISION_USE_REWRITTEN,
)
from utils.query_rewrite.pipeline import (
    QueryPlan,
    QueryRewritePipeline,
    cache_size,
    clear_cache,
    get_pipeline,
    plan_query,
)
from utils.query_rewrite.rewriter import Rewriter

__all__ = [
    "plan_query",
    "QueryPlan",
    "QueryRewritePipeline",
    "Rewriter",
    "get_pipeline",
    "clear_cache",
    "cache_size",
    "DECISION_USE_REWRITTEN",
    "DECISION_DUAL",
    "DECISION_USE_ORIGINAL",
    "DECISION_ASK_CLARIFY",
]
