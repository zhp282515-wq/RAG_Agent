from typing import Dict

from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, HumanMessage
from model.modelfactory import chat_model
from utils.logger_tool import logger
from utils.logger_tool import fmt_duration
from utils.prompts_tool import get_prompt
from utils.path_tool import get_abs_path

from ReAct.tools.agent_tools import (
    get_rerank_retriever,
    get_weather,
    get_user_location,
    get_current_month,
    get_user_id,
    fetch_external_data,
    fill_context_for_report,
)
from ReAct.middleware.agent_middleware import (
    monitor_tool,
    log_befor_model,
    log_after_model,
    report_prompt_model,
)

import time
import uuid
import base64

# 会话持久化(MySQL)相关
from utils.mysql_tool import init_db, get_checkpointer
from utils.session_store import (
    get_or_create_session,
    append_message,
    list_sessions,
    get_session,
    delete_session,
    rename_session,
)
from utils.image_input_tool import (
    resolve_image_paths,
    parse_local_image,
    path_to_mime,
)

class ReActAgentService:

    def __init__(self, model: str | None = None):
        # 静态系统提示词在此初始化;报告场景由 report_prompt_model 中间件临时切换
        # model: 聊天模型名(web 模型切换传入;默认走 model.yml 的 model)
        self.model = model
        system_prompt = get_prompt(get_abs_path("prompts\\system_prompt.md"))
        self.tools = [
            get_rerank_retriever,
            get_weather,
            get_user_location,
            get_current_month,
            get_user_id,
            fetch_external_data,
            fill_context_for_report,
        ]
        self.middleware = [
            monitor_tool,
            log_befor_model,
            log_after_model,
            report_prompt_model,
        ]
        # 会话记忆持久化到 MySQL:每个会话窗口 = 一个 thread_id;跨进程重启仍可续聊
        try:
            init_db()
        except Exception as e:
            logger.error(f"ReActAgentService: 初始化 MySQL(agent_sessions)失败,将以无记忆模式运行:{e}")

        resolved = ""
        if model:
            try:
                from model.modelfactory import get_chat_model
                chat_m = get_chat_model(model)
                resolved = getattr(chat_m, "model_name", "") or model
            except Exception as e:
                logger.error(f"ReActAgentService: 按模型 {model} 建聊天模型失败,回退默认:{e}")
                chat_m = chat_model
                resolved = getattr(chat_model, "model_name", "") or ""
        else:
            chat_m = chat_model
            resolved = getattr(chat_model, "model_name", "") or ""

        self.agent = create_agent(
            model=chat_m,
            system_prompt=system_prompt,
            tools=self.tools,
            middleware=self.middleware,
            checkpointer=get_checkpointer(),
        )

        logger.info(f"ReActAgentService: agent 创建完成(model={resolved or self.model or '默认'} / {len(self.tools)} 个工具 / {len(self.middleware)} 个中间件 / 系统提示词已注入 / MySQL 会话持久化已启用)")

    # ---------- 会话(窗口)管理 ----------

    def create_or_get_session(self, session_id: str | None = None) -> dict:
        """新建(或续用)一个会话窗口,返回窗口信息 {session_id,title,...}。"""
        info = get_or_create_session(session_id)
        logger.info(f"create_or_get_session: 会话窗口 {info['session_id']} (title={info.get('title')})")
        return info

    def get_sessions(self) -> list[dict]:
        """列出全部会话窗口(按最近更新倒序)。"""
        return list_sessions()

    def get_session(self, session_id: str) -> dict | None:
        """取单个会话窗口及其消息记录。"""
        return get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """删除会话窗口:先清 langgraph 记忆(thread=session_id),再删业务表。"""
        deleted = delete_session(session_id)
        try:
            get_checkpointer().delete_thread(session_id)
        except Exception as e:
            logger.warning(f"delete_session: 清理 langgraph 记忆失败:{e}")
        return deleted

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话标题。"""
        ok = rename_session(session_id, title)
        if ok:
            logger.info(f"rename_session: {session_id} 标题已改为「{title}」")
        return ok

    # ---------- 核心:流式/一次性 问答(可带会话窗口、可带图片) ----------

    def _build_config(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}}

    def _ensure_session(self, session_id: str | None) -> str:
        """若缺省则自动新建会话窗口并复用;返回确定后的 session_id。"""
        info = self.create_or_get_session(session_id)
        return info["session_id"]

    def _resolve_images(self, image_sources) -> list[tuple[bytes, str]]:
        """把用户给的本地图片引用(路径/目录)解析为 [(bytes, mime)]。"""
        images: list[tuple[bytes, str]] = []
        if not image_sources:
            return images
        sources = image_sources if isinstance(image_sources, (list, tuple)) else [image_sources]
        for src in sources:
            for path in resolve_image_paths(src):
                img = parse_local_image(path)
                mime = path_to_mime(path)
                if img and mime:
                    images.append((img, mime))
        return images

    def _user_images_meta(self, image_sources) -> list[dict]:
        """组装落库用图片元数据(image 引用列表)。"""
        meta: list[dict] = []
        if not image_sources:
            return meta
        sources = image_sources if isinstance(image_sources, (list, tuple)) else [image_sources]
        import uuid as _uuid
        for src in sources:
            for path in resolve_image_paths(src):
                meta.append({
                    "id": uuid.uuid4().hex,
                    "file_name": path.replace("\\", "/").split("/")[-1],
                    "saved_path": path.replace("\\", "/"),
                    "mime": path_to_mime(path),
                })
        return meta

    def stream_output(
        self,
        query: str,
        config: Dict | None = None,
        *,
        session_id: str | None = None,
        image_sources=None,
        doc_texts=None,
        trace_holder: dict | None = None,
    ):
        """在指定会话窗口内流式问答:每次 yield 一段文本(真流式生成器)。

        纯文本与图片问答统一走本入口。传入 image_sources 时,文字 + 图片作为
        多模态消息一起进入 agent(图片由模型直接理解;带图片时仍可能触发知识库检索)。
        传入 doc_texts(聊天附件文档解析后的文本列表)时,它们随消息一并喂给模型
        作为对话资料(不入库、不进知识库)。
        同一 session_id 复用时带该窗口历史上下文续聊(记忆持久化在 MySQL)。

        用法:
            for piece in svc.stream_output("你好", session_id="s1"):
                print(piece, end="", flush=True)   # 由调用方自行消费/渲染

        Args:
            query: 用户问题(可为空,纯看图场景)
            config: 可选 langgraph 运行时配置(thread_id 等);提供时将忽略 session_id
            session_id: 会话窗口 id;None 则自动新建
            image_sources: 单个图片路径或路径列表
            doc_texts: 附件文档文本列表 [str,...];随本轮消息喂给模型,不入知识库
            trace_holder: web 层注入的收集器 dict(见 middleware.new_trace_holder);
                          中间件会把 trace 事件/检索来源/报告元信息写入其中,流结束后读取

        Yields:
            str: 模型生成的一段文本(token 粒度由底层 stream 决定)。全部文本收完后,
                会把拼接好的 assistant 回答落库。
        """
        t0 = time.perf_counter()
        sid = self._ensure_session(session_id)
        cfg = config or self._build_config(sid)

        images = self._resolve_images(image_sources)
        docs = [t for t in (doc_texts or []) if str(t).strip()]
        logger.info(f"stream_output: session={sid} 问题:{query[:100]!r} 图片数={len(images)} 附件文档数={len(docs)}")

        # 组装用户消息:文字 + 图片(多模态)+ 附件文档文本
        # 附件文档按固定分隔包好,让模型区分"资料"与"问题"
        if images or docs:
            content: list = []
            text_parts: list[str] = []
            if query and str(query).strip():
                text_parts.append(query)
            for di, d in enumerate(docs, 1):
                text_parts.append(f"\n\n--- 附件文档 {di} 内容开始 ---\n{d}\n--- 附件文档 {di} 内容结束 ---")
            if text_parts:
                content.append({"type": "text", "text": "\n".join(text_parts)})
            for img_bytes, mime in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(img_bytes).decode()}"},
                })
            user_msg = HumanMessage(content=content)
        else:
            user_msg = HumanMessage(query or "")

        # 用户问题与图片引用先行落库(回答完成后补 assistant 消息)
        # 注意:content 只存问题文本,附件文档文本不入库
        append_message(
            sid, "user",
            {"content": query or "", "images": self._user_images_meta(image_sources)},
            title_from=query,
        )

        # web 收集器(若注入)随 context 进入中间件;trace 事件/来源/报告元信息写入其中
        ctx: dict = {"report": False}
        if trace_holder is not None:
            from ReAct.middleware.agent_middleware import TRACE_KEY
            ctx[TRACE_KEY] = trace_holder

        answer_parts: list[str] = []
        try:
            for chunk, _meta in self.agent.stream(
                {"messages": [user_msg]},
                stream_mode="messages",
                config=cfg,
                context=ctx,
            ):
                if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
                    answer_parts.append(chunk.content)
                    yield chunk.content
        except Exception as e:
            logger.error(f"stream_output: agent 执行异常:{e}")
            err = f"\n【服务异常】{e}"
            answer_parts.append(err)
            yield err
        finally:
            answer = "".join(answer_parts)
            # 回答落库:assistant 消息(首问标题已由上面的 user 消息生成)
            # 附带本轮工作流(events)与检索来源(sources),供前端按回答回溯执行过程
            asst_payload: dict = {"content": answer}
            if trace_holder is not None:
                events = list(trace_holder.get("events", []))
                srcs = trace_holder.get("sources")
                if events:
                    asst_payload["workflow"] = events
                if srcs:
                    asst_payload["sources"] = srcs
            append_message(sid, "assistant", asst_payload)
            logger.info(f"stream_output: session={sid} 回答生成完成,总耗时 {fmt_duration(time.perf_counter()-t0)}")


if __name__ == "__main__":
    svc = ReActAgentService()

    # ===== 纯文本会话(自动新建/复用会话窗口,历史存 MySQL) =====
    # stream_output 是真流式生成器:逐段 yield,调用方边收边渲染
    print(">>> 纯文本流式问答")
    for piece in svc.stream_output(
        "不同天气下机器人怎么保养",
        session_id="afac805048fe47d5832d1756f42be7d0",   # 同一 id 复用会话历史
    ):
        print(piece, end="", flush=True)
    print()
