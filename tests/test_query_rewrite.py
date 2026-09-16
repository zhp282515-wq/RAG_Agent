"""改写闭环的单元测试(标准库 unittest,零新依赖、不联网、不耗 token)。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -v

覆盖需求指定的五类场景(指代消解 / 否定词保留 / 数字保留 / 多意图 / 模糊追问)
以及各降级边界。所有 LLM、embedding、检索都换成桩,因此可以在离线状态下
反复运行,也不会产生任何 API 费用。

设计说明:
  - 桩模型(StubModel)按调用序返回预设的 JSON 字符串,同时记录调用次数 ——
    「缓存命中时不再调模型」「开关关闭时零调用」这两条断言依赖它。
  - 桩检索器(StubRetriever)记录每次收到的 query,用来断言路由分支究竟检索了什么。
  - config 的阈值通过环境变量(REWRITE_*)临时覆盖,测完在 tearDown 里还原,
    这样无需改 rag.yml 就能测到边界行为。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

# 允许直接 `python -m unittest discover -s tests` 时找到项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.query_rewrite import config, glossary  # noqa: E402
from utils.query_rewrite.hard_checker import HardChecker  # noqa: E402
from utils.query_rewrite.models import (  # noqa: E402
    DECISION_ASK_CLARIFY,
    DECISION_DUAL,
    DECISION_USE_ORIGINAL,
    DECISION_USE_REWRITTEN,
    RewriteResult,
    SelfCheck,
)
from utils.query_rewrite.pipeline import QueryRewritePipeline, clear_cache  # noqa: E402
from utils.query_rewrite.rewriter import Rewriter, _format_history, _parse_result  # noqa: E402
from utils.query_rewrite.router import Router, dedupe_hits  # noqa: E402


# ---------------- 测试替身 ----------------

class _StubMessage:
    def __init__(self, text: str):
        self.content = text
        self.text = text
        self.usage_metadata = {"total_tokens": 30}


class StubModel:
    """按调用序返回预设回复的假 chat model。

    只实现 Rewriter 会用到的 bind / with_config / invoke 三个方法。
    """

    def __init__(self, replies: list[str] | str):
        self.replies = [replies] if isinstance(replies, str) else list(replies)
        self.calls = 0
        self.seen_prompts: list = []
        self.configs: list = []

    def bind(self, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self

    def invoke(self, messages, config=None, **kwargs):
        # 接受 config:Rewriter._invoke 会显式传 config(含 callbacks=[] 与 lc_source),
        # 真实 LangChain 模型(super().invoke)支持该参数;桩必须同样支持,否则会
        # 误报成"模型调用失败"而降级。config 内容由 test_rewrite_stream_leak.py 断言。
        self.seen_prompts.append(messages)
        self.configs.append(config)
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return _StubMessage(self.replies[idx])


class BoomModel:
    """调用即抛异常的假模型(测降级路径)。"""

    def bind(self, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self

    def invoke(self, messages, config=None, **kwargs):
        raise RuntimeError("模型服务不可用")


class StubRetriever:
    """假检索器:按 query 造命中,并记录所有收到的 query。"""

    def __init__(self, hits_per_query: int = 2):
        self.queries: list[str] = []
        self.hits_per_query = hits_per_query
        self.fail_on: set[str] = set()

    def __call__(self, query, *, top_k, rerank_n, on_step=None, raise_on_infra_error=False):
        self.queries.append(query)
        if query in self.fail_on:
            raise RuntimeError(f"检索失败:{query}")
        return [
            {"pk": f"{query}#{i}", "text": f"{query} 的资料 {i}",
             "score": 0.9 - i * 0.1, "file_name": "test.txt"}
            for i in range(self.hits_per_query)
        ]


def payload(**overrides) -> str:
    """造一份合法的改写 JSON。默认是「安全的改写」,用 overrides 改单项。"""
    base = {
        "rewritten": "扫地机器人滤网多久更换一次",
        "changed": True,
        "changes": ["把口语规范为术语"],
        "preserved_constraints": {
            "entities": ["滤网"], "negations": [], "conditions": [], "numbers": [],
        },
        "self_check": {
            "intent_same": True, "entities_same": True,
            "constraints_same": True, "confidence": 0.95,
        },
        "decision": "use_rewritten",
        "reason": "仅做术语规范化",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class ConfigEnvMixin:
    """通过环境变量临时覆盖阈值,tearDown 时还原。

    这里默认**关掉**「规则直通」(`skip_when_no_rewrite_needed`):本文件绝大多数
    用例测的是改写→硬校验→路由这条主链路,直通会把它们短路掉、测不到目标行为。
    直通本身由 TestSkipRewrite 专门覆盖。
    """

    _ENV_KEYS = ("REWRITE_CONF_HIGH", "REWRITE_CONF_MID", "REWRITE_SIM_MIN",
                 "REWRITE_JACCARD_CLARIFY", "REWRITE_ENABLED", "REWRITE_CACHE_ENABLED",
                 "REWRITE_SKIP_WHEN_NO_REWRITE_NEEDED")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ["REWRITE_SKIP_WHEN_NO_REWRITE_NEEDED"] = "0"
        clear_cache()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_cache()


def build_pipeline(replies, *, embedder=None, retriever=None, query="滤网多久换一次"):
    """组装一个全桩的 pipeline,返回 (pipeline, 模型桩, 检索器桩)。"""
    model = StubModel(replies)
    retr = retriever or StubRetriever()
    pipe = QueryRewritePipeline(
        rewriter=Rewriter(model=model),
        router=Router(retriever=retr),
        embedder=embedder or (lambda t: [1.0, 0.0]),
    )
    return pipe, model, retr


# ---------------- 1. 词典与抽取规则 ----------------

class TestGlossary(ConfigEnvMixin, unittest.TestCase):
    def test_negation_extraction_prefers_longer_terms(self):
        """长否定词优先:「不再」不应被拆成「不」+「再」。"""
        self.assertEqual(glossary.negations("不再需要换滤网"), ["不再"])
        self.assertEqual(glossary.negations("不能用水洗"), ["不能"])
        self.assertEqual(glossary.negations("不含甲醛"), ["不"])

    def test_negation_count_matches_one_to_one(self):
        self.assertEqual(len(glossary.negations("不要不装滤网")), 2)

    def test_numbers_normalized_whitespace(self):
        """「120 分钟」与「120分钟」是同一数值,不该因空格判成不一致。"""
        self.assertEqual(glossary.numbers_and_times("续航 120 分钟"),
                         glossary.numbers_and_times("续航120分钟"))
        self.assertEqual(glossary.numbers_and_times("续航 120 分钟"), ["120分钟"])

    def test_number_unit_conversion_is_not_equal(self):
        """单位换算改变了数值表达,规则层必须判为不一致(铁律第 3 条)。"""
        self.assertNotEqual(glossary.numbers_and_times("120 分钟"),
                            glossary.numbers_and_times("2 小时"))

    def test_bare_number_extracted(self):
        self.assertIn("3", glossary.numbers("第 3 代机型"))

    def test_multi_intent_requires_three_segments(self):
        """三段及以上的并列才判多意图;两段式留着走 dual。"""
        self.assertTrue(glossary.looks_multi_intent("续航多久,噪音多大,售后怎么样"))
        self.assertTrue(glossary.looks_multi_intent("续航以及噪音以及售后分别是多少"))
        # 两段:「续航和噪音」是可比较的单意图 → 不判多意图(保留 dual 机会)
        self.assertFalse(glossary.looks_multi_intent("续航和噪音"))
        # 单一意图里出现「和」不能误判
        self.assertFalse(glossary.looks_multi_intent("扫地和拖地的区别"))
        self.assertFalse(glossary.looks_multi_intent("科沃斯和小米的扫地机器人哪个好"))

    def test_vague_and_reference_detection(self):
        self.assertTrue(glossary.is_vague("它怎么样"))
        self.assertFalse(glossary.is_vague("滤网的更换周期一般是多久呢"))
        self.assertTrue(glossary.has_reference("那它续航多久"))
        self.assertFalse(glossary.has_reference("滤网多久换一次"))

    def test_jaccard_bounds(self):
        self.assertEqual(glossary.jaccard("它怎么样", "它怎么样"), 1.0)
        self.assertEqual(glossary.jaccard("", "abc"), 0.0)

    def test_cosine_handles_bad_input(self):
        self.assertEqual(glossary.cosine([1.0, 0.0], [4.0, 0.0]), 1.0)
        # 维度不符 → -1,调用方据此跳过该检查
        self.assertEqual(glossary.cosine([1.0, 0.0], [1.0, 0.0, 0.0]), -1.0)
        self.assertEqual(glossary.cosine([], []), -1.0)


# ---------------- 2. Rewriter 解析与容错 ----------------

class TestRewriterParsing(ConfigEnvMixin, unittest.TestCase):
    def test_parse_valid_json(self):
        r = _parse_result("滤网多久换", payload())
        self.assertEqual(r.rewritten, "扫地机器人滤网多久更换一次")
        self.assertTrue(r.changed)
        self.assertEqual(r.decision, DECISION_USE_REWRITTEN)
        self.assertAlmostEqual(r.self_check.confidence, 0.95)

    def test_parse_tolerates_markdown_fence(self):
        fenced = "```json\n" + payload() + "\n```"
        self.assertEqual(_parse_result("q", fenced).decision, DECISION_USE_REWRITTEN)

    def test_parse_tolerates_surrounding_prose(self):
        noisy = "好的,结果如下:" + payload() + "以上。"
        self.assertEqual(_parse_result("q", noisy).decision, DECISION_USE_REWRITTEN)

    def test_parse_rejects_non_json(self):
        with self.assertRaises(ValueError):
            _parse_result("q", "这不是 JSON")

    def test_parse_rejects_missing_rewritten(self):
        with self.assertRaises(ValueError):
            _parse_result("q", json.dumps({"changed": False}))

    def test_unknown_decision_falls_back_to_use_original(self):
        r = _parse_result("q", payload(decision="something_weird"))
        self.assertEqual(r.decision, DECISION_USE_ORIGINAL)

    def test_confidence_clamped_to_unit_range(self):
        r = _parse_result("q", json.dumps({
            "rewritten": "x", "changed": True, "decision": "use_rewritten",
            "self_check": {"intent_same": True, "entities_same": True,
                           "constraints_same": True, "confidence": 5.0},
        }))
        self.assertEqual(r.self_check.confidence, 1.0)

    def test_history_budget_and_turn_limit(self):
        history = [("user", f"问题{i}") for i in range(10)]
        text = _format_history(history)
        # 只保留最近 history_turns 轮(默认 3)
        self.assertIn("问题9", text)
        self.assertNotIn("问题0", text)

    def test_history_renders_multimodal_text_blocks(self):
        hist = [{"role": "user", "content": [
            {"type": "text", "text": "扫地机器人续航多少"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
        ]}]
        text = _format_history(hist)
        self.assertIn("扫地机器人续航多少", text)
        self.assertNotIn("data:x", text)


# ---------------- 3. 需求指定的五类场景(闭环级) ----------------

class TestCoreScenarios(ConfigEnvMixin, unittest.TestCase):
    def test_reference_resolution_with_history_succeeds(self):
        """指代消解有上下文依据 → 采用改写。"""
        pipe, model, retr = build_pipeline(payload(
            rewritten="扫地机器人充电需要多长时间",
            changes=["把'它'消解为'扫地机器人'"],
            preserved_constraints={"entities": ["扫地机器人"], "negations": [],
                                   "conditions": [], "numbers": []},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.95},
        ))
        plan = pipe.run("那它充电要多久",
                        history=[("user", "扫地机器人续航一般多久"),
                                 ("assistant", "主流机型约 120 分钟。")],
                        top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_REWRITTEN)
        self.assertEqual(plan.queries, ["扫地机器人充电需要多长时间"])
        self.assertFalse(plan.fell_back)
        self.assertEqual(model.calls, 1, "闭环应只调一次改写模型")

    def test_reference_without_history_does_not_invent_entity(self):
        """无上下文依据的指代:模型若凭空补实体,规则层应拦下。"""
        # 模型"猜"出了实体(违规),且自报高置信度
        pipe, _, retr = build_pipeline(payload(
            rewritten="扫地机器人噪音怎么样",
            preserved_constraints={"entities": ["扫地机器人"], "negations": [],
                                   "conditions": [], "numbers": []},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.99},
        ))
        # 相似度过低(「它怎么样」vs「扫地机器人噪音怎么样」)由 embedding 桩模拟
        pipe._embedder = lambda t: [1.0, 0.0] if "它怎么样" in t else [0.2, 0.9]
        plan = pipe.run("它怎么样", history=None, top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertTrue(plan.fell_back)
        self.assertEqual(plan.queries, ["它怎么样"], "回退必须检索原问题")

    def test_negation_dropped_is_rejected(self):
        """否定词丢失:规则层必须覆盖模型的高置信度判断。"""
        pipe, _, retr = build_pipeline(payload(
            rewritten="含甲醛的清洁剂有哪些",
            preserved_constraints={"entities": ["清洁剂"], "negations": [],
                                   "conditions": [], "numbers": []},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.99},
        ))
        plan = pipe.run("不含甲醛的清洁剂有哪些", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("否定词数量不一致", plan.hard_check.violations)
        self.assertEqual(plan.queries, ["不含甲醛的清洁剂有哪些"])

    def test_number_conversion_is_rejected(self):
        """数字被换算(120 分钟 → 2 小时):规则层必须拦下。"""
        pipe, _, retr = build_pipeline(payload(
            rewritten="续航 2 小时的机型有哪些",
            preserved_constraints={"entities": [], "negations": [],
                                   "conditions": [], "numbers": ["120 分钟"]},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.99},
        ))
        plan = pipe.run("续航 120 分钟的机型有哪些", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("数字/时间不一致", plan.hard_check.violations)

    def test_multi_intent_cannot_use_dual_retrieval(self):
        """多意图:即便模型给了 dual_retrieval,也必须降级(双路无收益)。

        多意图是**策略**拦截而非安全违规(改写本身可能完全忠实),故记在
        detail["reasons"] 而不是 violations —— violations 只表示「改写不安全」。
        断言重点是行为:不检索两路。
        """
        multi_q = "续航多久,噪音多大,售后怎么样"
        # 改写结果必须与原文不同,否则会先被「未做改写」分支短路,测不到多意图闸门
        multi_rewritten = "续航时间多久,工作噪音多大,售后服务怎么样"
        pipe, _, retr = build_pipeline(payload(
            rewritten=multi_rewritten,
            changed=True,
            # 不带 entities,避免触发无关的实体保全规则干扰本用例
            preserved_constraints={"entities": [], "negations": [],
                                   "conditions": [], "numbers": []},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.88},
            decision="dual_retrieval",
        ))
        plan = pipe.run(multi_q, top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertNotEqual(plan.decision, DECISION_DUAL)
        self.assertEqual(len(retr.queries), 1, "多意图不该触发双路检索")
        self.assertIn("多意图禁用双路检索", plan.hard_check.detail["reasons"])
        self.assertTrue(plan.hard_check.multi_intent)

    def test_vague_question_triggers_clarify_and_skips_retrieval(self):
        """模糊问题 → ask_clarify,且**不检索**。"""
        pipe, _, retr = build_pipeline(payload(
            rewritten="它怎么样", changed=False, changes=[],
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.2},
            decision=DECISION_ASK_CLARIFY, reason="无依据可消解指代",
        ))
        plan = pipe.run("它怎么样", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_ASK_CLARIFY)
        self.assertTrue(plan.clarify, "追问分支必须给出追问文案")
        self.assertEqual(plan.hits, [])
        self.assertEqual(retr.queries, [], "追问分支不能检索")

    def test_implausible_clarify_is_downgraded(self):
        """模型实质改写了问题却要追问 = 答非所问 → 降级为 use_original。"""
        pipe, _, retr = build_pipeline(payload(
            rewritten="扫地机器人噪音怎么样", changed=True,
            decision=DECISION_ASK_CLARIFY,
        ))
        plan = pipe.run("它怎么样", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("追问与实质改写不一致", plan.hard_check.violations)
        self.assertEqual(retr.queries, ["它怎么样"])


# ---------------- 4. 分级路由分支 ----------------

class TestRouting(ConfigEnvMixin, unittest.TestCase):
    def test_dual_retrieval_merges_and_dedupes(self):
        """双路:两路都检索,结果按 pk 合并去重。"""
        pipe, _, retr = build_pipeline(payload(
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.82},
            decision=DECISION_DUAL,
        ))
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_DUAL)
        self.assertEqual(len(retr.queries), 2, "双路应检索两次")
        self.assertEqual(sorted(retr.queries),
                         sorted(["滤网多久换一次", "扫地机器人滤网多久更换一次"]))
        # 两路的 pk 不同(桩里 pk 含 query),故 2+2 条都在
        self.assertEqual(len(plan.hits), 4)

    def test_dual_retrieval_survives_one_leg_failure(self):
        """双路其中一路失败:用另一路结果,不整体报错。"""
        retr = StubRetriever()
        retr.fail_on = {"滤网多久换一次"}
        pipe, _, _ = build_pipeline(payload(
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.82},
            decision=DECISION_DUAL,
        ), retriever=retr)
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_DUAL)
        self.assertEqual(len(plan.hits), 2, "存活的那一路结果应保留")
        self.assertFalse(plan.error)

    def test_dedupe_keeps_higher_score(self):
        merged = dedupe_hits([
            [{"pk": "a", "score": 0.5, "text": "x"}],
            [{"pk": "a", "score": 0.9, "text": "x"}],
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["score"], 0.9)

    def test_dedupe_falls_back_to_text_when_no_pk(self):
        merged = dedupe_hits([
            [{"text": "同一段资料", "score": 0.4}],
            [{"text": "同一段资料", "score": 0.8}],
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["score"], 0.8)


# ---------------- 5. 降级与开关 ----------------

class TestDegradation(ConfigEnvMixin, unittest.TestCase):
    def test_changed_false_forces_use_original(self):
        """changed=false 时即便模型自称 use_rewritten,也必须走原问题。

        断言的是**行为**(检索原问题 + 决策被阈值重算),而非某个违规标签:
        changed=false 属「无需改写」的正常情形,按阈值口径落到 use_original 即可,
        不该记为违规(记违规会让日志里出现大量无意义的拦截噪音)。
        """
        pipe, _, retr = build_pipeline(payload(
            rewritten="滤网多久换一次", changed=False,
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.99},
            decision=DECISION_USE_REWRITTEN,
        ))
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertEqual(plan.queries, ["滤网多久换一次"])
        self.assertTrue(plan.hard_check.detail.get("recalibrated"),
                        "模型自称 use_rewritten 却被重算,应标记 recalibrated")

    def test_llm_exception_degrades_to_original(self):
        pipe = QueryRewritePipeline(
            rewriter=Rewriter(model=BoomModel()),
            router=Router(retriever=StubRetriever()),
            embedder=lambda t: [1.0, 0.0],
        )
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertTrue(plan.rewrite_result.rewriter_failed)
        self.assertEqual(plan.queries, ["滤网多久换一次"])

    def test_bad_json_degrades_after_retry(self):
        """JSON 坏 → 重试一次 → 仍坏则降级(且只调两次,不无限重试)。"""
        pipe, model, _ = build_pipeline(["不是 JSON", "还不是 JSON"])
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertTrue(plan.rewrite_result.rewriter_failed)
        self.assertEqual(model.calls, 2, "应恰好重试一次")

    def test_embedding_failure_skips_similarity_not_fatal(self):
        """embedding 不可用:跳过相似度检查,其余规则照跑,不因此判死。"""
        def boom(text):
            raise RuntimeError("embedding 服务不可用")

        pipe, _, retr = build_pipeline(payload(), embedder=boom)
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertTrue(plan.hard_check.similarity_skipped)
        self.assertNotIn("语义相似度低于阈值", plan.hard_check.violations)
        self.assertEqual(plan.decision, DECISION_USE_REWRITTEN)

    def test_similarity_below_threshold_forces_original(self):
        """相似度低于 sim_min → 覆盖为 use_original。"""
        def embed(text):
            return [1.0, 0.0] if text == "滤网多久换一次" else [0.1, 0.99]

        pipe, _, _ = build_pipeline(payload(), embedder=embed)
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("语义相似度低于阈值", plan.hard_check.violations)

    def test_similarity_threshold_is_configurable(self):
        """阈值可调:同一份向量在放宽阈值后应当通过。"""
        def embed(text):
            return [1.0, 0.0] if text == "滤网多久换一次" else [0.5, 0.866]  # cos ≈ 0.5

        os.environ["REWRITE_SIM_MIN"] = "0.9"
        pipe, _, _ = build_pipeline(payload(), embedder=embed)
        self.assertEqual(
            pipe.run("滤网多久换一次", top_k=20, rerank_n=5).decision,
            DECISION_USE_ORIGINAL)

        clear_cache()
        os.environ["REWRITE_SIM_MIN"] = "0.4"
        pipe2, _, _ = build_pipeline(payload(), embedder=embed)
        self.assertEqual(
            pipe2.run("滤网多久换一次", top_k=20, rerank_n=5).decision,
            DECISION_USE_REWRITTEN)

    def test_entity_dropped_is_rejected(self):
        """模型自报的实体没出现在改写结果里 → 拦下。"""
        pipe, _, _ = build_pipeline(payload(
            rewritten="吸尘器的滤网多久更换",
            preserved_constraints={"entities": ["科沃斯"], "negations": [],
                                   "conditions": [], "numbers": []},
        ))
        plan = pipe.run("科沃斯的滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("实体未被保留", plan.hard_check.violations)

    def test_domain_term_dropped_is_rejected_even_if_not_self_reported(self):
        """模型漏报 entities 也没用:领域专名由规则层从原问题重新抽取。"""
        pipe, _, _ = build_pipeline(payload(
            rewritten="家用清洁设备的耗材更换周期",
            preserved_constraints={"entities": [], "negations": [],
                                   "conditions": [], "numbers": []},
        ))
        plan = pipe.run("拖布多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertIn("领域专名未被保留", plan.hard_check.violations)

    def test_disabled_switch_skips_llm_and_embedding_entirely(self):
        """总开关关闭:零 LLM 调用、零 embedding,直接检索原问题。"""
        def exploding_embed(text):
            raise AssertionError("开关关闭时不应调用 embedding")

        model = StubModel([payload()])
        retr = StubRetriever()
        pipe = QueryRewritePipeline(
            rewriter=Rewriter(model=model),
            router=Router(retriever=retr),
            embedder=exploding_embed,
        )
        os.environ["REWRITE_ENABLED"] = "0"
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertTrue(plan.disabled)
        self.assertEqual(model.calls, 0, "开关关闭时不应调用改写模型")
        self.assertEqual(plan.queries, ["滤网多久换一次"])

    def test_empty_query_does_not_call_llm(self):
        model = StubModel([payload()])
        pipe = QueryRewritePipeline(
            rewriter=Rewriter(model=model),
            router=Router(retriever=StubRetriever()),
            embedder=lambda t: [1.0, 0.0],
        )
        plan = pipe.run("   ", top_k=20, rerank_n=5)
        self.assertEqual(model.calls, 0)
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)

    def test_retrieval_infra_failure_is_reported_not_raised(self):
        """检索崩溃:plan.error 记录,不抛出(由工具层决定是否重试)。

        用单路(use_rewritten)场景:双路场景下 router 会自行降级为「用存活的一路」,
        只有全部路都失败才上抛 —— 那是另一条更宽松的策略,单独测。
        """
        retr = StubRetriever()
        retr.fail_on = {"扫地机器人滤网多久更换一次"}
        pipe, _, _ = build_pipeline(payload(), retriever=retr)
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertTrue(plan.error)
        self.assertEqual(plan.hits, [])


# ---------------- 6. 缓存 ----------------

class TestCache(ConfigEnvMixin, unittest.TestCase):
    def test_cache_hit_avoids_second_llm_call(self):
        pipe, model, retr = build_pipeline(payload())
        p1 = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        calls_after_first = model.calls
        p2 = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertFalse(p1.cached)
        self.assertTrue(p2.cached)
        self.assertEqual(model.calls, calls_after_first, "缓存命中不应再调改写模型")
        self.assertEqual(p2.decision, p1.decision)

    def test_cache_key_ignores_whitespace_and_case(self):
        pipe, model, _ = build_pipeline(payload())
        pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        calls = model.calls
        pipe.run("滤网 多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(model.calls, calls)

    def test_cache_can_be_disabled(self):
        os.environ["REWRITE_CACHE_ENABLED"] = "0"
        pipe, model, _ = build_pipeline(payload())
        pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(model.calls, 2, "缓存关闭时应每次都调模型")

    def test_different_queries_do_not_share_cache(self):
        pipe, model, _ = build_pipeline(payload())
        pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        pipe.run("边刷多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(model.calls, 2)


# ---------------- 7. 配置可调性 ----------------

class TestConfig(ConfigEnvMixin, unittest.TestCase):
    def test_thresholds_readable_from_env(self):
        os.environ["REWRITE_CONF_HIGH"] = "0.99"
        self.assertEqual(config.get_float("conf_high"), 0.99)
        os.environ["REWRITE_SIM_MIN"] = "0.5"
        self.assertEqual(config.get_float("sim_min"), 0.5)

    def test_malformed_value_falls_back_to_default(self):
        os.environ["REWRITE_SIM_MIN"] = "不是数字"
        self.assertEqual(config.get_float("sim_min"), 0.75)

    def test_conf_high_threshold_actually_gates_decision(self):
        """把 conf_high 调到 0.99 后,0.95 的改写应降级为 dual,证明阈值真的生效。"""
        os.environ["REWRITE_CONF_HIGH"] = "0.99"
        pipe, _, _ = build_pipeline(payload())
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5)
        self.assertEqual(plan.decision, DECISION_DUAL)

    def test_rewrite_model_defaults_to_summary_model_not_main_model(self):
        """改写必须用轻量档模型,不能回退到对话主模型(需求硬约束)。"""
        from utils.config_tool import model_conf
        summary = (model_conf.get("summary_model") or "").split(":", 1)[-1]
        main = (model_conf.get("model") or "").split(":", 1)[-1]
        self.assertEqual(config.rewrite_model_name(), summary)
        self.assertNotEqual(config.rewrite_model_name(), main)

    def test_prompt_contains_iron_rules_and_few_shots(self):
        from utils.query_rewrite.prompts import FEW_SHOT_SCENARIOS, build_system_prompt
        prompt = build_system_prompt()
        for rule in ("不增删实体", "不增删逻辑约束", "不增删数字", "不改变问题意图",
                     "不合并或拆分用户意图", "指代消解必须有上下文依据"):
            self.assertIn(rule, prompt, f"铁律缺失:{rule}")
        self.assertGreaterEqual(len(FEW_SHOT_SCENARIOS), 3, "至少需要 3 个 few-shot")
        for scenario in FEW_SHOT_SCENARIOS:
            self.assertIn(scenario, prompt, f"few-shot 场景缺失:{scenario}")
        # 阈值必须真的注入进 prompt,否则模型按旧阈值自报
        self.assertIn(str(config.get("conf_high")), prompt)


# ---------------- 8. 日志 ----------------

class TestEvalLog(ConfigEnvMixin, unittest.TestCase):
    def test_soft_signals_recorded_without_blocking(self):
        """条件/比较/量词只做诊断记录:**不得**因此拦截改写。

        这条用例锁住一个刻意的设计决定:这三类词在中文里极易良性同义替换
        (「所有」→「全部」、「比」是「比较」的子串),若当违规会误杀大量忠实改写。
        """
        pipe, _, _ = build_pipeline(payload(
            rewritten="全部扫地机器人的滤网都多久更换一次",
            preserved_constraints={"entities": ["滤网"], "negations": [],
                                   "conditions": [], "numbers": []},
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.95},
        ))
        plan = pipe.run("所有扫地机器人的滤网多久换一次", top_k=20, rerank_n=5)
        # 原问题「所有」被改写成「全部」(良性同义替换)→ 记录差异,但不拦
        self.assertEqual(plan.decision, DECISION_USE_REWRITTEN)
        soft = plan.hard_check.detail["soft_signals"]
        self.assertIn("全部", soft["quantifiers"]["only_rewritten"])
        self.assertIn("所有", soft["quantifiers"]["only_original"])
        self.assertEqual(plan.hard_check.violations, [])

    def test_jsonl_record_written_with_required_fields(self):
        from utils.query_rewrite import pipeline as pl
        pipe, _, _ = build_pipeline(payload())
        plan = pipe.run("记录一条改写日志", top_k=20, rerank_n=5)

        log_path = Path(pl._LOG_DIR) / f"rewrite_{__import__('datetime').date.today():%Y-%m-%d}.jsonl"
        self.assertTrue(log_path.exists(), f"评估日志未生成:{log_path}")
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(lines)
        rec = json.loads(lines[-1])
        for key in ("original", "rewritten", "changed", "decision", "llm_decision",
                    "derived_decision", "fell_back", "fallback_reason", "violations",
                    "scores", "hit_count", "cached"):
            self.assertIn(key, rec, f"日志缺字段:{key}")
        self.assertEqual(rec["original"], "记录一条改写日志")
        self.assertIn("similarity", rec["scores"])


# ---------------- 9. 工作流阶段上报 ----------------

class TestPhaseEmit(ConfigEnvMixin, unittest.TestCase):
    """阶段上报:改写与检索必须是两个**各自计时**的平级阶段,且顺序正确。

    顺序很关键:改写发生在检索之前,若顺序反了(历史上踩过:monitor_tool 的容器阶段
    先发、工具内部的改写阶段后发),面板上会出现「检索 → 问题改写」的语义倒序。
    """

    def _run(self, payload_json, **kwargs):
        events: list = []
        pipe, _, _ = build_pipeline(payload_json)
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5,
                        phase_emit=lambda a, n, p=None, d=None, t=None:
                        events.append((a, n, dict(p or {}), d, t)))
        return plan, events

    def test_rewrite_and_retrieval_are_sibling_phases_in_order(self):
        plan, ev = self._run(payload())
        names = [e[1] for e in ev]
        self.assertEqual(names, ["问题改写", "问题改写", "知识库检索", "知识库检索"],
                         "应为 改写(start/end) + 检索(start/end) 四个事件且顺序正确")
        self.assertEqual([e[0] for e in ev], ["start", "end", "start", "end"])

    def test_rewrite_phase_carries_decision_and_scores(self):
        _, ev = self._run(payload())
        end = ev[1][2]
        self.assertEqual(end["决策"], DECISION_USE_REWRITTEN)
        self.assertEqual(end["原问题"], "滤网多久换一次")
        self.assertEqual(end["改写"], "扫地机器人滤网多久更换一次")
        self.assertIn("置信度", end)

    def test_retrieval_phase_reports_the_rewritten_query(self):
        """检索阶段开始的「检索词」就必须是最终要检索的串,不能先显示原问题再变。"""
        _, ev = self._run(payload())
        self.assertEqual(ev[2][2]["检索词"], "扫地机器人滤网多久更换一次",
                         "use_rewritten 时 start 就该显示改写后的检索词")

    def test_fallback_is_surfaced_in_phase_params(self):
        """回退时要在阶段参数里体现出「为什么没用改写」,便于排查。"""
        bad = payload(rewritten="含甲醛的清洁剂有哪些",
                      preserved_constraints={"entities": ["清洁剂"], "negations": [],
                                             "conditions": [], "numbers": []})
        events: list = []
        pipe, _, _ = build_pipeline(bad)
        pipe.run("不含甲醛的清洁剂有哪些", top_k=20, rerank_n=5,
                 phase_emit=lambda a, n, p=None, d=None, t=None:
                 events.append((a, n, dict(p or {}), d, t)))
        rewrite_end = events[1][2]
        self.assertEqual(rewrite_end["决策"], DECISION_USE_ORIGINAL)
        self.assertIn("违规项", rewrite_end)
        self.assertIn("回退", rewrite_end)
        # 检索阶段必须用原问题
        self.assertEqual(events[2][2]["检索词"], "不含甲醛的清洁剂有哪些")

    def test_clarify_branch_emits_retrieval_phase_marked_skipped(self):
        """追问分支不检索,但仍要闭合检索阶段 —— 否则面板会一直停在"检索中"。"""
        events: list = []
        pipe, _, retr = build_pipeline(payload(
            rewritten="它怎么样", changed=False, decision=DECISION_ASK_CLARIFY,
            self_check={"intent_same": True, "entities_same": True,
                        "constraints_same": True, "confidence": 0.2},
        ))
        pipe.run("它怎么样", top_k=20, rerank_n=5,
                 phase_emit=lambda a, n, p=None, d=None, t=None:
                 events.append((a, n, dict(p or {}), d, t)))
        self.assertEqual(retr.queries, [], "追问分支不应检索")
        self.assertEqual([e[1] for e in events],
                         ["问题改写", "问题改写", "知识库检索", "知识库检索"])
        self.assertIn("需澄清", events[3][2].get("说明", ""))
        self.assertEqual(events[3][2]["命中"], 0)

    def test_phase_emit_is_optional(self):
        """未注入回调时必须照常工作(CLI / 单测场景)。"""
        pipe, _, _ = build_pipeline(payload())
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5, phase_emit=None)
        self.assertEqual(plan.decision, DECISION_USE_REWRITTEN)

    def test_broken_phase_emit_does_not_break_pipeline(self):
        """上报回调抛异常不能影响问答本身(工作流可视化是附属能力)。"""
        def boom(*a, **k):
            raise RuntimeError("前端采集器炸了")

        pipe, _, _ = build_pipeline(payload())
        plan = pipe.run("滤网多久换一次", top_k=20, rerank_n=5, phase_emit=boom)
        self.assertEqual(plan.decision, DECISION_USE_REWRITTEN)
        self.assertTrue(plan.hits)


# ---------------- 10. 规则直通(跳过 LLM)与模型构造 ----------------

class TestSkipRewrite(ConfigEnvMixin, unittest.TestCase):
    """规则判定「无需改写」时直接跳过 LLM —— 省掉整次模型调用(实测十几秒)。

    这里刻意**打开**直通开关(基类默认关掉),验证它确实省了调用。
    """

    def setUp(self):
        super().setUp()
        os.environ["REWRITE_SKIP_WHEN_NO_REWRITE_NEEDED"] = "1"

    def test_canonical_question_skips_llm_entirely(self):
        """已是规范检索句(有专名/无指代/无口语/够长)→ 零 LLM 调用,直接用原问题检索。"""
        pipe, model, retr = build_pipeline(payload())
        plan = pipe.run("扫地机器人耗电快怎么办", top_k=20, rerank_n=5)
        self.assertTrue(plan.skipped_rewrite)
        self.assertEqual(model.calls, 0, "规则直通时不应调用改写模型")
        self.assertEqual(plan.queries, ["扫地机器人耗电快怎么办"], "应直接用原问题检索")
        self.assertEqual(plan.decision, DECISION_USE_ORIGINAL)
        self.assertTrue(plan.hits, "直通也要正常检索")

    def test_colloquial_question_still_calls_llm(self):
        """带口语的问题仍要走模型 —— 删口语正是改写的收益所在。"""
        pipe, model, _ = build_pipeline(payload())
        plan = pipe.run("我的机器人耗电太快咋办", top_k=20, rerank_n=5)
        self.assertFalse(plan.skipped_rewrite)
        self.assertEqual(model.calls, 1)

    def test_reference_question_still_calls_llm(self):
        """含指代的问题必须走模型(指代消解要结合上下文,规则判不了)。"""
        pipe, model, _ = build_pipeline(payload())
        plan = pipe.run("那它续航多久", top_k=20, rerank_n=5)
        self.assertFalse(plan.skipped_rewrite)
        self.assertEqual(model.calls, 1)

    def test_short_question_still_calls_llm(self):
        """过短的问题交给模型判是否该追问(「怎么配对」这类规则无法判定)。"""
        pipe, model, _ = build_pipeline(payload())
        plan = pipe.run("怎么配对", top_k=20, rerank_n=5)
        self.assertFalse(plan.skipped_rewrite)
        self.assertEqual(model.calls, 1)

    def test_skip_can_be_disabled(self):
        os.environ["REWRITE_SKIP_WHEN_NO_REWRITE_NEEDED"] = "0"
        pipe, model, _ = build_pipeline(payload())
        plan = pipe.run("扫地机器人耗电快怎么办", top_k=20, rerank_n=5)
        self.assertFalse(plan.skipped_rewrite)
        self.assertEqual(model.calls, 1)

    def test_skip_emits_both_phases(self):
        """直通路径仍要发完整的两个阶段(start/end 成对),否则面板会缺一段。"""
        events: list = []
        pipe, _, _ = build_pipeline(payload())
        pipe.run("扫地机器人耗电快怎么办", top_k=20, rerank_n=5,
                 phase_emit=lambda a, n, p=None, d=None, t=None:
                 events.append((a, n, dict(p or {}))))
        self.assertEqual([(a, n) for a, n, _ in events],
                         [("start", "问题改写"), ("end", "问题改写"),
                          ("start", "知识库检索"), ("end", "知识库检索")])
        self.assertIn("跳过模型", events[1][2].get("说明", ""))


class TestRewriteModelConstruction(ConfigEnvMixin, unittest.TestCase):
    """改写模型的构造参数:关思维链是实测 6 倍提速的关键,别被改回默认。"""

    def test_thinking_disabled_by_default(self):
        self.assertTrue(config.get_bool("disable_thinking"),
                        "默认应关闭思维链(实测开启时 18.4s 且撞 max_tokens 失败)")

    def test_get_chat_model_accepts_max_tokens_and_extra_body(self):
        """modelfactory 必须支持构造期传 max_tokens / extra_body。

        extra_body 只能构造期传:实测放进 invoke 的 config 里 DashScope 会忽略,
        enable_thinking 失效、思考照旧发生(这条踩过)。
        """
        import inspect
        from model.modelfactory import get_chat_model
        params = inspect.signature(get_chat_model).parameters
        self.assertIn("max_tokens", params)
        self.assertIn("extra_body", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
