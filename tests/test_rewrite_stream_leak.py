"""改写闭环的泄漏回归测试:真实流式模型 + 真实 create_agent。

**为什么单独一个文件**:其它测试把 rewriter / 检索全换成桩,好处是隔离、快、不耗 token,
代价是**桩没有回调机制,压根不存在泄漏路径** —— 于是真实环境里"改写 JSON 被当成回答
推给前端"这个问题,桩测试完全抓不到(已在真实环境复现并落库 4 条污染消息)。

本文件的模型是**真能流式**的 `BaseChatModel` 子类,并且用**真实 `create_agent`** 跑,
所以 `langchain_core.runnables.config.ensure_config` 从 `var_child_runnable_config`
继承 callbacks 这条路径是真实存在的。断言的是"内部调用的文本不出现在流出分片里"。

仍然不联网:模型是本地假实现,但走的是与真实模型完全相同的回调/流式协议。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from utils.query_rewrite.rewriter import LC_SOURCE  # noqa: E402


class ScriptedStreamingModel(BaseChatModel):
    """按脚本依次返回回复、并且**真的逐段流式**输出的假模型。

    关键点:`_stream` 里调用 `on_llm_new_token`,这样它才会像真实模型那样触发
    下游的流式回调 —— 泄漏路径正是通过这个回调把 token 送出去的。
    """

    replies: list = []
    idx: int = 0
    stream_chunk: int = 8  # 每段字符数

    @property
    def _llm_type(self) -> str:
        return "scripted-streaming"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        i = min(self.idx, len(self.replies) - 1)
        object.__setattr__(self, "idx", self.idx + 1)
        return ChatResult(generations=[ChatGeneration(message=self.replies[i])])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs
                ) -> Iterator[ChatGenerationChunk]:
        i = min(self.idx, len(self.replies) - 1)
        object.__setattr__(self, "idx", self.idx + 1)
        msg = self.replies[i]
        text = msg.content if isinstance(msg.content, str) else ""
        for start in range(0, max(len(text), 1), self.stream_chunk):
            piece = text[start:start + self.stream_chunk]
            if not piece:
                continue
            if run_manager is not None:
                run_manager.on_llm_new_token(piece)
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        # 工具调用要作为一个整体 chunk 发出,避免被切碎后无法解析
        if getattr(msg, "tool_calls", None):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content="", tool_calls=msg.tool_calls,
            ))

    def _combine_llm_outputs(self, llm_outputs) -> dict:
        return {}


# 内部调用产出的"改写 JSON"(真实形状,含易泄漏的自检字段)
INNER_JSON = (
    '{"rewritten": "扫地机器人耗电快怎么办", "changed": true, '
    '"preserved_constraints": {"entities": ["扫地机器人"]}, '
    '"self_check": {"intent_same": true, "confidence": 0.95}, '
    '"decision": "use_rewritten"}'
)

# 用于让内部调用走"带 callbacks=[]"的正确姿势
_CAPTURED_CONFIG: dict = {}


class RecordingModel(ScriptedStreamingModel):
    """精确记录「调用方传给模型的 config」的假模型。

    做法:让 `bind` / `with_config` 都返回 **self**(而不是框架默认的 RunnableBinding),
    这样 `Rewriter._invoke` 里那一串 `bind().bind().with_config()` 之后拿到的还是本对象,
    最后 `invoke(..., config=...)` 就会原样落进本类,便于断言。

    为什么不读 contextvar:`callbacks=[]` 的生效机制是让 `_should_stream` 判定为 False
    (chat_models.py 里 `_should_stream` 只认 v1 流式 handler),而不是把 contextvar 里的
    callbacks 清掉 —— 所以 contextvar 在两种姿势下看到的都一样,不能作为判据。
    断言"传了什么 config"才是这一层的契约。
    """

    captured: dict = {}

    def bind(self, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self

    def invoke(self, messages, config=None, **kwargs):
        # 写模块级 dict 而非类属性:BaseChatModel 是 pydantic 模型,类属性会被它清掉
        _CAPTURED_CONFIG.clear()
        if config:
            _CAPTURED_CONFIG.update(config)
        return super().invoke(messages, config=config, **kwargs)


def _make_tool(inner_model_factory, *, clear_callbacks: bool):
    """造一个"内部会再调一次模型"的工具(复刻 search_knowledge_base 的形状)。"""

    @tool
    def leaky_search(query: str) -> str:
        """在知识库中检索(内部会调用一次改写模型)。"""
        inner = inner_model_factory()
        if clear_callbacks:
            # 正确姿势:显式清空 callbacks + 打上内部标记
            inner.invoke(
                [HumanMessage(content=query)],
                config={"callbacks": [], "metadata": {"lc_source": LC_SOURCE}},
            )
        else:
            # 错误姿势:不传 config → 继承 agent 的推流回调 → token 泄漏给前端
            inner.invoke([HumanMessage(content=query)])
        return "检索结果片段"

    return leaky_search


def _run_agent_and_collect(tool_fn) -> str:
    """用真实 create_agent 跑一轮,返回流出的全部文本(即"用户看到的回答")。"""
    outer = ScriptedStreamingModel(replies=[
        AIMessage(content="正在为您检索相关资料。\n\n", tool_calls=[
            {"name": tool_fn.name, "args": {"query": "耗电快咋办"}, "id": "t1"},
        ]),
        AIMessage(content="## 结论\n\n耗电快通常是吸力档位过高导致的。"),
    ], idx=0)
    agent = create_agent(model=outer, tools=[tool_fn], system_prompt="你是助手")
    out = []
    for chunk, _meta in agent.stream({"messages": [HumanMessage(content="耗电太快咋办")]},
                                     stream_mode="messages"):
        if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
            out.append(chunk.content)
    return "".join(out)


class TestNoLeakIntoAnswer(unittest.TestCase):
    """核心回归:内部模型调用的文本绝不能出现在用户看到的回答里。"""

    def test_inner_llm_output_leaks_when_callbacks_not_cleared(self):
        """先证明泄漏路径**真实存在** —— 不传 config 时内部 JSON 确实会混进回答。

        这条用例是"反向对照":它故意复刻出 bug 的写法。如果哪天它不再泄漏
        (例如框架改了继承行为),说明下面那条修复用例的前提变了,需要重新评估。
        """
        tool_fn = _make_tool(
            lambda: ScriptedStreamingModel(replies=[AIMessage(content=INNER_JSON)], idx=0),
            clear_callbacks=False,
        )
        answer = _run_agent_and_collect(tool_fn)
        self.assertIn("self_check", answer,
                      "预期复现泄漏(未传 config 时内部调用应继承推流回调);"
                      "若此断言失败,说明框架继承行为已变,请重新评估泄漏假设")

    def test_inner_llm_output_does_not_leak_when_callbacks_cleared(self):
        """修复后:显式传 callbacks=[] 的内部调用,其文本不得出现在回答里。"""
        tool_fn = _make_tool(
            lambda: ScriptedStreamingModel(replies=[AIMessage(content=INNER_JSON)], idx=0),
            clear_callbacks=True,
        )
        answer = _run_agent_and_collect(tool_fn)
        self.assertNotIn("self_check", answer, "改写 JSON 泄漏到了回答里")
        self.assertNotIn("preserved_constraints", answer, "改写 JSON 泄漏到了回答里")
        self.assertNotIn('"rewritten"', answer, "改写 JSON 泄漏到了回答里")
        # 正式回答与工具链路的文本必须还在
        self.assertIn("耗电快通常是吸力档位过高", answer)
        self.assertIn("正在为您检索相关资料", answer)

    def test_agent_stream_filter_skips_internal_nodes(self):
        """第二道防线:按 langgraph_node 丢弃工具节点产出的文本。

        为什么最终用 node 而不是 lc_source:实测(真 DashScope + 真 agent)流式分片的
        meta 里 metadata 是**空的** —— 自定义 lc_source 不会透到 stream_mode="messages",
        所以只按 lc_source 过滤实际拦不住。node 稳定可辨:'tools' 是工具内部辅助调用
        (泄漏源),'model' 才是主模型回答。
        """
        import ReAct.ReAct_Agent.agent as agent_mod

        # 正式回答:只有 model 节点
        self.assertFalse(agent_mod._is_internal_chunk({"langgraph_node": "model"}))
        # 泄漏源:工具节点
        self.assertTrue(agent_mod._is_internal_chunk({"langgraph_node": "tools"}))
        # 钩子节点也归入内部
        self.assertTrue(agent_mod._is_internal_chunk({"langgraph_node": "pre_model_hook"}))
        # 兜底:拿不到 node 时退回 lc_source 标记
        self.assertTrue(agent_mod._is_internal_chunk({"metadata": {"lc_source": LC_SOURCE}}))
        self.assertTrue(agent_mod._is_internal_chunk(
            {"metadata": {"lc_source": "summarization"}}))
        # 不能误伤:无 node 且无标记时放行
        self.assertFalse(agent_mod._is_internal_chunk({}))
        self.assertFalse(agent_mod._is_internal_chunk({"metadata": {}}))
        self.assertFalse(agent_mod._is_internal_chunk(None))
        self.assertFalse(agent_mod._is_internal_chunk("不是 dict"))

    def test_leaked_chunks_come_from_tools_node(self):
        """端到端确认过滤判据与实际泄漏源一致:泄漏分片的 node 必须是 'tools'。

        这条是把"为什么用 node 而不是 lc_source"钉死的证据 —— 若哪天泄漏改从
        model 节点出来了(例如调用姿势又变),agent 的过滤判据就得跟着改。
        """
        tool_fn = _make_tool(
            lambda: ScriptedStreamingModel(replies=[AIMessage(content=INNER_JSON)], idx=0),
            clear_callbacks=False,
        )
        outer = ScriptedStreamingModel(replies=[
            AIMessage(content="", tool_calls=[
                {"name": tool_fn.name, "args": {"query": "耗电快咋办"}, "id": "t1"}]),
            AIMessage(content="正式回答。"),
        ], idx=0)
        agent = create_agent(model=outer, tools=[tool_fn], system_prompt="你是助手")

        # 必须**按节点拼接**后再判断:流式分片是 8 字符一段,'self_check' 这类词
        # 会被切断('"self_ch' + 'eck": {'),逐片做子串匹配会漏判。
        by_node: dict = {}
        for chunk, meta in agent.stream({"messages": [HumanMessage(content="q")]},
                                        stream_mode="messages"):
            if not (isinstance(chunk, AIMessageChunk)
                    and isinstance(chunk.content, str) and chunk.content):
                continue
            node = (meta or {}).get("langgraph_node")
            by_node.setdefault(node, []).append(chunk.content)

        joined = {node: "".join(parts) for node, parts in by_node.items()}
        leaked_nodes = {n for n, text in joined.items() if "self_check" in text}
        kept_nodes = {n for n, text in joined.items() if "self_check" not in text}

        self.assertEqual(leaked_nodes, {"tools"},
                         "泄漏文本应只来自 tools 节点;若不然 agent 的过滤判据需调整")
        self.assertIn("model", kept_nodes, "正式回答应来自 model 节点")
        self.assertIn("正式回答", joined.get("model", ""), "model 节点应承载正式回答")


class TestRewriterInvokeContract(unittest.TestCase):
    """锁住 Rewriter 的调用姿势:这两行是防泄漏的第一道防线,别被"顺手简化"掉。"""

    def test_rewriter_invoke_passes_empty_callbacks_and_lc_source(self):
        from langchain_core.messages import AIMessage as _AI
        from utils.query_rewrite.rewriter import Rewriter

        model = RecordingModel(replies=[_AI(content=INNER_JSON)], idx=0)
        Rewriter(model=model).rewrite("耗电太快咋办")

        cfg = _CAPTURED_CONFIG
        self.assertTrue(cfg, "未能捕获到 config(调用姿势可能已改)")
        self.assertEqual(cfg.get("callbacks"), [],
                         "callbacks 必须显式清空,否则内部调用会继承 agent 的推流回调"
                         "→ 改写 token 被当成回答推给前端")
        self.assertEqual((cfg.get("metadata") or {}).get("lc_source"), LC_SOURCE,
                         "必须打上内部调用标记,供 agent 流式层兜底识别")


if __name__ == "__main__":
    unittest.main(verbosity=2)
