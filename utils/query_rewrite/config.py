"""改写模块的配置读取层。

配置来源与优先级(高 → 低):
  1. 进程环境变量 REWRITE_<大写键名>(便于调试/评测脚本临时压阈值,不用改文件)
  2. config/rag.yml 的 rewrite 段
  3. 本文件内置 _DEFAULTS

**阈值不写死**是需求硬要求:所有数值都能在这里或 rag.yml 里调。缺键、类型不对、
yml 整个读不到,都回退内置默认并记一条 warning —— 配置问题不能拖垮检索。

不做运行时热加载:rag.yml 是静态兜底(与 model.yml / vector_store.yml 一致),
改完重启生效。反过来,「系统配置」页那个总开关走 MySQL(见 resolve_enabled)。
"""
from __future__ import annotations

import os

from utils.config_tool import rag_conf
from utils.logger_tool import logger

# 内置默认值与 config/rag.yml 的注释保持同一套语义,改一处要同步另一处
_DEFAULTS: dict = {
    # 开关
    "enabled": True,
    "skip_when_no_rewrite_needed": True,
    # 模型
    "model": "",
    "temperature": 0.0,
    "disable_thinking": True,
    "base_url": "",
    "timeout": 15.0,
    "max_tokens": 1500,
    # 阈值
    "conf_high": 0.9,
    "conf_mid": 0.75,
    "sim_min": 0.75,
    "jaccard_clarify": 0.6,
    # 输入预算
    "history_turns": 3,
    "history_char_budget": 1200,
    # 缓存
    "cache_enabled": True,
    "cache_size": 512,
    # 日志
    "log_enabled": True,
    # 词典
    "negations": [],
    "conditions": [],
    "comparatives": [],
    "quantifiers": [],
    "vague_markers": [],
    "time_patterns": [],
    "domain_terms": [],
}


def _section() -> dict:
    """取 rag.yml 的 rewrite 段;文件空/无该段/不是 dict 一律给空 dict。"""
    if not isinstance(rag_conf, dict):
        return {}
    sec = rag_conf.get("rewrite")
    return sec if isinstance(sec, dict) else {}


def _env_override(key: str):
    """环境变量覆盖:REWRITE_SIM_MIN=0.9 这类,取不到返回 None。"""
    raw = os.getenv(f"REWRITE_{key.upper()}")
    if raw is None or raw.strip() == "":
        return None
    return raw


def get(key: str):
    """读一项配置(env > rag.yml > 内置默认)。返回原始类型,由 get_float/get_int 收口。"""
    if key not in _DEFAULTS:
        # 打错键名是最常见的配置事故,显式报出来而不是静默 None
        logger.warning(f"query_rewrite.config: 未知配置键 {key!r},已按 None 处理")
        return None
    default = _DEFAULTS[key]
    raw = _env_override(key)
    if raw is None:
        raw = _section().get(key, default)
    if raw is None:
        return default
    # 类型按默认值的类型强制:yml 里把 0.9 写成 "0.9" 也能用
    try:
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, list):
            if isinstance(raw, (list, tuple)):
                return [str(x) for x in raw]
            logger.warning(f"query_rewrite.config: {key} 期望列表,实得 {type(raw).__name__},用默认")
            return list(default)
        return raw if isinstance(raw, str) else str(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"query_rewrite.config: {key} 取值 {raw!r} 无法转为 {type(default).__name__}({e}),用默认")
        return default


def get_float(key: str) -> float:
    return float(get(key))


def get_int(key: str) -> int:
    return int(get(key))


def get_bool(key: str) -> bool:
    return bool(get(key))


def get_list(key: str) -> list[str]:
    v = get(key)
    return list(v) if isinstance(v, list) else []


def resolve_enabled() -> bool:
    """总开关:rag.yml(或 env)与「系统配置」页的 DB 开关,**任一为 false 即关闭**。

    只有启用检索工具时才被调用,这里的 DB 往返(settings_store 现读)与
    一次向量检索相比可忽略。settings_store 不可用时按「未配置」处理,即只看 yml。
    """
    if not get_bool("enabled"):
        return False
    try:
        from utils.settings_store import get_json
        return bool(get_json("rag.rewrite_enabled", True))
    except Exception:
        # 与 agent_tools._setting 同一套容错:DB 不可用不该让检索链路断掉
        return True


def rewrite_model_name() -> str:
    """改写用模型名:rag.yml 填了就用,否则复用 model.yml 的 summary_model(轻量档)。

    **不回退对话主模型** —— 把大模型用在改写上正是需求明确要避免的。
    """
    name = (get("model") or "").strip()
    if name:
        return name
    from utils.config_tool import model_conf
    return (model_conf.get("summary_model") or "").split(":", 1)[-1] or "qwen3.7-flash"
