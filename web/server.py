"""RAG Agent Web 后端(FastAPI):静态资源 + 会话/上传/聊天SSE/报告/检索调试/文档管理/报告记录。

契约见同目录 web/API_SPEC.md。启动方式:
    uvicorn web.server:app --host 127.0.0.1 --port 8001
或直接:
    python web/server.py
"""
import asyncio
import json
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 项目根目录加入 sys.path(uvicorn 从任意 cwd 启动都能 import 项目模块)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from utils.logger_tool import logger
from utils.path_tool import get_abs_path
from utils.mysql_tool import init_db, get_connection
from ReAct.ReAct_Agent.agent import ReActAgentService
from ReAct.middleware.agent_middleware import new_trace_holder
from vector_store.vector_store import VectorStoreService

# ---------------- 常量 ----------------
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")
UPLOAD_DIR = get_abs_path("data\\uploads")
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 相关度分级(与 ReAct/tools/agent_tools.py 校准一致):高 ≥0.85;中 ≥0.60;<0.60 不返回
_SCORE_HIGH = 0.85
_SCORE_MIN = 0.60
# sources 推送的正文预览最大字符数(前端溯源记录面板展示用)
_SOURCE_TEXT_CHARS = 500

# 上传白名单与大小限制(图片上限与 image_input_tool.MAX_IMAGE_BYTES 一致)
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DOC_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".csv", ".xlsx", ".xls"}
_MAX_IMG_BYTES = 10 * 1024 * 1024
_MAX_DOC_BYTES = 50 * 1024 * 1024


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _label(score: float) -> str:
    return "高" if score >= _SCORE_HIGH else "中"


# ---------------- 服务单例(惰性,首次请求才初始化重型组件) ----------------
# agent 按聊天模型缓存:同一模型复用同一个 ReActAgentService(避免反复重建 agent)
_agent_cache: dict[str, ReActAgentService] = {}
_vector_svc: VectorStoreService | None = None


def _get_agent(model: str | None = None) -> ReActAgentService:
    key = model or ""
    svc = _agent_cache.get(key)
    if svc is None:
        svc = ReActAgentService(model=model or None)
        _agent_cache[key] = svc
    return svc


def _get_vector() -> VectorStoreService:
    global _vector_svc
    if _vector_svc is None:
        _vector_svc = VectorStoreService()
    return _vector_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 确保业务表/checkpointer/reports 表就绪(agent 懒加载前报告页也可能被访问)
    try:
        init_db()
        logger.info("server: MySQL 初始化完成(agent_sessions + reports)")
    except Exception as e:
        logger.error(f"server: 初始化 MySQL 失败:{e}")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    yield


app = FastAPI(title="RAG Agent Web", lifespan=lifespan)

# 统一错误格式为契约约定的 {"error": "..."}
@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _generic_exc_handler(request: Request, exc: Exception):
    logger.error(f"server: 未捕获异常 {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse({"error": f"服务器内部错误:{exc}"}, status_code=500)


# ---------------- 静态资源 ----------------
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """开发期禁用浏览器缓存,改完刷新即生效(静态资源/页面/接口全量 no-cache)。"""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    # 服务端 302 到带版本参数的页面(比本地跳转页更可靠:浏览器直接发全新请求,绕开缓存)
    return RedirectResponse("/static/index.html?v=27")


# ---------------- 工具函数 ----------------
def _sse(payload: dict) -> str:
    """把一个事件对象序列化为 SSE data 行(契约:每事件 `data: {json}\n\n`)。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _save_upload(file: UploadFile) -> str:
    """把上传文件落盘到 data/uploads(重名自动加序号),返回本地绝对路径。"""
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in _IMG_EXT and ext not in _DOC_EXT:
        raise HTTPException(400, f"不支持的文件格式 .{ext or '(无扩展名)'}")
    limit = _MAX_IMG_BYTES if ext in _IMG_EXT else _MAX_DOC_BYTES

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stem, e = os.path.splitext(name)
    dest = os.path.join(UPLOAD_DIR, name)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(UPLOAD_DIR, f"{stem}_{n}{e}")
        n += 1

    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(400, f"文件超过大小限制({limit // (1024 * 1024)}MB)")
                out.write(chunk)
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    logger.info(f"_save_upload: 已保存 {name} -> {dest} ({size} 字节)")
    return dest


def _resolve_upload_path(p: str) -> str | None:
    """解析前端传来的上传路径(绝对路径 / data\\uploads 相对路径 / 纯文件名)。"""
    if not p:
        return None
    for cand in (
        os.path.abspath(p),
        get_abs_path(p),
        os.path.join(UPLOAD_DIR, os.path.basename(p)),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _extract_report_title(md: str) -> str | None:
    """从报告 Markdown 全文提取标题:首个 # 标题,否则首个非空行。"""
    if not md:
        return None
    first_line = None
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        if first_line is None:
            first_line = s
        if s.startswith("#"):
            t = s.lstrip("#").strip()
            return t[:80] if t else None
    return first_line[:80] if first_line else None


def _save_report(holder: dict, content: str,
                 fallback_user: str | None = None, fallback_month: str | None = None) -> None:
    """报告生成完成后自动保存到 reports 表(契约第 7 节)。"""
    meta = holder.get("meta", {})
    user_id = meta.get("user_id") or fallback_user or ""
    month = meta.get("month") or fallback_month or ""
    title = _extract_report_title(content) or (f"{month} 使用报告" if month else "使用报告")
    now = _now()
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reports (user_id, month, title, content, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, month, title, content, now, now),
            )
        logger.info(f"_save_report: 已自动保存报告「{title}」(user={user_id or '未知'}, month={month or '未知'}, {len(content)} 字)")
    except Exception as e:
        logger.error(f"_save_report: 报告保存失败:{e}")


# ---------------- SSE 聊天生成器 ----------------
async def _chat_event_gen(query: str, session_id: str | None,
                          image_paths: list, doc_paths: list,
                          *, report_meta: dict | None = None, model: str | None = None):
    """把 agent 流式输出包装为 SSE 事件序列(trace / sources / token / done / error)。

    为了让 trace 事件(工作流面板)在模型"思考/检索"期间也能实时到达(而不是等
    下一个文本 token 才一起带出),这里用一个后台线程跑同步生成器 stream_output,
    把 token 逐段放进 asyncio.Queue;主协程以高频短循环持续 drain 中间件写入的
    trace holder 增量 + 队列文本 → 真正动态地推送工作流进展。
    """
    holder = new_trace_holder()
    if report_meta:
        holder["meta"].update(report_meta)

    # 聊天附带的文档:按格式解析成文本随消息喂给模型(不入知识库、不走入库去重)
    doc_texts: list[str] = []
    if doc_paths:
        vec = _get_vector()
        for p in doc_paths:
            path = _resolve_upload_path(str(p))
            if not path:
                logger.warning(f"chat: 附件文档路径无法解析,忽略: {p}")
                continue
            try:
                text = vec.preview_text(path, max_chars=8000)
                if text.strip():
                    doc_texts.append(f"[附件 {os.path.basename(path)}]\n{text}")
                else:
                    logger.warning(f"chat: 附件文档解析无文本,忽略: {path}")
            except Exception as e:
                logger.error(f"chat: 附件文档解析失败 {path}: {e}")

    svc = _get_agent(model)
    q: asyncio.Queue = asyncio.Queue()
    sent_events = 0
    sent_sources = None
    answer_parts: list[str] = []

    def drain_trace() -> list[dict]:
        nonlocal sent_events
        events = holder["events"]
        out = events[sent_events:]
        sent_events = len(events)
        return out

    def drain_sources() -> list[dict] | None:
        nonlocal sent_sources
        srcs = holder["sources"]
        if srcs is not None and srcs is not sent_sources:
            sent_sources = srcs
            return srcs
        return None

    def producer():
        """后台线程:消费同步生成器,产出 ('token', str) / ('done', None) / ('err', str)。"""
        try:
            it = svc.stream_output(
                query, session_id=session_id,
                image_sources=image_paths or None,
                doc_texts=doc_texts or None,
                trace_holder=holder,
            )
            try:
                for piece in it:
                    if isinstance(piece, str) and piece:
                        q.put_nowait(("token", piece))
            finally:
                try:
                    it.close()
                except (ValueError, RuntimeError):
                    pass
            q.put_nowait(("done", None))
        except Exception as e:
            logger.error(f"chat: 流式生成异常: {e}")
            q.put_nowait(("err", str(e)))

    # 后台线程跑同步生成器;异常/结束时它总会向队列放终止标记
    loop = asyncio.get_running_loop()
    thr = threading.Thread(target=producer, daemon=True)
    thr.start()

    try:
        while True:
            # 每次循环都先 drain 当前已发生的 trace/sources(不依赖是否有 token),
            # 再尽量取一批文本 → 工作流步骤能实时推进,而非挤在文本间隙
            for ev in drain_trace():
                yield _sse({"trace": ev})
            srcs = drain_sources()
            if srcs is not None:
                yield _sse({"sources": srcs})

            try:
                kind, payload = await asyncio.wait_for(q.get(), timeout=0.15)
            except asyncio.TimeoutError:
                # 队列暂时无文本(模型思考中)→ 继续 drain trace 后短暂等待
                continue

            if kind == "token":
                answer_parts.append(payload)
                yield _sse({"token": payload})
                continue
            if kind == "err":
                yield _sse({"error": payload})
                return
            if kind == "done":
                break
    finally:
        # 生产线程是 daemon,主生成器退出即可,无需 join
        pass

    # 收尾:剩余 trace/sources + 完成事件;报告场景自动落库
    for ev in drain_trace():
        yield _sse({"trace": ev})
    srcs = drain_sources()
    if srcs is not None:
        yield _sse({"sources": srcs})
    answer = "".join(answer_parts)
    if holder["report"] and answer.strip():
        _save_report(holder, answer)
    yield _sse({"done": True})


def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- 1. 会话 ----------------
@app.get("/api/sessions")
def api_sessions():
    """会话列表(按 updated_at 倒序)。"""
    return {"sessions": _get_agent().get_sessions()}


@app.post("/api/sessions")
def api_create_session():
    """新建会话(后端复用已有的空会话,保证不产生第二个空会话)。"""
    return {"session": _get_agent().create_or_get_session(None)}


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str):
    """会话详情(含历史消息)。"""
    s = _get_agent().get_session(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    return {"session": s}


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str):
    """删除会话(连同 langgraph 记忆)。"""
    if not _get_agent().delete_session(session_id):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.put("/api/sessions/{session_id}")
def api_rename_session(session_id: str, body: dict):
    """重命名会话。"""
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if not _get_agent().rename_session(session_id, title):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


# ---------------- 2. 附件上传(聊天图片/文档) ----------------
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """上传图片或文档到 data/uploads,返回本地路径(随聊天请求传入)。"""
    if not file.filename:
        raise HTTPException(400, "缺少文件")
    path = _save_upload(file)
    return {"path": path}


# ---------------- 3. 聊天(SSE 流式) ----------------
@app.post("/api/chat")
async def api_chat(body: dict):
    query = str(body.get("query") or "")
    session_id = str(body.get("session_id") or "").strip() or None
    image_paths = [str(p) for p in (body.get("image_paths") or [])]
    doc_paths = [str(p) for p in (body.get("doc_paths") or [])]
    model = str(body.get("model") or "").strip() or None
    if not query and not image_paths:
        raise HTTPException(400, "query 与 image_paths 不能同时为空")
    if session_id is None:
        raise HTTPException(400, "session_id 不能为空(请先 POST /api/sessions)")
    return _sse_response(_chat_event_gen(query, session_id, image_paths, doc_paths, model=model))


# ---------------- 3.5 模型列表 ----------------
@app.get("/api/models")
def api_models():
    """可选聊天模型清单(web 模型选择用)。"""
    from model.modelfactory import model_conf
    cur = model_conf.get("model", "") or ""
    # init_chat_model 用 openai:qwen3.8-flash 前缀,展示时去掉
    short = cur.split(":", 1)[-1] if ":" in cur else cur
    names = ["qwen3.7-max", "qwen3.8-max", "qwen3.8-flash"]
    if short not in names:
        names.insert(0, short)
    return {"models": [
        {"model": n, "label": n, "current": n == short}
        for n in names
    ]}


# ---------------- 4. 报告(SSE 流式) ----------------
@app.post("/api/report")
async def api_report(body: dict):
    user_id = str(body.get("user_id") or "").strip()
    month = str(body.get("month") or "").strip()
    if not user_id or not month:
        raise HTTPException(400, "必须提供 user_id 和 month(YYYY-MM)")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "month 格式应为 YYYY-MM(如 2026-08)")
    query = f"用户ID是{user_id},帮我生成{month}的使用报告"
    return _sse_response(
        _chat_event_gen(query, None, [], [], report_meta={"user_id": user_id, "month": month})
    )


# ---------------- 5. 检索调试(同步 JSON) ----------------
@app.post("/api/search")
def api_search(body: dict):
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    try:
        top_k = int(body.get("top_k") or 20)
    except (TypeError, ValueError):
        raise HTTPException(400, "top_k 必须为整数")
    top_k = max(1, min(top_k, 100))

    # 与 agent 工具同一条检索链路(向量粗排 + rerank 精排),分数体系与阈值一致
    hits = _get_vector().get_rerank_retriever(top_k=top_k)(query)
    out = []
    for h in hits:
        score = float(h.get("score", 0))
        if score < _SCORE_MIN:
            continue
        out.append({
            "score": round(score, 3),
            "label": _label(score),
            "file_name": h.get("file_name", ""),
            "page": h.get("page"),
            "chapter": h.get("chapter", "") or "",
            "section": h.get("section", "") or "",
            "text": (h.get("text", "") or "")[:_SOURCE_TEXT_CHARS],
        })
    logger.info(f"api_search: query={query[:50]!r} top_k={top_k} 返回 {len(out)} 条")
    return {"hits": out}


# ---------------- 6. 文档管理(知识库) ----------------
@app.post("/api/documents")
async def api_upload_document(file: UploadFile = File(...)):
    """上传文档并入库(解析→分片→向量化,耗时操作)。"""
    if not file.filename:
        raise HTTPException(400, "缺少文件")
    path = _save_upload(file)
    vec = _get_vector()

    if vec.check_md5_hex(path):
        logger.info(f"api_upload_document: {os.path.basename(path)} 已存在(md5 去重)")
        return {"ok": False, "file_name": os.path.basename(path),
                "message": "该文档已存在(md5 去重)"}

    if not vec.load_document(path):
        raise HTTPException(400, f"文档解析或入库失败:{os.path.basename(path)}")

    chunk_count = 0
    for d in vec.list_documents():
        if d["file_name"] == os.path.basename(path):
            chunk_count = d["chunk_count"]
            break
    return {"ok": True, "file_name": os.path.basename(path),
            "chunk_count": chunk_count, "message": "已入库"}


@app.get("/api/documents")
def api_list_documents():
    """已入库文档列表:文件名/格式/大小/分片数。"""
    return {"documents": _get_vector().list_documents()}


@app.get("/api/documents/preview/{file_name:path}")
def api_preview_document(file_name: str):
    """文档预览:按文件名定位原始文件,解析并返回开头文本。"""
    vec = _get_vector()
    target = None
    for d in vec.list_documents():
        if d["file_name"] == file_name:
            target = d
            break
    if not target:
        raise HTTPException(404, "文档不存在")
    # 优先库中记录的存储路径,兜底按文件名在 uploads 下查找
    path = _resolve_upload_path(target.get("file_path") or file_name)
    if not path:
        raise HTTPException(404, "文档原始文件已不存在,无法预览")
    text = vec.preview_text(path)
    return {"file_name": file_name, "text": text}


@app.get("/api/documents/chunks/{file_name:path}")
def api_document_chunks(file_name: str):
    """按分片预览:返回该文档在向量库中的全部分片(含页码/章节)。"""
    vec = _get_vector()
    exists = any(d["file_name"] == file_name for d in vec.list_documents())
    if not exists:
        raise HTTPException(404, "文档不存在")
    chunks = vec.list_chunks(file_name)
    return {"file_name": file_name, "chunk_count": len(chunks), "chunks": chunks}


@app.delete("/api/documents/{file_name:path}")
def api_delete_document(file_name: str):
    """按文档名从向量库删除(连同其 md5 记录)。"""
    if not _get_vector().del_document(file_name):
        raise HTTPException(404, "文档不存在")
    return {"ok": True}


# ---------------- 7. 报告记录(MySQL 持久化) ----------------
@app.get("/api/reports")
def api_list_reports():
    """报告列表(按创建时间倒序,不含正文)。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT report_id, user_id, month, title, created_at, updated_at "
            "FROM reports ORDER BY created_at DESC, report_id DESC"
        )
        return {"reports": list(cur.fetchall())}


@app.get("/api/reports/{report_id}")
def api_get_report(report_id: int):
    """报告详情(含 Markdown 全文)。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM reports WHERE report_id = %s", (report_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "报告不存在")
    return {"report": row}


@app.put("/api/reports/{report_id}")
def api_update_report(report_id: int, body: dict):
    """编辑保存:更新 title 和/或 content。"""
    title = body.get("title")
    content = body.get("content")
    fields, params = [], []
    if title is not None:
        fields.append("title = %s")
        params.append(str(title))
    if content is not None:
        fields.append("content = %s")
        params.append(str(content))
    if not fields:
        raise HTTPException(400, "缺少 title 或 content")
    fields.append("updated_at = %s")
    params.append(_now())
    params.append(report_id)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE reports SET {', '.join(fields)} WHERE report_id = %s",
            tuple(params),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "报告不存在")
    return {"ok": True}


@app.delete("/api/reports/{report_id}")
def api_delete_report(report_id: int):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM reports WHERE report_id = %s", (report_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "报告不存在")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    logger.info("server: 启动 RAG Agent Web 服务 http://127.0.0.1:8001")
    uvicorn.run(app, host="127.0.0.1", port=8001)
