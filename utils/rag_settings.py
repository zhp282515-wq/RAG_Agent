"""检索工具注册表快捷读写(跨模块共享,避免循环 import)。

为什么单独一个模块:改写闭环(utils/query_rewrite)需要用「当前向量库」和检索三件套,
而这些逻辑原来在 ReAct/tools/agent_tools.py 里;若 query_rewrite 直接 import
agent_tools,而 agent_tools 又要 import query_rewrite 跑闭环,就成环了。
把「读 app_settings」这一小块下沉到这里,两边都 import 本模块,依赖是单向的。

默认值刻意与 agent_tools 的历史常量一致(top_k 20 / rerank_n 5 / 高 0.85 / 达标 0.60)。
"""
from __future__ import annotations

_SCORE_HIGH = 0.85
_SCORE_MIN = 0.60
_TOP_K = 20
_RERANK_N = 5


def setting(name: str, default):
    """从 MySQL app_settings 现读一项全局设置;失败/未初始化回退 default。"""
    try:
        from utils.settings_store import get_json
        return get_json(name, default)
    except Exception:
        return default


def score_high() -> float:
    return float(setting("retrieval.score_high", _SCORE_HIGH))


def score_min() -> float:
    return float(setting("retrieval.score_min", _SCORE_MIN))


def top_k() -> int:
    return int(setting("retrieval.top_k", _TOP_K))


def rerank_n() -> int:
    return int(setting("retrieval.rerank_n", _RERANK_N))


def current_store() -> str | None:
    """当前向量库名(系统配置页可切);读失败回退 None,由服务层取 yml 默认。

    None 而非空串:VectorStoreService 对空值会回退默认库,交给它统一处理。
    """
    v = str(setting("rag.current_store", "") or "").strip()
    return v or None
