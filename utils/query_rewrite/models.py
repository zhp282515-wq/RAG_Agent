"""改写闭环的数据载体。

刻意用 dataclass 而非 pydantic 模型:LLM 回来的 JSON 可能缺字段/类型不符,
校验交给 `rewriter._parse_result` 的手写逻辑(能对缺字段逐个回退),而不是
让 pydantic 在异常里一次性报完。这里的 dataclass 只负责「结果长什么样」。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# 四个合法决策。HardChecker / Router 都按这组常量分支,避免散落字符串字面量。
DECISION_USE_REWRITTEN = "use_rewritten"
DECISION_DUAL = "dual_retrieval"
DECISION_USE_ORIGINAL = "use_original"
DECISION_ASK_CLARIFY = "ask_clarify"
VALID_DECISIONS = frozenset({
    DECISION_USE_REWRITTEN,
    DECISION_DUAL,
    DECISION_USE_ORIGINAL,
    DECISION_ASK_CLARIFY,
})


@dataclass
class PreservedConstraints:
    """改写铁律要求原样保留的四类约束。LLM 在 JSON 里回报,规则层做交叉校验。"""

    entities: list[str] = field(default_factory=list)
    negations: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw) -> "PreservedConstraints":
        """从 LLM 的 JSON 片段构造;非 dict / 值非列表一律按空处理(不抛)。"""
        if not isinstance(raw, dict):
            return cls()

        def _str_list(key: str) -> list[str]:
            v = raw.get(key)
            if not isinstance(v, (list, tuple)):
                return []
            return [str(x).strip() for x in v if str(x).strip()]

        return cls(
            entities=_str_list("entities"),
            negations=_str_list("negations"),
            conditions=_str_list("conditions"),
            numbers=_str_list("numbers"),
        )


@dataclass
class SelfCheck:
    """LLM 的自检结果(三个布尔 + 一个置信度)。"""

    intent_same: bool = False
    entities_same: bool = False
    constraints_same: bool = False
    confidence: float = 0.0

    @property
    def all_same(self) -> bool:
        return self.intent_same and self.entities_same and self.constraints_same

    @classmethod
    def from_dict(cls, raw) -> "SelfCheck":
        if not isinstance(raw, dict):
            return cls()

        def _bool(key: str) -> bool:
            # 兼容 LLM 偶发返回字符串 "true"/"false"
            v = raw.get(key)
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in ("true", "yes", "1", "是")
            return bool(v)

        try:
            conf = float(raw.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        # 越界值夹紧,避免阈值比较被 1.5 / -1 这类值带偏
        conf = min(1.0, max(0.0, conf))
        return cls(
            intent_same=_bool("intent_same"),
            entities_same=_bool("entities_same"),
            constraints_same=_bool("constraints_same"),
            confidence=conf,
        )


@dataclass
class RewriteResult:
    """Rewriter 的产出:LLM 的原始判断 + 解析是否成功。"""

    original: str
    rewritten: str
    changed: bool = False
    changes: list[str] = field(default_factory=list)
    constraints: PreservedConstraints = field(default_factory=PreservedConstraints)
    self_check: SelfCheck = field(default_factory=SelfCheck)
    decision: str = DECISION_USE_ORIGINAL
    reason: str = ""
    # True 表示这次没有拿到可用的 LLM 输出(无 key / 超时 / JSON 解析失败),
    # 此时 decision 一律是 use_original —— 上层据此区分「模型判的回退」与「不可用回退」。
    rewriter_failed: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        """摊平成可直接 json.dumps 的 dict(嵌套 dataclass 由 asdict 递归展开)。"""
        return asdict(self)

    @classmethod
    def fallback(cls, original: str, error: str) -> "RewriteResult":
        """构造降级结果:用原问题、use_original、标记失败原因。"""
        return cls(
            original=original,
            rewritten=original,
            changed=False,
            decision=DECISION_USE_ORIGINAL,
            reason="改写不可用,回退原问题",
            rewriter_failed=True,
            error=error,
        )


@dataclass
class HardCheckResult:
    """规则硬校验的结果。`passed=False` 时 Router 必须走 use_original。"""

    passed: bool = True
    # 触发的规则名列表(空表示全部通过),便于日志聚合统计哪条规则最常拦下改写
    violations: list[str] = field(default_factory=list)
    # True 表示硬校验把 LLM 的 decision 覆盖掉了
    overridden: bool = False
    original_decision: str = ""
    # 本层给出的最终决策(可能是 use_original,也可能是阈值推导出的 dual_retrieval)
    final_decision: str = DECISION_USE_ORIGINAL
    # 多意图闸门:为 True 时禁止 dual_retrieval
    multi_intent: bool = False
    # 相似度检查被跳过(embedding 不可用),不代表通过 —— 只是这一条没结论
    similarity_skipped: bool = False
    detail: dict = field(default_factory=dict)
