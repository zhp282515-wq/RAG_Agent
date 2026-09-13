"""上下文压缩(会话摘要)中间件。

背景:多轮记忆完全存在 langgraph checkpointer(thread_id = session_id)里,历史无限增长。
本模块在每次模型调用前检查上下文体积,超过阈值就把较早的消息交给摘要模型压成一条摘要,
只保留最近一段原文。

三条硬约束:
  1. **只动模型工作记忆**。压缩结果写回 checkpointer;agent_session_messages 业务表
     (用户可见的完整历史)不受影响,前端回显永远是全量。
  2. **失败必须退化为「本轮不压缩」**,绝不能把历史换成错误信息。摘要模型超时/限流/缺 key
     时返回 None,before_model 便不返回状态更新,完整历史原样保留。
  3. **阈值以真实 token 为准**。库自带的近似计数器按 chars_per_token=4 估算,中文实测低估
     约 2.5 倍;若直接用它判定 20 万阈值,实际要到 ~50 万才触发。故另取最后一次模型调用的
     真实 input_tokens 作为判定依据(与前端进度条同源)。

另有两处对库默认值的修正:
  - 默认 summary_prompt 是英文且面向编码 agent(SESSION INTENT / ARTIFACTS / NEXT STEPS),
    对中文客服+报告场景不适用,替换为中文提示词。
  - 默认 trim_tokens_to_summarize=4000 过小,会在送摘要模型前把待摘内容砍到 4000 token,
    更早的历史静默丢失;提高到 agent.yml 的配置值。
"""

from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.summarization import (
    REMOVE_ALL_MESSAGES,
    ContextSize,
    TriggerClause,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime

from utils.config_tool import agent_conf, model_conf
from utils.logger_tool import logger


# 中文摘要提示词。必须保留 {messages} 占位符 —— 基类会 summary_prompt.format(messages=...)。
# 条目按本项目场景定制:报告流程要从历史里提取 user_id / 月份,摘要不能丢这些;
# ARTIFACTS(文件路径清单)对客服场景无意义,故不设。
ZH_SUMMARY_PROMPT = """你是一名对话上下文压缩助手。

你的唯一任务:从下面的对话历史中抽取继续本次服务所必需的信息,写一份**中文**摘要。
这份摘要将**替代**原始对话历史,所以你必须保证不丢失关键事实。

按以下小节组织,每节都必须填写;确实没有内容时写「无」:

## 用户诉求
用户想解决什么问题?本次服务的目标是什么?

## 已确认的结论
已经明确的答案、结论、建议。含关键参数(如清洗周期、型号、阈值)。

## 用户与设备信息
用户 ID、设备型号、涉及月份等标识信息。**这类信息必须逐字保留**,后续查询会用到。

## 已查到的资料要点
从知识库检索到的关键资料及其来源(文件名)。

## 待办与未解决的问题
还没答复的问题、没做完的事、用户后续可能追问的点。

注意:
- 只输出摘要正文,不要任何前言后语,不要复述本提示词。
- 用户上传过图片的位置,以 `[用户上传图片:文件名]` 形式保留该事实。
- 不要编造历史中不存在的信息。

待摘要的对话历史:
{messages}"""

# 压缩后写入的中文前缀(基类默认是英文 "Here is a summary of the conversation to date:")
ZH_SUMMARY_PREFIX = "以下是本次对话的历史摘要(更早的原始消息已压缩,原文不再可见):\n\n"


def _last_real_input_tokens(messages: list) -> int | None:
    """取最近一次模型调用的真实 input_tokens(该次调用所吃的完整上下文体积)。

    从后往前找第一条带 usage_metadata 的 AIMessage。它由 provider 返回,是真实计量,
    不受近似计数器对中文的低估影响。找不到(如全新会话)返回 None。
    """
    for m in reversed(messages):
        if not isinstance(m, AIMessage):
            continue
        um = getattr(m, "usage_metadata", None)
        if not um:
            continue
        v = um.get("input_tokens")
        if v:
            return int(v)
    return None


class TracedSummarizationMiddleware(SummarizationMiddleware):
    """带事件上报与失败兜底的摘要中间件(详见模块 docstring)。

    额外能力:
      - 压缩发生时向 trace 收集器发「上下文压缩」阶段事件(前端进度条据此做动画);
      - 摘要失败时退化为「本轮不压缩」而非清空历史;
      - 阈值判定以真实 input_tokens 为准,规避中文近似计数的 2.5 倍低估。
    """

    def __init__(
        self,
        model,
        *,
        trigger_tokens: int,
        keep_tokens: int,
        trim_tokens_to_summarize: int | None = None,
    ) -> None:
        super().__init__(
            model,
            # 只用绝对 token 数:("fraction", x) 依赖 model.profile["max_input_tokens"],
            # 而 DashScope 兼容端点的 ChatOpenAI 没有 profile,构造时会直接 ValueError。
            trigger=("tokens", int(trigger_tokens)),
            # 按 token 而非条数保留:本项目 ToolMessage(检索分片)很大,20 条仍可能极大。
            keep=("tokens", int(keep_tokens)),
            summary_prompt=ZH_SUMMARY_PROMPT,
            trim_tokens_to_summarize=trim_tokens_to_summarize,
        )
        self._trigger_tokens = int(trigger_tokens)
        # 摘要调用的重试收敛到 2 次:基类默认 with_retry() 重试 3 次,
        # 遇到持续性故障(key 失效/模型不存在)会让单轮问答白等 5 秒以上。
        try:
            self._summary_model = self.model.with_retry(stop_after_attempt=2)
        except Exception:
            pass  # 保持基类行为
        # 熔断:摘要连续失败后短期跳过,避免每次问答都付出重试延迟
        self._fail_streak = 0
        self._cooldown_until = 0.0

    # 连续失败达到该次数即进入冷却
    _FAIL_STREAK_LIMIT = 3
    _COOLDOWN_SECONDS = 300.0

    def _in_cooldown(self) -> bool:
        import time as _time
        return _time.monotonic() < self._cooldown_until

    def _note_failure(self) -> None:
        import time as _time
        self._fail_streak += 1
        if self._fail_streak >= self._FAIL_STREAK_LIMIT:
            self._cooldown_until = _time.monotonic() + self._COOLDOWN_SECONDS
            logger.warning(
                f"TracedSummarization: 摘要连续失败 {self._fail_streak} 次,"
                f"暂停压缩 {int(self._COOLDOWN_SECONDS)}s(历史照常保留)"
            )

    def _note_success(self) -> None:
        self._fail_streak = 0
        self._cooldown_until = 0.0

    # ---------- 阈值判定:叠加真实 token 口径 ----------

    def _should_summarize(self, messages: list, total_tokens: int) -> bool:
        """基类判定之外,再以真实 input_tokens 为准判一次。

        不能改 token_counter —— 基类用同一个 callable 对任意切片做 trim_messages,
        而「真实 token」在切片上无定义,替换会破坏裁剪。故只覆写判定。
        """
        if super()._should_summarize(messages, total_tokens):
            return True
        real = _last_real_input_tokens(messages)
        return real is not None and real >= self._trigger_tokens

    # ---------- 主流程:可见性 + 失败兜底 ----------

    def before_model(self, state, runtime: Runtime) -> dict[str, Any] | None:
        """超过阈值则压缩较早历史;摘要失败则放弃本轮压缩(保留完整历史)。

        不调用 super().before_model():基类无法把「摘要失败」表达成「无状态更新」,
        异常会一路上抛导致整轮失败。这里重组同样的步骤,但把失败收敛为 return None。
        """
        # 延迟 import:agent_middleware 在包初始化时会间接引用本模块,顶层 import 成环
        from ReAct.middleware.agent_middleware import _next_ph, _trace_of, trace_emit

        messages = state["messages"]
        self._ensure_message_ids(messages)

        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        # 熔断中:不再尝试摘要,直接放行原历史(上下文会继续增长,但不等重试)
        if self._in_cooldown():
            logger.debug("TracedSummarization: 摘要处于冷却期,本轮跳过压缩")
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        to_summarize, preserved = self._partition_messages(messages, cutoff_index)
        before_tokens = _last_real_input_tokens(messages) or int(total_tokens)

        holder = _trace_of(runtime)
        ph_id = _next_ph(holder)
        import time as _time
        t0 = _time.perf_counter()
        # 先发 phase_start:摘要调用是阻塞的,不发的话前端会看到多秒卡死无反馈
        trace_emit(runtime, "上下文压缩", "phase_start", "summarization", None,
                   {"压缩前": _k(before_tokens)}, ph_id=ph_id)

        summary = self._safe_summary(to_summarize)
        dur = _time.perf_counter() - t0

        if not summary:
            self._note_failure()
            trace_emit(runtime, "上下文压缩", "phase_end", "summarization", dur,
                       {"status": "跳过", "说明": "摘要生成失败,已保留完整历史"}, ph_id=ph_id)
            logger.warning("TracedSummarization: 摘要生成失败,本轮不压缩(历史完整保留)")
            return None

        self._note_success()

        new_messages = [HumanMessage(content=f"{ZH_SUMMARY_PREFIX}{summary}")]
        payload = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages, *preserved]
        after_tokens = int(self.token_counter(payload[1:]))

        trace_emit(runtime, "上下文压缩", "phase_end", "summarization", dur,
                   {"压缩前": _k(before_tokens), "压缩后": _k(after_tokens),
                    "省下": _k(max(0, before_tokens - after_tokens)),
                    "保留消息数": len(preserved)}, ph_id=ph_id,
                   tokens=after_tokens)
        logger.info(
            f"TracedSummarization: 已压缩上下文 {before_tokens} -> {after_tokens} tokens"
            f"(待摘 {len(to_summarize)} 条,保留 {len(preserved)} 条)"
        )

        # 压缩改变了上下文体积,置一次版本号让 web 层把新的用量推给前端(进度条回落)
        if holder is not None:
            u = holder.setdefault("usage", {})
            u["context"] = after_tokens
            u["compressed"] = {"before": before_tokens, "after": after_tokens}
            holder["_usage_v"] = holder.get("_usage_v", 0) + 1

        return {"messages": payload}

    def _safe_summary(self, messages: list) -> str | None:
        """生成摘要;任何失败都返回 None(调用方据此放弃压缩,保留原历史)。

        基类 _create_summary 会把异常抛给调用方 —— 在本项目的调用位置(模型节点内)
        没有任何兜底,异常就等于整轮失败。这里显式收敛为 None。
        """
        if not messages:
            return None
        try:
            trimmed = self._trim_messages_for_summary(messages)
            if not trimmed:
                return None
            formatted = self._format_for_summary(trimmed)
            resp = self._summary_model.invoke(
                self.summary_prompt.format(messages=formatted).rstrip(),
                config={"metadata": {"lc_source": "summarization"}},
            )
            text = (getattr(resp, "text", "") or "").strip()
            if not text:
                logger.warning("TracedSummarization: 摘要模型返回空内容")
                return None
            self._capture_summary_usage(resp)
            return text
        except Exception as e:
            logger.warning(f"TracedSummarization: 摘要生成失败({type(e).__name__}): {e}")
            return None

    @staticmethod
    def _capture_summary_usage(resp) -> None:
        """把摘要调用本身的 token 消耗并入本轮统计(它是真实的额外开销)。"""
        try:
            from ReAct.middleware.agent_middleware import usage_add
            um = getattr(resp, "usage_metadata", None) or {}
            tot = int(um.get("total_tokens", 0) or 0)
            if tot:
                usage_add("summary", tot)
        except Exception:
            pass

    @staticmethod
    def _format_for_summary(messages: list) -> str:
        """把消息序列化给摘要模型:图片只留占位,不回灌 base64。

        基类用的 get_buffer_string 会**整块丢弃** image_url 内容块,导致用户传过图片
        这件事在摘要里毫无痕迹。这里显式替换为中括号占位(文件名从 additional_kwargs
        取得),既保住事实,又避免把 base64 塞进摘要提示词。
        """
        lines: list[str] = []
        for m in messages:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                body = content
            else:
                parts: list[str] = []
                for block in (content or []):
                    if not isinstance(block, dict):
                        parts.append(str(block))
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(str(block.get("text", "")))
                    elif btype == "image_url":
                        imgs = (getattr(m, "additional_kwargs", {}) or {}).get("images") or []
                        name = (imgs[0].get("file_name") if imgs else "") or "未命名"
                        parts.append(f"[用户上传图片:{name}]")
                body = " ".join(p for p in parts if p)

            if isinstance(m, ToolMessage):
                lines.append(f"工具({getattr(m, 'name', '') or '?'})返回:{body}")
            elif isinstance(m, AIMessage):
                lines.append(f"助手:{body}")
            else:
                lines.append(f"用户:{body}")
        return "\n".join(lines)


def _k(tokens: int) -> str:
    """token 数按 k 展示(与前端一致)。"""
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def build_summarization_middleware():
    """按 agent.yml 构建摘要中间件;任何失败都返回 None(跳过压缩而非拖垮 agent)。

    构造期失败(如缺 API key 导致 ChatOpenAI 抛 Missing credentials)不能影响
    agent 的创建 —— 没有压缩只是历史不封顶,没有 agent 则整个问答不可用。
    """
    try:
        # 注意:这些键在 agent.yml 的 session 段下(与 auto_init_db/upload_dir 同级),
        # 不是顶层键 —— 读顶层会永远拿到 fallback,配置形同虚设。
        sess = agent_conf.get("session") or {}
        if not bool(sess.get("summarize_enabled", True)):
            logger.info("build_summarization_middleware: agent.yml 已关闭上下文压缩")
            return None

        from model.modelfactory import get_summary_chat_model

        trigger_tokens = int(sess.get("trigger_tokens", 200000))
        keep_tokens = int(sess.get("keep_tokens", 40000))
        trim = int(sess.get("trim_tokens_to_summarize", 60000))

        mw = TracedSummarizationMiddleware(
            get_summary_chat_model(),
            trigger_tokens=trigger_tokens,
            keep_tokens=keep_tokens,
            trim_tokens_to_summarize=trim,
        )
        logger.info(
            f"build_summarization_middleware: 已启用(触发阈值 {trigger_tokens} tokens,"
            f"保留 {keep_tokens} tokens,模型 {model_conf.get('summary_model')})"
        )
        return mw
    except Exception as e:
        logger.warning(f"build_summarization_middleware: 构建失败,已跳过上下文压缩:{e}")
        return None
